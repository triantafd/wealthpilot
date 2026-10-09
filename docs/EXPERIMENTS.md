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

---

## Diagnostic — is the hit@1 ceiling in the ranker or the data?

Free: six method runs were already on disk, so this is arithmetic over saved
reports with no model calls. Run because the inference "every method gave the
same hit@1, so it must be the data" does not hold on its own — the *count* was
the same but the failing cases were not, with reranking gaining seven and losing
eight. A real ceiling means the *same* cases failing everywhere.

Methods compared: vector, full-text, hybrid at depth 20 and 10, vector+rerank,
hybrid+rerank. Scored cases: 70 of 75 (five expect refusals and have no source).

| | cases |
|---|---|
| rank 1 under **every** method | 27 |
| rank 1 under **some** method | 26 |
| rank 1 under **no** method | **17** |

So there is both a hard core and real headroom: 26 cases change hands between
methods, and 17 resist all six. **All 17 reach the top 6 under at least one
method**, so nothing is unretrievable — every one of them is a rank-2-to-6
ordering problem, which is recoverable.

### The data hypothesis is confirmed, and it has a name

`compliance-faq` is a wrong rank-1 winner in **all 17** of the never-rank-1
cases, and the *only* wrong winner in six of them.

| Document | Wrong rank-1s |
|---|---|
| **`compliance-faq`** | **96** |
| `client-restrictions` | 17 |
| `factsheet-meridian-uk-corporate-bond` | 14 |
| `account-types` | 11 |

It takes **20.8% of all retrieved slots but 57.1% of all wrong rank-1s** —
punching about 2.7x above its weight.

The cause is a property of the corpus, not a defect in any ranker. The FAQ
restates rules from every other document in question-shaped language, and the
golden set asks questions. For a question, a passage that *reads like an answer
to that question* legitimately looks like the better match; the governing
document states the rule as prose. No ranker is wrong to prefer it. This is why
vector, term matching, fusion and a cross-encoder all make the same mistake.

### This demotes the boilerplate hypothesis

Only **34 of 168 wrong rank-1s (20%) are a `#p1` chunk at all**, and that
includes p1 chunks from documents other than the FAQ. The shared
title-and-disclaimer text lives exclusively in p1 chunks, so it can explain at
most a fifth of the symptom — and `compliance-faq` wins wrongly from p1, p2 and
p3 alike.

The boilerplate variant is still worth running, because it is cheap and a
negative result is worth recording. But it should be expected to produce a small
effect, and this diagnostic is the reason to expect that rather than a surprise
after the fact.

### Hypothesis, not yet tested: the reranker prefers FAQ-style text

Recorded with the two observations that prompted it, and deliberately untested.

1. `mandate-consent-01` was unretrievable under vector search, reached rank 4
   under hybrid, and the **reranker pushed it back out of the top 6** in favour
   of a `compliance-faq` passage.
2. `prohibited-no-assessment-01` fails in the same shape — the FAQ restates the
   rule, the governing document rules.

Two cases is a hypothesis. The diagnostic above, however, suggests the
hypothesis as originally framed is **too narrow**: the preference is not
specific to the cross-encoder. Every method tried prefers `compliance-faq`, so
any test should compare how strongly each method over-ranks it rather than
treating it as a reranker quirk.

The test, when it is run: score `compliance-faq` chunks against
governing-document chunks for the same question across the whole set, and check
whether the gap is wider for the cross-encoder than for cosine distance. Not run
yet.

### A reproducibility gap this exposed

The six runs had to be identified **by their MRR values**, because a report's
`config` block records `llm_model`, `embedding_model`, `embedding_dimensions`
and `retrieval_top_k` — but not `retrieval_mode`, `retrieval_rerank` or the RRF
parameters. A report should say what produced it. Worth fixing before the next
variant, or the reports pile up indistinguishable.

---

## Phase 3, task 4 — Excluding repeated boilerplate from embeddings

`embed_strip_boilerplate` removes a chunk's leading H1 title and the identical
`**WealthPilot Advisers Ltd** — fictional firm, synthetic document.` line from
**what gets embedded**. The stored content is untouched, so a citation still
quotes the document as written and the generated `tsvector` still indexes the
text. The hypothesis is about vector similarity, so the experiment changes only
the vectors.

A setting rather than an edit to ingestion, so both corpora can be rebuilt on
demand. Changing it needs a re-ingest with `--force`, because the vectors on
disk were produced under whichever value was set at the time.

