---
policy_id: INT-HOLD-QUEUE-V1
title: Non-Terminal Action Resolution Policy
version: "1.0"
jurisdiction: INTERNAL
effective_date: 2024-03-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Non-Terminal Action Resolution Policy
Version 1.0 | Effective: 2024-03-01

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy governs the post-decision lifecycle for events whose enforcement
`decision_action` is non-terminal — `CHALLENGE` or `HOLD`. A non-terminal
decision suspends or applies friction to the subject request and requires a
resolution to reach a realized terminal action (`ALLOW` or `BLOCK`). This
policy defines the SLA for resolution completion, the resolver vocabulary,
and the actions available to resolvers.

`HOLD` and `CHALLENGE` are time-bounded states. Every non-terminal decision
has a defined SLA after which the default resolution is applied. Both kinds
exist because the pipeline's decision-time confidence is insufficient for an
automated terminal outcome — additional human or system input is required.

## 2. Resolution Audit Surface

The `DecisionBundle` is the authoritative source of truth for the
decision-time context. It carries the entity identifier, scorer signals,
retrieved policy snippets, gate output (when invoked), enforcement rule, and
override log. Resolvers SHALL read context directly from the bundle; no
secondary "review packet" is maintained.

Resolution outcomes are recorded in the append-only
`decision_resolution_attempts` table as one or more `ResolutionAttempt`
rows per `decision_id`. The realized action of a decision is computed at
read time by folding the attempt chain. See DR-18 for the framework
rationale.

## 3. Resolution SLA by Account Tier and Priority

| Tier | Priority | SLA |
|---|---|---|
| STANDARD | Normal | 24 hours |
| STANDARD | Elevated (campaign-related) | 4 hours |
| HIGH_VALUE | Normal | 2 hours |
| HIGH_VALUE | Elevated | 30 minutes |
| ENTERPRISE | Normal | 4 hours |
| ENTERPRISE | Elevated | 1 hour |

Priority elevation is triggered when:

- The non-terminal decision is related to a confirmed credential stuffing
  campaign (INT-CRED-STUFF-V2 §8).
- Multiple non-terminal decisions for accounts with shared signals
  (coordinated attack pattern).
- The account holder has contacted support to report the suspension or
  challenge.

Priority is a queue/operations concern computed by the review queue from
bundle and decision-context fields; it is not stored in the framework
record. SLA expiry triggers the default resolution defined in §5.

## 4. Resolution Actions

A resolver completing a non-terminal decision SHALL record a
`ResolutionAttempt` whose `resolver_kind`, `status`, and `resolution_action`
encode one of the following outcomes:

- **ALLOW**: The resolver confirms the access is legitimate. The session
  is released. Recorded as `status=COMPLETED`, `resolution_action=ALLOW`.
  The resolver SHALL document the basis for ALLOW in the attempt's `note`.
- **BLOCK**: The resolver confirms the access is suspicious. The session
  is blocked and account compromise response procedures
  (INT-INCIDENT-ATO-V1) are triggered. Recorded as `status=COMPLETED`,
  `resolution_action=BLOCK`.
- **ESCALATE**: The resolver requires additional information or a second
  opinion. Recorded as `status=ESCALATED`, `resolution_action=null`. The
  SLA is extended by 50% of the original SLA. Only one escalation is
  permitted per decision; a second escalation converts to BLOCK at the
  next review.
- **CONTACT_REQUIRED**: The resolver attempts to contact the account
  holder to verify identity. Typically recorded as a `PENDING` attempt
  with `resolver_kind=AUTOMATED_OUTREACH` (or `HUMAN` for manual
  contact); the eventual outcome is recorded as a subsequent attempt.
  SLA is suspended until contact is made or 48 hours elapse (whichever is
  shorter), after which the default resolution applies.

Resolvers SHALL NOT record `COMPLETED` attempts without a `note`. Empty
notes are rejected.

## 5. SLA Expiry Default Resolution

When a non-terminal decision exceeds its SLA without a `COMPLETED` resolution
attempt, the system records a final `ResolutionAttempt` with
`resolver_kind=SLA_DEFAULT`, `resolver_id="system:sla_timer"`, and an
outcome determined by the account tier:

- **STANDARD tier**: Default to BLOCK. An expired resolution without
  completion is treated as a failure to confirm legitimacy.
- **HIGH_VALUE tier**: Default to BLOCK with priority incident notification.
- **ENTERPRISE tier**: Default to CHALLENGE applied to the next
  authentication event from the account, given the elevated cost of
  blocking a legitimate enterprise service. Recorded as a CHALLENGE
  resolution_action; the next event opens its own resolution lifecycle.

The default resolution is logged as an automated attempt with
`note="SLA_EXPIRY_DEFAULT"`.

## 6. Audit Requirements

Every resolution attempt is itself the audit record. A `ResolutionAttempt`
row in `decision_resolution_attempts` SHALL contain:

- The original `decision_id` of the non-terminal decision.
- The resolver identifier (`resolver_kind` + `resolver_id`) and timestamps
  (`started_at`, `completed_at` when applicable).
- The status (`PENDING` / `COMPLETED` / `ESCALATED` / `EXPIRED`) and
  resolution_action (`ALLOW` / `CHALLENGE` / `HOLD` / `BLOCK`, or null
  while still in flight).
- The `note` capturing the resolver's reasoning.
- Any kind-specific evidence in the `evidence` payload (ticket reference,
  challenge id, contact channel, etc.).

Resolution attempt rows are retained for the period specified in
INT-DATA-MIN-GDPR-V1 §4. They are the primary evidence for any regulatory
inquiry into the handling of a specific account access event.
