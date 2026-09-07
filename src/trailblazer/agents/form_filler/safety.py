"""The no-pay/no-bind check, enforced in Python before any click is dispatched.

Purchasing or binding a policy is irreversible and external: unlike a corrupted
crawl, no later step can undo it. So the refusal is a function every click path
calls, not a line in a system prompt -- a prompt is an instruction to a model
that may ignore it, and the model is not the thing that dispatches the click.

Three signals, all read from the *resolved element*, never from the locator
string:

1. the accessible name, text, value and title, matched word-wise against
   `_DENIED_PHRASES`;
2. enclosure in a payment context -- an ancestor `form`/`section`/`dialog`/
   `fieldset` whose id, name, class, aria-label or test id reads as checkout,
   payment or billing;
3. a card-input relationship -- an `autocomplete` token in the `cc-*` family, or
   a card-number/CVV identifier, on any field inside the same form.

The known gap, recorded rather than papered over: a portal whose final
application submit *is* the bind, with no separate button, cannot be told apart
from an ordinary "Submit" by any of these signals. Section 5 of the spec makes
that a stop condition -- the crawl stops before the last button rather than
guessing -- and this module does not guess it either.
"""

import re

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator

from trailblazer.observability.logging import get_logger

log = get_logger(__name__)

# Matched as whole words against the element's own text, so "Payment" hits and
# "Repayment Terms" does not. Multi-word phrases match across any whitespace.
_DENIED_PHRASES = (
    "pay",
    "pay now",
    "paying",
    "payment",
    "payments",
    "confirm payment",
    "submit payment",
    "authorize payment",
    "bind",
    "binds",
    "binding",
    "bind policy",
    "bind coverage",
    "purchase",
    "buy",
    "buy now",
    "checkout",
    "check out",
    "place order",
    "complete purchase",
    "subscribe and pay",
    # Anything that could commit money or bind cover, however worded. The cost
    # of refusing a harmless button is one blocked report; the cost of pressing
    # a real one is an irreversible external action.
    "activate",
    "accept quote",
    "accept and",
    "agree and pay",
    "authorize",
    "card",
    "charge",
    "confirm and",
    "credit card",
    "debit",
    "deposit",
    "down payment",
    "enroll",
    "finalize",
    "invoice",
    "issue policy",
    "issue",
    "order",
    "pay by",
    "premium",
    "request to bind",
    "submit payment",
    "subscribe",
)

# Substring, not word-boundary: "Prepay", "Rebind" and "PaymentBtn" all carry a
# denied phrase glued to other text, and a boundary match would let them past.
# False positives cost one blocked report; a false negative is irreversible.
_DENY_RE = re.compile(
    "|".join(p.replace(" ", r"\s+") for p in _DENIED_PHRASES),
    re.IGNORECASE,
)

# An ancestor whose identity reads as a payment context. Substring rather than
# word, because these appear glued inside ids and class names: `paymentForm`,
# `js-checkout-step`.
_PAYMENT_CONTEXT_RE = re.compile(
    r"payment|checkout|billing|card-?details|credit-?card|purchase|braintree|stripe",
    re.IGNORECASE,
)

# The autocomplete tokens the HTML spec defines for card fields.
_CARD_AUTOCOMPLETE = (
    "cc-number",
    "cc-exp",
    "cc-exp-month",
    "cc-exp-year",
    "cc-csc",
    "cc-name",
    "cc-type",
)

_CARD_JS = """
(el, tokens) => {
  // A card relationship is a shared form: the element would be submitted
  // alongside the card field. With no enclosing form, section or dialog there
  // is nothing it is submitted with, and scanning the whole page instead
  // refused a body-level dropdown option because a payment form existed
  // elsewhere on the same page.
  const scope = el.closest('form, section, [role="form"], dialog');
  if (!scope) return null;
  const fields = Array.from(scope.querySelectorAll('input, select'));
  const hit = fields.find((i) => {
    const auto = (i.getAttribute('autocomplete') || '').toLowerCase().split(/\\s+/);
    if (tokens.some((t) => auto.includes(t))) return true;
    const ident = ((i.id || '') + ' ' + (i.name || '')).toLowerCase();
    return /card-?number|cardnum|cc-?num|cvv|cvc|security-?code/.test(ident);
  });
  return hit ? (hit.getAttribute('autocomplete') || hit.id || hit.name || 'card input') : null;
}
"""

_CONTEXT_JS = """
(el) => {
  for (let n = el.parentElement; n; n = n.parentElement) {
    const tag = n.tagName.toLowerCase();
    const isScope = tag === 'form' || tag === 'section' || tag === 'dialog'
      || tag === 'fieldset' || n.getAttribute('role') === 'form';
    if (!isScope) continue;
    const ident = [n.id, n.getAttribute('name'), n.className,
                   n.getAttribute('aria-label'), n.getAttribute('data-testid')]
      .filter(Boolean).join(' ');
    if (ident) return ident;
  }
  return null;
}
"""


def denial_reason(locator: Locator) -> str | None:
    """Why this element must not be clicked, or None when it is safe to click.

    The element is resolved and interrogated live; the locator string is never
    the evidence, because a harmless-looking selector can address a Pay button.

    A Playwright failure while checking is itself a denial. Treating "could not
    tell" as "safe" would make an unreadable element clickable, which is exactly
    backwards for an action that cannot be undone.
    """
    try:
        sources = (
            ("accessible name", (locator.get_attribute("aria-label") or "").strip()),
            ("text", (locator.inner_text() or "").strip()),
            ("value", (locator.get_attribute("value") or "").strip()),
            ("title", (locator.get_attribute("title") or "").strip()),
            ("name", (locator.get_attribute("name") or "").strip()),
        )
    except PlaywrightError as e:
        return f"could not read the element to check it against the denylist: {e}"

    for source, content in sources:
        hit = _DENY_RE.search(content)
        if hit:
            return f"{source} {content!r} contains the denied term {hit.group(0)!r}"

    try:
        context = locator.evaluate(_CONTEXT_JS)
    except PlaywrightError as e:
        return f"could not read the enclosing form to check it: {e}"
    if context and _PAYMENT_CONTEXT_RE.search(context):
        return f"the element sits inside a payment context ({context!r})"

    try:
        card = locator.evaluate(_CARD_JS, list(_CARD_AUTOCOMPLETE))
    except PlaywrightError as e:
        return f"could not read the enclosing form's fields to check it: {e}"
    if card:
        return f"the element's form collects card details ({card!r})"

    return None


def refuse_if_denied(locator: Locator, target: str) -> str | None:
    """`denial_reason`, logged at ERROR when it refuses. Returns the reason or None."""
    reason = denial_reason(locator)
    if reason is not None:
        log.error("refused to click %s: %s", target, reason)
    return reason
