---
policy_id: INT-GEO-RISK-V1
title: Geographic Risk Controls Policy
version: "1.0"
jurisdiction: INTERNAL
effective_date: 2024-03-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Geographic Risk Controls Policy
Version 1.0 | Effective: 2024-03-01

*SYNTHETIC REFERENCE DOCUMENT — For the DecisionLedger reference implementation only. Not legal compliance guidance.*

## 1. Purpose and Scope

This policy establishes geographic risk controls for login event evaluation. It defines
how the ATO Reasoner pipeline classifies geographic anomalies, calculates travel speed
between consecutive login locations, and determines the enforcement action appropriate
to each geographic risk level.

Geographic signals are among the highest-confidence indicators of account takeover.
Impossible travel — where consecutive logins originate from locations that could not be
reached in the elapsed time — is a near-definitive attack indicator and receives the
highest mandatory enforcement response under this policy.

This policy applies to all accounts across all risk tiers. High-value account
modifications are in INT-HVA-POLICY-V2.

## 2. Regulatory Basis

Geographic risk controls implement obligations under:

- **FFIEC-AUTH-GUIDANCE**: Institutions must assess anomalous geographic access patterns
  as part of layered authentication controls.
- **NIST-SP-800-63B §7.2**: Re-authentication is required when anomalous access
  indicators are detected, including unexpected geographic origin.
- **OCC-ATO-GUIDANCE**: Geographic displacement from established account patterns is a
  primary ATO indicator requiring immediate session controls.

## 3. Impossible Travel Detection and Mandatory BLOCK

Impossible travel is defined as a sequence of two or more login events for the same
account where the physical travel speed required between origin locations exceeds
**900 km/h** — the approximate maximum speed of commercial aviation.

When impossible travel is detected, **BLOCK is mandatory with no exception and no
override**. This rule takes priority over all other enforcement logic, including
high-value account SLAs and enterprise account velocity exemptions. The mandatory
nature of this rule reflects that no legitimate user behaviour produces impossible
travel; the signal has near-zero false positive rate at the 900 km/h threshold.

The travel speed threshold is intentionally set below supersonic speeds to accommodate
measurement imprecision in geolocation data. A threshold of 900 km/h eliminates
commercially available flights while providing a margin for geolocation error.

## 4. High-Speed Travel Zone (300–900 km/h)

Login sequences where travel speed falls between 300 and 900 km/h represent physically
possible but unusual travel. This zone captures transatlantic or transcontinental
flights and indicates the account holder may be travelling internationally.

For events in this zone, the action SHALL be determined by the presence or absence of
corroborating signals:

- Speed 300–900 km/h AND new device (INT-DEVICE-FP-V1 §5) AND velocity spike → HOLD.
- Speed 300–900 km/h AND established device AND normal velocity → CHALLENGE (step-up
  to confirm identity at new location).
- Speed 300–900 km/h AND established device AND normal velocity AND prior travel
  history to this region → ALLOW with logging.

## 5. Regional Mobility Classification

Geographic mobility is classified into three patterns used by the generator and feature
layer to contextualise location-based signals:

| Pattern | Description | Default risk contribution |
|---|---|---|
| STATIC | All events from a single city | Baseline — no geographic risk |
| REGIONAL | Events within the same continent/region | Low — consistent with normal travel |
| ERRATIC | Events across multiple continents | Moderate to high — requires corroboration |

ERRATIC mobility alone is not sufficient for BLOCK or HOLD. It must be combined with
speed-based impossible travel, new device signals, or velocity anomalies to trigger
mandatory actions.

## 6. Country Risk Signals

Events originating from countries with elevated fraud rates for this account's risk
profile contribute an additive risk signal to the scorer. This signal is not a
standalone BLOCK or HOLD trigger; it increases the likelihood of ROUTE_TO_GATE routing
by raising the aggregate risk score.

Country risk classification is maintained separately from this policy document and is
updated quarterly by the risk operations team.

## 7. Geographic Anomaly Response Matrix

| Condition | Action |
|---|---|
| Travel speed > 900 km/h | BLOCK (mandatory, no override) |
| Travel speed 300–900 km/h + new device + velocity spike | HOLD |
| Travel speed 300–900 km/h + established device | CHALLENGE |
| First login from new country, no other anomalies | CHALLENGE |
| ERRATIC mobility + normal velocity + stable device | ALLOW or CHALLENGE per scorer |
| ERRATIC mobility + high velocity + new device | HOLD or BLOCK per INT-AUTH-RISK-V2 §4 |

## 8. Interaction with Device and Velocity Controls

Geographic signals are evaluated in combination with device fingerprint (INT-DEVICE-FP-V1)
and velocity (INT-VELOCITY-V1) signals. The combination of impossible travel AND a new
device fingerprint AND high failure rate constitutes the post-breach ATO signature
described in INT-ATO-DETECT-V2 §3.

No single geographic signal independently triggers BLOCK except impossible travel.
Combined signals are assessed holistically by the policy gate when routed to ROUTE_TO_GATE.
