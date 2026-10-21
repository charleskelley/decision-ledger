"""LiveReasonerRegistry — the reasoners the ledger serves, declared or written.

Registrations reach the registry from three sources (DR-31, DR-33), composed
at startup by ``app/plugins.py:build_registry``:

- each installed reasoner plugin's ``registration`` (conventionally a
  ``<PREFIX>_REGISTRATION`` constant in the reasoner's own ``registry``
  module), labelled ``plugin:<module>``;
- the deployment's optional registration file
  (``DECISIONLEDGER_REGISTRATIONS``), for reasoners that run as their own
  services with no plugin installed here, labelled ``file:<path>``;
- the registrations written over the API
  (``PUT /api/v1/reasoners/{reasoner_id}``), labelled ``api``, which the
  ledger keeps in ``decisionledger.reasoner_registry`` and reads back on
  every start.

Plugins and the file are declared configuration: they change only when the
deployment does, and the registry refuses to replace them at runtime. An
API registration is replaced by another write: ``register`` has the
``RegistrationWriter`` store the new version (its snapshot and the
registry row, together) before the registry serves it, so the table stays
the source of truth on restart and every bundle's ``registration_version``
has a snapshot. Reads never wait for a write: a write builds a new map and
swaps it in whole. Writes are serialized by ``lock``, which the
registrations resource also holds while it admits a registration, so two
writes never admit against the same state (single-process MVP).

This module is reasoner-agnostic. The bootstrap hands the registry to the
bundle store (for replay) and to ``app/submit.py`` (for intake), and the API
serves it as the registrations resource.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from core.exceptions import RegistrationError, UnregisteredReasonerError
from core.observation import (
    ReasonerRegistration,
    registration_version,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

log = structlog.get_logger(__name__)

#: Source label of a registration passed in without one (tests, tools).
INLINE_SOURCE = "inline"
#: Source label of a registration written over the API (DR-33).
API_SOURCE = "api"


@dataclass(frozen=True)
class RegisteredReasoner:
    """One registration as the deployment serves it.

    Args:
        registration: The reasoner's registration.
        version: Its ``registration_version`` (content address).
        source: Where it came from: ``plugin:<module>`` for an installed
            reasoner plugin (the module that defines the plugin object's
            class), ``file:<path>`` for the deployment's registration file,
            ``api`` for a registration written over the API, or ``inline``.
        registered_by: Who wrote the version served now (a principal
            label); ``None`` for declared configuration.
        registered_at: When the reasoner was first registered over the API;
            ``None`` for declared configuration.
        updated_at: When the version served now was written over the API;
            ``None`` for declared configuration.
    """

    registration: ReasonerRegistration
    version: str
    source: str
    registered_by: str | None = None
    registered_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def reasoner_id(self) -> str:
        """The registered reasoner's id."""
        return self.registration.reasoner_id


@runtime_checkable
class RegistrationWriter(Protocol):
    """Where the registry keeps the registrations written at runtime.

    The reference implementation is ``app.audit.registrations.ApiRegistrations``
    (Postgres); tests use an in-memory one.
    """

    def save(
        self, registration: ReasonerRegistration, *, registered_by: str
    ) -> RegisteredReasoner:
        """Store ``registration`` as its reasoner's API registration.

        Stores the version's snapshot and the reasoner's row together, or
        neither.

        Args:
            registration: The registration to serve from now on.
            registered_by: Who wrote it (a principal label).

        Returns:
            The entry to serve, with the stored timestamps.
        """
        ...


