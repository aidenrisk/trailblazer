"""The Scraper on login pages: credentials are measured, the stage is named for it.

Real browser and real perceiver against three stand-in pages (tests/pages.py,
written to a temp dir for the module); no model. What is asserted is what the
extractor reads off the markup -- which inputs are credentials, that six digit
boxes are one control, that a hidden look-alike submit button does not hide the
visible one -- and that `finalize` turns a page with a credential into a
`login_*` stage.
"""

from pathlib import Path

import pytest

from trailblazer.agents.browser.session import BrowserSession
from trailblazer.agents.scraper.perceive import DomSnapshotPerceiver
from trailblazer.agents.scraper.scraper import finalize, restore_measured_locators
from trailblazer.contracts import Control, PageDescription
from tests.pages import page_uri, write_pages

_PAGES: dict[str, Path] = {}


@pytest.fixture(scope="module", autouse=True)
def _pages_dir(tmp_path_factory):
    _PAGES["dir"] = write_pages(tmp_path_factory.mktemp("login-pages"))


def _perceive(name: str, port: int) -> tuple[dict, dict[str, int]]:
    """Perceive one page and also count what its `next` locator resolves to."""
    with BrowserSession(cdp_port=port) as session:
        page = session.goto(page_uri(_PAGES["dir"], name))
        payload = DomSnapshotPerceiver().perceive(page)
        counts = {
            "next": page.locator(payload["next"]).count() if payload["next"] else 0,
            **{
                c["locator"]: page.locator(c["locator"]).count()
                for c in payload["controls"]
                if c["locator"]  # an unaddressable control has nothing to count
            },
        }
    return payload, counts


def _by_locator(payload: dict, locator: str) -> dict:
    return next(c for c in payload["controls"] if c["locator"] == locator)


# --------------------------------------------------------------------------- #
# login.html: username + password + remember-me + a hidden duplicate submit
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def login():
    return _perceive("login.html", 9231)


def test_username_and_password_are_measured_and_nothing_else_is(login) -> None:
    payload, _ = login
    assert _by_locator(payload, "#username")["credential"] == "username"
    assert _by_locator(payload, "#password")["credential"] == "password"
    assert _by_locator(payload, "#remember")["credential"] is None


def test_the_visible_sign_in_button_wins_over_its_hidden_twin(login) -> None:
    """Two "Sign in" buttons in the DOM, one visible: `next` is the one a person clicks."""
    payload, counts = login
    assert payload["next"] is not None and "Sign in" in payload["next"]
    assert counts["next"] == 1


def test_every_login_locator_resolves_to_exactly_one_node(login) -> None:
    payload, counts = login
    for c in payload["controls"]:
        assert c["unique"] and counts[c["locator"]] == 1, c["locator"]


# --------------------------------------------------------------------------- #
# otp.html: a single code input, a Resend button, a Verify button
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def otp():
    return _perceive("otp.html", 9232)


def test_one_time_code_input_is_an_otp_credential(otp) -> None:
    payload, _ = otp
    assert _by_locator(payload, "#code")["credential"] == "otp"
    assert _by_locator(payload, "#code")["otpBoxes"] == 0


def test_verify_is_next_and_resend_is_not(otp) -> None:
    payload, counts = otp
    assert payload["next"] == 'button:has-text("Verify")'
    assert counts["next"] == 1


# --------------------------------------------------------------------------- #
# otp-digits.html: six boxes, no ids, no labels, an <input type=submit>
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def digits():
    return _perceive("otp-digits.html", 9233)


def test_six_digit_boxes_are_one_otp_control(digits) -> None:
    """Six LOGIN_OTP fills would pull six codes; the group is one control."""
    payload, counts = digits
    assert len(payload["controls"]) == 1
    only = payload["controls"][0]
    assert only["credential"] == "otp"
    assert only["otpBoxes"] == 6
    assert only["unique"] and counts[only["locator"]] == 1


def test_the_group_is_named_for_its_legend_not_a_single_box(digits) -> None:
    payload, _ = digits
    assert "6-digit code" in payload["controls"][0]["accessibleName"]


def test_an_input_submit_is_found_as_next(digits) -> None:
    payload, counts = digits
    assert payload["next"] is not None and 'type="submit"' in payload["next"]
    assert counts["next"] == 1


