// MEMPOOL screen hero: the transaction pool as a field of light. One glowing
// dot per transaction waiting to go into a block, put where its own numbers say:
// across is the fee it paid (or, with PER KB picked, its fee per kB, which is
// what miners sort by; the dashed lines are then your node's four fee levels),
// on a scale where each step to the right is ten times more; down is how long it
// has waited, newest at the top, the first minutes stretched and the long waits
// squeezed. The dashed line across marks the last block: the dots under it have
// waited through at least one block.
//
// What moves because of data: a transaction that just arrived drops in at the
// top over its fee, with a small flash; every dot sinks as it waits; when a block
// arrives, the dots for exactly the transactions it took (the node says which)
// fly into a block outline that slides away; one that leaves without a block
// (dropped or replaced) fades. A dot's size is the transaction's size and its
// brightness how new it is. What is decoration: the slow breathing of the glow.
// Arrivals only ever flash while the screen is shown; what changed while it was
// hidden is simply there when it opens.
import { el, setText, setData } from "../dom.js";
import * as f from "../fmt.js";
import { hasHidden } from "../privacy.js";
import { createStage, glowSprite, stamp, moving, clamp, lerp, ease, TAU, cssRGB } from "../fx/space.js";

const DROP_S = 0.8;            // a new dot's fall
const FADE_S = 1.1;            // a dot leaving without a block
const SWEEP_S = 1.05;          // a dot flying into the block
const LIMBO_S = 2.5;           // a dot that left waits this long for the block that took it before it fades
const BLOCK_IN = 0.35, BLOCK_HOLD = 2.5, BLOCK_SLIDE = 1.5;
const STILL_BLOCK_S = 12;      // without motion, the block outline stays this long after a reading
const LATE_S = 0.3;            // a drop that finished longer ago than this (screen just opened) lands without a flash
const NEW_S = 20;              // a transaction younger than this when it first shows up drops in; older ones fade in
const BREATHE_S = 7;           // one slow glow cycle
const TARGET_S = 120;          // Monero aims for one block every 2 minutes
const TAU_S = 30;              // the time scale: equal steps down for 0 to 30 s, 30 s to 1.5 min, 1.5 to 3.5 min...
const MANY = 1500;             // past this many dots, plain dots without their own glow (and fewer frames)
const FEE_FLOOR = 1e5;         // atomic units (0.0000001 XMR, far under any real fee): a fee under it, or none, sits at it rather than stretching the scale
const FLAG_DOUBLE_SPEND = 1, FLAG_NOT_RELAYED = 2, FLAG_TIME_GUESSED = 4;   // pool.py
const LEVELS = ["LOW", "NORMAL", "HIGH", "HIGHEST"];
const AGE_TOPS = [120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 345600, 604800, 1209600];
const AGE_TICKS = [0, 10, 30, 60, 120, 300, 600, 1800, 3600, 7200, 21600, 43200, 86400, 172800, 604800];
const STORE = "memstara.mempool.across";
const FONT = '"Plex Mono", ui-monospace, monospace';

const fr = (x) => x - Math.floor(x);
const hash = (n) => fr(Math.sin(n * 12.9898 + 4.1414) * 43758.5453);
const clock = () => performance.now() / 1000;
const easeIn = (t) => t * t * t;
const ageF = (s) => Math.log1p(Math.max(0, s) / TAU_S);
const XMR = 1e12;

// a dot that still stands for a transaction in the pool
const present = (d) => d.st === "live" || d.st === "drop" || d.st === "fadein";

// which of the node's four fee levels a fee per byte reaches (a wallet pays
// the level times the transaction's weight, rounded up): -1 under the lowest
function levelOf(perByte, levels) {
  if (!levels) return null;
  let k = -1;
  for (let i = 0; i < levels.length; i++) if (perByte >= levels[i] * 0.95) k = i;
  return k;
}

// an amount on the fee axis, short: "0.00003", "0.0002", "0.01", "1.5"
function feeTick(xmr) {
  if (xmr >= 1) return String(Number(xmr.toPrecision(2)));
  const d = Math.max(0, Math.ceil(-Math.log10(xmr)) + 1);
  return xmr.toFixed(Math.min(12, d)).replace(/0+$/, "").replace(/\.$/, "");
}

// an amount in a tooltip: enough digits to tell the levels apart
function feeText(atomic) {
  if (!f.isNum(atomic)) return f.DASH;
  const v = atomic / XMR;
  if (v === 0) return "0 XMR";
  const digits = clamp(Math.ceil(-Math.log10(v)) + 2, 4, 12);
  return f.xmr(atomic, digits);
}

// an age on the axis, short enough for a phone: "45s", "7m", "1h", "1h 30m", "3d"
function axisAge(s) {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return s % 3600 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m` : `${s / 3600}h`;
  return `${Math.round(s / 86400)}d`;
}

// the pool against one block's room: the node's median block weight (what a
// block holds before the miner's reward starts to shrink), else the average
// weight of the recent blocks
function roomOf(snap) {
  const mp = snap.mempool || {};
  let per = snap.chain?.median_weight;
  let from = "median";
  if (!(f.isNum(per) && per > 0)) {
    const ws = (snap.chain?.blocks || []).map((b) => b.weight).filter((w) => f.isNum(w) && w > 0);
    per = ws.length ? ws.reduce((a, b) => a + b, 0) / ws.length : null;
    from = "recent";
  }
  if (!f.isNum(mp.bytes) || !per) return null;
  return { ratio: mp.bytes / per, per, from };
}

function worthWords(r) {
  if (r <= 0) return "NOTHING WAITING";
  if (r < 0.1) return "UNDER A TENTH OF A BLOCK";
  if (r < 0.95) return `ABOUT ${Math.max(10, Math.round(r * 20) * 5)}% OF A BLOCK`;
  if (r < 1.5) return "ABOUT 1 BLOCK'S WORTH";
  return `ABOUT ${r < 10 ? r.toFixed(1).replace(/\.0$/, "") : f.num(Math.round(r))} BLOCKS' WORTH`;
}

function loadAcross() {
  try { return localStorage.getItem(STORE) === "perkb" ? "perkb" : "fee"; } catch { return "fee"; }
}
function saveAcross(v) {
  try { localStorage.setItem(STORE, v); } catch { /* storage blocked: it lasts until the window closes */ }
}

