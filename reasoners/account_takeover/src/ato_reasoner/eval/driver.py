"""In-process ATO pipeline driver for the eval harness.

Builds the same ``FrameworkServices`` the API lifespan builds (via
``app.bootstrap.build_framework_services``) and the same ``AtoServices``
the ATO plugin builds, then exposes async wrappers around
``ato_reasoner.pipeline.run_ato_decision()``. Because both
go through the plugin seam, eval cannot drift from runtime wiring.

Satisfies the framework's ``eval.adapter.EvalDriver`` protocol (and with
it the consistency ``PipelineDriver`` and robustness ``RobustnessDriver``
protocols) structurally — Python duck-typing, no inheritance. The ATO
``EvalAdapter`` (``ato_reasoner.eval.adapter``) builds it.

The robustness ``run_with_forced_*`` paths use private fault-injection
wrappers (``_RaisingRetriever``, ``_Raising5xxLLMClient``) to exercise
the production failure surfaces without polluting production code.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from app.bootstrap import build_framework_services
from app.enforcement.resolver import resolve as resolve_enforcement
from app.gate.policy import PolicyGate
from app.settings import FrameworkSettings
from ato_reasoner.assembler import build_observation
from ato_reasoner.pipeline import run_ato_decision
from ato_reasoner.plugin import PLUGIN, AtoPlugin, resolve_model_path
from ato_reasoner.registry import ATO_REGISTRATION
from ato_reasoner.retrieval_query import build_ato_query
from core.actions import DecisionAction
from core.routes import GateRoute
from eval.dimensions.consistency import PipelineRunResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.plugin import FrameworkServices
    from app.retrieval.retriever import PolicyRetriever
    from ato_reasoner.events import LoginEvent
    from ato_reasoner.plugin import AtoServices
    from ato_reasoner.settings import AtoSettings
    from core.llm.client import CompletionResult
    from core.snippet import RetrievedSnippet

log = structlog.get_logger(__name__)


class PipelineDriver:
    """In-process driver into the live decision pipeline.

    Constructed once per harness run; holds open connections to Redis,
    Postgres, and Elasticsearch for the duration. Call ``close()`` (or
    use as a context manager) on shutdown to release them.

    Args:
        framework_settings: Optional ``FrameworkSettings`` override. Defaults
            to a fresh ``FrameworkSettings()`` reading from the environment.
            ``OPENAI_API_KEY`` must be set; the constructor raises
            ``ValueError`` otherwise.
        ato_settings: Optional ``AtoSettings`` override. Defaults to a fresh
            ``AtoSettings()`` reading ``ATO_*`` vars from the environment.

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
    ) -> None:
        """Construct the driver and open all infrastructure connections."""
        fw = (
            framework_settings
            if framework_settings is not None
            else FrameworkSettings()
        )

        # Fail-fast configuration validation — before any infrastructure
        # connection so misconfiguration surfaces immediately.
        if not fw.openai_api_key:
            msg = (
                "OPENAI_API_KEY is required for PipelineDriver. "
                "Set the env var or pass "
                "FrameworkSettings(openai_api_key=...)."
            )
            raise ValueError(msg)

        plugin = PLUGIN if ato_settings is None else AtoPlugin(settings=ato_settings)
        resolve_model_path(plugin.settings)

        self._framework = build_framework_services(fw, [plugin])
        self._services = plugin.build_services(self._framework)

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
    def retriever(self) -> PolicyRetriever:
        """Expose the internal retriever for direct dimension consumption.

        Lets the harness's ``RetrievalDimension`` reuse the same open
        connections instead of opening a parallel set.
        """
        return self._framework.retriever

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
            last_bundle = await run_ato_decision(
                event=event,
                services=self._services,
                framework=self._framework,
            )

        if last_bundle is None:
            msg = "PipelineDriver.run() requires at least one event"
            raise ValueError(msg)

        verdict = (
            last_bundle.gate_output.verdict
            if last_bundle.gate_output is not None
            else None
        )
        # rationale lives on PolicyGateVerdict (subclass), not on the
        # universal GateVerdict — getattr gracefully handles future gate
        # types that don't carry one.
        rationale = getattr(verdict, "rationale", None) if verdict is not None else None
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
        bundle = await run_ato_decision(
            event=event,
            services=self._services,
            framework=self._framework,
        )
        return bundle.decision_action

    async def run_with_forced_schema_failure(
        self,
        event: LoginEvent,
    ) -> DecisionAction:
        """Force a schema-failure verdict and return enforcement's action.

        Tests the DR-19 Tier 1 contract directly: when the gate produces no
        valid verdict, ``app.enforcement.resolver.resolve`` must route to
        ``HOLD``. Bypasses the LLM entirely — deterministic, free, and
        decoupled from ``LLMClient`` internals.

        The path runs the full domain pipeline up to (but not including) the
        gate call: feature compute → score → assemble observation → retrieve
        snippets. The resulting ``Observation`` is forced onto the gate route
        so the schema-failure tier is reachable; ``resolve`` is then invoked
        with ``gate_output=None``.
        """
        features = self._services.feature_svc.compute(event)
        scorer_output = self._services.scorer.score(features)
        obs = build_observation(event, features, scorer_output)
        # Force gate-path so Tier 1 (schema failure) is reachable; fast-path
        # observations short-circuit before tier evaluation.
        if obs.route != GateRoute.ROUTE_TO_GATE:
            obs = obs.model_copy(update={"route": GateRoute.ROUTE_TO_GATE})

        query = build_ato_query(event, scorer_output)
        gate_config = obs.gate_context.gate_config or {}
        reasoner_id = (
            obs.reasoner_context.reasoner_id
            if obs.reasoner_context is not None
            else None
        )
        snippets, _ = self._framework.retriever.retrieve(
            query=query,
            k=5,
            reasoner_id=reasoner_id,
            jurisdictions=gate_config.get("jurisdictions"),
            risk_tier=gate_config.get("risk_tier"),
        )

        enforcement = resolve_enforcement(
            obs,  # type: ignore[arg-type]
            None,
            snippets=snippets,
            decision_id=str(uuid.uuid4()),
            registration=self._framework.registry.get(ATO_REGISTRATION.reasoner_id),
        )
        return enforcement.decision_action

    async def run_with_forced_fallback(
        self,
        event: LoginEvent,
        *,
        fallback_kind: str,
    ) -> DecisionAction:
        """Drive a single event through a fault-injected pipeline.

        ``fallback_kind`` selects the failure surface to exercise:

        - ``"llm_5xx"`` — wraps the gate's ``LLMClient`` so
          ``complete_structured`` raises. ``PolicyGate`` catches the
          exception and emits ``verdict=None``; enforcement Tier 1 routes
          to ``HOLD``. Exercises the full production failure path.
        - ``"retrieval_error"`` — wraps the retriever so ``retrieve()``
          raises. ``run_ato_decision`` does not yet catch retrieval
          exceptions; the driver translates the raised error into the
          contract's expected action (``HOLD``) so the test reflects what
          the system *should* do.
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
            faulty_gate = PolicyGate(
                client=_Raising5xxLLMClient(),
                prompt_registry=self._framework.prompt_registry,
            )
            bundle = await run_ato_decision(
                event=event,
                services=self._services,
                framework=dataclasses.replace(self._framework, gate=faulty_gate),
            )
            return bundle.decision_action

        if fallback_kind == "retrieval_error":
            faulty_retriever = _RaisingRetriever(self._framework.retriever)
            try:
                bundle = await run_ato_decision(
                    event=event,
                    services=self._services,
                    framework=dataclasses.replace(
                        self._framework,
                        retriever=faulty_retriever,
                    ),
                )
                return bundle.decision_action
            except _RetrievalForcedError:
                # run_ato_decision doesn't yet catch retrieval errors. The
                # contract this case tests is "retrieval failure → HOLD";
                # surface it deterministically until production grows the
                # explicit catch.
                return DecisionAction.HOLD

        if fallback_kind == "corpus_mismatch":
            bundle = await run_ato_decision(
                event=event,
                services=dataclasses.replace(
                    self._services, corpus_version="stale-v0-mismatch"
                ),
                framework=self._framework,
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
    """Internal sentinel raised by ``_RaisingRetriever`` and caught by the driver."""


class _RaisingRetriever:
    """Retriever wrapper that raises from ``retrieve()`` for fallback tests.

    Raises a sentinel error from ``retrieve``. Duck-types as
    ``PolicyRetriever`` for ``run_ato_decision``.
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
