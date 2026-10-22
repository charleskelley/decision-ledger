"""Decision routes — write, batch-record, look up, replay, verify, and resolve.

Each route is a thin HTTP shell over an *operation*: a plain function that
takes the request's raw body (or path values), the caller's ``Principal``,
and the ``FrameworkServices``, and either returns a result or raises a
``LedgerError``. ``app.client.LocalClient`` calls the same operations with
the same serialized input, so a decision made in process and one made over
HTTP go through identical code.

Routes:

- ``POST /api/v1/decisions`` — one ``Submission`` (``mode`` in the body):
  record a decision the reasoner made, or ask the ledger to gate one. 201
  with a ``DecisionReceipt`` and a ``Location`` header.
- ``POST /api/v1/decisions:batch`` — NDJSON, one recorded ``Submission`` per
  line (a batch is record-only: no advisory gate runs). Every line is
  decided like a single record, mandatory gates included, and the bundles
  are written in one round trip. 200 with one result per line.
- ``GET /api/v1/decisions/{decision_id}`` — the stored bundle, any schema
  version.
- ``POST /api/v1/decisions/{decision_id}/replay`` — re-run enforcement on the
  bundle's cached states, against the registration version it cites.
- ``POST /api/v1/decisions/{decision_id}/verify`` — say how the evidence the
  decision relied on stands now (DR-34): for each evidence reference of each
  gate that ran, whether the evidence escrow holds the artifact it names,
  and what its gate reports when it checks the reference against its own
  record (``?live=true``: against its source as it is now). Replay never
  does this; verify decides nothing again.
- ``POST /api/v1/decisions/{decision_id}/resolutions`` — a human resolution
  of a held or challenged decision (operator key).

A reasoner key reads only its own reasoner's decisions; another tenant's
decision reads as ``404 DECISION_NOT_FOUND``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import structlog
from fastapi import APIRouter, Request, Response
from pydantic import ValidationError

from app.api.errors import invalid, problem_responses
from app.api.models import (
    BatchLineResult,
    BatchResult,
    DecisionReceipt,
    EscrowStatus,
    EvidenceVerification,
    GateReplayResult,
    ReplayResponse,
    ResolutionReceipt,
    ResolutionRequest,
    VerificationSummary,
    VerifiedReference,
)
from app.audit import as_bundle
from app.auth import Caller, require_operator, require_submitter
from app.gates import verify_evidence
from app.monitoring import duration_ms
from app.submit import DEFAULT_GATE_BUDGET_MS, decide, submit
from core.actions import DecisionAction
from core.bundle import BUNDLE_SCHEMA_VERSION
from core.exceptions import (
    BundleSchemaUnsupportedError,
    LedgerError,
    RegistrationSnapshotMissingError,
    SubmissionContractError,
    UnregisteredReasonerError,
)
from core.resolution import HumanResolutionAttempt, ResolutionStatus
from core.submission import Submission

if TYPE_CHECKING:
    from app.audit import StoredBundle
    from app.auth import Principal
    from app.plugin import FrameworkServices
    from core.bundle import DecisionBundle

log = structlog.get_logger(__name__)

#: Most lines one batch may carry.
MAX_BATCH_LINES = 10_000
#: Lines of one batch decided concurrently (their mandatory gates run then).
BATCH_CONCURRENCY = 32
#: Largest body of one submission or one resolution, in bytes.
MAX_SUBMISSION_BYTES = 1 << 20
#: Largest body of one batch, in bytes.
MAX_BATCH_BYTES = 64 << 20

router = APIRouter(prefix="/api/v1", tags=["decisions"])


def _framework(request: Request) -> FrameworkServices:
    return request.app.state.framework


# ---------------------------------------------------------------------------
# Operations (shared by the routes and LocalClient)
# ---------------------------------------------------------------------------


def _too_large(limit: int) -> LedgerError:
    return LedgerError(
        413,
        "BODY_TOO_LARGE",
        f"the request body exceeds {limit} bytes",
        extra={"limit_bytes": limit},
    )


def _bounded(body: bytes | str, limit: int) -> bytes:
    """The body as bytes, refused when it is longer than ``limit``."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    if len(data) > limit:
        raise _too_large(limit)
    return data


