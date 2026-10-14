"""PolicyGateSnapshot — schema of the policy gate's ``evidence_snapshot``.

Everything the LLM-backed policy gate consulted to reach a verdict: the
retrieval it ran (query, filters' result set, path taken, index version) and
the exact prompt it sent (model, template identity and verbatim snapshot,
substitutions, rendered text). The gate serializes one of these with
``model_dump(mode="json")`` into ``GateOutput.evidence_snapshot``; the
framework stores it verbatim and never reads it (DR-28). Readers that know
this gate re-validate it with ``PolicyGateSnapshot.model_validate(...)``.

This replaces two former bundle surfaces: the bundle-level retrieval layers
(``retrieval_query`` / ``retrieval_results`` / ``retrieval_path`` /
``index_version``) and the ``PolicyGateInput`` subclass. Together with the
template snapshot and the substitutions, an auditor can verify that
``rendered_prompt`` was built correctly without querying the prompt
registry or the corpus.

Every field is a typed attribute; the opacity is at the framework boundary,
not inside the gate.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from policy_rag_gate.models.prompt import PromptSnapshot
from policy_rag_gate.models.snippet import RetrievedSnippet


class PolicyGateSnapshot(BaseModel):
    """What the policy gate consulted for one invocation.

    Attributes:
        retrieval_query: The reasoner-rendered query string the gate sent
            to the retriever (from ``gate_config["retrieval_query"]``).
            Empty when the gate failed before retrieving.
        retrieval_results: Corpus passages the retriever returned, in rank
            order. Empty when the gate failed before retrieving.
        retrieval_path: Which retrieval path produced them (``"reranked"``
            or ``"rrf_only"``), or ``"skipped"`` when retrieval did not run.
        index_version: Version label of the retrieval index.
        model_version: LLM model identifier used for this invocation.
        prompt_template_id: Template identifier resolved from the registry
            (the audit key for the active prompt version).
        prompt_template_version: Semantic version of that template.
        corpus_version: Version label of the reasoner's policy corpus
            (from ``gate_config["corpus_version"]``).
        rendered_prompt: Full prompt as sent to the LLM. Empty when
            rendering failed or the gate stopped before rendering.
        prompt_snapshot: Verbatim template snapshot at invocation time.
        template_vars: Domain substitutions used for this invocation.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    retrieval_query: str
    retrieval_results: list[RetrievedSnippet]
    retrieval_path: str
    index_version: str
    model_version: str
    prompt_template_id: str
    prompt_template_version: str
    corpus_version: str
    rendered_prompt: str
    prompt_snapshot: PromptSnapshot
    template_vars: dict[str, str]
