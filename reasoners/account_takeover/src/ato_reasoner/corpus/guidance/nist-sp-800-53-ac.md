---
policy_id: NIST-SP-800-53-AC
title: NIST SP 800-53 Rev 5 — Access Control Family (AC)
version: "rev5"
jurisdiction: US_FEDERAL
effective_date: 2020-09-23
supersedes: null
risk_tier: null
document_type: GUIDANCE
---

# NIST SP 800-53 Rev 5 — Security and Privacy Controls
Access Control (AC) Family

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of NIST SP 800-53
Rev 5 Access Control controls for the DecisionLedger reference implementation.
NIST SP 800-53 is a publicly available US government document.*

## 1. AC-2: Account Management

Organisations must manage information system accounts, including establishing, activating,
modifying, reviewing, disabling, and removing accounts. Account management controls
must include monitoring account activity to detect anomalous behaviour that may indicate
account compromise.

Accounts that exhibit patterns inconsistent with the established account baseline —
including access from unusual devices, unusual geographies, or at unusual times —
must trigger a review of account status. The review may result in temporary suspension
(HOLD), step-up authentication (CHALLENGE), or permanent revocation (BLOCK) depending
on the risk assessment.

## 2. AC-3: Access Enforcement

The information system must enforce approved authorisations for logical access to
information in accordance with applicable policy. For online account systems:

- Access decisions must be based on the authenticated identity of the user AND
  contextual signals that confirm the identity holder is the authorised account owner.
- A valid credential does not automatically grant access; the contextual risk assessment
  may override credential validity when the risk is sufficient.
- Access enforcement must be applied consistently — the same risk signals must produce
  the same access decision regardless of account characteristics, unless tier-specific
  policy explicitly provides for different treatment.

## 3. AC-7: Unsuccessful Login Attempts

The information system must enforce a limit on consecutive invalid login attempts and
must apply increasingly restrictive controls as failed attempts accumulate. Controls
must include:

- Locking an account after a configurable number of consecutive failed login attempts.
- Informing the user of the number of unsuccessful login attempts during the last
  successful login.
- Implementing a delay or lockout to prevent automated brute-force attacks.

For credential stuffing detection, the relevant metric is not per-account failure count
but the aggregate failure rate across the source cluster. A single account may not
exceed the per-account lockout threshold while the source cluster collectively produces
thousands of failures per minute.

## 4. AC-17: Remote Access

The information system must implement monitoring and control of remote access sessions.
For internet-facing authentication systems, all access is effectively remote access.
Controls must include:

- Monitoring of session parameters including source IP, device identity, and geographic
  origin.
- Detection of session anomalies that may indicate hijacking or account takeover.
- Enforcement of session timeouts and re-authentication requirements for extended sessions.
- Logging of all remote access sessions with sufficient detail for forensic analysis.

## 5. AC-19: Access Control for Mobile Devices

Access from mobile devices presents specific risks including device loss, theft, and
the use of shared or public network infrastructure. Controls for mobile device access
must include:

- Device registration and recognition, so that access from a previously unseen device
  triggers step-up authentication.
- Enhanced monitoring for mobile access patterns, including geographic mobility that
  is consistent with physical device movement.
- Remote session invalidation capability for devices that are reported lost or stolen.

## 6. Least Privilege Principle

All access controls must implement the principle of least privilege: granting each
user only the access rights necessary for their legitimate purpose, and not more.
For authentication risk decisions, the least privilege principle applies to the
scope of the session — the authenticated session should grant access to only those
capabilities the account holder has a current, legitimate need to use.

The enforcement layer's proportionality principle (CHALLENGE for ambiguous signals,
HOLD for corroborated signals, BLOCK for definitive signals) reflects least privilege
applied to access control enforcement: the most restrictive action consistent with
the evidence is preferred.
