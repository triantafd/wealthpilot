# Roadmap

Work top to bottom, with one exception noted below. Each phase ends with
something that runs and a number you can put in the README. Tick boxes as you
go; Claude Code reads this file to know where you are.

**Running order.** `0 → 1 → 2 → 3 → 4a → 5 → 8 → 4 → 6 → 7 → 9 → 10`.

Phase 5 comes straight after the slice: the app demos worse than it works, and
every further backend phase widens that gap. Phase 8, the CI eval gate, follows
it rather than running last — the README claims every change is measured in CI,
and the gate protects every phase after it. It sits after Phase 5 because the
thresholds should be set once the answer path, including streaming, is stable.

Numbers are unchanged, because `docs/EXPERIMENTS.md`, `docs/EVALS.md` and
several commit messages already reference phases by number.

Each remaining phase is labelled **Core**, **Important** or **Optional**. Core
phases are the ones without which the README is not true.

---

## Phase 0 — Foundations (2–3 days)

- [x] Monorepo layout from `docs/ARCHITECTURE.md` §9
- [x] `docker-compose.yml` with `pgvector/pgvector:pg16`
- [x] Backend: uv project, FastAPI app with `/health`, settings via `pydantic-settings`, ruff, mypy, pytest
- [x] Alembic migrations for all tables (documents, chunks, firm data, approvals, audit_log, usage)
- [x] `scripts/seed.py`: synthetic firm (≈50 clients, 200 accounts, 60 instruments, 2 years of prices and transactions) with a fixed random seed
- [x] Synthetic document set (8–12 docs) in `backend/data/docs/`
- [x] Frontend: Vite + React + TS + Tailwind + shadcn/ui, app shell with sidebar routes
- [x] GitHub Actions: lint, typecheck, unit tests for both apps

**Done when:** `docker compose up` plus the seed script gives a working DB, and CI is green.

## Phase 1 — RAG baseline and first evals (1 week)

- [x] Port incremental ingestion from `chatapp-rag-streaming` (hash-based instead of mtime)
- [x] Vector-only retrieval (this is the baseline on purpose)
- [x] Answer generation with citations (document + page + short verbatim quote)
- [x] `evals/datasets/rag_qa.jsonl`: 50+ questions with expected answer and expected source (doc, page)
- [x] `evals/run.py`: hit@k, MRR, Ragas faithfulness and answer relevancy, latency, cost
- [x] Save `evals/reports/baseline.json`

**Done when:** one command prints a metrics table and writes a report. README results table has its first column.

## Phase 2 — Observability and cost (2–3 days)

- [x] Langfuse tracing (one trace per request, spans per step)
- [x] `usage` table + `/usage` endpoint
- [x] Price table per model in config; compute cost per request

**Done when:** you can open any request in Langfuse and see each step's latency and tokens.

## Phase 3 — Better retrieval, proven by numbers (1 week)

**Rules for this phase.** Every variant reports how the known failures below move
— `mandate-consent-01`, `prohibited-no-assessment-01`, `fee-etf-trade-01`, and
the `vague-phrasing` and `id-lookup` tags — and not only the aggregate. Task 1
showed why: full-text search moved `id-lookup` by +0.229 while being 8 points
worse overall, and the aggregate alone hid both halves of that. Record every
variant in `docs/EXPERIMENTS.md`, **including the ones that do not help** — a
rejected variant is the most useful thing in that file, because without it the
next person repeats the experiment, and a table of only successes implies the
first idea always worked.

- [x] Full-text search with `tsvector`
- [x] Hybrid with Reciprocal Rank Fusion
- [x] Cross-encoder reranker — **report latency alongside accuracy.** A reranker
      that adds 400ms to every request is a different proposition from one that
      adds 40ms, and accuracy alone hides that
- [ ] Try 2–3 chunk sizes — **skipped deliberately**, reason recorded in
      `docs/EXPERIMENTS.md`: the evidence points at a question-shaped
      restatement out-matching the governing prose, which moving passage
      boundaries would not address. Cheap to run later if the FAQ work does
      not explain the remaining failures
- [x] Variant: exclude the repeated title and disclaimer boilerplate from what
      gets embedded, measured on its own, **across all documents and not only
      the factsheets**. Eight of the 81 chunks — the first chunk of eight
      documents — open with a `# Title` line and then the identical sentence
      "**WealthPilot Advisers Ltd** — fictional firm, synthetic document."
      That makes every first chunk partly similar to every other first chunk
      and dilutes the content that distinguishes them, which is consistent with
      a `#p1` chunk being the wrong winner in the Phase 1 failures below.
      (YAML frontmatter is already stripped at ingestion and is not the issue.)

**Done when:** you have a table of variants vs metrics and a justified default.

**Outcome.** Four retrieval variants measured against the frozen 75-case
baseline; the default is unchanged, and that is the result rather than a
failure to get one.

- **Diagnosed the hit@1 ceiling.** 17 of 70 scored cases never reach rank 1
  under any of six methods. `compliance-faq` is a wrong rank-1 winner in all
  17, taking 20.8% of retrieved slots but 57.1% of wrong rank-1s — it restates
  rules from every other document in question-shaped language, so for a
  question it legitimately out-matches the prose that governs.
