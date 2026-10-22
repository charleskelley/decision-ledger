"""The rules gate escrows the rule set it applies and verifies it (DR-34 part 2).

The gate is built the way the plugin builds it — the deployment's rule-set
files, each with its origin, and an evidence escrow (a dict here; the
framework's store in a deployment) — so a decision's references can be
proven later: stored, from the escrowed canonical JSON; live, by reading the
rule set's file again. What is pinned:

- Escrow: every reference of a decided output carries the digest of the
  applied rule set's canonical JSON and the digest the escrow returned for
  it; with escrow off, or no escrow, it names no artifact; an output with no
  verdict escrows nothing.
- Stored verify: ``intact`` from the escrow; ``not_verifiable`` with nothing
  escrowed, the escrow lost, a reference that is not the snapshot's rule
  set, or a snapshot without it.
- Live verify: ``intact`` while the file holds the same rules (whatever its
  layout); ``source_changed`` when it holds others; ``source_unavailable``
  when it is gone, not YAML, not a rule set, or unknown to the gate.
- The plugin hands the gate the framework store's escrow and each rule
  set's file.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import yaml

from core.gate import EvidenceRef
from rules_gate import (
    RulesGate,
    RulesGatePlugin,
    RulesGateSettings,
    canonical_ruleset,
    load_rulesets,
    parse_ruleset,
)

if TYPE_CHECKING:
    from pathlib import Path

    from core.gate import GateOutput


class _Escrow:
    """The ledger's evidence escrow, in a dict."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.rows: dict[str, bytes] = {}
        self.puts: list[tuple[str, str, str]] = []

    def put(
        self, content: bytes, *, media_type: str, decision_id: str, gate_id: str
    ) -> str | None:
        self.puts.append((media_type, decision_id, gate_id))
        if not self.enabled:
            return None
        digest = hashlib.sha256(content).hexdigest()
        self.rows.setdefault(digest, content)
        return digest

    def get(self, artifact_hash: str) -> bytes | None:
        return self.rows.get(artifact_hash)


def _built(directory: Path, escrow: Any) -> RulesGate:
    """The gate as the plugin builds it over ``directory``."""
    loaded = load_rulesets(directory)
    return RulesGate(
        {rid: entry.ruleset for rid, entry in loaded.items()},
        origins={rid: directory / entry.origin for rid, entry in loaded.items()},
        escrow=escrow,
    )


def _decided(gate: RulesGate, submission_for, request_for, **kw: Any) -> GateOutput:
    return asyncio.run(gate.evaluate(request_for(submission_for(**kw))))


def _verify(gate: RulesGate, output: GateOutput, *, live: bool) -> tuple[str, ...]:
    return asyncio.run(gate.verify(output, live=live))


@pytest.fixture
def escrow() -> _Escrow:
    return _Escrow()


@pytest.fixture
def decided(write_ruleset, escrow, submission_for, request_for) -> dict[str, Any]:
    """A gate over one rule-set file, and an output that matched two rules."""
    directory = write_ruleset()
    gate = _built(directory, escrow)
    output = _decided(
        gate, submission_for, request_for, thin_history=True, label_value=0.95
    )
    return {"gate": gate, "output": output, "file": directory / "guardrails.yaml"}


# ---------------------------------------------------------------------------
# Escrow
# ---------------------------------------------------------------------------


def test_the_applied_rule_set_is_escrowed_and_cited_by_digest(
    decided, escrow, ruleset_data
):
    applied = canonical_ruleset(parse_ruleset(ruleset_data))
    digest = hashlib.sha256(applied).hexdigest()

    output = decided["output"]

    assert [ref.locator for ref in output.evidence] == [
        "thin-history-hold",
        "extreme-score-block",
    ]
    assert {(ref.content_hash, ref.artifact_hash) for ref in output.evidence} == {
        (digest, digest)
    }
    assert escrow.rows == {digest: applied}
    assert escrow.puts == [("application/json", "dec-rules-001", "rules")]
    # The escrowed bytes are the rule set the snapshot records.
    snapshot = output.evidence_snapshot
    assert isinstance(snapshot, dict)
    assert yaml.safe_load(applied) == snapshot["ruleset"]


