"""The ledger's seven MCP tools — what an agent can do with a DecisionLedger.

Each tool is a thin call on a ``LedgerClient`` (an ``HttpClient`` to a ledger
service, normally), so an agent goes through exactly the checks a reasoner
service does: its key, its registration, the gates, enforcement. The
descriptions are written for the agent that reads them.

- ``record_decision`` — record a decision the agent already made, citing one
  of its registered bypass rules.
- ``gate_decision`` — ask the ledger's gates to decide (or check) one.
- ``lookup_decision`` — read a decision's full bundle.
- ``replay_decision`` — re-run enforcement on a decision's stored evidence.
- ``verify_decision`` — say how the evidence a decision relied on stands now
  (the ledger's escrow, and each gate's check of its own references).
- ``list_decisions_for_entity`` — a subject's decisions, newest first.
- ``resolve_hold`` — a human's terminal resolution of a held decision
  (needs the operator key).

A refusal from the ledger comes back as a tool error whose content is the
ledger's problem document (``status``, stable ``code``, ``detail``); invalid
arguments come back as a tool error with the validation errors; a ledger
that cannot be reached, as a tool error with code ``LEDGER_UNAVAILABLE``.
"""

from __future__ import annotations

import functools
import json
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import ValidationError

from app.api.models import ResolutionRequest
from core.exceptions import LedgerError
from core.submission import Submission
from decisionledger_mcp.server import Tool, ToolError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.client import LedgerClient

_SUBMISSION = Submission.model_json_schema()

_UUID = {"type": "string", "format": "uuid"}


def _submission_input(mode: str) -> dict[str, Any]:
    """The input schema of a submitting tool: ``{"submission": Submission}``."""
    defs = _SUBMISSION.get("$defs", {})
    body = {k: v for k, v in _SUBMISSION.items() if k != "$defs"}
    return {
        "type": "object",
        "$defs": defs,
        "properties": {
            "submission": {
                **body,
                "description": (
                    f"The decision as a DecisionLedger Submission (wire schema "
                    f"version 2). `mode` may be omitted: this tool sets it to "
                    f"{mode!r}."
                ),
            }
        },
        "required": ["submission"],
        "additionalProperties": False,
    }


_RECORD_DESCRIPTION = """\
Record a decision you (the reasoner) already made, in the DecisionLedger \
system of record. Use this when one of your registered bypass rules applies: \
the ledger checks that the rule you cite (`submission.bypass.rule_id`) is \
registered for you, that `proposed_action` is the action it justifies, and \
evaluates its predicate on your `reasoner_context` (whether it held is \
recorded as `bypass_rule_holds`; a rule that does not hold makes the \
decision HOLD). Only the deployment's mandatory gates run. If no bypass rule \
applies, call `gate_decision` instead.

Fill `reasoner_context` with what you actually ran, never placeholders: \
`model_version` = the model id you called; `model_artifact_sha256` = SHA-256 \
of your rendered prompt (or null); `label_*` = your own output the bypass \
rules read; `feature_set` = the scalar inputs you consulted plus \
`tool_trace_sha256` (SHA-256 of your ordered tool calls); \
`attribution.narrative` = your reasoning. Address every gate your \
registration binds in `gate_contexts`. `raw_event` is your event, stored \
verbatim.

Returns the decision's receipt: `decision_id`, `decision_action` (ALLOW, \
CHALLENGE, HOLD, BLOCK — act on this, not on your proposal), each gate's \
outcome, and `registration_version`."""

_GATE_DESCRIPTION = """\
Ask the DecisionLedger's gates to decide an action for a subject. The ledger \
runs your registration's mandatory and advisory gates (policy retrieval with \
an LLM judge, rules, lookups) on your evidence and combines them: the final \
action is the most restrictive of every gate's and of your optional \
`proposed_action` (a floor). A gate that fails holds the decision. Use this \
when none of your bypass rules applies, or when you want the gates' \
judgment. Leave `bypass` unset.

Fill `reasoner_context` truthfully (see `record_decision`) and give every \
gate you bind its `gate_context` (call the ledger's gates resource or read \
your registration for what each accepts). Returns the decision's receipt: \
`decision_id`, `decision_action` — the action to take — and each gate's \
outcome; `lookup_decision` returns the full record with the gates' evidence."""

_LOOKUP_DESCRIPTION = """\
Read one decision's complete, immutable Decision Bundle by `decision_id`: \
the submission as received, every gate's input, output, and evidence, the \
enforcement trail, the bypass rule checks, and the registration version it \
was decided under. Works for decisions of your own reasoner (or any \
reasoner, with the operator key)."""

_REPLAY_DESCRIPTION = """\
Replay a decision: re-run the ledger's deterministic enforcement on the \
decision's stored gate outputs and evidence, under the registration version \
it was made under, without calling any gate or LLM again. `matched: true` \
means the recorded action is reproduced exactly; use it to audit a \
decision."""

