---
policy_id: INT-HOLD-QUEUE-V1
title: Review Queue and HOLD Processing Policy
version: "1.0"
jurisdiction: INTERNAL
effective_date: 2024-03-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Review Queue and HOLD Processing Policy
Version 1.0 | Effective: 2024-03-01

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy governs the human review process for events routed to HOLD by the
enforcement layer. A HOLD decision suspends the subject request pending asynchronous
human review. This policy defines the SLA for review completion, the criteria for
review resolution, and the actions available to reviewers.

HOLD is a time-bounded state. Every HOLD decision has a defined SLA after which the
default resolution is applied. The HOLD mechanism exists because the pipeline's
confidence is insufficient to take an automated ALLOW or BLOCK decision — human
judgment is required.

## 2. ReviewPacket Contents

Every HOLD decision produces a `ReviewPacket` attached to the `DecisionBundle`. The
ReviewPacket SHALL contain:

- The entity identifier and account metadata.
- The enforcement rule that triggered HOLD and the specific signals that fired it.
- The risk score, scorer confidence, and top SHAP signals.
- The policy gate output (if the gate was invoked), including rationale and citations.
- The account's recent event history summary.
- A recommended action from the policy gate, if produced.

The ReviewPacket is the complete context for the reviewer. Reviewers SHALL NOT access
raw infrastructure state (Redis, database) to supplement the ReviewPacket. The bundle
is the authoritative source of truth for every decision.

## 3. Review SLA by Account Tier and Priority

| Tier | Priority | SLA |
|---|---|---|
| STANDARD | Normal | 24 hours |
| STANDARD | Elevated (campaign-related) | 4 hours |
| HIGH_VALUE | Normal | 2 hours |
| HIGH_VALUE | Elevated | 30 minutes |
| ENTERPRISE | Normal | 4 hours |
| ENTERPRISE | Elevated | 1 hour |

Priority elevation is triggered when:

- The HOLD is related to a confirmed credential stuffing campaign
  (INT-CRED-STUFF-V2 §8).
- Multiple HOLD decisions for accounts with shared signals (coordinated attack pattern).
- The account holder has contacted support to report the suspension.

SLA expiry triggers the default resolution defined in §5.

## 4. Review Resolution Actions

A reviewer resolving a HOLD decision SHALL select one of the following actions:

- **ALLOW**: The reviewer confirms the access is legitimate. The session is released.
  The reviewer SHALL document the basis for ALLOW in the resolution note.
- **BLOCK**: The reviewer confirms the access is suspicious. The session is blocked.
  Account compromise response procedures (INT-INCIDENT-ATO-V1) are triggered.
- **ESCALATE**: The reviewer requires additional information or a second opinion. The
  SLA is extended by 50% of the original SLA. Only one escalation is permitted per
  HOLD — second escalation converts to BLOCK.
- **CONTACT_REQUIRED**: The reviewer attempts to contact the account holder to verify
  identity. SLA is suspended until contact is made or 48 hours elapse (whichever is
  shorter), after which the default resolution applies.

Reviewers SHALL NOT resolve HOLD decisions without recording a resolution note. Empty
resolution notes are rejected by the review queue system.

## 5. SLA Expiry Default Resolution

When a HOLD decision exceeds its SLA without reviewer action:

- **STANDARD tier**: Default to BLOCK. An expired review without resolution is treated
  as a failure to confirm legitimacy.
- **HIGH_VALUE tier**: Default to BLOCK with priority incident notification.
- **ENTERPRISE tier**: Default to CHALLENGE (not BLOCK), given the elevated cost of
  blocking a legitimate enterprise service. The CHALLENGE is applied to the next
  authentication event from the account.

The default resolution is logged as an automated action in the `override_log` with the
reason "SLA_EXPIRY_DEFAULT".

## 6. Audit Requirements

Every HOLD resolution SHALL produce an audit record containing:

- The original `decision_id` of the HOLD bundle.
- The reviewer identifier and timestamp.
- The resolution action selected.
- The resolution note.
- Any additional signals observed by the reviewer.

Audit records for HOLD resolutions are retained for the period specified in
INT-DATA-MIN-GDPR-V1 §4. They are the primary evidence for any regulatory inquiry
into the handling of a specific account access event.
