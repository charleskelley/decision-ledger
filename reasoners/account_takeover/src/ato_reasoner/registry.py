"""ATO Reasoner registration record.

The framework's ``StaticReasonerRegistry`` (in ``app/reasoner_registry.py``)
is reasoner-agnostic. Each reasoner declares its own
``ReasonerRegistration`` constant in its own package; the deployment
composer collects them.

``ATO_REGISTRATION`` is the canonical registration for the Account Takeover
reasoner:

- **Gate set**: the RAG policy gate (``"policy"``), advisory — it runs when
  ATO asks the ledger to gate a login (DR-29).
- **Bypass rules** (DR-30): the scorer's two confidence bands. A login whose
  ``risk_score`` (the ``label_value``) is below ``ALLOW_BAND_THRESHOLD`` is
  recorded ALLOW under ``allow-band``; one above ``BLOCK_BAND_THRESHOLD`` is
  recorded BLOCK under ``block-band``. Everything between asks for the gate.
  These numbers live here and nowhere in the framework; the scorer's band
  metrics read them from this module.
- **Recordable actions**: ALLOW and BLOCK (the bands' actions).
- **Enforcement profile**: Tier 3 fires on ``sparse_history`` (a novel
  account, on gated logins), and Tier 5 treats US and EU policy snippets in
  the same retrieval set as a jurisdiction conflict.

Which prompt templates and jurisdictions ATO may name in its gate context is
the RAG policy gate's call (DR-28): the gate accepts the templates in ATO's
``prompts/`` and the jurisdictions its ``corpus/`` front matter covers
(``ato-v1``..``ato-v3``; ``US_FEDERAL``, ``US_STATE``, ``EU_GDPR``,
``INTERNAL``), so tightening either is a change to those directories.
"""

from __future__ import annotations

from core.actions import DecisionAction
from core.gate import Compare
from core.observation import BypassRule, GateBinding, ReasonerRegistration

#: ``risk_score`` below this is recorded ALLOW (``allow-band``).
ALLOW_BAND_THRESHOLD = 0.20

#: ``risk_score`` above this is recorded BLOCK (``block-band``). The XGBoost
#: classifier's outputs cluster near 0 or 1 (binary objective), so
#: moderately confident attacks (~0.85-0.95) still go to the policy gate for
#: adjudication rather than being blocked outright (DR-25).
BLOCK_BAND_THRESHOLD = 0.95

ALLOW_BAND = BypassRule(
    rule_id="allow-band",
    version="1",
    description=(
        f"risk_score below {ALLOW_BAND_THRESHOLD}: the scorer is confident the "
        "login is benign; record ALLOW without the policy gate"
    ),
    predicate=Compare(field="label_value", op="lt", value=ALLOW_BAND_THRESHOLD),
    action=DecisionAction.ALLOW,
)

BLOCK_BAND = BypassRule(
    rule_id="block-band",
    version="1",
    description=(
        f"risk_score above {BLOCK_BAND_THRESHOLD}: the scorer is confident the "
        "login is an attack; record BLOCK without the policy gate"
    ),
    predicate=Compare(field="label_value", op="gt", value=BLOCK_BAND_THRESHOLD),
    action=DecisionAction.BLOCK,
)

ATO_REGISTRATION = ReasonerRegistration(
    reasoner_id="ato-reasoner",
    reasoner_name="ATO Reasoner",
    gates=(GateBinding(gate_id="policy"),),
    bypass_rules=(ALLOW_BAND, BLOCK_BAND),
    recordable_actions=frozenset({DecisionAction.ALLOW, DecisionAction.BLOCK}),
    novel_entity_feature="sparse_history",
    jurisdiction_conflict_groups=(
        frozenset({"US_FEDERAL", "US_STATE"}),
        frozenset({"EU_GDPR"}),
    ),
)
