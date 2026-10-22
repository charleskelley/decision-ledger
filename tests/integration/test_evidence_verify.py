"""Integration tests for evidence escrow and verify on Postgres (DR-34 part 2).

Requires PostgreSQL. Run with: make test-integration

The ledger's verify over its real store (``BundleStore`` with its
``ArtifactStore`` on the canonical schema), deciding and verifying through
``LocalClient``, with the stub reasoner as the tenant:

- The rules gate (``rules_gate.plugin.PLUGIN``'s class, over a temporary
  rule-set directory) escrows the rule set it applied: verify reports the
  reference ``intact`` with its escrow ``present``; after the rule set's file
  is edited, live verify reports ``source_changed`` while stored verify
  still proves ``intact`` from the escrow; after the file is removed, live
  verify reports ``source_unavailable``. Replay is unaffected throughout.
- A mandatory lookup gate (which does not verify) escrows the row it
  answered from: ``present`` and ``not_verifiable``, beside the scripted
  fake gate's ``intact`` — the two answers are independent.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
import yaml
from rules_gate.plugin import RulesGatePlugin
from rules_gate.settings import RulesGateSettings

from app.audit import ArtifactStore, BundleStore
from app.audit.schema import apply_canonical_schema
from app.client import LocalClient
from app.gates import ComposedGate
from app.plugin import FrameworkServices
from app.reasoner_registry import LiveReasonerRegistry
from app.settings import FrameworkSettings
from core.actions import DecisionAction
from core.observation import GateBinding, GateContext, ReasonerRegistration
from tests._fake_gate.plugin import FakeGatePlugin, FakeLookupGatePlugin
from tests._stub_reasoner.registry import (
    STUB_REGISTRATION,
    STUB_REGISTRATION_WITH_LOOKUP,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from app.gates import GatePlugin
    from core.submission import Submission

pytestmark = pytest.mark.integration

_PG_DSN = FrameworkSettings().postgres_dsn

#: The stub bound to the rules gate (advisory).
_WITH_RULES = ReasonerRegistration.model_validate_json(
    STUB_REGISTRATION.model_copy(
        update={"gates": (GateBinding(gate_id="rules"),)}
    ).model_dump_json()
)

#: A rule set for the stub: a middling widget score is challenged.
_RULESET: dict[str, Any] = {
    "ruleset_id": "stub-guardrails",
    "version": "1",
    "reasoner_id": "stub-reasoner",
    "default_action": "ALLOW",
    "rules": [
        {
            "rule_id": "middling-score-challenge",
            "when": {
                "kind": "compare",
                "field": "label_value",
                "op": "gt",
                "value": 0.5,
            },
            "action": "CHALLENGE",
        }
    ],
}


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection[Any]]:
    """An autocommit connection (as the bootstrap opens) with the DDL applied."""
    with psycopg.connect(_PG_DSN, autocommit=True) as conn:
        apply_canonical_schema(conn)
        yield conn


@pytest.fixture
def written(pg_conn: psycopg.Connection[Any]) -> Iterator[list[str]]:
    """Collects decision ids a test writes; deletes their rows on teardown.

    The escrow rows those decisions created go too (by ``first_decision_id``),
    so runs leave nothing behind.
    """
    ids: list[str] = []
    yield ids
    if ids:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM decisionledger.evidence_artifacts "
                "WHERE first_decision_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM decisionledger.replay_logs "
                "WHERE decision_id = ANY(%s::uuid[])",
                (ids,),
            )
            cur.execute(
                "DELETE FROM decisionledger.decision_bundles "
                "WHERE decision_id = ANY(%s::uuid[])",
                (ids,),
            )


def _ledger(
    conn: psycopg.Connection[Any],
    registration: ReasonerRegistration,
    plugins: list[GatePlugin],
) -> FrameworkServices:
    """Services over the real store, each gate built against them."""
    registry = LiveReasonerRegistry([registration])
    store = BundleStore(
        conn, registry=registry, artifacts=ArtifactStore(conn, enabled=True)
    )
    store.snapshots.record(registration)
    services = FrameworkServices(
        settings=FrameworkSettings(), pg_conn=conn, store=store, registry=registry
    )
    gates = {p.gate_id: ComposedGate(plugin=p, gate=p.build(services)) for p in plugins}
    return dataclasses.replace(services, gates=MappingProxyType(gates))


def _addressed(submission: Submission, *contexts: GateContext) -> Submission:
    return submission.model_copy(
        update={"event_id": f"evt-{uuid.uuid4()}", "gate_contexts": contexts}
    )


async def _decide_and_verify(
    client: LocalClient, submission: Submission, written: list[str], *lives: bool
) -> tuple[str, list[Any]]:
    receipt = await client.submit(submission)
    written.append(receipt.decision_id)
    return receipt.decision_id, [
        await client.verify(receipt.decision_id, live=live) for live in lives
    ]


def test_the_rules_gate_proves_its_rule_set_from_the_escrow(
    pg_conn, written, gated_submission, tmp_path: Path
):
    ruleset_file = tmp_path / "stub-guardrails.yaml"
    ruleset_file.write_text(yaml.safe_dump(_RULESET), encoding="utf-8")
    plugin = RulesGatePlugin(RulesGateSettings(rulesets_dir=str(tmp_path)))
    client = LocalClient(_ledger(pg_conn, _WITH_RULES, [plugin]))
    submission = _addressed(
        gated_submission,
        GateContext(gate_id="rules", gate_config={"ruleset_id": "stub-guardrails"}),
    )

    async def _run() -> None:
        decision_id, (stored, live) = await _decide_and_verify(
            client, submission, written, False, True
        )
        [ref] = stored.refs
        assert (ref.gate_id, ref.source_id, ref.locator) == (
            "rules",
            "stub-guardrails@1",
            "middling-score-challenge",
        )
        assert ref.artifact_hash == ref.content_hash
        assert (ref.escrow, ref.status) == ("present", "intact")
        assert live.refs[0].status == "intact"
        held = client.framework.store.artifacts.get(ref.artifact_hash or "")
        assert held is not None
        assert yaml.safe_load(held)["rules"][0]["action"] == "CHALLENGE"

        # The rule set's file is edited: the source changed, the escrow
        # still proves what the decision applied.
        edited = {**_RULESET, "default_action": "HOLD"}
        ruleset_file.write_text(yaml.safe_dump(edited), encoding="utf-8")
        live = await client.verify(decision_id, live=True)
        stored = await client.verify(decision_id)
        assert [r.status for r in live.refs] == ["source_changed"]
        assert [(r.escrow, r.status) for r in stored.refs] == [("present", "intact")]

        # The file is removed: the source is unavailable; the escrow holds.
        ruleset_file.unlink()
        live = await client.verify(decision_id, live=True)
        stored = await client.verify(decision_id)
        assert [r.status for r in live.refs] == ["source_unavailable"]
        assert [r.status for r in stored.refs] == ["intact"]
        assert live.summary.unavailable == 1

        # Verify is not replay: the decision replays to a match all along.
        replay = await client.replay(decision_id)
        assert replay.matched is True
        assert replay.replayed_action == DecisionAction.CHALLENGE.value

    asyncio.run(_run())


def test_a_gate_that_does_not_verify_still_has_its_escrow_checked(
    pg_conn, written, gated_submission
):
    lookup, fake = FakeLookupGatePlugin(), FakeGatePlugin()
    client = LocalClient(
        _ledger(pg_conn, STUB_REGISTRATION_WITH_LOOKUP, [lookup, fake])
    )
    submission = _addressed(
        gated_submission,
        GateContext(gate_id="fake-lookup", gate_config={}),
        GateContext(
            gate_id="fake",
            gate_config={
                "permitted_actions": ["CHALLENGE"],
                "evidence_sources": [f"doc-{uuid.uuid4()}"],
                "escrow_evidence": True,
            },
        ),
    )
    lookup.listed[submission.entity_id] = DecisionAction.HOLD

    async def _run() -> None:
        _, (stored,) = await _decide_and_verify(client, submission, written, False)
        by_gate = {ref.gate_id: ref for ref in stored.refs}
        assert (by_gate["fake-lookup"].escrow, by_gate["fake-lookup"].status) == (
            "present",
            "not_verifiable",
        )
        assert (by_gate["fake"].escrow, by_gate["fake"].status) == (
            "present",
            "intact",
        )
        assert stored.summary.escrow_missing == 0
        assert stored.summary.not_verifiable == 1

    asyncio.run(_run())


def test_escrow_reads_the_stored_artifact_by_its_digest(pg_conn):
    # The two halves meet at the digest: what a gate's reference names is
    # what the store answers ``held`` for.
    escrow = ArtifactStore(pg_conn, enabled=True)
    content = f"escrowed at {datetime.now(UTC).isoformat()}".encode()

    digest = escrow.put(content, media_type="text/plain", decision_id="d", gate_id="g")

    try:
        assert digest is not None
        assert escrow.held([digest]) == {digest}
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM decisionledger.evidence_artifacts "
                "WHERE artifact_hash = %s",
                (digest,),
            )
