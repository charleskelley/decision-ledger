"""Stub reasoner registration record.

Declares the stub reasoner's identity, the gate and prompt template it may
use, the jurisdictions it may claim, and its enforcement profile: Tier 3
fires on the ``thin_history`` feature and Tier 5 treats ``REGION_A`` and
``REGION_B`` policies in one retrieval set as a jurisdiction conflict.
"""

from __future__ import annotations

from core.observation import ReasonerRegistration

STUB_REGISTRATION = ReasonerRegistration(
    reasoner_id="stub-reasoner",
    reasoner_name="Stub Reasoner",
    allowed_gate_ids=frozenset({"policy"}),
    allowed_prompt_template_ids=frozenset({"stub-v1"}),
    allowed_jurisdictions=frozenset({"REGION_A", "REGION_B"}),
    novel_entity_feature="thin_history",
    jurisdiction_conflict_groups=(frozenset({"REGION_A"}), frozenset({"REGION_B"})),
)
