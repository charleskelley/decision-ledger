"""The ATO golden datasets parse with the framework's generic eval loaders.

The loaders live in ``eval/dimensions`` and know nothing about ATO; these
tests prove the ATO datasets the ``EvalAdapter`` points at are well-formed
for them, with every event validated as a ``LoginEvent``.
"""

from __future__ import annotations

from ato_reasoner.eval.adapter import ADAPTER
from ato_reasoner.events import LoginEvent
from eval.dimensions.consistency import load_scenarios
from eval.dimensions.robustness import (
    load_fallback_cases,
    load_injection_cases,
    load_novel_pattern_cases,
    load_schema_violation_cases,
)
from policy_rag_gate.eval.citation import load_citation_cases
from policy_rag_gate.eval.faithfulness import load_faithfulness_cases
from policy_rag_gate.eval.retrieval import load_golden_queries

_ROOT = ADAPTER.datasets_dir


def _unique(ids: list[str]) -> bool:
    return len(ids) == len(set(ids))


def test_retrieval_golden_queries_load_with_unique_ids_and_relevant_docs():
    queries = load_golden_queries(_ROOT / "retrieval" / "golden_queries.yaml")

    assert len(queries) >= 10
    assert all(q.relevant_document_ids for q in queries)
    assert _unique([q.query_id for q in queries])


def test_citation_cases_load_with_unique_ids():
    cases = load_citation_cases(_ROOT / "citations" / "golden_outputs.yaml")

    assert len(cases) >= 3
    assert _unique([c.case_id for c in cases])


def test_faithfulness_cases_load_with_unique_ids():
    cases = load_faithfulness_cases(_ROOT / "faithfulness" / "golden_outputs.yaml")

    assert cases
    assert _unique([c.case_id for c in cases])


def test_consistency_scenarios_load_as_login_events():
    paths = sorted((_ROOT / "scenarios").glob("*.yaml"))
    scenarios = [
        s for p in paths for s in load_scenarios(p, event_type=ADAPTER.event_type)
    ]

    assert len(paths) >= 1
    assert _unique([s.scenario_id for s in scenarios])
    events = [e for s in scenarios for o in s.orderings for e in o.events]
    assert events
    assert all(type(e) is LoginEvent for e in events)


def test_robustness_cases_load_as_login_events():
    robustness = _ROOT / "robustness"
    event_type = ADAPTER.event_type
    injection = load_injection_cases(
        robustness / "injection.yaml", event_type=event_type
    )
    schema = load_schema_violation_cases(
        robustness / "malformed.yaml", event_type=event_type
    )
    novel = load_novel_pattern_cases(robustness / "novel.yaml", event_type=event_type)
    fallback = load_fallback_cases(robustness / "fallback.yaml", event_type=event_type)

    assert injection
    assert schema
    assert novel
    assert fallback
    events = [
        *(e for c in injection for e in (c.clean_event, c.injected_event)),
        *(c.event for c in schema),
        *(e for c in novel for e in (c.event, *c.priming_events)),
        *(e for c in fallback for e in (c.event, *c.priming_events)),
    ]
    assert all(type(e) is LoginEvent for e in events)
