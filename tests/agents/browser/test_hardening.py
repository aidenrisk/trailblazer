"""Phase 7: code sources beyond email, the handoff hook, dead-session detection, lock sharing."""

import time
from pathlib import Path

import pytest

from trailblazer.agents.browser.code_sources import FileDropSource, TotpSource, code_source_for, totp
from trailblazer.agents.browser.mfa import HANDOFF_INSTRUCTION, wait_for_otp_clear
from trailblazer.agents.browser.net_watch import AuthFailureWatch
from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.browser import login_actions as la
from trailblazer.shared.carrier_creds import CarrierCreds, MfaConfig, resolve_carrier_creds
from trailblazer.shared.config import Settings
from trailblazer.shared.crypto import encrypt_secret, parse_key
from tests.agents.browser.test_mfa_pure import FakeElement, FakePage

# RFC 6238 test vector: ASCII seed "12345678901234567890", SHA-1, T=59 -> 94287082 (last six: 287082).
RFC_SEED_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #


def test_totp_matches_the_rfc_6238_vector() -> None:
    assert totp(RFC_SEED_B32, at=59) == "287082"
    assert totp(RFC_SEED_B32, at=1111111109) == "081804"
    assert totp(RFC_SEED_B32, at=1234567890) == "005924"


def test_totp_tolerates_spaces_lowercase_and_missing_padding() -> None:
    assert totp("gezd gnbv gy3t qojq gezd gnbv gy3t qojq", at=59) == "287082"


def test_totp_source_always_has_a_code_and_is_never_queued() -> None:
    source = TotpSource(RFC_SEED_B32)
    code = source.fetch("any")
    assert code is not None and len(code) == 6 and code.isdigit()
    assert source.queued is False and source.disabled is False
    with pytest.raises(ValueError):
        TotpSource("")


def test_a_totp_carrier_clears_the_code_screen_without_any_inbox(fixture_server, monkeypatch) -> None:
    """The stand-in accepts 123456; make the seed produce exactly that for this run."""
    monkeypatch.setattr("trailblazer.agents.browser.code_sources.totp", lambda *a, **k: "123456")
    creds = CarrierCreds(
        slug="mgt", login_url="https://x", username="u", password="p",
        mfa=MfaConfig(enabled=True, channel="totp", totp_secret=RFC_SEED_B32),
    )
    with BrowserSession(cdp_port=9301) as s:
        page = s.goto(f"{fixture_server}/otp.html")
        out = la.clear_otp(
            page, "#code", creds, inbox=None, human_entry_possible=False,
            timeout_s=20, poll_s=0.2, settle_s=0.5, markers=["/otp"],
        )
        landed = page.url
    assert out.cleared and landed.endswith("/dashboard.html")
    assert out.drained == 0 and out.steered is False  # nothing queued, nothing to steer


# --------------------------------------------------------------------------- #
# Manual file drop
# --------------------------------------------------------------------------- #


def test_file_drop_consumes_the_code_once_and_ignores_a_stale_one(tmp_path) -> None:
    drop = tmp_path / "pie.otp"
    source = FileDropSource(drop, max_age_s=60)
    assert source.fetch("pie") is None  # nothing yet; the operator was told where to write

    drop.write_text("654321\n")
    assert source.fetch("pie") == "654321"
    assert not drop.exists()  # consumed
    assert source.fetch("pie") is None

    drop.write_text("111111")
    old = time.time() - 3600
    import os

    os.utime(drop, (old, old))
    assert source.fetch("pie") is None  # older than any code's window


def test_code_source_for_follows_the_carriers_channel(tmp_path) -> None:
    inbox = OtpInbox("http://x", "a", "c")
    settings = Settings(_env_file=None, sessions_dir=str(tmp_path))
    email = CarrierCreds(slug="a", login_url="https://x", mfa=MfaConfig(enabled=True))
    seed = CarrierCreds(slug="b", login_url="https://x", mfa=MfaConfig(enabled=True, channel="totp", totp_secret=RFC_SEED_B32))
    manual = CarrierCreds(slug="c", login_url="https://x", mfa=MfaConfig(enabled=True, channel="manual"))
    off = CarrierCreds(slug="d", login_url="https://x")
    seedless = CarrierCreds(slug="e", login_url="https://x", mfa=MfaConfig(enabled=True, channel="totp"))

    assert code_source_for(email, inbox, settings) is inbox
    assert isinstance(code_source_for(seed, inbox, settings), TotpSource)
    drop = code_source_for(manual, inbox, settings)
    assert isinstance(drop, FileDropSource) and drop.path == Path(tmp_path) / "c.otp"
    assert code_source_for(off, inbox, settings) is inbox  # MFA off: the inbox is harmless and unused
    assert code_source_for(seedless, inbox, settings) is None  # misconfigured: say so, do not guess


