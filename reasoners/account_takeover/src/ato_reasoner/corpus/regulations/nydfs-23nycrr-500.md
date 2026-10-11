---
policy_id: NYDFS-23NYCRR-500
title: NYDFS 23 NYCRR Part 500 — Cybersecurity Requirements for Financial Services
version: "2023-amendment"
jurisdiction: US_STATE
effective_date: 2023-11-01
supersedes: null
risk_tier: null
document_type: REGULATION
---

# NYDFS 23 NYCRR Part 500
Cybersecurity Requirements for Financial Services Companies

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of publicly available
NYDFS regulatory material for the DecisionLedger reference implementation. It is not
a verbatim reproduction. The authoritative text is 23 NYCRR Part 500 as amended in
2023.*

## 1. Overview and Applicability

The New York Department of Financial Services (NYDFS) Cybersecurity Regulation (23 NYCRR
Part 500) establishes minimum cybersecurity standards for financial services companies
licensed or registered by the NYDFS. The 2023 amendments significantly expanded the
requirements, including new provisions for multi-factor authentication, access controls,
and incident reporting.

Companies subject to this regulation include banks, insurance companies, and other
financial services entities licensed to conduct business in New York State.

## 2. Multi-Factor Authentication Requirements (§500.12)

Covered entities must use multi-factor authentication for:

- All remote access to the covered entity's information systems by any authorised user.
- All access to non-public information through a web application accessible to a user
  outside the covered entity's network.
- All privileged accounts that have access to non-public information.
- All third-party access to non-public information systems.

MFA is defined as authentication using at least two of the following factors: something
the user knows (password), something the user has (authenticator app, hardware token),
or something the user is (biometric). SMS one-time passwords are not considered a
satisfactory "something you have" factor for the purposes of this regulation, per
amended guidance.

## 3. Access Controls (§500.7)

Covered entities must implement access controls that include:

- Limiting access to non-public information to authorised persons who have a
  legitimate business need for such access.
- Periodic review of access privileges to determine whether access remains appropriate
  and removing access that is no longer required.
- Monitoring of user activity to detect unauthorised access or anomalous behaviour
  that may indicate account compromise.
- Controls designed to prevent credential theft, including detection of credential
  stuffing attacks and protection against phishing.

Access control systems must be capable of detecting and responding to access attempts
that exhibit characteristics of automated attack patterns, including high-velocity
access from unusual locations.

## 4. Cybersecurity Event Detection (§500.14)

Covered entities must implement monitoring systems capable of detecting cybersecurity
events including:

- Unauthorised access to or use of non-public information.
- Deployment of malware or other malicious software.
- Attempts to circumvent or disable security controls.

Monitoring must produce audit trails that are sufficient to detect and respond to
cybersecurity events and to support forensic analysis of such events. Audit trails
must be retained for a minimum of 3 years.

For online authentication systems, monitoring must include the capability to detect
anomalous login patterns that may indicate account takeover, credential stuffing, or
other forms of identity-based attack.

## 5. Incident Response Plan (§500.16)

Covered entities must maintain a documented incident response plan that addresses:

- Internal and external communications and information sharing.
- Remediation and recovery activities.
- Documentation and reporting of cybersecurity events.
- Evaluation of the effectiveness of the response.

The incident response plan must be tested at least annually. Testing must include
scenarios relevant to the entity's risk profile, including account takeover scenarios
for entities that offer online account access.

## 6. Notification Requirements (§500.17)

Covered entities must notify the NYDFS Superintendent within 72 hours of determining
that a cybersecurity event has occurred that:

- Requires notification to any government body or self-regulatory organisation under
  any applicable law, rule, or regulation.
- Has a reasonable likelihood of materially harming any material part of the normal
  operations of the covered entity.

Notification to the NYDFS does not substitute for notification to affected customers
or other regulatory bodies. The covered entity must assess all applicable notification
obligations independently.
