"""Decision Bundle construction and deterministic replay.

Constructs the complete DecisionBundle audit record for every decision, writing all
intermediate states to the PostgreSQL replay store. Implements the replay command:
given a bundle ID, re-executes enforcement against cached intermediate states
(including the logged LLM output) to produce the same final action.

Replay does NOT re-invoke the LLM. The raw_llm_response and policy_gate_output
from the original bundle are fed directly to enforcement. This is what makes
replay deterministic — LLM API outputs are not deterministic even at temperature=0.
"""

from app.audit.bundle import build_bundle
from app.audit.store import BundleStore, ReplayResult

__all__ = ["BundleStore", "ReplayResult", "build_bundle"]
