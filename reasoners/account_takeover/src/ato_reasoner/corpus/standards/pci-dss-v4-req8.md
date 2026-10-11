---
policy_id: PCI-DSS-V4-REQ8
title: PCI DSS v4.0 — Requirement 8, Identify Users and Authenticate Access
version: "4.0"
jurisdiction: INTERNAL
effective_date: 2022-03-31
supersedes: null
risk_tier: null
document_type: STANDARD
---

# PCI DSS v4.0 — Requirement 8
Identify Users and Authenticate Access to System Components

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of PCI DSS v4.0
Requirement 8 for the DecisionLedger reference implementation. PCI DSS is a proprietary
standard; this adaptation focuses on authentication provisions relevant to identity risk
decisioning. The authoritative text is PCI DSS v4.0 published by the PCI Security
Standards Council.*

## 1. Overview of Requirement 8

PCI DSS Requirement 8 mandates that all access to system components and cardholder
data be identified to a specific individual user and that all authentication is achieved
through a minimum of two authentication factors. For online account systems that process
payment card data or provide access to financial accounts, Requirement 8 defines the
minimum authentication standards.

## 2. Requirement 8.4 — Multi-Factor Authentication

MFA is required for all access to the cardholder data environment (CDE) and for all
remote access. Specific requirements include:

- MFA must use at least two different authentication factors.
- MFA must be implemented for all personnel with non-consumer access to the cardholder
  data environment.
- MFA must be implemented for all remote administrative access to the CDE, regardless
  of whether the access is from inside or outside the entity's network.
- For customer-facing systems, MFA must be implemented when risk analysis determines
  that the transaction or account type warrants it.

MFA factors must be independent: a failure in one factor must not compromise the other.
SMS OTP combined with a password is not considered sufficient under v4.0 because both
factors can be compromised through phishing and SIM swapping.

## 3. Requirement 8.3 — Password and Passphrase Controls

Where passwords or passphrases are used as authentication factors:

- Minimum length of 12 characters (4 characters where the system does not allow 12).
- Contain both numeric and alphabetic characters.
- Be changed at least once every 90 days (or confirmed by analysis to not be a risk).
- Not be the same as any of the last four passwords.
- Be protected against access by any individual other than the user.

Password-only authentication (without a second factor) does not meet Requirement 8
for any access to the CDE. The password requirements above represent the minimum for
the first factor in a two-factor scheme.

## 4. Requirement 8.6 — System and Application Account Management

Accounts used by systems and applications (service accounts, API clients) must be
managed appropriately:

- System accounts must not be used for interactive human access.
- Credentials for system accounts must be managed through a secrets management
  process that prevents hardcoding in applications.
- System account activity must be monitored for anomalous patterns.

For enterprise API clients using SSO authentication, the SSO credential must satisfy
the AAL requirements of the identity provider and the SSO session must be monitored
for anomalous patterns consistent with session hijacking.

## 5. Requirement 8.7 — All Access to Databases Authenticated

All access to database components must be authenticated through application-level
credentials, and direct database access must be restricted to database administrators
with specific business need. This requirement is cited here as context for the
data retention obligations in the ATO Reasoner pipeline: the DecisionBundle audit
tables in PostgreSQL constitute database components subject to Requirement 8.7.

## 6. Requirement 10.2 — Audit Log Requirements

PCI DSS Requirement 10.2 requires audit logs for all:

- Individual user access to cardholder data.
- All actions taken by root or administrative users.
- Access to all audit trails.
- Invalid logical access attempts.
- Use of and changes to identification and authentication mechanisms.

For the ATO Reasoner, every DecisionBundle satisfies the audit log requirement for
enforcement actions. The `override_log` field captures the enumerated rule evaluations
required by Requirement 10.2. Log retention must meet the minimum of 12 months with
3 months immediately available.
