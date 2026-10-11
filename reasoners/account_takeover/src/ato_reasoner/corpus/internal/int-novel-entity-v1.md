---
policy_id: INT-NOVEL-ENTITY-V1
title: Novel Entity Risk Controls
version: "1.0"
jurisdiction: INTERNAL
effective_date: 2024-03-15
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Novel Entity Risk Controls
Version 1.0 | Effective: 2024-03-15

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy defines how the ATO Reasoner pipeline evaluates and responds to login events
from novel entities — accounts with insufficient event history to establish a reliable
behavioral baseline. Novel entity controls exist because risk scoring models require a
minimum history to produce reliable outputs; a low risk score for a novel entity reflects
model uncertainty, not confirmed safety.

The novel entity rule is an enforcement-layer override that operates independently of
the fast ML scorer. A novel entity with a risk score in the FAST_PATH_ALLOW band must
still be routed to the policy gate, because the scorer's confidence for novel entities
is structurally lower than its stated score.

## 2. Novel Entity Definition

An entity is classified as **novel** when `sparse_history=True` in the computed feature
vector. This flag is set by the online feature layer when the account has fewer than
**10 historical events** in the feature computation window.

The 10-event threshold reflects the minimum event history required for the sliding-window
features (velocity baseline, device consistency history, geographic baseline) to produce
statistically reliable values. Below this threshold, baseline comparisons are unreliable
and anomaly detection is ineffective.

A novel entity designation is **not** a permanent classification. Once an account
accumulates 10 or more events, `sparse_history` is cleared and the account is evaluated
under standard controls. The feature layer recomputes this flag on every event.

## 3. Mandatory Gate Routing

Regardless of risk score, all events from novel entities MUST be routed to the LLM
policy gate (ROUTE_TO_GATE). The fast path (FAST_PATH_ALLOW and FAST_PATH_BLOCK) is
bypassed entirely.

This rule is implemented in the enforcement layer as priority-order rule 3 per
INT-AUTH-RISK-V2 §8 and cannot be overridden by tier-specific policy.

The rationale for mandatory gate routing: the policy gate reasons over retrieved policy
chunks and produces a contextualised, citable decision. For novel entities, the gate's
reasoning incorporates the account's sparse history as a primary risk signal, producing
a more appropriate action than the scorer's band assignment.

## 4. Routing Rules: HOLD vs. CHALLENGE

Novel entity events are routed to either HOLD or CHALLENGE depending on the presence
or absence of corroborating risk signals.

### 4.1 Novel Entity → HOLD

HOLD is the default action for novel entities. HOLD is required when any of the following
corroborating signals are present:

- Failure rate in the novel entity's event window > 20%.
- New device (device consistency score < 0.35) on a novel entity.
- Geographic anomaly: event origin is in a country associated with elevated fraud rates
  for this account's profile.
- Velocity spike: events arriving faster than 10 req/min for a novel entity.
- Any signal that would independently trigger HOLD for an established entity.

HOLD for a novel entity follows the resolution lifecycle defined in INT-HOLD-QUEUE-V1.

### 4.2 Novel Entity → CHALLENGE

CHALLENGE is permitted (instead of HOLD) for novel entities only when all of the
following mitigating conditions are satisfied:

- Stable device fingerprint (score ≥ 0.70).
- Static geographic origin: login originates from a single country with no impossible
  travel or high-speed transitions.
- Failure rate ≤ 10% in the novel entity's event window.
- Velocity ≤ 5 req/min (below the elevated velocity threshold in INT-VELOCITY-V1 §3).
- The source IP and ASN are not on the known-bad classification list.
- Auth method is not PASSWORD-only (presence of MFA or SSO is a mitigating factor).

When all conditions in §4.2 are met, CHALLENGE is the preferred action over HOLD.
CHALLENGE allows the novel entity to complete step-up authentication and proceed,
while HOLD imposes a complete suspension pending human review. The distinction is
proportionate to the risk presented.

## 5. Gate Reasoning Guidance for Novel Entities

When the policy gate receives a novel entity event, the retrieved policy context
should include this document and INT-AUTH-RISK-V2. The gate's rationale SHALL
explicitly address:

1. The sparse_history flag and its implications for scorer reliability.
2. Whether §4.2 mitigating conditions are satisfied.
3. The enforcement rule selected (CHALLENGE or HOLD) and the specific signals that
   determined the choice.

The gate MUST cite this document or INT-AUTH-RISK-V2 in its `citations` list when
routing a novel entity event.

## 6. Novel Entity Graduation

Once a novel entity accumulates 10 historical events, the account graduates to
standard evaluation. The transition is automatic — the feature layer clears
`sparse_history` on the event that crosses the threshold.

Graduation does not retroactively clear any HOLD decisions made during the novel
entity period. Existing HOLDs in the review queue are processed by human review as
normal.

## 7. Interaction with Tier-Specific Controls

Novel entity controls apply to STANDARD and HIGH_VALUE tier accounts. For ENTERPRISE
tier accounts, novel entity routing is modified per INT-ENT-AUTH-V1 §4: enterprise
accounts with a confirmed service agreement and pre-established behavioral baseline
at enrollment are not subject to the novel entity HOLD rule, provided the SSO method
is used and the baseline was verified at account setup.