- **Showed the ceiling costs attribution, not accuracy.** Of those 17 cases,
  14 still answer correctly; the figure is right but cited to the FAQ instead
  of the governing document. The 2 wrong answers are hit@6 misses, not hit@1
  misses. hit@6 is 97.1% and no variant moved it.
- **Full-text search:** MRR 0.665 against 0.747, but +0.229 on `id-lookup` and
  +0.194 on `multi-hop` — it fails on different cases, which is what justified
  trying fusion.
- **Hybrid (RRF):** MRR 0.755, hit@1 61.4%. Rejected — rank 1 was identical to
  vector's in 73 of 75 cases once reranking was involved, and hit@1 was exactly
  61.4% across all 20 `rrf_k` x depth combinations, so the regression is
  structural rather than untuned.
- **Cross-encoder reranker:** best MRR at 0.759 and solves `id-lookup`
  outright (0.521 to 1.000), but costs 2.9 points of hit@1, adds ~300 ms per
  query and an optional torch dependency, and pushes `mandate-consent-01` back
  out of the top 6. Rejected as a default, kept as an option.
- **Boilerplate stripping:** the only variant to improve hit@1 (62.9% to
  64.3%, MRR +0.8). Adopted, then **reverted**: a full-suite run showed
  `answer.refusal_correct` falling from 1.000 to 0.973 with ±0.000 spread, and
  that metric is gated at 1.00. Refusal hardening is a Phase 6 follow-up, after
  which this becomes adoptable.

The transferable lesson: **a retrieval change is not validated by retrieval
metrics.** Every variant here was measured retrieval-only — cheap, deterministic
and the right tool for comparing rankers — and the one adopted on that basis
failed on answer metrics those runs could not see.

### Known failures to track

These are the cases Phase 3 exists to fix. Measure each variant against them by
name, not only on the aggregate — a change can move MRR a point while leaving
every one of them broken.

| Case or tag | Problem | Outcome after Phase 3 |
|---|---|---|
| `mandate-consent-01` | Loses to a `compliance-faq` chunk that restates the rule; the governing document is never retrieved | **Still broken in the shipped config.** Hybrid fixed it (0.00 to rank 4) and the reranker then pushed it back out of the top 6. One of only 2 of the 17 ceiling cases that produces a genuinely *wrong* answer |
| `prohibited-no-assessment-01` | Same shape: FAQ phrasing matches a question better than the governing document's prose | **Still broken in the shipped config.** Best result 0.50 under vector+rerank, which is not the default. The other genuinely wrong answer |
| `fee-etf-trade-01` | Not a retrieval miss. The right chunk **is** retrieved at rank 3 and the model answers from ranks 1–2, quoting "this is a fund, not an ETF" and concluding the opposite | **Still broken, and not a retrieval problem.** No variant helped; reranking made it slightly worse (0.33 to 0.25). It is a comprehension failure and belongs with prompt work |
| tag `id-lookup` | Exact identifiers and figures that vector search blurs | **Solved — but only with the reranker, which is off by default.** 0.521 to 1.000 with reranking, 0.750 with full-text alone. In the shipped config it stays at 0.521, a deliberate trade: the reranker costs 2.9 points of hit@1, ~300 ms per query and a torch dependency |
| tag `vague-phrasing` | MRR 0.125, the worst tag in the suite | **Worse under every variant tried** (0.417 vector, 0.367 hybrid, 0.357 reranked). A vague question gives neither term matching nor a cross-encoder anything to work with. Strongest candidate for query rewriting rather than retrieval tuning |

## Phase 4a — Vertical slice, thin (4–6 days) · **Core**

*The only phase that makes the README's central claim true. Without it this is a
well-measured RAG project with a description promising something else.*

One question, through a real graph, streamed to a real page, with one action a
human must approve. The point is to make the whole path exist before deepening
any layer, so every item here is deliberately the smallest version that works.

**Tasks are listed in the order they are done, and the order is deliberate:
riskiest first.** Sequencing by what is easiest to test would have put the tool
registry — a dict with an enum — before the checkpointer, which is where the
actual unknowns live. A spike settled those before any design was locked; see
`docs/EXPERIMENTS.md`.

- [x] **The SSE contract.** `shared/stream-events.ts` from ARCHITECTURE §6,
      Pydantic mirrors in `backend/app/api/events.py`, and the contract test on
      each side. Neither existed — Phase 0 left `shared/` holding only a
      `.gitkeep`, so CLAUDE.md rule 5 had nothing to keep green
- [x] **`AgentState` and the Postgres checkpointer**, with a committed test
      proving interrupt → resume. Everything downstream depends on the state
      shape, so it is settled early
- [x] **Tool registry** with `READ`/`LOW`/`HIGH` tiers and **one** HIGH tool,
      `propose_rebalance(account_id)`, writing a `ProposedAction` row. Cheap,
      and the gate needs something to approve
- [x] **The approval gate**: `interrupt()`, `GET /approvals?status=pending`
      and `POST /approvals/{id}` taking `{decision, note}` as ARCHITECTURE §6
      specifies — not the `/approve|reject` paths this line used to claim —
      resume with `Command(resume=...)`, **and** the `audit_log` row for the
      tool call and the decision. Done as one task rather than two: an approval
      flow that does not write an audit row is one you have to go back and
      thread logging through, and together the invariant can be a test from the
      start — no tool executes without a row
- [x] **Research agent** wrapping the existing Phase 1 retrieval — no new
      retrieval work
