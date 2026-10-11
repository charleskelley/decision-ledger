"""FastAPI router for the stub reasoner.

One trivial route is enough to prove that ``create_app`` mounts a discovered
plugin's router next to the framework routes.
"""

from __future__ import annotations

from fastapi import APIRouter

from tests._stub_reasoner.registry import STUB_REGISTRATION

router = APIRouter(prefix="/api/v1/stub", tags=["stub"])


@router.get("/ping")
def ping() -> dict[str, str]:
    """Return the stub reasoner's identity; proves the router is mounted."""
    return {"reasoner_id": STUB_REGISTRATION.reasoner_id, "status": "ok"}
