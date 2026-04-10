---
policy_id: INT-ATO-DETECT-V1
title: Account Takeover Detection and Response Policy (Superseded)
version: "1.0"
jurisdiction: INTERNAL
effective_date: 2023-06-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Account Takeover Detection and Response Policy
Version 1.0 | Effective: 2023-06-01

> **⚠ THIS DOCUMENT IS SUPERSEDED. Do not cite.**
>
> This document has been superseded by INT-ATO-DETECT-V2 (version 2.0, effective
> 2024-01-01). Any retrieval system returning this document as a primary citation
> has a version resolution failure. Enforcement decisions MUST reference INT-ATO-DETECT-V2.
> This document is retained solely to exercise retrieval version resolution logic and
> MUST NOT be used as the basis for enforcement actions.

## 1. Purpose (Superseded)

*Content below is from the original v1.0 policy and is no longer authoritative.*

This policy defined initial ATO detection criteria. The v1.0 signature was less precise
than the v2.0 three-signal definition and did not include the explicit policy conflict
documentation between NIST-SP-800-63B and internal velocity thresholds.

## 2. Original ATO Signature (Superseded — Do Not Apply)

The v1.0 ATO signature required only two concurrent signals: high success rate (> 60%)
and geographic anomaly. The device signal was treated as corroborating, not primary.

This definition is superseded by INT-ATO-DETECT-V2 §3, which requires all three primary
signals (success rate, new device, and geographic anomaly) and raises the success rate
threshold to 70%.

## 3. Supersession Notice

This document was superseded by INT-ATO-DETECT-V2 on 2024-01-01. The two-signal
signature in v1.0 produced elevated false positives for travellers accessing from
new geographies on established devices. The v2.0 three-signal requirement reduces
false positives while maintaining detection sensitivity for confirmed ATO patterns.

Any system retrieving this document for a current enforcement decision has a version
resolution failure that must be corrected before the decision proceeds.
