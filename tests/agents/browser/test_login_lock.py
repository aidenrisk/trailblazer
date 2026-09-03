"""The per-carrier login lock against the project Postgres.

Skipped when the database is not up (`docker compose up -d db`). Two locks on
one slug are two connections, which is exactly the cross-process case.
"""

import psycopg
import pytest

from trailblazer.agents.browser.login_lock import LoginLock, LoginLockTimeout
from trailblazer.shared.config import Settings

DB_URL = Settings(_env_file=None).database_url


def _db_up() -> bool:
    try:
        psycopg.connect(DB_URL, connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


needs_db = pytest.mark.skipif(not _db_up(), reason="project Postgres not running on 15434")


@needs_db
def test_one_login_per_carrier_at_a_time() -> None:
    first = LoginLock("thimble", DB_URL, poll_s=0.1).acquire()
    try:
        assert first.held
        second = LoginLock("thimble", DB_URL, acquire_timeout_s=0.6, poll_s=0.1)
        with pytest.raises(LoginLockTimeout, match="thimble"):
            second.acquire()
    finally:
        first.release()
    assert first.mode == "released"

    # Released: the next in the queue gets it straight away.
    with LoginLock("thimble", DB_URL, acquire_timeout_s=2, poll_s=0.1) as third:
        assert third.held


@needs_db
def test_different_carriers_do_not_queue_behind_each_other() -> None:
    with LoginLock("thimble", DB_URL, poll_s=0.1) as a, LoginLock("chubb", DB_URL, poll_s=0.1) as b:
        assert a.held and b.held


@needs_db
def test_the_key_is_the_one_roadrunner_uses() -> None:
    lock = LoginLock("next_insurance", DB_URL)
    assert lock.name == "rr:login:next_insurance"


@needs_db
def test_max_hold_releases_a_wedged_login() -> None:
    lock = LoginLock("pie", DB_URL, max_hold_s=0.3, poll_s=0.1).acquire()
    assert lock.held
    import time

    time.sleep(0.8)
    assert lock.mode == "released"
    with LoginLock("pie", DB_URL, acquire_timeout_s=1, poll_s=0.1) as again:
        assert again.held


def test_no_mfa_means_no_lock() -> None:
    lock = LoginLock(None, DB_URL).acquire()
    assert lock.mode == "noop" and not lock.held
    lock.release()  # harmless


def test_an_unreachable_database_fails_open() -> None:
    lock = LoginLock("thimble", "postgresql://postgres:postgres@127.0.0.1:9/nowhere", connect_timeout_s=1)
    assert lock.acquire().mode == "unserialised"
    lock.release()
    assert lock.mode == "released"
