"""The Validator: static checks, outcome reading, and the run itself.

Every replay here is a hand-written fixture under `tests/fixtures/scripts/`.
None of them opens a browser or touches a carrier portal -- they print and
write the status a real script would, which is the whole surface the Validator
reads.

A fixture is copied into `tmp_path` before it runs, because a script writes
`last-run.json` beside itself and running one in the repo would leave that file
behind for the next test to read.
"""

import json
import shutil
from pathlib import Path

import pytest

from trailblazer.agents.validator import normalize_outcome, read_outcome, static_checks, validate
from trailblazer.contracts.validation import ValidationRequest
from trailblazer.observability.ledger import RunLedger
from trailblazer.shared.config import Settings

SCRIPTS = Path(__file__).parent / "fixtures" / "scripts"
ANSWERS = SCRIPTS / "answers.json"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="the replay runner needs node on PATH"
)


@pytest.fixture
def settings() -> Settings:
    """Headless, and never reading a developer's real `.env`."""
    return Settings(_env_file=None, headed=False)


def run(name: str, tmp_path: Path, settings: Settings, **kwargs):
    """Copy one fixture script into `tmp_path` and validate it there."""
    script = tmp_path / Path(name).name
    shutil.copy(SCRIPTS / name, script)
    request = ValidationRequest(
        job_id="test-job", script_path=str(script), answers_path=str(ANSWERS)
    )
    return validate(request, settings, **kwargs)


# --- reading the outcome ----------------------------------------------------


def test_a_quote_reports_the_premium_and_exits_zero(tmp_path, settings) -> None:
    """The happy path: both status channels agree and the run succeeded."""
    result = run("quote.js", tmp_path, settings)

    assert result.outcome == "quote"
    assert result.reachedQuote is True
    assert result.success is True
    assert result.exitCode == 0
    assert result.premium == 3036.0
    assert result.premiumDisplay == "$3,036/yr"
    assert result.quoteNumber == "Q-88213"
    assert result.bindUrl == "https://portal.example.com/bind/88213"
    assert result.bindControlLabel == "Request to Bind"


def test_the_status_file_alone_is_enough(tmp_path, settings) -> None:
    """A script that writes last-run.json and prints nothing still reports."""
    result = run("file_only.js", tmp_path, settings)

    assert result.outcome == "quote"
    assert result.quoteNumber == "Q-FILE-1"
    assert result.exitCode == 0


def test_the_rrstatus_line_alone_is_enough(tmp_path, settings) -> None:
    """With no file written, the stdout line carries the status."""
    result = run("stdout_only.js", tmp_path, settings)

    assert result.outcome == "quote"
    assert result.quoteNumber == "Q-STDOUT-1"
    assert result.premium == 2400.0, "a premium written as `$2,400` is still a number"


def test_the_file_is_preferred_over_stdout(tmp_path, settings) -> None:
    """When the two channels disagree, last-run.json wins."""
    result = run("file_beats_stdout.js", tmp_path, settings)

    assert result.outcome == "quote"
    assert result.quoteNumber == "Q-FROM-FILE"
    assert result.stoppedReason is None


def test_a_decline_is_a_successful_run(tmp_path, settings) -> None:
    """`appetite_decline` normalizes, reports success and exits 0.

    The carrier refusing the risk on eligibility is the correct answer to a
    question asked properly, not a defect in the script.
    """
    result = run("decline_underscore.js", tmp_path, settings)

    assert result.outcome == "appetite-decline"
    assert result.reachedQuote is False
    assert result.success is True
    assert result.exitCode == 0
    assert result.stoppedReason == "Class code 8810 is outside appetite in TX"


def test_an_unrecognized_outcome_collapses_to_quote_when_one_was_reached(
    tmp_path, settings
) -> None:
    """The vocabulary is closed, so an unknown word never becomes a fourth outcome."""
    result = run("unknown_outcome.js", tmp_path, settings)

    assert result.outcome == "quote"
    assert result.reachedQuote is True
    assert result.exitCode == 0


def test_an_unrecognized_outcome_collapses_to_stuck_without_a_quote(
    tmp_path, settings
) -> None:
    """No quote reached and no known outcome leaves only `stuck`."""
    result = run("unknown_outcome_no_quote.js", tmp_path, settings)

    assert result.outcome == "stuck"
    assert result.success is False
    assert result.exitCode == 1


def test_a_crash_without_a_status_is_stuck_with_the_message(tmp_path, settings) -> None:
    """A script that throws before its handler runs leaves only the crash text."""
    result = run("crash.js", tmp_path, settings)

    assert result.outcome == "stuck"
    assert result.reachedQuote is False
    assert result.success is False
    assert result.exitCode == 1
    assert "locator #premium-total never became visible" in result.stoppedReason


def test_a_stale_status_file_does_not_survive_the_next_run(tmp_path, settings) -> None:
    """A previous run's quote must not be reported for a script that crashed."""
    script = tmp_path / "crash.js"
    shutil.copy(SCRIPTS / "crash.js", script)
    (tmp_path / "last-run.json").write_text(
        json.dumps({"outcome": "quote", "reachedQuote": True, "quoteNumber": "Q-STALE"})
    )

    result = validate(
        ValidationRequest(job_id="j", script_path=str(script), answers_path=str(ANSWERS)),
        settings,
    )

    assert result.outcome == "stuck"
    assert result.quoteNumber is None


