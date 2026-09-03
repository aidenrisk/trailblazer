"""
Reading the page: what kind of control is this, and what can it be set to?

FormFiller is the only agent holding a live element, so it is the only one that
can answer those two questions. Frontier's Assignment says WHICH control;
everything about what that control actually is gets discovered here.

Nothing in this module writes to the page except `open_widget`, which has to
click a menu open to see inside it. Everything else looks and measures.
"""

import logging
import re

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from trailblazer.contracts import Control, Option

logger = logging.getLogger(__name__)

# How long to wait for a custom widget's list to render after it is opened.
# Long enough for an animated menu, short enough that a control which simply
# isn't a chooser doesn't stall the walk.
MENU_TIMEOUT_MS = 1_500

# Where an opened widget's choices tend to live. Checked in order; the first
# selector that matches anything wins.
OPTION_SELECTORS = (
    "[role=option]",
    "[role=listbox] li",
    "[role=menu] [role=menuitem]",
)

# Roles a control announces when its job is to choose from a list. `listbox` is
# here because that is what the live Pie page puts on its dropdowns — on the
# <input> itself, which is not what the ARIA spec has in mind but is what ships.
CHOOSER_ROLES = frozenset({"combobox", "listbox", "menu"})

# One round trip that answers everything needed to classify a control. Done in
# the page rather than as a dozen get_attribute() calls: each of those is a
# separate CDP message, and a control that changes between them would be
# described inconsistently.
_DESCRIBE_JS = r"""
el => {
  const tag = el.tagName.toLowerCase();
  const attr = name => (el.getAttribute(name) || "").trim();

  // Accessible name, cheapest source first. Deliberately NOT the element's own
  // text: a custom combobox renders its current value as its text, so reading
  // that back would label the field "Pie Direct" instead of "Agency / Program".
  let label = attr("aria-label");
  if (!label && attr("aria-labelledby")) {
    label = attr("aria-labelledby")
      .split(/\s+/)
      .map(id => (document.getElementById(id) || {}).textContent || "")
      .join(" ");
  }
  if (!label && el.id) {
    const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (l) label = l.textContent;
  }
  if (!label) {
    const l = el.closest("label");
    if (l) label = l.textContent;
  }
  if (!label) label = attr("name") || attr("placeholder");

  const options = tag === "select"
    ? Array.from(el.options).map(o => ({
        label: (o.label || o.textContent || "").trim(),
        value: o.value,
      }))
    : null;

  return {
    tag,
    type: attr("type").toLowerCase(),
    role: attr("role").toLowerCase(),
    hasPopup: attr("aria-haspopup"),
    contentEditable: el.isContentEditable === true,
    label: label.replace(/\s+/g, " ").trim(),
    required: el.required === true || attr("aria-required") === "true",
    placeholder: attr("placeholder"),
    pattern: attr("pattern"),
    maxLength: el.maxLength > 0 ? el.maxLength : null,
    readOnly: el.readOnly === true
      || el.hasAttribute("readonly")
      || attr("aria-readonly") === "true",
    disabled: el.disabled === true || attr("aria-disabled") === "true",
    options,
    heading: ((document.querySelector("h1, h2") || {}).textContent || "").trim(),
    title: document.title || "",
  };
}
"""


class ElementInfo:
    """What one live element turned out to be."""

    NATIVE_SELECT = "native_select"
    TOGGLE = "toggle"
    TEXT = "text"
    WIDGET = "widget"

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.tag: str = raw["tag"]
        self.input_type: str = raw["type"]
        self.role: str = raw["role"]
        self.label: str = raw["label"]
        self.required: bool = raw["required"]
        self.placeholder: str = raw["placeholder"]
        self.pattern: str = raw["pattern"]
        self.max_length: int | None = raw["maxLength"]
        self.read_only: bool = raw["readOnly"]
        self.disabled: bool = raw["disabled"]
        self.page_heading: str = raw["heading"] or raw["title"]
        self.select_options: list[dict] | None = raw["options"]

    @property
    def kind(self) -> str:
        """
        Which of the four ways this control has to be operated.

        Order matters. A <select> is knowable without touching it; a custom
        widget is only knowable by opening it; and a plain input must never be
        opened, because clicking a text box and then hunting for a menu is how
        it gets mistaken for a chooser.

        A readonly input counts as a widget, and that is not a guess. Nothing
        can type into one, so reading it as TEXT costs a full fill() timeout —
        Playwright waits for the element to become editable and it never does —
        and then reports the field as having refused a value, which sends
        Frontier past a chooser whose options were never even seen. Clicking it
        instead either finds options or reports [], and both are answers.
        """
        if self.tag == "select":
            return self.NATIVE_SELECT
        if self.tag == "input" and self.input_type in ("checkbox", "radio"):
            return self.TOGGLE
        if self.role in CHOOSER_ROLES or self.raw["hasPopup"]:
            return self.WIDGET
        if self.tag in ("input", "textarea"):
            return self.WIDGET if self.read_only else self.TEXT
        if self.raw["contentEditable"]:
            return self.TEXT
        return self.WIDGET

    @property
    def control_type(self) -> str:
        """The element mapped onto the contract's `Control.type` enum."""
        if self.input_type == "date":
            return "date"
        if self.input_type == "number":
            return "number"
        if self.kind in (self.NATIVE_SELECT, self.WIDGET):
            return "select"
        if self.kind == self.TOGGLE:
            return "toggle"
        if self.tag in ("input", "textarea"):
            return "text"
        return "other"

    def as_control(self, field_id: str, locator: str) -> Control:
        """
        The element expressed as a `Control`, so the value picker has one shape
        to work with whether it came from Scraper or from this live lookup.
        """
        return Control(
            fieldId=field_id,
            label=self.label or field_id,
            type=self.control_type,
            required=self.required,
            options=None,
            locator=locator,
            unique=True,
        )


