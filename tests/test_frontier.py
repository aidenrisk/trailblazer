"""Tests for the board and the assignment order Frontier derives from it.

Fixtures, not a browser: Frontier never touches a page, so every input here is a
PageDescription built in code. What is under test is the walk order and the
gate accounting -- the two things a wrong answer from which silently produces an
incomplete crawl rather than a visible failure.
"""

import pytest

from trailblazer.agents.frontier import MAX_RESTARTS, Frontier, gate_sides
from trailblazer.contracts.assignment import Assignment, FillReport, Restart
from trailblazer.contracts.page_description import (
    Action,
    Control,
    Option,
    PageDescription,
    RevealedBy,
)
from trailblazer.observability.ledger import RunLedger

STAGE = "form_page_1_business_info"


def control(
    field_id: str,
    label: str = "Field",
    type: str = "text",
    options: list[str] | None = None,
    locator: str | None = None,
    option_locators: bool = False,
    disabled: bool = False,
    revealed_by: RevealedBy | None = None,
) -> Control:
    """One control. `options` is given as bare labels; locators are optional."""
    opts = None
    if options is not None:
        opts = [
            Option(label=o, locator=f"#{field_id}_{i}" if option_locators else None)
            for i, o in enumerate(options)
        ]
    return Control(
        fieldId=field_id,
        key=f"el_{field_id}",
        label=label,
        type=type,
        required=False,
        options=opts,
        locator=locator or f"#{field_id}",
        unique=True,
        disabled=disabled,
        revealedBy=revealed_by,
    )


def page(
    controls: list[Control],
    actions: list[Action] | None = None,
    blockers: list[str] | None = None,
    stage_id: str = STAGE,
    next: str | None = None,
) -> PageDescription:
    """One page description. `next` given makes it a page with somewhere to go."""
    return PageDescription(
        stageId=stage_id,
        url="https://partner.example.com/start",
        controls=controls,
        next=next,
        back=None,
        actions=actions or [],
        blockers=blockers or [],
    )


def report(assignment, value: str | None = None) -> FillReport:
    """The FillReport the filler would return for `assignment`, having succeeded."""
    return FillReport(
        fieldId=assignment.fieldId,
        intent=assignment.intent,
        locator=assignment.locator,
        ok=True,
        valueUsed=value if value is not None else assignment.value,
    )


def walk(frontier: Frontier, description: PageDescription, limit: int = 20) -> list:
    """Drive the page to completion, echoing each assignment straight back.

    Stands in for Loop plus the filler: perceive, assign, report, repeat. The
    page never changes, which is the case for a form whose fields reveal nothing.

    A `Restart` is handled the way `orchestrator._restart_walk` handles one --
    open the walk, replay the fills made before the branch point, then take the
    owed side. There is no browser here, so the renavigation is just the same
    description observed again and `prefix` stands in for what Loop holds.
    """
    assignments: list = []
    prefix: list = []
    frontier.observe(description)
    for _ in range(limit):
        decision = frontier.next_assignment()
        if decision is None:
            return assignments

        if isinstance(decision, Restart):
            frontier.open_restart(decision)
            frontier.observe(description)
            replayed = []
            for prior in prefix:
                if prior.fieldId == decision.fieldId:
                    break
                frontier.observe(description, report(prior))
                replayed.append(prior)
            owed = frontier.open_walk(decision.fieldId, decision.side)
            assignments.append(owed)
            frontier.observe(description, report(owed))
            prefix = replayed + [owed]
            continue

        assignments.append(decision)
        prefix.append(decision)
        frontier.observe(description, report(decision))
    raise AssertionError(f"page did not finish in {limit} assignments")


@pytest.fixture
def frontier() -> Frontier:
    return Frontier(business_types=["contractors"], insurance_types=["workers_comp"])


# --------------------------------------------------------------------------- #
# Every field walked at least once
# --------------------------------------------------------------------------- #


def test_three_text_fields_yield_three_assignments_then_done(frontier: Frontier) -> None:
    """A field never touched is a branch never tested (spec §4)."""
    controls = [control(f"q_00{i}", type="text") for i in (1, 2, 3)]

    assignments = walk(frontier, page(controls))

    assert [a.fieldId for a in assignments] == ["q_001", "q_002", "q_003"]
    assert all(a.intent == "fill" for a in assignments)
    assert frontier.page_done()


