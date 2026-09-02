"""Tests for the scraper's contract, extraction, and diff.

Split deliberately: nothing here needs an API key. The LLM enrich step is the
only part that does, and it is not exercised -- the assertion that matters is
that every locator the extractor produces resolves to exactly one node when
replayed on a fresh page, which is the contract the whole downstream pipeline
rests on.
"""

from pathlib import Path

import pytest

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.scraper.diff import diff_pages
from trailblazer.agents.scraper.perceive import DomSnapshotPerceiver
from trailblazer.agents.scraper.scraper import (
    derive_stage_slug,
    finalize,
    restore_measured_locators,
)
from trailblazer.contracts.page_description import Control, Option, PageDescription
from trailblazer.contracts.scraper_result import PerceiveRequest

FIXTURE_URL = (Path(__file__).parent / "fixtures" / "form.html").resolve().as_uri()


# --------------------------------------------------------------------------- #
# Perception against the fixture. Browser, no model.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def payload() -> dict:
    """Perceive the fixture once and reuse the payload across assertions."""
    with BrowserSession(cdp_port=9223) as session:
        page = session.goto(FIXTURE_URL)
        return DomSnapshotPerceiver().perceive(page)


def _by_locator(payload: dict, locator: str) -> dict:
    """The single extracted control at `locator`."""
    return next(c for c in payload["controls"] if c["locator"] == locator)


def test_every_locator_replays_to_exactly_one_node(payload: dict) -> None:
    """The assertion the pipeline rests on: locators survive on a fresh page.

    Replayed in a *separate* browser session, so nothing about the extraction
    run can carry over.
    """
    locators = [c["locator"] for c in payload["controls"]]
    assert locators, "extractor found no controls"

    with BrowserSession(cdp_port=9224) as session:
        page = session.goto(FIXTURE_URL)
        counts = {loc: page.locator(loc).count() for loc in locators}

    assert all(n == 1 for n in counts.values()), f"non-unique on replay: {counts}"


def test_no_locator_is_a_snapshot_ref(payload: dict) -> None:
    """`e12`-style refs die on re-render and are forbidden by the contract."""
    import re

    for control in payload["controls"]:
        assert not re.match(r"^e\d+$", control["locator"])


def test_native_select_carries_its_options(payload: dict) -> None:
    """A native <select> exposes its choices with no interaction."""
    options = _by_locator(payload, "#entityType")["options"]
    labels = [o["label"] for o in options]
    assert "Limited Liability Company" in labels
    assert "Sole Proprietor" in labels
    assert "Corporation" in labels
    # An `<option>` is not clickable: it is set by label on the select itself.
    assert all(o["locator"] is None for o in options)


def test_custom_combobox_has_no_options_and_is_addressable(payload: dict) -> None:
    """A widget whose choices are not in the DOM: null options, still located."""
    widget = _by_locator(payload, '[data-testid="agency-program"]')
    assert widget["options"] is None
    assert widget["unique"] is True


def test_date_input_is_reported_as_date(payload: dict) -> None:
    """The `date` type and its absent option list come from the DOM, not the model."""
    field = _by_locator(payload, "#effectiveDate")
    assert field["inputType"] == "date"
    assert field["options"] is None
    assert field["required"] is True


def test_asterisk_field_has_no_required_attribute(payload: dict) -> None:
    """The extractor reports the attribute honestly; the asterisk is the model's job."""
    fein = _by_locator(payload, "#fein")
    assert fein["required"] is False
    assert "*" in fein["accessibleName"]


def test_next_is_found_and_back_is_absent(payload: dict) -> None:
    """The fixture has a Next button and no Back."""
    assert payload["next"] is not None
    assert payload["back"] is None


def test_hidden_revealed_field_is_extracted_but_marked_invisible(payload: dict) -> None:
    """The Members field exists in the DOM before the gate is answered."""
    members = _by_locator(payload, "#members")
    assert members["visible"] is False


# --------------------------------------------------------------------------- #
# Contract validators. No browser, no model.
# --------------------------------------------------------------------------- #


