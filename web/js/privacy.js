// Privacy mode, the window's side. With it on, the dashboard's server swaps every
// private detail (addresses, ports, names, folders, transaction ids) for a token like
// ⟦ip_1a2b3c4d⟧ before anything leaves it (memstara/privacy.py), so the real ones never
// reach this window. This file only decides how a token looks: a block of scrambled
// glyphs in the shape of what it hides (an address is four groups, a port five
// characters), with a colour split and now and then a short glitch. The same hidden
// detail always gets the same glyphs, so one peer can still be followed from screen to
// screen without anyone learning who it is, and an address inside a hidden range starts
// with that range's glyphs. Screen readers hear "hidden address", "hidden port" and so on.
//
// Tokens reach the screens in four ways and each is covered here: text set through
// dom.js or textContent (a span or SVG tspan per token), text added any other way (a
// watcher swaps it before it is drawn), attributes (aria-label and title get words or
// glyphs) and canvas text (fillText draws the glyphs). Turning the mode on or off
// reloads the window, so nothing read before the switch stays on screen.
import { onHiddenText } from "./dom.js";
import { api } from "./api.js";
import { askConfirm } from "./confirm.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const HAS = /[⟦⟧]/;
// a whole token; the start of one a shortened label cut off ("⟦ip_1a2b"); the end of one ("2b3c4d⟧")
const TOK = /⟦([a-z0-9]{1,6})_([0-9a-f]{8})⟧|⟦([a-z0-9]{0,6})(?:_([0-9a-f]{0,8}))?⟧?|([a-z0-9_]{0,15})⟧/gi;
export const REVEAL_WORD = "SHOW";
const ARRIVE_KEY = "memstara-privacy-arrive";

const WORDS = {
  ip: "hidden address", ip6: "hidden address", n16: "hidden range", n24: "hidden range", net: "hidden range",
  port: "hidden port", tor: "hidden onion address", i2p: "hidden I2P address", mac: "hidden hardware address",
  host: "hidden computer name", user: "hidden user name", name: "hidden name", login: "hidden login",
  srv: "hidden server name", id: "hidden peer ID", tx: "hidden transaction", hex: "hidden code", addr: "hidden wallet address",
};

// the browser's own textContent, kept before privacy mode puts its own in front of it
const TEXT = Object.getOwnPropertyDescriptor(Node.prototype, "textContent");

let on = false;
let motion = "full";

export const isPrivate = () => on;
export const hasHidden = (s) => typeof s === "string" && HAS.test(s);

// ---- glyphs ----------------------------------------------------------------

const BLOCKS = "█▓▒░▚▞▙▟▛▜▌▐▀▄▖▗▘▝";
const IS_BLOCK = new RegExp(`[${BLOCKS}]`, "g");

function strHash(s) {
  let x = 2166136261;
  for (let i = 0; i < s.length; i++) { x ^= s.charCodeAt(i); x = Math.imul(x, 16777619); }
  return x >>> 0;
}
function rng(seed) {
  let a = seed >>> 0 || 0x9e3779b9;
  return () => { a ^= a << 13; a >>>= 0; a ^= a >>> 17; a ^= a << 5; a >>>= 0; return a / 4294967296; };
}
const pick = (r) => BLOCKS[(r() * BLOCKS.length) | 0];
function run(r, n) { let s = ""; for (let i = 0; i < n; i++) s += pick(r); return s; }
const between = (r, lo, hi) => lo + ((r() * (hi - lo + 1)) | 0);
const groups = (r, n, lo, hi, sep) => Array.from({ length: n }, () => run(r, between(r, lo, hi))).join(sep);

// the shape of each kind of hidden detail, so it still reads as what it is
const SHAPE = {
  ip: (r) => groups(r, 4, 1, 3, "."),
  ip6: (r) => groups(r, between(r, 3, 5), 2, 4, ":"),
  n16: (r) => groups(r, 2, 1, 3, "."),
  n24: (r) => groups(r, 3, 1, 3, "."),
  net: (r) => groups(r, 2, 1, 3, "."),
  port: (r) => run(r, between(r, 4, 5)),
  tor: (r) => run(r, 10) + ".onion",
  i2p: (r) => run(r, 10) + ".i2p",
  mac: (r) => groups(r, 6, 2, 2, ":"),
  srv: (r) => run(r, between(r, 4, 8)),
  id: (r) => run(r, 10),
  tx: (r) => run(r, 12),
  hex: (r) => run(r, 12),
  addr: (r) => "4" + run(r, 11),
  host: (r) => run(r, between(r, 6, 10)),
  user: (r) => run(r, between(r, 4, 8)),
  name: (r) => run(r, between(r, 4, 8)),
  login: (r) => run(r, between(r, 4, 8)),
};