def test_intent_follows_the_control_shape(frontier: Frontier) -> None:
    """`fill` for scalars, `select` where choices exist, `check` for a bare toggle."""
    controls = [
        control("q_001", type="number"),
        control("q_002", type="date"),
        control("q_003", type="select", options=["A", "B", "C"]),
        control("q_004", type="toggle"),
    ]

    assignments = walk(frontier, page(controls))

    assert [a.intent for a in assignments[:4]] == ["fill", "fill", "select", "check"]


def test_a_choice_control_with_no_options_is_expanded_not_filled(frontier: Frontier) -> None:
    """A combobox mounts its listbox on click, so the set is unreadable until then."""
    frontier.observe(page([control("q_001", type="select", options=None)]))

    assignment = frontier.next_assignment()

    assert assignment.intent == "expand"
    assert assignment.value is None


def test_frontier_never_chooses_a_value_for_a_plain_field(frontier: Frontier) -> None:
    """Picking the value is the filler's judgment (spec §4)."""
    frontier.observe(page([control("q_001", type="text"), control("q_002", type="select",
                                                                  options=["A", "B", "C"])]))

    first = frontier.next_assignment()

    assert first.value is None


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


def test_a_two_option_toggle_is_walked_both_ways_before_done(frontier: Frontier) -> None:
    """Both-ways coverage is blocking, not advisory (spec §4)."""
    gate = control("q_001", type="toggle", options=["Yes", "No"])

    assignments = walk(frontier, page([gate]))

    assert [a.value for a in assignments] == ["Yes", "No"]
    assert frontier.page_done()
    assert frontier.summary()["gates"]["q_001"] == {"walked": ["Yes", "No"], "remaining": []}


def test_a_five_option_select_is_walked_once(frontier: Frontier) -> None:
    """The walk covers branches, not combinations: three or more options is not a gate."""
    wide = control("q_001", type="select", options=["A", "B", "C", "D", "E"])

    assignments = walk(frontier, page([wide]))

    assert len(assignments) == 1
    assert "q_001" not in frontier.summary()["gates"]


def test_two_independent_gates_yield_four_assignments_not_a_product(frontier: Frontier) -> None:
    """Four walks, not 2x2 paths through the rest of the form."""
    gates = [
        control("q_001", type="toggle", options=["Yes", "No"]),
        control("q_002", type="toggle", options=["Owner", "Renter"]),
    ]

    assignments = walk(frontier, page(gates))

    assert len(assignments) == 4
    assert [(a.fieldId, a.value) for a in assignments] == [
        ("q_001", "Yes"),
        ("q_002", "Owner"),
        ("q_001", "No"),
        ("q_002", "Renter"),
    ]


def test_a_bare_toggle_is_walked_checked_then_unchecked(frontier: Frontier) -> None:
    """A checkbox with no options is a gate; its two sides have no option labels."""
    assignments = walk(frontier, page([control("q_001", type="toggle")]))

    assert [(a.intent, a.value) for a in assignments] == [("check", "true"), ("check", "false")]
    assert frontier.summary()["gates"]["q_001"]["walked"] == ["true", "false"]


def test_a_radio_gate_carries_the_chosen_options_own_locator(frontier: Frontier) -> None:
    """Each radio choice is a separate input, so the option's address is passed through."""
    gate = control("q_001", type="select", options=["Yes", "No"], option_locators=True)
    frontier.observe(page([gate]))
    frontier.next_assignment()
    frontier.observe(page([gate]), FillReport(
        fieldId="q_001", intent="select", locator="#q_001", ok=True, valueUsed="Yes"
    ))

    # The owed side arrives through a restart, so it is `open_walk` that builds
    # the assignment rather than `next_assignment`.
    restart = frontier.next_assignment()
    frontier.open_restart(restart)
    frontier.observe(page([gate]))
    second = frontier.open_walk(restart.fieldId, restart.side)

    assert second.value == "No"
    assert second.optionLocator == "#q_001_1"


def test_a_native_select_gate_carries_no_option_locator(frontier: Frontier) -> None:
    """A `<select>`'s choices are set by label against the parent, not clicked."""
    gate = control("q_001", type="select", options=["Yes", "No"])
    frontier.observe(page([gate]))

    assert frontier.next_assignment().optionLocator is None


