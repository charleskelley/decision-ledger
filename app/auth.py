"""API keys to principals — who may write, read, and resolve (DR-31).

Every ``/api/v1`` route authenticates the caller by the key in the
``X-DecisionLedger-Key`` header and acts as the ``Principal`` that key maps
to:

- a **reasoner** principal (a per-reasoner key, bound to one ``reasoner_id``)
  writes that reasoner's decisions and reads that reasoner's decisions only.
  A write naming another reasoner is refused (403 ``REASONER_MISMATCH``);
  another tenant's decision reads as not found.
- the **operator** principal (one shared key) reads every reasoner's
  decisions and resolves held ones; it writes no decision (403
  ``REASONER_KEY_REQUIRED``).
- the **trusted** principal is what an in-process caller acts as
  (``LocalClient``): it holds no key and may do everything.

Keys come from the environment (``DECISIONLEDGER_API_KEYS``, comma-separated
``reasoner_id:key`` pairs; ``DECISIONLEDGER_OPERATOR_KEY``) or a mounted
file (``DECISIONLEDGER_API_KEYS_FILE``, YAML mapping ``reasoner_id`` to a key
or a list of keys). There is no user model, no session, and no key issuance:
the MVP's limits, documented with the deployment. A deployment with no key
at all serves only ``/health``: every other route answers 401.

The ``KeyRing`` indexes keys by their SHA-256 digest, so a lookup never
compares secret text and no key is kept in a form a log line could print.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal

import structlog
import yaml
from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from core.exceptions import LedgerError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from app.settings import FrameworkSettings

log = structlog.get_logger(__name__)

#: The request header that carries an API key.
KEY_HEADER = "X-DecisionLedger-Key"

#: What kind of caller a principal is.
PrincipalKind = Literal["reasoner", "operator", "trusted"]


@dataclass(frozen=True)
class Principal:
    """The caller a request acts as.

    Args:
        kind: ``reasoner`` (a per-reasoner key), ``operator`` (the operator
            key), or ``trusted`` (an in-process caller).
        reasoner_id: The reasoner a ``reasoner`` principal is bound to;
            ``None`` otherwise.
    """

    kind: PrincipalKind
    reasoner_id: str | None = None

    @classmethod
    def for_reasoner(cls, reasoner_id: str) -> Principal:
        """The principal a reasoner's key maps to."""
        return cls(kind="reasoner", reasoner_id=reasoner_id)

    @property
    def label(self) -> str:
        """A log-safe description (``reasoner:<id>``, ``operator``, ``trusted``)."""
        return f"reasoner:{self.reasoner_id}" if self.kind == "reasoner" else self.kind

    def may_submit_for(self, reasoner_id: str) -> bool:
        """Whether this principal may write a decision for ``reasoner_id``."""
        return self.kind == "trusted" or (
            self.kind == "reasoner" and self.reasoner_id == reasoner_id
        )

    def may_read(self, reasoner_id: str) -> bool:
        """Whether this principal may read a decision of ``reasoner_id``."""
        return self.kind in ("trusted", "operator") or self.reasoner_id == reasoner_id

    @property
    def may_resolve(self) -> bool:
        """Whether this principal may resolve held decisions."""
        return self.kind in ("trusted", "operator")


#: The principal an in-process caller (``LocalClient``) acts as.
TRUSTED = Principal(kind="trusted")
#: The principal the operator key maps to.
OPERATOR = Principal(kind="operator")


def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


class KeyRing:
    """The deployment's API keys, indexed by digest, mapped to principals.

    Args:
        reasoner_keys: ``reasoner_id`` → its keys.
        operator_key: The operator key, or ``None`` for no operator access.

    Raises:
        ValueError: A key is blank, or one key is given to two principals.
    """

    def __init__(  # noqa: D107
        self,
        reasoner_keys: Mapping[str, Iterable[str]] | None = None,
        operator_key: str | None = None,
    ) -> None:
        principals: dict[bytes, Principal] = {}

        def _add(key: str, principal: Principal) -> None:
            if not key.strip():
                msg = f"a blank API key was configured for {principal.label}"
                raise ValueError(msg)
            digest = _digest(key)
            holder = principals.get(digest)
            if holder is not None and holder != principal:
                msg = (
                    f"one API key is configured for both {holder.label} and "
                    f"{principal.label}; every principal needs its own key"
                )
                raise ValueError(msg)
            principals[digest] = principal

        for reasoner_id, keys in (reasoner_keys or {}).items():
            for key in keys:
                _add(key, Principal.for_reasoner(reasoner_id))
        if operator_key:
            _add(operator_key, OPERATOR)
        self._principals = principals

    @classmethod
    def from_settings(cls, settings: FrameworkSettings) -> KeyRing:
        """Build the key ring from the environment and the mounted key file.

        Args:
            settings: Framework settings carrying ``DECISIONLEDGER_API_KEYS``,
                ``DECISIONLEDGER_API_KEYS_FILE``, and
                ``DECISIONLEDGER_OPERATOR_KEY``.

        Returns:
            The key ring.

        Raises:
            ValueError: A key entry is malformed, the key file cannot be
                read, or one key is given to two principals.
        """
        reasoner_keys = parse_api_keys(
            settings.decisionledger_api_keys.get_secret_value()
        )
        path = settings.api_keys_path
        if path is not None:
            for reasoner_id, keys in load_api_keys_file(path).items():
                reasoner_keys.setdefault(reasoner_id, []).extend(keys)
        operator_key = settings.decisionledger_operator_key.get_secret_value().strip()
        return cls(reasoner_keys, operator_key or None)

    @property
    def empty(self) -> bool:
        """``True`` when no key is configured (every request is refused)."""
        return not self._principals

    @property
    def reasoner_ids(self) -> frozenset[str]:
        """Every reasoner that holds at least one key."""
        return frozenset(
            p.reasoner_id for p in self._principals.values() if p.reasoner_id
        )

    def principal(self, key: str | None) -> Principal | None:
        """Return the principal ``key`` maps to, or ``None`` for an unknown key."""
        if not key:
            return None
        return self._principals.get(_digest(key))


