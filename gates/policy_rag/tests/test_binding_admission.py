"""Behavioral tests for how the policy gate admits a reasoner's binding (DR-33/34).

The framework asks the gate about every registration that binds it, at
startup and when a registration is written over the API
(``BindingValidator.validate_binding``), before it asks whether the gate
serves the reasoner (``ReasonerScope.serves``). These tests pin:

- the binding's configuration is a ``PolicyBindingConfig`` naming at least
  one corpus (DR-34); every corpus's provider must be installed, must
  describe the corpus (the reference provider: it is loaded), and must
  supply the provenance the binding asks for — checked before anything is
  added, and without retrieving;
- a reasoner the gate already serves (a composed plugin's) is admitted as it
  is, and a gate built without profiles admits everyone;
- any other reasoner is admitted from its directory under the asset root
  (``<root>/<reasoner_id>/prompts/*.yaml`` and ``corpus/**/*.md``): its
  templates join the prompt registry and its profile is added, so the gate
  serves it and authorizes its submissions; its directory is read once;
- the gate refuses (``GateConfigError``), adding nothing, a reasoner with no
  asset root set, a reasoner id
  that cannot name a directory, a missing ``prompts/`` or ``corpus/``
  directory, no template, a malformed template, a template id the
  deployment already registers, and a corpus with no document that parses;
- through the framework's ``check_gate_set``, a reasoner registered without
  a plugin binds the gate when its assets are under the root and its corpus
  is loaded, and the deployment refuses it, naming the gate, when they are
  not;
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
from policy_rag_gate.corpus import PolicyBindingConfig
from policy_rag_gate.gate import PolicyGate
from policy_rag_gate.plugin import PLUGIN
from policy_rag_gate.prompt_registry import YamlPromptRegistry
from policy_rag_gate.providers import ProviderRegistry, ProviderServices
from policy_rag_gate.settings import PolicyRagGateSettings

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


#: The binding every admission below uses unless it says otherwise.
_BINDING: dict[str, Any] = {"corpora": [{"corpus_id": "stub-policy", "tier": 1}]}


class _UnusedClient:
    model = "never-called"

    async def complete_structured(self, **_: Any) -> Any:
        raise AssertionError("admission must not call the LLM")


@pytest.fixture
def provider(stub_provider: Any) -> Any:
    """The fake reference provider: it holds ``stub-policy`` only."""
    return stub_provider


@pytest.fixture
def gate_for(provider: Any) -> Any:
    def _make(
        fixture_reasoner: Any,
        *,
        assets_root: Path | None,
        profiles: Any = None,
        plugin_profiles: bool = True,
        providers: ProviderRegistry | None = None,
    ) -> PolicyGate:
        if plugin_profiles and profiles is None:
            profiles = {"stub-reasoner": _STUB_PROFILE}
        return PolicyGate(
            client=_UnusedClient(),  # type: ignore[arg-type]
            prompt_registry=YamlPromptRegistry([fixture_reasoner.prompts_dir]),
            providers=providers or ProviderRegistry.of(provider),
            profiles=profiles,
            assets_root=assets_root,
        )

    return _make


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


def test_the_policy_gate_is_a_binding_validator(gate_for, fixture_reasoner):
    gate = gate_for(fixture_reasoner, assets_root=None)
    assert isinstance(gate, BindingValidator)
    assert isinstance(gate, ReasonerScope)


def test_a_reasoner_the_gate_serves_is_admitted_as_it_is(gate_for, fixture_reasoner):
    gate = gate_for(fixture_reasoner, assets_root=None)

    gate.validate_binding("stub-reasoner", _BINDING)

    assert gate.profiles == {"stub-reasoner": _STUB_PROFILE}


def test_a_gate_built_without_profiles_admits_everyone(gate_for, fixture_reasoner):
    gate = gate_for(fixture_reasoner, assets_root=None, plugin_profiles=False)

    gate.validate_binding("anyone", _BINDING)

    assert gate.serves("anyone")


def test_a_reasoner_without_a_plugin_is_admitted_from_the_asset_root(
    gate_for, fixture_reasoner, tmp_path: Path
):
    _ship(tmp_path, "root-reasoner", fixture_reasoner)
    gate = gate_for(fixture_reasoner, assets_root=tmp_path)
    assert not gate.serves("root-reasoner")

    gate.validate_binding("root-reasoner", _BINDING)

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


def test_a_reasoners_directory_is_read_once(gate_for, fixture_reasoner, tmp_path: Path):
    base = _ship(tmp_path, "root-reasoner", fixture_reasoner)
    gate = gate_for(fixture_reasoner, assets_root=tmp_path)
    gate.validate_binding("root-reasoner", _BINDING)

    shutil.rmtree(base)  # admitted already: the directory is not read again
    gate.validate_binding("root-reasoner", _BINDING)

    assert gate.serves("root-reasoner")


def test_a_plugin_reasoners_directory_under_the_root_is_never_read(
    gate_for, fixture_reasoner, tmp_path: Path
):
    """A composed plugin's reasoner is served from its plugin."""
    _ship(tmp_path, "stub-reasoner", fixture_reasoner, template_id="stub-v1")
    gate = gate_for(fixture_reasoner, assets_root=tmp_path)

    gate.validate_binding("stub-reasoner", _BINDING)

    assert gate.prompt_registry.registered_ids() == ["stub-v1"]


