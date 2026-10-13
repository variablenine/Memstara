// CHAIN screen hero: the block stream. The recent blocks stand in a line that
// curves away into the distance, newest nearest you, each one a glowing
// wireframe box. A box's size follows the block's size (in a straight line from
// the smallest block shown to the biggest, which stands 1.6 times as tall) and
// the gap between two boxes follows the wait between those two blocks, so a long
// wait is a visible gap (a block's own wait is the gap on its far side, between
// it and the older block behind it). At the front a dashed box
// stands for the next block, with the time since the last one ticking; its gap
// opens up as the wait grows. When a block arrives the dashed box turns solid
// with a flash and the whole line eases back one place. Blocks the network
// replaced (snap.chain.reorg, or a different block at a height already shown)
// flash in the heads-up colour with ▲ REPLACED.
//
// What is placed is real: heights and sizes come from the node's block headers
// (snap.chain.blocks), and times and waits from when your node got each block
// where the dashboard saw it arrive, else from the miner's stamp in its header
// (set when the miner starts on a block, so it can read minutes early). The
// slow drift of the view, the dust, the floor grid and the ripple when a block
// lands are decoration.
import { el, clear, setText, setData } from "../dom.js";
import * as f from "../fmt.js";
import { createStage, createCamera, attachOrbit, glowSprite, stamp, pick, moving, clamp, lerp, ease, TAU, cssRGB } from "../fx/space.js";

const TARGET = 120;            // Monero aims for one block every 2 minutes
const EDGE_MIN = 1.0;          // box edge for the smallest block shown...
const EDGE_MAX = 1.6;          // ...and for the biggest
const EDGE_MID = (EDGE_MIN + EDGE_MAX) / 2;
const GAP_MIN = 0.3;           // a block found seconds after the last still gets a sliver of space
const GAP_PER_AIM = 1.05;      // space for each 2 minutes of waiting
const GAP_MAX = 9;             // waits past ~17 min stop growing; the floor label gives the time
const SMALL_PX = 16;           // boxes fewer pixels across than this are drawn as one faint outline
const CROWD = 1.6;             // boxes closer together on screen than 1/CROWD of their size start to thin out
const LONG_WAIT = 300;         // waits this long have their time written on the floor
const STAMP_OFF_S = 30;        // a miner's stamp this far from the arrival is shown beside it
const REPLACED_S = 20;         // how long a replaced block stays amber
const IDLE_RESUME_S = 8;       // the drift picks up again this long after the last drag
const SWAY = { yaw: 0.1, yawW: TAU / 46, pitch: 0.035, pitchW: TAU / 29 };
const PITCH_MIN = 0.06;        // never under the floor
const PITCH_MAX = 1.2;
const MONO = "\"Plex Mono\", ui-monospace, monospace";
const LABEL_FONT = `600 10.5px ${MONO}`;   // small widely spaced captions on the picture
const TIMER_FONT = `400 17px ${MONO}`;    // the next block's timer

// ---- the line the blocks stand on: it heads off to the left and bends away
// from you, straightening out in the distance. s = distance along it from the
// next block's place (s = 0); a table makes lookups cheap.
const TH0 = 0.06, TH1 = 0.6, TH_L = 30, STEP = 0.25, TABLE = 2400;
const track = new Float64Array(TABLE * 3);
{
  let x = 0, z = 0;
  for (let i = 0; i < TABLE; i++) {
    const th = TH0 + TH1 * (1 - Math.exp(-(i * STEP) / TH_L));
    track[i * 3] = x; track[i * 3 + 1] = z; track[i * 3 + 2] = th;
    x -= Math.cos(th) * STEP; z -= Math.sin(th) * STEP;
  }
}
// position and heading at s: out.x, out.z, out.ux, out.uz (u points away from you)
function at(s, out) {
  let x, z, th;
  if (s <= 0) {
    th = TH0; x = -Math.cos(th) * s; z = -Math.sin(th) * s;
  } else {
    const fi = s / STEP;
    const i = Math.min(TABLE - 2, Math.floor(fi));
    const u = fi - i;
    const j = i * 3;
    if (fi >= TABLE - 1) {   // past the table: carry straight on
      th = track[(TABLE - 1) * 3 + 2];
      const extra = s - (TABLE - 1) * STEP;
      x = track[(TABLE - 1) * 3] - Math.cos(th) * extra;
      z = track[(TABLE - 1) * 3 + 1] - Math.sin(th) * extra;
    } else {
      x = track[j] + (track[j + 3] - track[j]) * u;
      z = track[j + 1] + (track[j + 4] - track[j + 1]) * u;
      th = track[j + 2] + (track[j + 5] - track[j + 2]) * u;
    }
  }
  out.x = x; out.z = z; out.ux = -Math.cos(th); out.uz = -Math.sin(th);
  return out;
}

const gapOf = (sec) => (f.isNum(sec) ? clamp(GAP_MIN + GAP_PER_AIM * (Math.max(0, sec) / TARGET), GAP_MIN, GAP_MAX) : GAP_PER_AIM);
// the wait before a block: between the two arrivals when the dashboard saw both, else between the miners' stamps
const waitOf = (b) => b.seen_interval_s ?? b.interval_s;

// box corners: bit 0 along the line, bit 1 across it, bit 2 top. Faces by
// axis * 2 + side; each edge knows its two faces (hidden edges draw dimmer).
const FACES = [[0, 2, 6, 4], [1, 3, 7, 5], [0, 1, 5, 4], [2, 3, 7, 6], [0, 1, 3, 2], [4, 5, 7, 6]];
const EDGES = [];
for (let a = 0; a < 8; a++) {
  for (let bit = 0; bit < 3; bit++) {
    if (a & (1 << bit)) continue;
    const [o1, o2] = [0, 1, 2].filter((x) => x !== bit);
    EDGES.push([a, a | (1 << bit), o1 * 2 + ((a >> o1) & 1), o2 * 2 + ((a >> o2) & 1)]);
  }
}