def test_the_second_side_arrives_as_a_restart_not_an_assignment(frontier: Frontier) -> None:
    """The page still holds the first side, so the owed side needs a reset first."""
    gate = control("q_001", type="toggle", options=["Yes", "No"])
    frontier.observe(page([gate]))
    frontier.observe(page([gate]), report(frontier.next_assignment()))

    decision = frontier.next_assignment()

    assert isinstance(decision, Restart)
    assert (decision.fieldId, decision.side, decision.walk) == ("q_001", "No", 2)


def test_a_restart_opens_the_next_walk_and_clears_the_attempt_record(
    frontier: Frontier,
) -> None:
    """The replay refills the prefix, so a field left attempted is never re-answered."""
    controls = [control("q_001", type="text"), control("q_002", type="toggle",
                                                       options=["Yes", "No"])]
    frontier.observe(page(controls))
    frontier.observe(page(controls), report(frontier.next_assignment()))
    frontier.observe(page(controls), report(frontier.next_assignment()))

    restart = frontier.next_assignment()
    frontier.open_restart(restart)

    assert frontier.walk == 2
    assert frontier.summary()["attempted"] == []
    assert frontier.summary()["gates"]["q_002"]["walked"] == ["Yes"]


def test_a_field_filled_on_walk_two_is_recorded_against_walk_two(frontier: Frontier) -> None:
    """The board says which pass each answer came from, so walks stay separable."""
    gate = control("q_001", type="toggle", options=["Yes", "No"])
    walk(frontier, page([gate]))

    assert frontier.summary()["walkOf"]["q_001"] == 2
    assert frontier.summary()["restarts"] == 1


def test_the_restart_cap_leaves_the_rest_unexplored_with_a_reason(
    frontier: Frontier,
) -> None:
    """A page whose gates each cost a restart must not run forever (MAX_RESTARTS).

    One gate is walked per page here, so each owed side costs its own restart.
    Independent gates sharing a page cost one restart between them, because the
    walk a restart opens takes every owed side -- see the four-walks test.
    """
    gate = control("q_001", type="toggle", options=["Yes", "No"])
    description = page([gate])

    frontier.observe(description)
    restarts = 0
    for _ in range(200):
        decision = frontier.next_assignment()
        if decision is None:
            break
        if isinstance(decision, Restart):
            restarts += 1
            frontier.open_restart(decision)
            frontier.observe(description)
            owed = frontier.open_walk(decision.fieldId, decision.side)
            # The page reports the side it already held: the control did not
            # leave the branch the first side selected, so the gate still owes
            # one and the page restarts again.
            frontier.observe(description, report(owed, value="Yes"))
            continue
        frontier.observe(description, report(decision, value="Yes"))
    else:
        raise AssertionError("page did not finish")

    assert restarts == MAX_RESTARTS
    summary = frontier.summary()
    assert summary["restarts"] == MAX_RESTARTS
    assert summary["unexplored"] == {
        "q_001": f"restart cap {MAX_RESTARTS} reached on {STAGE}"
    }
    # A capped page still finishes: the gate is declared, not silently owed.
    assert frontier.page_done()


def test_an_abandoned_gate_stops_owing_a_side_and_never_restarts_again(
    frontier: Frontier,
) -> None:
    """A failed replay must not restart the page forever on the same gate."""
    gate = control("q_001", type="toggle", options=["Yes", "No"])
    frontier.observe(page([gate]))
    frontier.observe(page([gate]), report(frontier.next_assignment()))
    restart = frontier.next_assignment()
    frontier.open_restart(restart)
    frontier.abandon_gate("q_001", "prefix replay failed at q_000 on walk 2")

    # The field is still owed an answer on the walk the restart opened -- it is
    # the *side* that is abandoned, not the control.
    frontier.observe(page([gate]))
    remainder = walk(frontier, page([gate]))

    assert [a.value for a in remainder] == [None]
    assert frontier.summary()["restarts"] == 1
    assert frontier.summary()["unexplored"] == {
        "q_001": "prefix replay failed at q_000 on walk 2"
    }


@pytest.mark.parametrize(
    "type,options,expected",
    [
        ("toggle", None, ["true", "false"]),
        ("toggle", ["On", "Off"], ["On", "Off"]),
        ("select", ["Yes", "No"], ["Yes", "No"]),
        ("select", ["A", "B", "C"], None),
        ("select", None, None),
        ("text", None, None),
        ("number", None, None),
        ("other", None, None),
    ],
)
def test_gate_shape_not_type_name(type: str, options: list[str] | None, expected) -> None:
    """The gate rule reads shape: a `toggle` always, else exactly two choices."""
    assert gate_sides(control("q_001", type=type, options=options)) == expected