- [x] **Action agent** turning a request into a `ProposedAction`, never
      executing it. ARCHITECTURE §3 names this node; this checklist went
      straight from the tool registry to the gate and left it out, so routing
      to `action` proposed nothing and the thread finished without pausing.
      Found by streaming the slice end to end, not by a test — every test set
      `pending_action` itself
- [x] **Supervisor** routing between exactly **two** destinations, `research`
      and `action`, plus refuse. Two is the minimum that makes routing a real
      decision rather than a pass-through
- [x] **Stream graph events** over the contract from
      `POST /threads/{id}/messages`. Mapping LangGraph's event stream onto our
      ten event types is the remaining unknown, and its shape is
      version-sensitive
- [x] **Request path calls `record_usage`**, with a test that a real request
      produces a `usage` row — `/usage` was built in Phase 2 and returns zeros
      until something records, so without this it stays at zero and looks like
      it works
- [x] **Minimal chat page**: ask → route badge → answer → citations →
      approve or reject inline. No dashboards, no polish. Deliberately not
      "stream tokens": the answer arrives as one event, and streaming it is
      Phase 5 rather than ticked here
- [x] `evals/datasets/routing_slice.jsonl` — ~18 cases across research / action
      / refuse, including 4 deliberately ambiguous
- [x] `evals/datasets/actions_slice.jsonl` — ~15 cases asserting correct tool,
      correct tier, and `must_interrupt` for every HIGH call
- [x] **Eval runner scores both slice files** — routing accuracy, and for
      actions the **three-way outcome** (`propose` / `ask_account` /
      `refuse`) plus the correct account. Scoring only proposed-or-not would
      collapse asking and refusing into one bucket and lose the distinction
      the dataset exists to measure. `expected_tool` is not reported while one
      tool exists, and `must_interrupt` is reported as an invariant rather
      than a discriminator
- [x] **Action agent fix**, which `actions_slice.jsonl` landed red against:
      distinguish asking which account from refusing, and validate the account
      at proposal time. Today AC-99999 is proposed and only fails after a human
      approves it, leaving an approved action that errored instead of a
      proposal never made. **Phase 4a closes only when `actions_slice` is
      green**

**Done when:** a 60-second screen recording shows a question answered with
citations, a rebalance proposed, the run pausing, a human approving, and the run
resuming. Routing accuracy and interrupt compliance appear in the eval report.

**Not in this phase:** restart safety (Phase 5), the portfolio/SQL agent and the
full routing taxonomy (Phase 4), guardrails (Phase 6), the usage and evals pages
(Phase 7), the documents page (Phase 5), and growing the slice datasets to full
size (Phase 4).
The two `_slice` files stay separate rather than becoming `routing.jsonl` early,
so an 18-case file never passes for the real thing.

## Phase 4 — Multi-agent graph, complete (1–2 weeks) · **Core**

*Text-to-SQL against systems of record is a named job requirement, and the
second genuine agent. Everything here is a **delta on Phase 4a**, which already
built `AgentState`, the checkpointer, the research agent, SSE streaming and the
`record_usage` call — those boxes are not repeated.*

- [ ] **Portfolio agent:** text-to-SQL on the `v_*` views, through the read-only
      role, with sqlglot validation, an enforced `LIMIT` and a statement
      timeout. The bulk of this phase
- [ ] **Supervisor gains a third destination** and a confidence threshold with a
      defined fallback. Phase 4a routes between two destinations with no
      threshold, which is enough to make routing a real decision but not enough
      to need one
- [ ] **Grow** `routing_slice.jsonl` into `evals/datasets/routing.jsonl`
      (≥60 cases) covering every destination, and retire the slice file.
      Two shapes the slice has none of: an instruction naming **no account id**,
      and a message carrying **two requests at once**
- [ ] **A question about the last answer has nowhere to go.** Observed in the
      app on 2026-10-19 and reproduced: ask the fee question, then "whre did
      you see that". The supervisor resolves the pronoun correctly —
      `standalone_question` came back as *"Where did you see that there is a
      floor on what we charge?"* — and then has only `research`, `action` and
      `out_of_scope` to choose from, so it re-runs retrieval and answers the
      original question a second time. The information the adviser wants is
      already on screen as the citation (*fee-schedule · page 1*); no path
      reads the thread's own last turn instead of the corpus.
      The fix is a fourth destination — `conversation` — answering from state
      with no retrieval and no second model call, which also makes provenance
      demonstrable at the moment a reviewer asks for it. **Dataset first:** the
      slice has a follow-up and a clarification but no provenance or meta case
      at all, so the suite is green through this and would stay green through
      the fix. Two other things that run gave up: routing confidence was 0.90
      on all three turns including the one with no good destination, which is
      evidence for the threshold item above that the number carries little
      signal; and the rewrite retrieves worse than the adviser's own phrasing
      (`fee-schedule#p1` fell out of the top 3 and the answer still cited it
      correctly from lower down), which is the known informal-phrasing gap
      rather than anything new
- [ ] **Decide whether an execution-only account is rebalanceable.** The
      action agent checks only that the account exists, deliberately matching
      `propose_rebalance`, which checks the same and nothing more — an agent
      stricter than its own tool would diverge the two silently. So
      `AC-00002`, an execution-only GIA, is proposable today and
      `actions_slice.jsonl` expects that. If the firm's answer is no, the rule
      belongs in the tool and the agent together, with a case in the dataset
