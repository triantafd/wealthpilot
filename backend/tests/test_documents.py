"""Document corpus tests.

These documents are the ground truth for every RAG eval case, so they are
treated as data under test rather than as prose nobody checks.

The most valuable assertions here are the coherence ones: the allocation
percentages, score bands and instrument details appear in both the documents
and `app/scripts/seed.py`. Nothing stops those drifting apart except these
tests, and if they drift, eval cases quietly start asserting the wrong answers.

No database needed: the documents are files and `generate()` is pure.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.db.models.firm import ACCOUNT_TYPES, CAPACITY_FOR_LOSS, MANDATES, RISK_TOLERANCES
from app.scripts.seed import _ALLOCATIONS, generate

DOCS_DIR = Path(__file__).resolve().parents[1] / "data" / "docs"
PAGE_BREAK = "<!-- page -->"

# Score band rows look like "| 1 - 25 | `conservative` | ..." with an en dash.
# Written as an escape rather than the literal character, which ruff flags as
# ambiguous in source; the class accepts a plain hyphen too.
BAND_RE = re.compile("\\|\\s*(\\d+)\\s*[-\\u2013]\\s*(\\d+)\\s*\\|\\s*`(\\w+)`")

REQUIRED_FRONT_MATTER = {"id", "title", "version", "effective", "owner", "classification"}

_, ROWS = generate()


def _parse(path: Path) -> tuple[dict[str, Any], str]:
    """Split a document into its front matter and body."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} has no front matter"

    _, raw, body = text.split("---\n", 2)
    meta = yaml.safe_load(raw)
    assert isinstance(meta, dict), f"{path.name} front matter is not a mapping"
    return meta, body


DOCUMENTS = sorted(DOCS_DIR.glob("*.md"))
PARSED = {path.name: _parse(path) for path in DOCUMENTS}


def _body(doc_id: str) -> str:
    return PARSED[f"{doc_id}.md"][1]


def _all_text() -> str:
    return "\n".join(body for _, body in PARSED.values())


# --- Corpus shape ------------------------------------------------------------


def test_corpus_size_is_within_the_roadmap_range() -> None:
    """ROADMAP Phase 0 task 6 asks for 8 to 12 documents."""
    assert 8 <= len(DOCUMENTS) <= 12, f"{len(DOCUMENTS)} documents"


def test_readme_is_not_in_the_ingested_directory() -> None:
    """Conventions live in data/README.md; anything in data/docs/ gets embedded."""
    assert not (DOCS_DIR / "README.md").exists()


@pytest.mark.parametrize("name", [p.name for p in DOCUMENTS])
def test_front_matter_is_complete(name: str) -> None:
    meta, _ = PARSED[name]
    missing = REQUIRED_FRONT_MATTER - set(meta)
    assert not missing, f"{name} missing {sorted(missing)}"


@pytest.mark.parametrize("name", [p.name for p in DOCUMENTS])
def test_id_matches_filename(name: str) -> None:
    """Citations and eval cases reference the id; a mismatch makes them unresolvable."""
    meta, _ = PARSED[name]
    assert meta["id"] == name.removesuffix(".md")


def test_ids_are_unique() -> None:
    ids = [meta["id"] for meta, _ in PARSED.values()]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("name", [p.name for p in DOCUMENTS])
def test_version_is_a_string_not_a_float(name: str) -> None:
    """Unquoted `version: 2026.1` parses as a float and loses trailing zeros."""
    meta, _ = PARSED[name]
    assert isinstance(meta["version"], str), f"{name}: quote the version"


@pytest.mark.parametrize("name", [p.name for p in DOCUMENTS])
def test_every_document_is_marked_synthetic(name: str) -> None:
    """Working rule 7, made machine-checkable."""
    meta, _ = PARSED[name]
    assert meta.get("synthetic") is True


@pytest.mark.parametrize("name", [p.name for p in DOCUMENTS])
def test_every_document_is_paginated(name: str) -> None:
    """`chunks.page` is an integer and citations are document plus page."""
    _, body = PARSED[name]
    pages = body.count(PAGE_BREAK) + 1
    assert 2 <= pages <= 8, f"{name} would be {pages} page(s)"


@pytest.mark.parametrize("name", [p.name for p in DOCUMENTS])
def test_page_breaks_sit_on_their_own_line(name: str) -> None:
    """The ingest script splits on the marker; an inline one would break a sentence."""
    _, body = PARSED[name]
    for line in body.splitlines():
        if PAGE_BREAK in line:
            assert line.strip() == PAGE_BREAK, f"{name}: {line!r}"


@pytest.mark.parametrize("name", [p.name for p in DOCUMENTS])
def test_no_page_is_empty_or_trivial(name: str) -> None:
    """A chunk from a near-empty page is retrievable noise."""
    _, body = PARSED[name]
    for index, page in enumerate(body.split(PAGE_BREAK), start=1):
        assert len(page.strip()) > 400, f"{name} page {index} is too thin"


# --- Synthetic-data guarantee ------------------------------------------------


def test_only_reserved_email_domains_appear() -> None:
    """RFC 2606 reserves .test and .example, so no address can reach a real inbox."""
    addresses = re.findall(r"[\w.+-]+@[\w.-]+\.\w+", _all_text())
    for address in addresses:
        assert address.endswith((".test", ".example")), address


