---
policy_id: SOC2-CC6-ACCESS
title: SOC 2 CC6 — Logical and Physical Access Controls
version: "2017"
jurisdiction: INTERNAL
effective_date: 2017-05-01
supersedes: null
risk_tier: null
document_type: STANDARD
---

# SOC 2 — Common Criteria 6 (CC6)
Logical and Physical Access Controls

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of the AICPA SOC 2
Trust Services Criteria, Common Criteria 6 (Logical and Physical Access Controls),
for the DecisionLedger reference implementation. SOC 2 Trust Services Criteria are
proprietary AICPA materials. This adaptation is not a verbatim reproduction.*

## 1. CC6.1 — Access Control Policy and Authentication

The entity implements logical access security software, infrastructure, and architectures
over protected information assets to protect them from security events to meet the
entity's objectives. For authentication systems:

- Access controls must restrict access to authorised users or system processes based
  on defined and documented criteria.
- Authentication mechanisms must verify the identity of users prior to granting access.
- The authentication process must be designed to prevent automated or manual bypass.

SOC 2 auditors assess whether the authentication controls in place are effective —
not merely whether they are documented. Detection systems that fail to catch known
attack patterns (credential stuffing, impossible travel, device rotation) are evidence
of control failure, regardless of the existence of a written policy.

## 2. CC6.2 — Authentication for New and Returning Users

The entity creates credentials for new and returning users based on an authorization
from an appropriate individual or program. For online accounts:

- New account creation must include identity verification appropriate to the risk
  profile of the account.
- Returning users must be authenticated through controls proportionate to the risk
  of the access attempt.
- The authentication controls applied to a returning user must account for changes
  in risk context since the previous authentication (new device, new location,
  velocity deviation).

For novel entities (new accounts with sparse history), the authentication controls
must account for the absence of a baseline. The inability to compare against a
baseline increases the risk of both false positives (blocking legitimate new customers)
and false negatives (permitting attackers who created fresh accounts).

## 3. CC6.3 — Role of the HOLD Mechanism

The entity removes access to protected information assets when appropriate. For
authentication risk systems, the HOLD mechanism serves as the primary suspension
control: when risk signals indicate that the legitimacy of the current access cannot
be determined automatically, access is suspended pending human review.

SOC 2 CC6.3 requires that the entity demonstrate it has controls for removing access
when access is no longer appropriate. HOLD followed by human review and resolution
(either ALLOW or BLOCK) is the operational implementation of this control for real-time
access decisions. The audit trail in the DecisionBundle provides the evidence required
by the SOC 2 auditor.

## 4. CC6.4 — Access Monitoring and Anomaly Detection

The entity monitors logical access controls to address the risk of access to protected
information assets. Monitoring controls must be capable of:

- Detecting access attempts that deviate from established patterns.
- Identifying access from unexpected sources (geographic, device, network).
- Alerting on patterns consistent with automated attacks.

SOC 2 auditors evaluate the effectiveness of monitoring controls by examining whether
the entity detects attack attempts in a timely manner and whether the monitoring
generates actionable output. A monitoring system that produces alerts but does not
result in enforcement actions is not an effective control.

## 5. CC6.6 — External Access Controls

The entity implements controls to prevent and detect unauthorized access from the
external environment. For internet-facing authentication systems:

- Controls must be applied at the ingestion point before events are processed by
  the risk assessment pipeline.
- Schema validation at ingestion prevents malformed events from reaching risk
  scoring and policy reasoning components.
- Rate limiting and IP filtering at the perimeter limit the volume of attack traffic
  that reaches the authentication layer.

Schema validation failure routing (INT-AUTH-RISK-V2 §4.3, INT-POLICY-GATE-V1 §5)
is a direct implementation of CC6.6: malformed inputs are intercepted before they
can influence enforcement decisions.

## 6. CC6.8 — Detecting and Responding to Anomalous Events

The entity detects and responds to anomalous events. For authentication risk systems,
the response to anomalous events must be:

- **Timely**: The response must be applied within the same authentication session, not
  detected in batch processing after the fact.
- **Proportionate**: The response must match the severity of the anomaly — CHALLENGE
  for ambiguous signals, HOLD for corroborated signals, BLOCK for definitive signals.
- **Documented**: The response and the basis for it must be recorded in an audit trail
  that supports post-incident analysis and SOC 2 audit.

The DecisionBundle structure satisfies CC6.8 documentation requirements: every
enforcement action is recorded with the signals, the policy citations, the gate output,
and the final action in a single immutable audit record.
