"""Generator tests: the pair joins, ids are stable, and the three artifacts agree.

Fixture-based. Frontier and the form filler do not exist on this branch, so
`GenerationRequest` objects are constructed directly rather than produced by a
walk.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from trailblazer.agents.generator import ArtifactMismatch, CredentialLeak, Generator
from trailblazer.agents.generator.canonical import CanonicalResolver
from trailblazer.agents.generator.reconcile import (
    catalog_type,
    clean_options,
    merge_captures,
    strip_merge_fields,
)
from trailblazer.contracts.assignment import FillReport
from trailblazer.contracts.generation import GenerationRequest
from trailblazer.contracts.page_description import Control, Option, PageDescription, RevealedBy
from trailblazer.observability.ledger import RunLedger

CARRIER, BIZ, INS = "pie", "contractors", "workers_comp"


def control(
    field_id: str = "q_001",
    label: str = "Legal Business Name",
    type_: str = "text",
    required: bool = True,
    options: list[Option] | None = None,
    locator: str = "#legalName",
    revealed_by: RevealedBy | None = None,
) -> Control:
    """One control, with the boilerplate the contract requires filled in."""
    return Control(
        fieldId=field_id,
        key=f"el_{field_id}",
        label=label,
        type=type_,
        required=required,
        options=options,
        locator=locator,
        unique=True,
        revealedBy=revealed_by,
    )


def page(stage_id: str, controls: list[Control], url: str = "https://carrier/1") -> PageDescription:
    return PageDescription(
        stageId=stage_id, url=url, controls=controls, next="#next", back=None, blockers=[]
    )


def request(
    pg: PageDescription,
    report: FillReport,
    control_label: str | None = None,
) -> GenerationRequest:
    return GenerationRequest(
        job_id="job-1",
        carrier=CARRIER,
        businessType=BIZ,
        insuranceType=INS,
        page=pg,
        report=report,
        control_label=control_label,
    )


def fill(field_id: str, locator: str, value: str, intent: str = "fill", **kw) -> FillReport:
    return FillReport(
        fieldId=field_id, intent=intent, locator=locator, ok=True, valueUsed=value, **kw
    )


@pytest.fixture
def gen(tmp_path: Path) -> Generator:
    return Generator(tmp_path, CARRIER, BIZ, INS)


# -- questionId allocation -------------------------------------------------


def test_question_ids_are_monotonic_across_two_pages(gen: Generator) -> None:
    """Allocation runs across the whole flow, not per page."""
    p1 = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p1, fill("q_001", "#legalName", "Acme LLC")))

    p2 = page("form_page_2_payroll", [control("q_001", "Annual Payroll", locator="#payroll")], url="https://carrier/2")
    gen.append(request(p2, fill("q_001", "#payroll", "250000")))

    assert gen.state().questionIds == ["q_001", "q_002"]


def test_a_reused_field_id_on_page_two_gets_a_new_question_id(gen: Generator) -> None:
    """`Control.fieldId` is a per-page counter, so it is not the join key.

    Two different facts both arriving as `q_001` must not collapse into one
    question, which is what using fieldId as identity would do.
    """
    p1 = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p1, fill("q_001", "#legalName", "Acme LLC")))

    p2 = page("form_page_2_payroll", [control("q_001", "Annual Payroll", locator="#payroll")], url="https://carrier/2")
    gen.append(request(p2, fill("q_001", "#payroll", "250000")))

    questions = gen.questions_doc.questions
    assert [q.questionId for q in questions] == ["q_001", "q_002"]
    assert [q.label for q in questions] == ["Legal Business Name", "Annual Payroll"]


def test_a_question_id_is_stable_when_a_field_is_captured_twice(gen: Generator) -> None:
    """A second capture of one fact reconciles into its entry, never a duplicate."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))
    gen.append(request(p, fill("q_001", "#legalName", "Acme Holdings LLC")))

    assert gen.state().questionIds == ["q_001"]
    assert gen.questions_doc.questions[0].exampleValue == "Acme Holdings LLC"


# -- the pair --------------------------------------------------------------


