"""The perceive -> Frontier -> fill cycle in `_walk_page`.

Browser, model and form filler are all replaced: what is under test is the
routing -- that the loop stops when Frontier says the page is done, that it
raises rather than looping forever, that a filler's report is folded back into
the board before the next assignment, and that a Restart renavigates and
replays the prefix before the owed side is taken.
"""

import pytest

from trailblazer.agents.frontier import MAX_RESTARTS, Frontier
from trailblazer.contracts.assignment import FillReport
from trailblazer.contracts.page_description import Control, Option, PageDescription
from trailblazer.contracts.scraper_result import ScraperResult
from trailblazer.loop import orchestrator
from trailblazer.observability.ledger import RunLedger
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


# --------------------------------------------------------------------------- #
# Backtracking
# --------------------------------------------------------------------------- #


class FakeTab:
    """A tab that records renavigations. `_walk_page` needs nothing else of it."""

    def __init__(self) -> None:
        self.visited: list[str] = []

    def goto(self, url: str) -> None:
        self.visited.append(url)


def _gate(field_id: str, options: list[str]) -> Control:
    """A two-option control: a gate by shape."""
    return Control(
        fieldId=field_id,
        key=f"el_{field_id}",
        label="Gate",
        type="toggle",
        required=False,
        options=[Option(label=o, locator=None) for o in options],
        locator=f"#{field_id}",
        unique=True,
        revealedBy=None,
    )


def _page(controls: list[Control]) -> ScraperResult:
    page = PageDescription(
        stageId="form_page_1_business_info",
        url="https://partner.example.com/start",
        controls=controls,
        next=None,
        back=None,
        blockers=[],
    )
    return ScraperResult(
        page=page,
        polarity="+ve",
        addedControls=[c.fieldId for c in controls],
        removedControls=[],
        changedControls=[],
    )


def _recording_fill(performed: list):
    """A filler that succeeds at everything and logs what it was asked to do."""

    def run(tab, assignment, settings, ledger=None):
        performed.append((assignment.fieldId, assignment.value))
        return FillReport(
            fieldId=assignment.fieldId,
            intent=assignment.intent,
            locator=assignment.locator,
            ok=True,
            valueUsed=assignment.value or "x",
        )

    return run


def test_one_two_sided_gate_produces_two_walks(monkeypatch, frontier: Frontier) -> None:
    """Both sides of a gate are walked, the second after a renavigation."""
    performed: list = []
    result = _page([_gate("q_001", ["Yes", "No"])])
    tab = FakeTab()

    monkeypatch.setattr(orchestrator, "fill", _recording_fill(performed))
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: result)

    orchestrator._walk_page(tab, result, frontier, "j1", "objective", Settings())

    assert performed == [("q_001", "Yes"), ("q_001", "No")]
    assert tab.visited == ["https://partner.example.com/start"]
    assert frontier.summary()["walk"] == 2
    assert frontier.summary()["gates"]["q_001"]["remaining"] == []
    assert frontier.page_done()


def test_the_prefix_is_replayed_in_order_before_the_owed_side(
    monkeypatch, frontier: Frontier
) -> None:
    """The owed side is taken against the page the prefix rebuilt, not a dirty one."""
    performed: list = []
    controls = [_control("q_001"), _control("q_002"), _gate("q_003", ["Yes", "No"])]
    result = _page(controls)
    tab = FakeTab()

    monkeypatch.setattr(orchestrator, "fill", _recording_fill(performed))
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: result)

    orchestrator._walk_page(tab, result, frontier, "j1", "objective", Settings())

    assert [f for f, _ in performed] == [
        "q_001", "q_002", "q_003",   # walk 1
        "q_001", "q_002", "q_003",   # walk 2: prefix replayed, then the owed side
    ]
    assert performed[2] == ("q_003", "Yes")
    assert performed[5] == ("q_003", "No")
    assert tab.visited == ["https://partner.example.com/start"]


def test_a_failed_replay_aborts_the_restart_and_marks_the_gate_unexplored(
    monkeypatch, frontier: Frontier
) -> None:
    """Continuing would record answers against a page that never existed."""
    performed: list = []
    controls = [_control("q_001"), _gate("q_002", ["Yes", "No"])]
    result = _page(controls)
    tab = FakeTab()

    # q_001 fills on walk 1 and refuses on the replay: the page is not in the
    # state the walk assumed.
    calls = {"n": 0}

    def flaky(tab_, assignment, settings, ledger=None):
        performed.append((assignment.fieldId, assignment.value))
        refuse = assignment.fieldId == "q_001" and calls["n"] > 0
        if assignment.fieldId == "q_001":
            calls["n"] += 1
        return FillReport(
            fieldId=assignment.fieldId,
            intent=assignment.intent,
            locator=assignment.locator,
            ok=not refuse,
            valueUsed=None if refuse else (assignment.value or "x"),
            blocked={"control": assignment.locator, "whatYouTried": "refused"} if refuse else None,
        )

    monkeypatch.setattr(orchestrator, "fill", flaky)
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: result)

    orchestrator._walk_page(tab, result, frontier, "j1", "objective", Settings())

    summary = frontier.summary()
    assert "q_002" in summary["unexplored"]
    assert "prefix replay failed" in summary["unexplored"]["q_002"]
    # The owed side was never filled against the dirty page.
    assert ("q_002", "No") not in performed
    assert frontier.page_done()


