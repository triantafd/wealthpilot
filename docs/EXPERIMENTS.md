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

---

## Phase 3, task 1 — Full-text search with `tsvector`

Measured with `--suite all --retrieval-only --retrieval text` against the frozen
baseline. Retrieval-only, so no LLM and no judge: every number here is
deterministic and reproducible from the report in `evals/reports/`.

### Result: worse overall, better where predicted

| Metric | Vector baseline | Full-text | Delta |
|---|---|---|---|
| MRR | 0.747 | **0.665** | −0.082 |
| hit@1 | 62.9% | **51.4%** | −11.4 |
| hit@6 | 97.1% | 92.9% | −4.3 |
| p50 latency | 2072 ms | **1 ms** | −2071 |
| Cost per query | $0.00032 | **$0** | — |

Full-text search alone is **not** a replacement for vector search, and was not
expected to be: it matches strings, not meaning. It is kept because it fails on
a *different* set of cases, which is the entire premise of the hybrid in task 2.
Six cases gained hit@1 and fourteen lost it — if the two methods failed on the
same cases, fusing them could not help.

The latency and cost columns are not a rounding artefact. Text search makes no
model call at all, so it skips the ~90 ms embedding round trip and the whole
per-query cost. A hybrid pays for one embedding, not two searches.

### Where it wins

| Tag | n | Vector | Full-text | Delta |
|---|---|---|---|---|
| `id-lookup` | 4 | 0.521 | **0.750** | +0.229 |
| `multi-hop` | 8 | 0.535 | **0.729** | +0.194 |
| `filtered` | 3 | 0.583 | **0.667** | +0.083 |

**`id-lookup` is the result this task was predicted to produce, and it held —
with a caveat worth stating.** Two of four cases improved, two were unchanged,
**none regressed**:

| Case | Vector | Full-text | Note |
|---|---|---|---|
| `id-ticker-ocf-01` | 0.33 | **1.00** | The clean win. "MER012 — what's the OCF on that one?" moved from rank 3 to rank 1 |
| `instrument-ocf-01` | 0.25 | **0.50** | Also `filtered`, so this improvement is *within* one document — an easier task than unfiltered retrieval, and not fully full-text's doing |
| `id-instrument-name-01` | 1.00 | 1.00 | Vector already ranked it first |
| `id-ticker-risk-01` | 0.50 | 0.50 | Still returns the wrong factsheet first |

So the honest claim is **one unfiltered case genuinely fixed**, on a four-case
tag. The direction matches the hypothesis and nothing regressed, but n=4 cannot
carry a strong conclusion on its own.

`multi-hop` improving by more than `id-lookup` was not predicted. A multi-hop
question names entities from two documents, and term matching finds both where a
single averaged embedding lands between them.

### Where it loses

| Tag | n | Vector | Full-text | Delta |
|---|---|---|---|---|
| `negation` | 4 | 0.875 | 0.500 | −0.375 |
| `policy` | 7 | 0.786 | 0.529 | −0.257 |
| `vague-phrasing` | 5 | 0.417 | **0.190** | −0.227 |

All three are the same failure. A negation question ("can he *not* hold crypto?")
shares every content word with the passage that says he can, and term matching
cannot tell them apart. `vague-phrasing` was already the worst tag in the suite
and full-text makes it worse: a vague question has no rare terms to match on.

Formal questions score 0.861 and informal ones 0.517 — a far wider split than
vector search shows, because formal phrasing reuses the documents' own
vocabulary.

### The limitation to understand before task 2

**`ts_rank` has no IDF.** Every query term counts equally, so a chunk matching
several common words outranks the one chunk matching a rare identifier. This is
why `id-ticker-risk-01` still fails: "MER011 — how risky is it on the 1-7
scale?" matches "risk" and "scale" across many passages, and `mer011` gets no
extra weight for appearing exactly once in the corpus.

Also worth knowing: `to_tsvector('english', 'IN-0011')` produces `'-0011'`, not
`'in-0011'` — the `IN` prefix is dropped as an English stopword. Matching still
works because `to_tsquery` normalises identically, but two identifiers whose
prefixes are both stopwords would collide. `MER012` survives intact.

### Variants tried and rejected

Recorded so the next person does not repeat them.

| Variant | MRR | Why rejected |
|---|---|---|
| `plainto_tsquery` (AND) | — | **Returned zero rows for all 75 cases.** A prose question's every word never appears in one passage |
| `websearch_to_tsquery` | 0.091 | ANDs by default too; near-total recall failure |
| OR + `ts_rank_cd` | 0.504 | Cover density rewards query terms appearing *close together*, which suits phrase search, not a question whose terms are scattered |
| OR + `ts_rank` | **0.611** | **Chosen.** Best of the four |
| Rare-terms weighting (drop query terms with corpus df above a threshold, as a cheap stand-in for the IDF `ts_rank` lacks) | 0.504–0.526 at thresholds 10%, 20%, 35% | **Worse than plain OR at every threshold.** Dropping common terms loses more signal than the spurious matches it avoids. Real IDF may still help — this crude proxy does not, and a proper BM25 would mean maintaining a lexeme-frequency table |

The rare-terms result was the surprise. It was the obvious fix for the missing
IDF and it made things worse at all three thresholds, which is why the limitation
is carried into task 2 rather than patched here.

### Not changed

`retrieval_mode` defaults to `vector`. Full-text is opt-in via the setting or
`--retrieval text`, because on this corpus it is worse overall. The baseline is
untouched.
