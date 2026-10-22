"""The example reasoner's registration record.

``EXAMPLE_REGISTRATION`` is what the ledger knows about the example reasoner:

- **Gate set**: the RAG policy gate (``"policy"``), advisory — it runs when
  the example asks the ledger to gate a widget (DR-29). The binding names
  the corpus the gate reads (DR-34): ``example-policy`` (the example's
  ``corpus/`` directory, loaded under that id) at tier 1, held by the
  reference provider, every passage pinned to its document version.
- **Bypass rules** (DR-30): ``low-score`` (``widget_score`` below
  ``LOW_SCORE_THRESHOLD`` → record ALLOW) and ``high-score`` (above
  ``HIGH_SCORE_THRESHOLD`` → record BLOCK). Everything between asks for the
  gate.
- **Recordable actions**: ALLOW and BLOCK.
- **Native namespace** (DR-32): the example names the subject it scores, a
  widget, as ``(example-widget, widget_id)``. The declaration carries the
  UUID5 namespace the example's widget ids have always derived under, so
  the ledger's passthrough resolver assigns exactly the ``entity_id`` (and
  the ``entity_type`` ``widget``) the example's decisions always had.
- **Enforcement profile**: Tier 3 fires on ``thin_history`` (a widget with
  few prior inspections, on gated decisions), and Tier 5 treats REGION_A
  and REGION_B policy evidence in one gate output as a jurisdiction
  conflict.
"""

from __future__ import annotations

from uuid import UUID

from core.actions import DecisionAction
from core.gate import Compare
from core.identity import NativeNamespace
from core.observation import BypassRule, GateBinding, ReasonerRegistration

#: The corpus the example's binding to the policy gate names: its
#: ``corpus/`` directory, loaded under this id.
EXAMPLE_CORPUS_ID = "example-policy"

#: The example's binding to the RAG policy gate (DR-34): one corpus at tier 1.
POLICY_GATE_BINDING = GateBinding(
    gate_id="policy",
    config={
        "corpora": [
            {
                "corpus_id": EXAMPLE_CORPUS_ID,
                "provider": "reference",
                "tier": 1,
                "min_provenance": "pinned_version",
            }
        ],
        "k_per_corpus": 5,
    },
)

#: Below this ``widget_score`` the example records ALLOW.
LOW_SCORE_THRESHOLD = 0.2
#: Above this ``widget_score`` the example records BLOCK.
HIGH_SCORE_THRESHOLD = 0.9

#: The native namespace of the example's subjects: a widget, keyed by
#: ``widget_id``. Its UUID5 namespace is the one the example's widget ids
#: have always derived under (``WidgetEvent.entity_id``), declared where the
#: ledger can read it.
EXAMPLE_WIDGET_NAMESPACE = NativeNamespace(
    native_namespace="example-widget",
    namespace_uuid=UUID("8b4f2a6e-1c3d-5e7f-9a0b-2c4d6e8f0a1b"),
    entity_type="widget",
)

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
    gates=(POLICY_GATE_BINDING,),
    bypass_rules=(LOW_SCORE_RULE, HIGH_SCORE_RULE),
    recordable_actions=frozenset({DecisionAction.ALLOW, DecisionAction.BLOCK}),
    native_namespaces=(EXAMPLE_WIDGET_NAMESPACE,),
    novel_entity_feature="thin_history",
    jurisdiction_conflict_groups=(frozenset({"REGION_A"}), frozenset({"REGION_B"})),
)