def _options(*labels: str, locators: dict[str, str] | None = None) -> list[Option]:
    """Choices as the contract holds them: a label, and a locator only if clickable."""
    locators = locators or {}
    return [Option(label=l, locator=locators.get(l)) for l in labels]


def _control(**overrides) -> Control:
    """A valid control, with fields overridden per test."""
    base = dict(
        fieldId="q_001",
        label="Legal Business Name",
        type="text",
        required=False,
        options=None,
        locator="#legalName",
        unique=True,
        revealedBy=None,
        key="",
    )
    return Control(**{**base, **overrides})


def _page(controls: list[Control], **overrides) -> PageDescription:
    """A valid page description wrapping `controls`."""
    base = dict(
        stageId="form_page_1_business_info",
        url="http://localhost:8765/form.html",
        controls=controls,
        next='button:has-text("Next")',
        back=None,
        candidateGates=[],
        blockers=[],
    )
    return PageDescription(**{**base, **overrides})


def test_snapshot_ref_locator_is_rejected() -> None:
    """A leaked accessibility ref must not validate."""
    with pytest.raises(ValueError, match="snapshot ref"):
        _control(locator="e12")


def test_scalar_types_must_have_null_options() -> None:
    """`[]` on a text field would read as 'zero choices' downstream."""
    with pytest.raises(ValueError, match="options=None"):
        _control(type="text", options=[])


def test_candidate_gates_must_name_known_controls() -> None:
    """A gate naming an absent fieldId would misroute Frontier."""
    with pytest.raises(ValueError, match="unknown fieldIds"):
        _page([_control()], candidateGates=["q_099"])


def test_stage_slug_is_stable_across_url_shapes() -> None:
    """The slug is what lets Frontier recognise a page it has walked before."""
    assert derive_stage_slug("https://x.com/work-comp/business-info", "t") == "business_info"
    # "form" is a routing word, not a page name, so the heading wins.
    assert derive_stage_slug("http://localhost:8765/form.html", "Business Info") == "business_info"
    assert derive_stage_slug("https://x.com/app/step/3", "Business Info") == "business_info"


# --------------------------------------------------------------------------- #
# Diff. Pure functions over contract objects -- cannot flake on page timing.
# --------------------------------------------------------------------------- #


def test_first_perceive_is_positive_with_everything_added() -> None:
    """With no prior, every control is new and polarity is `+ve` by definition."""
    page = _page([_control(fieldId="q_001"), _control(fieldId="q_002", locator="#fein")])
    result = diff_pages(page, prior=None)

    assert result.polarity == "+ve"
    assert result.addedControls == ["q_001", "q_002"]
    assert result.removedControls == []


def test_identical_descriptions_are_negative() -> None:
    """A settled page gates replay generation, so this must never be missed."""
    result = diff_pages(_page([_control()]), prior=_page([_control()]))

    assert result.polarity == "-ve"
    assert (result.addedControls, result.removedControls, result.changedControls) == ([], [], [])


def test_an_extra_control_is_added_and_positive() -> None:
    """The reveal case: a new control appears after an assignment."""
    prior = _page([_control()])
    new = _page([_control(), _control(fieldId="q_002", label="Members", locator="#members")])

    result = diff_pages(new, prior=prior)

    assert result.polarity == "+ve"
    assert result.addedControls == ["q_002"]
    assert result.changedControls == []


def test_alignment_is_by_locator_not_field_id() -> None:
    """`fieldId` is a per-page counter and does not survive a re-perceive."""
    prior = _page([_control(fieldId="q_007")])
    new = _page([_control(fieldId="q_001")])

    assert diff_pages(new, prior=prior).polarity == "-ve"


def test_changed_label_at_same_locator_is_a_change() -> None:
    """Same address, different description: the page moved."""
    prior = _page([_control(label="Legal Name")])
    new = _page([_control(label="Legal Business Name")])

    result = diff_pages(new, prior=prior)

    assert result.polarity == "+ve"
    assert result.changedControls == ["q_001"]