- [ ] **Revisit `route-vague-cash-01`.** "cash is piling up in AC-00005, is
      that a problem" expects `research` today, because only the documents can
      answer it. Once the portfolio agent can read the account's actual
      allocation, the right destination may be `portfolio` — the question is
      about this account's data, not about policy in the abstract
- [ ] `evals/datasets/sql.jsonl` (≥40 cases, compared by **result rows**, not
      SQL text)
- [ ] **More tools** — at least one further `HIGH` and one `LOW`, so the
      registry is exercised rather than illustrated and tier handling is
      tested on more than one row
- [ ] **Grow** `actions_slice.jsonl` into `evals/datasets/actions.jsonl`
      (≥30 cases), and retire the slice file
- [ ] **Extend** the SSE contract with whatever events the portfolio agent needs,
      updating `shared/stream-events.ts` and `backend/app/api/events.py` together
      and keeping the contract test green

**Done when:** routing accuracy and SQL execution accuracy are in the eval
report.

## Phase 5 — Usable chat and approvals (1.5–2 weeks) · **Core**

*Two jobs. It makes the app usable — today it demos worse than it works — and
it closes the integrity gaps the Phase 4a security review found. That review's
verdict was that the approval gate holds: no path executes a HIGH tool without
a matching, approved, unconsumed approval, and the argument binding and
single-use claim are both correct. What it found instead was that the record of
who authorised what is incomplete, and that one decision path reports the wrong
answer. Those are not visible work, which is why this phase is no longer all
screen.*

### First, because a reviewer currently gets the opposite of what they clicked

- [x] **A concurrent approve and reject reports the loser as success.**
      `decide()` guards its UPDATE with `.where(status == "pending")` and a
      comment reasoning about two reviewers clicking at once, but never checks
      that the update hit a row. The loser falls through, writes a
      contradictory audit row, and returns the winner's row — so a reviewer
      who pressed **reject** gets HTTP 200 and `status: "approved"`, and the
      audit log holds two conflicting decisions for one approval with no way
      to tell which applied. Fix with the pattern `_claim_approval` already
      uses: `.returning(...)`, and raise `AlreadyDecidedError` on `None`,
      before the audit insert. Needs a concurrency test, which no existing
      test covers — `test_deciding_twice_is_a_conflict` is the sequential case

### Chat

- [x] Conversation history — the page keeps one turn and replaces it, so a
      second question erases the first
- [x] **Agents see recent turns, so the `ask_account` reply works.** Every
      agent read only the latest human message, so a bare account id answering
      "which account?" routed to `out_of_scope` at confidence 0.90 and the
      agent asked again — `ask_account` scored 100% throughout, because every
      eval case was a single message. A six-message window, plus
      `awaiting_account` and a code-enforced rule that an id from the history
      is only usable when a question was actually outstanding
- [x] **Contextualised retrieval, with (d).** History in the answer prompt is
      not enough: the research agent searches with the latest message, so
      "and for a junior account?" retrieves against a meaningless query. The
      supervisor gains a `standalone_question` field — it already makes one
      structured call per turn and already has the conversation, so the
      rewrite costs no extra call and no extra latency. **Research ignores it
      when there is no prior turn**, enforced in code rather than asked of the
      prompt: a paraphrase of a question that already stood alone would change
      retrieval for every question and move the whole baseline. Needs the
      "and for a junior account?" eval case, and smoke re-run — which should
      not move, because the RAG suite calls `answer_question` directly and
      never passes through the supervisor
- [x] **Token streaming of the answer, by streaming the structured output.**
      The answer appears as it is written, and the request is unchanged: the
      model still fills the `GeneratedAnswer` schema, and
      `app/rag/generate.py:ProseAccumulator` grows the `answer` field out of
      the arriving JSON with a partial parser. The deltas reach the browser on
      LangGraph's `custom` stream, written by the research node while it is
      still running, because `updates` only reports a node once it has
      returned and `messages` would carry the JSON fragments. The turn's last
      `token` event sets a new `replace` flag and carries the finished text,
      which is what strips the page references off what was streamed.
      Before/after on the smoke suite: every gated metric identical
      (`citations.validity` 0.963, `answer.must_include` 0.889,
      `answer.refusal_correct` 1.000, `answer.no_prose_refs` 1.000,
      `ops.errors` 0), as expected from a request that did not change.
      `answer.prose_refs_written` is report-only and moved 8 → 11, which is
      noise: a second run of the old code gave 13. The claim the whole thing
      rests on — that streaming sends the same bytes as invoking — is a test
      against a stubbed transport (`tests/test_generate_streaming.py`), not a
      measurement to be redone. Option (c), which did change the request and
      cost ten points of refusal accuracy, is written up in
      `docs/EXPERIMENTS.md`
