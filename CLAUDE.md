# CLAUDE.md

WealthPilot: a multi-agent AI assistant for a synthetic wealth-management firm. Python backend (FastAPI + LangGraph), Postgres with pgvector, React + TypeScript + Tailwind frontend. This is a portfolio project: code quality, tests, evals and clear docs matter as much as features.

Read before starting any task:
- `docs/ARCHITECTURE.md` — design, schemas, SSE contract, risk tiers
- `docs/ROADMAP.md` — current phase and open tasks
- `docs/EVALS.md` — when changing anything that affects model behaviour

## Commands

```bash
# infra
docker compose up -d db

# backend (run from backend/)
uv sync
uv run fastapi dev app/main.py
uv run pytest                      # unit + integration
uv run ruff check . && uv run ruff format .
uv run mypy app
uv run alembic revision --autogenerate -m "msg" && uv run alembic upgrade head
uv run python -m app.scripts.seed
uv run python -m app.scripts.ingest

# evals (run from repo root)
uv run --project backend python evals/run.py --suite smoke
uv run --project backend python evals/run.py --suite all --compare evals/reports/baseline.json

# frontend (run from frontend/)
pnpm dev
pnpm lint && pnpm typecheck && pnpm test
```

## Working rules

1. **One roadmap task at a time.** Plan briefly, implement, test, then tick the box in `docs/ROADMAP.md`.
2. **Evals after behaviour changes.** Any change to prompts, retrieval, chunking, routing, tools or models: run the smoke suite before and after and report the numbers. Do not update `baseline.json` unless asked.
3. **Tools need a risk tier.** Every tool is registered in `app/tools/registry.py` with `READ`, `LOW` or `HIGH`. HIGH tools must go through the approval gate. Never bypass it, including in tests (use the approval API).
4. **SQL is read-only.** Generated SQL runs only through `app/tools/sql/executor.py` (read-only role, sqlglot validation, LIMIT, timeout). Never run model-generated SQL any other way.
5. **The SSE contract is shared.** If you change an event, update `shared/stream-events.ts` and `backend/app/api/events.py` together and keep the contract test passing.
6. **No secrets in code.** Settings come from env via `app/config.py`. Never commit `.env`.
7. **Synthetic data only.** No real people, accounts or client data anywhere.

## Code style

- Python: type hints everywhere, Pydantic models at boundaries, async I/O, small pure functions for logic so it is unit-testable without an LLM. Prompts live in `app/agents/prompts/` as files, not inline strings.
- Tests: mock the LLM in unit tests (fake chat model); real LLM calls only in evals and a few marked integration tests (`@pytest.mark.llm`).
- TypeScript: strict mode, no `any`, feature folders under `src/features/`, server state in TanStack Query, shadcn/ui components, Tailwind only (no CSS files except globals).
- Commits: conventional commits (`feat:`, `fix:`, `eval:`, `docs:`).

## Subagents (`.claude/agents/`)

| Agent | Use for |
|---|---|
| `agent-architect` | LangGraph state, nodes, routing, interrupts, checkpointer |
| `rag-engineer` | Ingestion, chunking, embeddings, hybrid search, reranking |
| `eval-engineer` | Golden datasets, metrics, eval runner, CI gate |
| `frontend-engineer` | React/Tailwind UI, SSE client, dashboards |
| `security-reviewer` | Read-only review of tools, SQL, guardrails, secrets before merging |

Use `security-reviewer` after any change under `app/tools/`, `app/guardrails/` or `app/tools/sql/`.

## Skills (`.claude/skills/`)

- `add-agent-tool` — adding or changing a tool
- `run-evals` — running, comparing and interpreting evals
- `improve-retrieval` — any retrieval experiment

## Git workflow

- Commits use only the user's git identity. Never add Co-Authored-By lines or "Generated with Claude Code" footers.
- After finishing a task, do NOT commit yet. First show:
  1. A short summary of what was built and why
  2. The list of changed files
  3. Test and lint results
  4. Anything I should check or learn from this task
- Commit only after I reply "commit". Then stop and wait for "next" before starting the next task.
- Commit format: conventional commits with a scope, e.g.
  `feat(backend): ...`, `chore(infra): ...`, `feat(frontend): ...`,
  `test(evals): ...`, `docs: ...`.
  Subject under 72 characters, imperative mood ("add", not "added").
  Body: `Phase N, task M` plus one or two lines on why, if not obvious.
- One commit per roadmap task. Tick the task's checkbox in
  docs/ROADMAP.md in the same commit.
- Default branch is `main`. Each roadmap phase gets its own branch
  (`phase-N-short-name`), with one commit per task on it.
- When a phase is complete and CI is green, open a PR into `main`
  titled "Phase N: <name>" with a summary of what was built and the
  phase's results. Merge with a merge commit (keep the task commits).
- Never commit directly to `main` after the initial docs commit.