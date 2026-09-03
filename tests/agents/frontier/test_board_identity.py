"""
Frontier tracks a control by (stage, locator), not by fieldId.

The real Scraper numbers controls q_001, q_002... on every perceive, so the
second page's q_001 is a different control from the first page's, and a control
revealed mid-page renumbers everything after it. Both cases used to make the
board believe a fresh control was one it had already explored.
"""

from trailblazer.agents.form_filler.stub import StubFormFiller
from trailblazer.agents.frontier.board import FrontierBoardState
from trailblazer.agents.frontier.frontier import FrontierAgent
from trailblazer.agents.scraper.stub import StubScraper
from trailblazer.contracts import Control, FillReport, FillFieldAssignment, Option, PageDescription
from trailblazer.loop.orchestrator import Loop


def _text(field_id: str, label: str, locator: str, **kw) -> Control:
    return Control(fieldId=field_id, label=label, type="text", required=True, locator=locator, unique=True, **kw)


def _page(stage: str, *controls: Control, next: str | None = None) -> PageDescription:
    return PageDescription(stageId=stage, url=f"https://x/{stage}", controls=list(controls), next=next)


def test_the_same_field_id_on_two_pages_is_two_controls() -> None:
    """Both pages number from q_001; both fields must be filled."""
    pages = [
        _page("form_page_1_a", _text("q_001", "Name", "#name"), next='button:has-text("Next")'),
        _page("form_page_2_b", _text("q_001", "Phone", "#phone")),
    ]
    frontier = FrontierAgent()
    walk = Loop(StubScraper(pages), frontier, StubFormFiller()).fill_form("job", pages[0])

    assert [(s.action, s.locator) for s in walk.paths[0].steps] == [
        ("type", "#name"),
        ("click", 'button:has-text("Next")'),
        ("type", "#phone"),
    ]
    assert [c.stageId for c in frontier.board.controls] == ["form_page_1_a", "form_page_2_b"]


def test_a_control_renumbered_mid_page_is_still_the_same_control() -> None:
    """A revealed field inserted before Email shifts Email from q_002 to q_003."""
    board = FrontierBoardState()
    first = _page("form_page_1_a", _text("q_001", "Name", "#name"), _text("q_002", "Email", "#email"))
    board.sync_controls(first)
    board.absorb_fill_report(
        FillReport(ok=True, fieldId="q_001", landed=["q_001"]),
        FillFieldAssignment(fieldId="q_001", locator="#name", value="Acme"),
    )

    renumbered = _page(
        "form_page_1_a",
        _text("q_001", "Name", "#name"),
        _text("q_002", "Middle name", "#middle"),
        _text("q_003", "Email", "#email"),
    )
    added = board.sync_controls(renumbered)

    assert [c.locator for c in added] == ["#middle"]
    by_locator = {c.locator: c for c in board.board.controls}
    assert by_locator["#email"].fieldId == "q_003"  # renumbered, not duplicated
    assert by_locator["#name"].explored and not by_locator["#email"].explored
    assert len(board.board.controls) == 3


def test_a_fill_report_is_attributed_by_locator_even_if_the_field_id_moved() -> None:
    """FormFiller answered for the control at #email; the board finds it whatever its number."""
    board = FrontierBoardState()
    board.sync_controls(_page("form_page_1_a", _text("q_002", "Email", "#email")))
    board.absorb_fill_report(
        FillReport(ok=True, fieldId="q_009", landed=["q_009"]),
        FillFieldAssignment(fieldId="q_009", locator="#email", value="a@b.c"),
    )

    assert board.board.controls[0].explored
    assert board.walk_log[0].locator == "#email"


def test_the_same_locator_on_two_stages_is_two_controls() -> None:
    """#password on a second host is a second login field, not the first one again."""
    board = FrontierBoardState()
    board.sync_controls(_page("login_a", _text("q_001", "Password", "#password", credential="password")))
    board.sync_controls(_page("login_b", _text("q_001", "Password", "#password", credential="password")))

    assert len(board.board.controls) == 2
    assert [c.stageId for c in board.board.controls] == ["login_a", "login_b"]


def test_choosers_with_the_same_field_id_on_different_pages_do_not_collide() -> None:
    """Two two-option choosers, both q_001 on their own page: three paths, not two."""
    choose = lambda a, b: [Option(label=a, locator=f"#{a}"), Option(label=b, locator=f"#{b}")]
    pages = [
        _page(
            "form_page_1_a",
            Control(fieldId="q_001", label="Entity", type="select", required=True, locator="#entity", unique=True, options=choose("LLC", "Corp")),
            next='button:has-text("Next")',
        ),
        _page(
            "form_page_2_b",
            Control(fieldId="q_001", label="Coverage", type="select", required=True, locator="#cov", unique=True, options=choose("GL", "BOP")),
        ),
    ]
    walk = Loop(StubScraper(pages), FrontierAgent(), StubFormFiller()).fill_form("job", pages[0])

    assert len(walk.paths) == 3
    for path in walk.paths:
        chosen = [s.locator for s in path.steps if s.action == "choose"]
        assert len(chosen) == 2, path.choices  # exactly one option per chooser on every path
