# Roadmap

Work top to bottom. Each phase ends with something that runs and a number you can put in the README. Tick boxes as you go; Claude Code reads this file to know where you are.

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

## Phase 4 — Multi-agent graph (1–2 weeks)

- [ ] `AgentState`, Postgres checkpointer
- [ ] Supervisor with structured output and confidence threshold
- [ ] Research agent (wraps Phase 3 retrieval)
- [ ] Portfolio agent: text-to-SQL on `v_*` views, read-only role, sqlglot validation, LIMIT, timeout
- [ ] SSE streaming of graph events using the contract in ARCHITECTURE §6
- [ ] Request path calls `record_usage`, with a test that a real request
      produces a `usage` row — `/usage` was built in Phase 2 and returns
      zeros until something records, so without this it can stay at zero
      silently and look like it works
- [ ] Evals: `routing.jsonl` (≥60 cases), `sql.jsonl` (≥40 cases, compared by **result rows**, not SQL text)

**Done when:** routing accuracy and SQL execution accuracy are in the eval
report, and `/usage` shows a non-zero row after a request.

## Phase 5 — Actions and human-in-the-loop (1 week)

- [ ] Tool registry with risk tiers (READ / LOW / HIGH) and per-tool feature flags
- [ ] Action agent producing `ProposedAction`
- [ ] `interrupt()` approval gate; `/approvals` endpoints; resume with `Command(resume=...)`
- [ ] `audit_log` entries for every tool call and decision

**Done when:** a HIGH-risk action pauses, appears in the approvals inbox, and resumes correctly after approve or reject, even after a server restart.

## Phase 6 — Guardrails and safety evals (3–5 days)

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

## Phase 7 — Frontend polish (1 week)

- [ ] Chat with streaming, route badge, agent timeline, citations drawer
- [ ] Approvals inbox
- [ ] Documents page
- [ ] Usage dashboard
- [ ] Evals page (baseline vs latest, failing cases)
- [ ] Dark mode, responsive, empty and error states
- [ ] Playwright smoke test: ask → stream → approve

**Done when:** a 2-minute screen recording shows the whole flow. Put it at the top of the README.

## Phase 8 — CI eval gate (2 days)

- [ ] `evals/thresholds.yaml`; fail CI if a metric drops below threshold or regresses more than X% vs baseline
- [ ] Small smoke subset on every PR, full suite nightly
- [ ] Post the metrics table as a PR comment

## Phase 9 — AWS deployment (1 week)

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

## Phase 10 — Stretch modules

- [ ] **MCP server** exposing `ask`, `search_docs`, `portfolio_query` (read-only tools only)
- [ ] **LoRA fine-tune** of a small open model for intent routing; compare against the prompted supervisor on accuracy, latency and cost
- [ ] **Framework-free agent**: the same research flow with a raw tool loop, to explain what LangGraph does for you
- [ ] **LlamaIndex comparison** for the RAG step