// which hidden range a hidden address is in, learned from the peer list (each peer
// carries its address and its ranges as tokens): the address then starts with the
// range's glyphs, so peers of one company look alike, as they would unhidden
const parent = new Map();   // "ip_1a2b3c4d" -> "n24_..." ; "n24_..." -> "n16_..."
const memo = new Map();     // "kind_hex" -> glyphs

const tokKey = (kind, hex) => `${kind}_${hex}`;

function glyphsOf(kind, hex) {
  const key = tokKey(kind, hex);
  let g = memo.get(key);
  if (g) return g;
  const r = rng(parseInt(hex.padEnd(8, "0"), 16) ^ strHash(kind));
  const up = parent.get(key);
  if (up && (kind === "ip" || kind === "n24")) {
    const [pk, ph] = up.split("_");
    // its range's glyphs, then its own last group (or two, under a /16)
    g = `${glyphsOf(pk, ph)}.${groups(r, kind === "ip" && pk === "n16" ? 2 : 1, 1, 3, ".")}`;
  } else {
    g = (SHAPE[kind] || ((x) => run(x, 8)))(r);
  }
  if (memo.size > 4000) memo.clear();
  memo.set(key, g);
  return g;
}

// each reading's peers say which hidden range each hidden address is in
const WHOLE = /^\u27e6([a-z0-9]{1,6})_([0-9a-f]{8})\u27e7$/i;
function tokOf(v) {
  const m = typeof v === "string" ? WHOLE.exec(v) : null;
  return m ? { kind: m[1].toLowerCase(), key: tokKey(m[1].toLowerCase(), m[2].toLowerCase()) } : null;
}
function link(child, up) {
  if (!child || !up || parent.get(child.key) === up.key) return;
  if (parent.size > 20000) parent.clear();
  parent.set(child.key, up.key);
  memo.clear();   // its glyphs, and those of the addresses under it, start with the range's now
}
export function learnHidden(snap) {
  const list = on && snap?.peers?.list;
  if (!Array.isArray(list)) return;
  for (const p of list) {
    const ip = tokOf(p?.ip), n24 = tokOf(p?.net24), n16 = tokOf(p?.net16);
    if (ip?.kind === "ip") link(ip, n24?.kind === "n24" ? n24 : n16?.kind === "n16" ? n16 : null);
    if (n24?.kind === "n24" && n16?.kind === "n16") link(n24, n16);
  }
}

// the same glyphs scrambled: the shape (length, dots, colons) kept, so nothing moves
function scramble(g, seed) {
  const r = rng(seed);
  return g.replace(IS_BLOCK, () => pick(r));
}

// one regex match -> {kind, hex, whole}; a cut-off piece gets glyphs as long as what's left of it
function parse(m) {
  if (m[1] !== undefined) return { kind: m[1].toLowerCase(), hex: m[2].toLowerCase(), whole: true, text: m[0] };
  const kind = (m[3] || "").toLowerCase();
  const hex = (m[4] || m[5] || "").toLowerCase().replace(/^.*_/, "");
  return { kind, hex, whole: false, text: m[0] };
}
function glyphsFor(t) {
  if (t.whole) return glyphsOf(t.kind, t.hex);
  const n = Math.max(1, t.text.length - 2);
  return run(rng(strHash(t.text.toLowerCase())), Math.min(n, 12));
}

// a string with its tokens as glyphs (inputs, canvas, tooltips' title attributes)
export function plain(s) {
  return hasHidden(s) ? String(s).replace(TOK, (...m) => glyphsFor(parse(m))) : s;
}
// ... and as words, for screen readers (aria-label)
export function spoken(s) {
  return hasHidden(s) ? String(s).replace(TOK, (...m) => WORDS[parse(m).kind] || "hidden") : s;
}