# --------------------------------------------------------------------------- #
# Pages with nothing fillable
# --------------------------------------------------------------------------- #


def test_no_controls_and_a_matching_action_yields_an_advance(frontier: Frontier) -> None:
    """A dashboard's only move is to click one specific thing."""
    actions = [
        Action(label="Manage Policies", href="/policies", locator="#policies", unique=True),
        Action(label="Start Workers Comp Quote", href="/wc/new", locator="#wc", unique=True),
    ]
    frontier.observe(page([], actions=actions))

    assignment = frontier.next_assignment()

    assert assignment.intent == "advance"
    assert assignment.locator == "#wc"
    assert assignment.fieldId is None


def test_the_action_is_matched_on_href_as_well_as_label(frontier: Frontier) -> None:
    """Match is a case-insensitive substring over both fields."""
    actions = [
        Action(label="Start", href="/quote/contractors", locator="#a", unique=True),
        Action(label="Other", href="/quote/restaurants", locator="#b", unique=True),
    ]
    frontier.observe(page([], actions=actions))

    assert frontier.next_assignment().locator == "#a"


def test_a_unique_action_is_preferred_over_a_repeated_one(frontier: Frontier) -> None:
    """Pie's dashboard carries "Get a Quote" twice; only one locator resolves alone."""
    actions = [
        Action(label="Workers Comp Quote", href="", locator="a.cta", unique=False),
        Action(label="Workers Comp Quote", href="", locator="#hero .cta", unique=True),
    ]
    frontier.observe(page([], actions=actions))

    assert frontier.next_assignment().locator == "#hero .cta"


def test_an_action_already_clicked_is_not_re_issued(frontier: Frontier) -> None:
    """A page whose only action has been taken is done, not looping."""
    actions = [Action(label="Workers Comp", href="", locator="#wc", unique=True)]
    description = page([], actions=actions)
    frontier.observe(description)
    first = frontier.next_assignment()
    frontier.observe(description, report(first))

    assert frontier.next_assignment() is None
    assert frontier.page_done()


def test_an_unmatched_action_is_ignored_while_the_page_still_holds_controls(
    frontier: Frontier,
) -> None:
    """On a form page every unmatched action is chrome; clicking one leaves the form."""
    actions = [Action(label="Contact Us", href="/help", locator="#help", unique=True)]
    frontier.observe(page([control("q_001")], actions=actions))

    assert frontier.next_assignment().fieldId == "q_001"


def test_the_action_starting_the_journey_outranks_the_page_it_sits_on() -> None:
    """Pie's dashboard carries a search box, filters and a paginated table.

    Filling first meant typing into the search box and paging the table with its
    "Next" button, never reaching "Get a Quote": 31 perceives on one page.
    """
    started = Frontier(
        business_types=["contractors"],
        insurance_types=["workers_comp"],
        start_text="Get a Quote",
    )
    actions = [
        Action(label="Get a Quote", href="/work-comp/business-info", locator="#quote", unique=True)
    ]
    started.observe(page([control("q_001", label="Search")], actions=actions))

    assignment = started.next_assignment()

    assert assignment.intent == "advance"
    assert assignment.locator == "#quote"


def test_the_start_action_is_chrome_on_every_page_after_the_landing_page() -> None:
    """The nav link that starts an application sits on the form too.

    Honoured there, it reloaded the form empty after a restart remounted a
    nested gate, and the crawl restarted for the same gate four times on a live
    run. Once taken on the landing page, the same action is never taken again.
    """
    started = Frontier(
        business_types=["contractors"], insurance_types=["workers_comp"],
        start_text="Get a Quote",
    )
    quote = Action(label="Get a Quote", href="/work-comp/business-info", locator="#quote", unique=True)

    # The landing page is its own stage, as Pie's dashboard is: the slug comes
    # from the URL path, and /search is not /work-comp/business-info.
    landing = page([control("q_001", label="Search")], actions=[quote], stage_id="form_page_1_search")
    started.observe(landing)
    assert started.next_assignment().intent == "advance"

    form = PageDescription(
        stageId="form_page_1_business_info", url="https://carrier/form",
        controls=[control("q_001", label="FEIN")], actions=[quote],
        next="#next", back=None, blockers=[],
    )
    started.observe(form)

    assignment = started.next_assignment()

    assert assignment.intent == "fill" and assignment.fieldId == "q_001"


