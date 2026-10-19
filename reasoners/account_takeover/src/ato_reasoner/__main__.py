r"""Command line for the ATO reasoner — decide logins from the event stream.

``consume`` reads ``ato:login-events`` through the ``ato-reasoner`` consumer
group and decides every accepted login through ``run_ato_decision`` (the
pipeline ATO's service route runs; see ``ato_reasoner.ingestion.stream``).
It submits to the ledger ATO's settings name: ``ATO_LEDGER_URL`` with
``ATO_LEDGER_KEY`` over HTTP, or, with no URL, a ledger composed in this
process from the deployment's settings::

    # Publish ten scenario logins, then decide them
    uv run python -m ato_reasoner.generator run --scenario baseline_normal --count 10
    uv run python -m ato_reasoner consume --max-events 10

    # Decide until interrupted (Ctrl-C finishes the messages already read;
    # a second Ctrl-C stops at once)
    ATO_LEDGER_URL=http://localhost:8000 ATO_LEDGER_KEY="$ATO_KEY" \
        uv run python -m ato_reasoner consume

Each decision prints one JSON line (the event, the decision, its mode and
action, and ATO's step timings); the run's counts go to stderr. The exit
status is 1 when the run stops on an error: a decision that failed other
than by a refusal of its login (that message is dead-lettered as
``pipeline_error``; the rest of its batch is decided first by the next run),
or Redis failing (nothing can be dead-lettered then).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
from typing import TYPE_CHECKING

import redis

from app.monitoring import configure_logging
from app.settings import FrameworkSettings
from ato_reasoner.app import ledger_client
from ato_reasoner.ingestion.consumer import (
    DEFAULT_CONSUMER,
    DEFAULT_GROUP,
    DEFAULT_LATENESS_SECONDS,
    DEFAULT_STREAM_KEY,
    IngestionConsumer,
)
from ato_reasoner.ingestion.stream import StreamRun, decide_from_stream
from ato_reasoner.plugin import build_ato_services
from ato_reasoner.settings import AtoSettings

if TYPE_CHECKING:
    from ato_reasoner.events import LoginEvent
    from ato_reasoner.pipeline import AtoDecision


def _print_decision(event: LoginEvent, decision: AtoDecision) -> None:
    """One JSON line per decided login, on stdout."""
    receipt = decision.receipt
    line = {
        "event_id": event.event_id,
        "account_id": event.account_id,
        "decision_id": receipt.decision_id,
        "mode": receipt.mode,
        "bypass_rule_id": receipt.bypass_rule_id,
        "decision_action": receipt.decision_action.value,
        "latency_ms": decision.latency_ms,
    }
    print(json.dumps(line), flush=True)


def _stop_on_signals(stop: asyncio.Event) -> None:
    """Set ``stop`` on the first SIGINT or SIGTERM; a second one interrupts."""
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)

    def _request_stop() -> None:
        stop.set()
        print("stopping after the messages already read", file=sys.stderr)
        for sig in signals:
            loop.remove_signal_handler(sig)

    for sig in signals:
        with contextlib.suppress(NotImplementedError):  # no signal handlers
            loop.add_signal_handler(sig, _request_stop)


async def _consume(args: argparse.Namespace) -> int:
    settings = AtoSettings()
    framework = FrameworkSettings()
    configure_logging(json_logs=framework.log_json, level=framework.log_level)
    run = StreamRun()
    # Closed in reverse order on every path, each even if another close raises.
    async with contextlib.AsyncExitStack() as resources:
        services = build_ato_services(settings)
        resources.callback(services.close)
        stream = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        resources.callback(stream.close)
        client = ledger_client(settings)
        resources.push_async_callback(client.aclose)
        consumer = IngestionConsumer(
            stream,
            stream_key=args.stream_key,
            group=args.group,
            consumer=args.consumer,
            lateness_seconds=args.lateness_seconds,
        )
        resources.callback(_print_counts, run)
        stop = asyncio.Event()
        _stop_on_signals(stop)
        try:
            await decide_from_stream(
                consumer,
                services=services,
                client=client,
                max_events=args.max_events,
                batch_size=args.batch_size,
                block_ms=args.block_ms,
                stop=stop,
                run=run,
                on_decision=_print_decision,
            )
        except Exception as err:
            if run.failed:
                # A decision's error can be the ledger client's: its type only.
                reason = f"a decision failed ({type(err).__name__})"
            else:
                reason = f"{type(err).__name__}: {err}"
            print(f"consume stopped: {reason}", file=sys.stderr)
            return 1
    return 0


def _print_counts(run: StreamRun) -> None:
    """The run's counts as one JSON line, on stderr."""
    counts = {
        "read": run.read,
        "decided": run.decided,
        "dead_lettered": run.dead_lettered,
        "refused": run.refused,
        "failed": run.failed,
        "actions": dict(sorted(run.actions.items())),
    }
    print(json.dumps(counts), file=sys.stderr)


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
    parser = argparse.ArgumentParser(prog="ato_reasoner")
    commands = parser.add_subparsers(dest="command", required=True)

    consume = commands.add_parser(
        "consume", help="decide the login-event stream's logins through the ledger"
    )
    consume.add_argument(
        "--max-events",
        type=_positive,
        default=None,
        metavar="N",
        help="stop after N stream messages (default: run until interrupted)",
    )
    consume.add_argument("--stream-key", default=DEFAULT_STREAM_KEY, metavar="KEY")
    consume.add_argument("--group", default=DEFAULT_GROUP)
    consume.add_argument(
        "--consumer",
        default=DEFAULT_CONSUMER,
        help="this instance's name in the group (unique per running consumer)",
    )
    consume.add_argument("--batch-size", type=_positive, default=10, metavar="N")
    consume.add_argument("--block-ms", type=_positive, default=1_000, metavar="MS")
    consume.add_argument(
        "--lateness-seconds",
        type=_positive,
        default=DEFAULT_LATENESS_SECONDS,
        metavar="S",
        help="dead-letter events older than this (default: one hour)",
    )

    args = parser.parse_args(argv)
    return asyncio.run(_consume(args))


if __name__ == "__main__":
    sys.exit(main())
