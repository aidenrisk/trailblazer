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

  const nodes = Array.from(document.querySelectorAll(SELECTOR));
  return nodes
    .filter((el) => el.type !== 'hidden')
    .map((el, i) => {
      const tag = el.tagName.toLowerCase();
      const name = el.getAttribute('name') || '';
      const testid = el.getAttribute('data-testid') || '';
      const ariaLabel = el.getAttribute('aria-label') || '';
      const role = el.getAttribute('role') || '';
      const forLabel = labelText(el);
      const byLabelled = labelledByText(el);
      const accName = ariaLabel || byLabelled || forLabel || el.getAttribute('placeholder') || '';

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
        inputType: el.getAttribute('type') || '',
        role,
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
        candidates: candidates(el, name, testid, role, accName),
      };
    });
}
