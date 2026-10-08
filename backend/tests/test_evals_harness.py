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
        "suite": "smoke",
        "git_sha": "abc1234",
        "created_at": "2026-01-01T00:00:00+00:00",
        "metrics": {"retrieval.mrr": 1.0},
    }

    rendered = render_table(report, baseline)
    assert "REGRESSED" in rendered
    assert "abc1234" in rendered


# --- Gate policy -------------------------------------------------------------


def test_only_deterministic_metrics_are_gateable() -> None:
    """A ragas metric moving may mean the judge had a different day. Worse,
    relevancy penalises the caveats the prompt explicitly requires, so a drop
    there can mean the answer got more correct."""
    from app.evals.report import GATEABLE

    assert "answer.faithfulness" not in GATEABLE
    assert "answer.relevancy" not in GATEABLE
    assert "retrieval.mrr" in GATEABLE
    assert "citations.validity" in GATEABLE


def test_the_report_records_which_metrics_may_gate() -> None:
    """CI reads this rather than hardcoding a list that silently drifts."""
    scored = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    report = build_report("smoke", aggregate([scored]), [scored])

    assert "retrieval.mrr" in report["gateable"]
    assert "answer.faithfulness" not in report["gateable"]


def test_a_report_without_repeats_carries_no_spread() -> None:
    scored = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    assert build_report("smoke", aggregate([scored]), [scored])["spread"] is None


def test_repeated_runs_are_summarised_into_the_report() -> None:
    from app.evals.report import summarise_runs

    scored = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    spreads = summarise_runs([{"retrieval.mrr": 0.7}, {"retrieval.mrr": 0.9}])
    report = build_report("all", {"retrieval.mrr": 0.8}, [scored], spreads=spreads)

    assert report["spread"]["retrieval.mrr"]["runs"] == 2
    assert report["spread"]["retrieval.mrr"]["minimum"] == pytest.approx(0.7)


def test_the_report_records_whether_the_tree_was_dirty() -> None:
    """git_sha alone is misleading: a baseline is measured before the commit
    that stores it, so its sha names the parent. If the tree was also dirty,
    checking out that sha does not reproduce the numbers."""
    scored = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    report = build_report("smoke", aggregate([scored]), [scored])

    assert "git_dirty" in report
    assert isinstance(report["git_dirty"], bool)


def test_a_baseline_from_a_different_suite_is_not_compared() -> None:
    """27 smoke cases against a 75-case baseline is different questions at
    different difficulty; every difference would read as a regression."""
    scored = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    report = build_report("smoke", aggregate([scored]), [scored])
    baseline = {
        "suite": "all",
        "git_sha": "abc1234",
        "created_at": "2026-01-01T00:00:00+00:00",
        "metrics": {"retrieval.mrr": 0.2},
    }

    rendered = render_table(report, baseline)

    assert "not compared" in rendered
    assert "REGRESSED" not in rendered
    assert "vs baseline" not in rendered


def test_prose_references_are_scored_and_gateable() -> None:
    """Enforced in code because the prompt rule is followed about 90% of the
    time, and the remaining 10% is an unverifiable claim in an answer."""
    from app.evals.report import GATEABLE

    assert "answer.no_prose_refs" in GATEABLE

    clean = score_case(case(), answer("£1,500 a year.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    dirty = score_case(
        case(),
        answer("£1,500 a year. See *Schedule of Fees*, page 2.", chunks=[("fee-schedule", 1)]),
        1.0,
        "m",
    )

    assert clean.no_prose_reference
    assert not dirty.no_prose_reference
    assert aggregate([clean, dirty])["answer.no_prose_refs"] == 0.5


def test_the_case_diff_names_what_flipped_in_both_directions() -> None:
    """An aggregate that moved says something changed; this says which case."""
    from app.evals.report import render_case_diff

    broke = score_case(
        case(must_include=["£9.95"]), answer("No charge.", chunks=[("fee-schedule", 1)]), 1.0, "m"
    )
    broke.case_id = "zt-broke"
    fixed = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    fixed.case_id = "zt-fixed"

    baseline = {
        "cases": [
            {"case_id": "zt-broke", "must_include_found": True},
            {"case_id": "zt-fixed", "must_include_found": False},
        ]
    }

    rendered = render_case_diff([broke, fixed], baseline)

    assert "zt-broke [must_include]" in rendered
    assert "zt-fixed [must_include]" in rendered
    assert "now failing (1)" in rendered
    assert "now passing (1)" in rendered


def test_the_case_diff_says_so_when_nothing_moved() -> None:
    from app.evals.report import render_case_diff

    scored = score_case(case(), answer("£1,500.", chunks=[("fee-schedule", 1)]), 1.0, "m")
    baseline = {"cases": [{"case_id": scored.case_id, "must_include_found": True}]}

    assert "no case changed" in render_case_diff([scored], baseline)


def test_a_count_metric_is_not_printed_as_a_percentage() -> None:
    """answer.prose_refs_written shares a prefix with the fraction metrics;
    without an exception it printed 16.3 references as "1633.3%"."""
    from app.evals.report import _format

    assert "%" not in _format("answer.prose_refs_written", 16.3)
    assert "16.3" in _format("answer.prose_refs_written", 16.3)
    assert "%" in _format("answer.must_include", 0.938)


def test_a_report_records_what_produced_it() -> None:
    """The Phase 3 diagnostic had to identify six saved runs by their MRR
    values, because the retrieval settings were absent from the config block.
    Two variants that happen to score the same would have been
    indistinguishable."""
    from app.evals.report import build_report

    report = build_report("all", {}, [])

    assert set(report["config"]) >= {
        "llm_model",
        "embedding_model",
        "retrieval_top_k",
        "retrieval_mode",
        "retrieval_rerank",
        "embed_strip_boilerplate",
    }


def test_rrf_parameters_are_recorded_only_for_the_hybrid_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording an RRF value for a vector-only run would suggest it affected
    the result."""
    from app.config import get_settings
    from app.evals.report import build_report

    assert build_report("all", {}, [])["config"]["retrieval_rrf_k"] is None

    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    get_settings.cache_clear()
    try:
        assert build_report("all", {}, [])["config"]["retrieval_rrf_k"] == 60
    finally:
        get_settings.cache_clear()