def test_single_assignment_populates_revealed_by() -> None:
    """The scraper holds both sides, so it can attribute the reveal."""
    prior = _page([_control()])
    new = _page([_control(), _control(fieldId="q_002", label="Members", locator="#members")])

    result = diff_pages(new, prior=prior, assignment={"q_001": "Limited Liability Company"})

    revealed = result.page.controls[1].revealedBy
    assert revealed is not None
    assert (revealed.fieldId, revealed.equals) == ("q_001", "Limited Liability Company")


def test_ambiguous_assignment_leaves_revealed_by_null() -> None:
    """With several assignments the cause is unknown; a guess would misroute."""
    prior = _page([_control()])
    new = _page([_control(), _control(fieldId="q_002", label="Members", locator="#members")])

    result = diff_pages(new, prior=prior, assignment={"q_001": "llc", "q_003": "CA"})

    assert result.page.controls[1].revealedBy is None


# --------------------------------------------------------------------------- #
# finalize(). Pure function: no browser, no API key.
# --------------------------------------------------------------------------- #


def test_finalize_renumbers_field_ids_from_one() -> None:
    """`fieldId` is a per-page counter assigned in code; the model never sets it."""
    page = _page(
        [
            _control(fieldId="", locator="#a"),
            _control(fieldId="q_099", locator="#b"),
            _control(fieldId="", locator="#c"),
        ]
    )

    finalize(page, page_index=1, url="https://x.com/work-comp/business-info", title="t")

    assert [c.fieldId for c in page.controls] == ["q_001", "q_002", "q_003"]


def test_finalize_builds_stage_id_from_page_index_and_slug() -> None:
    """The number comes from Loop, the slug from the page."""
    page = _page([_control()])

    finalize(page, page_index=3, url="https://x.com/work-comp/business-info", title="t")

    assert page.stageId == "form_page_3_business_info"
    assert page.url == "https://x.com/work-comp/business-info"


def test_finalize_sets_candidate_gates_to_controls_with_options() -> None:
    """The rule is: non-empty `options`, and nothing else. Over-reports on purpose."""
    page = _page(
        [
            _control(fieldId="", locator="#name", type="text", options=None),
            _control(fieldId="", locator="#entity", type="select", options=_options("LLC", "Corp")),
            _control(fieldId="", locator="#agency", type="other", options=None),
            _control(fieldId="", locator="#state", type="select", options=_options("CA", "NY")),
        ]
    )

    finalize(page, page_index=1, url="https://x.com/business-info", title="t")

    # q_002 and q_004 are the two with options.
    assert page.candidateGates == ["q_002", "q_004"]


def test_finalize_leaves_candidate_gates_empty_when_nothing_has_options() -> None:
    """A page of free-text fields branches nowhere."""
    page = _page([_control(fieldId="", type="text", options=None)])

    finalize(page, page_index=1, url="https://x.com/business-info", title="t")

    assert page.candidateGates == []


# --------------------------------------------------------------------------- #
# A1: the measured locator wins over whatever the model returned.
# --------------------------------------------------------------------------- #


def _payload_control(key: str, locator: str, unique: bool = True) -> dict:
    """One entry as the perceiver emits it, with the locator already measured."""
    return {"key": key, "locator": locator, "unique": unique}


def test_mangled_locator_is_replaced_by_the_measured_one() -> None:
    """A model that rewrites a locator must not break the contract.

    The perceiver established `#entityType` with `count() == 1`. The model
    returns something else; the measurement wins.
    """
    described = _page(
        [_control(key="el_0", locator="select.form-control-lg:nth-child(2)", unique=False)]
    )

    restore_measured_locators(described, [_payload_control("el_0", "#entityType")])

    assert described.controls[0].locator == "#entityType"
    assert described.controls[0].unique is True


def test_measured_locators_are_matched_by_key_not_order() -> None:
    """The payload key is the join, so a reordered response still lands correctly."""
    described = _page(
        [
            _control(key="el_1", locator="#wrong-b"),
            _control(key="el_0", locator="#wrong-a"),
        ]
    )

    restore_measured_locators(
        described,
        [_payload_control("el_0", "#legalName"), _payload_control("el_1", "#entityType")],
    )

    assert [c.locator for c in described.controls] == ["#entityType", "#legalName"]


