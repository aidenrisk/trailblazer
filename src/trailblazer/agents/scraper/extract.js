/**
 * In-page DOM extractor.
 *
 * Walks every form control and reports what actually exists in the DOM: tag,
 * type, identity attributes, associated label text, required/disabled state,
 * visibility, and `<option>` children for native selects.
 *
 * Radios sharing a `name` are one control, not one per choice: they answer one
 * question, and reporting them separately both loses the question and makes
 * their locators collide -- two questions with Yes/No produce four nodes whose
 * only distinguishing text is "Yes" or "No".
 *
 * It also proposes candidate locators in priority order but does NOT decide
 * which one is unique -- Playwright selector engines (`:has-text()`,
 * `internal:label`) do not exist in the page, so uniqueness is measured from
 * Python with `page.locator(sel).count()`.
 *
 * Also reports the page's clickable elements -- links, buttons, cards -- as
 * `actions`. A page that only advances (a dashboard, a business-type chooser)
 * has no fillable control, so without these its description is empty and there
 * is nothing for Frontier to choose between. The scraper reports them; it never
 * clicks one.
 *
 * Returns: { controls: RawControl[], actions: RawAction[] }
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
   * The nearest ancestor holding every member of the group and nothing of any
   * other group. It is what the group's own locator addresses; without it the
   * only candidate is the shared `name`, which matches every member.
   */
  const groupContainer = (lead, name, count) => {
    let n = lead.parentElement;
    for (let i = 0; i < 8 && n && n !== document.body; i++) {
      if (n.querySelectorAll(`input[name="${name}"]`).length === count) {
        if (n.id || n.getAttribute('role') === 'radiogroup' || n.tagName === 'FIELDSET') return n;
      }
      n = n.parentElement;
    }
    return null;
  };

  /**
   * Locators for one radio in a group, scoped by the group's `name` so that a
   * second question offering the same choice does not collide.
   */
  const radioOptionCandidates = (el, groupName, label) => {
    const out = [];
    if (el.id) out.push(`#${esc(el.id)}`);
    const v = el.getAttribute('value');
    if (v) out.push(`input[name="${groupName}"][value="${esc(v)}"]`);
    if (label) out.push(`label:has(input[name="${groupName}"]):has-text(${JSON.stringify(label)})`);
    return out;
  };

  const ACTION_SELECTOR = 'a[href], button, [role=button], [role=link], input[type=submit]';

  /** Clickable elements a page can be advanced by, with their own locators. */
  const actions = Array.from(document.querySelectorAll(ACTION_SELECTOR))
    .filter((el) => isVisible(el) && !el.disabled)
    .map((el, i) => {
      const tag = el.tagName.toLowerCase();
      // A row or card wrapped in <a> carries its whole contents as innerText.
      // Such an element is a record, not a control, and 30 of them bury the
      // handful of real navigation targets.
      const raw = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
      const text = raw.includes('\n') || raw.length > 80 ? '' : raw;
      const href = el.getAttribute('href') || '';
      const cands = [];
      if (el.id) cands.push(`#${esc(el.id)}`);
      const testid = el.getAttribute('data-testid') || '';
      if (testid) cands.push(`[data-testid="${testid}"]`);
      // An attribute *value* takes quote escaping, not CSS.escape, which is
      // for identifiers and turns "/search" into "\\/search" -- matching nothing.
      if (tag === 'a' && href) cands.push(`a[href=${JSON.stringify(href)}]`);
      if (text) cands.push(`${tag}:has-text(${JSON.stringify(text)})`);
      return { key: `ac_${i}`, tag, text, href, candidates: cands };
    })
    .filter((a) => a.text);

  const all = Array.from(document.querySelectorAll(SELECTOR)).filter((el) => el.type !== 'hidden');

  // One entry per radio group, keyed by name; every other control stands alone.
  const groups = new Map();
  const entries = [];
  for (const el of all) {
    const name = el.getAttribute('name') || '';
    if (el.type === 'radio' && name) {
      if (!groups.has(name)) {
        groups.set(name, { lead: el, members: [] });
        entries.push({ radioGroup: name });
      }
      groups.get(name).members.push(el);
    } else {
      entries.push({ el });
    }
  }

  const controls = entries
    .map((entry, i) => {
      if (entry.radioGroup) {
        const { lead, members } = groups.get(entry.radioGroup);
        const name = entry.radioGroup;
        // The group's question is the text above the choices, not any choice's
        // own label, so the fieldset legend and aria-labelledby come first.
        const fieldset = lead.closest('fieldset');
        const legend = fieldset ? fieldset.querySelector('legend') : null;
        const groupNode = lead.closest('[role=radiogroup]');
        const accName =
          labelledByText(groupNode || lead) ||
          (legend ? legend.innerText.trim() : '') ||
          (groupNode ? groupNode.getAttribute('aria-label') || '' : '') ||
          name;
        return {
          key: `el_${i}`,
          tag: 'input',
          inputType: 'radio',
          role: 'radiogroup',
          id: '',
          name,
          testid: '',
          ariaLabel: '',
          ariaLabelledbyText: labelledByText(groupNode || lead),
          labelText: legend ? legend.innerText.trim() : '',
          placeholder: '',
          accessibleName: accName,
          required:
            members.some((m) => m.hasAttribute('required')) ||
            (groupNode ? groupNode.getAttribute('aria-required') === 'true' : false),
          disabled: members.every((m) => m.disabled === true),
          visible: members.some((m) => isVisible(m)),
          options: members.map((m) => {
            const label = labelText(m) || m.getAttribute('aria-label') || m.value || '';
            return { label, candidates: radioOptionCandidates(m, name, label) };
          }),
          // The group's own address must resolve to one node, so a container
          // is preferred; the bare name matches every member and is the last
          // resort, reported non-unique rather than silently wrong.
          candidates: (() => {
            const box = groupNode || fieldset || groupContainer(lead, name, members.length);
            const out = [];
            if (box && box.id) out.push(`#${esc(box.id)}`);
            if (groupNode) out.push(`[role=radiogroup]:has(input[name="${name}"])`);
            if (fieldset) out.push(`fieldset:has(input[name="${name}"])`);
            if (box && box.tagName === 'DIV' && !box.id) {
              out.push(`div:has(> input[name="${name}"])`);
            }
            out.push(`input[name="${name}"]`);
            return out;
          })(),
        };
      }

      const el = entry.el;
      const tag = el.tagName.toLowerCase();
      // The help icon, if the field has one. Tagged so Python can hover it by
      // selector: it is a bare 16px svg with no name, id or test-id -- Pie's
      // are -- so nothing else can address it. The tooltip mounts only while
      // hovered, which is why this cannot be read from here.
      const helpTrigger = tagHelpTrigger(el, i);
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
        // What the page already says about the shape it wants. Read here rather
        // than discovered by being rejected: a date input carrying
        // placeholder="MM/DD/YYYY" states its format before the first attempt.
        formatHints: [
          el.getAttribute('placeholder'),
          el.getAttribute('pattern') ? `pattern ${el.getAttribute('pattern')}` : '',
          el.getAttribute('maxlength') ? `at most ${el.getAttribute('maxlength')} characters` : '',
          el.getAttribute('minlength') ? `at least ${el.getAttribute('minlength')} characters` : '',
          el.getAttribute('inputmode') ? `inputmode ${el.getAttribute('inputmode')}` : '',
          el.getAttribute('type') === 'number' || el.getAttribute('type') === 'range'
            ? [
                el.getAttribute('min') ? `min ${el.getAttribute('min')}` : '',
                el.getAttribute('max') ? `max ${el.getAttribute('max')}` : '',
                el.getAttribute('step') ? `step ${el.getAttribute('step')}` : '',
              ].filter(Boolean).join(', ')
            : '',
          el.getAttribute('title') || '',
          (el.getAttribute('aria-describedby') || '')
            .split(/\s+/)
            .map((id) => {
              const n = id && el.ownerDocument.getElementById(id);
              return n ? (n.textContent || '').trim() : '';
            })
            .filter(Boolean)
            .join(' '),
        ].filter(Boolean).join('; ').slice(0, 300),
        accessibleName: accName,
        helpTrigger,
        required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
        // readonly is grouped with disabled: both mean the value cannot be set,
        // and a click on either burns the filler's timeout before failing.
        disabled:
          el.disabled === true ||
          el.getAttribute('aria-disabled') === 'true' ||
          el.hasAttribute('readonly'),
        visible: isVisible(el),
        options,
        candidates: candidates(el, name, testid, role, accName),
      };
    });

  return { controls, actions };
}