class LiveReasonerRegistry:
    """The deployment's registrations: declared at startup, written at runtime.

    Satisfies the ``ReasonerRegistry`` protocol. Reads are lock-free (a write
    swaps in a new map); writes hold ``lock``.

    Args:
        registrations: The declared registrations (plugins, the registration
            file, or inline ones). Duplicate ``reasoner_id`` values raise
            ``ValueError``.
        sources: Optional ``reasoner_id`` → source label for
            ``registrations``; one without a label is labelled ``inline``.
        written: The registrations written over the API earlier, as stored
            (``RegistrationWriter``'s entries, read at startup).
        writer: Where ``register`` stores a registration before serving it.
            ``None`` keeps written registrations in memory only (tests,
            tools).

    Raises:
        ValueError: Two registrations share a ``reasoner_id``.
    """

    def __init__(  # noqa: D107
        self,
        registrations: Iterable[ReasonerRegistration] = (),
        *,
        sources: Mapping[str, str] | None = None,
        written: Iterable[RegisteredReasoner] = (),
        writer: RegistrationWriter | None = None,
    ) -> None:
        labels = dict(sources or {})
        store: dict[str, RegisteredReasoner] = {}
        entries = [
            RegisteredReasoner(
                registration=reg,
                version=registration_version(reg),
                source=labels.get(reg.reasoner_id, INLINE_SOURCE),
            )
            for reg in registrations
        ]
        for entry in [*entries, *written]:
            if entry.reasoner_id in store:
                raise ValueError(
                    f"Duplicate reasoner_id in registry: '{entry.reasoner_id}'"
                )
            store[entry.reasoner_id] = entry
        self._store: Mapping[str, RegisteredReasoner] = store
        self._writer = writer
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """Held by every write; hold it to admit and write as one step."""
        return self._lock

    def get(self, reasoner_id: str) -> ReasonerRegistration:
        """Return the registration for the given reasoner_id.

        Args:
            reasoner_id: The stable reasoner identifier to look up.

        Returns:
            The ``ReasonerRegistration`` for this reasoner.

        Raises:
            UnregisteredReasonerError: If ``reasoner_id`` is not registered.
        """
        return self.entry(reasoner_id).registration

    def is_registered(self, reasoner_id: str) -> bool:
        """Return True if reasoner_id has a registration entry.

        Args:
            reasoner_id: The stable reasoner identifier to check.

        Returns:
            ``True`` if registered, ``False`` otherwise.
        """
        return reasoner_id in self._store

    def entry(self, reasoner_id: str) -> RegisteredReasoner:
        """Return the registration of ``reasoner_id`` with its version and source.

        Args:
            reasoner_id: The stable reasoner identifier to look up.

        Returns:
            The registered entry.

        Raises:
            UnregisteredReasonerError: If ``reasoner_id`` is not registered.
        """
        try:
            return self._store[reasoner_id]
        except KeyError:
            raise UnregisteredReasonerError(reasoner_id) from None

    def entries(self) -> list[RegisteredReasoner]:
        """Every registered entry, sorted by ``reasoner_id``."""
        store = self._store
        return [store[reasoner_id] for reasoner_id in sorted(store)]

    def register(
        self, registration: ReasonerRegistration, *, registered_by: str
    ) -> str:
        """Serve ``registration`` from now on, as an API registration (DR-33).

        Creates the reasoner's API registration, or replaces it with a new
        version. The writer stores the version first, so a decision that
        cites it always finds its snapshot; an unchanged registration is left
        as it is (nothing is written). The caller admits the registration
        first, holding ``lock`` across the admission and this call.

        Args:
            registration: The registration to serve.
            registered_by: Who wrote it (a principal label).

        Returns:
            Its ``registration_version``.

        Raises:
            RegistrationError: The reasoner is registered by a plugin or the
                registration file (declared configuration stays declared).
        """
        reasoner_id = registration.reasoner_id
        with self._lock:
            current = self._store.get(reasoner_id)
            if current is not None and current.source != API_SOURCE:
                raise RegistrationError(
                    current.source,
                    f"reasoner_id {reasoner_id!r} is declared configuration; it "
                    "cannot be replaced at runtime",
                )
            version = registration_version(registration)
            if current is not None and current.version == version:
                return version
            if self._writer is not None:
                entry = self._writer.save(registration, registered_by=registered_by)
            else:
                now = datetime.now(UTC)
                entry = RegisteredReasoner(
                    registration=registration,
                    version=version,
                    source=API_SOURCE,
                    registered_by=registered_by,
                    registered_at=current.registered_at if current else now,
                    updated_at=now,
                )
            self._store = {**self._store, reasoner_id: entry}
        log.info(
            "registry.reasoner_registered",
            component="registry",
            reasoner_id=reasoner_id,
            registration_version=version,
            previous_version=current.version if current else None,
            registered_by=registered_by,
        )
        return version
