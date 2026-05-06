"""StaticReasonerRegistry — startup-time registry for known domain reasoners.

Loads from a list at process startup. Suitable for the MVP where the set of
registered reasoners is fixed at deployment time. A production implementation
could back this with a database or remote config service while satisfying the
same ``ReasonerRegistry`` protocol.

This module is reasoner-agnostic. Each reasoner ships its own
``ReasonerRegistration`` constant in its own package (e.g.,
``reasoner.account_takeover.registry.ATO_REGISTRATION``); the deployment
composer (``app/main.py``) collects them into a ``StaticReasonerRegistry``
and passes it to ``validate_observation()``.
"""

from __future__ import annotations

from core.exceptions import UnregisteredReasonerError
from core.observation import ReasonerRegistration  # noqa: TC001 (runtime use)


class StaticReasonerRegistry:
    """In-memory registry loaded from a list of ReasonerRegistration entries.

    Satisfies the ``ReasonerRegistry`` protocol. Thread-safe for reads after
    construction — the internal store is never mutated after ``__init__``.

    Args:
        registrations: The registrations to load. Duplicate ``reasoner_id``
            values raise ``ValueError`` at construction time.

    Raises:
        ValueError: If two registrations share the same ``reasoner_id``.
    """

    def __init__(self, registrations: list[ReasonerRegistration]) -> None:  # noqa: D107
        store: dict[str, ReasonerRegistration] = {}
        for reg in registrations:
            if reg.reasoner_id in store:
                raise ValueError(
                    f"Duplicate reasoner_id in registry: '{reg.reasoner_id}'"
                )
            store[reg.reasoner_id] = reg
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
        try:
            return self._store[reasoner_id]
        except KeyError:
            raise UnregisteredReasonerError(reasoner_id) from None

    def is_registered(self, reasoner_id: str) -> bool:
        """Return True if reasoner_id has a registration entry.

        Args:
            reasoner_id: The stable reasoner identifier to check.

        Returns:
            ``True`` if registered, ``False`` otherwise.
        """
        return reasoner_id in self._store