> **Adopted, then reverted.** The table below is a retrieval-only measurement.
> A full-suite run afterwards showed it costs `answer.refusal_correct`, which is
> gated at 1.00, so the default went back to off. The full story is in
> "Adopting it, and reverting" below.

### Result: the only thing in Phase 3 that improved hit@1

| Variant | MRR | hit@1 | hit@6 |
|---|---|---|---|
| vector (baseline) | 0.747 | 62.9% | 97.1% |
| **vector + strip boilerplate** | **0.754** (+0.8) | **64.3%** (+1.4) | 97.1% |
| vector + rerank | **0.759** | 61.4% | 97.1% |
| vector + strip + rerank | 0.756 | 61.4% | 97.1% |

Every ranking method tried in this phase left hit@1 at 61.4% or 62.9%. A
one-line change to what gets embedded moved it to **64.3%** — the best figure
recorded, and the diagnostic's prediction that the ceiling was in the data
rather than the ranker, confirmed from a second direction.

The effect was expected to be small, and it is small. It is also the only
positive one.

### The reranker overrides it

Reranking the improved candidate set returns hit@1 to exactly **61.4%** — the
same value it produces from plain vector candidates, from hybrid candidates, and
from fused candidates at twenty different RRF settings. The cross-encoder's
rank-1 choice is insensitive to how good the shortlist it was handed is, which
is a stronger version of the 73-of-75 finding in task 3 and further evidence
that its preference is systematic rather than incidental.

### Two implementations were wrong before this one

Worth recording, because both looked right and quietly tested something else.

1. **Stripped every markdown heading, anywhere in the chunk.** Turned
   `## 2. Annual allowances` followed by a table into a bare table.
2. **Stripped every *leading* heading.** Chunking splits on headings, so a chunk
   usually starts with one — same outcome for most chunks.

A section heading is the most retrievable text in a chunk. Removing it tests a
far more aggressive hypothesis than the repeated-boilerplate one, and would have
been reported as "excluding boilerplate" either way. The shipped version removes
a leading **H1** only: in this corpus `#` is the document title and `##`/`###`
are sections.

---

## Free check — does the FAQ winning cause wrong answers, or only weaker citations?

Asked of the 17 cases that never reach rank 1 under any method, using the
existing full-suite baseline report. No new runs.

| | cases |
|---|---|
| Answer still correct (`must_include` passes) | **14** |
| Answer wrong | **2** |
| Prose-only, nothing to judge | 1 |

**`compliance-faq` winning rank 1 costs citation authority, not correctness.**
The model reads all six retrieved passages, and the FAQ restates the rule
accurately, so the figure comes out right — attributed to the FAQ rather than to
the document that governs.

The two wrong answers are `mandate-consent-01` and
`prohibited-no-assessment-01`, and they are wrong for a different reason: in
those the governing document is outside the top **six**, not merely below rank
one. A passage at rank 4 still reaches the prompt; a passage that was never
retrieved cannot be cited however the ranking is ordered.

### So the FAQ-exclusion diagnostic is not warranted

The plan was to measure retrieval with `compliance-faq` removed from the index
*if* the FAQ caused wrong answers. On this evidence it does not, in 14 of 16
judgeable cases. Removing it would measure a corpus nobody will ship against to
diagnose a problem that costs attribution rather than accuracy.

What this reframes: **hit@1 is the wrong metric to chase for answer
correctness.** hit@6 governs correctness, it is 97.1%, and no variant in this
phase moved it. hit@1 governs whether the citation points at the authoritative
document, which matters for a compliance tool but is a different claim from
getting the answer right.

The two genuinely-wrong cases are hit@6 misses. Hybrid is the only variant that
fixed either — `mandate-consent-01` from unretrievable to rank 4 — and the
reranker then undid it.

---

## Phase 3, task 5 — Chunk sizes: not run

Skipped deliberately, with the reason recorded rather than left as an untouched
checkbox.

The diagnostic established that 17 of 70 cases resist every ranking method and
that `compliance-faq` is a wrong rank-1 winner in all 17, taking 20.8% of
retrieved slots and 57.1% of wrong rank-1s. The free check above then
established that this costs citation authority rather than correctness, and that
the two genuinely wrong answers are hit@6 misses.

A chunk-size sweep would move the boundaries of passages whose *content* is the
problem. It would not stop a question-shaped restatement from matching a
question better than the prose that governs it. Three re-ingests and three
measurements is real cost against a hypothesis this phase's own evidence points
away from.

It is a cheap experiment to run later if the FAQ work does not explain the
remaining failures, and `chunking.py` already keeps its sizes in one place and
its function pure, so nothing blocks it.

