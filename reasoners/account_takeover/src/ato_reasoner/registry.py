"""ATO Reasoner registration record.

The framework's ``StaticReasonerRegistry`` (in ``app/reasoner_registry.py``)
is reasoner-agnostic. Each reasoner declares its own
``ReasonerRegistration`` constant in its own package; the deployment
composer collects them.

``ATO_REGISTRATION`` is the canonical registration for the Account Takeover
reasoner — it pins the reasoner identity, allowed gate, and the enforcement
profile the framework reads for Tier 3 (``sparse_history`` marks a novel
account) and Tier 5 (US and EU policy snippets in the same retrieval set
constitute a jurisdiction conflict).

Which prompt templates and jurisdictions ATO may name in its gate context is
the RAG policy gate's call (DR-28): the gate accepts the templates in ATO's
``prompts/`` and the jurisdictions its ``corpus/`` front matter covers
(``ato-v1``..``ato-v3``; ``US_FEDERAL``, ``US_STATE``, ``EU_GDPR``,
``INTERNAL``), so tightening either is a change to those directories.
"""

from __future__ import annotations

from core.observation import ReasonerRegistration

ATO_REGISTRATION = ReasonerRegistration(
    reasoner_id="ato-reasoner",
    reasoner_name="ATO Reasoner",
    allowed_gate_ids=frozenset({"policy"}),
    novel_entity_feature="sparse_history",
    jurisdiction_conflict_groups=(
        frozenset({"US_FEDERAL", "US_STATE"}),
        frozenset({"EU_GDPR"}),
    ),
)
