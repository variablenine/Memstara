// BANS screen hero: the ban clock. One ring per address or range your
// node is banning right now with 24 h or less left (monerod's get_bans, read
// every minute). Each ring's lit arc starts at 12 o'clock and is as long as
// the time left out of 24 h, so it drains back toward the top as the ban runs
// out; it is counted down here every second between readings. Thin rings are
// single addresses, thick ones ranges (so the kind never rests on colour).
// The newest ban sweeps in and flashes once; one that ends fades away.
// The specks that run back toward the top along each ring are decoration.
// While the node isn't answering the clock holds still and dim: its rings are
// the bans from the last reading, still counted down to their ends.
//
// Hover, focus or tap a ring for its details; click it (or press Enter) to
// find that ban's row in the BAN LIST panel. Nothing here changes anything on
// the node.
import { $, el, clear, setText, setAttr } from "../dom.js";
import * as f from "../fmt.js";
import { rangeLabel } from "../panels/bans.js";
import { hiddenRange } from "../privacy.js";
import { createStage, glowSprite, stamp, moving, clamp, ease, TAU } from "../fx/space.js";

const DAY = 86400;
const RINGS = 12;               // rings drawn; any more are summed up in words
const MIN_TRACKS = 7;           // fewer bans than this spread out over this many tracks
const SWEEP_S = 1.3;            // a new ban's arc sweeps in this long
const FADE_S = 1.5;             // an ended ban fades this long
const NEW_FOR_S = 120;          // a fresh ban keeps its NEW mark this long
const FOUND_MS = 3400;          // the BAN LIST row stays lit this long
const LABEL_W = 124;            // room for a label beside the clock (CSS px)
const COL_GAP = 50;             // from the clock's rim to the label columns
const MIN_R_LABELLED = 128;     // with less room than this the labels go in the list
const TOP_ROOM = 58;            // a tall view keeps the clock this near its top (centre = R + this)
const STALE = 0.4;              // how lit the rings are while the node isn't answering
// how far down the window the sticky top bar and tab bar reach (they cover what's above)
const underBar = () => {
  const b = document.querySelector(".tabs")?.getBoundingClientRect().bottom;
  return Number.isFinite(b) && b > 0 ? b : 110;
};
const TOP = -Math.PI / 2;
const FONT = '"Plex Mono", ui-monospace, monospace';
const HINT = "HOVER OR TAB TO A RING · CLICK IT TO FIND THE BAN IN THE BAN LIST";
const HINT_TOUCH = "TAP A RING FOR ITS DETAILS · TAP IT AGAIN TO FIND THE BAN IN THE BAN LIST";
const touchFirst = () => typeof matchMedia === "function" && matchMedia("(hover: none)").matches;

// a ban's host in plain words: 62.233.57.0/24 -> 62.233.57.x, a block of 256
function hostWords(host) {
  const s = String(host ?? "");
  const hidden = hiddenRange(s);   // privacy mode: the range's shape and size, its numbers hidden
  if (hidden) return hidden;
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:\/(\d{1,2}))?$/.exec(s);
  if (!m) return { text: s || "?", size: 1, range: false };   // whole: a label too narrow for it is shortened where it's drawn
  const o = m.slice(1, 5).map(Number);
  const bits = m[5] === undefined ? 32 : clamp(Number(m[5]), 0, 32);
  if (bits === 32) return { text: o.join("."), size: 1, range: false };
  const size = 2 ** (32 - bits);
  let text = s;
  if (bits === 24) text = `${o[0]}.${o[1]}.${o[2]}.x`;
  else if (bits === 16) text = `${o[0]}.${o[1]}.x.x`;
  else if (bits === 8) text = `${o[0]}.x.x.x`;
  else if (bits > 24) { const a = o[3] - (o[3] % size); text = `${o[0]}.${o[1]}.${o[2]}.${a}–${a + size - 1}`; }
  else if (bits > 16) { const k = 2 ** (24 - bits); const a = o[2] - (o[2] % k); text = `${o[0]}.${o[1]}.${a}–${a + k - 1}.x`; }
  return { text, size, range: true };
}
const blockWords = (size) => `BLOCK OF ${f.num(size)}`;

// a steady 0..1 number per host (each ring's specks keep their own pace)
function hash01(id) {
  let x = 2166136261;
  for (let i = 0; i < id.length; i++) { x ^= id.charCodeAt(i); x = Math.imul(x, 16777619); }
  return ((x >>> 0) % 1000) / 1000;
}

