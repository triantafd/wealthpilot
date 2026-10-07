"""Dataset loading, case scoring and report shaping. No model, no database."""

import json
from pathlib import Path

import pytest

from app.evals.dataset import SMOKE_IDS, DatasetError, EvalCase, load_cases
from app.evals.report import build_report, render_table
from app.evals.runner import aggregate, score_case
from app.rag.generate import AnswerResult, Citation, CitationCheck, TokenUsage
from app.rag.retrieve import RetrievedChunk

FEE_TEXT = "The minimum annual advisory fee is £1,500, regardless of portfolio value."


def case(**overrides: object) -> EvalCase:
    defaults = {
        "id": "zt-case",
        "question": "What is the minimum fee?",
        "expected_answer": "£1,500.",
        "must_include": ["£1,500"],
        "expected_sources": [{"document_id": "fee-schedule", "page": 1}],
        "tags": ["numeric"],
        "style": "informal",
    }
    return EvalCase.model_validate({**defaults, **overrides})


def answer(
    text: str, *, chunks: list[tuple[str, int]], citations: list[Citation] | None = None
) -> AnswerResult:
    retrieved = [
        RetrievedChunk(chunk_id=i, document_id=d, page=p, content=FEE_TEXT, distance=0.1 * i)
        for i, (d, p) in enumerate(chunks, start=1)
    ]
    cites = citations or []
    return AnswerResult(
        question="q",
        answer=text,
        citations=cites,
        retrieved=retrieved,
        checks=[
            CitationCheck(citation=c, quote_found=True, document_retrieved=True) for c in cites
        ],
        usage=TokenUsage(input_tokens=1000, output_tokens=100),
    )


# --- Dataset -----------------------------------------------------------------


def test_the_real_dataset_loads() -> None:
    cases = load_cases("all")
    assert len(cases) >= 50, "docs/EVALS.md requires at least 50"


def test_smoke_is_a_subset_of_the_full_suite() -> None:
    """One file, so the two can never disagree about what a case says."""
    full = {c.id for c in load_cases("all")}
    assert {c.id for c in load_cases("smoke")} <= full


def test_smoke_spans_both_styles() -> None:
    """It gates every pull request; a style regression has to be able to show."""
    styles = {c.style for c in load_cases("smoke")}
    assert styles == {"informal", "formal"}


def test_smoke_includes_a_refusal_and_a_partial() -> None:
    cases = load_cases("smoke")
    assert any(c.expect_refusal for c in cases)
    assert any("partial" in c.tags for c in cases)


def test_smoke_ids_are_unique() -> None:
    assert len(set(SMOKE_IDS)) == len(SMOKE_IDS)


def test_an_unknown_suite_is_rejected() -> None:
    with pytest.raises(DatasetError, match="Unknown suite"):
        load_cases("nonsense")


def test_a_malformed_line_names_its_line_number(tmp_path: Path) -> None:
    (tmp_path / "rag_qa.jsonl").write_text('{"id": "ok"}\n', encoding="utf-8")
    with pytest.raises(DatasetError, match="line 1"):
        load_cases("all", datasets_dir=tmp_path)


