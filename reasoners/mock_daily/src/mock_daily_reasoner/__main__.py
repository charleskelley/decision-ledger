r"""Command line for the daily mock — a reconciled day, and its registration file.

::

    # Reconcile a synthetic day of 1,000 accounts and submit it to
    # MOCK_DAILY_LEDGER_URL with MOCK_DAILY_LEDGER_KEY (a blank URL refuses to
    # run), one decision per request: the accounts within tolerance recorded,
    # the exceptions gated; print what the ledger answered
    uv run python -m mock_daily_reasoner run --count 1000

    # Print the submissions as NDJSON instead, and submit nothing
    uv run python -m mock_daily_reasoner run --count 100 --dry-run

    # Print the registration file a ledger reads (registration.yaml)
    uv run python -m mock_daily_reasoner registration-yaml

``run`` first checks that ``--results-out``, when given, can be written,
and that the ledger serves this mock's registration (the same
``registration_version``), and sends nothing otherwise; a run the ledger
stops part way still prints, and writes, what it answered until then, and
names the row it sent without an answer, if any (``in_doubt``).

The same CLI is installed as the ``mock-daily-reasoner`` console script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError

from app.client import HttpClient
from core.exceptions import LedgerError
from mock_daily_reasoner.daily import (
    DayRunStopped,
    compute_day,
    registration_problem,
    run_day,
)
from mock_daily_reasoner.registry import registration_yaml
from mock_daily_reasoner.synthetic import (
    DEFAULT_COUNT,
    DEFAULT_SEED,
    MIN_COUNT,
    synthetic_day,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.client import LedgerClient
    from mock_daily_reasoner.settings import MockDailySettings


def ledger_client(settings: MockDailySettings) -> LedgerClient:
    """The ledger the mock submits to: always over HTTP.

    Args:
        settings: The mock's settings (``MOCK_DAILY_LEDGER_URL``,
            ``MOCK_DAILY_LEDGER_KEY``).

    Returns:
        An ``HttpClient`` for the ledger, carrying the mock's key.
    """
    return HttpClient(
        settings.ledger_url,
        api_key=settings.ledger_key.get_secret_value() or None,
    )


def _settings() -> MockDailySettings | None:
    """The mock's settings, or ``None`` (and why, on stderr) when invalid."""
    from mock_daily_reasoner.settings import MockDailySettings

    try:
        return MockDailySettings()
    except ValidationError as err:
        problems = "; ".join(e["msg"] for e in err.errors(include_url=False))
        print(f"run: the mock's settings are invalid: {problems}", file=sys.stderr)
        return None


def _dry_run(args: argparse.Namespace, run_tag: str) -> int:
    rows = synthetic_day(
        args.count, business_date=args.date, run_tag=run_tag, seed=args.seed
    )
    computed = compute_day(rows)
    for item in computed:
        sys.stdout.write(item.submission.model_dump_json() + "\n")
    exceptions = sum(1 for item in computed if item.is_exception)
    print(
        f"dry run: {len(rows)} rows; {len(rows) - exceptions} recorded within "
        f"tolerance, {exceptions} exception(s) gated; one request per row",
        file=sys.stderr,
    )
    return 0


