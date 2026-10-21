"""Behavioral tests for how the policy gate admits a reasoner's binding (DR-33).

The framework asks the gate about every registration that binds it, at
startup and when a registration is written over the API
(``BindingValidator.validate_binding``), before it asks whether the gate
serves the reasoner (``ReasonerScope.serves``). These tests pin:

- a reasoner the gate already serves (a composed plugin's) is admitted as it
  is, and a gate built without profiles admits everyone;
- any other reasoner is admitted from its directory under the asset root
  (``<root>/<reasoner_id>/prompts/*.yaml`` and ``corpus/**/*.md``): its
  templates join the prompt registry and its profile is added, so the gate
  serves it and authorizes its submissions; its directory is read once;
- the gate refuses (``GateConfigError``), adding nothing, a binding that
  carries a configuration, a reasoner with no asset root set, a reasoner id
  that cannot name a directory, a missing ``prompts/`` or ``corpus/``
  directory, no template, a malformed template, a template id the
  deployment already registers, and a corpus with no document that parses;
- through the framework's ``check_gate_set``, a reasoner registered without
  a plugin binds the gate when its assets are under the root, and the
  deployment refuses it, naming the gate, when they are not;
- two reasoners admitted at once both land; a profiles mapping read before
  an admission stays as it was.

No infrastructure, model, or LLM is touched.
"""

from __future__ import annotations

import shutil
import threading
from typing import TYPE_CHECKING, Any

import pytest

from app.gates import BindingValidator, ComposedGate, ReasonerScope, check_gate_set
from core.exceptions import GateConfigError, GatePluginError
from core.observation import GateBinding, ReasonerRegistration
from policy_rag_gate.config import PolicyGateConfig, ReasonerProfile
from policy_rag_gate.gate import PolicyGate
from policy_rag_gate.plugin import PLUGIN
from policy_rag_gate.prompt_registry import YamlPromptRegistry

if TYPE_CHECKING:
    from pathlib import Path

#: The fixture reasoner's profile (a composed plugin's).
_STUB_PROFILE = ReasonerProfile(
    template_ids=frozenset({"stub-v1"}),
    jurisdictions=frozenset({"REGION_A", "REGION_B"}),
)

#: A template for a reasoner registered without a plugin (its own id).
_ROOT_TEMPLATE = """\
template_id: {template_id}
version: 1.0.0
description: A reasoner registered without a plugin.
template: |
  Score {{widget_score}}.
  {{policy_snippets}}
"""


class _UnusedRetriever:
    def retrieve(self, **_: Any) -> Any:
        raise AssertionError("admission must not retrieve")


class _UnusedClient:
    model = "never-called"

    async def complete_structured(self, **_: Any) -> Any:
        raise AssertionError("admission must not call the LLM")


def _gate(
    fixture_reasoner: Any,
    *,
    assets_root: Path | None,
    profiles: Any = None,
    plugin_profiles: bool = True,
) -> PolicyGate:
    if plugin_profiles and profiles is None:
        profiles = {"stub-reasoner": _STUB_PROFILE}
    return PolicyGate(
        client=_UnusedClient(),  # type: ignore[arg-type]
        prompt_registry=YamlPromptRegistry([fixture_reasoner.prompts_dir]),
        retriever=_UnusedRetriever(),  # type: ignore[arg-type]
        profiles=profiles,
        assets_root=assets_root,
    )


def _ship(
    root: Path,
    reasoner_id: str,
    fixture_reasoner: Any,
    *,
    template_id: str | None = None,
    prompts: bool = True,
    corpus: bool = True,
) -> Path:
    """Lay out a reasoner's assets under the root, as an operator would."""
    base = root / reasoner_id
    if prompts:
        (base / "prompts").mkdir(parents=True)
        tid = template_id or f"{reasoner_id}-v1"
        (base / "prompts" / f"{tid}.yaml").write_text(
            _ROOT_TEMPLATE.format(template_id=tid), encoding="utf-8"
        )
    if corpus:
        shutil.copytree(fixture_reasoner.corpus_dir, base / "corpus")
    return base


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def test_the_policy_gate_is_a_binding_validator(fixture_reasoner):
    gate = _gate(fixture_reasoner, assets_root=None)
    assert isinstance(gate, BindingValidator)
    assert isinstance(gate, ReasonerScope)


def test_a_reasoner_the_gate_serves_is_admitted_as_it_is(fixture_reasoner):
    gate = _gate(fixture_reasoner, assets_root=None)

    gate.validate_binding("stub-reasoner", {})

    assert gate.profiles == {"stub-reasoner": _STUB_PROFILE}


def test_a_gate_built_without_profiles_admits_everyone(fixture_reasoner):
    gate = _gate(fixture_reasoner, assets_root=None, plugin_profiles=False)

    gate.validate_binding("anyone", {})

    assert gate.serves("anyone")


