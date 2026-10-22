"""Live capture of faithfulness fixtures from real gate runs.

Replaces the hand-crafted seed fixtures in the ATO faithfulness dataset
(``datasets/faithfulness/golden_outputs.yaml`` next to this module) with
snapshots of actual
gate output. This produces fixtures grounded in the model+prompt currently
shipping, so faithfulness eval scores reflect real behavior.

Re-run trigger: any time the gate model or prompt template version changes,
re-run this script and commit the regenerated YAML in the same change.
Stale fixtures across model swaps produce false-positive eval failures.

A gate output from a template that renders evidence by authority tier
(DR-34) also becomes an authority case
(``datasets/authority/golden_outputs.yaml``): the corpora and tiers of the
passages the gate rendered, the corpus the verdict named as governing, and
the documents it cites as the passages on point. The gate's eval reports
the authority-violation rate over them.

Prerequisites:
    docker compose up -d
    make build-policy-index-ato-reasoner   # the policy gate's stores seeded
    export OPENAI_API_KEY=sk-...           # the policy gate calls OpenAI

Usage:
    make capture-baselines-account_takeover
    # or directly:
    uv run python -m ato_reasoner.eval.capture_baselines
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from ato_reasoner.eval.driver import PipelineDriver
from ato_reasoner.generator.factory import EventFactory
from ato_reasoner.generator.loader import load_scenario
from policy_rag_gate import POLICY_GATE_ID
from policy_rag_gate.models import PolicyGateExtra, PolicyGateSnapshot

# Scenarios to drive — each maps to one captured fixture. Choose scenarios
# ATO asks the gate about (not recorded in a band); ALLOW/CHALLENGE/HOLD/BLOCK
# triggers all produce useful rationales.
_CAPTURE_SCENARIOS: list[str] = [
    "device_fingerprint_anomaly",
    "geo_impossible",
    "credential_stuffing_burst",
    "post_breach_ato",
    "novel_entity",
    "high_velocity_legitimate",
]


_OUTPUT_PATH = (
    Path(__file__).parent / "datasets" / "faithfulness" / "golden_outputs.yaml"
)
_AUTHORITY_PATH = (
    Path(__file__).parent / "datasets" / "authority" / "golden_outputs.yaml"
)


def _build_events_for_scenario(scenario_id: str, *, count: int = 5) -> list:
    """Return a deterministic event sequence for one scenario."""
    config = load_scenario(scenario_id)
    factory = EventFactory(
        config,
        account_ids=[f"acct-capture-{scenario_id}"],
        seed=42,
    )
    base_ts = datetime(2026, 4, 16, 9, 0, tzinfo=UTC)
    return [
        factory.build_event(
            f"acct-capture-{scenario_id}",
            base_ts + timedelta(minutes=i * 7),
        )
        for i in range(count)
    ]


def _bundle_to_case(scenario_id: str, bundle) -> dict | None:
    """Translate a DecisionBundle into a faithfulness case dict.

    Returns None when the bundle has no usable verdict (recorded decisions,
    whose policy gate was skipped; schema-validation failures). The caller
    skips those. The rationale and
    citations come from the policy gate's ``extra`` and the retrieved
    contexts from its ``evidence_snapshot`` (DR-28).
    """
    policy = bundle.gate(POLICY_GATE_ID)
    gate_output = policy.output if policy is not None else None
    if gate_output is None or gate_output.verdict is None:
        return None
    extra = PolicyGateExtra.model_validate(gate_output.extra, strict=False)
    if extra.rationale is None:
        return None
    rationale = extra.rationale
    snapshot = PolicyGateSnapshot.model_validate(
        gate_output.evidence_snapshot, strict=False
    )

    contexts = [c.text for c in snapshot.contexts]
    cited_snippets = [c.snippet for c in extra.citations]

    return {
        "case_id": f"f-cap-{scenario_id}",
        "rationale": rationale,
        "contexts": contexts,
        "cited_snippets": cited_snippets,
    }


def _bundle_to_authority_case(scenario_id: str, bundle) -> dict | None:
    """Translate a tiered gate output into an authority case.

    Returns None unless the policy gate ran a template that renders evidence
    by tier and returned a verdict: only that verdict names a governing
    corpus. The documents its citations name stand for the passages on
    point.
    """
    policy = bundle.gate(POLICY_GATE_ID)
    gate_output = policy.output if policy is not None else None
    if gate_output is None or gate_output.verdict is None:
        return None
    snapshot = PolicyGateSnapshot.model_validate(
        gate_output.evidence_snapshot, strict=False
    )
    if snapshot.evidence_layout != "tiered_blocks":
        return None
    extra = PolicyGateExtra.model_validate(gate_output.extra, strict=False)
    return {
        "case_id": f"a-cap-{scenario_id}",
        "governing_corpus_id": extra.governing_corpus_id,
        "contexts": [
            {"corpus_id": c.corpus_id, "tier": c.tier, "document_id": c.document_id}
            for c in snapshot.contexts
        ],
        "on_point_document_ids": sorted({c.policy_id for c in extra.citations}),
    }


async def _capture() -> tuple[list[dict], list[dict]]:
    """Drive every capture scenario; collect faithfulness and authority cases."""
    cases: list[dict] = []
    authority_cases: list[dict] = []
    driver = PipelineDriver()
    try:
        for scenario_id in _CAPTURE_SCENARIOS:
            print(f"capturing {scenario_id}...", file=sys.stderr)
            # Reset feature state at scenario boundaries so prior scenarios'
            # Redis state doesn't leak velocity/novelty signals across runs.
            # Within a scenario, drive all events sequentially to warm
            # feature history — first events compute neutral features
            # (new_account=True → novelty=0.5), later events have meaningful
            # signals tied to the scenario's YAML configuration.
            driver.reset_state()
            events = _build_events_for_scenario(scenario_id)
            # Track the latest bundle that produced a usable verdict. This
            # handles two cases: (1) the trigger event is the last in the
            # sequence (most scenarios), and (2) the gate fires mid-sequence
            # on a borderline event but the final event decays back to
            # a recorded band. The "latest verdict-bearing bundle" captures the
            # most recent semantically valid case.
            captured_case: dict | None = None
            authority_case: dict | None = None
            for event in events:
                bundle = await driver.decide(event)
                case = _bundle_to_case(scenario_id, bundle)
                if case is not None:
                    captured_case = case
                    authority_case = _bundle_to_authority_case(scenario_id, bundle)
            if authority_case is not None:
                authority_cases.append(authority_case)
            if captured_case is None:
                print(
                    f"  {scenario_id}: skipped (recorded or no verdict)",
                    file=sys.stderr,
                )
                continue
            cases.append(captured_case)
            case = captured_case
            print(
                f"  {scenario_id}: captured "
                f"({len(case['contexts'])} contexts, "
                f"{len(case['cited_snippets'])} cited)",
                file=sys.stderr,
            )
    finally:
        driver.close()
    return cases, authority_cases


def _merge_cases(existing: list[dict], captured: list[dict]) -> list[dict]:
    """Merge new captures into the existing case list.

    Captures with a ``case_id`` already present in ``existing`` overwrite
    that entry in place (re-running a scenario refreshes its case).
    Captures with a new ``case_id`` are appended. This preserves any
    hand-crafted seed cases that the live capture didn't replace —
    important for dimensions like faithfulness whose dataset must stay
    populated when only a subset of scenarios reach the gate.
    """
    by_id: dict[str, dict] = {c["case_id"]: c for c in existing}
    for c in captured:
        by_id[c["case_id"]] = c
    return list(by_id.values())


def _write_yaml(cases: list[dict], path: Path) -> None:
    """Merge new captures into the faithfulness dataset YAML.

    Reads the existing YAML if present, merges captures by ``case_id``,
    and writes the merged result. Preserves seed cases that the current
    capture run didn't refresh.
    """
    existing: list[dict] = []
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        existing = raw.get("cases", []) or []

    merged = _merge_cases(existing, cases)

    header = (
        "# Faithfulness golden outputs.\n"
        "#\n"
        "# Mixed seed + live-captured fixtures. Cases with case_id prefix\n"
        "# `f-cap-` are live-captured by ato_reasoner/eval/capture_baselines.py\n"
        "# (make capture-baselines-account_takeover) against\n"
        "# the gate's current model + prompt version; re-run that script\n"
        "# after any model or prompt change so captured fixtures reflect\n"
        "# shipping behavior. Cases with prefix `f0N-` are hand-crafted\n"
        "# seeds derived from the citations dataset; replace them by\n"
        "# capturing more scenarios as polish §6 (calibration notebook)\n"
        "# tightens scorer routing.\n"
        "#\n"
        "# Schema (from policy_rag_gate/eval/faithfulness.py):\n"
        "#   case_id          — stable identifier\n"
        "#   rationale        — gate's natural-language justification\n"
        "#   contexts         — full retrieved snippet text list (the haystack)\n"
        "#   cited_snippets   — subset of contexts the rationale grounds in\n\n"
    )
    payload = {"cases": merged}
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=80)
    path.write_text(header + body, encoding="utf-8")


def _write_authority_yaml(cases: list[dict], path: Path) -> None:
    """Merge new authority captures into the authority dataset YAML."""
    existing: list[dict] = []
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        existing = raw.get("cases", []) or []
    header = (
        "# Authority golden outputs (DR-34).\n"
        "#\n"
        "# Live-captured by ato_reasoner/eval/capture_baselines.py\n"
        "# (make capture-baselines-account_takeover) from gate outputs of a\n"
        "# template that renders evidence by authority tier. Each case: the\n"
        "# corpora and tiers of the passages the gate rendered, the corpus the\n"
        "# verdict named as governing, and the documents it cites (the\n"
        "# passages on point). The gate's eval reports the rate of verdicts\n"
        "# naming a lower tier than a passage on point\n"
        "# (policy_rag_gate/eval/authority.py). ATO binds one corpus at tier\n"
        "# 1, so its cases cannot violate authority; they keep the metric's\n"
        "# path exercised.\n\n"
    )
    payload = {"cases": _merge_cases(existing, cases)}
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=80)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + body, encoding="utf-8")


def main() -> int:
    """Run the capture and write the YAML; exit 0 on success."""
    cases, authority_cases = asyncio.run(_capture())
    if not cases:
        print(
            "error: no cases captured (every scenario recorded or no verdict). "
            "Investigate gate-route distribution before re-running.",
            file=sys.stderr,
        )
        return 1
    _write_yaml(cases, _OUTPUT_PATH)
    print(
        f"wrote {len(cases)} faithfulness cases to {_OUTPUT_PATH}",
        file=sys.stderr,
    )
    if authority_cases:
        _write_authority_yaml(authority_cases, _AUTHORITY_PATH)
        print(
            f"wrote {len(authority_cases)} authority cases to {_AUTHORITY_PATH}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
