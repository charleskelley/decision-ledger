---
policy_id: FFIEC-AUTH-GUIDANCE
title: FFIEC Authentication in an Internet Banking Environment
version: "2011-updated"
jurisdiction: US_FEDERAL
effective_date: 2011-06-28
supersedes: null
risk_tier: null
document_type: REGULATION
---

# FFIEC Authentication in an Internet Banking Environment
Guidance on Layered Security and Risk-Based Authentication

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of publicly available
FFIEC guidance for the DecisionLedger reference implementation. It is not a verbatim
reproduction. The authoritative text is the FFIEC Supplement to Authentication in an
Internet Banking Environment (June 2011) and subsequent guidance.*

## 1. Overview

The Federal Financial Institutions Examination Council (FFIEC) issued this guidance to
address the evolving threat landscape for online banking, particularly the inadequacy
of single-factor authentication and the need for layered security controls. Financial
institutions that offer internet-based products and services must implement an
authentication program commensurate with the risks posed by the accounts and
transactions they offer.

## 2. Risk Assessment Foundation

Authentication controls must be grounded in a documented risk assessment that considers:

- The sensitivity of customer information accessible through online channels.
- The value of transactions that can be initiated or approved through online accounts.
- The volume of customers affected and the potential aggregate harm from account
  compromise.
- The threat landscape including known attack vectors, attack sophistication, and the
  history of losses in the industry.

The risk assessment must be reviewed and updated periodically, and must be updated
promptly when material changes occur in the threat environment or the institution's
online product offerings.

## 3. Layered Security Controls

Single-factor authentication — password alone — is insufficient for online banking
environments. Institutions must implement a layered security program that includes
controls at multiple levels:

- **Authentication layer**: Controls that verify the identity of the customer, including
  password requirements and MFA.
- **Anomaly detection layer**: Controls that identify access patterns inconsistent with
  the customer's established behaviour, including device recognition, geographic analysis,
  and velocity monitoring.
- **Transaction layer**: Controls that assess the risk of individual transactions,
  including amount-based thresholds and out-of-band confirmation for high-risk transactions.

Layered controls are designed so that the failure of any single control does not
result in a complete security failure. Each layer provides independent risk reduction.

## 4. Anomalous Access Pattern Detection

Financial institutions must implement controls capable of detecting anomalous access
patterns and taking appropriate action. Anomalous patterns include but are not limited to:

- **Geographic anomalies**: Access from locations inconsistent with the customer's
  established geographic pattern, including access from multiple distant locations within
  a timeframe that makes legitimate travel impossible.
- **Device anomalies**: Access from a device not previously associated with the
  customer account, particularly when combined with other risk signals.
- **Velocity anomalies**: Access at a rate significantly higher than the customer's
  established pattern, indicating possible automated access or a compromised credential
  being tested rapidly.
- **Failure pattern anomalies**: A pattern of authentication failures that may indicate
  a brute-force or credential stuffing attack.

Detection systems must be capable of acting on these anomalies in near-real time. A
detection system that identifies anomalies only in batch processing is insufficient for
stopping active attacks.

## 5. Authentication for High-Risk Transactions

Financial institutions must implement stronger authentication controls for transactions
that present higher risk. Risk factors include:

- Transaction amount exceeding established thresholds for the customer's typical pattern.
- Transactions to new payees or beneficiaries.
- Changes to account contact information or authentication credentials.
- International transactions from customers without an established international transaction
  history.

For high-risk transactions, out-of-band authentication (a second channel independent
of the internet banking session) is a preferred control. The institution's risk
assessment must determine which transactions require out-of-band controls.

## 6. Customer Awareness and Education

Financial institutions must have a customer awareness program that educates customers
about the risks of online banking fraud and the controls the institution has implemented.
Customers should understand what actions the institution may take in response to anomalous
activity — including session suspension and re-authentication requirements — so that
legitimate customers are not confused or alarmed by these controls when they are applied.