_VERIFY_DESCRIPTION = """\
Verify the evidence a decision relied on: for each evidence reference of \
each gate that ran, `escrow` says whether the ledger's evidence escrow holds \
the copy of the content the gate relied on (`present`, `missing`, or `none` \
when the gate kept none), and `status` is the gate's own check (`intact`, \
`source_changed`, `source_unavailable`, or `not_verifiable`) against its \
stored record, or with `live: true` against each source as it is now. \
Unlike replay, verify answers whether what the decision relied on is still \
provable; it decides nothing again and changes nothing."""

_ENTITY_DESCRIPTION = """\
List the decisions about one subject (`entity_id`, a UUID), newest first: \
its timeline in the ledger, including decisions filed under ids the \
ledger's identity journal merged with it. Set `include_related` to a \
relationship type your deployment's identity resolver emits to add the \
decisions about subjects one such relationship away (a group's members, or \
a member's group). Each item has `decision_id`, `reasoner_id`, `entity_id` \
(the subject it is about), `relationship_type` (set for a related subject), \
`created_at`, `decision_action`, `mode`, and `event_id`; call \
`lookup_decision` for a full record. Page with `before` and `before_id` \
(the `created_at` and `decision_id` of the last item you have)."""

_RESOLVE_DESCRIPTION = """\
Record a human's resolution of a HELD (or CHALLENGED) decision: the \
reviewer's terminal action (ALLOW or BLOCK), who they are, and why. The \
decision itself never changes — the resolution is appended to its \
resolution log, and the returned `realized_action` is what now applies to \
the subject. Requires the operator key; refused for a decision that is \
already final or already resolved. Only call this to record a decision a \
human actually made."""


def _problem(err: LedgerError) -> ToolError:
    return ToolError(err.problem())


def _invalid(err: ValidationError, what: str) -> ToolError:
    return ToolError(
        {
            "code": "ARGUMENTS_INVALID",
            "detail": f"the {what} is not valid",
            "errors": [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in err.errors(include_url=False, include_input=False)
            ],
        }
    )


def _unavailable(err: httpx.HTTPError) -> dict[str, Any]:
    """The tool error for a ledger that could not be reached.

    Names the error's type and the request, never the error's text: an
    ``httpx`` message can quote a request header, and the key is one.
    """
    detail = f"the ledger could not be reached ({type(err).__name__})"
    try:
        request = err.request
    except RuntimeError:  # an error raised before a request existed
        request = None
    if request is not None:
        detail += f" on {request.method} {request.url.copy_with(query=None)}"
    return {"code": "LEDGER_UNAVAILABLE", "detail": detail}


def _reaching_the_ledger(
    handler: Callable[[dict[str, Any]], Awaitable[Any]],
) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    """Report a ledger that cannot be reached as a tool error."""

    @functools.wraps(handler)
    async def _wrapped(arguments: dict[str, Any]) -> Any:
        try:
            return await handler(arguments)
        except httpx.HTTPError as err:
            raise ToolError(_unavailable(err)) from err

    return _wrapped


def _required(arguments: dict[str, Any], name: str) -> Any:
    if name not in arguments:
        raise ToolError(
            {"code": "ARGUMENTS_INVALID", "detail": f"argument {name!r} is required"}
        )
    return arguments[name]


def _submitting(
    client: LedgerClient, mode: str
) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    async def _handler(arguments: dict[str, Any]) -> Any:
        raw = _required(arguments, "submission")
        if not isinstance(raw, dict):
            raise ToolError(
                {"code": "ARGUMENTS_INVALID", "detail": "submission must be an object"}
            )
        if raw.get("mode", mode) != mode:
            other = "gate_decision" if mode == "record" else "record_decision"
            raise ToolError(
                {
                    "code": "WRONG_TOOL",
                    "detail": (
                        f"this tool submits mode {mode!r}; a submission with mode "
                        f"{raw['mode']!r} goes to {other}"
                    ),
                }
            )
        try:
            submission = Submission.model_validate_json(
                json.dumps({**raw, "mode": mode})
            )
        except ValidationError as err:
            raise _invalid(err, "submission") from err
        try:
            receipt = await client.submit(submission)
        except LedgerError as err:
            raise _problem(err) from err
        return receipt.model_dump(mode="json")

    return _handler


