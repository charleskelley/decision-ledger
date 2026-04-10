---
policy_id: FFIEC-IT-INFO-SEC
title: FFIEC IT Examination Handbook — Information Security
version: "2006-updated"
jurisdiction: US_FEDERAL
effective_date: 2006-07-01
supersedes: null
risk_tier: null
document_type: GUIDANCE
---

# FFIEC IT Examination Handbook — Information Security

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of the FFIEC IT
Examination Handbook, Information Security booklet, for the DecisionLedger reference
implementation. It is not a verbatim reproduction.*

## 1. Information Security Program Requirements

Financial institutions must develop, implement, and maintain a comprehensive written
information security program. The program must include administrative, technical, and
physical safeguards appropriate to the size, complexity, and nature of the institution.

For online banking systems, the information security program must specifically address:

- Authentication and access control for online account access.
- Detection and response to suspicious access patterns.
- Monitoring and logging of access events.
- Incident response procedures for suspected account compromise.

## 2. Layered Security Model

The FFIEC information security examination framework evaluates institutions against
a layered security model in which each layer provides independent risk reduction.
For online authentication, the examination evaluates:

- **Perimeter controls**: Network-layer filtering, rate limiting, and IP reputation
  controls that limit exposure to known malicious infrastructure.
- **Authentication controls**: Credential validation, MFA enforcement, and device
  recognition.
- **Session controls**: Post-authentication monitoring for session anomalies.
- **Transaction controls**: Risk-based transaction monitoring for high-risk actions
  within authenticated sessions.
- **Detection and response**: Systems capable of detecting attacks in progress and
  initiating response actions automatically.

An institution that relies on a single layer — password authentication without anomaly
detection — fails to meet the layered security expectation and is subject to examination
findings.

## 3. Audit Trail and Logging Requirements

Financial institutions must maintain audit trails sufficient to detect and investigate
security incidents. For authentication systems, logging requirements include:

- A record of every authentication attempt, including the outcome, the source IP,
  the device identifier, and the timestamp.
- A record of every session created, including the authentication method, the session
  duration, and any anomalous events detected during the session.
- A record of every enforcement action taken, including the signals that triggered the
  action and the disposition.

Logs must be retained for a period sufficient to support forensic analysis and regulatory
examination. The examination framework typically expects authentication logs to be
retained for a minimum of 3 years.

## 4. Monitoring and Alerting

Information security programs must include ongoing monitoring of security controls
and alerts for conditions that require immediate response. For authentication risk
systems, monitoring must include:

- Real-time alerting when authentication failure rates exceed defined thresholds.
- Real-time alerting when velocity anomalies suggest automated attack activity.
- Alerting when detection systems identify attack patterns consistent with credential
  stuffing or account takeover.

Alerting systems must be tested periodically to confirm they are operational and that
alert thresholds are appropriately calibrated.

## 5. Vendor and Third-Party Risk

Financial institutions that use third-party services for authentication, identity
verification, or risk scoring must assess and manage the risk that those services
introduce. Third-party risk management requirements include:

- Due diligence on the security practices of the third-party service provider.
- Contractual requirements that the third party maintains controls consistent with
  the institution's information security program.
- Monitoring of third-party service performance and security incidents.
- Business continuity planning for the failure or compromise of third-party services.

For LLM-based policy gate components, the third-party risk assessment must address
the data sent to the LLM service, the security of that transmission, and the
availability and reliability of the service.
