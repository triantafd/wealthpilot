"""The human-in-the-loop gate: two nodes, and the split is the whole lesson.

ARCHITECTURE section 3: `interrupt()` for HIGH-risk actions, resuming with
`Command(resume=decision)`.

`request_approval` records the approval row and returns its id. `approval_gate`
interrupts, and on resume executes through the registry.

**Why two nodes rather than one.** `interrupt()` raises, so a node's return
value is never applied on the pass that pauses. A single node that created the
approval and then interrupted could not write the id to state — and on resume,
LangGraph re-runs the node from the top, so it created a *second* approval, in
`pending`, and then refused its own action for not being approved. Splitting
them means the state write lands between the two, and the gate re-runs with the
id already there.

The gate does not authorise anything. It passes the approval id to
`registry.execute`, which re-checks status, tool and arguments and spends the
approval. Resuming with "approve" when no human approved is therefore refused
by the registry, not trusted by the gate.
"""

from typing import Any

from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events import ProposedAction
from app.approvals import create_approval
from app.tools.registry import ApprovalRequiredError, execute


def _action(state: dict[str, Any]) -> ProposedAction | None:
    """The pending action, however it arrived.

    State holds it as a plain dict so the checkpointer stores JSON rather than
    a pickled Pydantic class — LangGraph warns that deserialising unregistered
    types will stop working, and a checkpoint that only one build of the app
    can read is not a checkpoint.
    """
    data = state.get("pending_action")
    if data is None:
        return None
    return data if isinstance(data, ProposedAction) else ProposedAction.model_validate(data)


async def request_approval(state: dict[str, Any], session: AsyncSession) -> dict[str, Any]:
    """Record the approval, so the inbox can show it before the graph pauses.

    Idempotent through `approval_id`: if the state already carries one, this is
    a re-run and no second row is created.
    """
    action = _action(state)
    if action is None or state.get("approval_id") is not None:
        return {}

    approval_id = await create_approval(
        session, action, thread_id=state["thread_id"], requested_by=state.get("user_id")
    )
    return {"approval_id": approval_id}


async def approval_gate(state: dict[str, Any], session: AsyncSession) -> dict[str, Any]:
    """Pause for a human, then run the action or record the refusal."""
    action = _action(state)
    if action is None:
        # A gate on a path with no action is a no-op, so the graph can route
        # through it unconditionally.
        return {}

    approval_id = state.get("approval_id")
    if approval_id is None:
        # request_approval did not run, so nothing authorises this. Refuse
        # rather than create an approval here: creating one before an interrupt
        # is the bug this split exists to fix.
        return {
            "pending_action": None,
            "guardrail_flags": [f"action_unauthorised:no approval recorded for {action.tool}"],
        }

    # Everything below runs again on resume: `interrupt` raises on the first
    # pass and returns the decision on the second.
    decision = interrupt({"approval_id": approval_id, "action": action.model_dump(by_alias=True)})

    if str(decision) != "approve":
        return {
            "pending_action": None,
            "guardrail_flags": [f"action_rejected:{action.tool}"],
        }

    try:
        result = await execute(
            session,
            action.tool,
            action.args,
            thread_id=state["thread_id"],
            user_id=state.get("user_id"),
            approval_id=approval_id,
        )
    except ApprovalRequiredError as exc:
        # The registry refused despite a resume saying otherwise — the decision
        # was never recorded, or it authorised something else. A flag rather
        # than an exception, so the thread finishes with an explanation.
        return {
            "pending_action": None,
            "guardrail_flags": [f"action_unauthorised:{exc}"],
        }

    return {"pending_action": None, "action_result": result}
