---
id: client-restrictions
title: Client Investment Restrictions
version: "2026.1"
effective: 2026-01-01
owner: Compliance
classification: Internal
synthetic: true
---

# Client Investment Restrictions

**WealthPilot Advisers Ltd** — fictional firm, synthetic document.

## 1. What a restriction is

A restriction is a client instruction that narrows what the firm may buy or
hold on their behalf. It is recorded against the client's suitability
assessment, applies to every account the client holds, and overrides the
Investment Policy Statement wherever the two conflict.

Restrictions are client instructions, not preferences. An adviser may not
override one, and no internal approval level permits a trade that breaches one.

## 2. Supported restriction types

Two types are supported. A client may have neither, either or both.

### Sector exclusions

A list of sectors the client will not hold. Recorded as `exclude_sectors`.

The firm may not purchase any instrument whose sector appears on the list. The
eight sectors that may be excluded are:

| Sector |
|---|
| Technology |
| Healthcare |
| Financials |
| Energy |
| Consumer Staples |
| Industrials |
| Utilities |
| Real Estate |

Sector is a property of the instrument, stated in its name and on its
factsheet. A fund without a sector — a multi-asset fund, a bond fund, a cash
balance — is not caught by a sector exclusion, because it is not a sector fund.

### Concentration limit

A maximum percentage of account value in any single instrument. Recorded as
`max_single_holding_pct`.

This applies **in addition to** the firm's own 20% single-instrument limit, and
only where it is stricter. A client limit of 10% means 10%. A client limit of
30% means 20%, because the firm's limit still applies.

## 3. How they are recorded

Restrictions are stored as a structured record against the client's suitability
assessment. Two examples:

```
{"exclude_sectors": ["Energy"]}
{"exclude_sectors": ["Real Estate"], "max_single_holding_pct": 15}
```

An empty record means no restrictions. It does **not** mean the restrictions are
unknown; it means the client has none. A client whose restrictions have never
been established has no valid suitability assessment, which is a separate
condition handled under *Suitability Assessment Framework*, section 8.

<!-- page -->

## 4. Applying a restriction to a purchase

Before any purchase, in this order:

1. Retrieve the client's restrictions.
2. If the instrument has a sector and that sector is excluded, **do not
   purchase**. There is no override.
3. Compute the resulting position as a percentage of account value after the
   trade.
4. Apply the stricter of the client's limit and the firm's 20% limit. If the
   resulting position would exceed it, reduce the trade or do not purchase.
5. Record which restrictions were checked, against the trade.

Step 5 matters: the record of a check that passed is what evidences the control
working. A trade with no restriction check recorded is treated as a breach even
if no restriction was in fact breached.

## 5. Existing holdings that become restricted

Where a client adds a restriction that their current portfolio breaches, the
holding is **not** sold automatically. Selling may crystallise a gain the client
did not ask to realise.

Instead:

1. Notify the client in writing within five business days, stating the holding,
   its value, and the estimated tax consequence of selling.
2. Ask for an instruction: sell now, sell over time, or retain.
3. Record the instruction and act on it.
4. Where no instruction is received within 30 days, retain the holding and flag
   the account for review at the next quarterly cycle.

Until the holding is sold, the account is marked as holding a restricted
position. No further purchase of that instrument is permitted.

## 6. Interaction with rebalancing

A rebalance must not use a restricted instrument to correct an allocation
breach, even where it is the obvious instrument to use.

Where no permitted instrument exists to correct a breach — for example, a client
excluding so many sectors that the equity target cannot be met — the rebalance
is escalated to the Investment Committee rather than forced. The committee
either approves an exception to the allocation band or records that the client's
restrictions make the target unattainable.

A client's restrictions are never the thing that gives way.

<!-- page -->

## 7. Changing a restriction

A change to restrictions is an event requiring an early suitability review
(*Suitability Assessment Framework*, section 6, item 6). The sequence is:

1. Receive the instruction from the client in writing, or record a verbal
   instruction the same business day.
2. Update the restrictions record.
3. Carry out the early suitability review.
4. Assess the existing portfolio against the new restrictions.
5. Follow section 5 above for any holding now in breach.

A restriction takes effect from the moment it is recorded, not from the end of
the review. No purchase may be made in a newly excluded sector while the review
is in progress.

## 8. Removing a restriction

A client may remove a restriction. The firm does not then buy into the released
sector automatically; the next rebalance or recommendation simply has a wider
universe available.

Removal is also a reviewable event, and is recorded with the date, the
instruction and the adviser who processed it.

## 9. What is not a restriction

These are frequently confused with restrictions and are handled elsewhere:

| Client statement | Where it belongs |
|---|---|
| "I don't want to lose more than 10%" | Capacity for loss |
| "I need the money in two years" | Investment horizon |
| "I want income rather than growth" | Investment objective |
| "Don't buy anything without asking me" | Mandate — this is an advisory mandate |
| "I never want to hold Energy funds" | A sector exclusion restriction |

Recording a capacity-for-loss statement as a restriction, or a restriction as an
objective, puts the instruction where no control reads it. The distinction is
not administrative.
