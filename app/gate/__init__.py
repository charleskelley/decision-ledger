"""Interim home of the reference policy gate's runtime.

Gates are plugins (DR-28): each is a workspace member under ``gates/`` that
publishes a ``GatePlugin`` through the ``decisionledger.gates`` entry-point
group, and the framework never imports one. The reference LLM-backed policy
gate still lives here, at ``app.gate.policy``, registered through that same
protocol by an interim entry point on the framework package; Phase 1b moves
it into ``gates/policy_rag/``. New gates are workspace members, never new
subpackages here.
"""
