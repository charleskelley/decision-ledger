---
policy_id: NIST-CSF-PR-AC
title: NIST Cybersecurity Framework — Protect, Access Control (PR.AC)
version: "1.1"
jurisdiction: US_FEDERAL
effective_date: 2018-04-16
supersedes: null
risk_tier: null
document_type: GUIDANCE
---

# NIST Cybersecurity Framework — Protect: Access Control (PR.AC)

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of the NIST
Cybersecurity Framework PR.AC category for the DecisionLedger reference implementation.
The NIST CSF is a publicly available US government framework.*

## 1. PR.AC-1: Identity and Credential Management

Identities and credentials are managed for authorised devices and users. For online
account systems, this means:

- Identities are uniquely assigned and bound to specific individuals or service accounts.
- Credentials are managed throughout their lifecycle: creation, use, rotation, and
  revocation.
- Anomalous credential use — use from an unexpected device, location, or at an
  unexpected time — is monitored and responded to.

Credential management includes detection of compromised credentials. An institution
that discovers its customer credentials are circulating in breach datasets must have
procedures for proactive credential invalidation and account holder notification.

## 2. PR.AC-3: Remote Access Management

Remote access is managed and controlled. For internet-accessible authentication systems,
all access is effectively remote. Controls must:

- Authenticate the identity of the remote user before granting access.
- Apply additional controls proportionate to the risk of the remote session.
- Monitor remote sessions for anomalous behaviour that may indicate compromise.
- Terminate sessions that exhibit indicators of compromise.

High-velocity remote access from distributed source clusters is a characteristic of
automated attacks rather than legitimate remote access. Detection controls must be
calibrated to distinguish the pattern of a single authorised remote user from the
pattern of a distributed automated attack.

## 3. PR.AC-4: Access Permissions Management

Access permissions are managed, incorporating the principles of least privilege and
separation of duties. For customer account access:

- Each account holder is granted access only to their own account data.
- Permissions are reviewed when the risk profile of the account changes materially.
- Step-up authentication enforces additional verification before granting access to
  high-sensitivity functions within an account (e.g., fund transfers, contact
  information changes).

## 4. PR.AC-5: Network Integrity Protection

Network integrity is protected, incorporating network segregation where appropriate.
For authentication systems:

- Traffic from known-bad infrastructure must be subject to enhanced scrutiny or filtering.
- Rate limiting at the network layer provides an initial defence against high-volume
  attacks before authentication-layer controls are engaged.
- Network-layer protections must be complemented by authentication-layer controls,
  as attackers increasingly use residential proxies that are not identified as
  known-bad at the network layer.

## 5. PR.AC-6: Identities Are Proofed and Bound

The identity proofing and binding process provides high confidence that the claimed
identity is the person it claims to be. For existing accounts, subsequent access
events must maintain this confidence through consistent signals:

- Device consistency supports the identity binding established at account creation.
- Geographic consistency supports the legitimacy of the access event.
- Behavioural consistency (velocity, timing, transaction patterns) supports ongoing
  identity confirmation.

Deviation from established identity binding signals — particularly when multiple
signals deviate simultaneously — warrants re-proofing through step-up authentication
or human review.

## 6. Service Account Authentication Controls

Service accounts (automated processes, API clients, enterprise integrations) require
specific access control treatment distinct from human user accounts:

- Service accounts should be assigned dedicated credentials that are not shared with
  human users.
- Service account activity should be monitored for deviation from the expected
  operational baseline.
- Service account velocity thresholds should be established based on the documented
  operational requirements of the service, not the default human-user thresholds.

For service accounts using SSO authentication, the identity provider's assurance level
and monitoring capabilities must be assessed as part of the access control framework.
The relying party should not assume that SSO authentication eliminates the need for
monitoring service account behaviour.
