"""Contract violations findable in a replay script's source, without running it.

Section 6 of the architecture spec calls these "checked at persist (warn-only;
the flow stays draft)". They are warnings rather than errors for a reason: a
script that trips one may still produce a quote, and refusing to run it would
discard that evidence. The Validator reports them and runs anyway.

Each check is a regex over the script text. That is deliberate -- the runner
itself regex-scrapes the script for its artifact filenames, so the script's
surface syntax is already load-bearing and a parser would assert more structure
than the contract does.
"""

import re
from pathlib import Path

METADATA_REF = re.compile(r"onboarding-[\w-]+\.metadata\.json")
"""The filename the runner materializes the metadata document under."""

QUESTIONS_REF = re.compile(r"onboarding-[\w-]+\.questions\.json")
"""The filename the runner materializes the questions document under."""

_ANY_METADATA = re.compile(r"[\w./-]*metadata\.json")
_ANY_QUESTIONS = re.compile(r"[\w./-]*questions\.json")

_RRSTATUS = re.compile(r"RRSTATUS")
_LAST_RUN = re.compile(r"last-run\.json")

_HUMAN_BEHAVIOR_FRAME = re.compile(
    r"new\s+HumanBehavior\s*\(\s*[\w.]*(?:frame|Frame)[\w.]*\s*\)"
)
"""`new HumanBehavior(<FrameLocator>)` crashes at runtime; construct on the Page."""

_LOGIN_LOCK_CALL = re.compile(r"acquireCarrierLoginLock\s*\(")
_LOGIN_LOCK_DESTRUCTURED = re.compile(
    r"(?:const|let|var)\s*\{[^}]*\brelease\b[^}]*\}\s*=\s*(?:await\s+)?acquireCarrierLoginLock\s*\("
)
"""`acquireCarrierLoginLock()` returns `{release}`; a bare binding is not callable."""

_HARDCODED_GATE = re.compile(
    r"\bpick(?:YesNo|Radio|Option|Choice)\s*\(\s*['\"`](?:Yes|No)['\"`]\s*\)",
    re.IGNORECASE,
)
"""A literal gate answer discards the client's real answer."""

_REQUIRED_FALLBACK = re.compile(
    r"answers(?:\.[\w$]+|\[[^\]]+\])+\s*\|\|\s*(?!\s*$)"
)
"""`||` on an answer invents a value the client never gave."""

_ALLOWED_CRED_KEYS = {"LOGIN_EMAIL", "LOGIN_PASSWORD", "MFA_CARRIER_ID", "HEADLESS"}

_CRED_READ = re.compile(
    r"(?:config|creds|cfg|credentials)\s*(?:\.\s*([A-Z_][A-Z0-9_]*)|\[\s*['\"`]([A-Z_][A-Z0-9_]*)['\"`]\s*\])"
)
"""A read off the creds object. Only the four contract keys hold a value."""

_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(source: str) -> str:
    """Remove JS comments so a commented-out violation is not reported.

    Naive: a `//` inside a string literal takes the rest of that line with it.
    Acceptable here because every check looks for code, and dropping too much
    can only lose a warning, never invent one.
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))


def static_checks(script_path: Path) -> list[str]:
    """Contract violations found without running. Empty list when clean.

    Raises `FileNotFoundError` when the script does not exist -- a missing
    script is not a warning, it is a broken request.
    """
    source = Path(script_path).read_text(encoding="utf-8")
    code = _strip_comments(source)
    warnings: list[str] = []

    if not METADATA_REF.search(code):
        found = _ANY_METADATA.search(code)
        warnings.append(
            "manifest reference: no `onboarding-*.metadata.json` filename in the script"
            + (f"; found {found.group(0)!r} instead" if found else "")
        )
    if not QUESTIONS_REF.search(code):
        found = _ANY_QUESTIONS.search(code)
        warnings.append(
            "manifest reference: no `onboarding-*.questions.json` filename in the script"
            + (f"; found {found.group(0)!r} instead" if found else "")
        )

    if not _RRSTATUS.search(code):
        warnings.append("status output: the script never prints an RRSTATUS line")
    if not _LAST_RUN.search(code):
        warnings.append("status output: the script never writes last-run.json")

    if _HUMAN_BEHAVIOR_FRAME.search(code):
        warnings.append(
            "new HumanBehavior(<FrameLocator>): crashes at runtime, construct it on the Page"
        )

    if _LOGIN_LOCK_CALL.search(code) and not _LOGIN_LOCK_DESTRUCTURED.search(code):
        warnings.append(
            "acquireCarrierLoginLock(): result is not destructured to {release}, "
            "so calling the bare binding throws"
        )

    for m in _HARDCODED_GATE.finditer(code):
        warnings.append(f"hardcoded gate answer: {m.group(0)} discards the client's answer")

    for m in _REQUIRED_FALLBACK.finditer(code):
        warnings.append(
            f"`||` fallback on a required field: {m.group(0).strip()} invents a missing answer"
        )

    for m in _CRED_READ.finditer(code):
        key = m.group(1) or m.group(2)
        if key not in _ALLOWED_CRED_KEYS:
            warnings.append(
                f"credentials: {key} is not one of "
                f"{', '.join(sorted(_ALLOWED_CRED_KEYS))}; it reads as empty"
            )

    return warnings
