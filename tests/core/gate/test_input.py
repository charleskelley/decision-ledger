"""Behavioral tests for the universal GateInput contract.

The framework's universal GateInput carries only ``gate_id`` — the record
that the framework invoked a gate. A gate records what it consulted in its
own ``GateOutput.evidence_snapshot`` (DR-28); the framework defines no
per-gate input subclass.
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
