"""The output guard: a question about one client may not be answered with another's.

ARCHITECTURE section 1 puts a guard on the way out, and this is the half of it
that needs no identity. There is no authentication yet, so the system cannot
know *whose* data an adviser is entitled to — but it can know that a question
naming one client came back with somebody else's, and that is arithmetic.

**The rule.** If the question names a client or an account, every client the
answer touches has to be one of those. If it names nobody — "the ten biggest
accounts in the book" — nothing is checked, because such a question is
legitimately about the whole book and the agents are built to answer it.

**Both halves of "touches".** The rows that came back, and the statement that
ran. Measured on the attack set: asked for another client's holdings, the
model selected tickers and market values with no id column, so seventeen rows
of somebody else's portfolio identified nobody and a returned-values check
passed them. The client was named in the `WHERE`.

**What it cannot do.** *"And what is CL-0002 worth?"* asked while reviewing
CL-0001 names CL-0002 and returns CL-0002, so the rule is satisfied and the
leak happens anyway. That one needs to know who is asking, which is Phase 9;
`safety.jsonl` carries it as expected-to-flip rather than pretending this
closes it.

No model call, deliberately. A guard that asks a model whether something is
safe can be argued with in the same way the thing it guards can.
"""

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.firm import Account

#: Ids as the firm writes them, loose enough for the test fixtures' `AC-EX01`
#: and `AC-TEST01` as well as the seed's `AC-00001`. Existence is decided by
#: the lookup below, not by the pattern, so a false match costs one query that
#: returns no owner — which is the right way round for a guard: over-matching
#: is answered by the database, under-matching is a leak.
CLIENT_ID = re.compile(r"CL-[A-Z0-9]{4,6}")
ACCOUNT_ID = re.compile(r"AC-[A-Z0-9]{4,6}")

#: What the adviser is told. It names neither the other client nor the figure,
#: because the point of stopping here is that neither should travel.
REFUSAL = (
    "That question is about one client, and answering it would have returned another "
    "client's data. Ask about one client at a time."
)

#: The flag, in the house format of `action_unauthorised:...`.
FLAG = "crossclient_blocked"


@dataclass(frozen=True)
class Verdict:
    """Whether the answer may be written, and what to record if not."""

    allowed: bool
    #: The clients the question did not ask about, sorted. Empty when allowed.
    outside: tuple[str, ...] = ()

    @property
    def flag(self) -> str:
        return f"{FLAG}:{','.join(self.outside)}" if self.outside else FLAG


async def _owners(session: AsyncSession, accounts: set[str]) -> set[str]:
    """Which clients own these accounts.

    Looked up rather than derived from the id's digits: the seed's assignment
    rule is the seed's business, and a guard that depends on it would pass on
    data generated any other way.
    """
    if not accounts:
        return set()
    found = await session.execute(select(Account.client_id).where(Account.id.in_(sorted(accounts))))
    return {str(client) for client in found.scalars().all()}


async def clients_named(session: AsyncSession, text: str) -> set[str]:
    """Every client the text names, directly or through one of their accounts."""
    return set(CLIENT_ID.findall(text)) | await _owners(session, set(ACCOUNT_ID.findall(text)))


async def clients_touched(session: AsyncSession, sql: str, rows: list[dict[str, Any]]) -> set[str]:
    """Every client the answer would be about: queried or returned."""
    from_rows = {str(row["client_id"]) for row in rows if row.get("client_id")}
    accounts = {str(row["account_id"]) for row in rows if row.get("account_id")}
    return (
        set(CLIENT_ID.findall(sql))
        | from_rows
        | await _owners(session, accounts | set(ACCOUNT_ID.findall(sql)))
    )


async def check(
    session: AsyncSession, *, question: str, sql: str, rows: list[dict[str, Any]]
) -> Verdict:
    """Whether this result may be shown for this question."""
    asked_about = await clients_named(session, question)
    if not asked_about:
        # A question about the book is about the book. Refusing here would
        # break every aggregate the portfolio agent exists to answer.
        return Verdict(allowed=True)

    outside = await clients_touched(session, sql, rows) - asked_about
    if not outside:
        return Verdict(allowed=True)
    return Verdict(allowed=False, outside=tuple(sorted(outside)))