def test_missing_keys_with_unrecognisable_locators_are_not_paired_by_position(caplog) -> None:
    """Dropped `key` plus invented locators leaves nothing to join on.

    Order alone was the old fallback, and it is what mispaired every control:
    index is not evidence. With no key and no locator the model recognisably
    shares with the payload, the honest answer is to restore nothing and warn.
    """
    described = _page([_control(key="", locator="#wrong-a"), _control(key="", locator="#wrong-b")])

    with caplog.at_level("WARNING", logger="trailblazer"):
        restore_measured_locators(
            described,
            [_payload_control("el_0", "#legalName"), _payload_control("el_1", "#entityType")],
        )

    assert [c.locator for c in described.controls] == ["#wrong-a", "#wrong-b"]
    assert "refusing to match by position" in caplog.text


def test_position_is_used_when_the_response_agrees_where_it_overlaps() -> None:
    """Order is trusted only on evidence: a locator the model kept, at the right index."""
    described = _page(
        [
            _control(key="", label="Legal Business Name", locator="#legalName"),
            _control(key="", label="Entity Type", locator="#mangled"),
        ]
    )

    restore_measured_locators(
        described,
        [_payload_control("el_0", "#legalName"), _payload_control("el_1", "#entityType")],
    )

    assert [c.locator for c in described.controls] == ["#legalName", "#entityType"]


def test_non_unique_measurement_is_restored_honestly(caplog) -> None:
    """`unique: false` is a measurement too; a model claiming otherwise is overruled."""
    described = _page([_control(key="el_0", locator="#fabricated", unique=True)])

    with caplog.at_level("WARNING", logger="trailblazer"):
        restore_measured_locators(described, [_payload_control("el_0", "input[name=x]", False)])

    assert described.controls[0].unique is False
    assert "restoring measured locator" in caplog.text


def test_count_mismatch_is_logged(caplog) -> None:
    """Positional matching is only sound on equal-length lists, so say when it is not."""
    described = _page([_control(key="", locator="#wrong")])

    with caplog.at_level("WARNING", logger="trailblazer"):
        restore_measured_locators(
            described,
            [_payload_control("el_0", "#a"), _payload_control("el_1", "#b")],
        )

    assert "model returned 1 controls for 2 payload entries" in caplog.text


def test_reversed_response_with_dropped_keys_never_mispairs() -> None:
    """DEFECT-1 probe: equal lengths + no keys must not become blind positional pairing.

    The model returns the controls in reverse order with `key` dropped. The
    lengths match, so a count check cannot catch it. Positional matching would
    hand every control a different field's locator.
    """
    described = _page(
        [
            _control(key="", label="Entity Type", locator="#entityType"),
            _control(key="", label="Legal Business Name", locator="#legalName"),
        ]
    )

    restore_measured_locators(
        described,
        [_payload_control("el_0", "#legalName"), _payload_control("el_1", "#entityType")],
    )

    by_label = {c.label: c.locator for c in described.controls}
    assert by_label["Legal Business Name"] == "#legalName"
    assert by_label["Entity Type"] == "#entityType"


def test_correct_locators_reordered_with_keys_dropped_are_preserved() -> None:
    """The model copied every locator correctly but reordered them, keyless.

    Pre-A1 code was already right here. Restoration must not make it wrong: a
    permutation of the payload's own locators proves order is not the join, so
    the returned locators stand.
    """
    described = _page(
        [
            _control(key="", label="Entity Type", locator="#entityType"),
            _control(key="", label="Employee Count", locator="#employeeCount"),
            _control(key="", label="Legal Business Name", locator="#legalName"),
        ]
    )

    restore_measured_locators(
        described,
        [
            _payload_control("el_0", "#legalName"),
            _payload_control("el_1", "#entityType"),
            _payload_control("el_2", "#employeeCount"),
        ],
    )

    assert [c.locator for c in described.controls] == [
        "#entityType",
        "#employeeCount",
        "#legalName",
    ]


