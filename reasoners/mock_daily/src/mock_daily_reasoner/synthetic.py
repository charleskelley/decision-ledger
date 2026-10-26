"""A synthetic day — the daily mock's deterministic reconciliation extract.

``synthetic_day`` writes one row per account for a business date: the same
arguments always give the same rows. Each row's actual amount is drawn from
one of four profiles of drift from its target, each well inside one band of
the bypass rule and the rule set (``mock-daily-v1``), so cent rounding never
moves a row across a threshold; the drift is a shortfall or an overage at
random:

- ``in_tolerance`` (about 95 % of accounts): within 0.9 % of the target —
  recorded ALLOW (``within-tolerance``), and the rules gate permits the
  default;
- ``minor``: 1.5 % to 4.5 % — an exception the rules gate challenges
  (``drift-minor``);
- ``major``: 6 % to 18 % — an exception the rules gate holds
  (``drift-major``);
- ``severe``: 25 % to 60 % — an exception the rules gate blocks
  (``drift-severe``).

The first ``MIN_COUNT`` rows are one of each profile, in that order, so every
day of at least ``MIN_COUNT`` rows records an account and holds an exception
of every severity. Account ids are stable across days (``acct-000001`` is
the same account every day); row ids carry the run tag, so two runs never
share an ``event_id``.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time

from mock_daily_reasoner.settlements import AccountSettlement

#: Rows in a default run: one per account.
DEFAULT_COUNT = 1_000
#: The fewest rows a synthetic day has: one of each profile.
MIN_COUNT = 4
DEFAULT_SEED = 11

#: The time of day a business date's reconciliation is taken.
CUT_OFF = time(23, 59, 59)

#: Typical settlement sizes; each account's target is one, within ±20 %.
_TARGETS = (1_250.0, 4_800.0, 12_500.0, 36_000.0, 98_000.0)
_PROFILES = ("in_tolerance", "minor", "major", "severe")
_WEIGHTS = (0.95, 0.03, 0.015, 0.005)
#: Each profile's drift from the target, in percent (before its sign).
_DRIFT_PCT = {
    "in_tolerance": (0.0, 0.9),
    "minor": (1.5, 4.5),
    "major": (6.0, 18.0),
    "severe": (25.0, 60.0),
}


def synthetic_day(
    count: int = DEFAULT_COUNT,
    *,
    business_date: date,
    run_tag: str,
    seed: int = DEFAULT_SEED,
) -> list[AccountSettlement]:
    """A deterministic day of account settlements.

    Args:
        count: Rows (one per account), at least ``MIN_COUNT``.
        business_date: The day reconciled; every row's ``as_of`` is its
            ``CUT_OFF`` in UTC.
        run_tag: The prefix of this run's row ids (``set-<run_tag>-<n>``).
        seed: The random seed; the same arguments give the same rows.

    Returns:
        ``count`` rows, for accounts ``acct-000001`` onward.

    Raises:
        ValueError: ``count`` is below ``MIN_COUNT``.
    """
    if count < MIN_COUNT:
        msg = f"a synthetic day has at least {MIN_COUNT} rows, got {count}"
        raise ValueError(msg)
    rng = random.Random(seed)
    as_of = datetime.combine(business_date, CUT_OFF, tzinfo=UTC)
    rows: list[AccountSettlement] = []
    for n in range(1, count + 1):
        profile = (
            _PROFILES[n - 1]
            if n <= MIN_COUNT
            else rng.choices(_PROFILES, weights=_WEIGHTS)[0]
        )
        target = round(rng.choice(_TARGETS) * rng.uniform(0.8, 1.2), 2)
        low, high = _DRIFT_PCT[profile]
        drift_pct = rng.uniform(low, high) * rng.choice((-1.0, 1.0))
        rows.append(
            AccountSettlement(
                event_id=f"set-{run_tag}-{n:06d}",
                as_of=as_of,
                account_id=f"acct-{n:06d}",
                target_amount=target,
                actual_amount=round(target * (1.0 + drift_pct / 100.0), 2),
            )
        )
    return rows