export default function create(body, env) {
  // ---- DOM: the clock and its centre readout, then the side column in words
  const canvas = el("canvas", { class: "bn-canvas", tabindex: "0", role: "img", "aria-label": "Ban clock" });
  const coreN = el("b", { class: "bn-core-n", text: "—" });
  const BANNED_24 = "BANNED · 24\u00a0H\u00a0OR\u00a0LESS";   // wraps only at the dot
  const coreL = el("span", { class: "bn-core-l", text: BANNED_24 });
  const coreLongN = el("b", { text: "—" });
  const core = el("div", { class: "bn-core" },
    coreN, coreL, el("span", { class: "bn-core-rule", "aria-hidden": "true" }),
    el("span", { class: "bn-core-long" }, coreLongN, " LONGER"),
    el("span", { class: "bn-core-src", text: "OVER 24 H LEFT" }));
  const empty = el("div", { class: "bn-empty", hidden: true });
  const view = el("div", { class: "bn-view" }, canvas, core, empty);

  const allN = el("b", { text: "—" });
  const allSub = el("span", { class: "bn-big-sub", text: "" });
  const nextHost = el("b", { class: "bn-next-host", text: "—" });
  const nextWhen = el("span", { class: "bn-next-when", text: "" });
  const nextK = el("span", { class: "bn-k", text: "ENDS NEXT" });
  const listHead = el("div", { class: "bn-list-head", hidden: true });
  const list = el("ol", { class: "bn-list", hidden: true });
  const more = el("p", { class: "bn-more", hidden: true });
  const hint = el("p", { class: "bn-hint", text: touchFirst() ? HINT_TOUCH : HINT });
  const side = el("aside", { class: "bn-side", "aria-label": "The bans in words" },
    el("div", { class: "bn-side-head" },
      el("span", { text: "BAN CLOCK" }),
      el("span", { class: "bn-side-sub", text: "ONE RING PER BAN · LENGTH IS TIME LEFT" })),
    el("div", { class: "bn-side-body" },
      el("div", { class: "bn-big" }, el("span", { class: "bn-k", text: "ALL BANS NOW" }), allN, allSub),
      el("div", { class: "bn-next" }, nextK, nextHost, nextWhen),
      el("div", { class: "bn-legend", "aria-hidden": "true" },
        el("span", { class: "bn-lg bn-lg-one", text: "ONE ADDRESS" }),
        el("span", { class: "bn-lg bn-lg-range", text: "A RANGE (BLOCK)" })),
      el("p", { class: "bn-cap", text: "Each ring is one ban. Its lit arc starts at the top and is as long as the time left out of 24 h, counted down here every second. The specks running back to the top are only decoration." }),
      listHead, list, more,
      hint));
  const say = el("p", { class: "sr-only", "aria-live": "polite" });
  const root = el("div", { class: "bn" }, view, side);
  body.append(root, say);

  // ---- state
  let look = env.look();
  let shown = false;
  let lastSnap = null;
  let primed = false;           // a ban list was read before, so bans not in it are new
  let introDue = true;          // the arcs sweep in the first time the clock is seen moving
  let state = "wait";           // ok | none | off (not answering) | hidden (restricted port) | wait
  const bans = new Map();       // host -> ring
  let order = [];               // live bans, most time left first
  let selected = null;
  let hovered = null;
  let tapped = null;            // ring tapped once on a touch screen
  let pointerFocus = false;
  let lastPointer = "mouse";
  let via = "pointer";          // how the ring was picked: "pointer" or "key"
  let wide = true;              // labels beside the clock (else in the side list)
  let unfitKey = "";            // which rings had no room for a label (list rebuilt when it changes)
  let unfit = [];
  let listKey = "";
  let stillSig = "";            // the labels' words when a still clock was last drawn
  let ticker = 0;
  let noteTimer = 0;
  let found = null;             // {host, until}
  let lastRead = null;          // when the node last answered (unix s), from the snapshot
  let tipOn = false;            // the shared tooltip is showing one of this clock's rings
  let fontGen = 0;              // bumped when a font loads (label widths change)
  let dimK = 1;                 // this frame: how lit the bans are (dim while the node isn't answering)
  const boxes = [];             // label hit boxes, this frame
  const fitMemo = new Map();    // label text -> how it fits its column
  const geo = { w: 0, h: 0, cx: 0, cy: 0, R: 0, rIn: 0, sp: 0, thin: 1.6, thick: 5, tracks: MIN_TRACKS };
  const trackGoal = () => clamp(order.length + 1, MIN_TRACKS, RINGS);

  const stage = createStage(canvas, { draw, maxFps: 30 });
  const fontsIn = () => { fontGen++; if (shown && !stage.running) stage.frame(); };
  if (document.fonts) {
    if (document.fonts.ready) document.fonts.ready.then(fontsIn);
    document.fonts.addEventListener?.("loadingdone", fontsIn);
  }

  const leftOf = (it) => it.end - env.now();
  const focusRing = () => hovered || selected;
  const redraw = () => { if (shown && !stage.running) stage.frame(); };
  // the clock moves only with full motion and while the node answers (its last reading holds still)
  const anim = () => moving(look) && state !== "off";
  const isNew = (it, now) => it.newAt !== null && now - it.newAt < NEW_FOR_S;
  const readAt = () => (lastRead !== null ? f.dayClock(lastRead) : null);

  // ---- the snapshot's bans -> rings
  function sync(snap) {
    const src = snap.source || {};
    const node = snap.node || {};
    const got = Array.isArray(node.ban_list) ? node.ban_list : null;
    state = src.answering === false ? "off" : src.restricted ? "hidden" : !got ? "wait" : "ok";
    lastRead = f.isNum(src.last_ok) && src.last_ok > 0 ? src.last_ok : null;
    if (state === "off") {
      // Not answering: the server keeps sending the last ban list it read, still counted down.
      // The rings keep the ends of the last good reading and run out here on time, and none
      // are added until the node answers again. A page opened during the outage starts from
      // the list as sent.
      if (!primed && got) { take(got, f.isNum(snap.generated) ? snap.generated : env.now(), false); primed = true; }
      restack();
      return;
    }
    const gen = f.isNum(snap.generated) ? snap.generated : env.now();
    const { seen, fresh } = take(got || [], gen, primed);
    if (got) primed = true;
    for (const it of bans.values()) if (!seen.has(it.host) && !it.gone) it.gone = -1;
    if (!shown) for (const it of [...bans.values()]) if (it.gone) bans.delete(it.host);   // nobody is watching it fade
    restack();
    if (state === "ok" && !order.length) state = "none";
    if (fresh.length) {
      say.textContent = fresh.length === 1
        ? `New ban: ${fresh[0].text}${fresh[0].range ? `, ${blockWords(fresh[0].size).toLowerCase()}` : ""}, ${f.dur(leftOf(fresh[0]))} left.`
        : `${fresh.length} new bans: ${fresh.map((it) => it.text).join(", ")}.`;
    }
  }

  // one reading's bans -> rings: each ends its `seconds` after `at`, when that list was read
  function take(list, at, markNew) {
    const seen = new Set();
    const fresh = [];
    for (const b of list) {
      const host = String(b?.host ?? "");
      const secs = Number(b?.seconds);
      if (!host || !Number.isFinite(secs) || secs <= 0) continue;
      seen.add(host);
      let it = bans.get(host);
      if (!it) {
        const w = hostWords(host);
        it = { host, text: w.text, size: w.size, range: w.range, end: 0, slot: -1, r: null, alpha: 0, gone: 0,
          sweepAt: null, sweepDue: false, flash: false, newAt: null, ph: hash01(host) };
        bans.set(host, it);
        if (markNew) { it.newAt = at; it.sweepDue = true; fresh.push(it); }
      }
      it.end = at + secs;
      it.gone = 0;
    }
    return { seen, fresh };
  }

  // live rings get their places: the most time left outermost
  function restack() {
    const now = env.now();
    order = [...bans.values()].filter((it) => !it.gone && it.end - now > 0)
      .sort((a, b) => b.end - a.end || (a.host < b.host ? -1 : 1));
    order.forEach((it, i) => { it.slot = i < RINGS ? i : -1; });
    for (const it of [...bans.values()]) if (it.gone && it.slot < 0) bans.delete(it.host);
    if (selected && (selected.gone || selected.slot < 0)) selected = null;
    if (hovered && (hovered.gone || hovered.slot < 0)) hovered = null;
  }

  // bans that ran out here between readings fade away without waiting for the node
  function expire() {
    const now = env.now();
    let any = false;
    for (const it of bans.values()) if (!it.gone && it.end - now <= 0) { it.gone = -1; any = true; }
    if (!any) return false;
    if (!shown || !anim()) for (const it of [...bans.values()]) if (it.gone) bans.delete(it.host);
    restack();
    if (state === "ok" && !order.length) state = "none";
    words();
    return true;
  }

  // ---- geometry: the clock fills the view; labels go beside it when there's room
  function measure(w, h) {
    const g = geo;
    g.w = w; g.h = h;
    const byH = h / 2 - 38;
    const byLabels = (w - 2 * (LABEL_W + COL_GAP)) / 2 - 6;
    const wasWide = wide;
    wide = Math.min(byH, byLabels) >= MIN_R_LABELLED;
    g.R = Math.max(70, Math.min(wide ? byLabels : w / 2 - 34, byH, 250));
    g.cx = Math.round(w / 2);
    // a view stretched tall (beside a long BAN LIST) keeps the clock near its top, not adrift mid-way
    g.cy = Math.round(Math.min(h / 2, g.R + TOP_ROOM));
    g.rIn = g.R * (g.R < 175 ? 0.5 : 0.43);   // a small clock gives its centre words more room
    spacing();
    core.style.left = `${g.cx}px`;
    core.style.top = `${g.cy}px`;
    // the state note sits at the bottom of the view, or just under the clock when the view runs on below it
    const under = g.cy < h / 2 - 1;
    empty.style.top = under ? `${Math.round(g.cy + g.R + 44)}px` : "";
    empty.style.bottom = under ? "auto" : "";
    empty.style.maxWidth = wide ? `${Math.round(2 * (g.R + COL_GAP) - 20)}px` : "";   // between the label columns
    core.style.width = `${Math.round((g.rIn - 12) * 1.9)}px`;
    view.classList.toggle("bn-small", g.R < 175);
    if (wasWide !== wide) { unfitKey = "?"; words(); }
  }
  // the tracks spread over the band between the centre readout and the rim:
  // a few bans get room to breathe, twelve sit close
  function spacing() {
    const g = geo;
    g.sp = (g.R - g.rIn) / (g.tracks - 1);
    g.thin = clamp(g.sp * 0.12, 1.4, 2.3);
    g.thick = clamp(g.sp * 0.42, 3.4, 7);
  }
  const slotR = (slot) => geo.R - slot * geo.sp;

  // how much of its arc a ring shows now: time left out of 24 h (times the sweep-in)
  function fracOf(it, t, now) {
    let fr = clamp((it.end - now) / DAY, 0, 1);
    if (it.sweepAt !== null) fr *= ease(clamp((t - it.sweepAt) / SWEEP_S, 0, 1));
    return fr;
  }

  // ---- drawing
  function draw(ctx, w, h, t, dt) {
    if (w !== geo.w || h !== geo.h) measure(w, h);
    const live = anim();
    const now = env.now();
    expire();
    dimK = state === "off" ? STALE : 1;
    const g = geo;
    const { cx, cy, R } = g;
    const acc = look.accent;
    const cool = look.head || "107, 174, 200";
    const glowK = look.glow === false ? 0.35 : 1;

    // sweeps: once when the clock is first seen moving, and for each new ban (staggered)
    if (live && introDue) {
      introDue = false;
      for (const it of order) if (it.slot >= 0) { it.sweepAt = t + it.slot * 0.07; it.sweepDue = false; }
    } else if (!live) introDue = false;
    let stagger = 0;
    for (const it of bans.values()) {
      if (!it.sweepDue) continue;
      it.sweepDue = false;
      if (live && it.slot >= 0 && it.newAt !== null && now - it.newAt < NEW_FOR_S) {
        it.sweepAt = t + 0.15 * stagger++;
        it.flash = true;
      }
    }

    // ease the track spacing and the rings to their places; fade new ones in and ended ones out
    const k = live ? 1 - Math.pow(0.04, dt || 0) : 1;
    const tg = trackGoal();
    if (g.tracks !== tg) { g.tracks = Math.abs(tg - g.tracks) < 0.01 ? tg : g.tracks + (tg - g.tracks) * k; spacing(); }
    for (const it of [...bans.values()]) {
      if (it.gone === -1) it.gone = t;
      if (it.slot >= 0 && !it.gone) {
        const rt = slotR(it.slot);
        it.r = it.r === null || !live ? rt : it.r + (rt - it.r) * k;
      }
      if (it.gone) {
        it.alpha = live ? Math.max(0, 1 - (t - it.gone) / FADE_S) : 0;
        if (it.alpha <= 0.01) { bans.delete(it.host); continue; }
      } else {
        const goal = it.slot >= 0 ? 1 : 0;
        it.alpha = live && it.sweepAt === null ? it.alpha + (goal - it.alpha) * Math.min(1, dt * 3) : goal;
      }
    }
    const rings = [...bans.values()].filter((it) => it.r !== null && it.alpha > 0.01).sort((a, b) => b.r - a.r);
    const focus = focusRing();

    ctx.clearRect(0, 0, w, h);

    // the light behind the clock, breathing slowly while it moves
    const br = live && look.glow !== false ? 0.5 + 0.5 * Math.sin((t * TAU) / 7) : 0.5;
    ctx.globalCompositeOperation = "lighter";
    ctx.globalAlpha = dimK < 1 ? 0.5 : 1;
    stamp(ctx, glowSprite(acc, 160, 0.09 * glowK), cx, cy, R * (2.25 + 0.3 * br));
    stamp(ctx, glowSprite(cool, 120, 0.05 * glowK), cx, cy, g.rIn * 2.6);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";

    dial(ctx, acc, cool);

    // the twelve tracks, always drawn (so an empty clock still reads as a clock)
    ctx.lineWidth = 1;
    for (let i = 0; i < Math.round(g.tracks); i++) {
      const r = slotR(i);
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, TAU);
      ctx.strokeStyle = `rgba(${cool}, ${state === "ok" ? 0.075 : 0.1})`;
      ctx.stroke();
    }
    if (focus && focus.r !== null) {
      ctx.beginPath(); ctx.arc(cx, cy, focus.r, 0, TAU);
      ctx.strokeStyle = `rgba(${cool}, 0.32)`;
      ctx.stroke();
    }

    // labels and their leaders (under the arcs), when there's room beside the clock
    const lay = wide ? layoutLabels(now) : { labels: [], unfit: [] };
    for (const L of lay.labels) leader(ctx, L, acc, focus);

    // the arcs
    for (const it of rings) arcOf(ctx, it, t, now, live, focus, acc, glowK);

    // the scale's words over the arcs and leaders (their black edge keeps them readable on a glow)
    scaleWords(ctx);

    // labels on top
    boxes.length = 0;
    for (const L of lay.labels) labelText(ctx, L, now, focus);

    // rings that had no room for a label go in the side list
    const key = (wide ? "W:" : "N:") + lay.unfit.map((it) => it.host).join(",");
    if (key !== unfitKey) { unfitKey = key; unfit = lay.unfit; words(); }
  }

  function dial(ctx, acc, cool) {
    const { cx, cy, R } = geo;
    const tick = (a, r0, r1) => {
      const c = Math.cos(a), s = Math.sin(a);
      ctx.moveTo(cx + c * r0, cy + s * r0);
      ctx.lineTo(cx + c * r1, cy + s * r1);
    };
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i < 96; i++) if (i % 4) tick(TOP + (i / 96) * TAU, R + 12, R + 15);
    ctx.strokeStyle = `rgba(${cool}, 0.2)`;
    ctx.stroke();
    ctx.beginPath();
    for (let i = 0; i < 24; i++) if (i % 6) tick(TOP + (i / 24) * TAU, R + 10, R + 17);
    ctx.strokeStyle = `rgba(${cool}, 0.42)`;
    ctx.stroke();
    ctx.beginPath();
    for (let i = 0; i < 4; i++) tick(TOP + (i / 4) * TAU, R + 7, R + 21);
    ctx.strokeStyle = `rgba(${acc}, 0.75)`;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.lineWidth = 1;
    // a hairline between the rings and the scale, and one round the centre readout
    ctx.beginPath(); ctx.arc(cx, cy, R + 6, 0, TAU);
    ctx.strokeStyle = `rgba(${cool}, 0.13)`; ctx.stroke();
    const rc = geo.rIn - Math.min(10, geo.sp * 0.6);
    ctx.beginPath(); ctx.arc(cx, cy, rc, 0, TAU);
    ctx.strokeStyle = `rgba(${cool}, 0.16)`; ctx.stroke();
    ctx.beginPath();
    for (let i = 0; i < 4; i++) tick(TOP + (i / 4) * TAU, rc - 4, rc);
    ctx.strokeStyle = `rgba(${cool}, 0.4)`; ctx.stroke();
    // where every arc starts: 12 o'clock
    ctx.beginPath(); ctx.moveTo(cx, cy - geo.rIn + geo.sp * 0.4); ctx.lineTo(cx, cy - R - 6);
    ctx.strokeStyle = `rgba(${acc}, 0.22)`; ctx.stroke();
  }

  // the scale's words: time left at the quarters
  function scaleWords(ctx) {
    const { cx, cy, R } = geo;
    const ink = look.ink || "232, 232, 240";
    ctx.font = `500 9.5px ${FONT}`;
    ctx.textBaseline = "middle";
    ctx.fillStyle = `rgba(${ink}, 0.5)`;
    ctx.strokeStyle = "rgba(0, 0, 0, 0.9)";
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.textAlign = "center";
    spaced(ctx, "0 · 24 H", cx, cy - R - 31, 1.2);
    spaced(ctx, "12 H", cx, cy + R + 31, 1.2);
    spaced(ctx, "6 H", cx + R + 14, cy - 16, 1.2);     // over the tick, clear of the label columns
    spaced(ctx, "18 H", cx - R - 14, cy - 16, 1.2);
  }

  function arcOf(ctx, it, t, now, live, focus, acc, glowK) {
    const { cx, cy } = geo;
    const fr = fracOf(it, t, now);
    if (fr <= 0.0004) return;
    const r = it.r;
    const a1 = TOP + fr * TAU;
    const lw = it.range ? geo.thick : geo.thin;
    const dim = focus && focus !== it ? 0.42 : 1;
    const a = it.alpha * dim * dimK;
    // the flash: bright while it sweeps in, then once more as it lands
    let fl = 0;
    if (it.flash && it.sweepAt !== null && live) {
      const u = t - it.sweepAt;
      fl = u < 0 ? 0 : u < SWEEP_S ? 0.7 : Math.max(0, 1 - (u - SWEEP_S) / 1.1);
      if (u > SWEEP_S + 1.6) it.flash = false;
    }
    const stroke = (width, color) => {
      ctx.beginPath(); ctx.arc(cx, cy, r, TOP, a1);
      ctx.lineWidth = width; ctx.strokeStyle = color; ctx.stroke();
    };
    ctx.lineCap = "butt";
    ctx.globalCompositeOperation = "lighter";
    stroke(lw + 9 + 6 * fl, `rgba(${acc}, ${((0.05 + 0.12 * fl) * a * glowK).toFixed(3)})`);
    stroke(lw + 3.5, `rgba(${acc}, ${((0.15 + 0.2 * fl) * a * glowK).toFixed(3)})`);
    ctx.globalCompositeOperation = "source-over";
    stroke(lw, `rgba(${acc}, ${(0.9 * a).toFixed(3)})`);
    const hot = (it === focus ? 0.5 : 0) + 0.65 * fl;
    if (hot > 0) stroke(Math.max(1, lw * 0.45), `rgba(255, 250, 238, ${(hot * it.alpha).toFixed(3)})`);

    // the specks: a glint running from the arc's end back to the top (decoration)
    const len = fr * TAU * r;
    if (live && len > 24 && !fl) {
      const travel = Math.max(1.4, len / 120);
      const cycle = travel + 2.2 + it.ph * 2.5;
      const u = ((t + it.ph * 11) % cycle) / travel;
      if (u < 1) {
        const ga = a1 - u * fr * TAU;
        const span = Math.min(fr * TAU * 0.5, 30 / r);
        const fade = Math.sin(Math.PI * u) * a;
        ctx.globalCompositeOperation = "lighter";
        ctx.beginPath(); ctx.arc(cx, cy, r, Math.max(TOP, ga), Math.min(a1, ga + span));
        ctx.lineWidth = lw + 1; ctx.strokeStyle = `rgba(255, 236, 200, ${(0.38 * fade).toFixed(3)})`; ctx.stroke();
        stamp(ctx, glowSprite(acc, 16, 0.8), cx + r * Math.cos(ga), cy + r * Math.sin(ga), (lw * 3 + 10) * (0.6 + 0.4 * fade));
        ctx.globalCompositeOperation = "source-over";
      }
    }

    // the head: where the time left ends, glowing (it breathes while moving)
    const hx = cx + r * Math.cos(a1), hy = cy + r * Math.sin(a1);
    const breathe = live ? 1 + 0.18 * Math.sin(t * 1.3 + it.ph * TAU) : 1;
    ctx.globalCompositeOperation = "lighter";
    ctx.globalAlpha = dimK;
    stamp(ctx, glowSprite(acc, 24, 0.75), hx, hy, (lw * 4.5 + 14 + 18 * fl) * breathe * (0.55 + 0.45 * glowK) * Math.max(0.5, dim));
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
    ctx.beginPath(); ctx.arc(hx, hy, lw * 0.55 + 1.3, 0, TAU);
    ctx.fillStyle = `rgba(255, 246, 228, ${(0.95 * a).toFixed(3)})`;
    ctx.fill();
    if (it === focus) {
      ctx.beginPath(); ctx.arc(hx, hy, lw * 0.55 + 6, 0, TAU);
      ctx.strokeStyle = `rgba(255, 255, 255, ${(0.8 * it.alpha).toFixed(3)})`; ctx.lineWidth = 1.2; ctx.stroke();
    }
    // a ring of light going out from a new ban as it lands
    if (fl > 0 && t - it.sweepAt > SWEEP_S) {
      const q = clamp((t - it.sweepAt - SWEEP_S) / 1.1, 0, 1);
      ctx.beginPath(); ctx.arc(cx, cy, r + 3 + 34 * ease(q), 0, TAU);
      ctx.strokeStyle = `rgba(${acc}, ${(0.5 * (1 - q)).toFixed(3)})`; ctx.lineWidth = 1.4; ctx.stroke();
    }
    it.hx = hx; it.hy = hy; it.a1 = a1; it.frShown = fr;
  }

  // label places: two columns beside the clock, each label near the height of
  // its arc's end, pushed apart so none overlap; too many for the height and
  // the inner rings' labels go in the side list instead
  function layoutLabels(now) {
    const g = geo;
    const sides = { L: [], R: [] };
    for (const it of order) {
      if (it.slot < 0 || it.r === null || it.alpha < 0.35) continue;
      const a = TOP + clamp((it.end - now) / DAY, 0, 1) * TAU;   // the true end, so labels don't ride the sweep
      const s = Math.cos(a) >= 0 ? "R" : "L";
      const hgt = (it.range ? 38 : 26) + (isNew(it, now) ? 12 : 0);
      sides[s].push({ it, a, s, hgt, want: g.cy + (g.R + 16) * Math.sin(a) - 7, y: 0 });
    }
    const top = 8, bottom = g.h - 8, gap = 7;
    const labels = [], left = [];
    for (const s of ["L", "R"]) {
      let arr = sides[s].sort((p, q) => p.want - q.want);
      let total = arr.reduce((n, p) => n + p.hgt + gap, -gap);
      if (total > bottom - top) {
        const keep = [...arr].sort((p, q) => p.it.slot - q.it.slot);
        while (keep.length && total > bottom - top) { const p = keep.pop(); left.push(p.it); total -= p.hgt + gap; }
        arr = arr.filter((p) => keep.includes(p));
      }
      let y = top;
      for (const p of arr) { p.y = Math.max(p.want, y); y = p.y + p.hgt + gap; }
      let lim = bottom;
      for (let i = arr.length - 1; i >= 0; i--) { const p = arr[i]; p.y = Math.min(p.y, lim - p.hgt); lim = p.y - gap; }
      labels.push(...arr);
    }
    left.sort((p, q) => p.slot - q.slot);
    return { labels, unfit: left };
  }

  function leader(ctx, L, acc, focus) {
    const { cx, cy, R } = geo;
    const it = L.it;
    const r = it.r;
    const dir = L.s === "R" ? 1 : -1;
    const colX = cx + dir * (R + COL_GAP);
    const ex = cx + (R + 16) * Math.cos(L.a), ey = cy + (R + 16) * Math.sin(L.a);
    const ly = L.y + 7;
    const on = focus === it;
    const a = it.alpha * (focus && !on ? 0.5 : 1) * (on ? 1 : dimK);
    ctx.beginPath();
    ctx.moveTo(cx + r * Math.cos(L.a), cy + r * Math.sin(L.a));
    ctx.lineTo(ex, ey);
    ctx.lineTo(colX - dir * 12, ly);
    ctx.lineTo(colX - dir * 4, ly);
    ctx.strokeStyle = `rgba(${acc}, ${((on ? 0.85 : 0.3) * a).toFixed(3)})`;
    ctx.lineWidth = on ? 1.2 : 1;
    ctx.stroke();
  }

  // a label in its column: the host, BLOCK OF n for a range, the time left, and NEW BAN on a fresh
  // one's own line (in words too, not only by its flash). Every line is measured and kept inside
  // the column, which runs from the leader's tip to the canvas edge.
  function labelText(ctx, L, now, focus) {
    const { cx, R, w } = geo;
    const it = L.it;
    const dir = L.s === "R" ? 1 : -1;
    const x = cx + dir * (R + COL_GAP);
    const room = Math.max(40, (dir > 0 ? w - x : x) - 6);
    const on = focus === it;
    const a = it.alpha * (focus && !on ? 0.55 : 1) * (on || dimK === 1 ? 1 : 0.65);
    const ink = look.ink || "232, 232, 240";
    ctx.textAlign = dir > 0 ? "left" : "right";
    ctx.textBaseline = "top";
    ctx.strokeStyle = `rgba(0, 0, 0, ${(0.9 * a).toFixed(3)})`;
    ctx.lineWidth = 4;
    ctx.lineJoin = "round";
    let wMax = 0;
    const line = (text, font, gap, rgb, k, y) => {
      ctx.font = font;
      const m = fit(ctx, text, gap, room);
      ctx.fillStyle = `rgba(${rgb}, ${(k * a).toFixed(3)})`;
      spaced(ctx, m.s, x, y, m.gap);
      wMax = Math.max(wMax, m.w);
    };
    line(it.text, `600 11px ${FONT}`, 0.9, on ? "255, 255, 255" : ink, 0.95, L.y);
    let y = L.y + 14;
    if (it.range) { line(blockWords(it.size), `400 10px ${FONT}`, 0.7, ink, 0.5, y); y += 12; }
    line(`${f.dur(it.end - now)} LEFT`, `400 10px ${FONT}`, 0.7, ink, 0.66, y);
    if (isNew(it, now)) line("NEW BAN", `600 10px ${FONT}`, 1.2, look.accent, 1, y + 12);
    const x0 = dir > 0 ? x : x - wMax, x1 = dir > 0 ? x + wMax : x;
    boxes.push({ x0: x0 - 4, y0: L.y - 3, x1: x1 + 4, y1: L.y + L.hgt + 1, it });
  }

  // how a label line fits `room` px: as it is; else with its letters closed up; else (a long
  // host) shortened in the middle, keeping its start and end. Remembered, so a frame measures
  // only what's new.
  function fit(ctx, text, gap, room) {
    const key = `${ctx.font}|${gap}|${Math.floor(room)}|${fontGen}|${text}`;
    let m = fitMemo.get(key);
    if (m) return m;
    let s = text, g = gap, wd = widthOf(ctx, s, g);
    if (wd > room) { g = 0; wd = widthOf(ctx, s, g); }
    for (let keep = text.length - 1; wd > room && keep >= 4; keep--) {
      const head = Math.ceil(keep / 2);
      s = `${text.slice(0, head)}…${text.slice(text.length - (keep - head))}`;
      wd = widthOf(ctx, s, g);
    }
    m = { s, gap: g, w: wd };
    if (fitMemo.size > 300) fitMemo.clear();
    fitMemo.set(key, m);
    return m;
  }

  function widthOf(ctx, text, gap) {
    const sp = "letterSpacing" in ctx;
    if (sp) ctx.letterSpacing = `${gap}px`;
    const wd = ctx.measureText(text).width;
    if (sp) ctx.letterSpacing = "0px";
    return wd;
  }

  // letter-spaced text with a black edge (canvas letterSpacing is not everywhere yet)
  function spaced(ctx, text, x, y, gap) {
    const sp = "letterSpacing" in ctx;
    if (sp) ctx.letterSpacing = `${gap}px`;
    ctx.strokeText(text, x, y);
    ctx.fillText(text, x, y);
    if (sp) ctx.letterSpacing = "0px";
  }

  // ---- picking: the ring under the pointer (its arc or head), or a label
  function hitAt(x, y) {
    for (const b of boxes) if (x >= b.x0 && x <= b.x1 && y >= b.y0 && y <= b.y1 && !b.it.gone) return b.it;
    const { cx, cy } = geo;
    const d = Math.hypot(x - cx, y - cy);
    let ang = Math.atan2(y - cy, x - cx) - TOP;
    if (ang < 0) ang += TAU;
    let best = null, bd = Infinity;
    for (const it of order) {
      if (it.slot < 0 || it.r === null || it.alpha < 0.3 || it.frShown === undefined) continue;
      const dh = Math.hypot(x - it.hx, y - it.hy);
      if (dh < 11 && dh < bd) { best = it; bd = dh; continue; }
      const dr = Math.abs(d - it.r);
      if (dr > Math.max(geo.sp * 0.55, 5)) continue;
      if (ang > it.frShown * TAU + 0.04) continue;
      if (dr < bd) { best = it; bd = dr; }
    }
    return best;
  }

  const local = (e) => {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  function headOf(it) {
    return it.range ? `BANNED · ${it.text} · ${blockWords(it.size)}` : `BANNED · ${it.text}`;
  }
  function rowsOf(it) {
    const left = leftOf(it);
    const rows = [
      { label: "TIME LEFT", value: f.dur(left) },
      { label: "ENDS AT", value: f.dayClock(it.end) },
      { label: "KIND", value: it.range ? `RANGE · ${f.num(it.size)} ADDRESSES` : "ONE ADDRESS" },
    ];
    if (state === "off") {
      rows.push({ label: "LAST READ", value: readAt() ?? "—" },
        { note: "Your node isn't answering, so this is its ban list from the last reading, counted down here. If monerod restarts, its bans are cleared." });
    }
    rows.push({ note: via === "key" ? "Enter finds this ban in the BAN LIST panel."
      : lastPointer === "touch" ? "Tap it again to find this ban in the BAN LIST panel."
      : "Click to find this ban in the BAN LIST panel." });
    return rows;
  }
  // the shared tooltip is fixed to the window, so a ring's tip goes when the page scrolls under it
  function tipShow(x, y, it) { env.tip.showTip(x, y, headOf(it), rowsOf(it)); tipOn = true; }
  function tipHide() { tipOn = false; env.tip.hideTip(); }
  function tipAt(it) {
    if (!it || it.hx === undefined || !shown) return;
    const r = canvas.getBoundingClientRect();
    const x = r.left + it.hx, y = r.top + it.hy;
    // only beside a ring end that's in sight (not one scrolled off or under the top bar)
    if (y < underBar() || y > window.innerHeight || x < 0 || x > window.innerWidth) { tipHide(); return; }
    tipShow(x, y, it);
  }
  // stepping with the keys brings the clock back into sight when the page was scrolled away from it
  function clockIntoView() {
    const r = canvas.getBoundingClientRect();
    const top = r.top + geo.cy - geo.R - 40, bottom = r.top + geo.cy + geo.R + 40;
    const vh = window.innerHeight;
    const bar = underBar();
    if (top >= bar && bottom <= vh) return;
    window.scrollBy({ top: top < bar || bottom - top > vh - bar ? top - bar : bottom - vh + 8, behavior: "auto" });
  }
  const onScroll = () => { if (tipOn) { tipHide(); tapped = null; } };   // a later tap shows it again

  function choose(it, { tip = false, tell = true } = {}) {
    selected = it || null;
    if (!selected) { tipHide(); lightRow(); redraw(); return; }
    if (tell) say.textContent = `${headOf(selected)}. ${f.dur(leftOf(selected))} left, ends at ${f.dayClock(selected.end)}. Enter shows it in the BAN LIST panel.`;
    lightRow();
    redraw();
    if (tip) requestAnimationFrame(() => tipAt(selected));
  }

  canvas.addEventListener("pointerdown", (e) => { pointerFocus = true; lastPointer = e.pointerType || "mouse"; via = "pointer"; });
  canvas.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse") return;
    via = "pointer";
    const p = local(e);
    const it = hitAt(p.x, p.y);
    if (it !== hovered) { hovered = it; lightRow(); redraw(); }
    if (it) tipShow(e.clientX, e.clientY, it);
    else tipHide();
    canvas.style.cursor = it ? "pointer" : "default";
  });
  canvas.addEventListener("pointerleave", (e) => {
    if (e.pointerType !== "mouse") return;
    hovered = null; tipHide(); lightRow(); redraw();
  });
  canvas.addEventListener("click", (e) => {
    pointerFocus = false;
    const p = local(e);
    const it = hitAt(p.x, p.y);
    if (!it) { tapped = null; choose(null); return; }
    if (lastPointer === "mouse") { choose(it, { tell: false }); find(it); return; }
    if (tapped === it) { find(it); return; }            // second tap: find it in the BAN LIST
    tapped = it;
    choose(it, { tell: false });
    tipShow(e.clientX, e.clientY, it);
  });
  canvas.addEventListener("focus", () => {
    if (pointerFocus) return;
    via = "key";
    clockIntoView();
    if (!selected) selected = order.find((it) => it.slot >= 0) || null;
    choose(selected, { tip: true });
  });
  canvas.addEventListener("blur", () => {
    pointerFocus = false;
    tapped = null;
    choose(null);
  });
  canvas.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Escape") { choose(null); return; }
    if ((e.key === "Enter" || e.key === " ") && selected) { e.preventDefault(); find(selected); return; }
    const rings = order.filter((it) => it.slot >= 0);
    if (!rings.length) return;
    const i = selected ? rings.indexOf(selected) : -1;
    const next = { ArrowDown: i + 1, ArrowRight: i + 1, ArrowUp: i - 1, ArrowLeft: i - 1, Home: 0, End: rings.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    via = "key";
    clockIntoView();
    choose(rings[(next + rings.length) % rings.length], { tip: true });
  });

  // ---- finding a ban's row in the BAN LIST panel (which re-draws its list on every reading)
  function rowFor(host) {
    const box = $("ban-list");
    if (!box) return null;
    const want = rangeLabel(host);
    for (const li of box.children) {
      if (li.dataset.host === host) return li;
      const h = li.querySelector(".cl-host");
      if (h && (h.textContent.trim() === want || h.textContent.trim() === host)) return li;
    }
    return null;
  }
  const watch = new MutationObserver(() => markFound());
  function markFound() {
    if (!found) return;
    const left = found.until - performance.now();
    if (left <= 0) return;
    const row = rowFor(found.host);
    if (!row || row.classList.contains("bn-found")) return;
    row.style.animationDelay = `${-((FOUND_MS - left) / 1000).toFixed(2)}s`;   // a re-drawn row carries on where it was
    row.classList.add("bn-found");
  }
  function unmark() {
    const box = $("ban-list");
    if (box) for (const li of box.querySelectorAll(".bn-found")) { li.classList.remove("bn-found"); li.style.animationDelay = ""; }
  }
  function find(it) {
    const panel = $("p-bans");
    const box = $("ban-list");
    if (!panel || !box || panel.hidden) { note("THE BAN LIST PANEL IS HIDDEN · SETTINGS → PANELS SHOWS IT"); return; }
    const row = rowFor(it.host);
    if (!row) { note(`${it.text} ISN'T IN THE BAN LIST RIGHT NOW`); return; }
    unmark();
    clearTimeout(found?.timer);
    found = { host: it.host, until: performance.now() + FOUND_MS, timer: setTimeout(() => { found = null; watch.disconnect(); unmark(); }, FOUND_MS) };
    watch.disconnect();
    watch.observe(box, { childList: true });
    markFound();
    const r = row.getBoundingClientRect();
    const inView = r.top >= underBar() && r.bottom <= window.innerHeight - 10;
    if (!inView) {
      tipHide();   // the tip is fixed to the window: it would be left floating over the panel
      row.scrollIntoView({ block: "center", behavior: moving(look) ? "smooth" : "auto" });
    }
    note(`${it.text} · LIT IN THE BAN LIST PANEL`);
  }
  function note(text) {
    clearTimeout(noteTimer);
    hint.textContent = text;
    hint.classList.add("is-note");
    say.textContent = text.charAt(0) + text.slice(1).toLowerCase();
    noteTimer = setTimeout(() => { hint.classList.remove("is-note"); hint.textContent = lastPointer === "touch" || touchFirst() ? HINT_TOUCH : HINT; }, 6000);
  }

  // ---- the words: centre readout, side column, list, subtitle, the canvas's label
  function words() {
    const s = lastSnap || {};
    const node = s.node || {};
    const known = state === "ok" || state === "none";
    const off = state === "off";   // the rings are the last reading's: every "now" number is unknown
    const t = readAt();
    // centre
    view.dataset.state = root.dataset.state = state;
    if (state === "none") {
      coreN.hidden = true;
      setText(coreL, "NO BANS WITH 24\u00a0H OR\u00a0LESS LEFT");
    } else {
      coreN.hidden = false;
      setText(coreN, known ? f.num(order.length) : "—");
      setText(coreL, BANNED_24);
    }
    setText(coreLongN, off ? "—" : f.num(node.bans_long));
    let why = null;
    if (off) why = `${t ? `LAST READ ${t} · ` : ""}NODE NOT ANSWERING${order.length ? " · THE DIM RINGS ARE ITS BANS THEN, STILL COUNTED DOWN" : ""}`;
    else if (state === "hidden") why = "THIS RPC PORT HIDES THE BAN LIST · YOUR NODE'S UNRESTRICTED PORT SHOWS IT (SETTINGS → DATA SOURCE)";
    else if (state === "wait") why = "THE NODE HASN'T SAID WHICH ADDRESSES IT BANS YET";
    empty.hidden = !why;
    setText(empty, why || "");
    // side column
    setText(allN, off ? "—" : f.num(node.bans));
    const ranges = node.ban_ranges;
    setText(allSub, off ? "NODE NOT ANSWERING" : f.isNum(node.bans) && f.isNum(ranges)
      ? `${f.plural(ranges, "RANGE", "RANGES")} · ${f.plural(node.bans - ranges, "SINGLE ADDRESS", "SINGLE ADDRESSES")}` : "");
    setText(nextK, off && order.length ? "ENDS NEXT · AS LAST READ" : "ENDS NEXT");
    tickWords();
    // the list: every ring when the labels can't go beside the clock, else the ones that had no room
    const rows = (wide ? unfit : order.filter((it) => it.slot >= 0)).filter((it) => bans.has(it.host) && !it.gone);
    const key = (wide ? "W" : "N") + rows.map((it) => it.host).join(",");
    if (key !== listKey) {
      listKey = key;
      clear(list);
      for (const it of rows) {
        const t = el("span", { class: "bn-row-t" });
        const li = el("li", { class: "bn-row", "data-host": it.host },
          el("span", { class: `bn-g${it.range ? " bn-g-range" : ""}`, "aria-hidden": "true" }),
          el("span", { class: "bn-row-h" }, el("b", { text: it.text }), it.range ? el("small", { text: blockWords(it.size) }) : null),
          t);
        li._ring = it;
        li._t = t;
        li.addEventListener("pointerenter", () => { hovered = it; lightRow(); redraw(); });
        li.addEventListener("pointerleave", () => { if (hovered === it) { hovered = null; lightRow(); redraw(); } });
        li.addEventListener("click", () => { if (bans.has(it.host) && !it.gone) { choose(it, { tell: false }); find(it); } });
        list.append(li);
      }
      tickWords();
      lightRow();
    }
    list.hidden = listHead.hidden = !rows.length;
    setText(listHead, wide ? "NO ROOM FOR A LABEL ON THE CLOCK" : "ON THE CLOCK · OUTSIDE IN");
    const rest = order.filter((it) => it.slot < 0);
    more.hidden = !rest.length;
    if (rest.length) {
      setText(more, `+${f.num(rest.length)} MORE NOT DRAWN (THE CLOCK HAS ${RINGS} RINGS) · EACH ENDS WITHIN ${f.dur(leftOf(rest[0]))} · ALL ARE IN THE BAN LIST`);
    }
    // subtitle and the canvas's spoken summary
    setText(env.sub, off ? `FROM YOUR NODE · NOT ANSWERING${t ? ` · LAST READ ${t}` : ""}`
      : state === "hidden" ? "FROM YOUR NODE · THIS RPC PORT HIDES BANS"
      : `FROM YOUR NODE · BAN LIST READ EVERY MINUTE, COUNTED DOWN HERE EVERY SECOND · ${known ? f.plural(order.length, "BAN", "BANS") : "— BANS"} WITH 24 H OR LESS · ${f.num(node.bans_long)} LONGER`);
    const soon = order[order.length - 1];
    setAttr(canvas, "aria-label", off
      ? `Ban clock: your node isn't answering.${order.length ? ` Showing the ${f.plural(order.length, "ban")} with 24 hours or less left at its last reading${t ? `, at ${t}` : ""}, counted down here; if monerod restarts, its bans are cleared.` : ""}`
      : known
      ? `Ban clock: ${f.plural(order.length, "ban")} with 24 hours or less left${order.length > RINGS ? ` (the ${RINGS} with the most time left are drawn)` : ""}, ${f.num(node.bans_long)} more with over 24 hours left.${soon ? ` Ends next: ${soon.text}, in ${f.dur(leftOf(soon))}.` : ""} Arrow keys step through the bans; Enter finds the selected one in the BAN LIST panel.`
      : `Ban clock: ${why ? why.toLowerCase() : "no ban list"}.`);
  }

  // the parts that count down every second
  function tickWords() {
    const soon = order[order.length - 1];
    if (soon) {
      setText(nextHost, soon.range ? `${soon.text} · ${blockWords(soon.size)}` : soon.text);
      setText(nextWhen, `IN ${f.dur(leftOf(soon))} · AT ${f.dayClock(soon.end)}`);
    } else {
      setText(nextHost, state === "none" ? "NOTHING" : "—");
      setText(nextWhen, state === "none" ? "NO BANS WITH 24 H OR LESS LEFT" : "");
    }
    for (const li of list.children) if (li._ring) setText(li._t, f.dur(leftOf(li._ring)));
  }

  function lightRow() {
    const on = focusRing();
    for (const li of list.children) li.classList.toggle("is-on", li._ring === on);
  }

  // run the frame loop only while the clock moves (full motion, node answering); else one still frame
  function motion() {
    if (anim()) stage.start();
    else if (stage.running) { stage.stop(); stage.frame(); }
  }

  // once a second while on show: count down the words; a still clock redraws when its labels change
  function tick() {
    if (!shown) return;
    if (expire() && !stage.running) stage.frame();
    tickWords();
    if (!stage.running && wide) {
      const now = env.now();
      const sig = order.filter((it) => it.slot >= 0).map((it) => `${it.host}${f.dur(it.end - now)}`).join("|");
      if (sig !== stillSig) { stillSig = sig; stage.frame(); }
    }
  }

  return {
    update(snap, now) {
      lastSnap = snap;
      sync(snap);
      words();
      if (selected && !bans.has(selected.host)) choose(null);
      if (shown) motion();
      redraw();
      void now;
    },
    show() {
      shown = true;
      if (anim()) stage.start(); else stage.frame();
      clearTimeout(ticker);
      // just after each whole second of the server's clock, as the other countdowns
      const step = () => { tick(); ticker = shown ? setTimeout(step, 1015 - ((env.now() * 1000) % 1000)) : 0; };
      ticker = setTimeout(step, 1015 - ((env.now() * 1000) % 1000));
      window.addEventListener("scroll", onScroll, { passive: true });
      tickWords();
    },
    hide() {
      shown = false;
      stage.stop();
      clearTimeout(ticker);
      ticker = 0;
      window.removeEventListener("scroll", onScroll);
      tipHide();
      for (const it of [...bans.values()]) if (it.gone) bans.delete(it.host);
    },
    look(lk) {
      look = lk;
      if (shown) { motion(); redraw(); }
    },
  };
}
