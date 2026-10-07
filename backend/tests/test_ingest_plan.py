"""The incremental decision, tested in isolation.

`plan_ingest` is pure: given what is on disk and what the database already
holds, decide what to embed, what to remove and what to leave alone. Every
interesting edge case lives here rather than behind a database and a provider.
"""

from app.rag.documents import SourceDocument
from app.rag.ingest import plan_ingest


def source(document_id: str, sha: str) -> SourceDocument:
    return SourceDocument(
        id=document_id,
        source_path=f"data/docs/{document_id}.md",
        title=document_id,
        sha256=sha,
        pages=("content",),
    )


def test_an_empty_database_indexes_everything() -> None:
    plan = plan_ingest([source("a", "1"), source("b", "2")], {})

    assert [d.id for d in plan.to_index] == ["a", "b"]
    assert plan.to_delete == ()
    assert plan.unchanged == ()


def test_matching_hashes_are_skipped() -> None:
    """The whole point: a second run with no edits does no work."""
    plan = plan_ingest([source("a", "1"), source("b", "2")], {"a": "1", "b": "2"})

    assert plan.to_index == ()
    assert plan.unchanged == ("a", "b")
    assert not plan.has_work


def test_a_changed_hash_is_reindexed() -> None:
    plan = plan_ingest([source("a", "NEW"), source("b", "2")], {"a": "1", "b": "2"})

    assert [d.id for d in plan.to_index] == ["a"]
    assert plan.unchanged == ("b",)


def test_a_new_file_is_indexed_without_touching_the_others() -> None:
    plan = plan_ingest([source("a", "1"), source("new", "9")], {"a": "1"})

    assert [d.id for d in plan.to_index] == ["new"]
    assert plan.unchanged == ("a",)


def test_a_file_removed_from_disk_is_deleted() -> None:
    """Otherwise retrieval keeps citing a document that no longer exists."""
    plan = plan_ingest([source("a", "1")], {"a": "1", "gone": "7"})

    assert plan.to_delete == ("gone",)
    assert plan.to_index == ()
    assert plan.has_work


def test_additions_changes_and_deletions_in_one_pass() -> None:
    plan = plan_ingest(
        [source("same", "1"), source("edited", "NEW"), source("added", "3")],
        {"same": "1", "edited": "OLD", "removed": "4"},
    )

    assert [d.id for d in plan.to_index] == ["edited", "added"]
    assert plan.to_delete == ("removed",)
    assert plan.unchanged == ("same",)


def test_an_empty_directory_deletes_the_whole_index() -> None:
    """Destructive, and correct: no files on disk means nothing should be cited."""
    plan = plan_ingest([], {"a": "1", "b": "2"})

    assert plan.to_delete == ("a", "b")
    assert plan.has_work


def test_nothing_on_either_side_is_no_work() -> None:
    assert not plan_ingest([], {}).has_work


def test_deletions_are_ordered_for_a_stable_report() -> None:
    plan = plan_ingest([], {"z": "1", "a": "2", "m": "3"})
    assert plan.to_delete == ("a", "m", "z")


def test_force_reindexes_everything() -> None:
    plan = plan_ingest([source("a", "1"), source("b", "2")], {"a": "1", "b": "2"}, force=True)

    assert [d.id for d in plan.to_index] == ["a", "b"]
    assert plan.unchanged == ()


def test_force_still_deletes_documents_removed_from_disk() -> None:
    """Treating a forced run as an empty database would orphan those rows.

    They would stay in the index, keep being retrieved, and keep being cited,
    with no file behind them.
    """
    plan = plan_ingest([source("a", "1")], {"a": "1", "gone": "7"}, force=True)

    assert plan.to_delete == ("gone",)
    assert [d.id for d in plan.to_index] == ["a"]
