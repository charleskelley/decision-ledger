r"""Command line for the batch mock — a scored day, and its registration file.

::

    # Score a synthetic day of 25,000 accounts and submit it to
    # MOCK_BATCH_LEDGER_URL with MOCK_BATCH_LEDGER_KEY (a blank URL refuses to
    # run): the records in three batch requests, the exceptions one at a time
    # for the rules gate; print what the ledger answered
    uv run python -m mock_batch_reasoner run --count 25000

    # Print the batch requests' NDJSON instead, and submit nothing
    uv run python -m mock_batch_reasoner run --count 100 --dry-run

    # Print the registration file a ledger reads (registration.yaml)
    uv run python -m mock_batch_reasoner registration-yaml

``run`` first checks that the ledger serves this mock's registration (the
same ``registration_version``) and sends nothing otherwise; a run the ledger
stops part way still prints, and writes, what it answered until then.

The same CLI is installed as the ``mock-batch-reasoner`` console script.
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
from mock_batch_reasoner.batch import (
    MAX_BATCH_LINES,
    DayRunStopped,
    plan_day,
    registration_problem,
    run_day,
    score_rows,
)
from mock_batch_reasoner.registry import registration_yaml
from mock_batch_reasoner.synthetic import (
    DEFAULT_COUNT,
    DEFAULT_SEED,
    MIN_COUNT,
    synthetic_day,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.client import LedgerClient
    from mock_batch_reasoner.settings import MockBatchSettings


def ledger_client(settings: MockBatchSettings) -> LedgerClient:
    """The ledger the mock submits to: always over HTTP.

    Args:
        settings: The mock's settings (``MOCK_BATCH_LEDGER_URL``,
            ``MOCK_BATCH_LEDGER_KEY``).

    Returns:
        An ``HttpClient`` for the ledger, carrying the mock's key.
    """
    return HttpClient(
        settings.ledger_url,
        api_key=settings.ledger_key.get_secret_value() or None,
    )


def _settings() -> MockBatchSettings | None:
    """The mock's settings, or ``None`` (and why, on stderr) when invalid."""
    from mock_batch_reasoner.settings import MockBatchSettings

    try:
        return MockBatchSettings()
    except ValidationError as err:
        problems = "; ".join(e["msg"] for e in err.errors(include_url=False))
        print(f"run: the mock's settings are invalid: {problems}", file=sys.stderr)
        return None


def _dry_run(args: argparse.Namespace, run_tag: str) -> int:
    rows = synthetic_day(
        args.count, business_date=args.date, run_tag=run_tag, seed=args.seed
    )
    plan = plan_day(
        score_rows(rows),
        chunk_size=args.chunk_size,
        provoke_record_only=not args.no_provoke,
    )
    for chunk in plan.chunks:
        for submission in chunk:
            sys.stdout.write(submission.model_dump_json() + "\n")
    sizes = ", ".join(str(len(chunk)) for chunk in plan.chunks)
    lines = sum(len(chunk) for chunk in plan.chunks)
    print(
        f"dry run: {len(rows)} rows; {lines} batch lines in {len(plan.chunks)} "
        f"request(s) ({sizes}); {len(plan.exceptions)} exception(s) to submit "
        f"on their own; provoked: {plan.provoked}",
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
            run = await run_day(
                rows,
                client,
                chunk_size=args.chunk_size,
                provoke_record_only=not args.no_provoke,
            )
        except DayRunStopped as stopped:
            run = stopped.run
    finally:
        await client.aclose()
    report = {
        "run_tag": run_tag,
        "business_date": args.date.isoformat(),
        "seed": args.seed,
        "provoked": run.provoked,
        **run.summary(),
    }
    print(json.dumps(report, indent=2))
    if args.results_out is not None:
        args.results_out.write_text(
            "".join(json.dumps(record) + "\n" for record in run.records()),
            encoding="utf-8",
        )
    if run.stopped is not None:
        print(
            f"run stopped: {run.stopped} (run {run_tag}: the report lists what "
            "the ledger answered before it stopped)",
            file=sys.stderr,
        )
        return 1
    if run.undecided:
        print(
            f"run: the ledger did not decide {run.undecided} of {run.rows} rows "
            "(see the report's rejections and refusals)",
            file=sys.stderr,
        )
        return 1
    return 0


def _at_least(minimum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        number = int(value)
        if number < minimum:
            msg = f"{value} is below {minimum}"
            raise argparse.ArgumentTypeError(msg)
        return number

    return parse


def _chunk_size(value: str) -> int:
    number = int(value)
    if not 1 <= number <= MAX_BATCH_LINES:
        msg = f"{value} is not from 1 to {MAX_BATCH_LINES}"
        raise argparse.ArgumentTypeError(msg)
    return number


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code: 0 when the ledger decided every row; 1 when
        it left one undecided, when the run stopped part way (the report
        still lists what the ledger answered), or when the ledger does not
        serve this mock's registration (nothing is sent); 2 for invalid
        arguments or settings.
    """
    parser = argparse.ArgumentParser(prog="mock_batch_reasoner")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser(
        "run", help="score a synthetic day and submit it to the ledger"
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
        help="the business date reviewed (default: yesterday, UTC)",
    )
    run.add_argument(
        "--run-tag", default=None, help="row id prefix of this run (random by default)"
    )
    run.add_argument(
        "--chunk-size",
        type=_chunk_size,
        default=MAX_BATCH_LINES,
        help=f"lines per batch request (at most {MAX_BATCH_LINES}, the ledger's limit)",
    )
    run.add_argument(
        "--no-provoke",
        action="store_true",
        help="do not send the first exception in a batch request (by default "
        "the ledger rejects it with BATCH_RECORD_ONLY, and it is submitted on "
        "its own)",
    )
    run.add_argument(
        "--results-out",
        type=Path,
        default=None,
        help="write every batch line's result and every exception's outcome "
        "here, as NDJSON",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the batch requests' NDJSON and submit nothing",
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
    settings = _settings()
    if settings is None:
        return 2
    try:
        client = ledger_client(settings)
    except ValueError as err:  # a key an HTTP header cannot carry
        print(f"run: the mock's settings are invalid: {err}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(args, client))
    except (LedgerError, httpx.HTTPError, OSError) as err:
        print(f"run stopped: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
