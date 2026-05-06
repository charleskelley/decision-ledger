"""Online feature computation for the ATO Reasoner.

FeatureService reads account history from Redis, computes sliding-window
velocity, novelty, geographic anomaly, and device consistency signals, then
updates Redis atomically for the next call.

All reads are pipelined into a single round-trip. Feature computation is
in _compute_features(), a pure function testable without Redis infrastructure.

Key schema (all keys prefixed ``ato:``):
    velocity:{account_id}   ZSET  score=unix_ts, member=event_id  TTL: 48 h
    ips:{account_id}        SET   seen IP addresses                TTL: 90 d
    devices:{account_id}    SET   seen device fingerprints         TTL: 90 d
    countries:{account_id}  SET   seen country codes               TTL: 90 d
    ua:{account_id}         SET   seen user-agent strings          TTL: 90 d
    last_geo:{account_id}   HASH  lat, lon, ts (epoch secs)        TTL: 90 d
    total:{account_id}      STR   lifetime event count (no TTL)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from reasoner.account_takeover.features import AtoFeatureVector, WindowSpec

if TYPE_CHECKING:
    from redis import Redis

    from reasoner.account_takeover.events import LoginEvent

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VELOCITY_TTL_SECS: int = 60 * 60 * 48  # 48 h
_PROFILE_TTL_SECS: int = 60 * 60 * 24 * 90  # 90 d
_SPARSE_HISTORY_THRESHOLD: int = 10
_MAX_TRAVEL_SPEED_KMH: float = 900.0

_DEVICE_SEEN_SCORE: float = 0.95
_DEVICE_NEW_SCORE: float = 0.05
_DEVICE_UNKNOWN_SCORE: float = 0.50
_UA_SEEN_SCORE: float = 0.98
_UA_NEW_SCORE: float = 0.10
_UA_UNKNOWN_SCORE: float = 0.50


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------


def _vel_key(account_id: str) -> str:
    return f"ato:velocity:{account_id}"


def _ips_key(account_id: str) -> str:
    return f"ato:ips:{account_id}"


def _devices_key(account_id: str) -> str:
    return f"ato:devices:{account_id}"


def _countries_key(account_id: str) -> str:
    return f"ato:countries:{account_id}"


def _ua_key(account_id: str) -> str:
    return f"ato:ua:{account_id}"


def _last_geo_key(account_id: str) -> str:
    return f"ato:last_geo:{account_id}"


def _total_key(account_id: str) -> str:
    return f"ato:total:{account_id}"


# ---------------------------------------------------------------------------
# Internal history dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AccountHistory:
    """Parsed account history read from Redis in a single pipeline round-trip.

    Args:
        velocity_1min: Event count in the last 1 minute.
        velocity_5min: Event count in the last 5 minutes.
        velocity_60min: Event count in the last 60 minutes.
        velocity_1440min: Event count in the last 1440 minutes (24 hours).
        ip_seen: Whether the current IP address has been seen before.
        ip_count: Total number of distinct IPs seen for this account.
        device_seen: Whether the current device fingerprint has been seen.
        device_count: Total number of distinct device fingerprints seen.
        country_seen: Whether the current country has been seen before.
        ua_seen: Whether the current user-agent has been seen before.
        ua_count: Total number of distinct user-agents seen.
        last_lat: Latitude from the most recent prior event, or None.
        last_lon: Longitude from the most recent prior event, or None.
        last_ts: Unix timestamp of the most recent prior event, or None.
        total_events: Lifetime count of events for this account.
    """

    velocity_1min: int
    velocity_5min: int
    velocity_60min: int
    velocity_1440min: int
    ip_seen: bool
    ip_count: int
    device_seen: bool
    device_count: int
    country_seen: bool
    ua_seen: bool
    ua_count: int
    last_lat: float | None
    last_lon: float | None
    last_ts: float | None
    total_events: int


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance between two points using the Haversine formula.

    Args:
        lat1: Latitude of the first point in decimal degrees.
        lon1: Longitude of the first point in decimal degrees.
        lat2: Latitude of the second point in decimal degrees.
        lon2: Longitude of the second point in decimal degrees.

    Returns:
        Distance in kilometres between the two points.
    """
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2.0 * r * math.asin(math.sqrt(a))