function labelNodeOf(el) {
  const labelledBy = el.getAttribute('aria-labelledby');
  if (labelledBy) {
    const n = el.ownerDocument.getElementById(labelledBy.split(/\s+/)[0]);
    if (n) return n;
  }
  if (el.id) {
    const n = el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (n) return n;
  }
  return el.closest('label');
}

// True when `n` looks like a help affordance: a small icon-sized node in the
// label's row that is neither the control, nor inside it, nor the error icon.
function isHelpIcon(n, el) {
  if (n === el || el.contains(n) || n.contains(el)) return false;
  const r = n.getBoundingClientRect();
  if (!r.width || !r.height || r.width > 28 || r.height > 28) return false;
  const cls = (n.getAttribute('class') || '').toLowerCase();
  if (/error|invalid|danger|chevron|arrow|caret/.test(cls)) return false;
  const t = (n.textContent || '').trim();
  return t === '' || t.length <= 2;
}

// Tag the help icon beside `el`'s label with data-tb-help="<key>" and return
// true, or return false when the field has none.
//
// Scoped to the label's own container and no wider. The icon is a sibling of
// the label -- Pie renders `<span>Label</span><svg/>` in one row -- and one
// level further up reached the whole form on a flat layout, so every control
// found the page's single icon and the last one to look claimed it. An icon
// already claimed is never re-tagged, so no tooltip can be attributed twice.
function tagHelpTrigger(el, i) {
  const label = labelNodeOf(el);
  if (!label || !label.parentElement) return false;
  const row = label.parentElement;
  // A field's row holds one label and one control. A container holding more
  // is a section or the form itself, and searching it lets the first control
  // in document order claim an icon that belongs to a later field.
  if (row.querySelectorAll('label').length > 1) return false;
  if (row.querySelectorAll('input, select, textarea').length > 1) return false;
  // Any icon carrier qualifies -- svg, span, i, img, button -- because the
  // affordance's tag is a styling choice. `isHelpIcon` does the filtering.
  const cands = row.querySelectorAll(
    'svg, span, i, img, button, [role="button"], [aria-haspopup], [tabindex]');
  for (const n of cands) {
    if (n.hasAttribute('data-tb-help')) continue;
    if (isHelpIcon(n, el)) {
      n.setAttribute('data-tb-help', 'el_' + i);
      return true;
    }
  }
  return false;
}
