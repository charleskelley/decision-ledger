"""Resolution layer — append-only log of resolver outcomes for non-terminal decisions.

A non-terminal ``DecisionAction`` (``CHALLENGE`` or ``HOLD``) requires
one or more resolution attempts to reach a realized terminal action.
Each attempt is immutable; new states are recorded as new attempts.

Per DR-21, the universal ``ResolutionAttempt`` base lives in
``core.resolution.attempt`` and per-resolver-kind subclasses live in
sibling modules (``human.py``, ``sla_default.py``, …). The discriminator
is ``resolver_kind`` (Pydantic ``Literal[...]`` narrowing in subclasses).

MVP-implemented kinds: ``HUMAN`` (analyst review),
``SLA_DEFAULT`` (system-driven default at SLA expiry). Other
``ResolverKind`` enum members are wireframed extension points; their
subclasses land when implemented post-MVP.

Public API:
    ResolutionAttempt:           Universal base (no extension surface dict).
    ResolverKind:                Enum of all resolver kinds.
    ResolutionStatus:            Lifecycle status of a single attempt.
    HumanResolutionAttempt:      MVP — analyst review.
    SlaDefaultResolutionAttempt: MVP — SLA-expiry default action.
"""

from core.resolution.attempt import ResolutionAttempt, ResolutionStatus, ResolverKind
from core.resolution.human import HumanResolutionAttempt
from core.resolution.sla_default import SlaDefaultResolutionAttempt

__all__ = [
    "HumanResolutionAttempt",
    "ResolutionAttempt",
    "ResolutionStatus",
    "ResolverKind",
    "SlaDefaultResolutionAttempt",
]
