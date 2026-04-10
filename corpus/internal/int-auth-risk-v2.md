---
policy_id: INT-AUTH-RISK-V2
title: Authentication Risk Controls Policy
version: "2.1"
jurisdiction: INTERNAL
effective_date: 2024-01-15
supersedes: INT-AUTH-RISK-V1
risk_tier: null
document_type: INTERNAL_POLICY
---

# Authentication Risk Controls Policy
Version 2.1 | Effective: 2024-01-15 | Supersedes: INT-AUTH-RISK-V1

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy establishes the authentication risk controls framework governing how the
ATO Reasoner pipeline evaluates and responds to login events. It defines the action
decision matrix, risk score thresholds, and routing rules that the enforcement layer
applies deterministically to every decision.

This document is the authoritative source for action thresholds within the ATO Reasoner
pipeline. Domain-specific procedures are governed by subordinate policy documents that
reference this master policy for action assignments. Tier-specific overrides are in
INT-HVA-POLICY-V2 (high-value accounts) and INT-ENT-AUTH-V1 (enterprise accounts).

## 2. Regulatory Basis

This policy operationalises the following external requirements:

- **FFIEC-AUTH-GUIDANCE**: Risk-based authentication controls proportionate to the risk
  presented by each event.
- **NIST-SP-800-63B**: Authenticator assurance levels and session controls commensurate
  with risk.
- **GLBA-SAFEGUARDS**: Safeguards to protect customer information from unauthorised
  access.
- **NYDFS-23NYCRR-500**: Multi-factor authentication for access to covered systems.

Where this policy conflicts with any of the above, the more restrictive requirement
SHALL govern.

## 3. Risk Score Thresholds and Fast-Path Routing

The fast ML scorer produces a risk score in [0.0, 1.0] for each event. The following
thresholds determine routing:

| Score band | Routing | Default action |
|---|---|---|
| 0.00–0.34 | FAST_PATH_ALLOW | ALLOW |
| 0.35–0.69 | ROUTE_TO_GATE | Per policy gate output |
| 0.70–1.00 | FAST_PATH_BLOCK | BLOCK |

Fast-path routing bypasses the LLM policy gate entirely. **Exception:** Events with
`sparse_history=True` (novel entities) MUST be routed to the gate regardless of risk
score. See INT-NOVEL-ENTITY-V1 §3.

## 4. Action Decision Matrix

The policy gate produces a `permitted_actions` list. The enforcement layer resolves to
the most conservative permissible action. Severity ordering: ALLOW < CHALLENGE < HOLD
< BLOCK.

### 4.1 ALLOW Criteria

ALLOW is appropriate when: risk score ≤ 0.34 on the fast path, OR the policy gate
permits ALLOW and no mandatory override condition is present. No BLOCK or HOLD
conditions may be active. `sparse_history` must be False, or novel entity conditions
(INT-NOVEL-ENTITY-V1 §4.2) must not be triggered.

### 4.2 CHALLENGE Criteria

CHALLENGE is appropriate when an isolated anomalous signal is present that is
insufficient for HOLD or BLOCK, and proportionate friction is warranted:

- Device consistency score in [0.35, 0.70] with no corroborating attack signals
  (INT-DEVICE-FP-V1 §4).
- Novel entity with clean signals and no corroborating risk indicators
  (INT-NOVEL-ENTITY-V1 §4.2).
- First login from a new country with no velocity or device anomalies.
- SSO client with no established behavioral baseline (INT-ENT-AUTH-V1 §3.2).

### 4.3 HOLD Criteria

HOLD is appropriate when multiple corroborating anomalous signals require human
judgment but do not meet the BLOCK threshold. HOLD is mandatory when:

- `sparse_history=True` AND any corroborating risk signal is present
  (INT-NOVEL-ENTITY-V1 §4.1).
- Device consistency score < 0.35 without impossible travel (impossible travel
  mandates BLOCK per §4.4).
- Policy gate confidence < 0.60 AND the gate's most conservative action is BLOCK.
- The policy gate produces `escalate_to_human=True`.
- A cross-jurisdiction policy conflict is detected.
- Policy gate output fails schema validation — HOLD is mandatory, no exception.

### 4.4 BLOCK Criteria

BLOCK is mandatory and cannot be overridden when:

- Impossible travel is detected: consecutive login speed > 900 km/h
  (INT-GEO-RISK-V1 §3).
- Credential stuffing signature: failure rate > 70% AND velocity > 60 req/min AND
  multi-account spread detected (INT-CRED-STUFF-V2 §3).
- Known-bad ASN detected AND failure rate > 50% (INT-VELOCITY-V1 §4).
- Risk score ≥ 0.70 on the fast path.
- Adversarial probe or schema injection detected (INT-POLICY-GATE-V1 §4).

## 5. Confidence-Based Override

When the policy gate produces a `confidence` value below the thresholds below, the
enforcement layer SHALL escalate:

| Gate confidence | Gate action | Enforcement override |
|---|---|---|
| < 0.50 | ALLOW | Escalate to CHALLENGE |
| < 0.60 | BLOCK | Escalate to HOLD |

Low-confidence BLOCK decisions are escalated to HOLD because an incorrect BLOCK causes
direct customer harm. Human review resolves the hold before the block is enforced.

## 6. Required Controls by Action

The following controls MUST be applied alongside the enforcement action:

- **CHALLENGE**: Log the step-up reason code. Notify the account owner if the challenge
  fails three consecutive times within a 10-minute window.
- **HOLD**: Construct and store a ReviewPacket. Record the triggering enforcement rule
  in the `override_log`. Set review SLA per INT-HOLD-QUEUE-V1 §3.
- **BLOCK**: Log the BLOCK reason code. Increment per-IP and per-account BLOCK counters.
  Evaluate notification obligation under INT-INCIDENT-ATO-V1 §5.

## 7. Override Priority Order

Enforcement override rules are evaluated in the following priority order. The first rule
that fires determines the final action. All evaluations MUST be recorded in `override_log`.

1. Schema validation failure → HOLD (mandatory, no exception)
2. Adversarial probe detected → BLOCK (mandatory, no exception)
3. Novel entity with corroborating signal → HOLD
4. Impossible travel detected → BLOCK (mandatory, no exception)
5. Credential stuffing signature → BLOCK (mandatory, no exception)
6. Low gate confidence + high-risk action → per §5
7. Cross-jurisdiction conflict → HOLD with escalation
8. No rule fired → policy gate `permitted_actions` governs

## 8. Revision History

| Version | Date | Summary |
|---|---|---|
| 1.0 | 2023-06-01 | Initial release. Basic threshold framework. |
| 2.0 | 2023-11-15 | Added confidence-based override (§5). Clarified HOLD mandatory conditions. |
| 2.1 | 2024-01-15 | Added adversarial probe rule. Updated tier references. |
