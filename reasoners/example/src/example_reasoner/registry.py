"""The example reasoner's registration record.

``EXAMPLE_REGISTRATION`` is what the ledger knows about the example reasoner:

- **Gate set**: the RAG policy gate (``"policy"``), advisory — it runs when
  the example asks the ledger to gate a widget (DR-29).
- **Bypass rules** (DR-30): ``low-score`` (``widget_score`` below
  ``LOW_SCORE_THRESHOLD`` → record ALLOW) and ``high-score`` (above
  ``HIGH_SCORE_THRESHOLD`` → record BLOCK). Everything between asks for the
  gate.
- **Recordable actions**: ALLOW and BLOCK.
- **Enforcement profile**: Tier 3 fires on ``thin_history`` (a widget with
  few prior inspections, on gated decisions), and Tier 5 treats REGION_A
  and REGION_B policy evidence in one gate output as a jurisdiction
  conflict.
"""

from __future__ import annotations

from core.actions import DecisionAction
from core.gate import Compare
from core.observation import BypassRule, GateBinding, ReasonerRegistration

#: Below this ``widget_score`` the example records ALLOW.
LOW_SCORE_THRESHOLD = 0.2
#: Above this ``widget_score`` the example records BLOCK.
HIGH_SCORE_THRESHOLD = 0.9

LOW_SCORE_RULE = BypassRule(
    rule_id="low-score",
    version="1",
    description=(
        f"widget_score below {LOW_SCORE_THRESHOLD}: the widget is steady and "
        "aligned; record ALLOW without the policy gate"
    ),
    predicate=Compare(field="label_value", op="lt", value=LOW_SCORE_THRESHOLD),
    action=DecisionAction.ALLOW,
)

HIGH_SCORE_RULE = BypassRule(
    rule_id="high-score",
    version="1",
    description=(
        f"widget_score above {HIGH_SCORE_THRESHOLD}: the widget is shaking "
        "apart; record BLOCK without the policy gate"
    ),
    predicate=Compare(field="label_value", op="gt", value=HIGH_SCORE_THRESHOLD),
    action=DecisionAction.BLOCK,
)

EXAMPLE_REGISTRATION = ReasonerRegistration(
    reasoner_id="example-reasoner",
    reasoner_name="Example Reasoner",
    gates=(GateBinding(gate_id="policy"),),
    bypass_rules=(LOW_SCORE_RULE, HIGH_SCORE_RULE),
    recordable_actions=frozenset({DecisionAction.ALLOW, DecisionAction.BLOCK}),
    novel_entity_feature="thin_history",
    jurisdiction_conflict_groups=(frozenset({"REGION_A"}), frozenset({"REGION_B"})),
)
