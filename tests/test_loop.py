"""The perceive -> Frontier -> fill cycle in `_walk_page`.

Browser, model and form filler are all replaced: what is under test is the
routing -- that the loop stops when Frontier says the page is done, that it
raises rather than looping forever, and that a filler's report is folded back
into the board before the next assignment.
"""

import pytest

from trailblazer.agents.frontier import Frontier
from trailblazer.contracts.assignment import FillReport
from trailblazer.contracts.page_description import Control, PageDescription
from trailblazer.contracts.scraper_result import ScraperResult
from trailblazer.loop import orchestrator
from trailblazer.shared.config import Settings


def _control(field_id: str, type: str = "text") -> Control:
    return Control(
        fieldId=field_id,
        key=f"el_{field_id}",
        label="Field",
        type=type,
        required=False,
        options=None,
        locator=f"#{field_id}",
        unique=True,
        revealedBy=None,
    )


def _result(field_ids: list[str]) -> ScraperResult:
    page = PageDescription(
        stageId="form_page_1_business_info",
        url="https://partner.example.com/start",
        controls=[_control(f) for f in field_ids],
        next=None,
        back=None,
        blockers=[],
    )
    return ScraperResult(
        page=page, polarity="+ve", addedControls=field_ids, removedControls=[], changedControls=[]
    )


@pytest.fixture
def frontier() -> Frontier:
    return Frontier(business_types=["contractors"], insurance_types=["workers_comp"])


def test_a_blocked_report_still_marks_the_field_attempted(
    monkeypatch, frontier: Frontier
) -> None:
    """A field the filler could not fill must not be re-issued forever."""

    def refuse(tab, assignment, settings, ledger=None):
        return FillReport(
            fieldId=assignment.fieldId,
            intent=assignment.intent,
            locator=assignment.locator,
            ok=False,
            blocked={"control": assignment.locator, "whatYouTried": "refused"},
        )

    monkeypatch.setattr(orchestrator, "fill", refuse)
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: _result(["q_001"]))

    orchestrator._walk_page(None, _result(["q_001"]), frontier, "j1", "objective", Settings())

    assert frontier.page_done()


def test_the_walk_returns_when_frontier_reports_the_page_done(
    monkeypatch, frontier: Frontier
) -> None:
    """Every field attempted and no gate half-walked ends the page."""
    calls: list = []

    def fake_fill(tab, assignment, settings, ledger=None):
        calls.append(assignment.fieldId)
        return FillReport(
            fieldId=assignment.fieldId,
            intent=assignment.intent,
            locator=assignment.locator,
            ok=True,
            valueUsed="x",
        )

    monkeypatch.setattr(orchestrator, "fill", fake_fill)
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: _result(["q_001", "q_002"]))

    result = orchestrator._walk_page(
        None, _result(["q_001", "q_002"]), frontier, "j1", "objective", Settings()
    )

    assert calls == ["q_001", "q_002"]
    assert result.page.stageId == "form_page_1_business_info"
    assert frontier.page_done()


def test_a_page_that_never_finishes_raises_rather_than_looping(
    monkeypatch, frontier: Frontier
) -> None:
    """Failures are loud: a control re-added under a new fieldId every perceive."""
    counter = iter(range(1, 1000))

    def fake_fill(tab, assignment, settings, ledger=None):
        return FillReport(
            fieldId=assignment.fieldId, intent=assignment.intent,
            locator=assignment.locator, ok=True, valueUsed="x",
        )

    monkeypatch.setattr(orchestrator, "fill", fake_fill)
    monkeypatch.setattr(
        orchestrator, "perceive", lambda *a, **k: _result([f"q_{next(counter):03d}"])
    )

    with pytest.raises(RuntimeError, match="did not finish"):
        orchestrator._walk_page(
            None, _result(["q_000"]), frontier, "j1", "objective", Settings()
        )


def test_every_fill_reaches_the_generator(monkeypatch, tmp_path, frontier: Frontier) -> None:
    """A fill recorded in no artifact is a branch the replay script cannot take."""
    from trailblazer.agents.generator import Generator

    appended: list = []

    def fake_fill(tab, assignment, settings, ledger=None):
        return FillReport(
            fieldId=assignment.fieldId,
            intent=assignment.intent,
            locator=assignment.locator,
            ok=True,
            valueUsed="x",
        )

    generator = Generator(
        out_dir=tmp_path, carrier="pie", business_type="contractors",
        insurance_type="workers_comp",
    )
    monkeypatch.setattr(generator, "append", lambda req, ledger=None: appended.append(req))
    monkeypatch.setattr(orchestrator, "fill", fake_fill)
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: _result(["q_001", "q_002"]))

    orchestrator._walk_page(
        None, _result(["q_001", "q_002"]), frontier, "j1", "objective",
        Settings(), None, generator,
    )

    assert [r.report.fieldId for r in appended] == ["q_001", "q_002"]
    assert appended[0].control_label == "Field"
    assert appended[0].carrier == "pie"
