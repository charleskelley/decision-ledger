"""Concrete ``JudgeClient`` implementations.

Each module in this package adapts a specific LLM SDK to the SDK-agnostic
``JudgeClient`` protocol defined in ``eval.judge``. Adding a new backend
(Anthropic, DeepSeek, local endpoint) means writing one file here — nothing
in ``eval/judge.py``, ``eval/dimensions/``, or ``eval/runners/`` changes.

This is the only subpackage in ``eval/`` permitted to import LLM SDK classes.
"""
