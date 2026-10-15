"""In-process ATO pipeline driver for the eval harness.

Builds the same ``FrameworkServices`` the ledger's lifespan builds (via
``app.bootstrap.build_framework_services``) and the same ``AtoServices``
ATO's own service builds (``build_ato_services``), then exposes async
wrappers around ``ato_reasoner.pipeline.run_ato_decision()``, which submits
through a ``LocalClient`` over those framework services — the same
operation the ledger's HTTP route runs. Eval cannot drift from runtime
wiring.

Satisfies the framework's ``eval.adapter.EvalDriver`` protocol (and with
it the consistency ``PipelineDriver`` and robustness ``RobustnessDriver``
protocols) structurally — Python duck-typing, no inheritance. The ATO
``EvalAdapter`` (``ato_reasoner.eval.adapter``) builds it.

The robustness ``run_with_forced_*`` paths use private fault-injection
wrappers (``_RaisingRetriever``, ``_Raising5xxLLMClient``) to exercise
the production failure surfaces without polluting production code. Both
build a policy gate (``policy_rag_gate.PolicyGate``, the RAG policy gate ATO
binds) around the wrapper and swap it into the framework's composed gates
for one decision (a ``LocalClient`` over those services); the retriever,
prompt registry, and per-reasoner profiles they reuse come from the composed
policy gate itself (DR-28: the gate owns them).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from app.bootstrap import build_framework_services
from app.client import LocalClient
from app.enforcement.resolver import resolve_decision, resolve_gate_output
from app.gates import discover_gates
from app.settings import FrameworkSettings
from ato_reasoner.assembler import build_submission
from ato_reasoner.pipeline import run_ato_decision
from ato_reasoner.plugin import PLUGIN, build_ato_services, resolve_model_path
from ato_reasoner.registry import ATO_REGISTRATION
from ato_reasoner.settings import AtoSettings
from core.bundle import GateRecord
from core.gate import GateInput, GateOutput
from eval.dimensions.consistency import PipelineRunResult
from policy_rag_gate import POLICY_GATE_ID, PolicyGate
from policy_rag_gate.models import PolicyGateExtra
from policy_rag_gate.plugin import PolicyGatePlugin
from policy_rag_gate.settings import PolicyRagGateSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.plugin import FrameworkServices
    from ato_reasoner.events import LoginEvent
    from ato_reasoner.plugin import AtoServices
    from core.actions import DecisionAction
    from core.bundle import DecisionBundle
    from core.llm.client import CompletionResult
    from policy_rag_gate.models.snippet import RetrievedSnippet
    from policy_rag_gate.retriever import PolicyRetriever

log = structlog.get_logger(__name__)


class PipelineDriver:
    """In-process driver into the live decision pipeline.

    Constructed once per harness run; holds open connections to Redis,
    Postgres, and (through the composed policy gate) Elasticsearch for the
    duration. Call ``close()`` (or use as a context manager) on shutdown to
    release them.

    Args:
        framework_settings: Optional ``FrameworkSettings`` override. Defaults
            to a fresh ``FrameworkSettings()`` reading from the environment.
        ato_settings: Optional ``AtoSettings`` override. Defaults to a fresh
            ``AtoSettings()`` reading ``ATO_*`` vars from the environment.
        gate_settings: Optional ``PolicyRagGateSettings`` for the policy
            gate ATO routes to. Defaults to a fresh one reading from the
            environment. The driver checks its ``OPENAI_API_KEY`` (raising
            ``ValueError`` when empty) and builds the composed policy gate
            from it, so its Elasticsearch URL and key are the ones used.

    Raises:
        ValueError: ``OPENAI_API_KEY`` is empty.
        FileNotFoundError: ``ATO_SCORER_MODEL_PATH`` does not point at a
            readable file. Run ``make train`` to produce one.
    """

    def __init__(
        self,
        *,
        framework_settings: FrameworkSettings | None = None,
        ato_settings: AtoSettings | None = None,
        gate_settings: PolicyRagGateSettings | None = None,
    ) -> None:
        """Construct the driver and open all infrastructure connections."""
        fw = (
            framework_settings
            if framework_settings is not None
            else FrameworkSettings()
        )
        gate = gate_settings if gate_settings is not None else PolicyRagGateSettings()

        # Fail-fast configuration validation — before any infrastructure
        # connection so misconfiguration surfaces immediately. The policy
        # gate ATO routes to calls OpenAI.
        if not gate.openai_api_key:
            msg = (
                "OPENAI_API_KEY is required for PipelineDriver (the policy "
                "gate calls OpenAI). Set the env var or pass "
                "PolicyRagGateSettings(openai_api_key=...)."
            )
            raise ValueError(msg)

        ato = ato_settings if ato_settings is not None else AtoSettings()
        resolve_model_path(ato)

        # Every installed gate, with the policy gate bound to the settings
        # checked above rather than to the entry point's own lazy settings.
        gate_plugins = [
            PolicyGatePlugin(settings=gate) if p.gate_id == POLICY_GATE_ID else p
            for p in discover_gates()
        ]
        self._framework = build_framework_services(fw, [PLUGIN], gate_plugins)
        self._client = LocalClient(self._framework)
        try:
            self._services = build_ato_services(ato)
        except Exception:
            self._framework.close()
            raise

        log.info(
            "eval.pipeline_driver.ready",
            component="eval_pipeline",
            msg="All services initialized",
        )

    def reset_state(self) -> None:
        """Discard all accumulated feature-window state in the ATO Redis.

        Called at scenario boundaries so velocity and novelty history from
        one scenario cannot leak into the next. Flushes the whole Redis
        database the reasoner is configured with, so point ``ATO_REDIS_URL``
        at a database reserved for this reasoner.
        """
        self._services.redis.flushdb()
        log.info(
            "eval.pipeline_driver.state_reset",
            component="eval_pipeline",
        )

    def close(self) -> None:
        """Release infrastructure connections held by the driver."""
        self._services.close()
        self._framework.close()
        log.info(
            "eval.pipeline_driver.closed",
            component="eval_pipeline",
            msg="Connections closed",
        )

    def __enter__(self) -> PipelineDriver:
        """Return self for context-manager use."""
        return self

    def __exit__(self, *_: object) -> None:
        """Release infrastructure connections on context-manager exit."""
        self.close()

    @property
    def framework(self) -> FrameworkServices:
        """The shared framework services this driver runs on."""
        return self._framework

    @property
    def services(self) -> AtoServices:
        """The ATO reasoner's services (feature service, scorer, Redis)."""
        return self._services

    @property
    def client(self) -> LocalClient:
        """The in-process ledger client the pipeline submits through."""
        return self._client

    async def decide(
        self,
        event: LoginEvent,
        *,
        client: LocalClient | None = None,
        services: AtoServices | None = None,
    ) -> DecisionBundle:
        """Run one event through the pipeline and return the written bundle.

        Args:
            event: The login.
            client: The ledger to submit to (a fault-injected one); this
                driver's own by default.
            services: ATO's services (a mislabeled corpus version); this
                driver's own by default.

        Returns:
            The bundle the ledger wrote for the decision.
        """
        ledger = client if client is not None else self._client
        decision = await run_ato_decision(
            event=event,
            services=services if services is not None else self._services,
            client=ledger,
        )
        return await ledger.get_bundle(decision.receipt.decision_id)

    def _policy_gate(self) -> PolicyGate:
        """Return the composed policy gate this reasoner routes to.

        Raises:
            RuntimeError: The deployment does not compose the policy gate.
        """
        composed = self._framework.gates.get(POLICY_GATE_ID)
        if composed is None or not isinstance(composed.gate, PolicyGate):
            msg = (
                f"The {POLICY_GATE_ID!r} gate is not composed in this deployment; "
                "the ATO eval driver needs it."
            )
            raise RuntimeError(msg)
        return composed.gate

    def _framework_with_policy_gate(self, gate: PolicyGate) -> FrameworkServices:
        """Return the framework services with ``gate`` swapped in as the policy gate.

        Only the runtime gate changes: the plugin and whether the deployment
        mandates the gate carry over, so a fault-injected run is gated the
        way the deployment gates.
        """
        composed = self._framework.gates[POLICY_GATE_ID]
        gates = {
            **self._framework.gates,
            POLICY_GATE_ID: dataclasses.replace(composed, gate=gate),
        }
        return dataclasses.replace(self._framework, gates=gates)

    # ------------------------------------------------------------------
    # PipelineDriver protocol (consumed by ConsistencyDimension)
    # ------------------------------------------------------------------

    async def run(self, events: Sequence[LoginEvent]) -> PipelineRunResult:
        """Feed events sequentially; return ``PipelineRunResult`` for the trigger.

        Earlier events warm the feature window state; the last event is the
        trigger whose decision is returned.

        Args:
            events: Ordered events. Must contain at least one entry.

        Returns:
            ``PipelineRunResult`` for the trigger event, with ``confidence``
            and ``rationale`` populated only when the gate ran.

        Raises:
            ValueError: ``events`` is empty.
        """
        last_bundle = None
        for event in events:
            last_bundle = await self.decide(event)

        if last_bundle is None:
            msg = "PipelineDriver.run() requires at least one event"
            raise ValueError(msg)

        policy = last_bundle.gate(POLICY_GATE_ID)
        gate_output = policy.output if policy is not None else None
        verdict = gate_output.verdict if gate_output is not None else None
        # The rationale is the policy gate's own output (its PolicyGateExtra
        # in ``gate_output.extra``), not a field of the universal verdict.
        rationale = (
            PolicyGateExtra.model_validate(gate_output.extra, strict=False).rationale
            if gate_output is not None and verdict is not None
            else None
        )
        return PipelineRunResult(
            decision_action=last_bundle.decision_action,
            confidence=verdict.confidence if verdict is not None else None,
            rationale=rationale,
        )

    # ------------------------------------------------------------------
    # RobustnessDriver protocol (consumed by RobustnessDimension)
    # ------------------------------------------------------------------

    async def run_event(self, event: LoginEvent) -> DecisionAction:
        """Run a single event through the normal pipeline."""
        return (await self.decide(event)).decision_action

    async def run_with_forced_schema_failure(
        self,
        event: LoginEvent,
    ) -> DecisionAction:
        """Force a schema-failure verdict and return enforcement's action.

        Tests the DR-19 Tier 1 contract directly: when the gate produces no
        valid verdict, enforcement must route to ``HOLD``. Bypasses the gate
        entirely — deterministic, free, and decoupled from ``LLMClient``
        internals.

        The path runs the full domain pipeline up to (but not including) the
        gate call: feature compute → score → assemble the submission. The
        submission is forced into gate mode — a login in a score band would
        otherwise be recorded and never reach the gate — and enforcement then
        runs on a policy-gate record whose output has ``verdict=None``.
        """
        features = self._services.feature_svc.compute(event)
        scorer_output = self._services.scorer.score(features)
        submission = build_submission(
            event, features, scorer_output, corpus_version=self._services.corpus_version
        )
        gated = submission.model_copy(
            update={"mode": "gate", "proposed_action": None, "bypass": None}
        )
        registration = self._framework.registry.get(ATO_REGISTRATION.reasoner_id)
        decision_id = str(uuid.uuid4())
        output = GateOutput(gate_id=POLICY_GATE_ID, deterministic=False, verdict=None)
        record = GateRecord(
            gate_id=POLICY_GATE_ID,
            mandatory=False,
            status="ran",
            input=GateInput(gate_id=POLICY_GATE_ID),
            output=output,
            enforcement=resolve_gate_output(
                output, registration=registration, decision_id=decision_id
            ),
        )
        decision = resolve_decision(
            submission=gated,
            records=[record],
            registration=registration,
            bypass_check=None,
            decision_id=decision_id,
        )
        return decision.decision_action

    async def run_with_forced_fallback(
        self,
        event: LoginEvent,
        *,
        fallback_kind: str,
    ) -> DecisionAction:
        """Drive a single event through a fault-injected pipeline.

        ``fallback_kind`` selects the failure surface to exercise:

        - ``"llm_5xx"`` — swaps in a policy gate whose ``LLMClient``
          raises from ``complete_structured``. ``PolicyGate`` catches the
          exception and emits ``verdict=None``; enforcement Tier 1 routes
          to ``HOLD``. Exercises the full production failure path.
        - ``"retrieval_error"`` — swaps in a policy gate whose retriever
          raises from ``retrieve()``. The framework fails the gate closed
          (DR-29): the policy gate's record is ``failed`` with the error,
          and enforcement Tier 1 routes to ``HOLD``.
        - ``"corpus_mismatch"`` — runs the pipeline with a deliberately
          stale ``corpus_version`` label. Currently a labeling-only
          mismatch (does not alter pipeline behavior); the driver returns
          the resulting ``decision_action``. Documents the contract for
          future corpus-version validation.

        Args:
            event: The login event to drive.
            fallback_kind: One of ``{"llm_5xx", "retrieval_error",
                "corpus_mismatch"}``.

        Returns:
            The pipeline's resulting ``DecisionAction``.

        Raises:
            ValueError: ``fallback_kind`` is not recognized.
        """
        if fallback_kind == "llm_5xx":
            policy_gate = self._policy_gate()
            faulty_gate = PolicyGate(
                client=_Raising5xxLLMClient(),
                prompt_registry=policy_gate.prompt_registry,
                retriever=policy_gate.retriever,
                profiles=policy_gate.profiles,
            )
            bundle = await self.decide(
                event,
                client=self._client.with_framework(
                    self._framework_with_policy_gate(faulty_gate)
                ),
            )
            return bundle.decision_action

        if fallback_kind == "retrieval_error":
            policy_gate = self._policy_gate()
            faulty_gate = PolicyGate(
                client=policy_gate.client,
                prompt_registry=policy_gate.prompt_registry,
                retriever=_RaisingRetriever(policy_gate.retriever),  # type: ignore[arg-type]
                profiles=policy_gate.profiles,
            )
            bundle = await self.decide(
                event,
                client=self._client.with_framework(
                    self._framework_with_policy_gate(faulty_gate)
                ),
            )
            return bundle.decision_action

        if fallback_kind == "corpus_mismatch":
            bundle = await self.decide(
                event,
                services=dataclasses.replace(
                    self._services, corpus_version="stale-v0-mismatch"
                ),
            )
            return bundle.decision_action

        msg = (
            f"Unknown fallback_kind {fallback_kind!r}. Expected one of: "
            "'llm_5xx', 'retrieval_error', 'corpus_mismatch'."
        )
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Private fault-injection wrappers (robustness sub-checks only)
# ---------------------------------------------------------------------------


