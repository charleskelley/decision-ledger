"""Behavioral tests for the decision routes (DR-31): write, look up, replay, resolve.

The ledger app runs for real over in-memory services (``tests/app/conftest``):
the stub reasoner, a scripted fake gate, ``MemoryBundleStore``. What is
pinned:

- ``POST /api/v1/decisions`` records or gates a submission and answers 201
  with its receipt and a ``Location``; the bundle records the registration
  version it was decided under.
- Input is parsed strictly as JSON; every refusal is a problem document with
  a stable code, and nothing is written: an invalid body, a submission its
  registration does not allow, an unregistered reasoner.
- Auth: no key or an unknown key is 401; another reasoner's key, or the
  operator key, cannot write (403); another tenant's decision reads as not
  found.
- Replay enforces against the registration snapshot the bundle cites, even
  after the registration changed; a missing snapshot, an unregistered
  reasoner, and an older bundle are 409s with their own codes.
- Resolutions: the operator resolves a held decision; a final decision, a
  second resolution, a non-terminal action, and a non-operator are refused.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from core.actions import DecisionAction
from core.observation import GateContext, ReasonerRegistration, registration_version
from core.submission import BypassCitation
from tests._memory_ledger import OPERATOR_KEY, OTHER_KEY, STUB_KEY
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from core.submission import Submission
    from tests.app.conftest import Ledger

_STUB_VERSION = registration_version(STUB_REGISTRATION)
_PROBLEM = "application/problem+json"


def _scripted(submission: Submission, **config: Any) -> Submission:
    return submission.model_copy(
        update={"gate_contexts": (GateContext(gate_id="fake", gate_config=config),)}
    )


def _held(submission: Submission) -> Submission:
    """A record citing ``low-score`` whose evidence (0.5) does not satisfy it."""
    context = submission.reasoner_context.model_copy(update={"label_value": 0.5})
    return submission.model_copy(update={"reasoner_context": context})


def _for_other_reasoner(submission: Submission) -> Submission:
    context = submission.reasoner_context.model_copy(
        update={"reasoner_id": "other-reasoner"}
    )
    return submission.model_copy(
        update={"reasoner_id": "other-reasoner", "reasoner_context": context}
    )


def _assert_problem(response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(_PROBLEM)
    body = response.json()
    assert body["code"] == code
    assert body["status"] == status
    assert (
        body["type"] == f"urn:decisionledger:problem:{code.lower().replace('_', '-')}"
    )
    assert body["title"]
    assert body["detail"]
    return body


# ---------------------------------------------------------------------------
# POST /api/v1/decisions
# ---------------------------------------------------------------------------


def test_a_record_is_written_and_answered_with_its_receipt(ledger: Ledger, submission):
    response = ledger.submit(submission)

    assert response.status_code == 201, response.text
    receipt = response.json()
    assert response.headers["location"] == f"/api/v1/decisions/{receipt['decision_id']}"
    assert receipt["mode"] == "record"
    assert receipt["decision_action"] == "ALLOW"
    assert receipt["bypass_rule_id"] == "low-score"
    assert receipt["bypass_rule_holds"] is True
    assert receipt["reasoner_id"] == "stub-reasoner"
    assert receipt["event_id"] == submission.event_id
    assert receipt["entity_id"] == str(submission.entity_id)
    assert receipt["registration_version"] == _STUB_VERSION
    assert receipt["gates"] == [
        {
            "gate_id": "fake",
            "mandatory": False,
            "status": "skipped",
            "action": None,
            "reason": receipt["gates"][0]["reason"],
        }
    ]
    [bundle] = ledger.store.bundles()
    assert bundle.decision_id == receipt["decision_id"]
    assert bundle.registration_version == _STUB_VERSION
    assert bundle.submission == submission


def test_a_gated_submission_runs_its_gates(ledger: Ledger, gated_submission):
    response = ledger.submit(
        _scripted(gated_submission, permitted_actions=["CHALLENGE"])
    )

    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["mode"] == "gate"
    assert receipt["decision_action"] == "CHALLENGE"
    assert receipt["bypass_rule_id"] is None
    [gate] = receipt["gates"]
    assert (gate["gate_id"], gate["status"], gate["action"]) == (
        "fake",
        "ran",
        "CHALLENGE",
    )
    assert len(ledger.gate_plugin.built[0].requests) == 1


def test_an_invalid_body_is_a_problem_with_the_errors_and_no_echo(ledger: Ledger):
    body = '{"mode": "record", "raw_event": {"secret": "do-not-echo"}}'
    response = ledger.post("/api/v1/decisions", body)

    problem = _assert_problem(response, 422, "SUBMISSION_INVALID")
    assert problem["instance"] == "/api/v1/decisions"
    locs = {tuple(e["loc"]) for e in problem["errors"]}
    assert ("reasoner_id",) in locs
    assert "do-not-echo" not in response.text
    assert ledger.store.rows == {}


def test_a_non_json_body_is_a_submission_problem(ledger: Ledger):
    _assert_problem(
        ledger.post("/api/v1/decisions", b"not json"), 422, "SUBMISSION_INVALID"
    )


def test_a_submission_its_registration_refuses_is_a_422_with_its_code(
    ledger: Ledger, submission
):
    unregistered_rule = submission.model_copy(
        update={"bypass": BypassCitation(rule_id="no-such-rule")}
    )
    problem = _assert_problem(
        ledger.submit(unregistered_rule), 422, "BYPASS_RULE_UNREGISTERED"
    )
    assert problem["reasoner_id"] == "stub-reasoner"
    assert ledger.store.rows == {}


def test_a_gate_config_its_gate_rejects_is_a_422(ledger: Ledger, gated_submission):
    response = ledger.submit(_scripted(gated_submission, unknown_knob=True))
    _assert_problem(response, 422, "GATE_CONFIG_INVALID")
    assert ledger.store.rows == {}


def test_a_reasoner_the_ledger_does_not_register_is_refused(ledger: Ledger, submission):
    response = ledger.submit(_for_other_reasoner(submission), key=OTHER_KEY)
    problem = _assert_problem(response, 422, "REASONER_NOT_REGISTERED")
    assert problem["reasoner_id"] == "other-reasoner"


def test_no_key_and_an_unknown_key_are_401(ledger: Ledger, submission):
    _assert_problem(ledger.submit(submission, key=None), 401, "UNAUTHENTICATED")
    _assert_problem(ledger.submit(submission, key="not-a-key"), 401, "UNAUTHENTICATED")
    assert ledger.store.rows == {}


def test_another_reasoners_key_cannot_write_for_this_one(ledger: Ledger, submission):
    response = ledger.submit(submission, key=OTHER_KEY)

    problem = _assert_problem(response, 403, "REASONER_MISMATCH")
    assert problem["reasoner_id"] == "stub-reasoner"
    assert problem["key_reasoner_id"] == "other-reasoner"
    assert ledger.store.rows == {}


def test_the_operator_key_writes_no_decision(ledger: Ledger, submission):
    response = ledger.submit(submission, key=OPERATOR_KEY)
    _assert_problem(response, 403, "REASONER_KEY_REQUIRED")
    assert ledger.store.rows == {}


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/{id}
# ---------------------------------------------------------------------------


def test_lookup_returns_the_bundle_to_its_reasoner_and_the_operator(
    ledger: Ledger, submission
):
    decision_id = ledger.submit(submission).json()["decision_id"]

    for key in (STUB_KEY, OPERATOR_KEY):
        response = ledger.get(f"/api/v1/decisions/{decision_id}", key=key)
        assert response.status_code == 200
        body = response.json()
        assert body["schema_version"] == 2
        assert body["registration_version"] == _STUB_VERSION
        assert body["submission"]["event_id"] == submission.event_id


def test_another_tenants_decision_reads_as_not_found(ledger: Ledger, submission):
    decision_id = ledger.submit(submission).json()["decision_id"]
    response = ledger.get(f"/api/v1/decisions/{decision_id}", key=OTHER_KEY)
    _assert_problem(response, 404, "DECISION_NOT_FOUND")


def test_lookup_of_an_unknown_or_malformed_id_is_404(ledger: Ledger):
    for decision_id in ("0f3a9d1e-7a2b-4c3d-9e8f-1a2b3c4d5e6f", "not-a-uuid"):
        response = ledger.get(f"/api/v1/decisions/{decision_id}")
        _assert_problem(response, 404, "DECISION_NOT_FOUND")


def test_lookup_returns_an_older_bundle_as_stored(ledger: Ledger):
    legacy = {"decision_action": "HOLD", "raw_event": {"route": "legacy"}}
    decision_id = ledger.store.insert_raw(
        legacy, reasoner_id="stub-reasoner", schema_version=1
    )

    response = ledger.get(f"/api/v1/decisions/{decision_id}")

    assert response.status_code == 200
    assert response.json() == {**legacy, "schema_version": 1}


def test_lookup_of_a_v2_row_that_does_not_validate_is_500(ledger: Ledger):
    decision_id = ledger.store.insert_raw(
        {"gates": 3}, reasoner_id="stub-reasoner", schema_version=2
    )
    response = ledger.get(f"/api/v1/decisions/{decision_id}")
    _assert_problem(response, 500, "BUNDLE_UNREADABLE")


# ---------------------------------------------------------------------------
# POST /api/v1/decisions/{id}/replay
# ---------------------------------------------------------------------------


def _replay(ledger: Ledger, decision_id: str, *, key: str = STUB_KEY):
    return ledger.post(f"/api/v1/decisions/{decision_id}/replay", b"", key=key)


def test_replay_reproduces_a_record_and_a_gated_decision(
    ledger: Ledger, submission, gated_submission
):
    record = ledger.submit(submission).json()["decision_id"]
    gated = ledger.submit(
        _scripted(gated_submission, permitted_actions=["HOLD"])
    ).json()["decision_id"]

    for decision_id, action in ((record, "ALLOW"), (gated, "HOLD")):
        response = _replay(ledger, decision_id)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["matched"] is True
        assert result["replayed_action"] == action
        assert result["registration_version"] == _STUB_VERSION
    assert [r.decision_id for r in ledger.store.replays] == [record, gated]


def _without_tier3(registration: ReasonerRegistration) -> ReasonerRegistration:
    return registration.model_copy(update={"novel_entity_feature": None})


def test_replay_enforces_against_the_registration_the_decision_was_made_under(
    ledger: Ledger, gated_submission
):
    # A gated decision about a novel entity: Tier 3 holds the advisory verdict.
    context = gated_submission.reasoner_context
    novel = gated_submission.model_copy(
        update={
            "reasoner_context": context.model_copy(
                update={"feature_set": {**context.feature_set, "thin_history": True}}
            )
        }
    )
    decision_id = ledger.submit(novel).json()["decision_id"]
    assert ledger.store.bundles()[0].decision_action == DecisionAction.HOLD

    # The reasoner re-registers without its novel-entity signal.
    from app.reasoner_registry import LiveReasonerRegistry

    ledger.store._registry = LiveReasonerRegistry([_without_tier3(STUB_REGISTRATION)])

    result = _replay(ledger, decision_id).json()
    assert result["matched"] is True
    assert result["replayed_action"] == "HOLD"
    assert result["registration_version"] == _STUB_VERSION


def test_replay_without_the_cited_snapshot_is_refused_when_the_registration_changed(
    ledger: Ledger, submission
):
    from app.reasoner_registry import LiveReasonerRegistry

    decision_id = ledger.submit(submission).json()["decision_id"]
    ledger.store.snapshots.rows.clear()  # type: ignore[attr-defined]
    ledger.store._registry = LiveReasonerRegistry([_without_tier3(STUB_REGISTRATION)])

    problem = _assert_problem(
        _replay(ledger, decision_id), 409, "REGISTRATION_SNAPSHOT_MISSING"
    )
    assert problem["registration_version"] == _STUB_VERSION
    assert ledger.store.replays == []


def test_replay_without_a_snapshot_uses_the_same_current_registration(
    ledger: Ledger, submission
):
    decision_id = ledger.submit(submission).json()["decision_id"]
    ledger.store.snapshots.rows.clear()  # type: ignore[attr-defined]
    assert _replay(ledger, decision_id).json()["matched"] is True


def test_replay_of_an_unversioned_bundle_needs_its_reasoner_registered(
    ledger: Ledger, submission
):
    from app.reasoner_registry import LiveReasonerRegistry

    decision_id = ledger.submit(submission).json()["decision_id"]
    row = ledger.store.rows[decision_id]
    row.payload = {**row.payload, "registration_version": None}

    assert _replay(ledger, decision_id).json()["matched"] is True
    ledger.store._registry = LiveReasonerRegistry([])
    problem = _assert_problem(
        _replay(ledger, decision_id, key=OPERATOR_KEY), 409, "REASONER_NOT_COMPOSED"
    )
    assert problem["reasoner_id"] == "stub-reasoner"


def test_replay_of_an_older_bundle_is_409(ledger: Ledger):
    decision_id = ledger.store.insert_raw(
        {"decision_action": "HOLD"}, reasoner_id="stub-reasoner", schema_version=1
    )
    problem = _assert_problem(
        _replay(ledger, decision_id), 409, "BUNDLE_SCHEMA_UNSUPPORTED"
    )
    assert problem["schema_version"] == 1
    assert problem["supported_schema_version"] == 2


def test_replay_of_another_tenants_decision_is_not_found_and_not_logged(
    ledger: Ledger, submission
):
    decision_id = ledger.submit(submission).json()["decision_id"]
    _assert_problem(
        _replay(ledger, decision_id, key=OTHER_KEY), 404, "DECISION_NOT_FOUND"
    )
    assert ledger.store.replays == []


# ---------------------------------------------------------------------------
# POST /api/v1/decisions/{id}/resolutions
# ---------------------------------------------------------------------------


def _resolution(action: str = "ALLOW", **extra: Any) -> str:
    return json.dumps(
        {
            "resolver_id": "analyst-7",
            "resolution_action": action,
            "note": "verified with the widget steward",
            **extra,
        }
    )


def _resolve(ledger: Ledger, decision_id: str, body: str, *, key: str = OPERATOR_KEY):
    return ledger.post(f"/api/v1/decisions/{decision_id}/resolutions", body, key=key)


def test_the_operator_resolves_a_held_decision(ledger: Ledger, submission):
    receipt = ledger.submit(_held(submission)).json()
    assert receipt["decision_action"] == "HOLD"
    assert receipt["decision_level_rule"] == "bypass_rule_not_satisfied"

    response = _resolve(
        ledger, receipt["decision_id"], _resolution(reference_ticket_id="T-1")
    )

    assert response.status_code == 201, response.text
    resolved = response.json()
    assert resolved["decision_id"] == receipt["decision_id"]
    assert resolved["sequence"] == 0
    assert resolved["resolver_kind"] == "HUMAN"
    assert resolved["resolver_id"] == "analyst-7"
    assert resolved["status"] == "COMPLETED"
    assert resolved["resolution_action"] == "ALLOW"
    assert resolved["realized_action"] == "ALLOW"
    [attempt] = ledger.journal.attempts
    assert attempt.reference_ticket_id == "T-1"  # type: ignore[attr-defined]
    # The bundle is immutable: its decision_action stays HOLD.
    lookup = ledger.get(f"/api/v1/decisions/{receipt['decision_id']}").json()
    assert lookup["decision_action"] == "HOLD"


def test_resolving_needs_the_operator_key(ledger: Ledger, submission):
    decision_id = ledger.submit(_held(submission)).json()["decision_id"]
    response = _resolve(ledger, decision_id, _resolution(), key=STUB_KEY)
    _assert_problem(response, 403, "OPERATOR_REQUIRED")
    assert ledger.journal.attempts == []


def test_a_final_decision_cannot_be_resolved(ledger: Ledger, submission):
    decision_id = ledger.submit(submission).json()["decision_id"]  # ALLOW
    problem = _assert_problem(
        _resolve(ledger, decision_id, _resolution("BLOCK")),
        409,
        "DECISION_NOT_RESOLVABLE",
    )
    assert problem["decision_action"] == "ALLOW"


def test_a_resolved_decision_cannot_be_resolved_again(ledger: Ledger, submission):
    decision_id = ledger.submit(_held(submission)).json()["decision_id"]
    assert _resolve(ledger, decision_id, _resolution()).status_code == 201
    _assert_problem(
        _resolve(ledger, decision_id, _resolution("BLOCK")),
        409,
        "DECISION_ALREADY_RESOLVED",
    )


def test_a_resolution_is_terminal_and_well_formed(ledger: Ledger, submission):
    decision_id = ledger.submit(_held(submission)).json()["decision_id"]
    for body in (_resolution("HOLD"), _resolution(unexpected="x"), "{}"):
        _assert_problem(_resolve(ledger, decision_id, body), 422, "RESOLUTION_INVALID")
    assert ledger.journal.attempts == []


def test_resolving_an_unknown_decision_is_404(ledger: Ledger):
    response = _resolve(ledger, "0f3a9d1e-7a2b-4c3d-9e8f-1a2b3c4d5e6f", _resolution())
    _assert_problem(response, 404, "DECISION_NOT_FOUND")


def test_a_concurrent_resolution_is_a_409_conflict(
    ledger: Ledger, submission, monkeypatch
):
    decision_id = ledger.submit(_held(submission)).json()["decision_id"]
    assert _resolve(ledger, decision_id, _resolution()).status_code == 201
    # A second resolver read the chain before the first one wrote.
    monkeypatch.setattr(ledger.journal, "load_attempts", lambda decision_id: [])
    _assert_problem(
        _resolve(ledger, decision_id, _resolution("BLOCK")), 409, "RESOLUTION_CONFLICT"
    )
    assert len(ledger.journal.attempts) == 1


# ---------------------------------------------------------------------------
# Bounds, identifiers, and storability
# ---------------------------------------------------------------------------


def test_an_oversized_submission_is_413_and_written_nowhere(
    ledger: Ledger, submission, monkeypatch
):
    import app.api.decisions as decisions_module

    monkeypatch.setattr(decisions_module, "MAX_SUBMISSION_BYTES", 256)
    problem = _assert_problem(ledger.submit(submission), 413, "BODY_TOO_LARGE")
    assert problem["limit_bytes"] == 256
    assert ledger.store.rows == {}


def test_a_decision_id_in_any_uuid_spelling_is_found(ledger: Ledger, submission):
    decision_id = ledger.submit(submission).json()["decision_id"]
    for spelling in (
        f"urn:uuid:{decision_id}",
        decision_id.upper(),
        decision_id.replace("-", ""),
    ):
        response = ledger.get(f"/api/v1/decisions/{spelling}")
        assert response.status_code == 200, spelling
        assert response.json()["decision_id"] == decision_id


def test_a_resolution_the_journal_cannot_store_is_422(ledger: Ledger, submission):
    decision_id = ledger.submit(_held(submission)).json()["decision_id"]
    body = _resolution(note="looks fine\u0000")
    _assert_problem(_resolve(ledger, decision_id, body), 422, "RESOLUTION_INVALID")
    assert ledger.journal.attempts == []


def test_a_cited_version_with_no_snapshot_and_no_registration_is_snapshot_missing(
    ledger: Ledger, submission
):
    from app.reasoner_registry import LiveReasonerRegistry

    decision_id = ledger.submit(submission).json()["decision_id"]
    ledger.store.snapshots.rows.clear()  # type: ignore[attr-defined]
    ledger.store._registry = LiveReasonerRegistry([])

    problem = _assert_problem(
        _replay(ledger, decision_id, key=OPERATOR_KEY),
        409,
        "REGISTRATION_SNAPSHOT_MISSING",
    )
    assert problem["registration_version"] == _STUB_VERSION
