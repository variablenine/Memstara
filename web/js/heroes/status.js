// STATUS screen hero: the status instrument. One round dial that answers "is
// my node all right?" at a glance.
//
//   outer ring  one segment per lamp on the LAMP WALL beside it, in the same
//               order clockwise from the top and with the same numbers (01,
//               02, ...): dim glow = normal, hollow = off or not known,
//               ▲ in the heads-up colour, ■ in the problem colour.
//   middle      three gauges: OUTGOING connections against the number wanted,
//               DISK FREE (free space against free space plus the blockchain),
//               and SYNCED.
//   centre      the overall state in words, the height, and how long since
//               the last block arrived (counting up each second; by the
//               miner's stamp when the dashboard didn't see it arrive), on two
//               short lines so it stays inside the innermost arc however long
//               it gets.
//
// Motion (full only): a slow sweep circles the ring and brightens each segment
// it passes. It is decoration, not a reading. A ripple runs out from the
// centre when the height goes up (a real new block; one that came while this
// screen was away plays on coming back). Problem lamps pulse about once a
// second; heads-ups glow steady; normal ones breathe slowly.
// Hover, focus or tap a segment or a gauge for its reading; click, a second
// tap or Enter opens the screen that explains it. The arrow keys step through
// the lamps and then the gauges. Beside the dial, a column of vital readings,
// each a button to its screen. Heads-up and problem colours always come from
// the look (--warn / --bad), never from here.
import { el, svg, setText, setData } from "../dom.js";
import { api } from "../api.js";
import * as f from "../fmt.js";
import { describe as describeLamp } from "../panels/lamps.js";   // plain-words help for the core lamps
import { createStage, glowSprite, stamp, moving, clamp, cssRGB, TAU } from "../fx/space.js";

const SWEEP_S = 11;            // seconds for the sweep to go round once
const ALARM_S = 1.2;           // a problem's pulse (the same beat as the lamp wall and the frame)
const BREATHE_S = 7.5;         // healthy things breathe this slowly
const TRAIL = 0.95;            // radians of fading light behind the sweep
const RIPPLE_S = 2.4;          // a new block's ripple, centre to rim
const ARC_START = 0.625 * TAU; // the gauges start at half past seven (clock angle, clockwise from 12)
const ARC_SPAN = 0.75 * TAU;   // and run three quarters of the way round
const ARC_R = [0.72, 0.595, 0.47];   // gauge radii, as a share of the ring's radius
const SCREEN_NAME = { network: "NETWORK", chain: "CHAIN", mempool: "MEMPOOL", traffic: "TRAFFIC", status: "STATUS" };
const STATE_WORD = { ok: "NORMAL", warn: "HEADS-UP", bad: "PROBLEM", off: "OFF OR NOT KNOWN" };
const RANK = { off: 0, ok: 0, warn: 1, bad: 2 };
const HISTORY_S = 900;         // the faint traces behind the vital readings: the last 15 min
const HISTORY_EVERY_MS = 15000;   // asked for again this often; each reading moves the newest point meanwhile
const TRACE_KEYS = ["out", "in", "pool", "rate_in", "rate_out"];
const GAP_BARS = 24;           // block gaps drawn behind LAST BLOCK
const RATE_S = 5;              // DOWN · UP: the readings of the last 5 s averaged (one second's rate is too jumpy to read)
const NEW_S = 5;               // the height stays lit this long after a new block (its animation takes 3 s)
// the lamps' glow sprites are made once at full strength and faded with globalAlpha,
// so the breathing and the sweep never make a new sprite per frame
const SEG_GLOW = 0.45;         // a normal lamp at its brightest (0.1 + 0.05 breathing + 0.3 sweep)
const LIT_GLOW = 0.7;          // a lit lamp at its brightest (0.3 + 0.4 pulse)

// which screen explains a lamp; "status" means this one (no jump)
const CORE_SCREEN = {
  node: "status", peers: "network", sync: "chain", block: "chain", disk: "chain", reorg: "chain",
  double_spend: "mempool", restarts: "status", spread_out: "network", spread_in: "network", tor: "network",
  flood: "chain", spy: "network",
};
function screenFor(l) {
  return CORE_SCREEN[l.id] || "status";
}

const lit = (s) => s === "warn" || s === "bad";
const nb = (x) => String(x).replace(/ /g, "\u00a0");   // keep a number with its unit when a line wraps

// "42 S", "4 MIN 05 S", "1 h 12 min"
function since(s) {
  if (!f.isNum(s)) return "—";
  s = Math.max(0, Math.floor(s));
  if (s < 60) return `${s} S`;
  if (s < 3600) return `${Math.floor(s / 60)} MIN ${String(s % 60).padStart(2, "0")} S`;
  return f.dur(s).toUpperCase();
}