def test_every_metadata_field_has_a_question_and_the_reverse(gen: Generator) -> None:
    """Neither file is complete alone; the pair joins on questionId."""
    p = page(
        "form_page_1_business",
        [
            control("q_001", "Legal Business Name"),
            control("q_002", "ZIP Code", locator="#zip"),
        ],
    )
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))
    gen.append(request(p, fill("q_002", "#zip", "94107")))

    question_ids = {q.questionId for q in gen.questions_doc.questions}
    field_ids = {f.questionId for s in gen.metadata_doc.stages for f in s.fields}
    assert question_ids == field_ids == {"q_001", "q_002"}


def test_metadata_holds_no_semantic_keys(gen: Generator) -> None:
    """Label, type, required, option text and exampleValue live in questions only."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))

    written = json.loads(gen.metadata_path.read_text())
    field = written["stages"][0]["fields"][0]
    assert set(field) == {"questionId", "selector"}


def test_stage_counts_agree_across_the_three(gen: Generator) -> None:
    """A stage in the metadata and not in the script is the gate-in-neither defect."""
    p1 = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p1, fill("q_001", "#legalName", "Acme LLC")))
    p2 = page("form_page_2_payroll", [control("q_001", "Annual Payroll", locator="#payroll")], url="https://carrier/2")
    gen.append(request(p2, fill("q_001", "#payroll", "250000")))

    script = gen.script_path.read_text()
    script_stages = re.findall(r"// --- stage: (.+?) ---", script)
    assert script_stages == gen.state().stages == [
        "form_page_1_business",
        "form_page_2_payroll",
    ]


def test_a_stage_desynchronised_after_the_fact_is_caught(gen: Generator) -> None:
    """The check is a real assertion, not a comment claiming one."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))

    # Simulate the RoadRunner failure: a stage recorded in the metadata whose
    # script block was dropped.
    gen.metadata_doc.stages.append(gen.metadata_doc.stages[0].model_copy(update={"name": "ghost"}))
    with pytest.raises(ArtifactMismatch):
        gen._assert_stages_agree()


def test_the_three_way_write_is_all_or_none(gen: Generator) -> None:
    """A rejected append leaves no partial record in any of the three."""
    p = page("form_page_1_business", [control("q_001", "Password", locator="#pw")])
    bad = fill("q_001", "#pw", "hunter2-real-secret")

    with pytest.raises(CredentialLeak):
        gen.append(request(p, bad))

    assert gen.questions_doc.questions == []
    assert gen.metadata_doc.stages[0].fields == []


# -- credentials -----------------------------------------------------------


def test_a_password_literal_is_never_written(gen: Generator) -> None:
    """A literal in the artifacts is a persist-time throw; caught at write time."""
    p = page("form_page_0_login", [control("q_001", "Password", locator="#pw")])
    with pytest.raises(CredentialLeak):
        gen.append(request(p, fill("q_001", "#pw", "s3cr3t-value")))


def test_a_password_placeholder_is_accepted(gen: Generator) -> None:
    """`$PASSWORD` is what the login stage stores; the real value never arrives."""
    p = page("form_page_0_login", [control("q_001", "Password", locator="#pw")])
    gen.append(request(p, fill("q_001", "#pw", "$PASSWORD")))

    assert gen.questions_doc.questions[0].exampleValue == "$PASSWORD"
    assert "s3cr3t" not in gen.script_path.read_text()


# -- options and openSet ---------------------------------------------------


def test_help_text_as_the_sole_option_sets_open_set(gen: Generator) -> None:
    """A typeahead's prose must not become the field's only accepted value."""
    prose = [Option(label="Start typing to search...", locator=None)]
    p = page(
        "form_page_1_business",
        [control("q_001", "Class of Business", type_="select", options=prose, locator="#cob")],
    )
    gen.append(request(p, fill("q_001", "#cob", "Roofing", intent="select")))

    q = gen.questions_doc.questions[0]
    assert q.openSet is True
    assert q.options is None


def test_a_real_option_set_is_kept_and_typed_enum(gen: Generator) -> None:
    opts = [Option(label="LLC", locator="#llc"), Option(label="Sole Proprietor", locator="#sp")]
    p = page(
        "form_page_1_business",
        [control("q_001", "Legal Entity Type", type_="select", options=opts, locator="#entity")],
    )
    gen.append(request(p, fill("q_001", "#entity", "LLC", intent="select")))

    q = gen.questions_doc.questions[0]
    assert q.type == "enum"
    assert q.options == ["LLC", "Sole Proprietor"]
    assert q.openSet is None


