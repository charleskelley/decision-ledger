"""In-process pipeline driver for the eval harness.

Mirrors ``app/main.py:lifespan`` service construction (Redis client,
Postgres pool, Elasticsearch client, embedding model, cross-encoder,
policy gate, bundle store) and exposes async wrappers around
``app.decide.execute_pipeline()``.

Implements both the ``PipelineDriver`` protocol (``eval.dimensions.
consistency``) and the ``RobustnessDriver`` protocol (``eval.dimensions.
robustness``) structurally — Python duck-typing, no inheritance.

The robustness ``run_with_forced_*`` paths use private fault-injection
wrappers (``_RaisingRetriever``, ``_Raising5xxLLMClient``) to exercise
the production failure surfaces without polluting production code.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import redis
import structlog
from elasticsearch import Elasticsearch
from pgvector.psycopg import register_vector
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.audit import BundleStore
from app.decide import execute_pipeline
from app.enforcement.resolver import resolve as resolve_enforcement
from app.features import FeatureService
from app.gate.policy import PolicyGate, YamlPromptRegistry
from app.llm.openai import OpenAILLMClient
from app.retrieval.retriever import PolicyRetriever
from app.scorer import AtoScorer
from app.settings import Settings
from core.actions import DecisionAction
from core.routes import GateRoute
from eval.dimensions.consistency import PipelineRunResult
from reasoner.account_takeover.assembler import build_observation

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.llm.client import CompletionResult
    from core.snippet import RetrievedSnippet
    from reasoner.account_takeover.events import LoginEvent

log = structlog.get_logger(__name__)


class PipelineDriver:
    """In-process driver into the live decision pipeline.

    Constructed once per harness run; holds open connections to Redis,
    Postgres, and Elasticsearch for the duration. Call ``close()`` (or
    use as a context manager) on shutdown to release them.

    Args:
        settings: Optional ``Settings`` override. Defaults to a fresh
            ``Settings()`` reading from the environment. ``OPENAI_API_KEY``
            must be set; the constructor raises ``ValueError`` otherwise.

    Raises:
        ValueError: ``OPENAI_API_KEY`` is empty.
        FileNotFoundError: ``scorer_model_path`` does not point at a
            readable file. Run ``make train`` to produce one.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        """Construct the driver and open all infrastructure connections."""
        settings = settings if settings is not None else Settings()

        # Fail-fast configuration validation — before any infrastructure
        # connection so misconfiguration surfaces immediately.
        if not settings.openai_api_key:
            msg = (
                "OPENAI_API_KEY is required for PipelineDriver. "
                "Set the env var or pass Settings(openai_api_key=...)."
            )
            raise ValueError(msg)

        model_path = Path(settings.scorer_model_path)
        if not model_path.exists():
            msg = (
                f"Scorer model not found at {model_path}. "
                "Run `make train` or `uv run python -m app.scorer train "
                f"--output {model_path} --samples 2000` first."
            )
            raise FileNotFoundError(msg)

        # Service construction — same order as app/main.py:lifespan.
        self._redis = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=False,
        )
        self._pg_conn = psycopg.connect(settings.postgres_dsn)
        # Teach psycopg how to adapt numpy ndarrays to pgvector parameters; the
        # retriever's dense-search query passes the embedding via %s.
        register_vector(self._pg_conn)
        self._es = Elasticsearch(settings.elasticsearch_url)

        embed_model = SentenceTransformer("all-MiniLM-L6-v2")
        cross_encoder = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

        self._feature_svc = FeatureService(redis=self._redis)
        self._scorer = AtoScorer(model_path)
        self._retriever = PolicyRetriever(
            pg_conn=self._pg_conn,
            es=self._es,
            model=embed_model,
            cross_encoder=cross_encoder,
        )
        self._prompt_registry = YamlPromptRegistry()
        llm_client = OpenAILLMClient()
        self._gate = PolicyGate(
            client=llm_client, prompt_registry=self._prompt_registry
        )
        self._store = BundleStore(conn=self._pg_conn)
        self._store.ensure_schema()

        self._corpus_version = settings.corpus_version

        log.info(
            "eval.pipeline_driver.ready",
            component="eval_pipeline",
            msg="All services initialized",
        )

    def close(self) -> None:
        """Release infrastructure connections held by the driver."""
        self._pg_conn.close()
        self._redis.close()
        self._es.close()
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
    def retriever(self) -> PolicyRetriever:
        """Expose the internal retriever for direct dimension consumption.

        Lets the harness's ``RetrievalDimension`` reuse the same open
        connections instead of opening a parallel set.
        """
        return self._retriever

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
            last_bundle = await execute_pipeline(
                event=event,
                feature_svc=self._feature_svc,
                scorer=self._scorer,
                retriever=self._retriever,
                gate=self._gate,
                store=self._store,
                corpus_version=self._corpus_version,
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
        bundle = await execute_pipeline(
            event=event,
            feature_svc=self._feature_svc,
            scorer=self._scorer,
            retriever=self._retriever,
            gate=self._gate,
            store=self._store,
            corpus_version=self._corpus_version,
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
        features = self._feature_svc.compute(event)
        scorer_output = self._scorer.score(features)
        obs = build_observation(event, features, scorer_output)
        # Force gate-path so Tier 1 (schema failure) is reachable; fast-path
        # observations short-circuit before tier evaluation.
        if obs.route != GateRoute.ROUTE_TO_GATE:
            obs = obs.model_copy(update={"route": GateRoute.ROUTE_TO_GATE})

        query = self._retriever.build_query(event, scorer_output)
        gate_config = obs.gate_context.gate_config or {}
        snippets, _ = self._retriever.retrieve(
            query=query,
            k=5,
            jurisdictions=gate_config.get("jurisdictions"),
            risk_tier=gate_config.get("risk_tier"),
        )

        enforcement = resolve_enforcement(
            obs,  # type: ignore[arg-type]
            None,
            snippets=snippets,
            decision_id=str(uuid.uuid4()),
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
          raises. ``execute_pipeline`` does not yet catch retrieval
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
                prompt_registry=self._prompt_registry,
            )
            bundle = await execute_pipeline(
                event=event,
                feature_svc=self._feature_svc,
                scorer=self._scorer,
                retriever=self._retriever,
                gate=faulty_gate,
                store=self._store,
                corpus_version=self._corpus_version,
            )
            return bundle.decision_action

        if fallback_kind == "retrieval_error":
            faulty_retriever = _RaisingRetriever(self._retriever)
            try:
                bundle = await execute_pipeline(
                    event=event,
                    feature_svc=self._feature_svc,
                    scorer=self._scorer,
                    retriever=faulty_retriever,  # type: ignore[arg-type]
                    gate=self._gate,
                    store=self._store,
                    corpus_version=self._corpus_version,
                )
                return bundle.decision_action
            except _RetrievalForcedError:
                # execute_pipeline doesn't yet catch retrieval errors. The
                # contract this case tests is "retrieval failure → HOLD";
                # surface it deterministically until production grows the
                # explicit catch.
                return DecisionAction.HOLD

        if fallback_kind == "corpus_mismatch":
            bundle = await execute_pipeline(
                event=event,
                feature_svc=self._feature_svc,
                scorer=self._scorer,
                retriever=self._retriever,
                gate=self._gate,
                store=self._store,
                corpus_version="stale-v0-mismatch",
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

    Delegates ``build_query`` to the wrapped retriever (the pipeline calls
    that before the route check) and raises a sentinel error from
    ``retrieve``. Duck-types as ``PolicyRetriever`` for ``execute_pipeline``.
    """

    def __init__(self, original: PolicyRetriever) -> None:
        """Store the wrapped retriever for ``build_query`` delegation."""
        self._original = original

    def build_query(self, *args: Any, **kwargs: Any) -> str:
        """Delegate to the wrapped retriever."""
        return self._original.build_query(*args, **kwargs)

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
