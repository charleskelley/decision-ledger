"""Deterministic enforcement layer — rule-based action resolution from gate output.

The enforcement layer is pure rule-based logic with no LLM calls. Every execution path
is deterministic given the same inputs. This determinism is what makes audit replay
work: re-executing enforcement against cached intermediate states produces an identical
result.

Gate-level tiers (applied to one gate's ``GateOutput``; first that fires wins):
    1. Schema validation failure (no verdict, or the gate failed closed) → HOLD
    2. Adversarial probe flag detected → BLOCK (skipped for a deterministic gate)
    4. Low confidence + high-risk action (BLOCK or HOLD) → HOLD
       (skipped for a deterministic gate)
    5. Jurisdiction conflict across the gate's evidence → HOLD
       (runs only when the evidence carries jurisdictions)
    6. No trigger fired → most conservative permitted action from the verdict

Decision-level rules (applied once per decision):
    3. Novel entity (gated decisions; insufficient history) → the advisory
       gates' verdicts are held for review (HOLD), unless an advisory gate's
       own Tier 1 or 2 decided; mandatory gates and the proposal are untouched
    B. Bypass rule not satisfied (recorded decisions) → HOLD floor

The combinator then takes the most restrictive of the proposal, every
mandatory gate, the advisory side, and any floor (DR-29). A decision with a
single advisory gate and no proposal resolves exactly as the single-gate
resolver of DR-28 did.
"""