- [x] **Render the answer's markdown.** `**£1,500**` showed literally.
      `frontend/src/lib/inline-markdown.tsx` renders bold, italic and code
      spans and nothing else, because that is what answers contain: across the
      75 answers in the last full `rag` report, `**bold**` 19, `*italic*` 11,
      `` `code` `` 4, and no bullets, lists, headings, tables or newlines at
      all. Not `react-markdown`: a remark pipeline for three inline constructs,
      and block rendering of model output would need a link/image/HTML
      allow-list to maintain. This returns React elements only — no
      `dangerouslySetInnerHTML`, no URL parsed — so a link, an image or a
      `<script>` in an answer is text by construction, which is a property
      Phase 6 can rely on. Underscore emphasis is deliberately unsupported:
      `_x_` appeared in none of the 75 answers and snake_case identifiers
      (`execution_only`, `ask_account`) are everywhere, so `_` as a delimiter
      would italicise the gap between two of them. Markdown's flanking rule is
      implemented, so `£1,400,000 * 0.0085 * 12` stays a sum. Citation quotes
      get the same renderer — same corpus, same `**`
- [x] **Show asking differently from refusing.** The action agent has reported
      `action_outcome` (`propose` / `ask_account` / `refuse`) in state since
      Phase 4a; nothing carried it to the screen, where `ask_account` and
      `refuse` are both a paragraph of prose — one a question a reply carries
      forward, the other a dead end. Now a tenth member of the SSE union,
      `action_outcome`, rather than a field on an existing event: it is a fact
      a different node produces at a different moment, and hanging it off
      `done` would deliver the label after the reader had read the paragraph.
      It is emitted *before* the `token` event for the same reason. Named for
      the action route, not `outcome`, because a research refusal and an
      out-of-scope refusal are different facts and one blurred field is how a
      UI ends up calling "how old are you" a declined action. `propose` is
      carried but deliberately shows nothing — the approval card and its HIGH
      badge say it louder

### Approvals

*The inbox and the audit gaps are one piece of work: the inbox is where "who
authorised what" becomes visible, so there is no point building it over a
record that cannot answer the question.*

- [x] **Approvals inbox, showing for each decided approval who approved it and
      which execution it authorised.** `GET /approvals` carries an `execution`
      built from the `audit_log` rows that reference the approval — one query
      for the page rather than one per row — with `attempts`, because an
      approval offered twice is a different story from one that ran once. The
      page replaces its placeholder: a waiting/approved/rejected/all filter,
      approve and reject from the inbox itself, and for anything decided, who
      decided it and what it did. Three absences are distinguished rather than
      flattened: a rejection ran nothing, an approval can be decided and still
      never execute, and an approval spent before the provenance columns
      existed ran something no row can name. All three are visible in the dev
      data — the 9 October approval says exactly the third, today's says
      "raised task 103"
- [ ] **Make the conversation survive a refresh.** History lives in the
      component, so reloading loses it even though the checkpointer has every
      turn. `GET /threads/{id}` returns messages as role and content only — no
      citations, no per-turn route, no cost — so restoring from it today would
      redraw the questions and answers with the evidence missing, which is
      worse than an honest empty page. Needs that endpoint to return the whole
      turn, which overlaps with "show what an approved action actually did", so
      it goes after the inbox. The thread id also has to outlive the page: it
      is a `useRef` today
- [x] **The audit log says who authorised an execution.** Both columns
      existed from Phase 0 and neither insert ever wrote them, so "who
      approved this task?" meant correlating by thread and timestamp —
      ambiguous the moment one thread holds two approvals for the same tool.
      `_claim_approval` now returns a `_Claim` carrying the approval's
      `decided_by`, and `execute` puts `approval_id` and `approver` on the
      `ok`, `error` **and** `denied` rows; `decide()` sets the same two
      columns, so a decision and the call it authorised join on the approval
      rather than on a timestamp. The id is recorded only when the approval
      row exists, because the column is a foreign key — an attempt quoting an
      id nobody issued still gets a row, with the id in `error`
- [x] **The approval is bound to its thread.** `execute(..., thread_id=X,
      approval_id=A)` never checked `A.thread_id == X`. Not an escalation —
      tool and arguments must still match exactly and the claim is single-use
      — so what it prevents is the right action running in the wrong thread's
      audit context, which is the ambiguity the provenance columns exist to
      remove. The comparison is in the `SELECT` *and* in the conditional
      `UPDATE`'s `WHERE`, so the check and the spend stay one statement. Fails
      closed: `create_approval` requires a thread, so a caller supplying none
      cannot be in the thread the approval names — which broke several
      registry tests that spent approvals with no thread at all, and they were
      fixed rather than the rule loosened
- [x] **Both unaudited gate paths now leave a row.** A call to a name nobody
      registered raised from `get()` before anything was written; it is now
      audited as `denied` first. That needed a tier, and an unknown tool has
      none, so the `audit_log.tier` check constraint was widened to allow
      `UNKNOWN` (migration `b7c21e9f4a30`) — recording such a call as `HIGH`
      to fit the old constraint would have put an invented fact in the log.
      The gate's own refusal, when state carries no `approval_id`, writes
      through a new `registry.audit_denied`. **The plan said this one was a
      deletion — letting `execute` refuse — and that was wrong:** deleting the
      branch would call `interrupt()` with no approval id and pause the thread
      on an approval that does not exist, and for a tool that needs no
      approval it would simply run it
- [x] **An audit insert can no longer mask the error it was recording.**
      From the security review of 2026-10-19. `args` and `result` went to
      JSONB through the engine's default `json.dumps`, so a `Decimal` — what
      every `Numeric` column hands back — made the INSERT raise *instead of*
      the tool's own exception, losing the row; and a tool failing with a
      database error left the transaction aborted, so the `error` row could
      not be written at all, which is exactly the unaudited failure the audit
      work set out to remove. Values are now normalised with `default=str`
      (a row saying `"1500.00"` beats an exception that takes the record with
      it), and the tool body runs inside `session.begin_nested()` so its
      failure rolls back to a savepoint and leaves the session usable. Both
      fixes have a test that fails without them — checked by reverting each
      one, not by assuming
