# Gates

A **gate** is a bounded evaluation step that examines an assembled
`Observation` and emits a typed verdict the enforcement layer can act on.
Gates are where judgment happens — everything downstream of a gate
(enforcement, audit, replay) is deterministic.

DecisionLedger separates the **universal gate contracts** — `GateInput`,
`GateOutput`, and `GateVerdict` in `core/gate/` — from **per-gate-kind
concrete contracts** that subclass them in sibling subpackages (e.g.
`core/gate/policy/` for the LLM-backed policy gate). The enforcement and
audit layers depend only on the universal base contracts, so new gate kinds
plug in without framework changes. See
[DR-20](../decisions.md#dr-20-universal-gate-contracts-per-gate-type-subpackages-closed-discriminated-union-for-mvp)
for the rationale.

## In this section

- **[Implementation](implementation.md)** — the end-to-end guide to the
  universal contracts and how to add a new gate kind, with the LLM policy
  gate as the worked reference.
- **[Policy](policy/index.md)** — the LLM policy gate: its typed contracts,
  prompt versioning, and the [policy corpus](policy/policy-corpus.md) that
  grounds its citations.
