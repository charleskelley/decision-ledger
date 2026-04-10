---
policy_id: INT-CRED-STUFF-V2
title: Credential Stuffing Response Procedure
version: "2.0"
jurisdiction: INTERNAL
effective_date: 2024-04-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Credential Stuffing Response Procedure
Version 2.0 | Effective: 2024-04-01

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This procedure defines the ATO Reasoner pipeline's response to credential stuffing
attacks — automated campaigns that test large volumes of username/password combinations
sourced from prior data breaches against live accounts. Credential stuffing is the
primary mechanism for account takeover at scale and must be detected and blocked rapidly
to limit account compromise.

The credential stuffing signature is a compound signal combining failure rate, velocity,
device rotation, geographic spread, and ASN classification. No single signal alone is
definitively characteristic of stuffing; the signature is defined by the combination
of signals described in §2.

## 2. Regulatory Basis

Response obligations are grounded in:

- **GLBA-SAFEGUARDS**: Financial institutions must implement safeguards to detect and
  respond to unauthorised access attempts.
- **FFIEC-AUTH-GUIDANCE**: Layered controls must include anomaly detection capable of
  identifying high-volume automated access.
- **CISA-ATO-PREVENTION**: Velocity-based detection and immediate blocking of credential
  stuffing campaigns is a mandatory control for financial services.

## 3. Credential Stuffing Attack Signature

The credential stuffing signature is defined by the simultaneous presence of the
following signals:

| Signal | Threshold | Weight |
|---|---|---|
| Authentication failure rate (10-min window) | > 70% | Primary |
| Per-IP velocity (1-min window) | > 60 req/min | Primary |
| Cross-account spread (10-min window) | > 50 distinct accounts | Primary |
| Device fingerprint rotation | Each event has score < 0.20 | Corroborating |
| Inter-event timing regularity | Jitter < 0.1 (bot-like) | Corroborating |
| Known-bad ASN | Any | Corroborating |

**BLOCK is mandatory when all three primary signals are simultaneously present**,
regardless of any individual account's risk score or tier. This rule fires in the
enforcement layer as priority-order rule 5 per INT-AUTH-RISK-V2 §8.

When two of three primary signals are present, events are escalated to ROUTE_TO_GATE
for policy gate review. The policy gate should flag `escalate_to_human=True` for
cross-account attacks requiring coordinated response beyond individual account decisions.

## 4. Failure Rate Controls

Authentication failure rate is the highest-confidence single signal for credential
stuffing, as legitimate users rarely fail authentication at rates above 30%.

| Failure rate (10-min window) | Action |
|---|---|
| ≤ 30% | No failure rate signal |
| 31–50% | Elevated — contributes to ROUTE_TO_GATE band |
| 51–70% | High — mandatory ROUTE_TO_GATE; evaluate against §3 signature |
| > 70% | Very high — apply §3 mandatory BLOCK when combined with velocity and spread |

A failure rate above 70% from a known-bad ASN triggers BLOCK independently, without
requiring the full three-signal credential stuffing signature. See INT-VELOCITY-V1 §4.1.

## 5. Automated Traffic Discrimination

Human users produce irregular inter-event timing (jitter > 0.2 in the generator model).
Automated credential stuffing tools produce highly regular timing (jitter < 0.1) because
they iterate through credential lists at a fixed rate.

Inter-event timing regularity is a corroborating signal that increases confidence in
the credential stuffing classification. When timing regularity (jitter < 0.1) is
combined with a failure rate above 50%, it shifts the evaluation toward the BLOCK band
even when cross-account spread is below the 50-account primary threshold.

This signal is computed by the online feature layer as a jitter coefficient over a
sliding window of events from the same source cluster.

## 6. Known-Bad ASN Infrastructure

Credential stuffing campaigns increasingly use commercial residential proxy networks
and compromised ASN infrastructure to distribute requests and evade IP-based blocking.
The known-bad ASN classification captures IP ranges associated with proxy and VPN
services commonly used in prior attack campaigns.

Known-bad ASN classification triggers elevated scrutiny regardless of per-event signals:

- Events from known-bad ASNs are routed to ROUTE_TO_GATE regardless of risk score band.
- Failure rate above 50% from a known-bad ASN → BLOCK per INT-VELOCITY-V1 §4.1.
- A cluster of events from multiple known-bad ASNs targeting the same accounts is
  treated as a coordinated campaign and triggers escalation to INT-INCIDENT-ATO-V1.

## 7. Multi-Account Attack Escalation

Credential stuffing attacks targeting more than 200 distinct accounts in a 10-minute
window SHALL trigger incident-level response under INT-INCIDENT-ATO-V1 §3. At this
scale, per-account BLOCK decisions are insufficient; coordinated infrastructure-level
response is required.

The per-account BLOCK decisions continue to fire for individual events during the
incident response process. The incident response trigger is an additional, parallel
escalation path — not a replacement for per-account enforcement.

## 8. Post-Attack Account Review

Accounts that returned SUCCESS outcomes during a confirmed credential stuffing campaign
are at elevated risk of post-breach account takeover. When a campaign is confirmed:

- All accounts with SUCCESS outcomes during the campaign window SHALL be flagged for
  proactive review.
- The review packet SHALL include the campaign attribution, the specific event_ids that
  produced SUCCESS during the window, and the recommended response action.
- The review SLA for campaign-related SUCCESS accounts is elevated to Priority 1 per
  INT-HOLD-QUEUE-V1 §4.
