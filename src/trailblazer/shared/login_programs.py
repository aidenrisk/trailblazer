"""The store for published login prefixes: `carrier_login_programs`.

One login serves every (insurance type, business type) combination of a carrier,
so the artifact is keyed by carrier slug, not by carrier combo. Versions are
insert-only: a bad one is degraded, never overwritten, so it stays revertible.
The active version is the highest that is not degraded.

`walk` is the captured `Walk.login` slice (WalkStep JSON). `program` is left
for ReplayGen's owner: the compiled Program, once that agent compiles one. The
toolkit's `login_replay` runs the slice directly, so nothing here waits on it.
"""

import json
import logging
from dataclasses import dataclass
from typing import Callable, Literal

import psycopg
from psycopg.rows import dict_row

from trailblazer.contracts import WalkStep
from trailblazer.shared.config import Settings, get_settings

log = logging.getLogger(__name__)

Status = Literal["candidate", "locked", "degraded"]
ACTIVE_STATUSES: tuple[Status, ...] = ("locked", "candidate")


@dataclass
class LoginProgram:
    id: int
    carrier_slug: str
    version: int
    status: Status
    steps: list[WalkStep]
    program: dict | None = None
    degraded_reason: str | None = None


Connector = Callable[[], psycopg.Connection]


class LoginProgramStore:
    def __init__(self, settings: Settings | None = None, connect: Connector | None = None) -> None:
        settings = settings or get_settings()
        self._connect = connect or (lambda: psycopg.connect(settings.database_url, row_factory=dict_row))

    @staticmethod
    def _row(row: dict) -> LoginProgram:
        raw_steps = row["walk"] if isinstance(row["walk"], list) else json.loads(row["walk"])
        return LoginProgram(
            id=row["id"],
            carrier_slug=row["carrier_slug"],
            version=row["version"],
            status=row["status"],
            steps=[WalkStep.model_validate(s) for s in raw_steps],
            program=row.get("program"),
            degraded_reason=row.get("degraded_reason"),
        )

    def active(self, slug: str) -> LoginProgram | None:
        """The highest non-degraded version for `slug`, or None when the login must be captured."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, carrier_slug, version, status, walk, program, degraded_reason
                  FROM carrier_login_programs
                 WHERE carrier_slug = %(slug)s AND status = ANY(%(statuses)s)
                 ORDER BY version DESC
                 LIMIT 1
                """,
                {"slug": slug, "statuses": list(ACTIVE_STATUSES)},
            ).fetchone()
        return self._row(row) if row else None

    def save(
        self, slug: str, steps: list[WalkStep], *, program: dict | None = None, status: Status = "candidate"
    ) -> LoginProgram:
        """Insert the next version. Two racing captures cannot clobber each other: the loser retries."""
        if not steps:
            raise ValueError("a login program needs at least one step")
        walk_json = json.dumps([s.model_dump(mode="json") for s in steps])
        program_json = json.dumps(program) if program is not None else None
        for _ in range(5):
            with self._connect() as conn:
                current = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS v FROM carrier_login_programs WHERE carrier_slug = %(slug)s",
                    {"slug": slug},
                ).fetchone()["v"]
                try:
                    row = conn.execute(
                        """
                        INSERT INTO carrier_login_programs (carrier_slug, version, status, walk, program)
                        VALUES (%(slug)s, %(version)s, %(status)s, %(walk)s::jsonb, %(program)s::jsonb)
                        RETURNING id, carrier_slug, version, status, walk, program, degraded_reason
                        """,
                        {"slug": slug, "version": current + 1, "status": status, "walk": walk_json, "program": program_json},
                    ).fetchone()
                    conn.commit()
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
                    continue
            saved = self._row(row)
            log.info("saved login program %s v%d (%d steps, %s)", slug, saved.version, len(steps), status)
            return saved
        raise RuntimeError(f"could not allocate a login program version for {slug!r} after 5 attempts")

    def mark_degraded(self, program_id: int, reason: str) -> None:
        """Take a version out of service. Replay falls back to the previous one, or to capture."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE carrier_login_programs
                   SET status = 'degraded', degraded_reason = %(reason)s, updated_at = CURRENT_TIMESTAMP
                 WHERE id = %(id)s
                """,
                {"id": program_id, "reason": (reason or "")[:500]},
            )
            conn.commit()
        log.warning("login program %d degraded: %s", program_id, reason)

    def mark_locked(self, program_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE carrier_login_programs SET status = 'locked', updated_at = CURRENT_TIMESTAMP WHERE id = %(id)s",
                {"id": program_id},
            )
            conn.commit()

    def versions(self, slug: str) -> list[LoginProgram]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, carrier_slug, version, status, walk, program, degraded_reason
                  FROM carrier_login_programs WHERE carrier_slug = %(slug)s ORDER BY version
                """,
                {"slug": slug},
            ).fetchall()
        return [self._row(r) for r in rows]
