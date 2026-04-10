"""Behaviour tests for generator.loader.

Verifies:
    - All 8 scenario YAML files load without error
    - scenario_id in each YAML body matches its filename stem
    - load_scenario raises FileNotFoundError for unknown scenario IDs
    - load_scenario raises ValueError when scenario_id mismatches filename
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from generator.loader import load_all_scenarios, load_scenario

_EXPECTED_SCENARIO_IDS = {
    "adversarial_probe",
    "baseline_normal",
    "credential_stuffing_burst",
    "device_fingerprint_anomaly",
    "geo_impossible",
    "high_velocity_legitimate",
    "novel_entity",
    "post_breach_ato",
}


def test_all_scenarios_load_without_error():
    scenarios = load_all_scenarios()
    assert set(scenarios.keys()) == _EXPECTED_SCENARIO_IDS


def test_scenario_id_matches_filename_for_all_scenarios():
    scenarios = load_all_scenarios()
    for scenario_id, config in scenarios.items():
        assert config.scenario_id == scenario_id, (
            f"scenario_id mismatch: file={scenario_id!r}, "
            f"config.scenario_id={config.scenario_id!r}"
        )


def test_load_scenario_returns_correct_config():
    config = load_scenario("baseline_normal")
    assert config.scenario_id == "baseline_normal"
    assert config.generation.user_count == 1
    assert config.generation.events_per_run == 50


def test_load_scenario_raises_for_unknown_id():
    with pytest.raises(FileNotFoundError, match="no_such_scenario"):
        load_scenario("no_such_scenario")


def test_load_scenario_raises_on_scenario_id_mismatch(tmp_path: Path):
    """scenario_id field in YAML body must match the filename stem."""
    bad_yaml = tmp_path / "wrong_name.yaml"
    scenarios_dir = Path(__file__).parent.parent.parent / "generator" / "scenarios"
    original = yaml.safe_load((scenarios_dir / "baseline_normal.yaml").read_text())
    # Write under a different name — scenario_id in body still says "baseline_normal"
    bad_yaml.write_text(yaml.dump(original))

    # Patch the loader's _SCENARIOS_DIR to our temp dir so we can trigger the check
    import generator.loader as loader_module

    original_dir = loader_module._SCENARIOS_DIR
    loader_module._SCENARIOS_DIR = tmp_path
    try:
        with pytest.raises(ValueError, match="scenario_id mismatch"):
            load_scenario("wrong_name")
    finally:
        loader_module._SCENARIOS_DIR = original_dir
