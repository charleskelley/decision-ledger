---
policy_id: MS-PRAC-VELOCITY
title: Transfer Velocity and Amount Practice
version: "1.0"
jurisdiction: GLOBAL
effective_date: 2026-01-01
supersedes: null
risk_tier: null
document_type: GUIDANCE
---

# Transfer Velocity and Amount Practice
Version 1.0 | Effective: 2026-01-01

> Synthetic practice note for the DecisionLedger stream mock. It describes
> imaginary accounts and transfers and carries no real policy content.

## 1. Purpose

MS-PRAC-VELOCITY sets the operating practice for transfers from established
accounts, those with three or more prior transfers on record, whose amount or
pace departs from the account's own history. The account's baseline is the
mean amount of its earlier transfers.

## 2. Amount Deviation

A transfer whose amount is five times the account's baseline or more, sent
while the account is otherwise quiet, SHALL be confirmed with the account
holder before release. The decision for such a transfer is a challenge: it is
released when the holder confirms it, and held for review when the holder
cannot be reached.

## 3. Velocity

An account that sends more than five transfers within ten minutes is moving
money faster than its holder normally does. Transfers that continue such a
burst SHALL be stopped, and the account routed to a reviewer who decides
whether the burst was the holder's own. A burst of three to five transfers
calls for confirmation of the next transfer.

## 4. Review Records

Every hold or stop under this practice SHALL record the amount, the account's
baseline, and the number of transfers in the last ten minutes, so that the
reviewer and any later audit can see exactly which measurement exceeded its
limit and by how much.