def test_a_reasoner_without_a_plugin_is_admitted_from_the_asset_root(
    fixture_reasoner, tmp_path: Path
):
    _ship(tmp_path, "root-reasoner", fixture_reasoner)
    gate = _gate(fixture_reasoner, assets_root=tmp_path)
    assert not gate.serves("root-reasoner")

    gate.validate_binding("root-reasoner", {})

    assert gate.serves("root-reasoner")
    assert gate.profiles is not None
    assert gate.profiles["root-reasoner"] == ReasonerProfile(
        template_ids=frozenset({"root-reasoner-v1"}),
        jurisdictions=frozenset({"REGION_A", "REGION_B"}),
    )
    assert gate.prompt_registry.get("root-reasoner-v1").version == "1.0.0"
    gate.authorize_config(
        PolicyGateConfig(template_id="root-reasoner-v1", jurisdictions=["REGION_A"]),
        reasoner_id="root-reasoner",
    )
    with pytest.raises(ValueError, match="is not a prompt template"):
        gate.authorize_config(
            PolicyGateConfig(template_id="stub-v1"), reasoner_id="root-reasoner"
        )


def test_a_reasoners_directory_is_read_once(fixture_reasoner, tmp_path: Path):
    base = _ship(tmp_path, "root-reasoner", fixture_reasoner)
    gate = _gate(fixture_reasoner, assets_root=tmp_path)
    gate.validate_binding("root-reasoner", {})

    shutil.rmtree(base)  # admitted already: the directory is not read again
    gate.validate_binding("root-reasoner", {})

    assert gate.serves("root-reasoner")


def test_a_plugin_reasoners_directory_under_the_root_is_never_read(
    fixture_reasoner, tmp_path: Path
):
    """A composed plugin's reasoner is served from its plugin."""
    _ship(tmp_path, "stub-reasoner", fixture_reasoner, template_id="stub-v1")
    gate = _gate(fixture_reasoner, assets_root=tmp_path)

    gate.validate_binding("stub-reasoner", {})

    assert gate.prompt_registry.registered_ids() == ["stub-v1"]


def test_a_profiles_mapping_read_before_an_admission_stays_as_it_was(
    fixture_reasoner, tmp_path: Path
):
    _ship(tmp_path, "root-reasoner", fixture_reasoner)
    gate = _gate(fixture_reasoner, assets_root=tmp_path)
    before = gate.profiles

    gate.validate_binding("root-reasoner", {})

    assert before == {"stub-reasoner": _STUB_PROFILE}


# ---------------------------------------------------------------------------
# Refusals: nothing is added
# ---------------------------------------------------------------------------


def _refuses(gate: PolicyGate, reasoner_id: str, fragment: str) -> None:
    ids = gate.prompt_registry.registered_ids()
    with pytest.raises(GateConfigError, match=fragment):
        gate.validate_binding(reasoner_id, {})
    assert not gate.serves(reasoner_id)
    assert gate.prompt_registry.registered_ids() == ids


def test_a_binding_configuration_is_refused(fixture_reasoner, tmp_path: Path):
    gate = _gate(fixture_reasoner, assets_root=tmp_path)

    with pytest.raises(GateConfigError, match=r"takes no binding configuration.*'k'"):
        gate.validate_binding("stub-reasoner", {"k": 1})


def test_a_reasoner_without_a_plugin_needs_the_asset_root(fixture_reasoner):
    _refuses(
        _gate(fixture_reasoner, assets_root=None),
        "file-reasoner",
        "POLICY_RAG_GATE_ASSETS_ROOT is not set",
    )


@pytest.mark.parametrize("reasoner_id", ["../escape", "a/b", ".hidden", ""])
def test_a_reasoner_id_that_cannot_name_a_directory_is_refused(
    fixture_reasoner, tmp_path: Path, reasoner_id: str
):
    _refuses(
        _gate(fixture_reasoner, assets_root=tmp_path / "root"),
        reasoner_id,
        "cannot name a directory",
    )


@pytest.mark.parametrize(
    ("prompts", "corpus", "fragment"),
    [
        (False, True, "has no prompts directory"),
        (True, False, "has no corpus directory"),
    ],
)
def test_a_missing_asset_directory_is_refused(
    fixture_reasoner, tmp_path: Path, prompts: bool, corpus: bool, fragment: str
):
    _ship(tmp_path, "root-reasoner", fixture_reasoner, prompts=prompts, corpus=corpus)
    _refuses(_gate(fixture_reasoner, assets_root=tmp_path), "root-reasoner", fragment)


def test_a_reasoner_that_ships_no_template_is_refused(fixture_reasoner, tmp_path):
    base = _ship(tmp_path, "root-reasoner", fixture_reasoner, prompts=False)
    (base / "prompts").mkdir()
    _refuses(
        _gate(fixture_reasoner, assets_root=tmp_path),
        "root-reasoner",
        "ships no prompt template",
    )


