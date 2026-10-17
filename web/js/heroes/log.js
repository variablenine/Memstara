// LOG screen hero: the event timeline. A seismograph of what happened: one
// lane per kind of event (blocks, peers, the node itself, alerts, monerod's
// own log, blocks replaced), time running from an hour ago at the left to NOW
// at the right. Every event the dashboard logged is one glowing mark in its
// lane at the moment it happened; a heads-up is a ▲ and a problem a ■ in their
// own colours (a problem pulses only while its alert is still up). In the PEERS
// lane a mark reaches up for connections that joined and down for ones that
// left (taller for more). The whole record slides left as time passes, new
// events land at the NOW cursor with a flash, and the newest one types itself
// out on the terminal line underneath.
//
// Everything placed is real: times, kinds, levels and words come from
// snap.events, the dashboard's own event list (kept in memory while it runs,
// last 200). The cursor's scanning light, the breathing glow, the short ring
// after each mark and the flashes are decoration.
import { el, clear, setText, setData } from "../dom.js";
import * as f from "../fmt.js";
import { createStage, glowSprite, stamp, moving, clamp, TAU, cssRGB } from "../fx/space.js";

const LANES = [
  { id: "block", label: "BLOCKS", word: "block", words: "blocks" },
  { id: "peers", label: "PEERS", word: "peer change", words: "peer changes" },
  { id: "node", label: "NODE", word: "node event", words: "node events" },
  { id: "alert", label: "ALERTS", word: "alert raised or cleared", words: "alerts raised or cleared" },
  { id: "log", label: "MONEROD LOG", word: "line from monerod's log", words: "lines from monerod's log" },
  { id: "reorg", label: "BLOCKS REPLACED", word: "time blocks were replaced", words: "times blocks were replaced" },
];
// event kind -> lane (anything new lands in NODE)
const LANE_OF = {
  block: 0, peer_in: 1, peer_out: 1, peer_gone: 1,
  sync: 2, restart: 2, source: 2, settings: 2,
  alert: 3, clear: 3, log: 4, reorg: 5,
};
// the short tag on the terminal line
const TAG = {
  block: "BLOCK", peer_in: "PEERS", peer_out: "PEERS", peer_gone: "PEERS", sync: "SYNC", restart: "RESTART",
  source: "SOURCE", settings: "SETTINGS", alert: "ALERT", clear: "CLEARED", log: "MONEROD", reorg: "REORG",
};
const LEVEL_WORD = { warn: "▲ HEADS-UP", bad: "■ PROBLEM" };
const HOT = ["warn", "bad"];  // drawn in this order where marks pile up, so a problem stays on top
const SPANS = [   // major = clock lines (narrow screens use majorN), minor = small axis ticks
  { s: 3600, key: "1 H", ago: "1 H AGO", word: "LAST HOUR", major: 600, majorN: 1200, minor: 300 },
  { s: 21600, key: "6 H", ago: "6 H AGO", word: "LAST 6 HOURS", major: 3600, majorN: 7200, minor: 1800 },
  { s: 86400, key: "24 H", ago: "24 H AGO", word: "LAST 24 HOURS", major: 10800, majorN: 21600, minor: 3600 },
];
const KEEP = 200;            // the dashboard keeps its last 200 events
const FLASH_S = 1.9;         // a new mark's flash
const PEN_S = 1.6;           // the lane's pen at NOW flares this long after a mark lands
const SCAN_S = 6.5;          // the cursor's light travels down and back up in this long
const TYPE_MS = 26;          // terminal line: one character every this many ms...
const TYPE_MAX_MS = 2600;    // ...but never longer than this for the whole line
const FONT = '"Plex Mono", ui-monospace, monospace';
const NEAR_NOW = 12;         // px: a ▲/■ this close to the NOW cursor is drawn over it, and its lane's pen is left off
const RISE = 20;             // px the lanes' canvas reaches up behind the readouts (log.css: .et-view margin-top)

function peerCounts(text) {
  const j = /(\d+) joined/.exec(text);
  const l = /(\d+) left/.exec(text);
  return { joined: j ? Number(j[1]) : 0, left: l ? Number(l[1]) : 0 };
}
// how far a mark reaches (0..1 of the lane's half height) for n things
const reach = (n) => (n > 0 ? clamp(0.5 + 0.2 * Math.log2(n), 0.5, 1) : 0.14);
const short = (t) => f.clock(t).replace(/\s?[AP]M$/i, "");

