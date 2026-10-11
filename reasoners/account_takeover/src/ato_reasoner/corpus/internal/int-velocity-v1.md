---
policy_id: INT-VELOCITY-V1
title: Velocity and Rate Limiting Policy
version: "1.3"
jurisdiction: INTERNAL
effective_date: 2024-02-15
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Velocity and Rate Limiting Policy
Version 1.3 | Effective: 2024-02-15

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy establishes velocity thresholds and rate limiting controls for login event
evaluation. Velocity — the rate of authentication events over a sliding time window —
is a primary indicator of automated attack activity. High velocity from a single source
is characteristic of credential stuffing, brute-force, and account enumeration attacks.

This policy defines per-account, per-IP, per-ASN, and cross-account velocity thresholds,
burst detection rules, and the enforcement actions triggered by threshold violations.
Legitimate high-velocity exemptions for enterprise and service accounts are documented
separately in INT-ENT-AUTH-V1.

## 2. Velocity Measurement Definitions

All velocity measurements use a sliding window computed by the online feature layer.
Window sizes are calibrated to the attack pattern being detected:

| Metric | Window | Description |
|---|---|---|
| Per-account velocity | 1 minute | Events for a single account_id from any source |
| Per-IP velocity | 1 minute | Events from a single source IP across all accounts |
| Per-ASN velocity | 5 minutes | Events from a single ASN across all accounts |
| Cross-account spread | 10 minutes | Distinct account_ids targeted from a source cluster |
| Failure rate | 10 minutes | Ratio of FAILURE outcomes to total events per source |
| Burst intensity | 30 seconds | Peak velocity relative to 5-minute baseline |

## 3. Per-Account Velocity Thresholds

Per-account velocity measures how frequently a single account is being targeted,
regardless of the source IP.

| Per-account velocity | Action |
|---|---|
| ≤ 5 req/min | No velocity signal |
| 6–30 req/min | Elevated velocity flag — contributes to ROUTE_TO_GATE band |
| > 30 req/min | High velocity flag — contributes to FAST_PATH_BLOCK band when combined with other signals |

Per-account velocity above 30 req/min without corroborating signals (new device,
failure rate above 30%, or new geography) does not independently trigger BLOCK. The
high-velocity legitimate scenario (INT-ENT-AUTH-V1 §3.1) demonstrates that a single
service account may legitimately exceed this threshold via SSO.

## 4. Per-IP and Per-ASN Thresholds

Per-IP velocity measures how many requests originate from a single address, indicating
the breadth of attack activity from that source.

| Per-IP velocity | Action |
|---|---|
| ≤ 10 req/min | No IP velocity signal |
| 11–60 req/min | Elevated IP velocity — contributes to ROUTE_TO_GATE band |
| > 60 req/min | High IP velocity — escalation required per §4.1 below |

**§4.1 Known-Bad ASN Rule:** When a login event originates from an ASN classified as
known-bad (identified proxy or VPN infrastructure associated with prior attacks), the
following rule applies regardless of per-IP velocity:

- Known-bad ASN AND failure rate > 50% → **BLOCK is mandatory**.
- Known-bad ASN AND failure rate ≤ 50% → Escalate to ROUTE_TO_GATE.

Known-bad ASN classification is maintained by the risk operations team and updated
continuously from threat intelligence feeds.

## 5. Burst Detection

A burst event is a short period of significantly elevated velocity that may represent
an automated attack executing a rapid credential test sequence.

A burst is detected when: velocity over a 30-second window is **≥ 3× the 5-minute
baseline velocity** for that source, AND the burst duration exceeds **15 seconds**.

When a burst is detected:

- Burst from a single IP with failure rate > 30% → elevate the event to
  ROUTE_TO_GATE regardless of risk score band.
- Burst from a single IP with failure rate > 60% → apply credential stuffing
  signature evaluation per INT-CRED-STUFF-V2 §2.
- Burst from multiple IPs in the same ASN → apply per-ASN thresholds in §4.

Burst events that fall below the failure rate thresholds above are logged but do not
independently trigger action escalation. Legitimate service accounts (INT-ENT-AUTH-V1)
may produce burst patterns during batch processing.

## 6. Cross-Account Velocity (Multi-Account Attack Detection)

Cross-account velocity measures how many distinct accounts are targeted from a source
cluster in a time window. This signal distinguishes targeted account attacks (one or
few accounts) from credential stuffing campaigns (many accounts).

| Distinct accounts in 10 min from source | Signal |
|---|---|
| 1–5 | Low — targeted attack or legitimate multi-account management |
| 6–50 | Moderate — possible credential stuffing, requires combined signal evaluation |
| > 50 | High — credential stuffing campaign; apply INT-CRED-STUFF-V2 |
| > 200 | Critical — large-scale campaign; escalate to incident response per INT-INCIDENT-ATO-V1 |

Cross-account spread above 50 accounts SHALL trigger INT-CRED-STUFF-V2 evaluation
regardless of per-IP or per-account velocity levels.

## 7. Legitimate High-Velocity Exemptions

Certain account types produce legitimately elevated velocity that would otherwise
trigger BLOCK or HOLD. The following exemptions apply:

**Service accounts with established SSO pattern**: Accounts authenticated via SSO
(auth_method = SSO) with a stable device fingerprint, fixed IP, and consistent
geographic origin are eligible for velocity exemption. The exemption applies when
the behavioral baseline over the preceding 30 days shows a consistent pattern
above the per-account velocity threshold. See INT-ENT-AUTH-V1 §3 for the full
exemption criteria.

**API clients under enterprise agreement**: Enterprise accounts with contractual
velocity thresholds defined in their service agreement. Custom thresholds take
precedence over the defaults in this policy. The custom threshold SHALL be recorded
in the account metadata and honored by the feature layer.

Exemption eligibility is determined at account enrollment, not at evaluation time.
Accounts not explicitly classified as service accounts or enterprise accounts are
subject to standard thresholds.

## 8. Velocity Override Logging

Every velocity threshold evaluation — including evaluations that did not trigger a
threshold — SHALL be recorded in the `override_log` of the DecisionBundle. The log
entry SHALL include: the metric name, the measured value, the threshold, and whether
the threshold was exceeded.

This logging requirement ensures that velocity signals are auditable in the event of
a false positive challenge by the account holder.
