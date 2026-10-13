// Small DOM helpers. All text goes in through textContent, never innerHTML,
// because peer addresses, alert texts and log lines come from outside.

const SVG_NS = "http://www.w3.org/2000/svg";

export function $(id) {
  return document.getElementById(id);
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  setAttrs(node, attrs);
  append(node, children);
  return node;
}

export function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  setAttrs(node, attrs);
  append(node, children);
  return node;
}

function setAttrs(node, attrs) {
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "text") node.textContent = String(v);
    else if (k === "class") node.setAttribute("class", v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
}

// privacy mode (privacy.js) turns a string with hidden details into text and glitch
// spans; otherwise a string is plain text
let textNode = (parent, s) => document.createTextNode(s);
export function onHiddenText(fn) {
  textNode = fn;
}

function append(node, children) {
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : textNode(node, String(c)));
  }
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  node.__rdSrc = undefined;
  return node;
}

// (in privacy mode a node showing hidden details remembers the string it was given in
// __rdSrc, so the same string again changes nothing and its glitch keeps running)
export function setText(node, value) {
  const s = value === null || value === undefined ? "—" : String(value);
  if (node && (node.__rdSrc ?? node.textContent) !== s) node.textContent = s;
}

// an attribute set only when it changed (charts that move a little every second)
export function setAttr(node, key, value) {
  const s = String(value);
  if (node && node.getAttribute(key) !== s) node.setAttribute(key, s);
}

export function setData(node, key, value) {
  if (!node) return;
  if (value === null || value === undefined) delete node.dataset[key];
  else if (node.dataset[key] !== String(value)) node.dataset[key] = String(value);
}

// A list of label and value rows (<dl> of <div><dt><dd>), written in place: rows that
// stay are only touched where their words or level changed, so nothing flickers, a
// selection survives and a screen reader isn't read the whole list again every second.
// rows: [[label, value, level?], ...]
export function syncKV(dl, rows) {
  if (!dl) return;
  rows.forEach(([k, v, level], i) => {
    let row = dl.children[i];
    if (!row) dl.append(row = el("div", {}, el("dt"), el("dd")));
    setText(row.children[0], k);
    setText(row.children[1], v);
    setData(row.children[1], "level", level ?? null);
  });
  while (dl.children.length > rows.length) dl.lastElementChild.remove();
}

// Charts redrawn on every snapshot (block tape, peer blips, mempool bars) have
// one Tab stop each: the arrow keys step along the items, Home and End jump to
// the ends. Each item carries data-key; a redraw puts focus back on the same
// item (or the one at the same place when it's gone), so focus isn't lost
// every few seconds.
export function focusedItem(host) {
  const a = document.activeElement;
  if (!a || a === host || !host.contains(a) || !a.hasAttribute("data-key")) return null;
  const tip = document.getElementById("tooltip");
  return { key: a.getAttribute("data-key"), index: [...host.querySelectorAll("[data-key]")].indexOf(a), tip: !!tip && !tip.hidden };
}

// Focus put back by a redraw rather than moved by the person: listeners that
// pop up a tooltip on focus leave it alone when it wasn't showing before.
export let refocusing = false;
export function refocus(node, quietly) {
  refocusing = !!quietly;
  try { node.focus({ preventScroll: true }); } finally { refocusing = false; }
}

export function rove(host, items, was, { start = "first" } = {}) {
  host.onkeydown = null;
  if (!items.length) return;
  let i = was ? items.findIndex((n) => n.getAttribute("data-key") === was.key) : -1;
  if (was && i < 0) i = Math.max(0, Math.min(items.length - 1, was.index));
  if (i < 0 && host.dataset.roveKey) i = items.findIndex((n) => n.getAttribute("data-key") === host.dataset.roveKey);
  if (i < 0) i = start === "last" ? items.length - 1 : 0;
  items.forEach((n, k) => n.setAttribute("tabindex", k === i ? "0" : "-1"));
  host.onkeydown = (e) => {
    const cur = items.indexOf(document.activeElement);
    if (cur < 0) return;
    const next = { ArrowRight: cur + 1, ArrowDown: cur + 1, ArrowLeft: cur - 1, ArrowUp: cur - 1, Home: 0, End: items.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    const n = Math.max(0, Math.min(items.length - 1, next));
    items[cur].setAttribute("tabindex", "-1");
    items[n].setAttribute("tabindex", "0");
    host.dataset.roveKey = items[n].getAttribute("data-key");
    items[n].focus();
  };
  if (was) refocus(items[i], !was.tip);
}

// Width of an element's content box, for sizing SVG charts to their slot.
export function widthOf(node, fallback = 600) {
  const w = node.getBoundingClientRect().width;
  return w > 0 ? Math.round(w) : fallback;
}

// A big figure with its unit in a smaller span ("65.7" + " kB/s"), so it fits
// on one line in narrow panels.
export function setFig(node, text) {
  const s = String(text ?? "—");
  const cut = s.lastIndexOf(" ");
  if (cut < 0) { delete node.dataset.fig; setText(node, s); return; }
  if (node.dataset.fig === s) return;
  node.dataset.fig = s;
  clear(node).append(s.slice(0, cut), el("span", { class: "fig-unit", text: s.slice(cut) }));
}
