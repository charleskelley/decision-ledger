---
policy_id: MS-PRAC-NEW-ACCOUNT
title: New Account Transfer Practice
version: "1.0"
jurisdiction: GLOBAL
effective_date: 2026-01-01
supersedes: null
risk_tier: null
document_type: GUIDANCE
---

# New Account Transfer Practice
Version 1.0 | Effective: 2026-01-01

> Synthetic practice note for the DecisionLedger stream mock. It describes
> imaginary accounts and transfers and carries no real policy content.

## 1. Purpose

MS-PRAC-NEW-ACCOUNT sets the operating practice for transfers sent by accounts
with a thin history: accounts that have sent fewer than three transfers on
record. A new account has no baseline of its own, so every amount it sends is
measured against the reference amount of one hundred units instead.

## 2. Thin History

A transfer from an account with fewer than three prior transfers on record has
a thin history. Its activity score is computed against too little history to
be trusted on its own, and a reviewer cannot tell an ordinary first payment
from the first move of an account opened to move money out quickly.

## 3. First Large Transfer

A transfer from a thin-history account whose amount is three times the
reference amount or more SHALL be held for review before release. The
reviewer confirms the transfer with the account holder through a channel the
account did not use to send it, and releases it only when the holder confirms
the amount and the recipient.

## 4. Small First Transfers

A thin-history transfer below three times the reference amount MAY be
released without review when nothing else about it is unusual. Such transfers
build the history that later transfers are measured against, and holding them
would teach reviewers to approve holds without reading them.