export default function create(body, env) {
  // ---- DOM: the picture with its legend and caption, and the readout column
  const canvas = el("canvas", {
    class: "tp-canvas", tabindex: "0", role: "img",
    "aria-label": "Transaction pool: one dot per waiting transaction, across by the fee it paid, down by how long it has waited. The arrow keys step through them.",
  });
  const lgDs = el("span", { class: "tp-lg tp-lg-ds", text: "▲ DOUBLE SPEND", hidden: true });
  const lgNr = el("span", { class: "tp-lg tp-lg-nr", text: "NOT SENT ON YET", hidden: true });
  const legend = el("div", { class: "hero-hud tp-legend" },
    el("span", { class: "tp-lg tp-lg-new", text: "BRIGHTER · NEWER" }),
    el("span", { class: "tp-lg tp-lg-size", text: "BIGGER · LARGER" }),
    lgNr, lgDs);
  const cap = el("p", { class: "tp-cap" });
  const empty = el("div", { class: "tp-empty", hidden: true });
  const view = el("div", { class: "tp-view" }, el("div", { class: "tp-pic" }, canvas, empty), legend, cap);

  const big = el("b", { class: "tp-big", text: "—" });
  const bigUnit = el("span", { class: "tp-unit", text: "TRANSACTIONS" });
  const bigNote = el("div", { class: "tp-note", text: "—" });
  const row = (label) => {
    const dd = el("dd", { text: "—" });
    return { dd, node: el("div", {}, el("dt", { text: label }), dd) };
  };
  const rOld = row("OLDEST");
  const r10 = row("OVER 10 MIN");
  const rLv = row("BY FEE LEVEL");
  // the four levels always in the same two rows, so the column never changes height
  const lvCells = LEVELS.map((n) => {
    const v = el("b", { text: "—" });
    return { v, node: el("span", { class: "tp-lv-c" }, el("span", { text: n }), v) };
  });
  rLv.node.className = "tp-kv-lv";
  rLv.dd.textContent = "";
  rLv.dd.append(el("span", { class: "tp-lv" }, ...lvCells.map((c) => c.node)));
  const rNr = row("NOT SENT ON YET");
  const rDs = row("DOUBLE SPENDS");
  const dsNote = el("p", { class: "tp-ds-note", hidden: true });
  const roomVal = el("div", { class: "tp-room-v", text: "—" });
  const roomFill = el("i", { class: "tp-bar-fill" });
  const leds = el("div", { class: "tp-leds", "aria-hidden": "true" });
  for (let i = 0; i < 10; i++) leds.append(el("i"));
  const roomNote = el("div", { class: "tp-note", text: "—" });
  const room = el("div", { class: "tp-box tp-room", tabindex: "0" },
    el("div", { class: "tp-lab", text: "POOL AGAINST ONE BLOCK" }), roomVal,
    el("div", { class: "tp-bar", "aria-hidden": "true" }, roomFill), leds, roomNote);
  const each = el("div", { class: "tp-each", text: "—" });
  const side = el("aside", { class: "tp-side", "aria-label": "Pool readings" },
    el("div", { class: "tp-box tp-count" },
      el("div", { class: "tp-lab", text: "WAITING" }),
      el("div", { class: "tp-bigline" }, big, bigUnit), bigNote),
    el("dl", { class: "kv tp-kv" }, rOld.node, r10.node, rLv.node, rNr.node, rDs.node),
    dsNote, room, each);
  const say = el("p", { class: "sr-only", "aria-live": "polite" });
  body.append(el("div", { class: "tp" }, view, side), say);

  // header tools: what "across" means
  let across = loadAcross();
  const acrossBtns = [["fee", "FEE PAID", "Across: the fee each transaction paid"],
    ["perkb", "PER KB", "Across: each transaction's fee per kB, what miners sort by, with your node's four fee levels"]]
    .map(([k, text, title]) => {
      const b = el("button", { type: "button", role: "radio", "aria-checked": String(k === across), text, title });
      b.addEventListener("click", () => setAcross(k));
      return b;
    });
  const acrossCtl = el("div", { class: "seg-ctl tp-across", role: "radiogroup", "aria-label": "What across means" }, ...acrossBtns);
  if (env.tools) env.tools.append(acrossCtl);

  // ---- state
  let look = env.look();
  let warnRGB = cssRGB("--warn-rgb", "255, 112, 64");
  let shown = false;
  const dots = new Map();     // transaction id -> dot
  let serial = 0;
  let seq = 0;                // readings seen
  let prevHeight = null;
  let prevIds = new Set();    // the transactions in the reading before
  let mined = new Map();      // transaction id -> height of the (recent) block that took it
  let block = null;           // the outline carrying the latest block away
  let pickedId = null;        // the dot under the pointer (or picked by key / tap)
  let lastSnap = null;
  let state = "wait";         // ok | empty | down | none | catching
  let stale = false;          // down or catching: the last picture stays, dimmed and still
  let heldAt = null;          // while stale: the moment the picture stands still at (unix s)
  let lag = 0;                // just back: how far the picture's time is behind now (s), easing to 0 so the dots glide
  let poolAt = null;          // when the picture's pool reading was taken (unix s)
  let levels = null;          // the node's four fee levels, per byte
  let lastBlockAt = null;     // when the newest block reached your node (never its miner's stamp, which can be minutes early)
  let lvCounts = null;        // waiting transactions at each fee level, [under low, low, normal, high, highest]
  let nrSeenAt = -1e9;        // when a dot not sent on yet, or a double spend, was last in the picture (s, this page's
  let dsSeenAt = -1e9;        //   clock): their legend entries stay a while, so the legend doesn't blink
  let overlay = true;        // legend and caption sit over the picture (wide) or under it (phone)
  let geo = null;             // last frame's layout
  let capH = 18;              // the caption's height while it sits over the picture
  let legH = 14;              // the legend's height while it sits over the picture
  // the scales, each easing toward where the readings put it
  const xs = { lo: -5, hi: -3, tlo: -5, thi: -3, ok: false };   // log10 of XMR (or XMR per kB)
  const ys = { top: ageF(600), want: ageF(600), maxAge: 600 };

  const stage = createStage(canvas, { draw, maxFps: 30, onResize: () => { kick = true; measure(); } });
  let lastPaint = 0;          // when the last frame was drawn (s, this page's clock)
  let busy = true;            // something in the picture is moving faster than the slow sink (a drop, a block, a scale easing)
  let kick = true;            // draw the next frame whatever the rate: a reading, a tap or a resize just changed something
  function measure() {
    overlay = getComputedStyle(cap).position === "absolute";
    if (cap.offsetHeight) capH = cap.offsetHeight;
    if (legend.offsetHeight) legH = legend.offsetHeight;
  }
  measure();

  // ---- the across value of a dot (log10 of XMR, or of XMR per kB)
  function acrossOf(d) {
    const v = across === "perkb" ? (d.fee / Math.max(1, d.w)) * 1000 : d.fee;
    return Math.log10(Math.max(v, FEE_FLOOR) / XMR);
  }
  // the across scale: every dot in view (and in PER KB, the fee levels), on round edges, at least one step of ten wide
  function aimAcross() {
    let lo = Infinity, hi = -Infinity;
    for (const d of dots.values()) {
      if (d.st === "gone") continue;
      lo = Math.min(lo, d.lt);
      hi = Math.max(hi, d.lt);
    }
    if (across === "perkb" && levels) {
      for (const v of levels) {
        const l = Math.log10((v * 1000) / XMR);
        lo = Math.min(lo, l);
        hi = Math.max(hi, l);
      }
    }
    if (!Number.isFinite(lo)) return;
    lo = niceDown(lo - 0.08);
    hi = niceUp(hi + 0.08);
    if (hi - lo < 1) {
      const mid = (lo + hi) / 2;
      lo = niceDown(mid - 0.5);
      hi = niceUp(mid + 0.5);
    }
    xs.tlo = lo; xs.thi = hi;
    if (!xs.ok) { xs.lo = lo; xs.hi = hi; xs.ok = true; }
  }
  // round edges for a log scale: 1, 2 or 5 times a power of ten
  function niceDown(l) {
    const k = Math.floor(l);
    const m = Math.pow(10, l - k);
    return k + Math.log10(m >= 5 ? 5 : m >= 2 ? 2 : 1);
  }
  function niceUp(l) {
    const k = Math.floor(l);
    const m = Math.pow(10, l - k);
    if (m <= 1.0001) return k;
    return k + Math.log10(m <= 2 ? 2 : m <= 5 ? 5 : 10);
  }
  // the time scale reaches past the oldest dot, on a round age
  function aimDown(oldest) {
    const want = Math.max(TARGET_S, (oldest || 0) * 1.08);
    const top = AGE_TOPS.find((a) => a >= want) || Math.ceil(want / 86400) * 86400;
    ys.maxAge = top;
    ys.want = ageF(top);
  }

  // ---- dots
  function newDot(e) {
    serial++;
    const s = serial;
    const d = {
      id: e[0], t: e[1], fee: e[2], w: e[3], flags: e[4],
      h: hash(s), j: (hash(s + 3.3) - 0.5) * 2,
      st: "live", t0: 0, dur: 1, fx: NaN, fy: NaN, x: NaN, y: NaN, flash: -9, born: seq,
      lt: 0, lv: 0, tx: 0, ty: 0, freeze: null, blk: null,
    };
    d.lt = d.lv = acrossOf(d);
    return d;
  }
  // start a dot on its next step from where it was last drawn; one that has
  // never been drawn (the screen was hidden) skips straight to the end
  function begin(d, st, t0, dur) {
    d.st = st; d.t0 = t0; d.dur = dur;
    d.fx = d.x; d.fy = d.y;
    if (st !== "drop" && st !== "fadein" && !Number.isFinite(d.x)) d.st = present(d) ? "live" : "gone";
  }
  function leave(d, t) {
    const h = mined.get(d.id);
    if (h !== undefined && block && block.heights.has(h) && block.open) {
      begin(d, "sweep", t + 0.15 + Math.random() * 0.75, SWEEP_S * (0.85 + Math.random() * 0.3));
      d.tx = (Math.random() - 0.5) * 0.7;
      d.ty = (Math.random() - 0.5) * 0.7;
      // it counts toward the block that took it, even if another block comes before it lands
      if (d.st === "sweep") { d.blk = block; block.expect++; }
    } else if (d.st === "limbo") {
      begin(d, "out", t + Math.random() * 0.3, FADE_S);
    } else {
      // it may be in a block the dashboard hasn't seen yet: hold it a moment
      d.freeze = lastAge(d);
      begin(d, "limbo", t, LIMBO_S);
    }
  }
  // the picture's time: now; held where it stood while the node is away; just
  // after it is back, a little behind now and catching up, so nothing jumps
  function picNow(now) {
    return heldAt ?? now - lag;
  }
  // the age a dot is drawn at
  function ageNow(d, now) {
    return Math.max(0, picNow(now) - d.t);
  }
  function lastAge(d) {
    return ageNow(d, env.now());
  }
  // how long it had waited, for its details: now, or (node away) as of the last reading
  // (not the picture's time, which may still be catching up)
  function waited(d, now) {
    return Math.max(0, (stale && f.isNum(poolAt) ? poolAt : now) - d.t);
  }

  // while the screen isn't shown nothing is drawn, so nothing is half-way:
  // dots that left are gone, dots that came are simply there
  function settle() {
    for (const [id, d] of dots) {
      if (!present(d)) { dots.delete(id); continue; }
      d.st = "live";
      d.flash = -9;
    }
    if (block) { block.expect = 0; block.got = 0; }
    lag = 0;
  }

  function setAcross(k) {
    if (k === across) return;
    across = k;
    saveAcross(k);
    acrossBtns.forEach((b, i) => b.setAttribute("aria-checked", String((i === 0 ? "fee" : "perkb") === k)));
    for (const d of dots.values()) d.lt = acrossOf(d);
    aimAcross();
    if (!moving(look)) { xs.lo = xs.tlo; xs.hi = xs.thi; for (const d of dots.values()) d.lv = d.lt; }
    if (lastSnap) hud(lastSnap);
    redraw();
  }

  // ---- readings
  function ingest(snap) {
    const src = snap.source || {};
    const mp = snap.mempool || {};
    const t = clock();
    const txs = mp.txs;
    const before = state;
    if (src.answering === false) state = "down";
    else if (src.bootstrap && !f.isNum(txs)) state = "catching";
    else if (!f.isNum(txs)) state = "none";
    else if (txs === 0) state = "empty";
    else state = "ok";
    const ok = state === "ok" || state === "empty";
    // the first pool reading, or the first after the node was away: what is
    // there fades in and what went fades out (nothing arrived or was mined just now)
    const fresh = ok && before !== "ok" && before !== "empty";
    const wasStale = stale;
    stale = state === "down" || state === "catching";
    if (stale && !wasStale) heldAt = picNow(env.now());   // the picture stands still where it is
    else if (!stale && wasStale) {
      // back: the picture's time catches up with now over a moment (straight away without motion)
      lag = heldAt != null && shown && moving(look) ? Math.max(0, env.now() - heldAt) : 0;
      heldAt = null;
    }
    levels = Array.isArray(snap.node?.fee_per_byte) && snap.node.fee_per_byte.length === 4 ? snap.node.fee_per_byte : levels;
    const top = (snap.chain?.blocks || []).at(-1);
    // the last-block line goes where the block reached your node; until the
    // dashboard has seen one come in, there is no line rather than a guess
    const lb = snap.node?.last_block_seen;
    if (f.isNum(lb)) lastBlockAt = lb;

    // which transactions the newest blocks took (the node says, per block)
    mined = new Map();
    for (const m of Array.isArray(mp.mined) ? mp.mined : []) for (const id of m.ids || []) mined.set(id, m.height);

    // a new block: the height went up
    const h = snap.node?.height;
    const up = f.isNum(h) && f.isNum(prevHeight) && h > prevHeight && !fresh && ok;
    if (up) {
      const heights = new Set();
      for (let x = prevHeight; x < h; x++) heights.add(x);
      let here = 0;
      for (const [id, bh] of mined) if (heights.has(bh) && (prevIds.has(id) || dots.get(id)?.st === "limbo")) here++;
      const got = (snap.chain?.blocks || []).filter((b) => heights.has(b.height) && f.isNum(b.txs));
      block = {
        t0: t, at: snap.generated || 0, height: top && top.height >= prevHeight ? top.height : h - 1, heights,
        count: heights.size, txs: got.length === heights.size ? got.reduce((n, b) => n + b.txs, 0) : null,
        here, expect: 0, got: 0, full: -9, open: true,
      };
    }
    if (f.isNum(h)) prevHeight = h;

    if (ok) {
      const list = Array.isArray(mp.list) ? mp.list : [];
      const now = env.now();
      const spread = clamp((Number(src.poll_s) || 1) * 0.8, 0.4, 4);
      const ids = new Set();
      const arrivals = [];
      for (const e of list) {
        if (!Array.isArray(e) || typeof e[0] !== "string") continue;
        ids.add(e[0]);
        let d = dots.get(e[0]);
        // (one missing from a single reading and back in the next never left: it stays where it is)
        if (d && d.st === "limbo") { d.st = "live"; d.freeze = null; }
        if (d && present(d)) {
          if (d.flags !== e[4] || d.fee !== e[2] || d.w !== e[3] || d.t !== e[1]) {
            d.t = e[1]; d.fee = e[2]; d.w = e[3]; d.flags = e[4];
            d.lt = acrossOf(d);
          }
          continue;
        }
        if (d && d.st === "sweep" && d.blk) {
          // back in the pool while flying into a block (the block was undone): it no longer counts toward it
          const b = d.blk;
          b.expect = Math.max(0, b.expect - 1);
          if (b.expect && b.got >= b.expect && b.full < 0) b.full = t;
        }
        d = newDot(e);
        dots.set(d.id, d);
        if (fresh || seq === 0) {
          d.born = -1;   // the first picture: these were there already, not arrivals
          begin(d, "fadein", t + Math.random() * 0.9, 0.7);
        } else if (now - d.t < NEW_S) arrivals.push(d);
        else begin(d, "fadein", t + Math.random() * 0.4, 0.7);   // an older one just read (a big pool comes in over a few readings)
      }
      arrivals.forEach((d, k) => {
        d.arrived = true;
        begin(d, "drop", t + ((k + Math.random() * 0.8) / arrivals.length) * spread, DROP_S);
      });
      // the ones that left: into the block that took them, else held, then faded
      for (const d of dots.values()) {
        if (ids.has(d.id)) continue;
        if (present(d)) leave(d, t);
        else if (d.st === "limbo" && block && block.open && block.heights.has(mined.get(d.id))) leave(d, t);
      }
      prevIds = ids;
      poolAt = f.isNum(snap.t) ? snap.t : f.isNum(snap.generated) ? snap.generated : null;
      let oldest = 0;
      for (const d of dots.values()) if (present(d)) oldest = Math.max(oldest, now - d.t);
      aimDown(oldest);
      aimAcross();
      if (!shown || !moving(look)) {
        // no motion: everything goes straight to where it belongs
        xs.lo = xs.tlo; xs.hi = xs.thi; ys.top = ys.want;
        for (const d of dots.values()) d.lv = d.lt;
      }
    } else if (!stale) {
      for (const d of dots.values()) if (present(d)) begin(d, "out", t + Math.random() * 0.4, FADE_S);
      prevIds = new Set();
    }
    // (down or catching: the last picture stays, dimmed and still; nothing new arrives)
    if (!shown) settle();
    else for (const [id, d] of dots) if (d.st === "gone") dots.delete(id);
    seq++;
    kick = true;
    const pd = pickedId && dots.get(pickedId);
    if (pickedId && !(pd && present(pd))) { pickedId = null; env.tip.hideTip(); }
  }

  // ---- drawing
  function layout(w, h) {
    const compact = w < 560;
    const top = overlay ? Math.max(40, legH + 26) : 26;
    const bottom = h - (overlay ? capH + 18 : 4) - 38;
    const axisX = compact ? 40 : 52;
    const x0 = axisX + 14;
    const x1 = w - (compact ? 14 : 22);
    return { w, h, compact, top, bottom, axisX, x0, x1 };
  }
  const xAt = (g, l) => g.x0 + ((l - xs.lo) / Math.max(1e-6, xs.hi - xs.lo)) * (g.x1 - g.x0);
  const yAt = (g, age) => g.top + 6 + (ageF(age) / Math.max(1e-6, ys.top)) * (g.bottom - g.top - 12);

  function draw(ctx, w, h) {
    const T = clock();
    const live = moving(look);
    // frames: 30 a second while something moves quickly (15 with plenty of dots,
    // so the window stays light); 10 while the dots only sink, which is slow
    // enough to stay smooth; a new reading, a tap or a resize draws at once
    if (live && stage.running && !kick) {
      const gap = busy ? (dots.size > MANY ? 1 / 15 : 0) : 1 / 10;
      if (T - lastPaint < gap - 0.004) return;
    }
    kick = false;
    const dt = lastPaint ? Math.min(0.1, T - lastPaint) : 0;
    lastPaint = T;
    // just back from the node being away: the picture's time catches up
    if (lag) {
      lag = live ? lag * Math.exp(-dt * 2.6) : 0;
      if (lag < 0.05) lag = 0;
    }
    const now = env.now();
    const g = layout(w, h);
    geo = g;
    // ease the scales and each dot's across value toward the readings
    const k = live && dt > 0 ? 1 - Math.exp(-dt * 3.2) : 1;
    xs.lo += (xs.tlo - xs.lo) * k;
    xs.hi += (xs.thi - xs.hi) * k;
    ys.top += (ys.want - ys.top) * k;
    let easing = Math.abs(xs.tlo - xs.lo) + Math.abs(xs.thi - xs.hi) + Math.abs(ys.want - ys.top) > 2e-3;
    for (const d of dots.values()) {
      d.lv += (d.lt - d.lv) * k;
      if (Math.abs(d.lt - d.lv) > 2e-3) easing = true;
    }

    ctx.clearRect(0, 0, w, h);
    const acc = look.accent || "255, 184, 64";
    const head = look.head || "107, 174, 200";
    const ink = look.ink || "232, 232, 240";
    const dim = stale ? 0.65 : 1;
    const glowOn = look.glow !== false;
    const breathe = live && glowOn && !stale ? 0.86 + 0.14 * Math.sin((T * TAU) / BREATHE_S) : 1;

    // newest light: a soft wash at the top of the field, where transactions arrive
    if (state === "ok") {
      const grad = ctx.createLinearGradient(0, g.top, 0, g.top + (g.bottom - g.top) * 0.3);
      grad.addColorStop(0, `rgba(${acc}, ${(glowOn ? 0.075 : 0.04) * breathe})`);
      grad.addColorStop(1, `rgba(${acc}, 0)`);
      ctx.fillStyle = grad;
      ctx.fillRect(g.axisX, g.top, g.x1 - g.axisX + 4, g.bottom - g.top);
    }

    drawAxes(ctx, g, head, ink, acc, dim, now);
    const stirring = drawDots(ctx, g, T, now, live, acc, dim, breathe, glowOn);
    if (block) drawBlock(ctx, g, T, live, acc, ink, glowOn);
    drawNote(ctx, g, ink);
    busy = !!block || lag > 0 || easing || stirring;
    // an old picture that has finished moving needs no more frames
    if (stale && stage.running && !block && [...dots.values()].every((d) => d.st === "live")) stage.stop();
  }

  function spaced(ctx, s, x, y, gap) {
    const sp = "letterSpacing" in ctx;
    if (sp) ctx.letterSpacing = `${gap}px`;
    ctx.fillText(s, x + (sp && ctx.textAlign === "right" ? gap : 0), y);
    if (sp) ctx.letterSpacing = "0px";
  }

  function drawAxes(ctx, g, head, ink, acc, dim, now) {
    const fs = g.compact ? 9 : 10;
    ctx.textBaseline = "middle";
    // column heads: what down and across mean
    ctx.font = `600 ${fs}px ${FONT}`;
    ctx.fillStyle = `rgba(${head}, ${0.95 * dim})`;
    ctx.textAlign = "left";
    spaced(ctx, "WAITED ↓", g.axisX - (g.compact ? 34 : 44), g.top - 14, 1.4);
    ctx.textAlign = "right";
    spaced(ctx, across === "perkb" ? "FEE PER KB, XMR →" : "FEE PAID, XMR →", g.x1, g.bottom + 30, 1.4);
    // frame: the time axis and the fee axis
    ctx.lineWidth = 1;
    ctx.strokeStyle = `rgba(${head}, ${0.38 * dim})`;
    ctx.beginPath();
    ctx.moveTo(g.axisX + 0.5, g.top); ctx.lineTo(g.axisX + 0.5, g.bottom);
    ctx.moveTo(g.axisX, g.bottom + 0.5); ctx.lineTo(g.x1, g.bottom + 0.5);
    ctx.stroke();
    if (!xs.ok) return;

    // time ticks: the age at each line, short forms, never crowded
    ctx.font = `500 ${fs}px ${FONT}`;
    ctx.textAlign = "right";
    // (the scale's own end always gets its label; one just above it that would crowd it gives way)
    const ticks = [];
    for (const a of [...AGE_TICKS.filter((v) => v < ys.maxAge), ys.maxAge]) {
      const y = yAt(g, a);
      if (y > g.bottom + 1) break;
      const last = ticks[ticks.length - 1];
      if (last && y - last.y < fs + 5) {
        if (a !== ys.maxAge) continue;
        ticks.pop();
      }
      ticks.push({ a, y });
    }
    const rules = ticks.map((k) => k.y);
    for (const { a, y } of ticks) {
      ctx.fillStyle = `rgba(${ink}, ${(a === 0 ? 0.62 : 0.46) * dim})`;
      spaced(ctx, axisAge(a), g.axisX - 7, y, 0.4);
    }
    ctx.setLineDash([1, 3]);
    ctx.strokeStyle = `rgba(${head}, ${0.14 * dim})`;
    ctx.beginPath();
    for (const y of rules) { ctx.moveTo(g.axisX + 4, Math.round(y) + 0.5); ctx.lineTo(g.x1, Math.round(y) + 0.5); }
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.strokeStyle = `rgba(${head}, ${0.55 * dim})`;
    ctx.beginPath();
    for (const y of rules) { ctx.moveTo(g.axisX - 3, Math.round(y) + 0.5); ctx.lineTo(g.axisX + 3, Math.round(y) + 0.5); }
    ctx.stroke();

    // fee ticks: 1, 2 and 5 times each power of ten, as many as fit
    ctx.textAlign = "center";
    const minGap = g.compact ? 52 : 64;
    const marks = [];
    for (let p = Math.floor(xs.lo); p <= Math.ceil(xs.hi); p++) {
      for (const m of [1, 2, 5]) {
        const l = p + Math.log10(m);
        if (l < xs.lo - 0.02 || l > xs.hi + 0.02) continue;
        marks.push({ l, major: m === 1 });
      }
    }
    // the powers of ten first, then the 2s and 5s where there's room; a label at
    // either end moves in just enough to stay whole
    const placed = [];
    for (const pass of [true, false]) {
      for (const mk of marks) {
        if (mk.major !== pass) continue;
        const x = xAt(g, mk.l);
        const s = feeTick(Math.pow(10, mk.l));
        const half = (ctx.measureText(s).width + s.length * 0.3) / 2;
        const lx = clamp(x, g.axisX + half, g.w - 2 - half);
        if (placed.some((q) => Math.abs(q - lx) < minGap)) continue;
        placed.push(lx);
        ctx.fillStyle = `rgba(${ink}, ${(mk.major ? 0.55 : 0.4) * dim})`;
        spaced(ctx, s, lx, g.bottom + 12, 0.3);
        ctx.strokeStyle = `rgba(${head}, ${0.5 * dim})`;
        ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, g.bottom - 2); ctx.lineTo(Math.round(x) + 0.5, g.bottom + 4); ctx.stroke();
      }
    }

    // PER KB: the node's four fee levels, where a wallet's choice puts a transaction
    if (across === "perkb" && levels) {
      ctx.setLineDash([3, 4]);
      ctx.font = `600 ${g.compact ? 8.5 : 9}px ${FONT}`;
      ctx.textAlign = "center";
      let lastLx = -1e9;
      levels.forEach((v, i) => {
        const l = Math.log10((v * 1000) / XMR);
        if (l < xs.lo - 0.02 || l > xs.hi + 0.02) return;
        const x = Math.round(xAt(g, l)) + 0.5;
        ctx.strokeStyle = `rgba(${head}, ${0.42 * dim})`;
        ctx.beginPath(); ctx.moveTo(x, g.top - 1); ctx.lineTo(x, g.bottom); ctx.stroke();
        if (x - lastLx < (g.compact ? 44 : 56)) return;
        lastLx = x;
        const half = (ctx.measureText(LEVELS[i]).width + LEVELS[i].length) / 2;
        ctx.fillStyle = `rgba(${head}, ${0.9 * dim})`;
        spaced(ctx, LEVELS[i], clamp(x, g.axisX + half, g.w - 2 - half), g.top - 7, 1);
      });
      ctx.setLineDash([]);
    }

    // the last block: dots under this line have waited through at least one block
    // (it sinks with them, and holds still with them while the node is away)
    if (f.isNum(lastBlockAt) && (state === "ok" || (stale && dots.size))) {
      const age = Math.max(0, picNow(now) - lastBlockAt);
      const y = Math.round(yAt(g, age)) + 0.5;
      if (y > g.top + 2 && y < g.bottom - 2) {
        ctx.setLineDash([6, 5]);
        ctx.strokeStyle = `rgba(${acc}, ${0.42 * dim})`;
        ctx.beginPath(); ctx.moveTo(g.axisX + 1, y); ctx.lineTo(g.x1, y); ctx.stroke();
        ctx.setLineDash([]);
        ctx.font = `600 ${g.compact ? 8.5 : 9}px ${FONT}`;
        ctx.textAlign = "left";
        ctx.fillStyle = `rgba(0, 0, 0, ${0.7 * dim})`;
        const s = "LAST BLOCK";
        ctx.fillRect(g.axisX + 5, y - 7, ctx.measureText(s).width + s.length * 1 + 8, 13);
        ctx.fillStyle = `rgba(${acc}, ${0.9 * dim})`;
        spaced(ctx, s, g.axisX + 9, y - 0.5, 1);
      }
    }
  }

  // a note on the column-head row: when the picture is an old one
  function drawNote(ctx, g, ink) {
    if (!stale || !f.isNum(poolAt) || !dots.size) return;
    const s = `LAST READING ${f.clock(poolAt)}`;
    ctx.font = `600 ${g.compact ? 9 : 10}px ${FONT}`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const wid = ctx.measureText(s).width + s.length * 1.2;
    const x = g.x0 + 2, y = g.top + 11;
    ctx.fillStyle = "rgba(0, 0, 0, 0.7)";
    ctx.fillRect(x - 5, y - 8, wid + 10, 16);
    ctx.fillStyle = `rgba(${ink}, 0.95)`;
    spaced(ctx, s, x, y, 1.2);
  }

  // a dot's size from the transaction's size, shrunk evenly when the field is crowded
  function sizer(g, n) {
    const area = Math.max(1, (g.x1 - g.x0) * (g.bottom - g.top));
    const room = Math.sqrt(area / Math.max(1, n));
    const crowd = clamp((room * 0.3) / 3, 0.38, 1);
    return (w) => Math.max(1.1, clamp(1.4 + 1.0 * Math.log2(1 + w / 1000), 2, 7) * crowd);
  }

  const P = { x: 0, y: 0 };
  function home(d, g, now, out) {
    // many transactions pay exactly the same fee: a nudge of a pixel or three keeps each one in sight
    out.x = xAt(g, d.lv) + d.j * (g.compact ? 2.5 : 3.5);
    out.y = yAt(g, d.st === "limbo" && d.freeze != null ? d.freeze : ageNow(d, now));
    return out;
  }

  function drawDots(ctx, g, T, now, live, acc, dim, breathe, glowOn) {
    const n = dots.size;
    const rOf = sizer(g, n);
    const many = n > MANY;
    const glowA = (glowOn ? 1 : 0.35) * breathe;
    const sprite = glowSprite(acc, 32, 0.9);
    const wsprite = glowSprite(warnRGB, 32, 0.8);
    let stirring = false;   // a dot is on its way somewhere, or flashing
    ctx.globalCompositeOperation = "lighter";
    for (const d of dots.values()) {
      // move the dot along whatever it is doing
      let p = live ? (T - d.t0) / d.dur : 1;
      if (d.st === "gone") continue;
      if (d.st !== "live" && p >= 1) {
        if (d.st === "out" || d.st === "sweep") {
          // it landed: it counts toward the block that took it (another may have come since)
          const b = d.st === "sweep" ? d.blk : null;
          if (b) {
            b.got++;
            if (b.got >= b.expect) b.full = T;
          }
          d.st = "gone";
          continue;
        }
        if (d.st === "limbo") {
          // no block came for it: it fades where it is
          begin(d, "out", T, FADE_S);
          if (d.st === "gone") continue;
          p = live ? 0 : 1;
        } else {
          // a drop that lands now flashes; one that landed while nothing was drawn (the screen just opened) doesn't
          if (d.st === "drop" && live && T - (d.t0 + d.dur) < LATE_S) d.flash = T;
          d.st = "live";
        }
      }
      if (d.st !== "live") stirring = true;
      const age = ageNow(d, now);
      const newness = Math.exp(-age / 90);
      let r = rOf(d.w);
      let a = (0.4 + 0.6 * newness) * dim;
      let x, y;
      if (d.st === "live") {
        home(d, g, now, P); x = P.x; y = P.y;
      } else if (d.st === "drop") {
        if (p < 0) continue;
        home(d, g, now, P);
        x = P.x; y = lerp(g.top - 6 - d.h * 12, P.y, p * p);
        a *= Math.min(1, p * 3);
      } else if (d.st === "fadein") {
        if (p < 0) continue;
        home(d, g, now, P); x = P.x; y = P.y;
        a *= clamp(p, 0, 1);
      } else if (d.st === "limbo") {
        home(d, g, now, P); x = P.x; y = P.y;
        a *= 0.65;
      } else if (d.st === "out") {
        const q = clamp(p, 0, 1);
        x = Number.isFinite(d.fx) ? d.fx : xAt(g, d.lv);
        y = (Number.isFinite(d.fy) ? d.fy : yAt(g, age)) - 8 * q;
        a *= 0.65 * (1 - q); r *= 1 - 0.5 * q;
      } else {   // sweep
        if (!block || p < 0) { x = d.fx; y = d.fy; }
        else {
          const bp = blockPos(g, T);
          const q = ease(clamp(p, 0, 1));
          const tx = bp.x + d.tx * bp.s * 0.8, ty = bp.y + d.ty * bp.s * 0.8;
          const cx = lerp(d.fx, tx, 0.45), cy = Math.min(d.fy, ty) - 40 - 30 * d.h;
          const m = 1 - q;
          x = m * m * d.fx + 2 * m * q * cx + q * q * tx;
          y = m * m * d.fy + 2 * m * q * cy + q * q * ty;
          r *= 1 - 0.45 * q;
          a = Math.min(1, a + 0.4 * q) * dim;
        }
      }
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      d.x = x; d.y = y;
      // a flash for a transaction that just arrived; without motion, a still ring
      // on the ones that arrived since the reading before
      const fl = live ? clamp(1 - (T - d.flash) / 0.9, 0, 1) : (d.arrived && d.born === seq - 1 && seq > 1 ? 0.5 : 0);
      if (live && fl > 0) stirring = true;
      const picked = d.id === pickedId;
      if (d.flags & FLAG_DOUBLE_SPEND) {
        ctx.globalAlpha = Math.min(1, a * 0.9 * glowA + 0.15);
        stamp(ctx, wsprite, x, y, r * 9 + 8);
        ctx.globalAlpha = 1;
        tri(ctx, x, y, Math.max(3.4, r * 1.6), `rgba(${warnRGB}, ${Math.min(1, a + 0.35)})`);
        if (picked) ring(ctx, x, y, r + 6, warnRGB);
        continue;
      }
      // the glow: every dot while there are few, only the newest, flashing and picked ones when there are many
      if (!many || newness > 0.5 || fl > 0 || picked) {
        ctx.globalAlpha = Math.min(1, a * 0.85 * glowA * (1 + 1.4 * fl));
        stamp(ctx, sprite, x, y, r * 7.5 + 7 + 16 * fl);
      }
      ctx.globalAlpha = Math.min(1, a * (1 + 0.8 * fl));
      if (d.flags & FLAG_NOT_RELAYED) {
        // not passed on yet: a ring, not a filled dot
        ctx.strokeStyle = `rgb(${acc})`;
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(x, y, Math.max(1.6, r), 0, TAU); ctx.stroke();
      } else if (many && r < 2.2 && !picked) {
        ctx.fillStyle = `rgb(${acc})`;
        ctx.fillRect(x - r, y - r, r * 2, r * 2);
      } else {
        ctx.fillStyle = `rgb(${acc})`;
        ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill();
        if (newness > 0.55 || fl > 0) {   // the newest have a white-hot centre
          ctx.globalAlpha = Math.min(1, (newness - 0.4) * a + fl);
          ctx.fillStyle = "#fff";
          ctx.beginPath(); ctx.arc(x, y, Math.max(0.6, r * 0.45), 0, TAU); ctx.fill();
        }
      }
      if (fl > 0) {
        ctx.globalAlpha = 0.8 * fl;
        ctx.strokeStyle = `rgb(${acc})`;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(x, y, r + 2 + (live ? 12 * (1 - fl) : 5), 0, TAU); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      if (picked) ring(ctx, x, y, r + 5, acc);
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
    // the picked one: guide lines to both scales
    const pd = pickedId && dots.get(pickedId);
    if (pd && Number.isFinite(pd.x)) {
      ctx.setLineDash([2, 3]);
      ctx.strokeStyle = `rgba(${look.head || "107, 174, 200"}, 0.55)`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(g.axisX, Math.round(pd.y) + 0.5); ctx.lineTo(pd.x - 8, Math.round(pd.y) + 0.5);
      ctx.moveTo(Math.round(pd.x) + 0.5, pd.y + 8); ctx.lineTo(Math.round(pd.x) + 0.5, g.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    return stirring;
  }

  function ring(ctx, x, y, r, rgb) {
    ctx.globalAlpha = 0.95;
    ctx.strokeStyle = `rgb(${rgb})`;
    ctx.lineWidth = 1.3;
    ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.stroke();
    ctx.globalAlpha = 1;
  }

  function tri(ctx, x, y, s, color) {
    ctx.beginPath();
    ctx.moveTo(x, y - s); ctx.lineTo(x + s * 0.92, y + s * 0.62); ctx.lineTo(x - s * 0.92, y + s * 0.62);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  }

  // ---- the block: a wireframe box at the right of the field that collects
  // the dots it took, then slides away
  const BP = { x: 0, y: 0, s: 0, a: 1, slide: 0, base: 0 };
  function blockPos(g, T) {
    const s = g.compact ? 30 : clamp((g.bottom - g.top) * 0.16, 34, 54);
    const baseX = g.x1 - s * 0.75;
    const baseY = g.top + (g.bottom - g.top) * 0.5;
    const live = moving(look);
    const age = live ? T - block.t0 : BLOCK_IN + 0.5;
    const appear = clamp(age / BLOCK_IN, 0, 1);
    const slide = live ? clamp((age - BLOCK_IN - BLOCK_HOLD) / BLOCK_SLIDE, 0, 1) : 0;
    BP.s = s * (0.7 + 0.3 * ease(appear));
    BP.base = baseX;
    BP.x = baseX + easeIn(slide) * (g.w - baseX + s * 2);
    BP.y = baseY - 10 * easeIn(slide);
    BP.a = appear * (1 - slide);
    BP.slide = slide;
    return BP;
  }

  function drawBlock(ctx, g, T, live, acc, ink, glowOn) {
    if (live) {
      const age = T - block.t0;
      // dots that leave once it has started sliding fade instead of chasing it
      if (age > BLOCK_IN + BLOCK_HOLD - SWEEP_S) block.open = false;
      if (age > BLOCK_IN + BLOCK_HOLD + BLOCK_SLIDE) { block = null; return; }
    } else {
      block.open = false;
      const gen = lastSnap?.generated || 0;
      if (gen - block.at > STILL_BLOCK_S) return;
    }
    const bp = blockPos(g, T);
    const fill = live ? (block.expect ? clamp(block.got / block.expect, 0, 1) : clamp((T - block.t0) / 1.2, 0, 1)) : 1;
    const kick = live && block.full > 0 ? clamp(1 - (T - block.full) / 0.8, 0, 1) : 0;
    const a = bp.a;
    const s = bp.s;
    const dx = s * 0.42, dy = -s * 0.3;
    const fx = bp.x - s / 2 - dx / 2, fy = bp.y - s / 2 - dy / 2;
    ctx.globalCompositeOperation = "lighter";
    ctx.globalAlpha = a * (glowOn ? 1 : 0.4);
    stamp(ctx, glowSprite(acc, 64, 0.5), bp.x, bp.y, s * (2.4 + 1.4 * fill + 1.6 * kick));
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
    // faces: front, top, side, filling with light as the dots arrive
    const face = (pts, al) => {
      ctx.beginPath();
      ctx.moveTo(pts[0], pts[1]);
      for (let i = 2; i < pts.length; i += 2) ctx.lineTo(pts[i], pts[i + 1]);
      ctx.closePath();
      ctx.fillStyle = `rgba(${acc}, ${al})`;
      ctx.fill();
    };
    face([fx, fy, fx + s, fy, fx + s, fy + s, fx, fy + s], a * (0.05 + 0.24 * fill + 0.3 * kick));
    face([fx, fy, fx + dx, fy + dy, fx + s + dx, fy + dy, fx + s, fy], a * (0.04 + 0.16 * fill));
    face([fx + s, fy, fx + s + dx, fy + dy, fx + s + dx, fy + s + dy, fx + s, fy + s], a * (0.03 + 0.11 * fill));
    // hidden edges, faint and dashed
    ctx.setLineDash([2, 3]);
    ctx.strokeStyle = `rgba(${acc}, ${0.28 * a})`;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(fx, fy + s); ctx.lineTo(fx + dx, fy + s + dy); ctx.lineTo(fx + s + dx, fy + s + dy);
    ctx.moveTo(fx + dx, fy + s + dy); ctx.lineTo(fx + dx, fy + dy);
    ctx.stroke();
    ctx.setLineDash([]);
    // visible edges
    ctx.strokeStyle = `rgba(${acc}, ${0.95 * a})`;
    ctx.lineWidth = 1.3 + 0.8 * kick;
    ctx.beginPath();
    ctx.rect(fx, fy, s, s);
    ctx.moveTo(fx, fy); ctx.lineTo(fx + dx, fy + dy); ctx.lineTo(fx + s + dx, fy + dy); ctx.lineTo(fx + s + dx, fy + s + dy); ctx.lineTo(fx + s, fy + s);
    ctx.moveTo(fx + s, fy); ctx.lineTo(fx + s + dx, fy + dy);
    ctx.stroke();
    // its words: which block, and how many of its transactions were waiting here
    const l1 = `${block.count > 1 ? `${block.count} NEW BLOCKS · TO ${f.num(block.height)}` : `NEW BLOCK ${f.num(block.height)}`}${f.isNum(block.txs) ? ` · ${f.plural(block.txs, "TX", "TXS")}` : ""}`;
    const l2 = block.here > 0 ? `${f.num(block.here)} WERE WAITING HERE` : "";
    ctx.font = `600 ${g.compact ? 9.5 : 10.5}px ${FONT}`;
    const w1 = ctx.measureText(l1).width + l1.length * 1.2;
    ctx.font = `500 ${g.compact ? 9 : 10}px ${FONT}`;
    const w2 = l2 ? ctx.measureText(l2).width + l2.length * 0.8 : 0;
    const half = Math.max(w1, w2) / 2;
    // the words (and their dark backing) stay inside the field; as the block
    // slides away they slide out behind it
    const cx = clamp(bp.base + dx / 2, g.x0 + half + 6, g.x1 - 6 - half) + (bp.x - bp.base);
    const ty = fy + dy - (l2 ? 30 : 16);
    ctx.save();
    ctx.beginPath();
    ctx.rect(g.axisX + 1, 0, g.x1 + 2 - g.axisX, g.h);
    ctx.clip();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = `rgba(0, 0, 0, ${0.72 * a})`;
    ctx.fillRect(cx - half - 6, ty - 8, half * 2 + 12, l2 ? 30 : 16);
    ctx.font = `600 ${g.compact ? 9.5 : 10.5}px ${FONT}`;
    ctx.fillStyle = `rgba(${ink}, ${0.97 * a})`;
    spaced(ctx, l1, cx, ty, 1.2);
    if (l2) {
      ctx.font = `500 ${g.compact ? 9 : 10}px ${FONT}`;
      ctx.fillStyle = `rgba(${acc}, ${0.95 * a})`;
      spaced(ctx, l2, cx, ty + 14, 0.8);
    }
    ctx.restore();
  }

  // ---- hover, tap and keys: one transaction's details
  function dotAt(x, y, reach) {
    let best = null, bd = reach * reach;
    for (const d of dots.values()) {
      if (!present(d) || !Number.isFinite(d.x)) continue;
      const dx = d.x - x, dy = d.y - y;
      const dd = dx * dx + dy * dy;
      if (dd < bd) { bd = dd; best = d; }
    }
    return best;
  }
  function tipFor(d) {
    const now = env.now();
    const per = d.w > 0 ? (d.fee / d.w) * 1000 : null;
    const lv = levelOf(d.w > 0 ? d.fee / d.w : 0, levels);
    const age = waited(d, now);
    const rows = [
      { label: "FEE PAID", value: feeText(d.fee) },
      { label: "PER KB", value: per == null ? f.DASH : feeText(per) },
    ];
    if (lv !== null) rows.push({ label: "FEE LEVEL", value: lv < 0 ? "UNDER LOW" : LEVELS[lv] });
    rows.push({ label: "SIZE", value: f.bytes(d.w) });
    rows.push({ label: "WAITED", value: `${d.flags & FLAG_TIME_GUESSED ? "AT LEAST " : ""}${f.wait(age)}` });
    rows.push({ label: d.flags & FLAG_TIME_GUESSED ? "FIRST SEEN" : "ARRIVED", value: f.clock(d.t) });
    if (stale) rows.unshift({ label: "LAST READING", value: f.clock(poolAt) });
    if (d.flags & FLAG_DOUBLE_SPEND) rows.push({ note: "▲ Someone tried to spend the same money as this one a second time. Wait for extra confirmations before trusting a payment it makes." });
    if (d.flags & FLAG_NOT_RELAYED) rows.push({ note: "Your node hasn't passed this one on to its peers yet." });
    if (d.flags & FLAG_TIME_GUESSED) rows.push({ note: "Your node didn't say when it got this one (a restricted port doesn't), so the wait counts from when the dashboard first saw it." });
    if (lv === -1) rows.push({ note: "It pays less per kB than your node's lowest fee level now: miners may leave it for a while." });
    return { head: `TRANSACTION ${hasHidden(d.id) ? d.id : `${d.id.slice(0, 12)}…`}`, rows };   // a hidden id is already short
  }
  const tipAt = { x: 0, y: 0, key: false };   // where the open tooltip was put (pointer, tap or key)
  function showDot(d, cx, cy, byKey) {
    pickedId = d ? d.id : null;
    if (!d) { env.tip.hideTip(); redraw(); return; }
    const t = tipFor(d);
    tipAt.x = cx; tipAt.y = cy; tipAt.key = !!byKey;
    env.tip.showTip(cx, cy, t.head, t.rows);
    redraw();
  }
  function dotClient(d) {
    const r = canvas.getBoundingClientRect();
    return { x: r.left + (Number.isFinite(d.x) ? d.x : r.width / 2), y: r.top + (Number.isFinite(d.y) ? d.y : r.height / 2) };
  }
  const redraw = () => {
    kick = true;
    if (shown && !stage.running) stage.frame();
  };

  canvas.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse") return;
    const r = canvas.getBoundingClientRect();
    const d = dotAt(e.clientX - r.left, e.clientY - r.top, 12);
    canvas.style.cursor = d ? "pointer" : "";
    if (!d) { if (pickedId) { pickedId = null; env.tip.hideTip(); redraw(); } return; }
    showDot(d, e.clientX, e.clientY);
  });
  canvas.addEventListener("pointerleave", (e) => {
    if (e.pointerType !== "mouse") return;
    pickedId = null; env.tip.hideTip(); redraw();
  });
  let down = null;
  canvas.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY }; });
  canvas.addEventListener("pointerup", (e) => {
    if (e.pointerType === "mouse" || !down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 8) { down = null; return; }
    down = null;
    const r = canvas.getBoundingClientRect();
    const d = dotAt(e.clientX - r.left, e.clientY - r.top, 20);
    if (!d || d.id === pickedId) { pickedId = null; env.tip.hideTip(); redraw(); return; }
    showDot(d, e.clientX, e.clientY);
  });
  // the arrow keys step through the transactions: up and down by how long they
  // have waited, left and right by fee; Home the newest, End the oldest
  canvas.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const list = [...dots.values()].filter((d) => present(d) && Number.isFinite(d.x));
    if (!list.length) return;
    let next = null;
    const cur = pickedId && dots.get(pickedId);
    if (e.key === "Escape") { pickedId = null; env.tip.hideTip(); redraw(); return; }
    if (e.key === "ArrowUp" || e.key === "ArrowDown" || e.key === "Home" || e.key === "End") {
      list.sort((a, b) => b.t - a.t || (a.id < b.id ? -1 : 1));   // newest first
      const i = cur ? list.indexOf(cur) : -1;
      if (e.key === "Home") next = list[0];
      else if (e.key === "End") next = list[list.length - 1];
      else if (e.key === "ArrowDown") next = list[Math.min(list.length - 1, i + 1)];
      else next = list[Math.max(0, i - 1)];
    } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      list.sort((a, b) => a.lt - b.lt || b.t - a.t);
      const i = cur ? list.indexOf(cur) : -1;
      next = e.key === "ArrowRight" ? list[Math.min(list.length - 1, i + 1)] : list[Math.max(0, i < 0 ? 0 : i - 1)];
    } else return;
    e.preventDefault();
    const c = dotClient(next);
    showDot(next, c.x, c.y, true);
    const t = tipFor(next);
    say.textContent = `${t.head}: ${t.rows.filter((r) => !r.note).map((r) => `${r.label} ${r.value}`).join(", ")}`;
  });
  canvas.addEventListener("blur", () => { if (tipAt.key) { pickedId = null; env.tip.hideTip(); redraw(); } });

  // the room box explains itself on hover and focus
  env.tip.attachTip(room, () => {
    const s = lastSnap || {};
    const rm = roomOf(s);
    return {
      head: "POOL AGAINST ONE BLOCK",
      rows: [
        { label: "WAITING", value: f.bytes(s.mempool?.bytes) },
        { label: "ONE BLOCK'S ROOM", value: rm ? f.bytes(rm.per) : "—" },
        { note: rm && rm.from === "recent"
          ? "One block's room here is the average size of the recent blocks. Miners pick by fee, so this is a rough guide."
          : "One block's room is your node's median block size: what a block holds before the miner's reward starts to shrink. Miners pick by fee, so this is a rough guide." },
      ],
    };
  });
  env.tip.attachTip(rNr.node, () => {
    if (!lastSnap?.source?.restricted) return null;
    return { head: "NOT SENT ON YET", rows: [{ note: "Not shown on a restricted port: there the node only counts transactions it has already passed on, so this would always read 0." }] };
  });
  env.tip.attachTip(rDs.node, () => {
    const n = lastSnap?.mempool?.double_spends;
    if (!n) return null;
    return { head: "DOUBLE SPENDS", rows: [{ note: `Someone tried to spend the same money twice (${f.num(n)} seen). Wait for extra confirmations before trusting a payment. The ▲ marks the transaction it was seen with.` }] };
  });
  env.tip.attachTip(rLv.node, () => {
    const rows = lvCounts
      ? [{ label: "UNDER LOW", value: f.num(lvCounts[0]) }, ...LEVELS.map((n, i) => ({ label: n, value: f.num(lvCounts[i + 1]) }))]
      : [];
    rows.push({ note: "Each waiting transaction, counted at the highest of your node's four fee levels per kB that it pays. A wallet picks one of these levels; most pay the lowest, and miners take the highest first. Under low pays less than the lowest level asks for now, so miners may leave it for a while." });
    return { head: "BY FEE LEVEL", rows };
  });

  // ---- readout column, caption, subtitle, empty states
  function hud(snap) {
    const src = snap.source || {};
    const mp = snap.mempool || {};
    const ok = state === "ok" || state === "empty";
    const val = (v) => (ok ? v : "—");
    setText(big, ok ? f.num(mp.txs) : "—");
    setText(bigUnit, mp.txs === 1 ? "TRANSACTION" : "TRANSACTIONS");
    setText(bigNote, ok ? `${f.bytes(mp.bytes)} · FEES ${f.xmr(mp.fee_total, 5)}` : "—");
    setText(rOld.dd, val(f.isNum(mp.oldest_age_s) ? f.dur(mp.oldest_age_s) : mp.txs === 0 ? "NOTHING WAITING" : "—"));
    setText(r10.dd, val(f.num(mp.over_10m)));
    // a restricted port only counts transactions already passed on, so this would always read 0: unknown, not 0
    setText(rNr.dd, src.restricted ? "—" : val(f.num(mp.not_relayed)));
    rNr.node.tabIndex = src.restricted ? 0 : -1;
    const ds = ok ? mp.double_spends : null;
    setText(rDs.dd, ds ? f.flag("warn", `${f.num(ds)} SEEN`) : f.isNum(ds) ? "NONE" : "—");
    setData(rDs.dd, "level", ds ? "warn" : null);
    rDs.node.tabIndex = ds ? 0 : -1;
    dsNote.hidden = !ds;
    setText(dsNote, ds ? "▲ HEADS-UP · SOMEONE TRIED TO SPEND THE SAME MONEY TWICE · WAIT FOR EXTRA CONFIRMATIONS" : "");
    let anyDs = false, anyNr = false;
    const byLevel = [0, 0, 0, 0];
    let under = 0;
    for (const d of dots.values()) {
      if (!present(d)) continue;
      if (d.flags & FLAG_DOUBLE_SPEND) anyDs = true;
      if (d.flags & FLAG_NOT_RELAYED) anyNr = true;
      const lv = levelOf(d.w > 0 ? d.fee / d.w : 0, levels);
      if (lv === null) continue;
      if (lv < 0) under++; else byLevel[lv]++;
    }
    // (an entry stays half a minute after the last such dot, so the legend doesn't blink)
    const tl = clock();
    if (anyDs) dsSeenAt = tl;
    if (anyNr) nrSeenAt = tl;
    lgDs.hidden = tl - dsSeenAt > 30;
    lgNr.hidden = tl - nrSeenAt > 30;
    const counted = ok && levels;
    lvCounts = counted ? [under, ...byLevel] : null;
    lvCells.forEach((c, i) => {
      setText(c.v, counted ? f.num(byLevel[i]) : "—");
      setData(c.node, "none", counted && !byLevel[i] ? "1" : null);
    });
    rLv.node.tabIndex = counted ? 0 : -1;

    const rm = ok ? roomOf(snap) : null;
    setText(roomVal, rm ? worthWords(rm.ratio) : "—");
    roomFill.style.width = `${rm ? clamp(rm.ratio, 0, 1) * 100 : 0}%`;
    const lit = rm ? (rm.ratio > 0 ? Math.max(1, Math.round(clamp(rm.ratio, 0, 1) * 10)) : 0) : 0;
    [...leds.children].forEach((n, i) => n.classList.toggle("on", i < lit));
    setData(room, "over", rm && rm.ratio > 1 ? "1" : null);
    setText(roomNote, !rm ? "—" : rm.ratio <= 0 ? "NOTHING TO FIT IN"
      : rm.ratio <= 1 ? "FITS IN THE NEXT BLOCK"
        : `A BACKLOG OF ABOUT ${f.dur(Math.ceil(rm.ratio) * TARGET_S).toUpperCase()}`);

    const wanted = mp.list_wanted || 0, cut = mp.list_cut || 0;
    setText(each, !ok ? "EACH DOT · ONE TRANSACTION"
      : wanted ? `EACH DOT · ONE TRANSACTION · READING ${f.num(wanted)} MORE`
        : cut ? `EACH DOT · ONE TRANSACTION · THE NEWEST ${f.num(dots.size)} SHOWN`
          : mp.check === "differs" ? "EACH DOT · ONE TRANSACTION · ▲ THE FEES READ DON'T ADD UP TO THE NODE'S TOTAL"
            : "EACH DOT · ONE TRANSACTION");
    setText(cap, across === "perkb"
      ? "Each dot is a waiting transaction: across is its fee per kB (what miners sort by; the dashed lines are your node's fee levels), down is how long it has waited. When a block comes, the ones it took fly into it."
      : "Each dot is a waiting transaction: across is the fee it paid, down is how long it has waited. When a block comes, the ones it took fly into it.");

    let why = null;
    if (state === "down") why = "YOUR NODE ISN'T ANSWERING · THE POOL SHOWS AGAIN WHEN IT DOES";
    else if (state === "catching") why = "YOUR NODE IS STILL CATCHING UP AND ASKS ANOTHER NODE FOR NOW · ITS OWN POOL SHOWS WHEN IT IS DONE";
    else if (state === "none") why = "NO POOL READING YET";
    else if (state === "empty") why = "POOL · EMPTY · NOTHING WAITING TO GO INTO A BLOCK";
    empty.hidden = !why;
    setData(empty, "kind", state);
    setText(empty, why || "");
    setData(view, "state", state);

    const poll = f.num(src.poll_s || 1);
    setText(env.sub, src.answering === false ? "FROM YOUR NODE · NOT ANSWERING"
      : `FROM YOUR NODE · EVERY ${poll} S · ONE DOT PER TRANSACTION${src.restricted ? " · RESTRICTED PORT · COUNTS ONLY TRANSACTIONS ALREADY PASSED ON" : ""}`);
    canvas.setAttribute("aria-label", ok
      ? `Transaction pool: ${f.plural(mp.txs, "transaction")} waiting, ${f.bytes(mp.bytes)}, oldest ${f.dur(mp.oldest_age_s)}${ds ? `, ${f.num(ds)} double-spend attempts` : ""}. Across: ${across === "perkb" ? "fee per kB" : "fee paid"}; down: how long each has waited. The arrow keys step through them.`
      : `Transaction pool: ${why || "no reading"}.`);
  }

  return {
    update(snap) {
      lastSnap = snap;
      ingest(snap);
      hud(snap);
      if (shown) measure();
      // an open tooltip follows the new reading
      const pd = pickedId && dots.get(pickedId);
      if (shown && pd) {
        const c = tipAt.key || document.activeElement === canvas ? dotClient(pd) : tipAt;
        const t = tipFor(pd);
        env.tip.showTip(c.x, c.y, t.head, t.rows);
      }
      if (!shown) return;
      // an old picture (node away) holds still: the loop stops itself once it has settled
      if (moving(look) && !stale) stage.start();
      else if (!stage.running) stage.frame();
    },
    show() {
      shown = true;
      lastPaint = 0;
      kick = true;
      measure();
      if (moving(look) && !stale) stage.start(); else stage.frame();
    },
    hide() {
      shown = false;
      stage.stop();
      settle();
      env.tip.hideTip();
      pickedId = null;
    },
    look(lk) {
      look = lk;
      warnRGB = cssRGB("--warn-rgb", "255, 112, 64");
      if (!shown) return;
      if (moving(lk) && !stale) stage.start(); else { stage.stop(); stage.frame(); }
    },
  };
}
