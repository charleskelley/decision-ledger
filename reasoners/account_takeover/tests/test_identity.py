"""ATO and the identity contract (DR-32): the account is ATO's native subject.

ATO names the subject it scores as ``(ato-account, account_id)`` and
declares that namespace in its registration with the UUID5 namespace its
account ids have always derived under. The compatibility guarantee: with no
identity resolver active, the ledger's passthrough assigns every ATO decision
exactly the ``entity_id`` and ``entity_type`` ATO decisions always had (the
namespace below is pinned as a literal, so a change to it fails here), and a
whole decision through the ledger records that subject with its provenance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.models import DecisionReceipt
from app.client import LocalClient
from app.gates import ComposedGate, GateKind
from app.identity import NO_RESOLVER, PASSTHROUGH, passthrough_identity
from app.plugin import FrameworkServices
from app.reasoner_registry import LiveReasonerRegistry
from ato_reasoner.assembler import build_submission
from ato_reasoner.pipeline import run_ato_decision
from ato_reasoner.plugin import AtoServices
from ato_reasoner.registry import ATO_ACCOUNT_NAMESPACE, ATO_REGISTRATION
from ato_reasoner.scorer import ScorerOutput
from core.exceptions import SubmissionContractError
from core.identity import NativeSubject
from core.submission import validate_submission

#: The UUID5 namespace ATO's account ids derived under before DR-32, when it
#: lived in ``ato_reasoner/events.py``. Pinned: ATO's subjects keep their ids.
LEGACY_ACCOUNT_NAMESPACE = uuid.UUID("8a1f9b2e-4c3d-5e6f-7a8b-9c0d1e2f3a4b")


def _scorer(risk_score: float = 0.05) -> ScorerOutput:
    return ScorerOutput(
        entity_id=uuid.uuid4(),
        risk_score=risk_score,
        top_signals=[],
        scorer_version="xgb-test",
        inference_latency_ms=1.0,
    )


def _submission(login_event, ato_feature_vector, risk_score: float = 0.05):
    return build_submission(login_event, ato_feature_vector, _scorer(risk_score))


def test_atos_registration_declares_the_account_namespace():
    assert ATO_REGISTRATION.native_namespaces == (ATO_ACCOUNT_NAMESPACE,)
    assert ATO_ACCOUNT_NAMESPACE.native_namespace == "ato-account"
    assert ATO_ACCOUNT_NAMESPACE.namespace_uuid == LEGACY_ACCOUNT_NAMESPACE
    assert ATO_ACCOUNT_NAMESPACE.entity_type == "account"


def test_a_login_names_its_account_as_the_native_subject(
    login_event, ato_feature_vector
):
    submission = _submission(login_event, ato_feature_vector)
    assert submission.native_subject == NativeSubject(
        native_namespace="ato-account", native_subject_id=login_event.account_id
    )
    assert submission.entity_id == uuid.uuid5(
        LEGACY_ACCOUNT_NAMESPACE, login_event.account_id
    )
    assert submission.entity_type == "account"


@pytest.mark.parametrize("risk_score", [0.05, 0.5, 0.99])
def test_the_passthrough_reproduces_atos_account_subject_exactly(
    login_event, ato_feature_vector, risk_score
):
    """Recorded or gated, the subject is today's account-scoped id and type."""
    submission = _submission(login_event, ato_feature_vector, risk_score)
    at = datetime(2026, 10, 15, tzinfo=UTC)

    identity = passthrough_identity(
        submission, ATO_REGISTRATION, reason=NO_RESOLVER, resolved_at=at
    )

    assert identity.entity_id == uuid.uuid5(
        LEGACY_ACCOUNT_NAMESPACE, login_event.account_id
    )
    assert identity.entity_type == "account"
    assert identity.derivation is not None
    assert identity.derivation.namespace_uuid == LEGACY_ACCOUNT_NAMESPACE
    assert identity.derivation.enterprise_key == login_event.account_id
    provenance = identity.provenance
    assert (provenance.native_namespace, provenance.native_subject_id) == (
        "ato-account",
        login_event.account_id,
    )
    assert provenance.resolved_by == PASSTHROUGH
    assert provenance.method == "unresolved"
    assert identity.related == ()


def test_a_login_whose_entity_id_contradicts_its_account_is_refused(
    login_event, ato_feature_vector
):
    submission = _submission(login_event, ato_feature_vector)
    forged = submission.model_copy(update={"entity_id": uuid.uuid4()})
    with pytest.raises(SubmissionContractError) as caught:
        validate_submission(forged, ATO_REGISTRATION)
    assert caught.value.code == "NATIVE_SUBJECT_MISMATCH"


class _PolicyGatePlugin:
    gate_id = "policy"
    kind = GateKind.LLM
    deterministic = False
    config_model = None

    def build(self, framework: FrameworkServices) -> MagicMock:
        raise AssertionError("composed directly")


async def test_a_decision_through_the_ledger_records_the_account_with_provenance(
    login_event, ato_feature_vector
):
    """ATO → LocalClient → the ledger: the bundle's subject is the account."""
    feature_svc = MagicMock()
    feature_svc.compute.return_value = ato_feature_vector
    scorer = MagicMock()
    scorer.score.return_value = _scorer(0.05)
    gate = MagicMock()
    gate.evaluate = AsyncMock()
    store = MagicMock()
    framework = FrameworkServices(
        settings=MagicMock(),
        pg_conn=MagicMock(),
        store=store,
        registry=LiveReasonerRegistry([ATO_REGISTRATION]),
        gates={"policy": ComposedGate(plugin=_PolicyGatePlugin(), gate=gate)},
    )
    services = AtoServices(
        feature_svc=feature_svc,
        scorer=scorer,
        template_id="ato-v4",
        redis=MagicMock(),
    )

    decision = await run_ato_decision(
        event=login_event, services=services, client=LocalClient(framework)
    )

    [call] = store.write.call_args_list
    bundle = call.args[0]
    expected = uuid.uuid5(LEGACY_ACCOUNT_NAMESPACE, login_event.account_id)
    assert bundle.identity is not None
    assert bundle.entity_id == expected
    assert bundle.entity_type == "account"
    assert bundle.identity.provenance.native_subject_id == login_event.account_id
    assert bundle.identity.provenance.resolved_by == PASSTHROUGH
    assert "identity_ms" in bundle.latency_breakdown
    assert isinstance(decision.receipt, DecisionReceipt)
    assert decision.receipt.entity_id == str(expected)
