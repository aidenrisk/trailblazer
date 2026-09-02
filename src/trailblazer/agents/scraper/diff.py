"""Compare the prior page description with the new one, and decide polarity.

Pure function over two contract objects: no browser, no model, so a settled page
is *always* recognised as settled. That matters because `-ve` is the condition
that gates replay generation.

Alignment is by `locator`, not `fieldId`: `fieldId` is a per-page counter and
does not survive a re-perceive.
"""

from trailblazer.contracts.page_description import Control, PageDescription, RevealedBy
from trailblazer.contracts.scraper_result import ScraperResult

# Properties whose change means the control is meaningfully different. `fieldId`
# is excluded because it is a counter, and `revealedBy` because the diff sets it.
_COMPARED = ("label", "type", "required", "options", "unique")


def _differs(a: Control, b: Control) -> bool:
    """True when two controls at the same locator describe different things."""
    return any(getattr(a, f) != getattr(b, f) for f in _COMPARED)


def diff_pages(
    new: PageDescription,
    prior: PageDescription | None,
    assignment: dict[str, str] | None = None,
) -> ScraperResult:
    """Diff `new` against `prior` and return the result Loop routes on.

    On a first perceive `prior` is `None`: every control is added and polarity is
    `+ve` by definition.

    `assignment` is the fieldId -> value mapping submitted just before this look.
    Controls that are new since `prior` are attributed to it, which is what makes
    `revealedBy` answerable at all -- only the scraper holds both sides.
    """
    if prior is None:
        added = [c.fieldId for c in new.controls]
        return ScraperResult(
            page=new, polarity="+ve", addedControls=added, removedControls=[], changedControls=[]
        )

    prior_by_loc = {c.locator: c for c in prior.controls}
    new_by_loc = {c.locator: c for c in new.controls}

    added = [c.fieldId for loc, c in new_by_loc.items() if loc not in prior_by_loc]
    removed = [c.fieldId for loc, c in prior_by_loc.items() if loc not in new_by_loc]
    changed = [
        c.fieldId
        for loc, c in new_by_loc.items()
        if loc in prior_by_loc and _differs(c, prior_by_loc[loc])
    ]

    if assignment:
        _attribute_reveals(new, prior_by_loc, assignment)

    polarity = "-ve" if not (added or removed or changed) else "+ve"
    return ScraperResult(
        page=new,
        polarity=polarity,
        addedControls=added,
        removedControls=removed,
        changedControls=changed,
    )


def _attribute_reveals(
    new: PageDescription, prior_by_loc: dict[str, Control], assignment: dict[str, str]
) -> None:
    """Set `revealedBy` on controls that appeared since the prior look.

    A single assignment is attributed unambiguously. With several, the cause is
    genuinely unknown from one comparison, so `revealedBy` is left `null` rather
    than guessed -- a wrong attribution would send Frontier down a branch that
    does not exist.
    """
    if len(assignment) != 1:
        return
    field_id, value = next(iter(assignment.items()))
    for control in new.controls:
        if control.locator not in prior_by_loc:
            control.revealedBy = RevealedBy(fieldId=field_id, equals=value)
