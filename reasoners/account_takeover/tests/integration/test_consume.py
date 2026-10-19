"""Integration: logins on the Redis stream, decided into a Postgres ledger.

Requires Docker Redis and Postgres. Run with: make test-integration

Event in, bundle out. The real ``IngestionConsumer`` reads a stream of its
own; ATO's real services (the Redis feature windows and the trained scorer)
score each accepted login; ``decide_from_stream`` submits it to a ledger
composed in process on Postgres (``build_framework_services`` with ATO's
plugin). A scripted policy gate stands in for the RAG policy gate, so a
login that lands between the score bands is gated without an LLM call.

- Valid logins are decided in stream order and their bundles are in the
  ledger, each about its account as ATO's native subject, with the
  generator's scenario tag stripped before scoring.
- A malformed message and a duplicate are dead-lettered by the consumer;
  nothing is left pending in the group.
- A login the ledger refuses (here: a gate config it does not accept) is
  dead-lettered as ``ledger_refused``, the run goes on, and nothing is
  written.
- An error that would refuse every login (here: no reasoner is registered)
  stops the run: that login is dead-lettered as ``pipeline_error``, the rest
  of its batch stays pending in the group, and the next run decides it
  first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from pydantic import BaseModel, ConfigDict

from app.bootstrap import build_framework_services
from app.client import LocalClient
from app.gates import GateKind
from app.settings import FrameworkSettings
from ato_reasoner.events import AuthMethod, AuthOutcome, Geolocation, LoginEvent
from ato_reasoner.ingestion.consumer import IngestionConsumer
from ato_reasoner.ingestion.stream import (
    LEDGER_REFUSED,
    PIPELINE_ERROR,
    StreamRun,
    decide_from_stream,
)
from ato_reasoner.plugin import PLUGIN, build_ato_services
from ato_reasoner.settings import AtoSettings
from core.actions import DecisionAction
from core.exceptions import LedgerError
from core.gate import GateOutput, GateVerdict

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ato_reasoner.pipeline import AtoDecision

pytestmark = pytest.mark.integration

_PG_DSN = FrameworkSettings().postgres_dsn
_GROUP = "test-consume"


class _ScriptedPolicyGate:
    """Answers every gated login with CHALLENGE; no retrieval, no LLM."""

    async def evaluate(self, request: Any) -> GateOutput:
        return GateOutput(
            gate_id="policy",
            deterministic=False,
            verdict=GateVerdict(
                gate_id="policy",
                permitted_actions=[DecisionAction.CHALLENGE],
                required_controls=["step_up_mfa"],
                confidence=0.9,
                escalate_to_human=False,
                escalation_reason=None,
            ),
        )

    def close(self) -> None:
        pass


class _ScriptedPolicyPlugin:
    gate_id = "policy"
    kind = GateKind.LLM
    deterministic = False
    config_model: type[BaseModel] | None = None

    def build(self, framework: Any) -> _ScriptedPolicyGate:
        return _ScriptedPolicyGate()


class _NoConfigAccepted(BaseModel):
    """A gate config model that accepts no key: every ATO context fails it."""

    model_config = ConfigDict(extra="forbid")


class _RefusingPolicyPlugin(_ScriptedPolicyPlugin):
    """The policy gate double, with a config model ATO's contexts fail."""

    config_model = _NoConfigAccepted


def _login(account_id: str, *, scenario_tag: str | None = None) -> LoginEvent:
    return LoginEvent(
        event_id=f"evt-consume-{uuid.uuid4()}",
        timestamp=datetime.now(UTC),
        account_id=account_id,
        session_id=f"sess-{uuid.uuid4().hex[:8]}",
        ip_address="203.0.113.7",
        geolocation=Geolocation(
            latitude=37.77,
            longitude=-122.42,
            country="US",
            city="San Francisco",
            asn="AS7922",
        ),
        device_fingerprint="fp-consume",
        user_agent="Mozilla/5.0 (Integration)",
        auth_method=AuthMethod.PASSWORD,
        outcome=AuthOutcome.SUCCESS,
        scenario_tag=scenario_tag,
    )


@pytest.fixture
def written() -> Iterator[list[str]]:
    """Decision ids a test wrote; deleted (with their logs) on teardown."""
    ids: list[str] = []
    yield ids
    if ids:
        with psycopg.connect(_PG_DSN, autocommit=True) as conn, conn.cursor() as cur:
            for table in ("replay_logs", "decision_resolution_attempts"):
                cur.execute(
                    f"DELETE FROM decisionledger.{table} "  # noqa: S608 (fixed names)
                    "WHERE decision_id = ANY(%s::uuid[])",
                    (ids,),
                )
            cur.execute(
                "DELETE FROM decisionledger.decision_bundles "
                "WHERE decision_id = ANY(%s::uuid[])",
                (ids,),
            )


@pytest.fixture
def stream_key(redis_client, unique_stream_key) -> Iterator[str]:
    """A fresh stream; it, its dead-letter stream, and the group are dropped after."""
    yield unique_stream_key
    redis_client.delete(unique_stream_key, f"{unique_stream_key}:dlq")


@pytest.fixture
def ato_services():
    services = build_ato_services(AtoSettings())
    yield services
    services.close()


