"""Saving and restoring a carrier session: the jar, the auth-only bucket, the profile.

`session.html?seed=1` behaves like a portal that just logged in (an MSAL token
and a draft pointer in sessionStorage, a theme and an in-progress application in
localStorage); without the query it only reports what it finds.
"""

import json

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.browser.session_store import (
    PERSISTABLE_SESSION_KEY,
    SessionStore,
    auth_keys_only,
    reset_state,
)


def _storage(page) -> dict:
    return json.loads(page.locator("#dump").inner_text())


def test_save_keeps_cookies_local_storage_and_only_auth_shaped_session_keys(
    fixture_server, tmp_path
) -> None:
    store = SessionStore(tmp_path)
    with BrowserSession(cdp_port=9251) as session:
        session.goto(f"{fixture_server}/session.html?seed=1")
        path = store.save(session.context, "pie")

    jar = json.loads(path.read_text())
    origin = next(o for o in jar["origins"] if o["origin"] == fixture_server)
    assert {kv["name"] for kv in origin["localStorage"]} == {"theme", "inProgressApplication"}
    assert jar["sessionStorage"] == {fixture_server: {"msal.idtoken": "tok-1"}}  # draftId dropped


def test_restore_brings_the_token_back_and_not_the_draft(fixture_server, tmp_path) -> None:
    store = SessionStore(tmp_path)
    with BrowserSession(cdp_port=9252) as session:
        session.goto(f"{fixture_server}/session.html?seed=1")
        store.save(session.context, "pie")

    saved = store.load("pie")
    assert saved is not None
    with BrowserSession(cdp_port=9253, storage_state=saved.state, session_storage=saved.session_storage) as warm:
        assert warm.reused_session
        page = warm.goto(f"{fixture_server}/session.html")  # no seed: nothing set by the page itself
        got = _storage(page)

    assert got["session"] == {"msal.idtoken": "tok-1"}
    assert got["local"]["theme"] == "dark"


def test_drop_and_keep_keys_prune_local_storage_at_load(fixture_server, tmp_path) -> None:
    store = SessionStore(tmp_path)
    with BrowserSession(cdp_port=9254) as session:
        session.goto(f"{fixture_server}/session.html?seed=1")
        store.save(session.context, "pie")

    dropped = store.load("pie", drop_keys=["inProgressApplication"])
    names = lambda s: {kv["name"] for o in s.state["origins"] for kv in o["localStorage"]}
    assert names(dropped) == {"theme"}

    kept = store.load("pie", keep_keys=["theme"])
    assert names(kept) == {"theme"}


def test_a_restored_session_can_be_reset_before_a_clean_login(fixture_server, tmp_path) -> None:
    store = SessionStore(tmp_path)
    with BrowserSession(cdp_port=9255) as session:
        session.goto(f"{fixture_server}/session.html?seed=1")
        store.save(session.context, "pie")
    saved = store.load("pie")

    with BrowserSession(cdp_port=9256, storage_state=saved.state, session_storage=saved.session_storage) as warm:
        page = warm.goto(f"{fixture_server}/session.html")
        assert _storage(page)["session"]  # restored
        reset_state(warm.context, page)
        page.reload()
        # The init script re-seeds only keys the page has not set; after a reset
        # the token comes back on reload by design, but localStorage is gone.
        assert _storage(page)["local"] == {}


def test_a_persistent_profile_survives_a_new_browser_process(fixture_server, tmp_path) -> None:
    profile = SessionStore(tmp_path).profile_dir("thimble")
    with BrowserSession(cdp_port=9257, profile_dir=profile) as first:
        first.goto(f"{fixture_server}/session.html?seed=1")
    with BrowserSession(cdp_port=9258, profile_dir=profile) as second:
        page = second.goto(f"{fixture_server}/session.html")
        got = _storage(page)
    assert got["local"]["theme"] == "dark"
    assert profile.exists()  # a persistent profile is kept on close


def test_missing_or_broken_jars_read_as_none(tmp_path) -> None:
    store = SessionStore(tmp_path)
    assert store.load("nobody") is None
    store.jar_path("broken").parent.mkdir(parents=True, exist_ok=True)
    store.jar_path("broken").write_text("{not json")
    assert store.load("broken") is None


def test_auth_key_rule() -> None:
    assert PERSISTABLE_SESSION_KEY.search("msal.idtoken")
    assert PERSISTABLE_SESSION_KEY.search("oidc.user:https://idp")
    assert not PERSISTABLE_SESSION_KEY.search("draftId")
    assert auth_keys_only({"o": {"jwt": "x", "draft": "y"}, "p": {"draft": "z"}}) == {"o": {"jwt": "x"}}