def test_the_totp_seed_is_stored_encrypted_and_comes_back_decrypted() -> None:
    key_hex = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    settings = Settings(_env_file=None, cred_encryption_key=key_hex)
    row = {
        "slug": "mgt", "login_url": "https://x", "username": "u", "password": None,
        "mfa": {"enabled": True, "channel": "totp", "totp_secret": encrypt_secret(RFC_SEED_B32, parse_key(key_hex))},
    }
    creds = resolve_carrier_creds("mgt", settings, fetch_row=lambda _: row)
    assert creds.mfa.channel == "totp" and creds.mfa.totp_secret == RFC_SEED_B32
    assert RFC_SEED_B32 in creds.secrets()  # redacted from logs like the password


# --------------------------------------------------------------------------- #
# Human handoff
# --------------------------------------------------------------------------- #


def test_the_handoff_hook_is_told_once_what_a_person_must_do() -> None:
    told: list[str] = []
    page = FakePage([FakeElement("code", kind="otp"), FakeElement("Verify")])
    out = wait_for_otp_clear(
        page, inbox=None, human_entry_possible=True, on_handoff=told.append, timeout_s=0.3, poll_s=0.1
    )
    assert told == [HANDOFF_INSTRUCTION]
    assert not out.cleared and "nobody entered a code" in out.reason


def test_a_failing_hook_never_takes_the_run_down() -> None:
    def boom(msg):
        raise RuntimeError("UI is gone")

    page = FakePage([FakeElement("code", kind="otp")])
    out = wait_for_otp_clear(page, inbox=None, human_entry_possible=True, on_handoff=boom, timeout_s=0.2, poll_s=0.1)
    assert not out.cleared  # timed out normally; the hook's failure was logged and swallowed


# --------------------------------------------------------------------------- #
# Dead-session detection
# --------------------------------------------------------------------------- #


def test_the_watch_counts_the_portals_own_401s(fixture_server) -> None:
    with BrowserSession(cdp_port=9302) as s:
        watch = AuthFailureWatch().attach(s.context)
        s.goto(f"{fixture_server}/stale.html")
        s.page.wait_for_timeout(500)
        dead = watch.session_looks_dead
        seen = watch.count
        s.goto(f"{fixture_server}/dashboard.html")
        watch.reset()
        s.page.wait_for_timeout(300)
        after_reset = watch.count
    assert dead and seen >= 3
    assert after_reset == 0


def test_a_dead_restored_session_is_reset_and_logged_in_clean() -> None:
    """ensure_login with fakes: the first look is an app shell, the API said 401 three times."""
    from trailblazer.agents.browser.login_replay import LoginReplay
    from trailblazer.contracts import LOGIN_EMAIL, LOGIN_PASSWORD, Diff, PageDescription, WalkStep
    from trailblazer.loop.login import ensure_login
    from tests.agents.frontier.frontier_test_data import FORM_AFTER_LOGIN, LOGIN_PAGE
    from tests.fakes import FakeLock, FakeProgramStore

    class Page:
        url = "https://portal.example/app"
        gotos: list[str] = []

        def goto(self, url, **kw):
            self.gotos.append(url)
            self.url = url

        def evaluate(self, script):
            return None

    class Context:
        def clear_cookies(self):
            pass

    class Session:
        page = Page()
        context = Context()
        auth_failures = AuthFailureWatch(count=3, samples=["401 https://portal.example/api/me"])

    looks = [PageDescription(**FORM_AFTER_LOGIN), PageDescription(**LOGIN_PAGE), PageDescription(**FORM_AFTER_LOGIN)]

    class Scraper:
        def look(self, *a, **k):
            return looks.pop(0), Diff(polarity="-ve")

    prefix = [
        WalkStep(action="type", fieldId="q", locator="#username", credentialKey=LOGIN_EMAIL),
        WalkStep(action="type", fieldId="q", locator="#password", credentialKey=LOGIN_PASSWORD),
        WalkStep(action="click", locator="#go"),
    ]
    creds = CarrierCreds(slug="pie", login_url="https://portal.example/login", username="u", password="p", mfa=MfaConfig(enabled=True))
    store = FakeProgramStore("pie", prefix)
    FakeLock.made.clear()

    outcome, page, _, _ = ensure_login(
        "job", Session(), creds, scraper=Scraper(), frontier=None, programs=store,
        settings=Settings(_env_file=None), lock_url="postgresql://unused",
        replay=lambda *a, **k: LoginReplay("ok", "faked", final_url="https://portal.example/dash"),
        lock_factory=FakeLock,
    )

    assert outcome.status == "replayed"  # not "session_held": the dead jar was seen through
    assert Session.page.gotos == ["https://portal.example/login"]  # reset, then back to the login page
    assert Session.auth_failures.count == 0
    assert page.stageId == "form_page_1_business_info"


# --------------------------------------------------------------------------- #
# Sharing the lock with Roadrunner is one setting
# --------------------------------------------------------------------------- #


def test_the_lock_database_defaults_to_ours_and_can_point_at_roadrunners() -> None:
    ours = Settings(_env_file=None)
    assert ours.effective_login_lock_database_url == ours.database_url
    shared = Settings(_env_file=None, login_lock_database_url="postgresql://rr:rr@roadrunner-db/road_runner")
    assert shared.effective_login_lock_database_url.endswith("/road_runner")
