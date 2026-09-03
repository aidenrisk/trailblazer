"""The inbox client against a scripted backend. No browser."""

import httpx

from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.shared.config import Settings


def _client(url: str, **kw) -> OtpInbox:
    return OtpInbox(url, "app-secret", "cron-secret", backoff_s=0, **kw)


def test_a_waiting_code_is_returned_and_both_secrets_are_sent(fake_inbox) -> None:
    server = fake_inbox([(200, {"code": "123456"})])

    assert _client(server.url).fetch("thimble") == "123456"
    call = server.calls[0]
    assert call["path"] == "/api/internal/mfa/thimble/otp"
    assert call["headers"]["x-api-secret"] == "app-secret"
    assert call["headers"]["x-cron-secret"] == "cron-secret"


def test_nothing_waiting_is_none_and_not_retried(fake_inbox) -> None:
    server = fake_inbox([(204, None)])

    assert _client(server.url, retries=3).fetch("thimble") is None
    assert len(server.calls) == 1  # a 204 means "poll again later", not "try harder now"


def test_a_malformed_code_is_refused(fake_inbox) -> None:
    server = fake_inbox([(200, {"code": "12-34"})])
    assert _client(server.url).fetch("thimble") is None


def test_a_bad_secret_disables_auto_pull_for_the_run(fake_inbox) -> None:
    server = fake_inbox([(401, {"error": "unauthorized"}), (200, {"code": "123456"})])
    client = _client(server.url)

    assert client.fetch("thimble") is None
    assert client.disabled
    assert client.fetch("thimble") is None  # never asks again
    assert len(server.calls) == 1


def test_transport_errors_are_retried_then_given_up_for_this_poll() -> None:
    dead = _client("http://127.0.0.1:9", retries=2)  # port 9 is discard; nothing listens

    assert dead.fetch("thimble") is None
    assert not dead.disabled  # a blip is not a bad secret; the next poll tries again


def test_no_slug_means_no_request(fake_inbox) -> None:
    server = fake_inbox([(200, {"code": "123456"})])
    assert _client(server.url).fetch(None) is None
    assert server.calls == []


def test_from_settings_needs_all_three_values() -> None:
    assert OtpInbox.from_settings(Settings(_env_file=None)) is None
    partial = Settings(aiden_backend_url="http://x", aiden_app_secret="a", _env_file=None)
    assert OtpInbox.from_settings(partial) is None
    full = Settings(
        aiden_backend_url="http://x/", aiden_app_secret="a", aiden_internal_secret="c", _env_file=None
    )
    inbox = OtpInbox.from_settings(full)
    assert inbox is not None and inbox.base == "http://x"


def test_an_injected_client_is_used(fake_inbox) -> None:
    server = fake_inbox([(200, {"code": "654321"})])
    with httpx.Client(timeout=2) as http:
        assert _client(server.url, client=http).fetch("pie") == "654321"


def test_peek_reads_without_consuming_and_reachable_accepts_204(fake_inbox) -> None:
    server = fake_inbox([(200, {"code": "123456", "receivedAt": "2026-09-03T00:00:00Z"}), (204, None), (401, None)])
    client = _client(server.url)

    assert client.peek("pie") == (200, "123456")
    assert client.reachable("pie") is True  # 204: reachable and empty
    assert client.reachable("pie") is False  # 401: wrong secret
    assert all(c["path"].endswith("/pie/peek") for c in server.calls)


def test_reachable_is_false_when_nothing_listens() -> None:
    assert _client("http://127.0.0.1:9", retries=1).reachable("pie") is False
