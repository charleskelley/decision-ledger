"""The example CLI prints wire submissions a ledger accepts as they are."""

from __future__ import annotations

import json

import pytest

from core.submission import Submission
from example_reasoner.__main__ import main


@pytest.mark.parametrize(
    ("band", "mode"), [("low", "record"), ("mid", "gate"), ("high", "record")]
)
def test_sample_prints_one_submission(capsys, band, mode):
    assert main(["sample", "--band", band, "--region", "REGION_B"]) == 0
    submission = Submission.model_validate_json(capsys.readouterr().out)
    assert submission.mode == mode
    assert submission.raw_event["region"] == "REGION_B"


def test_batch_prints_ndjson_records_over_a_few_widgets(capsys):
    assert main(["batch", "--count", "40", "--widgets", "4"]) == 0
    lines = capsys.readouterr().out.splitlines()
    submissions = [Submission.model_validate_json(line) for line in lines]
    assert len(submissions) == 40
    assert {s.mode for s in submissions} == {"record"}
    assert {s.bypass.rule_id for s in submissions if s.bypass} == {
        "low-score",
        "high-score",
    }
    assert len({s.entity_id for s in submissions}) == 4
    assert len({s.event_id for s in submissions}) == 40
    assert all(json.loads(line)["schema_version"] == 2 for line in lines)