def test_options_revealed_by_expand_outrank_a_null_option_list(gen: Generator) -> None:
    """A combobox mounts its listbox on click, so the scraper saw `options: null`."""
    p = page(
        "form_page_1_business",
        [control("q_001", "Legal Entity Type", type_="other", options=None, locator="#entity")],
    )
    report = fill(
        "q_001", "#entity", "LLC", intent="select", optionsRevealed=[
            {"label": "LLC", "locator": None}, {"label": "Sole Proprietor", "locator": None},
        ]
    )
    gen.append(request(p, report))

    assert gen.questions_doc.questions[0].options == ["LLC", "Sole Proprietor"]


# -- labels ----------------------------------------------------------------


def test_merge_fields_are_stripped_from_labels(gen: Generator) -> None:
    """Nothing downstream substitutes `{business_address}`; the braces reach the client."""
    p = page(
        "form_page_1_business",
        [control("q_001", "Square footage of space at {business_address}", locator="#sqft")],
    )
    gen.append(request(p, fill("q_001", "#sqft", "2400")))

    label = gen.questions_doc.questions[0].label
    assert "{" not in label and "}" not in label
    assert label == "Square footage of space at"


# -- conditional -----------------------------------------------------------


def test_conditional_resolves_a_field_id_to_a_question_id(gen: Generator) -> None:
    """`revealedBy` names a per-page fieldId; the artifact must name a questionId."""
    gate = control(
        "q_001",
        "Do you have multiple locations?",
        type_="toggle",
        options=[Option(label="Yes", locator="#yes"), Option(label="No", locator="#no")],
        locator="#multi",
    )
    p1 = page("form_page_1_business", [gate])
    gen.append(request(p1, fill("q_001", "#multi", "Yes", intent="select")))

    child = control(
        "q_002",
        "How many locations?",
        locator="#count",
        revealed_by=RevealedBy(fieldId="q_001", equals="Yes"),
    )
    p2 = page("form_page_1_business", [gate, child])
    gen.append(request(p2, fill("q_002", "#count", "3")))

    cond = gen.questions_doc.questions[1].conditional
    assert cond is not None
    assert cond.questionId == "q_001"
    assert cond.value == "Yes"


# -- type mapping (arch doc 3.5) ------------------------------------------


@pytest.mark.parametrize(
    "portal_type,unit,option_count,canonical,label,open_set,expected",
    [
        ("text", "usd", 0, "payroll", "Annual Payroll", False, "currency"),
        ("text", "date", 0, "policyEffectiveDate", "Effective Date", False, "date"),
        ("text", "percent", 0, "discount", "Discount", False, "number"),
        ("text", "count", 0, "full_time_employees", "Employees", False, "number"),
        ("select", None, 2, "entity_type", "Entity Type", False, "enum"),
        ("toggle", None, 2, "has_multiple_locations", "Multiple?", False, "boolean"),
        ("checkbox", None, 0, "agrees", "Agree", False, "boolean"),
        ("stepper", None, 3, "vehicles", "Vehicles", False, "enum"),
        ("stepper", None, 0, "vehicles", "Vehicles", False, "number"),
        ("other", None, 0, "notes", "Notes", False, "string"),
        # A ZIP in a `tel` box is not a phone.
        ("tel", None, 0, "zip_code", "ZIP Code", False, "string"),
        ("tel", None, 0, "phone_number", "Phone", False, "phone"),
        # A date control asking for a year holds a number.
        ("date", None, 0, "year_founded", "Year Founded", False, "number"),
        ("date", None, 0, "policyEffectiveDate", "Effective Date", False, "date"),
        # openSet beats enum: the set could not be enumerated.
        ("select", None, 3, "class_code", "Class of Business", True, "string"),
    ],
)
def test_type_mapping(
    portal_type, unit, option_count, canonical, label, open_set, expected
) -> None:
    assert (
        catalog_type(portal_type, unit, option_count, canonical, label, open_set) == expected
    )


def test_options_are_null_unless_the_type_is_enum(gen: Generator) -> None:
    """Arch doc 3.5, stated literally."""
    p = page(
        "form_page_1_business",
        [control("q_001", "Class of Business", type_="select",
                 options=[Option(label="Roofing", locator=None)], locator="#cob")],
    )
    gen.append(request(p, fill("q_001", "#cob", "Roofing", intent="select")))
    q = gen.questions_doc.questions[0]
    assert (q.options is None) == (q.type != "enum")


