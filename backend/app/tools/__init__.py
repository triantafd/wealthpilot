"""Agent tools, and the registry that gives each one a risk tier.

**Importing this package registers every tool.** That is the point: a tool
defined but never imported is absent from the registry, and the symptom is an
`UnknownToolError` at runtime that depends on import order. The warning was
written in `actions/__init__.py` and then shipped anyway — the app never
imported the package, only the tests did, so the registry was populated under
pytest and empty in production. Streaming a real "rebalance AC-0001" found it;
no unit test could have, because every one of them imports the tools itself.

The import sits at the bottom, after nothing: `registry` does not import from
this package, so there is no cycle.
"""

from app.tools import actions as actions
