r"""Command line for the text mock — one decided message, and its registration file.

::

    # Classify one customer message and ask the ledger at MOCK_TEXT_LEDGER_URL
    # (with MOCK_TEXT_LEDGER_KEY; a blank URL refuses to run) to decide it;
    # print the decision: its bundle id, the label and proposal sent, the
    # final action, and the policy gate's evidence protocol
    # (extra.protocol_hold among it)
    uv run python -m mock_text_reasoner decide \
        --text "I lost my job and can't pay this month's installment."

    # Print the submission instead, and send nothing
    uv run python -m mock_text_reasoner decide --text "..." --dry-run

    # Print the registration file a ledger reads (registration.yaml)
    uv run python -m mock_text_reasoner registration-yaml

The same CLI is installed as the ``mock-text-reasoner`` console script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, get_args

import httpx
from pydantic import ValidationError

from app.client import HttpClient
from core.exceptions import LedgerError
from mock_text_reasoner.assembler import build_submission
from mock_text_reasoner.classifier import classify
from mock_text_reasoner.messages import CustomerMessage
from mock_text_reasoner.pipeline import decide_message, decision_report
from mock_text_reasoner.registry import registration_yaml

if TYPE_CHECKING:
    from app.client import LedgerClient
    from mock_text_reasoner.settings import MockTextSettings

#: The customer a message is from when ``--customer-id`` is not given.
DEFAULT_CUSTOMER_ID = "cust-demo"

#: The channels a message may arrive on.
CHANNELS: tuple[str, ...] = get_args(CustomerMessage.model_fields["channel"].annotation)


def ledger_client(settings: MockTextSettings) -> LedgerClient:
    """The ledger the mock submits to: always over HTTP.

    Args:
        settings: The mock's settings (``MOCK_TEXT_LEDGER_URL``,
            ``MOCK_TEXT_LEDGER_KEY``).

    Returns:
        An ``HttpClient`` for the ledger, carrying the mock's key.
    """
    return HttpClient(
        settings.ledger_url,
        api_key=settings.ledger_key.get_secret_value() or None,
    )


def _settings() -> MockTextSettings | None:
    """The mock's settings, or ``None`` (and why, on stderr) when invalid."""
    from mock_text_reasoner.settings import MockTextSettings

    try:
        return MockTextSettings()
    except ValidationError as err:
        problems = "; ".join(e["msg"] for e in err.errors(include_url=False))
        print(f"decide: the mock's settings are invalid: {problems}", file=sys.stderr)
        return None


def _message(args: argparse.Namespace) -> CustomerMessage | None:
    """The message the arguments describe, or ``None`` (and why) when invalid."""
    try:
        return CustomerMessage(
            event_id=args.event_id or f"msg-{uuid.uuid4().hex[:12]}",
            received_at=datetime.now(UTC),
            customer_id=args.customer_id,
            channel=args.channel,
            text=args.text,
        )
    except ValidationError as err:
        problems = "; ".join(e["msg"] for e in err.errors(include_url=False))
        print(f"decide: the message is invalid: {problems}", file=sys.stderr)
        return None


async def _decide(message: CustomerMessage, client: LedgerClient) -> int:
    try:
        receipt, _ = await decide_message(message, client=client)
        try:
            bundle = await client.get_bundle(receipt.decision_id)
        except (LedgerError, httpx.HTTPError, ValueError) as err:
            print(
                f"decide: the ledger decided {receipt.decision_id} "
                f"({receipt.decision_action.value}), but its bundle could not "
                f"be read back: {type(err).__name__}: {err}",
                file=sys.stderr,
            )
            return 1
    finally:
        await client.aclose()
    print(json.dumps(decision_report(bundle), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code: 0 when the ledger decided the message (whatever
        it decided) and its bundle was read back; 1 when the ledger refused
        it, could not be reached, or answered with something the mock cannot
        read (a decision whose bundle could not be read back is named on
        stderr); 2 for invalid arguments, an invalid message, or invalid
        settings.
    """
    parser = argparse.ArgumentParser(prog="mock_text_reasoner")
    commands = parser.add_subparsers(dest="command", required=True)

    decide = commands.add_parser(
        "decide", help="classify one message and ask the ledger to decide it"
    )
    decide.add_argument("--text", required=True, help="what the customer wrote")
    decide.add_argument(
        "--customer-id",
        default=DEFAULT_CUSTOMER_ID,
        help="who wrote it (the decision's native subject; default "
        f"{DEFAULT_CUSTOMER_ID})",
    )
    decide.add_argument("--channel", choices=CHANNELS, default="chat")
    decide.add_argument(
        "--event-id", default=None, help="the message's id (random by default)"
    )
    decide.add_argument(
        "--dry-run",
        action="store_true",
        help="print the submission as JSON and send nothing",
    )

    commands.add_parser(
        "registration-yaml", help="print the registration file a ledger reads"
    )

    args = parser.parse_args(argv)
    if args.command == "registration-yaml":
        sys.stdout.write(registration_yaml())
        return 0
    message = _message(args)
    if message is None:
        return 2
    if args.dry_run:
        submission = build_submission(message, classify(message.text))
        print(submission.model_dump_json(indent=2))
        return 0
    settings = _settings()
    if settings is None:
        return 2
    try:
        client = ledger_client(settings)
    # A key an HTTP header cannot carry, or a URL httpx cannot parse.
    except (ValueError, httpx.InvalidURL) as err:
        print(f"decide: the mock's settings are invalid: {err}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_decide(message, client))
    except (LedgerError, httpx.HTTPError, ValueError, OSError) as err:
        print(f"decide stopped: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
