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
| p50 latency | 266 ms | **1 ms** | −265 |
| Cost per query | — | — | see below |

Full-text search alone is **not** a replacement for vector search, and was not
expected to be: it matches strings, not meaning. It is kept because it fails on
a *different* set of cases, which is the entire premise of the hybrid in task 2.
Six cases gained hit@1 and fourteen lost it — if the two methods failed on the
same cases, fusing them could not help.

**Corrected.** This table first reported p50 latency as 2072 ms → 1 ms and cost
as $0.00032 → $0. Both compared a retrieval-only run against the frozen
baseline, whose latency and cost *include LLM generation*. The honest comparator
is vector retrieval-only, measured at 266 ms p50. Text search is therefore about
265x faster on retrieval, not 2000x, and the cost column is not meaningful at
all here: the eval harness prices only the chat model, so every retrieval-only
run reports $0 whether it embeds or not.

What survives the correction is the shape of it. Text search makes no model call,
so it avoids the embedding round trip that dominates retrieval latency — 266 ms
of the vector path is mostly one HTTP request to OpenAI, not pgvector work. That
is also why adding full-text to a hybrid is nearly free.

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

---

## Phase 3, task 2 — Hybrid with Reciprocal Rank Fusion

`search_hybrid` runs both strategies and fuses by RRF: a chunk's score is the sum
over strategies of `1 / (rrf_k + rank)`. Rank-based rather than score-based,
because cosine distance and `ts_rank` are not on the same scale and share no
zero, so averaging them would mean inventing a conversion.

### Result: a wash on aggregates, a real fix for the structural misses

| Metric | Vector | Full-text | **Hybrid** |
|---|---|---|---|
| MRR | 0.747 | 0.665 | **0.755** (+0.8) |
| hit@1 | **62.9%** | 51.4% | 61.4% (−1.4) |
| hit@6 | 97.1% | 92.9% | **97.1%** (±0) |
| p50 latency (retrieval only) | 266 ms | 1 ms | 253 ms |

Hybrid beats both parents on MRR and beats full-text everywhere, but **loses
1.4 points of hit@1 to plain vector search**. On 75 cases that is about one
case, and it is not noise: see the sweep below.

### How the known failures move

Required by the ROADMAP checklist. This is where hybrid earns its place.

| Case / tag | Vector | Hybrid | |
|---|---|---|---|
| `mandate-consent-01` | **0.00** | **0.25** | Was *never retrieved*. Now at rank 4, so it reaches the prompt |
| `prohibited-no-assessment-01` | **0.00** | **0.33** | Was *never retrieved*. Now at rank 3 |
| `fee-etf-trade-01` | 0.33 | 0.25 | Worse. Still not a retrieval problem — the model misreads a passage it has |
| tag `id-lookup` (n=4) | 0.521 | **0.833** | +0.312, and better than full-text alone (0.750) |
| tag `vague-phrasing` (n=5) | 0.417 | 0.367 | −0.050 |

**The two zeros becoming non-zero is the result that matters.** Both cases were
the reason hit@1 sat at 62.9%: the governing document was not in the top 6 at
all, so the model could not cite it however good the prompt was. A missing
passage is unrecoverable downstream; a passage at rank 3 is not.

`id-lookup` at 0.833 beating *both* parents is fusion working as advertised —
each strategy contributes cases the other misses.

### Parameter sweep: RRF is insensitive here

Fused offline from cached depth-30 rankings, so 20 configurations cost one pass
over the corpus instead of 20 eval runs.

| | depth 6 | depth 10 | depth 20 | depth 30 |
|---|---|---|---|---|
| `rrf_k` 5 | 0.751 | 0.750 | 0.746 | 0.751 |
| `rrf_k` 10 | 0.751 | 0.753 | 0.749 | 0.752 |
| `rrf_k` 20 | 0.751 | **0.755** | 0.754 | 0.753 |
| `rrf_k` 60 | 0.751 | **0.755** | 0.753 | 0.753 |
| `rrf_k` 120 | 0.751 | **0.755** | 0.753 | 0.753 |

MRR spans 0.746 to 0.755 — under one case of movement. And **hit@1 was exactly
61.4% in all twenty combinations**, which is the real finding: the hit@1
regression is structural to fusing these two rankers, not a tuning artefact. No
parameter choice recovers it, so none was chosen to.

Shipped: `rrf_k = 60` (the value from Cormack et al. and tied for best here) and
`retrieval_candidates = 10` (tied for best, and the least work). Within this
corpus the choice is arbitrary, and saying so is more useful than presenting 60
as tuned.

### Not made the default

`retrieval_mode` still defaults to `vector`. hit@1 is a **primary** Phase 3
metric and hybrid regresses it, with the sweep showing that cannot be tuned away.

The case for deciding after the reranker rather than now: hybrid's demonstrated
strength is pulling the right chunk *into* the candidate set — two cases went
from absent to ranks 3 and 4 — which is precisely what a reranker needs in order
to promote them to rank 1. Hybrid may be the right input to task 3 even though
it is not the right final answer on its own. Defaulting to it now would bank a
hit@1 regression for a benefit that only a later task realises.

