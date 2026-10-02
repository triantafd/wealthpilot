# Document corpus conventions

This file sits **outside** `data/docs/` on purpose. The ingest script indexes
`data/docs/` only, so anything in here is never embedded and never cited.

## Why these documents exist

They are the ground truth for the RAG eval suite. Every case in
`evals/datasets/rag_qa.jsonl` names an `expected_source` of
`{document_id, page}`, and retrieval metrics only mean something if that
expected source is unambiguous. So each document is written to a shape the eval
suite can rely on.

## File format

One Markdown file per document, with YAML front matter:

```markdown
---
id: fee-schedule
title: Schedule of Fees and Charges
version: "2026.1"
effective: 2026-01-01
owner: Finance
classification: Client-facing
synthetic: true
---

# Schedule of Fees and Charges

...page 1 content...

<!-- page -->

...page 2 content...
```

| Field | Purpose |
|---|---|
| `id` | The `documents.id` primary key, and what citations and eval cases reference. Must equal the filename without `.md`. |
| `title` | `documents.title`. Shown in the citations drawer. |
| `version`, `effective` | Quoted so YAML reads `version` as a string, not a number. |
| `owner`, `classification` | Metadata, stored on the chunk for future metadata filtering. |
| `synthetic` | Always `true`. A machine-checkable assertion of working rule 7. |

Factsheets additionally carry `instrument_id` and `ticker`, which must match a
row the seed script generates.

## Pages

Markdown has no pages, but `chunks.page` is an integer and citations are
`document + page`. So pages are explicit: **`<!-- page -->` on its own line is a
page break.** Page numbers are 1-based, so the content before the first marker
is page 1.

An HTML comment is used because it is invisible in every Markdown renderer, so
the documents still read correctly in an editor or on GitHub.

Each page is written to stand alone. A chunk retrieved from page 3 should make
sense without pages 1 and 2, because that is the only context the model gets.

## Rules for writing a document

1. **Facts must be specific and unique.** "The advisory fee for portfolios
   between £2,000,001 and £5,000,000 is 0.55%" is checkable. "Fees are
   competitive" is not.
2. **A figure lives in one place.** If the same number appears in two
   documents, an eval case asking for it has two correct sources and `hit@k`
   becomes ambiguous. Cross-reference by document title instead of repeating
   the figure.

   *Rules* are different from figures: a rule may be restated where a reader
   would look for it, as the sector-exclusion prohibition is restated in the
   FAQ and on the Energy factsheet. Where that happens, one document is
   authoritative and the others must say so explicitly and name it. An eval case
   on a restated rule should expect the authoritative source.
3. **Agree with the seeded data.** Allocation percentages, score bands,
   capacity-for-loss definitions, account types, mandates, sectors and
   instrument details all appear in both the documents and
   `app/scripts/seed.py`. `tests/test_documents.py` asserts they match.
4. **Synthetic only.** Fictional firm (WealthPilot Advisers Ltd), fictional
   regulator (Office of Investment Conduct), fictional ombudsman. Email
   addresses use the RFC 2606 reserved `.test` and `.example` domains. No real
   person, firm, fund, regulator or figure.
5. **Vary the shape.** The corpus deliberately mixes prose, dense tables,
   procedures and question-and-answer format, because chunking strategy affects
   each differently and Phase 3 compares chunk sizes.

## Deliberate retrieval difficulty

A corpus where every answer is easy to find measures nothing. These are planted
on purpose:

| Difficulty | Where |
|---|---|
| Similar numbers in unrelated contexts | 0.55% fee tier vs 55% conservative bond allocation |
| A rule stated once and referenced elsewhere | The execution-only prohibition, stated in *Mandates* §2 and referenced from the FAQ |
| Semantically identical, lexically different statements | The sector-exclusion rule is stated four ways across three documents: "no internal approval level permits…", "There is no override.", "no approval route that permits it", "no approval route that overrides it" |
| Multi-hop | "Can this client hold this fund?" needs the client's restrictions, the fund's sector, and the concentration limit |
| Negation | A bond fund has no sector, so a sector exclusion does **not** catch it |
| Arithmetic | The tiered fee worked example |

Vector-only retrieval should struggle with the near-duplicates and the
negation. That is the point: Phase 3 has to show hybrid search and reranking
beating the Phase 1 baseline, and it cannot do that on a corpus with no hard
cases.

## Not yet present

Phase 6 adds a document containing hidden instructions, to test indirect prompt
injection. It is deliberately absent for now — the corpus should be clean while
the retrieval baseline is being established, so that a later drop in safety
metrics is attributable.