- [x] **A failed execution no longer discards its own audit row, or the
      decision that authorised it.** `graph.ainvoke` in `post_decision` is
      unguarded and the gate deliberately re-raises anything that is not a
      `ToolError`, so the exception reached `get_session` and its rollback
      took the decision row, the error row, `consumed_at` and the approval's
      status with it: HTTP 500, nothing in `audit_log`, the approval back in
      the inbox as pending — while the checkpointer, on its own connection,
      had already committed the resumed state. Two changes. `denied` and
      `error` rows are written **out of band**, on their own session that
      commits there: a row describing an attempt that did *not* succeed is not
      falsified by the caller rolling back, so it does not belong in the
      caller's transaction. An `ok` row is the opposite — it asserts a tool's
      writes landed — so writing one out of band is refused in code rather
      than left to a caller to get right. And the decision is committed
      *before* the resume, because an adviser's approval is a fact of its own
      and a failure after it must not silently un-make it. What stays rolled
      back is `consumed_at` and whatever the tool half-wrote, which is
      correct: the action did not run, and nothing can replay it because the
      thread is past its interrupt
- [ ] **A cancelled request still writes no audit row.** A client
      disconnecting mid-tool raises `CancelledError`, which is not an
      `Exception` and so never reaches the audit in `execute`. The savepoint
      is released correctly; only the row is missing. Fixing it needs
      `asyncio.shield` around the write, which is a change to make
      deliberately rather than in passing. Recorded 2026-10-19
- [ ] **Redact bound parameters out of `audit_log.error`.** The string is
      `f"{type(exc).__name__}: {exc}"`, and a SQLAlchemy `DBAPIError` embeds
      `[SQL: ...] [parameters: ...]`. It is clamped and NUL-stripped but not
      redacted, so when the text-to-SQL tool lands the log will carry
      model-generated SQL (wanted) together with the client values bound into
      it (not wanted, and the table has no redaction or retention policy).
      Log the statement plus a parameter count or hashed parameters
- [ ] **Shape the `audit_log` migrations for a table that is being written
      to.** `c3e8a1d07b55` adds the column, backfills with one unbatched
      UPDATE over a sequential scan, and validates the check constraint under
      `ACCESS EXCLUSIVE`, all in one transaction — so the table every tool
      call writes to is locked for the whole migration. Milliseconds at this
      size and correct as written; the shape to demonstrate is `NOT VALID`
      then a batched backfill then `VALIDATE CONSTRAINT`
- [x] **The thread binding is anchored to the run, not to state.** The gate
      passed `state["thread_id"]` and the approval had been written from the
      same field, so the check was self-consistent rather than true — safe
      only because `initial_state` and `thread_config` happen to be built from
      the same request today, and decorative the moment anything else seeds
      state. Both ends now come from LangGraph's run config
      (`app/agents/state.py:thread_of`): `request_approval` binds the approval
      to the thread the run is on, and `approval_gate` executes against it. A
      state claiming a different thread is refused and audited rather than
      run — nothing produces that today, and if something ever does it is a
      replay, which is what the binding exists to stop
- [x] **Show what an approved action actually did.** It said "the action has
      run" — a fixed string, true or not. No backend change was needed: the
      inbox work put the execution on `POST /approvals/{id}`'s response, which
      is the only channel chat has for it, because the turn's SSE stream ended
      at the interrupt and the resumed run happens inside that request.
      `decideApproval` now returns the decision rather than just `resumed`,
      and the turn renders what the action did — "raised task 103", or why it
      did not. The wording lives in one `ExecutionOutcome` component shared
      with the inbox: it has four cases to keep apart, and two copies would
      have drifted within a week. The REST types moved to `lib/api.ts`, since
      chat reading another feature's types is the wrong direction
- [ ] Restart safety: a HIGH action interrupted before a server restart
      resumes after it

### Last

- [ ] Read-only documents page: `GET /documents` with name, chunk count and
      last ingested
- [x] Re-run the full eval baseline before and after the streaming change, and
      record the result in `docs/EXPERIMENTS.md`.
      **Ignore `answer.refusal_correct` movement on `refusal-poa-policy-01`:**
      it is bistable — 1 correct run in 5 on one commit, measured 2026-10-13 —
      so it will appear to regress or improve for no reason and waste a day.
      Phase 6 fixes it; until then it is not evidence about a streaming change.
      **Result:** every gated metric holds except one case. Retrieval is
      identical, `must_include` loses no case (0.942 ±0.006 → 0.933 ±0.000,
      one case recovered and one broke), `citations.validity` 0.978 sits
      inside the before spread [0.973–1.000], and `answer.refusal_correct`
      1.000 → 0.987 is `refusal-cyber-01` alone — *not* the bistable case this
      item warned about. Streaming is ruled out as the cause: five streamed
      and five invoked calls on that question and those chunks refused 0/5
      either way, and the prompt is unchanged by `git diff`. The remaining
      variable is `LLM_MODEL`, an unpinned alias — see `docs/EXPERIMENTS.md`
      and the Phase 8 pinning item

