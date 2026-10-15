r"""Command line for the example reasoner — sample submissions and live submits.

Prints the example's wire submissions, so a ledger can be exercised with
nothing but ``curl``, and submits widgets to a ledger through a client::

    # One submission (low band → a record citing low-score; mid → a gate
    # request for the policy gate; high → a record citing high-score)
    uv run python -m example_reasoner sample --band mid > widget.json

    # An NDJSON batch of recorded decisions for POST /api/v1/decisions:batch
    uv run python -m example_reasoner batch --count 1000 > widgets.ndjson

    # Score and submit one widget to a ledger service (or --in-process)
    uv run python -m example_reasoner submit --band low \
        --ledger-url http://localhost:8000 --key "$EXAMPLE_KEY"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

from app.client import HttpClient, LedgerClient, LocalClient
from core.exceptions import LedgerError
from example_reasoner.assembler import build_submission
from example_reasoner.events import WidgetEvent
from example_reasoner.pipeline import decide_widget
from example_reasoner.scorer import score_widget

#: Measurements that land a widget in each band (see ``scorer``).
BANDS: dict[str, tuple[float, float]] = {
    "low": (0.05, 0.2),  # widget_score ≈ 0.06 → low-score → ALLOW
    "mid": (0.5, 2.0),  # widget_score = 0.50 → the policy gate
    "high": (0.95, 6.0),  # widget_score ≈ 0.98 → high-score → BLOCK
}


def sample_event(
    band: str,
    *,
    widget_id: str | None = None,
    region: str = "REGION_A",
    prior_inspections: int = 25,
) -> WidgetEvent:
    """An inspection whose score lands in ``band``.

    Args:
        band: ``low``, ``mid``, or ``high``.
        widget_id: The widget (a fresh one when omitted).
        region: ``REGION_A`` or ``REGION_B``.
        prior_inspections: Below 10 is a thin history (Tier 3 when gated).

    Returns:
        A new inspection with a fresh ``event_id``.
    """
    wobble, misalignment = BANDS[band]
    return WidgetEvent.model_validate(
        {
            "event_id": f"evt-example-{uuid.uuid4()}",
            "timestamp": datetime.now(UTC),
            "widget_id": widget_id or f"widget-{uuid.uuid4().hex[:12]}",
            "region": region,
            "wobble": wobble,
            "misalignment_mm": misalignment,
            "prior_inspections": prior_inspections,
        }
    )


def _client(args: argparse.Namespace) -> LedgerClient:
    if args.in_process:
        return LocalClient.from_settings()
    return HttpClient(args.ledger_url, api_key=args.key)


async def _submit(args: argparse.Namespace) -> int:
    client = _client(args)
    try:
        receipt = await decide_widget(sample_event(args.band), client=client)
    except LedgerError as err:
        print(err.problem(), file=sys.stderr)
        return 1
    finally:
        await client.aclose()
    print(receipt.model_dump_json(indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(prog="example_reasoner")
    commands = parser.add_subparsers(dest="command", required=True)

    sample = commands.add_parser("sample", help="print one wire submission")
    sample.add_argument("--band", choices=sorted(BANDS), default="low")
    sample.add_argument("--widget-id", default=None)
    sample.add_argument(
        "--region", choices=["REGION_A", "REGION_B"], default="REGION_A"
    )
    sample.add_argument("--prior-inspections", type=int, default=25)

    batch = commands.add_parser("batch", help="print an NDJSON batch of records")
    batch.add_argument("--count", type=int, default=1000)
    batch.add_argument(
        "--widgets", type=int, default=50, help="distinct widgets the batch covers"
    )

    submit = commands.add_parser("submit", help="score and submit one widget")
    submit.add_argument("--band", choices=sorted(BANDS), default="low")
    target = submit.add_mutually_exclusive_group(required=True)
    target.add_argument("--ledger-url")
    target.add_argument("--in-process", action="store_true")
    submit.add_argument("--key", default=None, help="the example's API key")

    args = parser.parse_args(argv)
    if args.command == "sample":
        event = sample_event(
            args.band,
            widget_id=args.widget_id,
            region=args.region,
            prior_inspections=args.prior_inspections,
        )
        print(build_submission(event, score_widget(event)).model_dump_json())
        return 0
    if args.command == "batch":
        widgets = [f"widget-batch-{n:04d}" for n in range(max(args.widgets, 1))]
        for n in range(args.count):
            band = "high" if n % 10 == 9 else "low"
            event = sample_event(band, widget_id=widgets[n % len(widgets)])
            print(build_submission(event, score_widget(event)).model_dump_json())
        return 0
    return asyncio.run(_submit(args))


if __name__ == "__main__":
    sys.exit(main())
