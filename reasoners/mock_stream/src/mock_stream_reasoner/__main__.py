r"""Command line for the stream mock — its service, a replayed stream, its file.

::

    # Run the mock's service: it submits to MOCK_STREAM_LEDGER_URL with
    # MOCK_STREAM_LEDGER_KEY (a blank URL refuses to start)
    uv run python -m mock_stream_reasoner serve --port 8002

    # Replay a synthetic stream of 200 events into the running service and
    # print what the ledger decided (--dry-run prints the stream instead)
    uv run python -m mock_stream_reasoner replay-events \
        --service-url http://localhost:8002 --count 200

    # Print the registration file a ledger reads (registration.yaml)
    uv run python -m mock_stream_reasoner registration-yaml

The same CLI is installed as the ``mock-stream-reasoner`` console script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from mock_stream_reasoner.registry import registration_yaml
from mock_stream_reasoner.replay import (
    DEFAULT_COUNT,
    DEFAULT_SEED,
    MIN_COUNT,
    replay_events,
    synthetic_stream,
)

#: The port ``serve`` listens on by default (the ledger's is 8000, ATO's 8001).
DEFAULT_PORT = 8002
DEFAULT_SERVICE_URL = f"http://localhost:{DEFAULT_PORT}"


def _serve(args: argparse.Namespace) -> int:
    from mock_stream_reasoner.settings import MockStreamSettings

    try:
        MockStreamSettings()
    except ValidationError as err:
        problems = "; ".join(e["msg"] for e in err.errors(include_url=False))
        print(f"serve: the mock's settings are invalid: {problems}", file=sys.stderr)
        return 2
    import uvicorn

    uvicorn.run(
        "mock_stream_reasoner.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )
    return 0


def _read_ndjson(path: Path) -> list[Any]:
    payloads: list[Any] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError as err:
            msg = f"{path}:{number} is not JSON: {err}"
            raise ValueError(msg) from err
    return payloads


async def _stream_start(
    client: httpx.AsyncClient, *, required: bool
) -> datetime | None:
    """Just after the service's watermark, so nothing replayed arrives late.

    Args:
        client: The service.
        required: Whether the service must answer (it must when the stream
            is posted to it; a dry run that cannot reach it starts now).

    Returns:
        A minute after the watermark, or ``None`` (start now) when the
        service holds none yet, or does not answer a dry run.
    """
    try:
        stats = (await client.get("/api/v1/stream/stats")).raise_for_status().json()
    except httpx.HTTPError:
        if required:
            raise
        return None
    watermark = stats.get("watermark")
    if not watermark:
        return None
    return datetime.fromisoformat(watermark) + timedelta(minutes=1)


def summarize(run_tag: str | None, summary: Any, payloads: list[Any]) -> dict[str, Any]:
    """The replay's report: what was published, decided, and dead-lettered.

    Args:
        run_tag: The synthetic stream's run tag (``None`` for a file).
        summary: The ``ReplaySummary``.
        payloads: The stream that was posted.

    Returns:
        A JSON-shaped report.
    """
    decisions: list[dict[str, Any]] = summary.decisions
    return {
        "run_tag": run_tag,
        "published": len(payloads),
        "outcomes": summary.outcomes,
        "decided": len(decisions),
        "decisions_missing": summary.missing,
        "modes": dict(sorted(Counter(d["mode"] for d in decisions).items())),
        "actions": dict(
            sorted(Counter(d["decision_action"] for d in decisions).items())
        ),
        "decision_level_rules": dict(
            sorted(
                Counter(
                    d["decision_level_rule"]
                    for d in decisions
                    if d["decision_level_rule"] is not None
                ).items()
            )
        ),
        "gated": [d for d in decisions if d["mode"] == "gate"],
        "recorded_sample": [d for d in decisions if d["mode"] == "record"][:3],
        "dead_letters": [
            {key: letter[key] for key in ("reason", "event_id", "detail")}
            for letter in summary.dead_letters
        ],
    }


async def _replay(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(
        base_url=args.service_url.rstrip("/"), timeout=60.0
    ) as client:
        run_tag = None
        if args.file is not None:
            payloads = _read_ndjson(args.file)
        else:
            start = args.start
            if start is None:
                start = await _stream_start(client, required=not args.dry_run)
            run_tag = args.run_tag or uuid.uuid4().hex[:6]
            payloads = synthetic_stream(
                args.count, seed=args.seed, start=start, run_tag=run_tag
            )
        if args.dry_run:
            for payload in payloads:
                print(json.dumps(payload))
            return 0
        summary = await replay_events(
            payloads, client=client, chunk=args.chunk, timeout=args.timeout
        )
    if args.decisions_out is not None:
        args.decisions_out.write_text(
            "".join(json.dumps(d) + "\n" for d in summary.decisions), encoding="utf-8"
        )
    if summary.missing:
        print(
            f"replay-events: {summary.missing} of this run's decisions are no longer "
            "listed by the service (it keeps its last MOCK_STREAM_HISTORY); the "
            "ledger holds every bundle",
            file=sys.stderr,
        )
    print(json.dumps(summarize(run_tag, summary, payloads), indent=2))
    return 0


def _aware(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        msg = f"{value!r} has no time zone (for example 2026-10-01T09:00:00+00:00)"
        raise argparse.ArgumentTypeError(msg)
    return moment


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        msg = f"{value} is not a positive integer"
        raise argparse.ArgumentTypeError(msg)
    return number


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(prog="mock_stream_reasoner")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the mock's service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)

    replay = commands.add_parser(
        "replay-events", help="replay a stream into the running service"
    )
    replay.add_argument("--service-url", default=DEFAULT_SERVICE_URL)
    replay.add_argument(
        "--count",
        type=_positive,
        default=DEFAULT_COUNT,
        help=f"payloads of the synthetic stream, faults included (at least "
        f"{MIN_COUNT}; the report lists what the service still keeps, its last "
        "MOCK_STREAM_HISTORY decisions)",
    )
    replay.add_argument("--seed", type=int, default=DEFAULT_SEED)
    replay.add_argument(
        "--run-tag", default=None, help="id prefix of this run (random by default)"
    )
    replay.add_argument(
        "--start",
        type=_aware,
        default=None,
        help="event time of the first transfer (default: a minute after the "
        "service's watermark; now when it holds none, or when a dry run cannot "
        "reach it)",
    )
    replay.add_argument(
        "--file",
        type=Path,
        default=None,
        help="replay this NDJSON stream instead of a synthetic one",
    )
    replay.add_argument("--chunk", type=_positive, default=50)
    replay.add_argument("--timeout", type=float, default=600.0)
    replay.add_argument(
        "--decisions-out",
        type=Path,
        default=None,
        help="write the run's decisions the service still lists here, as NDJSON",
    )
    replay.add_argument(
        "--dry-run",
        action="store_true",
        help="print the stream as NDJSON and post nothing",
    )

    commands.add_parser(
        "registration-yaml", help="print the registration file a ledger reads"
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "registration-yaml":
        sys.stdout.write(registration_yaml())
        return 0
    try:
        return asyncio.run(_replay(args))
    except (httpx.HTTPError, TimeoutError, ValueError, OSError) as err:
        print(f"replay-events stopped: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