class _RetrievalForcedError(RuntimeError):
    """Raised by ``_RaisingRetriever``; the framework fails the gate closed on it."""


class _RaisingRetriever:
    """Retriever wrapper that raises from ``retrieve()`` for fallback tests.

    Raises a sentinel error from ``retrieve``. Duck-types as
    ``PolicyRetriever`` for the policy gate.
    """

    def __init__(self, original: PolicyRetriever) -> None:
        """Store the wrapped retriever (kept for parity with sibling wrappers)."""
        self._original = original

    def retrieve(self, *args: Any, **kwargs: Any) -> tuple[list[RetrievedSnippet], str]:
        """Always raises — simulates retrieval-layer failure (ES/PG outage)."""
        del args, kwargs
        msg = "forced retrieval error for robustness fallback test"
        raise _RetrievalForcedError(msg)


class _Raising5xxLLMClient:
    """``LLMClient`` wrapper that raises from ``complete_structured``.

    Satisfies the ``LLMClient`` Protocol structurally. The ``model``
    attribute lets ``PolicyGate`` record a model label without crashing on
    ``getattr(client, "model", ...)``.
    """

    model: str = "raising-5xx-stub"

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_format: type,
    ) -> CompletionResult[Any]:
        """Always raises — simulates an upstream LLM 5xx error."""
        del system, user, response_format
        msg = "forced LLM 5xx for robustness fallback test"
        raise RuntimeError(msg)
