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
- [ ] Try 2–3 chunk sizes
- [ ] Variant: exclude the repeated title and disclaimer boilerplate from what
      gets embedded, measured on its own, **across all documents and not only
      the factsheets**. Eight of the 81 chunks — the first chunk of eight
      documents — open with a `# Title` line and then the identical sentence
      "**WealthPilot Advisers Ltd** — fictional firm, synthetic document."
      That makes every first chunk partly similar to every other first chunk
      and dilutes the content that distinguishes them, which is consistent with
      a `#p1` chunk being the wrong winner in the Phase 1 failures below.
      (YAML frontmatter is already stripped at ingestion and is not the issue.)

**Done when:** you have a table of variants vs metrics and a justified default.
(CV line: "Improved hit@5 from X to Y with hybrid search and reranking.")

### Known failures to track

These are the cases Phase 3 exists to fix. Measure each variant against them by
name, not only on the aggregate — a change can move MRR a point while leaving
every one of them broken.

| Case or tag | Problem | What should fix it |
|---|---|---|
| `mandate-consent-01` | Loses to a `compliance-faq` chunk that restates the rule; the governing document is never retrieved | Hybrid or reranking |
| `prohibited-no-assessment-01` | Same shape: FAQ phrasing matches a question better than the governing document's prose | Hybrid or reranking |
| `fee-etf-trade-01` | Not a retrieval miss. The right chunk **is** retrieved at rank 3 and the model answers from ranks 1–2, quoting "this is a fund, not an ETF" and concluding the opposite | Reranking, by moving it to rank 1 |
| tag `id-lookup` | Exact identifiers and figures that vector search blurs | Full-text search |
| tag `vague-phrasing` | MRR 0.125, the worst tag in the suite | Unclear — may need query rewriting rather than retrieval |

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
