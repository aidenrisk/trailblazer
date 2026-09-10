"""What Frontier hands the form filler, and what the filler hands back.

One assignment is one action on one control. The filler never chooses the next
one: Frontier holds the board and decides, so a walk is reconstructible from
the assignment sequence alone.

`Restart` is the other thing Frontier can answer with. It is not an action and
never reaches the filler: it tells Loop to renavigate and replay the prefix so a
gate's owed side is taken against a clean page rather than a dirty one.

Field names are camelCase for the same reason as `page_description.py` -- the
wire format is the contract.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Intent = Literal["fill", "select", "check", "expand", "advance"]
"""What the filler is being asked to do.

`fill` types into a text-like control. `select` sets a choice, by clicking the
option's own locator or by `select_option(label)` against the parent. `check`
toggles a checkbox. `expand` opens a widget so its options can be read without
committing to one -- the answer to a dropdown whose choices are not in the DOM
until it is opened. `advance` clicks a link or button that moves the page on:
login submit, "Get a Quote", "Next".
"""


class Assignment(BaseModel):
    """One action, decided by Frontier, performed by the filler."""

    model_config = ConfigDict(populate_by_name=True)

    intent: Intent

    locator: str
    """Measured by the scraper. The filler does not construct or repair it."""

    fieldId: str | None = None

    step: str = ""
    """Ties every event about this one assignment together: the frontier's
    choice, the value chooser's question, the fill, its refusal, the vision
    look and the reopen. `grep step=s014` is one field's whole history in
    order, which a fieldId alone is not -- `q_002` repeats on every page and
    across every walk."""

    label: str = ""
    """The question this control asks, as the scraper cleaned it.

    Carried on the assignment because the filler cannot always read it off the
    page: Pie's eligibility percentages have no accessible name, the filler was
    handed `q_002` as the field's whole identity, and the value chooser answered
    four "what percentage of labor cost" questions with business names.
    """
    """The control acted on. `None` for `advance`, which targets an action."""

    value: str | None = None
    """Text for `fill`, option label for `select`. `None` for the rest.

    A credential is passed as the placeholder `$EMAIL` / `$PASSWORD` / `$OTP`;
    the filler resolves it from the session's credentials so the literal never
    enters an assignment, a report or a log.
    """

    optionLocator: str | None = None
    """The chosen option's own address, when it has one.

    Set for a radio, where each choice is a separate clickable input. Absent for
    a native `<select>`, whose choices are set by label against the parent.
    """

    options: list[dict[str, str | None]] | None = None
    """The choices of a select whose value is left to the filler, as `{label, locator}`.

    Frontier names the value only for a gate side. A five-option dropdown --
    Pie's Legal Entity Type -- is walked once with a value someone has to pick,
    and that is the filler's judgment like any text field's. The locator is
    what gets clicked once the label is chosen; `None` for a native select,
    which is set by label against the parent.
    """

    typeahead: bool = False
    """The control is typed into and then a suggestion is picked. The filler
    types the value, waits for the suggestions it raises, and clicks the one
    matching it; the committed value is what the control then holds."""

    constraintHint: str | None = None
    """What is known about the shape the field wants.

    Two sources. `Control.formatHint` is what the page states about itself --
    the placeholder, a `pattern`, a length or numeric bound -- and is available
    before the first attempt. A rejection replaces it with what the page
    actually complained about, which is the stronger evidence.
    """

    helpText: str | None = None
    """The field's tooltip text, from `Control.helpText`. The rule a portal
    states only behind a help icon, available before the first attempt."""

    shownBecause: str | None = None
    """The earlier question and answer that made this field appear, in words:
    `'Has the business had any claims?' was answered 'Yes'`. None for a field
    the page held from its first look.

    The chooser sees one field at a time. This is the piece of the rest of the
    page its answer has to agree with: a count revealed by a Yes cannot be 0.
    """


class Restart(BaseModel):
    """Frontier's request that Loop return the page to its pre-gate state.

    Returned instead of an Assignment when every field on the page has been
    attempted and a gate still has a side owed. Setting the gate back is not
    equivalent to never having set it: the abandoned branch's fields stay
    mounted and anything filled underneath them stays filled, so the owed side
    must be taken after a renavigation and a replay of the fills that preceded
    the branch point.

    Not an action. The filler never receives one -- Loop performs the
    renavigation itself and then asks Frontier for assignments again.
    """

    model_config = ConfigDict(populate_by_name=True)

    fieldId: str
    """The gate owing a side."""

    side: str
    """The value the gate is to be set to once the prefix is replayed.

    An option label, or `"true"`/`"false"` for a checkbox-shaped gate, matching
    `Assignment.value`.
    """

    walk: int
    """The walk id this restart opens. `Frontier.walk_seq` after the increment."""

    stageId: str
    """The stage the gate is on.

    A restart may target a gate on a page the walk has already left, in which
    case the replay crosses page boundaries to reach it. Loop replays the
    prefix up to this stage's branch point and no further.
    """


class FillReport(BaseModel):
    """What the filler did, and what the page said about it."""

    model_config = ConfigDict(populate_by_name=True)

    fieldId: str | None
    intent: Intent
    locator: str

    ok: bool
    """False when the action could not be completed. `blocked` says why."""

    valueUsed: str | None = None
    """What was actually entered, after any correction.

    Becomes `exampleValue` in the questions artifact, which is what lets a flow
    self-validate after persist. A credential appears here as its placeholder.
    """

    constraint: dict[str, str] | None = None
    """What is known about the shape the field wants, as `{unit, format, hint}`.
    Always present; `None` only when nothing at all is known.

    Populated on both paths. Up front, from what the page states about itself
    and what its help tooltip says, so a field answered correctly first time
    still records what was known. On rejection, with the page's complaint and
    the shape of the value that was finally accepted. `format` is a mask
    (`999999999`) recorded only for digit-shaped values, where a mask is a real
    rule; the shape of free text is noise.

    It reaches the questions artifact and from there the replay script, which
    shapes a different client answer to the mask or fails naming the field.
    """

    retried: bool = False
    """Whether a validation error was cleared within this assignment."""

    optionsRevealed: list[dict[str, str | None]] | None = None
    """Choices read from a widget opened by `expand`, as `{label, locator}`.

    A custom listbox mounts its options on click, so they do not exist in the
    DOM until then and the scraper reports `options: null`. Each option carries
    the locator that addresses it while the widget is open -- Pie's are
    `<button role="option">` in a popper with no id, so the address is the role
    plus the option's own text -- because setting a custom listbox means
    clicking the option, and a label alone cannot be clicked. `locator` is
    `None` when no unique address exists.
    """

    blocked: dict[str, str] | None = None
    """`{control, whatYouTried}` when the action failed. Reaches `metadata.blocked`."""
