/**
 * Draw a numbered badge over each element the caller names, so a screenshot can
 * be talked about by number.
 *
 * The model is shown a picture and asked which badge it means. Nothing else in
 * the page identifies these elements -- that is why the vision pass ran -- so
 * the badge number is the only handle both sides share. Each badged element is
 * also stamped with `data-tb-badge`, which is how Python gets back to the exact
 * node afterwards to prove a proposed locator resolves to it.
 *
 * Called with the keys the extractor stamped (`data-tb-key`). Only elements on
 * screen are drawn, and numbers count drawn badges: an element that is missing,
 * hidden or outside the viewport takes no number. Returns one entry per badge
 * with the element's rectangle in image pixels and its tag.
 */
(keys) => {
  const overlayId = 'tb-badges';
  document.getElementById(overlayId)?.remove();
  document.querySelectorAll('[data-tb-badge]').forEach((n) => n.removeAttribute('data-tb-badge'));

  // Fixed to the viewport, which is what is photographed, so the badges and
  // the picture are one region whatever the screen size and whichever element
  // scrolls. An overlay sized from the document's scroll height was one screen
  // tall on a form that scrolls an inner panel, and badges were drawn to a
  // height the capture never reached.
  const overlay = document.createElement('div');
  overlay.id = overlayId;
  overlay.setAttribute('style', [
    'position:fixed', 'inset:0', 'z-index:2147483647', 'pointer-events:none',
  ].join(';'));
  document.body.appendChild(overlay);

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  };
  // The capture is scaled by the device pixel ratio, and the rectangles are
  // handed to the model beside it.
  const dpr = window.devicePixelRatio || 1;

  const out = [];
  keys.forEach((key) => {
    const el = document.querySelector(`[data-tb-key="${CSS.escape(key)}"]`);
    if (!el || !visible(el)) return;

    const n = out.length + 1;
    el.setAttribute('data-tb-badge', String(n));
    const r = el.getBoundingClientRect();

    // The outline sits over the element and the number beside it. Both are in
    // the fixed overlay rather than on the element, so no layout is disturbed:
    // a border on the element itself moves everything after it, and the
    // screenshot would then show a page the crawl never acted on.
    const box = document.createElement('div');
    box.setAttribute('style', [
      'position:absolute',
      `left:${r.left - 2}px`, `top:${r.top - 2}px`,
      `width:${r.width + 4}px`, `height:${r.height + 4}px`,
      'border:2px solid #e11d48', 'border-radius:3px',
    ].join(';'));
    overlay.appendChild(box);

    const tag = document.createElement('div');
    tag.textContent = String(n);
    tag.setAttribute('style', [
      'position:absolute',
      `left:${Math.max(0, r.left - 20)}px`, `top:${Math.max(0, r.top - 10)}px`,
      'background:#e11d48', 'color:#fff', 'font:bold 12px/16px system-ui',
      'padding:0 5px', 'border-radius:8px', 'min-width:16px', 'text-align:center',
    ].join(';'));
    overlay.appendChild(tag);

    out.push({
      badge: n,
      key,
      tag: el.tagName.toLowerCase(),
      inputType: el.getAttribute('type') || '',
      rect: { x: Math.round(r.left * dpr), y: Math.round(r.top * dpr), w: Math.round(r.width * dpr), h: Math.round(r.height * dpr) },
    });
  });
  return out;
}
