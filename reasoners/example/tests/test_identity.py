"""The example and the identity contract (DR-32): the widget is its native subject.

The example names the subject it scores as ``(example-widget, widget_id)``
and declares that namespace in its registration with the UUID5 namespace its
widget ids have always derived under. With no identity resolver active, the
ledger's passthrough assigns every example decision exactly the
``entity_id`` and ``entity_type`` the example's decisions always had (the
namespace below is pinned as a literal, so a change to it fails here), and a
whole decision through the ledger records that subject with its provenance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.client import LocalClient
from app.identity import NO_RESOLVER, PASSTHROUGH, passthrough_identity
from core.exceptions import SubmissionContractError
from core.identity import NativeSubject
from core.submission import validate_submission
from example_reasoner.__main__ import sample_event
from example_reasoner.assembler import build_submission
from example_reasoner.pipeline import decide_widget
from example_reasoner.registry import EXAMPLE_REGISTRATION, EXAMPLE_WIDGET_NAMESPACE
from example_reasoner.scorer import score_widget

#: The UUID5 namespace the example's widget ids derived under before the
#: example declared it, when it lived in ``example_reasoner/events.py``.
#: Pinned: the example's subjects keep their ids.
LEGACY_WIDGET_NAMESPACE = uuid.UUID("8b4f2a6e-1c3d-5e7f-9a0b-2c4d6e8f0a1b")


def test_the_registration_declares_the_widget_namespace():
    assert EXAMPLE_REGISTRATION.native_namespaces == (EXAMPLE_WIDGET_NAMESPACE,)
    assert EXAMPLE_WIDGET_NAMESPACE.native_namespace == "example-widget"
    assert EXAMPLE_WIDGET_NAMESPACE.namespace_uuid == LEGACY_WIDGET_NAMESPACE
    assert EXAMPLE_WIDGET_NAMESPACE.entity_type == "widget"


@pytest.mark.parametrize("band", ["low", "mid", "high"])
def test_an_inspection_names_its_widget_as_the_native_subject(band):
    event = sample_event(band, widget_id="widget-identity-1")
    submission = build_submission(event, score_widget(event))

    assert submission.native_subject == NativeSubject(
        native_namespace="example-widget", native_subject_id="widget-identity-1"
    )
    assert submission.entity_id == uuid.uuid5(
        LEGACY_WIDGET_NAMESPACE, "widget-identity-1"
    )
    assert submission.entity_type == "widget"
    validate_submission(submission, EXAMPLE_REGISTRATION)


@pytest.mark.parametrize("band", ["low", "mid", "high"])
def test_the_passthrough_reproduces_the_widget_subject_exactly(band):
    """Recorded or gated, the subject is the widget's id and type as before."""
    event = sample_event(band, widget_id="widget-identity-2")
    submission = build_submission(event, score_widget(event))
    at = datetime(2026, 10, 18, tzinfo=UTC)

    identity = passthrough_identity(
        submission, EXAMPLE_REGISTRATION, reason=NO_RESOLVER, resolved_at=at
    )

    assert identity.entity_id == uuid.uuid5(
        LEGACY_WIDGET_NAMESPACE, "widget-identity-2"
    )
    assert identity.entity_type == "widget"
    assert identity.derivation is not None
    assert identity.derivation.namespace_uuid == LEGACY_WIDGET_NAMESPACE
    assert identity.derivation.enterprise_key == "widget-identity-2"
    provenance = identity.provenance
    assert (provenance.native_namespace, provenance.native_subject_id) == (
        "example-widget",
        "widget-identity-2",
    )
    assert provenance.resolved_by == PASSTHROUGH
    assert provenance.method == "unresolved"
    assert identity.related == ()


def test_an_inspection_whose_entity_id_contradicts_its_widget_is_refused():
    event = sample_event("low")
    submission = build_submission(event, score_widget(event))
    forged = submission.model_copy(update={"entity_id": uuid.uuid4()})

    with pytest.raises(SubmissionContractError) as caught:
        validate_submission(forged, EXAMPLE_REGISTRATION)
    assert caught.value.code == "NATIVE_SUBJECT_MISMATCH"


async def test_a_gated_decision_records_the_widget_with_provenance(ledger, policy_gate):
    """Example → LocalClient → the ledger: the bundle's subject is the widget."""
    client = LocalClient(ledger)
    event = sample_event("mid", widget_id="widget-identity-3")

    receipt = await decide_widget(event, client=client)

    bundle = await client.get_bundle(receipt.decision_id)
    expected = uuid.uuid5(LEGACY_WIDGET_NAMESPACE, "widget-identity-3")
    assert receipt.mode == "gate"
    assert len(policy_gate.requests) == 1
    assert bundle.identity is not None
    assert bundle.entity_id == expected
    assert bundle.entity_type == "widget"
    assert bundle.submission.native_subject == event.native_subject
    provenance = bundle.identity.provenance
    assert provenance.native_namespace == "example-widget"
    assert provenance.native_subject_id == "widget-identity-3"
    assert provenance.resolved_by == PASSTHROUGH
    assert provenance.method == "unresolved"
    assert provenance.reason == NO_RESOLVER
    assert receipt.entity_id == str(expected)
