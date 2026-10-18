// L · Black marble watch: what a new transaction's 16-member ring works like if one sender
// made the recent surge of new outputs, and the last 30 days of outputs against a usual day.
// The numbers come worked out from the server (memstara/flood.py, snapshot.chain.flood).
import { $, el, svg, clear, setText, setData, syncKV, widthOf, focusedItem, rove } from "../dom.js";
import { attachTip } from "../tooltip.js";
import * as f from "../fmt.js";

const RING = 16;
let limits = { warn: 11, bad: 8, min: 30 };
let last = null;          // the snapshot drawn last
let tableKey = "";

const WHY = {
  starting: "WAITING FOR THE FIRST READING",
  sync: "WAITS UNTIL YOUR NODE IS SYNCED",
  bootstrap: "WAITS UNTIL YOUR NODE IS SYNCED",
  short: "THE CHAIN HAS LESS THAN A YEAR OF BLOCKS",
};

export function setFloodSettings(v) {
  limits = {
    warn: v?.["thresholds.flood_warn"] ?? 11,
    bad: v?.["thresholds.flood_bad"] ?? 8,
    min: v?.["grace.flood_min"] ?? 30,
  };
}

export function initFlood() {
  attachTip($("flood-dots"), () => {
    const fl = last?.chain?.flood;
    const rows = [{ note: "Each dot is one member of a new transaction's ring: the coin really being spent (the first) and 15 decoys a wallet picks. Hollow dots are the decoys that would come from the surge of outputs above a usual day, which one sender could rule out if they made them all." }];
    if (fl?.state === "ok") rows.unshift({ label: "DECOYS FROM THE SURGE", value: f.pct(fl.share * 100, 0) });
    return { head: "RING MEMBERS", rows };
  });
  window.addEventListener("resize", () => last && drawDays(last.chain?.flood));
  $("flood-table").closest("details").addEventListener("toggle", () => last && fillTable(last.chain?.flood));
}

export function renderFlood(snap) {
  last = snap;
  const fl = snap.chain?.flood || { state: "off", why: "starting" };
  const answering = snap.source?.answering !== false;
  const alert = (snap.alerts || []).find((a) => a && a.id === "flood" && a.source === "dashboard");
  const level = alert ? alert.level : null;
  const ok = fl.state === "ok";

  // the figure: about how many members, the dots, or why there's nothing yet
  const fig = $("flood-fig");
  setData(fig, "level", ok && answering ? level : null);
  const meter = $("flood-meter");
  meter.hidden = fl.state !== "filling";
  if (fl.state === "filling") meter.firstElementChild.style.width = Math.round((fl.progress || 0) * 100) + "%";
  const value = $("flood-rings");
  if (ok) {
    const n = Math.round(fl.rings);
    if (value.dataset.n !== String(n)) {
      value.dataset.n = String(n);
      clear(value).append(`≈ ${n}`, el("span", { class: "fig-unit", text: ` OF ${RING}` }));
    }
  } else {
    delete value.dataset.n;
    setText(value, fl.state === "filling" ? "READING" : "—");
  }
  let note;
  if (!answering) note = "NODE NOT ANSWERING · LAST READING";
  else if (ok && level) note = f.flag(level, `${level === "bad" ? "PROBLEM" : "HEADS-UP"} · IF ONE SENDER MADE THE SURGE`);
  else if (ok) note = fl.rings >= RING - 0.5 ? "NOTHING ABOVE A USUAL DAY" : "IF ONE SENDER MADE THE SURGE";
  else if (fl.state === "filling") note = `READING A YEAR OF BLOCKS · ${f.pct((fl.progress || 0) * 100, 0)}`;
  else if (fl.state === "error") note = `COULDN'T READ THE OUTPUT COUNTS${fl.why ? " · " + String(fl.why).toUpperCase() : ""}`;
  else note = WHY[fl.why] || "—";
  setText($("flood-note"), note);
  drawDots(ok ? fl.rings : null);

  // the numbers
  const rows = [];
  if (ok) {
    rows.push(["DECOYS FROM THE SURGE", f.pct(fl.share * 100, 0), level]);
    rows.push(["NEW OUTPUTS · LAST 24 H", `${f.num(fl.last_day)}${f.isNum(fl.volume_x) ? ` · ${times(fl.volume_x)} USUAL` : ""}`]);
    rows.push(["A USUAL DAY", `${f.num(fl.usual_day)} · MIDDLE OF 180 DAYS`]);
    rows.push(["READ UP TO", `BLOCK ${f.num(fl.height)}`]);
  } else {
    for (const k of ["DECOYS FROM THE SURGE", "NEW OUTPUTS · LAST 24 H", "A USUAL DAY", "READ UP TO"]) rows.push([k, "—"]);
  }
  rows.push(["LIMITS", `▲ UNDER ${limits.warn} · ■ UNDER ${limits.bad} · FOR ${limits.min} MIN`]);
  syncKV($("flood-kv"), rows);
  setText($("flood-sub"), "FROM YOUR NODE · OUTPUTS IN EACH BLOCK OF THE LAST YEAR · AGAIN WITH EACH NEW BLOCK");

  drawDays(ok ? fl : null);
  fillTable(ok ? fl : null);
}