# -- canonical -------------------------------------------------------------


def test_an_existing_canonical_is_reused_rather_than_minted() -> None:
    """A new name for an existing fact makes the chat ask the user twice."""
    r = CanonicalResolver()
    assert r.resolve("Legal Business Name") == "business.legal_name"
    assert r.resolve("Employer Identification Number") == "ein"
    assert r.resolve("ZIP Code") == "zip_code"
    assert r.minted == {}


def test_a_new_canonical_is_minted_and_recorded_as_a_candidate() -> None:
    """Minted names are `canonical_aliases` candidates, not silent vocabulary."""
    r = CanonicalResolver()
    name = r.resolve("Number of Company Vehicles")
    assert name == "number_of_company_vehicles"
    assert r.minted == {"number_of_company_vehicles": "Number of Company Vehicles"}


def test_the_more_specific_canonical_wins() -> None:
    """Longest phrase first, or a specific fact collapses into a vaguer one."""
    r = CanonicalResolver()
    assert r.resolve("Legal Business Name") == "business.legal_name"
    assert r.resolve("Doing Business As") == "dba"


# -- reconciliation helpers ------------------------------------------------


def test_requiredness_ors_and_the_richer_option_list_wins() -> None:
    """County: once required with a placeholder, once optional with all 58."""
    placeholder = [Option(label="Select a county", locator=None)]
    full = [Option(label=f"County {i}", locator=None) for i in range(58)]
    required, options = merge_captures(True, placeholder, False, full)
    assert required is True
    assert options is not None and len(options) == 58


def test_clean_options_drops_prose_and_keeps_values() -> None:
    mixed = [Option(label="-- Select one --", locator=None), Option(label="LLC", locator=None)]
    options, open_set = clean_options(mixed)
    assert options is not None and [o.label for o in options] == ["LLC"]
    assert open_set is False


def test_strip_merge_fields_removes_templates_and_markers() -> None:
    assert strip_merge_fields("Space at {business_address}") == "Space at"
    assert strip_merge_fields("Legal Name *") == "Legal Name"
    assert strip_merge_fields("Payroll:") == "Payroll"


# -- the replay script -----------------------------------------------------


def test_the_script_references_its_pair_by_the_onboarding_names(gen: Generator) -> None:
    """The runner regex-scrapes these exact names; any other reference materializes nothing."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))

    script = gen.script_path.read_text()
    assert re.search(r"onboarding-[\w-]+\.metadata\.json", script)
    assert re.search(r"onboarding-[\w-]+\.questions\.json", script)
    # The names the regex finds must be the files actually written.
    assert gen.metadata_path.name in script
    assert gen.questions_path.name in script


def test_the_script_writes_last_run_json_and_prints_rrstatus(gen: Generator) -> None:
    """Both are required: the runner prefers the file and falls back to the line."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))

    script = gen.script_path.read_text()
    assert "last-run.json" in script
    assert "RRSTATUS" in script


def test_the_script_has_no_hardcoded_gate_answer_and_no_fallback(gen: Generator) -> None:
    """`pickYesNo('No')` discards the client's answer; `||` invents a missing one."""
    gate = control(
        "q_001",
        "Do you have multiple locations?",
        type_="toggle",
        options=[Option(label="Yes", locator="#yes"), Option(label="No", locator="#no")],
        locator="#multi",
    )
    p = page("form_page_1_business", [gate])
    gen.append(request(p, fill("q_001", "#multi", "Yes", intent="select")))

    script = gen.script_path.read_text()
    # The answer is read by canonical, not baked in.
    assert 'requiredAnswer(answers, "has_multiple_locations"' in script
    assert "pickYesNo(" not in script
    # No `||` on any line that supplies an answer. The rule is about inventing a
    # missing required value, so the footer's exit-code expression is not in scope.
    answer_lines = [
        ln for ln in script.splitlines()
        if "requiredAnswer(" in ln or "optionalAnswer(" in ln or "answers[" in ln
    ]
    assert answer_lines
    assert all("||" not in ln for ln in answer_lines)


