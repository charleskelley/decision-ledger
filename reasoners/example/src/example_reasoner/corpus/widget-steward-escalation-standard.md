---
policy_id: EX-WIDGET-ESC
title: Widget Steward Escalation Standard
version: "1.0"
jurisdiction: GLOBAL
effective_date: 2026-02-01
supersedes: null
risk_tier: null
document_type: STANDARD
---

# Widget Steward Escalation Standard
Version 1.0 | Effective: 2026-02-01

> Synthetic example standard for the DecisionLedger example reasoner. It
> describes imaginary widgets and carries no real policy content.

## 1. Applicability

EX-WIDGET-ESC applies to widgets in every region. It defines when a widget
decision is escalated to a widget steward and what the steward records.

## 2. When to Escalate

A decision SHALL be escalated to a widget steward when the applicable
regional policies disagree about the widget (for example, REGION_A requires a
calibration review that REGION_B guidance does not), when the evidence for a
release is ambiguous, or when the widget has fewer than ten prior inspections
and its measurements are outside the release criteria.

## 3. What the Steward Records

The steward records the decision (release or block), the measurements and
policy sections it relied on, and a ticket reference in the widget ledger, so
the decision can be audited and replayed later. A steward decision is final
for that inspection; a new inspection starts a new decision.
