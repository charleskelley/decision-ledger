"""Stub reasoner registration record.

Declares the stub reasoner's identity, its gate set, its bypass rules, and
its enforcement profile:

- **Gate set**: the test-only scripted gate ``"fake"`` (``tests/_fake_gate``),
  advisory — it runs when the stub asks for a gate. ``STUB_REGISTRATION_WITH_LOOKUP``
  also binds the lookup gate ``"fake-lookup"`` mandatory, so every decision,
  recorded ones too, runs it (the integration tests of gate sets, escrow,
  and verify). Tests that need another gate set build a variant with
  ``model_copy(update={"gates": ...})``.
- **Bypass rules**: ``low-score`` (``widget_score`` below 0.2 → record
  ALLOW) and ``high-score`` (above 0.9 → record BLOCK). Everything between
  goes to the gate.
- **Enforcement profile**: Tier 3 fires on the ``thin_history`` feature, and
  Tier 5 treats ``REGION_A`` and ``REGION_B`` evidence in one gate output as
  a jurisdiction conflict.

Which templates and jurisdictions it may name in a gate's configuration is
that gate's business (DR-28).
"""

from __future__ import annotations

from core.actions import DecisionAction
from core.gate import Compare
from core.observation import BypassRule, GateBinding, ReasonerRegistration

#: Below this ``widget_score`` the stub records ALLOW.
LOW_SCORE_THRESHOLD = 0.2
#: Above this ``widget_score`` the stub records BLOCK.
HIGH_SCORE_THRESHOLD = 0.9

LOW_SCORE_RULE = BypassRule(
    rule_id="low-score",
    version="1",
    description="widget_score below 0.2: the stub scorer is confident it is fine",
    predicate=Compare(field="label_value", op="lt", value=LOW_SCORE_THRESHOLD),
    action=DecisionAction.ALLOW,
)
HIGH_SCORE_RULE = BypassRule(
    rule_id="high-score",
    version="1",
    description="widget_score above 0.9: the stub scorer is confident it is broken",
    predicate=Compare(field="label_value", op="gt", value=HIGH_SCORE_THRESHOLD),
    action=DecisionAction.BLOCK,
)

STUB_REGISTRATION = ReasonerRegistration(
    reasoner_id="stub-reasoner",
    reasoner_name="Stub Reasoner",
    gates=(GateBinding(gate_id="fake"),),
    bypass_rules=(LOW_SCORE_RULE, HIGH_SCORE_RULE),
    recordable_actions=frozenset({DecisionAction.ALLOW, DecisionAction.BLOCK}),
    novel_entity_feature="thin_history",
    jurisdiction_conflict_groups=(frozenset({"REGION_A"}), frozenset({"REGION_B"})),
)

#: The stub registration with the lookup gate bound mandatory beside ``fake``:
#: a record runs ``fake-lookup`` too, and a gated decision runs both.
STUB_REGISTRATION_WITH_LOOKUP = ReasonerRegistration(
    reasoner_id=STUB_REGISTRATION.reasoner_id,
    reasoner_name=STUB_REGISTRATION.reasoner_name,
    gates=(
        GateBinding(gate_id="fake-lookup", mandatory=True),
        GateBinding(gate_id="fake"),
    ),
    bypass_rules=STUB_REGISTRATION.bypass_rules,
    recordable_actions=STUB_REGISTRATION.recordable_actions,
    novel_entity_feature=STUB_REGISTRATION.novel_entity_feature,
    jurisdiction_conflict_groups=STUB_REGISTRATION.jurisdiction_conflict_groups,
)
