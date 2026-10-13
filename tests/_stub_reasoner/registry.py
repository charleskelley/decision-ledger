"""Stub reasoner registration record.

Declares the stub reasoner's identity, the gates and prompt template it may
use, the jurisdictions it may claim, and its enforcement profile: Tier 3
fires on the ``thin_history`` feature and Tier 5 treats ``REGION_A`` and
``REGION_B`` evidence in one decision as a jurisdiction conflict.

The allowed gates are the policy gate (``"policy"``, the stub's default gate
context) and the two test-only fake gates in ``tests/_fake_gate``
(``"fake"`` and the deterministic ``"fake-rules"``), so framework tests can
route a stub observation to a scripted gate without a registration variant.
"""

from __future__ import annotations

from core.observation import ReasonerRegistration

STUB_REGISTRATION = ReasonerRegistration(
    reasoner_id="stub-reasoner",
    reasoner_name="Stub Reasoner",
    allowed_gate_ids=frozenset({"policy", "fake", "fake-rules"}),
    allowed_prompt_template_ids=frozenset({"stub-v1"}),
    allowed_jurisdictions=frozenset({"REGION_A", "REGION_B"}),
    novel_entity_feature="thin_history",
    jurisdiction_conflict_groups=(frozenset({"REGION_A"}), frozenset({"REGION_B"})),
)
