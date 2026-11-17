# Evaluation strategy

LLM output is non-deterministic, so the project treats quality like a test suite: fixed datasets, fixed metrics, a committed baseline, and a CI gate.

## Datasets (`evals/datasets/`)

All JSONL, one case per line, each with an `id` and `tags` (e.g. `easy`, `multi-hop`, `numeric`, `adversarial`).

| File | Fields | Min size |
|---|---|---|
| `rag_qa.jsonl` | `question`, `expected_answer`, `expected_sources: [{document_id, page}]` | 50 |
| `routing.jsonl` | `message`, `expected_route` | 60 |
| `sql.jsonl` | `question`, `client_id?`, `expected_rows` (or a reference SQL that is executed to produce them) | 40 |
| `actions.jsonl` | `message`, `expected_outcome` (`propose`/`ask_account`/`refuse`), `expected_account`, `expected_tool`, `must_interrupt: bool`, `why` | 30 |
| `safety.jsonl` | `message` or `poisoned_document`, `forbidden: [tool names or data patterns]` | 40 |

Write cases by hand first. Then generate more with an LLM from the seed data and **review every generated case** before committing it.

Every case carries a **`why`**: one sentence saying why that answer is right
*for the firm*, argued from the documents and the firm's obligations rather
than from what a prompt currently says. That is what makes a dataset the
authority — when a case fails, the system is wrong until someone argues
otherwise, and a case is never reworded because it fails.

The `_slice` files (`routing_slice.jsonl`, `actions_slice.jsonl`) are the
Phase 4a versions, smaller than the sizes above. Phase 4 grows them into
`routing.jsonl` and `actions.jsonl` and retires them.

**`actions_slice.jsonl` runs against the action agent directly, not the
graph.** Three of its cases are questions that reach the agent only by a
routing mistake; through the whole graph the supervisor would divert them and
they would pass while testing nothing. `routing_slice.jsonl` exercises the
supervisor instead, and the integration tests exercise the two together.

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
comparison. On the frozen baseline MRR is 0.747 and hit@1 is 62.9% — over a
third of cases do not put the right source first, which is exactly what
reranking should fix.

hit@k is kept rather than dropped. It becomes informative again as the corpus
grows, and ARCHITECTURE section 2 anticipates adding public regulatory PDFs as
a harder retrieval test. Read a saturated hit@k as a statement about corpus
size, not as a result.

## Running

```bash
uv run --project backend --group evals python evals/run.py --suite all
uv run --project backend python evals/run.py --suite smoke   # used on every PR
uv run --project backend --group evals python evals/run.py --suite rag \
    --repeat 3 --compare evals/reports/baseline.json
```

The smoke suite runs on every pull request, in the `evals` job of
`.github/workflows/ci.yml`, against a database migrated, seeded and ingested
from empty. The full suite runs on a button — Actions → CI → **Run workflow**,
with the suite as an input — rather than on a nightly cron; `docs/NOTES.md` says
why.

`--group evals` installs ragas, which is deliberately not a runtime dependency
— the service never scores itself. The smoke suite does not need it (the judge
is off by default there); `rag` and `all` do, and the runner now says so and
exits before spending a suite's worth of calls rather than after.

Output: a table in the terminal and `evals/reports/<timestamp>.json` containing per-case results, aggregates, git SHA, model names and config.

## Gate (`evals/thresholds.yaml`)

The file is committed and the numbers in it are measured; read it rather than
this section for the current values. What matters here is the two rules it
follows, both of which were learned by getting them wrong first.

**Floors are per suite.** The proposal this section used to carry set
`retrieval.mrr: 0.70` for everything. Checked against the 23 smoke runs and 3
rag runs on record, that fails **23 of 23** smoke runs — 0.70 is the rag
suite's level and smoke sits at 0.6927. They are different sets of questions,
so there is no single right number, and a gate that fails every pull request
gets switched off within a day.

**Floors come from what the current configuration measures**, floored to two
decimals for one notch of headroom — not from a target. "Current
configuration" matters because the `all` suite's history contains retrieval
variants that were measured and abandoned, one at `retrieval.mrr` 0.6645; the
lowest value *ever recorded* would put the floor underneath a config nobody
runs, and a real regression could land inside it.

Thresholds for `routing.accuracy`, `sql.execution_accuracy`,
`actions.interrupt_compliance` and `safety.pass_rate` are **not** in the file
yet: those suites arrive in Phases 4 and 6. A floor for a metric nothing
produces gates nothing and gives the impression something is protected.
`routing.accuracy` is a special case — it reads 1.0000 across 15 runs, but
Phase 4 grows that dataset from a 15-case slice to 60 or more and the number
will fall for that reason alone, so a floor set now would fail on the commit
that improves coverage.

### Only deterministic metrics gate

A run report carries a `gateable` list, and the gate reads it rather than
trusting the YAML: a floor for a metric outside that list is ignored, so
editing the file is not a way to gate something noisy.

**The list used to be longer, and the claim made for it was false.** It was
written from the reasoning that a calculation over fixed inputs must be
deterministic, before anyone measured the repeat-run spread, and this section
stated that every entry "moves by exactly zero". Measured within a single
commit, three of them do not:

| metric | moves by | where |
|---|---|---|
| `citations.validity` | 0.074 | smoke, one commit, 3 runs |
| `answer.refusal_correct` | 0.037 | smoke; and bistable, see `docs/NOTES.md` |
| `answer.must_include` | 0.062 | rag, commit `33e7026`, same code |

All three are report-only. What gates is retrieval — MRR, hit@1, hit@6 — plus
`answer.no_prose_refs`, which has read 1.0000 in every run since Phase 5, and
`ops.errors`, which fails the build on any errored case. Those four do move by
exactly zero, measured across 23 smoke runs spanning several commits.

They are not all noise-free for the same reason: the three that move depend on
what the model *wrote*, not only on what was retrieved. `answer.must_include`
is the one worth revisiting — its spread on smoke is zero across every repeat
we have, and only the larger rag suite has caught it moving.

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
