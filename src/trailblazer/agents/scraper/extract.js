/**
 * In-page DOM extractor.
 *
 * Walks every form control and reports what actually exists in the DOM: tag,
 * type, identity attributes, associated label text, required/disabled state,
 * visibility, and `<option>` children for native selects.
 *
 * It also proposes candidate locators in priority order but does NOT decide
 * which one is unique -- Playwright selector engines (`:has-text()`,
 * `internal:label`) do not exist in the page, so uniqueness is measured from
 * Python with `page.locator(sel).count()`.
 *
 * Returns: RawControl[]
 */
() => {
  const SELECTOR =
    'input, select, textarea, [role=combobox], [role=switch], [contenteditable=""], [contenteditable="true"]';

  /** CSS-escape a value for use in an attribute selector. */
  const esc = (v) => (window.CSS && CSS.escape ? CSS.escape(v) : v);

  /** Text of the <label> associated with `el`, by `for`, wrapping, or aria-labelledby. */
  const labelText = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${esc(el.id)}"]`);
      if (l) return l.innerText.trim();
    }
    const wrapping = el.closest('label');
    if (wrapping) return wrapping.innerText.trim();
    return '';
  };

  /** Concatenated text of the elements named by aria-labelledby. */
  const labelledByText = (el) => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    return ids
      .map((id) => {
        const n = document.getElementById(id);
        return n ? n.innerText.trim() : '';
      })
      .filter(Boolean)
      .join(' ');
  };

  /** Rendered, non-collapsed, and not hidden by an ancestor. */
  const isVisible = (el) => {
    if (el.hidden) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  /**
   * Candidate locators, most stable first. Python takes the first that
   * resolves to exactly one node.
   */
  const candidates = (el, name, testid, role, accName) => {
    const out = [];
    const tag = el.tagName.toLowerCase();
    if (el.id) out.push(`#${esc(el.id)}`);
    if (testid) out.push(`[data-testid="${testid}"]`);
    if (name) out.push(`${tag}[name="${name}"]`);
    if (accName) {
      // Playwright-only engines; they resolve in Python, never here.
      out.push(`internal:label=${JSON.stringify(accName)}i`);
      if (role) out.push(`internal:role=${role}[name=${JSON.stringify(accName)}i]`);
    }
    return out;
  };

  /**
   * What a login control asks for: 'username' | 'password' | 'otp' | null.
   *
   * Measured from the markup, never inferred from prose. Strong signals first
   * (input type, autocomplete tokens, unambiguous words in id/name/label). A
   * bare "email" is NOT a credential on its own -- an applicant's contact email
   * on a form page must never be filled with the agency's login -- so it only
   * counts when the page also carries a password or a code field.
   */
  const NON_TEXT_TYPES = new Set([
    'checkbox', 'radio', 'submit', 'button', 'reset', 'file', 'range', 'color', 'date',
    'datetime-local', 'month', 'week', 'time', 'image',
  ]);
  const OTP_WORDS =
    /\b(otp|one[\s_-]?time|verification[\s_-]?code|security[\s_-]?code|passcode|mfa|2fa|two[\s_-]?factor|authenticat(?:ion|or)[\s_-]?code|portal[\s_-]?authentication[\s_-]?token)\b/;
  const PASSWORD_WORDS = /\b(password|passwd|pwd)\b/;
  const USERNAME_WORDS =
    /\b(username|user[\s_-]?name|user[\s_-]?id|login[\s_-]?id|sign[\s_-]?in[\s_-]?name|agent[\s_-]?id|producer[\s_-]?id|broker[\s_-]?id)\b/;
  const EMAIL_WORDS = /\be-?mail\b/;

  const strongCredential = (el, tag, inputType, name, accName) => {
    if (tag !== 'input') return null;
    const t = (inputType || 'text').toLowerCase();
    if (NON_TEXT_TYPES.has(t)) return null;
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    const im = (el.getAttribute('inputmode') || '').toLowerCase();
    const hay = `${el.id} ${name} ${accName} ${el.getAttribute('placeholder') || ''}`.toLowerCase();
    if (ac.includes('one-time-code')) return 'otp';
    if (t === 'password' || ac.includes('current-password') || ac.includes('new-password')) return 'password';
    if (ac === 'username') return 'username';
    if (OTP_WORDS.test(hay)) return 'otp';
    if (/\bcode\b/.test(hay) && (im === 'numeric' || t === 'tel' || t === 'number')) return 'otp';
    if (PASSWORD_WORDS.test(hay)) return 'password';
    if (USERNAME_WORDS.test(hay)) return 'username';
    return null;
  };

  const weakUsername = (el, tag, inputType, name, accName) => {
    if (tag !== 'input') return false;
    const t = (inputType || 'text').toLowerCase();
    if (NON_TEXT_TYPES.has(t)) return false;
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    const hay = `${el.id} ${name} ${accName} ${el.getAttribute('placeholder') || ''}`.toLowerCase();
    return t === 'email' || ac === 'email' || EMAIL_WORDS.test(hay);
  };

  /**
   * A one-time code split across single-character boxes is ONE control, not
   * six. Report the first box, addressed so Playwright finds exactly it, and
   * skip its siblings; the filler types the code one box at a time.
   */
  const isDigitBox = (el) => {
    if (el.tagName.toLowerCase() !== 'input' || el.maxLength !== 1) return false;
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    const im = (el.getAttribute('inputmode') || '').toLowerCase();
    return im === 'numeric' || ['text', 'tel', 'number'].includes(t);
  };
  const digitGroupOf = (el) => {
    if (!isDigitBox(el) || !el.parentElement) return null;
    const boxes = Array.from(el.parentElement.children).filter(isDigitBox);
    return boxes.length >= 4 ? boxes : null;
  };
  const groupLabel = (el) => {
    const fs = el.closest('fieldset');
    const legend = fs && fs.querySelector('legend');
    if (legend) return legend.innerText.trim();
    const parent = el.parentElement;
    const byId = parent && parent.getAttribute('aria-labelledby');
    if (byId) {
      const n = document.getElementById(byId);
      if (n) return n.innerText.trim();
    }
    return '';
  };

  // Inputs that are buttons, not fields: a submit is the page's forward control
  // (found separately as `next`), never something to describe or fill.
  const CLICKABLE_INPUT_TYPES = new Set(['hidden', 'submit', 'button', 'reset', 'image']);

  const nodes = Array.from(document.querySelectorAll(SELECTOR));
  const skip = new Set();
  return nodes
    .filter((el) => !CLICKABLE_INPUT_TYPES.has((el.type || '').toLowerCase()))
    .map((el, i) => {
      if (skip.has(el)) return null;
      const group = digitGroupOf(el);
      if (group) group.slice(1).forEach((b) => skip.add(b));
      const tag = el.tagName.toLowerCase();
      const name = el.getAttribute('name') || '';
      const testid = el.getAttribute('data-testid') || '';
      const ariaLabel = el.getAttribute('aria-label') || '';
      const role = el.getAttribute('role') || '';
      const forLabel = labelText(el);
      const byLabelled = labelledByText(el);
      const ownName = ariaLabel || byLabelled || forLabel || el.getAttribute('placeholder') || '';
      // A digit group is named for the group, not for "Digit 1".
      const accName = group ? groupLabel(el) || ownName || 'Verification code' : ownName;
      const inputType = el.getAttribute('type') || '';
      const credential = group
        ? 'otp'
        : strongCredential(el, tag, inputType, name, accName);
      const cands = candidates(el, name, testid, role, ownName);
      // The first box of a digit group rarely has an id; a positional
      // Playwright address still resolves to exactly it.
      if (group) cands.push('input[maxlength="1"] >> nth=0');

      // Only native <select> exposes its choices without interaction. A custom
      // widget mounts its listbox into a portal on open, so there is nothing to
      // read: it gets options: null and never becomes a candidate gate.
      // An <option> is set by label against the select's own locator, never
      // clicked, so each choice carries locator: null. A choice that IS its own
      // clickable node -- a radio -- gets a measured locator in Python.
      const options =
        tag === 'select'
          ? Array.from(el.options)
              .map((o) => o.text.trim())
              .filter(Boolean)
              .map((label) => ({ label, locator: null }))
          : null;

      return {
        key: `el_${i}`,
        tag,
        inputType,
        role,
        credential,
        weakUsername: !credential && weakUsername(el, tag, inputType, name, accName),
        otpBoxes: group ? group.length : 0,
        id: el.id || '',
        name,
        testid,
        ariaLabel,
        ariaLabelledbyText: byLabelled,
        labelText: forLabel,
        placeholder: el.getAttribute('placeholder') || '',
        accessibleName: accName,
        required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
        disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
        visible: isVisible(el),
        options,
        candidates: cands,
      };
    })
    .filter(Boolean)
    .map((c, _i, all) => {
      // Bare "email" becomes a username only on a page that is visibly a login:
      // one that also asks for a password or a one-time code.
      if (c.weakUsername && all.some((o) => o.credential === 'password' || o.credential === 'otp'))
        c.credential = 'username';
      delete c.weakUsername;
      return c;
    });
}
