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

## In this section

- **[Corpus](policy-corpus.md)** — the 32-document policy corpus across four
  jurisdictions: document types, chunking strategy, the two-layer citation
  model, and the jurisdiction filter semantics.
