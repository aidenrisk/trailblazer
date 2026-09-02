"""Frontier's memory: one exploration record per control.

Frontier owns this — Loop does not pass it in or out. The models exist so the
board can be serialized for logging and debugging.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trailblazer.contracts.page_description import ControlType, Option, RevealedBy


class ControlState(BaseModel):
    """Frontier's exploration record for ONE control on the form.

    Frontier tracks every control, not just the branching ones. It does not need
    to know in advance which controls branch: it walks them in page order, and a
    control turns out to be a chooser either because the scraper reported
    `options` or because FormFiller discovered them while filling it.

    Attributes:
    - fieldId / label / stageId / locator / type / required: copied from the Control
    - explored: True once this control is completely done (filled, or all its
                options walked). Frontier will not move past a control that is
                not explored.
    - options: None = we do not know whether this control has options yet.
               []   = confirmed plain field, no options.
               [..] = the choices.
    - walked: options already tried, in the order tried. `walked[-1]` is what is
              currently set on the page.
    - pending: options still to try
    - revealedBy: the gate condition that makes this control exist, if any

    Walk strategy for a chooser:
      Discover:     options=[Male, Female], pending=[Male, Female], walked=[]
      Pick Male:    pending=[Female],       walked=[Male],           explored=False
      Pick Female:  pending=[],             walked=[Male, Female],   explored=True
    Only now may Frontier move to the next control.
    """

    model_config = ConfigDict(populate_by_name=True)

    fieldId: str
    label: str
    stageId: str
    locator: str
    type: ControlType
    required: bool = False
    explored: bool = False
    options: list[Option] | None = None
    walked: list[Option] = Field(default_factory=list)
    pending: list[Option] = Field(default_factory=list)
    revealedBy: RevealedBy | None = None
    """Copied from the Control, and populated by the scraper's diff.

    This is how an action gets attributed to a branch: a field that only exists
    when `q_gender == "Female"` belongs to the Female path and must be left out
    of the Male one.
    """


class FrontierBoard(BaseModel):
    """The state of the whole walk so far.

    Attributes:
    - controls: exploration record for every control seen, in the order first
                seen (page order, with revealed controls appended)
    - currentStageId: which page are we on right now?
    - status: high-level state machine
        * "exploring": absorbing feedback, deciding what is next
        * "awaiting_fill": an assignment was issued, waiting for FormFiller
        * "slice_stable": paths are ready to send to replay gen
        * "advancing": page fully explored, clicking Next
        * "backtracking": (v1) undoing a choice to try another option
        * "complete": entire form walked, paths published
        * "blocked": validation error or blocker prevents proceeding
    """

    model_config = ConfigDict(populate_by_name=True)

    controls: list[ControlState] = Field(default_factory=list)
    currentStageId: str
    status: Literal[
        "exploring",
        "awaiting_fill",
        "slice_stable",
        "advancing",
        "backtracking",
        "complete",
        "blocked",
    ]