@pytest.mark.parametrize("escrow_of", [lambda: None, lambda: _Escrow(enabled=False)])
def test_without_escrow_references_name_no_artifact(
    write_ruleset, submission_for, request_for, escrow_of
):
    gate = _built(write_ruleset(), escrow_of())

    output = _decided(gate, submission_for, request_for)

    [ref] = output.evidence
    assert ref.content_hash is not None
    assert ref.artifact_hash is None


def test_an_output_without_a_verdict_escrows_nothing(
    write_ruleset, escrow, submission_for, request_for
):
    gate = _built(write_ruleset(), escrow)

    output = _decided(gate, submission_for, request_for, ruleset_id="unknown")

    assert output.verdict is None
    assert output.evidence == ()
    assert escrow.rows == {}
    assert _verify(gate, output, live=True) == ()


# ---------------------------------------------------------------------------
# Verify, stored
# ---------------------------------------------------------------------------


def test_stored_verify_proves_the_rules_from_the_escrow_after_the_file_goes(
    decided,
):
    decided["file"].unlink()

    assert _verify(decided["gate"], decided["output"], live=False) == (
        "intact",
        "intact",
    )


def test_stored_verify_cannot_prove_rules_it_did_not_escrow(
    decided, escrow, write_ruleset, submission_for, request_for
):
    lost_output = decided["output"]
    escrow.rows.clear()
    unescrowed = _decided(
        _built(write_ruleset(), None), submission_for, request_for, thin_history=True
    )

    assert _verify(decided["gate"], lost_output, live=False) == ("not_verifiable",) * 2
    assert _verify(decided["gate"], unescrowed, live=False) == ("not_verifiable",)


def test_a_reference_that_is_not_the_snapshots_rule_set_is_not_verifiable(decided):
    output = decided["output"]
    first, second = output.evidence
    forged = output.model_copy(
        update={
            "evidence": (
                first.model_copy(update={"content_hash": "0" * 64}),
                second,
                EvidenceRef(source_id="other@1", content_hash=None),
            )
        }
    )

    for live in (False, True):
        assert _verify(decided["gate"], forged, live=live) == (
            "not_verifiable",
            "intact",
            "not_verifiable",
        )


@pytest.mark.parametrize("snapshot", [None, {"ruleset_id": "widget-guardrails"}])
def test_a_snapshot_without_its_rule_set_is_not_verifiable(decided, snapshot):
    output = decided["output"].model_copy(update={"evidence_snapshot": snapshot})

    assert _verify(decided["gate"], output, live=False) == ("not_verifiable",) * 2


# ---------------------------------------------------------------------------
# Verify, live
# ---------------------------------------------------------------------------


def test_live_verify_finds_the_same_rules_in_a_reformatted_file(decided, ruleset_data):
    decided["file"].write_text(
        "# reformatted, same rules\n" + yaml.safe_dump(ruleset_data, indent=4),
        encoding="utf-8",
    )

    assert _verify(decided["gate"], decided["output"], live=True) == ("intact",) * 2


def test_live_verify_finds_rules_that_changed(decided, ruleset_data):
    ruleset_data["rules"][0]["action"] = "BLOCK"
    decided["file"].write_text(yaml.safe_dump(ruleset_data), encoding="utf-8")

    assert _verify(decided["gate"], decided["output"], live=True) == (
        "source_changed",
        "source_changed",
    )
    # The escrow still proves what the decision applied.
    assert _verify(decided["gate"], decided["output"], live=False) == ("intact",) * 2