@pytest.mark.parametrize(
    "text",
    [
        "template_id: broken-v1\nversion: 1.0.0\n",  # no template or description
        "- not\n- a mapping\n",
        "template_id: [unclosed\n",
        "template_id: empty-v1\nversion: 1.0.0\ndescription: d\ntemplate: '  '\n",
        "template_id: int-v1\nversion: 1.0.0\ndescription: d\ntemplate: 42\n",
        "template_id: null-v1\nversion: 1.0.0\ndescription: d\nsystem:\ntemplate: t\n",
        "template_id: list-v1\nversion: 1.0.0\ndescription: d\nsystem: [a]\n"
        "template: t\n",
    ],
    ids=[
        "missing-key",
        "not-a-mapping",
        "not-yaml",
        "empty-template",
        "template-not-text",
        "system-null",
        "system-not-text",
    ],
)
def test_a_malformed_template_is_refused(fixture_reasoner, tmp_path: Path, text):
    base = _ship(tmp_path, "root-reasoner", fixture_reasoner)
    (base / "prompts" / "broken.yaml").write_text(text, encoding="utf-8")
    _refuses(
        _gate(fixture_reasoner, assets_root=tmp_path),
        "root-reasoner",
        "do not load",
    )


def test_an_unreadable_directory_is_refused(fixture_reasoner, tmp_path: Path):
    base = _ship(tmp_path, "root-reasoner", fixture_reasoner)
    base.chmod(0)
    try:
        _refuses(
            _gate(fixture_reasoner, assets_root=tmp_path),
            "root-reasoner",
            "cannot be read",
        )
    finally:
        base.chmod(0o755)


def test_a_reasoner_id_too_long_for_a_directory_name_is_refused(
    fixture_reasoner, tmp_path: Path
):
    _refuses(
        _gate(fixture_reasoner, assets_root=tmp_path),
        "r" * 300,
        "cannot be read",
    )


def test_a_template_id_the_deployment_registers_is_refused(
    fixture_reasoner, tmp_path: Path
):
    """Template ids are global and immutable: another reasoner's is taken."""
    _ship(tmp_path, "root-reasoner", fixture_reasoner, template_id="stub-v1")
    _refuses(
        _gate(fixture_reasoner, assets_root=tmp_path),
        "root-reasoner",
        "'stub-v1' is declared twice",
    )


def test_a_corpus_with_no_document_that_parses_is_refused(
    fixture_reasoner, tmp_path: Path
):
    base = _ship(tmp_path, "root-reasoner", fixture_reasoner, corpus=False)
    (base / "corpus").mkdir()
    (base / "corpus" / "no-front-matter.md").write_text("# Policy\n", encoding="utf-8")
    _refuses(
        _gate(fixture_reasoner, assets_root=tmp_path),
        "root-reasoner",
        "has front matter that parses",
    )


# ---------------------------------------------------------------------------
# Through the framework: check_gate_set, at startup and on a write
# ---------------------------------------------------------------------------


def _file_registration(reasoner_id: str) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id=reasoner_id,
        reasoner_name=reasoner_id,
        gates=(GateBinding(gate_id="policy"),),
    )


def test_a_reasoner_registered_without_a_plugin_binds_the_gate_from_the_root(
    fixture_reasoner, tmp_path: Path
):
    _ship(tmp_path, "file-reasoner", fixture_reasoner)
    gate = _gate(fixture_reasoner, assets_root=tmp_path)
    gates = {"policy": ComposedGate(plugin=PLUGIN, gate=gate)}

    check_gate_set(_file_registration("file-reasoner"), gates)

    assert gate.serves("file-reasoner")


def test_a_reasoner_whose_assets_are_missing_is_refused_naming_the_gate(
    fixture_reasoner, tmp_path: Path
):
    gate = _gate(fixture_reasoner, assets_root=tmp_path)
    gates = {"policy": ComposedGate(plugin=PLUGIN, gate=gate)}

    with pytest.raises(GatePluginError) as err:
        check_gate_set(_file_registration("file-reasoner"), gates)

    assert err.value.gate_id == "policy"
    assert "refuses the binding of reasoner 'file-reasoner'" in err.value.reason
    assert "has no prompts directory" in err.value.reason


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_two_reasoners_admitted_at_once_both_land(fixture_reasoner, tmp_path: Path):
    for reasoner_id in ("left-reasoner", "right-reasoner"):
        _ship(tmp_path, reasoner_id, fixture_reasoner)
    gate = _gate(fixture_reasoner, assets_root=tmp_path)
    errors: list[BaseException] = []

    def _admit(reasoner_id: str) -> None:
        try:
            gate.validate_binding(reasoner_id, {})
        except BaseException as err:
            errors.append(err)

    threads = [
        threading.Thread(target=_admit, args=(reasoner_id,))
        for reasoner_id in ("left-reasoner", "right-reasoner", "left-reasoner")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert gate.serves("left-reasoner")
    assert gate.serves("right-reasoner")
    assert gate.prompt_registry.registered_ids() == [
        "left-reasoner-v1",
        "right-reasoner-v1",
        "stub-v1",
    ]
