"""EnforcementDecision — output contract of the deterministic enforcement layer.

The enforcement layer works at two levels (DR-28, DR-29). At the gate level
it reads one gate's ``GateOutput`` (or none, when the gate failed) and
produces that gate's enforced action plus an audit-grade override log; each
``GateRecord`` in a bundle carries one such ``EnforcementDecision``. At the
decision level it applies the decision-level tiers and the conjunction
combinator to the gate set and produces the bundle's ``decision_action``,
the decision-level rule, and the decision-level log — the same type again.

Per DR-21 this lives in its own module rather than under ``core/bundle.py``
because it is the enforcement layer's output contract: a gate record embeds
it, and the bundle composition layer (``app/audit/bundle.build_bundle``)
unpacks the decision-level one into the ``DecisionBundle`` it produces.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from core.actions import DecisionAction


class EnforcementDecision(BaseModel):
    """Decision-time action plus override log from the deterministic enforcement layer.

    The enforcement layer is purely rule-based — no calls to the gate,
    the domain reasoner, or any other non-deterministic component. Every
    execution path is deterministic given the same inputs. This is what
    makes replay work: re-executing enforcement against the cached gate
    outputs in a bundle must produce the same ``decision_action``.

    For non-terminal actions (CHALLENGE, HOLD), the ultimately-realized
    action is recorded later in the resolution attempt log (see
    ``core.resolution``). The enforcement layer's job ends at producing
    this decision; resolution is a separate, append-only artifact.

    Attributes:
        decision_action: The action this decision produces. Immutable.
            For terminal actions (ALLOW, BLOCK) this is the realized
            action; for non-terminal actions (CHALLENGE, HOLD) the
            realized action is computed from the resolution attempt log.
        enforcement_rule_applied: The rule that fired, if any: a gate-level
            tier for a gate's enforcement, a decision-level rule for the
            decision's. ``None`` when no rule overrode the result.
        override_log: Ordered list of rule evaluations and outcomes.
            Records every trigger check performed, including those that
            did not fire.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    decision_action: DecisionAction
    enforcement_rule_applied: str | None
    override_log: list[str]
