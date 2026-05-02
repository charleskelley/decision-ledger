"""In-process pipeline driver for the eval harness.

Mirrors ``app/main.py:lifespan`` service construction (Redis client,
Postgres pool, Elasticsearch client, embedding model, cross-encoder,
policy gate, bundle store) and exposes async wrappers around the
synchronous ``app.decide.execute_pipeline()`` function.

Implements both the ``PipelineDriver`` protocol (``eval.dimensions.
consistency``) and the ``RobustnessDriver`` protocol (``eval.dimensions.
robustness``) structurally — Python duck-typing, no inheritance.

Async wrappers use ``asyncio.to_thread()`` so the driver satisfies the
dimensions' async contract while reusing the same orchestration as the
HTTP route handler. Single source of truth for the pipeline behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import redis
import structlog
from elasticsearch import Elasticsearch
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.audit import BundleStore
from app.decide import execute_pipeline
from app.features import FeatureService
from app.gate.policy import PolicyGate, YamlPromptRegistry
from app.llm.openai import OpenAILLMClient
from app.retrieval.retriever import PolicyRetriever
from app.scorer import AtoScorer
from app.settings import Settings
from eval.dimensions.consistency import PipelineRunResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.actions import DecisionAction
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
        prompt_registry = YamlPromptRegistry()
        llm_client = OpenAILLMClient()
        self._gate = PolicyGate(client=llm_client, prompt_registry=prompt_registry)
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
        """Stub — full implementation lands with Step 4 robustness datasets."""
        msg = (
            "Forced schema-failure path requires Step 4 robustness dataset "
            "curation. RobustnessDimension calls this method only when "
            "schema_cases is non-empty; pass schema_cases=None to skip."
        )
        raise NotImplementedError(msg)

    async def run_with_forced_fallback(
        self,
        event: LoginEvent,
        *,
        fallback_kind: str,
    ) -> DecisionAction:
        """Stub — full implementation lands with Step 4 robustness datasets."""
        msg = (
            f"Forced fallback path ({fallback_kind!r}) requires Step 4 "
            "robustness dataset curation. RobustnessDimension calls this "
            "method only when fallback_cases is non-empty; pass "
            "fallback_cases=None to skip."
        )
        raise NotImplementedError(msg)
