"""The output guard: a question about one client, answered with another's data.

Pure set arithmetic over a question, a statement and some rows, so most of
this needs no model. The one thing that does is the node: the guard has to
stop the rows *before* they reach the answer prompt, and the only way to show
that is to run the node and find no answer.
"""

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.sql  # noqa: F401  -- registers run_sql
from app.agents.nodes.portfolio import SqlAttempt, portfolio_agent
from app.guardrails import cross_client
from app.guardrails.cross_client import FLAG, REFUSAL, check, clients_named, clients_touched

from .conftest import SQL_FIXTURE_ACCOUNT, SQL_FIXTURE_CLIENT, FakeChatModel

#: Spelled out rather than built from the fixture ids, because an f-string
#: holding SQL trips ruff's S608. Kept in step by the test at the bottom.
ABOUT_THE_FIXTURE = "SELECT market_value FROM v_holdings WHERE account_id = 'AC-EX01'"
#: Another client's account: `an_execution_only_account` belongs to CL-TEST01
#: while `a_portfolio` belongs to CL-EX01, so the two fixtures together give
#: two clients that both exist to be confused.
ABOUT_ANOTHER = "SELECT market_value FROM v_holdings WHERE account_id = 'AC-TESTEO'"
BOOK_WIDE = "SELECT SUM(total_value) FROM v_account_summary"


async def test_a_question_naming_nobody_is_not_checked(db_session: AsyncSession) -> None:
    """ "The ten biggest accounts in the book" is legitimately about the book.
    Refusing here would break every aggregate the agent exists to answer."""
    verdict = await check(
        db_session,
        question="which are our ten biggest accounts?",
        sql=BOOK_WIDE,
        rows=[{"account_id": "AC-00110", "total_value": 2007263.08}],
    )

    assert verdict.allowed


@pytest.mark.usefixtures("a_portfolio")
async def test_a_question_about_one_client_may_be_answered_about_them(
    db_session: AsyncSession,
) -> None:
    verdict = await check(
        db_session,
        question=f"what is {SQL_FIXTURE_ACCOUNT} worth?",
        sql=ABOUT_THE_FIXTURE,
        rows=[{"account_id": SQL_FIXTURE_ACCOUNT, "market_value": 1000}],
    )

    assert verdict.allowed


@pytest.mark.usefixtures("a_portfolio")
async def test_another_client_in_the_rows_is_blocked(db_session: AsyncSession) -> None:
    """The obvious shape: one client named, the book returned."""
    verdict = await check(
        db_session,
        question=f"what is {SQL_FIXTURE_ACCOUNT} worth, and everyone else's too?",
        sql=BOOK_WIDE,
        rows=[{"client_id": SQL_FIXTURE_CLIENT, "sum": 1}, {"client_id": "CL-0002", "sum": 2}],
    )

    assert not verdict.allowed
    assert verdict.outside == ("CL-0002",)
    assert verdict.flag == f"{FLAG}:CL-0002"


@pytest.mark.usefixtures("a_portfolio")
async def test_another_client_named_only_in_the_statement_is_blocked(
    db_session: AsyncSession, an_execution_only_account: str
) -> None:
    """The measured hole in a returned-values check: asked for another
    client's holdings, the model selected tickers and values with no id
    column, so seventeen rows of somebody else's portfolio identified nobody.
    The client was named in the `WHERE`."""
    verdict = await check(
        db_session,
        question=f"what is {SQL_FIXTURE_ACCOUNT} worth?",
        sql=ABOUT_ANOTHER,
        rows=[{"ticker": "MER011", "market_value": 99668.83}],
    )

    assert not verdict.allowed
    assert verdict.outside == ("CL-TEST01",)


@pytest.mark.usefixtures("a_portfolio")
async def test_an_account_in_the_question_stands_for_its_client(
    db_session: AsyncSession,
) -> None:
    """An adviser names accounts, not client ids. Without the lookup the guard
    would block a question about an account answered about that account."""
    named = await clients_named(db_session, f"what is {SQL_FIXTURE_ACCOUNT} worth?")

    assert named == {SQL_FIXTURE_CLIENT}


@pytest.mark.usefixtures("a_portfolio")
async def test_rows_that_identify_nobody_touch_whoever_the_statement_named(
    db_session: AsyncSession,
) -> None:
    touched = await clients_touched(db_session, ABOUT_THE_FIXTURE, [{"market_value": 1000}])

    assert touched == {SQL_FIXTURE_CLIENT}


async def test_an_unknown_account_names_no_client(db_session: AsyncSession) -> None:
    """A loose pattern plus a lookup: an id that matches the shape and exists
    nowhere contributes nothing, rather than blocking the turn."""
    assert await clients_named(db_session, "what is AC-99999 worth?") == set()


# --- Through the node --------------------------------------------------------


@pytest.mark.usefixtures("readonly", "a_portfolio")
async def test_the_rows_never_reach_the_answer_prompt(
    db_session: AsyncSession, an_execution_only_account: str
) -> None:
    """The reason the guard sits where it does. The model is given prose to
    write and never gets the chance: `history` stays empty for the second
    call, so no client figure was put into a prompt."""
    model = FakeChatModel(
        {SqlAttempt: [SqlAttempt(sql=ABOUT_ANOTHER)]},
        prose="£99,668.83 in MER011.",
    )

    result = await portfolio_agent(
        {"messages": [HumanMessage(content=f"what is {SQL_FIXTURE_ACCOUNT} worth?")]},
        db_session,
        model,
        thread_id="t-guard",
    )

    assert str(result["messages"][0].content) == REFUSAL
    assert result["guardrail_flags"][0].startswith(FLAG)
    # One call only — the statement — and none to write an answer from rows.
    assert len(model.history) == 1


def test_the_statements_above_name_the_fixture_account() -> None:
    assert SQL_FIXTURE_ACCOUNT in ABOUT_THE_FIXTURE
    assert cross_client.ACCOUNT_ID.search(ABOUT_ANOTHER)
