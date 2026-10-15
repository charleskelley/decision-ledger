"""ATO Reasoner plugin — what the framework discovers and composes.

``PLUGIN`` is the object published under the ``decisionledger.reasoners``
entry point (name ``ato-reasoner``). It carries the registration record,
the FastAPI router, the prompt and corpus directories, and
``build_services`` — which opens the reasoner's own Redis connection and
loads the XGBoost scorer against the shared ``FrameworkServices``. Replay
needs no ATO code (each bundle stores ATO's ``Submission``), only ATO's
registration composed, for its enforcement profile.

Redis is ATO infrastructure: only the feature service, the ingestion
consumer, and the scenario generator touch it, so its URL lives in
``AtoSettings`` rather than in the framework settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import redis

from ato_reasoner.api import router
from ato_reasoner.feature_service import FeatureService
from ato_reasoner.registry import ATO_REGISTRATION
from ato_reasoner.scorer import AtoScorer
from ato_reasoner.settings import AtoSettings

if TYPE_CHECKING:
    from app.plugin import FrameworkServices

_PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class AtoServices:
    """Request-time services the ATO pipeline needs beyond the framework's.

    Args:
        feature_svc: Redis-backed sliding-window feature computer.
        scorer: Trained XGBoost fast scorer.
        corpus_version: Policy corpus version stamped on every bundle.
        redis: The Redis client the feature service runs on; closed on
            shutdown.
    """

    feature_svc: FeatureService
    scorer: AtoScorer
    corpus_version: str
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


class AtoPlugin:
    """``ReasonerPlugin`` implementation for the Account Takeover reasoner.

    Args:
        settings: Optional explicit ``AtoSettings``. When omitted, settings
            are read from the environment the first time ``build_services``
            runs, so importing the plugin never touches ``.env``.
    """

    def __init__(self, settings: AtoSettings | None = None) -> None:
        """Bind the plugin's static surface; defer settings to build time."""
        self.registration = ATO_REGISTRATION
        self.router = router
        self.prompts_dir = _PACKAGE_DIR / "prompts"
        self.corpus_dir = _PACKAGE_DIR / "corpus"
        self._settings = settings

    @property
    def settings(self) -> AtoSettings:
        """The settings ``build_services`` will use (read lazily)."""
        if self._settings is None:
            self._settings = AtoSettings()
        return self._settings

    def build_services(self, framework: FrameworkServices) -> AtoServices:
        """Open Redis and load the scorer; the framework owns everything else.

        Args:
            framework: Shared services. Unused today — ATO keeps no state in
                the framework's stores beyond what ``submit`` writes — but
                every plugin receives it.

        Returns:
            ``AtoServices`` ready for ``run_ato_decision``.

        Raises:
            FileNotFoundError: The scorer artifact is missing.
        """
        del framework
        settings = self.settings
        model_path = resolve_model_path(settings)
        redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=False)
        return AtoServices(
            feature_svc=FeatureService(redis=redis_client),
            scorer=AtoScorer(model_path),
            corpus_version=settings.corpus_version,
            redis=redis_client,
        )


#: The entry-point target: ``ato-reasoner = "ato_reasoner.plugin:PLUGIN"``.
PLUGIN = AtoPlugin()
