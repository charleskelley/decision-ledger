---
policy_id: STUB-POLICY-A
title: Stub Widget Policy A
version: "1.0"
jurisdiction: REGION_A
effective_date: 2024-01-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Stub Widget Policy A
Version 1.0 | Effective: 2024-01-01

> This document is synthetic test data for the DecisionLedger framework test
> suite. It describes imaginary widgets and carries no real policy content.

## 1. Purpose

STUB-POLICY-A defines how the stub reasoner evaluates widgets observed in
REGION_A. Every widget receives a widget score between zero and one from the
stub scorer. A widget with a score below the calibration threshold is released
without further review, while a widget above it is forwarded to the policy gate
for a governed decision backed by retrieved policy evidence.

## 2. Calibration Review

Widgets with a thin history SHALL be held for calibration review before any
release. A thin history means the widget has fewer than ten prior observations
in the stub feature store. Calibration review confirms the widget score was
computed from a complete feature set and records the reviewer's decision in the
widget ledger so that the review can be replayed later.

## 3. Release Criteria

A widget MAY be released when its calibration review is complete, its widget
score is below the release threshold, and no conflicting guidance from another
region applies. Release decisions SHALL cite the section of this policy that
permits them. A widget that fails any release criterion is held and escalated
to a widget steward for a manual decision within one business day.
