"""Gate runtime layer — concrete gate implementations.

Per DR-20, each gate kind lives in its own subpackage that mirrors the
contract layout under ``core/gate/<kind>/``. The reference LLM-backed
policy gate lives at ``app.gate.policy``; future gate kinds (rule
engines, second-opinion models, webhook gates) add sibling subpackages.
"""
