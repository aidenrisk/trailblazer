"""One place that knows how to reach the project's Postgres.

Thin by design: `connect()` returns a psycopg connection with dict rows, and
callers own the transaction. Anything needing a connection takes it (or a
fetcher built on it) as a parameter, so tests never touch a database.
"""

import psycopg
from psycopg.rows import dict_row

from trailblazer.shared.config import Settings, get_settings


def connect(settings: Settings | None = None, url: str | None = None) -> psycopg.Connection:
    """Open a connection to `url`, or to the configured `database_url`."""
    settings = settings or get_settings()
    return psycopg.connect(url or settings.database_url, row_factory=dict_row)


def fetch_one(sql: str, params: dict, settings: Settings | None = None) -> dict | None:
    """Run one query and return its first row as a dict, or None."""
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    return dict(row) if row is not None else None