def test_unmatchable_control_is_left_alone_and_warned(caplog) -> None:
    """No key, no locator hit, no safe position: refuse to guess, and say so."""
    described = _page(
        [_control(key="", locator="#invented-a"), _control(key="", locator="#invented-b")]
    )

    with caplog.at_level("WARNING", logger="trailblazer"):
        restore_measured_locators(described, [_payload_control("el_0", "#legalName")])

    assert [c.locator for c in described.controls] == ["#invented-a", "#invented-b"]
    assert "NOT restored" in caplog.text


def test_no_correction_warning_when_the_model_got_it_right(caplog) -> None:
    """The per-control warning must mean something: only fire on an actual change."""
    described = _page([_control(key="el_0", locator="#legalName", unique=True)])

    with caplog.at_level("WARNING", logger="trailblazer"):
        restore_measured_locators(described, [_payload_control("el_0", "#legalName")])

    assert "restoring measured locator" not in caplog.text


def test_key_is_required_by_the_schema_the_model_sees() -> None:
    """Structured output must fail loudly on a dropped `key`, not fall back to guessing."""
    assert "key" in Control.model_json_schema()["required"]

    with pytest.raises(ValueError, match="key"):
        Control(
            fieldId="q_001",
            label="Legal Business Name",
            type="text",
            required=False,
            options=None,
            locator="#legalName",
            unique=True,
            revealedBy=None,
        )


def test_serialized_control_has_exactly_the_documented_fields() -> None:
    """`scraper_io.txt` fixes Control at eight fields; `key` is transport, not output."""
    assert list(_control(key="el_0").model_dump().keys()) == [
        "fieldId",
        "label",
        "type",
        "required",
        "options",
        "locator",
        "unique",
        "revealedBy",
    ]


# --------------------------------------------------------------------------- #
# perceive() wiring. Real browser and real perceiver; only the model is stubbed.
# --------------------------------------------------------------------------- #


class _StubAgent:
    """Stands in for `create_agent`'s return, echoing a canned PageDescription."""

    def __init__(self, described: PageDescription) -> None:
        self._described = described

    def invoke(self, _payload: dict, config: dict | None = None) -> dict:
        """Mimic the one call `perceive` makes, ignoring messages and callbacks."""
        return {"structured_response": self._described}


def _stub_model(monkeypatch, scraper_mod, described: PageDescription) -> None:
    """Replace the model call, leaving the browser and perceiver real.

    `get_model` goes too: `perceive` builds the model to hand to `create_agent`,
    and that needs an API key, which no test in this file requires.
    """
    monkeypatch.setattr(scraper_mod, "get_model", lambda settings: None)
    monkeypatch.setattr(scraper_mod, "create_agent", lambda **kwargs: _StubAgent(described))


def test_perceive_restores_measured_locators_end_to_end(monkeypatch) -> None:
    """The whole seam: real measurement in, mangled model out, contract held.

    The stub returns the fixture's controls REVERSED with `key` carried through
    and every locator rewritten -- the shape that silently mispaired all eight.
    `key` is the join, so each control must come back with the measured locator
    for the field it actually describes, and none with another field's.
    """
    from trailblazer.agents.scraper import scraper as scraper_mod

    with BrowserSession(cdp_port=9225) as session:
        page = session.goto(FIXTURE_URL)
        measured = DomSnapshotPerceiver().perceive(page)

        mangled = _page(
            [
                _control(
                    key=c["key"],
                    label=c["locator"],  # label doubles as the identity to check
                    locator=f"div.mangled:nth-child({i})",
                    unique=False,
                )
                for i, c in enumerate(reversed(measured["controls"]), start=1)
            ]
        )
        _stub_model(monkeypatch, scraper_mod, mangled)

        result = scraper_mod.perceive(page, PerceiveRequest(job_id="t", page_index=1))

    for control in result.page.controls:
        assert control.locator == control.label, "control holds another field's locator"

    assert {c.locator for c in result.page.controls} == {
        c["locator"] for c in measured["controls"]
    }
    assert result.page.next == measured["next"]


