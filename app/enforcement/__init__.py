"""Deterministic enforcement layer — rule-based action resolution from gate output.

The enforcement layer is pure rule-based logic with no LLM calls. Every execution path
is deterministic given the same inputs. This determinism is what makes audit replay
work: re-executing enforcement against cached intermediate states produces an identical
result.

Gate-level tiers (applied to one gate's ``GateOutput``; first that fires wins):
    1. Schema validation failure (no verdict) → HOLD
    2. Adversarial probe flag detected → BLOCK (skipped for a deterministic gate)
    4. Low confidence + high-risk action (BLOCK or HOLD) → HOLD
       (skipped for a deterministic gate)
    5. Jurisdiction conflict across the gate's evidence → HOLD
       (runs only when the evidence carries jurisdictions)
    6. No trigger fired → most conservative permitted action from the verdict

Decision-level tier (applied once per decision):
    3. Novel entity (insufficient history) → HOLD (route to human review)

The two levels combine in the historical tier priority (DR-28): gate Tiers 1-2
outrank Tier 3, which outranks gate Tiers 4-6, so a single-gate decision
resolves exactly as the single-pass resolver did.
"""
