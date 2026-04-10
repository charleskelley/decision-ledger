"""Deterministic enforcement layer — rule-based action resolution from policy gate.

The enforcement layer is pure rule-based logic with no LLM calls. Every execution path
is deterministic given the same inputs. This determinism is what makes audit replay
work: re-executing enforcement against cached intermediate states produces an identical
result.

Routing triggers (in priority order):
    1. Schema validation failure → HOLD
    2. Adversarial probe flag detected → BLOCK
    3. Novel entity (< N historical events) → HOLD with review packet
    4. Low confidence + high-risk action (BLOCK or HOLD) → HOLD
    5. Jurisdiction conflict in retrieved policies → HOLD with conflict detail
    6. No trigger fired → most conservative permitted_action from PolicyGateOutput
"""