def test_the_restart_cap_stops_the_page_with_the_gate_recorded(
    monkeypatch, frontier: Frontier
) -> None:
    """A gate that never leaves its first side must not restart the page forever."""
    result = _page([_gate("q_001", ["Yes", "No"])])
    tab = FakeTab()

    def stuck_on_yes(tab_, assignment, settings, ledger=None):
        # Whatever side is assigned, the control stays on "Yes": the page owes a
        # side after every restart, which is what the cap exists to bound.
        return FillReport(
            fieldId=assignment.fieldId,
            intent=assignment.intent,
            locator=assignment.locator,
            ok=True,
            valueUsed="Yes",
        )

    monkeypatch.setattr(orchestrator, "fill", stuck_on_yes)
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: result)

    orchestrator._walk_page(tab, result, frontier, "j1", "objective", Settings())

    summary = frontier.summary()
    assert summary["restarts"] == MAX_RESTARTS
    assert len(tab.visited) == MAX_RESTARTS
    assert "restart cap" in summary["unexplored"]["q_001"]
    assert frontier.page_done()


def test_every_restart_reaches_the_ledger(monkeypatch, frontier: Frontier) -> None:
    """A restart costs a renavigation and a replay, so it is accounted for."""
    result = _page([_gate("q_001", ["Yes", "No"])])
    ledger = RunLedger(job_id="j1")

    monkeypatch.setattr(orchestrator, "fill", _recording_fill([]))
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: result)

    orchestrator._walk_page(
        FakeTab(), result, frontier, "j1", "objective", Settings(), ledger
    )

    restarts = [s for s in ledger.steps if s.action == "restart"]
    assert len(restarts) == 1
    assert restarts[0].agent == "frontier"
    assert restarts[0].detail == "q_001"
    assert restarts[0].usd == 0.0


def test_two_independent_gates_give_four_walks_not_a_product(
    monkeypatch, frontier: Frontier
) -> None:
    """Two two-sided gates is 4 walks total, not 4 paths through the rest."""
    performed: list = []
    gates = [_gate("q_001", ["Yes", "No"]), _gate("q_002", ["Owner", "Renter"])]
    result = _page(gates)
    tab = FakeTab()

    monkeypatch.setattr(orchestrator, "fill", _recording_fill(performed))
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: result)

    orchestrator._walk_page(tab, result, frontier, "j1", "objective", Settings())

    assert performed == [
        ("q_001", "Yes"), ("q_002", "Owner"),   # walk 1
        ("q_001", "No"), ("q_002", "Renter"),   # walk 2: q_001's owed side, then q_002's
    ]
    assert frontier.summary()["walk"] == 2
    assert frontier.page_done()


def test_a_fill_on_walk_two_does_not_overwrite_walk_ones_answer(
    monkeypatch, tmp_path, frontier: Frontier
) -> None:
    """Answers assembled across walks are not a path the form ever rendered."""
    from trailblazer.agents.generator import Generator

    controls = [_control("q_001"), _gate("q_002", ["Yes", "No"])]
    result = _page(controls)

    values = iter(["first", "second"])

    def fill_with_distinct_values(tab, assignment, settings, ledger=None):
        return FillReport(
            fieldId=assignment.fieldId,
            intent=assignment.intent,
            locator=assignment.locator,
            ok=True,
            valueUsed=assignment.value or next(values),
        )

    generator = Generator(
        out_dir=tmp_path, carrier="pie", business_type="contractors",
        insurance_type="workers_comp",
    )
    monkeypatch.setattr(orchestrator, "fill", fill_with_distinct_values)
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: result)

    orchestrator._walk_page(
        FakeTab(), result, frontier, "j1", "objective", Settings(), None, generator,
    )

    # Both walks answered q_001, with different values. Publishing walk 1 gives
    # the value that walk actually used, not the last one written.
    generator.publish_walk(1)
    by_id = {q.questionId: q.exampleValue for q in generator.questions_doc.questions}
    assert by_id["q_001"] == "first"
    assert by_id["q_002"] == "Yes"

    generator.publish_walk(2)
    by_id = {q.questionId: q.exampleValue for q in generator.questions_doc.questions}
    assert by_id["q_001"] == "second"
    assert by_id["q_002"] == "No"


