# Notes

What `docs/ROADMAP.md` is not. The roadmap holds **work that is planned**; this
file holds everything that was learned, measured or decided and does not need
doing. The split exists because the two were in one file and it was 975 lines
of diary with 61 checkboxes, which made the project look three times longer
than it is — nothing here was ever work, but every item had a box that could
not be ticked.

Three sections, and the distinction is what each one is *for*:

- **Measurements** constrain how a future task must be done. Read them before
  setting a threshold or trusting a number.
- **Known gaps** are real defects nobody is fixing yet. Each says what would
  make it matter, so deferring stays a decision.
- **Decisions** are closed questions. They are here so they are not reopened
  from scratch, and so the reasoning survives the person who had it.

Experiments with numbers live in `docs/EXPERIMENTS.md`; this file points at it
rather than repeating it.

---

## Measurements that constrain future work

*Read before setting any eval threshold. The project has been wrong twice by
trusting a single run.*

- **This case is worse than "config-dependent": it is bistable.** Measured
      2026-10-13, the same question on the same commit refused correctly 1 of
      5 times, and 3 of the 5 wrong answers asserted "the firm may not act
      under a power of attorney" — a confident wrong answer, not a paraphrase.
      So `answer.refusal_correct: 1.000` in `baseline.json` is not a
      reproducible number, and **the Phase 8 CI gate must not gate on it until
      this is fixed**, or the gate fails at random. `GATEABLE` in
      `app/evals/report.py` is a label today and enforces nothing, which is the
      only reason this has not already broken CI.

      **Seen again on 2026-11-17, and this time the model was fixed.** The
      Phase 8 pin ran the smoke suite three times before and three times
      after, six runs in all against the same snapshot
      (`gpt-4o-mini-2024-07-18` — the alias already resolved to it, confirmed
      from a real response). This case failed in exactly one of the six.
      That rules out the explanation that was still open: it is **not**
      provider drift. The model did not change between those runs, and the
      case still flipped, so whatever Phase 6 fixes is in the prompt or in
      the passage ordering rather than at the provider. Useful because
      `refusal-cyber-01` in Phase 5 *was* provider drift, and the two were
      easy to confuse

- **`citations.validity` also varies, and by more than it looks.** Seven
      smoke runs measured 0.926 to 1.000 while `retrieval.mrr` sat at exactly
      0.693 every time, so the movement is the model's quote wording, not
      retrieval. Any threshold on it has to sit below the observed floor, or
      be set on the full suite where one case moves it less

- **Set every threshold from several runs, not one.** Three runs of a
      suite are cheap and the project has now been wrong twice by trusting a
      single sample

- **Do not gate on `answer.refusal_correct` until Phase 6 fixes it.**
      Measured 2026-10-13, `refusal-poa-policy-01` refused correctly 1 run in
      5 on the same commit, so the baseline's 1.000 is luck rather than a
      measurement and a threshold on it fails at random. `refusal-cyber-01` is
      the second one, found on 2026-10-19: it refused in three runs on
      `33e7026` and in none of three on `0abe814` with retrieval byte-identical
      — so two of the suite's five refusal cases are coin-flips. `GATEABLE` in
      `app/evals/report.py` is a label that enforces nothing today, which is
      the only reason this has not already bitten. Either Phase 6 lands first
      or this metric starts report-only, stated in `thresholds.yaml`

---

## Known gaps, not scheduled

*Real, and nobody is fixing them yet. The trigger line is the point: it turns
"not now" into a condition rather than a shrug.*

- **A cancelled request still writes no audit row.** A client
      disconnecting mid-tool raises `CancelledError`, which is not an
      `Exception` and so never reaches the audit in `execute`. The savepoint
      is released correctly; only the row is missing. Fixing it needs
      `asyncio.shield` around the write, which is a change to make
      deliberately rather than in passing. Recorded 2026-10-19.
      **Matters when:** anything can actually cancel a run — a stop button,
      a proxy timeout, a deploy draining connections. Today nothing does, so
      the missing row describes an event that does not happen; Phase 9 is
      where it becomes a real hole in the record

- **Shape the `audit_log` migrations for a table that is being written
      to.** `c3e8a1d07b55` adds the column, backfills with one unbatched
      UPDATE over a sequential scan, and validates the check constraint under
      `ACCESS EXCLUSIVE`, all in one transaction — so the table every tool
      call writes to is locked for the whole migration. Milliseconds at this
      size and correct as written; the shape to demonstrate is `NOT VALID`
      then a batched backfill then `VALIDATE CONSTRAINT`.
      **Matters when:** the table is big enough or busy enough for an
      `ACCESS EXCLUSIVE` lock to be felt — so, a deployed Phase 9 rather than
      a dev database. It is also the shape worth *showing*, which is its own
      reason to do it before the repo is read by anyone

