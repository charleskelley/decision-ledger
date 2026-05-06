"""ATO Reasoner registration record.

The framework's ``StaticReasonerRegistry`` (in ``app/reasoner_registry.py``)
is reasoner-agnostic. Each reasoner declares its own
``ReasonerRegistration`` constant in its own package; the deployment
composer collects them.

``ATO_REGISTRATION`` is the canonical registration for the Account Takeover
reasoner — it pins the reasoner identity, allowed gate, allowed prompt
templates, and allowed jurisdictions. Any of these tightening would
require a coordinated change here and in the corresponding policy corpus.
"""

from __future__ import annotations

from core.observation import ReasonerRegistration

ATO_REGISTRATION = ReasonerRegistration(
    reasoner_id="ato-reasoner",
    reasoner_name="ATO Reasoner",
    allowed_gate_ids=frozenset({"policy"}),
    allowed_prompt_template_ids=frozenset({"ato-v1"}),
    allowed_jurisdictions=frozenset({"US_FEDERAL", "US_STATE", "EU_GDPR", "INTERNAL"}),
)
