"""Decision Bundle construction and deterministic replay.

Constructs the complete DecisionBundle audit record for every decision, writing all
intermediate states to the PostgreSQL replay store. Implements the replay command:
given a bundle ID, re-executes enforcement against cached intermediate states
(including every gate's logged output and the cited bypass rule) to produce the
same decision action.

Replay does NOT re-invoke any gate. Each gate's stored output is fed directly to
enforcement. This is what makes replay deterministic and gate-implementation-
agnostic — non-deterministic gates (like LLMs at any temperature) cannot be
reproduced from inputs alone, so the bundle caches the outputs instead. The
enforcement profile comes from the snapshot of the registration version the
bundle cites (``RegistrationSnapshots``), and human resolutions of held
decisions are appended to the ``ResolutionJournal``. Changes to the identity
graph (merges, detaches, relationships) are appended to the
``EntityJournal``, which rebuilds the membership projection a subject's
timeline reads (DR-32).
"""

from app.audit.bundle import build_bundle
from app.audit.entity_journal import EntityJournal
from app.audit.registrations import RegistrationSnapshot, RegistrationSnapshots
from app.audit.resolution_journal import ResolutionJournal
from app.audit.schema import apply_canonical_schema
from app.audit.store import (
    BundleStore,
    DecisionSummary,
    GateReplay,
    RecordedSample,
    ReplayResult,
    StoredBundle,
    as_bundle,
    replay_bundle,
)

__all__ = [
    "BundleStore",
    "DecisionSummary",
    "EntityJournal",
    "GateReplay",
    "RecordedSample",
    "RegistrationSnapshot",
    "RegistrationSnapshots",
    "ReplayResult",
    "ResolutionJournal",
    "StoredBundle",
    "apply_canonical_schema",
    "as_bundle",
    "build_bundle",
    "replay_bundle",
]