---

## Phase 3 — adopting boilerplate stripping, and reverting it

Made the default on the retrieval-only evidence above, then re-measured the
baseline over the full suite, three runs, on a clean tree. The retrieval numbers
reproduced exactly. Two **gated** answer metrics did not.

| Metric | Old baseline | With stripping | Spread |
|---|---|---|---|
| `retrieval.mrr` | 0.747 | **0.754** | ±0.000 |
| `retrieval.hit_at_1` | 62.9% | **64.3%** | ±0.000 |
| `retrieval.hit_at_6` | 97.1% | 97.1% | ±0.000 |
| `citations.validity` | 0.984 | 0.987 | ±0.011 |
| **`answer.must_include`** | 0.933 | **0.898** | ±0.013 |
| **`answer.refusal_correct`** | **1.000** | **0.973** | **±0.000** |
| `answer.faithfulness` | 0.870 | 0.841 | ±0.007 |

`answer.refusal_correct` is gated at 1.00 in `docs/EVALS.md`. It failed
identically in all three runs — ±0.000 spread, so reproducible rather than
noise. The default was reverted: 1.4 points of rank-1 ordering is not worth a
reproducible refusal failure in a tool whose job includes declining to answer.

### Ruled out first: did stripping reach the text the model reads?

The obvious explanation would be that stripping leaked past the embedding into
the passage text, so the model was reading truncated documents. **It did not.**
Checked against the index built under stripping: all 54 chunks beginning with a
heading and all 8 carrying the disclaimer retained that text in `content`, and
`format_passages` renders `chunk.content`. The model read the documents as
written.

So the cause is not truncated passages. It is that **changing the vectors
changed which passages arrive, and in one case their order.**

### Which cases failed, and why

Three `must_include` cases were lost and one gained. They do not share a cause,
which is why the aggregate alone would have been misleading.

| Case | Retrieval changed? | Cause |
|---|---|---|
| `fee-floor-01` | **yes** | `fee-schedule#p1`, the only passage containing £1,500, **dropped out of the top 6** and was replaced by `fee-schedule#p3`. The model then correctly said the documents did not answer — so this one case cost `must_include` *and* `refusal_correct` at once. Genuinely caused by stripping |
| `rebalance-eo-02` | **no — byte-identical** | The answer says "must have a specific instruction"; `must_include` wants "specifically instructed". Pure wording variation, **not caused by the change** |
| `restriction-bond-01` | yes, but the expected source was present before and after | Ranks 4 and 6 swapped within the factsheets. The answer is substantively right and omits the phrase "underlying holdings". Ambiguous attribution |

So of three losses, **one is attributable, one is demonstrably not, and one is
ambiguous** — against a ±0.013 spread of roughly one case. On its own
`must_include` would have been a weak signal.

### The refusal failure is the serious one, and it is not really retrieval

`refusal-poa-policy-01` asks about powers of attorney, which the corpus does not
cover, and must refuse. Under stripping the model answered:

> "An `execution_only` account must **never** be rebalanced by the firm, even
> when it breaches allocation bands."

Confident, well-cited, and irrelevant to the question.

**The same six passages were retrieved before and after.** The only difference
is that ranks 1 and 2 swapped — `mandates-and-rebalancing#p1` moved ahead of
`compliance-faq#p1`. Identical passage set, different order, and a correct
refusal became a confident wrong answer.

That is a prompt-robustness problem wearing a retrieval problem's clothes. If
the refusal decision turns on which of two passages is listed first, then it
will keep turning on that, and any future retrieval change can flip it back.
`fee-floor-01` is the second refusal failure and is the opposite error — it
refused when the answer was simply absent from its passages, which is arguably
correct behaviour that the case's `expect_refusal=False` does not allow for.

### Follow-up recorded for Phase 6

Refusal hardening belongs with the guardrails work, not with retrieval, and is
noted in the Phase 6 checklist. The specific target: an out-of-scope question
must refuse regardless of the order its passages arrive in, measured by running
the `out-of-scope` and `adversarial` tags under several retrieval
configurations rather than one.

If that holds, boilerplate stripping becomes adoptable — the retrieval gain is
real and costs nothing at runtime — and this entry is the evidence for trying it
again rather than rediscovering it.

### The lesson worth keeping

A retrieval change is not validated by retrieval metrics. Four variants in this
phase were measured retrieval-only, which is cheap and deterministic and made
the comparisons clean. The one that was adopted then failed on metrics the
retrieval-only runs could not see. **Measure the answer side before changing a
default**, not after.
