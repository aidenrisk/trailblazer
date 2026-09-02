"""Two tiny servers the browser-level tests share.

`fixture_server` serves the stand-in pages from tests/pages.py over HTTP, written
to a temp directory for the session, so pages have a real origin (localStorage
and sessionStorage need one; `file://` gives `null`). `fake_inbox` plays the
backend's one-time-code endpoint from a script of responses and records what it
was asked.
"""

import json
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.pages import write_pages


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - silence per-request logging
        pass


SERVED_DIRS: dict[str, str] = {}
"""base URL -> the directory it serves, so a test can add a page beside the stand-ins."""


@pytest.fixture(scope="session")
def fixture_server(tmp_path_factory):
    """Base URL of an HTTP server rooted at a temp dir holding the stand-in pages."""
    pages = write_pages(tmp_path_factory.mktemp("pages"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(pages)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    SERVED_DIRS[url] = str(pages)
    yield url
    server.shutdown()


class FakeInbox:
    """A scripted `/api/internal/mfa/{slug}/otp`. Each call pops one response."""

    def __init__(self, responses: list[tuple[int, dict | None]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.default = (204, None)
        inbox = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                inbox.calls.append({"path": self.path, "headers": dict(self.headers)})
                status, body = inbox.responses.pop(0) if inbox.responses else inbox.default
                self.send_response(status)
                if body is not None:
                    payload = json.dumps(body).encode()
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_header("content-length", "0")
                    self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def fake_inbox():
    """Factory: `fake_inbox([(204, None), (200, {"code": "123456"})])`."""
    made: list[FakeInbox] = []

    def make(responses):
        inbox = FakeInbox(responses)
        made.append(inbox)
        return inbox

    yield make
    for inbox in made:
        inbox.close()
