"""Behavioral tests for the policy gate's configuration contract.

- ``PolicyGateConfig`` is the gate's ``config_model``: typed keys, lax so a
  reasoner's plain dict (string enum values included) validates, and closed
  to unknown keys.
- ``ReasonerProfile.from_assets`` derives what a reasoner may submit from
  what it ships: the templates in its ``prompts_dir`` and the jurisdictions
  in its ``corpus_dir`` front matter (unparseable documents never reach the
  index, so they grant nothing).
- ``PolicyGate.authorize_config`` accepts only templates the submitting
  reasoner ships and jurisdictions its corpus covers — replacing the
  registration's former allow-lists (DR-28) — and the framework enforces it
  at intake through its ``ConfigAuthorizer`` seam, for gated and recorded
  submissions alike, before the gate is ever called.

No infrastructure, model, or LLM is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from app.gates import ComposedGate, ConfigAuthorizer
from app.reasoner_registry import LiveReasonerRegistry
from app.submit import submit
from core.actions import DecisionAction
from core.exceptions import SubmissionContractError
from core.observation import GateContext
from core.submission import BypassCitation
from policy_rag_gate.config import PolicyGateConfig, ReasonerProfile
from policy_rag_gate.gate import PolicyGate
from policy_rag_gate.plugin import PLUGIN
from policy_rag_gate.prompt_registry import YamlPromptRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from core.bundle import DecisionBundle

_PROFILE = ReasonerProfile(
    template_ids=frozenset({"stub-v1"}),
    jurisdictions=frozenset({"REGION_A", "REGION_B"}),
)

# ---------------------------------------------------------------------------
# PolicyGateConfig
# ---------------------------------------------------------------------------


class _Tier(StrEnum):
    HIGH = "HIGH_VALUE"


def test_config_types_every_key_and_defaults_the_optional_ones():
    config = PolicyGateConfig.model_validate({"template_id": "stub-v1"})

    assert config.template_id == "stub-v1"
    assert config.template_vars == {}
    assert config.retrieval_query is None
    assert config.jurisdictions == []
    assert config.risk_tier is None
    assert config.corpus_version == "unknown"


def test_config_accepts_a_reasoners_plain_dict_with_string_enum_values():
    config = PolicyGateConfig.model_validate(
        {
            "template_id": "stub-v1",
            "template_vars": {"risk_tier": _Tier.HIGH},
            "retrieval_query": "calibration review",
            "jurisdictions": ["REGION_A"],
            "risk_tier": _Tier.HIGH,
            "corpus_version": "c-1",
        }
    )

    assert config.risk_tier == "HIGH_VALUE"
    assert config.template_vars["risk_tier"] == "HIGH_VALUE"


def test_config_requires_a_template_id():
    with pytest.raises(ValidationError, match="template_id"):
        PolicyGateConfig.model_validate({"retrieval_query": "q"})


def test_config_rejects_unknown_keys():
    # A misspelled key fails at intake rather than being silently ignored.
    with pytest.raises(ValidationError, match="jurisdiction"):
        PolicyGateConfig.model_validate(
            {"template_id": "stub-v1", "jurisdiction": ["REGION_A"]}
        )


def test_config_rejects_a_non_string_template_var():
    with pytest.raises(ValidationError, match="template_vars"):
        PolicyGateConfig.model_validate(
            {"template_id": "stub-v1", "template_vars": {"score": 0.5}}
        )


# ---------------------------------------------------------------------------
# ReasonerProfile
# ---------------------------------------------------------------------------


def test_profile_derives_from_the_shipped_prompts_and_corpus(fixture_reasoner):
    profile = ReasonerProfile.from_assets(
        prompts_dir=fixture_reasoner.prompts_dir,
        corpus_dir=fixture_reasoner.corpus_dir,
    )

    assert profile == _PROFILE


def test_profile_reads_the_corpus_recursively_and_skips_unparseable_documents(
    tmp_path: Path, fixture_reasoner
):
    nested = tmp_path / "corpus" / "regional"
    nested.mkdir(parents=True)
    (nested / "doc.md").write_text(
        (fixture_reasoner.corpus_dir / "stub-policy-b.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "corpus" / "broken.md").write_text("no front matter\n", "utf-8")

    profile = ReasonerProfile.from_assets(
        prompts_dir=tmp_path / "no-prompts", corpus_dir=tmp_path / "corpus"
    )

    assert profile == ReasonerProfile(
        template_ids=frozenset(), jurisdictions=frozenset({"REGION_B"})
    )


def test_profile_skips_a_document_whose_front_matter_is_not_valid_yaml(
    tmp_path: Path, fixture_reasoner
):
    # An unquoted colon in a title is a YAML syntax error. The loader skips
    # such a document, so it grants nothing — and building the gate (which
    # derives profiles at startup) must not fail on it.
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "good.md").write_text(
        (fixture_reasoner.corpus_dir / "stub-policy-a.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (corpus / "colon.md").write_text(
        "---\ntitle: NIST SP 800-63B: Digital Identity Guidelines\n---\nBody\n",
        encoding="utf-8",
    )
    (corpus / "unclosed.md").write_text("---\ntitle: [unclosed\n---\nBody\n", "utf-8")

    profile = ReasonerProfile.from_assets(
        prompts_dir=fixture_reasoner.prompts_dir, corpus_dir=corpus
    )

    assert profile.jurisdictions == {"REGION_A"}


def test_profile_accepts_its_own_template_and_jurisdictions():
    _PROFILE.authorize(
        PolicyGateConfig(template_id="stub-v1", jurisdictions=["REGION_A", "REGION_B"])
    )
    _PROFILE.authorize(PolicyGateConfig(template_id="stub-v1"))


def test_profile_refuses_a_template_the_reasoner_does_not_ship():
    with pytest.raises(ValueError, match="'other-v9' is not a prompt template"):
        _PROFILE.authorize(PolicyGateConfig(template_id="other-v9"))


def test_profile_refuses_a_jurisdiction_outside_the_reasoners_corpus():
    with pytest.raises(ValueError, match=r"\['REGION_C'\] are not covered"):
        _PROFILE.authorize(
            PolicyGateConfig(
                template_id="stub-v1", jurisdictions=["REGION_A", "REGION_C"]
            )
        )


# ---------------------------------------------------------------------------
# PolicyGate.authorize_config — the framework's ConfigAuthorizer seam
# ---------------------------------------------------------------------------


class _UnusedRetriever:
    def retrieve(self, **_: Any) -> Any:
        raise AssertionError("authorization must not retrieve")


class _UnusedClient:
    model = "never-called"

    async def complete_structured(self, **_: Any) -> Any:
        raise AssertionError("authorization must not call the LLM")


def _gate(fixture_reasoner: Any, *, profiles: Any) -> PolicyGate:
    return PolicyGate(
        client=_UnusedClient(),  # type: ignore[arg-type]
        prompt_registry=YamlPromptRegistry([fixture_reasoner.prompts_dir]),
        retriever=_UnusedRetriever(),  # type: ignore[arg-type]
        profiles=profiles,
    )


def test_the_policy_gate_is_a_config_authorizer(fixture_reasoner):
    assert isinstance(_gate(fixture_reasoner, profiles=None), ConfigAuthorizer)


def test_authorize_config_checks_the_submitting_reasoners_profile(fixture_reasoner):
    gate = _gate(fixture_reasoner, profiles={"stub-reasoner": _PROFILE})

    gate.authorize_config(
        PolicyGateConfig(template_id="stub-v1"), reasoner_id="stub-reasoner"
    )
    with pytest.raises(ValueError, match="is not a prompt template"):
        gate.authorize_config(
            PolicyGateConfig(template_id="other-v9"), reasoner_id="stub-reasoner"
        )


def test_authorize_config_validates_a_raw_mapping(fixture_reasoner):
    gate = _gate(fixture_reasoner, profiles={"stub-reasoner": _PROFILE})

    with pytest.raises(ValueError, match="REGION_C"):
        gate.authorize_config(
            {"template_id": "stub-v1", "jurisdictions": ["REGION_C"]},
            reasoner_id="stub-reasoner",
        )


def test_authorize_config_refuses_a_reasoner_the_gate_was_not_built_for(
    fixture_reasoner,
):
    gate = _gate(fixture_reasoner, profiles={"stub-reasoner": _PROFILE})

    with pytest.raises(ValueError, match="'stranger' is not one this gate"):
        gate.authorize_config(
            PolicyGateConfig(template_id="stub-v1"), reasoner_id="stranger"
        )


def test_a_gate_built_without_profiles_authorizes_everything(fixture_reasoner):
    gate = _gate(fixture_reasoner, profiles=None)

    gate.authorize_config(
        PolicyGateConfig(template_id="anything", jurisdictions=["ANYWHERE"]),
        reasoner_id="anyone",
    )


# ---------------------------------------------------------------------------
# Through the framework: intake enforces the gate's authorization
# ---------------------------------------------------------------------------


class _RecordingStore:
    def __init__(self) -> None:
        self.written: list[DecisionBundle] = []

    def write(self, bundle: DecisionBundle) -> None:
        self.written.append(bundle)


def _composed(fixture_reasoner: Any) -> dict[str, ComposedGate]:
    gate = _gate(fixture_reasoner, profiles={"stub-reasoner": _PROFILE})
    return {"policy": ComposedGate(plugin=PLUGIN, gate=gate)}


def _recorded(submission: Any, **config: Any) -> Any:
    """A record (low score, citing ``low-score``) whose gate context is ``config``."""
    context = submission.reasoner_context.model_copy(update={"label_value": 0.1})
    return submission.model_copy(
        update={
            "mode": "record",
            "proposed_action": DecisionAction.ALLOW,
            "bypass": BypassCitation(rule_id="low-score"),
            "reasoner_context": context,
            "gate_contexts": (GateContext(gate_id="policy", gate_config=config),),
        }
    )


async def _decide(submission: Any, fixture_reasoner: Any, store: _RecordingStore):
    return await submit(
        submission=submission,
        gates=_composed(fixture_reasoner),
        store=store,  # type: ignore[arg-type]
        registry=LiveReasonerRegistry([fixture_reasoner.registration]),
        ingestion_timestamp=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
    )


async def test_intake_records_a_decision_whose_context_the_gate_accepts(
    submission, fixture_reasoner
):
    store = _RecordingStore()

    bundle = await _decide(
        _recorded(submission, template_id="stub-v1", jurisdictions=["REGION_A"]),
        fixture_reasoner,
        store,
    )

    assert bundle.decision_action == DecisionAction.ALLOW
    assert store.written == [bundle]


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({"template_id": "other-v9"}, "is not a prompt template"),
        ({"template_id": "stub-v1", "jurisdictions": ["REGION_C"]}, "REGION_C"),
    ],
    ids=["foreign-template", "foreign-jurisdiction"],
)
async def test_intake_rejects_a_recorded_context_the_gate_refuses(
    submission, fixture_reasoner, config, reason
):
    store = _RecordingStore()

    with pytest.raises(SubmissionContractError) as exc_info:
        await _decide(_recorded(submission, **config), fixture_reasoner, store)

    assert exc_info.value.code == "GATE_CONFIG_UNAUTHORIZED"
    message = str(exc_info.value)
    assert "gate_config for gate 'policy' is not authorized" in message
    assert reason in message
    assert store.written == []


async def test_intake_rejects_a_routed_context_the_gate_refuses_before_evaluating(
    submission, fixture_reasoner
):
    # _UnusedRetriever / _UnusedClient fail the test if the gate runs.
    store = _RecordingStore()
    routed = submission.model_copy(
        update={
            "gate_contexts": (
                GateContext(
                    gate_id="policy",
                    gate_config={"template_id": "other-v9", "retrieval_query": "q"},
                ),
            )
        }
    )

    with pytest.raises(SubmissionContractError, match="not authorized"):
        await _decide(routed, fixture_reasoner, store)

    assert store.written == []


async def test_intake_rejects_a_malformed_context_with_the_config_models_name(
    submission, fixture_reasoner
):
    store = _RecordingStore()

    with pytest.raises(SubmissionContractError, match="PolicyGateConfig"):
        await _decide(
            _recorded(submission, template_id="stub-v1", jurisdiction=["X"]),
            fixture_reasoner,
            store,
        )

    assert store.written == []


def test_the_gate_serves_only_reasoners_it_holds_a_profile_for(fixture_reasoner):
    # The framework asks at startup (ReasonerScope) so that a reasoner the
    # gate would refuse on every submission fails the deployment instead.
    from app.gates import ReasonerScope

    scoped = _gate(fixture_reasoner, profiles={"stub-reasoner": _PROFILE})
    assert isinstance(scoped, ReasonerScope)
    assert scoped.serves("stub-reasoner")
    assert not scoped.serves("file-reasoner")
    assert _gate(fixture_reasoner, profiles=None).serves("anyone")
