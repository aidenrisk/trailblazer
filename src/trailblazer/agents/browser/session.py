"""Browser lifecycle: launch Chromium with a debugging port, attach over CDP.

The CDP endpoint is kept on the session so a later MCP client (the form filler
is the likely first caller) can attach to the *same* browser with no rework.

Sync Playwright API deliberately: nothing here wants concurrency, and it keeps
the CLI and the tests straightforward.
"""

import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import Browser, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from trailblazer.observability.logging import get_logger

log = get_logger(__name__)


def _devtools_version(port: int, host: str = "127.0.0.1") -> dict | None:
    """Return `/json/version` if a DevTools server answers on `port`, else None.

    The HTTP endpoint is probed rather than the TCP port: a port accepts
    connections before DevTools is serving, and an unrelated process (a running
    Chrome, commonly) can hold the port and 404 every request. Both cases
    produce a confusing `connect_over_cdp` failure if not distinguished here.
    """
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=0.5) as r:
            body = json.load(r)
        return body if "webSocketDebuggerUrl" in body else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True when anything at all is listening on `port`."""
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


class BrowserSession:
    """One Chromium process plus a CDP connection to it.

    Use as a context manager; `page` is the live tab.
    """

    def __init__(self, cdp_port: int = 9222, headed: bool = False) -> None:
        self.cdp_port = cdp_port
        self.headed = headed
        self.cdp_endpoint = f"http://127.0.0.1:{cdp_port}"
        self._proc: subprocess.Popen | None = None
        self._playwright = None
        self._profile_dir: str | None = None
        self.browser: Browser | None = None
        self.page: Page | None = None

    def start(self) -> "BrowserSession":
        """Launch Chromium on the debugging port and attach to it.

        Every failure after the driver is started tears down what was built
        before re-raising. `__enter__` returns `start()`, so a raise here means
        `__exit__` never runs -- without this the driver, the Chromium process
        and the temp profile all leak, and the leaked browser then holds the
        port so the next run fails the same way.
        """
        # Chromium given a busy port exits or falls back silently, and the
        # resulting connect_over_cdp error points at the wrong thing entirely.
        if _port_in_use(self.cdp_port):
            raise RuntimeError(
                f"port {self.cdp_port} is already in use (often a running Chrome). "
                "Set CDP_PORT to a free port, or close the other browser."
            )
        try:
            return self._start()
        except BaseException:
            self.close()
            raise

    def _start(self) -> "BrowserSession":
        """Do the launching. Called only by `start()`, which owns the cleanup."""
        log.info("browser launch cdp_port=%s headed=%s", self.cdp_port, self.headed)
        self._playwright = sync_playwright().start()
        self._profile_dir = tempfile.mkdtemp(prefix="trailblazer-profile-")
        args = [
            self._playwright.chromium.executable_path,
            f"--remote-debugging-port={self.cdp_port}",
            f"--user-data-dir={self._profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if not self.headed:
            args.append("--headless=new")
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        deadline = time.time() + 30
        while _devtools_version(self.cdp_port) is None:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"Chromium exited with code {self._proc.returncode} before serving CDP; "
                    f"port {self.cdp_port} may be in use by another process"
                )
            if time.time() > deadline:
                raise RuntimeError(f"Chromium did not serve CDP on port {self.cdp_port} within 30s")
            time.sleep(0.1)

        self.browser = self._playwright.chromium.connect_over_cdp(self.cdp_endpoint)

        # Both lists can be empty on a browser with no context or tab yet; an
        # IndexError here would surface as a confusing failure far from the cause.
        contexts = self.browser.contexts
        context = contexts[0] if contexts else self.browser.new_context()
        pages = context.pages
        self.page = pages[0] if pages else context.new_page()
        log.info("browser attached cdp_endpoint=%s", self.cdp_endpoint)
        return self

    def goto(self, url: str) -> Page:
        """Navigate the live tab and wait for the network to go quiet."""
        if self.page is None:
            raise RuntimeError("session not started; call start() or use the context manager")
        log.info("navigate url=%s", url)
        try:
            self.page.goto(url, wait_until="networkidle")
        except PlaywrightTimeoutError as e:
            raise RuntimeError(
                f"navigation to {url} timed out waiting for the network to go quiet; "
                "the page may load forever (polling, websockets) or the URL may be wrong"
            ) from e
        except PlaywrightError as e:
            raise RuntimeError(f"navigation to {url} failed: {e}") from e
        return self.page

    def close(self) -> None:
        """Detach, kill the browser, drop the temp profile.

        Order matters: the CDP connection is dropped before the process is
        killed, otherwise Playwright's event loop is left talking to a dead
        target and raises TargetClosedError during teardown.
        """
        self.page = None
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass  # browser already gone; nothing to detach from
            self.browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._profile_dir is not None:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()
