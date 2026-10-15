"""ATO domain pipeline — score a login, then submit it to the ledger.

``run_ato_decision()`` runs the ATO-specific portion of a decision against a
single ``LoginEvent``: feature computation, scoring, and submission assembly
(which picks the mode from ATO's bypass rules and renders the policy gate's
retrieval query into its gate context). It then hands the ``Submission`` to
the ledger through a ``LedgerClient`` (DR-31): ``LocalClient`` when ATO runs
the ledger in its own process, ``HttpClient`` when it calls a ledger service.
The ledger runs the decision's gate set — the advisory policy gate only when
ATO asks for a gate, plus any gate the deployment mandates — enforces, and
writes the bundle; ATO gets the receipt back.

This split keeps the framework domain-agnostic: the ledger receives a
``Submission`` and never imports anything from ``ato_reasoner``.

Async (DR-23): the await is the ledger call (the gates run inside it).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from app.monitoring import duration_ms
from ato_reasoner.assembler import build_submission

if TYPE_CHECKING:
    from app.api.models import DecisionReceipt
    from app.client import LedgerClient
    from ato_reasoner.events import LoginEvent
    from ato_reasoner.plugin import AtoServices

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AtoDecision:
    """One login's decision: the ledger's receipt and ATO's own timings.

    Args:
        receipt: What the ledger decided (``DecisionReceipt``); the full
            bundle is one ``client.get_bundle(receipt.decision_id)`` away.
        latency_ms: ATO's steps in milliseconds: ``features_ms``,
            ``scorer_ms``, and ``ledger_ms`` (the ledger call, round trip).
    """

    receipt: DecisionReceipt
    latency_ms: dict[str, float] = field(default_factory=dict)

    @property
    def total_ms(self) -> float:
        """Wall-clock time of the whole decision, ATO's steps and the ledger's."""
        return round(sum(self.latency_ms.values()), 1)


async def run_ato_decision(
    *,
    event: LoginEvent,
    services: AtoServices,
    client: LedgerClient,
) -> AtoDecision:
    """Score one ``LoginEvent`` and submit it to the ledger.

    Domain steps (features → score → assemble) execute first, then the
    ledger decides (gate set → enforce → bundle → persist).

    Args:
        event: Validated login event to evaluate.
        services: ATO's own services — feature service, scorer, and the
            corpus version stamp.
        client: The ledger, in process or over HTTP. The ledger must
            register ATO (its plugin installed, or its registration file).

    Returns:
        The ledger's receipt and ATO's step timings.

    Raises:
        LedgerError: The ledger refused the submission.

    Side effects:
        - Updates Redis sliding-window features for the event's entity.
        - When ATO asks for a gate, the ledger's policy gate retrieves from
          its own stores and calls the LLM API.
        - The ledger writes the bundle.
    """
    latency: dict[str, float] = {}

    # --- Features ---
    t0 = time.perf_counter()
    features = services.feature_svc.compute(event)
    latency["features_ms"] = duration_ms(t0)

    # --- Scorer ---
    t0 = time.perf_counter()
    scorer_output = services.scorer.score(features)
    latency["scorer_ms"] = duration_ms(t0)

    # --- Assembly (mode, gate context, and the domain-owned retrieval query) ---
    submission = build_submission(
        event, features, scorer_output, corpus_version=services.corpus_version
    )

    # --- Hand off to the ledger ---
    t0 = time.perf_counter()
    receipt = await client.submit(submission)
    latency["ledger_ms"] = duration_ms(t0)

    log.info(
        "ato.pipeline.decided",
        component="ato_pipeline",
        event_id=event.event_id,
        decision_id=receipt.decision_id,
        mode=receipt.mode,
        decision_action=receipt.decision_action.value,
        **latency,
    )
    return AtoDecision(receipt=receipt, latency_ms=latency)
