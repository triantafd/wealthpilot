"""The slice's one HIGH-risk tool.

`propose_rebalance` is the whole reason the approval gate exists in Phase 4a:
something that touches a client's positions and therefore must not run because
a model decided it should.

What it actually does when approved is write a back-office task, not move
holdings. That mirrors how the firm works — an adviser proposes, back office
executes — and it keeps `holdings` derived from `transactions`, which
ARCHITECTURE makes the source of truth.
"""

from typing import Any, Final

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.firm import MANDATES, Account, Task
from app.tools.registry import ToolError, register

#: The mandate under which this action is prohibited outright.
#: `mandates-and-rebalancing.md`: *"An `execution_only` account must **never**
#: be rebalanced by the firm"*, and `complaints-and-breaches.md` lists it under
#: "Prohibited absolutely". Not a preference, and not a threshold — there is no
#: breach, review or risk change that permits it, which the document spells out
#: because it is the rule it calls most often misunderstood.
FORBIDDEN_MANDATE: Final = "execution_only"

# Pinned to the schema's own enumeration, the way `registry.py` pins its
# outcomes. Renaming the stored value would otherwise make every comparison
# below silently stop matching — the only fail-*open* failure in a design that
# fails closed everywhere else, and the one prohibition the documents call
# absolute. A rename now fails at import rather than at the next
# execution-only rebalance.
if FORBIDDEN_MANDATE not in MANDATES:
    raise RuntimeError(f"{FORBIDDEN_MANDATE!r} is not one of {MANDATES}")


class AccountNotFoundError(ToolError):
    """The account does not exist, so there is nothing to rebalance.

    Checked rather than left to a foreign key: a model asking to rebalance an
    invented account should get a clear refusal, not an IntegrityError.
    """


class MandateForbidsAction(ToolError):
    """The account's mandate prohibits this action.

    Raised here as well as refused by the agent, deliberately. This runs only
    after a human has approved, so it is the check that holds when a proposal
    reaches the inbox that should never have been made and somebody clicks
    approve — the one path where every earlier defence has already failed.
    """


def mandate_refusal(account_id: str) -> str:
    """What to tell an adviser, in one place because two callers say it.

    It names the permitted step as well as the prohibition: the document does
    too, and a refusal that leaves the adviser to guess what to do instead is
    half an answer. Written for a person, so the mandate is spelled the way
    they say it rather than the way it is stored.
    """
    return (
        f"{account_id} is execution-only, so the firm may not rebalance it — not for a "
        "band breach, a concentration limit or a changed risk category. The only "
        "permitted step is to inform the client in writing and record that it was done."
    )


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

    if account.mandate == FORBIDDEN_MANDATE:
        raise MandateForbidsAction(mandate_refusal(account_id))

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