def test_publishing_a_walk_that_was_never_walked_raises(tmp_path) -> None:
    """Failures are loud: a missing walk is not silently an empty answer set."""
    from trailblazer.agents.generator import Generator

    generator = Generator(
        out_dir=tmp_path, carrier="pie", business_type="contractors",
        insurance_type="workers_comp",
    )

    with pytest.raises(ValueError, match="no walk 3"):
        generator.publish_walk(3)


def test_a_walked_gate_reaches_branch_exploration(
    monkeypatch, tmp_path, frontier: Frontier
) -> None:
    """The completion assertion reads the artifact, not Frontier's board."""
    from trailblazer.agents.generator import Generator

    # Two plain fields first, so the gate's questionId (q_003) differs from its
    # fieldId: the entry must carry the id the artifacts join on.
    result = _page([_control("f_1"), _control("f_2"), _gate("f_3", ["Yes", "No"])])
    generator = Generator(
        out_dir=tmp_path, carrier="pie", business_type="contractors",
        insurance_type="workers_comp",
    )
    monkeypatch.setattr(orchestrator, "fill", _recording_fill([]))
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: result)

    orchestrator._walk_page(
        FakeTab(), result, frontier, "j1", "objective", Settings(), None, generator,
    )

    exploration = generator.metadata_doc.branchExploration
    assert exploration.gatesWalkedBothSides == ["q_003"]
    assert exploration.unexplored == []


def test_a_capped_gate_reaches_branch_exploration_with_its_reason(
    monkeypatch, tmp_path, frontier: Frontier
) -> None:
    """A gate neither walked nor declared fails the completion assertion."""
    from trailblazer.agents.generator import Generator

    result = _page([_gate("q_001", ["Yes", "No"])])
    generator = Generator(
        out_dir=tmp_path, carrier="pie", business_type="contractors",
        insurance_type="workers_comp",
    )

    def stuck_on_yes(tab_, assignment, settings, ledger=None):
        return FillReport(
            fieldId=assignment.fieldId,
            intent=assignment.intent,
            locator=assignment.locator,
            ok=True,
            valueUsed="Yes",
        )

    monkeypatch.setattr(orchestrator, "fill", stuck_on_yes)
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: result)

    orchestrator._walk_page(
        FakeTab(), result, frontier, "j1", "objective", Settings(), None, generator,
    )

    exploration = generator.metadata_doc.branchExploration
    assert exploration.gatesWalkedBothSides == []
    assert len(exploration.unexplored) == 1
    assert exploration.unexplored[0]["questionId"] == "q_001"
    assert "restart cap" in exploration.unexplored[0]["reason"]


def test_answers_are_filed_per_walk_as_the_loop_appends(
    monkeypatch, tmp_path, frontier: Frontier
) -> None:
    """The Generator is told which walk each fill came from."""
    from trailblazer.agents.generator import Generator

    result = _page([_gate("q_001", ["Yes", "No"])])
    generator = Generator(
        out_dir=tmp_path, carrier="pie", business_type="contractors",
        insurance_type="workers_comp",
    )
    monkeypatch.setattr(orchestrator, "fill", _recording_fill([]))
    monkeypatch.setattr(orchestrator, "perceive", lambda *a, **k: result)

    orchestrator._walk_page(
        FakeTab(), result, frontier, "j1", "objective", Settings(), None, generator,
    )

    assert generator.walks == [1, 2]


def test_the_published_route_is_the_first_that_reached_a_terminal(tmp_path) -> None:
    """A route cut short by a restart is not a path to a terminal."""
    from trailblazer.agents.generator import Generator

    generator = Generator(
        out_dir=tmp_path, carrier="c", business_type="b", insurance_type="i"
    )
    generator._answers = {1: {"q_001": "LLC"}, 2: {"q_001": "Sole"}, 3: {"q_001": "LLC"}}
    generator.record_route_end(1, "form_page_2_details", settled=False)
    generator.record_route_end(2, "quote_page", settled=True)
    generator.record_route_end(3, "quote_page", settled=True)

    assert generator.first_settled_walk() == 2


def test_no_settled_route_leaves_the_choice_to_the_caller(tmp_path) -> None:
    """`None` rather than a plausible-looking route the crawl never finished."""
    from trailblazer.agents.generator import Generator

    generator = Generator(
        out_dir=tmp_path, carrier="c", business_type="b", insurance_type="i"
    )
    generator._answers = {1: {"q_001": "x"}}
    generator.record_route_end(1, "form_page_2_details", settled=False)

    assert generator.first_settled_walk() is None