export default function create(body, env) {
  // ---- DOM: the dial (canvas + text over it) and the column of vitals
  const canvas = el("canvas", { class: "ss-canvas", tabindex: "0", role: "img", "aria-label": "Status dial" });
  const stateG = el("span", { class: "ss-state-g", "aria-hidden": "true", text: "●" });
  const stateW = el("span", { class: "ss-state-w", text: "—" });
  const stateEl = el("div", { class: "ss-state" }, stateG, stateW);
  const heightEl = el("div", { class: "ss-height", text: "—" });
  // two short lines ("NEW BLOCK" / "1 MIN 24 S AGO"): one long line would run across the SYNCED arc
  const ageCap = el("div", { class: "ss-age-k", text: "NEW BLOCK" });
  const ageB = el("b", { text: "—" });
  const ageTail = el("span", { class: "ss-age-k", text: " AGO" });
  const ageEl = el("div", { class: "ss-age" }, ageCap, el("div", { class: "ss-age-v" }, ageB, ageTail));
  const centre = el("div", { class: "ss-centre" }, stateEl, el("div", { class: "ss-cap", text: "HEIGHT" }), heightEl, ageEl);

  const gauges = [
    { key: "out", name: "OUTGOING", lamp: "peers", go: "network" },
    { key: "disk", name: "DISK FREE", lamp: "disk", go: "chain" },
    { key: "sync", name: "SYNCED", lamp: "sync", go: "chain" },
  ];
  for (const g of gauges) {
    Object.assign(g, { frac: null, shown: 0, state: "ok", text: "—", r: 0 });
    g.glyph = el("span", { class: "ss-al-g", "aria-hidden": "true" });
    g.nameEl = el("span", { class: "ss-al-k", text: g.name });
    g.valEl = el("b", { text: "—" });
    g.label = el("div", { class: "ss-al" }, g.glyph, g.nameEl, g.valEl);
  }

  // top left: a few counts, written in place each reading
  const hudRow = (k) => {
    const v = el("b", { text: "—" });
    return { v, node: el("div", { class: "ss-hud-row" }, el("span", { text: k }), v) };
  };
  const hudLamps = hudRow("LAMPS"), hudOff = hudRow("OFF"), hudNode = hudRow("NODE");
  hudOff.node.hidden = true;
  const hudRows = el("div", { class: "hero-hud ss-hud-tl" }, hudLamps.node, hudOff.node, hudNode.node);
  const litList = el("div", { class: "hero-hud ss-hud-tr" });   // the lit lamps in words, beside the dial
  const legend = el("div", { class: "hero-hud ss-legend", "aria-hidden": "true" },
    el("span", { class: "ss-lg ss-lg-ok", text: "NORMAL" }),
    el("span", { class: "ss-lg ss-lg-off", text: "OFF" }),
    el("span", { class: "ss-lg ss-lg-warn", text: "▲ HEADS-UP" }),
    el("span", { class: "ss-lg ss-lg-bad", text: "■ PROBLEM" }));
  const hint = el("div", { class: "hero-hud ss-hint" },
    el("span", { text: "POINT AT A LAMP TO READ IT" }),
    el("span", { text: "CLICK IT FOR ITS SCREEN" }),
    el("span", { class: "ss-hint-deco", text: "THE SWEEP IS DECORATION" }));
  const empty = el("div", { class: "ss-empty", hidden: true });
  const view = el("div", { class: "ss-view" }, canvas, centre, ...gauges.map((g) => g.label), hudRows, litList, legend, hint, empty);

  const vitals = [
    { key: "peers", name: "PEERS", go: "network" },
    { key: "rates", name: "DOWN · UP", go: "traffic" },
    { key: "pool", name: "POOL", go: "mempool" },
    { key: "block", name: "LAST BLOCK", go: "chain" },
  ];
  const side = el("nav", { class: "ss-side", "aria-label": "Vital readings" });
  for (const v of vitals) {
    v.val = el("span", { class: "ss-v-v", text: "—" });
    v.sub = el("span", { class: "ss-v-s" });
    v.trace = svg("svg", { class: "ss-trace", viewBox: "0 0 100 40", preserveAspectRatio: "none", "aria-hidden": "true" });
    v.btn = el("button", { class: "ss-vital", type: "button" },
      el("span", { class: "ss-v-head" }, el("span", { class: "ss-v-k", text: v.name }), el("span", { class: "ss-v-go", "aria-hidden": "true", text: `${SCREEN_NAME[v.go]} ›` })),
      v.val, v.sub, v.trace);
    v.btn.addEventListener("click", () => env.goScreen(v.go));
    side.append(v.btn);
  }
  const say = el("p", { class: "sr-only", "aria-live": "polite" });
  body.append(el("div", { class: "ss" }, view, side), say);

  // ---- state
  let look = env.look();
  let shown = false;
  let snap = null;
  let lamps = [];             // [{id, label, state, value, since, help, go}]
  // what the keyboard or a tap picked: a lamp (0 .. n-1), then the three gauges (n, n+1, n+2)
  let sel = -1;
  let tipOpen = false;        // the pick's details are showing beside it
  let hov = -1;               // segment under the mouse
  let hovGauge = null;
  let keyIndex = 0;           // where the arrow keys resume (same numbering as sel)
  let ripple = -1e9;          // stage time the last new-block ripple started
  let rippleDue = null;       // a new block's ripple waits for the next frame drawn: its delay in s
  let prevHeight = null;
  let newAt = 0;              // when the height lit up for a new block (performance.now()), 0 while it isn't lit
  let newDue = false;         // a new block came while this screen was away: the height lights up on coming back
  let introAt = null;         // stage time the ring first lit up
  let ticker = 0;
  let lastPointer = "mouse";
  let hoverAt = null;         // where the mouse last was over the dial (window coordinates)
  const rates = [];           // the last few seconds of readings for DOWN · UP: {t, d, u}
  // colours from the look: ink for text and marks, and the reserved heads-up / problem colours
  let col = { i1: "232, 232, 240", i3: "112, 112, 128", i4: "58, 58, 70", head: "107, 174, 200", warn: "255, 112, 64", bad: "240, 32, 110" };
  const stateRGB = (s) => (s === "bad" ? col.bad : s === "warn" ? col.warn : look.accent);
  let hist = null;            // /api/history for the traces, its newest row kept current from each reading
  let histAt = -1e9;
  let histBusy = false;
  let tracesStale = true;     // the traces are drawn only while shown: something changed while away
  let geo = { key: "", w: 0, h: 0, cx: 0, cy: 0, R: 0, rIn: 0, narrow: false, top: 0 };

  // 30 frames a second is plenty for a slow sweep and soft light, and leaves the computer free for monerod
  const stage = createStage(canvas, { draw, maxFps: 30 });

  // ---- geometry
  function layout(w, h) {
    // narrow (a phone): no side readouts; the legend, and any lit lamps in words, go under the dial
    const narrow = w < 560;
    if (narrow !== (view.dataset.narrow === "on")) setData(view, "narrow", narrow ? "on" : null);
    // a notice (node not answering, restricted port) takes a strip across the top
    const top = empty.hidden ? 0 : Math.ceil(empty.offsetHeight) + 4;
    const litH = narrow && litList.dataset.lit === "on" ? Math.ceil(litList.offsetHeight) + 6 : 0;
    const foot = narrow ? 34 + litH : 0;
    const key = `${w}x${h}:${top}:${foot}`;
    if (key === geo.key) return;
    const R = Math.max(80, Math.min(w / 2 - 26, (h - foot - top) / 2 - 22));
    geo = { key, w, h, cx: w / 2, cy: top + (h - foot - top) / 2, R, rIn: R - clamp(R * 0.15, 16, 27), narrow, top };
    gauges.forEach((g, i) => { g.r = R * ARC_R[i]; });
    view.style.setProperty("--ss-r", `${R.toFixed(1)}px`);
    view.style.setProperty("--ss-top", `${top}px`);
    view.style.setProperty("--ss-grow", `${top + litH}px`);   // the view grows rather than the dial shrinking
    centre.style.left = `${geo.cx}px`;
    centre.style.top = `${geo.cy}px`;
    for (const g of gauges) {
      const d = g.r * Math.SQRT1_2;   // the gauge's start, down and to the left
      g.label.style.left = `${(geo.cx - d + 13).toFixed(1)}px`;
      g.label.style.top = `${(geo.cy + d).toFixed(1)}px`;
    }
    corners();
  }

  // corner readouts stand back when the view is too narrow for them beside the dial
  // (checked when the dial's size changes or one of them changes size, not every reading)
  function corners() {
    if (!geo.w || !shown) return;
    const vr = view.getBoundingClientRect();
    for (const node of [hudRows, litList, legend, hint]) {
      node.style.visibility = "";
      const r = node.getBoundingClientRect();
      if (!r.width || !vr.width) continue;
      const x = clamp(geo.cx, r.left - vr.left, r.right - vr.left);
      const y = clamp(geo.cy, r.top - vr.top, r.bottom - vr.top);
      if (Math.hypot(x - geo.cx, y - geo.cy) < geo.R + 22) node.style.visibility = "hidden";
    }
  }
  const cornerWatch = new ResizeObserver(() => corners());
  cornerWatch.observe(hudRows);
  cornerWatch.observe(litList);

  // clock angle (0 at 12, clockwise) -> canvas angle
  const ca = (a) => a - TAU / 4;
  const at = (a, r) => [geo.cx + Math.sin(a) * r, geo.cy - Math.cos(a) * r];

  // each lamp's slice of the ring
  function slices() {
    const n = lamps.length;
    if (!n) return [];
    const step = TAU / n;
    const gap = Math.min(0.045, step * 0.2);
    return lamps.map((l, i) => ({ a0: i * step + gap / 2, a1: (i + 1) * step - gap / 2, mid: (i + 0.5) * step }));
  }
  let segs = [];

  // which segment (index) or gauge sits under x, y
  function hit(x, y) {
    const dx = x - geo.cx, dy = y - geo.cy;
    const r = Math.hypot(dx, dy);
    let a = Math.atan2(dx, -dy);
    if (a < 0) a += TAU;
    if (lamps.length && r >= geo.rIn - 6 && r <= geo.R + 20) {
      return { seg: Math.min(lamps.length - 1, Math.floor((a / TAU) * lamps.length)) };
    }
    const into = (a - ARC_START + TAU) % TAU;
    if (into <= ARC_SPAN + 0.08) {
      for (const g of gauges) if (Math.abs(r - g.r) <= 9) return { gauge: g };
    }
    return null;
  }

  // ---- drawing
  function draw(ctx, w, h, t, dt) {
    layout(w, h);
    const live = moving(look);
    // a new block's ripple starts from this frame's own time (the last frame before the
    // screen was away is long gone); in still motion it is let go
    if (rippleDue !== null) {
      if (live) ripple = t + rippleDue;
      rippleDue = null;
    }
    const acc = look.accent;
    const { cx, cy, R, rIn } = geo;
    const gk = look.glow ? 1 : 0.35;
    const breathe = live && look.glow ? 0.5 + 0.5 * Math.sin((t * TAU) / BREATHE_S) : 0.55;
    const pulse = live ? 0.5 + 0.5 * Math.sin((t * TAU) / ALARM_S) : 0.75;   // problems only
    const sweep = live ? ((t / SWEEP_S) * TAU) % TAU : null;
    if (introAt === null && lamps.length) introAt = t;
    const intro = live && introAt !== null ? t - introAt : 99;
    const rip = live ? (t - ripple) / RIPPLE_S : 9;
    const ripR = rip >= 0 && rip < 1 ? lerpR(rip) : -1;
    const lvl = centreLevel();
    const lvlRGB = stateRGB(lvl);

    ctx.clearRect(0, 0, w, h);

    // light behind the dial, breathing slowly; in the heads-up colour (steady) or the
    // problem colour (pulsing) when something needs a look
    const kick = rip >= 0 && rip < 1 ? Math.pow(1 - rip, 2) : 0;   // a new block: the centre flares
    const back = lvl === "bad" ? 0.55 + 0.55 * pulse : lvl === "warn" ? 0.95 : 0.8 + 0.4 * breathe;
    ctx.globalCompositeOperation = "lighter";
    stamp(ctx, glowSprite(lvlRGB, 160, 0.13 * gk), cx, cy, R * 1.7 * back);
    if (kick > 0) {
      ctx.globalAlpha = kick;
      stamp(ctx, glowSprite(acc, 128, 0.32), cx, cy, R * (0.8 + 0.7 * (1 - kick)));
      ctx.globalAlpha = 1;
    }
    stamp(ctx, glowSprite(acc, 120, 0.07 * gk), cx, cy, R * 2.6);
    ctx.globalCompositeOperation = "source-over";

    // faint cool guide circles: the rim, and a scale just inside the lamps
    const cool = col.head;
    ring(ctx, cx, cy, R + 4, `rgba(${cool}, 0.16)`, 1);
    ring(ctx, cx, cy, rIn - 9, `rgba(${cool}, 0.14)`, 1);
    ctx.beginPath();
    for (let i = 0; i < 72; i++) {
      const a = (i / 72) * TAU;
      const len = i % 6 === 0 ? 6 : 3;
      const [x1, y1] = at(a, rIn - 9);
      const [x2, y2] = at(a, rIn - 9 - len);
      ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
    }
    ctx.strokeStyle = `rgba(${cool}, 0.24)`;
    ctx.lineWidth = 1;
    ctx.stroke();

    // the sweep's fading trail, under the lamps
    if (sweep !== null) drawTrail(ctx, sweep, acc, gk);

    // the gauges
    for (const g of gauges) drawGauge(ctx, g, dt, live, acc, gk, intro, ripR);

    // the lamps
    segs.forEach((s, i) => drawSeg(ctx, i, s, t, live, acc, gk, pulse, breathe, sweep, intro, ripR));

    // the sweep line itself
    if (sweep !== null) {
      const [x1, y1] = at(sweep, gauges[0].r + 10);
      const [x2, y2] = at(sweep, R + 7);
      const grad = ctx.createLinearGradient(x1, y1, x2, y2);
      grad.addColorStop(0, `rgba(${acc}, 0)`);
      grad.addColorStop(1, `rgba(${acc}, ${0.75 * (look.glow ? 1 : 0.7)})`);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
      ctx.strokeStyle = grad; ctx.lineWidth = 1.4; ctx.stroke();
      ctx.globalCompositeOperation = "lighter";
      stamp(ctx, glowSprite(acc, 20, 0.8 * gk), x2, y2, 16);
      ctx.globalCompositeOperation = "source-over";
    }

    // a new block: a ring of light running out from the centre
    if (ripR > 0) {
      const fade = 1 - rip;
      ring(ctx, cx, cy, ripR, `rgba(${acc}, ${(0.10 * fade * gk).toFixed(3)})`, 9);
      ring(ctx, cx, cy, ripR, `rgba(${col.i1}, ${(0.65 * fade).toFixed(3)})`, 1.3);
    }

    // lamp numbers round the outside, matching the lamp wall
    ctx.font = `${R < 140 ? 8.5 : 9.5}px "Plex Mono", ui-monospace, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    segs.forEach((s, i) => {
      const l = lamps[i];
      const [x, y] = at(s.mid, R + 13);
      const on = i === hov || i === sel;
      ctx.fillStyle = on ? `rgb(${col.i1})` : lit(l.state) ? `rgb(${stateRGB(l.state)})` : `rgb(${l.state === "off" ? col.i4 : col.i3})`;
      ctx.fillText(String(i + 1).padStart(2, "0"), x, y + 0.5);
    });

    if (!lamps.length) {
      // no lamps yet: the ring's empty outline, waiting
      ctx.setLineDash([2, 4]);
      ring(ctx, cx, cy, R, `rgba(${col.i3}, 0.55)`, 1);
      ring(ctx, cx, cy, rIn, `rgba(${col.i3}, 0.55)`, 1);
      ctx.setLineDash([]);
    }
  }

  // the ripple starts just outside the centre's words and runs past the rim
  const lerpR = (p) => geo.R * (ARC_R[2] - 0.06 + (1.16 - ARC_R[2] + 0.06) * (1 - Math.pow(1 - p, 2)));

  function drawTrail(ctx, sweep, acc, gk) {
    const { cx, cy, R } = geo;
    const r0 = gauges[0].r + 10;
    const s0 = ca(sweep - TRAIL), s1 = ca(sweep);
    if (typeof ctx.createConicGradient === "function") {
      const g = ctx.createConicGradient(s0, cx, cy);
      const k = TRAIL / TAU;
      g.addColorStop(0, `rgba(${acc}, 0)`);
      g.addColorStop(k * 0.999, `rgba(${acc}, ${(0.16 * (0.5 + 0.5 * gk)).toFixed(3)})`);
      g.addColorStop(Math.min(1, k + 0.0001), `rgba(${acc}, 0)`);
      g.addColorStop(1, `rgba(${acc}, 0)`);
      ctx.beginPath();
      ctx.arc(cx, cy, R + 5, s0, s1);
      ctx.arc(cx, cy, r0, s1, s0, true);
      ctx.closePath();
      ctx.fillStyle = g;
      ctx.fill();
      return;
    }
    for (let i = 0; i < 8; i++) {   // older web engines: a few slices
      const a = sweep - TRAIL + (i / 8) * TRAIL;
      ctx.beginPath();
      ctx.arc(cx, cy, R + 5, ca(a), ca(a + TRAIL / 8));
      ctx.arc(cx, cy, r0, ca(a + TRAIL / 8), ca(a), true);
      ctx.closePath();
      ctx.fillStyle = `rgba(${acc}, ${((i / 8) * 0.12).toFixed(3)})`;
      ctx.fill();
    }
  }

  function drawGauge(ctx, g, dt, live, acc, gk, intro, ripR) {
    const { cx, cy } = geo;
    const target = f.isNum(g.frac) ? clamp(g.frac, 0, 1) : 0;
    if (live) g.shown += (target - g.shown) * (1 - Math.exp(-dt * (intro < 3 ? 2.2 : 3.5)));
    else g.shown = target;
    const rgb = stateRGB(g.state);
    const on = hovGauge === g || gaugeAt(sel) === g;
    // track, with quarter ticks
    ctx.beginPath();
    ctx.arc(cx, cy, g.r, ca(ARC_START), ca(ARC_START + ARC_SPAN));
    ctx.strokeStyle = `rgba(${acc}, ${on ? 0.2 : 0.11})`;
    ctx.lineWidth = 5;
    ctx.lineCap = "round";
    ctx.stroke();
    ctx.lineCap = "butt";
    ctx.beginPath();
    for (let q = 0; q <= 4; q++) {
      const a = ARC_START + (q / 4) * ARC_SPAN;
      const [x1, y1] = at(a, g.r + 4.5);
      const [x2, y2] = at(a, g.r + 7.5);
      ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
    }
    ctx.strokeStyle = `rgba(${acc}, 0.22)`;
    ctx.lineWidth = 1;
    ctx.stroke();
    if (!f.isNum(g.frac) || g.shown < 0.002) {
      // unknown: a hollow start cap only
      const [x, y] = at(ARC_START, g.r);
      ring(ctx, x, y, 2.6, `rgba(${col.i3}, 0.9)`, 1);
      return;
    }
    const a1 = ARC_START + g.shown * ARC_SPAN;
    const near = ripR > 0 ? Math.max(0, 1 - Math.abs(ripR - g.r) / 22) : 0;
    // glow under the fill, then the fill
    ctx.beginPath();
    ctx.arc(cx, cy, g.r, ca(ARC_START), ca(a1));
    ctx.strokeStyle = `rgba(${rgb}, ${((0.13 + 0.2 * near) * gk).toFixed(3)})`;
    ctx.lineWidth = 13;
    ctx.lineCap = "round";
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(cx, cy, g.r, ca(ARC_START), ca(a1));
    ctx.strokeStyle = `rgba(${rgb}, ${0.82 + 0.18 * near})`;
    ctx.lineWidth = 4;
    ctx.stroke();
    ctx.lineCap = "butt";
    // the bright head where the reading is
    const [hx, hy] = at(a1, g.r);
    ctx.globalCompositeOperation = "lighter";
    stamp(ctx, glowSprite(rgb, 24, 0.85 * gk), hx, hy, 22 + (on ? 8 : 0));
    ctx.globalCompositeOperation = "source-over";
    dot(ctx, hx, hy, 2.6, `rgba(${col.i1}, 0.95)`);
  }

  function segPath(ctx, s, rOut, rIn) {
    ctx.beginPath();
    ctx.arc(geo.cx, geo.cy, rOut, ca(s.a0), ca(s.a1));
    ctx.arc(geo.cx, geo.cy, rIn, ca(s.a1), ca(s.a0), true);
    ctx.closePath();
  }

  function drawSeg(ctx, i, s, t, live, acc, gk, pulse, breathe, sweep, intro, ripR) {
    const l = lamps[i];
    const { R, rIn } = geo;
    const appear = clamp((intro - i * 0.045) / 0.3, 0, 1);
    if (appear <= 0) return;
    let boost = 0;
    if (sweep !== null) {
      const behind = (sweep - s.mid + TAU) % TAU;
      if (behind < TRAIL) boost = Math.pow(1 - behind / TRAIL, 2);
    }
    if (ripR > 0) boost = Math.max(boost, 1 - Math.abs(ripR - (R + rIn) / 2) / 26);
    if (intro < 2) boost = Math.max(boost, 1 - clamp((intro - i * 0.045) / 0.6, 0, 1));
    const on = i === hov || i === sel;
    const [mx, my] = at(s.mid, (R + rIn) / 2);
    ctx.globalAlpha = appear;
    if (lit(l.state)) {
      // lit: a problem pulses about once a second, a heads-up glows steady
      const rgb = stateRGB(l.state);
      const k = l.state === "bad" ? 0.15 + 0.85 * pulse : 0.75;
      ctx.globalCompositeOperation = "lighter";
      ctx.globalAlpha = (appear * (0.3 + 0.4 * k)) / LIT_GLOW;
      stamp(ctx, glowSprite(rgb, 48, LIT_GLOW * gk), mx, my, (R - rIn) * (2.8 + 2.2 * k));
      ctx.globalAlpha = appear;
      ctx.globalCompositeOperation = "source-over";
      segPath(ctx, s, R, rIn);
      ctx.fillStyle = `rgba(${rgb}, ${Math.min(1, 0.42 + 0.53 * k + 0.1 * boost).toFixed(3)})`;
      ctx.fill();
      ctx.strokeStyle = `rgba(${rgb}, 1)`;
      ctx.lineWidth = 1.2;
      ctx.stroke();
      glyph(ctx, l.state, mx, my, s.mid, (R - rIn) * 0.42);
    } else if (l.state === "off") {
      segPath(ctx, s, R - 0.5, rIn + 0.5);
      ctx.fillStyle = `rgba(${acc}, ${(0.02 + 0.06 * boost).toFixed(3)})`;
      ctx.fill();
      ctx.setLineDash([2, 3]);
      ctx.strokeStyle = `rgba(${col.i3}, ${0.75 + 0.25 * boost})`;
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.setLineDash([]);
    } else {
      // normal: a dim lens of accent light, breathing slowly; the sweep brightens it as it passes
      ctx.globalCompositeOperation = "lighter";
      ctx.globalAlpha = (appear * (0.1 + 0.05 * breathe + 0.3 * boost)) / SEG_GLOW;
      stamp(ctx, glowSprite(acc, 40, SEG_GLOW * gk), mx, my, (R - rIn) * (1.9 + 0.25 * breathe + 1.3 * boost));
      ctx.globalAlpha = appear;
      ctx.globalCompositeOperation = "source-over";
      segPath(ctx, s, R, rIn);
      ctx.fillStyle = `rgba(${acc}, ${(0.06 + 0.03 * breathe + 0.34 * boost).toFixed(3)})`;
      ctx.fill();
      ctx.strokeStyle = `rgba(${acc}, ${(0.26 + 0.5 * boost).toFixed(3)})`;
      ctx.lineWidth = 1;
      ctx.stroke();
      // a brighter inner edge, like a lit lens
      ctx.beginPath();
      ctx.arc(geo.cx, geo.cy, rIn + 2, ca(s.a0 + 0.012), ca(s.a1 - 0.012));
      ctx.strokeStyle = `rgba(${acc}, ${(0.4 + 0.15 * breathe + 0.45 * boost).toFixed(3)})`;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    if (on) {
      segPath(ctx, { a0: s.a0 - 0.012, a1: s.a1 + 0.012 }, R + 3, rIn - 3);
      ctx.strokeStyle = `rgba(${col.i1}, 0.95)`;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  // ▲ or ■ cut out of a lit segment, upright
  function glyph(ctx, state, x, y, a, size) {
    ctx.fillStyle = "rgba(0, 0, 0, 0.82)";
    ctx.beginPath();
    if (state === "bad") {
      const s = size * 0.8;
      ctx.rect(x - s / 2, y - s / 2, s, s);
    } else {
      const s = size;
      ctx.moveTo(x, y - s * 0.58);
      ctx.lineTo(x + s * 0.6, y + s * 0.45);
      ctx.lineTo(x - s * 0.6, y + s * 0.45);
      ctx.closePath();
    }
    ctx.fill();
  }

  function ring(ctx, x, y, r, color, lw) {
    ctx.beginPath(); ctx.arc(x, y, Math.max(0.5, r), 0, TAU); ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.stroke();
  }
  function dot(ctx, x, y, r, color) {
    ctx.beginPath(); ctx.arc(x, y, Math.max(0.5, r), 0, TAU); ctx.fillStyle = color; ctx.fill();
  }

  // ---- reading the snapshot
  function centreLevel() {
    if (!snap) return "off";
    let lvl = snap.level === "bad" || snap.level === "warn" ? snap.level : "ok";
    // the node not answering lights its lamp before the alert's grace runs out
    const nodeLamp = lamps.find((l) => l.id === "node");
    if (snap.source?.answering === false && nodeLamp && RANK[nodeLamp.state] > RANK[lvl]) lvl = nodeLamp.state;
    return lvl;
  }

  function readGauges() {
    const node = snap.node || {};
    const lampState = (id) => lamps.find((l) => l.id === id)?.state || "ok";
    const [gOut, gDisk, gSync] = gauges;
    const op = node.out_peers, ow = node.out_wanted;
    gOut.frac = f.isNum(op) && f.isNum(ow) && ow > 0 ? op / ow : null;
    gOut.text = f.isNum(op) ? `${f.num(op)} / ${f.num(ow)}` : "—";
    const free = node.free_bytes, db = node.db_bytes;
    gDisk.frac = f.isNum(free) && f.isNum(db) && free + db > 0 ? free / (free + db) : null;
    gDisk.text = f.isNum(free) ? f.bytes(free) : snap.source?.restricted ? "HIDDEN" : "—";
    gSync.frac = f.isNum(node.sync_pct) ? node.sync_pct / 100 : null;
    gSync.text = f.isNum(node.sync_pct) ? f.pct(node.sync_pct) : "—";
    setText(gSync.nameEl, node.synced === false ? "SYNCING" : "SYNCED");
    for (const g of gauges) {
      g.state = lampState(g.lamp);
      setText(g.valEl, g.text);
      setText(g.glyph, f.flag(g.state, "").trim());
      setData(g.label, "level", lit(g.state) ? g.state : null);
    }
  }

  function readCentre() {
    const node = snap.node || {};
    const src = snap.source || {};
    const lvl = centreLevel();
    const nodeLamp = lamps.find((l) => l.id === "node");
    let word = lvl === "bad" ? "PROBLEM" : lvl === "warn" ? "HEADS-UP" : "ALL CLEAR";
    if (src.answering === false) word = nodeLamp?.state === "off" ? "STARTING" : lvl === "bad" ? "PROBLEM" : "NOT ANSWERING";
    setText(stateG, lvl === "bad" ? "■" : lvl === "warn" ? "▲" : src.answering === false ? "◌" : "●");
    setText(stateW, word);
    setData(stateEl, "level", src.answering === false && lvl === "ok" ? "off" : lvl);
    setData(centre, "level", lvl);
    setText(heightEl, f.isNum(node.height) ? f.num(node.height) : "—");
    tickAge();
  }

  // what the centre counts up from: when the newest block arrived (its miner's stamp when
  // the dashboard didn't see it arrive), or while the node isn't answering, when it last did
  function countFrom() {
    const node = snap?.node || {};
    const src = snap?.source || {};
    const t = src.answering === false ? src.last_ok : node.last_block_seen ?? node.last_block_time;
    return f.isNum(t) ? t : null;
  }

  function tickAge() {
    if (!snap) return;
    const node = snap.node || {};
    const src = snap.source || {};
    const t = node.last_block_seen ?? node.last_block_time;
    const age = f.isNum(t) ? Math.max(0, env.now() - t) : f.isNum(node.last_block_age_s) ? node.last_block_age_s : null;
    const text = src.answering === false ? null : since(age);
    if (text === null) {
      // not answering: how long since it last did (counting up too), when known
      const ok = src.last_ok;
      setText(ageCap, f.isNum(ok) ? "LAST ANSWERED" : "NODE");
      setText(ageB, f.isNum(ok) ? since(env.now() - ok) : "NOT ANSWERING");
      setText(ageTail, f.isNum(ok) ? " AGO" : "");
    } else {
      setText(ageCap, "NEW BLOCK");
      setText(ageB, text);
      setText(ageTail, f.isNum(age) ? " AGO" : "");   // unknown reads "—", not "— AGO"
    }
    const v = vitals.find((x) => x.key === "block");
    setText(v.val, f.isNum(age) && src.answering !== false ? `${since(age)} AGO` : "—");
  }

  function readVitals() {
    const node = snap.node || {};
    const tr = snap.traffic || {};
    const mp = snap.mempool || {};
    const by = Object.fromEntries(vitals.map((v) => [v.key, v]));
    const lampState = (id) => lamps.find((l) => l.id === id)?.state;
    const level = (v, s) => setData(v.btn, "level", lit(s) ? s : null);

    const p = by.peers;
    setText(p.val, `${f.num(node.out_peers)} / ${f.num(node.out_wanted)} · ${f.num(node.in_peers)} IN`);
    subline(p, [["s1", "OUT / WANTED"], ["s2", "IN"], f.isNum(node.tor_out) ? [null, `TOR ${f.num(node.tor_out)}`] : null]);
    level(p, lampState("peers"));

    const r = by.rates;
    setText(r.val, `${nb(`↓ ${f.rate(rateNow("d"))}`)}  ${nb(`↑ ${f.rate(rateNow("u"))}`)}`);
    subline(r, [["s1", "DOWN"], ["s2", "UP"], [null, "ALL PEERS"]]);

    const q = by.pool;
    const txs = f.isNum(mp.txs) ? mp.txs : node.tx_pool;
    // lit with the DOUBLE SPEND lamp (which stays lit a while after the last one seen), and saying so
    const ds = lamps.find((l) => l.id === "double_spend");
    const dsLit = !!ds && lit(ds.state);
    setText(q.val, f.isNum(txs) ? f.plural(txs, "TX", "TXS") : "—");
    // while lit the small print says why, whole (there is no room on the line for more beside it)
    subline(q, dsLit ? [[null, "DOUBLE SPEND"], ds.value ? [null, String(ds.value)] : null] : [   // "SEEN 9:00 AM"
      f.isNum(txs) ? ["s1", txs === 0 ? "EMPTY" : "WAITING"] : null,   // not known: neither word
      f.isNum(mp.oldest_age_s) ? [null, `OLDEST ${nb(f.dur(mp.oldest_age_s).toUpperCase())}`] : null,
    ]);
    level(q, dsLit ? ds.state : null);

    const b = by.block;
    const avg = snap.chain?.avg_interval_s;
    subline(b, [["bar", "GAPS"], f.isNum(avg) ? [null, `USUALLY ${nb(f.wait(avg).toUpperCase())}`] : null]);
    level(b, lampState("block"));

    // the traces move with each reading; while this screen is away they wait until it is back
    mergeRow(snap);
    tracesStale = true;
    if (shown) drawTraces();

    for (const v of vitals) {
      const s = v.btn.dataset.level || "";
      // the focused one keeps the name it was read out with until its state changes: a name
      // that changed with every reading (the rates, the seconds) would be read out every second
      const label = `${v.name}: ${v.val.textContent}${s ? ` · ${STATE_WORD[s]}` : ""}. Opens the ${SCREEN_NAME[v.go].toLowerCase()} screen.`;
      if (label !== v.btn.getAttribute("aria-label") && (document.activeElement !== v.btn || s !== v.said)) {
        v.btn.setAttribute("aria-label", label);
        v.said = s;
      }
      const title = v.key === "block" ? "Bars: the gaps between the last blocks, newest on the right" : "Lines: the last 15 minutes";
      if (v.btn.title !== title) v.btn.title = title;
    }
  }

  // DOWN · UP: the readings of the last RATE_S seconds averaged, as the TRAFFIC screen's
  // big figures read; none while the newest reading has none (the node away)
  function addRate(s) {
    const tr = s.traffic || {};
    const t = f.isNum(s.t) ? s.t : s.generated;
    if (!f.isNum(t)) return;
    const last = rates[rates.length - 1];
    if (last && t < last.t) rates.length = 0;      // the server's clock went back: start again
    else if (last && t === last.t) rates.pop();     // the same reading again
    rates.push({ t, d: f.isNum(tr.rate_in) ? tr.rate_in : null, u: f.isNum(tr.rate_out) ? tr.rate_out : null });
    while (rates[0].t <= t - RATE_S) rates.shift();
  }
  function rateNow(k) {
    if (!rates.length || rates[rates.length - 1][k] === null) return null;
    let sum = 0, n = 0;
    for (const r of rates) if (r[k] !== null) { sum += r[k]; n++; }
    return sum / n;
  }

  // a vital's small print; a coloured mark in front of a part names its trace
  function subline(v, parts) {
    const key = JSON.stringify(parts);
    if (v.sub.dataset.k === key) return;
    v.sub.dataset.k = key;
    v.sub.replaceChildren();
    // each part stays whole when the line wraps (on a phone), so a mark never parts from its word
    parts.filter(Boolean).forEach(([mark, text], i) => {
      if (i) v.sub.append(" · ");
      const part = el("span", { class: "ss-v-p" });
      if (mark) part.append(el("i", { class: `ss-mk ss-mk-${mark}`, "aria-hidden": "true" }));
      part.append(text);
      v.sub.append(part);
    });
  }

  // the faint traces behind the readings: the last 15 minutes from the
  // dashboard's history, and the gaps between recent blocks
  async function loadHistory() {
    if (histBusy || !shown || performance.now() - histAt < HISTORY_EVERY_MS) return;
    histBusy = true;
    try {
      hist = await api.history(HISTORY_S);
      histAt = performance.now();
      if (snap) mergeRow(snap);
      drawTraces();
    } catch {
      histAt = performance.now();   // try again later; the readings don't depend on it
    } finally {
      histBusy = false;
    }
  }

  // the row still filling on the server (snap.hist) is the traces' newest point: it moves
  // with each reading, and a row the server has started since the last history request
  // takes a new place at the end (the oldest drops off). Says whether anything changed.
  function mergeRow(s) {
    const r = s?.hist;
    const n = hist && Array.isArray(hist.t) ? hist.t.length : 0;
    if (!n || !r || !f.isNum(r.t) || r.step !== hist.step) return false;
    const last = hist.t[n - 1];
    const put = (i) => { for (const k of TRACE_KEYS) if (Array.isArray(hist[k])) hist[k][i] = f.isNum(r[k]) ? r[k] : null; };
    if (r.t === last) { put(n - 1); return true; }
    if (r.t < last || r.t - last > hist.step * 6) return false;   // older, or too far on: the next request fills it in
    for (let t = last + hist.step; t <= r.t; t += hist.step) {
      hist.t.push(t); hist.t.shift();
      for (const k of TRACE_KEYS) if (Array.isArray(hist[k])) { hist[k].push(null); hist[k].shift(); }
    }
    put(n - 1);
    return true;
  }

  function tracePath(values, lo, hi, area) {
    const n = values.length;
    if (n < 2 || !(hi > lo)) return "";
    let d = "", run = [];
    const flush = () => {
      if (run.length > 1) {
        const pts = run.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`);
        d += area ? `M${run[0][0].toFixed(2)},40L${pts.join("L")}L${run[run.length - 1][0].toFixed(2)},40Z` : `M${pts.join("L")}`;
      }
      run = [];
    };
    values.forEach((v, i) => {
      if (!f.isNum(v)) { flush(); return; }
      run.push([(i / (n - 1)) * 100, 38 - ((v - lo) / (hi - lo)) * 35]);
    });
    flush();
    return d;
  }

  function drawTraces() {
    tracesStale = false;
    const by = Object.fromEntries(vitals.map((v) => [v.key, v]));
    const h = hist || {};
    const lines = (v, series) => {
      const all = series.flatMap(([, vals]) => (vals || []).filter(f.isNum));
      v.trace.replaceChildren();
      if (all.length < 2) return;
      const hi = Math.max(...all, 1e-9);
      series.forEach(([key, vals], i) => {
        if (i === 0) v.trace.append(svg("path", { class: `ss-tr-area ${key}`, d: tracePath(vals || [], 0, hi * 1.08, true) }));
        v.trace.append(svg("path", { class: `ss-tr-line ${key}`, d: tracePath(vals || [], 0, hi * 1.08, false) }));
      });
    };
    lines(by.peers, [["s1", h.out], ["s2", h.in]]);
    lines(by.rates, [["s1", h.rate_in], ["s2", h.rate_out]]);
    lines(by.pool, [["s1", h.pool]]);
    // block gaps as bars (real intervals from the chain list, newest on the right)
    const b = by.block;
    b.trace.replaceChildren();
    // (the wait at your node where the dashboard saw both blocks arrive, else by the miners' stamps)
    const gaps = (snap?.chain?.blocks || []).map((x) => x.seen_interval_s ?? x.interval_s).filter(f.isNum).slice(-GAP_BARS);
    if (gaps.length) {
      const top = Math.max(240, ...gaps);
      const w = 100 / GAP_BARS;
      gaps.forEach((g, i) => {
        const x = 100 - (gaps.length - i) * w;
        const bh = Math.max(3, (Math.min(g, top) / top) * 38);
        b.trace.append(svg("rect", { class: "ss-tr-bar", x: (x + w * 0.2).toFixed(2), y: (40 - bh).toFixed(2), width: (w * 0.6).toFixed(2), height: bh.toFixed(2) }));
      });
    }
  }

  // beside the dial, the lit lamps in words (problems first); with none lit, the last alert
  const LIT_SHOWN = 4;
  function readLit() {
    // what it says, as a list of lines; the words are only written again when that changes
    const lines = [];
    const head = (text) => lines.push(["head", text]);
    const item = (lvl, name, value) => lines.push(["item", lvl || "", name, value || ""]);
    const on = lamps.map((l, i) => ({ l, i })).filter(({ l }) => lit(l.state))
      .sort((x, y) => RANK[y.l.state] - RANK[x.l.state] || x.i - y.i);
    setData(litList, "lit", on.length ? "on" : null);
    head("LIT NOW");
    if (on.length) {
      for (const { l, i } of on.slice(0, LIT_SHOWN)) item(l.state, `${String(i + 1).padStart(2, "0")} ${l.label}`, l.value || "");
      if (on.length > LIT_SHOWN) lines.push(["more", `+ ${on.length - LIT_SHOWN} MORE LIT · SEE THE LAMP WALL`]);
    } else {
      item(null, lamps.length ? "NONE" : "—", "");
      // the newest raised or cleared alert in the dashboard's event list
      const evs = Array.isArray(snap.events) ? snap.events : [];
      const last = evs.find((e) => e.kind === "alert" || e.kind === "clear");
      head("LAST ALERT");
      if (last) {
        const text = String(last.text || "");
        const title = (last.kind === "clear" ? text.replace(/^Cleared:\s*/i, "") : text.split(":")[0]).toUpperCase();
        item(null, title || "—", `${last.kind === "clear" ? "CLEARED" : "RAISED"} ${f.dayClock(last.t)}`);
      } else {
        const first = evs.reduce((m, e) => (f.isNum(e.t) && (m === null || e.t < m) ? e.t : m), null);
        item(null, first !== null ? `NONE SINCE ${f.dayClock(first)}` : "NONE YET", "");
      }
    }
    const key = JSON.stringify(lines);
    if (litList.dataset.k === key) return;
    litList.dataset.k = key;
    litList.replaceChildren(...lines.map(([kind, a, b, c]) => kind === "head" ? el("div", { class: "ss-tr-k", text: a })
      : kind === "more" ? el("div", { class: "ss-lit-more", text: a })
      : el("div", { class: "ss-lit", "data-level": a || null },
        el("b", {}, a ? el("span", { class: "ss-lit-g", "aria-hidden": "true", text: a === "bad" ? "■" : "▲" }) : "", b),
        c ? el("span", { text: c }) : "")));
  }

  function readHud() {
    const src = snap.source || {};
    const litN = lamps.filter((l) => lit(l.state)).length;
    const off = lamps.filter((l) => l.state === "off").length;
    setText(hudLamps.v, !lamps.length ? "—" : f.num(lamps.length));
    hudOff.node.hidden = !off;
    setText(hudOff.v, f.num(off));
    setText(hudNode.v, src.answering === false ? "NOT ANSWERING" : f.isNum(src.latency_ms) ? `ANSWERS IN ${f.ms(src.latency_ms)}` : "—");
    readLit();

    let why = null;
    if (!lamps.length) why = src.answering === false ? "YOUR NODE ISN'T ANSWERING · THE LAMPS COME BACK WHEN IT DOES" : "NO LAMPS YET";
    else if (src.answering === false) why = "YOUR NODE ISN'T ANSWERING · READINGS COME BACK WHEN IT DOES";
    else if (src.restricted) why = "A RESTRICTED RPC PORT · DISK SPACE AND THE PEER LIST ARE HIDDEN (SETTINGS → DATA SOURCE)";
    empty.hidden = !why;
    setText(empty, why || "");

    setText(env.sub, src.answering === false
      ? `FROM YOUR NODE · NOT ANSWERING${lamps.length ? ` · ${nb(f.plural(lamps.length, "LAMP", "LAMPS"))}` : ""}`
      : `FROM YOUR NODE · ${nb(f.plural(lamps.length, "LAMP", "LAMPS"))} · ${litN ? nb(`${litN} LIT`) : "NONE LIT"} · ${nb(`EVERY ${f.num(src.poll_s || 5)} S`)}`);

    const g = gauges;
    const litNames = lamps.filter((l) => lit(l.state)).map((l) => `${l.label} (${STATE_WORD[l.state].toLowerCase()})`);
    canvas.setAttribute("aria-label",
      `Status dial: ${stateW.textContent}. ${f.plural(lamps.length, "lamp")}, ${litNames.length ? `lit: ${litNames.join(", ")}` : "none lit"}. ` +
      `Outgoing ${g[0].text}, disk free ${g[1].text}, ${g[2].nameEl.textContent.toLowerCase()} ${g[2].text}. Height ${heightEl.textContent}. ` +
      "The arrow keys step through the lamps and then the three gauges; Enter opens the screen that explains one.");
  }

  // ---- tooltips, picking and keys
  // the arrow keys and taps pick from the lamps (0 .. n-1), then the three gauges
  const stopCount = () => lamps.length + gauges.length;
  const gaugeAt = (i) => (i >= lamps.length ? gauges[i - lamps.length] || null : null);
  const goOf = (i) => gaugeAt(i)?.go || lamps[i]?.go || null;

  function lampTip(i) {
    const l = lamps[i];
    const rows = [
      { label: "READING", value: l.value || (l.state === "off" ? "OFF" : "—") },
      { label: "STATE", value: f.flag(l.state, STATE_WORD[l.state] || String(l.state || "—").toUpperCase()) },
    ];
    if (f.isNum(l.since) && lit(l.state)) rows.push({ label: "LIT SINCE", value: f.dayClock(l.since) });
    // what the lamp watches, in plain words
    const about = l.help || describeLamp(l.id) || "";
    if (about) rows.push({ note: about });
    const touch = lastPointer !== "mouse";
    rows.push({ note: l.go === "status" ? "Explained on this screen: the same lamp on the LAMP WALL." : `${touch ? "Tap again" : "Click or press Enter"} to open the ${SCREEN_NAME[l.go]} screen.` });
    return { head: `${String(i + 1).padStart(2, "0")} · ${l.label}`, rows };
  }

  function gaugeTip(g) {
    const node = snap?.node || {};
    let rows;
    if (g.key === "out") {
      rows = [
        { label: "OUTGOING", value: f.num(node.out_peers) },
        { label: "WANTED", value: f.num(node.out_wanted) },
        { label: "INCOMING", value: f.num(node.in_peers) },
        { note: "The arc is outgoing connections against the number your node aims for." },
      ];
    } else if (g.key === "disk") {
      const free = node.free_bytes, db = node.db_bytes;
      rows = [
        { label: "FREE", value: f.bytes(free) },
        { label: "BLOCKCHAIN", value: f.bytes(db) },
        { note: "The arc is free space against free space plus the blockchain's size." },
      ];
      if (g.frac !== null) rows.splice(2, 0, { label: "ARC", value: f.pct(g.frac * 100) });
    } else {
      rows = [
        { label: "SYNCED", value: f.pct(node.sync_pct) },
        { label: "HEIGHT", value: f.num(node.height) },
        { label: "NETWORK'S HEIGHT", value: f.num(node.target_height) },
      ];
    }
    if (lit(g.state)) rows.unshift({ label: "STATE", value: f.flag(g.state, STATE_WORD[g.state]) });
    const touch = lastPointer !== "mouse";
    rows.push({ note: `${touch ? "Tap again" : "Click or press Enter"} to open the ${SCREEN_NAME[g.go]} screen.` });
    return { head: g.nameEl.textContent, rows };
  }

  // where a pick's details point (window coordinates): just outside its lamp, or at its gauge's reading
  function stopPoint(i) {
    const g = gaugeAt(i);
    if (!geo.w || (!g && !segs[i])) return null;
    const r = canvas.getBoundingClientRect();
    const [x, y] = g ? at(ARC_START + (f.isNum(g.frac) ? clamp(g.frac, 0, 1) : 0) * ARC_SPAN, g.r) : at(segs[i].mid, geo.R + 6);
    return [r.left + x, r.top + y];
  }

  // show the pick's details beside it. The tooltip is fixed to the window, so it can
  // only stand by a lamp that is on screen and not under anything (the tab bar, a
  // drawer); otherwise it goes. Says whether it is showing.
  function placeTip() {
    const p = shown && sel >= 0 ? stopPoint(sel) : null;
    if (!p || document.elementFromPoint(p[0], p[1]) !== canvas) {
      env.tip.hideTip();
      return false;
    }
    const g = gaugeAt(sel);
    const c = g ? gaugeTip(g) : lampTip(sel);
    env.tip.showTip(p[0], p[1], c.head, c.rows);
    return true;
  }

  function spoken(i) {
    const g = gaugeAt(i);
    if (g) {
      return `${g.nameEl.textContent}: ${g.text}${lit(g.state) ? `, ${STATE_WORD[g.state]}` : ""}. Enter opens the ${SCREEN_NAME[g.go].toLowerCase()} screen.`;
    }
    const l = lamps[i];
    return `${String(i + 1).padStart(2, "0")}, ${l.label}: ${l.value || "—"}, ${STATE_WORD[l.state] || l.state}. ${l.go === "status" ? "" : `Enter opens the ${SCREEN_NAME[l.go].toLowerCase()} screen.`}`;
  }

  // pick a lamp or gauge (-1: none) and show its details beside it
  function pick(i, { announce = false } = {}) {
    sel = i >= 0 && i < stopCount() ? i : -1;
    if (sel >= 0) keyIndex = sel;
    tipOpen = placeTip();
    if (sel >= 0 && announce) say.textContent = spoken(sel);
    redraw();
  }

  // the pick's details can't follow it off screen: they go, and so does the pick
  // (the arrow keys still resume from it, and a later tap shows details first again)
  function dropPick() {
    tipOpen = false;
    if (sel < 0) return;
    sel = -1;
    redraw();
  }

  function open(i) {
    const go = goOf(i);
    if (!go) return;
    if (go !== "status") { tipOpen = false; env.tip.hideTip(); env.goScreen(go); return; }
    pick(i, { announce: true });   // explained here: keep its details showing
  }

  // open details follow their lamp as the page scrolls (once a frame), and go once it has left
  let scrollQueued = false;
  function onScroll() {
    if (!tipOpen || scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      if (tipOpen && !placeTip()) dropPick();
    });
  }

  // the mouse's own details give way to the pick's again when it moves off
  function restoreTip() {
    if (tipOpen && !placeTip()) dropPick();
    else if (!tipOpen) env.tip.hideTip();
  }

  canvas.addEventListener("pointerdown", (e) => { lastPointer = e.pointerType || "mouse"; });
  canvas.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse") return;
    hoverAt = { x: e.clientX, y: e.clientY };
    const r = canvas.getBoundingClientRect();
    const h = hit(e.clientX - r.left, e.clientY - r.top);
    const seg = h && h.seg !== undefined ? h.seg : -1;
    const g = h && h.gauge ? h.gauge : null;
    const changed = seg !== hov || g !== hovGauge;
    if (changed) { hov = seg; hovGauge = g; redraw(); }
    if (seg >= 0) { const c = lampTip(seg); env.tip.showTip(e.clientX, e.clientY, c.head, c.rows); }
    else if (g) { const c = gaugeTip(g); env.tip.showTip(e.clientX, e.clientY, c.head, c.rows); }
    else if (changed) restoreTip();
    canvas.style.cursor = seg >= 0 || g ? "pointer" : "default";
  });
  canvas.addEventListener("pointerleave", (e) => {
    if (e.pointerType !== "mouse") return;
    hov = -1; hovGauge = null; hoverAt = null;
    restoreTip();
    redraw();
  });
  canvas.addEventListener("click", (e) => {
    const r = canvas.getBoundingClientRect();
    const h = hit(e.clientX - r.left, e.clientY - r.top);
    if (!h) { if (sel >= 0) pick(-1); return; }
    const i = h.gauge ? lamps.length + gauges.indexOf(h.gauge) : h.seg;
    // a finger has no hover: the first tap shows the details (a lamp's or a gauge's),
    // a second tap opens the screen
    if (lastPointer !== "mouse" && i !== sel) { pick(i, { announce: true }); return; }
    open(i);
  });
  canvas.addEventListener("focus", () => {
    if (!canvas.matches(":focus-visible") || !stopCount()) return;
    const firstLit = lamps.findIndex((l) => lit(l.state));
    pick(sel >= 0 ? sel : keyIndex || Math.max(0, firstLit), { announce: true });
  });
  canvas.addEventListener("blur", () => { if (sel >= 0 || tipOpen) { sel = -1; tipOpen = false; env.tip.hideTip(); redraw(); } });
  canvas.addEventListener("keydown", (e) => {
    const n = stopCount();
    if (e.ctrlKey || e.metaKey || e.altKey || !n) return;
    let i = sel >= 0 ? sel : clamp(keyIndex, 0, n - 1);
    if (e.key === "ArrowRight" || e.key === "ArrowDown") i = sel < 0 ? i : (i + 1) % n;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") i = sel < 0 ? i : (i - 1 + n) % n;
    else if (e.key === "Home") i = 0;
    else if (e.key === "End") i = n - 1;
    else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      lastPointer = "mouse";
      // nothing showing (after Escape, or coming back): show it first, open on the next press
      if (sel >= 0) open(sel); else pick(i, { announce: true });
      return;
    }
    else if (e.key === "Escape") { pick(-1); return; }
    else return;
    e.preventDefault();
    lastPointer = "mouse";
    pick(i, { announce: true });
  });

  function redraw() {
    if (shown && !stage.running) stage.frame();
  }

  // ---- the live count since the last block: it steps on just as each second passes (the
  // count is shown in whole seconds), between readings as well
  function startTicker() {
    if (ticker) return;
    const due = () => {
      const t = countFrom();
      return t === null ? 1000 : 1015 - ((env.now() - t) % 1) * 1000;
    };
    const step = () => {
      tickAge();
      ticker = setTimeout(step, due());
    };
    ticker = setTimeout(step, due());
  }
  function stopTicker() {
    if (ticker) clearTimeout(ticker);
    ticker = 0;
  }

  // the height lights up for a new block (its animation takes 3 s, so it stays lit a few
  // seconds whatever the readings do); one that came while this screen was away, on coming back
  function lightNew() {
    centre.classList.remove("is-new");
    void centre.offsetWidth;   // restart the highlight
    centre.classList.add("is-new");
    newAt = performance.now();
    newDue = false;
  }

  // the details under a resting mouse are written again with each reading
  function refreshHover() {
    if (!shown || !hoverAt) return;
    const r = canvas.getBoundingClientRect();
    const h = hit(hoverAt.x - r.left, hoverAt.y - r.top);
    const seg = h && h.seg !== undefined ? h.seg : -1;
    const g = h && h.gauge ? h.gauge : null;
    if (seg !== hov || g !== hovGauge) { hov = seg; hovGauge = g; redraw(); }
    if (seg >= 0) { const c = lampTip(seg); env.tip.showTip(hoverAt.x, hoverAt.y, c.head, c.rows); }
    else if (g) { const c = gaugeTip(g); env.tip.showTip(hoverAt.x, hoverAt.y, c.head, c.rows); }
  }

  function readLook(lk) {
    look = lk;
    col = {
      i1: lk.ink || cssRGB("--ink", col.i1),
      i3: cssRGB("--ink-3", col.i3),
      i4: cssRGB("--ink-4", col.i4),
      head: lk.head || cssRGB("--head", col.head),
      warn: cssRGB("--warn-rgb", col.warn),
      bad: cssRGB("--bad-rgb", col.bad),
    };
    view.dataset.motion = moving(lk) ? "full" : "still";
    view.dataset.glow = lk.glow ? "on" : "off";
  }
  readLook(look);

  return {
    update(s) {
      snap = s || {};
      const list = Array.isArray(snap.lamps) ? snap.lamps : [];
      const ids = list.map((l) => l.id).join(",");
      const was = lamps.map((l) => l.id).join(",");
      const oldN = lamps.length;
      lamps = list.map((l) => ({ ...l, go: screenFor(l) }));
      if (ids !== was) {
        segs = slices();
        // a picked gauge keeps its place after the lamps; a lamp past the new end is let go
        const remap = (i) => (i >= oldN ? lamps.length + (i - oldN) : i < lamps.length ? i : -1);
        if (sel >= 0) sel = remap(sel);
        keyIndex = oldN ? Math.max(0, remap(keyIndex)) : 0;   // the first lamps: start from 01
      }
      addRate(snap);
      readGauges();
      readCentre();
      readVitals();
      readHud();
      // a new block: the height went up
      const h = snap.node?.height;
      if (f.isNum(h) && f.isNum(prevHeight) && h > prevHeight) {
        // the ripple plays from the next frame drawn: now, or just after coming back to this screen
        if (moving(look)) rippleDue = shown ? 0 : 0.4;
        if (shown) lightNew(); else newDue = true;
      }
      if (newAt && performance.now() - newAt > NEW_S * 1000) { centre.classList.remove("is-new"); newAt = 0; }
      if (f.isNum(h)) prevHeight = h;
      refreshHover();
      // keep open details current, beside their lamp; they go once it has left the screen
      if (tipOpen && !(sel >= 0 && document.activeElement === canvas && placeTip())) { env.tip.hideTip(); dropPick(); }
      loadHistory();
      redraw();
    },
    show() {
      shown = true;
      startTicker();
      tickAge();
      if (newDue) lightNew();
      if (tracesStale) drawTraces();
      corners();
      window.addEventListener("scroll", onScroll, { passive: true, capture: true });   // any scroller, the page's too
      loadHistory();
      if (moving(look)) stage.start(); else stage.frame();
    },
    hide() {
      shown = false;
      stage.stop();
      stopTicker();
      window.removeEventListener("scroll", onScroll, { capture: true });
      // off screen the details can't stay by their lamp: drop the pick (the arrow keys resume from it)
      tipOpen = false;
      sel = -1;
      hov = -1;
      hovGauge = null;
      hoverAt = null;
      env.tip.hideTip();
    },
    look(lk) {
      readLook(lk);
      if (!shown) return;
      if (moving(lk)) stage.start(); else { stage.stop(); stage.frame(); }
    },
  };
}