@pytest.mark.parametrize(
    "damage",
    [
        lambda path: path.unlink(),
        lambda path: path.write_text("rules: [unclosed", encoding="utf-8"),
        lambda path: path.write_text("- a list, not a mapping\n", encoding="utf-8"),
        lambda path: path.write_text("ruleset_id: only-an-id\n", encoding="utf-8"),
    ],
    ids=["gone", "not-yaml", "not-a-mapping", "not-a-rule-set"],
)
def test_live_verify_of_a_file_that_cannot_be_read_is_unavailable(decided, damage):
    damage(decided["file"])

    assert _verify(decided["gate"], decided["output"], live=True) == (
        ("source_unavailable",) * 2
    )


def test_a_rule_set_the_gate_has_no_file_for_is_unavailable_live(decided):
    gate = decided["gate"]
    unknown = RulesGate({}, escrow=gate.escrow)

    assert _verify(unknown, decided["output"], live=True) == ("source_unavailable",) * 2


# ---------------------------------------------------------------------------
# The plugin
# ---------------------------------------------------------------------------


def test_the_plugin_builds_the_gate_with_the_stores_escrow_and_each_file(
    write_ruleset, submission_for, request_for
):
    directory = write_ruleset()
    framework = MagicMock(name="framework")
    framework.store.artifacts = _Escrow()
    plugin = RulesGatePlugin(RulesGateSettings(rulesets_dir=str(directory)))

    gate = plugin.build(framework)
    output = _decided(gate, submission_for, request_for)
    (directory / "guardrails.yaml").unlink()

    assert gate.escrow is framework.store.artifacts
    assert _verify(gate, output, live=False) == ("intact",)
    assert _verify(gate, output, live=True) == ("source_unavailable",)


def test_the_plugin_built_without_a_framework_escrows_nothing(write_ruleset):
    plugin = RulesGatePlugin(RulesGateSettings(rulesets_dir=str(write_ruleset())))

    assert plugin.build(None).escrow is None


def test_the_snapshot_records_the_digest_the_references_name(decided):
    output = decided["output"]
    snapshot = output.evidence_snapshot
    assert isinstance(snapshot, dict)

    assert {ref.content_hash for ref in output.evidence} == {snapshot["ruleset_hash"]}


def test_verify_survives_the_store_rendering_numbers_differently(
    write_ruleset, escrow, submission_for, request_for, ruleset_data
):
    # jsonb keeps numbers as numeric: a float constant of 1e16 comes back as
    # 10000000000000000. The digest the decision recorded is checked, not
    # one recomputed from the stored JSON.
    ruleset_data["rules"][1]["when"]["value"] = 1.0e16
    gate = _built(write_ruleset(ruleset_data), escrow)
    output = _decided(gate, submission_for, request_for, thin_history=True)
    snapshot = dict(output.evidence_snapshot)  # type: ignore[arg-type]
    rules = snapshot["ruleset"]["rules"]  # type: ignore[index]
    rules[1]["when"]["value"] = 10_000_000_000_000_000
    stored = output.model_copy(update={"evidence_snapshot": snapshot})

    assert _verify(gate, stored, live=False) == ("intact",)
    assert _verify(gate, stored, live=True) == ("intact",)


def test_a_file_too_deep_to_parse_is_unavailable(decided):
    decided["file"].write_text("[" * 5000, encoding="utf-8")

    assert _verify(decided["gate"], decided["output"], live=True) == (
        ("source_unavailable",) * 2
    )


def test_a_rule_set_that_cannot_be_canonicalized_is_unavailable(decided, ruleset_data):
    # A valid rule set whose description is a lone surrogate: it parses and
    # validates, but its canonical JSON cannot be encoded as UTF-8.
    ruleset_data["description"] = "PLACEHOLDER"
    text = yaml.safe_dump(ruleset_data).replace("PLACEHOLDER", '"\\ud83d"')
    decided["file"].write_text(text, encoding="utf-8")
    assert parse_ruleset(yaml.safe_load(text)).description == "\ud83d"

    assert _verify(decided["gate"], decided["output"], live=True) == (
        ("source_unavailable",) * 2
    )
