// One shared tooltip. Content is built with textContent only.
import { $, el, clear, refocusing } from "./dom.js";

let tip;
let held = null;   // the attachTip target whose details are showing: {target, build, e, x, y}
const builders = new WeakMap();   // attachTip target -> its build function

function node() {
  if (!tip) tip = $("tooltip");
  return tip;
}

// rows: [{label, value, key?: "s1"|"s2"}] or [{note: "a sentence"}]
export function showTip(x, y, head, rows) {
  held = null;
  paint(x, y, head, rows);
}

function paint(x, y, head, rows) {
  const t = node();
  clear(t);
  if (head) t.append(el("div", { class: "tt-head", text: head }));
  for (const r of rows) {
    if (r.note) { t.append(el("div", { class: "tt-note", text: r.note })); continue; }
    const label = el("span", {}, r.key ? el("span", { class: `lg-line ${r.key}` }) : null, r.label);
    t.append(el("div", { class: "tt-row" }, label, el("b", { text: r.value })));
  }
  t.hidden = false;
  const pad = 14;
  const w = t.offsetWidth;
  const h = t.offsetHeight;
  let left = x + pad;
  let top = y + pad;
  if (left + w > window.innerWidth - 8) left = x - w - pad;
  if (top + h > window.innerHeight - 8) top = y - h - pad;
  t.style.left = Math.max(8, left) + "px";
  t.style.top = Math.max(8, top) + "px";
}

export function hideTip() {
  held = null;
  const t = node();
  if (t) t.hidden = true;
}

// Hover and keyboard focus show the same details.
export function attachTip(target, build) {
  builders.set(target, build);
  const open = (e, x, y) => {
    const c = build(e);
    if (!c) return;
    paint(x, y, c.head, c.rows);
    held = { target, build, e, x, y };
  };
  target.addEventListener("pointermove", (e) => open(e, e.clientX, e.clientY));
  target.addEventListener("pointerleave", hideTip);
  target.addEventListener("focus", () => {
    if (refocusing) return;   // a redraw put focus back; the tooltip wasn't open
    const r = target.getBoundingClientRect();
    open(null, r.left + r.width / 2, r.top + r.height / 2);
  });
  target.addEventListener("blur", hideTip);
}

// With each new reading: details on show from attachTip are filled in again where they
// are, so their numbers move with the screen's. Gone with their target.
// A chart drawn again from scratch (the block tape, the peer scope) has new targets:
// under a still pointer, the details become those of whatever is under it now. A
// keyboard user's item is focused again by the redraw, which opens its own details.
export function refreshTip() {
  const h = held;
  if (!h || node().hidden) return;
  if (!h.target.isConnected && h.e) {
    let at = document.elementFromPoint(h.x, h.y);
    while (at && !builders.has(at)) at = at.parentElement;
    if (at) { h.target = at; h.build = builders.get(at); }
  }
  if (!h.target.isConnected || !h.target.getClientRects().length) { hideTip(); return; }
  const c = h.build(h.e);
  if (c) paint(h.x, h.y, c.head, c.rows);
  else hideTip();
}