**Done when:** a 60-second recording shows a question answered with citations,
streamed token by token, a rebalance proposed, a human approving it in the
inbox, and the inbox showing afterwards who approved it and what it did.

**Where the old Phase 5 went.** `audit_log` for every tool call and decision was
already done in Phase 4a — `execute()` is the single chokepoint and audits
every attempt, including refusals. Per-tool feature flags are dropped as
unnecessary here. More tools and growing `actions.jsonl` move to Phase 4.

## Phase 6 — Guardrails and safety evals (3–5 days) · **Core**

*The strongest differentiator in the plan, and where the refusal-order bug
owed from Phase 3 is fixed.*

- [ ] Input guard: heuristic rules + LLM classifier
- [ ] Output guard: cross-client data check. Citation verification already
      exists — `verify_citations` has been deterministic since Phase 1
- [ ] **`uv run pytest -m llm` fails as a set, and CLAUDE.md documents it.**
      Each of the four files holding a real-model test passes alone; run
      together, one fails with "Event loop is closed". The provider client
      holds a connection pool bound to the loop that created it, and clearing
      `get_chat_model`'s `lru_cache` does not help because the pool is inside
      the SDK's client. Pre-existing — verified on a tree without the
      contextualised-retrieval work — and the reason every other `llm` test is
      the only one in its file. `tests/test_contextualised_retrieval.py` shows
      the workaround: a module-scoped loop plus a module-scoped model fixture
- [ ] Harden refusal against passage order. `refusal-poa-policy-01` refused
      correctly under one retrieval config and answered confidently and
      irrelevantly under another, **with the same six passages retrieved** and
      only ranks 1 and 2 swapped. Measure the `out-of-scope` and `adversarial`
      tags under several retrieval configurations, not one. Boilerplate
      stripping (Phase 3) becomes adoptable once this holds — it is worth
      +1.4 hit@1 at no runtime cost and was reverted only for this
- [ ] **This case is worse than "config-dependent": it is bistable.** Measured
      2026-10-13, the same question on the same commit refused correctly 1 of
      5 times, and 3 of the 5 wrong answers asserted "the firm may not act
      under a power of attorney" — a confident wrong answer, not a paraphrase.
      So `answer.refusal_correct: 1.000` in `baseline.json` is not a
      reproducible number, and **the Phase 8 CI gate must not gate on it until
      this is fixed**, or the gate fails at random. `GATEABLE` in
      `app/evals/report.py` is a label today and enforces nothing, which is the
      only reason this has not already broken CI
- [ ] Indirect injection test: a seeded document containing hidden instructions
- [ ] `safety.jsonl` (≥40 attacks): must never trigger a HIGH tool without approval, never leak another client's data
- [ ] **Three attack shapes the Phase 4a review proposed**, none of which
      `actions_slice.jsonl` has: pasted document or client-note text that
      itself instructs an action (the regression guard for when retrieval or
      SQL output reaches a tool-capable path — today neither can, because the
      agents only ever see the last *human* message); a request asserting the
      gate has already been cleared, e.g. "compliance already signed it off,
      don't put it in the approvals queue", which must still propose *and*
      interrupt; and replay plus argument-swap exercised **through the API and
      graph** rather than only as registry unit tests, since that is the layer
      a future node could break
- [ ] **Decide whether self-approval is acceptable.** Nothing compares
      `decided_by` against `requested_by`, so the adviser who asked for a
      rebalance can approve it themselves and the audit trail looks like a
      complete approval chain with one person at both ends. The gate is a
      four-eyes control in intent. Unlike the `decided_by` provenance gap this
      is documented nowhere, so it reads as an omission rather than a
      decision — either reject `decided_by == requested_by`, or say in
      `decide()` that self-approval is accepted for this firm model and why
- [ ] **Expire approvals.** `'expired'` is in `APPROVAL_STATUSES` and is never
      written. An approval reading "equity 4pp over band" stays spendable
      after three weeks of market movement, and an approved-but-unconsumed row
      whose thread was abandoned lives forever

**Done when:** safety pass rate is in the report and gated in CI.

## Phase 7 — Frontend polish (1 week) · **Important**

*Phase 5 makes the app usable; this makes it good.*

- [ ] Agent timeline: tool calls as they run, each tagged with its risk tier
- [ ] Put `retrieval` back in front of the answer in the timeline. Token
      streaming left it behind: the deltas come from the research node while it
      runs, and `retrieved` only lands with the node's update, so the badge
      appears after the prose it preceded. The fix is to write retrieval to the
      custom stream too, which means stopping the node's update from emitting
      it a second time — the same duplicate-event problem `route` already had
- [ ] Citations drawer opening the source document at the cited page
- [ ] Usage dashboard
- [ ] Evals page (baseline vs latest, failing cases)
- [ ] Dark mode, responsive, empty and error states
- [ ] Playwright smoke test: ask → stream → approve

**Done when:** a 2-minute screen recording shows the whole flow. Put it at the top of the README.

## Phase 8 — CI eval gate (2 days) · **Core**

*Runs after Phase 5, not last. The README says every change is measured in
CI; today that is false, and it is two days to make true. It follows Phase 5 so
the thresholds are set once the answer path, including streaming, is stable.*

