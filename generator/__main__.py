"""CLI entry point for the ATO Reasoner synthetic event generator.

Usage::

    uv run python -m generator run --scenario baseline_normal --count 50
    uv run python -m generator run --scenario all
    uv run python -m generator run --scenario post_breach_ato --dry-run
    uv run python -m generator list
"""

from __future__ import annotations

import argparse
import sys

from generator.loader import load_all_scenarios
from generator.runner import GeneratorRunner


def _cmd_run(args: argparse.Namespace) -> None:
    runner = GeneratorRunner()
    emitted = runner.run(
        args.scenario,
        count=args.count,
        dry_run=args.dry_run,
        redis_url=args.redis_url,
        stream_key=args.stream_key,
    )
    if not args.dry_run:
        print(f"Emitted {emitted} events → {args.stream_key}", file=sys.stderr)


def _cmd_list(_args: argparse.Namespace) -> None:
    scenarios = load_all_scenarios()
    col = 36
    print(f"{'SCENARIO':<{col}}  EXPECTED ACTIONS")
    print("-" * (col + 28))
    for sid, config in scenarios.items():
        actions = ", ".join(a.value for a in config.expected_actions)
        print(f"{sid:<{col}}  {actions}")
        desc = config.description.strip().replace("\n", " ")
        print(f"  {desc[:80]}")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="generator",
        description="ATO Reasoner synthetic event generator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    run_p = subparsers.add_parser("run", help="Generate and emit events")
    run_p.add_argument(
        "--scenario",
        required=True,
        metavar="SCENARIO_ID",
        help="Scenario ID (e.g. baseline_normal) or 'all' to run every scenario",
    )
    run_p.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="Number of events to generate (default: scenario's events_per_run)",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON to stdout instead of emitting to Redis; skips timing delays",
    )
    run_p.add_argument(
        "--redis-url",
        default="redis://localhost:6379",
        metavar="URL",
        help="Redis connection URL (default: redis://localhost:6379)",
    )
    run_p.add_argument(
        "--stream-key",
        default="ato:login-events",
        metavar="KEY",
        help="Redis Streams key to publish events to (default: ato:login-events)",
    )

    # --- list ---
    subparsers.add_parser("list", help="List available scenarios and expected actions")

    args = parser.parse_args()

    if args.command == "run":
        _cmd_run(args)
    elif args.command == "list":
        _cmd_list(args)


if __name__ == "__main__":
    main()
