"""Create LangGraph's checkpoint tables.

    uv run python -m app.scripts.setup_checkpointer

Run once per database, alongside `alembic upgrade head`. Deliberately not done
on application startup: it issues DDL, and two replicas booting together would
race to create the same tables.
"""

import asyncio

from app.agents.checkpointer import setup_checkpointer


def main() -> None:
    asyncio.run(setup_checkpointer())
    print("checkpoint tables ready")


if __name__ == "__main__":
    main()
