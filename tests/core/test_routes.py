"""Behavioral tests for GateRoute — gate path vocabulary.

GateRoute defines what gate paths exist (core/routes.py). The domain
reasoner's assembler decides which one applies. Parallels DecisionAction /
actions.py.
"""

from core.routes import GateRoute


def test_all_three_gate_route_values_exist():
    assert GateRoute.FAST_PATH_ALLOW == "FAST_PATH_ALLOW"
    assert GateRoute.FAST_PATH_BLOCK == "FAST_PATH_BLOCK"
    assert GateRoute.ROUTE_TO_GATE == "ROUTE_TO_GATE"


def test_fast_path_values_are_distinct_from_route_to_gate():
    fast_path = {GateRoute.FAST_PATH_ALLOW, GateRoute.FAST_PATH_BLOCK}
    assert GateRoute.ROUTE_TO_GATE not in fast_path
