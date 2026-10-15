---
policy_id: EX-WIDGET-REL-A
title: Widget Release Policy (Region A)
version: "1.0"
jurisdiction: REGION_A
effective_date: 2026-01-01
supersedes: null
risk_tier: null
document_type: INTERNAL_POLICY
---

# Widget Release Policy (Region A)
Version 1.0 | Effective: 2026-01-01

> Synthetic example policy for the DecisionLedger example reasoner. It
> describes imaginary widgets and carries no real policy content.

## 1. Purpose

EX-WIDGET-REL-A governs the release of widgets inspected in REGION_A. Every
inspection produces a widget score between zero and one from the widget
heuristic, computed from the measured wobble and the gizmo misalignment. A
steady, aligned widget is released without review and a widget that is
shaking apart is blocked without review; this policy governs every widget
between those bands.

## 2. Release Criteria

A widget MAY be released when its wobble is below 0.40, its gizmo
misalignment is below 1.5 mm, and it has at least ten prior inspections on
record. A widget that meets the wobble and misalignment criteria but has
fewer than ten prior inspections SHALL be held for calibration review before
release, because its score was computed against too little history to be
trusted.

## 3. Step-Up Inspection

A widget whose wobble is between 0.40 and 0.70, or whose gizmo misalignment
is between 1.5 mm and 4.0 mm, SHALL receive a step-up inspection (a second
measurement by a different inspector) before release. The decision for such a
widget is a challenge: it is released only when the step-up inspection
confirms the measurements are within the release criteria.

## 4. Block Criteria

A widget whose wobble exceeds 0.70, or whose gizmo misalignment exceeds
4.0 mm, SHALL NOT be released. It is blocked and routed to the widget
steward for disposal or rework. Block decisions SHALL cite the measurement
that exceeded its limit.
