---
policy_id: FTC-SAFEGUARDS-IMPL
title: FTC Safeguards Rule — Implementation Guidance for Financial Institutions
version: "2023"
jurisdiction: US_FEDERAL
effective_date: 2023-06-09
supersedes: null
risk_tier: null
document_type: GUIDANCE
---

# FTC Safeguards Rule — Implementation Guidance for Financial Institutions

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of FTC implementation
guidance for the Safeguards Rule for the DecisionLedger reference implementation.
It is not a verbatim reproduction.*

## 1. Overview

The FTC's implementation guidance for the Safeguards Rule (16 CFR Part 314) provides
practical direction for financial institutions implementing the information security
program requirements. This guidance is particularly relevant for the access control
and authentication components of the security program.

## 2. Risk Assessment for Authentication

The Safeguards Rule requires a written risk assessment that identifies reasonably
foreseeable internal and external risks to customer information. For authentication
systems, the risk assessment must address:

- The risk of credential theft through phishing, data breaches, or social engineering.
- The risk of automated credential stuffing attacks targeting customer accounts.
- The risk of account takeover through session hijacking or device compromise.
- The risk from novel or unknown account holders who have not established a behavioural
  baseline.

The risk assessment must be reviewed and updated at least annually and whenever
material changes occur in the business or threat environment.

## 3. Novel Account and New Customer Controls

Financial institutions face elevated risk from new customers and new accounts because
they have no established behavioural baseline against which to detect anomalies. The
implementation guidance recommends:

- Enhanced monitoring for the first 30 days of account activity.
- More conservative authentication requirements for new accounts (higher friction
  thresholds) until a reliable baseline is established.
- Verification of new account information through out-of-band channels before
  granting full access.

The rationale is that fraudulent accounts and compromised new accounts are most
likely to exhibit anomalous behaviour immediately upon creation, before the institution
has enough data to detect the anomaly through statistical comparison to a baseline.

## 4. Ongoing Monitoring Requirements

The Safeguards Rule requires ongoing monitoring of the information security program
to ensure continued effectiveness. For authentication risk systems, monitoring must
include:

- Regular testing of detection thresholds to ensure they remain calibrated to the
  current threat environment.
- Review of false positive rates to ensure controls are not impairing legitimate
  customer access.
- Post-incident review of detection failures to identify threshold adjustments.
- Monitoring of third-party service providers for changes that may affect the
  security of the institution's authentication program.

## 5. Employee Training and Awareness

The Safeguards Rule requires that all employees receive security awareness training
commensurate with their responsibilities. For staff involved in reviewing HOLD
decisions or responding to ATO incidents:

- Training must include the ability to identify ATO indicators and distinguish them
  from legitimate customer behaviour.
- Training must cover the review procedures, resolution criteria, and escalation
  paths defined in INT-HOLD-QUEUE-V1.
- Training must be updated when threat patterns change or when post-incident reviews
  identify gaps in reviewer judgment.

## 6. Vendor Management for Authentication Services

For institutions using third-party authentication services, risk scoring tools, or
identity verification providers, the Safeguards Rule requires:

- Contractual requirements for the third party to implement security controls
  appropriate to the data they process.
- Periodic review of third-party compliance with those requirements.
- An incident response plan that accounts for third-party failures or security incidents.

For LLM-based policy gate components, vendor management must address the security of
data transmitted to the LLM service, the availability and reliability commitments of
the provider, and the fallback procedures when the service is unavailable.