// "⟦n16_..⟧.0.0/16", "⟦n24_..⟧.0/24", "⟦ip_..⟧/32", "⟦ip_..⟧" (a ban's host) in plain
// words, as the panels write ranges: {text: "⟦n16_..⟧.x.x", size, range}; null when it isn't one
export function hiddenRange(host) {
  const s = String(host ?? "");
  if (!hasHidden(s)) return null;
  const m = /^(⟦([a-z0-9]{1,6})_[0-9a-f]{8}⟧)(.*)$/i.exec(s);
  if (!m) return { text: s, size: 1, range: false };
  const [, tok, kind, rest] = m;
  const k = kind.toLowerCase();
  if (k === "n16" && /^(\.0\.0\/16|\.x\.x)$/i.test(rest)) return { text: `${tok}.x.x`, size: 65536, range: true };
  if (k === "n24" && /^(\.0\/24|\.x)$/i.test(rest)) return { text: `${tok}.x`, size: 256, range: true };
  if ((k === "ip" || k === "ip6") && /^(\/32|\/128)?$/.test(rest)) return { text: tok, size: 1, range: false };
  const bits = /^\/(\d{1,3})$/.exec(rest);
  if (bits && k === "net") return { text: s, size: 2 ** Math.max(0, 32 - Number(bits[1])), range: true };
  return { text: s, size: 1, range: false };
}

// shortened to `n` characters as drawn (a token counts as its glyphs) without cutting a token
export function cut(s, n) {
  s = String(s ?? "");
  if (!hasHidden(s)) return s.length > n ? s.slice(0, Math.max(1, n - 1)) + "…" : s;
  if (plain(s).length <= n) return s;
  let out = "", len = 0, at = 0;
  for (const m of s.matchAll(TOK)) {
    const before = s.slice(at, m.index);
    if (len + before.length >= n - 1) return out + before.slice(0, Math.max(0, n - 1 - len)) + "…";
    out += before; len += before.length;
    const g = glyphsFor(parse(m));
    if (len + g.length > n - 1) return out + "…";
    out += m[0]; len += g.length;
    at = m.index + m[0].length;
  }
  const rest = s.slice(at);
  return out + rest.slice(0, Math.max(0, n - 1 - len)) + "…";
}

// ---- the DOM ---------------------------------------------------------------

// elements whose text can't hold a span: their tokens become glyphs as plain text
const PLAIN_TAGS = new Set(["option", "textarea", "title", "style", "script"]);

// `tail`: the rest of a range written after its token (".x", ".0/24"), kept on the same line
function span(t, tail) {
  const v = document.createElement("span");
  v.className = "rd-v rd-x";
  v.setAttribute("aria-hidden", "true");
  v.dataset.g = glyphsFor(t);
  v.__rd = t;
  const sr = document.createElement("span");
  sr.className = "sr-only";
  TEXT.set.call(sr, WORDS[t.kind] || "hidden");
  const o = document.createElement("span");
  o.className = "rd";
  o.dataset.k = t.kind || "x";
  o.append(v, sr);
  if (tail) o.append(tail);
  return o;
}
function tspan(t) {
  const e = document.createElementNS(SVG_NS, "tspan");
  e.setAttribute("class", "rd-t rd-x");
  TEXT.set.call(e, glyphsFor(t));
  e.__rd = t;
  return e;
}

const TAIL = /^(?:\.x\.x|\.x|\.0\.0\/16|\.0\/24|\/\d{1,3})(?![\w.])/;

// a string as text and glitch spans (or SVG tspans), ready to go into `parent`
export function render(s, parent) {
  const f = document.createDocumentFragment();
  const svgText = !!parent && parent.namespaceURI === SVG_NS;
  let at = 0;
  for (const m of s.matchAll(TOK)) {
    if (m.index < at) continue;
    if (m.index > at) f.append(s.slice(at, m.index));
    at = m.index + m[0].length;
    const tail = svgText ? null : TAIL.exec(s.slice(at))?.[0];
    f.append(svgText ? tspan(parse(m)) : span(parse(m), tail));
    if (tail) at += tail.length;
  }
  if (at < s.length) f.append(s.slice(at));
  return f;
}

// `node`'s text set to `s`, its tokens drawn as glitches. The string is kept on the node
// (__rdSrc), so dom.js's setText can tell nothing changed and leaves the glitch running.
function put(node, s) {
  node.__rdSrc = s;
  const tag = node.localName;
  if (node.namespaceURI === SVG_NS) {
    if (tag === "title" || tag === "desc") { TEXT.set.call(node, spoken(s)); return; }
  } else if (PLAIN_TAGS.has(tag)) { TEXT.set.call(node, plain(s)); return; }
  TEXT.set.call(node, "");
  node.append(render(s, node));
}

