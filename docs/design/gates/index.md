# Gates

A **gate** is a plugin the ledger runs on a decision. It reads the
reasoner's submission, the subject the ledger resolved, and its own
configuration, and returns a `GateOutput`: a typed verdict the ledger's
deterministic enforcement acts on, plus whatever the gate wants recorded for
audit. Gates are where judgment happens; everything downstream of a gate
(enforcement, the combinator, the bundle write, replay) is deterministic.

The framework owns the gate *contract* and nothing about how a gate reaches
its verdict ([DR-28](../decisions.md#dr-28-gate-contract-and-gate-plugins)).
A gate is discovered through the `decisionledger.gates` entry-point group,
built once per process, and owns its own infrastructure, prompts, data, and
eval. The framework never imports a gate package.

## What a gate declares

Each gate plugin declares a `gate_id`, a `kind`, whether it is
`deterministic`, and an optional `config_model` that validates the
`gate_config` a reasoner sends it.

| `kind` (`GateKind`) | What it is | Reference |
|---|---|---|
| `llm` | A model-backed gate; non-deterministic by nature. | The RAG policy gate (`gates/policy_rag`) |
| `rules` | Declarative rules over the reasoner's evidence. | The rules gate (`gates/rules`) |
| `lookup` | An adapter to an enterprise system of record (a do-not-contact list, a sanctions screen). | None shipped |

The kind describes the gate; enforcement does not branch on it. What
enforcement reads is `deterministic`: a deterministic gate (a pure function
of its request) skips the adversarial-probe and low-confidence tiers, which
are meaningless for it. The jurisdiction-conflict tier runs only when a
gate's evidence carries jurisdictions. See
[How to implement a gate](implementation.md#enforcement-of-one-gate) for the
tiers.

Lookup gates are adapters to systems the enterprise already runs. The
framework hosts no lookup table and ships no reference lookup gate; a lookup
gate keys on the resolved subject the ledger hands it
(`GateRequest.identity`, see [Identity](../identity.md)).

## Gate sets: mandatory and advisory

A reasoner's registration binds the gates its decisions go through, each as
a `GateBinding(gate_id, mandatory)`
([DR-29](../decisions.md#dr-29-gate-sets-and-the-conjunction-combinator)):

- A **mandatory** gate runs on every decision, recorded ones included.
- An **advisory** gate runs only when the reasoner asks the ledger to gate
  the decision (mode `gate`). On a recorded decision (mode `record`, citing
  a registered bypass rule) it is recorded as skipped
  ([DR-30](../decisions.md#dr-30-record-and-gate-the-two-submission-modes)).

The deployment can also mandate gates for every reasoner with
`DECISIONLEDGER_MANDATORY_GATES` (a comma-separated list of gate ids). A
mandated gate the reasoner binds becomes mandatory for it; a mandated gate
the reasoner does not bind runs with an empty configuration, and the
reasoner cannot address it: a gate context for a gate the registration does
not bind is rejected at intake with `422 GATE_NOT_BOUND`. Startup refuses a
deployment whose mandated gate is not composed, or does not accept that
empty configuration from every reasoner that does not bind it. Neither
reference gate accepts an empty configuration (the rules gate needs a
`ruleset_id`, the RAG gate a `template_id`), so today a reasoner must bind
either one for it to run.

Every submission carries one `GateContext` per bound gate, recorded ones
too: a recorded decision's contexts are what shadow evaluation re-runs its
advisory gates on ([Evaluation](../evaluation.md#shadow)). A missing context
is `422 GATE_CONTEXT_MISSING`; a context that fails the gate's
`config_model` is `422 GATE_CONFIG_INVALID`, and one the gate refuses for
this reasoner is `422 GATE_CONFIG_UNAUTHORIZED`. All of this is checked
before any gate runs.

## How the ledger combines gates

The ledger runs the mandatory gates concurrently, then, on a gated decision,
the advisory gates concurrently. Gates are independent: each sees the same
submission and its own context, never another gate's output.

- **Most restrictive wins.** The final action is the most restrictive of the
  reasoner's proposal and every gate's enforced action, over
  ALLOW < CHALLENGE < HOLD < BLOCK (`core.gate.combinator.most_restrictive`).
  The combinator is fixed, associative, and commutative, so no gate can relax
  another and evaluation order cannot change the result. Decision-level rules
  apply on top of it: on a gated decision about a novel entity, the advisory
  gates' verdicts are held for review (HOLD, or BLOCK when an advisory gate's
  own adversarial-probe tier fired); mandatory gates, bypass-rule floors, and
  the proposal still count.
- **Fail closed.** A gate that raises, runs past its budget, returns an
  output the ledger cannot use or store, or is not composed in the
  deployment is recorded as failed, and its enforcement holds the decision
  (HOLD). The decision is still written.
- **Short-circuit.** When a mandatory gate's enforced action is already
  BLOCK, the advisory gates are skipped (BLOCK absorbs, so they cannot change
  the result). The reasoner's own proposal never skips them.

The bundle records one `GateRecord` per gate in the set, with its status:

| Status | Meaning |
|---|---|
| `ran` | The gate returned a usable output; its gate-level enforcement is recorded. |
| `skipped` | The ledger did not invoke it: an advisory gate on a recorded decision, or after a mandatory BLOCK. The reason says which. |
| `failed` | The gate produced no usable output; the reason says why, and its enforcement is the fail-closed HOLD. |

Replay re-runs enforcement on the stored gate outputs and statuses under the
registration snapshot the bundle cites. It never re-invokes a gate.

## The reference gates

Both ship as uv workspace members under `gates/`, installed by the root
`gates` dependency group:

- **The rules gate** (`gates/rules`, distribution
  `decisionledger-gate-rules`, package `rules_gate`, `gate_id` `rules`):
  zero infrastructure, deterministic. It evaluates deployment-registered rule
  sets of predicates over a submission's `label_value` and `feature_set`. See
  [`gates/rules/README.md`](https://github.com/charleskelley/decision-ledger/blob/main/gates/rules/README.md)
  and the [`rules_gate` reference](../../reference/gates/rules.md).
- **The RAG policy gate** (`gates/policy_rag`, distribution
  `decisionledger-gate-policy-rag`, package `policy_rag_gate`, `gate_id`
  `policy`): hybrid retrieval over the submitting reasoner's policy corpus
  and a structured LLM verdict. See [Policy gate](policy/index.md).

`tests/_fake_gate` is a test-only gate (scriptable outcomes, a
deterministic variant, and a lookup-style variant) that framework tests
compose instead of either member.

## Discovering a deployment's gates

`GET /api/v1/gates` lists every composed gate: its `gate_id`, `kind`,
whether it is `deterministic`, whether the deployment mandates it, and the
JSON schema of the `gate_config` it accepts (`config_schema`, `null` when the
gate declares no `config_model`). A reasoner that calls the ledger over HTTP
builds its gate contexts from that schema without the gate's code.

## In this section

- **[How to implement a gate](implementation.md)**: the contract, what the
  framework checks, enforcement, owning infrastructure and eval, with both
  reference gates as worked examples. The step list is the
  [new-gate checklist](../reasoners/reasoner-handoff.md#new-gate-checklist).
- **[Policy gate](policy/index.md)**: the RAG policy gate, its configuration,
  retrieval, prompts, and the [policy corpus](policy/policy-corpus.md)
  format.
