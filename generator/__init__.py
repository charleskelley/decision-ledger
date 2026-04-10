"""Synthetic event generator for the ATO Reasoner.

Produces LoginEvent streams for testing and evaluation.

Generates statistically realistic event streams for all 8 named scenarios, calibrated
against the DAS Group RBA dataset distributions for baseline behavior. Adds device
fingerprints, geographic coordinates, session context, attack sequences, and adversarial
payloads beyond what the RBA dataset provides.

Design goal: clean interfaces for potential extraction as a standalone library.
Scenarios are configuration-driven (YAML), distributions are pluggable, and the
CLI + programmatic API are both supported.

Usage::

    # CLI
    uv run python -m generator run --scenario baseline_normal --count 50

    # Programmatic
    from generator.generator import ScenarioGenerator
    from generator.config import ScenarioConfig

Subpackages:
    scenarios/: YAML scenario configuration files for all 8 named scenarios.
"""
