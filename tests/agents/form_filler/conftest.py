"""
A real Chromium against a local file, shared by every FormFiller test.

Real browser rather than a stand-in for Playwright's Page: the things worth
proving here are that a fill actually lands in the DOM, that a native <select>
really does report its options, and that a widget which renders nothing until
clicked is discovered by clicking it. A fake Page proves only that the filler
calls the methods its author expected.

Local `file://` rather than a server: no network, no port, no flakes, and the
page is identical on every run.
"""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from trailblazer.agents.form_filler.form_filler import FormFiller

FIXTURES = Path(__file__).parents[2] / "fixtures"
FORM_URL = (FIXTURES / "form_page.html").as_uri()

JOB = "job_filler_test"


@pytest.fixture(scope="session")
def browser():
    """One browser for the whole session; launching costs about a second."""
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    """A fresh tab on the fixture form, so no test sees another's fills."""
    context = browser.new_context()
    tab = context.new_page()
    tab.goto(FORM_URL)
    yield tab
    context.close()


def fixed(value="Test Value"):
    """
    A ValuePicker that always answers the same thing.

    Injected everywhere below so the tests never call a model: they are about
    whether the value LANDS, not about which value was chosen. What the real
    picker chooses is tested separately, against the live model.
    """

    def pick(control, context="", constraints=None):
        return value

    return pick


@pytest.fixture
def filler(page):
    """The filler under test: deterministic values, recovery off."""
    return FormFiller(page, value_picker=fixed())
