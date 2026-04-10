---
policy_id: INT-DATA-MIN-GDPR-V1
title: Data Minimisation and Retention Policy (EU Compliance)
version: "1.0"
jurisdiction: EU_GDPR
effective_date: 2024-01-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Data Minimisation and Retention Policy (EU Compliance)
Version 1.0 | Effective: 2024-01-01

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy establishes data minimisation and retention requirements for the ATO
Reasoner pipeline when processing events associated with EU data subjects — persons
whose login events originate from EU member state jurisdictions. These requirements
implement obligations under GDPR-ART32-SECURITY and the broader GDPR data minimisation
principle (Article 5(1)(c)).

This policy applies when the jurisdiction metadata of a login event indicates EU origin.
For events with ambiguous or unknown jurisdiction, the more restrictive EU controls
apply.

## 2. Regulatory Basis and Conflict Note

- **GDPR-ART32-SECURITY (Article 32)**: Processing of personal data for security
  purposes must use the minimum data necessary and implement appropriate technical
  measures.
- **GDPR Article 5(1)(c)**: Personal data must be adequate, relevant, and limited to
  what is necessary in relation to the purposes for which they are processed.
- **GLBA-SAFEGUARDS**: US federal retention requirements for financial records may
  exceed GDPR minimisation requirements.

**Known conflict:** GLBA requires retention of authentication records for examination
purposes (minimum 3 years in certain contexts). GDPR data minimisation may require
shorter retention of certain personal data fields. Where these obligations conflict for
EU data subjects who are also subject to US jurisdiction (e.g., US citizens residing
in the EU), the conflict MUST be escalated to human review with `escalate_to_human=True`.
The resolution of this conflict is outside the automated pipeline's authority.

## 3. Data Fields Subject to Minimisation

The following `LoginEvent` fields contain personal data subject to GDPR minimisation
for EU data subjects:

| Field | Classification | Minimisation requirement |
|---|---|---|
| `account_id` | Pseudonymised identifier | Retain for operational period only |
| `source_ip` | Personal data (IP address) | Anonymise after 90 days |
| `geo.city`, `geo.country` | Location data | Retain for anomaly detection window (30 days) |
| `user_agent` | Device identifier | Retain for fingerprint baseline (90 days) |
| `device_fingerprint` | Device identifier | Retain for fingerprint baseline (90 days) |

Fields that do not contain personal data (risk scores, action codes, policy citations)
are not subject to GDPR minimisation and follow standard retention per §4.

## 4. Retention Periods

| Data category | Retention period | Legal basis |
|---|---|---|
| Authentication events (full record) | 90 days | Legitimate interest (fraud prevention) |
| DecisionBundle (full record) | 3 years | GLBA compliance obligation |
| Personal data fields in DecisionBundle | 90 days (then anonymise in-place) | GDPR minimisation |
| Anonymised DecisionBundle | 3 years | GLBA compliance |
| ReviewPacket personal data | 1 year from resolution | Accountability obligation |

Anonymisation of personal data fields after 90 days means replacing the field value
with a cryptographic hash that preserves uniqueness for deduplication but prevents
identification of the data subject. The `decision_id` and `entity_id` remain intact
for audit trail purposes.

## 5. Data Subject Rights Implications for Decisions

When a data subject exercises GDPR rights (access, erasure, portability) with respect
to their authentication events:

- **Right of access**: The data subject is entitled to a summary of enforcement
  decisions made about their account, the signals used, and the actions taken.
  Full `DecisionBundle` contents are not disclosed; the `policy_gate_output.rationale`
  and `final_action` are disclosed.
- **Right to erasure**: Personal data fields in `LoginEvent` records may be erased
  after 90 days consistent with §4. The `DecisionBundle` structure is retained for
  GLBA compliance with personal fields anonymised.
- **Right to explanation**: Automated decisions that adversely affect a data subject
  (HOLD, BLOCK) must be explainable. The `rationale` and `citations` in the
  `PolicyGateOutput` serve as the explanation record.

## 6. Cross-Border Data Transfer Controls

Login events for EU data subjects that are processed by pipeline components running
outside the EU (e.g., US-based LLM API calls for the policy gate) constitute a
cross-border data transfer under GDPR Chapter V.

Mitigation: Event data sent to the LLM API SHALL be minimised to the feature vector
summary and retrieved policy chunks. The full LoginEvent — including source IP,
device fingerprint, and geolocation — SHALL NOT be included in the rendered prompt.
The prompt template (INT-POLICY-GATE-V1, prompt versioning §6) controls the fields
included in each API call.
