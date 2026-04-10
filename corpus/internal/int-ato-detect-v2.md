---
policy_id: INT-ATO-DETECT-V2
title: Account Takeover Detection and Response Policy
version: "2.0"
jurisdiction: INTERNAL
effective_date: 2024-01-01
supersedes: INT-ATO-DETECT-V1
risk_tier: null
document_type: INTERNAL_POLICY
---

# Account Takeover Detection and Response Policy
Version 2.0 | Effective: 2024-01-01 | Supersedes: INT-ATO-DETECT-V1

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy defines the ATO Reasoner's detection criteria and mandatory response for
post-breach account takeover — the scenario where an attacker uses valid credentials
obtained from a data breach to access a victim account. Post-breach ATO is distinct
from credential stuffing (INT-CRED-STUFF-V2): stuffing campaigns test many accounts
with low per-account success; post-breach ATO targets specific accounts with high
success rates because the attacker holds confirmed valid credentials.

## 2. Regulatory Basis

- **OCC-ATO-GUIDANCE**: Institutions must implement controls to detect account access
  by unauthorised parties even when valid credentials are presented.
- **FFIEC-AUTH-GUIDANCE**: Risk-based authentication must account for contextual signals
  beyond credential validity, including device and location anomalies.
- **NIST-SP-800-63B §7.1**: Re-authentication and binding verification are required
  when contextual signals indicate possible credential compromise.

## 3. Post-Breach ATO Attack Signature

Post-breach account takeover is characterised by the simultaneous presence of:

1. **High success rate**: Authentication success rate > 70% in the evaluation window.
   This distinguishes ATO from stuffing, where most attempts fail.
2. **New or unknown device**: Device consistency score < 0.35, indicating access from
   hardware not previously associated with the account.
3. **Geographic anomaly**: Login origin deviates significantly from the account's
   established geographic baseline. Impossible travel (speed > 900 km/h) or first
   access from a new country within the evaluation window.
4. **Velocity deviation**: Current per-account velocity exceeds the account's 30-day
   baseline by a factor of ≥ 2.5.

**The combination of signals (1) + (2) + (3) is the definitive post-breach ATO
signature.** When all three are present, HOLD with a populated ReviewPacket is the
minimum required action. BLOCK is required when impossible travel is also detected
(INT-GEO-RISK-V1 §3).

## 4. Response Matrix

| Signals present | Minimum action | Notes |
|---|---|---|
| (1) + (2) + (3) + impossible travel | BLOCK | Mandatory per INT-GEO-RISK-V1 §3 |
| (1) + (2) + (3) | HOLD | ReviewPacket required |
| (1) + (2) only | HOLD | Possible ATO without geo confirmation |
| (1) + (3) only | CHALLENGE | High success + geo anomaly, established device |
| (2) + (3) only | CHALLENGE | New device + geo, normal success rate |
| Any single signal | Per INT-AUTH-RISK-V2 §4 | Isolated signal — standard evaluation |

## 5. Policy Conflict: NIST 800-63B vs. Internal Velocity Thresholds

A known policy conflict exists between NIST-SP-800-63B §7.1 (which recommends immediate
strong re-authentication upon any credential compromise indicator) and the internal
velocity threshold approach in INT-AUTH-RISK-V2 (which uses the ROUTE_TO_GATE band to
avoid false positives from isolated velocity spikes).

When the policy gate retrieves both documents for a post-breach ATO scenario, it MUST
flag this conflict explicitly in its rationale, note that the internal policy (INT-AUTH-RISK-V2)
is the operative control, and recommend `escalate_to_human=True` if the conflict cannot
be resolved within the gate's confidence threshold.

The enforcement layer will produce a HOLD with a conflict annotation in the `override_log`
when a cross-jurisdiction or cross-document conflict is flagged.

## 6. Version Note on Superseded Policy

This document (v2.0) supersedes INT-ATO-DETECT-V1 (v1.0). The primary change in v2.0
is the formal definition of the three-signal ATO signature (§3) and the explicit policy
conflict documentation (§5). Decisions logged under v1.0 used a less precise signature
definition that did not explicitly require all three signals. Replay of v1.0-era bundles
should apply the v1.0 logic, not v2.0.

## 7. Notification and Account Owner Communication

When HOLD is applied for the post-breach ATO signature, the account owner SHALL be
notified via the contact channel registered on the account within the SLA defined in
INT-HOLD-QUEUE-V1 §3. Notification SHALL include:

- The nature of the anomaly detected (new device, new geography, or both).
- The specific action taken (session suspended for review).
- Instructions for the account holder to initiate identity verification if the access
  was legitimate.

BLOCK decisions for impossible travel trigger an immediate notification obligation
without waiting for human review.
