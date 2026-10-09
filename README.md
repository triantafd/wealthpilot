# WealthPilot

A multi-agent AI assistant for a (synthetic) wealth-management firm. Advisors ask questions in plain language; a LangGraph supervisor routes them to specialist agents that search policy documents (RAG, with hybrid search and reranking measured and available as options), query client portfolios (safe text-to-SQL), and propose actions that a human must approve before they run.

The point of the project is not the chatbot. It is the engineering around it: every change is measured by an eval suite, every request is traced with cost and latency, and every tool has an explicit risk tier.

## Status

**Phases 0–3 complete. Phase 4a — a thin end-to-end slice — is next.** Working
today: document ingestion, retrieval, and answers with mechanically verified
citations; a 75-case eval suite with a frozen baseline and variant comparison;
Langfuse tracing; and per-request token, cost and latency accounting.

| Area | State |
|---|---|
| RAG pipeline: ingest → retrieve → cite | **working** (`backend/app/rag/`) |
| Eval suite, frozen baseline, variant comparison | **working** (75 cases, `evals/`) |
| Tracing, `usage` table, `/usage`, per-model pricing | **working** |
| Retrieval variants: full-text, hybrid RRF, cross-encoder | **measured, off by default** (`docs/EXPERIMENTS.md`) |
| LangGraph supervisor and agents | not built — Phase 4a |
| SSE streaming, chat page | not built — Phase 4a |
| Risk-tiered tools, approval gate, audit log | not built — Phase 4a |
| Text-to-SQL portfolio agent | not built — Phase 4 |
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

| Metric | Phase 1 baseline | Current | Spread over 3 runs |
|---|---|---|---|
| Retrieval MRR | 0.747 | — | ±0.000 |
| Retrieval hit@1 | 62.9% | — | ±0.000 |
| Retrieval hit@6 | 97.1% | — | ±0.000 |
| Citation validity | 98.4% | — | ±0.003 |
| Faithfulness (Ragas) | 87.0% | — | ±0.011 |
| Answer relevancy (Ragas) | 62.1% | — | ±0.008 |
| Refusal correctness | 100% | — | ±0.000 |
| p95 latency | 3.5 s | — | ±0.46 s |
| Cost per query | $0.00032 | — | ±0.000 |
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
**eligible** to gate CI; the gate itself arrives in Phase 8.

## Quick start

```bash
cp .env.example .env            # add your LLM API key
docker compose up -d db         # Postgres + pgvector
cd backend && uv sync && uv run alembic upgrade head
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
