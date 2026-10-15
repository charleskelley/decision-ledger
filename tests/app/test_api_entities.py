"""Behavioral tests for ``GET /api/v1/entities/{entity_id}/decisions``.

A subject's ledger timeline: every decision about one ``entity_id``, newest
first, rows of every schema version included. A reasoner key is narrowed to
its own decisions (and cannot ask for another reasoner's); the operator sees
every reasoner's and may filter. A malformed id is 422.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from tests._memory_ledger import OPERATOR_KEY, OTHER_KEY

if TYPE_CHECKING:
    from tests.app.conftest import Ledger


def _path(entity_id: object) -> str:
    return f"/api/v1/entities/{entity_id}/decisions"


def test_a_subjects_decisions_are_listed_newest_first(ledger: Ledger, submission):
    first = ledger.submit(submission).json()["decision_id"]
    second = ledger.submit(
        submission.model_copy(update={"event_id": "evt-second"})
    ).json()["decision_id"]
    ledger.store.rows[first].created_at -= timedelta(seconds=5)

    response = ledger.get(_path(submission.entity_id))

    assert response.status_code == 200
    body = response.json()
    assert body["entity_id"] == str(submission.entity_id)
    assert [d["decision_id"] for d in body["decisions"]] == [second, first]
    newest = body["decisions"][0]
    assert newest["mode"] == "record"
    assert newest["event_id"] == "evt-second"
    assert newest["entity_type"] == "widget"
    assert newest["schema_version"] == 2
    assert newest["decision_action"] == "ALLOW"


def test_limit_and_before_page_the_timeline(ledger: Ledger, submission):
    ids = []
    now = datetime.now(UTC)
    for n in range(3):
        decision_id = ledger.submit(
            submission.model_copy(update={"event_id": f"evt-{n}"})
        ).json()["decision_id"]
        ledger.store.rows[decision_id].created_at = now - timedelta(minutes=3 - n)
        ids.append(decision_id)

    page = ledger.get(_path(submission.entity_id), params={"limit": 2}).json()
    assert [d["decision_id"] for d in page["decisions"]] == [ids[2], ids[1]]
    before = page["decisions"][-1]["created_at"]
    rest = ledger.get(
        _path(submission.entity_id), params={"limit": 2, "before": before}
    ).json()
    assert [d["decision_id"] for d in rest["decisions"]] == [ids[0]]


def test_older_rows_are_listed_with_what_they_carry(ledger: Ledger, submission):
    ledger.store.insert_raw(
        {"decision_action": "HOLD"},
        reasoner_id="stub-reasoner",
        schema_version=1,
        entity_id=str(submission.entity_id),
    )
    [row] = ledger.get(_path(submission.entity_id)).json()["decisions"]
    assert row["schema_version"] == 1
    assert row["mode"] is None
    assert row["event_id"] is None


def test_a_reasoner_key_sees_only_its_own_decisions(ledger: Ledger, submission):
    ledger.submit(submission)
    ledger.store.insert_raw(
        {"decision_action": "ALLOW"},
        reasoner_id="other-reasoner",
        schema_version=1,
        entity_id=str(submission.entity_id),
    )

    own = ledger.get(_path(submission.entity_id)).json()["decisions"]
    assert {d["reasoner_id"] for d in own} == {"stub-reasoner"}
    other = ledger.get(_path(submission.entity_id), key=OTHER_KEY).json()
    assert {d["reasoner_id"] for d in other["decisions"]} == {"other-reasoner"}
    everyone = ledger.get(_path(submission.entity_id), key=OPERATOR_KEY).json()
    assert {d["reasoner_id"] for d in everyone["decisions"]} == {
        "stub-reasoner",
        "other-reasoner",
    }
    narrowed = ledger.get(
        _path(submission.entity_id),
        key=OPERATOR_KEY,
        params={"reasoner_id": "other-reasoner"},
    ).json()
    assert {d["reasoner_id"] for d in narrowed["decisions"]} == {"other-reasoner"}


def test_a_reasoner_key_cannot_ask_for_another_reasoners_decisions(
    ledger: Ledger, submission
):
    response = ledger.get(
        _path(submission.entity_id), params={"reasoner_id": "other-reasoner"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "REASONER_MISMATCH"


def test_a_malformed_entity_id_or_limit_is_422(ledger: Ledger, submission):
    response = ledger.get(_path("not-a-uuid"))
    assert response.status_code == 422
    assert response.json()["code"] == "ENTITY_ID_INVALID"
    response = ledger.get(_path(submission.entity_id), params={"limit": 0})
    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_INVALID"


def test_an_unknown_subject_has_an_empty_timeline(ledger: Ledger):
    response = ledger.get(_path("0f3a9d1e-7a2b-4c3d-9e8f-1a2b3c4d5e6f"))
    assert response.status_code == 200
    assert response.json()["decisions"] == []


def test_paging_neither_skips_nor_repeats_decisions_of_one_instant(
    ledger: Ledger, submission
):
    instant = datetime.now(UTC)
    ids = set()
    for n in range(3):
        decision_id = ledger.submit(
            submission.model_copy(update={"event_id": f"evt-tie-{n}"})
        ).json()["decision_id"]
        ledger.store.rows[decision_id].created_at = instant
        ids.add(decision_id)

    seen: list[str] = []
    params: dict[str, object] = {"limit": 1}
    for _ in range(4):
        page = ledger.get(_path(submission.entity_id), params=params).json()
        if not page["decisions"]:
            break
        last = page["decisions"][-1]
        seen.append(last["decision_id"])
        params = {
            "limit": 1,
            "before": last["created_at"],
            "before_id": last["decision_id"],
        }

    assert sorted(seen) == sorted(ids)
    assert len(seen) == 3


def test_a_cursor_id_needs_its_instant_and_must_be_a_uuid(ledger: Ledger, submission):
    path = _path(submission.entity_id)
    response = ledger.get(path, params={"before_id": str(submission.entity_id)})
    assert (response.status_code, response.json()["code"]) == (422, "REQUEST_INVALID")
    response = ledger.get(
        path, params={"before": datetime.now(UTC).isoformat(), "before_id": "nope"}
    )
    assert (response.status_code, response.json()["code"]) == (422, "REQUEST_INVALID")