def test_a_gate_on_the_landing_page_never_holds_the_flow_open() -> None:
    """A filter toggle on a dashboard is not a branch of the application."""
    started = Frontier(
        business_types=["contractors"], insurance_types=["workers_comp"],
        start_text="Get a Quote",
    )
    quote = Action(label="Get a Quote", href="/work-comp/business-info", locator="#quote", unique=True)
    toggle = Control(
        fieldId="q_004", key="el_4", label="Submitted", type="toggle", required=False,
        options=None, locator="#submitted", unique=True, revealedBy=None,
    )
    started.observe(page([toggle], actions=[quote], stage_id="form_page_1_search"))
    assert started.next_assignment().intent == "advance"          # leaves via the start action

    form = PageDescription(
        stageId="form_page_1_business_info", url="https://carrier/form",
        controls=[control("q_001", label="FEIN")], actions=[],
        next=None, back=None, blockers=[],
    )
    started.observe(form)
    started.observe(form, FillReport(fieldId="q_001", intent="fill", locator="#q_001", ok=True, valueUsed="x"))

    assert started.next_assignment() is None       # not a Restart for the dashboard toggle
    assert started.flow_done()


def test_a_non_gate_choice_is_handed_to_the_filler_with_its_options(frontier: Frontier) -> None:
    """Frontier names a value only for a gate side; five kinds are the filler's to pick from.

    On a live run the select arrived with no value and no options, and the
    filler could only block. The choices travel on the assignment: labels to
    judge by, locators to click.
    """
    kinds = ["Corporation", "Partnership", "Limited Liability Company"]
    entity = Control(
        fieldId="q_006", key="el_6", label="Legal Entity Type", type="other", required=True,
        options=None, locator="#entityType", unique=True, revealedBy=None,
    )
    frontier.observe(page([entity]))
    assert frontier.next_assignment().intent == "expand"
    frontier.observe(
        page([entity]),
        FillReport(
            fieldId="q_006", intent="expand", locator="#entityType", ok=True,
            optionsRevealed=[{"label": k, "locator": f'role=option[name="{k}"] >> visible=true'} for k in kinds],
        ),
    )

    assignment = frontier.next_assignment()

    assert assignment.intent == "select" and assignment.value is None
    assert [o["label"] for o in assignment.options] == kinds
    assert assignment.options[1]["locator"] == 'role=option[name="Partnership"] >> visible=true'
    assert "q_006" not in frontier.summary()["gates"]   # five sides: walked once, not a gate


def test_the_start_control_is_matched_on_its_own_text_not_the_job_types() -> None:
    """`workers_comp` is our identifier; Pie's href says "work comp"."""
    started = Frontier(
        business_types=["contractors"],
        insurance_types=["workers_comp"],
        start_text="Get a Quote",
    )
    quote = Action(label="Get a Quote", href="/work-comp/business-info", locator="#q", unique=True)
    other = Action(label="Appetite Checker", href="/appetite", locator="#a", unique=True)

    assert started._matches_target(quote)
    assert not started._matches_target(other)


# --------------------------------------------------------------------------- #
# Blockers
# --------------------------------------------------------------------------- #


def test_a_blocker_with_no_dismissing_action_does_not_stall_the_page(frontier: Frontier) -> None:
    """Nothing on the page clears it, so the walk proceeds and the blocker is logged."""
    frontier.observe(page([control("q_001")], blockers=["This field is required"]))

    assert frontier.next_assignment().fieldId == "q_001"


# --------------------------------------------------------------------------- #
# Reveals and page boundaries
# --------------------------------------------------------------------------- #


def test_a_revealed_field_is_added_to_the_board_and_walked(frontier: Frontier) -> None:
    """Reveal-on-change: the new field is attributed to the assignment that made it."""
    parent = control("q_001", type="select", options=["LLC", "Sole Proprietor"])
    child = control("q_002", type="text")

    frontier.observe(page([parent]))
    first = frontier.next_assignment()
    frontier.observe(page([parent, child]), report(first), added=["q_002"])

    assert frontier.summary()["revealed"] == {"q_002": "q_001"}
    assert not frontier.page_done()
    assert frontier.next_assignment().fieldId == "q_002"


