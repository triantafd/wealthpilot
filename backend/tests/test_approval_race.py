"""Two reviewers deciding one approval at the same time.

The first concurrency test in this project, and it needs real concurrency: the
`db_session` fixture hands every test one connection inside a transaction that
is rolled back, so two "concurrent" callers would share a connection and
serialise, and the race could not happen at all.

**It is deterministic, not hopeful.** `asyncio.gather` on two decisions would
usually interleave the wrong way and pass whether or not the bug is present,
and a race test that passes by luck is the same failure as a metric that
measures by luck. So the interleaving is forced through the row lock:

1. The winner's transaction runs `decide()` and does **not** commit, holding
   the lock on the approval row.
2. The loser's `decide()` starts and blocks on that lock. The test waits until
   Postgres itself reports the wait in `pg_stat_activity`, so the loser is
   provably inside the UPDATE before anything else happens.
3. The winner commits. The loser wakes, re-evaluates `where status =
   'pending'` at READ COMMITTED, matches no row, and must raise.

Verified to fail on the code before the fix, 10 runs out of 10.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.approvals import AlreadyDecidedError, decide
from app.db.models.ops import Approval, AuditLogEntry
from tests.conftest import _test_database_name, _url

TOOL = "propose_rebalance"
ARGS = {"account_id": "AC-RACE01"}


@pytest.fixture
async def committed(test_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, against the test database.

    Each is an independent connection, which is the whole point: the race
    needs two transactions, not two cursors on one.
    """
    engine = create_async_engine(_url(_test_database_name(), driver="asyncpg"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.fixture
async def observer(test_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A connection pool of its own, for watching the lock.

    Separate from `committed` on purpose. Borrowing the third connection from
    the same pool as the two racing transactions made this file hang
    intermittently — it passed alone and wedged the suite on other runs, which
    is disqualifying for a test whose whole job is to be deterministic.
    """
    engine = create_async_engine(_url(_test_database_name(), driver="asyncpg"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _an_approval(maker: async_sessionmaker[AsyncSession]) -> str:
    """A pending approval, committed so both transactions can see it."""
    approval_id = str(uuid.uuid4())
    async with maker() as session:
        await session.execute(
            insert(Approval).values(
                id=approval_id,
                thread_id=f"t-{uuid.uuid4()}",
                tool_name=TOOL,
                tier="HIGH",
                args=ARGS,
                status="pending",
                requested_by="adviser-7",
            )
        )
        await session.commit()
    return approval_id


async def _cleanup(maker: async_sessionmaker[AsyncSession], approval_id: str) -> None:
    async with maker() as session:
        await session.execute(
            AuditLogEntry.__table__.delete().where(AuditLogEntry.approval_id == approval_id)
        )
        await session.execute(
            text("DELETE FROM audit_log WHERE result->>'approval_id' = :a"), {"a": approval_id}
        )
        await session.execute(Approval.__table__.delete().where(Approval.id == approval_id))
        await session.commit()


async def _wait_until_blocked(maker: async_sessionmaker[AsyncSession]) -> None:
    """Block until Postgres reports a backend waiting on the approvals row.

    Polling the server rather than sleeping is what makes this deterministic:
    when it returns, the loser is inside its UPDATE and waiting, so the
    winner's commit is guaranteed to be the later event. A sleep would be a
    guess, and the race would resolve the wrong way on a slow run.

    `pg_blocking_pids()` is the question actually being asked — "is some
    backend waiting for another to release a row?" — rather than matching on
    query text, which also matched this query's own ILIKE pattern.
    """
    async with maker() as watcher:
        for _ in range(500):
            blocked = (
                await watcher.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE cardinality(pg_blocking_pids(pid)) > 0"
                    )
                )
            ).scalar_one()
            if blocked:
                return
            await asyncio.sleep(0.01)
    raise AssertionError("the second decision never blocked on the row lock")


async def test_the_loser_of_a_race_is_told_it_was_already_decided(
    committed: async_sessionmaker[AsyncSession],
    observer: async_sessionmaker[AsyncSession],
) -> None:
    """The bug: the loser used to fall through and return the winner's row, so
    a reviewer who pressed reject got HTTP 200 and status "approved"."""
    approval_id = await _an_approval(committed)
    try:
        async with asyncio.timeout(30), committed() as winner, committed() as loser:
            await decide(winner, approval_id, "approve", decided_by="reviewer-a")

            async def _reject() -> Any:
                return await decide(loser, approval_id, "reject", decided_by="reviewer-b")

            losing = asyncio.create_task(_reject())
            try:
                await _wait_until_blocked(observer)
                await winner.commit()

                with pytest.raises(AlreadyDecidedError) as raised:
                    await losing
            finally:
                losing.cancel()
            await loser.rollback()

            assert "approved" in str(raised.value)
            assert "reviewer-a" in str(raised.value), "the message names who won"
    finally:
        await _cleanup(committed, approval_id)


async def test_a_race_leaves_exactly_one_decision_in_the_audit_log(
    committed: async_sessionmaker[AsyncSession],
    observer: async_sessionmaker[AsyncSession],
) -> None:
    """The permanent half of the bug. A status code is transient; an audit row
    claiming a rejection that never applied is forever, and two contradictory
    rows for one approval leave nobody able to say which took effect."""
    approval_id = await _an_approval(committed)
    try:
        async with asyncio.timeout(30), committed() as winner, committed() as loser:
            await decide(winner, approval_id, "approve", decided_by="reviewer-a")
            losing = asyncio.create_task(
                decide(loser, approval_id, "reject", decided_by="reviewer-b")
            )
            try:
                await _wait_until_blocked(observer)
                await winner.commit()
                with pytest.raises(AlreadyDecidedError):
                    await losing
            finally:
                losing.cancel()
            await loser.rollback()

        async with committed() as verifier:
            decisions = (
                (
                    await verifier.execute(
                        text(
                            "SELECT result->>'decision' FROM audit_log "
                            "WHERE result->>'approval_id' = :a"
                        ),
                        {"a": approval_id},
                    )
                )
                .scalars()
                .all()
            )
            status = (
                await verifier.execute(
                    select(Approval.status, Approval.decided_by).where(Approval.id == approval_id)
                )
            ).one()

        assert decisions == ["approve"], f"expected one decision row, got {decisions}"
        assert status.status == "approved"
        assert status.decided_by == "reviewer-a"
    finally:
        await _cleanup(committed, approval_id)
