"""Decision Bundle construction and deterministic replay.

Constructs the complete DecisionBundle audit record for every decision, writing all
intermediate states to the PostgreSQL replay store. Implements the replay command:
given a bundle ID, re-executes enforcement against cached intermediate states
(including the logged gate output) to produce the same decision action.

Replay does NOT re-invoke the gate. The raw_gate_response and gate_output from
the original bundle are fed directly to enforcement. This is what makes replay
deterministic and gate-implementation-agnostic — non-deterministic gates (like
LLMs at any temperature) cannot be reproduced from inputs alone, so the bundle
caches the output instead.
"""

from app.audit.bundle import build_bundle
from app.audit.store import BundleStore, ReplayResult

__all__ = ["BundleStore", "ReplayResult", "build_bundle"]
