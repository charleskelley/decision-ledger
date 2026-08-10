"""Citation — a policy-document citation supporting a verdict's rationale.

Specific to gates that explain themselves with reference to retrieved
policy snippets (the LLM-backed policy gate's primary use case). Lives
under ``core/gate/policy/`` because gates that don't cite — rule engines,
ML classifiers, webhook gates — have no need for this type.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Citation(BaseModel):
    """A single policy citation supporting a rationale claim.

    Attributes:
        policy_id: Identifier of the cited policy document.
        snippet: The specific text passage cited (verbatim from the corpus).
        relevance: Human-readable explanation of how this snippet supports
            the rationale claim it is attached to.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    policy_id: str
    snippet: str
    relevance: str