def parse_api_keys(value: str) -> dict[str, list[str]]:
    """Parse ``DECISIONLEDGER_API_KEYS``: comma-separated ``reasoner_id:key`` pairs.

    A pair splits at its first colon, so a key may itself contain colons.

    Args:
        value: The setting's value (blank for none).

    Returns:
        ``reasoner_id`` → its keys, in the order given.

    Raises:
        ValueError: A pair has no colon, or an empty id or key.
    """
    keys: dict[str, list[str]] = {}
    for raw in value.split(","):
        pair = raw.strip()
        if not pair:
            continue
        reasoner_id, sep, key = pair.partition(":")
        if not sep or not reasoner_id.strip() or not key.strip():
            msg = (
                "DECISIONLEDGER_API_KEYS entries are 'reasoner_id:key' pairs; "
                "an entry has no id or no key"
            )
            raise ValueError(msg)
        keys.setdefault(reasoner_id.strip(), []).append(key.strip())
    return keys


def load_api_keys_file(path: Path) -> dict[str, list[str]]:
    """Read a mounted key file: YAML mapping ``reasoner_id`` to a key or keys.

    Args:
        path: The file (``DECISIONLEDGER_API_KEYS_FILE``).

    Returns:
        ``reasoner_id`` → its keys.

    Raises:
        ValueError: The file cannot be read or is not such a mapping.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        msg = f"cannot read the API key file {path}: {err.strerror}"
        raise ValueError(msg) from None
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as err:
        # The parser's message quotes the offending source lines, which hold
        # keys: report the position only, and do not chain the error.
        mark = getattr(err, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        msg = f"the API key file {path} is not valid YAML{where}"
        raise ValueError(msg) from None
    if not isinstance(data, dict):
        msg = f"the API key file {path} must map reasoner_id to a key or keys"
        raise ValueError(msg)
    keys: dict[str, list[str]] = {}
    for reasoner_id, entry in data.items():
        values = entry if isinstance(entry, list) else [entry]
        if not all(isinstance(v, str) and v.strip() for v in values):
            msg = f"the API key file {path} has a non-text key for {reasoner_id!r}"
            raise ValueError(msg)
        keys[str(reasoner_id)] = [v.strip() for v in values]
    return keys


#: OpenAPI-documented header scheme; ``auto_error=False`` so a missing key
#: becomes the ledger's own problem response instead of FastAPI's.
api_key_header = APIKeyHeader(
    name=KEY_HEADER,
    auto_error=False,
    description=(
        "A reasoner's API key (writes and reads its own decisions) or the "
        "operator key (reads everything, resolves held decisions)."
    ),
)


def authenticate(
    request: Request, key: str | None = Depends(api_key_header)
) -> Principal:
    """FastAPI dependency: the principal the request's API key maps to.

    Args:
        request: The request (its app holds the ``KeyRing``).
        key: The ``X-DecisionLedger-Key`` header value.

    Returns:
        The caller's principal.

    Raises:
        LedgerError: 401 ``UNAUTHENTICATED`` when the key is missing or
            unknown.
    """
    keyring: KeyRing = request.app.state.keyring
    if not key:
        raise LedgerError(
            401, "UNAUTHENTICATED", f"send an API key in the {KEY_HEADER} header"
        )
    principal = keyring.principal(key)
    if principal is None:
        raise LedgerError(401, "UNAUTHENTICATED", "the API key is not recognized")
    return principal


#: A route parameter that authenticates the request: ``principal: Caller``.
Caller = Annotated[Principal, Depends(authenticate)]


def require_submitter(principal: Principal, reasoner_id: str) -> None:
    """Refuse a write the principal may not make for ``reasoner_id``.

    Raises:
        LedgerError: 403 ``REASONER_KEY_REQUIRED`` for the operator, 403
            ``REASONER_MISMATCH`` for another reasoner's key.
    """
    if principal.may_submit_for(reasoner_id):
        return
    if principal.kind == "operator":
        raise LedgerError(
            403,
            "REASONER_KEY_REQUIRED",
            "decisions are written with the submitting reasoner's own key, "
            "not the operator key",
        )
    raise LedgerError(
        403,
        "REASONER_MISMATCH",
        f"this key belongs to reasoner {principal.reasoner_id!r}; it cannot "
        f"write a decision for {reasoner_id!r}",
        extra={"reasoner_id": reasoner_id, "key_reasoner_id": principal.reasoner_id},
    )


def require_operator(principal: Principal) -> None:
    """Refuse a resolution the principal may not make.

    Raises:
        LedgerError: 403 ``OPERATOR_REQUIRED``.
    """
    if not principal.may_resolve:
        raise LedgerError(
            403, "OPERATOR_REQUIRED", "resolving a decision needs the operator key"
        )
