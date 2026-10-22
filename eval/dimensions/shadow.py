"""Shadow evaluation — recorded decisions re-decided with their advisory gates.

A reasoner that *records* a decision (DR-30) skips its advisory gates: its
registered bypass rule is the justification. Shadow evaluation asks what
the ledger would have decided had the reasoner asked for a gate instead. It
samples a reasoner's recorded bundles and, for each, re-decides it offline:

1. the stored submission becomes a gate request with no proposal (the
   shape the reasoner would have sent);
2. the reasoner's advisory gates run on it, each with its stored gate
   context, through the same fail-closed runner live decisions use
   (``app.submit.run_gate_set``);
3. the stored mandatory gate outcomes are kept as they were;
4. the decision-level rules and the combinator apply as in gate mode
   (``resolve_decision``), the novel-entity tier included.

It then compares that hypothetical action with the recorded one: agreement,
escalation (the gates would have been stricter — what the bypass rule let
through), and relaxation. Nothing is written to the ledger.

The dimension is **report-only** until a baseline calibrates a threshold:
it passes whenever it runs, and its rates land in the report. Each sampled
decision costs one call per advisory gate — for a model-backed gate, one
paid model call — so the sample is small and bounded (``sample_size``).

It samples the ledger, so the harness runs it after the dimensions that
write decisions (``runs_after_others``) and scopes the sample to the run
(``since``): it shadows what this run recorded, not the ledger's history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from app.enforcement.resolver import resolve_decision
from app.submit import DEFAULT_GATE_BUDGET_MS, run_gate_set
from core.eval.metrics import DimensionResult, EvalDimension, ShadowMetrics
from core.gate import severity
from core.gate_request import GateRequest
from core.observation import GateBinding, GateContext
from eval.dimensions.base import DimensionRun

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.audit.store import RecordedSample
    from app.gates import ComposedGate
    from core.actions import DecisionAction
    from core.bundle import DecisionBundle
    from core.observation import ReasonerRegistration, ReasonerRegistry

#: Recorded decisions sampled per run when the caller does not say.
DEFAULT_SHADOW_SAMPLE_SIZE = 10


class RecordedBundleSource(Protocol):
    """Where the dimension samples recorded decisions from (the bundle store)."""

    def recent_recorded(
        self, reasoner_id: str, *, limit: int, since: datetime | None = None
    ) -> RecordedSample:
        """Return the reasoner's most recent recorded bundles, newest first."""
        ...


class ShadowDimension:
    """Re-decides a sample of recorded decisions with the advisory gates.

    Args:
        source: The bundle store (or a test double) to sample from.
        gates: The deployment's composed gates, keyed by ``gate_id``.
        registry: Registry holding the reasoner's registration.
        reasoner_id: The reasoner whose recorded decisions to shadow.
        sample_size: Maximum recorded decisions to re-decide.
        since: Only decisions recorded at or after this instant (the eval
            run's start).
        budget_ms: Budget for each shadow gate invocation.
    """

    kind = EvalDimension.SHADOW

    #: The harness runs this dimension after the others (it samples what
    #: they record).
    runs_after_others = True

    def __init__(
        self,
        *,
        source: RecordedBundleSource,
        gates: Mapping[str, ComposedGate],
        registry: ReasonerRegistry,
        reasoner_id: str,
        sample_size: int = DEFAULT_SHADOW_SAMPLE_SIZE,
        since: datetime | None = None,
        budget_ms: int = DEFAULT_GATE_BUDGET_MS,
    ) -> None:
        """Construct the dimension."""
        self._source = source
        self._gates = gates
        self._registry = registry
        self._reasoner_id = reasoner_id
        self._sample_size = sample_size
        self._since = since
        self._budget_ms = budget_ms

    async def evaluate(self) -> DimensionRun:
        """Shadow the sample and assemble the (report-only) dimension run."""
        sample = self._source.recent_recorded(
            self._reasoner_id, limit=self._sample_size, since=self._since
        )
        registration = self._registry.get(self._reasoner_id)

        compared = agreed = escalated = relaxed = 0
        invocations = failures = 0
        for bundle in sample.bundles:
            outcome = await self._shadow(bundle, registration)
            if outcome is None:
                continue
            hypothetical, ran, failed = outcome
            invocations += ran
            failures += failed
            compared += 1
            delta = severity(hypothetical) - severity(bundle.decision_action)
            if delta == 0:
                agreed += 1
            elif delta > 0:
                escalated += 1
            else:
                relaxed += 1

        def _rate(count: int, total: int) -> float:
            return round(count / total, 4) if total else 0.0

        metrics = ShadowMetrics(
            num_decisions=len(sample.bundles),
            num_compared=compared,
            agreement_rate=_rate(agreed, compared),
            escalation_rate=_rate(escalated, compared),
            relaxation_rate=_rate(relaxed, compared),
            gate_failure_rate=_rate(failures, invocations),
            unloadable=sample.unloadable,
        )
        result = DimensionResult(
            dimension=self.kind,
            passed=True,  # report-only until a baseline calibrates a gate
            metrics={
                "agreement_rate": metrics.agreement_rate,
                "escalation_rate": metrics.escalation_rate,
                "relaxation_rate": metrics.relaxation_rate,
                "gate_failure_rate": metrics.gate_failure_rate,
            },
            threshold_violations=[],
            evaluated_at=datetime.now(UTC),
            num_samples=compared,
        )
        return DimensionRun(result=result, metrics=metrics)

    async def _shadow(
        self, bundle: DecisionBundle, registration: ReasonerRegistration
    ) -> tuple[DecisionAction, int, int] | None:
        """Re-decide one recorded bundle; ``None`` when it has no advisory gate.

        Returns:
            ``(hypothetical_action, gates_invoked, gates_failed)``.
        """
        advisory = [record for record in bundle.gates if not record.mandatory]
        if not advisory:
            return None
        gated = bundle.submission.model_copy(
            update={"mode": "gate", "proposed_action": None, "bypass": None}
        )

        def _request(gate_id: str) -> GateRequest:
            binding = registration.binding(gate_id)
            return GateRequest(
                decision_id=bundle.decision_id,
                submission=gated,
                gate_context=gated.gate_context(gate_id)
                or GateContext(gate_id=gate_id),
                budget_ms=self._budget_ms,
                identity=bundle.identity,
                binding_config=dict(binding.config) if binding is not None else {},
            )

        shadow_records = await run_gate_set(
            [GateBinding(gate_id=record.gate_id) for record in advisory],
            gates=self._gates,
            request=_request,
            registration=registration,
            decision_id=bundle.decision_id,
        )
        mandatory = [record for record in bundle.gates if record.mandatory]
        decision = resolve_decision(
            submission=gated,
            records=[*mandatory, *shadow_records],
            registration=registration,
            bypass_check=None,
            decision_id=bundle.decision_id,
        )
        failed = sum(1 for record in shadow_records if record.status == "failed")
        return decision.decision_action, len(shadow_records), failed


__all__ = [
    "DEFAULT_SHADOW_SAMPLE_SIZE",
    "RecordedBundleSource",
    "ShadowDimension",
]
