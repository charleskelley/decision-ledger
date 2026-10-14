"""The ATO plugin satisfies the framework's ``ReasonerPlugin`` contract.

These checks are what makes the package discoverable: the installed entry
points resolve to the module-level ``PLUGIN`` / ``ADAPTER`` singletons, the
directories the policy gate reads (prompts, corpus) ship inside the package,
and every gate context the assembler builds is one the policy gate accepts
— its ``config_model`` validates it and the profile the gate derives from
those directories authorizes it (DR-28).
"""

from __future__ import annotations

import importlib.metadata
from uuid import uuid4

import pytest

from app.plugin import ReasonerPlugin
from app.plugins import PLUGIN_GROUP, discover_plugins
from ato_reasoner.assembler import build_observation
from ato_reasoner.eval.adapter import ADAPTER
from ato_reasoner.events import Geolocation
from ato_reasoner.plugin import PLUGIN
from ato_reasoner.registry import ATO_REGISTRATION
from ato_reasoner.scorer import ScorerOutput
from core.routes import GateRoute
from eval.adapter import EVAL_ADAPTER_GROUP
from policy_rag_gate.config import PolicyGateConfig, ReasonerProfile


def _entry_point(group: str) -> importlib.metadata.EntryPoint:
    matches = [
        ep
        for ep in importlib.metadata.entry_points(group=group)
        if ep.name == ATO_REGISTRATION.reasoner_id
    ]
    assert len(matches) == 1, matches
    return matches[0]


def test_ato_plugin_satisfies_the_reasoner_plugin_protocol():
    assert isinstance(PLUGIN, ReasonerPlugin)


def test_ato_plugin_carries_the_ato_registration():
    assert PLUGIN.registration is ATO_REGISTRATION


def test_ato_plugin_ships_its_prompts_and_corpus_inside_the_package():
    assert PLUGIN.prompts_dir.is_dir()
    assert PLUGIN.corpus_dir.is_dir()
    assert any(PLUGIN.prompts_dir.glob("*.yaml"))
    assert any(PLUGIN.corpus_dir.rglob("*.md"))


@pytest.mark.parametrize("country", ["US", "DE"])
@pytest.mark.parametrize(
    ("risk_score", "route"),
    [
        (0.05, GateRoute.FAST_PATH_ALLOW),
        (0.61, GateRoute.ROUTE_TO_GATE),
        (0.97, GateRoute.FAST_PATH_BLOCK),
    ],
)
def test_every_gate_context_the_assembler_builds_is_one_the_policy_gate_accepts(
    login_event, ato_feature_vector, country, risk_score, route
):
    # The gate validates and authorizes ATO's gate_config at intake on every
    # route, so every band and every geography must pass both checks.
    event = login_event.model_copy(
        update={
            "geolocation": Geolocation(
                latitude=52.52,
                longitude=13.40,
                country=country,
                city="Somewhere",
                asn="AS3320",
            )
        }
    )
    scorer = ScorerOutput(
        entity_id=uuid4(),
        risk_score=risk_score,
        top_signals=[],
        scorer_version="xgb-test",
        inference_latency_ms=1.0,
        route=route,
    )
    observation = build_observation(
        event, ato_feature_vector, scorer, corpus_version="ato-v1"
    )
    profile = ReasonerProfile.from_assets(
        prompts_dir=PLUGIN.prompts_dir, corpus_dir=PLUGIN.corpus_dir
    )

    config = PolicyGateConfig.model_validate(observation.gate_context.gate_config)
    profile.authorize(config)

    assert observation.gate_context.gate_id == "policy"
    assert config.template_id == "ato-v3"


def test_installed_runtime_entry_point_resolves_to_the_plugin_singleton():
    assert _entry_point(PLUGIN_GROUP).load() is PLUGIN


def test_installed_eval_entry_point_resolves_to_the_adapter_singleton():
    assert _entry_point(EVAL_ADAPTER_GROUP).load() is ADAPTER


def test_framework_discovery_finds_the_ato_plugin_by_its_reasoner_id():
    plugins = discover_plugins(allow=frozenset({ATO_REGISTRATION.reasoner_id}))
    assert [p.registration.reasoner_id for p in plugins] == [
        ATO_REGISTRATION.reasoner_id
    ]
    assert plugins[0] is PLUGIN
