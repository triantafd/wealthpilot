---
id: mandates-and-rebalancing
title: Mandates, Authority and Rebalancing Procedure
version: "2026.1"
effective: 2026-01-01
owner: Compliance
classification: Internal
synthetic: true
---

# Mandates, Authority and Rebalancing Procedure

**WealthPilot Advisers Ltd** — fictional firm, synthetic document.

## 1. The three mandates

Every account carries exactly one mandate, recorded on the account and not on
the client. A client may hold accounts under different mandates.

| Mandate | The firm may | The firm may not |
|---|---|---|
| `discretionary` | Buy, sell and rebalance within the Investment Policy Statement without contacting the client for each trade | Trade outside policy bands; breach recorded restrictions |
| `advisory` | Recommend trades, and execute them once the client has consented to that specific recommendation | Execute any trade before the client consents |
| `execution_only` | Execute trades the client has specifically instructed | Recommend, rebalance, or trade on its own initiative under any circumstances |

## 2. Execution-only accounts

An `execution_only` account must **never** be rebalanced by the firm. This holds
even when:

- the account breaches an allocation band in the Investment Policy Statement;
- the account breaches a concentration limit;
- the client's risk category has changed;
- the quarterly review has flagged it;
- the client has a discretionary account elsewhere with the firm.

Where a breach is identified on an execution-only account, the only permitted
action is to **inform the client in writing** and record that it was done. The
decision then belongs to the client. No proposal is generated and no trade is
placed.

This is the single most frequently misunderstood rule in this document.

## 3. Advisory accounts

A rebalance on an advisory account requires the client's consent to the specific
set of trades. Generic consent given at onboarding is not sufficient.

The sequence is: identify the breach, produce a proposal showing each trade and
its cost, obtain and record the client's consent, then execute. Consent may be
given verbally provided it is recorded on the same business day.

A proposal expires if consent is not obtained within **10 business days**, after
which it must be regenerated against current valuations.

<!-- page -->

## 4. Internal approval thresholds

Separately from client consent, the firm's own authority limits apply to every
proposed trade. These are cumulative per account per business day.

| Proposed value | Internal approval required |
|---|---|
| Under £25,000 | None. Adviser may proceed. |
| £25,000 – £250,000 | One senior adviser |
| Above £250,000 | Head of Investments, countersigned by Compliance |
| Any value, if it breaches a concentration limit | Investment Committee |
| Any value, on an account with no valid suitability assessment | Not permitted at any level |

Approval is recorded against the proposal with the approver's identity and the
time of approval. An approved proposal that is not executed within five business
days lapses.

## 5. Prohibited actions

The following are prohibited in all mandates, and no level of approval makes
them permissible:

1. Rebalancing an `execution_only` account.
2. Purchasing any instrument in a sector the client has excluded.
3. Purchasing an instrument not in the permitted asset classes.
4. Any purchase for a client with no valid suitability assessment.
5. Trading to generate transaction charges with no change in exposure.
6. Executing a trade on behalf of a client whose account status is `closed` or
   `suspended`.

## 6. Suspended and closed accounts

| Status | Permitted activity |
|---|---|
| `active` | All activity permitted subject to mandate |
| `suspended` | No purchases. Sales permitted only to meet fees or a court order. No rebalancing. |
| `closed` | No activity. Historic records retained. |

A rebalancing proposal must exclude any account not in `active` status. A
proposal that includes one is rejected without review.

<!-- page -->

## 7. Rebalancing procedure

1. **Value** the account on the most recent available price date.
2. **Compare** each asset class against its permitted range for the client's
   risk category.
3. **Check** every concentration limit and the client's recorded restrictions.
4. **Confirm** the suitability assessment is valid and the mandate permits a
   rebalance.
5. **Construct** the minimum set of trades that brings every asset class inside
   its range. Target allocations are the aim, but trading all the way to target
   is not required and usually not justified.
6. **Cost** the proposal, including transaction charges and any stamp duty.
7. **Obtain** internal approval at the level in section 4.
8. **Obtain** client consent where the mandate requires it.
9. **Execute**, then record the proposal, the approvals and the resulting
   transactions.

## 8. Minimum trade sizes

To avoid trades whose cost outweighs their benefit:

| Rule | Threshold |
|---|---|
| Minimum individual trade value | £500 |
| Minimum change in asset class weight to justify a trade | 2 percentage points |
| Maximum trades per account per rebalance | 8 |

A breach that cannot be corrected within these limits is escalated to the
Investment Committee rather than corrected with uneconomic trades.

## 9. Records

Every proposal is retained for six years whether or not it was executed,
including rejected and lapsed proposals. The record must show what was proposed,
who approved or rejected it, when, and any note they gave.

A rejected proposal is as important a record as an executed one: it evidences
that the control worked.