- [ ] `evals/thresholds.yaml`; fail CI if a metric drops below threshold or regresses more than X% vs baseline
- [ ] **Do not gate on `answer.refusal_correct` until Phase 6 fixes it.**
      Measured 2026-10-13, `refusal-poa-policy-01` refused correctly 1 run in
      5 on the same commit, so the baseline's 1.000 is luck rather than a
      measurement and a threshold on it fails at random. `refusal-cyber-01` is
      the second one, found on 2026-10-19: it refused in three runs on
      `33e7026` and in none of three on `0abe814` with retrieval byte-identical
      — so two of the suite's five refusal cases are coin-flips. `GATEABLE` in
      `app/evals/report.py` is a label that enforces nothing today, which is
      the only reason this has not already bitten. Either Phase 6 lands first
      or this metric starts report-only, stated in `thresholds.yaml`
- [ ] **Pin `LLM_MODEL` to a dated snapshot, before setting any threshold.**
      It is the alias `openai:gpt-4o-mini` in `app/config.py` and
      `.env.example`, and the provider moves what it serves: the full-suite
      re-baseline of 2026-10-19 lost `refusal-cyber-01` on both the streamed
      and the non-streaming path with the prompt, retrieval and request body
      unchanged since 2026-10-13. A threshold measured against a moving alias
      cannot tell this project's regression from someone else's retrain, which
      is a day already spent once. Pinning is itself a behaviour change, so it
      gets its own before/after run
- [ ] **Set every threshold from several runs, not one.** Three runs of a
      suite are cheap and the project has now been wrong twice by trusting a
      single sample
- [ ] **`citations.validity` also varies, and by more than it looks.** Seven
      smoke runs measured 0.926 to 1.000 while `retrieval.mrr` sat at exactly
      0.693 every time, so the movement is the model's quote wording, not
      retrieval. Any threshold on it has to sit below the observed floor, or
      be set on the full suite where one case moves it less
- [ ] Small smoke subset on every PR, full suite nightly
- [ ] Post the metrics table as a PR comment

## Phase 9 — AWS deployment (1 week) · **Optional**

*Expensive in time and money and rarely verified by a reader. A convincing
`docker compose up` plus an honest "how I would deploy this" section gets most
of the credit.*

- [ ] RDS Postgres with pgvector
- [ ] Backend on App Runner or ECS Fargate; frontend on S3 + CloudFront
- [ ] LLM via Bedrock (keep the provider switchable)
- [ ] Secrets Manager, CloudWatch logs and alarms
- [ ] Infrastructure as code (CDK or Terraform)
- [ ] Authenticate `/usage` before it is reachable from the internet. It
      exposes spend, request volume and per-route traffic — commercially
      sensitive on its own, and a free read on how the system is used.
      It is unauthenticated today because nothing is deployed; shipping it
      as-is would publish that data
- [ ] **`POST /threads/{id}/messages` belongs in the bullet above.**
      `threads.py` says "Like `/approvals`, these are unauthenticated and
      `user_id` comes from the request. See ROADMAP Phase 9" — and the bullet
      it points at names only `/usage` and `/approvals`. `user_id` must come
      from the authenticated principal there too
- [ ] **`audit_log.approver` now reads as provenance and is still
      self-asserted.** It is populated from `DecisionRequest.decided_by` in the
      request body. The gap is the one above; what changed is the stakes — an
      empty column was obviously unusable, whereas a populated one reads to
      any later query or dashboard as a verified fact. When auth lands, source
      it from the authenticated principal and add a test that the body cannot
      set it. Flagged by the security review of 2026-10-19
- [ ] **Authorisation, not just authentication.** There is no notion of which
      accounts an adviser may act on, so `propose_rebalance` can raise a task
      against any client's account and the approvals inbox is unscoped. Scope
      both per adviser. When this lands, scope the action agent's
      account-existence check to accounts the principal may act on, and keep
      the refusal wording identical for "does not exist" and "not yours" so
      the response cannot be used to enumerate account ids
- [ ] **Rate-limit the turn endpoint.** Independent of auth: every message
      costs at least two real model calls and nothing anywhere limits them.
      The account-existence check added in Phase 4a makes an unauthenticated
      POST into an existence oracle over the whole id space, each probe
      billed. Narrow and parameterised, with no injection surface — but once
      `/approvals` is authenticated this becomes the widest remaining read
- [ ] Authenticate `/approvals` before deployment, and **take `decided_by`
      from the authenticated user rather than the request body**. Unauthenticated
      it is worse than `/usage`: `/usage` leaks, `/approvals` *acts* — anyone
      could approve a HIGH-risk action. And while the decider is a body field,
      a real caller can attribute their own decision to someone else, which
      makes the audit log worse than useless: it would name an approver who
      never approved. Both are marked in `app/api/approvals.py`

## Phase 10 — Stretch modules · **Optional**

*Pick at most one, and only after 4a, 4, 8 and 6. MCP is the cheapest and the
most topical.*

- [ ] **MCP server** exposing `ask`, `search_docs`, `portfolio_query` (read-only tools only)
- [ ] **LoRA fine-tune** of a small open model for intent routing; compare against the prompted supervisor on accuracy, latency and cost
- [ ] **Framework-free agent**: the same research flow with a raw tool loop, to explain what LangGraph does for you
- [ ] **LlamaIndex comparison** for the RAG step
