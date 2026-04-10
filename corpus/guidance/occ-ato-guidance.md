---
policy_id: OCC-ATO-GUIDANCE
title: OCC Guidance on Online Account Takeover and Mitigation
version: "2012"
jurisdiction: US_FEDERAL
effective_date: 2012-12-01
supersedes: null
risk_tier: null
document_type: GUIDANCE
---

# OCC Guidance on Online Account Takeover and Mitigation

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of OCC guidance
materials on account takeover for the DecisionLedger reference implementation.
It is not a verbatim reproduction.*

## 1. Overview of Account Takeover Risk

Account takeover (ATO) occurs when an unauthorised person gains control of a customer's
online account, typically through credential theft. The OCC has identified ATO as a
significant risk for financial institutions due to the potential for financial loss,
reputational harm, and customer harm.

Financial institutions must implement effective controls to prevent, detect, and respond
to ATO. Controls must address both the authentication layer and the post-authentication
session layer, because ATO can occur through credential theft, session hijacking, or
social engineering.

## 2. ATO Attack Vectors Requiring Detection Controls

Financial institutions must implement detection controls capable of identifying the
following ATO attack vectors:

- **Credential stuffing**: Automated testing of username/password combinations from
  breach datasets against live accounts. Characterised by high velocity, high failure
  rate, and multi-account targeting.
- **Post-breach access**: Attacker uses confirmed valid credentials from a specific
  breach. Characterised by high success rate, access from new devices and geographies,
  and immediate high-value transaction activity.
- **Session hijacking**: Attacker intercepts or steals an active authenticated session.
  Characterised by session continuity from a new network location or device after
  the original authentication.

Detection controls must be capable of distinguishing these attack patterns from
legitimate high-frequency access, legitimate travel, and legitimate device changes.
False positive rates must be managed to avoid impairing the customer experience for
legitimate users.

## 3. Response to Suspected ATO

When detection systems identify a suspected ATO event, financial institutions must
have documented response procedures that include:

- **Immediate session controls**: Suspending the current session and requiring
  re-authentication when ATO signals are detected.
- **Account holder notification**: Contacting the account holder through a registered
  out-of-band channel to confirm whether the access is legitimate.
- **Transaction reversal capability**: Procedures for reversing or halting transactions
  initiated during a suspected ATO event.
- **Evidence preservation**: Preserving logs, transaction records, and authentication
  event data for forensic analysis and potential law enforcement referral.

Response procedures must be automated for high-confidence ATO signals (such as impossible
travel) to ensure the account is protected before the attacker can complete high-value
transactions.

## 4. Geographic and Device Controls for ATO Detection

Geographic and device signals are primary indicators of post-breach ATO. The OCC
expects financial institutions to implement controls that:

- Monitor for access from geographic locations significantly different from the
  customer's established pattern.
- Identify access from devices not previously associated with the customer account.
- Assess the combination of geographic and device anomalies as a compounded risk signal.

Access from a new device in a new geographic location, particularly when combined with
a velocity deviation from the customer's baseline, is a high-confidence ATO indicator
that warrants immediate session controls.

## 5. Proportionate Response and False Positive Management

Controls must be proportionate to the risk. An institution that blocks all legitimate
customer access due to overly aggressive ATO controls creates customer harm and
regulatory risk. Controls must:

- Apply step-up authentication (CHALLENGE) for ambiguous signals that do not warrant
  an outright block.
- Reserve immediate block or session suspension (BLOCK or HOLD) for high-confidence
  ATO signals.
- Maintain accessible dispute and recovery procedures for customers whose access is
  incorrectly restricted.
- Track false positive rates and adjust control thresholds based on observed data.

The layered approach — step-up authentication for ambiguous cases, suspension for
corroborated cases, and block for definitive cases — is the appropriate model for
balancing security and customer experience.
