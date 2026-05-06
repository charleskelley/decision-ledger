"""SHA-256 idempotency deduplication backed by Redis.

Each event_id is hashed to a fixed-length key and stored with a TTL.
The check and the store are atomic via Redis SET NX EX, so there is no
race condition between two consumers processing the same event_id
concurrently.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis

_KEY_PREFIX = "ato:dedup:"
_DEFAULT_TTL_SECONDS = 86_400  # 24 hours


class IdempotencyStore:
    """Redis-backed idempotency store for login event deduplication.

    SHA-256 of the event_id is used as the Redis key so that key length
    is bounded and constant regardless of event_id format. Keys expire
    after ttl_seconds to bound memory growth.

    Args:
        client: Connected Redis client (decode_responses=True).
        ttl_seconds: Key TTL in seconds. Defaults to 86400 (24 hours).
    """

    def __init__(
        self,
        client: redis.Redis,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        """Initialize the store with a Redis client and TTL."""
        self._client = client
        self._ttl = ttl_seconds

    def is_new(self, event_id: str) -> bool:
        """Register event_id and return whether it is new.

        Uses Redis SET NX EX so the check and store are a single atomic
        operation. No two callers can both receive True for the same
        event_id within the TTL window.

        Args:
            event_id: Unique event identifier from the LoginEvent.

        Returns:
            True if this is the first time this event_id has been seen
            within the TTL window. False if it is a duplicate.
        """
        key = _KEY_PREFIX + hashlib.sha256(event_id.encode()).hexdigest()
        return self._client.set(key, "1", nx=True, ex=self._ttl) is not None