def test_a_conditional_field_is_emitted_inside_its_parents_branch(gen: Generator) -> None:
    """Every path must run, not only the one whose answers were published.

    An unguarded child reads an answer the client was never asked for and sets a
    control that is not mounted, so a client taking the other branch fails.
    """
    gate = control(
        "q_001",
        "Legal Entity Type",
        type_="select",
        options=[Option(label="LLC", locator="#llc"),
                 Option(label="Sole Proprietor", locator="#sp")],
        locator="#entity",
    )
    child = control(
        "q_002",
        "Number of Members",
        type_="number",
        locator="#members",
        revealed_by=RevealedBy(fieldId="q_001", equals="LLC"),
    )
    p = page("form_page_1_business", [gate, child])
    gen.append(request(p, fill("q_001", "#entity", "LLC", intent="select")))
    gen.append(request(p, fill("q_002", "#members", "3")))

    script = gen.script_path.read_text()
    guard = 'if (String(v_q_001) === "LLC") {'
    assert guard in script
    body = script.split(guard, 1)[1]
    assert 'requiredAnswer(answers, "number_of_members", "q_002")' in body.split("}", 1)[0]


def test_a_two_option_gate_branches_on_its_real_labels(gen: Generator) -> None:
    """Positional yes/no made "LLC" match no branch, so the script threw.

    Only a gate whose labels really are yes/no gets `selectorYes`/`selectorNo`.
    """
    gate = control(
        "q_001",
        "Legal Entity Type",
        type_="select",
        options=[Option(label="LLC", locator="#llc"),
                 Option(label="Sole Proprietor", locator="#sp")],
        locator="#entity",
    )
    p = page("form_page_1_business", [gate])
    gen.append(request(p, fill("q_001", "#entity", "LLC", intent="select")))

    script = gen.script_path.read_text()
    assert '=== "LLC"' in script and '=== "Sole Proprietor"' in script
    assert '=== "Yes"' not in script

    field = json.loads(gen.metadata_path.read_text())["stages"][0]["fields"][0]
    assert field.get("selectorYes") is None
    assert [o["label"] for o in field["options"]] == ["LLC", "Sole Proprietor"]


def test_a_boolean_gate_keeps_the_yes_no_selector_shape(gen: Generator) -> None:
    """The shape is right for a gate that really is yes/no."""
    gate = control(
        "q_001",
        "Do you have prior claims?",
        type_="toggle",
        options=[Option(label="Yes", locator="#yes"), Option(label="No", locator="#no")],
        locator="#claims",
    )
    p = page("form_page_1_business", [gate])
    gen.append(request(p, fill("q_001", "#claims", "Yes", intent="select")))

    field = json.loads(gen.metadata_path.read_text())["stages"][0]["fields"][0]
    assert field["selectorYes"] == "#yes" and field["selectorNo"] == "#no"


LOGIN_STEPS = [
    ("goto", "", "https://carrier/sign-in"),
    ("fill", "#email", "$EMAIL"),
    ("fill", "#password", "$PASSWORD"),
    ("click", "#signin", ""),
]


def test_the_script_can_log_itself_in(gen: Generator) -> None:
    """A replay runs in a fresh browser; without this it starts unauthenticated."""
    gen.record_login(LOGIN_STEPS, "#dashboard")
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))

    script = gen.script_path.read_text()
    assert "// --- stage: login ---" in script
    assert 'requireCredential(config.LOGIN_EMAIL, "$EMAIL")' in script
    assert 'requireCredential(config.LOGIN_PASSWORD, "$PASSWORD")' in script
    # Login is the first stage the runner walks.
    assert script.index("stage: login") < script.index("stage: form_page_1_business")


def test_the_script_asserts_the_login_took(gen: Generator) -> None:
    """A portal answers a rejected sign-in by re-rendering the same form."""
    gen.record_login(LOGIN_STEPS, "#dashboard")

    script = gen.script_path.read_text()
    assert 'waitForSelector("#dashboard"' in script
    assert "login did not complete" in script


def test_the_login_stage_carries_no_credential_literal(gen: Generator) -> None:
    """Persist runs a leak scan and throws; this catches it at write time."""
    leaked = [("fill", "#password", "hunter2-password")]

    with pytest.raises(CredentialLeak):
        gen.record_login(leaked, "#dashboard")


def test_login_must_be_recorded_before_any_form_page(gen: Generator) -> None:
    """Login is the first stage; recording it later would emit it out of order."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))

    with pytest.raises(ArtifactMismatch, match="before any form page"):
        gen.record_login(LOGIN_STEPS, "#dashboard")


def test_the_login_writes_no_question(gen: Generator) -> None:
    """A credential is supplied by the runner's config, not collected from the client."""
    gen.record_login(LOGIN_STEPS, "#dashboard")

    assert gen.state().questionIds == []
    stages = json.loads(gen.metadata_path.read_text())["stages"]
    assert stages[0]["name"] == "login" and stages[0]["fields"] == []


