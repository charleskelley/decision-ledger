"""Gates resource — the gates the deployment composes (DR-31).

``GET /api/v1/gates`` lists every composed gate: its id, kind, whether it is
deterministic, whether the deployment mandates it for every reasoner, and
the JSON schema of the ``gate_config`` it accepts, so a reasoner calling the
ledger over HTTP can build its gate contexts without the gate's code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from app.api.errors import problem_responses
from app.api.models import GateList, GateView
from app.auth import Caller  # noqa: TC001 (FastAPI resolves the dependency)

if TYPE_CHECKING:
    from app.auth import Principal
    from app.plugin import FrameworkServices

router = APIRouter(prefix="/api/v1", tags=["gates"])


def list_gates(*, principal: Principal, framework: FrameworkServices) -> GateList:
    """Every composed gate, sorted by ``gate_id``."""
    del principal  # any authenticated caller may read the gates
    views = []
    for gate_id in sorted(framework.gates):
        composed = framework.gates[gate_id]
        plugin = composed.plugin
        config_model = plugin.config_model
        views.append(
            GateView(
                gate_id=gate_id,
                kind=str(plugin.kind),
                deterministic=plugin.deterministic,
                mandatory=composed.mandatory,
                config_schema=(
                    config_model.model_json_schema()
                    if config_model is not None
                    else None
                ),
            )
        )
    return GateList(gates=views)


@router.get("/gates", response_model=GateList, responses=problem_responses(401))
def get_gates(request: Request, principal: Caller) -> GateList:
    """Every gate the deployment composes."""
    return list_gates(principal=principal, framework=request.app.state.framework)