def test_a_profiles_mapping_read_before_an_admission_stays_as_it_was(
    gate_for, fixture_reasoner, tmp_path: Path
):
    _ship(tmp_path, "root-reasoner", fixture_reasoner)
    gate = gate_for(fixture_reasoner, assets_root=tmp_path)
    before = gate.profiles

    gate.validate_binding("root-reasoner", _BINDING)

    assert before == {"stub-reasoner": _STUB_PROFILE}


# ---------------------------------------------------------------------------
# Refusals: nothing is added
# ---------------------------------------------------------------------------


def _refuses(
    gate: PolicyGate,
    reasoner_id: str,
    fragment: str,
    binding: Any = None,
) -> None:
    ids = gate.prompt_registry.registered_ids()
    with pytest.raises(GateConfigError, match=fragment):
        gate.validate_binding(reasoner_id, _BINDING if binding is None else binding)
    assert not gate.serves(reasoner_id)
    assert gate.prompt_registry.registered_ids() == ids


@pytest.mark.parametrize(
    ("binding", "fragment"),
    [
        ({}, r"corpora: Field required"),
        ({"corpora": []}, r"corpora: .*at least 1"),
        ({"corpora": [{"corpus_id": "stub-policy"}]}, r"corpora\.0\.tier"),
        (
            {"corpora": [{"corpus_id": "stub-policy", "tier": 1}], "top_k": 5},
            r"top_k: Extra inputs",
        ),
        (
            [{"corpus_id": "stub-policy", "tier": 1}],
            r"binding configuration is invalid",
        ),
    ],
    ids=["empty", "no-corpus", "no-tier", "unknown-key", "not-a-mapping"],
)
def test_a_binding_that_is_not_a_policy_binding_config_is_refused(
    gate_for, fixture_reasoner, tmp_path: Path, binding, fragment
):
    _ship(tmp_path, "root-reasoner", fixture_reasoner)
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path),
        "root-reasoner",
        fragment,
        binding=binding,
    )


def test_admission_describes_every_bound_corpus_without_retrieving(
    gate_for, fixture_reasoner, provider
):
    gate = gate_for(fixture_reasoner, assets_root=None)

    gate.validate_binding("stub-reasoner", PolicyBindingConfig.model_validate(_BINDING))

    assert provider.described == ["stub-policy"]
    assert provider.calls == []


def test_a_corpus_the_provider_does_not_hold_is_refused_before_anything_is_added(
    gate_for, fixture_reasoner, tmp_path: Path, provider
):
    _ship(tmp_path, "root-reasoner", fixture_reasoner)
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path),
        "root-reasoner",
        r"corpus 'ato-polcy' cannot be described by provider 'reference'",
        binding={"corpora": [{"corpus_id": "ato-polcy", "tier": 1}]},
    )
    assert provider.described == ["ato-polcy"]


