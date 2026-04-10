---
policy_id: INT-DEVICE-FP-V1
title: Device Fingerprint Anomaly Response Procedure
version: "1.2"
jurisdiction: INTERNAL
effective_date: 2024-02-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Device Fingerprint Anomaly Response Procedure
Version 1.2 | Effective: 2024-02-01

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This procedure governs how the ATO Reasoner pipeline responds to device fingerprint
anomalies detected during login event evaluation. Device fingerprints provide a
persistent, cross-session identifier for the hardware and software environment used to
access an account. Anomalies in the device fingerprint — partial matches, new devices,
and rotating fingerprints — are significant risk signals that must be evaluated in
context with other available signals.

This procedure applies to all accounts across all risk tiers. High-value account
modifications are in INT-HVA-POLICY-V2 §4.

## 2. Device Fingerprint Components

A device fingerprint consists of five colon-separated components that collectively
identify a unique device context:

1. **Hardware identifier**: A hash of hardware characteristics (CPU, memory, storage
   configuration). Stable unless hardware changes.
2. **Browser configuration**: Browser type, version, and extension profile. Changes on
   browser update or browser switch.
3. **Operating system profile**: OS type and version. Changes on OS upgrade.
4. **Screen and display profile**: Resolution, colour depth, and display configuration.
   Stable unless monitor configuration changes.
5. **Timezone and locale**: System timezone and locale settings. Changes when travelling
   across time zones or changing system locale.

A `device_consistency_score` in [0.0, 1.0] is computed by the feature layer. A score
of 1.0 means all five components match the account's established fingerprint. A score
of 0.0 means no components match.

## 3. Consistency Score Bands and Interpretation

| Score band | Interpretation |
|---|---|
| 0.70–1.00 | Stable device — all or most components match |
| 0.35–0.69 | Ambiguous device — partial match, possibly same user on different device or after software update |
| 0.00–0.34 | Unknown device — few or no components match established fingerprint |

The 0.35 and 0.70 boundaries reflect the expected component-match distributions for
common legitimate scenarios. A browser update typically changes one or two components,
keeping the score above 0.35. A complete device change produces a score near 0.0. The
ambiguous band captures transitions between these extremes.

## 4. Ambiguous Device Response (Score 0.35–0.69)

An ambiguous device consistency score indicates a partial fingerprint match. Legitimate
causes include a browser update, OS upgrade, new monitor, or the same user accessing
from a second owned device with similar but not identical configuration.

When `device_consistency_score` is in [0.35, 0.69] AND no corroborating attack signals
are present, the required action is **CHALLENGE**. Corroborating attack signals include:
impossible travel (INT-GEO-RISK-V1 §3), velocity anomaly (INT-VELOCITY-V1 §3), known-bad
ASN (INT-VELOCITY-V1 §4), or failure rate above 40%.

When `device_consistency_score` is in [0.35, 0.69] AND one or more corroborating attack
signals are present, the required action escalates to **HOLD**, pending human review of
the combined signal pattern.

A CHALLENGE for ambiguous device consistency requires the account holder to complete
step-up authentication. The specific step-up method is determined by INT-MFA-REQ-V2.
Successful step-up re-establishes device trust and updates the fingerprint baseline for
that account.

## 5. Unknown Device Response (Score 0.00–0.34)

An unknown device score indicates that the login originates from a device that does not
match the account's established fingerprint in any meaningful way. Legitimate causes
include account access from a public terminal, a new device, or complete hardware
replacement.

When `device_consistency_score` is below 0.35 AND no corroborating attack signals are
present, the required action is **HOLD**. A new device without corroboration warrants
human review before permitting access.

When `device_consistency_score` is below 0.35 AND corroborating signals are present
(impossible travel, high failure rate, velocity spike, or known-bad ASN), the required
action escalates to **BLOCK** per INT-AUTH-RISK-V2 §4.4.

The unknown device threshold of 0.35 acknowledges that geolocation imprecision and
clock skew may cause timezone component mismatches that reduce scores by up to 0.20.
The threshold is therefore not set at 0.0.

## 6. Stable Device Handling (Score 0.70–1.00)

A device consistency score above 0.70 does not independently produce CHALLENGE, HOLD,
or BLOCK. The device signal is treated as a mitigating factor that reduces the weight
of other risk signals in the scorer's output.

An established device history for an account SHALL be used to reduce the risk
contribution of other anomalous signals when evaluating whether to route to CHALLENGE
vs. ALLOW for borderline cases.

## 7. Fingerprint Rotation Detection

A pattern of rapidly rotating device fingerprints across multiple events for the same
account — where each event presents a new device with a score below 0.20, and no two
consecutive events share any fingerprint components — is a strong indicator of attacker
infrastructure cycling device identifiers to evade detection.

Rotating fingerprint detection is a compound signal evaluated by the scorer across the
event window. When the rotating pattern is detected in combination with high velocity
or multi-account spread, it contributes to the credential stuffing signature defined in
INT-CRED-STUFF-V2 §2.

## 8. Interaction with Other Controls

Device fingerprint signals are evaluated in combination with geographic signals
(INT-GEO-RISK-V1) and velocity signals (INT-VELOCITY-V1). The combination of unknown
device + impossible travel + high failure rate constitutes the post-breach ATO signature
in INT-ATO-DETECT-V2 §3.

The device fingerprint signal alone — absent corroborating signals — should not produce
BLOCK. Proportionate response to device anomaly is CHALLENGE for ambiguous scores and
HOLD for unknown scores.
