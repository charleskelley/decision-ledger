# Policy Gate

The **policy gate** is the LLM-backed gate kind (`gate_id: "policy"`) and the
reference implementation of the [gate contracts](../implementation.md). For
events the fast-path scorer routes to the gate, it retrieves relevant policy
chunks, renders an immutable versioned prompt, and asks the LLM to reason
against retrieved policy — producing a `PolicyGateVerdict` with a rationale
and two-layer citations (the internal rule applied, and the regulatory
authority behind it).

Its concrete contracts live in `core/gate/policy/`: `PolicyGateInput`
captures the full invocation context (model version, prompt template id +
version, corpus version, rendered prompt), and `PolicyGateOutput` carries the
verdict plus raw response text and token cost. Because every input and the
logged output land in the Decision Bundle, replay re-executes enforcement
against the cached gate output without ever re-invoking the LLM.

## Prompts belong to the reasoner

The gate is shared by every reasoner and contains no domain wording. Each
reasoner ships its own prompt templates in its plugin's `prompts_dir` (ATO:
`ato_reasoner/prompts/ato-v{1,2,3}.yaml`), and `YamlPromptRegistry` loads
one directory per composed reasoner at startup. A template YAML carries
`template_id`, `version`, `description`, `template`, and an optional
top-level `system:` key with the system message the gate sends alongside
the rendered template. A template without `system:` gets the framework's
generic `DEFAULT_SYSTEM_TEXT` (`app/gate/policy/prompt_registry.py`). The
system text is part of the `PromptSnapshot` recorded in the bundle.

Template ids are global: the same `template_id` in two reasoners'
directories fails startup, because the id is the audit key in every
Decision Bundle. Template files are immutable, so moving ATO's former
hardcoded system prompt into its template required a new version,
`ato-v3` (v2's user text byte for byte plus `system:`); see DR-26.

## In this section

- **[Corpus](policy-corpus.md)** — the ATO reasoner's 32-document policy corpus across four
  jurisdictions: document types, chunking strategy, the two-layer citation
  model, and the jurisdiction filter semantics.
