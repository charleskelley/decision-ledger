"""GateRoute — gate path vocabulary for the DecisionLedger framework.

Lives at the top level of ``core/`` because it's a routing concept the
domain reasoner produces and the gate layer consumes. Placing it under
either subpackage would force a cross-subpackage import in the other —
both ``core/observation/`` (which carries the route on ``Observation``)
and ``core/gate/`` (which dispatches based on it) need the type, but
neither owns it.
"""

from enum import StrEnum


class GateRoute(StrEnum):
    """Gate path the domain reasoner selects for an observation.

    Parallels ``DecisionAction``: the enum defines what paths exist, the
    domain reasoner (assembler) decides which one applies based on scorer
    confidence bands. The framework honors the selected route.

    Attributes:
        FAST_PATH_ALLOW: The reasoner is confident the observation is low risk.
            The framework routes directly to ALLOW without invoking the
            gate.
        FAST_PATH_BLOCK: The reasoner is confident the observation is high risk.
            The framework routes directly to BLOCK without invoking the
            gate.
        ROUTE_TO_GATE: The reasoner's confidence is insufficient for a
            fast-path decision. The framework forwards the observation to
            the configured gate for full reasoning.
    """

    FAST_PATH_ALLOW = "FAST_PATH_ALLOW"
    FAST_PATH_BLOCK = "FAST_PATH_BLOCK"
    ROUTE_TO_GATE = "ROUTE_TO_GATE"
