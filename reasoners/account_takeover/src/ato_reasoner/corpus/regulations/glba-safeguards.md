---
policy_id: GLBA-SAFEGUARDS
title: Gramm-Leach-Bliley Act — Safeguards Rule (Authentication Provisions)
version: "2023"
jurisdiction: US_FEDERAL
effective_date: 2023-06-09
supersedes: null
risk_tier: null
document_type: REGULATION
---

# Gramm-Leach-Bliley Act — Safeguards Rule
Authentication and Access Control Provisions

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of publicly available
regulatory material for the DecisionLedger reference implementation. It is not a
verbatim reproduction and must not be used as legal compliance guidance. Cite the
authoritative regulatory text (16 CFR Part 314) for compliance purposes.*

## 1. Overview of the Safeguards Rule

The Gramm-Leach-Bliley Act requires financial institutions to implement a comprehensive
information security program to protect customer financial information. The Safeguards
Rule (16 CFR Part 314) specifies the minimum elements of that program, including access
controls, authentication requirements, and incident response obligations.

The 2023 amendments to the Safeguards Rule added specific requirements for multi-factor
authentication and enhanced the incident notification provisions. Financial institutions
that provide online account access must implement controls proportionate to the risk
presented by each transaction or access pattern.

## 2. Authentication Safeguards Requirements

Financial institutions must implement authentication safeguards that:

- Prevent unauthorised access to customer financial information through account credentials.
- Detect and respond to unauthorised access attempts, including automated credential
  testing campaigns.
- Apply risk-based controls that are proportionate to the sensitivity of the information
  accessible through the authenticated session.

Multi-factor authentication is required for all administrative access to covered systems.
For customer-facing authentication, MFA is required when the risk assessment determines
that password-only authentication presents unacceptable risk. The risk assessment must
account for the value of assets accessible through the account and the transaction risk
profile of the typical access pattern.

## 3. Access Control Requirements

Access controls must be designed to limit access to customer information to authorised
persons who have a legitimate need for access in connection with the institution's
business. Controls must include:

- User identification and authentication procedures.
- Access limitation to the minimum information necessary.
- Monitoring of access to detect anomalous patterns indicative of unauthorised access.
- Regular review of access permissions and the removal of permissions for persons who
  no longer have a legitimate need.

For online systems, access controls must address the risk of credential theft and
account takeover. An institution that relies solely on static passwords for customer
authentication without additional risk-based controls does not meet the Safeguards
Rule requirements where the transaction risk profile indicates elevated risk.

## 4. Detection and Response Obligations

Financial institutions must implement procedures to detect and respond to actual and
attempted unauthorised access, use, disclosure, or misuse of customer information.
Detection systems must be capable of identifying:

- High-velocity access attempts inconsistent with normal customer behaviour.
- Access attempts from geographic locations inconsistent with the customer's established
  pattern, including impossible travel.
- Access from device or network contexts associated with prior fraudulent activity.

Response procedures must be activated promptly when detection systems identify a
potential unauthorised access event. Procedures must include escalation paths, customer
notification obligations, and coordination with law enforcement where appropriate.

## 5. Incident Notification Requirements

Under the 2023 amendments, financial institutions must notify the Federal Trade Commission
within 30 days of discovering that customer information has been acquired by an
unauthorised person. The notification requirement is triggered by confirmation of
unauthorised access, not merely by detection of a suspicious event.

Institutions must also notify affected customers promptly when their financial
information has been or is reasonably likely to have been acquired by an unauthorised
person. The customer notification obligation exists independently of the FTC notification
requirement.

## 6. Records Retention

Financial institutions must retain records sufficient to demonstrate compliance with
the Safeguards Rule. For authentication events, this includes records of access
decisions, the controls applied, and the outcome of those controls. Retention periods
must be sufficient to support regulatory examination and are typically interpreted
as a minimum of 3 years for financial records subject to examination.
