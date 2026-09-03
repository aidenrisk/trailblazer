"""The crawl endpoint, with the perceive step patched out.

The browser and the model are both replaced: what is under test is the wiring
-- payload parsing, the URL fallback, the response shape, and the status codes
-- not the crawl itself, which the scraper tests cover.
"""

import pytest
from fastapi.testclient import TestClient

from trailblazer.api import app
from trailblazer.contracts.page_description import Control, PageDescription
from trailblazer.contracts.scraper_result import ScraperResult

client = TestClient(app)

PAYLOAD = {"insuranceTypes": ["workers_comp"], "businessTypes": ["contractors"], "headed": False}


def _fake_result() -> ScraperResult:
    """What the loop would return for one page."""
    page = PageDescription(
        stageId="form_page_1_business_info",
        url="https://partner.example.com/work-comp/business-info",
        controls=[
            Control(
                fieldId="q_001",
                key="el_0",
                label="Agency / Program",
                type="other",
                required=True,
                options=None,
                locator="#agencyProgram",
                unique=True,
                revealedBy=None,
            )
        ],
        next='button:has-text("Next")',
        back=None,
        candidateGates=[],
        blockers=[],
    )
    return ScraperResult(
        page=page, polarity="+ve", addedControls=["q_001"], removedControls=[], changedControls=[]
    )


@pytest.fixture
def crawled(monkeypatch) -> list[dict]:
    """Replace the loop's crawl with a recorder, and return what it was called with."""
    seen: list[dict] = []

    def fake_run_crawl(**kwargs):
        seen.append(kwargs)
        return _fake_result()

    monkeypatch.setattr("trailblazer.api.run_crawl", fake_run_crawl)
    return seen


def test_crawl_returns_the_scraper_result(crawled: list[dict]) -> None:
    """The documented payload plus a temporary `url` gives back a ScraperResult."""
    response = client.post(
        "/v0/carriers/pie/crawl", json={**PAYLOAD, "url": "https://partner.example.com/start"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["polarity"] == "+ve"
    assert body["page"]["stageId"] == "form_page_1_business_info"
    assert body["page"]["controls"][0]["locator"] == "#agencyProgram"


def test_carrier_id_and_payload_reach_the_loop(crawled: list[dict]) -> None:
    """The path parameter and both type lists are passed through, not dropped."""
    client.post(
        "/v0/carriers/pie/crawl", json={**PAYLOAD, "url": "https://partner.example.com/start"}
    )

    call = crawled[0]
    assert call["carrier_id"] == "pie"
    assert call["url"] == "https://partner.example.com/start"
    assert call["insurance_types"] == ["workers_comp"]
    assert call["business_types"] == ["contractors"]
    assert call["headed"] is False


def test_url_falls_back_to_the_carrier_url_setting(monkeypatch, crawled: list[dict]) -> None:
    """With no `url` in the body, the temporary setting supplies it."""
    monkeypatch.setenv("CARRIER_URL", "https://partner.example.com/from-env")

    response = client.post("/v0/carriers/pie/crawl", json=PAYLOAD)

    assert response.status_code == 200
    assert crawled[0]["url"] == "https://partner.example.com/from-env"


def test_missing_url_is_a_400_naming_both_ways_to_supply_it(monkeypatch) -> None:
    """No URL anywhere is bad input, not a server failure."""
    monkeypatch.delenv("CARRIER_URL", raising=False)

    response = client.post("/v0/carriers/pie/crawl", json=PAYLOAD)

    assert response.status_code == 400
    assert "CARRIER_URL" in response.json()["detail"]


def test_malformed_payload_is_a_422() -> None:
    """FastAPI validates the body; a wrong type never reaches the loop."""
    response = client.post("/v0/carriers/pie/crawl", json={"insuranceTypes": "workers_comp"})

    assert response.status_code == 422


def test_a_crawl_failure_is_a_500_with_the_cause_in_detail(monkeypatch) -> None:
    """A missing API key, a dead port, a nav timeout: all surface with their message."""

    def boom(**kwargs):
        raise RuntimeError("OPENROUTER_API_KEY is not set; put it in .env")

    monkeypatch.setattr("trailblazer.api.run_crawl", boom)

    response = client.post(
        "/v0/carriers/pie/crawl", json={**PAYLOAD, "url": "https://partner.example.com/start"}
    )

    assert response.status_code == 500
    assert "OPENROUTER_API_KEY is not set" in response.json()["detail"]
