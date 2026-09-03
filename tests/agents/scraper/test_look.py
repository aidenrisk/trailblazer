"""The real Scraper behind Loop's `look()` contract, with `perceive` patched out.

What is under test is the memory the class supplies -- prior page, page index,
assignment attribution -- and the conversion from the perceive result to the
Diff Loop carries. Perceiving itself is covered by `tests/test_scraper.py`.
"""

import pytest

from trailblazer.agents.scraper import scraper as scraper_module
from trailblazer.agents.scraper.scraper import Scraper
from trailblazer.contracts import (
    Control,
    FillFieldAssignment,
    FillReport,
    Option,
    PageDescription,
    PerceiveRequest,
    ScraperResult,
    SetOptionAssignment,
    SimpleAssignment,
)
from trailblazer.shared.config import Settings

SETTINGS = Settings(_env_file=None)


def _page(stage: str, *controls: Control) -> PageDescription:
    return PageDescription(stageId=stage, url=f"https://x/{stage}", controls=list(controls))


def _c(field_id: str, label: str, locator: str) -> Control:
    return Control(fieldId=field_id, label=label, type="text", required=True, locator=locator, unique=True)


@pytest.fixture
def perceives(monkeypatch):
    """Record every PerceiveRequest and hand back the scripted ScraperResults in order."""
    requests: list[PerceiveRequest] = []
    results: list[ScraperResult] = []

    def fake_perceive(page, request, settings=None):
        requests.append(request)
        return results.pop(0)

    monkeypatch.setattr(scraper_module, "perceive", fake_perceive)
    return requests, results


def test_first_look_has_no_prior_and_converts_added_ids_to_references(perceives) -> None:
    requests, results = perceives
    page = _page("form_page_1_start", _c("q_001", "Name", "#name"))
    result = ScraperResult(page=page, polarity="+ve", addedControls=["q_001"], removedControls=[], changedControls=[])
    results.append(result)

    scraper = Scraper(page=object(), settings=SETTINGS, objective="Describe")
    got, diff = scraper.look("job")

    assert requests[0].prior is None and requests[0].page_index == 1 and requests[0].objective == "Describe"
    assert got is page
    assert diff.polarity == "+ve"
    assert [(r.fieldId, r.label, r.locator) for r in diff.addedControls] == [("q_001", "Name", "#name")]
    # run_crawl reads the perceive result back off the scraper, so it must be kept.
    assert scraper.last_result is result


def test_second_look_passes_the_prior_page_and_resolves_removed_ids_against_it(perceives) -> None:
    requests, results = perceives
    first = _page("s", _c("q_001", "Name", "#name"), _c("q_002", "Gone", "#gone"))
    second = _page("s", _c("q_001", "Name", "#name"))
    results.append(ScraperResult(page=first, polarity="+ve", addedControls=["q_001", "q_002"], removedControls=[], changedControls=[]))
    results.append(ScraperResult(page=second, polarity="+ve", addedControls=[], removedControls=["q_002"], changedControls=[]))

    scraper = Scraper(page=object(), settings=SETTINGS)
    scraper.look("job")
    _, diff = scraper.look("job", "post_fill", SimpleAssignment(type="stop"), FillReport(ok=True))

    assert requests[1].prior is first
    # q_002 is not on the new page, so its reference must come from the prior one.
    assert [(r.fieldId, r.locator) for r in diff.removedControls] == [("q_002", "#gone")]


def test_next_advances_the_page_index_and_back_retreats_but_never_below_one(perceives) -> None:
    requests, results = perceives
    page = _page("s")
    for _ in range(4):
        results.append(ScraperResult(page=page, polarity="-ve", addedControls=[], removedControls=[], changedControls=[]))

    scraper = Scraper(page=object(), settings=SETTINGS)
    scraper.look("job")
    scraper.look("job", "post_fill", SimpleAssignment(type="next"))
    scraper.look("job", "post_fill", SimpleAssignment(type="back"))
    scraper.look("job", "post_fill", SimpleAssignment(type="back"))

    assert [r.page_index for r in requests] == [1, 2, 1, 1]


def test_set_option_and_fills_are_attributed_for_revealed_by(perceives) -> None:
    requests, results = perceives
    page = _page("s")
    for _ in range(4):
        results.append(ScraperResult(page=page, polarity="-ve", addedControls=[], removedControls=[], changedControls=[]))

    scraper = Scraper(page=object(), settings=SETTINGS)
    scraper.look("job")
    scraper.look("job", "post_fill", SetOptionAssignment(fieldId="q_e", option="LLC", locator="#llc", controlLocator="#entity"))
    scraper.look("job", "post_fill", FillFieldAssignment(fieldId="q_n", locator="#name", value="Acme"))
    # A credential fill has no value; the key stands in so a field revealed by it is still attributed.
    scraper.look(
        "job", "post_fill", FillFieldAssignment(fieldId="q_p", locator="#pw", credentialKey="LOGIN_PASSWORD")
    )

    assert [r.assignment for r in requests] == [
        None,
        {"q_e": "LLC"},
        {"q_n": "Acme"},
        {"q_p": "LOGIN_PASSWORD"},
    ]


def test_a_discovered_choosers_chosen_option_beats_the_value_asked_for(perceives) -> None:
    requests, results = perceives
    page = _page("s")
    results.append(ScraperResult(page=page, polarity="-ve", addedControls=[], removedControls=[], changedControls=[]))
    results.append(ScraperResult(page=page, polarity="-ve", addedControls=[], removedControls=[], changedControls=[]))

    scraper = Scraper(page=object(), settings=SETTINGS)
    scraper.look("job")
    scraper.look(
        "job",
        "post_fill",
        FillFieldAssignment(fieldId="q_g", locator="#gender", value="Test Value"),
        FillReport(ok=True, fieldId="q_g", discoveredOptions=[Option(label="Male")], chosenOption="Male"),
    )

    assert requests[1].assignment == {"q_g": "Male"}