def _bundle_count(event_ids: list[str]) -> int:
    with psycopg.connect(_PG_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM decisionledger.decision_bundles "
            "WHERE bundle -> 'submission' ->> 'event_id' = ANY(%s)",
            (event_ids,),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_logins_on_the_stream_are_decided_into_the_ledger(
    redis_client, stream_key, ato_services, written
):
    run_id = uuid.uuid4().hex[:8]
    logins = [
        _login(f"acct-consume-{run_id}-{n}", scenario_tag="baseline_normal")
        for n in range(3)
    ]
    for login in logins:
        redis_client.xadd(stream_key, {"data": login.model_dump_json()})
    redis_client.xadd(stream_key, {"data": '{"not": "a login"}'})
    redis_client.xadd(stream_key, {"data": logins[0].model_dump_json()})

    framework = build_framework_services(
        FrameworkSettings(), [PLUGIN], [_ScriptedPolicyPlugin()]
    )
    client = LocalClient(framework)
    decided: list[tuple[LoginEvent, AtoDecision]] = []
    consumer = IngestionConsumer(
        redis_client, stream_key=stream_key, group=_GROUP, dedup_ttl_seconds=60
    )
    try:
        run = await decide_from_stream(
            consumer,
            services=ato_services,
            client=client,
            max_events=5,
            block_ms=200,
            on_decision=lambda event, decision: decided.append((event, decision)),
        )
        written.extend(decision.receipt.decision_id for _, decision in decided)

        assert (run.read, run.decided, run.dead_lettered, run.refused) == (5, 3, 2, 0)
        assert [event.event_id for event, _ in decided] == [
            login.event_id for login in logins
        ]
        for login, (_, decision) in zip(logins, decided, strict=True):
            bundle = await client.get_bundle(decision.receipt.decision_id)
            assert bundle.submission.event_id == login.event_id
            assert bundle.submission.raw_event["scenario_tag"] is None
            assert bundle.identity is not None
            provenance = bundle.identity.provenance
            assert provenance.native_namespace == "ato-account"
            assert provenance.native_subject_id == login.account_id
            assert bundle.entity_id == login.entity_id
        assert _bundle_count([login.event_id for login in logins]) == 3
    finally:
        await client.aclose()

    reasons = sorted(
        fields["reason"] for _, fields in redis_client.xrange(f"{stream_key}:dlq")
    )
    assert reasons == ["duplicate", "schema_error"]
    assert redis_client.xpending(stream_key, _GROUP)["pending"] == 0


async def test_a_login_the_ledger_refuses_is_dead_lettered_and_the_run_goes_on(
    redis_client, stream_key, ato_services
):
    logins = [_login(f"acct-consume-refused-{uuid.uuid4().hex[:8]}") for _ in range(2)]
    for login in logins:
        redis_client.xadd(stream_key, {"data": login.model_dump_json()})
    framework = build_framework_services(
        FrameworkSettings(), [PLUGIN], [_RefusingPolicyPlugin()]
    )
    client = LocalClient(framework)
    consumer = IngestionConsumer(
        redis_client, stream_key=stream_key, group=_GROUP, dedup_ttl_seconds=60
    )
    try:
        run = await decide_from_stream(
            consumer, services=ato_services, client=client, max_events=2, block_ms=200
        )
    finally:
        await client.aclose()

    assert (run.read, run.decided, run.refused, run.failed) == (2, 0, 2, 0)
    entries = [fields for _, fields in redis_client.xrange(f"{stream_key}:dlq")]
    assert [e["reason"] for e in entries] == [LEDGER_REFUSED, LEDGER_REFUSED]
    assert [e["event_id"] for e in entries] == [login.event_id for login in logins]
    assert all(e["detail"].startswith("422 GATE_CONFIG_INVALID") for e in entries)
    assert _bundle_count([login.event_id for login in logins]) == 0
    assert redis_client.xpending(stream_key, _GROUP)["pending"] == 0


async def test_a_stopped_run_leaves_its_batch_pending_for_the_next_run(
    redis_client, stream_key, ato_services, written
):
    run_id = uuid.uuid4().hex[:8]
    logins = [_login(f"acct-consume-backlog-{run_id}-{n}") for n in range(3)]
    for login in logins:
        redis_client.xadd(stream_key, {"data": login.model_dump_json()})
    consumer = IngestionConsumer(
        redis_client, stream_key=stream_key, group=_GROUP, dedup_ttl_seconds=60
    )

    # No reasoner registered: the first login is refused like every other
    # would be, so the run stops with the rest of its batch unread.
    unregistered = LocalClient(build_framework_services(FrameworkSettings(), [], []))
    stopped = StreamRun()
    try:
        with pytest.raises(LedgerError, match="REASONER_NOT_REGISTERED"):
            await decide_from_stream(
                consumer,
                services=ato_services,
                client=unregistered,
                batch_size=3,
                block_ms=200,
                run=stopped,
            )
    finally:
        await unregistered.aclose()
    assert (stopped.read, stopped.failed) == (1, 1)
    [(_, entry)] = redis_client.xrange(f"{stream_key}:dlq")
    assert entry["reason"] == PIPELINE_ERROR
    assert entry["detail"] == "the decision failed (422 REASONER_NOT_REGISTERED)"
    assert redis_client.xpending(stream_key, _GROUP)["pending"] == 2

    framework = build_framework_services(
        FrameworkSettings(), [PLUGIN], [_ScriptedPolicyPlugin()]
    )
    client = LocalClient(framework)
    decided: list[str] = []
    try:
        run = await decide_from_stream(
            consumer,
            services=ato_services,
            client=client,
            max_events=2,
            block_ms=200,
            on_decision=lambda event, decision: (
                decided.append(event.event_id),
                written.append(decision.receipt.decision_id),
            ),
        )
    finally:
        await client.aclose()

    assert decided == [logins[1].event_id, logins[2].event_id]
    assert (run.read, run.decided) == (2, 2)
    assert redis_client.xpending(stream_key, _GROUP)["pending"] == 0
    assert _bundle_count([login.event_id for login in logins[1:]]) == 2