def test_a_provider_that_is_not_installed_is_refused(
    gate_for, fixture_reasoner, tmp_path: Path
):
    _ship(tmp_path, "root-reasoner", fixture_reasoner)
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path),
        "root-reasoner",
        r"no retrieval provider 'vertex' is installed.*\['reference'\]",
        binding={"corpora": [{"corpus_id": "x", "provider": "vertex", "tier": 1}]},
    )


def test_a_provider_that_cannot_be_built_is_refused(
    gate_for, fixture_reasoner, tmp_path: Path
):
    class _Broken:
        provider_id = "reference"

        def build(self, services: Any) -> Any:
            raise RuntimeError("legacy policy_rag schema")

    providers = ProviderRegistry(
        {"reference": _Broken()},  # type: ignore[dict-item]
        services=ProviderServices(
            postgres_dsn="dsn", settings=PolicyRagGateSettings(elasticsearch_url="x")
        ),
    )
    _ship(tmp_path, "root-reasoner", fixture_reasoner)
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path, providers=providers),
        "root-reasoner",
        "provider 'reference' cannot be built: legacy policy_rag schema",
    )


def test_a_provider_below_the_bindings_min_provenance_is_refused(
    gate_for, fixture_reasoner, fake_provider, fake_passage
):
    text_only = fake_provider(
        {"stub-policy": [fake_passage("A", "a")]}, provenance_level="text_only"
    )
    gate = gate_for(
        fixture_reasoner, assets_root=None, providers=ProviderRegistry.of(text_only)
    )

    with pytest.raises(
        GateConfigError, match=r"'text_only' provenance.*'uri_and_hash'"
    ):
        gate.validate_binding(
            "stub-reasoner",
            {
                "corpora": [
                    {
                        "corpus_id": "stub-policy",
                        "tier": 1,
                        "min_provenance": "uri_and_hash",
                    }
                ]
            },
        )


def test_two_corpora_at_two_tiers_are_admitted(
    gate_for, fixture_reasoner, fake_provider, fake_passage
):
    provider = fake_provider(
        {
            "regulatory": [fake_passage("R", "r")],
            "practice": [fake_passage("P", "p")],
        }
    )
    gate = gate_for(
        fixture_reasoner, assets_root=None, providers=ProviderRegistry.of(provider)
    )

    gate.validate_binding(
        "stub-reasoner",
        {
            "corpora": [
                {
                    "corpus_id": "regulatory",
                    "tier": 1,
                    "min_provenance": "pinned_version",
                },
                {"corpus_id": "practice", "tier": 2},
            ]
        },
    )

    assert provider.described == ["regulatory", "practice"]


def test_a_reasoner_without_a_plugin_needs_the_asset_root(gate_for, fixture_reasoner):
    _refuses(
        gate_for(fixture_reasoner, assets_root=None),
        "file-reasoner",
        "POLICY_RAG_GATE_ASSETS_ROOT is not set",
    )


@pytest.mark.parametrize("reasoner_id", ["../escape", "a/b", ".hidden", ""])
def test_a_reasoner_id_that_cannot_name_a_directory_is_refused(
    gate_for, fixture_reasoner, tmp_path: Path, reasoner_id: str
):
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path / "root"),
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
    gate_for,
    fixture_reasoner,
    tmp_path: Path,
    prompts: bool,
    corpus: bool,
    fragment: str,
):
    _ship(tmp_path, "root-reasoner", fixture_reasoner, prompts=prompts, corpus=corpus)
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path), "root-reasoner", fragment
    )


def test_a_reasoner_that_ships_no_template_is_refused(
    gate_for, fixture_reasoner, tmp_path
):
    base = _ship(tmp_path, "root-reasoner", fixture_reasoner, prompts=False)
    (base / "prompts").mkdir()
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path),
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
def test_a_malformed_template_is_refused(
    gate_for, fixture_reasoner, tmp_path: Path, text
):
    base = _ship(tmp_path, "root-reasoner", fixture_reasoner)
    (base / "prompts" / "broken.yaml").write_text(text, encoding="utf-8")
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path),
        "root-reasoner",
        "do not load",
    )


def test_an_unreadable_directory_is_refused(gate_for, fixture_reasoner, tmp_path: Path):
    base = _ship(tmp_path, "root-reasoner", fixture_reasoner)
    base.chmod(0)
    try:
        _refuses(
            gate_for(fixture_reasoner, assets_root=tmp_path),
            "root-reasoner",
            "cannot be read",
        )
    finally:
        base.chmod(0o755)


