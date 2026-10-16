"""Ledger clients — how a reasoner submits to the ledger, in process or over HTTP.

A reasoner never calls ``app.submit`` directly any more (DR-31). It talks to
the ledger through a ``LedgerClient``:

- ``HttpClient`` speaks to a ledger service over HTTP (``httpx``), with the
  reasoner's API key in the ``X-DecisionLedger-Key`` header. A reasoner that
  runs as its own service uses it.
- ``LocalClient`` runs the ledger in the caller's process. It serializes the
  same ``Submission`` and calls the same operation the HTTP route calls
  (``app.api.decisions.create_decision`` and its siblings), acting as the
  trusted in-process principal. Eval drivers, tests, and a reasoner that
  embeds the ledger use it.

Both return the same wire models (``app.api.models``) and raise the same
``core.exceptions.LedgerError`` (``status`` and stable ``code``), so code
written against one runs against the other. ``submit`` returns the
decision's receipt; ``get_bundle`` reads the full ``DecisionBundle``.

The methods are coroutines; close a client with ``aclose()`` (or use it as
an async context manager). ``HttpClient`` does not retry: a decision write
is not idempotent on the wire, and a retried timeout could record twice.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol, Self, runtime_checkable

import httpx

from app.api.decisions import (
    create_decision,
    lookup_decision,
    record_batch,
    replay_decision,
    resolve_decision,
)
from app.api.entities import DEFAULT_LIMIT, entity_decisions
from app.api.gates import list_gates
from app.api.identity import resolve_identity
from app.api.models import (
    BatchResult,
    DecisionReceipt,
    EntityDecisions,
    GateList,
    ReasonerList,
    RegisteredReasonerView,
    ReplayResponse,
    ResolutionReceipt,
    ResolutionRequest,
)
from app.api.registrations import get_reasoner, list_reasoners
from app.auth import KEY_HEADER, TRUSTED, Principal
from core.bundle import DecisionBundle
from core.exceptions import LedgerError
from core.identity import ResolvedIdentity

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from types import TracebackType
    from uuid import UUID

    from app.plugin import FrameworkServices
    from app.settings import FrameworkSettings
    from core.submission import Submission

#: Default request timeout of ``HttpClient``, in seconds. A gated decision
#: runs two gate phases one after the other (mandatory, then advisory), each
#: bounded by the ledger's per-gate budget (45 s), so this outlasts both.
DEFAULT_TIMEOUT_S = 120.0

#: Media type of a batch body.
NDJSON = "application/x-ndjson"


@runtime_checkable
class LedgerClient(Protocol):
    """What a reasoner (or an agent tool) needs from the ledger."""

    async def submit(self, submission: Submission) -> DecisionReceipt:
        """Record or gate one decision (its ``mode`` says which)."""
        ...

    async def submit_batch(self, submissions: Iterable[Submission]) -> BatchResult:
        """Record many decisions (``mode: record`` only) in one request."""
        ...

    async def get_decision(self, decision_id: str) -> dict[str, Any]:
        """The stored bundle as JSON, any schema version."""
        ...

    async def get_bundle(self, decision_id: str) -> DecisionBundle:
        """The stored bundle, typed (schema version 2)."""
        ...

    async def replay(self, decision_id: str) -> ReplayResponse:
        """Replay enforcement on the bundle's cached states."""
        ...

    async def entity_decisions(
        self,
        entity_id: str | UUID,
        *,
        reasoner_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
        before: datetime | None = None,
        before_id: str | None = None,
        include_related: str | None = None,
    ) -> EntityDecisions:
        """A subject's decisions, newest first (page with ``before``, ``before_id``).

        Covers the ids merged with the subject; ``include_related`` adds the
        subjects one relationship of that type away (DR-32).
        """
        ...

    async def resolve_identity(
        self, native_namespace: str, native_subject_id: str
    ) -> ResolvedIdentity:
        """The subject a native subject resolves to (DR-32)."""
        ...

    async def resolve(
        self, decision_id: str, resolution: ResolutionRequest
    ) -> ResolutionReceipt:
        """Resolve a held or challenged decision (operator)."""
        ...

    async def reasoners(self) -> ReasonerList:
        """Every registration the ledger serves."""
        ...

    async def reasoner(self, reasoner_id: str) -> RegisteredReasonerView:
        """One registration, as served now."""
        ...

    async def gates(self) -> GateList:
        """Every gate the ledger composes."""
        ...

    async def aclose(self) -> None:
        """Release the client's resources."""
        ...


