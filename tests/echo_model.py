"""A stand-in for the Scraper's model: echoes the measured payload back as a page.

The real Scraper asks a model for judgment (clean labels, the type enum,
`required` when the attribute is absent, blockers) and keeps everything else
measured. For an offline end-to-end run the judgment is replaced by rules over
the payload, which is enough to drive the chain against a stand-in portal with
no API key: labels come from the accessible name, types from the tag and input
type, options from the select's own list, and `credential` from the measurement.
"""

import json

from pydantic import BaseModel

from trailblazer.contracts import Option

_PLACEHOLDERS = ("select", "choose", "--", "...")


def _type(c: dict) -> str:
    tag, input_type, role = c.get("tag", ""), (c.get("inputType") or "").lower(), (c.get("role") or "").lower()
    if tag == "select":
        return "select"
    if input_type in ("checkbox", "radio") or role == "switch":
        return "toggle"
    if input_type == "date":
        return "date"
    if input_type == "number":
        return "number"
    if role == "combobox" and tag not in ("input", "textarea"):
        return "other"
    return "text"


def _options(c: dict, type_: str) -> list[Option] | None:
    if type_ in ("text", "number", "date") or c.get("options") is None:
        return None
    return [
        Option(label=o["label"], locator=o.get("locator"))
        for o in c["options"]
        if o["label"] and not o["label"].strip().lower().startswith(_PLACEHOLDERS)
    ]


class _EchoAgent:
    def __init__(self, page_model: type[BaseModel], control_model: type[BaseModel]) -> None:
        self.page_model = page_model
        self.control_model = control_model

    def invoke(self, payload: dict, config: dict | None = None) -> dict:
        text = payload["messages"][0]["content"]
        raw = json.loads(text.split("Extractor payload:\n", 1)[1])
        controls = []
        for c in raw["controls"]:
            if not c.get("locator"):
                continue  # unaddressable: nothing downstream could act on it
            type_ = _type(c)
            controls.append(
                self.control_model(
                    fieldId="",
                    key=c["key"],
                    label=c.get("accessibleName") or c.get("labelText") or c.get("name") or c.get("id") or "field",
                    type=type_,
                    required=bool(c.get("required")),
                    options=_options(c, type_),
                    locator=c["locator"],
                    unique=bool(c.get("unique")),
                    revealedBy=None,
                    credential=c.get("credential"),
                )
            )
        page = self.page_model(
            stageId="",
            url=raw["url"],
            controls=controls,
            next=raw.get("next"),
            back=raw.get("back"),
            candidateGates=[],
            blockers=[],
        )
        return {"structured_response": page}


def install_echo_model(monkeypatch) -> None:
    """Replace the Scraper's model call with the echo; the browser and perceiver stay real."""
    from trailblazer.agents.scraper import scraper as scraper_mod

    monkeypatch.setattr(scraper_mod, "get_model", lambda settings: None)
    monkeypatch.setattr(
        scraper_mod,
        "create_agent",
        lambda **kwargs: _EchoAgent(scraper_mod._ModelPage, scraper_mod._ModelControl),
    )
