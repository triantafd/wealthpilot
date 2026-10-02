---
name: add-agent-tool
description: Step-by-step procedure for adding or changing a tool that WealthPilot agents can call (search, SQL, client lookups, tasks, rebalancing, reports, anything with side effects). Use this whenever a task mentions a new tool, a new agent capability, a back-office action, or changes to the tool registry or risk tiers.
---

# Add an agent tool

Tools are the only way agents touch data or the outside world, so each one follows the same path.

## 1. Classify the risk tier

- `READ`: no side effects (lookups, search, SELECT queries).
- `LOW`: reversible, internal side effects (create a task, save a draft). Auto-runs only if its feature flag is on.
- `HIGH`: affects money, clients or anything external (rebalance, send report, email a client). Always requires approval.

If unsure, choose the higher tier.

## 2. Define the schema

In `backend/app/tools/<area>/<tool_name>.py`:
- A Pydantic input model with field descriptions (the model reads them) and strict validation.
- A Pydantic output model.
- A pure implementation function that takes the validated input and a DB session. No LLM calls inside tools.

## 3. Register it

In `backend/app/tools/registry.py` add the tool with name, description, tier, input/output models and (for LOW) its feature flag. Do not call tools directly from nodes; always go through the registry so tiering, auditing and tracing apply.

## 4. Context first, action second

For LOW and HIGH tools, make sure the agent has READ tools to gather what a human approver needs to see. The `ProposedAction` shown in the approvals UI must include that context.

## 5. Test

- Unit tests for the implementation (valid input, invalid input, edge cases).
- Registry test: tier is set, and for HIGH tools the graph interrupts before execution.
- Audit test: a call writes an `audit_log` row.

## 6. Add eval cases

- `evals/datasets/actions.jsonl`: at least 3 messages that should trigger the tool (with `expected_tier`, `must_interrupt`).
- `evals/datasets/safety.jsonl`: at least 2 attempts to trigger it improperly (injection, wrong client).

## 7. Verify and review

Run `uv run pytest`, then the smoke evals (see the `run-evals` skill), then ask the `security-reviewer` subagent to review the change. Update `docs/ARCHITECTURE.md` §5 if it adds a new example to the tiers table.
