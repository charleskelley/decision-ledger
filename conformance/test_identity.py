"""The identity contract (DR-32): ``GET /api/v1/identity/resolve`` and the
identity layer every decision records.

Deployment-agnostic: the native namespaces here are fresh random names, which
no identity resolver matches and no registration declares, so on any
instance a subject named in one is recorded unresolved under the reasoner's
own ``entity_id`` — and resolving it finds nothing. A decision that names no
native subject is recorded the same way.
"""

from __future__ import annotations

import uuid

from conformance._ledger import Ledger, example, problem


def _unknown_namespace() -> str:
    return f"conformance-{uuid.uuid4()}"


def test_resolving_a_subject_nobody_knows_is_404(ledger: Ledger, key: str):
    response = ledger.get(
        "/api/v1/identity/resolve",
        key=key,
        params={"namespace": _unknown_namespace(), "native_id": "k-1"},
    )
    problem(response, 404, "IDENTITY_UNRESOLVED")


def test_resolving_needs_a_namespace_and_a_native_id(ledger: Ledger, key: str):
    response = ledger.get(
        "/api/v1/identity/resolve", key=key, params={"namespace": "n"}
    )
    problem(response, 422, "REQUEST_INVALID")


def test_a_decision_records_the_native_subject_it_names(ledger: Ledger, key: str):
    submission = example("record.json")
    namespace = _unknown_namespace()
    submission["native_subject"] = {
        "native_namespace": namespace,
        "native_subject_id": "widget-conformance-1",
    }
    response = ledger.submit(submission)
    assert response.status_code == 201, response.text
    receipt = response.json()

    bundle = ledger.get(response.headers["location"], key=key).json()
    identity = bundle["identity"]
    provenance = identity["provenance"]
    assert provenance["native_namespace"] == namespace
    assert provenance["native_subject_id"] == "widget-conformance-1"
    assert provenance["method"] == "unresolved"
    assert provenance["reason"]
    assert identity["entity_id"] == submission["entity_id"]
    assert identity["related"] == []
    assert receipt["entity_id"] == identity["entity_id"]


def test_a_decision_naming_no_native_subject_is_its_reasoners_own(
    ledger: Ledger, key: str
):
    submission = example("record.json")
    response = ledger.submit(submission)
    assert response.status_code == 201, response.text

    identity = ledger.get(response.headers["location"], key=key).json()["identity"]
    assert identity["entity_id"] == submission["entity_id"]
    assert identity["entity_type"] == submission["entity_type"]
    assert identity["provenance"]["native_namespace"] is None
    assert identity["provenance"]["method"] == "unresolved"


def test_a_timeline_says_which_subject_each_decision_is_about(ledger: Ledger, key: str):
    submission = example("record.json")
    decision_id = ledger.submit(submission).json()["decision_id"]
    path = f"/api/v1/entities/{submission['entity_id']}/decisions"

    plain = ledger.get(path, key=key).json()
    [row] = plain["decisions"]
    assert row["decision_id"] == decision_id
    assert row["entity_id"] == submission["entity_id"]
    assert row["relationship_type"] is None

    # A relationship type nothing emits adds nothing.
    related = ledger.get(
        path, key=key, params={"include_related": _unknown_namespace()}
    ).json()
    assert [d["decision_id"] for d in related["decisions"]] == [decision_id]