async def _run(args: argparse.Namespace, client: LedgerClient) -> int:
    run_tag = args.run_tag or uuid.uuid4().hex[:8]
    rows = synthetic_day(
        args.count, business_date=args.date, run_tag=run_tag, seed=args.seed
    )
    try:
        problem = await registration_problem(client)
        if problem is not None:
            print(f"run stopped before sending anything: {problem}", file=sys.stderr)
            return 1
        try:
            run = await run_day(rows, client)
        except DayRunStopped as stopped:
            run = stopped.run
    finally:
        await client.aclose()
    report = {
        "run_tag": run_tag,
        "business_date": args.date.isoformat(),
        "seed": args.seed,
        **run.summary(),
    }
    print(json.dumps(report, indent=2))
    if args.results_out is not None:
        args.results_out.write_text(
            "".join(json.dumps(record) + "\n" for record in run.records()),
            encoding="utf-8",
        )
    if run.stopped is not None:
        doubt = (
            f"; row {run.in_doubt} was sent and not answered, so the ledger may "
            "have decided it: look it up before sending it again"
            if run.in_doubt is not None
            else ""
        )
        print(
            f"run stopped: {run.stopped} (run {run_tag}: the report lists what "
            f"the ledger answered before it stopped{doubt})",
            file=sys.stderr,
        )
        return 1
    if run.undecided:
        print(
            f"run: the ledger did not decide {run.undecided} of {run.rows} rows "
            "(see the report's refusals)",
            file=sys.stderr,
        )
        return 1
    failures = [d for d in run.decisions if d.failure is not None]
    if failures:
        first = failures[0]
        hint = (
            ": stage the rule set (make prepare-rulesets) and export "
            "RULES_GATE_RULESETS_DIR before the ledger starts"
            if first.failure == "unknown_ruleset"
            else ""
        )
        print(
            f"run: the rules gate gave no verdict on {len(failures)} of "
            f"{run.decided} decisions, and the ledger held them (the first, "
            f"{first.event_id}: {first.failure}){hint}",
            file=sys.stderr,
        )
        return 1
    return 0


def _unwritable(path: Path) -> str | None:
    """Why ``path`` cannot be written, or ``None`` (it is created if missing)."""
    try:
        with path.open("a", encoding="utf-8"):
            pass
    except OSError as err:
        return str(err)
    return None


def _at_least(minimum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        number = int(value)
        if number < minimum:
            msg = f"{value} is below {minimum}"
            raise argparse.ArgumentTypeError(msg)
        return number

    return parse


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code: 0 when the ledger decided every row and the
        rules gate gave a verdict on each; 1 when it left a row undecided,
        when the rules gate gave no verdict on a decision (the ledger held
        it), when the run stopped part way (the report still lists what the
        ledger answered), or when the ledger does not serve this mock's
        registration (nothing is sent); 2 for invalid arguments (a
        ``--results-out`` that cannot be written included) or invalid
        settings (a URL httpx cannot parse, and a key an HTTP header cannot
        carry, included).
    """
    parser = argparse.ArgumentParser(prog="mock_daily_reasoner")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser(
        "run", help="reconcile a synthetic day and submit it to the ledger"
    )
    run.add_argument(
        "--count",
        type=_at_least(MIN_COUNT),
        default=DEFAULT_COUNT,
        help=f"rows, one per account (at least {MIN_COUNT})",
    )
    run.add_argument("--seed", type=int, default=DEFAULT_SEED)
    run.add_argument(
        "--date",
        type=date.fromisoformat,
        default=datetime.now(UTC).date() - timedelta(days=1),
        help="the business date reconciled (default: yesterday, UTC)",
    )
    run.add_argument(
        "--run-tag", default=None, help="row id prefix of this run (random by default)"
    )
    run.add_argument(
        "--results-out",
        type=Path,
        default=None,
        help="write every decision's outcome and every refusal here, as NDJSON",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the submissions as NDJSON and submit nothing",
    )

    commands.add_parser(
        "registration-yaml", help="print the registration file a ledger reads"
    )

    args = parser.parse_args(argv)
    if args.command == "registration-yaml":
        sys.stdout.write(registration_yaml())
        return 0
    if args.dry_run:
        return _dry_run(args, args.run_tag or uuid.uuid4().hex[:8])
    if args.results_out is not None:
        problem = _unwritable(args.results_out)
        if problem is not None:
            print(
                f"run: --results-out cannot be written ({problem}); nothing was sent",
                file=sys.stderr,
            )
            return 2
    settings = _settings()
    if settings is None:
        return 2
    try:
        client = ledger_client(settings)
    # A key an HTTP header cannot carry, or a URL httpx cannot parse.
    except (ValueError, httpx.InvalidURL) as err:
        print(f"run: the mock's settings are invalid: {err}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(args, client))
    except (LedgerError, httpx.HTTPError, OSError) as err:
        print(f"run stopped: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