def test_the_answers_file_is_keyed_by_the_canonical_the_script_reads(gen: Generator) -> None:
    """`questionId` is our join key; the script looks answers up by canonical."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))
    gen.record_route_end(1, "form_page_1_business", True)
    gen.publish_walk(1)

    written = json.loads(gen.write_answers().read_text())

    # The canonical is the resolver's, not the label slugged: the chat's
    # identity for a fact is reused across carriers.
    assert written == {"answers": {"business.legal_name": "Acme LLC"}}
    assert 'requiredAnswer(answers, "business.legal_name"' in gen.script_path.read_text()


def test_the_answers_file_holds_no_credential(gen: Generator) -> None:
    """The script resolves $EMAIL/$PASSWORD from --config, not from the answers."""
    gen.record_login(LOGIN_STEPS, "#dashboard")
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))
    gen.record_route_end(1, "form_page_1_business", True)
    gen.publish_walk(1)

    written = json.loads(gen.write_answers().read_text())["answers"]

    assert "$EMAIL" not in written.values()
    assert "$PASSWORD" not in written.values()


def test_the_script_shapes_a_masked_answer_and_asserts_acceptance(gen: Generator) -> None:
    """There is no model at replay time; the script carries the rule itself."""
    p = page("form_page_1_business", [control("q_001", "FEIN", locator="#fein")])
    gen.append(request(p, fill(
        "q_001", "#fein", "842673915",
        constraint={"unit": "", "format": "999999999", "hint": "Nine digits, no dashes"},
    )))

    script = gen.script_path.read_text()
    assert 'shapeAnswer(v_q_001, "999999999", "q_001",' in script
    assert 'await page.fill("#fein", String(v_q_001_shaped));' in script
    assert 'assertAccepted(page, "#fein", "q_001",' in script
    assert "// q_001: Nine digits, no dashes" in script


def test_every_text_fill_asserts_acceptance_even_without_a_mask(gen: Generator) -> None:
    """A value the page refused must fail the stage, never be walked past."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))

    script = gen.script_path.read_text()
    assert "shapeAnswer(" not in script.split("// --- stage: form_page_1_business", 1)[1]
    assert 'assertAccepted(page, "#legalName", "q_001",' in script


def test_an_optional_field_is_guarded_as_a_block(gen: Generator) -> None:
    """Several statements under one null-guard need braces, not a one-liner."""
    p = page("form_page_1_business", [control("q_001", "DBA", required=False)])
    gen.append(request(p, fill("q_001", "#legalName", "Acme")))

    script = gen.script_path.read_text()
    assert "if (v_q_001 !== null) {" in script


def test_the_shaping_helper_coerces_or_fails_loudly() -> None:
    """Run the emitted JavaScript itself, not a Python model of it."""
    from trailblazer.agents.generator.script import SHAPE_HELPERS_JS

    probe = SHAPE_HELPERS_JS + """
const out = [];
out.push(shapeAnswer("84-2673915", "999999999", "q_005", "ein"));
out.push(shapeAnswer("04152027", "99/99/9999", "q_001", "effective_date"));
try { shapeAnswer("12345", "999999999", "q_005", "ein"); out.push("no-throw"); }
catch (e) { out.push(e.message); }
process.stdout.write(JSON.stringify(out));
"""
    done = subprocess.run(["node", "-e", probe], capture_output=True, text=True, check=True)
    shaped, dated, failure = json.loads(done.stdout)

    assert shaped == "842673915"
    assert dated == "04/15/2027"
    assert failure == 'q_005 (ein): expected 9 digits, got 5 in "12345"'


