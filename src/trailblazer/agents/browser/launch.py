"""Launch a headed Chromium that outlives the command that started it.

`BrowserSession` is the wrong tool for a hand-authenticated session on two
counts: it launches on a `mkdtemp` profile and its `close()` kills the browser
and deletes that profile. A human who logs in would lose the login the moment
the scrape process exited.

So this launches detached and leaves it running. The profile is a fixed
directory in the repo (gitignored -- it holds real carrier session cookies), so
cookies also survive a browser restart, not just a scrape.

Nothing here connects over CDP. The launched browser only *serves* the DevTools
endpoint; `AttachedSession` is what later connects to it, once per scrape.
"""

import subprocess
import sys
import time
from pathlib import Path

from trailblazer.agents.browser.session import devtools_running, port_in_use
from trailblazer.observability.logging import get_logger

log = get_logger(__name__)

PROFILE_DIR = Path(".browser-profile")
"""Default persistent user-data-dir. Gitignored: it holds real session cookies."""


def _chromium_path() -> str:
    """Ask Playwright where its Chromium lives, without launching anything.

    Playwright installs Chromium under `~/Library/Caches/ms-playwright`, whose
    name carries a build number that changes on upgrade, so the path is queried
    rather than assumed.

    The driver runs an asyncio loop that is not fully drained by `stop()`, and
    the leftover task prints a TargetClosedError traceback at interpreter exit.
    It is cosmetic -- the path was already read -- but it reads as a crash, so
    the driver is read in a subprocess whose exit takes the loop with it.
    """
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from playwright.sync_api import sync_playwright\n"
            "p = sync_playwright().start()\n"
            "print(p.chromium.executable_path)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def launch_persistent(cdp_port: int = 9222, profile_dir: Path = PROFILE_DIR) -> str:
    """Start a headed Chromium serving CDP on `cdp_port` and return its endpoint.

    Returns as soon as DevTools answers, leaving the browser running with no
    parent to wait on it. Raises if the port is taken -- unless a DevTools
    server is already answering there, in which case that browser is reused and
    an existing login is not disturbed.
    """
    endpoint = f"http://127.0.0.1:{cdp_port}"

    if devtools_running(cdp_port):
        log.info("reusing browser already serving cdp_endpoint=%s", endpoint)
        return endpoint
    if port_in_use(cdp_port):
        raise RuntimeError(
            f"port {cdp_port} is held by a process that is not serving DevTools "
            "(a normal Chrome, commonly). Set CDP_PORT to a free port."
        )

    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        _chromium_path(),
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    # start_new_session detaches it from this process group, so the browser is
    # not killed when the launching shell or CLI exits.
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    log.info("browser launching pid=%s cdp_port=%s profile=%s", proc.pid, cdp_port, profile_dir)

    # Wait for DevTools rather than for the process: a launch that dies on a bad
    # profile or a taken port exits within a second, and polling the endpoint
    # distinguishes "not ready yet" from "never coming".
    deadline = time.time() + 30
    while not devtools_running(cdp_port):
        if proc.poll() is not None:
            raise RuntimeError(
                f"Chromium exited with code {proc.returncode} before serving CDP on {cdp_port}"
            )
        if time.time() > deadline:
            raise RuntimeError(f"Chromium did not serve CDP on port {cdp_port} within 30s")
        time.sleep(0.1)

    log.info("browser ready cdp_endpoint=%s pid=%s", endpoint, proc.pid)
    return endpoint
