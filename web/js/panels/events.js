// J · Log: what happened, newest first (kept in the dashboard's memory).
// Lines from monerod's own log file carry a MONEROD LOG tag.
import { $, el, clear, setText } from "../dom.js";
import * as f from "../fmt.js";

const GLYPH = { info: "·", ok: "✓", warn: "▲", bad: "■" };
const KIND_GLYPH = { block: "◆", peer_in: "◂", peer_out: "▸", peer_gone: "×", reorg: "↺", restart: "↻", log: "≡" };
let top = null;   // the newest line on show, to find where the new ones end
let look = "";    // how times are written (12 h or 24 h): lines already on show are written again when it changes

// an event's own mark: the server numbers them, and a restarted server starts again at 1
const sig = (e) => (e ? `${e.id}|${e.t}|${e.kind}|${e.text}` : null);

function line(e) {
  const level = e.level || "info";
  const g = level === "info" ? KIND_GLYPH[e.kind] || GLYPH.info : GLYPH[level] || "·";
  return el("li", { "data-level": level },
    el("span", { class: "ev-t", text: f.clock(e.t) }),
    el("span", { class: "ev-g", text: g, "aria-hidden": "true" }),
    el("span", { class: "ev-x" }, e.kind === "log" ? el("span", { class: "ev-src", text: "MONEROD LOG" }) : null, e.text));
}

export function renderEvents(snap) {
  const list = Array.isArray(snap.events) ? snap.events : [];
  setText($("events-sub"), `NEWEST FIRST · ${f.plural(list.length, "LINE", "LINES")} · KEPT WHILE THE DASHBOARD RUNS`);
  const ol = $("events");
  if (!list.length) {
    if (top !== "") { top = ""; clear(ol).append(el("li", { class: "ev-empty", text: "LOG · EMPTY · NOTHING HAS HAPPENED YET" })); }
    return;
  }
  const was = look === f.clock(0) ? top : null;
  look = f.clock(0);
  top = sig(list[0]);
  // new lines go on top and the oldest fall off the bottom; the rest stay as they are,
  // so a line being read or selected doesn't move (when the list is scrolled down, it
  // stays where it was on screen)
  const k = was ? list.findIndex((e) => sig(e) === was) : -1;
  const keep = ol.scrollTop, before = ol.scrollHeight;
  if (k < 0 || ol.querySelector(".ev-empty")) {
    clear(ol).append(...list.map(line));
  } else if (k > 0) {
    ol.prepend(...list.slice(0, k).map(line));
  }
  while (ol.children.length > list.length) ol.lastElementChild.remove();
  if (keep > 0) ol.scrollTop = keep + (ol.scrollHeight - before);
}
