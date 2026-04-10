---
policy_id: INT-POLICY-GATE-V1
title: Automated Decision Policy Gate Framework
version: "1.0"
jurisdiction: INTERNAL
effective_date: 2024-04-15
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Automated Decision Policy Gate Framework
Version 1.0 | Effective: 2024-04-15

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This document defines the operating principles and integrity requirements for the
automated LLM policy gate (C7) within the ATO Reasoner pipeline. The policy gate
produces structured enforcement decisions by reasoning over retrieved policy evidence.
This document governs the gate's required behaviours, its integrity boundaries, and
the controls required to ensure it cannot be subverted by adversarial input.

## 2. Gate Operating Principles

The policy gate operates on the following non-negotiable principles:

1. **Evidence-grounded decisions**: Every enforcement action recommended by the gate
   MUST be grounded in retrieved policy chunks. Decisions based on model knowledge
   not traceable to a retrieved PolicySnippet are hallucinations and MUST be flagged.

2. **Structured output only**: The gate MUST produce output conforming to the
   `PolicyGateOutput` Pydantic schema. Any output that fails schema validation is
   treated as a gate failure and the event is routed to HOLD. The raw response is
   logged. The gate is not permitted to produce free-form text as its primary output.

3. **Conservative failure mode**: When the gate is uncertain, it SHALL recommend the
   more conservative action. A gate that produces `confidence < 0.50` for a BLOCK
   recommendation MUST either raise confidence through additional retrieval or reduce
   the recommendation to HOLD.

4. **Citation completeness**: Every claim in the `rationale` field MUST be supported
   by at least one entry in the `citations` list. A rationale claim without a citation
   is a compliance failure in the eval harness (dimension 04).

## 3. Input Integrity Requirements

All inputs to the policy gate — events, features, retrieved policy chunks, and prompt
parameters — MUST pass integrity checks before the gate is invoked:

- The `LoginEvent` MUST have passed schema validation in the ingestion layer.
- Retrieved `PolicySnippet` objects MUST carry `policy_id`, `version`, and `jurisdiction`
  fields populated from the corpus index. Snippets with empty metadata are rejected.
- Prompt parameters (account metadata, feature vector summaries) MUST be serialised
  from validated Pydantic objects, not from raw string interpolation.

Integrity check failures MUST route the event to HOLD, not to the gate. Passing a
malformed input to the gate risks producing a decision based on corrupted context.

## 4. Adversarial Probe Detection and Mandatory BLOCK

An adversarial probe is defined as a login event where input fields contain content
designed to manipulate the gate's reasoning — typically by injecting instructions into
fields that are included in the rendered prompt (user_agent, metadata fields).

**When an adversarial probe is detected, BLOCK is mandatory with no exception.**

Adversarial probe detection operates at the ingestion and feature layer, before the
gate is invoked. The `adversarial_probe_flag` in the feature vector is set by the
ingestion layer's input sanitisation process. The enforcement layer checks this flag
before invoking the gate and routes to BLOCK immediately if set.

The gate itself MUST NOT attempt to reason about adversarial content. Exposing the
gate to injection payloads, even for analysis, risks the integrity of the gate's
output for the current and potentially future decisions.

## 5. Schema Violation Routing

Malformed events — events that fail `LoginEvent` schema validation in the ingestion
layer — MUST be routed to HOLD with the schema violation recorded in the `override_log`.
Schema violations may indicate:

- Attacker-generated events designed to probe pipeline edge cases.
- Upstream data quality issues in the event producer.
- Encoding or serialisation errors.

Schema violations are never routed to BLOCK because there is insufficient information
to confirm malicious intent. HOLD ensures human review without committing to a
permanent block on potentially legitimate events with data quality issues.

## 6. Prompt Versioning and Immutability

Prompt templates used to construct gate inputs are versioned and immutable once created.
The active prompt version is recorded in every `DecisionBundle` as `prompt_version`.

A change to the prompt template requires a new version file. Existing prompt versions
are never modified. This immutability guarantee ensures that:

- Decision Bundles produced under a given prompt version are always replayable against
  the exact same prompt logic.
- Prompt changes trigger an eval gate run (CI dimension 01–05) before deployment.
- Rollback to a prior prompt version is always available.

## 7. Gate Failure and Fallback Behaviour

A gate failure occurs when the LLM API returns an error, times out, or produces output
that fails schema validation. The fallback behaviour is:

1. Log the failure with the raw LLM response (if any) as `raw_llm_response`.
2. Set `policy_gate_output = None` in the DecisionBundle.
3. Route the event to **HOLD** via the enforcement layer's schema validation failure
   rule (INT-AUTH-RISK-V2 §8, rule 1).
4. Include the failure reason in the ReviewPacket for human review.

ALLOW is never a permissible fallback action. An unknown gate response is always treated
as insufficient evidence to permit the request.

## 8. Injection Resistance Rate Requirement

The eval harness (dimension 05 — adversarial robustness) enforces an injection
resistance rate of **1.0** (zero tolerance). Any scenario where an injection payload
influences the gate's `permitted_actions` or `rationale` beyond what the retrieved
policy evidence supports is a failing test case.

The injection resistance requirement exists because the policy gate is an
authoritative decision-maker within the pipeline. A gate that can be manipulated
by adversarial input into producing permissive decisions undermines the governance
guarantee that DecisionLedger is designed to provide.