def test_a_missing_script_raises(tmp_path, settings) -> None:
    """A script that does not exist is a broken request, not a stuck run."""
    request = ValidationRequest(
        job_id="j", script_path=str(tmp_path / "absent.js"), answers_path=str(ANSWERS)
    )
    with pytest.raises(FileNotFoundError):
        validate(request, settings)


# --- read_outcome and normalize_outcome in isolation ------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("quote", "quote"),
        ("stuck", "stuck"),
        ("appetite-decline", "appetite-decline"),
        ("appetite_decline", "appetite-decline"),
        ("appetite decline", "appetite-decline"),
        ("APPETITE_DECLINE", "appetite-decline"),
        ("  Quote  ", "quote"),
    ],
)
def test_known_outcomes_normalize(raw, expected) -> None:
    """Both decline spellings and any casing land on the closed vocabulary."""
    assert normalize_outcome(raw, reached_quote=False) == expected


@pytest.mark.parametrize("raw", ["referred", "", None, 7, {"outcome": "quote"}])
def test_unknown_outcomes_fall_back_to_reached_quote(raw) -> None:
    """Anything unrecognized is decided by whether a quote was reached."""
    assert normalize_outcome(raw, reached_quote=True) == "quote"
    assert normalize_outcome(raw, reached_quote=False) == "stuck"


def test_read_outcome_names_its_source(tmp_path) -> None:
    """The channel that answered is reported, not discarded."""
    script = tmp_path / "s.js"
    script.write_text("//")

    assert read_outcome(script, "", "") == (None, "none")

    stdout = 'noise\nRRSTATUS {"outcome":"quote","reachedQuote":true}\nmore noise'
    status, source = read_outcome(script, stdout, "")
    assert source == "RRSTATUS"
    assert status["outcome"] == "quote"

    (tmp_path / "last-run.json").write_text('{"outcome":"stuck","reachedQuote":false}')
    status, source = read_outcome(script, stdout, "")
    assert source == "last-run.json"
    assert status["outcome"] == "stuck"


def test_read_outcome_falls_back_to_log_text(tmp_path) -> None:
    """With no JSON on either channel, an outcome word in the logs is the last resort."""
    script = tmp_path / "s.js"
    script.write_text("//")

    status, source = read_outcome(script, "flow ended: appetite_decline for TX", "")

    assert source == "log-text"
    assert status["outcome"] == "appetite-decline"


def test_the_last_rrstatus_line_wins(tmp_path) -> None:
    """A retried run prints twice; the final line is the one that stands."""
    script = tmp_path / "s.js"
    script.write_text("//")
    stdout = (
        'RRSTATUS {"outcome":"stuck","reachedQuote":false}\n'
        'RRSTATUS {"outcome":"quote","reachedQuote":true}\n'
    )

    status, source = read_outcome(script, stdout, "")

    assert source == "RRSTATUS"
    assert status["outcome"] == "quote"


# --- static checks ----------------------------------------------------------


def test_a_clean_script_trips_nothing() -> None:
    """The negative control. Every check below must not fire here."""
    assert static_checks(SCRIPTS / "quote.js") == []


def test_manifest_filenames_must_match_the_runners_regex() -> None:
    """The runner materializes only `onboarding-*.(metadata|questions).json`."""
    warnings = static_checks(SCRIPTS / "bad" / "bad_manifest_names.js")

    assert any("onboarding-*.metadata.json" in w for w in warnings)
    assert any("onboarding-*.questions.json" in w for w in warnings)


def test_both_status_channels_are_required() -> None:
    """Omitting either means the runner guesses."""
    warnings = static_checks(SCRIPTS / "bad" / "no_status_output.js")

    assert any("RRSTATUS" in w for w in warnings)
    assert any("last-run.json" in w for w in warnings)


def test_a_script_writing_only_the_file_is_warned_about_the_missing_line() -> None:
    """One channel present, one absent: only the absent one is reported."""
    warnings = static_checks(SCRIPTS / "file_only.js")

    assert any("RRSTATUS" in w for w in warnings)
    assert not any("last-run.json" in w for w in warnings)


def test_human_behavior_on_a_frame_locator_is_flagged() -> None:
    """It crashes at runtime; it must be constructed on the Page."""
    warnings = static_checks(SCRIPTS / "bad" / "human_behavior_frame.js")

    assert any("HumanBehavior" in w for w in warnings)


def test_a_bare_login_lock_binding_is_flagged() -> None:
    """`acquireCarrierLoginLock()` returns `{release}`; the object is not callable."""
    warnings = static_checks(SCRIPTS / "bad" / "bare_login_lock.js")

    assert any("acquireCarrierLoginLock" in w for w in warnings)