// 2.6× (one decimal under 10)
function times(x) {
  return `${x < 10 ? x.toFixed(1) : Math.round(x)}×`;
}

function drawDots(rings) {
  const dots = $("flood-dots").children;
  // the first is the real coin; then the decoys not from the surge; the rest hollow
  const kept = rings === null ? RING - 1 : Math.max(0, Math.min(RING - 1, Math.round(rings) - 1));
  for (let i = 1; i < dots.length; i++) dots[i].classList.toggle("rd-surge", rings !== null && i > kept);
  $("flood-dots").classList.toggle("rd-none", rings === null);
  $("flood-dots").setAttribute("aria-label", rings === null ? "Ring members: not worked out yet"
    : `Ring members: the real coin, ${kept} decoys from a usual day's outputs and ${RING - 1 - kept} from the surge`);
}

// the last 30 days of new outputs: the part up to a usual day and the part above it
function drawDays(fl) {
  const host = $("flood-chart");
  const width = widthOf(host.parentElement, 420);
  const height = 120;
  const m = { l: 4, r: 52, t: 10, b: 16 };
  const iw = width - m.l - m.r, ih = height - m.t - m.b;
  const was = focusedItem(host);
  clear(host);
  host.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const days = fl && Array.isArray(fl.days) ? fl.days : [];
  if (!days.length || !f.isNum(fl.usual_day) || fl.usual_day <= 0) {
    host.append(svg("text", { x: width / 2, y: height / 2, "text-anchor": "middle", text: "WAITING FOR A YEAR OF BLOCKS" }));
    rove(host, [], null);
    return;
  }
  const usual = fl.usual_day;
  const top = Math.max(usual * 1.5, ...days);
  const y = (v) => m.t + ih - (v / top) * ih;
  const slot = iw / days.length;
  const bw = Math.max(2, slot * 0.62);
  host.append(svg("line", { class: "f-axis", x1: m.l, x2: m.l + iw, y1: m.t + ih + 0.5, y2: m.t + ih + 0.5 }));
  const uy = Math.round(y(usual)) + 0.5;
  host.append(svg("line", { class: "f-usual", x1: m.l, x2: m.l + iw, y1: uy, y2: uy }));
  host.append(svg("text", { x: m.l + iw + 6, y: uy + 3, text: "USUAL" }));
  host.append(svg("text", { x: m.l + iw + 6, y: m.t + 7, text: f.num(Math.round(top)) }));
  host.append(svg("text", { x: m.l, y: height - 3, text: "30 DAYS AGO" }));
  host.append(svg("text", { x: m.l + iw, y: height - 3, "text-anchor": "end", text: "LAST 24 H" }));
  const hits = [];
  days.forEach((n, i) => {
    const x = m.l + i * slot + (slot - bw) / 2;
    const ago = days.length - 1 - i;
    const hit = svg("rect", {
      class: "f-hit", x: m.l + i * slot, y: m.t, width: slot, height: ih, tabindex: -1, role: "img", "data-key": String(ago),
      "aria-label": `${agoText(ago)} · ${f.num(n)} NEW OUTPUTS · ${times(n / usual)} A USUAL DAY`,
    });
    const low = Math.min(n, usual);
    const base = svg("rect", { class: "f-bar", x, y: y(low), width: bw, height: Math.max(0, m.t + ih - y(low)) });
    hits.push(hit);
    host.append(hit, base);
    if (n > usual) host.append(svg("rect", { class: "f-over", x, y: y(n), width: bw, height: Math.max(0, y(usual) - y(n)) }));
    attachTip(hit, () => ({ head: agoText(ago), rows: [
      { label: "NEW OUTPUTS", value: f.num(n) },
      { label: "A USUAL DAY", value: f.num(usual) },
      { label: "AGAINST IT", value: `${times(n / usual)}` },
    ] }));
  });
  rove(host, hits, was, { start: "last" });
}

function agoText(ago) {
  return ago === 0 ? "LAST 24 H" : ago === 1 ? "24 TO 48 H AGO" : `${ago} TO ${ago + 1} DAYS AGO`;
}

// the table view: filled only while it is open, and again only when the days change
function fillTable(fl) {
  const table = $("flood-table");
  if (!table.closest("details").open) { tableKey = ""; return; }
  const days = fl && Array.isArray(fl.days) ? fl.days : [];
  const key = JSON.stringify([days, fl?.usual_day]);
  if (key === tableKey) return;
  tableKey = key;
  const tb = clear(table.tBodies[0]);
  days.slice().reverse().forEach((n, ago) => {
    tb.append(el("tr", {},
      el("td", { text: agoText(ago) }),
      el("td", { text: f.num(n) }),
      el("td", { text: f.isNum(fl.usual_day) && fl.usual_day > 0 ? times(n / fl.usual_day) : "—" })));
  });
}
