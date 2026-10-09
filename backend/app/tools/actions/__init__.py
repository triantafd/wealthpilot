"""Side-effecting tools.

Importing this package registers them. A tool that is defined but never
imported is absent from the registry, and the symptom is an
`UnknownToolError` that depends on import order.
"""

from app.tools.actions.rebalance import propose_rebalance

__all__ = ["propose_rebalance"]