---

## Phase 3, task 3 — Cross-encoder reranker

`cross-encoder/ms-marco-MiniLM-L-6-v2` through sentence-transformers, on CPU,
reordering a 20-candidate shortlist down to 6. Measured over **both** candidate
sets, to answer whether hybrid earns its place once a reranker is present.

### Accuracy and latency together

| Variant | MRR | hit@1 | hit@6 | p50 (retrieval only) |
|---|---|---|---|---|
| vector (baseline) | 0.747 | **62.9%** | 97.1% | **266 ms** |
| hybrid | 0.755 | 61.4% | 97.1% | 253 ms |
| **vector + rerank** | **0.759** | 61.4% | 97.1% | 568 ms |
| hybrid + rerank | 0.756 | 61.4% | 97.1% | 438 ms |

The reranker costs roughly **+300 ms per query** for 20 candidates, about 15 ms
per passage once the model is warm. First call is far worse — 9.5 s, almost all
of it loading weights — so a process that reranks must load the model at startup,
not on the first user's request.

The p50 ordering of the two rerank rows is noise: both pay one embedding round
trip, and that HTTP request dominates and varies more than the difference
between them.

### Does hybrid earn its place? No — not under reranking

**Rank 1 was identical between the two candidate sets in 73 of 75 cases.** The
cross-encoder picks the same winning passage whether it is handed vector
candidates or fused ones, so the second search buys a 97%-identical answer.
Vector + rerank also has marginally the better MRR of the two.

This is the question task 2 deferred, and the answer is cleaner than expected.
Hybrid's value was surfacing the right chunk *into* the candidate set; a
20-deep vector candidate set already contains it, so the fusion adds complexity
without changing what comes out.

### How the known failures move

| Case / tag | vector | hybrid | vector+rerank | hybrid+rerank |
|---|---|---|---|---|
| `mandate-consent-01` | 0.00 | **0.25** | 0.00 | 0.00 |
| `prohibited-no-assessment-01` | 0.00 | 0.33 | **0.50** | 0.33 |
| `fee-etf-trade-01` | **0.33** | 0.25 | 0.25 | 0.25 |
| tag `id-lookup` (n=4) | 0.521 | 0.833 | **1.000** | **1.000** |
| tag `vague-phrasing` (n=5) | **0.417** | 0.367 | 0.357 | 0.357 |

**`id-lookup` reaches 1.000** — every one of the four cases now ranks its
expected source first, from 0.521 under plain vector search. That is the
clearest win anywhere in Phase 3.

**But reranking un-fixed a case hybrid had fixed.** `mandate-consent-01` went
from unretrievable (0.00) to rank 4 under hybrid, and the reranker pushed it
back out of the top 6 entirely. The cross-encoder is confident and wrong about
that pair: the restating FAQ passage reads as a better answer to the question
than the governing document does, which is exactly the failure the case was
written to capture. A reranker is not a safety net; it is another ranker with
its own blind spots.

`vague-phrasing` gets worse under every variant tried in this phase. A vague
question gives neither term matching nor a cross-encoder anything to work with,
and it is now the strongest candidate for query rewriting rather than retrieval
tuning.

### hit@1 will not move

hit@1 is 61.4% for hybrid, for all twenty RRF parameter combinations, for
vector + rerank and for hybrid + rerank — and 62.9% for plain vector search.
Seven cases gain rank 1 under reranking and eight lose it, netting one case
worse, and no configuration tried in this phase beats the baseline on it.

That is worth stating plainly because hit@1 is a primary Phase 3 metric: **the
headline retrieval metric this phase set out to improve has not improved.** MRR
is up 1.2 points and `id-lookup` is solved, so the work is not worthless, but
the aggregate rank-1 story is flat.

### Decision

`retrieval_mode` stays `vector` and `retrieval_rerank` stays `false`, so the
default is unchanged and the baseline still describes what ships.

On the evidence the defensible default is **vector + rerank**: the best MRR,
`id-lookup` solved, at +300 ms and one new optional dependency. It is not
switched on yet because it costs 1.5 points of hit@1 and loses
`mandate-consent-01`, and two tasks remain — chunk sizes and the boilerplate
variant — either of which could change the picture. Deciding the default is the
phase's closing step, not this task's.

**Hybrid should probably be dropped** rather than carried further: 73-of-75
identical rank 1 under reranking is a weak case for maintaining a second search
path. Keeping the code costs nothing and the setting documents the finding.

### Dependency note

sentence-transformers lives in an optional `rerank` group because it pulls
torch. Installing it naively produced a **5.9 GB** virtualenv: torch defaults to
its CUDA build, and this machine has no GPU. Pinning the CPU wheel through
`[tool.uv.index]` brought it to **1.3 GB**. `[tool.uv.sources]` only binds
*direct* dependencies, so torch also had to be named explicitly in the group —
without that line the pin is silently ignored and the CUDA build returns.
