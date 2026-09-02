"""The crawl endpoint, with the perceive step patched out.

The browser, the model and the credential store are all replaced: what is
under test is the wiring -- payload parsing, the credential lookup, the response
shape, and the status codes -- not the crawl itself, which the scraper tests
cover.
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from trailblazer.api import app
from trailblazer.contracts.page_description import Control, PageDescription
from trailblazer.contracts.scraper_result import ScraperResult
from trailblazer.shared.carrier_creds import CarrierCreds, UnknownCarrierError

client = TestClient(app)

PAYLOAD = {"insuranceTypes": ["workers_comp"], "businessTypes": ["contractors"], "headed": False}
PIE = CarrierCreds(slug="pie", login_url="https://partner.example.com/start", username="u")


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
def known_carrier(monkeypatch) -> None:
    """The credential store knows `pie`."""
    monkeypatch.setattr("trailblazer.api.resolve_carrier_creds", lambda carrier_id: PIE)


@pytest.fixture
def crawled(monkeypatch, known_carrier) -> list[dict]:
    """Replace the loop's crawl with a recorder, and return what it was called with."""
    seen: list[dict] = []

    def fake_run_crawl(**kwargs):
        seen.append(kwargs)
        return _fake_result()

    monkeypatch.setattr("trailblazer.api.run_crawl", fake_run_crawl)
    return seen


def test_crawl_returns_the_scraper_result(crawled: list[dict]) -> None:
    """The documented payload gives back a ScraperResult."""
    response = client.post("/v0/carriers/pie/crawl", json=PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["polarity"] == "+ve"
    assert body["page"]["stageId"] == "form_page_1_business_info"
    assert body["page"]["controls"][0]["locator"] == "#agencyProgram"


def test_carrier_id_and_payload_reach_the_loop(crawled: list[dict]) -> None:
    """The path parameter and both type lists are passed through, not dropped."""
    client.post("/v0/carriers/pie/crawl", json=PAYLOAD)

    call = crawled[0]
    assert call["carrier_id"] == "pie"
    assert call["insurance_types"] == ["workers_comp"]
    assert call["business_types"] == ["contractors"]
    assert call["headed"] is False


def test_url_comes_from_the_carriers_credentials(crawled: list[dict]) -> None:
    """The client never supplies a URL; the carrier's login_url is the start."""
    client.post("/v0/carriers/pie/crawl", json=PAYLOAD)

    assert crawled[0]["url"] == "https://partner.example.com/start"


def test_unknown_carrier_is_a_400(monkeypatch) -> None:
    """No credentials on file is bad input, not a server failure."""

    def unknown(carrier_id):
        raise UnknownCarrierError(f"no credentials on file for carrier {carrier_id!r}")

    monkeypatch.setattr("trailblazer.api.resolve_carrier_creds", unknown)

    response = client.post("/v0/carriers/nobody/crawl", json=PAYLOAD)

    assert response.status_code == 400
    assert "nobody" in response.json()["detail"]


def test_unreachable_credential_store_is_a_503(monkeypatch) -> None:
    """A database that is down is an outage, reported as such rather than as a traceback."""

    def down(carrier_id):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr("trailblazer.api.resolve_carrier_creds", down)

    response = client.post("/v0/carriers/pie/crawl", json=PAYLOAD)

    assert response.status_code == 503
    assert "connection refused" in response.json()["detail"]


def test_malformed_payload_is_a_422(known_carrier) -> None:
    """FastAPI validates the body; a wrong type never reaches the loop."""
    response = client.post("/v0/carriers/pie/crawl", json={"insuranceTypes": "workers_comp"})

    assert response.status_code == 422


def test_a_crawl_failure_is_a_500_with_the_cause_in_detail(monkeypatch, known_carrier) -> None:
    """A missing API key, a dead port, a nav timeout: all surface with their message."""

    def boom(**kwargs):
        raise RuntimeError("OPENROUTER_API_KEY is not set; put it in .env")

    monkeypatch.setattr("trailblazer.api.run_crawl", boom)

    response = client.post("/v0/carriers/pie/crawl", json=PAYLOAD)

    assert response.status_code == 500
    assert "OPENROUTER_API_KEY is not set" in response.json()["detail"]
