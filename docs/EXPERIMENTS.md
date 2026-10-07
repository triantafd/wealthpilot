# Experiments

One entry per retrieval, prompt or model experiment. See
`.claude/skills/improve-retrieval/SKILL.md` for the format.

---

## Known failures carried into Phase 3

Cases the Phase 1 baseline gets wrong, recorded rather than fixed, so the
retrieval work has concrete targets instead of an aggregate to chase.

### `fee-etf-trade-01` — right passage retrieved, wrong one used

*"Does he get stung for buying an ETF?"* The answer should be £9.95 per trade,
from `fee-schedule` page 2.

The correct chunk **is retrieved, at rank 3**. The model answers from the two
factsheets at ranks 1 and 2 instead, quotes the line *"No transaction charge —
this is a fund, not an ETF"*, and concludes the opposite of what it says: that
funds "including ETFs" carry no charge. The factsheet is distinguishing *that
instrument* from an ETF.

This is not a retrieval miss and not a citation failure — the quote is verbatim
and from a retrieved passage, so `citations.validity` scores it as valid. It is
a comprehension failure that only `must_include` catches, by noticing £9.95
never appears.

Two things follow for Phase 3:

* **Reranking has a concrete target here.** Moving `fee-schedule#p2` from rank 3
  to rank 1 would likely fix it, and this case is the test.
* **Faithfulness will not catch this class of error.** The answer is faithful to
  the passages it used; they were simply the wrong ones. Read faithfulness
  alongside MRR, never instead of it.

### `mandate-consent-01` and `prohibited-no-assessment-01` — retrieval misses

Both lose to a `compliance-faq` chunk that restates the rule, while the
authoritative document is never retrieved. The FAQ's question-shaped language
matches a question better than the governing document's prose does. These are
the two cases behind `retrieval.hit_at_1` sitting at 62.9%.

---

## Prompt: page and section references in answer prose

**Problem.** The model wrote page references into its answers — *"See Mandates
and Rebalancing, page 1"* — which nothing checks. The structured citation's page
is verified against what was retrieved; one written into a sentence is not, and
the same question produced "page 1" in one run and "page 5" in another.

**Attempt 1 — rewrite rule 5 to forbid pages.** Prose references fell from 11 of
75 answers to 1. But `must_include` dropped 93.3% → 92.0% and two cases
regressed, with **identical retrieval** in both, so the prompt was the only
variable. Rewriting rule 5 had re-centred it from *name the governing document
when you cannot cite it* to a general instruction to name documents, and
`fee-etf-trade-01` switched from naming `fee-schedule` to naming the factsheets.

**Attempt 2 — restore rule 5 verbatim, add the prohibition as a separate rule.**
`rebalance-eo-02` recovered, confirming the diagnosis. But `fee-etf-trade-01`
stayed broken, and clean answers only reached 92% — a prompt rule moved the
number seven points and stalled. A gate at 1.0 was unreachable by prompting.

**Resolution — strip them in code.** The prompt is back to exactly what the
baseline measured. References attached to a document name, or sitting in
brackets, are removed from the answer after generation; a bare "section 12" is
left alone because it may be content. The count before stripping is reported so
the fix cannot hide what the model does.

**Lesson.** A prompt rule is a prior, not a guarantee. Where a property must
hold every time, enforce it in code and use the prompt to reduce how often the
code has to intervene.