// text added some other way (a text node made by hand, its data changed): swapped
// before the next frame is drawn
function fixText(t) {
  if (!t.isConnected || !HAS.test(t.data)) return;
  const p = t.parentNode;
  if (!p || p.nodeType !== 1) return;
  const tag = p.localName;
  const svg = p.namespaceURI === SVG_NS;
  if (svg && (tag === "title" || tag === "desc")) { t.data = spoken(t.data); return; }
  if (!svg && PLAIN_TAGS.has(tag)) { t.data = plain(t.data); return; }
  const only = p.childNodes.length === 1;
  const s = t.data;
  p.replaceChild(render(s, p), t);
  if (only) p.__rdSrc = s;
}
function fixTree(n) {
  if (n.nodeType === 3) { fixText(n); return; }
  if (n.nodeType !== 1 || !HAS.test(TEXT.get.call(n) || "")) return;
  const walk = document.createTreeWalker(n, NodeFilter.SHOW_TEXT);
  const hits = [];
  for (let t = walk.nextNode(); t; t = walk.nextNode()) if (HAS.test(t.data)) hits.push(t);
  hits.forEach(fixText);
}

const WORD_ATTRS = /^(aria-label|aria-description|aria-valuetext|aria-roledescription|alt)$/i;
const GLYPH_ATTRS = /^(title|placeholder)$/i;

function patchDom() {
  // textContent: a token-bearing string becomes text and glitch spans
  Object.defineProperty(Node.prototype, "textContent", {
    configurable: true, enumerable: TEXT.enumerable, get: TEXT.get,
    set(v) {
      if (this.nodeType === 1 && typeof v === "string" && HAS.test(v)) { put(this, v); return; }
      if (this.__rdSrc !== undefined) this.__rdSrc = undefined;
      TEXT.set.call(this, v);
    },
  });
  // attributes people read or hear: words for screen readers, glyphs for tooltips
  const setAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (typeof value === "string" && HAS.test(value)) {
      if (WORD_ATTRS.test(name)) value = spoken(value);
      else if (GLYPH_ATTRS.test(name)) value = plain(value);
    }
    return setAttribute.call(this, name, value);
  };
  // dom.js's el() and append() hand their strings over here
  onHiddenText((parentNode, s) => (HAS.test(s) ? render(s, parentNode) : document.createTextNode(s)));
  // anything else, caught as it lands
  new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === "characterData") fixText(r.target);
      else for (const n of r.addedNodes) fixTree(n);
    }
  }).observe(document.documentElement, { childList: true, characterData: true, subtree: true });
}

// canvas text: the glyphs, scrambled now and then while the screens move
function patchCanvas() {
  const P = window.CanvasRenderingContext2D && CanvasRenderingContext2D.prototype;
  if (!P) return;
  for (const name of ["fillText", "strokeText", "measureText"]) {
    const orig = P[name];
    P[name] = function (text, ...rest) {
      return orig.call(this, typeof text === "string" && HAS.test(text) ? canvasText(text) : text, ...rest);
    };
  }
}
function canvasText(s) {
  const t = performance.now();
  const live = moving();
  return s.replace(TOK, (...m) => {
    const p = parse(m);
    const g = glyphsFor(p);
    if (!live) return g;
    // each hidden detail glitches for a quarter second every 8 to 13 s, at its own moment
    const seed = strHash(p.text.toLowerCase());
    const period = 8000 + (seed % 5000);
    if ((t + (seed >>> 13)) % period >= 260) return g;
    return scramble(g, seed ^ Math.floor(t / 65));
  });
}

// ---- the glitch -------------------------------------------------------------

const moving = () => on && motion === "full" && !document.hidden;
const TICK_MS = 120;
let ticker = 0;

// now and then one hidden detail on show tears and scrambles for a moment (about one
// every 9 s each, at most one start per tick, so a full peer list doesn't flicker)
function tick() {
  if (!moving()) return;
  const all = document.getElementsByClassName("rd-x");
  const n = all.length;
  if (!n || Math.random() > Math.min(1, (n * TICK_MS) / 9000)) return;
  for (let tries = 0; tries < 4; tries++) {
    const e = all[(Math.random() * n) | 0];
    if (e && !e.__rdBusy && e.getClientRects().length) { burst(e); return; }
  }
}
function burst(e) {
  const t = e.__rd;
  if (!t) return;
  const keep = glyphsFor(t);
  const svgT = e.namespaceURI === SVG_NS;
  const set = (g) => { if (svgT) TEXT.set.call(e, g); else e.dataset.g = g; };
  e.__rdBusy = true;
  e.classList.add("is-burst");
  let steps = 2 + ((Math.random() * 3) | 0);
  const step = () => {
    if (steps-- > 0 && e.isConnected) {
      set(scramble(keep, (Math.random() * 4294967296) >>> 0));
      setTimeout(step, 60 + Math.random() * 50);
      return;
    }
    set(keep);
    e.classList.remove("is-burst");
    e.__rdBusy = false;
  };
  step();
}

