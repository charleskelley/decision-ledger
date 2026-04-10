"""YAML scenario loader for the ATO Reasoner synthetic event generator.

Loads and validates scenario configuration files from generator/scenarios/.
Each YAML filename stem must match its scenario_id field — this is enforced
at load time so mismatches are caught before any events are generated.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from generator.config import ScenarioConfig

_SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def load_scenario(scenario_id: str) -> ScenarioConfig:
    """Load and validate a single scenario from its YAML file.

    Args:
        scenario_id: The scenario identifier, must match a filename stem in
            generator/scenarios/ (e.g. "baseline_normal").

    Returns:
        Validated ScenarioConfig for the requested scenario.

    Raises:
        FileNotFoundError: No YAML file exists for the given scenario_id.
        ValueError: The scenario_id in the YAML body does not match the filename.
        pydantic.ValidationError: The YAML does not conform to ScenarioConfig schema.
    """
    path = _SCENARIOS_DIR / f"{scenario_id}.yaml"
    if not path.exists():
        available = [p.stem for p in sorted(_SCENARIOS_DIR.glob("*.yaml"))]
        raise FileNotFoundError(
            f"No scenario file found for '{scenario_id}'. Available: {available}"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = ScenarioConfig.model_validate(raw)
    if config.scenario_id != scenario_id:
        raise ValueError(
            f"scenario_id mismatch: filename='{scenario_id}', "
            f"config.scenario_id='{config.scenario_id}'. "
            "The scenario_id field must match the YAML filename stem."
        )
    return config


def load_all_scenarios() -> dict[str, ScenarioConfig]:
    """Load and validate all scenario YAML files from generator/scenarios/.

    Returns:
        Mapping of scenario_id to ScenarioConfig, sorted by scenario_id.

    Raises:
        ValueError: Any scenario_id does not match its filename.
        pydantic.ValidationError: Any YAML does not conform to ScenarioConfig schema.
    """
    return {
        path.stem: load_scenario(path.stem)
        for path in sorted(_SCENARIOS_DIR.glob("*.yaml"))
    }
