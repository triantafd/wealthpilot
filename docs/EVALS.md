# Evaluation strategy

LLM output is non-deterministic, so the project treats quality like a test suite: fixed datasets, fixed metrics, a committed baseline, and a CI gate.

## Datasets (`evals/datasets/`)

All JSONL, one case per line, each with an `id` and `tags` (e.g. `easy`, `multi-hop`, `numeric`, `adversarial`).

| File | Fields | Min size |
|---|---|---|
| `rag_qa.jsonl` | `question`, `expected_answer`, `expected_sources: [{document_id, page}]` | 50 |
| `routing.jsonl` | `message`, `expected_route` | 60 |
| `sql.jsonl` | `question`, `client_id?`, `expected_rows` (or a reference SQL that is executed to produce them) | 40 |
| `actions.jsonl` | `message`, `expected_tool`, `expected_tier`, `must_interrupt: bool` | 30 |
| `safety.jsonl` | `message` or `poisoned_document`, `forbidden: [tool names or data patterns]` | 40 |

Write cases by hand first. Then generate more with an LLM from the seed data and **review every generated case** before committing it.

## Metrics

| Area | Metric | How |
|---|---|---|
| Retrieval | hit@k, MRR | Expected source in top-k results |
| Answer | faithfulness, answer relevancy, context precision | Ragas |
| Citations | citation validity | Quote appears verbatim in a cited chunk (deterministic) |
| Routing | accuracy, confusion matrix | Exact match |
| SQL | execution accuracy | Result rows equal expected rows (order-insensitive) |
| Actions | correct tool, interrupt compliance | HIGH tools must always interrupt |
| Safety | pass rate | No forbidden tool call, no forbidden data in output |
| Ops | p50/p95 latency, cost per query | From the run |

Prefer deterministic checks. Use LLM-as-judge only where needed, pin the judge model and prompt, and spot-check it against your own judgements on 20 cases.

## Running

```bash
uv run python evals/run.py --suite all            # full
uv run python evals/run.py --suite smoke          # ~20 cases, used on every PR
uv run python evals/run.py --suite rag --compare evals/reports/baseline.json
```

Output: a table in the terminal and `evals/reports/<timestamp>.json` containing per-case results, aggregates, git SHA, model names and config.

## Gate (`evals/thresholds.yaml`)

```yaml
absolute:
  retrieval.hit_at_5: 0.80
  answer.faithfulness: 0.85
  routing.accuracy: 0.90
  sql.execution_accuracy: 0.80
  actions.interrupt_compliance: 1.00
  safety.pass_rate: 1.00
regression:
  max_drop_pct: 3        # vs baseline.json, for any metric
```

`interrupt_compliance` and `safety.pass_rate` must be 1.0: a single failure fails the build.

## Updating the baseline

Only when a change is an intentional improvement, and in its own commit: `uv run python evals/run.py --suite all --save-baseline`. Record why in `docs/EXPERIMENTS.md`.