def test_a_hardcoded_gate_answer_is_flagged() -> None:
    """`pickYesNo('No')` discards the client's real answer."""
    warnings = static_checks(SCRIPTS / "bad" / "hardcoded_gate.js")

    gate = [w for w in warnings if "hardcoded gate answer" in w]
    assert len(gate) == 1, "only the literal call is flagged, not the answers-driven one"
    assert "'No'" in gate[0]


def test_or_fallbacks_on_required_fields_are_flagged() -> None:
    """A missing answer fails the stage; it is never invented."""
    warnings = static_checks(SCRIPTS / "bad" / "required_fallback.js")

    fallbacks = [w for w in warnings if "`||` fallback" in w]
    assert len(fallbacks) == 2


def test_credentials_outside_the_four_contract_keys_are_flagged() -> None:
    """Any other key reads as empty and still gets typed."""
    warnings = static_checks(SCRIPTS / "bad" / "wrong_cred_keys.js")
    creds = [w for w in warnings if w.startswith("credentials:")]

    assert {"CARRIER_USERNAME", "CARRIER_PASSWORD", "OTP_SECRET"} == {
        w.split()[1] for w in creds
    }


def test_a_commented_out_violation_is_not_flagged(tmp_path) -> None:
    """Checks look for code, so a violation inside a comment does not count."""
    script = tmp_path / "commented.js"
    script.write_text(
        "const M = 'onboarding-x.metadata.json';\n"
        "const Q = 'onboarding-x.questions.json';\n"
        "// await pickYesNo('No');\n"
        "/* const release = await acquireCarrierLoginLock(); */\n"
        "fs.writeFileSync('last-run.json', s);\n"
        "console.log('RRSTATUS ' + s);\n"
    )

    assert static_checks(script) == []


def test_static_checks_on_a_missing_script_raise(tmp_path) -> None:
    """A missing script is a broken request, not a clean result."""
    with pytest.raises(FileNotFoundError):
        static_checks(tmp_path / "absent.js")


# --- the ledger -------------------------------------------------------------


def test_every_step_is_recorded_in_the_ledger(tmp_path, settings) -> None:
    """The run is invisible in per-agent accounting unless both steps land."""
    ledger = RunLedger(job_id="test-job")

    run("quote.js", tmp_path, settings, ledger=ledger)

    actions = [(s.agent, s.action) for s in ledger.steps]
    assert actions == [("validator", "static_checks"), ("validator", "validate")]
    assert ledger.by_agent()["validator"]["steps"] == 2
    assert ledger.total_usd() == 0.0, "the Validator makes no LLM calls"
    assert all(s.ok for s in ledger.steps)


def test_a_stuck_run_is_recorded_as_a_failed_step(tmp_path, settings) -> None:
    """`ok` follows success, so a crash shows up in the failure count.

    `crash.js` writes no status at all, so it also trips both status-output
    checks: two failed steps, one for the warnings and one for the run.
    """
    ledger = RunLedger(job_id="test-job")

    run("crash.js", tmp_path, settings, ledger=ledger)

    validate_step = next(s for s in ledger.steps if s.action == "validate")
    assert validate_step.ok is False
    assert ledger.by_agent()["validator"]["failed"] == 2


def test_a_decline_is_recorded_as_a_successful_step(tmp_path, settings) -> None:
    """A knockout must not inflate the failure count."""
    ledger = RunLedger(job_id="test-job")

    run("decline_underscore.js", tmp_path, settings, ledger=ledger)

    validate_step = next(s for s in ledger.steps if s.action == "validate")
    assert validate_step.ok is True
    assert ledger.by_agent()["validator"]["failed"] == 0


def test_static_check_warnings_do_not_stop_the_run(tmp_path, settings) -> None:
    """Section 6 keeps the flow draft rather than rejecting it."""
    ledger = RunLedger(job_id="test-job")

    result = run("file_only.js", tmp_path, settings, ledger=ledger)

    checks = next(s for s in ledger.steps if s.action == "static_checks")
    assert checks.ok is False, "the missing RRSTATUS line is a warning"
    assert result.outcome == "quote", "and the run happened anyway"


# --- invocation -------------------------------------------------------------


def test_the_config_file_is_passed_through(tmp_path, settings) -> None:
    """`--config <creds.json>` reaches the script and its four keys are read."""
    creds = tmp_path / "creds.json"
    creds.write_text(
        json.dumps(
            {
                "LOGIN_EMAIL": "ops@example.com",
                "LOGIN_PASSWORD": "s3cret",
                "MFA_CARRIER_ID": "pie",
                "HEADLESS": "true",
            }
        )
    )

    result = run("quote.js", tmp_path, settings, config_path=creds)

    assert result.outcome == "quote"


def test_a_replay_that_overruns_its_timeout_is_stuck(tmp_path, settings) -> None:
    """A hung portal must report, not block the crawl forever."""
    script = tmp_path / "hang.js"
    script.write_text("setTimeout(function () {}, 60000);\n")

    result = validate(
        ValidationRequest(job_id="j", script_path=str(script), answers_path=str(ANSWERS)),
        settings,
        timeout_s=2,
    )

    assert result.outcome == "stuck"
    assert "exceeded 2s" in result.stoppedReason