def test_the_script_carries_the_bind_denylist(gen: Generator) -> None:
    """On the script's first run there is no agent watching."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))

    script = gen.script_path.read_text()
    for term in ("pay", "bind", "purchase", "checkout", "confirm payment"):
        assert term in script


def test_the_script_uses_the_closed_outcome_vocabulary(gen: Generator) -> None:
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))

    script = gen.script_path.read_text()
    assert "appetite-decline" in script
    assert "'stuck'" in script
    # Exit 0 for quote and appetite-decline; a decline is a successful run.
    assert "outcome === 'appetite-decline') ? 0 : 1" in script


@pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node is not installed",
)
def test_the_generated_script_is_valid_javascript(gen: Generator, tmp_path: Path) -> None:
    """A script that does not parse fails the runner before it can report anything."""
    gate = control(
        "q_001",
        "Do you have multiple locations?",
        type_="toggle",
        options=[Option(label="Yes", locator="#yes"), Option(label="No", locator="#no")],
        locator="#multi",
    )
    p1 = page("form_page_1_business", [gate])
    gen.append(request(p1, fill("q_001", "#multi", "Yes", intent="select")))
    p2 = page("form_page_2_payroll", [control("q_001", "Annual Payroll", locator="#payroll")], url="https://carrier/2")
    gen.append(request(p2, fill("q_001", "#payroll", "250000")))
    gen.append(request(p2, FillReport(fieldId=None, intent="advance", locator="#next", ok=True)))

    result = subprocess.run(
        ["node", "--check", str(gen.script_path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# -- blocked and advance ---------------------------------------------------


def test_a_failed_fill_writes_no_question_and_records_the_blocker(gen: Generator) -> None:
    """An action is appended only when known-good."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    report = FillReport(
        fieldId="q_001",
        intent="fill",
        locator="#legalName",
        ok=False,
        blocked={"control": "Legal Business Name", "whatYouTried": "typed, field stayed empty"},
    )
    gen.append(request(p, report))

    assert gen.questions_doc.questions == []
    assert len(gen.metadata_doc.blocked) == 1
    assert gen.metadata_doc.blocked[0].whatYouTried == "typed, field stayed empty"


def test_an_advance_sets_the_stage_next_and_writes_no_question(gen: Generator) -> None:
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))
    gen.append(request(p, FillReport(fieldId=None, intent="advance", locator="#next", ok=True)))

    assert gen.metadata_doc.stages[0].next == "#next"
    assert gen.state().questionIds == ["q_001"]


# -- constraints -----------------------------------------------------------


def test_a_discovered_constraint_reaches_unit_format_and_answer_hint(gen: Generator) -> None:
    """A script hardcoding the walked value hits the same validation on a new input."""
    p = page("form_page_1_business", [control("q_001", "Federal Tax ID", locator="#ein")])
    report = fill(
        "q_001",
        "#ein",
        "12-3456789",
        constraint={"unit": "text", "format": "NN-NNNNNNN", "hint": "nine digits with a dash"},
        retried=True,
    )
    gen.append(request(p, report))

    q = gen.questions_doc.questions[0]
    assert q.format == "NN-NNNNNNN"
    assert q.answerHint == "nine digits with a dash"
    assert q.canonical == "ein"


# -- incremental output ----------------------------------------------------


def test_all_three_files_exist_after_the_first_append(gen: Generator) -> None:
    """Durable progress: a crash mid-flow leaves partial work that can be inspected."""
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")))

    assert gen.questions_path.exists()
    assert gen.metadata_path.exists()
    assert gen.script_path.exists()


# -- ledger ----------------------------------------------------------------


def test_every_append_is_recorded_on_the_ledger(gen: Generator) -> None:
    """An agent that does not record its steps is invisible in the run's accounting."""
    ledger = RunLedger(job_id="job-1")
    p = page("form_page_1_business", [control("q_001", "Legal Business Name")])
    gen.append(request(p, fill("q_001", "#legalName", "Acme LLC")), ledger=ledger)
    gen.append(
        request(p, FillReport(fieldId=None, intent="advance", locator="#next", ok=True)),
        ledger=ledger,
    )

    assert [s.action for s in ledger.steps] == ["append", "advance"]
    assert all(s.agent == "generator" for s in ledger.steps)
    # The Generator is deterministic: it makes no LLM calls.
    assert ledger.total_usd() == 0.0


def test_a_failed_append_is_recorded_as_not_ok(gen: Generator) -> None:
    ledger = RunLedger(job_id="job-1")
    p = page("form_page_0_login", [control("q_001", "Password", locator="#pw")])
    with pytest.raises(CredentialLeak):
        gen.append(request(p, fill("q_001", "#pw", "real-secret")), ledger=ledger)

    assert ledger.steps[-1].ok is False
    assert ledger.by_agent()["generator"]["failed"] == 1