export default function create(body, env) {
  // ---- DOM: the picture, its readouts, the legend, the empty state
  const canvas = el("canvas", { class: "bs-canvas", tabindex: "0", role: "img", "aria-label": "Block stream" });
  const lgSize = el("span", { text: "BIGGER BOX = BIGGER BLOCK" });
  const glyph = (kind) => el("i", { class: `bs-g bs-g-${kind}`, "aria-hidden": "true" });
  const legend = el("div", { class: "hero-hud bs-legend" },
    el("span", { class: "bs-lg" }, glyph("size"), lgSize),
    el("span", { class: "bs-lg" }, glyph("gap"), el("span", { text: "GAP BETWEEN BOXES = WAIT BETWEEN THEM" })),
    el("span", { class: "bs-lg" }, glyph("next"), el("span", { text: "NEXT BLOCK" })));
  const hint = el("div", { class: "hero-hud bs-hint", text: "DRAG TO TURN · TAP A BLOCK · ARROW KEYS STEP" });
  const empty = el("div", { class: "bs-empty", hidden: true });
  const view = el("div", { class: "bs-view" }, canvas, hint, empty);

  const val = (cls = "") => el("b", { class: `bs-v ${cls}`, text: "—" });
  const hHeight = val("bs-big");
  const hAvg = val();
  const hReorg = val();
  const hAge = val("bs-big");
  const hAgeUnit = el("span", { class: "bs-unit", text: "AGO" });
  const hOdds = val();
  const hSync = el("div", { class: "bs-fnote", hidden: true });
  const row = (label, v) => el("div", { class: "bs-row" }, el("span", { text: label }), v);
  const factsL = el("div", { class: "bs-facts-l" },
    el("div", { class: "bs-fl", text: "HEIGHT" }), hHeight,
    row("AVERAGE GAP", hAvg), row("BLOCKS REPLACED · 24 H", hReorg));
  const factsR = el("div", { class: "bs-facts-r" },
    el("div", { class: "bs-fl", text: "LAST BLOCK" }), el("div", { class: "bs-bigline" }, hAge, hAgeUnit),
    row("CHANCE OF A WAIT THIS LONG", hOdds), hSync);
  const facts = el("div", { class: "bs-facts" }, factsL, factsR);
  const say = el("p", { class: "sr-only", "aria-live": "polite" });
  body.append(el("div", { class: "bs" }, view, facts, legend), say);

  // header tools: drift, zoom, reset
  const driftBtn = el("button", { class: "key", type: "button", "aria-pressed": "true", text: "DRIFT", title: "Let the view drift slowly on its own" });
  const zoomOut = el("button", { class: "key", type: "button", text: "−", title: "Zoom out", "aria-label": "Zoom out" });
  const zoomIn = el("button", { class: "key", type: "button", text: "+", title: "Zoom in", "aria-label": "Zoom in" });
  const resetBtn = el("button", { class: "key", type: "button", text: "RESET", title: "Put the view back" });
  if (env.tools) env.tools.append(driftBtn, zoomOut, zoomIn, resetBtn);

  // ---- state
  let look = env.look();
  let shown = false;
  let drift = true;
  let touched = false;          // someone moved the view (then a resize leaves it alone)
  let lastTouch = -1e9;
  let driftT = 0;
  let lastSnap = null;
  const items = new Map();      // height -> block item
  let order = [];               // live items, oldest first
  let prevTop = null;           // newest height seen
  let reorgKey = null;          // the last reorg record seen
  let holdUntil = 0;            // after an arrival the line waits a moment, then eases back
  let selected = null;
  let hovered = null;
  let hoverAt = null;           // where the mouse last was over the picture: x, y on it; cx, cy in the window
  let snapNext = false;         // the first frame after opening shows things where they are, without easing
  let ariaText = "";
  let ticker = 0;
  let shownSec = -2;            // the whole seconds the readout shows
  let rgbWarn = "255, 112, 64";   // the look's heads-up and problem colours (read from CSS)
  let rgbBad = "240, 32, 110";
  let ink = "232, 232, 240";
  let ink2 = "164, 164, 180";
  let head = "107, 174, 200";     // captions (cyan under the amber look)
  const ghost = { ghost: true, s: 0, sT: 0, e: EDGE_MID, eT: EDGE_MID, alpha: 1, back: 0, sx: undefined, sy: 0, sr: 0 };
  const T0 = performance.now();
  const clk = () => (performance.now() - T0) / 1000;

  const cam = createCamera({ yaw: 0.34, pitch: 0.3, dist: 14, fov: 0.8, target: [0, 0.6, 0] });
  let homeFor = "";              // the canvas size the starting view was fitted to
  let homeS = 6;                 // where along the line the view looks at rest...
  let camS = 6;                  // ...and where it looks now (stepping to a far block with the keys brings it closer)
  let following = false;         // the view follows the block picked with the keys
  let tipPending = false;        // its details show once the view has arrived
  const stage = createStage(canvas, { draw, maxFps: 30 });   // slow light and easing: 30 a second is plenty (the same computer often runs the node)
  const orbit = attachOrbit(canvas, cam, {
    minDist: 5, maxDist: 42, pitchMin: PITCH_MIN, pitchMax: PITCH_MAX,
    onInteract: () => { lastTouch = clk(); touched = true; },
    onChange: () => { if (!stage.running) stage.frame(); if (selected) tipFor(selected); },
    onTap: (x, y) => {
      const it = pick(pickable(), x, y, 10);
      choose(it && it === selected ? null : it, true);
    },
    onHover: (x, y, e) => {
      hoverAt = { x, y, cx: e.clientX, cy: e.clientY };
      const it = pick(pickable(), x, y, 8);
      const changed = it !== hovered;
      if (changed) { hovered = it; if (!stage.running) stage.frame(); }
      if (changed || (it && it !== selected)) showTipNow();   // a hovered block's details follow the mouse
      canvas.style.cursor = it ? "pointer" : "grab";
    },
    onLeave: () => { hovered = null; hoverAt = null; if (!selected) env.tip.hideTip(); else showTipNow(); if (!stage.running) stage.frame(); },
  });

  driftBtn.addEventListener("click", () => {
    drift = !drift;
    driftBtn.setAttribute("aria-pressed", drift ? "true" : "false");
    lastTouch = -1e9;
  });
  zoomIn.addEventListener("click", () => orbit.zoom(1 / 1.2));
  zoomOut.addEventListener("click", () => orbit.zoom(1.2));
  resetBtn.addEventListener("click", () => { touched = false; setHome(stage.w, stage.h); orbit.reset(); choose(null); });

  // the starting view suits the box's shape: a wide strip looks across the
  // line, a phone looks more along it
  function setHome(w, h) {
    homeFor = `${w}x${h}`;
    const k = clamp((w / Math.max(1, h) - 1.15) / (3.6 - 1.15), 0, 1);
    homeS = lerp(4.2, 10.5, k);
    if (!following) camS = homeS;
    aim(0);
    cam.target[1] = lerp(1.9, 1.45, k);
    cam.home.yaw = lerp(0.55, 0.22, k);
    cam.home.pitch = lerp(0.3, 0.1, k);
    cam.home.dist = lerp(12.5, 18, k);
    cam.fov = lerp(0.8, 0.5, k);
    // the far end of the line runs up toward the horizon. Where the readouts lie over
    // the top of the picture (not on a phone, where they sit under it), look down a
    // little less so the horizon, and the far end with it, stays below them
    const clear = readoutsBottom();
    if (clear > 0) {
      const focal = (h / 2) / Math.tan(cam.fov / 2);
      cam.home.pitch = clamp(Math.atan((h / 2 - clear - 10) / focal), PITCH_MIN + 0.03, cam.home.pitch);
    }
    if (!touched) cam.reset();
  }
  // how far down the picture the left-hand readouts reach (0 when they aren't over it)
  function readoutsBottom() {
    if (getComputedStyle(facts).position !== "absolute") return 0;
    return Math.max(0, factsL.getBoundingClientRect().bottom - canvas.getBoundingClientRect().top);
  }

  // where the view looks along the line: at rest a little behind the front; while
  // stepping with the keys to a block further back, at that block. k: how far to
  // move toward that this frame (1 = all the way)
  const wantS = () => (following && selected && !selected.ghost && selected.s !== null ? Math.max(homeS, selected.s) : homeS);
  // the view and the picked block have both come to rest (a tooltip placed now stays put)
  const settled = () => Math.abs(wantS() - camS) < 0.05 && (!selected || selected.s === null || Math.abs(selected.sT - selected.s) < 0.05);
  const AIM = {};
  function aim(k) {
    camS += (wantS() - camS) * k;
    at(camS, AIM);
    cam.target[0] = AIM.x; cam.target[2] = AIM.z;
  }

  // keyboard: one tab stop; the arrows step along the line (left = older),
  // Home is the next block, End the oldest, Escape lets go
  canvas.addEventListener("keydown", (e) => {
    if (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
    const list = steps();
    if (!list.length) return;
    let i = selected ? list.indexOf(selected) : -1;
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") i = i < 0 ? 0 : Math.min(list.length - 1, i + 1);
    else if (e.key === "ArrowRight" || e.key === "ArrowDown") i = i < 0 ? 0 : Math.max(0, i - 1);
    else if (e.key === "Home") i = 0;
    else if (e.key === "End") i = list.length - 1;
    else if (e.key === "Escape") { if (!selected) return; choose(null); e.preventDefault(); return; }
    else return;
    e.preventDefault();
    choose(list[i], true, true);
  });
  canvas.addEventListener("focus", () => {
    if (!selected && canvas.matches(":focus-visible")) { const l = steps(); if (l.length) choose(l[ghostOn() ? 1 : 0] || l[0], true, true); }
  });
  canvas.addEventListener("blur", () => { if (selected) choose(null); });
  window.addEventListener("scroll", () => { if (selected && shown) tipFor(selected); }, { passive: true });

  // newest first: the next block (when it shows), then the blocks
  function steps() {
    const list = order.slice().reverse();
    return ghostOn() ? [ghost, ...list] : list;
  }
  function pickable() {
    const list = order.filter((it) => it.alpha > 0.3);
    if (ghostOn() && ghost.alpha > 0.3) list.push(ghost);
    return list;
  }

  // ---- the snapshot: which blocks, their sizes, arrivals, replacements
  function ingest(snap, now) {
    const list = (Array.isArray(snap.chain?.blocks) ? snap.chain.blocks : [])
      .filter((b) => b && f.isNum(b.height)).slice().sort((a, b) => a.height - b.height);
    const ws = list.map((b) => b.weight).filter((w) => f.isNum(w) && w >= 0);
    const wmin = ws.length ? Math.min(...ws) : null;
    const wmax = ws.length ? Math.max(...ws) : null;
    const edgeOf = (w) => (!f.isNum(w) || wmin === null || wmax === wmin ? EDGE_MID
      : EDGE_MIN + (EDGE_MAX - EDGE_MIN) * clamp((w - wmin) / (wmax - wmin), 0, 1));
    setText(lgSize, wmin !== null && wmax !== wmin ? `BIGGER BOX = BIGGER BLOCK · ${f.bytes(wmin)} TO ${f.bytes(wmax)}` : "BIGGER BOX = BIGGER BLOCK");

    const t = clk();
    const top = list.length ? list[list.length - 1].height : null;
    const arrived = prevTop !== null && top !== null && top > prevTop;
    const animate = shown && moving(look);
    const seen = new Set();
    for (const b of list) {
      let it = items.get(b.height);
      if (!it) {
        it = { h: b.height, b, hash: b.hash, e: edgeOf(b.weight), eT: 0, s: null, sT: 0, alpha: 1, flash: -1e9, replaced: -1e9, gone: false, crowd: 1 };
        items.set(b.height, it);
        if (arrived && b.height > prevTop && animate) {
          it.s = ghost.s; it.e = ghost.e;            // the next block turns solid where it stood
          if (b.height === top) it.flash = t;
        } else if (prevTop !== null && animate) it.alpha = 0;   // filling in at the back: fade in
      } else it.gone = false;
      if (it.hash && b.hash && it.hash !== b.hash) it.replaced = t;   // same height, a different block
      it.hash = b.hash;
      it.b = b;
      it.eT = edgeOf(b.weight);
      seen.add(b.height);
    }
    // blocks that left the list: one on screen fades out; one never drawn (it came
    // and went while CHAIN was hidden) or not being animated now just goes, so
    // nothing builds up while hidden and nothing appears for a moment on opening
    for (const it of items.values()) {
      if (seen.has(it.h)) continue;
      if (it === selected) choose(null);
      if (it === hovered) hovered = null;
      if (!animate || it.s === null) items.delete(it.h);
      else it.gone = true;
    }
    order = [...items.values()].filter((it) => !it.gone).sort((a, b) => a.h - b.h);
    if (arrived) {
      const was = selected;
      if (animate) {
        ghost.alpha = 0; ghost.back = t + 1.0;     // a fresh dashed box fades in after the flash
        holdUntil = t + 0.55;
      }
      if (was === ghost) choose(null);
    }
    if (top !== null) prevTop = top;

    // blocks replaced, as the server saw it
    const last = snap.chain?.reorg?.last;
    if (last && f.isNum(last.time) && f.isNum(last.height)) {
      const key = `${last.time}:${last.height}:${last.depth}`;
      if (key !== reorgKey) {
        const fresh = reorgKey !== null || (f.isNum(now) && now - last.time < 90);
        reorgKey = key;
        const depth = clamp(Math.round(f.isNum(last.depth) ? last.depth : 1), 1, 500);
        if (fresh) for (let hh = last.height; hh < last.height + depth; hh++) { const it = items.get(hh); if (it) it.replaced = t; }
      }
    } else if (reorgKey === null) reorgKey = "";
  }

  // seconds since the newest block arrived (by its miner's stamp when the dashboard
  // didn't see it arrive), or null when that isn't known now
  function age() {
    const s = lastSnap;
    if (!s || s.source?.answering === false) return null;
    const lb = s.node?.last_block_seen ?? s.node?.last_block_time;
    return f.isNum(lb) ? Math.max(0, env.now() - lb) : null;
  }
  const syncing = () => lastSnap?.node?.synced === false;
  const ghostOn = () => !!lastSnap && lastSnap.source?.answering !== false && !syncing() && order.length > 0;
  function lateLevel() {
    const a = (lastSnap?.alerts || []).find((x) => x && x.id === "no_blocks");
    return a ? (a.level === "bad" ? "bad" : "warn") : null;
  }

  // where everything belongs along the line: the next block at s = 0, then
  // each block behind the one in front by half of each box plus its wait
  function place() {
    let s = 0;
    let front = ghost.eT;
    const a = ghostOn() ? age() : null;
    for (let i = order.length - 1; i >= 0; i--) {
      const it = order[i];
      const g = i === order.length - 1 ? gapOf(a ?? TARGET) : gapOf(waitOf(order[i + 1].b));
      s += front / 2 + g + it.eT / 2;
      it.sT = s;
      if (it.s === null) { it.s = s; it.e = it.eT; }
      front = it.eT;
    }
  }

  // ---- tooltips
  function headOf(it) {
    if (it.ghost) return "NEXT BLOCK · NOT FOUND YET";
    const t = clk();
    if (t - it.replaced < REPLACED_S * 3) return `▲ REPLACED · BLOCK ${f.num(it.h)}`;
    return it === order[order.length - 1] ? `NEWEST BLOCK · ${f.num(it.h)}` : `BLOCK ${f.num(it.h)}`;
  }
  function rowsOf(it) {
    const now = env.now();
    if (it.ghost) {
      const a = age();
      const node = lastSnap?.node || {};
      const rows = [
        { label: "IT WILL BE HEIGHT", value: f.num(node.height) },
        { label: "WAITING SO FAR", value: a === null ? "—" : f.wait(a) },
        { label: "CHANCE OF A WAIT THIS LONG", value: a === null ? "—" : f.flag(lateLevel(), f.odds(Math.exp(-a / TARGET))) },
        { note: "Monero aims for a block every 2 minutes, but each wait is luck: half come within 1m 23s, 9 in 10 within 4m 36s. The gap between the newest block and this box grows with the wait." },
      ];
      if (a !== null && !f.isNum(node.last_block_seen)) {
        rows.push({ note: "The dashboard didn't see the newest block arrive, so the wait counts from the time its miner gave it and can read minutes long." });
      }
      return rows;
    }
    const b = it.b;
    const when = (t) => `${f.dayClock(t)} · ${f.wait(Math.max(0, now - t))} AGO`;
    const iv = waitOf(b);
    // when your node got it; the miner's own time too when that reads far from it
    const off = f.isNum(b.seen) && f.isNum(b.time) ? b.time - b.seen : null;
    const far = f.isNum(off) && Math.abs(off) > STAMP_OFF_S;
    const rows = [{ label: "HEIGHT", value: f.num(b.height) }];
    if (f.isNum(b.seen)) rows.push({ label: "ARRIVED", value: when(b.seen) });
    if (!f.isNum(b.seen) || far) rows.push({ label: "MINER'S TIME", value: f.isNum(b.time) ? when(b.time) : "—" });
    rows.push(
      { label: "WAIT BEFORE IT", value: f.isNum(iv) ? (iv < 0 ? "EARLIER STAMP" : f.wait(iv)) : "—" },
      { label: "TRANSACTIONS", value: f.num(b.txs) },
      { label: "SIZE", value: f.bytes(b.weight) },
      { label: "REWARD", value: f.xmr(b.reward) },
    );
    if (!f.isNum(b.seen)) {
      rows.push({ note: "The dashboard didn't see this block arrive (it came before the dashboard was watching, or with several at once), so its time is the one its miner gave it. Miners date a block when they start on it, so it can read minutes early." });
    } else if (far && off < 0) {
      rows.push({ note: "Miners date a block when they start on it, so its own time can read minutes early." });
    } else if (far) {
      rows.push({ note: "Its miner's clock was ahead, so its own time reads later than when it arrived." });
    }
    if (clk() - it.replaced < REPLACED_S * 3) {
      rows.push({ note: "A different block used to sit at this height; the network swapped it for this one (blocks replaced). It happens now and then; wait for extra confirmations on a payment from around then." });
    }
    return rows;
  }

  // screen point of an item's middle (for a tooltip that isn't at the mouse)
  const A = {};
  const Q = [0, 0, 0];
  function tipFor(it) {
    if (!it || !stage.w) return;
    cam.prepare(stage.w, stage.h);
    at(it.s ?? 0, A);
    Q[0] = A.x; Q[1] = it.e * 0.5; Q[2] = A.z;
    const p = cam.project(Q, {});
    const r = canvas.getBoundingClientRect();
    if (!p || p.x < 0 || p.y < 0 || p.x > r.width || p.y > r.height) { env.tip.hideTip(); return; }
    const half = Math.max(6, it.e * 0.6 * p.s);
    env.tip.showTip(r.left + p.x + half * 0.5, r.top + p.y - half, headOf(it), rowsOf(it));
  }

  // the details showing: the block under the mouse beside it, else the picked one by its box
  function showTipNow() {
    if (hovered && hovered !== selected && hoverAt) env.tip.showTip(hoverAt.cx, hoverAt.cy, headOf(hovered), rowsOf(hovered));
    else if (selected) { if (!tipPending) tipFor(selected); }
    else env.tip.hideTip();
  }
  // a new reading, or a second gone by: the details showing are written again (their
  // times move on), for whatever is under a resting mouse now if the line moved under it
  function refreshTip() {
    if (!shown || (!hovered && !selected)) return;
    if (hovered && hoverAt) {
      const it = pick(pickable(), hoverAt.x, hoverAt.y, 8);
      if (it !== hovered) { hovered = it; canvas.style.cursor = it ? "pointer" : "grab"; if (!stage.running) stage.frame(); }
    }
    showTipNow();
  }

  function choose(it, speak = false, follow = false) {
    selected = it || null;
    following = !!selected && follow;
    if (selected) hovered = null;   // the keys or a tap take over from the mouse until it moves again
    env.tip.hideTip();
    tipPending = false;
    if (selected) {
      if (!moving(look) || !shown) aim(1);
      if (moving(look) && shown && !settled()) tipPending = true;   // shown once the view gets there
      else tipFor(selected);
      if (speak) say.textContent = `${headOf(selected)}. ${rowsOf(selected).filter((r) => !r.note).map((r) => `${r.label} ${r.value}`).join(", ")}`;
    }
    if (!stage.running) stage.frame();
  }

  // ---- drawing
  const dust = Array.from({ length: 110 }, (_, i) => {
    const fr = (x) => x - Math.floor(x);
    const u = fr(Math.sin(i * 12.9898) * 43758.5453), v = fr(Math.sin(i * 78.233) * 12345.678), w = fr(Math.sin(i * 39.425) * 24634.6345);
    return [-38 + u * 50, 0.3 + v * 9, -46 + w * 54, fr(u * 7 + v)];
  });
  const C = Array.from({ length: 8 }, () => ({ x: 0, y: 0, d: 0, s: 0 }));
  const W3 = [0, 0, 0];
  const P = {};
  const tmp = {};
  const vis = [false, false, false, false, false, false];
  const camP = [0, 0, 0];
  const buckets = [[], [], [], [], [], []];
  const drawn = [];              // what the frame drew, far first (reused frame to frame)

  // the eight corners of a box standing at s with edge e (mirrored below the floor when flip = -1)
  function corners(s, e, flip = 1, grow = 1) {
    at(s, P);
    const hh = (e / 2) * grow;
    const lift = flip > 0 ? (e - e * grow) / 2 : 0;
    for (let k = 0; k < 8; k++) {
      const a = k & 1 ? hh : -hh, b = k & 2 ? hh : -hh;
      W3[0] = P.x + P.ux * a - P.uz * b;
      W3[1] = flip * (lift + (k & 4 ? e * grow : 0));
      W3[2] = P.z + P.uz * a + P.ux * b;
      if (!cam.project(W3, C[k])) return false;
    }
    if (flip > 0) {   // which faces look at the camera
      const rx = camP[0] - P.x, ry = camP[1] - e / 2, rz = camP[2] - P.z;
      const du = rx * P.ux + rz * P.uz, dv = -rx * P.uz + rz * P.ux;
      vis[0] = -du > hh; vis[1] = du > hh; vis[2] = -dv > hh; vis[3] = dv > hh; vis[4] = -ry > hh; vis[5] = ry > hh;
    }
    return true;
  }

  function strokeEdges(ctx, which) {   // which: 1 front edges, 0 hidden edges, 2 all
    ctx.beginPath();
    for (const [a, b, f1, f2] of EDGES) {
      const front = vis[f1] || vis[f2];
      if (which !== 2 && (which === 1) !== front) continue;
      ctx.moveTo(C[a].x, C[a].y);
      ctx.lineTo(C[b].x, C[b].y);
    }
    ctx.stroke();
  }

  // a circle on the floor around the point s along the line
  function floorRing(ctx, s, r, rgb, a) {
    if (a <= 0.01) return;
    at(s, P);
    const cx = P.x, cz = P.z;
    ctx.beginPath();
    let open = false;
    for (let i = 0; i <= 48; i++) {
      const th = (i / 48) * TAU;
      W3[0] = cx + Math.cos(th) * r; W3[1] = 0; W3[2] = cz + Math.sin(th) * r;
      const p = cam.project(W3, tmp);
      if (!p) { open = false; continue; }
      if (open) ctx.lineTo(p.x, p.y); else { ctx.moveTo(p.x, p.y); open = true; }
    }
    ctx.globalCompositeOperation = "lighter";
    ctx.lineWidth = 1.2;
    ctx.strokeStyle = `rgba(${rgb}, ${a.toFixed(3)})`;
    ctx.stroke();
    ctx.globalCompositeOperation = "source-over";
  }

  // back: also draw the hidden edges, dimmer (off for far boxes only a few pixels across)
  function drawBox(ctx, s, e, rgb, a, { faces = 0.05, glow = 1, dashed = false, hot = 0, grow = 1, white = 0, back = true } = {}) {
    if (!corners(s, e, 1, grow)) return null;
    ctx.globalCompositeOperation = "source-over";
    if (faces > 0) {
      for (let i = 0; i < 6; i++) {
        if (!vis[i]) continue;
        const q = FACES[i];
        ctx.beginPath();
        ctx.moveTo(C[q[0]].x, C[q[0]].y);
        for (let j = 1; j < 4; j++) ctx.lineTo(C[q[j]].x, C[q[j]].y);
        ctx.closePath();
        ctx.fillStyle = `rgba(${rgb}, ${(faces * (i === 5 ? 1.7 : i < 2 ? 1.15 : 1) * a).toFixed(3)})`;
        ctx.fill();
      }
    }
    if (dashed) ctx.setLineDash([3, 3.5]);
    ctx.lineJoin = "round";
    ctx.lineWidth = 1;
    if (back) {
      ctx.strokeStyle = `rgba(${rgb}, ${(0.26 * a).toFixed(3)})`;
      strokeEdges(ctx, 0);
    }
    ctx.globalCompositeOperation = "lighter";
    if (glow > 0) {
      ctx.lineWidth = 3.2;
      ctx.strokeStyle = `rgba(${rgb}, ${(0.16 * a * glow).toFixed(3)})`;
      strokeEdges(ctx, 1);
    }
    ctx.lineWidth = 1.15 + hot * 0.5;
    ctx.strokeStyle = `rgba(${rgb}, ${(0.9 * a).toFixed(3)})`;
    strokeEdges(ctx, 1);
    if (white > 0) {
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = `rgba(255, 255, 255, ${(white * a).toFixed(3)})`;
      strokeEdges(ctx, 1);
    }
    if (dashed) ctx.setLineDash([]);
    ctx.globalCompositeOperation = "source-over";
    return true;
  }

  function draw(ctx, w, h, _t, dt) {
    const t = clk();
    const live = moving(look);
    const answering = lastSnap?.source?.answering !== false;
    // a slow sway while nobody is looking closely
    if (live && drift && !orbit.dragging && !selected && !hovered && t - lastTouch > IDLE_RESUME_S) {
      driftT += dt;
      cam.yaw += SWAY.yaw * SWAY.yawW * Math.cos(driftT * SWAY.yawW) * dt;
      cam.pitch = clamp(cam.pitch + SWAY.pitch * SWAY.pitchW * Math.cos(driftT * SWAY.pitchW) * dt, PITCH_MIN, PITCH_MAX);
    }
    if (homeFor !== `${w}x${h}`) setHome(w, h);
    // just opened: everything goes straight to where it is now (blocks that came
    // while the screen was hidden don't slide in as if they had just arrived)
    const snapNow = snapNext;
    snapNext = false;
    const easing = live && !snapNow;
    aim(easing ? 1 - Math.pow(0.04, dt) : 1);
    if (tipPending && settled()) { tipPending = false; if (selected) tipFor(selected); }
    cam.prepare(w, h);
    {
      const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw), cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
      camP[0] = cam.target[0] + cam.dist * cp * sy;
      camP[1] = cam.target[1] + cam.dist * sp;
      camP[2] = cam.target[2] + cam.dist * cp * cy;
    }

    // ease toward the layout; an arrival holds a moment first
    place();
    const k = easing ? (t < holdUntil ? 0 : 1 - Math.pow(0.12, dt)) : 1;
    const kA = easing ? Math.min(1, dt * 2.5) : 1;
    const kE = easing ? Math.min(1, dt * 2) : 1;
    for (const it of items.values()) {   // (deleting the current entry while iterating a Map is safe)
      if (it.s === null) { if (it.gone) items.delete(it.h); continue; }   // never placed: nothing to ease from
      it.s += (it.sT - it.s) * k;
      it.e += (it.eT - it.e) * kE;
      it.alpha += ((it.gone ? 0 : 1) - it.alpha) * kA;
      if (it.gone && (it.alpha < 0.02 || !easing)) items.delete(it.h);
    }
    ghost.alpha += ((t >= ghost.back && ghostOn() ? 1 : 0) - ghost.alpha) * (easing ? Math.min(1, dt * 1.6) : 1);
    if (!ghostOn()) ghost.sx = undefined;

    ctx.clearRect(0, 0, w, h);
    const acc = look.accent;
    const breathe = live && look.glow ? 0.5 + 0.5 * Math.sin(t * TAU / 7) : 0.55;
    const glowK = look.glow ? 0.75 + 0.5 * breathe : 0.3;
    const dim = answering ? 1 : 0.35;
    const fogOf = (d) => clamp(1.12 - (d - cam.dist) / 70, 0.05, 1);

    // light pooling on the floor around the front of the line (flattened, like a pool on the ground)
    const front = order.length ? order[order.length - 1] : null;
    at(front ? front.s * 0.6 : 0, P);
    W3[0] = P.x; W3[1] = 0; W3[2] = P.z;
    const fp = cam.project(W3, tmp);
    if (fp) {
      const pw = Math.min(w * 0.95, 820), ph = pw * clamp(0.22 + cam.pitch * 0.5, 0.25, 0.7);
      ctx.globalCompositeOperation = "lighter";
      ctx.globalAlpha = 0.7 * glowK * dim;
      ctx.drawImage(glowSprite(acc, 160, 0.2), fp.x - pw / 2, fp.y - ph * 0.62, pw, ph);
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
    }
    // a ring of light running out across the floor from a block that just landed
    for (const it of order) {
      const k = (t - it.flash) / 2.2;
      if (k < 0 || k >= 1 || !live) continue;
      floorRing(ctx, it.s, it.e * (0.75 + 5.5 * ease(k)), acc, 0.5 * (1 - k) * (1 - k) * dim);
      if (k > 0.12) floorRing(ctx, it.s, it.e * (0.75 + 3.2 * ease((k - 0.12) / 0.88)), acc, 0.28 * (1 - k) * dim);
    }

    // dust, for depth when it turns
    for (const d of dust) {
      const p = cam.project(d, tmp);
      if (!p) continue;
      const a = clamp(0.42 - (p.d - cam.dist) / 70, 0.03, 0.4) * (live ? 0.6 + 0.4 * Math.sin(t * 0.7 + d[3] * 9) : 0.8);
      ctx.fillStyle = `rgba(${acc}, ${a.toFixed(3)})`;
      ctx.fillRect(p.x, p.y, 1.2, 1.2);
    }

    // the floor: faint grid lines, fading with distance (strokes grouped by brightness)
    for (const b of buckets) b.length = 0;
    for (let dir = 0; dir < 2; dir++) {
      for (let g = -56; g <= 16; g += 4) {
        let px = 0, py = 0, have = false;
        for (let u = -64; u <= 16; u += 4) {
          W3[0] = dir ? g : u; W3[1] = 0; W3[2] = dir ? u : g;
          const p = cam.project(W3, tmp);
          if (!p) { have = false; continue; }
          if (have) {
            const a = clamp(1 - (p.d - cam.dist + 4) / 46, 0, 1);
            const bi = Math.min(buckets.length - 1, Math.floor(a * buckets.length));
            if (a > 0.04) buckets[bi].push(px, py, p.x, p.y);
          }
          px = p.x; py = p.y; have = true;
        }
      }
    }
    ctx.lineWidth = 1;
    buckets.forEach((b, i) => {
      if (!b.length) return;
      ctx.beginPath();
      for (let j = 0; j < b.length; j += 4) { ctx.moveTo(b[j], b[j + 1]); ctx.lineTo(b[j + 2], b[j + 3]); }
      ctx.strokeStyle = `rgba(${acc}, ${(0.075 * ((i + 0.5) / buckets.length) * dim).toFixed(3)})`;
      ctx.stroke();
    });

    // the line along the floor, from just ahead of the next block to the oldest
    const tail = order.length ? order[0].s + order[0].e : -3;
    ctx.lineWidth = 1;
    let prev = null;
    for (let s = -2.2; s <= tail; s += 0.6) {
      at(s, P);
      W3[0] = P.x; W3[1] = 0; W3[2] = P.z;
      const p = cam.project(W3, tmp);
      if (!p) { prev = null; continue; }
      if (prev) {
        const fade = Math.min(1, (s + 2.2) / 2.2) * fogOf(p.d);
        ctx.beginPath(); ctx.moveTo(prev.x, prev.y); ctx.lineTo(p.x, p.y);
        ctx.strokeStyle = `rgba(${acc}, ${(0.32 * fade * dim).toFixed(3)})`;
        ctx.stroke();
      }
      prev = { x: p.x, y: p.y };
    }

    // where each box stands on screen (for picking, the labels and the drawing below)
    drawn.length = 0;
    for (const it of items.values()) {
      if (it.s === null) { it.sx = undefined; continue; }
      at(it.s, P);
      W3[0] = P.x; W3[1] = it.e / 2; W3[2] = P.z;
      const p = cam.project(W3, tmp);
      if (!p) { it.sx = undefined; continue; }
      it.sx = p.x; it.sy = p.y; it.sr = Math.max(3, it.e * 0.5 * p.s); it.d = p.d; it.ps = p.s;
      drawn.push(it);
    }
    // far away the boxes stand a pixel or two apart and overlap many deep; under
    // "lighter" their strokes would add up to a smear brighter than the boxes nearer
    // you. Each box is thinned by how closely its neighbours crowd it, so a pile of
    // overlapping boxes is about as bright as one or two
    for (let i = 0; i < order.length; i++) {
      const it = order[i];
      if (it.sx === undefined) continue;
      let sum = 0, n = 0;
      const o = order[i - 1], nw = order[i + 1];
      if (o && o.sx !== undefined) { sum += Math.hypot(it.sx - o.sx, it.sy - o.sy); n++; }
      if (nw && nw.sx !== undefined) { sum += Math.hypot(it.sx - nw.sx, it.sy - nw.sy); n++; }
      it.crowd = n ? clamp((CROWD * sum) / n / Math.max(1, it.e * it.ps), 0.05, 1) : 1;
    }

    // reflections in the black floor (none for the far boxes only a few pixels across)
    ctx.lineWidth = 1;
    for (const it of order) {
      if (it.sx === undefined) continue;
      const rk = clamp((it.e * it.ps - SMALL_PX * 0.6) / (SMALL_PX * 0.8), 0, 1);
      if (rk <= 0 || !corners(it.s, it.e, -1)) continue;
      const fog = fogOf(C[0].d);
      ctx.strokeStyle = `rgba(${acc}, ${(0.13 * it.alpha * fog * dim * it.crowd * rk).toFixed(3)})`;
      strokeEdges(ctx, 2);
    }

    // the boxes, far ones first
    if (ghostOn() && ghost.alpha > 0.01) {
      at(ghost.s, P);
      W3[0] = P.x; W3[1] = ghost.e / 2; W3[2] = P.z;
      const p = cam.project(W3, tmp);
      if (p) { ghost.sx = p.x; ghost.sy = p.y; ghost.sr = Math.max(4, ghost.e * 0.5 * p.s); ghost.d = p.d; ghost.ps = p.s; drawn.push(ghost); }
      else ghost.sx = undefined;
    }
    drawn.sort((a, b) => b.d - a.d);
    const newest = order[order.length - 1];
    const late = lateLevel();
    for (const it of drawn) {
      const fog = fogOf(it.d);
      if (it.ghost) {
        const rgb = late === "bad" ? rgbBad : late === "warn" ? rgbWarn : acc;
        const pulse = live ? 0.5 + 0.5 * Math.sin(t * TAU / 2.4) : 0.6;
        const a = ghost.alpha * (0.45 + 0.4 * pulse);
        ctx.globalCompositeOperation = "lighter";
        ctx.globalAlpha = ghost.alpha * (0.35 + 0.35 * pulse) * glowK;
        stamp(ctx, glowSprite(rgb, 64, 0.5), it.sx, it.sy, ghost.e * it.ps * 2.6);
        ctx.globalAlpha = 1;
        if (live) ctx.lineDashOffset = -t * 6;
        drawBox(ctx, ghost.s, ghost.e, rgb, a, { faces: 0.025, glow: glowK, dashed: true, hot: it === selected || it === hovered ? 1 : 0 });
        // picked: still dashed (it isn't a block yet), lit white
        if (it === selected || it === hovered) drawBox(ctx, ghost.s, ghost.e, rgb, ghost.alpha, { faces: 0, glow: 0, dashed: true, white: 0.75 });
        ctx.lineDashOffset = 0;
        continue;
      }
      const isNew = it === newest;
      const repl = t - it.replaced < REPLACED_S;
      const flash = Math.max(0, 1 - (t - it.flash) / 1.6);
      const on = it === selected || it === hovered;
      let rgb = acc;
      let a = it.alpha * fog * dim * (isNew ? 1 : 0.78);
      if (repl) {
        rgb = rgbWarn;
        const blink = live && t - it.replaced < 3.2 ? (Math.sin((t - it.replaced) * TAU * 1.2) > 0 ? 1 : 0.35) : 1;
        a = it.alpha * Math.max(0.55, fog) * dim * blink;
      }
      // far boxes are only a few pixels across: one faint outline each, thinned where
      // they crowd together and fading with distance faster than the near ones, so the
      // far end of the line stays dimmer than its middle. A replaced block, the one
      // picked and the newest keep their full strength
      const px = it.e * it.ps;
      const plain = !on && !repl && !isNew && flash <= 0;
      const small = plain && px < SMALL_PX;
      if (plain) a *= it.crowd;
      if (small) { const sk = clamp((px - 2) / 14, 0.25, 1); a *= fog * sk * sk; }
      // glow under the box (none under the smallest)
      const gk = plain ? clamp((px - SMALL_PX * 0.6) / (SMALL_PX * 0.8), 0, 1) : 1;
      if (gk > 0) {
        ctx.globalCompositeOperation = "lighter";
        ctx.globalAlpha = Math.min(1, a * glowK * (isNew ? 0.9 : 0.5) * gk + flash * 0.9);
        stamp(ctx, glowSprite(rgb, 48, 0.5), it.sx, it.sy, px * (2.2 + 2.5 * flash));
        ctx.globalAlpha = 1;
      }
      drawBox(ctx, it.s, it.e, rgb, a, small ? { faces: 0, glow: 0, back: false }
        : { faces: (isNew ? 0.07 : 0.045) + 0.3 * flash * flash, glow: glowK * (isNew ? 1.2 : 0.8), hot: on ? 1 : 0, white: Math.max(flash * 0.95, on ? 0.75 : 0) });
      // the flash of an arrival: an outline that grows and fades
      if (flash > 0 && live) {
        drawBox(ctx, it.s, it.e, "255, 255, 255", flash * flash * 0.7, { faces: 0, glow: 0.6, grow: 1 + (1 - flash) * 0.7 });
        drawBox(ctx, it.s, it.e, acc, flash * flash * 0.45, { faces: 0, glow: 1, grow: 1 + (1 - flash) * 1.4 });
      }
    }

    labels(ctx, w, h, t, live, drawn, fogOf);
    // keep the readout's seconds in step with the timer drawn here
    if (live) { const a = age(); if ((a === null ? -1 : Math.round(a)) !== shownSec) tick(); }
  }

  // ---- words on the picture: the next block and its timer, the newest height,
  // long waits on the floor, replaced blocks, the one picked
  function labels(ctx, w, h, t, live, drawn, fogOf) {
    ctx.textBaseline = "alphabetic";
    ctx.lineJoin = "round";
    const newest = order[order.length - 1];
    const topOf = (it, extra) => {
      at(it.s, P);
      W3[0] = P.x; W3[1] = it.e + extra; W3[2] = P.z;
      return cam.project(W3, {});
    };
    const nearSide = (s) => {   // +1 or -1: which side of the line faces the camera
      at(s, P);
      return (-(camP[0] - P.x) * P.uz + (camP[2] - P.z) * P.ux) >= 0 ? 1 : -1;
    };
    const floorAt = (s, side) => {
      at(s, P);
      W3[0] = P.x - P.uz * side; W3[1] = 0; W3[2] = P.z + P.ux * side;
      return cam.project(W3, {});
    };
    // long waits, written in the gap
    for (let i = 1; i < order.length; i++) {
      const it = order[i];
      const iv = waitOf(it.b);
      if (!f.isNum(iv) || iv < LONG_WAIT) continue;
      const back = order[i - 1];
      const mid = (it.s + it.e / 2 + back.s - back.e / 2) / 2;
      const p = floorAt(mid, 0);
      const p0 = floorAt(it.s + it.e / 2, 0), p1 = floorAt(back.s - back.e / 2, 0);
      if (!p || !p0 || !p1) continue;
      const a = fogOf(p.d) * Math.min(it.alpha, back.alpha);
      if (a < 0.25 || Math.hypot(p1.x - p0.x, p1.y - p0.y) < 60) continue;   // only where the gap has room for the words
      text(ctx, `${f.wait(iv)} WAIT`, p.x, p.y + 16, { font: `500 10px ${MONO}`, rgb: ink2, a: 0.85 * a, align: "center" });
    }
    // the newest block's height, on the floor in front of it
    if (newest && newest.sx !== undefined && newest !== selected && newest !== hovered) {
      const p = floorAt(newest.s, (newest.e / 2 + 0.45) * nearSide(newest.s));
      if (p) text(ctx, f.num(newest.h), p.x, p.y + 12, { font: `400 10.5px ${MONO}`, rgb: ink2, a: 0.95 * newest.alpha, align: "center" });
    }
    // replaced blocks: one label over each run of them
    for (let i = 0; i < order.length; i++) {
      if (t - order[i].replaced >= REPLACED_S) continue;
      let j = i;
      while (j + 1 < order.length && t - order[j + 1].replaced < REPLACED_S) j++;
      const run = order.slice(i, j + 1).filter((it) => it.sx !== undefined);
      i = j;
      if (!run.length) continue;
      let x = 0, y = Infinity;
      for (const it of run) { const p = topOf(it, 0.35); if (p) { x += p.x / run.length; y = Math.min(y, p.y); } }
      if (y === Infinity) continue;
      const words = run.length > 1 ? `▲ ${run.length} REPLACED` : "▲ REPLACED";
      text(ctx, words, inside(x, textW(ctx, words, LABEL_FONT, 1.6) / 2, w), y - 4, { font: LABEL_FONT, rgb: rgbWarn, a: run[0].alpha, align: "center", gap: 1.6 });
    }
    // the next block: what it is and how long it has been
    if (ghostOn() && ghost.alpha > 0.05 && ghost.sx !== undefined) {
      const p = topOf(ghost, 0.3);
      const a = age();
      const late = lateLevel();
      if (p) {
        const rgbT = late === "bad" ? rgbBad : late === "warn" ? rgbWarn : ink;
        const word = late ? f.flag(late, "LATE · NEXT BLOCK") : "NEXT BLOCK";
        const tm = a === null ? "—" : f.wait(a);
        // both lines share a middle, moved in from the side so the longer late wording isn't cut off
        const x = inside(p.x, Math.max(textW(ctx, word, LABEL_FONT, 2), textW(ctx, tm, TIMER_FONT)) / 2, w);
        text(ctx, word, x, p.y - 26, { font: LABEL_FONT, rgb: late ? rgbT : head, a: ghost.alpha, align: "center", gap: 2 });
        text(ctx, tm, x, p.y - 6, { font: TIMER_FONT, rgb: rgbT, a: ghost.alpha, align: "center", glow: true });
      }
    } else if (syncing() && newest && newest.sx !== undefined) {
      const n = lastSnap?.node || {};
      const left = f.isNum(n.target_height) && f.isNum(n.height) ? Math.max(0, n.target_height - n.height) : null;
      const p = topOf(newest, 0.4);
      if (p) {
        const togo = left === null ? "—" : `${f.num(left)} TO GO`;
        const x = inside(p.x, Math.max(textW(ctx, "CATCHING UP", LABEL_FONT, 2), textW(ctx, togo, `400 13px ${MONO}`)) / 2, w);
        text(ctx, "CATCHING UP", x, p.y - 22, { font: LABEL_FONT, rgb: head, a: 1, align: "center", gap: 2 });
        text(ctx, togo, x, p.y - 6, { font: `400 13px ${MONO}`, rgb: ink, a: 1, align: "center" });
      }
    }
    // the picked or hovered block
    for (const it of [hovered, selected]) {
      if (!it || it.ghost || it.sx === undefined) continue;
      const p = topOf(it, 0.3);
      if (p) text(ctx, f.num(it.h), p.x, p.y - 6, { font: `500 11px ${MONO}`, rgb: "255, 255, 255", a: 1, align: "center" });
    }
  }

  // how wide a caption is drawn, letter spacing included
  function textW(ctx, s, font, gap = 0) {
    ctx.font = font;
    const sp = gap && "letterSpacing" in ctx;
    if (sp) ctx.letterSpacing = `${gap}px`;
    const wd = ctx.measureText(s).width;
    if (sp) ctx.letterSpacing = "0px";
    return wd;
  }
  // the middle for a centred caption half wide, moved in so it isn't cut off at the sides
  const inside = (x, half, w) => (w > half * 2 + 16 ? clamp(x, half + 8, w - half - 8) : w / 2);

  function text(ctx, s, x, y, { font, rgb, a, align = "left", gap = 0, glow = false }) {
    if (a <= 0.02) return;
    ctx.font = font;
    ctx.textAlign = align;
    const sp = gap && "letterSpacing" in ctx;
    if (sp) ctx.letterSpacing = `${gap}px`;
    const dx = sp && align === "center" ? gap / 2 : 0;
    ctx.lineWidth = 4;
    ctx.strokeStyle = `rgba(0, 0, 0, ${(0.85 * a).toFixed(3)})`;
    ctx.strokeText(s, x + dx, y);
    if (glow && look.glow) {
      ctx.globalCompositeOperation = "lighter";
      ctx.fillStyle = `rgba(${look.accent}, ${(0.18 * a).toFixed(3)})`;
      for (const [ox, oy] of [[-1, 0], [1, 0], [0, -1], [0, 1]]) ctx.fillText(s, x + dx + ox, y + oy);
      ctx.globalCompositeOperation = "source-over";
    }
    ctx.fillStyle = `rgba(${rgb}, ${a.toFixed(3)})`;
    ctx.fillText(s, x + dx, y);
    if (sp) ctx.letterSpacing = "0px";
  }

  // ---- readouts (the live wait ticks every second while shown)
  function hud(snap) {
    const src = snap.source || {};
    const node = snap.node || {};
    const chain = snap.chain || {};
    setText(hHeight, f.num(node.height));
    setText(hAvg, f.isNum(chain.avg_interval_s) ? `${f.wait(chain.avg_interval_s)} · AIM 2m 00s` : "—");
    // worded like the CHAIN panel; amber only while the dashboard raises it as a heads-up
    const r = chain.reorg || {};
    const al = (snap.alerts || []).find((x) => x && x.id === "reorg");
    const lvl = al ? (al.level === "bad" ? "bad" : al.level === "warn" ? "warn" : null) : null;
    // until the dashboard has had one reading it hasn't watched the chain at all: unknown, not "none"
    const neverRead = src.answering === false && !f.isNum(src.last_ok);
    const deepest = neverRead ? null : r.deepest_24h;
    if (f.isNum(deepest) && deepest > 0) setText(hReorg, f.flag(lvl, `${f.num(deepest)} AT ONCE`));
    else setText(hReorg, f.isNum(deepest) ? "NONE" : "—");
    setData(hReorg, "level", f.isNum(deepest) ? lvl : null);

    let why = null;
    if (src.answering === false) why = "YOUR NODE ISN'T ANSWERING · THE STREAM PICKS UP WHEN IT DOES";
    else if (!order.length && src.bootstrap) why = "YOUR NODE IS LEANING ON ANOTHER NODE WHILE IT CATCHES UP · ITS OWN BLOCKS SHOW HERE ONCE IT HAS THEM";
    else if (!order.length) why = "NO BLOCKS READ YET · THEY SHOW HERE AFTER THE NEXT READING";
    empty.hidden = !why;
    setText(empty, why || "");
    setData(view, "state", src.answering === false ? "down" : null);

    const n = order.length;
    setText(env.sub, src.answering === false ? "FROM YOUR NODE · NOT ANSWERING"
      : n ? `FROM YOUR NODE · LAST ${f.plural(n, "BLOCK", "BLOCKS")} · EVERY ${f.num(src.poll_s || 5)} S · NEWEST IN FRONT`
        : `FROM YOUR NODE · NO BLOCKS YET · EVERY ${f.num(src.poll_s || 5)} S`);

    // the picture's summary for screen readers: rebuilt once per reading and only
    // written when it changes (the ticking seconds stay in the readout; a name
    // that changed every second would be read out again and again)
    const newest = order[order.length - 1];
    const parts = [];
    if (src.answering === false) parts.push(newest ? `your node isn't answering; these are the last ${f.plural(n, "block", "blocks")} it gave` : "your node isn't answering");
    else if (newest) parts.push(`the last ${f.plural(n, "block", "blocks")}, newest ${f.num(newest.h)}`);
    else parts.push("no blocks to show yet");
    if (f.isNum(chain.avg_interval_s)) parts.push(`average gap ${f.wait(chain.avg_interval_s)} against the 2-minute aim`);
    if (src.answering !== false) {
      const late = lateLevel();
      if (syncing()) parts.push("your node is catching up, so the next block isn't timed");
      else if (late) parts.push(`the next block is late (${late === "bad" ? "a problem" : "a heads-up"})`);
    }
    if (f.isNum(deepest) && deepest > 0) parts.push(`blocks replaced in the last 24 hours: up to ${f.num(deepest)} at once${lvl ? ` (${lvl === "bad" ? "a problem" : "a heads-up"})` : ""}`);
    const label = `Block stream: ${parts.join("; ")}.${newest ? " Arrow keys step through the blocks; Shift and the arrow keys turn; plus and minus zoom." : ""}`;
    if (label !== ariaText) { ariaText = label; canvas.setAttribute("aria-label", label); }
    tick();
  }

  function tick() {
    const snap = lastSnap;
    if (!snap) return;
    const node = snap.node || {};
    const a = age();
    shownSec = a === null ? -1 : Math.round(a);
    const late = lateLevel();
    if (syncing()) {
      const left = f.isNum(node.target_height) && f.isNum(node.height) ? Math.max(0, node.target_height - node.height) : null;
      setText(hAge, "—");
      setText(hAgeUnit, "");
      setText(hOdds, "—");
      hSync.hidden = false;
      setText(hSync, `CATCHING UP · ${f.pct(node.sync_pct)}${left === null ? "" : ` · ${f.plural(left, "BLOCK", "BLOCKS")} TO GO`}`);
    } else {
      hSync.hidden = true;
      setText(hAge, a === null ? "—" : f.wait(a));
      setText(hAgeUnit, a === null ? "" : late ? `AGO · ${f.flag(late, "LATE")}` : "AGO");
      setText(hOdds, a === null ? "—" : f.odds(Math.exp(-a / TARGET)));
    }
    setData(hAge, "level", late);
    setData(hAgeUnit, "level", late);
  }

  // while shown: the readout, the timer and any details showing step on just as the
  // wait passes each half second (it is shown rounded), between readings as well
  function startTicker() {
    if (ticker) return;
    const due = () => { const a = age(); return a === null ? 1000 : 1020 - ((a + 0.5) % 1) * 1000; };
    const step = () => {
      tick();
      if (!stage.running) stage.frame();
      refreshTip();
      ticker = setTimeout(step, due());
    };
    ticker = setTimeout(step, due());
  }
  function stopTicker() { clearTimeout(ticker); ticker = 0; }

  function readColours() {
    rgbWarn = cssRGB("--warn-rgb", rgbWarn);
    rgbBad = cssRGB("--bad-rgb", rgbBad);
    ink = look.ink || cssRGB("--ink", ink);
    ink2 = cssRGB("--ink-2", ink2);
    head = look.head || cssRGB("--head", head);
  }
  readColours();

  return {
    update(snap, now) {
      lastSnap = snap;
      ingest(snap, now);
      hud(snap);
      if (selected) {
        if (selected.ghost ? !ghostOn() : !items.has(selected.h) || selected.gone) choose(null);
        else {
          place();
          // placed again once the line has moved (at once when still, after the easing when
          // moving); a block under the mouse keeps its details meanwhile
          if (shown && !settled()) { if (moving(look) && !(hovered && hovered !== selected)) env.tip.hideTip(); tipPending = true; }
        }
      }
      refreshTip();
      if (shown && !stage.running) stage.frame();
    },
    show() {
      shown = true;
      snapNext = true;
      readColours();
      startTicker();
      if (moving(look)) stage.start(); else stage.frame();
    },
    hide() {
      shown = false;
      stage.stop();
      stopTicker();
      selected = null;
      hovered = null;
      hoverAt = null;
      env.tip.hideTip();
    },
    look(lk) {
      look = lk;
      readColours();
      driftBtn.hidden = !moving(lk);
      if (!shown) return;
      if (moving(lk)) stage.start(); else { stage.stop(); stage.frame(); }
    },
  };
}
