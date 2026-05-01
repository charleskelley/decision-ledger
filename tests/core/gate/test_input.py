"""Behavioral tests for the universal GateInput contract.

The framework's universal GateInput carries only ``gate_id``. Per-gate-type
subclasses (e.g., ``PolicyGateInput``) extend with kind-specific fields —
those tests live in ``tests/core/gate/policy/test_input.py``.
"""

import pytest
from pydantic import ValidationError

from core.gate import GateInput


def test_gate_input_minimal_construction():
    gi = GateInput(gate_id="policy")
    assert gi.gate_id == "policy"


def test_gate_input_requires_gate_id():
    with pytest.raises(ValidationError):
        GateInput()


def test_gate_input_is_frozen():
    gi = GateInput(gate_id="policy")
    with pytest.raises(ValidationError):
        gi.gate_id = "tampered"
