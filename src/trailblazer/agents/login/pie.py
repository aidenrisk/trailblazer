"""Pie's sign-in journey.

Selectors are taken from live captures of `partner.pieinsurance.com/sign-in`
(`outputs/signin`, `outputs/live-login`) and of the signed-in dashboard
(`outputs/dash`, `outputs/dash2`), not from reading the page by hand.

The sign-in page raises a privacy dialog over the form, which is why
`navigate` dismisses it: the form is behind the overlay and a fill against a
covered input times out.
"""

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.agents.login.base import CarrierLogin
from trailblazer.agents.login.registry import register
from trailblazer.observability.logging import get_logger

log = get_logger(__name__)


@register
class PieLogin(CarrierLogin):
    """Sign in to the Pie partner portal.

    One instance serves every NAICS code and every state: the journey is a
    property of the carrier, and the class code is answered inside the
    application form, not here.
    """

    carrier_id = "pie"

    username_selector = "#emailAddress"
    password_selector = "#password"

    submit_selector = "button[type=submit]"
    """Not measured. The captures of the sign-in page recorded no actions, so
    this is the generic default and is the first thing to check if login fails
    with 'matched 0 elements'."""

    authenticated_selector = '[data-testid="partnerNavSearch"]'
    """The dashboard's own nav item. Present on `/search` after sign-in and
    absent from `/sign-in`, so it separates a completed login from a rejected
    one that re-rendered the same form."""

    mfa_selector = 'input[name="otp"], input[autocomplete="one-time-code"]'
    """Not observed on Pie. Kept as the generic shape so an MFA prompt is
    reported rather than mistaken for a failed sign-in."""

    _PRIVACY_ACCEPT = 'button:has-text("Accept All")'
    """The privacy dialog covers the form; the crawl's own blocker list names it."""

    def navigate(self, page: Page) -> None:
        """Open the sign-in page and clear the privacy dialog over the form."""
        super().navigate(page)
        self._dismiss_privacy(page)

    def _dismiss_privacy(self, page: Page) -> None:
        """Accept the privacy dialog if it is up.

        Absence is normal -- the profile keeps the choice between runs -- so a
        missing dialog is not an error. A dialog that is present and does not
        clear is left to fail at the fill, where the message names the field.
        """
        try:
            button = page.locator(self._PRIVACY_ACCEPT)
            if button.count() == 1 and button.is_visible(timeout=2_000):
                button.click(timeout=5_000)
                log.info("login dismissed the privacy dialog carrier_id=%s", self.carrier_id)
        except PlaywrightError as e:
            log.debug("no privacy dialog to dismiss: %s", e)
