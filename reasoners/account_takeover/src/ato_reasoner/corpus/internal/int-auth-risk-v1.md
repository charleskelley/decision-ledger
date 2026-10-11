---
policy_id: INT-AUTH-RISK-V1
title: Authentication Risk Controls Policy (Superseded)
version: "1.0"
jurisdiction: INTERNAL
effective_date: 2023-06-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Authentication Risk Controls Policy
Version 1.0 | Effective: 2023-06-01

> **⚠ THIS DOCUMENT IS SUPERSEDED. Do not cite.**
>
> This document has been superseded by INT-AUTH-RISK-V2 (version 2.1, effective
> 2024-01-15). Any retrieval system returning this document as a primary citation
> has a version resolution failure. Enforcement decisions MUST reference INT-AUTH-RISK-V2.
> This document is retained in the corpus solely to exercise retrieval version resolution
> logic. It MUST NOT be used as the basis for enforcement actions.

## 1. Purpose and Scope (Superseded)

*Content below is from the original v1.0 policy and is no longer authoritative.*

This policy established the initial authentication risk controls framework for the ATO
Reasoner pipeline. It defined basic risk score thresholds and routing rules.

## 2. Risk Score Thresholds (Superseded — Do Not Apply)

The following thresholds were used in v1.0 and are no longer applicable. They are
documented here for audit trail purposes only.

| Score band | Routing | Default action |
|---|---|---|
| 0.00–0.39 | FAST_PATH_ALLOW | ALLOW |
| 0.40–0.74 | ROUTE_TO_GATE | Per policy gate output |
| 0.75–1.00 | FAST_PATH_BLOCK | BLOCK |

Note: These thresholds differ from INT-AUTH-RISK-V2. The v2.1 thresholds (0.35/0.70)
supersede these values. Do not apply v1.0 thresholds to current decisions.

## 3. Supersession Notice

This document was superseded by INT-AUTH-RISK-V2 on 2024-01-15. The primary changes
in v2.1 include: revised score thresholds, added confidence-based override rules,
formalised novel entity routing, and added adversarial probe mandatory BLOCK rule.

Any system retrieving this document for a current enforcement decision has a version
resolution failure that must be corrected before the decision proceeds.
