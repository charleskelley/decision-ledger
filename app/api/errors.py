"""Problem-style errors for the ledger API (RFC 9457, DR-31).

Every error the framework's routes return is an ``application/problem+json``
document with the standard members (``type``, ``title``, ``status``,
``detail``, ``instance``) and a stable machine-readable ``code`` that clients
switch on. The ledger's operations raise ``core.exceptions.LedgerError``;
``install_problem_handlers`` renders it, and also turns FastAPI's own
request-validation errors, Starlette's routing errors, and any unhandled
exception into the same shape, so a client never parses two error formats.

Stable codes (the ``code`` member):

- 400s on input: ``SUBMISSION_INVALID`` (the body is not a valid
  ``Submission``), a ``SubmissionContractError`` code (the submission breaks
  its reasoner's registration: ``BYPASS_RULE_UNREGISTERED``,
  ``GATE_CONFIG_INVALID``, ...), ``REASONER_NOT_REGISTERED``,
  ``REQUEST_INVALID`` (a query or path parameter), ``RESOLUTION_INVALID``,
  ``ENTITY_ID_INVALID``, ``BATCH_EMPTY``, ``BODY_TOO_LARGE``,
  ``BATCH_TOO_LARGE`` (413).
- auth: ``UNAUTHENTICATED`` (401), ``REASONER_MISMATCH``,
  ``REASONER_KEY_REQUIRED``, ``OPERATOR_REQUIRED`` (403).
- state: ``DECISION_NOT_FOUND``, ``REASONER_NOT_FOUND``,
  ``REGISTRATION_VERSION_NOT_FOUND``, ``NOT_FOUND`` (404);
  ``BUNDLE_SCHEMA_UNSUPPORTED``, ``REASONER_NOT_COMPOSED``,
  ``REGISTRATION_SNAPSHOT_MISSING``, ``DECISION_NOT_RESOLVABLE``,
  ``DECISION_ALREADY_RESOLVED``, ``RESOLUTION_CONFLICT`` (409).
- server: ``BUNDLE_UNREADABLE``, ``INTERNAL_ERROR`` (500),
  ``RESOLUTIONS_UNAVAILABLE`` (503).
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import structlog
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.exceptions import LedgerError

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from pydantic import ValidationError

log = structlog.get_logger(__name__)

#: The media type of every error response.
PROBLEM_MEDIA_TYPE = "application/problem+json"


#: Component every problem response's schema references (``app.main``
#: registers it, with the wire models, in the OpenAPI document).
PROBLEM_SCHEMA_REF = "#/components/schemas/ProblemDocument"

_PROBLEM_DESCRIPTIONS = {
    401: "Missing or unknown API key (`UNAUTHENTICATED`)",
    403: "The key may not do this (`REASONER_MISMATCH`, `OPERATOR_REQUIRED`, ...)",
    404: "Not found (`DECISION_NOT_FOUND`, `REASONER_NOT_FOUND`, ...)",
    409: "The request conflicts with the stored state",
    413: "The body is too large (`BODY_TOO_LARGE`, `BATCH_TOO_LARGE`)",
    422: "The request is invalid (a stable `code` says which rule)",
    500: "The ledger failed (`BUNDLE_UNREADABLE`, `INTERNAL_ERROR`)",
    503: "A service the request needs is unavailable",
}


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` documenting problem documents for ``statuses``."""
    return {
        status: {
            "description": _PROBLEM_DESCRIPTIONS.get(status, HTTPStatus(status).phrase),
            "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": PROBLEM_SCHEMA_REF}}},
        }
        for status in statuses
    }


def problem_response(err: LedgerError, *, instance: str | None = None) -> JSONResponse:
    """Render a ``LedgerError`` as its problem document."""
    return JSONResponse(
        status_code=err.status,
        content=err.problem(instance=instance),
        media_type=PROBLEM_MEDIA_TYPE,
    )


def validation_errors(err: ValidationError) -> list[dict[str, Any]]:
    """Pydantic's error list without the offending input.

    The input is not echoed: it can hold what JSON cannot carry (a ``NaN``)
    or what the caller should not see repeated back (a secret it pasted).

    Args:
        err: The validation error.

    Returns:
        One ``{"type", "loc", "msg"}`` mapping per error.
    """
    return [
        {"type": e["type"], "loc": list(e["loc"]), "msg": e["msg"]}
        for e in err.errors(
            include_url=False, include_context=False, include_input=False
        )
    ]


def invalid(code: str, detail: str, err: ValidationError) -> LedgerError:
    """A 422 problem carrying a validation error list."""
    return LedgerError(422, code, detail, extra={"errors": validation_errors(err)})


def _status_code(status: int) -> str:
    try:
        return HTTPStatus(status).name
    except ValueError:
        return f"HTTP_{status}"


async def _ledger_error(request: Request, err: Exception) -> JSONResponse:
    assert isinstance(err, LedgerError)
    return problem_response(err, instance=request.url.path)


async def _request_invalid(request: Request, err: Exception) -> JSONResponse:
    assert isinstance(err, RequestValidationError)
    errors = [
        {"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": e.get("msg")}
        for e in err.errors()
    ]
    problem = LedgerError(
        422,
        "REQUEST_INVALID",
        "a path or query parameter is invalid",
        extra={"errors": errors},
    )
    return problem_response(problem, instance=request.url.path)


async def _http_error(request: Request, err: Exception) -> JSONResponse:
    assert isinstance(err, StarletteHTTPException)
    detail = err.detail if isinstance(err.detail, str) else str(err.detail)
    problem = LedgerError(err.status_code, _status_code(err.status_code), detail)
    response = problem_response(problem, instance=request.url.path)
    for name, value in (err.headers or {}).items():
        response.headers[name] = value
    return response


async def _unhandled(request: Request, err: Exception) -> JSONResponse:
    log.error(
        "api.unhandled_error",
        component="api",
        path=request.url.path,
        error_type=type(err).__name__,
        error=str(err),
    )
    problem = LedgerError(
        500, "INTERNAL_ERROR", "the ledger failed to complete the request"
    )
    return problem_response(problem, instance=request.url.path)


def install_problem_handlers(app: FastAPI) -> None:
    """Make every error ``app`` returns a problem document.

    Args:
        app: The ledger's FastAPI application.
    """
    app.add_exception_handler(LedgerError, _ledger_error)
    app.add_exception_handler(RequestValidationError, _request_invalid)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled)