def _typed_bundle(document: dict[str, Any], decision_id: str) -> DecisionBundle:
    """Parse a looked-up bundle; refuse one older than schema version 2."""
    version = document.get("schema_version")
    if version != 2:
        raise LedgerError(
            409,
            "BUNDLE_SCHEMA_UNSUPPORTED",
            f"decision {decision_id!r} is stored as schema version {version}; "
            "a typed bundle needs schema version 2",
            extra={"schema_version": version, "supported_schema_version": 2},
        )
    return DecisionBundle.model_validate_json(json.dumps(document))


class LocalClient:
    """The ledger in the caller's process, through the same operations as HTTP.

    Args:
        framework: The ledger's services (``build_framework_services``).
        principal: Who the calls act as; the trusted in-process principal by
            default. Pass a reasoner or operator principal to exercise the
            same authorization the HTTP routes apply.
        owns_framework: Close ``framework`` in ``aclose()`` (set by
            ``from_settings``).
    """

    def __init__(
        self,
        framework: FrameworkServices,
        *,
        principal: Principal = TRUSTED,
        owns_framework: bool = False,
    ) -> None:
        """Bind the client to the ledger's services."""
        self._framework = framework
        self._principal = principal
        self._owns_framework = owns_framework

    @classmethod
    def from_settings(cls, settings: FrameworkSettings | None = None) -> LocalClient:
        """Compose the deployment in process, exactly as ``create_app`` does.

        Discovers the reasoner plugins (filtered by
        ``DECISIONLEDGER_REASONERS``) and the gate plugins, reads the
        registration file, and builds the framework services, which the
        client then owns and closes.

        Args:
            settings: Framework settings; read from the environment when
                omitted.

        Returns:
            A client that closes the framework services in ``aclose()``.
        """
        from app.bootstrap import build_framework_services
        from app.gates import discover_gates
        from app.plugins import (
            allowlist_from_settings,
            discover_plugins,
        )
        from app.settings import FrameworkSettings

        settings = settings if settings is not None else FrameworkSettings()
        plugins = discover_plugins(allow=allowlist_from_settings(settings))
        framework = build_framework_services(settings, plugins, discover_gates())
        return cls(framework, owns_framework=True)

    @property
    def framework(self) -> FrameworkServices:
        """The ledger services this client runs on."""
        return self._framework

    def with_framework(self, framework: FrameworkServices) -> LocalClient:
        """A client acting as the same principal on other services (not owned)."""
        return LocalClient(framework, principal=self._principal)

    async def submit(self, submission: Submission) -> DecisionReceipt:
        """Record or gate one decision; return its receipt.

        Raises:
            LedgerError: As ``POST /api/v1/decisions`` would answer.
        """
        bundle = await create_decision(
            submission.model_dump_json(),
            principal=self._principal,
            framework=self._framework,
        )
        return DecisionReceipt.from_bundle(bundle)

    async def submit_batch(self, submissions: Iterable[Submission]) -> BatchResult:
        """Record many decisions; one result per submission, in order."""
        body = "".join(f"{s.model_dump_json()}\n" for s in submissions)
        return await record_batch(
            body, principal=self._principal, framework=self._framework
        )

    async def get_decision(self, decision_id: str) -> dict[str, Any]:
        """The stored bundle as JSON (any schema version)."""
        return lookup_decision(
            decision_id, principal=self._principal, framework=self._framework
        )

    async def get_bundle(self, decision_id: str) -> DecisionBundle:
        """The stored bundle, typed (schema version 2)."""
        return _typed_bundle(await self.get_decision(decision_id), decision_id)

    async def replay(self, decision_id: str) -> ReplayResponse:
        """Replay enforcement on the bundle's cached states."""
        return replay_decision(
            decision_id, principal=self._principal, framework=self._framework
        )

    async def entity_decisions(
        self,
        entity_id: str | UUID,
        *,
        reasoner_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
        before: datetime | None = None,
        before_id: str | None = None,
        include_related: str | None = None,
    ) -> EntityDecisions:
        """A subject's decisions, newest first (page with ``before``, ``before_id``).

        Covers the ids merged with the subject; ``include_related`` adds the
        subjects one relationship of that type away.
        """
        return entity_decisions(
            str(entity_id),
            principal=self._principal,
            framework=self._framework,
            reasoner_id=reasoner_id,
            limit=limit,
            before=before,
            before_id=before_id,
            include_related=include_related,
        )

    async def resolve_identity(
        self, native_namespace: str, native_subject_id: str
    ) -> ResolvedIdentity:
        """The subject a native subject resolves to.

        Raises:
            LedgerError: As ``GET /api/v1/identity/resolve`` would answer.
        """
        return await resolve_identity(
            native_namespace,
            native_subject_id,
            principal=self._principal,
            framework=self._framework,
        )

    async def resolve(
        self, decision_id: str, resolution: ResolutionRequest
    ) -> ResolutionReceipt:
        """Resolve a held or challenged decision."""
        return resolve_decision(
            decision_id,
            resolution.model_dump_json(),
            principal=self._principal,
            framework=self._framework,
        )

    async def reasoners(self) -> ReasonerList:
        """Every registration the ledger serves."""
        return list_reasoners(principal=self._principal, framework=self._framework)

    async def reasoner(self, reasoner_id: str) -> RegisteredReasonerView:
        """One registration, as served now."""
        return get_reasoner(
            reasoner_id, principal=self._principal, framework=self._framework
        )

    async def gates(self) -> GateList:
        """Every gate the ledger composes."""
        return list_gates(principal=self._principal, framework=self._framework)

    async def aclose(self) -> None:
        """Close the framework services when this client composed them."""
        if self._owns_framework:
            self._framework.close()

    async def __aenter__(self) -> Self:
        """Return the client."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client."""
        await self.aclose()