def test_a_new_stage_id_retires_the_previous_board(frontier: Frontier) -> None:
    """A new stage opens its own board. Presence and attempted fills do not carry over."""
    walk(frontier, page([control("q_001", type="text")]))

    frontier.observe(page([control("q_001", type="text")], stage_id="form_page_2_locations"))

    assert frontier.summary()["stageId"] == "form_page_2_locations"
    assert frontier.summary()["attempted"] == []
    assert frontier.next_assignment().fieldId == "q_001"


def test_a_failed_report_still_counts_the_field_as_attempted(frontier: Frontier) -> None:
    """A blocked field is a stop condition, not a retry (spec §5)."""
    description = page([control("q_001")])
    frontier.observe(description)
    first = frontier.next_assignment()
    frontier.observe(description, FillReport(
        fieldId="q_001", intent="fill", locator="#q_001", ok=False,
        blocked={"control": "q_001", "whatYouTried": "typed 5"},
    ))

    assert frontier.next_assignment() is None


def test_next_assignment_before_observe_raises(frontier: Frontier) -> None:
    """Failures are loud: there is no board to read yet."""
    with pytest.raises(RuntimeError, match="before observe"):
        frontier.next_assignment()


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


def test_every_step_is_recorded_against_the_ledger_at_zero_cost() -> None:
    """Frontier is deterministic bookkeeping, so every step costs nothing."""
    ledger = RunLedger(job_id="j1")
    frontier = Frontier(["contractors"], ["workers_comp"], ledger=ledger)

    walk(frontier, page([control("q_001"), control("q_002")]))

    agent = ledger.by_agent()["frontier"]
    assert agent["usd"] == 0.0
    assert agent["unpriced"] == 0
    assert {s.action for s in ledger.steps} == {"observe", "assign", "done"}


def test_a_disabled_control_is_never_assigned() -> None:
    """Pie's "Agency / Program" is disabled readonly: a click on it only times out."""
    frontier = Frontier(business_types=["contractors"], insurance_types=["workers_comp"])

    frontier.observe(
        page([control("q_001", type="other", disabled=True), control("q_002")]),
        None,
        ["q_001", "q_002"],
    )

    assert frontier.summary()["unattempted"] == ["q_002"]
    assert frontier.next_assignment().fieldId == "q_002"


def test_a_disabled_two_option_control_is_not_a_gate() -> None:
    """A gate that cannot be set has no sides to walk."""
    frontier = Frontier(business_types=["contractors"], insurance_types=["workers_comp"])

    frontier.observe(
        page([control("q_001", type="toggle", options=["Yes", "No"], disabled=True)]),
        None,
        ["q_001"],
    )

    assert frontier.summary()["gates"] == {}
    assert frontier.page_done()


def test_a_nested_gate_owes_its_side_from_off_the_page(frontier: Frontier) -> None:
    """A gate revealed by another's branch is not assignable once that branch is left."""
    parent = control("q_001", type="select", options=["Yes", "No"])
    child = control(
        "q_002",
        type="select",
        options=["A", "B"],
        revealed_by=RevealedBy(fieldId="q_001", equals="Yes"),
    )

    frontier.observe(page([parent, child]))
    frontier.board.gates["q_002"].take("A")
    frontier.board.record_fill("q_002")
    frontier.observe(page([parent]))

    assert frontier.board.remaining_absent() == ["q_002"]
    assert "q_002" not in frontier.board.unattempted()
    assert "q_002" not in frontier.board.half_walked()


def test_an_absent_nested_gate_restarts_to_the_branch_that_reveals_it(
    frontier: Frontier,
) -> None:
    """The restart targets the parent's revealing side, not the unreachable child."""
    parent = control("q_001", type="select", options=["Yes", "No"])
    child = control(
        "q_002",
        type="select",
        options=["A", "B"],
        revealed_by=RevealedBy(fieldId="q_001", equals="Yes"),
    )

    frontier.observe(page([parent, child]))
    frontier.board.gates["q_001"].take("Yes")
    frontier.board.gates["q_001"].take("No")
    frontier.board.record_fill("q_001")
    frontier.board.gates["q_002"].take("A")
    frontier.board.record_fill("q_002")
    frontier.observe(page([parent]))

    decision = frontier.next_assignment()
    assert isinstance(decision, Restart)
    assert (decision.fieldId, decision.side) == ("q_001", "Yes")