def test_perceive_refuses_to_guess_when_the_response_has_no_join(monkeypatch, caplog) -> None:
    """Keys dropped and locators invented: restore nothing rather than mispair.

    This is the case that produced eight wrong-but-unique locators. Refusing
    leaves the model's own values in place and says so in the log; it must never
    hand a control a different field's address.
    """
    from trailblazer.agents.scraper import scraper as scraper_mod

    with BrowserSession(cdp_port=9227) as session:
        page = session.goto(FIXTURE_URL)
        measured = DomSnapshotPerceiver().perceive(page)

        mangled = _page(
            [
                _control(key="", label=c["locator"], locator=f"div.mangled:nth-child({i})")
                for i, c in enumerate(reversed(measured["controls"]), start=1)
            ]
        )
        _stub_model(monkeypatch, scraper_mod, mangled)

        with caplog.at_level("WARNING", logger="trailblazer"):
            result = scraper_mod.perceive(page, PerceiveRequest(job_id="t", page_index=1))

    measured_locators = {c["locator"] for c in measured["controls"]}
    for control in result.page.controls:
        assert control.locator not in measured_locators, "guessed a measured locator"
    assert "refusing to match by position" in caplog.text


def test_perceive_output_matches_the_documented_contract(monkeypatch) -> None:
    """The serialized endpoint body carries the eight documented Control fields."""
    from trailblazer.agents.scraper import scraper as scraper_mod

    with BrowserSession(cdp_port=9226) as session:
        page = session.goto(FIXTURE_URL)
        measured = DomSnapshotPerceiver().perceive(page)

        described = _page(
            [
                _control(key=c["key"], label=f"F{i}", locator=c["locator"])
                for i, c in enumerate(measured["controls"])
            ]
        )
        _stub_model(monkeypatch, scraper_mod, described)

        result = scraper_mod.perceive(page, PerceiveRequest(job_id="t", page_index=1))

    body = result.model_dump()
    assert list(body["page"]["controls"][0].keys()) == [
        "fieldId",
        "label",
        "type",
        "required",
        "options",
        "locator",
        "unique",
        "revealedBy",
    ]
    assert body["page"]["next"] == 'button:has-text("Next")'


# --------------------------------------------------------------------------- #
# next/back carry the same snapshot-ref guard as Control.locator.
# --------------------------------------------------------------------------- #


def test_snapshot_ref_in_next_is_rejected() -> None:
    """`next` is a locator and dies on re-render just as a control's does."""
    with pytest.raises(ValueError, match="snapshot ref"):
        _page([_control()], next="e12")


def test_snapshot_ref_in_back_is_rejected() -> None:
    """Same guard on the other side."""
    with pytest.raises(ValueError, match="snapshot ref"):
        _page([_control()], back="e7")


# --------------------------------------------------------------------------- #
# Browser session teardown. A2: a raise inside start() must not leak.
# --------------------------------------------------------------------------- #


def test_failed_start_closes_what_it_opened(monkeypatch) -> None:
    """`__enter__` returns `start()`, so a raise means `__exit__` never runs.

    Without the teardown the driver, the Chromium process and the temp profile
    all survive the failure, and the leaked browser then holds the port -- so
    the next run fails on the port check and leaks again.
    """
    session = BrowserSession(cdp_port=9299)
    closed: list[bool] = []

    def fail_after_launch(self: BrowserSession) -> BrowserSession:
        self._profile_dir = "/tmp/does-not-matter"
        raise RuntimeError("Chromium exited before serving CDP")

    monkeypatch.setattr(BrowserSession, "_start", fail_after_launch)
    monkeypatch.setattr(BrowserSession, "close", lambda self: closed.append(True))

    with pytest.raises(RuntimeError, match="Chromium exited"):
        with session:
            pass

    assert closed == [True], "start() did not clean up before re-raising"


def test_goto_before_start_says_what_to_do() -> None:
    """The error names the fix rather than surfacing as an AttributeError later."""
    with pytest.raises(RuntimeError, match="session not started"):
        BrowserSession().goto("https://example.com")
