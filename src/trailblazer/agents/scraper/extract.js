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
  /**
   * An id a framework generated for this render, not a name the page gave the
   * field: a UUID, or React's `:r3:`. Pie's eligibility inputs carry a fresh
   * UUID per submission, so a locator built on one resolves on the crawl and
   * dies in the replay script the next day.
   */
  const generatedId = (id) =>
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) ||
    /^[:«][^:»]*[:»]$/.test(id) ||
    /^(mui|radix|headlessui|react-aria)[-_:]/i.test(id);

  /**
   * The question printed beside a control, when the page never linked the two.
   * Pie's eligibility inputs carry a generated id and nothing else: no `for=`,
   * no `aria-label`, no wrapping label, so `internal:label` has nothing to
   * match and the control came back with no address at all. The words are on
   * the screen and are unique, which is enough to build one.
   *
   * The address is checked here, in the page, against the element it was built
   * from: an xpath can resolve to exactly one node and still be the wrong node,
   * which is how an earlier structural guess produced confident wrong answers.
   * Only an address that finds this very element is offered.
   */
  const nearbyText = (el) => {
    let node = el.previousElementSibling;
    for (let i = 0; i < 3 && node; i++, node = node.previousElementSibling) {
      if (node.querySelector(SELECTOR)) break;
      const t = (node.innerText || '').trim().replace(/\s+/g, ' ');
      if (t.length >= 8 && t.length <= 160 && !t.includes('"')) return t;
    }
    // A wrapper holding this control and its caption, and nothing else.
    const box = el.parentElement;
    if (box && box.querySelectorAll(SELECTOR).length === 1) {
      const t = (box.innerText || '').trim().replace(/\s+/g, ' ');
      if (t.length >= 8 && t.length <= 160 && !t.includes('"')) return t;
    }
    return '';
  };

  const findsThisElement = (xpath, el) => {
    try {
      const r = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      return r.snapshotLength === 1 && r.snapshotItem(0) === el;
    } catch (e) {
      return false;
    }
  };

  const candidates = (el, name, testid, role, accName) => {
    const out = [];
    const tag = el.tagName.toLowerCase();
    if (el.id && !generatedId(el.id)) out.push(`#${esc(el.id)}`);
    if (testid) out.push(`[data-testid="${testid}"]`);
    if (name) out.push(`${tag}[name="${name}"]`);
    if (accName) {
      // Playwright-only engines; they resolve in Python, never here.
      out.push(`internal:label=${JSON.stringify(accName)}i`);
      if (role) out.push(`internal:role=${role}[name=${JSON.stringify(accName)}i]`);
    }
    if (!out.length) {
      const text = nearbyText(el);
      if (text) {
        const type = el.getAttribute('type');
        const kind = type ? `[@type="${type}"]` : '';
        for (const xp of [
          `//*[normalize-space(.)="${text}"][not(*)]/following::${tag}${kind}[1]`,
          `//*[normalize-space(.)="${text}"][not(*)]/ancestor::*[.//${tag}${kind}][1]//${tag}${kind}`,
        ]) {
          if (findsThisElement(xp, el)) { out.push(`xpath=${xp}`); break; }
        }
      }
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
        lead.setAttribute('data-tb-key', `el_${i}`);
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
      // The key, stamped on the node. The vision pass finds a control by key to
      // badge it, and only the element itself can carry that join: `key` is an
      // index into this payload and nothing in the DOM records it otherwise.
      el.setAttribute('data-tb-key', `el_${i}`);
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
      const direct = ariaLabel || byLabelled || forLabel || el.getAttribute('placeholder') || '';
      const accName = direct;

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
        // A chooser you can type into. Pie's class-code box carries the same
        // listbox role as its entity picker but is writable: suggestions appear
        // as you type and one must be picked. Opening it reads nothing, and the
        // crawl left it empty on every row.
        typeahead: isChooser(el) && tag === 'input' && !el.readOnly,
        required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
        // readonly is grouped with disabled -- both mean the value cannot be
        // typed -- unless the input is a chooser. A listbox or combobox renders
        // its text box read-only because the value is picked from a list, not
        // because it is locked: Pie's Legal Entity Type is `readonly` with
        // role="listbox", and the plain rule marked it disabled, so the one
        // gate this pipeline exists to walk was never touched.
        disabled:
          el.disabled === true ||
          el.getAttribute('aria-disabled') === 'true' ||
          (el.hasAttribute('readonly') && !isChooser(el)),
        visible: isVisible(el),
        options,
        candidates: candidates(el, name, testid, role, direct),
        error: '',
      };
    });

  markAdditionalRows(controls);

  /**
   * What the page is rejecting right now, tied to the field it is about.
   * Three sources, in order of certainty: the slot a control names in its own
   * aria-errormessage / aria-describedby; an error-styled or alert node inside
   * the control's own label or field wrapper; and, for a message that belongs to
   * no field -- a table's "select at least one term" -- the last control above
   * it inside the nearest ancestor that holds controls. What still matches
   * nothing is reported as a page error. Attribution is what lets Frontier
   * reopen the right field and hand the filler the text.
   */
  const controlEls = all.filter((el) => isVisible(el));
  const errorNodes = Array.from(document.querySelectorAll(
    '[role="alert"], [aria-live="assertive"], [aria-live="polite"], [class*="error" i], [class*="invalid" i], [id$="-error"], [id*="error" i]',
  )).filter((n) => isVisible(n) && !n.matches(SELECTOR) && !n.querySelector(SELECTOR));
  const messageOf = (n) => (n.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 200);
  const byEl = new Map(); // control element -> message
  const pageErrors = [];
  const controlOfEntry = (entry) => (entry.radioGroup ? groups.get(entry.radioGroup).lead : entry.el);
  const referencing = (id) => controlEls.find((c) => {
    const refs = `${c.getAttribute('aria-errormessage') || ''} ${c.getAttribute('aria-describedby') || ''}`;
    return id && refs.split(/\s+/).includes(id);
  });
  for (const node of errorNodes) {
    const text = messageOf(node);
    if (!text) continue;
    let owner = referencing(node.id);
    if (!owner) {
      const wrapper = node.closest('label, [class*="field" i], [class*="form-group" i], [class*="input" i]');
      const inside = wrapper ? Array.from(wrapper.querySelectorAll(SELECTOR)).filter(isVisible) : [];
      if (inside.length === 1) owner = inside[0];
    }
    if (!owner) {
      let scope = node.parentElement;
      for (let i = 0; i < 6 && scope && scope !== document.body; i++, scope = scope.parentElement) {
        const inScope = controlEls.filter((c) => scope.contains(c) && !c.contains(node));
        if (inScope.length) {
          const before = inScope.filter((c) => c.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
          owner = (before.length ? before : inScope)[before.length ? before.length - 1 : 0];
          break;
        }
      }
    }
    if (owner) {
      const prev = byEl.get(owner);
      byEl.set(owner, prev && prev !== text ? `${prev}; ${text}` : text);
    } else {
      pageErrors.push(text);
    }
  }
  entries.forEach((entry, i) => {
    const el = controlOfEntry(entry);
    const hit = byEl.get(el) || (entry.radioGroup
      ? groups.get(entry.radioGroup).members.map((m) => byEl.get(m)).find(Boolean)
      : undefined);
    if (hit) controls[i].error = hit;
  });

  /**
   * Dialogs over the page, by the role the page gives them -- the same three
   * attributes `wait_for_content` keys on. Each carries its clickables with
   * addresses *relative* to the dialog: Python prefixes the dialog's own
   * locator, so a "Close" inside it is never the page's own. A nameless icon
   * button still gets a positional address inside the dialog. `title` is the
   * fingerprint a later appearance is recognised by: heading, else first line.
   */
  const DIALOG_SELECTOR = '[role="dialog"], [role="alertdialog"], [aria-modal="true"]';
  const overlays = Array.from(document.querySelectorAll(DIALOG_SELECTOR))
    .filter(isVisible)
    .map((d, i) => {
      const heading = d.querySelector('h1, h2, h3, h4, h5, [role="heading"]');
      const lines = (d.innerText || '').split('\n').map((s) => s.trim()).filter(Boolean);
      const title = ((heading && heading.innerText.trim()) || lines[0] || '').slice(0, 80);
      const dialogCands = [];
      if (d.id && !/[«»]/.test(d.id)) dialogCands.push(`#${esc(d.id)}`);
      if (title) dialogCands.push(`[role="dialog"]:has-text(${JSON.stringify(title)})`);
      dialogCands.push('[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]');
      const hasControls = Array.from(d.querySelectorAll('input, select, textarea')).some(
        (e) => e.type !== 'hidden' && isVisible(e),
      );
      const clickables = Array.from(d.querySelectorAll('button, [role="button"], a[href]'))
        .filter((el) => isVisible(el) && !el.disabled)
        .map((el, j) => {
          const label = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '')
            .trim().replace(/\s+/g, ' ').slice(0, 60);
          const cands = [];
          if (el.id && !/[«»]/.test(el.id)) cands.push(`#${esc(el.id)}`);
          const testid = el.getAttribute('data-testid') || '';
          if (testid) cands.push(`[data-testid="${testid}"]`);
          if (label) cands.push(`role=button[name=${JSON.stringify(label)}]`);
          cands.push(`button, [role="button"], a[href] >> nth=${j}`);
          return { key: `ov_${i}_${j}`, label, candidates: cands };
        });
      return { key: `ov_${i}`, title, text: lines.join(' ').slice(0, 300), hasControls, candidates: dialogCands, clickables };
    });

  return { controls, actions, overlays, pageErrors };
}

