"""Helpers the conformance tests share: the instance, keys, and payloads.

Everything goes over HTTP (``httpx``): the suite imports no DecisionLedger
code, so it checks the contract a reasoner team builds against, not the
implementation.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

EXAMPLES = Path(__file__).resolve().parent / "examples"
KEY_HEADER = "X-DecisionLedger-Key"
PROBLEM = "application/problem+json"
VERSION = re.compile(r"^[0-9a-f]{16}$")


def env(name: str) -> str | None:
    """A configuration value, or ``None`` when unset or blank."""
    value = os.environ.get(name, "").strip()
    return value or None


def example(name: str) -> dict[str, Any]:
    """An example submission with a fresh event id and subject."""
    payload = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    return fresh(payload)


def fresh(payload: dict[str, Any], *, entity_id: str | None = None) -> dict[str, Any]:
    """A copy with a unique ``event_id`` (and subject, unless given)."""
    copied = copy.deepcopy(payload)
    copied["event_id"] = f"conformance-{uuid.uuid4()}"
    copied["entity_id"] = entity_id or str(uuid.uuid4())
    return copied


@dataclass
class Ledger:
    """One DecisionLedger instance and the keys the suite may use."""

    http: httpx.Client
    key: str | None
    other_key: str | None
    operator_key: str | None

    def headers(self, key: str | None) -> dict[str, str]:
        return {KEY_HEADER: key} if key else {}

    def post(self, path: str, body: Any, *, key: str | None, **kw: Any):
        content = body if isinstance(body, str | bytes) else json.dumps(body)
        return self.http.post(path, content=content, headers=self.headers(key), **kw)

    def get(self, path: str, *, key: str | None, **kw: Any):
        return self.http.get(path, headers=self.headers(key), **kw)

    def submit(self, submission: dict[str, Any], *, key: str | None = None):
        return self.post("/api/v1/decisions", submission, key=key or self.key)


def problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    """Assert ``response`` is an RFC 9457 problem with ``status`` and ``code``."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM), response.headers
    body = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"].startswith("urn:decisionledger:problem:")
    assert body["title"]
    assert body["detail"]
    return body
