"""StaticReasonerRegistry — startup-time registry for known domain reasoners.

Loads from a list at process startup. Suitable for the MVP where the set of
registered reasoners is fixed at deployment time. A production implementation
could back this with a database or remote config service while satisfying the
same ``ReasonerRegistry`` protocol.

This module is reasoner-agnostic. Registrations reach it from two sources
(DR-31), both composed by ``app/plugins.py:build_registry``: each installed
reasoner plugin's ``registration`` (conventionally a ``<PREFIX>_REGISTRATION``
constant in the reasoner's own ``registry`` module), and the deployment's
optional registration file (``DECISIONLEDGER_REGISTRATIONS``), for reasoners
that run as their own services with no plugin installed here. The bootstrap
hands the registry to the bundle store (for replay) and to ``app/submit.py``
(for intake), and the API serves it read-only as the registrations resource.

Every entry carries its ``registration_version`` (the content address every
bundle snapshots) and the source it came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.exceptions import UnregisteredReasonerError
from core.observation import (
    ReasonerRegistration,
    registration_version,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Source label of a registration passed in without one (tests, tools).
INLINE_SOURCE = "inline"


@dataclass(frozen=True)
class RegisteredReasoner:
    """One registration as the deployment serves it.

    Args:
        registration: The reasoner's registration.
        version: Its ``registration_version`` (content address).
        source: Where it came from: ``plugin:<module>`` for an installed
            reasoner plugin (the module that defines the plugin object's
            class), ``file:<path>`` for the deployment's registration file,
            or ``inline``.
    """

    registration: ReasonerRegistration
    version: str
    source: str

    @property
    def reasoner_id(self) -> str:
        """The registered reasoner's id."""
        return self.registration.reasoner_id


class StaticReasonerRegistry:
    """In-memory registry loaded from a list of ReasonerRegistration entries.

    Satisfies the ``ReasonerRegistry`` protocol. Thread-safe for reads after
    construction — the internal store is never mutated after ``__init__``.

    Args:
        registrations: The registrations to load. Duplicate ``reasoner_id``
            values raise ``ValueError`` at construction time.
        sources: Optional ``reasoner_id`` → source label; a registration
            without one is labelled ``inline``.

    Raises:
        ValueError: If two registrations share the same ``reasoner_id``.
    """

    def __init__(  # noqa: D107
        self,
        registrations: list[ReasonerRegistration],
        *,
        sources: Mapping[str, str] | None = None,
    ) -> None:
        labels = dict(sources or {})
        store: dict[str, RegisteredReasoner] = {}
        for reg in registrations:
            if reg.reasoner_id in store:
                raise ValueError(
                    f"Duplicate reasoner_id in registry: '{reg.reasoner_id}'"
                )
            store[reg.reasoner_id] = RegisteredReasoner(
                registration=reg,
                version=registration_version(reg),
                source=labels.get(reg.reasoner_id, INLINE_SOURCE),
            )
        self._store = store

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
        return [self._store[reasoner_id] for reasoner_id in sorted(self._store)]