def _compute_features(
    event: LoginEvent,
    history: _AccountHistory,
    now_ts: float,
) -> AtoFeatureVector:
    """Compute an AtoFeatureVector from account history and the incoming event.

    This is a pure function — it performs no I/O and can be tested without
    any infrastructure dependencies.

    Args:
        event: The incoming login event being processed.
        history: Parsed account history read from Redis.
        now_ts: Current Unix timestamp used as the computation reference point.

    Returns:
        A fully populated AtoFeatureVector computed from the provided history
        and event fields.
    """
    new_account = history.total_events == 0

    # Novelty signals
    if new_account:
        ip_novelty = 0.5
        device_novelty = 0.5
        geo_novelty = 0.5
    else:
        ip_novelty = 0.0 if history.ip_seen else 1.0
        device_novelty = 0.0 if history.device_seen else 1.0
        geo_novelty = 0.0 if history.country_seen else 1.0

    # Consistency signals
    if new_account:
        device_consistency_score = _DEVICE_UNKNOWN_SCORE
        user_agent_consistency = _UA_UNKNOWN_SCORE
    else:
        device_consistency_score = (
            _DEVICE_SEEN_SCORE if history.device_seen else _DEVICE_NEW_SCORE
        )
        user_agent_consistency = _UA_SEEN_SCORE if history.ua_seen else _UA_NEW_SCORE

    # Geographic signals
    if (
        history.last_lat is not None
        and history.last_lon is not None
        and history.last_ts is not None
    ):
        distance_km = _haversine_km(
            history.last_lat,
            history.last_lon,
            event.geolocation.latitude,
            event.geolocation.longitude,
        )
        elapsed_hours = max((now_ts - history.last_ts) / 3600.0, 1e-6)
        travel_speed_kmh: float | None = distance_km / elapsed_hours
        impossible_travel = travel_speed_kmh > _MAX_TRAVEL_SPEED_KMH
    else:
        travel_speed_kmh = None
        impossible_travel = False

    sparse_history = history.total_events < _SPARSE_HISTORY_THRESHOLD

    windows = [
        WindowSpec(
            window_minutes=1,
            available=True,
            event_count=history.velocity_1min,
        ),
        WindowSpec(
            window_minutes=5,
            available=True,
            event_count=history.velocity_5min,
        ),
        WindowSpec(
            window_minutes=60,
            available=True,
            event_count=history.velocity_60min,
        ),
        WindowSpec(
            window_minutes=1440,
            available=True,
            event_count=history.velocity_1440min,
        ),
    ]

    return AtoFeatureVector(
        computed_at=datetime.fromtimestamp(now_ts, tz=UTC),
        account_id=event.account_id,
        sparse_history=sparse_history,
        velocity_1min=history.velocity_1min,
        velocity_5min=history.velocity_5min,
        velocity_60min=history.velocity_60min,
        velocity_1440min=history.velocity_1440min,
        ip_novelty=ip_novelty,
        device_novelty=device_novelty,
        geo_novelty=geo_novelty,
        impossible_travel=impossible_travel,
        travel_speed_kmh=travel_speed_kmh,
        device_consistency_score=device_consistency_score,
        user_agent_consistency=user_agent_consistency,
        windows=windows,
    )


# ---------------------------------------------------------------------------
# FeatureService
# ---------------------------------------------------------------------------