def test_an_absent_gate_with_no_reachable_parent_is_declared_unexplored(
    frontier: Frontier,
) -> None:
    """No branch point means no page state in which the owed side could be taken."""
    orphan = control("q_001", type="select", options=["A", "B"])

    frontier.observe(page([orphan]))
    frontier.board.gates["q_001"].take("A")
    frontier.board.record_fill("q_001")
    frontier.observe(page([]))

    assert frontier.next_assignment() is None
    assert "q_001" in frontier.summary()["unexplored"]


def test_a_chained_nested_gate_restarts_to_the_deepest_reachable_ancestor(
    frontier: Frontier,
) -> None:
    """q_003 under q_002 under q_001: with both ancestors absent, q_001 is set first."""
    g1 = control("q_001", type="select", options=["Yes", "No"])
    g2 = control(
        "q_002",
        type="select",
        options=["Yes", "No"],
        revealed_by=RevealedBy(fieldId="q_001", equals="Yes"),
    )
    g3 = control(
        "q_003",
        type="select",
        options=["A", "B"],
        revealed_by=RevealedBy(fieldId="q_002", equals="Yes"),
    )

    frontier.observe(page([g1, g2, g3]))
    for field_id, sides in (("q_001", ["Yes", "No"]), ("q_002", ["Yes", "No"])):
        for side in sides:
            frontier.board.gates[field_id].take(side)
        frontier.board.record_fill(field_id)
    frontier.board.gates["q_003"].take("A")
    frontier.board.record_fill("q_003")
    frontier.observe(page([g1]))

    decision = frontier.next_assignment()
    assert isinstance(decision, Restart)
    assert (decision.fieldId, decision.side) == ("q_001", "Yes")

    frontier.open_restart(decision)
    # Both ancestors are pinned, so neither is set to its owed side on this
    # walk and q_003 stays reachable.
    assert frontier.board.pinned == {"q_001": "Yes", "q_002": "Yes"}


def test_a_pinned_ancestor_is_assigned_its_revealing_side_not_its_owed_one(
    frontier: Frontier,
) -> None:
    """Taking the owed side of an intermediate gate would unmount the target."""
    g1 = control("q_001", type="select", options=["Yes", "No"])
    g2 = control(
        "q_002",
        type="select",
        options=["Yes", "No"],
        revealed_by=RevealedBy(fieldId="q_001", equals="Yes"),
    )

    frontier.observe(page([g1, g2]))
    frontier.board.pinned["q_002"] = "Yes"
    frontier.board.gates["q_002"].take("Yes")
    frontier.board.record_fill("q_001")

    decision = frontier.next_assignment()
    assert isinstance(decision, Assignment)
    assert (decision.fieldId, decision.value) == ("q_002", "Yes")
    assert frontier.board.gates["q_002"].remaining == ["No"]


def test_a_route_advances_to_the_end_before_a_gate_is_revisited(
    frontier: Frontier,
) -> None:
    """A gate decides what later pages render, so the route runs on before restarting."""
    gate = control("q_001", type="select", options=["LLC", "Sole"])
    described = page([gate])
    described = described.model_copy(update={"next": "#next"})

    frontier.observe(described)
    first = frontier.next_assignment()
    assert isinstance(first, Assignment)
    assert (first.fieldId, first.value) == ("q_001", "LLC")

    frontier.observe(
        described,
        FillReport(fieldId="q_001", intent="select", locator="#q_001", ok=True, valueUsed="LLC"),
    )
    # The gate still owes "Sole", but the page can be advanced, so it is.
    second = frontier.next_assignment()
    assert isinstance(second, Assignment)
    assert second.intent == "advance"


def test_a_gate_on_an_earlier_page_restarts_the_flow(frontier: Frontier) -> None:
    """The route ended, and page one's owed side is what makes the next route differ."""
    gate = control("q_001", type="select", options=["LLC", "Sole"])
    first_page = page([gate]).model_copy(update={"next": "#next"})
    second_page = page([control("q_001")], stage_id="form_page_2_details")

    frontier.observe(first_page)
    frontier.next_assignment()
    frontier.observe(
        first_page,
        FillReport(fieldId="q_001", intent="select", locator="#q_001", ok=True, valueUsed="LLC"),
    )
    frontier.next_assignment()
    frontier.observe(
        first_page,
        FillReport(fieldId=None, intent="advance", locator="#next", ok=True),
    )

    # On page two, with nothing left to do there and no way forward.
    frontier.observe(second_page)
    frontier.next_assignment()
    frontier.observe(
        second_page,
        FillReport(fieldId="q_001", intent="fill", locator="#q_001", ok=True, valueUsed="x"),
    )

    decision = frontier.next_assignment()
    assert isinstance(decision, Restart)
    assert (decision.stageId, decision.fieldId, decision.side) == (
        "form_page_1_business_info",
        "q_001",
        "Sole",
    )