def ledger_tools(client: LedgerClient) -> list[Tool]:
    """The seven tools, bound to ``client``.

    Args:
        client: The ledger the tools act on.

    Returns:
        ``record_decision``, ``gate_decision``, ``lookup_decision``,
        ``replay_decision``, ``verify_decision``,
        ``list_decisions_for_entity``, ``resolve_hold``.
    """

    async def _lookup(arguments: dict[str, Any]) -> Any:
        try:
            return await client.get_decision(str(_required(arguments, "decision_id")))
        except LedgerError as err:
            raise _problem(err) from err

    async def _replay(arguments: dict[str, Any]) -> Any:
        try:
            result = await client.replay(str(_required(arguments, "decision_id")))
        except LedgerError as err:
            raise _problem(err) from err
        return result.model_dump(mode="json")

    async def _verify(arguments: dict[str, Any]) -> Any:
        live = arguments.get("live", False)
        if not isinstance(live, bool):
            raise ToolError(
                {"code": "ARGUMENTS_INVALID", "detail": "live must be a boolean"}
            )
        try:
            result = await client.verify(
                str(_required(arguments, "decision_id")), live=live
            )
        except LedgerError as err:
            raise _problem(err) from err
        return result.model_dump(mode="json")

    async def _entity(arguments: dict[str, Any]) -> Any:
        from datetime import datetime

        before = arguments.get("before")
        try:
            moment = datetime.fromisoformat(before) if before else None
        except (TypeError, ValueError) as err:
            raise ToolError(
                {"code": "ARGUMENTS_INVALID", "detail": "before must be ISO 8601"}
            ) from err
        try:
            timeline = await client.entity_decisions(
                str(_required(arguments, "entity_id")),
                reasoner_id=arguments.get("reasoner_id"),
                limit=int(arguments.get("limit", 50)),
                before=moment,
                before_id=arguments.get("before_id"),
                include_related=arguments.get("include_related"),
            )
        except LedgerError as err:
            raise _problem(err) from err
        return timeline.model_dump(mode="json")

    async def _resolve(arguments: dict[str, Any]) -> Any:
        decision_id = str(_required(arguments, "decision_id"))
        fields = {k: v for k, v in arguments.items() if k != "decision_id"}
        try:
            resolution = ResolutionRequest.model_validate(fields)
        except ValidationError as err:
            raise _invalid(err, "resolution") from err
        try:
            receipt = await client.resolve(decision_id, resolution)
        except LedgerError as err:
            raise _problem(err) from err
        return receipt.model_dump(mode="json")

    decision_id_input = {
        "type": "object",
        "properties": {"decision_id": {**_UUID, "description": "The decision's id."}},
        "required": ["decision_id"],
        "additionalProperties": False,
    }
    writes = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
    return [
        Tool(
            name="record_decision",
            title="Record a decision",
            description=_RECORD_DESCRIPTION,
            input_schema=_submission_input("record"),
            handler=_reaching_the_ledger(_submitting(client, "record")),
            annotations={**writes, "openWorldHint": False},
        ),
        Tool(
            name="gate_decision",
            title="Gate a decision",
            description=_GATE_DESCRIPTION,
            input_schema=_submission_input("gate"),
            handler=_reaching_the_ledger(_submitting(client, "gate")),
            annotations={**writes, "openWorldHint": True},
        ),
        Tool(
            name="lookup_decision",
            title="Look up a decision",
            description=_LOOKUP_DESCRIPTION,
            input_schema=decision_id_input,
            handler=_reaching_the_ledger(_lookup),
            annotations={"readOnlyHint": True, "openWorldHint": False},
        ),
        Tool(
            name="replay_decision",
            title="Replay a decision",
            description=_REPLAY_DESCRIPTION,
            input_schema=decision_id_input,
            handler=_reaching_the_ledger(_replay),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        ),
        Tool(
            name="verify_decision",
            title="Verify a decision's evidence",
            description=_VERIFY_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "decision_id": {**_UUID, "description": "The decision's id."},
                    "live": {
                        "type": "boolean",
                        "description": (
                            "Also check each source as it is now (default: "
                            "false, the gates' stored records only)."
                        ),
                    },
                },
                "required": ["decision_id"],
                "additionalProperties": False,
            },
            handler=_reaching_the_ledger(_verify),
            annotations={"readOnlyHint": True, "openWorldHint": True},
        ),
        Tool(
            name="list_decisions_for_entity",
            title="List a subject's decisions",
            description=_ENTITY_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {**_UUID, "description": "The subject's id."},
                    "reasoner_id": {
                        "type": "string",
                        "description": "Only this reasoner's decisions.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    "before": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "The created_at of the last decision you have; the "
                            "page continues after it."
                        ),
                    },
                    "before_id": {
                        **_UUID,
                        "description": "The decision_id of that last decision.",
                    },
                    "include_related": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "A relationship type: also list the decisions about "
                            "subjects one such relationship away."
                        ),
                    },
                },
                "required": ["entity_id"],
                "additionalProperties": False,
            },
            handler=_reaching_the_ledger(_entity),
            annotations={"readOnlyHint": True, "openWorldHint": False},
        ),
        Tool(
            name="resolve_hold",
            title="Resolve a held decision",
            description=_RESOLVE_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "decision_id": {**_UUID, "description": "The held decision."},
                    "resolution_action": {
                        "type": "string",
                        "enum": ["ALLOW", "BLOCK"],
                        "description": "The reviewer's terminal action.",
                    },
                    "resolver_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Who resolved it (the reviewer's id).",
                    },
                    "note": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The reviewer's rationale.",
                    },
                    "reviewer_role": {"type": "string"},
                    "reference_ticket_id": {"type": "string"},
                },
                "required": ["decision_id", "resolution_action", "resolver_id", "note"],
                "additionalProperties": False,
            },
            handler=_reaching_the_ledger(_resolve),
            annotations={**writes, "openWorldHint": False},
        ),
    ]
