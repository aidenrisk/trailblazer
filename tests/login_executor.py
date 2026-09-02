"""A test-only stand-in for the FormFiller, just enough to drive a LOGIN.

The FormFiller agent is another team's work and does not exist yet. The
end-to-end login test still needs something in its slot that acts on the page,
so this executor implements the FormFiller interface (`execute(job, stageId,
Assignment) -> FillReport`) using only the login actions from
`trailblazer.agents.browser.login_actions`, plus the two page-agnostic moves a
login screen needs: click one option (the email channel) and click Next.

It is NOT the FormFiller. It fills no form fields, discovers no widgets, and
lives under tests/. When the real agent lands, it should call the same
`login_actions` functions and this file can go.
"""

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from trailblazer.agents.browser import login_actions as la
from trailblazer.agents.browser.otp_inbox import OtpInbox
from trailblazer.contracts import (
    LOGIN_OTP,
    Assignment,
    FillFieldAssignment,
    FillReport,
    FillStep,
    SetOptionAssignment,
    SimpleAssignment,
)
from trailblazer.shared.carrier_creds import CarrierCreds


class LoginExecutor:
    def __init__(
        self,
        page: Page,
        *,
        credentials: CarrierCreds | None,
        inbox: OtpInbox | None = None,
        human_entry_possible: bool = False,
        mfa_timeout_s: float = 600.0,
        poll_s: float = 2.0,
        settle_s: float = 12.0,
        markers: list[str] | None = None,
    ) -> None:
        self.page = page
        self.credentials = credentials
        self.inbox = inbox
        self.human_entry_possible = human_entry_possible
        self.mfa_timeout_s = mfa_timeout_s
        self.poll_s = poll_s
        self.settle_s = settle_s
        self.markers = markers
        self._credential_anchor: str | None = None

    def execute(self, job: str, stage_id: str, assignment: Assignment) -> FillReport:
        try:
            if isinstance(assignment, FillFieldAssignment):
                return self._fill(assignment)
            if isinstance(assignment, SetOptionAssignment):
                return self._choose(assignment)
            if isinstance(assignment, SimpleAssignment):
                return self._navigate(assignment)
        except PlaywrightError as e:
            return FillReport(ok=False, errorClass="widget", fieldId=getattr(assignment, "fieldId", None))
        return FillReport(ok=False, errorClass="widget")

    def _overlays(self) -> list[FillStep]:
        return [FillStep(action="click", locator=sel) for sel in la.dismiss_overlays(self.page)]

    def _fill(self, a: FillFieldAssignment) -> FillReport:
        overlays = self._overlays()
        if a.credentialKey is None:
            raise AssertionError(f"LoginExecutor only fills credentials; got a plain fill for {a.fieldId}")
        if a.credentialKey == LOGIN_OTP:
            out = la.clear_otp(
                self.page,
                a.locator,
                self.credentials,
                self.inbox,
                human_entry_possible=self.human_entry_possible,
                timeout_s=self.mfa_timeout_s,
                poll_s=self.poll_s,
                settle_s=self.settle_s,
                markers=self.markers,
            )
            if not out.cleared:
                return FillReport(ok=False, steps=overlays, errorClass=la.otp_error_class(out), fieldId=a.fieldId)
            return FillReport(
                ok=True,
                steps=overlays + [FillStep(fieldId=a.fieldId, action="fill", locator=a.locator, value=LOGIN_OTP)],
                advance=True,
                landed=[a.fieldId],
                fieldId=a.fieldId,
            )
        out = la.fill_credential(self.page, a.locator, a.credentialKey, self.credentials)
        if not out.ok:
            return FillReport(ok=False, steps=overlays, errorClass=out.error, fieldId=a.fieldId)
        self._credential_anchor = out.selector
        return FillReport(
            ok=True,
            steps=overlays + [FillStep(fieldId=a.fieldId, action="fill", locator=out.selector, value=a.credentialKey)],
            landed=[a.fieldId],
            fieldId=a.fieldId,
        )

    def _choose(self, a: SetOptionAssignment) -> FillReport:
        overlays = self._overlays()
        found = la.resolve_unique(self.page, a.locator)
        if found.error:
            return FillReport(ok=False, steps=overlays, errorClass=found.error, fieldId=a.fieldId)
        if (found.locator.get_attribute("type") or "").lower() in ("radio", "checkbox"):
            found.locator.check()
        else:
            found.locator.click()
        return FillReport(
            ok=True,
            steps=overlays + [FillStep(fieldId=a.fieldId, action="select", locator=found.selector, value=a.option)],
            landed=[a.fieldId],
            fieldId=a.fieldId,
            chosenOption=a.option,
        )

    def _navigate(self, a: SimpleAssignment) -> FillReport:
        if a.type == "stop":
            return FillReport(ok=True)
        overlays = self._overlays()
        if not a.locator:
            return FillReport(ok=False, steps=overlays, errorClass="not_found")
        found = la.resolve_unique(self.page, a.locator, credential_anchor=self._credential_anchor)
        if found.error:
            return FillReport(ok=False, steps=overlays, errorClass=found.error)
        if not la.ensure_enabled(self.page, found.locator):
            return FillReport(ok=False, steps=overlays, errorClass="validation")
        before = self.page.url
        found.locator.click()
        return FillReport(
            ok=True,
            steps=overlays + [FillStep(action="click", locator=found.selector)],
            advance=la.settle_after_click(self.page, before, found.selector),
        )
