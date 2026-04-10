---
policy_id: NIST-SP-800-63B
title: NIST SP 800-63B — Digital Identity Guidelines, Authentication
version: "3.0"
jurisdiction: US_FEDERAL
effective_date: 2017-06-22
supersedes: null
risk_tier: null
document_type: GUIDANCE
---

# NIST SP 800-63B — Digital Identity Guidelines
Authentication and Lifecycle Management

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of NIST SP 800-63B
for the DecisionLedger reference implementation. NIST SP 800-63B is a publicly
available US government document. This adaptation focuses on provisions directly
relevant to authentication risk decisioning.*

## 1. Authenticator Assurance Levels

NIST SP 800-63B defines three Authenticator Assurance Levels (AAL) that describe the
strength of the authentication process. Each level provides higher assurance that the
claimant controls an authenticator bound to the subscriber's account.

- **AAL1**: Provides some assurance that the claimant controls an authenticator bound
  to the subscriber account. Single-factor authentication (password) is permitted.
- **AAL2**: Provides high confidence that the claimant controls an authenticator bound
  to the subscriber account. Requires a secure authentication protocol and proof of
  possession through a hardware or software cryptographic authenticator, or an
  approved device combined with a memorised secret.
- **AAL3**: Provides very high confidence. Requires proof of possession through a
  hardware authenticator and phishing resistance.

For financial services applications, AAL2 is the minimum for access to accounts with
significant assets or transaction authority.

## 2. Multi-Factor Authentication Requirements

AAL2 requires multi-factor authentication: the use of two or more authentication factors
from different categories (something you know, something you have, something you are).
Permitted authenticator types for AAL2 include:

- Multi-factor OTP devices (authenticator apps such as MFA_TOTP).
- Multi-factor cryptographic software (authenticator apps bound to a specific device).
- Hardware security keys (FIDO2/WebAuthn devices).
- Push notification authenticators (MFA_PUSH) where the push is bound to a registered device.

SMS one-time passwords are not permitted for AAL2 under the current guidance due to
known vulnerabilities in the SS7 protocol that allow interception of SMS messages.

## 3. Session Management

After initial authentication, a session is created to persist the authentication state.
Session management requirements include:

- Sessions SHOULD be re-authenticated when the user's risk profile changes materially.
- Reauthentication SHALL be triggered when the identity provider detects anomalous
  activity that may indicate session hijacking or account takeover.
- Sessions SHALL be terminated after a period of inactivity.

**§7.2 Re-authentication on anomalous signals**: The verifier SHOULD require
re-authentication when anomalous activity is detected during a session, including
a change in the user's IP address to a distant geographic location, a change in
device characteristics, or a significant increase in the sensitivity of the actions
being performed.

## 4. Device Binding and Authenticator Confidence

When a registered authenticator is bound to a specific device, the presence of that
device provides confidence that the authenticator is being used by the authorised
subscriber. Conversely, authentication from an unrecognised device — one not previously
registered or not presenting the expected device characteristics — reduces confidence
in the authentication.

**§5.1.3 Software authenticators**: Software authenticators are bound to a specific
device at registration. A device that presents a software authenticator from a
different device context (e.g., different hardware, different OS) has either migrated
the authenticator (a supported operation) or is presenting a copied credential (an
attack indicator).

Device consistency signals derived from device fingerprinting are an appropriate input
to the risk-based authentication assessment. A device fingerprint consistency score
below the established threshold (INT-DEVICE-FP-V1 §3) is a signal consistent with
§5.1.3 device binding anomalies and warrants step-up authentication to AAL2.

## 5. Authentication of Novel Entities

When a subscriber has insufficient authentication history to establish a reliable
baseline, the verifier cannot assess whether the current authentication is anomalous
relative to a prior pattern. This condition — novel entity — requires the verifier
to apply conservative controls.

The absence of a historical baseline is not evidence of legitimacy. A new account or
an account with sparse history should be treated with the same caution as an account
exhibiting anomalous behaviour, because the controls that would detect anomalous
behaviour are not yet operational for that account.

## 6. Federation and SSO

In federated authentication (SSO), the relying party receives an assertion from an
identity provider rather than directly authenticating the subscriber. The relying party's
trust in the assertion depends on the assurance level negotiated with the identity
provider.

For machine-to-machine or service account authentication using SSO, the identity provider
assertion carries AAL2 assurance when the SSO credential is bound to a specific software
or hardware authenticator at enrollment. The relying party need not re-authenticate the
service account on each request, provided the assertion is current and the session has
not been invalidated.

SSO-based authentication does not exempt service accounts from risk-based monitoring.
The relying party SHOULD monitor SSO sessions for anomalous patterns even when the
authentication credential is trusted, because a compromised identity provider assertion
is a known attack vector.