async def read_body(request: Request, *, limit: int) -> bytes:
    """Read a request body, refusing one longer than ``limit`` bytes.

    A declared ``Content-Length`` over the limit is refused before anything
    is read; otherwise the body is read in chunks and refused as soon as it
    passes the limit, so an oversized body is never held whole.

    Raises:
        LedgerError: 413 ``BODY_TOO_LARGE``.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def _ndjson_lines(data: bytes) -> list[tuple[int, bytes]]:
    """The non-blank lines of an NDJSON body with their 1-based line numbers.

    Lines end at a line feed only (a trailing carriage return is dropped), as NDJSON
    specifies: JSON strings may hold U+2028, U+2029, and U+0085 raw, so a
    general line splitter would cut a valid submission in two. Lines stay
    bytes, so a line that is not valid UTF-8 fails its own parse instead of
    being silently repaired.

    Raises:
        LedgerError: 413 ``BATCH_TOO_LARGE`` as soon as the body holds more
            than ``MAX_BATCH_LINES`` non-blank lines.
    """
    lines: list[tuple[int, bytes]] = []
    start, number = 0, 0
    while start <= len(data):
        end = data.find(b"\n", start)
        if end == -1:
            end = len(data)
        number += 1
        line = data[start:end].removesuffix(b"\r")
        if line.strip():
            lines.append((number, line))
            if len(lines) > MAX_BATCH_LINES:
                raise LedgerError(
                    413,
                    "BATCH_TOO_LARGE",
                    f"a batch holds at most {MAX_BATCH_LINES} lines",
                    extra={"max_lines": MAX_BATCH_LINES},
                )
        start = end + 1
    return lines


def parse_submission(body: bytes | str) -> Submission:
    """Parse a wire submission (JSON text), strictly.

    Raises:
        LedgerError: 422 ``SUBMISSION_INVALID`` with the validation errors.
    """
    try:
        return Submission.model_validate_json(body)
    except ValidationError as err:
        raise invalid(
            "SUBMISSION_INVALID", "the body is not a valid Submission", err
        ) from err


def _contract_error(err: SubmissionContractError) -> LedgerError:
    return LedgerError(
        422, err.code, err.violation, extra={"reasoner_id": err.reasoner_id}
    )


def _unregistered(err: UnregisteredReasonerError) -> LedgerError:
    return LedgerError(
        422,
        "REASONER_NOT_REGISTERED",
        f"reasoner {err.reasoner_id!r} is not registered with this ledger",
        extra={"reasoner_id": err.reasoner_id},
    )


async def create_decision(
    body: bytes | str, *, principal: Principal, framework: FrameworkServices
) -> DecisionBundle:
    """Decide one submission and write its bundle.

    Args:
        body: The submission as JSON text.
        principal: The caller.
        framework: The ledger's services.

    Returns:
        The written bundle.

    Raises:
        LedgerError: 413 ``BODY_TOO_LARGE``; 422 ``SUBMISSION_INVALID``; 403
            ``REASONER_MISMATCH`` / ``REASONER_KEY_REQUIRED``; 422
            ``REASONER_NOT_REGISTERED``; 422 with a ``SubmissionContractError``
            code.
    """
    submission = parse_submission(_bounded(body, MAX_SUBMISSION_BYTES))
    require_submitter(principal, submission.reasoner_id)
    try:
        bundle = await submit(
            submission=submission,
            gates=framework.gates,
            store=framework.store,
            registry=framework.registry,
            ingestion_timestamp=datetime.now(UTC),
            identity=framework.identity,
        )
    except UnregisteredReasonerError as err:
        raise _unregistered(err) from err
    except SubmissionContractError as err:
        raise _contract_error(err) from err
    log.info(
        "api.decision_written",
        component="api",
        decision_id=bundle.decision_id,
        event_id=submission.event_id,
        reasoner_id=submission.reasoner_id,
        mode=submission.mode,
        decision_action=bundle.decision_action.value,
        principal=principal.label,
    )
    return bundle


def _rejected(line: int, err: LedgerError, event_id: str | None) -> BatchLineResult:
    return BatchLineResult(
        line=line,
        status="rejected",
        event_id=event_id,
        code=err.code,
        detail=err.detail,
    )


async def record_batch(
    body: bytes | str, *, principal: Principal, framework: FrameworkServices
) -> BatchResult:
    """Record every line of an NDJSON batch; write the bundles in one round trip.

    Each non-blank line (lines end at a line feed) must be a recorded submission
    (``mode: record``) of a reasoner the caller may write for. A line that
    does not parse (invalid UTF-8 included), asks for a gate, or breaks its
    registration is rejected on its own; the other lines are still
    recorded. A line naming a reasoner the caller may not write for refuses
    the whole batch.

    Args:
        body: NDJSON: one ``Submission`` per line.
        principal: The caller.
        framework: The ledger's services.

    Returns:
        One result per non-blank line, in line order.

    Raises:
        LedgerError: 413 ``BODY_TOO_LARGE`` or ``BATCH_TOO_LARGE``; 422
            ``BATCH_EMPTY``; 403 ``REASONER_MISMATCH`` /
            ``REASONER_KEY_REQUIRED``.
    """
    lines = _ndjson_lines(_bounded(body, MAX_BATCH_BYTES))
    if not lines:
        raise LedgerError(422, "BATCH_EMPTY", "the batch holds no submission")

    results: dict[int, BatchLineResult] = {}
    parsed: list[tuple[int, Submission]] = []
    for number, line in lines:
        try:
            submission = parse_submission(line)
        except LedgerError as err:
            results[number] = _rejected(number, err, None)
            continue
        parsed.append((number, submission))

    for _, submission in parsed:
        require_submitter(principal, submission.reasoner_id)

    received_at = datetime.now(UTC)
    semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)
    decided: dict[int, DecisionBundle] = {}

    async def _one(number: int, submission: Submission) -> None:
        if submission.mode != "record":
            results[number] = _rejected(
                number,
                LedgerError(
                    422,
                    "BATCH_RECORD_ONLY",
                    "a batch records decisions; send a gated submission to "
                    "POST /api/v1/decisions",
                ),
                submission.event_id,
            )
            return
        try:
            async with semaphore:
                bundle = await decide(
                    submission=submission,
                    gates=framework.gates,
                    registry=framework.registry,
                    ingestion_timestamp=received_at,
                    identity=framework.identity,
                )
        except UnregisteredReasonerError as err:
            results[number] = _rejected(number, _unregistered(err), submission.event_id)
            return
        except SubmissionContractError as err:
            results[number] = _rejected(
                number, _contract_error(err), submission.event_id
            )
            return
        decided[number] = bundle
        results[number] = BatchLineResult(
            line=number,
            status="recorded",
            decision_id=bundle.decision_id,
            event_id=submission.event_id,
            decision_action=bundle.decision_action,
            bypass_rule_holds=bundle.bypass_rule_holds,
        )

    await asyncio.gather(*(_one(number, s) for number, s in parsed))
    framework.store.write_many([decided[n] for n in sorted(decided)])
    ordered = [results[number] for number, _ in lines]
    recorded = sum(1 for r in ordered if r.status == "recorded")
    log.info(
        "api.batch_recorded",
        component="api",
        lines=len(lines),
        recorded=recorded,
        rejected=len(lines) - recorded,
        principal=principal.label,
    )
    return BatchResult(
        recorded=recorded, rejected=len(lines) - recorded, results=ordered
    )


def _not_found(decision_id: str) -> LedgerError:
    return LedgerError(
        404, "DECISION_NOT_FOUND", f"no decision found for id {decision_id!r}"
    )


def _readable(
    decision_id: str, *, principal: Principal, framework: FrameworkServices
) -> StoredBundle:
    """The stored row, if it exists and the principal may read it."""
    try:
        stored = framework.store.lookup(decision_id)
    except KeyError as err:
        raise _not_found(decision_id) from err
    if not principal.may_read(stored.reasoner_id):
        raise _not_found(decision_id)
    return stored


def _unreadable(
    decision_id: str, schema_version: int, err: ValidationError | None = None
) -> LedgerError:
    return LedgerError(
        500,
        "BUNDLE_UNREADABLE",
        f"decision {decision_id!r} is stored as schema version {schema_version} "
        "but does not validate against it",
        extra={"error_count": err.error_count() if err is not None else 1},
    )


def lookup_decision(
    decision_id: str, *, principal: Principal, framework: FrameworkServices
) -> dict[str, Any]:
    """Return a stored bundle as JSON, any schema version.

    A schema-version-2 bundle is validated and returned in full; an older one
    exactly as stored, with its ``schema_version``. Neither needs the
    reasoner that wrote it to be composed here.

    Raises:
        LedgerError: 404 ``DECISION_NOT_FOUND``; 500 ``BUNDLE_UNREADABLE``.
    """
    stored = _readable(decision_id, principal=principal, framework=framework)
    try:
        return as_bundle(stored).model_dump(mode="json")
    except BundleSchemaUnsupportedError:
        return {**stored.payload, "schema_version": stored.schema_version}
    except ValidationError as err:
        raise _unreadable(decision_id, stored.schema_version, err) from err


def replay_decision(
    decision_id: str, *, principal: Principal, framework: FrameworkServices
) -> ReplayResponse:
    """Replay enforcement against a bundle's cached states.

    Raises:
        LedgerError: 404 ``DECISION_NOT_FOUND``; 409
            ``BUNDLE_SCHEMA_UNSUPPORTED``, ``REASONER_NOT_COMPOSED``, or
            ``REGISTRATION_SNAPSHOT_MISSING``; 500 ``BUNDLE_UNREADABLE``.
    """
    _readable(decision_id, principal=principal, framework=framework)
    try:
        result = framework.store.replay(decision_id)
    except KeyError as err:
        raise _not_found(decision_id) from err
    except BundleSchemaUnsupportedError as err:
        raise _schema_unsupported(err) from err
    except UnregisteredReasonerError as err:
        raise LedgerError(
            409,
            "REASONER_NOT_COMPOSED",
            f"decision {decision_id!r} belongs to reasoner {err.reasoner_id!r}, "
            "which this deployment does not register, and the bundle records no "
            "registration version to replay against",
            extra={"reasoner_id": err.reasoner_id},
        ) from err
    except RegistrationSnapshotMissingError as err:
        raise LedgerError(
            409,
            "REGISTRATION_SNAPSHOT_MISSING",
            str(err),
            extra={"reasoner_id": err.reasoner_id, "registration_version": err.version},
        ) from err
    except ValidationError as err:  # only a version-2 row reaches validation
        raise _unreadable(decision_id, BUNDLE_SCHEMA_VERSION, err) from err
    return ReplayResponse(
        decision_id=result.decision_id,
        original_action=result.original_action,
        replayed_action=result.replayed_action,
        actions_match=result.actions_match,
        original_bypass_rule_holds=result.original_bypass_rule_holds,
        bypass_rule_holds=result.bypass_rule_holds,
        gates=[
            GateReplayResult(
                gate_id=g.gate_id,
                status=g.status,
                original_action=g.original_action,
                replayed_action=g.replayed_action,
            )
            for g in result.gates
        ],
        registration_version=result.registration_version,
        matched=result.matched,
    )


def _schema_unsupported(err: BundleSchemaUnsupportedError) -> LedgerError:
    return LedgerError(
        409,
        "BUNDLE_SCHEMA_UNSUPPORTED",
        str(err),
        extra={
            "schema_version": err.schema_version,
            "supported_schema_version": err.supported,
        },
    )


async def verify_decision(
    decision_id: str,
    *,
    live: bool,
    principal: Principal,
    framework: FrameworkServices,
) -> EvidenceVerification:
    """Say how the evidence a decision relied on stands now (DR-34).

    For every gate that ran (its record holds an output), each evidence
    reference gets two independent answers. ``escrow``: whether the evidence
    escrow holds the artifact the reference names (``none`` when it names
    none). ``status``: what the gate answers when asked (``EvidenceVerifier``)
    to check the reference against its own stored record and, with
    ``live``, against its source as it is now; a gate that is not composed
    here, does not verify, fails, or answers out of contract leaves its
    references ``not_verifiable``. Nothing is decided again, the bundle is
    not changed, and replay is unaffected.

    Raises:
        LedgerError: 404 ``DECISION_NOT_FOUND`` (also another tenant's); 409
            ``BUNDLE_SCHEMA_UNSUPPORTED`` (a version-1 bundle, as replay);
            500 ``BUNDLE_UNREADABLE``.
    """
    t0 = time.perf_counter()
    stored = await asyncio.to_thread(
        _readable, decision_id, principal=principal, framework=framework
    )
    try:
        bundle = as_bundle(stored)
    except BundleSchemaUnsupportedError as err:
        raise _schema_unsupported(err) from err
    except ValidationError as err:
        raise _unreadable(decision_id, stored.schema_version, err) from err
    ran = [
        (record.gate_id, record.output)
        for record in bundle.gates
        if record.output is not None
    ]
    named = [ref.artifact_hash for _, output in ran for ref in output.evidence]
    held = await asyncio.to_thread(
        framework.store.artifacts.held, [h for h in named if h is not None]
    )
    refs: list[VerifiedReference] = []
    for gate_id, output in ran:
        statuses = await verify_evidence(
            framework.gates.get(gate_id),
            output,
            live=live,
            budget_ms=DEFAULT_GATE_BUDGET_MS,
        )
        for ref, status in zip(output.evidence, statuses, strict=True):
            escrow: EscrowStatus = (
                "none"
                if ref.artifact_hash is None
                else "present"
                if ref.artifact_hash in held
                else "missing"
            )
            refs.append(
                VerifiedReference(
                    gate_id=gate_id,
                    source_id=ref.source_id,
                    locator=ref.locator,
                    content_hash=ref.content_hash,
                    artifact_hash=ref.artifact_hash,
                    escrow=escrow,
                    status=status,
                )
            )
    summary = VerificationSummary(
        intact=sum(r.status == "intact" for r in refs),
        changed=sum(r.status == "source_changed" for r in refs),
        unavailable=sum(r.status == "source_unavailable" for r in refs),
        not_verifiable=sum(r.status == "not_verifiable" for r in refs),
        escrow_missing=sum(r.escrow == "missing" for r in refs),
    )
    log.info(
        "api.decision_verified",
        component="api",
        decision_id=decision_id,
        live=live,
        references=len(refs),
        **summary.model_dump(),
        duration_ms=duration_ms(t0),
    )
    return EvidenceVerification(
        decision_id=decision_id,
        checked_at=datetime.now(UTC),
        live=live,
        refs=refs,
        summary=summary,
    )


#: Decision actions a resolution can settle.
_RESOLVABLE = frozenset({DecisionAction.CHALLENGE, DecisionAction.HOLD})


def resolve_decision(
    decision_id: str,
    body: bytes | str,
    *,
    principal: Principal,
    framework: FrameworkServices,
) -> ResolutionReceipt:
    """Append a human resolution to a held or challenged decision.

    The attempt is ``COMPLETED`` with the resolver's terminal action; the
    decision's bundle is never touched (its ``decision_action`` is
    immutable), and the realized action is derived from the attempt chain.

    Args:
        decision_id: The decision to resolve.
        body: A ``ResolutionRequest`` as JSON text.
        principal: The caller (operator or trusted).
        framework: The ledger's services.

    Returns:
        The attempt written and the realized action.

    Raises:
        LedgerError: 403 ``OPERATOR_REQUIRED``; 422 ``RESOLUTION_INVALID``;
            404 ``DECISION_NOT_FOUND``; 409 ``DECISION_NOT_RESOLVABLE`` (an
            ALLOW or BLOCK decision), ``DECISION_ALREADY_RESOLVED``, or
            ``RESOLUTION_CONFLICT`` (a concurrent resolution); 503
            ``RESOLUTIONS_UNAVAILABLE`` (no journal composed).
    """
    require_operator(principal)
    try:
        request = ResolutionRequest.model_validate_json(
            _bounded(body, MAX_SUBMISSION_BYTES)
        )
    except ValidationError as err:
        raise invalid(
            "RESOLUTION_INVALID", "the body is not a valid resolution", err
        ) from err
    journal = framework.journal
    if journal is None:
        raise LedgerError(
            503, "RESOLUTIONS_UNAVAILABLE", "this ledger keeps no resolution journal"
        )
    stored = _readable(decision_id, principal=principal, framework=framework)
    try:
        action = DecisionAction(stored.payload.get("decision_action"))
    except ValueError as err:
        raise _unreadable(decision_id, stored.schema_version) from err
    if action not in _RESOLVABLE:
        raise LedgerError(
            409,
            "DECISION_NOT_RESOLVABLE",
            f"decision {decision_id!r} is {action.value}: only a CHALLENGE or HOLD "
            "is resolved",
            extra={"decision_action": action.value},
        )
    if journal.realized_action(decision_id, action) is not None:
        raise LedgerError(
            409,
            "DECISION_ALREADY_RESOLVED",
            f"decision {decision_id!r} already has a terminal resolution",
        )
    now = datetime.now(UTC)
    attempt = HumanResolutionAttempt(
        decision_id=decision_id,
        attempt_id=str(uuid.uuid4()),
        sequence=len(journal.load_attempts(decision_id)),
        started_at=now,
        completed_at=now,
        resolver_id=request.resolver_id,
        status=ResolutionStatus.COMPLETED,
        resolution_action=request.resolution_action,
        note=request.note,
        reviewer_role=request.reviewer_role,
        reference_ticket_id=request.reference_ticket_id,
    )
    try:
        journal.record_attempt(attempt)
    except psycopg.errors.UniqueViolation as err:
        raise LedgerError(
            409,
            "RESOLUTION_CONFLICT",
            f"another resolution of decision {decision_id!r} was recorded "
            "concurrently; read it and retry if it is still unresolved",
        ) from err
    log.info(
        "api.decision_resolved",
        component="api",
        decision_id=decision_id,
        resolution_action=request.resolution_action.value,
        principal=principal.label,
    )
    return ResolutionReceipt(
        decision_id=decision_id,
        attempt_id=attempt.attempt_id,
        sequence=attempt.sequence,
        resolver_kind=str(attempt.resolver_kind),
        resolver_id=attempt.resolver_id,
        status=str(attempt.status),
        resolution_action=request.resolution_action,
        realized_action=journal.realized_action(decision_id, action),
        completed_at=now,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/decisions",
    status_code=201,
    response_model=DecisionReceipt,
    responses=problem_responses(401, 403, 413, 422),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/Submission"}
                }
            },
        }
    },
)
async def post_decision(
    request: Request,
    response: Response,
    principal: Caller,
) -> DecisionReceipt:
    """Record a decision the reasoner made, or ask the ledger to gate one.

    The body is one ``Submission`` (``mode`` ``record`` or ``gate``),
    parsed strictly as JSON. Returns the decision's receipt; the full bundle
    is at the ``Location`` the response names.
    """
    body = await read_body(request, limit=MAX_SUBMISSION_BYTES)
    bundle = await create_decision(
        body, principal=principal, framework=_framework(request)
    )
    response.headers["Location"] = f"/api/v1/decisions/{bundle.decision_id}"
    return DecisionReceipt.from_bundle(bundle)


@router.post(
    "/decisions:batch",
    response_model=BatchResult,
    responses=problem_responses(401, 403, 413, 422),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/x-ndjson": {
                    "schema": {
                        "type": "string",
                        "description": "One recorded Submission per line (NDJSON).",
                    }
                }
            },
        }
    },
)
async def post_decisions_batch(request: Request, principal: Caller) -> BatchResult:
    """Record many decisions at once (NDJSON, ``mode: record`` only)."""
    body = await read_body(request, limit=MAX_BATCH_BYTES)
    return await record_batch(body, principal=principal, framework=_framework(request))


@router.get(
    "/decisions/{decision_id}",
    responses=problem_responses(401, 404, 500),
)
def get_decision(
    decision_id: str, request: Request, principal: Caller
) -> dict[str, Any]:
    """Retrieve a stored DecisionBundle (any schema version) by id."""
    return lookup_decision(
        decision_id, principal=principal, framework=_framework(request)
    )


@router.post(
    "/decisions/{decision_id}/replay",
    response_model=ReplayResponse,
    responses=problem_responses(401, 404, 409, 500),
)
def post_replay(
    decision_id: str, request: Request, principal: Caller
) -> ReplayResponse:
    """Replay enforcement against a bundle's cached states (no gate re-runs)."""
    return replay_decision(
        decision_id, principal=principal, framework=_framework(request)
    )


@router.post(
    "/decisions/{decision_id}/verify",
    response_model=EvidenceVerification,
    responses=problem_responses(401, 404, 409, 500),
)
async def post_verify(
    decision_id: str, request: Request, principal: Caller, live: bool = False
) -> EvidenceVerification:
    """Verify the evidence a decision relied on (no gate decides again).

    ``live=false`` (the default) asks each gate to check its references
    against its own stored record; ``live=true`` also against each source
    as it is now.
    """
    return await verify_decision(
        decision_id, live=live, principal=principal, framework=_framework(request)
    )


@router.post(
    "/decisions/{decision_id}/resolutions",
    status_code=201,
    response_model=ResolutionReceipt,
    responses=problem_responses(401, 403, 404, 409, 413, 422, 503),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ResolutionRequest"}
                }
            },
        }
    },
)
async def post_resolution(
    decision_id: str, request: Request, principal: Caller
) -> ResolutionReceipt:
    """Resolve a held or challenged decision (operator key)."""
    body = await read_body(request, limit=MAX_SUBMISSION_BYTES)
    return await asyncio.to_thread(
        resolve_decision,
        decision_id,
        body,
        principal=principal,
        framework=_framework(request),
    )
