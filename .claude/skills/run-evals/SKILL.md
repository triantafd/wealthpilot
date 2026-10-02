---
name: run-evals
description: How to run, compare and interpret WealthPilot's eval suite (retrieval, Ragas answer quality, routing, SQL execution accuracy, actions, safety, latency, cost). Use this after any change to prompts, retrieval, chunking, models, routing or tools, whenever the user asks whether something got better or worse, and before marking a roadmap task done.
---

# Run evals

## Pick the suite

- `smoke` (~20 cases, fast): after every behaviour change.
- `rag`, `routing`, `sql`, `actions`, `safety`: when working on that area.
- `all`: before a PR is merged, and for README numbers.

## Run with a comparison

```bash
uv run --project backend python evals/run.py --suite smoke --compare evals/reports/baseline.json
```

To measure a change fairly, run the same suite on the code before and after the change (stash or check out the previous commit) with identical config, rather than comparing to an old report.

## Report results like this

| Metric | Before | After | Δ |
|---|---|---|---|

Then list:
- Any metric under its threshold in `evals/thresholds.yaml`.
- Any metric that dropped more than `max_drop_pct`.
- The 3–5 most informative failing cases, with case id, what was expected, what happened, and a likely cause (retrieval miss, wrong route, bad SQL, judge disagreement).

## Interpreting

- LLM metrics are noisy. A change under ~2 points on a small suite may be noise; rerun or use the full suite before claiming it.
- If faithfulness drops but hit@k is stable, look at the prompt or context size, not retrieval.
- If routing drops, check the confusion matrix for which pair of routes is being confused.
- Any failure in `actions.interrupt_compliance` or `safety.pass_rate` is a blocker; stop and fix it first.

## Do not

- Do not edit dataset cases to make a run pass. If a case is wrong, say so and propose a fix for the user to approve.
- Do not overwrite `baseline.json` unless the user asks (`--save-baseline`).