def test_the_fictional_firm_and_regulator_are_used() -> None:
    """Naming a real regulator in synthetic policy documents invites confusion."""
    text = _all_text()
    assert "WealthPilot Advisers Ltd" in text
    assert "Office of Investment Conduct" in text
    # Real UK bodies that must not appear.
    for real in ("Financial Conduct Authority", "FCA", "HMRC", "Financial Ombudsman Service"):
        assert real not in text, f"real body named: {real}"


# --- Coherence with the seeded data -----------------------------------------
# If these fail, the documents and the database disagree, and eval cases built
# on either one are asserting the wrong answer.


@pytest.mark.parametrize("tolerance", RISK_TOLERANCES)
def test_policy_allocations_match_the_seed(tolerance: str) -> None:
    """The Investment Policy Statement's target table must equal _ALLOCATIONS."""
    body = _body("investment-policy")

    # The document's tables are "| Bond | 55% | 45% - 65% |" under a heading
    # naming the category, so take the section and look for each target.
    heading = f"### {tolerance.capitalize()}"
    assert heading in body, f"no section for {tolerance}"
    section = body.split(heading, 1)[1].split("###", 1)[0]

    for asset_class, weight in _ALLOCATIONS[tolerance]:
        target = f"{round(weight * 100)}%"
        row = f"| {asset_class.capitalize()} | {target} |"
        assert row in section, f"{tolerance}: expected row starting {row!r}"


def test_policy_lists_every_permitted_asset_class() -> None:
    body = _body("investment-policy")
    for asset_class in {a for a, _ in sum(_ALLOCATIONS.values(), ())}:
        assert f"`{asset_class}`" in body


def test_suitability_score_bands_cover_one_to_one_hundred() -> None:
    """Four contiguous bands, matching the four tolerance categories in the seed."""
    body = _body("suitability-framework")
    bands = BAND_RE.findall(body)

    assert len(bands) == len(RISK_TOLERANCES)
    previous_high = 0
    for low, high, category in bands:
        assert int(low) == previous_high + 1, f"gap before {category}"
        previous_high = int(high)
    assert previous_high == 100


def test_seeded_scores_fall_inside_their_documented_band() -> None:
    """The strongest coherence check: every client's score matches their category."""
    body = _body("suitability-framework")
    bands = {category: (int(low), int(high)) for low, high, category in BAND_RE.findall(body)}

    for profile in ROWS["risk_profiles"]:
        low, high = bands[profile.tolerance]
        assert low <= profile.score <= high, (
            f"{profile.client_id}: score {profile.score} is {profile.tolerance}, "
            f"but the framework puts that at {low}-{high}"
        )


@pytest.mark.parametrize("band", CAPACITY_FOR_LOSS)
def test_every_capacity_band_is_defined_and_populated(band: str) -> None:
    """A documented band with no clients is a definition nothing can exercise."""
    assert f"`{band}`" in _body("suitability-framework")
    assert any(p.capacity_for_loss == band for p in ROWS["risk_profiles"])


@pytest.mark.parametrize("mandate", MANDATES)
def test_every_mandate_is_documented(mandate: str) -> None:
    assert f"`{mandate}`" in _body("mandates-and-rebalancing")


@pytest.mark.parametrize("account_type", ACCOUNT_TYPES)
def test_every_account_type_is_documented(account_type: str) -> None:
    assert f"`{account_type}`" in _body("account-types")


def test_documented_sectors_match_the_instruments() -> None:
    """A restriction can only exclude a sector some instrument actually has."""
    seeded = {i.sector for i in ROWS["instruments"] if i.sector}
    body = _body("client-restrictions")
    for sector in seeded:
        assert sector in body, f"{sector} is held but not listed in client-restrictions"


# --- Factsheets describe instruments that exist ------------------------------

FACTSHEETS = [p.name for p in DOCUMENTS if p.name.startswith("factsheet-")]


def test_there_is_at_least_one_factsheet_per_major_asset_class() -> None:
    assert len(FACTSHEETS) >= 2


@pytest.mark.parametrize("name", FACTSHEETS)
def test_factsheet_instrument_exists_in_the_seed(name: str) -> None:
    meta, _ = PARSED[name]
    instrument_id = meta["instrument_id"]

    match = next((i for i in ROWS["instruments"] if i.id == instrument_id), None)
    assert match is not None, f"{name} describes {instrument_id}, which the seed never creates"


@pytest.mark.parametrize("name", FACTSHEETS)
def test_factsheet_details_match_the_instrument(name: str) -> None:
    """Name, ticker, sector and OCF must agree, or a citation contradicts a SQL answer."""
    meta, body = PARSED[name]
    instrument = next(i for i in ROWS["instruments"] if i.id == meta["instrument_id"])

    assert instrument.name in body, f"{name} does not state the instrument's full name"
    assert meta["ticker"] == instrument.ticker

    # OCF is written as a percentage to two decimal places, e.g. 0.0052 -> 0.52%
    ocf = f"{float(instrument.ongoing_charge) * 100:.2f}%"
    assert ocf in body, f"{name} should quote an OCF of {ocf}"

    if instrument.sector:
        assert instrument.sector in body
