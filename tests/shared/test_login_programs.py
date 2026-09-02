"""The login program store against the project Postgres. Skips when it is down."""

import uuid

import psycopg
import pytest

from trailblazer.contracts import LOGIN_EMAIL, LOGIN_PASSWORD, WalkStep
from trailblazer.shared.config import Settings
from trailblazer.shared.login_programs import LoginProgramStore

SETTINGS = Settings(_env_file=None)


def _db_up() -> bool:
    try:
        psycopg.connect(SETTINGS.database_url, connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="project Postgres not running on 15434")

STEPS = [
    WalkStep(action="type", fieldId="q_001", locator="#username", credentialKey=LOGIN_EMAIL),
    WalkStep(action="type", fieldId="q_002", locator="#password", credentialKey=LOGIN_PASSWORD),
    WalkStep(action="click", locator='button:has-text("Sign in") >> visible=true'),
]


@pytest.fixture
def slug():
    """A throwaway carrier slug, cleaned up afterwards."""
    s = f"test-{uuid.uuid4().hex[:8]}"
    yield s
    with psycopg.connect(SETTINGS.database_url) as conn:
        conn.execute("DELETE FROM carrier_login_programs WHERE carrier_slug = %s", (s,))
        conn.commit()


def test_versions_are_insert_only_and_the_newest_non_degraded_is_active(slug) -> None:
    store = LoginProgramStore(SETTINGS)
    assert store.active(slug) is None

    v1 = store.save(slug, STEPS)
    v2 = store.save(slug, STEPS[:2])
    assert (v1.version, v2.version) == (1, 2)
    assert store.active(slug).version == 2

    store.mark_degraded(v2.id, "step 2 (#password) not_found")
    active = store.active(slug)
    assert active.version == 1  # the previous good version comes back into service
    assert [p.status for p in store.versions(slug)] == ["candidate", "degraded"]
    assert store.versions(slug)[1].degraded_reason.startswith("step 2")


def test_steps_round_trip_with_their_credential_keys_and_no_values(slug) -> None:
    store = LoginProgramStore(SETTINGS)
    saved = store.save(slug, STEPS)
    loaded = store.active(slug)
    assert loaded.steps == STEPS
    assert all(s.value is None for s in loaded.steps if s.credentialKey)
    assert saved.program is None


def test_locking_a_version_keeps_it_active_over_a_newer_candidate_only_by_version_order(slug) -> None:
    store = LoginProgramStore(SETTINGS)
    v1 = store.save(slug, STEPS)
    store.mark_locked(v1.id)
    assert store.active(slug).status == "locked"
    store.save(slug, STEPS)  # a new candidate is newer, so it is the active one; locking is a promotion mark
    assert store.active(slug).version == 2


def test_an_empty_prefix_is_refused(slug) -> None:
    with pytest.raises(ValueError):
        LoginProgramStore(SETTINGS).save(slug, [])