// A repeated-row table -- class code, full-time, part-time, payroll, times
// three -- names its cells by prefix and row index: classCode0, fte-1,
// payroll-2. Row 0 is the record the form asks for; rows 1+ are "add another"
// entries. Pie's Next moved with row 0 alone and rejected three identical rows
// as "Duplicate class code", so filling the extra rows at all is wrong. A row
// counts as additional only when at least two different prefixes share its
// index and each has a row-0 sibling: "address2" beside "address1" is a second
// field, not a second record.
function markAdditionalRows(controls) {
  const parse = (s) => { const m = /^(.*?)[-_]?(\d+)$/.exec(s || ''); return m && m[1] ? { prefix: m[1], index: Number(m[2]) } : null; };
  const rows = new Map();
  const seen = new Set();
  for (const c of controls) {
    const key = parse(c.id) || parse(c.name);
    if (!key) continue;
    seen.add(key.prefix + '|' + key.index);
    if (!rows.has(key.index)) rows.set(key.index, new Set());
    rows.get(key.index).add(key.prefix);
  }
  for (const c of controls) {
    const key = parse(c.id) || parse(c.name);
    c.additionalRow = !!(key && key.index > 0 && rows.get(key.index).size >= 2 && seen.has(key.prefix + '|0'));
  }
}

// A control whose value is picked from a list rather than typed. Such an input
// is often read-only by construction and is still very much settable.
function isChooser(el) {
  const role = (el.getAttribute('role') || '').toLowerCase();
  return role === 'listbox' || role === 'combobox' ||
    el.hasAttribute('aria-haspopup') || el.hasAttribute('aria-expanded') ||
    el.hasAttribute('aria-controls');
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
