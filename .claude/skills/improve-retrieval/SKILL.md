---
name: improve-retrieval
description: Protocol for any retrieval experiment in WealthPilot — chunk size or overlap, embedding model, hybrid search, Reciprocal Rank Fusion weights, reranker, top-k, query rewriting, metadata filters. Use this whenever someone wants to make RAG answers better, fix wrong or missing sources, or try a new retrieval technique.
---

# Improve retrieval

Retrieval changes are experiments. Each one gets a hypothesis, a measurement and a written result.

## 1. Diagnose first

Run `--suite rag` and look at failing cases before changing anything. Classify each failure:
- **Not retrieved:** expected source not in top-k → retrieval problem.
- **Retrieved but ranked low:** → fusion or reranking problem.
- **Retrieved, answer still wrong:** → prompt or context problem, not retrieval.

## 2. One variable at a time

Write the hypothesis in `docs/EXPERIMENTS.md` before running, e.g. "Exact fee codes are missed by vectors; adding full-text search should raise hit@5 on `numeric`-tagged cases."

Make the change a config option (`app/config.py`) rather than replacing the old behaviour, so both stay runnable.

## 3. Re-ingest if needed

Chunking or embedding changes require re-ingestion: `uv run python -m app.scripts.ingest --force`. Note the time and embedding cost.

## 4. Measure

Run `--suite rag` for the old and new config. Record hit@5, MRR, faithfulness, p95 latency and cost per query. Slice by tags to see where it helped or hurt.

## 5. Record

Append to `docs/EXPERIMENTS.md`:

```
## <date> — <short name>
Hypothesis:
Change:
Results: (table before / after)
Decision: keep / revert, and why
```

Keep a change only if it improves the target metric without breaking thresholds, and the latency/cost trade-off is acceptable. Mention that trade-off explicitly.
