"""SlaDefaultResolutionAttempt — SLA-expiry default-action resolver subclass.

The MVP-implemented system-driven variant of ``ResolutionAttempt``.
Recorded when a non-terminal decision exceeds its SLA without a
``COMPLETED`` resolution attempt; the default action depends on the
account tier (per ``int-hold-queue-v1.md`` §5).
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from core.resolution.attempt import ResolutionAttempt


class SlaDefaultResolutionAttempt(ResolutionAttempt):
    """Resolution attempt produced by the SLA-expiry default-action timer.

    Attributes:
        resolver_kind: Discriminator value ``"SLA_DEFAULT"``.
        account_tier: The account tier whose default-action policy was
            applied at SLA expiry. Plain string — domain code uses its
            own tier enum for type-safe construction. Common values:
            ``"STANDARD"``, ``"HIGH_VALUE"``, ``"ENTERPRISE"``.
        sla_window_seconds: The SLA window (in seconds) configured at the
            time of expiry. Recorded for audit so that subsequent SLA
            policy changes don't obscure the policy that actually applied
            to this decision.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    resolver_kind: Literal["SLA_DEFAULT"] = "SLA_DEFAULT"
    account_tier: str
    sla_window_seconds: int = Field(ge=0)
