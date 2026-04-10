---
policy_id: INT-HVA-POLICY-V2
title: High-Value Account Security Policy
version: "2.0"
jurisdiction: INTERNAL
effective_date: 2024-03-01
supersedes: null
risk_tier: HIGH_VALUE
document_type: INTERNAL_POLICY
---

# High-Value Account Security Policy
Version 2.0 | Effective: 2024-03-01

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy establishes enhanced security controls for accounts classified as HIGH_VALUE
tier. High-value accounts are subject to stricter risk thresholds, lower BLOCK floors,
and mandatory MFA requirements that override the baseline controls in INT-AUTH-RISK-V2.

An account is classified as HIGH_VALUE when it meets one or more of the following
criteria defined in account metadata:

- Account balance or asset value exceeds the configurable high-value threshold.
- Account is flagged as high-value by the account management system at enrollment.
- Account has been manually elevated to HIGH_VALUE by risk operations.

This policy overrides INT-AUTH-RISK-V2 for all HIGH_VALUE accounts. Where this policy
is silent, INT-AUTH-RISK-V2 applies.

## 2. Regulatory Basis

- **FFIEC-AUTH-GUIDANCE**: Higher-risk transactions require stronger authentication
  controls proportionate to the risk presented.
- **GLBA-SAFEGUARDS**: Safeguards must be calibrated to the sensitivity of the
  customer information at risk.
- **NYDFS-23NYCRR-500 §500.12**: MFA is mandatory for any access to accounts holding
  significant assets.

## 3. Risk Score Thresholds — HIGH_VALUE Override

High-value accounts use a lower FAST_PATH_BLOCK threshold to reduce the window of
time an attacker can operate before being blocked:

| Score band | Routing | Default action |
|---|---|---|
| 0.00–0.34 | FAST_PATH_ALLOW | ALLOW |
| 0.35–0.59 | ROUTE_TO_GATE | Per policy gate output |
| 0.60–1.00 | FAST_PATH_BLOCK | BLOCK |

The ROUTE_TO_GATE band is narrower for HIGH_VALUE accounts (0.35–0.59 vs. 0.35–0.69
for STANDARD). Events that would be FAST_PATH_BLOCK for STANDARD at scores above 0.70
are FAST_PATH_BLOCK for HIGH_VALUE at scores above 0.60.

## 4. Mandatory MFA for Any Device Anomaly

For HIGH_VALUE accounts, MFA (CHALLENGE) is mandatory whenever the device consistency
score falls below 1.0. This is a stricter threshold than the 0.70 stable-device floor
used for STANDARD accounts (INT-DEVICE-FP-V1 §6).

Acceptable MFA methods for HIGH_VALUE accounts are limited to MFA_TOTP, MFA_PUSH, or
hardware security keys (FIDO2/WebAuthn). Password-only authentication is not permitted
for any HIGH_VALUE account session, regardless of device consistency score.

## 5. Enhanced Geographic Controls

High-value accounts apply the high-speed travel zone (INT-GEO-RISK-V1 §4) more
conservatively:

- Speed 300–900 km/h: HOLD is required regardless of device consistency or velocity.
  The CHALLENGE option available to STANDARD accounts is not available for HIGH_VALUE.
- First login from a new country: HOLD is required regardless of device or velocity
  signals. CHALLENGE is not permitted for HIGH_VALUE accounts at new geographies.

The impossible travel BLOCK rule (speed > 900 km/h) applies identically to all tiers.

## 6. Confidence Floor

For HIGH_VALUE accounts, the policy gate confidence floor for ALLOW is raised to 0.75.
If the gate produces a confidence below 0.75 for an ALLOW or CHALLENGE decision, the
enforcement layer SHALL escalate to CHALLENGE or HOLD respectively.

This higher confidence floor reflects the elevated consequence of a false negative
(missed attack) for high-value accounts relative to STANDARD accounts.

## 7. Notification Requirements

All CHALLENGE, HOLD, and BLOCK actions for HIGH_VALUE accounts trigger immediate
notification to the account holder via all registered contact channels (not just
primary). HOLD notifications for HIGH_VALUE accounts include a direct callback number
for the priority review team, bypassing the standard review queue.

The notification SLA for HIGH_VALUE HOLD decisions is 2 hours (vs. 24 hours for
STANDARD accounts per INT-HOLD-QUEUE-V1 §3).