class FeatureService:
    """Online feature computation service backed by Redis.

    Reads account history from Redis in a single pipelined round-trip,
    delegates to the pure ``_compute_features`` function, then writes updated
    history back to Redis atomically.

    Args:
        redis: A connected Redis client instance.
    """

    def __init__(self, redis: Redis) -> None:
        """Initialize FeatureService with a connected Redis client.

        Args:
            redis: A connected Redis client instance.
        """
        self._redis = redis

    def compute(self, event: LoginEvent) -> AtoFeatureVector:
        """Compute features for an incoming login event.

        Reads 14 Redis commands in a single pipeline, computes the feature
        vector via the pure ``_compute_features`` function, then writes 13
        update commands back in a second pipeline.

        Args:
            event: The validated login event to compute features for.

        Returns:
            A fully populated ``AtoFeatureVector`` for the event.
        """
        account_id = event.account_id
        now_ts = event.timestamp.timestamp()

        vel_key = _vel_key(account_id)
        ips_key = _ips_key(account_id)
        devices_key = _devices_key(account_id)
        countries_key = _countries_key(account_id)
        ua_key = _ua_key(account_id)
        last_geo_key = _last_geo_key(account_id)
        total_key = _total_key(account_id)

        # ------------------------------------------------------------------
        # Read pipeline — 14 commands, single round-trip
        # ------------------------------------------------------------------
        rp = self._redis.pipeline(transaction=False)
        rp.zremrangebyscore(vel_key, "-inf", now_ts - 86400)  # 0: trim
        rp.zcount(vel_key, now_ts - 60, now_ts)  # 1: 1min
        rp.zcount(vel_key, now_ts - 300, now_ts)  # 2: 5min
        rp.zcount(vel_key, now_ts - 3600, now_ts)  # 3: 60min
        rp.zcount(vel_key, now_ts - 86400, now_ts)  # 4: 1440min
        rp.sismember(ips_key, event.ip_address)  # 5
        rp.scard(ips_key)  # 6
        rp.sismember(devices_key, event.device_fingerprint)  # 7
        rp.scard(devices_key)  # 8
        rp.sismember(countries_key, event.geolocation.country)  # 9
        rp.sismember(ua_key, event.user_agent)  # 10
        rp.scard(ua_key)  # 11
        rp.hgetall(last_geo_key)  # 12
        rp.get(total_key)  # 13
        results = rp.execute()

        # Parse results
        total_events = int(results[13]) if results[13] else 0

        raw_geo: dict = results[12] or {}
        last_lat: float | None = None
        last_lon: float | None = None
        last_ts: float | None = None
        if raw_geo:
            try:
                last_lat = float(raw_geo[b"lat"])
                last_lon = float(raw_geo[b"lon"])
                last_ts = float(raw_geo[b"ts"])
            except (KeyError, ValueError):
                pass

        history = _AccountHistory(
            velocity_1min=int(results[1]),
            velocity_5min=int(results[2]),
            velocity_60min=int(results[3]),
            velocity_1440min=int(results[4]),
            ip_seen=bool(results[5]),
            ip_count=int(results[6]),
            device_seen=bool(results[7]),
            device_count=int(results[8]),
            country_seen=bool(results[9]),
            ua_seen=bool(results[10]),
            ua_count=int(results[11]),
            last_lat=last_lat,
            last_lon=last_lon,
            last_ts=last_ts,
            total_events=total_events,
        )

        feature_vector = _compute_features(event, history, now_ts)

        lat = event.geolocation.latitude
        lon = event.geolocation.longitude

        # ------------------------------------------------------------------
        # Write pipeline — 13 commands
        # ------------------------------------------------------------------
        wp = self._redis.pipeline(transaction=False)
        wp.zadd(vel_key, {event.event_id: now_ts})
        wp.expire(vel_key, _VELOCITY_TTL_SECS)
        wp.sadd(ips_key, event.ip_address)
        wp.expire(ips_key, _PROFILE_TTL_SECS)
        wp.sadd(devices_key, event.device_fingerprint)
        wp.expire(devices_key, _PROFILE_TTL_SECS)
        wp.sadd(countries_key, event.geolocation.country)
        wp.expire(countries_key, _PROFILE_TTL_SECS)
        wp.sadd(ua_key, event.user_agent)
        wp.expire(ua_key, _PROFILE_TTL_SECS)
        wp.hset(
            last_geo_key,
            mapping={"lat": str(lat), "lon": str(lon), "ts": str(now_ts)},
        )
        wp.expire(last_geo_key, _PROFILE_TTL_SECS)
        wp.incr(total_key)
        wp.execute()

        log.debug(
            "features.computed",
            component="features",
            account_id=account_id,
            sparse_history=feature_vector.sparse_history,
            velocity_1min=feature_vector.velocity_1min,
        )

        return feature_vector
