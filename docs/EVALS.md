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
| Retrieval | **MRR, hit@1** (primary), hit@k (secondary) | Expected source in top-k results |
| Answer | faithfulness, answer relevancy, context precision | Ragas |
| Citations | citation validity | Quote appears verbatim in a cited chunk (deterministic) |
| Routing | accuracy, confusion matrix | Exact match |
| SQL | execution accuracy | Result rows equal expected rows (order-insensitive) |
| Actions | correct tool, interrupt compliance | HIGH tools must always interrupt |
| Safety | pass rate | No forbidden tool call, no forbidden data in output |
| Ops | p50/p95 latency, cost per query | From the run |

Prefer deterministic checks. Use LLM-as-judge only where needed, pin the judge model and prompt, and spot-check it against your own judgements on 20 cases.

### Why MRR and hit@1 lead the retrieval metrics

The corpus is 10 documents and 81 chunks, so `k=6` already covers 7.4% of it.
hit@6 is close to saturated by the size of the corpus rather than by the
quality of retrieval: the Phase 1 vector-only baseline scores 97% on it, which
leaves three points of headroom and makes it a weak gate for the Phase 3
comparison. On the same run MRR is 0.767 and hit@1 is 67% — a third of cases do
not put the right source first, which is exactly what reranking should fix.

hit@k is kept rather than dropped. It becomes informative again as the corpus
grows, and ARCHITECTURE section 2 anticipates adding public regulatory PDFs as
a harder retrieval test. Read a saturated hit@k as a statement about corpus
size, not as a result.

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
  retrieval.mrr: 0.70
  retrieval.hit_at_1: 0.60
  citations.validity: 0.95
  answer.refusal_correct: 1.00
  routing.accuracy: 0.90
  sql.execution_accuracy: 0.80
  actions.interrupt_compliance: 1.00
  safety.pass_rate: 1.00
regression:
  max_drop_pct: 3        # vs baseline.json, for any gateable metric
```

`interrupt_compliance` and `safety.pass_rate` must be 1.0: a single failure fails the build.

### Only deterministic metrics gate

A run report carries a `gateable` list, and CI reads it rather than hardcoding
one that silently drifts. It contains the metrics whose value is a calculation
over fixed inputs: MRR, hit@1, hit@k, citation validity, `must_include`, and
refusal correctness. Measured across three repeat runs of the same suite, every
one of them moves by **exactly zero** — so a drop is a regression, not a mood.

The Ragas metrics — faithfulness and answer relevancy — are **report-only**.
Two reasons:

1. **An LLM judge is itself non-deterministic.** Until the repeat-run spread
   says how much it moves on its own, a threshold would fire on noise.
2. **Relevancy is partly misaligned with what we want.** It penalises answers
   that add caveats, and the research prompt explicitly requires them — rule 7
   says an answer that omits a condition is wrong even when the words are
   right. A relevancy *drop* can therefore mean the answer got more correct.

Faithfulness is the more trustworthy of the two, but it measures **grounding,
not correctness**: an answer built confidently on wrongly retrieved passages is
faithful to them. Read it alongside MRR, never instead of it.

## Updating the baseline

Only when a change is an intentional improvement, and in its own commit. Record
why in `docs/EXPERIMENTS.md`.

**Commit the code first, then measure, then commit the baseline file.** In that
order, so the `git_sha` in the report names a clean tree that reproduces the
numbers:

```bash
git commit -am "feat: the change being measured"      # 1. code lands first
uv run --project backend python evals/run.py \
    --suite all --repeat 3 --save-baseline            # 2. measure a clean tree
git commit -m "eval: update the baseline" \
    evals/reports/baseline.json                       # 3. the file follows
```

Doing it the other way round produces a baseline whose sha names the *parent*
commit, so checking out that sha does not reproduce the numbers. Reports record
`git_dirty` to make that visible when it happens, but the fix is the ordering,
not the flag.

Use `--repeat 3`: a single run is one sample of a process with an LLM judge in
it, and the spread is what tells you whether the next run's movement is a result
or noise. For the Phase 1 baseline every deterministic metric moved by ±0.000
and faithfulness by ±0.011, which is why a faithfulness change under about three
points should not be reported as an improvement.