def describe(target: Locator) -> ElementInfo:
    """Read one element. Raises PlaywrightError if it isn't there."""
    return ElementInfo(target.evaluate(_DESCRIBE_JS))


def resolve(page: Page, locator: str) -> tuple[Locator | None, str | None]:
    """
    Find the one element `locator` addresses.

    Returns (locator, None) on success, or (None, errorClass) when it matched
    nothing or matched several. Matching two elements is not a near miss:
    acting on `.first` would silently fill whichever happened to come first in
    the DOM, and no downstream check would catch it.
    """
    try:
        target = page.locator(locator)
        count = target.count()
    except PlaywrightError as e:
        logger.warning("locator %r is not usable: %s", locator, e)
        return None, "not_found"

    if count == 0:
        return None, "not_found"
    if count > 1:
        logger.warning("locator %r matches %d elements", locator, count)
        return None, "not_unique"
    return target, None


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"^(select|choose|please select|pick one|--)\b.*$|^-+$")


def looks_like_placeholder(label: str) -> bool:
    """
    True for the "Select..." entry a chooser opens on.

    Walking it would set the control back to nothing and read downstream as a
    branch that never existed.
    """
    return bool(_PLACEHOLDER.match(label.strip().lower()))


def _escape(text: str) -> str:
    return text.replace('"', '\\"')


def native_select_options(control_locator: str, info: ElementInfo) -> list[Option]:
    """
    A native <select>'s choices, each with its own locator.

    `:text-is()` rather than `:has-text()`: the latter is a substring match, so
    with both "Corporation" and "S Corporation" on the page the "Corporation"
    locator would match two options.
    """
    options: list[Option] = []
    for raw in info.select_options or []:
        label = raw["label"].strip()
        if not label or not raw["value"] or looks_like_placeholder(label):
            continue
        options.append(
            Option(
                label=label,
                locator=f'{control_locator} >> option:text-is("{_escape(label)}")',
            )
        )
    return options


def _option_locator(page: Page, element: Locator, label: str, index: int) -> str:
    """
    Address one discovered option, preferring a locator that survives a reopen.

    Measured, not assumed: every candidate is checked with count() == 1 before
    it is handed out. An option locator that matches two elements sends the
    filler to the wrong branch on a later set_option, and by then the page looks
    perfectly fine — nothing downstream would notice.
    """
    by_role = f'role=option[name="{_escape(label)}"]'
    try:
        if page.locator(by_role).count() == 1:
            return by_role
    except PlaywrightError:
        pass

    element_id = element.get_attribute("id")
    if element_id:
        by_id = f"#{element_id}"
        try:
            if page.locator(by_id).count() == 1:
                return by_id
        except PlaywrightError:
            pass

    # Last resort: position within the open menu. Fragile across reopens, but
    # better than handing back a locator that matches nothing at all.
    return f"{OPTION_SELECTORS[0]} >> nth={index}"


def read_open_options(page: Page) -> list[Option]:
    """
    Read the choices of a widget that is already open.

    An empty list is a real answer, not a failure: the caller reports `[]` to
    mean "opened it, it genuinely has none", which is what stops Frontier
    re-filling a zero-option chooser forever.
    """
    for selector in OPTION_SELECTORS:
        items = page.locator(selector)
        try:
            count = items.count()
        except PlaywrightError:
            continue
        if count == 0:
            continue

        options: list[Option] = []
        for i in range(count):
            item = items.nth(i)
            try:
                label = (item.inner_text() or "").strip()
            except PlaywrightError:
                continue
            if not label or looks_like_placeholder(label):
                continue
            options.append(
                Option(label=label, locator=_option_locator(page, item, label, i))
            )
        if options:
            return options
    return []


def open_widget(page: Page, target: Locator) -> list[Option]:
    """
    Click a custom widget open and read what appears.

    The wait is for the options to RENDER, not for the click to return: the
    whole point of a widget Scraper reported as `options: null` is that its list
    does not exist in the DOM until it is opened.
    """
    target.click()
    for selector in OPTION_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=MENU_TIMEOUT_MS, state="visible")
            break
        except PlaywrightError:
            continue
    return read_open_options(page)
