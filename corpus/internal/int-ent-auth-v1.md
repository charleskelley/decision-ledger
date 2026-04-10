---
policy_id: INT-ENT-AUTH-V1
title: Enterprise Account Authentication Policy
version: "1.1"
jurisdiction: INTERNAL
effective_date: 2024-02-15
supersedes: null
risk_tier: ENTERPRISE
document_type: INTERNAL_POLICY
---

# Enterprise Account Authentication Policy
Version 1.1 | Effective: 2024-02-15

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy governs authentication risk controls for ENTERPRISE tier accounts — accounts
held by enterprise customers with contractual service agreements that include negotiated
SLA obligations and custom authentication parameters. Enterprise accounts include service
accounts, API clients, and organizational accounts where automated access patterns are
expected and legitimate.

Enterprise tier accounts present a unique challenge: they often produce event patterns
that would trigger BLOCK or HOLD under standard controls (high velocity, stable SSO,
fixed IP) while being entirely legitimate. This policy defines the modifications to
standard controls that prevent false positives for these accounts.

This policy overrides INT-AUTH-RISK-V2 where specified. All other controls apply.

## 2. Regulatory Basis

- **NIST-CSF-PR-AC**: Access control policies must account for legitimate high-privilege
  and high-frequency system accounts.
- **FFIEC-AUTH-GUIDANCE**: Layered security controls should distinguish between human
  user access and automated system access when calibrating velocity thresholds.
- **SOC2-CC6-ACCESS**: Access controls for service accounts require documented policies
  governing authentication, monitoring, and periodic review.

## 3. SSO-Based Velocity Exemption

Enterprise accounts authenticated via SSO (auth_method = SSO) with an established
behavioral baseline are exempt from per-account velocity BLOCK rules.

### 3.1 Exemption Eligibility Criteria

An enterprise account is eligible for the SSO velocity exemption when:

- Auth method is SSO at every evaluation event (no password fallback).
- The account has an established behavioral baseline: consistent device fingerprint
  (score ≥ 0.90 over the preceding 30 days), fixed or narrow IP range, and stable
  geographic origin.
- The account is explicitly enrolled in the enterprise service agreement with velocity
  thresholds documented in account metadata.
- The account's velocity pattern is within ±50% of the documented baseline velocity.

When eligible, the per-account velocity threshold from INT-VELOCITY-V1 §3 does not
apply. The maximum enforcement action for velocity alone is **CHALLENGE**, not BLOCK.

### 3.2 Exemption Expiry

The SSO velocity exemption expires when:

- A non-SSO authentication event is detected for the account.
- The device fingerprint drops below 0.80 (significant hardware or software change).
- Velocity exceeds the documented baseline by more than 3×.
- Geographic origin changes to a new country (requires re-baseline via CHALLENGE).

Expired exemptions revert the account to standard INT-AUTH-RISK-V2 controls until
re-eligibility is confirmed by the account management system.

## 4. Novel Entity Override for Enterprise Accounts

Enterprise accounts that are pre-enrolled with a documented baseline at service
agreement setup are not subject to the novel entity HOLD rule (INT-NOVEL-ENTITY-V1 §3).

Pre-enrollment baseline documentation SHALL include:

- Expected authentication method (must be SSO).
- Expected velocity range (events per minute, daily peak).
- Expected geographic origin (country and city range).
- Expected device fingerprint range (initial fingerprint hash or component profile).

Accounts without pre-enrollment documentation are subject to standard novel entity
controls until their event history crosses the 10-event threshold.

## 5. Custom Velocity Thresholds

Enterprise service agreements may specify custom per-account velocity thresholds that
supersede INT-VELOCITY-V1 §3 defaults. Custom thresholds:

- MUST be documented in the account metadata and accessible to the feature layer.
- MUST be reviewed and re-confirmed at service agreement renewal (annually).
- CANNOT exceed 10× the standard per-account threshold (30 req/min maximum for the
  standard tier; enterprise maximum is 300 req/min).
- Do not modify per-IP or per-ASN thresholds — these are infrastructure-level controls
  that apply regardless of account tier.

## 6. Monitoring and Periodic Review

Enterprise accounts with active velocity exemptions SHALL be reviewed quarterly by risk
operations to confirm:

- The velocity pattern remains consistent with the documented baseline.
- No CHALLENGE, HOLD, or BLOCK events have occurred that warrant exemption review.
- The SSO integration remains active and is not subject to credential fallback.

Accounts that fail quarterly review have their exemption suspended until the review is
resolved. Suspended accounts revert to standard INT-AUTH-RISK-V2 controls.