def test_a_smoke_list_naming_a_missing_case_fails_loudly(tmp_path: Path) -> None:
    """Otherwise the pull-request gate silently shrinks."""
    row = case().model_dump()
    (tmp_path / "rag_qa.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="unknown cases"):
        load_cases("smoke", datasets_dir=tmp_path)


# --- Scoring -----------------------------------------------------------------


def test_a_first_place_hit_scores_one() -> None:
    result = score_case(
        case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 100.0, "gpt-4o-mini"
    )

    assert result.reciprocal_rank == 1.0
    assert result.hit_at_1 and result.hit_at_k


def test_must_include_is_checked_against_the_answer_not_the_source() -> None:
    """The figure has to reach the adviser, not merely exist in the corpus."""
    hit = score_case(case(), answer("£1,500 a year.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    miss = score_case(case(), answer("It varies.", chunks=[("fee-schedule", 1)]), 1.0, "m")

    assert hit.must_include_found
    assert not miss.must_include_found


def test_must_include_ignores_whitespace_and_case() -> None:
    scored = score_case(
        case(must_include=["THERE IS NO maximum"]),
        answer("There is no\nmaximum.", chunks=[("fee-schedule", 1)]),
        1.0,
        "m",
    )
    assert scored.must_include_found


def test_a_correct_refusal_scores_as_correct() -> None:
    refusal = case(id="refusal-x", expect_refusal=True, expected_sources=[], must_include=[])
    from app.rag.generate import NO_ANSWER

    scored = score_case(refusal, answer(NO_ANSWER, chunks=[]), 1.0, "m")
    assert scored.refusal_correct


def test_answering_a_question_that_should_be_refused_is_wrong() -> None:
    refusal = case(id="refusal-x", expect_refusal=True, expected_sources=[], must_include=[])
    scored = score_case(refusal, answer("The base rate is 4%.", chunks=[]), 1.0, "m")
    assert not scored.refusal_correct


def test_cost_comes_from_the_tokens_actually_used() -> None:
    scored = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "gpt-4o-mini")
    assert scored.cost_usd == pytest.approx(1000 * 0.15 / 1e6 + 100 * 0.60 / 1e6)


# --- Aggregation -------------------------------------------------------------


def test_refusals_are_excluded_from_retrieval_metrics() -> None:
    """They have no expected source, so scoring them would drag MRR down for
    behaving correctly."""
    good = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    refusal = case(id="refusal-x", expect_refusal=True, expected_sources=[], must_include=[])
    from app.rag.generate import NO_ANSWER

    refused = score_case(refusal, answer(NO_ANSWER, chunks=[]), 1.0, "m")

    metrics = aggregate([good, refused])
    assert metrics["retrieval.mrr"] == 1.0


def test_answer_metrics_are_omitted_when_nothing_was_generated() -> None:
    """A retrieval-only run must not report the dataclass defaults as a result."""
    scored = score_case(case(), answer("", chunks=[("fee-schedule", 1)]), 1.0, "m")
    scored.answer = ""

    metrics = aggregate([scored])
    assert "citations.validity" not in metrics
    assert "retrieval.mrr" in metrics


def test_an_errored_case_is_counted_but_does_not_poison_the_averages() -> None:
    from app.evals.runner import CaseResult

    good = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    broken = CaseResult(case_id="zt-broken", tags=[], style="informal", question="q", error="boom")

    metrics = aggregate([good, broken])
    assert metrics["ops.errors"] == 1.0
    assert metrics["retrieval.mrr"] == 1.0


# --- Report ------------------------------------------------------------------


def test_the_report_records_what_produced_it() -> None:
    """A number nobody can trace to a commit and a model is not evidence."""
    scored = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    report = build_report("smoke", aggregate([scored]), [scored])

    assert report["git_sha"]
    assert report["config"]["llm_model"]
    assert report["config"]["embedding_model"]
    assert report["case_count"] == 1


def test_contexts_are_not_serialised() -> None:
    """They are the corpus; copying them into every report tells you nothing the
    source identifiers do not."""
    scored = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    report = build_report("smoke", aggregate([scored]), [scored])

    assert "contexts" not in report["cases"][0]
    assert report["cases"][0]["retrieved"] == ["fee-schedule#p1"]


def test_the_table_marks_a_regression_against_a_baseline() -> None:
    scored = score_case(case(), answer("£1,500.", chunks=[("other", 9)]), 1.0, "m")
    report = build_report("smoke", aggregate([scored]), [scored])
    baseline = {
        "git_sha": "abc1234",
        "created_at": "2026-01-01T00:00:00+00:00",
        "metrics": {"retrieval.mrr": 1.0},
    }

    rendered = render_table(report, baseline)
    assert "REGRESSED" in rendered
    assert "abc1234" in rendered
