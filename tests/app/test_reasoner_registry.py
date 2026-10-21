"""Behavioral tests for the framework's LiveReasonerRegistry.

Reasoner-specific registration tests live with the reasoner package
(e.g., reasoners/account_takeover/tests/test_registry.py for ATO).

Key contracts:
- LiveReasonerRegistry rejects duplicate reasoner_id at construction.
- get() raises UnregisteredReasonerError for unknown ids.
- register() (DR-33) creates a reasoner's API registration or replaces it
  with a new version, through its writer, before it serves the version; an
  unchanged registration writes nothing; a plugin's or the file's
  registration is never replaced. The registrations read back from the
  writer at startup are served with their stored source and timestamps.
- A write swaps in a new map: a list of entries read before it stays as it
  was.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from app.reasoner_registry import (
    API_SOURCE,
    LiveReasonerRegistry,
    RegisteredReasoner,
    RegistrationWriter,
)
from core.exceptions import RegistrationError, UnregisteredReasonerError
from core.observation import (
    GateBinding,
    ReasonerRegistration,
    ReasonerRegistry,
    registration_version,
)
from tests._memory_ledger import MemoryApiRegistrations

if TYPE_CHECKING:
    from collections.abc import Callable


def _reg(reasoner_id: str) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id=reasoner_id,
        reasoner_name=f"Reasoner {reasoner_id}",
        gates=(GateBinding(gate_id="policy"),),
    )


# ---------------------------------------------------------------------------
# LiveReasonerRegistry construction
# ---------------------------------------------------------------------------


def test_the_registry_satisfies_the_protocol():
    assert isinstance(LiveReasonerRegistry([]), ReasonerRegistry)


def test_the_registry_rejects_a_duplicate_reasoner_id():
    with pytest.raises(ValueError, match="Duplicate"):
        LiveReasonerRegistry([_reg("dup"), _reg("dup")])


def test_the_registry_accepts_multiple_distinct_registrations():
    registry = LiveReasonerRegistry([_reg("r-1"), _reg("r-2")])
    assert registry.is_registered("r-1")
    assert registry.is_registered("r-2")


# ---------------------------------------------------------------------------
# LiveReasonerRegistry get / is_registered
# ---------------------------------------------------------------------------


def test_is_registered_true_for_known_id():
    registry = LiveReasonerRegistry([_reg("r-1")])
    assert registry.is_registered("r-1") is True


def test_is_registered_false_for_unknown_id():
    registry = LiveReasonerRegistry([_reg("r-1")])
    assert registry.is_registered("unknown") is False


def test_get_returns_registration_for_known_id():
    reg = _reg("r-1")
    registry = LiveReasonerRegistry([reg])
    assert registry.get("r-1") == reg


def test_get_raises_unregistered_error_for_unknown_id():
    registry = LiveReasonerRegistry([])
    with pytest.raises(UnregisteredReasonerError) as exc_info:
        registry.get("unknown")
    assert exc_info.value.reasoner_id == "unknown"


# ---------------------------------------------------------------------------
# The registrations resource view: versions and sources (DR-31)
# ---------------------------------------------------------------------------


def test_entries_carry_each_registrations_version_and_source():
    from app.reasoner_registry import INLINE_SOURCE
    from core.observation import registration_version

    first, second = _reg("stub-reasoner"), _reg("b-reasoner")
    registry = LiveReasonerRegistry(
        [second, first], sources={"b-reasoner": "file:/etc/r.yaml"}
    )

    entries = registry.entries()

    assert [e.reasoner_id for e in entries] == ["b-reasoner", "stub-reasoner"]
    assert entries[0].source == "file:/etc/r.yaml"
    assert entries[1].source == INLINE_SOURCE
    assert entries[1].version == registration_version(first)
    assert registry.entry("stub-reasoner") is entries[1]


def test_an_unknown_entry_raises_unregistered():
    registry = LiveReasonerRegistry([_reg("stub-reasoner")])
    with pytest.raises(UnregisteredReasonerError):
        registry.entry("nobody")


# ---------------------------------------------------------------------------
# register: registrations written at runtime (DR-33)
# ---------------------------------------------------------------------------


def _renamed(reasoner_id: str, name: str) -> ReasonerRegistration:
    return _reg(reasoner_id).model_copy(update={"reasoner_name": name})


@pytest.fixture(params=["in-memory", "writer"])
def make_registry(
    request: pytest.FixtureRequest,
) -> Callable[..., LiveReasonerRegistry]:
    """A registry without a writer, or with an in-memory one."""

    def _make(
        registrations: list[ReasonerRegistration] | None = None,
        *,
        sources: dict[str, str] | None = None,
    ) -> LiveReasonerRegistry:
        writer = MemoryApiRegistrations() if request.param == "writer" else None
        return LiveReasonerRegistry(registrations or [], sources=sources, writer=writer)

    return _make


def test_register_creates_an_api_registration(make_registry):
    registry = make_registry([_reg("declared")], sources={"declared": "plugin:x"})

    version = registry.register(_reg("new-reasoner"), registered_by="operator")

    entry = registry.entry("new-reasoner")
    assert version == registration_version(_reg("new-reasoner")) == entry.version
    assert entry.source == API_SOURCE
    assert entry.registered_by == "operator"
    assert entry.registered_at is not None
    assert entry.updated_at == entry.registered_at
    assert registry.get("new-reasoner") == _reg("new-reasoner")
    assert [e.reasoner_id for e in registry.entries()] == ["declared", "new-reasoner"]


def test_register_replaces_an_api_registration_with_a_new_version(make_registry):
    registry = make_registry()
    registry.register(_reg("api-reasoner"), registered_by="operator")
    first = registry.entry("api-reasoner")

    version = registry.register(
        _renamed("api-reasoner", "Renamed"), registered_by="trusted"
    )

    entry = registry.entry("api-reasoner")
    assert version != first.version
    assert entry.version == version
    assert entry.registration.reasoner_name == "Renamed"
    assert entry.registered_by == "trusted"
    assert entry.registered_at == first.registered_at
    assert entry.updated_at is not None
    assert first.updated_at is not None
    assert entry.updated_at >= first.updated_at


def test_register_leaves_an_unchanged_registration_as_it_is(make_registry):
    registry = make_registry()
    registry.register(_reg("api-reasoner"), registered_by="operator")
    first = registry.entry("api-reasoner")

    version = registry.register(_reg("api-reasoner"), registered_by="trusted")

    assert version == first.version
    assert registry.entry("api-reasoner") is first


@pytest.mark.parametrize("source", ["plugin:some.module", "file:/etc/r.yaml", "inline"])
def test_register_never_replaces_declared_configuration(make_registry, source):
    registry = make_registry([_reg("declared")], sources={"declared": source})

    with pytest.raises(RegistrationError, match="declared configuration") as err:
        registry.register(_renamed("declared", "Changed"), registered_by="operator")

    assert err.value.source == source
    assert registry.entry("declared").source == source
    assert registry.get("declared") == _reg("declared")


def test_register_stores_through_the_writer_before_serving():
    writer = MemoryApiRegistrations()
    registry = LiveReasonerRegistry(writer=writer)

    version = registry.register(_reg("api-reasoner"), registered_by="operator")

    assert isinstance(writer, RegistrationWriter)
    assert writer.rows["api-reasoner"] is registry.entry("api-reasoner")
    snapshot = writer.snapshots.get("api-reasoner", version)
    assert snapshot is not None
    assert snapshot.registration == _reg("api-reasoner")


def test_an_unchanged_registration_writes_nothing():
    calls: list[str] = []

    class _Recording(MemoryApiRegistrations):
        def save(
            self, registration: ReasonerRegistration, *, registered_by: str
        ) -> RegisteredReasoner:
            calls.append(registration.reasoner_id)
            return super().save(registration, registered_by=registered_by)

    registry = LiveReasonerRegistry(writer=_Recording())
    registry.register(_reg("api-reasoner"), registered_by="operator")
    registry.register(_reg("api-reasoner"), registered_by="operator")

    assert calls == ["api-reasoner"]


def test_a_failed_write_serves_nothing_new():
    class _Failing(MemoryApiRegistrations):
        def save(
            self, registration: ReasonerRegistration, *, registered_by: str
        ) -> RegisteredReasoner:
            msg = "the database is gone"
            raise ConnectionError(msg)

    registry = LiveReasonerRegistry(writer=_Failing())

    with pytest.raises(ConnectionError):
        registry.register(_reg("api-reasoner"), registered_by="operator")

    assert not registry.is_registered("api-reasoner")


def test_written_registrations_are_served_with_their_stored_details():
    """What a restart reads back from the writer is served as it was stored."""
    stored_at = datetime(2026, 10, 1, tzinfo=UTC)
    written = RegisteredReasoner(
        registration=_reg("api-reasoner"),
        version=registration_version(_reg("api-reasoner")),
        source=API_SOURCE,
        registered_by="operator",
        registered_at=stored_at,
        updated_at=stored_at,
    )

    registry = LiveReasonerRegistry([_reg("declared")], written=[written])

    assert registry.entry("api-reasoner") is written
    assert [e.reasoner_id for e in registry.entries()] == ["api-reasoner", "declared"]


def test_a_written_registration_cannot_share_a_declared_id():
    written = RegisteredReasoner(
        registration=_reg("declared"),
        version=registration_version(_reg("declared")),
        source=API_SOURCE,
    )
    with pytest.raises(ValueError, match="Duplicate"):
        LiveReasonerRegistry([_reg("declared")], written=[written])


def test_a_write_swaps_the_map_in_whole():
    """A reader holding the map while a write lands never sees it change.

    Reads take no lock: a write builds a new map and swaps it in, so a
    reader iterating the old one (``entries()`` sorts it) is never cut off
    mid-iteration by a concurrent write.
    """
    registry = LiveReasonerRegistry([_reg("declared")])
    held = registry._store

    registry.register(_reg("api-reasoner"), registered_by="operator")

    assert set(held) == {"declared"}
    assert registry._store is not held
    assert [e.reasoner_id for e in registry.entries()] == ["api-reasoner", "declared"]


def test_the_write_lock_is_reentrant():
    """The registrations resource holds it across admission and register."""
    registry = LiveReasonerRegistry()
    with registry.lock:
        registry.register(_reg("api-reasoner"), registered_by="operator")
    assert registry.is_registered("api-reasoner")
