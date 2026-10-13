// Alerts strip: every active alert, problems first. Empty = "NONE RAISED".
// Each alert keeps its own row while it is up, and only the words that changed are
// written (a temperature, the "FOR n min"), so an address selected in a row stays
// selected long enough to copy it.
import { $, el, setText, setData } from "../dom.js";
import * as f from "../fmt.js";
import { addonTitle } from "../addons.js";

// alerts the core raises itself; anything else names the add-on it came from
const CORE_SOURCES = new Set(["dashboard", "rpc", "node", "local", "log"]);

const shown = new Map();   // alert key -> its row's parts
let order = "";            // the keys in list order, as last put in

const forText = (since, now) => (f.isNum(since) ? "FOR " + f.dur(now - since) : "");
// a part with words in it, or hidden
const put = (node, text) => { if (node.hidden !== !text) node.hidden = !text; if (text) setText(node, text); };

function makeRow() {
  const r = {
    tag: el("span", { class: "alert-tag" }),
    title: el("span", { class: "alert-title" }),
    text: el("span", { class: "alert-text" }),
    ips: el("span", { class: "alert-ips" }),
    span: el("span", { class: "alert-for" }),
    src: el("span", { class: "alert-src" }),
  };
  r.li = el("li", { class: "alert-row" }, r.tag, el("div", {}, r.title, r.text, r.ips), el("span", { class: "alert-since" }, r.span, r.src));
  return r;
}

export function renderAlerts(snap, now) {
  const alerts = (snap.alerts || []).slice().sort((a, b) =>
    (a.level === "bad" ? 0 : 1) - (b.level === "bad" ? 0 : 1) || (a.since || 0) - (b.since || 0));
  const bad = alerts.filter((a) => a.level === "bad").length;
  const warn = alerts.length - bad;
  setText($("alerts-tally"), alerts.length
    ? `PROBLEMS ${bad} · HEADS-UPS ${warn}`
    : "0 · NONE RAISED · NOTHING NEEDS YOU");

  const keys = [];
  for (const a of alerts) {
    let key = `${a.source || ""}:${a.id}`;
    while (keys.includes(key)) key += "+";   // the same alert twice: a row each
    keys.push(key);
    let r = shown.get(key);
    if (!r) shown.set(key, r = makeRow());
    setData(r.li, "level", a.level);
    setText(r.tag, a.level === "bad" ? "■ PROBLEM" : "▲ HEADS-UP");
    put(r.title, a.title);
    setText(r.text, a.text || "");
    put(r.ips, Array.isArray(a.ips) && a.ips.length ? "ADDRESSES · " + a.ips.join("  ") : "");
    setText(r.span, forText(a.since, now));
    put(r.src, a.source && !CORE_SOURCES.has(a.source) ? " · " + addonTitle(a.source).toUpperCase() : "");
  }
  for (const [key, r] of shown) if (!keys.includes(key)) { r.li.remove(); shown.delete(key); }
  // rows are moved only when an alert came, went or changed places
  const inOrder = keys.join("\n");
  if (inOrder !== order) {
    order = inOrder;
    $("alerts-list").append(...keys.map((k) => shown.get(k).li));
  }
}