class HttpClient:
    """A ledger service over HTTP.

    Args:
        base_url: The ledger's base URL (``http://localhost:8000``).
        api_key: The reasoner's API key (or the operator key, for an
            operator-only client), sent as ``X-DecisionLedger-Key``.
            Surrounding whitespace is dropped, as the ledger drops it.
        operator_key: The operator key, sent instead of ``api_key`` on
            resolutions when given.
        timeout: Request timeout in seconds (every call but a batch).
        batch_timeout: Read timeout of a batch, in seconds; ``None`` (the
            default) waits as long as the ledger works — a batch decides
            every line's mandatory gates before it answers.
        transport: An ``httpx`` transport to send through instead of the
            network (tests pass ``httpx.ASGITransport(app)``).

    Raises:
        ValueError: A key holds a character an HTTP header cannot carry
            (the message never repeats the key).
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        operator_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        batch_timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Open the HTTP connection pool (lazily; nothing connects yet)."""
        self._api_key = _header_key(api_key, "api_key")
        self._operator_key = _header_key(operator_key, "operator_key")
        self._timeout = timeout
        self._batch_timeout = httpx.Timeout(timeout, read=batch_timeout)
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def _headers(
        self, key: str | None, content_type: str | None = None
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if key:
            headers[KEY_HEADER] = key
        if content_type:
            headers["content-type"] = content_type
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        key: str | None = None,
        content: str | None = None,
        content_type: str | None = None,
        params: dict[str, Any] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response:
        response = await self._http.request(
            method,
            path,
            content=content,
            params=params,
            headers=self._headers(key or self._api_key, content_type),
            timeout=timeout if timeout is not None else self._timeout,
        )
        raise_for_problem(response)
        return response

    async def submit(self, submission: Submission) -> DecisionReceipt:
        """Record or gate one decision; return its receipt.

        Raises:
            LedgerError: The ledger's problem response, or ``HTTP_<status>``
                for a non-problem error.
        """
        response = await self._request(
            "POST",
            "/api/v1/decisions",
            content=submission.model_dump_json(),
            content_type="application/json",
        )
        return DecisionReceipt.model_validate_json(response.content)

    async def submit_batch(self, submissions: Iterable[Submission]) -> BatchResult:
        """Record many decisions in one NDJSON request."""
        body = "".join(f"{s.model_dump_json()}\n" for s in submissions)
        response = await self._request(
            "POST",
            "/api/v1/decisions:batch",
            content=body,
            content_type=NDJSON,
            timeout=self._batch_timeout,
        )
        return BatchResult.model_validate_json(response.content)

    async def get_decision(self, decision_id: str) -> dict[str, Any]:
        """The stored bundle as JSON (any schema version)."""
        response = await self._request("GET", f"/api/v1/decisions/{decision_id}")
        return response.json()

    async def get_bundle(self, decision_id: str) -> DecisionBundle:
        """The stored bundle, typed (schema version 2)."""
        return _typed_bundle(await self.get_decision(decision_id), decision_id)

    async def replay(self, decision_id: str) -> ReplayResponse:
        """Replay enforcement on the bundle's cached states."""
        response = await self._request(
            "POST", f"/api/v1/decisions/{decision_id}/replay"
        )
        return ReplayResponse.model_validate_json(response.content)

    async def entity_decisions(
        self,
        entity_id: str | UUID,
        *,
        reasoner_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
        before: datetime | None = None,
        before_id: str | None = None,
        include_related: str | None = None,
    ) -> EntityDecisions:
        """A subject's decisions, newest first (page with ``before``, ``before_id``).

        Covers the ids merged with the subject; ``include_related`` adds the
        subjects one relationship of that type away.
        """
        params: dict[str, Any] = {"limit": limit}
        if reasoner_id is not None:
            params["reasoner_id"] = reasoner_id
        if before is not None:
            params["before"] = before.isoformat()
        if before_id is not None:
            params["before_id"] = before_id
        if include_related is not None:
            params["include_related"] = include_related
        response = await self._request(
            "GET", f"/api/v1/entities/{entity_id}/decisions", params=params
        )
        return EntityDecisions.model_validate_json(response.content)

    async def resolve_identity(
        self, native_namespace: str, native_subject_id: str
    ) -> ResolvedIdentity:
        """The subject a native subject resolves to."""
        response = await self._request(
            "GET",
            "/api/v1/identity/resolve",
            params={"namespace": native_namespace, "native_id": native_subject_id},
        )
        return ResolvedIdentity.model_validate_json(response.content)

    async def resolve(
        self, decision_id: str, resolution: ResolutionRequest
    ) -> ResolutionReceipt:
        """Resolve a held or challenged decision (operator key)."""
        response = await self._request(
            "POST",
            f"/api/v1/decisions/{decision_id}/resolutions",
            key=self._operator_key,
            content=resolution.model_dump_json(),
            content_type="application/json",
        )
        return ResolutionReceipt.model_validate_json(response.content)

    async def reasoners(self) -> ReasonerList:
        """Every registration the ledger serves."""
        response = await self._request("GET", "/api/v1/reasoners")
        return ReasonerList.model_validate_json(response.content)

    async def reasoner(self, reasoner_id: str) -> RegisteredReasonerView:
        """One registration, as served now."""
        response = await self._request("GET", f"/api/v1/reasoners/{reasoner_id}")
        return RegisteredReasonerView.model_validate_json(response.content)

    async def gates(self) -> GateList:
        """Every gate the ledger composes."""
        response = await self._request("GET", "/api/v1/gates")
        return GateList.model_validate_json(response.content)

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        """Return the client."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client."""
        await self.aclose()


def _header_key(key: str | None, name: str) -> str | None:
    """A key as the ledger compares it: stripped; ``None`` when blank.

    Raises:
        ValueError: The key holds a character outside visible ASCII, which an
            HTTP header cannot carry (the message names the argument, never
            the key).
    """
    if key is None or not key.strip():
        return None
    key = key.strip()
    if not all(0x21 <= ord(char) <= 0x7E for char in key):
        msg = f"{name} must be visible ASCII with no spaces (the key is not shown)"
        raise ValueError(msg)
    return key


#: Members of a problem document that are not extension members.
_PROBLEM_MEMBERS = frozenset({"type", "title", "status", "detail", "code", "instance"})


def raise_for_problem(response: httpx.Response) -> None:
    """Raise the ``LedgerError`` an error response carries; pass a success.

    Args:
        response: A ledger response.

    Raises:
        LedgerError: With the problem's ``status``, ``code``, ``detail``,
            ``title``, and extension members; ``HTTP_<status>`` with the
            body's start when the response is not a problem document.
    """
    if response.is_success:
        return
    try:
        document = response.json()
    except ValueError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("code"), str):
        raise LedgerError(
            int(document.get("status", response.status_code)),
            document["code"],
            str(document.get("detail", "")),
            title=document.get("title"),
            extra={k: v for k, v in document.items() if k not in _PROBLEM_MEMBERS},
        )
    raise LedgerError(
        response.status_code,
        f"HTTP_{response.status_code}",
        response.text[:500] or response.reason_phrase,
    )
