"""Read-only text-to-SQL: generation, sqlglot validation, safe execution.

Importing this package registers `run_sql`. A tool that is defined but never
imported is absent from the registry, and the symptom is an
`UnknownToolError` that depends on import order.
"""

from app.tools.sql.run_sql import run_sql

__all__ = ["run_sql"]
