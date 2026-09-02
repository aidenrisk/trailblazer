"""The six contract changes that let login be page 1 of the same chain."""

import pytest
from pydantic import ValidationError

from trailblazer.contracts import (
    LOGIN_OTP,
    LOGIN_PASSWORD,
    Control,
    FillFieldAssignment,
    FillReport,
    Option,
    PageDescription,
    Walk,
    WalkPath,
    WalkStep,
)


def _control(**overrides) -> Control:
    base = dict(
        fieldId="q_001", label="Password", type="text", required=True, locator="#password", unique=True
    )
    return Control(**{**base, **overrides})


# 1. Control.credential ------------------------------------------------------


@pytest.mark.parametrize("kind", ["username", "password", "otp"])
def test_control_carries_a_measured_credential_kind(kind: str) -> None:
    assert _control(credential=kind).credential == kind


def test_control_credential_defaults_to_none_and_rejects_unknown_kinds() -> None:
    assert _control().credential is None
    with pytest.raises(ValidationError):
        _control(credential="pin")


def test_serialized_control_is_the_eight_documented_fields_plus_credential() -> None:
    """`key` stays transport-only; `credential` is the one deliberate addition."""
    assert list(_control(key="el_0", credential="password").model_dump().keys()) == [
        "fieldId",
        "label",
        "type",
        "required",
        "options",
        "locator",
        "unique",
        "revealedBy",
        "credential",
    ]


def test_a_hand_built_control_needs_no_transport_key() -> None:
    """Fixtures and stubs build Controls without `key`; only the model's schema requires it."""
    assert _control().key is None


def test_option_locator_may_be_absent_for_a_select_option() -> None:
    """A native <select> option is set by label on the parent; it has no node of its own."""
    assert Option(label="LLC").locator is None


# 2. login_* stage naming ----------------------------------------------------


def test_stage_prefix_says_whether_a_page_is_part_of_the_login() -> None:
    login = PageDescription(stageId="login_sign_in", url="https://x/login", controls=[])
    form = PageDescription(stageId="form_page_1_business_info", url="https://x/app", controls=[])
    assert login.is_login_stage and not form.is_login_stage


# 3. FillFieldAssignment.credentialKey ---------------------------------------


def test_fill_field_takes_a_credential_key_instead_of_a_value() -> None:
    a = FillFieldAssignment(fieldId="q_001", locator="#password", credentialKey=LOGIN_PASSWORD)
    assert a.value is None and a.credentialKey == "LOGIN_PASSWORD"


def test_fill_field_needs_exactly_one_of_value_or_credential_key() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        FillFieldAssignment(fieldId="q_001", locator="#p")
    with pytest.raises(ValidationError, match="exactly one"):
        FillFieldAssignment(fieldId="q_001", locator="#p", value="x", credentialKey=LOGIN_PASSWORD)


def test_only_known_credential_keys_are_accepted() -> None:
    with pytest.raises(ValidationError, match="unknown credentialKey"):
        FillFieldAssignment(fieldId="q_001", locator="#p", credentialKey="API_TOKEN")


def test_otp_is_an_ordinary_credential_key() -> None:
    """No new assignment type: the code is resolved like any other credential."""
    a = FillFieldAssignment(fieldId="q_009", locator="#code", credentialKey=LOGIN_OTP)
    assert a.credentialKey == "LOGIN_OTP"


# 4. FillReport.errorClass -----------------------------------------------------


@pytest.mark.parametrize("kind", ["auth", "mfa_timeout"])
def test_fill_report_names_the_two_login_failures(kind: str) -> None:
    assert FillReport(ok=False, errorClass=kind).errorClass == kind


# 5. Walk.login ---------------------------------------------------------------


def test_walk_has_a_login_prefix_separate_from_its_paths() -> None:
    login = [
        WalkStep(action="type", fieldId="q_u", locator="#user", credentialKey="LOGIN_EMAIL"),
        WalkStep(action="type", fieldId="q_p", locator="#password", credentialKey=LOGIN_PASSWORD),
        WalkStep(action="click", locator='button:has-text("Sign in")'),
    ]
    walk = Walk(login=login, paths=[WalkPath(steps=[WalkStep(action="type", locator="#name", value="x")])])

    assert walk.login == login
    assert all(s.value is None for s in walk.login if s.credentialKey)  # never the secret
    assert Walk().login == []  # a walk from an authenticated tab has no login prefix
