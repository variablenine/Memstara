// The big animated piece at the top of each screen ("hero"). Each module's
// default export is create(body, env) and returns
//   { update(snap, now), show(), hide(), look(look) }
//   update: every snapshot, shown or not (keep state current, draw only if shown)
//   show / hide: the screen came into view / went away (start / stop animating)
//   look: {motion: "full" | "reduced" | "off", glow: bool, accent, head, ink: "r, g, b"} changed
// env: { sub, tools, now(), look(), goScreen(id), tip: {showTip, hideTip, attachTip} }
// One hero failing never stops the others or the panels. A hero only counts as
// shown while its screen is open AND it is on screen (scrolled past, or every
// screen stacked on one page, it rests until it comes back into view).
import { $, el, clear } from "../dom.js";
import { showTip, hideTip, attachTip } from "../tooltip.js";
import status from "./status.js";
import network from "./network.js";
import chain from "./chain.js";
import mempool from "./mempool.js";
import traffic from "./traffic.js";
import computer from "./computer.js";
import bans from "./bans.js";
import log from "./log.js";

const MODULES = { status, network, chain, mempool, traffic, computer, bans, log };
const heroes = new Map();   // key -> {api, shown}
const inView = new Map();   // key -> on screen (unknown counts as yes)
let lastShownFn = () => false;
const watcher = "IntersectionObserver" in window
  ? new IntersectionObserver((entries) => {
    for (const e of entries) inView.set(e.target.dataset.hero, e.isIntersecting);
    showHeroes(lastShownFn);
  }, { rootMargin: "120px 0px" })
  : null;

// a failing hero's warning is logged once (again only when it changes), not every second
const warned = new Map();
function guard(key, what, fn) {
  try {
    return fn();
  } catch (e) {
    const msg = `hero ${key} (${what}): ${e.message}`;
    if (warned.get(key) !== msg) { warned.set(key, msg); console.warn(msg); }
    return null;
  }
}

export function initHeroes({ now, look, goScreen }) {
  for (const [key, create] of Object.entries(MODULES)) {
    const body = $(`hero-${key}`);
    if (!body) continue;
    const env = {
      sub: $(`hero-${key}-sub`), tools: $(`hero-${key}-tools`), now, look, goScreen,
      tip: { showTip, hideTip, attachTip },
    };
    const api = guard(key, "create", () => create(body, env));
    if (!api) {
      clear(body).append(el("p", { class: "pcap", text: "This screen's picture didn't load. The panels below still work." }));
      continue;
    }
    heroes.set(key, { api, shown: false });
    const frame = body.closest("[data-hero]");
    if (watcher && frame) watcher.observe(frame);
  }
}

export function updateHeroes(snap, now) {
  for (const [key, h] of heroes) guard(key, "update", () => h.api.update(snap, now));
}

// shownFn(key) says whether that hero's screen is on show
export function showHeroes(shownFn) {
  lastShownFn = shownFn;
  for (const [key, h] of heroes) {
    const on = !document.hidden && shownFn(key) && inView.get(key) !== false;
    if (on === h.shown) continue;
    h.shown = on;
    guard(key, on ? "show" : "hide", () => (on ? h.api.show() : h.api.hide()));
  }
}

export function lookHeroes(lk) {
  for (const [key, h] of heroes) guard(key, "look", () => h.api.look(lk));
}
