"""Policy gate plugin — published under ``decisionledger.gates`` as ``policy``.

``PLUGIN`` satisfies ``app.gates.GatePlugin``. The member's own
``pyproject.toml`` publishes it (``policy = "policy_rag_gate.plugin:PLUGIN"``),
so the framework discovers this gate exactly as it discovers any other and
never imports it (DR-28).

``build`` constructs everything the gate owns, so the framework bootstrap
builds none of it:

- a ``ReasonerProfile`` per composed reasoner plugin — the templates in its
  ``prompts_dir`` and the jurisdictions of its ``corpus_dir`` — which the
  gate authorizes every submission against;
- a ``YamlPromptRegistry`` over every composed reasoner's ``prompts_dir``
  (two reasoners shipping the same ``template_id`` fail the build);
- the asset root (``POLICY_RAG_GATE_ASSETS_ROOT``, DR-33) the gate admits a
  reasoner registered without a plugin from: when the framework asks it to
  validate that reasoner's binding (at startup, or when the registration is
  written over the API), the gate reads ``<root>/<reasoner_id>/prompts`` and
  ``corpus`` and adds the reasoner's templates and profile;
- the retrieval providers (DR-34): every plugin published under the gate's
  own ``policy_rag_gate.providers`` entry-point group, each built the first
  time a binding names one (the reference provider opens its own Postgres
  connection on the framework's database, applies the gate's DDL, and loads
  the embedding and cross-encoder models and the Elasticsearch client);
- the OpenAI-backed ``LLMClient``, with the settings' ``OPENAI_API_KEY``.

The plugin's ``binding_config_model`` is ``PolicyBindingConfig``: a
reasoner's binding to this gate names the corpora the gate retrieves from
(DR-34), and the framework validates it with this model before the gate
admits it.

Building opens no provider, so a deployment whose registrations bind no
corpus opens neither Elasticsearch nor the models.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.gates import GateKind
from policy_rag_gate.config import PolicyGateConfig, ReasonerProfile
from policy_rag_gate.corpus import PolicyBindingConfig
from policy_rag_gate.gate import POLICY_GATE_ID, PolicyGate
from policy_rag_gate.llm.openai import OpenAILLMClient
from policy_rag_gate.prompt_registry import YamlPromptRegistry
from policy_rag_gate.providers import (
    ProviderRegistry,
    ProviderServices,
    discover_provider_plugins,
)
from policy_rag_gate.settings import PolicyRagGateSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

    from app.plugin import FrameworkServices, ReasonerPlugin

log = structlog.get_logger(__name__)


def reasoner_profiles(
    reasoners: Sequence[ReasonerPlugin],
) -> dict[str, ReasonerProfile]:
    """Derive what each reasoner may submit from the assets it ships.

    Args:
        reasoners: The composed reasoner plugins.

    Returns:
        One ``ReasonerProfile`` per reasoner, keyed by ``reasoner_id``.

    Raises:
        ValueError: A reasoner's prompt template file is malformed.
    """
    return {
        plugin.registration.reasoner_id: ReasonerProfile.from_assets(
            prompts_dir=plugin.prompts_dir, corpus_dir=plugin.corpus_dir
        )
        for plugin in reasoners
    }


class PolicyGatePlugin:
    """``GatePlugin`` for the LLM-backed policy gate.

    Args:
        settings: Optional explicit ``PolicyRagGateSettings``. When omitted,
            settings are read from the environment the first time ``build``
            runs, so importing the plugin never touches ``.env``.

    Attributes:
        gate_id: ``"policy"``.
        kind: ``GateKind.LLM``.
        deterministic: ``False`` — an LLM verdict is not a pure function of
            its input, so enforcement applies Tiers 2 and 4.
        config_model: ``PolicyGateConfig``, validated by the framework at
            intake.
        binding_config_model: ``PolicyBindingConfig`` — the corpora a
            reasoner's binding names (DR-34), validated by the framework
            when a registration is composed or written; the built gate's
            ``validate_binding`` then admits the binding.
    """

    gate_id: str = POLICY_GATE_ID
    kind: GateKind = GateKind.LLM
    deterministic: bool = False
    config_model: type[BaseModel] | None = PolicyGateConfig
    binding_config_model: type[BaseModel] | None = PolicyBindingConfig

    def __init__(self, settings: PolicyRagGateSettings | None = None) -> None:
        """Bind the plugin; defer reading settings to build time."""
        self._settings = settings

    @property
    def settings(self) -> PolicyRagGateSettings:
        """The settings ``build`` will use (read lazily)."""
        if self._settings is None:
            self._settings = PolicyRagGateSettings()
        return self._settings

    def build(self, framework: FrameworkServices) -> PolicyGate:
        """Build the policy gate and the infrastructure it owns.

        Args:
            framework: Shared services. The gate reads ``settings`` (the
                Postgres DSN of the database that holds its schema) and
                ``reasoners`` (each reasoner's ``prompts_dir`` and
                ``corpus_dir``). A reasoner registered without a plugin is
                admitted later, from the asset root, when the framework
                asks the gate to validate its binding.

        Returns:
            The runtime ``PolicyGate``; ``close()`` releases the providers
            it built (the reference provider's Postgres connection and
            Elasticsearch client).

        Raises:
            ValueError: Two composed reasoners ship a prompt template with
                the same ``template_id``, a template file is malformed, or
                two provider plugins share an id.
        """
        settings = self.settings
        prompt_registry = YamlPromptRegistry(
            [p.prompts_dir for p in framework.reasoners]
        )
        profiles = reasoner_profiles(framework.reasoners)
        providers = ProviderRegistry(
            discover_provider_plugins(),
            services=ProviderServices(
                postgres_dsn=framework.settings.postgres_dsn, settings=settings
            ),
        )
        client = OpenAILLMClient(api_key=settings.openai_api_key or None)

        log.info(
            "policy_gate.built",
            component="policy_gate",
            prompt_templates=prompt_registry.registered_ids(),
            reasoner_ids=sorted(profiles),
            providers=providers.provider_ids(),
            assets_root=str(settings.assets_root_path or ""),
        )
        return PolicyGate(
            client=client,
            prompt_registry=prompt_registry,
            providers=providers,
            profiles=profiles,
            assets_root=settings.assets_root_path,
        )


#: The entry-point target: ``policy = "policy_rag_gate.plugin:PLUGIN"``.
PLUGIN = PolicyGatePlugin()