# --------------------------------------------------------------------------- #
# The weak signal: a bare email field is a credential only on a login page.
# --------------------------------------------------------------------------- #


CONTACT_PAGE = """
<form>
  <label for="contact">Contact email</label>
  <input type="email" id="contact" name="contactEmail">
  <label for="name">Business name</label>
  <input type="text" id="name" name="businessName">
  <button type="button">Next</button>
</form>
"""

OTP_ONLY_LOGIN = """
<form>
  <label for="email">Email</label>
  <input type="email" id="email" name="email">
  <label for="pin">Verification code</label>
  <input type="text" id="pin" name="pin" inputmode="numeric">
  <button type="submit">Continue</button>
</form>
"""


def _perceive_html(tmp_path, html: str, port: int) -> dict:
    """Serve an inline page from disk: the tab Chromium opens with refuses set_content."""
    path = tmp_path / "page.html"
    path.write_text(f"<!doctype html><html><body>{html}</body></html>")
    with BrowserSession(cdp_port=port) as session:
        page = session.goto(path.resolve().as_uri())
        return DomSnapshotPerceiver().perceive(page)


def test_an_applicants_email_on_a_form_page_is_not_a_credential(tmp_path) -> None:
    """The agency's login must never be typed into a customer's contact field."""
    payload = _perceive_html(tmp_path, CONTACT_PAGE, 9234)
    assert _by_locator(payload, "#contact")["credential"] is None


def test_an_email_beside_a_code_field_is_the_username_of_an_otp_only_login(tmp_path) -> None:
    """Next Insurance signs in with an email and a code, no password."""
    payload = _perceive_html(tmp_path, OTP_ONLY_LOGIN, 9235)
    assert _by_locator(payload, "#email")["credential"] == "username"
    assert _by_locator(payload, "#pin")["credential"] == "otp"


# --------------------------------------------------------------------------- #
# finalize() and restore_measured_locators(): no browser.
# --------------------------------------------------------------------------- #


def _control(**overrides) -> Control:
    base = dict(fieldId="", key="el_0", label="x", type="text", required=True, locator="#x", unique=True)
    return Control(**{**base, **overrides})


def test_a_page_with_a_credential_is_a_login_stage() -> None:
    page = PageDescription(stageId="", url="", controls=[_control(credential="password")])

    finalize(page, page_index=1, url="https://idp.example.test/u/login?state=abc", title="Log in")

    assert page.stageId == "login_login"
    assert page.is_login_stage


def test_a_page_without_credentials_keeps_the_form_stage_name() -> None:
    page = PageDescription(stageId="", url="", controls=[_control()])

    finalize(page, page_index=2, url="https://x.com/app/business-info", title="t")

    assert page.stageId == "form_page_2_business_info"


def test_the_measured_credential_overrules_the_model() -> None:
    """Same rule as locators: the model's opinion of `credential` is discarded."""
    described = PageDescription(
        stageId="",
        url="",
        controls=[
            _control(key="el_0", locator="#password", credential=None),  # model dropped it
            _control(key="el_1", locator="#name", credential="username"),  # model invented it
        ],
    )
    payload = [
        {"key": "el_0", "locator": "#password", "unique": True, "credential": "password"},
        {"key": "el_1", "locator": "#name", "unique": True, "credential": None},
    ]

    restore_measured_locators(described, payload)

    assert [c.credential for c in described.controls] == ["password", None]


def test_a_landing_page_whose_only_way_forward_is_log_in_is_a_login_stage() -> None:
    """Thimble opens on a gate with no inputs and a Log In button that hands off to Auth0."""
    gate = PageDescription(stageId="", url="", controls=[], next='button:has-text("Log In")')
    finalize(gate, page_index=1, url="https://broker.thimble.com/", title="Thimble for Brokers")
    assert gate.is_login_stage

    dashboard = PageDescription(stageId="", url="", controls=[], next=None)
    finalize(dashboard, page_index=1, url="https://broker.thimble.com/dashboard/policies", title="Policies")
    assert not dashboard.is_login_stage

    form = PageDescription(stageId="", url="", controls=[_control()], next='button:has-text("Log In")')
    finalize(form, page_index=1, url="https://x/app", title="t")
    assert not form.is_login_stage  # inputs on the page: the button's wording alone does not decide