export default function create(body, env) {
  // ---- DOM: readouts, the lanes, the terminal line
  const hudBig = el("b", { class: "et-bigv", text: "—" });
  const hudBigL = el("span", { class: "et-l", text: "EVENTS · LAST HOUR" });
  const warnV = el("b", { class: "et-v", text: "—" });
  const badV = el("b", { class: "et-v", text: "—" });
  const lastV = el("b", { class: "et-v", text: "—" });
  const keptL = el("span", { class: "et-l", text: "WATCHING SINCE" });
  const keptV = el("b", { class: "et-v", text: "—" });
  const nodeV = el("b", { class: "et-v", text: "▲ NOT ANSWERING · STILL LOGGING" });
  const stat = (label, v, cls = "") => el("div", { class: `et-stat ${cls}` }, typeof label === "string" ? el("span", { class: "et-l", text: label }) : label, v);
  const legend = el("div", { class: "et-legend", "aria-hidden": "true" },
    el("span", { class: "et-lg et-lg-ev", text: "EVENT" }),
    el("span", { class: "et-lg et-lg-warn", text: "HEADS-UP" }),
    el("span", { class: "et-lg et-lg-bad", text: "PROBLEM" }),
    el("span", { class: "et-lg et-lg-peer", text: "PEERS: ↑ JOINED · ↓ LEFT" }));
  const hud = el("div", { class: "et-hud" },
    el("div", { class: "et-stat et-big" }, hudBigL, hudBig),
    stat("HEADS-UP", warnV, "et-s-warn"), stat("PROBLEMS", badV, "et-s-bad"), stat("LATEST", lastV),
    stat(keptL, keptV, "et-s-kept"), stat("YOUR NODE", nodeV, "et-s-node"), legend);
  const nodeStat = nodeV.parentNode;
  nodeStat.hidden = true;
  const canvas = el("canvas", {
    class: "et-canvas", tabindex: "0", role: "img",
    "aria-label": "Event timeline. Arrow keys step through the events.",
  });
  const note = el("div", { class: "et-note", hidden: true });
  const view = el("div", { class: "et-view" }, canvas, note);
  const tkT = el("span", { class: "et-tk-t" });
  const tkG = el("span", { class: "et-tk-g" });
  const tkF = el("span", { class: "et-tk-f" });
  const tkX = el("span", { class: "et-tk-x" });
  const tkC = el("span", { class: "et-tk-c" });
  const tkLine = el("div", { class: "et-tk-line" }, tkT, tkG, el("span", { class: "et-tk-body" }, tkF, tkX, tkC));
  const hint = el("span", { class: "et-hint", text: "HOVER OR TAP A MARK · ← → STEP" });
  const ticker = el("div", { class: "et-tk", "aria-hidden": "true" }, el("span", { class: "et-tk-p", text: "LATEST" }), tkLine, hint);
  const say = el("p", { class: "sr-only", "aria-live": "polite" });
  body.append(el("div", { class: "et" }, hud, view, ticker), say);

  // header tools: how far back (only the spans the kept events reach into)
  const spanBtns = SPANS.map((s, i) => {
    const b = el("button", { type: "button", role: "radio", "aria-checked": i === 0 ? "true" : "false", text: s.key, title: `Show the ${s.word.toLowerCase()}` });
    b.addEventListener("click", () => setSpan(i));
    return b;
  });
  const spanCtl = el("div", { class: "seg-ctl et-span", role: "radiogroup", "aria-label": "How far back", hidden: true }, ...spanBtns);
  if (env.tools) env.tools.append(spanCtl);

  // ---- state
  let look = env.look();
  let warnRGB = cssRGB("--warn-rgb", "255, 112, 64");
  let badRGB = cssRGB("--bad-rgb", "240, 32, 110");
  let shown = false;
  let spanI = 0;
  let lastSnap = null;
  let first = true;
  let evs = [];                 // events, oldest first: {key, t, level, kind, text, lane, up, down, born, sx, sy}
  const byKey = new Map();
  let newest = null;            // the event the dashboard logged last (snap.events[0])
  let cover = null;             // {t, why: "start" | "trim"}: where the record begins
  const liveBad = new Set();    // problem marks whose alert is still up: only these pulse
  const laneHit = LANES.map(() => -1e9);
  const lastX = LANES.map(() => 0);
  const nearNow = LANES.map(() => false);   // a ▲/■ sits at the NOW cursor in this lane (drawn over it)
  let selected = null;
  let hovered = null;
  let tipOn = null;             // the event the tooltip is about
  let fromPointer = false;
  let geo = null;               // the last frame's layout
  const type = { text: null, n: 0, start: 0, per: TYPE_MS };

  const stage = createStage(canvas, { draw, maxFps: 30, onResize: () => { if (tipOn) tipAt(tipOn); } });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { if (shown && !stage.running) stage.frame(); });

  const span = () => SPANS[spanI];
  const focusRing = () => { try { return document.activeElement === canvas && canvas.matches(":focus-visible"); } catch { return document.activeElement === canvas; } };
  const pnow = () => performance.now() / 1000;

  function setSpan(i) {
    spanI = i;
    spanBtns.forEach((b, k) => b.setAttribute("aria-checked", k === i ? "true" : "false"));
    if (selected && selected.t < env.now() - span().s) choose(null);
    if (lastSnap) hudFill(lastSnap);
    if (shown && !stage.running) stage.frame();
  }

  // ---- events from the snapshot
  function ingest(snap) {
    const list = Array.isArray(snap.events) ? snap.events.filter((e) => e && f.isNum(e.t)) : [];
    const counts = new Map();
    const next = [];
    const now = pnow();
    // oldest first, so a repeated line keeps its number when a newer twin arrives
    for (let i = list.length - 1; i >= 0; i--) {
      const e = list[i];
      const level = ["info", "ok", "warn", "bad"].includes(e.level) ? e.level : "info";
      const kind = String(e.kind || "");
      const text = String(e.text ?? "");
      const base = `${e.t}|${kind}|${level}|${text}`;
      const c = counts.get(base) || 0;
      counts.set(base, c + 1);
      const key = c ? `${base}#${c}` : base;
      let ev = byKey.get(key);
      if (!ev) {
        const lane = LANE_OF[kind] ?? 2;
        ev = { key, t: e.t, level, kind, text, lane, up: 0.55, down: 0.55, born: first ? -1e9 : now, sx: undefined, sy: 0 };
        if (lane === 1) {
          const { joined, left } = peerCounts(text);
          if (joined || left) { ev.up = reach(joined); ev.down = reach(left); }
        } else if (lane === 0) {
          const m = /^(\d+) new blocks/.exec(text);
          ev.up = ev.down = m ? reach(Number(m[1])) * 0.9 + 0.1 : 0.62;
        } else if (level === "info") ev.up = ev.down = 0.5;
        if (!first) laneHit[lane] = now;
        byKey.set(key, ev);
      }
      ev.i = i;
      next.push(ev);
    }
    const keep = new Set(next.map((e) => e.key));
    for (const k of [...byKey.keys()]) if (!keep.has(k)) byKey.delete(k);
    // by time (monerod's log lines carry the log's own time); ties keep logging order
    next.sort((a, b) => a.t - b.t || b.i - a.i);
    evs = next;
    newest = list.length ? byKey.get(next.find((e) => e.i === 0)?.key) || null : null;
    if (selected && !byKey.has(selected.key)) choose(null);
    if (hovered && !byKey.has(hovered.key)) hovered = null;

    // which problems are still up: for each "bad" alert in snap.alerts, the newest event
    // that raised it (its text starts with the alert's title, and it is no older than the
    // alert's "since"). A problem that has since cleared stays steady crimson.
    liveBad.clear();
    for (const a of Array.isArray(snap.alerts) ? snap.alerts : []) {
      if (!a || a.level !== "bad") continue;
      const since = f.isNum(a.since) ? a.since : -Infinity;
      const title = typeof a.title === "string" && a.title ? `${a.title}:` : null;
      for (let i = evs.length - 1; i >= 0 && evs[i].t >= since; i--) {
        const e = evs[i];
        if (e.kind === "alert" && e.level === "bad" && (!title || e.text.startsWith(title))) { liveBad.add(e); break; }
      }
    }

    // where the record begins: the dashboard's first reading ("Reading host:port"),
    // or the oldest kept line when the list is full
    const starts = evs.filter((e) => e.kind === "source" && /^Reading /.test(e.text));
    if (starts.length) cover = { t: starts[0].t, why: "start" };
    else if (list.length >= KEEP) cover = { t: evs[0].t, why: "trim" };
    else if (evs.length) cover = { t: evs[0].t, why: "start" };
    else cover = null;

    // longer spans only once the kept events reach back past the shorter one
    const now2 = env.now();
    const oldest = evs.length ? evs[0].t : null;
    let avail = 1;
    for (let i = 1; i < SPANS.length; i++) if (oldest !== null && now2 - oldest > SPANS[i - 1].s) avail = i + 1;
    spanBtns.forEach((b, k) => { b.hidden = k >= avail; });
    spanCtl.hidden = avail < 2;
    if (spanI >= avail) setSpan(0);
    first = false;
  }

  // ---- readouts
  function inSpan() {
    const from = env.now() - span().s;
    return evs.filter((e) => e.t >= from);
  }

  function hudFill(snap) {
    const src = snap.source || {};
    const now = env.now();
    const sp = span();
    const vis = inSpan();
    const nWarn = vis.filter((e) => e.level === "warn").length;
    const nBad = vis.filter((e) => e.level === "bad").length;
    // the record may not reach back the whole span: since the dashboard started, or, once
    // its list is full, since the oldest event it kept. The numbers then cover only that.
    const partial = !!cover && cover.t > now - sp.s;
    const trimmed = partial && cover.why === "trim";
    const since = trimmed ? f.clock(cover.t, false) : null;
    // (a narrow screen wraps it only between its parts)
    setText(hudBigL, !partial ? `EVENTS · ${sp.word}`
      : trimmed ? ["EVENTS", `SINCE ${since}`, `LAST ${KEEP} KEPT`].map((p) => p.replace(/ /g, "\u00a0")).join(" · ")
        : "EVENTS · SINCE THE DASHBOARD STARTED");
    setText(hudBig, Array.isArray(snap.events) ? f.num(vis.length) : "—");
    setText(warnV, nWarn ? `▲ ${f.num(nWarn)}` : "0 · NONE");
    setText(badV, nBad ? `■ ${f.num(nBad)}` : "0 · NONE");
    setData(warnV, "level", nWarn ? "warn" : null);
    setData(badV, "level", nBad ? "bad" : null);
    // pulse only while one of these problems is still up; cleared ones stay steady
    setData(badV, "live", vis.some((e) => liveBad.has(e)) ? "" : null);
    setText(lastV, newest ? `${f.dur(Math.max(0, now - newest.t))} AGO` : "—");
    if (cover && cover.why === "trim") {
      setText(keptL, "KEPT FROM");
      setText(keptV, `${f.clock(cover.t, false)} · LAST ${KEEP}`);
    } else {
      setText(keptL, "WATCHING SINCE");
      setText(keptV, cover ? `${f.clock(cover.t, false)} · ${f.dur(Math.max(0, now - cover.t))}` : "—");
    }
    keptV.parentNode.hidden = trimmed;   // the headline already says it

    // the plain notes over the lanes
    let why = null;
    if (!Array.isArray(snap.events)) why = "THE DASHBOARD SENT NO EVENT LIST · —";
    else if (!evs.length) why = "LISTENING · NOTHING HAS HAPPENED YET · EVENTS LAND HERE AS THEY HAPPEN";
    else if (!vis.length) why = `NOTHING HAPPENED IN THE ${sp.word} · OLDER EVENTS ARE ON THE LONGER SPANS`;
    if (src.answering === false && !evs.length) why = "YOUR NODE ISN'T ANSWERING · THE DASHBOARD STILL LOGS WHAT IT SEES";
    const down = src.answering === false;
    nodeStat.hidden = !down;
    // as the node lamp has it: a heads-up at first, a problem once the "not answering" alert is up
    const nodeLamp = Array.isArray(snap.lamps) ? snap.lamps.find((l) => l && l.id === "node") : null;
    const downBad = nodeLamp?.state === "bad"
      || (Array.isArray(snap.alerts) && snap.alerts.some((a) => a && a.id === "node_down" && a.level === "bad"));
    setText(nodeV, `${downBad ? "■" : "▲"} NOT ANSWERING · STILL LOGGING`);
    setData(nodeV, "level", down ? (downBad ? "bad" : "warn") : null);
    setData(nodeV, "live", down && downBad ? "" : null);
    note.hidden = !why;
    setText(note, why || "");

    const poll = f.num(src.poll_s || 5);
    setText(env.sub, `WHAT THE DASHBOARD SAW · EVERY ${poll} S · KEPT WHILE IT RUNS`);

    const per = LANES.map(() => 0);
    for (const e of vis) per[e.lane]++;
    const parts = LANES.map((l, i) => `${f.num(per[i])} ${per[i] === 1 ? l.word : l.words}`).join(", ");
    const over = !partial ? sp.word.toLowerCase()
      : trimmed ? `since ${since} (the dashboard keeps only its last ${KEEP} events)` : "since the dashboard started";
    canvas.setAttribute("aria-label", `Event timeline, ${over}: `
      + `${f.plural(vis.length, "event")} (${parts}); ${f.plural(nWarn, "heads-up", "heads-ups")}, ${f.plural(nBad, "problem")}.`
      + `${newest ? ` Newest, at ${f.clock(newest.t)}: ${newest.text}.` : ""} Arrow keys step through the events; Escape lets go.`);
  }

  // ---- the terminal line: the newest event, typed out
  function tickerFill(animate) {
    if (!newest) {
      if (tkLine.dataset.key === "none") return;
      tkLine.dataset.key = "none";
      setData(tkLine, "level", null);
      setText(tkT, "");
      setText(tkG, "");
      setText(tkF, "");
      tkX.textContent = "LISTENING · EVENTS SHOW HERE AS THEY HAPPEN";
      tkLine.classList.add("is-idle");
      type.text = null;
      return;
    }
    if (tkLine.dataset.key === newest.key) return;
    tkLine.dataset.key = newest.key;
    tkLine.classList.remove("is-idle");
    setData(tkLine, "level", newest.level);
    setData(tkLine, "lane", LANES[newest.lane].id);
    tkT.textContent = short(newest.t);
    tkG.textContent = `[${TAG[newest.kind] || String(newest.kind || "EVENT").toUpperCase()}]`;
    tkF.textContent = LEVEL_WORD[newest.level] ? `${LEVEL_WORD[newest.level]} · ` : "";
    if (animate) {
      type.text = newest.text;
      type.n = 0;
      type.start = performance.now();
      type.per = Math.min(TYPE_MS, TYPE_MAX_MS / Math.max(1, newest.text.length));
      tkX.textContent = "";
      tkLine.classList.remove("is-new");
      void tkLine.offsetWidth;   // restart the landing flash
      tkLine.classList.add("is-new");
    } else {
      type.text = null;
      tkX.textContent = newest.text;
    }
  }
  function typeStep() {
    if (type.text === null) return;
    const n = Math.min(type.text.length, Math.floor((performance.now() - type.start) / type.per) + 1);
    if (n !== type.n) { type.n = n; tkX.textContent = type.text.slice(0, n); }
    if (n >= type.text.length) type.text = null;
  }
  function typeDone() {
    if (type.text === null) return;
    tkX.textContent = type.text;
    type.text = null;
  }

  // ---- layout
  function layout(w, h) {
    const narrow = w < 640;
    const padL = narrow ? 10 : 16;
    const labelW = narrow ? 0 : 176;
    const plotL = padL + labelW;
    const plotR = w - (narrow ? 30 : 46);
    const top = (narrow ? 6 : 10) + RISE;   // the canvas reaches up under the readouts, so glows fade out there
    const axisH = 26;
    const laneH = Math.max(16, (h - top - axisH) / LANES.length);
    const band = narrow ? 12 : 0;   // narrow: each lane's name sits in a strip of its own at the top of the lane
    return {
      w, h, narrow, padL, plotL, plotR, plotW: Math.max(40, plotR - plotL), top, laneH, band,
      amp: (laneH - band) * 0.4, bottom: top + laneH * LANES.length,
    };
  }
  const laneY = (g, i) => g.top + i * g.laneH + g.band + (g.laneH - g.band) / 2;
  function xOf(g, t, now) {
    return g.plotR - ((now - Math.min(t, now)) / span().s) * g.plotW;
  }

  // ---- drawing
  function draw(ctx, w, h) {
    const P = pnow();
    const live = moving(look);
    const now = env.now();
    const g = layout(w, h);
    geo = g;
    const sp = span();
    const tLeft = now - sp.s;
    const acc = look.accent;
    const head = look.head || "107, 174, 200";
    const ink = look.ink || "232, 232, 240";
    const gm = look.glow === false ? 0.4 : 1;
    const breath = live && look.glow !== false ? 0.5 + 0.5 * Math.sin((P * TAU) / 7) : 0.55;
    const A = g.amp;
    if (live) typeStep();

    ctx.clearRect(0, 0, w, h);
    ctx.lineCap = "butt";

    // lanes: a faint band on every other one, hairlines between them
    for (let i = 0; i < LANES.length; i++) {
      const y0 = g.top + i * g.laneH;
      if (i % 2 === 0) { ctx.fillStyle = `rgba(${head}, 0.026)`; ctx.fillRect(g.plotL, y0, g.plotW, g.laneH); }
    }
    ctx.lineWidth = 1;
    ctx.strokeStyle = `rgba(${head}, 0.09)`;
    ctx.beginPath();
    for (let i = 0; i <= LANES.length; i++) {
      const y = Math.round(g.top + i * g.laneH) + 0.5;
      ctx.moveTo(g.padL, y); ctx.lineTo(g.plotR, y);
    }
    ctx.stroke();
    // the left edge of the record
    ctx.strokeStyle = `rgba(${head}, 0.22)`;
    ctx.beginPath(); ctx.moveTo(Math.round(g.plotL) + 0.5, g.top); ctx.lineTo(Math.round(g.plotL) + 0.5, g.bottom + 4); ctx.stroke();

    // before the record begins: hatched, with a dashed line where it starts
    let xc = g.plotL;
    if (cover && cover.t > tLeft) {
      xc = clamp(xOf(g, cover.t, now), g.plotL, g.plotR);
      ctx.save();
      ctx.beginPath(); ctx.rect(g.plotL + 1, g.top, xc - g.plotL - 1, g.bottom - g.top); ctx.clip();
      ctx.strokeStyle = `rgba(${head}, 0.06)`;
      ctx.beginPath();
      for (let x = g.plotL - (g.bottom - g.top); x < xc; x += 8) { ctx.moveTo(x, g.bottom); ctx.lineTo(x + (g.bottom - g.top), g.top); }
      ctx.stroke();
      ctx.restore();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = `rgba(${head}, 0.55)`;
      ctx.beginPath(); ctx.moveTo(Math.round(xc) + 0.5, g.top); ctx.lineTo(Math.round(xc) + 0.5, g.bottom + 6); ctx.stroke();
      ctx.setLineDash([]);
      if (xc - g.plotL > 80) caption(ctx, g, xc, ink);
    }

    // clock lines (local time, or UTC in privacy mode)
    const major = g.narrow ? sp.majorN : sp.major;
    const off = f.zoneOffset(now);
    const grid = [];
    for (let s = Math.ceil((tLeft + off) / major) * major - off; s <= now; s += major) grid.push(s);
    ctx.lineWidth = 1;
    ctx.strokeStyle = `rgba(${head}, 0.075)`;
    ctx.beginPath();
    for (const s of grid) { const x = Math.round(xOf(g, s, now)) + 0.5; ctx.moveTo(x, g.top); ctx.lineTo(x, g.bottom); }
    ctx.stroke();

    // each lane's trace line: solid where there is a record, dotted before it
    for (let i = 0; i < LANES.length; i++) {
      const y = Math.round(laneY(g, i)) + 0.5;
      if (xc > g.plotL + 1) {
        ctx.setLineDash([1, 4]);
        ctx.strokeStyle = `rgba(${head}, 0.16)`;
        ctx.beginPath(); ctx.moveTo(g.plotL, y); ctx.lineTo(xc, y); ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.strokeStyle = `rgba(${head}, 0.26)`;
      ctx.beginPath(); ctx.moveTo(xc, y); ctx.lineTo(g.plotR, y); ctx.stroke();
    }

    // the recent minutes, lit softly by the cursor (breathes)
    ctx.globalCompositeOperation = "lighter";
    const wake = Math.min(220, g.plotW * 0.22);
    const grad = ctx.createLinearGradient(g.plotR - wake, 0, g.plotR, 0);
    grad.addColorStop(0, `rgba(${acc}, 0)`);
    grad.addColorStop(1, `rgba(${acc}, ${((0.035 + 0.045 * breath) * gm).toFixed(3)})`);
    ctx.fillStyle = grad;
    ctx.fillRect(g.plotR - wake, g.top, wake, g.bottom - g.top);

    // marks: glows first, all together
    const vis = [];
    for (const e of evs) {
      e.sx = undefined;
      if (e.t < tLeft - 30) continue;
      const x = xOf(g, e.t, now);
      if (x < g.plotL - 3) continue;
      e.sx = x;
      e.sy = laneY(g, e.lane);
      e.gap = 1e9;
      vis.push(e);
    }
    // crowded marks glow less, so a busy stretch stays a row of ticks, not a blaze
    lastX.fill(-1e9);
    for (const e of vis) { const d = e.sx - lastX[e.lane]; e.gap = d; lastX[e.lane] = e.sx; }
    lastX.fill(1e9);
    for (let i = vis.length - 1; i >= 0; i--) { const e = vis[i]; e.gap = Math.min(e.gap, lastX[e.lane] - e.sx); lastX[e.lane] = e.sx; }
    // a problem that is still up pulses about once a second; cleared ones stay steady
    const pulse = live ? 0.55 + 0.45 * Math.sin(P * TAU * 0.85) : 1;
    const throb = (e) => (liveBad.has(e) ? pulse : 1);
    nearNow.fill(false);
    for (const e of vis) {
      const hot = e.level === "warn" || e.level === "bad";
      const rgb = e.level === "warn" ? warnRGB : e.level === "bad" ? badRGB : acc;
      const k = live ? (P - e.born) / FLASH_S : 1;
      const fl = k >= 0 && k < 1 ? 1 - k : 0;
      const crowd = hot ? 1 : clamp(e.gap / 14, 0.07, 1);
      if (hot && g.plotR - e.sx < NEAR_NOW) nearNow[e.lane] = true;
      ctx.globalAlpha = (hot ? (e.level === "bad" ? 0.55 + 0.45 * throb(e) : 0.95) : 0.85 * crowd) * gm;
      stamp(ctx, glowSprite(rgb, 24, 0.6), e.sx, e.sy, (hot ? 40 : 22 + 14 * Math.max(e.up, e.down)) + 70 * fl * fl);
      if (fl > 0) {
        ctx.globalAlpha = fl;
        stamp(ctx, glowSprite("255, 255, 255", 20, 0.8), e.sx, e.sy, 10 + 26 * fl);
      }
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";

    // then the marks themselves
    const gr = g.narrow ? 5 : 5.6;
    for (const e of vis) {
      const x = e.sx, y = e.sy;
      const k = live ? (P - e.born) / FLASH_S : 1;
      const fl = k >= 0 && k < 1 ? 1 - k : 0;
      if (e.level === "warn" || e.level === "bad") {
        const rgb = e.level === "warn" ? warnRGB : badRGB;
        ctx.strokeStyle = `rgba(${rgb}, 0.55)`;
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.moveTo(x, y - A); ctx.lineTo(x, y + A); ctx.stroke();
        glyph(ctx, e, x, y, gr, e.level === "bad" ? 0.75 + 0.25 * throb(e) : 1);
      } else {
        // a tick: up and down from the trace, then a short ring-down like a seismograph pen
        ctx.strokeStyle = fl > 0 ? `rgba(255, 255, 255, ${0.6 + 0.4 * fl})` : `rgba(${acc}, 0.95)`;
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(x, y - A * e.up); ctx.lineTo(x, y + A * e.down); ctx.stroke();
        if (e.level === "ok") { dot(ctx, x, y - A * e.up, 1.9, `rgba(${acc}, 1)`); }
        ctx.strokeStyle = `rgba(${acc}, 0.32)`;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, y);
        const amp = A * 0.42 * Math.max(e.up, e.down);
        for (let d = 1; d <= 10; d++) ctx.lineTo(x + d, y - amp * Math.exp(-d / 3.2) * Math.sin(d * 1.75));
        ctx.stroke();
      }
      if (fl > 0) {
        const rgb = e.level === "warn" ? warnRGB : e.level === "bad" ? badRGB : acc;
        const kk = 1 - fl;
        ring(ctx, x, y, 4 + 24 * (1 - Math.pow(1 - kk, 3)), `rgba(${rgb}, ${(0.85 * fl).toFixed(3)})`, 1.2);
      }
    }

    // selection and hover: a bracket round the mark and a hairline through every lane
    for (const e of [hovered, selected]) {
      if (!e || e.sx === undefined) continue;
      const on = e === selected;
      const x = Math.round(e.sx) + 0.5;
      ctx.setLineDash([2, 3]);
      ctx.strokeStyle = `rgba(${ink}, ${on ? 0.32 : 0.2})`;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, g.top); ctx.lineTo(x, g.bottom); ctx.stroke();
      ctx.setLineDash([]);
      ctx.strokeStyle = `rgba(${ink}, ${on ? 0.95 : 0.6})`;
      bracket(ctx, e.sx, e.sy, 7, A + 4);
    }

    // NOW: a bright cursor whose light slowly scans up and down it, and a pen on each lane
    const xN = g.plotR;
    ctx.globalCompositeOperation = "lighter";
    ctx.globalAlpha = (0.45 + 0.35 * breath) * gm;
    ctx.drawImage(glowSprite(acc, 32, 0.6), xN - 16, g.top - 14, 32, g.bottom - g.top + 28);
    if (live) {
      const u = 0.5 - 0.5 * Math.cos((P * TAU) / SCAN_S);
      const sy = g.top + 30 + u * (g.bottom - g.top - 60);
      ctx.globalAlpha = 0.9 * gm;
      stamp(ctx, glowSprite(acc, 40, 0.65), xN, sy, 60);
      ctx.globalAlpha = 1;
      ctx.drawImage(glowSprite("255, 255, 255", 16, 0.8), xN - 3, sy - 22, 6, 44);
    }
    ctx.globalAlpha = 1;
    for (let i = 0; i < LANES.length; i++) {
      if (nearNow[i]) continue;   // a ▲/■ is right at NOW in this lane: no pen over it
      const k = live ? (P - laneHit[i]) / PEN_S : 1;
      const fl = k >= 0 && k < 1 ? 1 - k : 0;
      stamp(ctx, glowSprite(acc, 20, 0.6), xN, laneY(g, i), (16 + 44 * fl) * (0.6 + 0.4 * gm));
    }
    ctx.globalCompositeOperation = "source-over";
    ctx.strokeStyle = `rgba(${acc}, 0.95)`;
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(xN, g.top - 2); ctx.lineTo(xN, g.bottom + 4); ctx.stroke();
    ctx.fillStyle = `rgba(${acc}, 1)`;
    ctx.beginPath(); ctx.moveTo(xN - 4, g.top - 3); ctx.lineTo(xN + 4, g.top - 3); ctx.lineTo(xN, g.top + 2); ctx.closePath(); ctx.fill();
    for (let i = 0; i < LANES.length; i++) if (!nearNow[i]) dot(ctx, xN, laneY(g, i), 2.8, `rgba(${acc}, 1)`);
    // the newest heads-ups and problems sit right at the cursor: draw them again over it,
    // oldest first and problems last, so a problem is never hidden under a heads-up
    let near = vis.length;
    while (near > 0 && g.plotR - vis[near - 1].sx < NEAR_NOW) near--;
    for (const lvl of HOT) {
      for (let i = near; i < vis.length; i++) {
        const e = vis[i];
        if (e.level === lvl) glyph(ctx, e, e.sx, e.sy, gr, lvl === "bad" ? 0.75 + 0.25 * throb(e) : 1);
      }
    }

    // the time axis: an hour ago ... NOW, with clock times between
    axis(ctx, g, now, grid, xc, ink, head, acc);

    // lane names (in a column on wide screens, inside each lane on narrow ones)
    lanesText(ctx, g, ink, head);
    // keyboard focus: a frame round the lanes (the canvas reaches up under the readouts, so no outline)
    if (focusRing()) {
      ctx.strokeStyle = `rgba(${acc}, 0.85)`;
      ctx.lineWidth = 1;
      ctx.strokeRect(2.5, Math.round(g.top - 8) + 0.5, w - 5, h - Math.round(g.top - 8) - 3);
    }
    if (tipOn && shown && live) tipFollow();
  }

  // what the hatched stretch is: only the lines that fit inside it (the second goes
  // first), and on narrow screens on the NODE lane's trace, clear of the lane names
  // that sit in a strip at the top of each lane there
  function caption(ctx, g, xc, ink) {
    const fit = xc - g.plotL - 16;
    const l1 = cover.why === "start" ? "BEFORE THE DASHBOARD STARTED" : "OLDER EVENTS NOT KEPT";
    const l2 = cover.why === "start" ? "IT KEEPS EVENTS ONLY WHILE IT RUNS" : `IT KEEPS ITS LAST ${KEEP} EVENTS`;
    const f1 = `600 ${g.narrow ? 9 : 10}px ${FONT}`, f2 = `400 ${g.narrow ? 8.5 : 9.5}px ${FONT}`;
    ctx.font = f1;
    if (ctx.measureText(l1).width + l1.length * 1.6 > fit) return;
    ctx.font = f2;
    const two = ctx.measureText(l2).width + l2.length * 1 <= fit;
    const cx = (g.plotL + xc) / 2;
    // narrow: centred between the NODE lane's name strip and the ALERTS lane's name
    const cy = g.narrow ? g.top + 2 * g.laneH + (g.band + g.laneH + 3.5) / 2 : (g.top + g.bottom) / 2;
    const d = g.narrow ? 6 : 7.5;
    ctx.save();
    ctx.beginPath(); ctx.rect(g.plotL + 1, g.top, xc - g.plotL - 1, g.bottom - g.top); ctx.clip();
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.font = f1;
    ctx.fillStyle = `rgba(${ink}, 0.5)`;
    halo(ctx, l1, cx, two ? cy - d : cy, 1.6);
    if (two) {
      ctx.font = f2;
      ctx.fillStyle = `rgba(${ink}, 0.32)`;
      halo(ctx, l2, cx, cy + d, 1);
    }
    ctx.restore();
  }

  // a heads-up ▲ or a problem ■, with a black keyline so it reads over anything
  function glyph(ctx, e, x, y, r, alpha) {
    ctx.beginPath();
    if (e.level === "warn") { ctx.moveTo(x, y - r * 1.1); ctx.lineTo(x + r, y + r * 0.75); ctx.lineTo(x - r, y + r * 0.75); }
    else ctx.rect(x - r * 0.78, y - r * 0.78, r * 1.56, r * 1.56);
    ctx.closePath();
    ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0, 0, 0, 0.9)"; ctx.stroke();
    ctx.fillStyle = `rgba(${e.level === "warn" ? warnRGB : badRGB}, ${alpha})`;
    ctx.fill();
  }

  function axis(ctx, g, now, grid, xc, ink, head, acc) {
    const sp = span();
    const y = Math.round(g.bottom) + 0.5;
    ctx.lineWidth = 1;
    ctx.strokeStyle = `rgba(${head}, 0.3)`;
    ctx.beginPath(); ctx.moveTo(g.plotL, y); ctx.lineTo(g.plotR, y);
    const tLeft = now - sp.s;
    const off = f.zoneOffset(now);
    for (let s = Math.ceil((tLeft + off) / sp.minor) * sp.minor - off; s <= now; s += sp.minor) {
      const x = Math.round(xOf(g, s, now)) + 0.5;
      ctx.moveTo(x, y); ctx.lineTo(x, y + 3);
    }
    for (const s of grid) { const x = Math.round(xOf(g, s, now)) + 0.5; ctx.moveTo(x, y); ctx.lineTo(x, y + 5); }
    ctx.stroke();

    const ty = y + 7;
    ctx.textBaseline = "top";
    const fLabel = `500 ${g.narrow ? 9 : 9.5}px ${FONT}`;
    const width = (text, gap) => ctx.measureText(text).width + text.length * gap;
    // NOW always shows; the selected mark's time comes next, then the start of the
    // record, then "1 H AGO", then the clock times wherever there is room
    ctx.font = `600 ${g.narrow ? 9 : 10}px ${FONT}`;
    const nw = width("NOW", 1.6);
    const rightStart = g.plotR - nw / 2 - 8;
    ctx.font = fLabel;
    const taken = [[rightStart, 1e9]];
    const hits = (a, c) => taken.some(([p, q]) => a < q && c > p);
    const boxes = [];
    const sel = selected && selected.sx !== undefined ? selected : null;
    if (sel) {
      const text = f.clock(sel.t);
      const w = width(text, 0.8) + 12;
      const x0 = clamp(sel.sx - w / 2, g.plotL, rightStart - w - 2);
      boxes.push({ text, w, x0, rgb: ink, strong: true });
      taken.push([x0 - 6, x0 + w + 6]);
    }
    const agoW = width(sp.ago, 1.2);
    const showAgo = !hits(g.plotL, g.plotL + agoW);
    if (showAgo) taken.push([-1e9, g.plotL + agoW + 10]);
    if (cover && xc > g.plotL + 2 && xc < g.plotR - 2) {
      const text = `${cover.why === "start" ? "STARTED" : "KEPT FROM"} ${short(cover.t).replace(/:\d\d$/, "")}`;
      const w = width(text, 0.8) + 12;
      let x0 = clamp(xc - w / 2, g.plotL, rightStart - w - 2);
      // the start flag may lean right of its line to keep clear of "1 H AGO"
      if (showAgo && x0 < g.plotL + agoW + 10 && xc >= g.plotL + agoW + 10 - w * 0.5) x0 = g.plotL + agoW + 10;
      if (!hits(x0, x0 + w)) { boxes.push({ text, w, x0, rgb: head }); taken.push([x0 - 6, x0 + w + 6]); }
    }
    ctx.textAlign = "center";
    ctx.fillStyle = `rgba(${ink}, 0.42)`;
    for (const s of grid) {
      const x = xOf(g, s, now);
      const text = sp.s > 3600 ? f.clock(s, false) : f.clock(s, false).replace(/\s?[AP]M$/i, "");
      const half = width(text, 0.8) / 2 + 4;
      if (hits(x - half, x + half)) continue;
      taken.push([x - half, x + half]);
      spaced(ctx, text, x, ty, 0.8);
    }
    if (showAgo) {
      ctx.textAlign = "left";
      ctx.fillStyle = `rgba(${ink}, 0.55)`;
      spaced(ctx, sp.ago, g.plotL, ty, 1.2);
    }
    ctx.textAlign = "center";
    for (const b of boxes) {
      ctx.fillStyle = "#000";
      ctx.fillRect(b.x0, ty - 3, b.w, 15);
      ctx.strokeStyle = `rgba(${b.rgb}, ${b.strong ? 0.7 : 0.45})`;
      ctx.lineWidth = 1;
      ctx.strokeRect(Math.round(b.x0) + 0.5, Math.round(ty - 3) + 0.5, Math.round(b.w), 15);
      ctx.fillStyle = `rgba(${b.rgb}, ${b.strong ? 1 : 0.85})`;
      spaced(ctx, b.text, b.x0 + b.w / 2, ty, 0.8);
    }
    ctx.font = `600 ${g.narrow ? 9 : 10}px ${FONT}`;
    ctx.fillStyle = `rgba(${acc}, 1)`;
    spaced(ctx, "NOW", g.plotR, ty, 1.6);
  }

  function lanesText(ctx, g, ink, head) {
    const from = env.now() - span().s;
    const per = LANES.map(() => 0);
    for (const e of evs) if (e.t >= from) per[e.lane]++;
    const restricted = !!lastSnap?.source?.restricted;
    ctx.textBaseline = "middle";
    for (let i = 0; i < LANES.length; i++) {
      const y = laneY(g, i);
      const n = per[i];
      if (!g.narrow) {
        ctx.textAlign = "left";
        ctx.font = `500 10px ${FONT}`;
        ctx.fillStyle = `rgba(${ink}, ${n ? 0.78 : 0.5})`;
        spaced(ctx, LANES[i].label, g.padL, y + 0.5, 1.5);
        ctx.textAlign = "right";
        ctx.font = `${n ? 600 : 400} 11px ${FONT}`;
        ctx.fillStyle = n ? `rgba(${ink}, 0.95)` : `rgba(${ink}, 0.3)`;
        spaced(ctx, f.num(n), g.plotL - 14, y + 0.5, 0.6);
      } else {
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.font = `500 8.5px ${FONT}`;
        ctx.fillStyle = `rgba(${ink}, ${n ? 0.72 : 0.45})`;
        halo(ctx, `${LANES[i].label} · ${f.num(n)}`, g.plotL + 5, g.top + i * g.laneH + 3.5, 1.1);
        ctx.textBaseline = "middle";
      }
      if (i === 1 && restricted && !n) {
        ctx.textAlign = "center";
        ctx.font = `400 ${g.narrow ? 8.5 : 9.5}px ${FONT}`;
        ctx.fillStyle = `rgba(${ink}, 0.42)`;
        halo(ctx, "NOT SHOWN ON A RESTRICTED RPC PORT", (g.plotL + g.plotR) / 2, y, 1);
      }
    }
  }

  // letter-spaced text (canvas letterSpacing is not everywhere yet); returns its width
  function spaced(ctx, text, x, y, gap) {
    const sp = "letterSpacing" in ctx;
    if (sp) ctx.letterSpacing = `${gap}px`;
    const dx = sp && ctx.textAlign === "center" ? gap / 2 : 0;
    ctx.fillText(text, x + dx, y);
    const w = ctx.measureText(text).width;
    if (sp) ctx.letterSpacing = "0px";
    return w;
  }
  function halo(ctx, text, x, y, gap) {
    const fill = ctx.fillStyle;
    const sp = "letterSpacing" in ctx;
    if (sp) ctx.letterSpacing = `${gap}px`;
    const dx = sp && ctx.textAlign === "center" ? gap / 2 : 0;
    ctx.strokeStyle = "rgba(0, 0, 0, 0.92)";
    ctx.lineWidth = 4;
    ctx.lineJoin = "round";
    ctx.strokeText(text, x + dx, y);
    ctx.fillStyle = fill;
    ctx.fillText(text, x + dx, y);
    if (sp) ctx.letterSpacing = "0px";
  }
  function ring(ctx, x, y, r, color, lw) {
    ctx.beginPath(); ctx.arc(x, y, Math.max(0.5, r), 0, TAU); ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.stroke();
  }
  function dot(ctx, x, y, r, color) {
    ctx.beginPath(); ctx.arc(x, y, Math.max(0.5, r), 0, TAU); ctx.fillStyle = color; ctx.fill();
  }
  function bracket(ctx, x, y, hw, hh) {
    const c = 4;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(x - hw + c, y - hh); ctx.lineTo(x - hw, y - hh); ctx.lineTo(x - hw, y + hh); ctx.lineTo(x - hw + c, y + hh);
    ctx.moveTo(x + hw - c, y - hh); ctx.lineTo(x + hw, y - hh); ctx.lineTo(x + hw, y + hh); ctx.lineTo(x + hw - c, y + hh);
    ctx.stroke();
  }

  // ---- picking, the tooltip, the keyboard
  function local(e) {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }
  function pickAt(x, y, reachPx) {
    const g = geo;
    if (!g || y < g.top - 4 || y > g.bottom + 4) return null;
    const lane = clamp(Math.floor((y - g.top) / g.laneH), 0, LANES.length - 1);
    let best = null, bd = Infinity;
    for (const e of evs) {
      if (e.lane !== lane || e.sx === undefined) continue;
      const d = Math.abs(e.sx - x);
      if (d <= reachPx && (d < bd || (d === bd && e.t > best.t))) { bd = d; best = e; }
    }
    return best;
  }
  function headOf(e) {
    const lvl = LEVEL_WORD[e.level] ? `${LEVEL_WORD[e.level]} · ` : e.level === "ok" ? "OK · " : "";
    return `${lvl}${LANES[e.lane].label}`;
  }
  function rowsOf(e) {
    const ago = Math.max(0, env.now() - e.t);
    return [
      { label: "AT", value: f.clock(e.t) },
      { label: "AGO", value: ago < 5 ? "JUST NOW" : f.dur(ago) },
      { note: e.text || "—" },
    ];
  }
  function screenPos(e) {
    const r = canvas.getBoundingClientRect();
    const g = geo || layout(r.width, r.height);
    const x = xOf(g, e.t, env.now());
    return { x: r.left + x, y: r.top + laneY(g, e.lane) - g.amp };
  }
  function tipAt(e, cx, cy) {
    if (!e) return;
    const p = cx === undefined ? screenPos(e) : { x: cx, y: cy };
    tipOn = e;
    tipFollowAt = performance.now();
    // its mark is scrolled out of view: no tooltip floating over something else meanwhile
    if (p.y < 0 || p.y > window.innerHeight) { env.tip.hideTip(); return; }
    env.tip.showTip(p.x, p.y, headOf(e), rowsOf(e));
  }
  let tipFollowAt = 0;
  // a selected mark slides left with time: move its tooltip along now and then
  function tipFollow() {
    if (!tipOn || tipOn !== selected) return;
    if (performance.now() - tipFollowAt < 1000) return;
    tipAt(selected);
  }
  function tipHide() {
    if (tipOn) env.tip.hideTip();
    tipOn = null;
  }

  function choose(e, quiet) {
    selected = e || null;
    if (!selected) {
      tipHide();
      if (shown && !stage.running) stage.frame();
      return;
    }
    tipAt(selected);
    if (!quiet) say.textContent = `${headOf(selected)}, ${f.clock(selected.t)}: ${selected.text}`;
    if (shown && !stage.running) stage.frame();
  }

  canvas.addEventListener("pointerdown", () => { fromPointer = true; });
  canvas.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse") return;
    const p = local(e);
    const ev = pickAt(p.x, p.y, 8);
    if (ev !== hovered) { hovered = ev; if (!stage.running) stage.frame(); }
    canvas.style.cursor = ev ? "pointer" : "default";
    if (ev) tipAt(ev, e.clientX, e.clientY);
    else if (selected && document.activeElement === canvas) { if (tipOn !== selected) tipAt(selected); }
    else tipHide();
  });
  canvas.addEventListener("pointerleave", (e) => {
    if (e.pointerType !== "mouse") return;
    hovered = null;
    if (selected && document.activeElement === canvas) tipAt(selected); else tipHide();
    if (!stage.running) stage.frame();
  });
  canvas.addEventListener("pointerup", (e) => {
    const p = local(e);
    const ev = pickAt(p.x, p.y, e.pointerType === "touch" ? 18 : 9);
    choose(ev && ev !== selected ? ev : e.pointerType === "mouse" && ev ? ev : null);
    setTimeout(() => { fromPointer = false; }, 0);
  });
  canvas.addEventListener("focus", () => {
    if (shown && !stage.running) stage.frame();
    if (fromPointer || selected) return;
    const vis = evs.filter((x) => x.sx !== undefined);
    if (vis.length) choose(vis[vis.length - 1]);
  });
  canvas.addEventListener("blur", () => { selected = null; hovered = null; tipHide(); if (shown && !stage.running) stage.frame(); });
  canvas.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const vis = evs.filter((x) => x.sx !== undefined);
    if (!vis.length) return;
    let i = selected ? vis.indexOf(selected) : -1;
    let next = null;
    if (e.key === "ArrowRight") next = vis[i < 0 ? vis.length - 1 : Math.min(vis.length - 1, i + 1)];
    else if (e.key === "ArrowLeft") next = vis[i < 0 ? vis.length - 1 : Math.max(0, i - 1)];
    else if (e.key === "Home") next = vis[0];
    else if (e.key === "End") next = vis[vis.length - 1];
    else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      const cur = selected || vis[vis.length - 1];
      const step = e.key === "ArrowUp" ? -1 : 1;
      for (let l = cur.lane + step; l >= 0 && l < LANES.length && !next; l += step) {
        let bd = Infinity;
        for (const x of vis) if (x.lane === l && Math.abs(x.t - cur.t) < bd) { bd = Math.abs(x.t - cur.t); next = x; }
      }
      if (!next) next = cur;
    } else if (e.key === "Escape") { choose(null); return; }
    else return;
    e.preventDefault();
    choose(next);
  });
  // the tooltip is fixed to the window: when the page scrolls, a selected mark's tooltip
  // moves with its mark (once a frame at most) and a hover tooltip goes until the pointer moves
  let scrollQueued = false;
  window.addEventListener("scroll", () => {
    if (!tipOn || scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      if (!tipOn) return;
      if (tipOn !== selected) {
        hovered = null;
        if (shown && !stage.running) stage.frame();
      }
      if (selected) tipAt(selected); else tipHide();
    });
  }, { passive: true });

  return {
    update(snap) {
      lastSnap = snap;
      const was = newest ? newest.key : null;
      ingest(snap);
      hudFill(snap);
      tickerFill(shown && moving(look) && was !== null && newest && newest.key !== was);
      if (tipOn && !byKey.has(tipOn.key)) tipHide();
      else if (tipOn && tipOn === selected) tipAt(selected);
      if (selected && selected.t < env.now() - span().s) choose(null);
      if (shown && !stage.running) stage.frame();
    },
    show() {
      shown = true;
      if (moving(look)) stage.start(); else stage.frame();
    },
    hide() {
      shown = false;
      stage.stop();
      typeDone();
      selected = null;
      hovered = null;
      tipHide();
    },
    look(lk) {
      look = lk;
      warnRGB = cssRGB("--warn-rgb", "255, 112, 64");
      badRGB = cssRGB("--bad-rgb", "240, 32, 110");
      if (!moving(lk)) typeDone();
      if (!shown) return;
      if (moving(lk)) stage.start(); else { stage.stop(); stage.frame(); }
    },
  };
}
