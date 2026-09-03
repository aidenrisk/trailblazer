"""The `canonical` fact name: look it up before minting a new one.

`canonical` is the chat's identity for a fact across every carrier. A new name
for a fact that already has one makes the chat ask the user for it twice, so
reuse is the default and minting is the exception.

The real lookup reads `question_catalog.canonical_key` across all tiers
(arch doc 3.3). There is no database in this repo, so the vocabulary is a local
JSON file and every minted name is recorded separately -- those are the
`canonical_aliases` candidate rows the persist step will write.
"""

import json
import re
from pathlib import Path

_VOCAB_FILE = Path(__file__).with_name("canonicals.json")

# Trailing punctuation and the required-field asterisk carriers append to labels.
_TRIM = re.compile(r"[\s*:?.]+$")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _normalize(label: str) -> str:
    """Lowercase a label and collapse punctuation to single spaces, for matching."""
    return _NON_WORD.sub(" ", _TRIM.sub("", label).lower()).strip()


def load_vocabulary(path: Path | None = None) -> dict[str, list[str]]:
    """Read the canonical vocabulary: key -> the label phrases that identify it.

    Keys beginning with `_` are documentation, not vocabulary.
    """
    raw = json.loads((path or _VOCAB_FILE).read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def mint(label: str) -> str:
    """Derive a snake_case fact name from a label, for a fact with no existing name.

    Deterministic, so the same label mints the same name on a re-crawl rather
    than a second alias for one fact.
    """
    name = _NON_WORD.sub("_", _normalize(label)).strip("_")
    return name or "unnamed_field"


class CanonicalResolver:
    """Maps a question label to a canonical fact name, preferring existing ones.

    Holds the names it had to mint so they can be written to `canonical_aliases`
    as candidates rather than silently entering the vocabulary as though they
    were already agreed.
    """

    def __init__(self, vocabulary: dict[str, list[str]] | None = None) -> None:
        self._vocabulary = load_vocabulary() if vocabulary is None else vocabulary
        # Longest phrase first: "legal business name" must win over "legal name"
        # when both match, or the more specific fact collapses into the vaguer one.
        self._phrases: list[tuple[str, str]] = sorted(
            ((phrase, key) for key, phrases in self._vocabulary.items() for phrase in phrases),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )
        self.minted: dict[str, str] = {}
        """Minted name -> the label it came from. The `canonical_aliases` candidates."""

    def resolve(self, label: str) -> str:
        """Return the canonical name for `label`, reusing an existing one where it matches."""
        normalized = _normalize(label)
        if not normalized:
            return "unnamed_field"
        for phrase, key in self._phrases:
            if phrase in normalized:
                return key
        name = mint(label)
        self.minted.setdefault(name, label)
        return name
