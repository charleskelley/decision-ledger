"""EnforcementDecision — output contract of the deterministic enforcement layer.

The enforcement layer reads a ``GateVerdict`` (or ``None`` on the fast
path / schema-failure path) and produces a deterministic
``decision_action`` plus an audit-grade override log. ``EnforcementDecision``
is the typed contract for that intermediate result.

Per DR-21 this lives in its own module rather than under
``core/bundle.py`` because it's the enforcement layer's transient output,
not a bundle component — the bundle composition layer
(``app/audit/bundle.build_bundle``) consumes an ``EnforcementDecision``
and unpacks its fields into the ``DecisionBundle`` it produces.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from core.actions import DecisionAction


class EnforcementDecision(BaseModel):
    """Decision-time action plus override log from the deterministic enforcement layer.

    The enforcement layer is purely rule-based — no calls to the gate,
    the domain reasoner, or any other non-deterministic component. Every
    execution path is deterministic given the same inputs. This is what
    makes replay work: re-executing enforcement against the cached
    ``gate_output.verdict`` from a bundle must produce the same
    ``decision_action``.

    For non-terminal actions (CHALLENGE, HOLD), the ultimately-realized
    action is recorded later in the resolution attempt log (see
    ``core.resolution``). The enforcement layer's job ends at producing
    this decision; resolution is a separate, append-only artifact.

    Args:
        decision_action: The action this decision produces. Immutable.
            For terminal actions (ALLOW, BLOCK) this is the realized
            action; for non-terminal actions (CHALLENGE, HOLD) the
            realized action is computed from the resolution attempt log.
        enforcement_rule_applied: The override rule that fired, if any.
            ``None`` when the gate output was accepted without override.
        override_log: Ordered list of rule evaluations and outcomes.
            Records every trigger check performed, including those that
            did not fire.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    decision_action: DecisionAction
    enforcement_rule_applied: str | None
    override_log: list[str]
