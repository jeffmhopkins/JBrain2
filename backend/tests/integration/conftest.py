"""Shared Postgres fixtures for the integration suite.

`database_url` and the session-scoped `_pg_cluster` it depends on live in
`test_rls.py`; re-exporting them here registers both for every integration
module, so `_pg_cluster` resolves even in modules that only import
`database_url`.
"""

from tests.integration.test_rls import _pg_cluster, database_url  # noqa: F401
