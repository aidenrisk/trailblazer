"""
The value picker: what to type, and who decides.

Two layers. The offline tests prove the plumbing — caching, degradation, the
constraints reaching the model — with a fake model, so they cost nothing and
never flake. The live test at the bottom calls the real model and is skipped
unless a provider key is set; it is the only thing that can prove the prompt
actually returns a value a form would accept.
"""

import os

import pytest

from trailblazer.agents.form_filler.value_picker import (
    FieldValue,
    LLMValuePicker,
    rule_based,
)
from trailblazer.contracts import Control


def control(label, control_type="text", required=True):
    return Control(
        fieldId="q_001",
        label=label,
        type=control_type,
        required=required,
        options=None,
        locator="#x",
        unique=True,
    )


class FakeModel:
    """Stands in for the chat model, and counts how often it was asked."""

    def __init__(self, answer="12345"):
        self.answer = answer
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.prompts.append(messages[-1]["content"])
        return FieldValue(value=self.answer)


class TestTheRuleTable:
    """
    The fallback, and the reason a model is wanted in the first place.

    It answers by sniffing the label for substrings somebody wrote a branch for,
    so it is right about the labels that were anticipated and wrong about the
    rest — which is a rule table's ceiling, not a bug to fix.
    """

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("Business Zip Code", "10001"),
            ("FEIN", "12-3456789"),
            ("Email Address", "test@example.com"),
        ],
    )
    def test_knows_the_labels_it_was_written_for(self, label, expected):
        assert rule_based(control(label)) == expected

    @pytest.mark.parametrize(
        "label", ["Suite / Unit", "NAICS Code", "Years in Business", "Number of Members"]
    )
    def test_falls_through_on_everything_else(self, label):
        # A real form rejects every one of these. This is what the model fixes.
        assert rule_based(control(label)) == "Test Value"


class TestPickerPlumbing:
    def test_asks_the_model_and_returns_what_it_says(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        model = FakeModel("11217")
        picker = LLMValuePicker(model=model)

        assert picker(control("Business Zip Code")) == "11217"
        assert len(model.prompts) == 1

    def test_the_model_is_told_what_it_needs_to_judge(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        model = FakeModel()
        picker = LLMValuePicker(model=model)

        picker(
            control("Mailing Zip", "text"),
            context="Business Information",
            constraints={"pattern": r"\d{5}", "max_length": 5, "placeholder": ""},
        )
        prompt = model.prompts[0]

        assert "Mailing Zip" in prompt
        assert "Business Information" in prompt  # which form this is
        assert r"\d{5}" in prompt  # what the field will accept
        assert "Max length: 5" in prompt
        assert "placeholder" not in prompt.lower()  # empty constraints are dropped

    def test_repeats_are_answered_from_cache(self, monkeypatch):
        """
        A chooser is re-filled once per option walked and a multi-page form
        repeats fields like Zip, so the same field is asked for many times over
        one walk. It should be paid for once.
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        model = FakeModel()
        picker = LLMValuePicker(model=model)

        for _ in range(5):
            picker(control("Business Zip Code"))

        assert len(model.prompts) == 1

    def test_different_fields_are_asked_separately(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        model = FakeModel()
        picker = LLMValuePicker(model=model)

        picker(control("Business Zip Code"))
        picker(control("Legal Business Name"))

        assert len(model.prompts) == 2


class TestDegradation:
    """A model problem must cost one bad value, never the walk."""

    def test_no_key_falls_back_to_the_rule_table(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("LLM_PROVIDER", "openrouter")

        assert LLMValuePicker()(control("Business Zip Code")) == "10001"

    def test_a_raising_model_falls_back(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

        class Broken:
            def invoke(self, messages):
                raise RuntimeError("502 upstream")

        assert LLMValuePicker(model=Broken())(control("FEIN")) == "12-3456789"

    def test_an_empty_answer_falls_back(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

        assert LLMValuePicker(model=FakeModel(""))(control("FEIN")) == "12-3456789"


@pytest.fixture(scope="module")
def picker():
    """One picker for the live tests, so its cache is shared across them."""
    return LLMValuePicker()


@pytest.mark.skipif(
    not LLMValuePicker.is_configured(),
    reason="no LLM provider key set; this test calls the real model",
)
class TestAgainstTheRealModel:
    """
    Opt-in, and the only test that proves the prompt works.

    Everything above runs against a fake and so can only show that the wiring is
    right. Whether the model actually returns five digits for "Business Zip
    Code" is a property of the prompt, and nothing but a real call can tell.

    Assertions are on shape, not on exact strings — the model is free to pick
    any valid ZIP, and pinning one would make this fail on a model change for no
    good reason.
    """

    def test_zip_code_is_five_digits(self, picker):
        value = picker(control("Business Zip Code"), context="Business Information")
        assert value.isdigit() and len(value) == 5, value

    def test_fein_is_formatted(self, picker):
        import re

        value = picker(control("FEIN"), context="Business Information")
        assert re.fullmatch(r"\d{2}-\d{7}", value), value

    def test_number_field_has_no_units_or_separators(self, picker):
        value = picker(
            control("Target or Incumbent Premium", "number"),
            context="Business Information",
        )
        assert value.replace(".", "").isdigit(), value

    def test_a_label_no_rule_table_anticipated(self, picker):
        """
        The whole point. `rule_based` answers "Test Value" here; a form asking
        for a member count wants a number.
        """
        value = picker(control("Number of Members", "text"), context="Business Information")

        assert value != "Test Value"
        assert value.strip(), value

    def test_naics_code_looks_like_a_naics_code(self, picker):
        value = picker(control("NAICS Code"), context="Business Information")

        assert value.isdigit() and 2 <= len(value) <= 6, value

    def test_constraints_are_respected(self, picker):
        value = picker(
            control("Mailing Zip"),
            context="Business Information",
            constraints={"max_length": 5, "pattern": r"\d{5}"},
        )
        assert len(value) <= 5, value


def test_provider_detection_matches_the_environment():
    """Sanity: `is_configured` reads the same variable `get_model` requires."""
    provider = os.getenv("LLM_PROVIDER", "openrouter").lower()
    expected = "OPENROUTER_API_KEY" if provider == "openrouter" else "ANTHROPIC_API_KEY"

    assert LLMValuePicker.is_configured() == bool(os.getenv(expected))
