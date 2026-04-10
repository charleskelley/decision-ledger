---
policy_id: CISA-ATO-PREVENTION
title: CISA Account Takeover Prevention Guide for Financial Services
version: "2022"
jurisdiction: US_FEDERAL
effective_date: 2022-09-01
supersedes: null
risk_tier: null
document_type: GUIDANCE
---

# CISA Account Takeover Prevention Guide for Financial Services

*SYNTHETIC ADAPTATION — This document is a synthetic adaptation of CISA guidance
materials on account takeover prevention for the DecisionLedger reference implementation.
It is not a verbatim reproduction.*

## 1. Credential Stuffing as a Primary ATO Vector

Credential stuffing has become the dominant mechanism for large-scale account takeover
against financial services institutions. Attackers obtain username/password pairs from
data breaches — often from non-financial sites where users reused credentials — and
test them at scale against financial accounts.

Effective credential stuffing defence requires detection at multiple layers:

- Network layer: IP reputation, ASN classification, and request rate limiting.
- Authentication layer: Failure rate monitoring, device fingerprinting, and anomalous
  pattern detection.
- Account layer: Cross-account spread analysis and campaign attribution.

No single layer is sufficient. Attackers actively adapt to single-layer defences by
rotating IPs, using residential proxies, and adjusting attack velocity.

## 2. Velocity-Based Detection Requirements

Financial institutions must implement velocity-based detection capable of identifying
credential stuffing campaigns. Velocity detection must operate at multiple scopes:

- **Per-IP velocity**: The number of authentication requests from a single IP address
  per unit time. Thresholds must be set conservatively to catch attacks while allowing
  legitimate high-frequency API clients.
- **Cross-account velocity**: The number of distinct accounts targeted from a source
  cluster per unit time. Cross-account spread is a more reliable stuffing indicator
  than per-IP velocity alone, because sophisticated attackers use distributed
  infrastructure to stay below per-IP thresholds.
- **Failure rate velocity**: The ratio of failed to total authentication attempts.
  A failure rate above 70% from any source cluster is a definitive indicator of
  credential testing, not legitimate user activity.

Institutions should calibrate velocity thresholds to their specific traffic patterns.
Thresholds that are too low create false positives; thresholds that are too high allow
campaigns to proceed undetected. Thresholds must be reviewed following each confirmed
attack.

## 3. Known-Bad Infrastructure Classification

A significant proportion of credential stuffing attacks are conducted from known-bad
infrastructure: commercial residential proxy services, compromised home routers, and
bulletproof hosting ASNs. Financial institutions should:

- Subscribe to threat intelligence feeds that provide updated known-bad ASN and IP
  classifications.
- Treat events from known-bad infrastructure with elevated scrutiny regardless of
  per-event risk signals.
- Apply immediate escalation to high-confidence block when a known-bad ASN is combined
  with any elevated failure rate.

Known-bad infrastructure classification has a non-trivial false positive rate because
legitimate users may share infrastructure with attackers (e.g., a university ASN that
is also used for research scanning). Thresholds must account for this overlap and
apply graduated responses rather than blanket blocking of entire ASNs.

## 4. Bot Behaviour Discrimination

Automated credential stuffing tools produce distinctive timing patterns that differ
from human user behaviour. Key discriminating features include:

- **Inter-request timing regularity**: Human users exhibit irregular timing; bots
  exhibit high regularity (low jitter). A jitter coefficient below 0.1 in a window
  of events from the same source is a strong bot indicator.
- **User-agent consistency**: Bots often use a single user-agent string or rotate
  through a small set. Human users exhibit more natural variation.
- **Request structure uniformity**: Bot requests often have identical header structures
  across all requests; human browser requests vary in header ordering, accepted
  encoding, and other parameters.

These features are supplementary to velocity and failure rate signals. They should
increase the confidence of a stuffing classification when present, rather than serve
as standalone triggers.

## 5. Multi-Factor Authentication as Stuffing Defence

MFA is the most effective technical control against credential stuffing, because
knowledge of a password alone is insufficient to complete authentication. Institutions
should:

- Require MFA for all accounts where the value of accessible assets justifies the
  friction cost.
- Ensure MFA methods are phishing-resistant where possible (FIDO2/WebAuthn preferred
  over TOTP for highest-risk accounts).
- Monitor MFA challenge response rates as a signal: a source that fails MFA at the
  same rate as it fails passwords is likely an automated attack.

MFA does not eliminate the value of velocity and failure rate detection. Even with MFA
in place, detection systems should monitor for patterns that indicate active stuffing
attempts, so that compromised credentials can be invalidated before attackers obtain
MFA tokens through phishing or SIM swapping.
