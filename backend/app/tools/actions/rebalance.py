"""The slice's one HIGH-risk tool.

`propose_rebalance` is the whole reason the approval gate exists in Phase 4a:
something that touches a client's positions and therefore must not run because
a model decided it should.

What it actually does when approved is write a back-office task, not move
holdings. That mirrors how the firm works — an adviser proposes, back office
executes — and it keeps `holdings` derived from `transactions`, which
ARCHITECTURE makes the source of truth.
"""

from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.firm import Account, Task
from app.tools.registry import ToolError, register


class AccountNotFoundError(ToolError):
    """The account does not exist, so there is nothing to rebalance.

    Checked rather than left to a foreign key: a model asking to rebalance an
    invented account should get a clear refusal, not an IntegrityError.
    """


def _summarise(args: dict[str, Any]) -> str:
    """One sentence for the human deciding. See ToolSpec.summarise."""
    account = args.get("account_id", "?")
    reason = args.get("reason") or "no reason given"
    return f"Raise a rebalance task for account {account} ({reason})."


@register(
    name="propose_rebalance",
    tier="HIGH",
    description=(
        "Raise a back-office task to rebalance an account to its mandate's "
        "allocation bands. Requires human approval."
    ),
    summarise=_summarise,
)
async def propose_rebalance(
    session: AsyncSession, *, account_id: str, reason: str | None = None
) -> dict[str, Any]:
    """Create the rebalance task. Only reachable through an approved approval."""
    account = (
        await session.execute(
            select(Account.id, Account.client_id, Account.mandate).where(Account.id == account_id)
        )
    ).one_or_none()
    if account is None:
        raise AccountNotFoundError(f"no account {account_id!r}")

    task_id = (
        await session.execute(
            insert(Task)
            .values(
                client_id=account.client_id,
                title=f"Rebalance {account_id}",
                description=reason or f"Rebalance to the {account.mandate} mandate's bands.",
                created_by="action_agent",
            )
            .returning(Task.id)
        )
    ).scalar_one()

    return {"task_id": task_id, "account_id": account_id, "client_id": account.client_id}