- **Give a `usage` row the turn it belongs to.** `thread_id` is the only
      key `usage` and the checkpointer share, so the turn-level history matches
      a row to a turn by time window — correct, and a heuristic over two
      clocks. The fix is a column naming the turn (the checkpoint id the turn
      ended on, or a per-thread ordinal) written by `stream_turn`, after which
      `app/history.py:attach_usage` becomes a join and `created_at` stops
      carrying meaning it was never meant to. A migration plus a backfill that
      can only be approximate for existing rows, which is why it is not part
      of the history endpoint. Recorded 2026-10-21.
      **Matters when:** a turn's cost has to be exact rather than attributed —
      anything billed, or an eval that reports cost per turn. It breaks
      outright the day one thread can run two turns at once, because the
      window stops naming a single turn

- **Cap the result in bytes, not only in rows.** `MAX_ROWS` is 100 and
      nothing bounds how wide a row is: `repeat(md5(random()::text), 32768)`
      returned 100MB in 72ms, and `run_select` materialises the whole result
      with `[dict(row) for row in result.mappings()]` before anything sees it.
      The function allow-list removed `repeat` and `string_agg` as the levers,
      so this is no longer reachable *today*, but the property worth having is
      an octet budget with `yield_per` streaming rather than an allow-list that
      happens to exclude the wide functions. `temp_file_limit` belongs here
      too: it is the other half of the resource cap and it is SUSET, so it
      cannot be set by the role or by a migration running as the owner — it
      needs `postgresql.conf` or a `GRANT SET ON PARAMETER`.
      **Matters when:** any function returning a large value is added to the
      allow-list, or the views grow a wide text column. Until then the
      allow-list is load-bearing for a property it was not designed for,
      which is the part that should not be left standing indefinitely

---

## Decisions taken

### Chunk sizes: not tried, deliberately

- Try 2–3 chunk sizes — **skipped deliberately**, reason recorded in
      `docs/EXPERIMENTS.md`: the evidence points at a question-shaped
      restatement out-matching the governing prose, which moving passage
      boundaries would not address. Cheap to run later if the FAQ work does
      not explain the remaining failures

### `route-vague-cash-01` keeps expecting `research`

Asked when the portfolio agent arrived: should *"cash is piling up in AC-00005,
is that a problem"* route to `portfolio` instead, now that an agent can read
the account's actual cash weight?

**No.** Settled against the corpus rather than by preference:
`investment-policy.md` says cash triggers a rebalance when it *"exceeds 15% of
account value for more than 30 consecutive days"*. The account data gives the
15%; only the document gives the 30 days and the word "problem". A query
alone cannot answer it.

Worth recording as a limitation rather than a routing bug: a complete answer
needs **both** sources, and the graph routes to one destination per turn. That
is the case the supervisor's confidence threshold and fallback exist for, and
it is the first question in the corpus that genuinely needs two agents.

### Self-approval is not acceptable — and the check is meaningless until auth

Nothing compares `decided_by` against `requested_by`, so the adviser who asked
for a rebalance can approve it. For a firm whose whole control is a second pair
of eyes, that is wrong, so the rule is: **an approval may not be decided by the
principal who requested it.**

It is not implemented here, and the reason is not cost. Both fields come from
the request body today, so a caller who wants to self-approve just sends a
different name — the check would refuse the honest case and miss the dishonest
one, which is worse than no check because it reads like a control. It is
therefore attached to the authentication work in Phase 9, where `decided_by`
starts coming from an authenticated principal, and it is listed there.

---

## Considered and not planned

*Cut on purpose. Each one is defensible work; none of it changes what the
project demonstrates, and the list of 61 open items was itself the problem.*

- **More tools** — at least one further `HIGH` and one `LOW`, so the
      registry is exercised rather than illustrated and tier handling is
      tested on more than one row
  **Cut:** the registry has a `READ`, a `HIGH` and 979 tests exercising the
  tier logic, including the gate refusing an unapproved call and auditing it.
  Adding features to prove a mechanism works is a test, not a feature.

- **Frontend decoration** — an agent timeline, a citations drawer opening the
  source at the cited page, a usage dashboard, an evals page, and dark mode
  with responsive layout. **Cut:** chat with streaming and citations, the
  approvals inbox and the documents page already demonstrate the frontend
  claim. These are each a day, and `/usage` and the eval reports are already
  reachable as JSON for anyone who wants the numbers.

- **LoRA fine-tune** of a small open model for intent routing; a
  **framework-free agent** rebuilding the research flow on a raw tool loop;
  and a **LlamaIndex comparison** for the RAG step. **Cut:** all three are
  essays about alternatives rather than parts of this system. The MCP server
  stays, because exposing the agents as tools is a capability rather than a
  comparison.
