"""Per-carrier login serialiser.

Carrier one-time codes are session-specific, but the shared inbox is keyed only
by carrier. N concurrent logins to one carrier drop N codes into one mailbox
with no way to tell whose is whose; every runner then takes the newest (usually
another run's) and fails. This gates just the login-and-code window: one login
per carrier at a time, so exactly one code is ever in flight. Everything after
login stays parallel.

Cross-process by design: a Postgres session-level advisory lock, auto-released
when the holder's connection dies. The key is `rr:login:<slug>`, the same one
Roadrunner takes, so pointing `LOGIN_LOCK_DATABASE_URL` at Roadrunner's database
serialises both engines against each other.

No-op when the carrier has no MFA (`slug` is None). Fail-open when the database
is unreachable: an unserialised login is a risk, a run that never starts is a
certainty, and the log says which happened. Loop releases the lock on the first
`PageDescription` that is no longer a login stage; `max_hold_s` is the net for
a login that never gets there.
"""

import logging
import threading
import time

import psycopg

log = logging.getLogger(__name__)

LOCK_PREFIX = "rr:login:"


class LoginLockTimeout(TimeoutError):
    """The login queue for this carrier did not clear within the acquire timeout."""


class LoginLock:
    """Acquire with `acquire()` or as a context manager; always `release()` in a finally."""

    def __init__(
        self,
        slug: str | None,
        database_url: str,
        *,
        acquire_timeout_s: float = 900.0,
        max_hold_s: float = 120.0,
        poll_s: float = 1.0,
        connect_timeout_s: float = 5.0,
    ) -> None:
        self.slug = slug
        self.database_url = database_url
        self.acquire_timeout_s = acquire_timeout_s
        self.max_hold_s = max_hold_s
        self.poll_s = poll_s
        self.connect_timeout_s = connect_timeout_s
        self.mode: str = "unacquired"
        """`noop` (no MFA), `held`, `released`, or `unserialised` (database unreachable)."""
        self._conn: psycopg.Connection | None = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return f"{LOCK_PREFIX}{self.slug}"

    @property
    def held(self) -> bool:
        return self.mode == "held"

    def acquire(self) -> "LoginLock":
        if not self.slug:
            self.mode = "noop"
            return self
        try:
            self._conn = psycopg.connect(
                self.database_url, autocommit=True, connect_timeout=int(self.connect_timeout_s)
            )
        except psycopg.Error as e:
            log.warning("login lock database unavailable (%s): proceeding WITHOUT serialising %s", e, self.slug)
            self.mode = "unserialised"
            return self

        log.info("waiting for the login lock %r", self.name)
        deadline = time.monotonic() + self.acquire_timeout_s
        while True:
            try:
                row = self._conn.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s)::bigint)", (self.name,)
                ).fetchone()
            except psycopg.Error as e:
                log.warning("login lock query failed (%s): proceeding WITHOUT serialising %s", e, self.slug)
                self._close()
                self.mode = "unserialised"
                return self
            if row and row[0]:
                break
            if time.monotonic() >= deadline:
                self._close()
                raise LoginLockTimeout(
                    f"could not acquire the login lock for {self.slug!r} within {int(self.acquire_timeout_s)}s"
                )
            time.sleep(self.poll_s)

        self.mode = "held"
        log.info("acquired the login lock %r; logins to %s are serialised", self.name, self.slug)
        if self.max_hold_s:
            self._timer = threading.Timer(self.max_hold_s, self._expire)
            self._timer.daemon = True
            self._timer.start()
        return self

    def _expire(self) -> None:
        if self.held:
            log.warning("login lock %r hit its max hold (%ss); releasing", self.name, self.max_hold_s)
            self.release()

    def release(self) -> None:
        """Idempotent. Safe to call from a finally whatever `acquire()` did."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self.mode == "held" and self._conn is not None:
                try:
                    self._conn.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (self.name,))
                except psycopg.Error:
                    pass  # the connection is gone, and with it the lock
                log.info("released the login lock %r", self.name)
            if self.mode in ("held", "unserialised"):
                self.mode = "released"
            self._close()

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                pass
            self._conn = None

    def __enter__(self) -> "LoginLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
