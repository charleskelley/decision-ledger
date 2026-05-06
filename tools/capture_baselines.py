"""Live capture of faithfulness fixtures from real gate runs.

Replaces the hand-crafted seed fixtures in
``eval/datasets/faithfulness/golden_outputs.yaml`` with snapshots of actual
gate output. This produces fixtures grounded in the model+prompt currently
shipping, so faithfulness eval scores reflect real behavior.

Re-run trigger: any time the gate model or prompt template version changes,
re-run this script and commit the regenerated YAML in the same change.
Stale fixtures across model swaps produce false-positive eval failures.

Prerequisites:
    docker compose up -d
    make build-policy-index   # pgvector + ES seeded with policy corpus
    export OPENAI_API_KEY=sk-...
    export ANTHROPIC_API_KEY=sk-ant-...   # not used by this script, but
                                          # required by PipelineDriver
                                          # constructor when available

Usage:
    make capture-baselines
    # or directly:
    uv run python -m tools.capture_baselines
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from eval.clients.pipeline import PipelineDriver
from generator.factory import EventFactory
from generator.loader import load_scenario

# Scenarios to drive — each maps to one captured fixture. Choose scenarios
# the scorer routes to the gate (not fast-path); ALLOW/CHALLENGE/HOLD/BLOCK
# triggers all produce useful rationales.
_CAPTURE_SCENARIOS: list[str] = [
    "device_fingerprint_anomaly",
    "geo_impossible",
    "credential_stuffing_burst",
    "post_breach_ato",
    "novel_entity",
    "high_velocity_legitimate",
]


_OUTPUT_PATH = Path("eval/datasets/faithfulness/golden_outputs.yaml")


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

    Returns None when the bundle has no usable verdict (fast-path bundles,
    schema-validation failures). The caller skips those.
    """
    if bundle.gate_output is None or bundle.gate_output.verdict is None:
        return None
    verdict = bundle.gate_output.verdict
    rationale = getattr(verdict, "rationale", None)
    citations = getattr(verdict, "citations", None) or []
    if rationale is None:
        return None

    contexts = [s.text for s in bundle.retrieval_results]
    cited_snippets = [c.snippet for c in citations]

    return {
        "case_id": f"f-cap-{scenario_id}",
        "rationale": rationale,
        "contexts": contexts,
        "cited_snippets": cited_snippets,
    }


async def _capture() -> list[dict]:
    """Drive every capture scenario and collect faithfulness cases."""
    from reasoner.account_takeover.pipeline import run_ato_decision

    cases: list[dict] = []
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
            driver._redis.flushdb()
            events = _build_events_for_scenario(scenario_id)
            # Track the latest bundle that produced a usable verdict. This
            # handles two cases: (1) the trigger event is the last in the
            # sequence (most scenarios), and (2) the gate fires mid-sequence
            # on a borderline event but the final event decays back to
            # fast-path. The "latest verdict-bearing bundle" captures the
            # most recent semantically valid case.
            captured_case: dict | None = None
            for event in events:
                bundle = await run_ato_decision(
                    event=event,
                    feature_svc=driver._feature_svc,
                    scorer=driver._scorer,
                    retriever=driver._retriever,
                    gate=driver._gate,
                    store=driver._store,
                    corpus_version=driver._corpus_version,
                )
                case = _bundle_to_case(scenario_id, bundle)
                if case is not None:
                    captured_case = case
            if captured_case is None:
                print(
                    f"  {scenario_id}: skipped (fast-path or no verdict)",
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
    return cases


def _merge_cases(existing: list[dict], captured: list[dict]) -> list[dict]:
    """Merge new captures into the existing case list.

    Captures with a ``case_id`` already present in ``existing`` overwrite
    that entry in place (re-running a scenario refreshes its case).
    Captures with a new ``case_id`` are appended. This preserves any
    hand-crafted seed cases that the live capture didn't replace —
    important for dimensions like faithfulness whose dataset must stay
    populated when only a subset of scenarios route to gate.
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
        "# `f-cap-` are live-captured by tools/capture_baselines.py against\n"
        "# the gate's current model + prompt version; re-run that script\n"
        "# after any model or prompt change so captured fixtures reflect\n"
        "# shipping behavior. Cases with prefix `f0N-` are hand-crafted\n"
        "# seeds derived from the citations dataset; replace them by\n"
        "# capturing more scenarios as polish §6 (calibration notebook)\n"
        "# tightens scorer routing.\n"
        "#\n"
        "# Schema (from eval/dimensions/faithfulness.py):\n"
        "#   case_id          — stable identifier\n"
        "#   rationale        — gate's natural-language justification\n"
        "#   contexts         — full retrieved snippet text list (the haystack)\n"
        "#   cited_snippets   — subset of contexts the rationale grounds in\n\n"
    )
    payload = {"cases": merged}
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=80)
    path.write_text(header + body, encoding="utf-8")


def main() -> int:
    """Run the capture and write the YAML; exit 0 on success."""
    cases = asyncio.run(_capture())
    if not cases:
        print(
            "error: no cases captured (every scenario hit fast-path or no verdict). "
            "Investigate gate-route distribution before re-running.",
            file=sys.stderr,
        )
        return 1
    _write_yaml(cases, _OUTPUT_PATH)
    print(
        f"wrote {len(cases)} faithfulness cases to {_OUTPUT_PATH}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
