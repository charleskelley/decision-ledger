"""LoginEvent factory for the ATO Reasoner synthetic event generator.

EventFactory is the stateful core of the generator. It produces LoginEvent
objects conforming to scenario configuration parameters. Each factory
instance is bound to a single scenario and a fixed set of account IDs.

Design notes:
    - The factory is infrastructure-free: no Redis, no I/O, no external calls.
    - Device fingerprints are structured as colon-separated component tokens
      so the feature layer can compute similarity as a fraction of matching
      components. STABLE → all 5 components fixed. PARTIAL → 3 of 5 fixed
      (yielding ~0.60 similarity). ROTATING → all components random.
    - partial_match_score in DeviceConfig documents the intended feature layer
      output — the feature layer must be calibrated to honor it. The generator
      produces structurally similar fingerprints; exact score calibration is
      a feature layer concern.
    - Impossible travel is implemented by routing consecutive events for the
      same account to cities in different geographic regions. At any realistic
      event velocity this produces travel speeds > 10,000 km/h — well above
      any configured min_travel_speed_kmh.
    - schema_violation_attempt in AdversarialConfig is intentionally ignored.
      The generator always emits valid LoginEvent objects; testing the
      ingestion layer's schema validation is an ingestion layer concern.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

from generator import data as _data
from generator.config import (
    DeviceConsistency,
    GeoMobility,
    RotationIntensity,
    ScenarioConfig,
)
from reasoner.account_takeover.events import (
    AuthMethod as EventAuthMethod,
)
from reasoner.account_takeover.events import (
    AuthOutcome,
    Geolocation,
    LoginEvent,
)

# Number of IP addresses pre-built per account for each rotation intensity.
# HIGH rotation draws from a large shared pool instead.
_IP_POOL_SIZE: dict[RotationIntensity, int] = {
    RotationIntensity.NONE: 1,
    RotationIntensity.LOW: 2,
    RotationIntensity.MODERATE: 8,
    RotationIntensity.HIGH: 40,
}

# How many events before the IP rotates to the next address in the pool.
_IP_ROTATION_INTERVAL: dict[RotationIntensity, int] = {
    RotationIntensity.NONE: 10_000,  # effectively never
    RotationIntensity.LOW: 10,
    RotationIntensity.MODERATE: 3,
    RotationIntensity.HIGH: 1,
}

# How many events before the ASN rotates.
_ASN_ROTATION_INTERVAL: dict[RotationIntensity, int] = {
    RotationIntensity.NONE: 10_000,
    RotationIntensity.LOW: 15,
    RotationIntensity.MODERATE: 5,
    RotationIntensity.HIGH: 1,
}


class EventFactory:
    """Stateful factory that builds LoginEvents according to a ScenarioConfig.

    One factory instance is created per scenario run. It maintains per-account
    state so that patterns like device stability, IP rotation, and impossible
    travel are correctly reproduced across consecutive events for the same
    account.

    Args:
        config: The scenario configuration driving all generation parameters.
        account_ids: Fixed pool of account IDs to draw from. The factory
            never creates new account IDs after initialisation.
        seed: Optional random seed for reproducible output.
    """

    def __init__(
        self,
        config: ScenarioConfig,
        account_ids: list[str],
        seed: int | None = None,
    ) -> None:
        """Initialise factory and build per-account state pools."""
        self._config = config
        self._gen = config.generation
        self._account_ids = account_ids
        self._rng = random.Random(seed)

        # Per-account state
        self._event_counts: dict[str, int] = dict.fromkeys(account_ids, 0)
        self._device_pools: dict[str, list[str]] = {}
        self._ip_pools: dict[str, list[str]] = {}
        self._asn_pool: list[str] = self._build_asn_pool()
        self._account_asn_indices: dict[str, int] = {}
        self._home_cities: dict[str, _data.CityRecord] = {}
        self._last_regions: dict[str, str] = {}
        self._ua_pool: list[str] = self._build_ua_pool()

        # Build per-account state
        for aid in account_ids:
            self._device_pools[aid] = self._build_device_pool(aid)
            self._ip_pools[aid] = self._build_ip_pool()
            asn_max = len(self._asn_pool) - 1
            self._account_asn_indices[aid] = self._rng.randint(0, asn_max)
            home = self._rng.choice(_data.GEO_POOL)
            self._home_cities[aid] = home
            self._last_regions[aid] = home.region

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def build_event(self, account_id: str, timestamp: datetime) -> LoginEvent:
        """Build one LoginEvent for the given account at the given time.

        Args:
            account_id: Must be a member of the account_ids list provided
                at construction. The factory uses per-account state to
                maintain consistent device, IP, and geo patterns.
            timestamp: UTC timestamp to assign to this event.

        Returns:
            A validated LoginEvent ready for Redis emission.
        """
        n = self._event_counts[account_id]
        self._event_counts[account_id] += 1

        return LoginEvent(
            event_id=str(uuid.uuid4()),
            timestamp=timestamp,
            account_id=account_id,
            session_id=f"sess_{uuid.uuid4().hex[:16]}",
            ip_address=self._pick_ip(account_id, n),
            geolocation=self._pick_geo(account_id),
            device_fingerprint=self._pick_fingerprint(account_id, n),
            user_agent=self._pick_user_agent(),
            auth_method=self._pick_auth_method(),
            outcome=self._pick_outcome(),
            scenario_tag=self._config.scenario_id,
        )

    # ------------------------------------------------------------------
    # Pool builders (called once at init)
    # ------------------------------------------------------------------

    def _build_asn_pool(self) -> list[str]:
        """Build the ASN pool for this scenario."""
        net = self._gen.network
        if net.use_known_bad_asn:
            return list(_data.KNOWN_BAD_ASNS)
        if net.asn_rotation in (RotationIntensity.NONE, RotationIntensity.LOW):
            return list(_data.RESIDENTIAL_ASNS[:5])
        return list(_data.RESIDENTIAL_ASNS)

    def _build_ip_pool(self) -> list[str]:
        """Build the IP address pool for one account."""
        net = self._gen.network
        size = _IP_POOL_SIZE[net.ip_rotation]
        return [
            _data.random_ipv4(self._rng, known_bad=net.use_known_bad_asn)
            for _ in range(size)
        ]

    def _build_ua_pool(self) -> list[str]:
        """Build the user agent pool for this scenario."""
        from reasoner.account_takeover.events import AuthMethod as M

        cfg_method = self._gen.auth.method
        if cfg_method.value == M.SSO.value:
            return list(_data.SERVICE_USER_AGENTS)
        return list(_data.USER_AGENTS)

    def _build_device_pool(self, account_id: str) -> list[str]:
        """Build the device fingerprint pool for one account.

        Fingerprints are structured as five colon-separated hex tokens so the
        feature layer can compute component-level similarity. Token derivation:

            - STABLE: all 5 tokens seeded from account_id + component index.
              Every event for this account returns the same fingerprint.
            - PARTIAL: tokens 0-2 are stable (account-seeded), tokens 3-4
              are per-fingerprint (yielding ~0.60 component similarity). The
              feature layer should produce device_consistency_score ~0.52-0.65
              after calibration.
            - ROTATING: all 5 tokens are randomly seeded per fingerprint.
              Distinct fingerprints share no components.

        Args:
            account_id: Used as the stable seed for STABLE and PARTIAL modes.

        Returns:
            A list of fingerprint strings of length fingerprint_count.
        """
        cfg = self._gen.device
        count = cfg.fingerprint_count

        def _token(seed: str) -> str:
            return hashlib.sha1(seed.encode()).hexdigest()[:8]  # noqa: S324

        # Stable base tokens — derived from account_id + position
        stable = [_token(f"{account_id}:comp{i}") for i in range(5)]

        fingerprints: list[str] = []
        for fp_idx in range(count):
            if cfg.consistency == DeviceConsistency.STABLE:
                tokens = stable[:]
            elif cfg.consistency == DeviceConsistency.PARTIAL:
                # First 3 tokens stable, last 2 vary per fingerprint
                tokens = [
                    *stable[:3],
                    _token(f"{account_id}:fp{fp_idx}:comp3"),
                    _token(f"{account_id}:fp{fp_idx}:comp4"),
                ]
            else:  # ROTATING
                tokens = [
                    _token(f"rotate:{self._rng.random()}:comp{i}") for i in range(5)
                ]
            fingerprints.append(":".join(tokens))

        return fingerprints

    # ------------------------------------------------------------------
    # Per-event pickers
    # ------------------------------------------------------------------

    def _pick_fingerprint(self, account_id: str, event_num: int) -> str:
        """Return the device fingerprint for this event.

        Args:
            account_id: Account whose fingerprint pool to draw from.
            event_num: Zero-based event count for this account.
        """
        pool = self._device_pools[account_id]
        cfg = self._gen.device

        if cfg.consistency == DeviceConsistency.STABLE:
            return pool[0]
        if cfg.consistency == DeviceConsistency.PARTIAL:
            # Alternate between the fingerprints to produce partial matches
            return pool[event_num % len(pool)]
        # ROTATING: cycle sequentially (not randomly, so consecutive events
        # are always on different fingerprints for easy feature validation)
        return pool[event_num % len(pool)]

    def _pick_ip(self, account_id: str, event_num: int) -> str:
        """Return the source IP for this event based on ip_rotation config.

        Args:
            account_id: Account whose IP pool to draw from.
            event_num: Zero-based event count for this account.
        """
        pool = self._ip_pools[account_id]
        interval = _IP_ROTATION_INTERVAL[self._gen.network.ip_rotation]
        return pool[(event_num // interval) % len(pool)]

    def _pick_asn(self, account_id: str, event_num: int) -> str:
        """Return the ASN for this event based on asn_rotation config.

        Args:
            account_id: Account whose base ASN index to offset from.
            event_num: Zero-based event count for this account.
        """
        interval = _ASN_ROTATION_INTERVAL[self._gen.network.asn_rotation]
        base = self._account_asn_indices[account_id]
        idx = (base + event_num // interval) % len(self._asn_pool)
        return self._asn_pool[idx]

    def _pick_geo(self, account_id: str) -> Geolocation:
        """Return geographic data for this event.

        Respects geo.mobility and geo.impossible_travel. For impossible travel,
        each event is placed in a region different from the account's previous
        event — guaranteeing physically impossible travel speeds at any
        realistic event velocity.

        Args:
            account_id: Account whose home city and last region to reference.
        """
        geo_cfg = self._gen.geo
        mobility = geo_cfg.mobility

        if mobility == GeoMobility.STATIC:
            city = self._home_cities[account_id]
        elif mobility == GeoMobility.REGIONAL:
            home_region = self._home_cities[account_id].region
            city = self._rng.choice(_data.cities_for_region(home_region))
        else:  # ERRATIC
            if geo_cfg.impossible_travel:
                # Force a region change to guarantee impossible travel speed
                other = _data.other_regions(self._last_regions[account_id])
                target_region = self._rng.choice(other)
                city = self._rng.choice(_data.cities_for_region(target_region))
            else:
                city = self._rng.choice(_data.GEO_POOL)

        self._last_regions[account_id] = city.region

        # Derive a plausible ASN for this event — use account_id + event_count
        # to index consistently (the exact event count isn't available here,
        # so we use the _account_asn_indices base offset + region hash for variety)
        asn = self._rng.choice(
            _data.KNOWN_BAD_ASNS
            if self._gen.network.use_known_bad_asn
            else _data.RESIDENTIAL_ASNS
        )

        return Geolocation(
            latitude=city.latitude,
            longitude=city.longitude,
            country=city.country,
            city=city.city,
            asn=asn,
        )

    def _pick_user_agent(self) -> str:
        """Return a user agent string.

        For adversarial_probe scenarios with user_agent_injection enabled,
        returns the exact injection payload defined in the scenario config.
        This tests whether the pipeline sanitises user-supplied fields before
        constructing LLM prompts.
        """
        adv = self._gen.adversarial
        if adv and adv.user_agent_injection and adv.user_agent_payload:
            return adv.user_agent_payload
        return self._rng.choice(self._ua_pool)

    def _pick_auth_method(self) -> EventAuthMethod:
        """Return the auth method for this event, mapped from config enum.

        The generator config AuthMethod is a subset of the event AuthMethod
        enum. Conversion is safe for all four config values.
        """
        return EventAuthMethod(self._gen.auth.method.value)

    def _pick_outcome(self) -> AuthOutcome:
        """Return a weighted-random auth outcome based on outcome_weights config."""
        weights = self._gen.auth.outcome_weights
        outcomes = list(weights.keys())
        probabilities = list(weights.values())
        chosen = self._rng.choices(outcomes, weights=probabilities, k=1)[0]
        return AuthOutcome(chosen)
