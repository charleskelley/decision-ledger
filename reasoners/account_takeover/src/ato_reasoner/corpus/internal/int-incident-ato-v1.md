---
policy_id: INT-INCIDENT-ATO-V1
title: Incident Response — Account Takeover Playbook
version: "1.0"
jurisdiction: INTERNAL
effective_date: 2024-03-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Incident Response — Account Takeover Playbook
Version 1.0 | Effective: 2024-03-01

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This playbook defines the incident response procedures triggered when the ATO Reasoner
pipeline identifies a confirmed account takeover or a large-scale credential stuffing
campaign. Individual HOLD and BLOCK decisions are handled by INT-HOLD-QUEUE-V1; this
playbook addresses events that exceed the per-account response threshold and require
coordinated organisational response.

## 2. Incident Triggers

The following conditions trigger incident-level response and escalation beyond the
standard per-account enforcement pipeline:

- **Campaign-level credential stuffing**: Cross-account spread exceeds 200 distinct
  accounts in a 10-minute window (INT-CRED-STUFF-V2 §7).
- **Confirmed post-breach ATO at scale**: More than 10 HOLD decisions with the post-breach
  ATO signature (INT-ATO-DETECT-V2 §3) within a 1-hour window.
- **Adversarial probe at infrastructure level**: Injection payload detected in multiple
  events from different source clusters, indicating coordinated probing of the pipeline.
- **Pipeline integrity failure**: Gate failure rate exceeds 10% in a 30-minute window,
  suggesting infrastructure attack or compromise.

## 3. Escalation Paths

| Trigger | Immediate action | Escalation target | SLA |
|---|---|---|---|
| Campaign stuffing (> 200 accounts) | Block source cluster; create incident ticket | Risk operations + Security | 15 minutes |
| Post-breach ATO at scale | HOLD all affected accounts; notify account owners | Risk operations + Account management | 30 minutes |
| Infrastructure-level probe | Engage security team; preserve pipeline logs | Security operations | Immediate |
| Pipeline integrity failure | Activate fallback mode; suspend automated ALLOW | Engineering + Security | Immediate |

## 4. Fallback Mode

When a pipeline integrity failure is declared, the system enters fallback mode:

- All FAST_PATH_ALLOW decisions are suspended. Events that would be FAST_PATH_ALLOW
  are escalated to ROUTE_TO_GATE.
- If the gate is unavailable, all events are routed to HOLD pending manual review.
- FAST_PATH_BLOCK decisions continue to fire — the block path is preserved even when
  the allow path is suspended.
- The engineering on-call team is notified immediately.

Fallback mode is cleared only when the pipeline integrity failure is resolved and
engineering confirms the system is operating normally.

## 5. Notification Obligations

### 5.1 Account Holder Notification

For confirmed post-breach ATO events (individual or campaign):

- Account holders with HOLD or BLOCK decisions SHALL be notified within 24 hours.
- Notification SHALL include: the nature of the suspicious activity, the action taken,
  and instructions for recovering account access.

### 5.2 Regulatory Notification

Certain ATO events trigger regulatory notification obligations:

- **GLBA-SAFEGUARDS**: Notification to the Federal Trade Commission is required for
  confirmed breaches of customer financial data, within 30 days of discovery.
- **NYDFS-23NYCRR-500**: Notification to the New York Department of Financial Services
  is required within 72 hours of a cybersecurity event likely to affect operations.
- **GDPR-ART32-SECURITY**: Data breaches affecting EU data subjects require notification
  to the relevant supervisory authority within 72 hours.

Regulatory notification decisions are outside the automated pipeline's authority. The
incident response team SHALL assess notification obligations for each declared incident.

## 6. Post-Incident Review

Every declared incident SHALL trigger a post-incident review within 5 business days:

- Timeline reconstruction from DecisionBundle audit records.
- Assessment of pipeline detection latency (time from first attack event to first BLOCK).
- Identification of signals that could have triggered earlier detection.
- Recommended threshold adjustments for INT-AUTH-RISK-V2, INT-VELOCITY-V1, or
  INT-CRED-STUFF-V2 based on the attack pattern observed.

Recommended threshold changes require the policy update process: draft change to the
relevant policy document, eval harness regression test, and approval before deployment.
