"""ATO Reasoner feature vector and sliding window contracts.

entity_id is derived from account_id using the same UUID5 namespace as
LoginEvent, ensuring consistent framework identity across the event and
feature layers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from reasoner.account_takeover.events import _ATO_ACCOUNT_NS


class WindowSpec(BaseModel):
    """Sliding window specification used during ATO feature computation.

    Attributes:
        window_minutes: Duration of the window in minutes.
        available: Whether sufficient history existed to compute this window.
            False when the account has fewer events than the window requires.
        event_count: Number of events observed in this window.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    window_minutes: int = Field(gt=0)
    available: bool
    event_count: int = Field(ge=0)


class AtoFeatureVector(BaseModel):
    """Computed ATO feature snapshot from the online feature layer.

    entity_id is derived deterministically from account_id via UUID5.
    account_id is retained as a domain attribute for application-layer use.

    All novelty and consistency values are in [0.0, 1.0]. sparse_history is
    set True when the account has fewer than the configured minimum historical
    events — downstream components treat sparse-history vectors with lower
    confidence.

    Attributes:
        entity_type: Framework subject classification. Always "account".
        account_id: ATO domain business key for the account.
        computed_at: Timestamp of feature computation (UTC).
        sparse_history: True if the account has fewer than N historical events.
        velocity_1min: Event count for this account in the last 1 minute.
        velocity_5min: Event count in the last 5 minutes.
        velocity_60min: Event count in the last 60 minutes.
        velocity_1440min: Event count in the last 24 hours.
        ip_novelty: [0.0-1.0] — 0 = known IP, 1 = never seen before.
        device_novelty: [0.0-1.0] novelty of the device fingerprint.
        geo_novelty: [0.0-1.0] country-level novelty for this account.
        impossible_travel: True if speed between last and current location
            exceeds the configured physical travel limit.
        travel_speed_kmh: Speed in km/h between last and current location.
            None if no previous location is on record for this account.
        device_consistency_score: Fingerprint match score vs. historical mean.
        user_agent_consistency: User-agent consistency vs. historical pattern.
        windows: Window specifications used during computation.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    # Framework identity fields
    entity_type: Literal["account"] = "account"
    computed_at: datetime

    # Domain identity
    account_id: str

    # History quality
    sparse_history: bool

    # Velocity features
    velocity_1min: int = Field(ge=0)
    velocity_5min: int = Field(ge=0)
    velocity_60min: int = Field(ge=0)
    velocity_1440min: int = Field(ge=0)

    # Novelty features — [0.0, 1.0]
    ip_novelty: float = Field(ge=0.0, le=1.0)
    device_novelty: float = Field(ge=0.0, le=1.0)
    geo_novelty: float = Field(ge=0.0, le=1.0)

    # Geographic signals
    impossible_travel: bool
    travel_speed_kmh: float | None

    # Device signals
    device_consistency_score: float = Field(ge=0.0, le=1.0)
    user_agent_consistency: float = Field(ge=0.0, le=1.0)

    # Window metadata
    windows: list[WindowSpec]

    @computed_field
    @property
    def entity_id(self) -> UUID:
        """Framework UUID derived deterministically from account_id."""
        return uuid.uuid5(_ATO_ACCOUNT_NS, self.account_id)