def test_a_boards_gates_survive_leaving_its_page(frontier: Frontier) -> None:
    """A gate on page one still owes a side once page two is reached."""
    gate = control("q_001", type="select", options=["LLC", "Sole"])
    first_page = page([gate]).model_copy(update={"next": "#next"})
    second_page = page([control("q_001")], stage_id="form_page_2_details")

    frontier.observe(first_page)
    frontier.board.gates["q_001"].take("LLC")
    frontier.observe(second_page)

    assert frontier.board.stage_id == "form_page_2_details"
    kept = frontier.boards["form_page_1_business_info"]
    assert kept.gates["q_001"].remaining == ["Sole"]


def test_the_walk_id_is_flow_wide_not_per_page(frontier: Frontier) -> None:
    """One walk is one route, so answers filed under it rendered together."""
    gate = control("q_001", type="select", options=["LLC", "Sole"])
    first_page = page([gate]).model_copy(update={"next": "#next"})
    second_page = page([control("q_001")], stage_id="form_page_2_details")

    frontier.observe(first_page)
    assert frontier.walk == 1
    frontier.observe(second_page)
    assert frontier.walk == 1

    restart = Restart(
        fieldId="q_001", side="Sole", walk=2, stageId="form_page_1_business_info"
    )
    assert frontier.open_restart(restart) == 2
    assert frontier.walk == 2


# --------------------------------------------------------------------------- #
# One complete path before any branch
# --------------------------------------------------------------------------- #


def test_a_gate_is_not_restarted_for_while_the_flow_has_pages_ahead(
    frontier: Frontier,
) -> None:
    """A gate's side decides what later pages render, so branching from the
    middle leaves them described under whichever side was set last."""
    gate = control("q_001", type="select", options=["Yes", "No"])

    # A page with somewhere to go: every field done, and Next not yet pressed.
    frontier.observe(page([gate], next='button:has-text("Next")'))
    assignment = frontier.next_assignment()
    frontier.observe(
        page([gate], next='button:has-text("Next")'), report(assignment, "Yes")
    )

    decision = frontier.next_assignment()

    assert frontier.reached_end is False
    assert not isinstance(decision, Restart)
    assert decision.intent == "advance"


def test_the_owed_side_is_restarted_for_once_a_walk_reaches_the_end(
    frontier: Frontier,
) -> None:
    """The flow's end is a completed page with no forward control: the crawl
    stops before a form's submit, so no Next there means arrival."""
    gate = control("q_001", type="select", options=["Yes", "No"])

    frontier.observe(page([gate]))
    assignment = frontier.next_assignment()
    frontier.observe(page([gate]), report(assignment, "Yes"))
    frontier.next_assignment()  # sees the completed page with no next

    assert frontier.reached_end is True

    decision = frontier.next_assignment()

    assert isinstance(decision, Restart)
    assert (decision.fieldId, decision.side) == ("q_001", "No")


def test_an_earlier_gate_is_not_restarted_for_from_the_middle_of_the_flow(
    frontier: Frontier,
) -> None:
    """The live failure: page two's assignable fields ran out while nine unnamed
    controls sat unassigned, and the flow was re-entered for a page-one gate
    with two pages never walked."""
    gate = control("q_009", type="select", options=["Yes", "No"])
    first = page([gate], stage_id="form_page_1_business_info", next='button:has-text("Next")')

    frontier.observe(first)
    taken = frontier.next_assignment()
    frontier.observe(first, report(taken, "Yes"), None, "form_page_1_business_info")
    forward = frontier.next_assignment()
    frontier.observe(first, report(forward), None, "form_page_1_business_info")

    # Page two as Pie renders it: controls the extractor could not address, so
    # none is assignable, and no forward control until its real fields are set.
    second = page(
        [control(f"q_{i:03d}", locator="") for i in range(1, 10)],
        stage_id="form_page_1_workforce_details",
    )
    frontier.observe(second, None, None, None)

    decision = frontier.next_assignment()

    assert frontier.reached_end is False
    assert not isinstance(decision, Restart)
