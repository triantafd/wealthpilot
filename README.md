# WealthPilot

A multi-agent AI assistant for a (synthetic) wealth-management firm. Advisors ask questions in plain language; a LangGraph supervisor routes them to specialist agents that search policy documents (hybrid RAG), query client portfolios (safe text-to-SQL), and propose actions that a human must approve before they run.

The point of the project is not the chatbot. It is the engineering around it: every change is measured by an eval suite in CI, every request is traced with cost and latency, and every tool has an explicit risk tier.

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
- **AI:** model-agnostic via LangChain (OpenAI, Anthropic, or AWS Bedrock), sentence-transformers reranker
- **Evals & observability:** Ragas, pytest, Langfuse
- **Frontend:** Vite, React, TypeScript, Tailwind CSS, shadcn/ui, TanStack Query, Recharts
- **Infra:** Docker Compose locally, AWS for deployment, GitHub Actions CI

## Results (fill in as you go — this table is what recruiters read)

| Metric | Baseline (v0) | Current |
|---|---|---|
| Retrieval hit@5 | | |
| Faithfulness (Ragas) | | |
| Routing accuracy | | |
| SQL execution accuracy | | |
| Safety pass rate | | |
| p95 latency | | |
| Cost per query | | |

## Quick start

```bash
cp .env.example .env            # add your LLM API key
docker compose up -d db         # Postgres + pgvector
cd backend && uv sync && uv run alembic upgrade head
uv run python -m app.scripts.seed        # synthetic clients, portfolios, docs
uv run python -m app.scripts.ingest      # embed documents
uv run fastapi dev app/main.py           # http://localhost:8000
cd ../frontend && pnpm i && pnpm dev     # http://localhost:5173
```

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Roadmap and tasks](docs/ROADMAP.md)
- [Evaluation strategy](docs/EVALS.md)
- [Claude Code setup](CLAUDE.md)