export function setPrivacyMotion(m) {
  motion = m || "full";
}

// ---- switching ---------------------------------------------------------------

// Called once at start, before anything is drawn: with privacy mode on, the patches go in.
export function startPrivacy(isOn) {
  on = isOn === true;
  document.documentElement.dataset.privacy = on ? "on" : "off";
  if (!on) return;
  patchDom();
  patchCanvas();
  clearInterval(ticker);
  ticker = setInterval(tick, TICK_MS);
}

// the screen tears and goes dark (leaving), or comes back out of it (arriving)
function flash(dir) {
  const box = document.getElementById("rd-flash");
  if (!box) return Promise.resolve();
  const still = motion !== "full" || matchMedia("(prefers-reduced-motion: reduce)").matches;
  box.hidden = false;
  box.dataset.go = dir;
  box.dataset.still = still ? "yes" : "no";
  box.replaceChildren();
  if (!still) {
    for (let i = 0; i < 16; i++) {
      const bar = document.createElement("i");
      bar.style.top = `${(Math.random() * 100).toFixed(1)}%`;
      bar.style.height = `${(2 + Math.random() * (i % 4 ? 18 : 70)).toFixed(0)}px`;
      bar.style.animationDelay = `${(Math.random() * (dir === "out" ? 0.45 : 0.5)).toFixed(2)}s`;
      bar.dataset.c = String(i % 3);
      box.append(bar);
    }
  }
  const ms = still ? (dir === "out" ? 60 : 0) : dir === "out" ? 640 : 760;
  return new Promise((resolve) => setTimeout(() => {
    if (dir === "in") { box.hidden = true; box.replaceChildren(); }
    resolve();
  }, ms));
}

function reloadAs(state) {
  try { sessionStorage.setItem(ARRIVE_KEY, state); } catch { /* storage blocked: no arriving glitch */ }
  location.reload();
}

// after a switch: the screens come out of the glitch
export function arrive() {
  let state = null;
  try { state = sessionStorage.getItem(ARRIVE_KEY); sessionStorage.removeItem(ARRIVE_KEY); } catch { /* blocked */ }
  if (state) flash("in");
}

// a reading says the mode isn't what this window was opened in (switched in another
// window): start again in the right one, before the reading is drawn
let reloading = false;
export function followPrivacy(snap) {
  const want = snap?.privacy === true;
  if (want === on) return false;
  if (!reloading) { reloading = true; flash("out").then(() => reloadAs(want ? "on" : "off")); }
  return true;
}

let busy = false;
function note(btn, text) {
  const w = btn.querySelector(".tb-word");
  if (!w) return;
  const was = w.textContent;
  w.textContent = text;
  setTimeout(() => { w.textContent = was; }, 4000);
}

async function turnOn(btn) {
  if (busy) return;
  busy = true;
  const going = flash("out");
  try {
    await api.privacy(true);
    await going;
    reloadAs("on");
  } catch (e) {
    await going;
    const box = document.getElementById("rd-flash");
    if (box) { box.hidden = true; box.replaceChildren(); }
    busy = false;
    note(btn, " DIDN'T SWITCH");
    btn.title = `Privacy mode didn't switch on: ${e.message}`;
  }
}

function turnOff() {
  askConfirm({
    title: "SHOW EVERYTHING AGAIN",
    body: "Addresses, ports, names, folders and transaction ids come back on every screen, and the clocks go back to your time zone. Check that nobody is watching or recording first.",
    word: REVEAL_WORD, action: "SHOW",
    run: async (word) => { await api.privacy(false, word); return "SHOWING EVERYTHING"; },
    done: () => { flash("out").then(() => reloadAs("off")); },
  });
}

// the PRIVACY switch in the top bar
export function initPrivacySwitch() {
  const btn = document.getElementById("privacy");
  if (!btn) return;
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  btn.title = on
    ? "Privacy mode is on: addresses, ports, names, folders and transaction ids are hidden. Click to show them again (you type SHOW to confirm)."
    : "Privacy mode: hides addresses, ports, names, folders and transaction ids, so the screens can be shown to other people";
  const w = btn.querySelector(".tb-word");
  if (w) w.textContent = on ? " PRIVACY ON" : " PRIVACY";
  btn.addEventListener("click", () => (on ? turnOff() : turnOn(btn)));
}
