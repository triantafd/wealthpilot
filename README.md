# WealthPilot

A multi-agent AI assistant for a (synthetic) wealth-management firm. Advisors ask questions in plain language; a LangGraph supervisor routes them to specialist agents that search policy documents (RAG, with hybrid search and reranking measured and available as options), query client portfolios (safe text-to-SQL), and propose actions that a human must approve before they run.

The point of the project is not the chatbot. It is the engineering around it: every change is measured by an eval suite, every request is traced with cost and latency, and every tool has an explicit risk tier.

## Status

**Phases 0–5 complete. Phase 4 — the text-to-SQL portfolio agent — is
next.** The numbering is deliberate: Phase 5 was brought forward so the app
would be usable before the second agent landed.

A question goes through a LangGraph supervisor to a research or action agent
and streams back token by token over SSE, with its citations. A HIGH-risk
action never runs on the model's say-so — it pauses at an approval gate, and a
human approves or rejects it in an inbox that afterwards says who decided and
what the decision authorised. The conversation survives a page reload,
restored from the checkpointer with its routes, citations and costs intact.
Around it: a 75-case eval suite with a frozen baseline, Langfuse tracing, and
per-request token, cost and latency accounting.

| Area | State |
|---|---|
| RAG pipeline: ingest → retrieve → cite | **working** (`backend/app/rag/`) |
| Eval suite, frozen baseline, variant comparison | **working** (75 cases, `evals/`) |
| Tracing, `usage` table, `/usage`, per-model pricing | **working** |
| Retrieval variants: full-text, hybrid RRF, cross-encoder | **measured, off by default** (`docs/EXPERIMENTS.md`) |
| LangGraph supervisor, research and action agents | **working** (`backend/app/agents/`) |
| SSE streaming, chat page | **working** — token by token, whole conversation, markdown rendered |
| Risk-tiered tools, approval gate, audit log | **working** — every attempt audited, with who authorised it (`backend/app/tools/registry.py`) |
| Approvals inbox: who approved what, and what it did | **working** (`frontend/src/features/approvals/`) |
| Conversation history across a reload, documents page | **working** — restored from the checkpointer (`backend/app/history.py`) |
| Text-to-SQL portfolio agent | not built — **Phase 4, next** |
| Guardrails, prompt-injection defence, safety evals | not built — Phase 6 |
| CI eval gate | not built — Phase 8 |
| AWS deployment, MCP server | not built — Phases 9–10 |

The table below describes the intended architecture. Rows marked *not built*
are planned, not present; `docs/ROADMAP.md` is the current state of play.

## What it demonstrates

| Job requirement | Where it lives |
|---|---|
| RAG: ingestion, chunking, embeddings, retrieval strategies | `backend/app/rag/` |
| Multi-agent / supervisor architecture (LangGraph) | `backend/app/agents/` |
| Tool calling, risk-tiered tools, human-in-the-loop | `backend/app/tools/`, approvals API + UI |
| SQL integration with systems of record | `backend/app/tools/sql/` |
| Evaluation of non-deterministic systems, regression tests | `evals/`, CI gate |
| Observability, tracing, cost tracking | Langfuse + `usage` table + dashboard |
| Guardrails, prompt-injection defence | `backend/app/guardrails/` |
| Production APIs, streaming | FastAPI + SSE |
| Cloud deployment | `infra/` (AWS: ECS/App Runner, RDS, Bedrock) |
| MCP | `mcp-server/` exposes the agents as MCP tools |

## Stack

- **Backend:** Python 3.12, FastAPI, LangGraph, LangChain, SQLAlchemy, Alembic, uv
- **Data:** PostgreSQL 16 with pgvector (vectors, full-text search, and relational data in one DB)
- **AI:** model-agnostic via LangChain (OpenAI, Anthropic, or AWS Bedrock); optional sentence-transformers cross-encoder reranker, off by default
- **Evals & observability:** Ragas, pytest, Langfuse
- **Frontend:** Vite, React, TypeScript, Tailwind CSS, shadcn/ui, TanStack Query, Recharts
- **Infra:** Docker Compose locally, AWS for deployment, GitHub Actions CI

## Results

Phase 1 baseline: vector-only retrieval, measured over 75 golden questions,
averaged across three runs of the same suite. `evals/reports/baseline.json`
carries the per-case detail and the run-to-run spread.

**Current** is the full-suite run of 2026-10-19 — three runs, the same 75
cases, the same config (`openai:gpt-4o-mini`, `text-embedding-3-small`,
vector, top-k 6, no reranker), report `evals/reports/20261019-142323.json` at
commit `0abe814`. Nothing merged since then changes model behaviour, so it
still describes `main`. The one movement worth reading is refusal
correctness, 100% → 98.7%: that is a single case, and `docs/EXPERIMENTS.md`
traces it to provider drift on an unpinned model alias rather than to a change
in this repo — six days apart, same inputs, on both the streamed and the
non-streamed path. Phase 8 pins the model, which is the fix.

| Metric | Phase 1 baseline | Current | Spread, current, 3 runs |
|---|---|---|---|
| Retrieval MRR | 0.747 | 0.747 | ±0.000 |
| Retrieval hit@1 | 62.9% | 62.9% | ±0.000 |
| Retrieval hit@6 | 97.1% | 97.1% | ±0.000 |
| Citation validity | 98.4% | 97.8% | ±0.006 |
| Faithfulness (Ragas) | 87.0% | 90.2% | ±0.005 |
| Answer relevancy (Ragas) | 62.1% | 60.8% | ±0.015 |
| Refusal correctness | 100% | 98.7% | ±0.000 |
| p95 latency | 3.5 s | 2.8 s | not measured per-run |
| Cost per query | $0.00032 | $0.00032 | not measured per-run |
| Routing accuracy | *Phase 4* | | |
| SQL execution accuracy | *Phase 4* | | |
| Safety pass rate | *Phase 6* | | |

Read MRR and hit@1 as the retrieval headline. hit@6 is near-saturated: `k=6`
covers 7.4% of an 81-chunk corpus, so it says more about corpus size than about
retrieval, and it is kept because it becomes informative as the corpus grows.

The Ragas metrics are **report-only**, not gates. An LLM judge moves on its own
— the spread column is how much — and answer relevancy penalises the caveats the
research prompt explicitly requires, so a drop there can mean the answer got
*more* correct. Everything with a ±0.000 spread is deterministic and therefore
**eligible** to gate CI; the gate itself arrives in Phase 8. Refusal
correctness is the case that shows why the gate needs the model pinned first:
it is deterministic *and* currently one case below the 1.00 it would be gated
at, for a reason outside this repo.

## Quick start

```bash
cp .env.example .env            # add your LLM API key
docker compose up -d db         # Postgres + pgvector
cd backend && uv sync && uv run alembic upgrade head
uv run python -m app.scripts.setup_checkpointer   # LangGraph's tables
uv run python -m app.scripts.seed        # synthetic clients, portfolios, docs
uv run python -m app.scripts.ingest      # embed documents
uv run uvicorn app.main:app --reload     # http://localhost:8000
cd ../frontend && pnpm i && pnpm dev     # http://localhost:5173
```

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Roadmap and tasks](docs/ROADMAP.md)
- [Evaluation strategy](docs/EVALS.md)
- [Claude Code setup](CLAUDE.md)
