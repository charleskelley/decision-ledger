"""``GET /health`` answers without a key."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conformance._ledger import Ledger


def test_health_needs_no_key(ledger: Ledger):
    response = ledger.http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
