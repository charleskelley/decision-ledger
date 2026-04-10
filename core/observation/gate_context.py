"""GateContext — domain reasoner policy gate input contract.

The domain reasoner builds a GateContext from its own event, feature, and
scorer artifacts before submitting an Observation to DecisionLedger. The
policy gate consumes GateContext directly and has no knowledge of
domain-specific field names.

Template rendering:
    The gate resolves the full template content from a ``PromptRegistry``
    using ``prompt_template_id`` as the lookup key. The template content is
    not carried inline — keeping it out of GateContext enforces immutability
    and ensures the gate always renders the registered version, not an
    arbitrary string injected per-observation.

Retrieval filters:
    ``jurisdictions`` and ``risk_tier`` are the only external parameters that
    the hybrid retriever accepts as metadata filters. ``jurisdictions`` is
    applied to both pgvector (HNSW) and Elasticsearch; ``risk_tier`` is
    applied to pgvector only (ES filters by jurisdiction only). All other
    retrieval logic — query construction, RRF fusion, reranking — is internal
    framework logic driven by the retrieval query string.

Shadow evaluation:
    GateContext is required on every Observation, including fast-path ones.
    Populating it on fast-path observations enables shadow evaluation: any
    fast-path decision can be retrospectively routed through the policy gate
    offline to compare scorer and gate outputs on the same event.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GateContext(BaseModel):
    """Domain-assembled context for the policy gate.

    Built by the domain assembler from its own event, feature, and scoring
    artifacts. The gate uses ``prompt_template_id`` to resolve the template,
    substitutes ``template_vars`` into the placeholders, injects retrieved
    policy snippets at ``{policy_snippets}``, and calls the LLM. The gate
    never reads domain-specific field names.

    Args:
        prompt_template_id: Versioned template identifier
            (e.g., ``"ato-v1"``). Used as the ``PromptRegistry`` lookup
            key and recorded verbatim in ``DecisionBundle.prompt_template_id``
            for audit traceability. Immutable once a template version is
            deployed — any change requires a new version tag.
        template_vars: Domain-rendered substitutions for the template.
            Keys are template variable names (matching ``{placeholder}``
            tokens in the template); values are pre-rendered strings.
            The domain assembler is responsible for converting typed field
            values (floats, enums, booleans) to human-readable strings
            suitable for LLM injection. Must satisfy the template's
            ``required_vars`` — validated by the registry before rendering.
        jurisdictions: Regulatory jurisdiction codes for retrieval filtering
            (e.g., ``["US_FEDERAL", "INTERNAL"]``). Applied as a metadata
            filter to both the pgvector HNSW index and the Elasticsearch
            BM25 index. Empty list means no jurisdiction filter (all
            documents are candidates).
        risk_tier: Account risk tier for retrieval filtering
            (e.g., ``"HIGH_VALUE"``). Applied to the pgvector index only;
            includes chunks where ``risk_tier`` is NULL (applies to all
            tiers). ``None`` means no tier filter.
        target_model: Optional LLM model identifier requested by the domain
            assembler (e.g., ``"gpt-4o-2024-08-06"``). ``None`` means use
            the gate's configured default. The gate implementation may
            honor this hint or override it per its own model governance
            policy (allowlist, cost controls, fallback logic). Recorded in
            ``DecisionBundle.gate_model_version`` regardless.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    prompt_template_id: str
    template_vars: dict[str, str]
    jurisdictions: list[str]
    risk_tier: str | None = None
    target_model: str | None = None
