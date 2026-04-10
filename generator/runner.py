"""Scenario runner for the ATO Reasoner synthetic event generator.

GeneratorRunner orchestrates the full generation loop for a scenario:
builds the account pool, drives EventFactory, handles timing and bursts,
and emits events to Redis Streams (or stdout in dry-run mode).

Redis emission:
    Events are serialised with model_dump_json() and stored as a single
    "data" field in each stream entry. The ingestion layer reads this field
    and validates the payload against LoginEvent before processing.

    Stream key default: "ato:login-events"

Timing:
    Live emission (not dry-run) sleeps between events based on
    events_per_minute and jitter_factor. Burst windows apply
    burst_factor as a velocity multiplier for burst_duration_s seconds.
    Burst windows are triggered every ~20% of total events.

Dry-run mode:
    Skips all sleeps and Redis connections. Emits one JSON object per
    line to stdout. Useful for offline inspection and testing.
"""

from __future__ import annotations

import random
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import redis as redis_lib

from generator.factory import EventFactory
from generator.loader import load_all_scenarios, load_scenario

if TYPE_CHECKING:
    from generator.config import ScenarioConfig, VelocityConfig

# How often (as a fraction of total events) to trigger a burst window.
_BURST_TRIGGER_FRACTION = 0.20


class GeneratorRunner:
    """Orchestrates scenario event generation and emission.

    Example::

        runner = GeneratorRunner()
        emitted = runner.run("baseline_normal", count=50, dry_run=True)
    """

    def run(
        self,
        scenario_id: str,
        count: int | None = None,
        *,
        dry_run: bool = False,
        redis_url: str = "redis://localhost:6379",
        stream_key: str = "ato:login-events",
        seed: int | None = None,
    ) -> int:
        """Run a scenario and emit events.

        Args:
            scenario_id: Scenario identifier, or "all" to run every scenario
                sequentially using each scenario's default events_per_run count.
            count: Number of events to generate. Defaults to the scenario's
                events_per_run value if None.
            dry_run: If True, print JSON to stdout and skip Redis + all timing
                delays. Useful for offline inspection and testing.
            redis_url: Redis connection URL. Ignored when dry_run is True.
            stream_key: Redis Streams key to XADD events to.
            seed: Optional random seed for reproducible output. Applies to
                both account ID generation and the EventFactory.

        Returns:
            Total number of events emitted.
        """
        if scenario_id == "all":
            scenarios = load_all_scenarios()
            total = 0
            for sid, config in scenarios.items():
                print(f"  {sid}...", file=sys.stderr, end="", flush=True)
                n = count if count is not None else config.generation.events_per_run
                total += self._run_scenario(
                    config,
                    n,
                    dry_run=dry_run,
                    redis_url=redis_url,
                    stream_key=stream_key,
                    seed=seed,
                )
                print(" done", file=sys.stderr)
            return total

        config = load_scenario(scenario_id)
        n = count if count is not None else config.generation.events_per_run
        return self._run_scenario(
            config,
            n,
            dry_run=dry_run,
            redis_url=redis_url,
            stream_key=stream_key,
            seed=seed,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_scenario(
        self,
        config: ScenarioConfig,
        count: int,
        *,
        dry_run: bool,
        redis_url: str,
        stream_key: str,
        seed: int | None,
    ) -> int:
        """Run a single scenario.

        Args:
            config: Validated scenario configuration.
            count: Number of events to generate.
            dry_run: Skip Redis + timing when True.
            redis_url: Redis connection URL.
            stream_key: Redis Streams key.
            seed: Random seed for account ID and factory RNG.

        Returns:
            Number of events emitted.
        """
        rng = random.Random(seed)
        account_ids = [
            f"acct_{rng.getrandbits(48):012x}"
            for _ in range(config.generation.user_count)
        ]
        factory = EventFactory(config, account_ids, seed=seed)

        redis_client: redis_lib.Redis | None = None
        if not dry_run:
            redis_client = redis_lib.Redis.from_url(redis_url, decode_responses=True)

        velocity = config.generation.velocity
        base_interval_s = 60.0 / velocity.events_per_minute
        burst_interval_events = max(5, int(count * _BURST_TRIGGER_FRACTION))

        # Back-date start so events look historical rather than future-dated.
        start_ts = datetime.now(UTC) - timedelta(seconds=count * base_interval_s)

        burst_end: float = 0.0
        emitted = 0

        for i in range(count):
            # Timing (skipped in dry-run)
            if not dry_run:
                in_burst = time.monotonic() < burst_end
                interval = _compute_interval(velocity, in_burst=in_burst, rng=rng)
                time.sleep(interval)

                # Trigger a new burst window at regular intervals
                if (
                    velocity.burst_duration_s
                    and i > 0
                    and i % burst_interval_events == 0
                    and time.monotonic() >= burst_end
                ):
                    burst_end = time.monotonic() + velocity.burst_duration_s

            # Build event — assign monotonically increasing timestamp
            account_id = rng.choice(account_ids)
            ts = start_ts + timedelta(seconds=i * base_interval_s)
            event = factory.build_event(account_id, ts)

            # Emit
            if dry_run:
                print(event.model_dump_json())
            else:
                assert redis_client is not None
                redis_client.xadd(stream_key, {"data": event.model_dump_json()})

            emitted += 1

        if redis_client is not None:
            redis_client.close()

        return emitted


def _compute_interval(
    velocity: VelocityConfig,
    *,
    in_burst: bool,
    rng: random.Random,
) -> float:
    """Compute the sleep interval before the next event.

    Args:
        velocity: Velocity configuration from the scenario.
        in_burst: Whether the runner is currently inside a burst window.
        rng: Seeded random instance for jitter.

    Returns:
        Sleep duration in seconds (minimum 0.001).
    """
    base = 60.0 / velocity.events_per_minute
    if in_burst and velocity.burst_factor > 1.0:
        base /= velocity.burst_factor
    jitter = base * velocity.jitter_factor * (2.0 * rng.random() - 1.0)
    return max(0.001, base + jitter)
