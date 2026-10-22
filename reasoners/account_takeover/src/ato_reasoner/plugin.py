"""ATO Reasoner plugin — what the framework discovers, and ATO's own services.

``PLUGIN`` is the object published under the ``decisionledger.reasoners``
entry point (name ``ato-reasoner``). It carries what a ledger deployment
needs from ATO and nothing it would run: the registration record (ATO's gate
set, bypass rules, and enforcement profile) and the prompt and corpus
directories the RAG policy gate reads. Replay needs no ATO code (each bundle
stores ATO's ``Submission``, and the ledger snapshots each registration
version a bundle cites).

ATO runs itself (DR-31): its own FastAPI service (``ato_reasoner.app``), its
eval driver, and its baseline capture open the services below with
``build_ato_services`` — Redis for the feature windows and the XGBoost
scorer — and submit to the ledger through a ``LedgerClient``. Redis is ATO
infrastructure: only the feature service, the ingestion consumer, and the
scenario generator touch it, so its URL lives in ``AtoSettings``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import redis

from ato_reasoner.feature_service import FeatureService
from ato_reasoner.registry import ATO_REGISTRATION
from ato_reasoner.scorer import AtoScorer
from ato_reasoner.settings import AtoSettings

_PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class AtoServices:
    """Request-time services the ATO pipeline needs to score a login.

    Args:
        feature_svc: Redis-backed sliding-window feature computer.
        scorer: Trained XGBoost scorer.
        template_id: The policy gate prompt template ATO's gate context
            names (``AtoSettings.template_id``).
        redis: The Redis client the feature service runs on; closed on
            shutdown.
    """

    feature_svc: FeatureService
    scorer: AtoScorer
    template_id: str
    redis: redis.Redis

    def close(self) -> None:
        """Release the Redis connection."""
        self.redis.close()


def resolve_model_path(settings: AtoSettings) -> Path:
    """Return the scorer artifact path, failing fast when it is missing.

    Args:
        settings: ATO settings carrying ``scorer_model_path``.

    Returns:
        The existing artifact path.

    Raises:
        FileNotFoundError: The artifact does not exist. The message names
            the training command that produces it.
    """
    model_path = Path(settings.scorer_model_path)
    if not model_path.exists():
        msg = (
            f"Scorer model not found at {model_path}. "
            "Run `make train-account_takeover` or "
            "`uv run python -m ato_reasoner.scorer train "
            f"--output {model_path} --samples 2000` first."
        )
        raise FileNotFoundError(msg)
    return model_path


def build_ato_services(settings: AtoSettings | None = None) -> AtoServices:
    """Open Redis and load the scorer.

    Args:
        settings: ATO settings; read from the environment when omitted.

    Returns:
        ``AtoServices`` ready for ``run_ato_decision``; close them on
        shutdown.

    Raises:
        FileNotFoundError: The scorer artifact is missing.
    """
    settings = settings if settings is not None else AtoSettings()
    model_path = resolve_model_path(settings)
    redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=False)
    return AtoServices(
        feature_svc=FeatureService(redis=redis_client),
        scorer=AtoScorer(model_path),
        template_id=settings.template_id,
        redis=redis_client,
    )


class AtoPlugin:
    """``ReasonerPlugin`` implementation for the Account Takeover reasoner.

    Attributes:
        registration: ``ATO_REGISTRATION``.
        prompts_dir: ATO's prompt templates (``prompts/``), which the RAG
            policy gate loads.
        corpus_dir: ATO's policy corpus (``corpus/``), which the RAG policy
            gate indexes under ATO's ``reasoner_id``.
    """

    def __init__(self) -> None:
        """Bind the plugin's static surface."""
        self.registration = ATO_REGISTRATION
        self.prompts_dir = _PACKAGE_DIR / "prompts"
        self.corpus_dir = _PACKAGE_DIR / "corpus"


#: The entry-point target: ``ato-reasoner = "ato_reasoner.plugin:PLUGIN"``.
PLUGIN = AtoPlugin()
