# Roadmap

Work top to bottom, with one exception noted below. Each phase ends with
something that runs and a number you can put in the README. Tick boxes as you
go; Claude Code reads this file to know where you are.

**Running order.** `0 → 1 → 2 → 3 → 4a → 4 → 8 → 5 → 6 → 7 → 9 → 10`.

Phase 8, the CI eval gate, moves up to run straight after Phase 4 rather than
last. The README's central claim is that every change is measured by an eval
suite in CI, which is currently false; the gate is about two days' work and
protects every phase after it, so running it late means the phases that most
need protecting are the ones that go without. Numbers are unchanged, because
`docs/EXPERIMENTS.md`, `docs/EVALS.md` and several commit messages already
reference phases by number.

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

- [ ] `AgentState` and the Postgres checkpointer — enough to resume one interrupted run
- [ ] Supervisor routing between exactly **two** destinations, `research` and
      `action`, plus refuse. Two is the minimum that makes routing a real
      decision rather than a pass-through
- [ ] Research agent wrapping the existing Phase 1 retrieval — no new retrieval work
- [ ] Tool registry with `READ`/`LOW`/`HIGH` tiers and **one** HIGH tool:
      `propose_rebalance(account_id)`, writing a `ProposedAction` row
- [ ] `interrupt()` approval gate; `POST /approvals/{id}/approve|reject`;
      resume with `Command(resume=...)`
- [ ] `audit_log` entry for the tool call and the decision
- [ ] SSE streaming over the existing `shared/stream-events.ts` contract,
      keeping the contract test green
- [ ] Request path calls `record_usage`, with a test that a real request
      produces a `usage` row
- [ ] Minimal chat page: ask → stream tokens → route badge → citations →
      approve or reject inline. No dashboards, no polish
- [ ] `evals/datasets/routing_slice.jsonl` — ~18 cases across research / action
      / refuse, including 4 deliberately ambiguous
- [ ] `evals/datasets/actions_slice.jsonl` — ~15 cases asserting correct tool,
      correct tier, and `must_interrupt` for every HIGH call

**Done when:** a 60-second screen recording shows a question answered with
citations, a rebalance proposed, the run pausing, a human approving, and the run
resuming. Routing accuracy and interrupt compliance appear in the eval report.

**Not in this phase:** restart safety (Phase 5), the portfolio/SQL agent and the
full routing taxonomy (Phase 4), guardrails (Phase 6), the usage, documents and
evals pages (Phase 7), and growing the slice datasets to full size (Phase 4).
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
      (≥60 cases) covering every destination, and retire the slice file
- [ ] `evals/datasets/sql.jsonl` (≥40 cases, compared by **result rows**, not
      SQL text)
- [ ] **Extend** the SSE contract with whatever events the portfolio agent needs,
      updating `shared/stream-events.ts` and `backend/app/api/events.py` together
      and keeping the contract test green

**Done when:** routing accuracy and SQL execution accuracy are in the eval
report.

## Phase 5 — Actions and human-in-the-loop, complete (1 week) · **Important**

*Phase 4a already built the tool registry with its three tiers, one HIGH tool,
the `interrupt()` gate, the approve and reject endpoints, resume, and an
`audit_log` entry for that one tool. This phase hardens all of it; those boxes
are not repeated.*

- [ ] **Restart safety**, deferred from Phase 4a: a HIGH action interrupted
      before a server restart resumes correctly after it. This is the
      checkpointer's real test and the reason it exists
- [ ] **More tools** — at least one further `HIGH` and one `LOW`, so the registry
      is exercised rather than illustrated, and so tier handling is tested on
      more than a single row
- [ ] **Per-tool feature flags**, so a tool can be disabled without a deploy
- [ ] **`audit_log` for every tool call and decision**, not only the Phase 4a
      tool — with a test that asserts no tool can execute without a row
- [ ] **Grow** `actions_slice.jsonl` into `evals/datasets/actions.jsonl`
      (≥30 cases), and retire the slice file

**Done when:** a HIGH-risk action pauses, appears in the approvals inbox, and
resumes correctly after approve or reject, **including across a server
restart** — the part Phase 4a deliberately leaves out.

## Phase 6 — Guardrails and safety evals (3–5 days) · **Core**

*The strongest differentiator in the plan, and where the refusal-order bug
owed from Phase 3 is fixed.*

- [ ] Input guard: heuristic rules + LLM classifier
- [ ] Output guard: citation verification, cross-client data check
- [ ] Harden refusal against passage order. `refusal-poa-policy-01` refused
      correctly under one retrieval config and answered confidently and
      irrelevantly under another, **with the same six passages retrieved** and
      only ranks 1 and 2 swapped. Measure the `out-of-scope` and `adversarial`
      tags under several retrieval configurations, not one. Boilerplate
      stripping (Phase 3) becomes adoptable once this holds — it is worth
      +1.4 hit@1 at no runtime cost and was reverted only for this
- [ ] Indirect injection test: a seeded document containing hidden instructions
- [ ] `safety.jsonl` (≥40 attacks): must never trigger a HIGH tool without approval, never leak another client's data

**Done when:** safety pass rate is in the report and gated in CI.

## Phase 7 — Frontend polish (1 week) · **Important**

*Someone will look at this. But Phase 4a's thin page already demos the flow,
so this raises quality rather than enabling it.*

- [ ] Chat with streaming, route badge, agent timeline, citations drawer
- [ ] Approvals inbox
- [ ] Documents page
- [ ] Usage dashboard
- [ ] Evals page (baseline vs latest, failing cases)
- [ ] Dark mode, responsive, empty and error states
- [ ] Playwright smoke test: ask → stream → approve

**Done when:** a 2-minute screen recording shows the whole flow. Put it at the top of the README.

## Phase 8 — CI eval gate (2 days) · **Core**

*Runs straight after Phase 4, not last. The README says every change is
measured in CI; today that is false, and it is two days to make true.*

- [ ] `evals/thresholds.yaml`; fail CI if a metric drops below threshold or regresses more than X% vs baseline
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

## Phase 10 — Stretch modules · **Optional**

*Pick at most one, and only after 4a, 4, 8 and 6. MCP is the cheapest and the
most topical.*

- [ ] **MCP server** exposing `ask`, `search_docs`, `portfolio_query` (read-only tools only)
- [ ] **LoRA fine-tune** of a small open model for intent routing; compare against the prompted supervisor on accuracy, latency and cost
- [ ] **Framework-free agent**: the same research flow with a raw tool loop, to explain what LangGraph does for you
- [ ] **LlamaIndex comparison** for the RAG step
