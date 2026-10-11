---
policy_id: INT-MFA-REQ-V2
title: Multi-Factor Authentication Requirement Policy
version: "2.0"
jurisdiction: INTERNAL
effective_date: 2024-02-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Multi-Factor Authentication Requirement Policy
Version 2.0 | Effective: 2024-02-01

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy defines when multi-factor authentication (MFA) is mandatory, when it is
recommended, and when it is exempted. MFA is the primary friction control applied by
the CHALLENGE action. This policy governs which MFA methods are acceptable for each
account risk context and what constitutes successful MFA completion.

## 2. Regulatory Basis

- **NYDFS-23NYCRR-500 §500.12**: Multi-factor authentication is required for remote
  access to internal systems and for customer-facing privileged actions.
- **NIST-SP-800-63B §4**: Authenticator Assurance Level 2 (AAL2) requires possession
  of a physical authenticator or a software authenticator bound to a specific device.
- **PCI-DSS-V4-REQ8 §8.4**: MFA is required for all access to the cardholder data
  environment and for all remote administrative access.

## 3. Mandatory MFA Conditions

MFA MUST be applied (CHALLENGE action required) in the following conditions:

- Device consistency score in [0.35, 0.69] — ambiguous device per INT-DEVICE-FP-V1 §4.
- First login from a new country for an established account.
- Novel entity with all §4.2 mitigating conditions met (CHALLENGE preferred over HOLD
  per INT-NOVEL-ENTITY-V1 §4.2).
- High-value account accessing from any new device, regardless of consistency score,
  per INT-HVA-POLICY-V2 §3.
- Password-only authentication method when any risk signal is elevated above baseline.

## 4. Acceptable MFA Methods by Risk Context

| Risk context | Acceptable MFA methods | Minimum AAL |
|---|---|---|
| Standard CHALLENGE (device anomaly) | MFA_TOTP, MFA_PUSH | AAL2 |
| Standard CHALLENGE (new geography) | MFA_TOTP, MFA_PUSH | AAL2 |
| Novel entity CHALLENGE | MFA_TOTP, MFA_PUSH | AAL2 |
| High-value account any CHALLENGE | MFA_TOTP, MFA_PUSH, hardware key | AAL2+ |
| Enterprise SSO re-authentication | SSO step-up per INT-ENT-AUTH-V1 | AAL2 |

SMS one-time passwords are not listed as acceptable methods. SMS-based OTP is
deprecated for financial services authentication per NIST-SP-800-63B §5.1.3.2.

## 5. MFA Exemptions

The following conditions exempt an event from MFA even when a CHALLENGE would
otherwise apply:

- **Established SSO pattern**: Enterprise accounts with a confirmed SSO behavioral
  baseline are exempt from device-anomaly CHALLENGE per INT-ENT-AUTH-V1 §3.1. The
  SSO authentication itself satisfies the MFA requirement at enrollment.
- **PASSKEY authentication**: Accounts using FIDO2/WebAuthn passkey authentication
  (auth_method = PASSKEY) are exempt from additional MFA, as passkey authentication
  inherently satisfies AAL2 requirements per FIDO2-WEBAUTHN-CRED §3.

Exemptions do not apply when impossible travel, HOLD conditions, or BLOCK conditions
are active. Exemptions reduce friction for legitimate users; they do not reduce the
mandatory security posture for high-risk signals.

## 6. Failed MFA Handling

When an account holder fails MFA during a CHALLENGE:

- **First and second failure**: Allow retry. Log the failure event with reason code.
- **Third consecutive failure** within a 10-minute window: Escalate to HOLD per
  INT-AUTH-RISK-V2 §6. Notify the account owner via registered contact channel.
- **Failure combined with impossible travel or known-bad ASN**: Escalate directly to
  BLOCK, bypassing the three-attempt threshold.

Failed MFA attempts are counted per session. A new session (new device, new IP, or
time gap > 30 minutes) resets the failure counter.

## 7. MFA Completion and Baseline Update

Successful MFA completion during a CHALLENGE event SHALL trigger the following updates:

- If the CHALLENGE was due to ambiguous device (score 0.35–0.69): Update the device
  fingerprint baseline for the account to include the new device context.
- If the CHALLENGE was due to new geography: Record the new country as a known
  geography for the account.
- If the CHALLENGE was for a novel entity: Increment the event history count. This
  contributes toward graduation from novel entity status per INT-NOVEL-ENTITY-V1 §6.

Baseline updates are applied by the feature layer after the enforcement decision is
logged and must not retroactively alter the current DecisionBundle.
