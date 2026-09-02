"""One saved browser session per carrier, so a warm run performs zero logins.

A jar is Playwright's `storage_state()` (cookies plus per-origin localStorage)
with one extra bucket: `sessionStorage`, filtered to auth-shaped keys. It exists
because MSAL and Azure B2C keep their tokens in sessionStorage, which
`storage_state()` never captures, so a restored jar came back with live cookies
and no app session. Only auth-shaped keys survive, at save AND at restore:
sessionStorage also holds per-draft app state (a live submission pointer, an
in-progress application), and persisting that made a warm run silently resume a
dead draft.

Some identity providers enforce MFA per browser, not per session: a cookie jar
still presents as a brand-new browser and gets re-challenged. For those a
persistent Chromium profile directory is the only thing that survives device
trust; `profile_dir(slug)` names it and `BrowserSession` launches on it.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PERSISTABLE_SESSION_KEY = re.compile(r"token|auth|msal|oidc|jwt|credential", re.IGNORECASE)
"""sessionStorage keys worth keeping. Everything else is app state and is dropped."""


@dataclass
class SavedSession:
    state: dict[str, Any]
    """A Playwright storage_state dict: cookies + origins[].localStorage."""

    session_storage: dict[str, dict[str, str]] = field(default_factory=dict)
    """origin -> {key: value}, auth-shaped keys only."""


def auth_keys_only(by_origin: dict[str, dict[str, str]] | None) -> dict[str, dict[str, str]]:
    kept: dict[str, dict[str, str]] = {}
    for origin, entries in (by_origin or {}).items():
        rows = {k: v for k, v in entries.items() if PERSISTABLE_SESSION_KEY.search(k)}
        if rows:
            kept[origin] = rows
    return kept


def collect_session_storage(context: Any) -> dict[str, dict[str, str]]:
    """Read sessionStorage from every open page, keyed by origin, auth keys only."""
    by_origin: dict[str, dict[str, str]] = {}
    for page in context.pages:
        try:
            got = page.evaluate(
                "() => ({ origin: window.location.origin, entries: Object.fromEntries("
                "Object.keys(sessionStorage).map(k => [k, sessionStorage.getItem(k)])) })"
            )
        except Exception:
            continue  # a closed or cross-origin page contributes nothing
        origin = got.get("origin")
        entries = got.get("entries") or {}
        if origin and origin != "null" and entries:
            by_origin[origin] = {**by_origin.get(origin, {}), **entries}
    return auth_keys_only(by_origin)


def install_session_storage(context: Any, by_origin: dict[str, dict[str, str]]) -> None:
    """Re-seed sessionStorage on every navigation, without overwriting live values.

    An init script that overwrote would clobber a FRESH token with the stale one
    it was restoring, so a key the page has already set is left alone. Filtered
    again here, so a jar written before the auth-key rule cannot re-inject app state.
    """
    saved = auth_keys_only(by_origin)
    if not saved:
        return
    context.add_init_script(
        "(saved => { const mine = saved[window.location.origin]; if (!mine) return;"
        " for (const [k, v] of Object.entries(mine)) { try {"
        " if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k, v); } catch (e) {} } })"
        f"({json.dumps(saved)})"
    )


def reset_state(context: Any, page: Any) -> None:
    """Throw away a restored session that did not hold, then log in clean.

    An SPA that caches its token in memory comes back with live cookies and no
    app session; the login page it shows has nothing our recipe expects. Clear
    everything and start over.
    """
    try:
        context.clear_cookies()
    except Exception:
        pass
    try:
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
    except Exception:
        pass


class SessionStore:
    """Jars and profiles under one directory, one of each per carrier slug."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def jar_path(self, slug: str) -> Path:
        return self.root / f"{slug}.json"

    def profile_dir(self, slug: str) -> Path:
        return self.root / "profiles" / slug

    def load(
        self,
        slug: str,
        *,
        drop_keys: tuple[str, ...] | list[str] = (),
        keep_keys: list[str] | None = None,
    ) -> SavedSession | None:
        """The saved jar for `slug`, or None. Tolerates a hand-edited or truncated file.

        `keep_keys` whitelists localStorage keys (anything the portal adds later
        is dropped by default); `drop_keys` removes named ones. Both exist
        because a portal that parks an in-progress application in localStorage
        resumes it on the next run and lands the wizard off-script.
        """
        path = self.jar_path(slug)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            log.warning("session jar for %s is unreadable (%s); ignoring it", slug, e)
            return None
        session_storage = auth_keys_only(raw.pop("sessionStorage", None))
        for origin in raw.get("origins") or []:
            entries = origin.get("localStorage") or []
            if keep_keys is not None:
                entries = [kv for kv in entries if kv.get("name") in keep_keys]
            if drop_keys:
                entries = [kv for kv in entries if kv.get("name") not in drop_keys]
            origin["localStorage"] = entries
        return SavedSession(state=raw, session_storage=session_storage)

    def save(self, context: Any, slug: str) -> Path:
        """Persist the context's cookies, localStorage and auth-shaped sessionStorage."""
        state = context.storage_state()
        state["sessionStorage"] = collect_session_storage(context)
        path = self.jar_path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
        log.info("saved the browser session for %s", slug)
        return path

    def clear(self, slug: str) -> None:
        path = self.jar_path(slug)
        if path.exists():
            path.unlink()
