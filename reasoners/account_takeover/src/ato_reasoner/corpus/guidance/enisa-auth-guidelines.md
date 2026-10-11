---
policy_id: ENISA-AUTH-GUIDELINES
title: ENISA Guidelines on Authentication for eCommunications
version: "2017"
jurisdiction: EU_GDPR
effective_date: 2017-01-01
supersedes: null
risk_tier: null
document_type: GUIDANCE
---

# ENISA Guidelines on Authentication for eCommunications

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of ENISA authentication
guidelines for the DecisionLedger reference implementation. It is not a verbatim
reproduction.*

## 1. Authentication in the EU Regulatory Context

The European Union Agency for Cybersecurity (ENISA) has published guidelines on
authentication security aligned with the requirements of the GDPR, the Network and
Information Security (NIS) Directive, and sector-specific regulation. These guidelines
provide technical guidance on implementing authentication controls that satisfy GDPR
Article 32 obligations.

For identity risk decisioning systems operating in the EU context, the guidelines
recommend risk-based authentication that is proportionate to the risk of the transaction,
the sensitivity of the data accessible, and the potential harm to the data subject from
unauthorised access.

## 2. Risk-Based Authentication Framework

ENISA's risk-based authentication framework assesses authentication risk across four
dimensions:

- **Credential risk**: The risk that the credential used for authentication has been
  compromised (stolen password, phished MFA code).
- **Device risk**: The risk associated with the device from which authentication is
  attempted, based on device reputation, device configuration, and device identity
  consistency.
- **Geographic risk**: The risk associated with the geographic origin of the
  authentication attempt, based on deviation from established patterns and the
  presence of geographic anomalies.
- **Behavioural risk**: The risk based on patterns of behaviour (velocity, timing,
  transaction patterns) that deviate from established baselines.

The aggregate risk across these dimensions determines the strength of authentication
required. Low aggregate risk permits AAL1 authentication; elevated risk in one or more
dimensions requires AAL2; very high aggregate risk warrants session suspension and
human review.

## 3. Device Binding and Geographic Controls

ENISA guidelines recommend device binding as a risk reduction measure: associating
an authenticated session with a specific device context and detecting deviation from
that context. Implementation guidance includes:

- Device identifiers must be derived from stable device characteristics to minimise
  false positives from minor software updates.
- Device binding should use a scoring model that allows for partial matches (a browser
  update changes some but not all device characteristics) rather than binary
  match/no-match logic.
- Geographic controls should use realistic travel models to distinguish legitimate
  travel from impossible travel. The travel time between consecutive login locations
  should be calculated using the shortest feasible route, and locations that are
  physically impossible to travel between in the elapsed time should trigger
  immediate re-authentication.

## 4. Step-Up Authentication Triggers

ENISA recommends defining explicit conditions that trigger step-up authentication
(equivalent to CHALLENGE) as distinct from conditions that require session suspension
(equivalent to HOLD). Step-up triggers include:

- Device consistency score below the established threshold for the account.
- First login from a new country in the account's history.
- Transaction value or sensitivity above a defined threshold.
- Inactivity period exceeding the established session policy.

Session suspension triggers include:

- Geographic impossibility (travel speed indicating physical impossibility).
- Device completely unrecognised with no matching characteristics.
- Velocity spike well above the established account baseline combined with device
  or geographic anomaly.

## 5. Transparency and Explainability

Under GDPR Article 22, automated decisions that significantly affect data subjects must
be explainable. ENISA guidelines recommend that authentication systems:

- Produce a human-readable explanation for any adverse enforcement action (HOLD, BLOCK).
- Provide data subjects with a mechanism to challenge automated decisions.
- Maintain records sufficient to reconstruct the basis for each automated decision.

For LLM-based policy gates, the `rationale` and `citations` fields of the gate output
serve as the primary explainability record. These records must be retained and made
accessible to data subjects upon request, consistent with GDPR data subject rights.

## 6. Data Minimisation in Authentication Systems

ENISA guidelines emphasise that authentication and risk scoring systems must be designed
for data minimisation from the outset:

- Collect only the data necessary to compute the risk signal.
- Pseudonymise or anonymise data that is no longer needed for active risk monitoring.
- Avoid retaining raw event data beyond the window needed for baseline computation.

The principle applies with particular force to biometric data and precise geolocation,
both of which are special categories or highly sensitive data under GDPR.