def test_a_reasoner_id_too_long_for_a_directory_name_is_refused(
    gate_for, fixture_reasoner, tmp_path: Path
):
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path),
        "r" * 300,
        "cannot be read",
    )


def test_a_template_id_the_deployment_registers_is_refused(
    gate_for, fixture_reasoner, tmp_path: Path
):
    """Template ids are global and immutable: another reasoner's is taken."""
    _ship(tmp_path, "root-reasoner", fixture_reasoner, template_id="stub-v1")
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path),
        "root-reasoner",
        "'stub-v1' is declared twice",
    )


def test_a_corpus_with_no_document_that_parses_is_refused(
    gate_for, fixture_reasoner, tmp_path: Path
):
    base = _ship(tmp_path, "root-reasoner", fixture_reasoner, corpus=False)
    (base / "corpus").mkdir()
    (base / "corpus" / "no-front-matter.md").write_text("# Policy\n", encoding="utf-8")
    _refuses(
        gate_for(fixture_reasoner, assets_root=tmp_path),
        "root-reasoner",
        "has front matter that parses",
    )


# ---------------------------------------------------------------------------
# Through the framework: check_gate_set, at startup and on a write
# ---------------------------------------------------------------------------


def _file_registration(
    reasoner_id: str, config: dict[str, Any] | None = None
) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id=reasoner_id,
        reasoner_name=reasoner_id,
        gates=(
            GateBinding(
                gate_id="policy", config=_BINDING if config is None else config
            ),
        ),
    )


def test_a_reasoner_registered_without_a_plugin_binds_the_gate_from_the_root(
    gate_for, fixture_reasoner, tmp_path: Path
):
    _ship(tmp_path, "file-reasoner", fixture_reasoner)
    gate = gate_for(fixture_reasoner, assets_root=tmp_path)
    gates = {"policy": ComposedGate(plugin=PLUGIN, gate=gate)}

    check_gate_set(_file_registration("file-reasoner"), gates)

    assert gate.serves("file-reasoner")


def test_a_reasoner_whose_assets_are_missing_is_refused_naming_the_gate(
    gate_for, fixture_reasoner, tmp_path: Path
):
    gate = gate_for(fixture_reasoner, assets_root=tmp_path)
    gates = {"policy": ComposedGate(plugin=PLUGIN, gate=gate)}

    with pytest.raises(GatePluginError) as err:
        check_gate_set(_file_registration("file-reasoner"), gates)

    assert err.value.gate_id == "policy"
    assert "refuses the binding of reasoner 'file-reasoner'" in err.value.reason
    assert "has no prompts directory" in err.value.reason


@pytest.mark.parametrize(
    ("config", "fragment"),
    [
        ({}, "the binding's config is invalid: corpora: Field required"),
        (
            {"corpora": [{"corpus_id": "not-loaded", "tier": 1}]},
            "corpus 'not-loaded' cannot be described",
        ),
    ],
)
def test_a_registration_whose_binding_names_no_loaded_corpus_is_refused(
    gate_for, fixture_reasoner, tmp_path: Path, config, fragment
):
    _ship(tmp_path, "file-reasoner", fixture_reasoner)
    gate = gate_for(fixture_reasoner, assets_root=tmp_path)
    gates = {"policy": ComposedGate(plugin=PLUGIN, gate=gate)}

    with pytest.raises(GatePluginError) as err:
        check_gate_set(_file_registration("file-reasoner", config), gates)

    assert err.value.gate_id == "policy"
    assert fragment in err.value.reason
    assert not gate.serves("file-reasoner")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_two_reasoners_admitted_at_once_both_land(
    gate_for, fixture_reasoner, tmp_path: Path
):
    for reasoner_id in ("left-reasoner", "right-reasoner"):
        _ship(tmp_path, reasoner_id, fixture_reasoner)
    gate = gate_for(fixture_reasoner, assets_root=tmp_path)
    errors: list[BaseException] = []

    def _admit(reasoner_id: str) -> None:
        try:
            gate.validate_binding(reasoner_id, _BINDING)
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
