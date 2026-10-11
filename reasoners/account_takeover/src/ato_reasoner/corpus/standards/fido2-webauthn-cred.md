---
policy_id: FIDO2-WEBAUTHN-CRED
title: FIDO2/WebAuthn Credential Management Standard
version: "Level2"
jurisdiction: INTERNAL
effective_date: 2021-04-08
supersedes: null
risk_tier: null
document_type: STANDARD
---

# FIDO2/WebAuthn Credential Management Standard
W3C Web Authentication API, Level 2

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of the W3C WebAuthn
Level 2 specification and FIDO2 standard for the DecisionLedger reference implementation.
WebAuthn is a W3C Recommendation. This adaptation focuses on credential lifecycle and
device binding aspects relevant to authentication risk decisioning.*

## 1. Overview of FIDO2/WebAuthn

The FIDO2 framework, comprising the W3C WebAuthn specification and the FIDO Alliance's
CTAP protocol, defines a credential model that provides phishing-resistant, strong
authentication without passwords. A WebAuthn credential is a public-private key pair
where the private key is bound to a specific authenticator (device) and never leaves
that device.

For identity risk decisioning systems, FIDO2 credentials provide the strongest possible
device binding: the cryptographic proof of credential use requires the specific
registered authenticator to be present. An account using FIDO2 authentication cannot
have its credential used from a different device without re-registration.

## 2. Credential Lifecycle and Device Binding

A FIDO2 credential is created during registration and bound to:

- A specific relying party (the authentication service).
- A specific authenticator (hardware security key or platform authenticator).
- A specific user account at the relying party.

The binding to a specific authenticator is the critical property for device risk
assessment. Unlike password-based authentication, where the credential can be used
from any device, a FIDO2 credential can only be used from the registered authenticator.

**Device consistency implications**: For accounts using FIDO2 authentication, the
device fingerprint consistency signal has a different interpretation than for password
or TOTP accounts. A FIDO2 authentication from an unrecognised device fingerprint
indicates credential migration (the authenticator was moved to a new device) rather
than a new device accessing an existing credential. Credential migration requires
explicit re-registration and should be treated with the same caution as a new device
in other authentication contexts.

## 3. Platform Authenticators and Device Attestation

A platform authenticator is a FIDO2 authenticator embedded in a device (Windows Hello,
Apple Touch ID/Face ID, Android biometric). Platform authenticators use device TPM or
Secure Enclave hardware to protect the private key.

Device attestation allows the relying party to verify the make and model of the
authenticator during registration. This enables the relying party to:

- Reject credentials from authenticators that do not meet security requirements.
- Detect when a credential is being used from a different device model than the
  one registered (e.g., credential backup restoration to a new device).

For risk decisioning, device attestation provides a higher-confidence device binding
signal than fingerprint-based device recognition, because attestation is cryptographically
signed by the authenticator rather than derived from observable device characteristics.

## 4. Resident Credentials and Cross-Device Authentication

WebAuthn Level 2 introduced enhanced support for resident credentials (discoverable
credentials) stored on the authenticator. These credentials enable cross-device
authentication flows where the credential is used from a different form factor.

Cross-device authentication — for example, using a mobile device's FIDO2 credentials
to authenticate on a desktop browser — produces a cross-device signal that is distinct
from a new-device attack. Relying parties must distinguish between:

- Legitimate cross-device authentication using a registered authenticator.
- Attack attempts using a credential copied from a compromised device.

The distinction requires integration between the FIDO2 authentication layer and the
device risk assessment layer. Cross-device authentication should elevate the device
signal to CHALLENGE rather than ALLOW, even when the credential itself is valid.

## 5. Credential Revocation and Account Recovery

FIDO2 credentials can be revoked by the relying party when the associated device is
lost, stolen, or compromised. Revocation requires the relying party to:

- Remove the credential from the account's registered credential list.
- Invalidate any active sessions created using the revoked credential.
- Trigger account recovery procedures to allow the legitimate account holder to
  register a new credential.

For accounts where FIDO2 is the primary authentication method, credential revocation
has the same effect as account suspension until the new credential is registered.
The account recovery process must verify the identity of the person claiming to be
the account holder before permitting new credential registration.

## 6. FIDO2 Authentication as MFA Exemption

Accounts using FIDO2 authentication satisfy AAL2 requirements without an additional
second factor, because FIDO2 credentials inherently provide two-factor authentication:
something you have (the authenticator) and either something you know (PIN) or something
you are (biometric) for local authenticator verification.

For the purposes of INT-MFA-REQ-V2 §5, FIDO2 authentication (auth_method = PASSKEY)
constitutes a complete MFA exemption: no additional step-up challenge is required for
the MFA requirement specifically. Risk-based challenges for device anomaly
(INT-DEVICE-FP-V1) or geographic anomaly (INT-GEO-RISK-V1) may still apply.
