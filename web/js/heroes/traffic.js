// TRAFFIC screen hero: an oscilloscope of your node's traffic. Two live traces,
// DOWN (data coming in) and UP (data going out), scroll from right to left over
// the last 15 minutes on a faint glowing grid. The newest stretch of each trace
// glows brighter and fades back, like phosphor, and a bright dot at the head
// carries the newest point.
//
// Everything drawn is a reading. The node is read every second, but one
// second's rate is too jumpy to draw, so each point is a row of the server's
// history: the readings in 5 seconds, averaged (longer rows when the node is
// read less often), and the history the server sends later draws the same
// line. The newest point is the last 5 seconds of readings averaged and drawn
// at their middle: it moves on a little with every reading, and with a row's
// last reading it lands on that row's own point, so nothing jumps when the
// next row starts. The head dot and its label sit on it. In full motion only
// the time axis moves: the right edge is "now" on the server's clock and
// slides on smoothly; in reduced motion it is a still chart redrawn on each
// reading. Gaps where the node wasn't answering stay gaps. The node's own
// limits show as dashed lines when they are near enough to the readings to
// matter (within 3x of the peak), and as a note otherwise.
//
// Beside the scope: the newest point's two figures in big (the same numbers as
// the head labels), totals since the node started, the 15-minute peaks, and a
// flow strip per direction where dots run toward your node (data in) or away
// from it (data out), faster when more data flows. The dots show the speed,
// not single messages; the caption says so.
//
// Two stages: the scope scrolls about a pixel a second, so it draws at 12
// frames a second; only the small flow strip runs at 30.
import { el, setText, setFig } from "../dom.js";
import * as f from "../fmt.js";
import { api } from "../api.js";
import { createStage, glowSprite, stamp, moving, clamp, TAU, cssRGB } from "../fx/space.js";

const WINDOW = 900;          // seconds across the scope
const KEEP = WINDOW + 120;   // readings kept (a little past the left edge, so lines leave it smoothly)
const TRAIL_S = 45;          // the bright, fading stretch behind the head
const LIMIT_NEAR = 3;        // a limit is drawn when it is at most this many times the peak
const NICE = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
const FONT = '"Plex Mono", ui-monospace, monospace';
const HEAD_FONT = `600 11px ${FONT}`;   // the readings beside the head dots
const HEAD_GAP = 0.4;                   // their letter spacing
const AXIS_FONT = `500 9.5px ${FONT}`;
const isNum = f.isNum;
const val = (v) => (isNum(v) && v >= 0 ? v : null);

// the smallest "nice" number at or above v (for the top of the scale)
function niceCeil(v) {
  const e = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of NICE) if (m * e >= v * 0.9999) return m * e;
  return 10 * e;
}
// gridline spacing for a scale: about five lines, on round numbers
function gridStep(scale) {
  const rough = scale / 5;
  const e = Math.pow(10, Math.floor(Math.log10(rough)));
  for (const m of [1, 2, 2.5, 5]) if (m * e >= rough * 0.9999) return m * e;
  return 10 * e;
}
const mantissa = (s) => Math.round(s / Math.pow(10, Math.floor(Math.log10(s))) * 10) / 10;
// "r, g, b" mixed toward white (for the hot core of a trace)
function whiter(rgb, k) {
  return rgb.split(",").map((c) => Math.round(Number(c) + (255 - Number(c)) * k)).join(", ");
}
// monerod's own unit for limits (kB/s of 1,024 bytes), as the TRAFFIC panel and `limit` show them
const kbps = (v) => (isNum(v) ? `${f.num(Math.round(v / 1024))} kB/s` : "—");
const shortClock = (t, sec) => f.clock(t, sec).replace(/\s?[AP]M$/i, "");

export default function create(body, env) {
  // ---- DOM: the scope (canvas + corner readouts), and the side column
  const canvas = el("canvas", {
    class: "lt-canvas", tabindex: "0", role: "img",
    "aria-label": "Live traffic over the last 15 minutes. Left and right arrow keys step through the readings.",
  });
  const legend = el("div", { class: "hero-hud lt-legend" },
    el("span", { class: "lt-lg" }, el("span", { class: "lg-line s1" }), el("span", { text: "DOWN · RECEIVED" })),
    el("span", { class: "lt-lg" }, el("span", { class: "lg-line s2" }), el("span", { text: "UP · SENT" })),
    el("span", { class: "lt-lg", hidden: true }, el("span", { class: "lt-dash" }),
      el("span", { class: "lt-lg-long", text: "YOUR NODE'S LIMIT" }), el("span", { class: "lt-lg-short", text: "LIMIT" })));
  const limitDash = legend.lastChild;
  // limits far above the readings: a note over the scope (wide screens) or in the side column (narrower ones)
  const note = el("div", { class: "hero-hud lt-note", hidden: true });
  const sideNote = el("p", { class: "lt-side-note", hidden: true });
  const empty = el("div", { class: "lt-empty", hidden: true });
  const scope = el("div", { class: "lt-scope" }, canvas, legend, note, empty);

  function dirBlock(key, name, sub) {
    const big = el("div", { class: "lt-big", text: "—" });
    const total = el("dd", { text: "—" });
    const peak = el("dd", { text: "—" });
    const block = el("div", { class: `lt-dir lt-dir-${key}` },
      el("div", { class: "lt-dir-head" },
        el("span", { class: `lg-line ${key === "down" ? "s1" : "s2"}` }),
        el("span", { class: "lt-dir-name", text: name }),
        el("span", { class: "lt-dir-sub", text: sub })),
      big,
      el("dl", { class: "lt-stats" },
        el("div", {}, el("dt", { text: "SINCE START" }), total),
        el("div", {}, el("dt", { text: "15-MIN PEAK" }), peak)));
    return { block, big, total, peak };
  }
  const down = dirBlock("down", "DOWN", "FROM OTHER NODES");
  const up = dirBlock("up", "UP", "TO OTHER NODES");
  const flowCanvas = el("canvas", { class: "lt-flow-canvas", "aria-hidden": "true" });
  const flowBox = el("div", { class: "lt-flow" },
    flowCanvas,
    el("div", { class: "lt-flow-ends", "aria-hidden": "true" }, el("span", { text: "OTHER NODES" }), el("span", { text: "YOUR NODE" })));
  const cap = el("p", { class: "lt-cap" });
  const side = el("div", { class: "lt-side" }, down.block, flowBox, up.block, sideNote, cap);
  const say = el("p", { class: "sr-only", "aria-live": "polite" });
  body.append(el("div", { class: "lt" }, scope, side), say);

  // ---- state
  let look = env.look();
  let shown = false;
  let rgbDown = "30, 162, 180", rgbUp = "138, 120, 236", hotDown = "", hotUp = "";
  let hist = null;              // the server's history: {s: each row's start, x: where it is drawn, d, u, step}
  const rows = [];              // the 5 s rows the snapshots brought: {s: start, d, u: the server's averages, x: where it is drawn}
  const recent = [];            // the last minute's readings: {t, d, u}
  let front = null;             // the newest point: the last row's length of readings averaged, {x, d, u}
  let T = [], D = [], U = [];   // the merged rows, oldest first (d / u null where there was no reading)
  let step = 5;                 // seconds in a row
  let poll = 5;
  let lastSnap = null;
  let lastT = null;             // when the newest reading was taken
  let latest = { d: null, u: null };   // the newest point (the big figures and the head labels)
  let flow = { d: null, u: null };     // the flow strips' rates: the same, or none while the node isn't answering
  let peaks = { d: null, u: null, dt: null, ut: null, all: 0 };
  let limits = { down: null, up: null, showDown: false, showUp: false };
  let head = null;              // the time at the scope's right edge (full motion: "now", sliding on smoothly)
  let scale = 0;                // value at the top of the scope (eases toward scaleWant)
  let scaleWant = 10e3;
  const grids = [];             // [{step, a}] value gridlines, cross-faded when the spacing changes
  let hover = null;             // {t, how: "mouse" | "touch" | "key", gap}: gap = pointing where there was no reading
  let touching = false;
  let histAsked = 0;
  let hudBottom = 0;            // the lowest edge of the readouts over the scope (the plot starts below it)
  const geo = { x0: 0, x1: 0, y0: 0, yb: 0, pps: 1, head: 0, w: 0 };   // last frame's layout, for pointing

  // ---- colours from the look
  function colours() {
    rgbDown = cssRGB("--s1", "30, 162, 180");
    rgbUp = cssRGB("--s2", "138, 120, 236");
    hotDown = whiter(rgbDown, 0.38);
    hotUp = whiter(rgbUp, 0.38);
  }
  colours();

  // ---- readings
  // The server's history for the window answers one row per 5 s (one per poll when
  // the node is read less often); each row is drawn at the middle of its time.
  function askHistory() {
    const n = Date.now();
    if (n - histAsked < 20000) return;
    histAsked = n;
    api.history(WINDOW).then((h) => {
      if (!h || !Array.isArray(h.t)) return;
      const hs = Number(h.step) || 5;
      const s = [], x = [], d = [], u = [];
      for (let i = 0; i < h.t.length; i++) {
        if (!isNum(h.t[i])) continue;
        s.push(h.t[i]);
        x.push(h.t[i] + hs / 2);
        d.push(val(h.rate_in?.[i]));
        u.push(val(h.rate_out?.[i]));
      }
      // the newest row is still filling: drawn no later than now
      const k = x.length - 1;
      if (k >= 0) x[k] = clamp(env.now(), s[k], x[k]);
      // the newest row may just not be filled yet: an empty one at the end is not a gap
      if (s.length && d[d.length - 1] === null && u[u.length - 1] === null) { s.pop(); x.pop(); d.pop(); u.pop(); }
      hist = { s, x, d, u, step: hs };
      merge();
      if (lastSnap) readouts(lastSnap);
      if (shown && !stage.running) stage.frame();
    }).catch(() => {});
  }

  // the history's rows, then the snapshots' (averaged into the history's step the way
  // the server does it). Where both have a row, the history's wins if that row had
  // surely finished when it answered (a reading the window missed is in it), and the
  // snapshots' wins otherwise (it is newer). A row the snapshots brought stays where
  // its newest point last was; the newest row is the newest point (see average()).
  function merge() {
    const hs = hist ? hist.step : Math.max(5, Math.ceil(poll));
    step = hs;
    front = lastT === null ? null : average(lastT);
    if (front && rows.length) rows[rows.length - 1].x = front.x;
    const live = [];
    for (const r of rows) {
      const b = r.s - (r.s % hs);
      let c = live[live.length - 1];
      if (!c || c.b !== b) live.push(c = { b, d: null, u: null, nd: 0, nu: 0, x: r.x });
      if (r.d !== null) c.d = ((c.d ?? 0) * c.nd + r.d) / ++c.nd;
      if (r.u !== null) c.u = ((c.u ?? 0) * c.nu + r.u) / ++c.nu;
      c.x = r.x;
    }
    if (live.length && front) Object.assign(live[live.length - 1], front);
    const nt = [], nd = [], nu = [];
    const hn = hist ? hist.s.length : 0;
    // history rows that start before this one had finished when it answered (the one
    // before its newest may still have been waiting for a reading under way)
    const done = hn ? hist.s[hn - 1] - hs : -Infinity;
    let i = 0, j = 0;
    while (i < hn || j < live.length) {
      const a = i < hn ? hist.s[i] : Infinity;
      const c = j < live.length ? live[j] : null;
      const b = c ? c.b : Infinity;
      let x, d, u;
      if (a < b || (a === b && a < done)) {
        x = hist.x[i]; d = hist.d[i]; u = hist.u[i];
        if (a === b) j++;
        i++;
      } else {
        x = c.x; d = c.d; u = c.u;
        if (a === b) i++;
        j++;
      }
      // always in time order (pointing looks rows up by time)
      if (nt.length && x <= nt[nt.length - 1]) x = nt[nt.length - 1] + 0.01;
      nt.push(x); nd.push(d); nu.push(u);
    }
    // keep only what can still be seen
    const newest = nt.length ? nt[nt.length - 1] : 0;
    let s = 0;
    while (s < nt.length && nt[s] < newest - KEEP) s++;
    T = nt.slice(s); D = nd.slice(s); U = nu.slice(s);
    latest = { d: D.length ? D[D.length - 1] : null, u: U.length ? U[U.length - 1] : null };
    measure();
  }

  // peaks, the scale and the limits from the readings in view
  function measure() {
    const newest = T.length ? T[T.length - 1] : null;
    let pd = null, pu = null, pdt = null, put = null;
    for (let i = 0; i < T.length; i++) {
      if (T[i] < newest - WINDOW) continue;
      if (D[i] !== null && (pd === null || D[i] > pd)) { pd = D[i]; pdt = T[i]; }
      if (U[i] !== null && (pu === null || U[i] > pu)) { pu = U[i]; put = T[i]; }
    }
    const all = Math.max(pd || 0, pu || 0);
    peaks = { d: pd, u: pu, dt: pdt, ut: put, all };
    const node = lastSnap?.node || {};
    const ld = isNum(node.limit_down) && node.limit_down > 0 ? node.limit_down : null;
    const lu = isNum(node.limit_up) && node.limit_up > 0 ? node.limit_up : null;
    limits = {
      down: ld, up: lu,
      showDown: ld !== null && all > 0 && ld <= all * LIMIT_NEAR,
      showUp: lu !== null && all > 0 && lu <= all * LIMIT_NEAR,
    };
    let top = Math.max(all * 1.12, 8e3);
    if (limits.showDown) top = Math.max(top, ld * 1.08);
    if (limits.showUp) top = Math.max(top, lu * 1.08);
    scaleWant = niceCeil(top);
    if (!scale || !moving(look) || !shown) scale = scaleWant;
  }

  // a snapshot's reading, and the row it is in as the server has it so far (snap.hist;
  // a server without it: the reading itself)
  function addReading(snap) {
    const g = isNum(snap.t) ? snap.t : snap.generated;
    if (!isNum(g) || (lastT !== null && g <= lastT)) return false;
    const tr = snap.traffic || {};
    const answering = snap.source?.answering !== false;
    const d = answering ? val(tr.rate_in) : null, u = answering ? val(tr.rate_out) : null;
    // a long silence (the window was hidden or the computer slept): the server's history fills it in
    if (lastT !== null && g - lastT > step * 3) askHistory();
    lastT = g;
    recent.push({ t: g, d, u });
    while (recent[0].t < g - 60) recent.shift();
    const h = snap.hist;
    const r = h && isNum(h.t) ? { s: h.t, d: val(h.rate_in), u: val(h.rate_out) } : { s: g - (g % 5), d, u };
    const last = rows[rows.length - 1];
    if (last && last.s === r.s) Object.assign(last, r);
    else if (!last || r.s > last.s) rows.push(r);
    while (rows.length && rows[0].x < g - KEEP) rows.shift();
    merge();
    return true;
  }

  // the newest point: the readings in the last row's length averaged (the last 5 s when
  // the node is read every second), at their middle. One second's rate alone jumps
  // about; this moves on smoothly with each reading, and at a row's last reading it is
  // that row's own average, so the row takes its place without a jump.
  function average(g) {
    let n = 0, t = 0, d = 0, nd = 0, u = 0, nu = 0;
    for (let k = recent.length - 1; k >= 0 && recent[k].t > g - step + 0.5; k--) {
      const r = recent[k];
      n++; t += r.t;
      if (r.d !== null) { d += r.d; nd++; }
      if (r.u !== null) { u += r.u; nu++; }
    }
    return { x: t / n, d: nd ? d / nd : null, u: nu ? u / nu : null };
  }

  // ---- the side column, the corner notes and the empty states
  function readouts(snap) {
    const src = snap.source || {};
    const tr = snap.traffic || {};
    const answering = src.answering !== false;
    // the newest point, like the head labels (one second alone jumps about too much to read);
    // the flow strips run on the same numbers
    flow = answering ? latest : { d: null, u: null };
    setFig(down.big, f.rate(flow.d));
    setFig(up.big, f.rate(flow.u));
    setText(down.total, f.bytes(val(tr.bytes_in)));
    setText(up.total, f.bytes(val(tr.bytes_out)));
    setText(down.peak, f.rate(peaks.d));
    setText(up.peak, f.rate(peaks.u));

    // limits: drawn when near the readings, a note otherwise (over the scope when wide,
    // a short line in the side column below 1200px, where the scope has no room for it)
    const far = [];
    if (limits.down !== null && !limits.showDown) far.push(["DOWN", limits.down]);
    if (limits.up !== null && !limits.showUp) far.push(["UP", limits.up]);
    const both = far.length === 2;
    setText(note, far.length ? `NODE LIMITS · ${far.map(([k, v]) => `${k} ${kbps(v)}`).join(" · ")} · ${both ? "BOTH" : "IT IS"} FAR ABOVE THESE LINES` : "");
    setText(sideNote, !far.length ? ""
      : both ? `NODE LIMITS · DOWN ${f.num(Math.round(far[0][1] / 1024))} · UP ${kbps(far[1][1])} · BOTH FAR ABOVE YOUR TRAFFIC`
      : `NODE LIMIT · ${far[0][0]} ${kbps(far[0][1])} · FAR ABOVE YOUR TRAFFIC`);
    note.hidden = sideNote.hidden = !far.length;
    limitDash.hidden = !(limits.showDown || limits.showUp);

    let why = null;
    if (src.restricted) why = "THIS RPC PORT DOESN'T SHARE TRAFFIC NUMBERS · POINT THE DASHBOARD AT THE UNRESTRICTED PORT (SETTINGS → DATA SOURCE)";
    else if (!answering) why = "YOUR NODE ISN'T ANSWERING · THE LINES BREAK UNTIL IT DOES";
    else if (!D.some((v) => v !== null) && !U.some((v) => v !== null)) why = "COLLECTING · THE LINES START WITH THE NEXT READINGS";
    empty.hidden = !why;
    setText(empty, why || "");

    words();
    // (left alone while the scope has focus: a screen reader could read out the new numbers every second)
    if (document.activeElement === canvas) return;
    const lim = limits.down !== null ? ` Your node's limits: ${kbps(limits.down)} down, ${kbps(limits.up)} up.` : "";
    const label = `Live traffic over the last 15 minutes. Now ${f.rate(flow.d)} down and ${f.rate(flow.u)} up; `
      + `15-minute peaks ${f.rate(peaks.d)} down and ${f.rate(peaks.u)} up.${lim} Left and right arrow keys step through the readings.`;
    if (canvas.getAttribute("aria-label") !== label) canvas.setAttribute("aria-label", label);
  }

  // the subtitle and the caption say what the dots do, which depends on the motion setting,
  // and that the lines and figures are averages when the node is read more often than a row
  function words() {
    const src = lastSnap?.source || {};
    const go = moving(look);
    setText(env.sub, src.answering === false ? "FROM YOUR NODE · NOT ANSWERING"
      : `FROM YOUR NODE · EVERY ${f.num(src.poll_s || 5)} S · LAST 15 MIN · ${go ? "DOTS SPEED UP WITH THE DATA" : "DOTS STILL · MOTION OFF"}`);
    const avg = step > (src.poll_s || 5) ? ` The lines and figures are averages over ${f.num(step)} seconds.` : "";
    setText(cap, (go
      ? "Dots run toward your node for data coming in and away from it for data going out; the more data, the faster they go. The dots show speed, not single messages."
      : "Motion is off, so the dots stand still and only show which way data flows: in toward your node, out away from it.")
      + `${avg} Point at the lines for exact numbers.`);
  }
  words();

  // the plot starts below the readouts over the scope (the legend can wrap onto a second row)
  function measureHud() {
    let b = 0;
    for (const n of [legend, note]) if (!n.hidden && n.offsetParent) b = Math.max(b, n.offsetTop + n.offsetHeight);
    if (b === hudBottom) return;
    hudBottom = b;
    if (shown && !stage.running) stage.frame();
  }
  const hudRO = new ResizeObserver(measureHud);
  hudRO.observe(legend);
  hudRO.observe(note);

  // ---- drawing
  // the scope moves about a pixel a second: 12 frames a second is plenty (the flow strip has its own stage)
  const stage = createStage(canvas, { draw, maxFps: 12 });
  const X = (t) => geo.x1 - (geo.head - t) * geo.pps;
  let Y = (v) => v;
  // width of letter-spaced text (as text() draws it)
  const textW = (ctx, s, gap) => ctx.measureText(s).width + gap * s.length;

  function draw(ctx, w, h, t, dt) {
    const go = moving(look);
    ctx.clearRect(0, 0, w, h);

    // where the right edge is in time. Still: the newest reading. Moving: "now" on the
    // server's clock, sliding on smoothly (small corrections, never backwards); the
    // newest point is never past the edge, so its dot and label are always drawn.
    const newest = T.length ? T[T.length - 1] : null;
    if (newest === null) head = null;
    else if (!go) head = Math.max(newest, lastT ?? newest);
    else {
      const want = Math.max(newest, env.now());
      if (head === null || want - head > 8 || head - want > 30) head = want;
      else head += dt * clamp(1 + (want - head) * 0.8, 0.25, 2);
      if (head < newest) head = newest;
    }
    // the scale eases toward its new top; gridlines cross-fade when their spacing changes
    if (go) scale += (scaleWant - scale) * (1 - Math.exp(-dt * 2.4));
    else scale = scaleWant;
    if (!scale) scale = scaleWant;
    const gs = gridStep(scale);
    if (!grids.length || grids[grids.length - 1].step !== gs) {
      const old = grids.find((g) => g.step === gs);
      if (old) { grids.splice(grids.indexOf(old), 1); grids.push(old); } else grids.push({ step: gs, a: go ? 0 : 1 });
    }
    for (let i = grids.length - 1; i >= 0; i--) {
      const g = grids[i];
      const want = i === grids.length - 1 ? 1 : 0;
      g.a = go ? g.a + (want - g.a) * Math.min(1, dt * 3.5) : want;
      if (want === 0 && g.a < 0.02) grids.splice(i, 1);
    }

    // layout: room on the right for the widest head reading ("88.8 kB/s": the font is
    // monospaced and a rate is never longer), and the plot below the legend and note
    const narrow = w < 560;
    const x0 = narrow ? 8 : 12;
    ctx.font = HEAD_FONT;
    const x1 = w - Math.max(narrow ? 0 : 96, Math.ceil(11 + textW(ctx, "88.8 kB/s", HEAD_GAP) + 5));
    const y0 = Math.max(40, Math.ceil(hudBottom) + 10);
    const yb = h - 24;
    Object.assign(geo, { x0, x1, y0, yb, pps: (x1 - x0) / WINDOW, head: head ?? 0, w });
    Y = (v) => yb - (v / scale) * (yb - y0);

    const ink = look.ink || "232, 232, 240";
    const cool = look.head || "107, 174, 200";
    const glowOn = look.glow !== false;
    const breath = glowOn && go ? 0.86 + 0.14 * Math.sin((t * TAU) / 7) : glowOn ? 0.92 : 0.45;

    // a faint light in the glass
    ctx.globalCompositeOperation = "lighter";
    ctx.globalAlpha = glowOn ? 0.5 : 0.25;
    stamp(ctx, glowSprite(cool, 128, 0.12), (x0 + x1) / 2, (y0 + yb) / 2, Math.max(x1 - x0, 300) * 1.1);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";

    drawGrid(ctx, x0, x1, y0, yb, cool, ink, go);
    if (head !== null) {
      drawLimits(ctx, x0, x1, ink);
      const hd = drawTrace(ctx, D, rgbDown, hotDown, x0, x1, y0, yb, breath);
      const hu = drawTrace(ctx, U, rgbUp, hotUp, x0, x1, y0, yb, breath);
      // the edge where readings leave: fade out, like the end of a phosphor trace
      ctx.globalCompositeOperation = "destination-out";
      const fade = ctx.createLinearGradient(x0, 0, x0 + 46, 0);
      fade.addColorStop(0, "rgba(0, 0, 0, 1)");
      fade.addColorStop(1, "rgba(0, 0, 0, 0)");
      ctx.fillStyle = fade;
      ctx.fillRect(0, y0 - 8, x0 + 46, yb - y0 + 9);
      ctx.globalCompositeOperation = "source-over";
      drawPeaks(ctx, x0, x1, y0, ink);
      drawHeads(ctx, hd, hu, x1, y0, yb, ink, breath);
    }
    // the crosshair and its time box go over everything, the axis labels included
    findSpot(ctx, x0, x1);
    drawAxisText(ctx, x0, x1, y0, yb, ink);
    if (head !== null) drawHover(ctx, x0, x1, y0, yb, ink);
  }

  function hline(ctx, x0, x1, y, rgb, a) {
    ctx.strokeStyle = `rgba(${rgb}, ${a.toFixed(3)})`;
    ctx.beginPath(); ctx.moveTo(x0, Math.round(y) + 0.5); ctx.lineTo(x1, Math.round(y) + 0.5); ctx.stroke();
  }
  function vline(ctx, x, y0, y1, rgb, a) {
    ctx.strokeStyle = `rgba(${rgb}, ${a.toFixed(3)})`;
    ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y1); ctx.stroke();
  }

  // the graticule: value lines (major and minor) and time lines (every minute,
  // brighter every five), with a soft glow on the major ones
  function drawGrid(ctx, x0, x1, y0, yb, cool, ink, go) {
    ctx.lineWidth = 1;
    const span = yb - y0;
    for (const g of grids) {
      const s = g.step;
      const per = mantissa(s) === 2 ? 4 : 5;
      const minor = s / per;
      const px = (minor / scale) * span;
      if (px >= 8) {
        for (let k = 1; k * minor <= scale * 1.0001; k++) {
          if (k % per === 0) continue;
          hline(ctx, x0, x1, Y(k * minor), cool, 0.06 * g.a);
        }
      }
      for (let k = 1; k * s <= scale * 1.0001; k++) {
        const y = Y(k * s);
        ctx.lineWidth = 3;
        hline(ctx, x0, x1, y, cool, 0.035 * g.a);
        ctx.lineWidth = 1;
        hline(ctx, x0, x1, y, cool, 0.13 * g.a);
      }
    }
    // time lines scroll with the readings
    if (head !== null) {
      const tStart = head - WINDOW;
      for (let m = Math.ceil(tStart / 60) * 60; m <= head; m += 60) {
        const x = Math.round(X(m)) + 0.5;
        if (x < x0) continue;
        const major = m % 300 === 0;
        if (major) { ctx.lineWidth = 3; vline(ctx, x, y0, yb, cool, 0.03); ctx.lineWidth = 1; }
        vline(ctx, x, y0, yb, cool, major ? 0.13 : 0.06);
      }
      // small ticks every 15 s along the floor
      for (let m = Math.ceil(tStart / 15) * 15; m <= head; m += 15) {
        const x = Math.round(X(m)) + 0.5;
        if (x < x0 + 2) continue;
        vline(ctx, x, yb - (m % 60 === 0 ? 5 : 3), yb, cool, 0.3);
      }
    }
    // the floor (zero) and the right edge (now)
    ctx.lineWidth = 3;
    hline(ctx, x0, x1, yb, cool, 0.05);
    ctx.lineWidth = 1;
    hline(ctx, x0, x1, yb, cool, 0.28);
    vline(ctx, Math.round(x1) + 0.5, y0 - 4, yb, cool, 0.16);
  }

  // one trace: a soft wash under it, a wide glow, the line (older parts dimmer),
  // and the bright persistence stretch behind the head. Returns the head point:
  // the newest point, {x, y, v}, or null when the newest point has no value.
  function drawTrace(ctx, V, rgb, hot, x0, x1, y0, yb, breath) {
    const n = T.length;
    const gapLim = step * 2.6;
    const tLeft = head - WINDOW - step * 2;
    // the readings in view (all of them up to the newest: the edge is never before it)
    let i0 = 0;
    while (i0 < n && T[i0] < tLeft) i0++;
    let ib = n - 1;
    while (ib >= 0 && T[ib] > head) ib--;
    const path = new Path2D();    // the line, broken at gaps
    const area = new Path2D();    // each unbroken stretch closed down to the floor, for the wash
    const trail = new Path2D();   // the newest stretch
    const single = [];            // readings with no neighbour (drawn as dots)
    const tTrail = head - TRAIL_S;
    let pen = false, trailPen = false, prevT = 0, lastX = 0, lastY = 0, runN = 0, any = false;
    const endRun = () => {
      if (!pen) return;
      area.lineTo(lastX, yb);
      area.closePath();
      if (runN === 1) single.push([lastX, lastY]);
      pen = false; trailPen = false; runN = 0;
    };
    const add = (tt, v) => {
      const x = X(tt), y = Y(v);
      if (pen && tt - prevT > gapLim) endRun();
      if (pen) { path.lineTo(x, y); area.lineTo(x, y); } else { path.moveTo(x, y); area.moveTo(x, yb); area.lineTo(x, y); }
      if (tt >= tTrail) { if (trailPen) trail.lineTo(x, y); else trail.moveTo(x, y); trailPen = true; }
      pen = true; any = true; prevT = tt; lastX = x; lastY = y; runN++;
    };
    for (let i = i0; i <= ib; i++) {
      if (V[i] === null) { endRun(); continue; }
      // the trail starts exactly where it should, part-way along a line
      if (pen && !trailPen && prevT < tTrail && T[i] >= tTrail && T[i] - prevT <= gapLim && V[i - 1] !== null) {
        const u = (tTrail - prevT) / (T[i] - prevT);
        trail.moveTo(X(tTrail), Y(V[i - 1] + (V[i] - V[i - 1]) * u));
        trailPen = true;
      }
      add(T[i], V[i]);
    }
    // the head: the newest point itself, never a place between points
    const hp = ib === n - 1 && pen ? { x: lastX, y: lastY, v: V[ib] } : null;
    endRun();
    if (!any) return null;

    ctx.save();
    ctx.beginPath();
    ctx.rect(x0, 0, x1 - x0 + 12, yb + 2);
    ctx.clip();
    // a faint wash under each unbroken stretch
    const wash = ctx.createLinearGradient(0, y0, 0, yb);
    wash.addColorStop(0, `rgba(${rgb}, ${(0.13 * breath).toFixed(3)})`);
    wash.addColorStop(1, `rgba(${rgb}, 0)`);
    ctx.fillStyle = wash;
    ctx.fill(area);
    ctx.globalCompositeOperation = "lighter";
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    // glow, then the line; older readings sit a little dimmer
    const age = ctx.createLinearGradient(x0, 0, x1, 0);
    age.addColorStop(0, `rgba(${rgb}, ${(0.05 * breath).toFixed(3)})`);
    age.addColorStop(1, `rgba(${rgb}, ${(0.11 * breath).toFixed(3)})`);
    ctx.strokeStyle = age;
    ctx.lineWidth = 7;
    ctx.stroke(path);
    ctx.lineWidth = 3.2;
    ctx.stroke(path);
    const core = ctx.createLinearGradient(x0, 0, x1, 0);
    core.addColorStop(0, `rgba(${rgb}, 0.55)`);
    core.addColorStop(1, `rgba(${rgb}, 0.95)`);
    ctx.strokeStyle = core;
    ctx.lineWidth = 1.6;
    ctx.stroke(path);
    // persistence: the newest minute burns brighter and fades back
    const tx = X(tTrail);
    const hotG = ctx.createLinearGradient(tx, 0, x1, 0);
    hotG.addColorStop(0, `rgba(${hot}, 0)`);
    hotG.addColorStop(1, `rgba(${hot}, ${(0.32 * breath).toFixed(3)})`);
    ctx.strokeStyle = hotG;
    ctx.lineWidth = 6;
    ctx.stroke(trail);
    const hotC = ctx.createLinearGradient(tx, 0, x1, 0);
    hotC.addColorStop(0, `rgba(${hot}, 0)`);
    hotC.addColorStop(1, `rgba(${hot}, 0.75)`);
    ctx.strokeStyle = hotC;
    ctx.lineWidth = 1.8;
    ctx.stroke(trail);
    // a reading with no neighbours still shows, as a dot
    ctx.fillStyle = `rgba(${rgb}, 0.9)`;
    for (const [x, y] of single) { ctx.beginPath(); ctx.arc(x, y, 1.8, 0, TAU); ctx.fill(); }
    ctx.restore();
    ctx.globalCompositeOperation = "source-over";
    return hp;
  }

  // the node's own limits, dashed, when they are near the readings
  function drawLimits(ctx, x0, x1, ink) {
    const items = [];
    if (limits.showDown) items.push({ v: limits.down, rgb: rgbDown, text: `DOWN LIMIT ${kbps(limits.down)}` });
    if (limits.showUp) items.push({ v: limits.up, rgb: rgbUp, text: `UP LIMIT ${kbps(limits.up)}` });
    items.sort((a, b) => b.v - a.v);
    let lastY = -1e9;
    for (const it of items) {
      const y = Math.round(Y(it.v)) + 0.5;
      ctx.setLineDash([6, 5]);
      ctx.lineWidth = 1.2;
      hline(ctx, x0, x1, y, it.rgb, 0.75);
      ctx.setLineDash([]);
      ctx.font = AXIS_FONT;
      ctx.textAlign = "right";
      ctx.textBaseline = "bottom";
      const ty = y - lastY < 16 ? y + 13 : y - 3;
      text(ctx, it.text, x1 - 6, ty, `rgba(${ink}, 0.8)`, 1);
      lastY = y;
    }
  }

  // the highest reading each way in view, marked small, so the eye finds it
  function drawPeaks(ctx, x0, x1, y0, ink) {
    const marks = [];
    if (peaks.d && peaks.dt !== null) marks.push({ t: peaks.dt, v: peaks.d, rgb: rgbDown });
    if (peaks.u && peaks.ut !== null) marks.push({ t: peaks.ut, v: peaks.u, rgb: rgbUp });
    const boxes = [];
    ctx.font = `500 9px ${FONT}`;
    ctx.textBaseline = "bottom";
    ctx.textAlign = "center";
    for (const m of marks.sort((a, b) => b.v - a.v)) {
      const x = X(m.t);
      if (x < x0 + 50 || x > x1 - 70) continue;    // near the head the big readout already says it
      const y = Y(m.v);
      const label = `PEAK ${f.rate(m.v)}`;
      const half = ctx.measureText(label).width / 2 + 8;
      const cx = clamp(x, x0 + half, x1 - half);
      const box = [cx - half, y - 22, cx + half, y - 6];
      if (box[1] < y0 - 12 || boxes.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])) continue;
      boxes.push(box);
      ctx.strokeStyle = `rgba(${m.rgb}, 0.8)`;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x - 5, y - 5.5); ctx.lineTo(x, y - 2); ctx.lineTo(x + 5, y - 5.5); ctx.stroke();
      text(ctx, label, cx, y - 8, `rgba(${ink}, 0.62)`, 0.8);
    }
  }

  // the bright dots on the newest points, with their figures beside them
  function drawHeads(ctx, hd, hu, x1, y0, yb, ink, breath) {
    const heads = [];
    if (hd) heads.push({ p: hd, rgb: rgbDown, hot: hotDown, v: hd.v });
    if (hu) heads.push({ p: hu, rgb: rgbUp, hot: hotUp, v: hu.v });
    // a faint upright beam where "now" is drawn
    const beam = ctx.createLinearGradient(0, y0, 0, yb);
    beam.addColorStop(0, `rgba(${ink}, 0)`);
    beam.addColorStop(0.5, `rgba(${ink}, ${(0.07 * breath).toFixed(3)})`);
    beam.addColorStop(1, `rgba(${ink}, 0)`);
    ctx.fillStyle = beam;
    ctx.fillRect(x1 - 1, y0, 2, yb - y0);
    ctx.globalCompositeOperation = "lighter";
    for (const h of heads) {
      stamp(ctx, glowSprite(h.rgb, 48, 0.7), h.p.x, h.p.y, 44 * breath);
      stamp(ctx, glowSprite(h.hot, 16, 0.9), h.p.x, h.p.y, 12);
    }
    ctx.globalCompositeOperation = "source-over";
    for (const h of heads) {
      ctx.beginPath(); ctx.arc(h.p.x, h.p.y, 3.2, 0, TAU);
      ctx.fillStyle = `rgba(${h.hot}, 1)`; ctx.fill();
    }
    // labels: never on top of each other; a short leader when pushed off their dot, or
    // when the dot is more than a row back from the edge (no new readings for a while)
    const back = x1 - Math.max(2, step * geo.pps);
    const lab = heads.map((h) => ({ ...h, y: clamp(h.p.y, y0 + 4, yb - 4) }));
    if (lab.length === 2) {
      const [a, b] = lab[0].y <= lab[1].y ? [lab[0], lab[1]] : [lab[1], lab[0]];
      if (b.y - a.y < 16) {
        const mid = (a.y + b.y) / 2;
        a.y = mid - 8; b.y = mid + 8;
        if (a.y < y0 + 4) { a.y = y0 + 4; b.y = a.y + 16; }
        if (b.y > yb - 4) { b.y = yb - 4; a.y = b.y - 16; }
      }
    }
    ctx.font = HEAD_FONT;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    for (const h of lab) {
      const lx = x1 + 11;
      if (Math.abs(h.y - h.p.y) > 3 || h.p.x < back) {
        ctx.strokeStyle = `rgba(${h.rgb}, 0.6)`;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(h.p.x + 5, h.p.y); ctx.lineTo(lx - 3, h.y); ctx.stroke();
      }
      text(ctx, f.rate(h.v), lx, h.y + 0.5, `rgba(${ink}, 0.96)`, HEAD_GAP);
    }
  }

  // where the crosshair is this frame: the reading, its x, and the time box on the
  // floor (the axis labels make way for the box). One object, reused every frame.
  const spot = { on: false, i: -1, x: 0, label: "", bx: 0, wd: 0 };
  function findSpot(ctx, x0, x1) {
    spot.on = false;
    if (!hover || hover.gap || head === null) return;
    const i = nearest(hover.t);
    if (i < 0) return;
    const x = Math.round(X(T[i])) + 0.5;
    if (x < x0 - 1 || x > x1 + 1) return;
    ctx.font = AXIS_FONT;
    spot.label = shortClock(T[i], true);
    spot.wd = ctx.measureText(spot.label).width + 12;
    spot.bx = clamp(x - spot.wd / 2, x0, x1 + 40 - spot.wd);
    spot.i = i; spot.x = x; spot.on = true;
  }

  // the crosshair: a hairline at the reading pointed at, a ring on each line, its time
  // on the floor (pointing into a gap: a faint hairline only, there is no reading to ring)
  function drawHover(ctx, x0, x1, y0, yb, ink) {
    if (hover && hover.gap) {
      const xg = Math.round(X(hover.t)) + 0.5;
      if (xg >= x0 && xg <= x1) vline(ctx, xg, y0 - 4, yb, ink, 0.22);
      return;
    }
    if (!spot.on) return;
    const { i, x, label, bx, wd } = spot;
    vline(ctx, x, y0 - 4, yb, ink, 0.5);
    for (let k = 0; k < 2; k++) {
      const v = k === 0 ? D[i] : U[i];
      if (v === null) continue;
      const y = Y(v);
      ctx.beginPath(); ctx.arc(x, y, 6, 0, TAU); ctx.fillStyle = "#000"; ctx.fill();
      ctx.beginPath(); ctx.arc(x, y, 4, 0, TAU); ctx.fillStyle = `rgb(${k === 0 ? rgbDown : rgbUp})`; ctx.fill();
    }
    ctx.font = AXIS_FONT;
    ctx.fillStyle = "#000";
    ctx.fillRect(bx, yb + 3, wd, 16);
    ctx.strokeStyle = `rgba(${ink}, 0.35)`;
    ctx.lineWidth = 1;
    ctx.strokeRect(Math.round(bx) + 0.5, yb + 3.5, Math.round(wd), 15);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    text(ctx, label, bx + wd / 2, yb + 11.5, `rgba(${ink}, 0.95)`, 0.4, false);
  }

  // value labels on the left, clock times under the five-minute lines, NOW at the edge;
  // a floor label that would touch the crosshair's time box is left out while it shows
  const underBox = (l, r) => spot.on && l < spot.bx + spot.wd + 4 && r > spot.bx - 4;
  function drawAxisText(ctx, x0, x1, y0, yb, ink) {
    ctx.font = AXIS_FONT;
    ctx.textBaseline = "bottom";
    ctx.textAlign = "left";
    for (const g of grids) {
      if (g.a < 0.05) continue;
      for (let k = 1; k * g.step <= scale * 1.0001; k++) {
        const y = Y(k * g.step);
        if (y < y0 + 8) continue;
        text(ctx, f.rate(k * g.step), x0 + 6, y - 2, `rgba(${ink}, ${(0.5 * g.a).toFixed(3)})`, 0.6);
      }
    }
    // a clock time is left out wherever it would touch "15 MIN AGO" or "NOW"
    ctx.textBaseline = "top";
    const agoRight = x0 + 2 + textW(ctx, "15 MIN AGO", 0.8);
    const nowLeft = x1 + 3 - textW(ctx, "NOW", 1.2);
    ctx.textAlign = "center";
    if (head !== null) {
      for (let m = Math.ceil((head - WINDOW) / 300) * 300; m <= head; m += 300) {
        const x = X(m);
        const s = shortClock(m, false);
        const half = textW(ctx, s, 0.8) / 2;
        if (x - half < agoRight + 8 || x + half > nowLeft - 8 || underBox(x - half, x + half)) continue;
        text(ctx, s, x, yb + 7, `rgba(${ink}, 0.42)`, 0.8);
      }
    }
    ctx.textAlign = "right";
    if (!underBox(nowLeft, x1 + 3)) text(ctx, "NOW", x1 + 3, yb + 7, `rgba(${ink}, 0.6)`, 1.2);
    ctx.textAlign = "left";
    if (!underBox(x0 + 2, agoRight)) text(ctx, "15 MIN AGO", x0 + 2, yb + 7, `rgba(${ink}, 0.32)`, 0.8);
  }

  // letter-spaced text with a black edge so it reads over lines
  function text(ctx, s, x, y, fill, gap = 0, edge = true) {
    const sp = "letterSpacing" in ctx;
    if (sp) ctx.letterSpacing = `${gap}px`;
    const dx = sp && ctx.textAlign === "center" ? gap / 2 : 0;
    if (edge) {
      ctx.strokeStyle = "rgba(0, 0, 0, 0.9)";
      ctx.lineWidth = 3;
      ctx.lineJoin = "round";
      ctx.strokeText(s, x + dx, y);
    }
    ctx.fillStyle = fill;
    ctx.fillText(s, x + dx, y);
    if (sp) ctx.letterSpacing = "0px";
  }

  // ---- the flow strips: dots run toward your node (in) or away from it (out)
  // (its own stage at 30 frames a second: the dots move quickly, the scope doesn't)
  const lanes = [
    { dir: "in", key: "d", dots: [], speed: 0, alpha: 0, gap: 0, next: 20, filled: false },
    { dir: "out", key: "u", dots: [], speed: 0, alpha: 0, gap: 0, next: 20, filled: false },
  ];
  const flowStage = createStage(flowCanvas, { draw: drawFlow, maxFps: 30, onResize: () => { for (const l of lanes) l.filled = false; } });

  // pixels a second for a rate: any traffic moves, more moves faster (square root, so
  // a busy node doesn't blur); none stands still
  const speedOf = (bps) => (bps > 0 ? Math.min(240, 9 + 5 * Math.sqrt(bps / 1000)) : 0);
  // a steady spread of spacings (decoration: where dots start, not what they carry)
  let seed = 7;
  const rnd = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };

  function drawFlow(ctx, w, h, t, dt) {
    ctx.clearRect(0, 0, w, h);
    if (w < 20) return;
    const go = moving(look);
    const glowOn = look.glow !== false;
    const acc = look.accent || "255, 184, 64";
    const nx = w - 20, ny = h / 2;              // your node, at the right
    const xa = 4, xb = nx - 16;                 // the lanes run between the other nodes and yours
    const len = xb - xa;
    for (let li = 0; li < lanes.length; li++) {
      const l = lanes[li];
      const rgb = l.key === "d" ? rgbDown : rgbUp;
      const hot = l.key === "d" ? hotDown : hotUp;
      const y = li === 0 ? ny - 9 : ny + 9;
      const v = flow[l.key];
      const want = speedOf(v);
      l.speed = go ? l.speed + (want - l.speed) * Math.min(1, dt * 1.2) : want;
      const aim = v === null ? 0 : v > 0 ? 1 : 0.25;
      l.alpha = go ? l.alpha + (aim - l.alpha) * Math.min(1, dt * 1.5) : aim;
      // the lane: a hairline with a faint glow
      ctx.lineWidth = 1;
      ctx.strokeStyle = `rgba(${rgb}, 0.22)`;
      ctx.beginPath(); ctx.moveTo(xa, y + 0.5); ctx.lineTo(xb, y + 0.5); ctx.stroke();
      // where it meets your node: a short bend into the node
      ctx.beginPath(); ctx.moveTo(xb, y + 0.5); ctx.lineTo(nx - 7, ny + (y < ny ? -3 : 3)); ctx.stroke();
      // the arrowhead at the far end says which way it runs, moving or still
      const ax = l.dir === "in" ? xb - 4 : xa + 4;
      const s = l.dir === "in" ? 1 : -1;
      ctx.strokeStyle = `rgba(${rgb}, ${0.35 + 0.45 * l.alpha})`;
      ctx.beginPath(); ctx.moveTo(ax - 4 * s, y - 3.5); ctx.lineTo(ax, y + 0.5); ctx.lineTo(ax - 4 * s, y + 4.5); ctx.stroke();

      if (!go) {
        // still: evenly spaced dots along the lane (no speed to show)
        l.dots.length = 0;
        l.filled = false;
        if (v === null || v <= 0) continue;
        ctx.globalCompositeOperation = "lighter";
        for (let x = xa + 14; x < xb - 12; x += 22) {
          stamp(ctx, glowSprite(rgb, 12, 0.8), x, y + 0.5, 11);
          ctx.fillStyle = `rgba(${hot}, 0.85)`;
          ctx.fillRect(x - 1, y - 0.5, 2, 2);
        }
        ctx.globalCompositeOperation = "source-over";
        continue;
      }
      // moving: fill the lane once, then add a dot each time the last one has moved on
      if (!l.filled) {
        l.dots.length = 0;
        for (let p = rnd() * 0.06; p < 1; p += (16 + rnd() * 22) / len) l.dots.push({ p, z: 0.7 + rnd() * 0.6 });
        l.filled = true;
        l.gap = 0;
      }
      const dp = (l.speed * dt) / len;
      l.gap += l.speed * dt;
      if (l.gap >= l.next && l.alpha > 0.02) {
        l.dots.push({ p: 0, z: 0.7 + rnd() * 0.6 });
        l.gap = 0;
        l.next = 16 + rnd() * 22;
      }
      ctx.globalCompositeOperation = "lighter";
      const tail = clamp(l.speed * 0.09, 1.5, 16);
      let kept = 0;
      for (const d of l.dots) {
        d.p += dp;
        if (d.p >= 1) continue;          // reached the end of the lane: gone
        l.dots[kept++] = d;
        const x = l.dir === "in" ? xa + d.p * len : xb - d.p * len;
        const edge = Math.min(1, d.p * 8, (1 - d.p) * 8);
        const a = edge * l.alpha;
        if (a < 0.02) continue;
        ctx.globalAlpha = a;
        stamp(ctx, glowSprite(rgb, 12, 0.8), x, y + 0.5, 12 * d.z);
        ctx.strokeStyle = `rgba(${rgb}, 0.55)`;
        ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.moveTo(x, y + 0.5); ctx.lineTo(x - s * tail, y + 0.5); ctx.stroke();
        ctx.fillStyle = `rgba(${hot}, 0.95)`;
        ctx.fillRect(x - 1, y - 0.5, 2, 2);
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
      l.dots.length = kept;
    }
    // your node: a ring round a bright centre, in the accent, like the top of the node tree
    // (it breathes slowly; nothing here pretends a dot "arrived")
    const br = go && glowOn ? 0.5 + 0.5 * Math.sin((t * TAU) / 7) : 0.6;
    ctx.globalCompositeOperation = "lighter";
    stamp(ctx, glowSprite(acc, 40, glowOn ? 0.55 : 0.3), nx, ny, 30 + 10 * br);
    ctx.globalCompositeOperation = "source-over";
    ctx.lineWidth = 1.3;
    ctx.strokeStyle = `rgba(${acc}, 0.9)`;
    ctx.beginPath(); ctx.arc(nx, ny, 7, 0, TAU); ctx.stroke();
    ctx.beginPath(); ctx.arc(nx, ny, 3, 0, TAU); ctx.fillStyle = `rgba(${acc}, 1)`; ctx.fill();
  }

  // ---- pointing: hover, tap or the arrow keys put a crosshair on a reading
  function nearest(tp) {
    if (!T.length) return -1;
    let lo = 0, hi = T.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (T[m] <= tp) lo = m; else hi = m; }
    return Math.abs(T[lo] - tp) <= Math.abs(T[hi] - tp) ? lo : hi;
  }
  function firstInView() {
    const tl = geo.head - WINDOW;
    let i = 0;
    while (i < T.length - 1 && T[i] < tl) i++;
    return i;
  }
  const NO_READING = "No reading then: your node wasn't answering, or the dashboard wasn't running.";
  // "LATEST" only for the newest point; any other says how long ago it was
  function tipFor(i) {
    const t = T[i];
    const ago = `${f.dur(Math.max(0, env.now() - t)).toUpperCase()} AGO`;
    const head = `${f.clock(t)} · ${i === T.length - 1 ? (env.now() - t >= 30 ? `LATEST · ${ago}` : "LATEST") : ago}`;
    if (D[i] === null && U[i] === null) return { head, rows: [{ note: NO_READING }] };
    return { head, rows: [
      { label: "DOWN", value: f.rate(D[i]), key: "s1" },
      { label: "UP", value: f.rate(U[i]), key: "s2" },
    ] };
  }
  // quiet: a new snapshot refreshing the crosshair (the screen reader hears only key presses)
  function setHover(i, how, cx, cy, quiet) {
    if (i < 0 || i >= T.length) return;
    hover = { t: T[i], how, cx, cy };
    const tip = tipFor(i);
    if (cx === undefined) {
      const r = canvas.getBoundingClientRect();
      const ys = [D[i], U[i]].filter((v) => v !== null).map((v) => Y(v));
      cx = r.left + X(T[i]);
      cy = r.top + (ys.length ? Math.min(...ys) : geo.y0 + 20);
    }
    env.tip.showTip(cx, cy, tip.head, tip.rows);
    if (how === "key" && !quiet) say.textContent = `${tip.head}: ${tip.rows.map((r) => r.note || `${r.label} ${r.value}`).join(", ")}`;
    if (!stage.running) stage.frame();
  }
  // (also lets go of the "no reading then" note, which is a hover too)
  function clearHover() {
    if (!hover) return;
    hover = null;
    env.tip.hideTip();
    if (shown && !stage.running) stage.frame();
  }
  function pointAt(e) {
    const r = canvas.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    if (!T.length || head === null || x < geo.x0 - 4 || x > geo.x1 + 8 || y < geo.y0 - 12 || y > geo.yb + 22) { clearHover(); return; }
    const tp = geo.head - (geo.x1 - clamp(x, geo.x0, geo.x1)) / geo.pps;
    const i = nearest(tp);
    if (i < 0) return;
    const how = e.pointerType === "mouse" ? "mouse" : "touch";
    if (Math.abs(T[i] - tp) > step * 1.6) {
      // pointing into a gap: say there was no reading then (kept as a hover, so
      // leaving, tapping elsewhere or changing screens takes the note away)
      hover = { t: tp, how, gap: true };
      env.tip.showTip(e.clientX, e.clientY, f.clock(tp), [{ note: NO_READING }]);
      if (!stage.running) stage.frame();
      return;
    }
    setHover(i, how, e.clientX, e.clientY);
  }
  canvas.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse" && !touching) return;
    pointAt(e);
  });
  canvas.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse") return;
    touching = true;
    pointAt(e);
  });
  const lift = () => { touching = false; };
  canvas.addEventListener("pointerup", lift);
  canvas.addEventListener("pointercancel", lift);
  canvas.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") clearHover(); });
  // a tap anywhere else lets go of the crosshair (phones have no "leave")
  document.addEventListener("pointerdown", (e) => { if (hover && hover.how !== "mouse" && e.target !== canvas) clearHover(); }, true);
  canvas.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || !T.length) return;
    const last = T.length - 1;
    let i = hover ? nearest(hover.t) : last;
    const lo = firstInView();
    // with Shift, a minute on by the clock (rows aren't always evenly spaced), and at least one row
    const by = (dir) => {
      if (!e.shiftKey) return i + dir;
      const j = nearest(T[i] + dir * 60);
      return j === i ? i + dir : j;
    };
    if (e.key === "ArrowLeft") i = hover ? by(-1) : last;
    else if (e.key === "ArrowRight") i = hover ? by(1) : last;
    else if (e.key === "Home") i = lo;
    else if (e.key === "End") i = last;
    else if (e.key === "Escape") { clearHover(); return; }
    else return;
    e.preventDefault();
    setHover(clamp(i, lo, last), "key");
  });
  canvas.addEventListener("focus", () => {
    if (!hover && canvas.matches(":focus-visible") && T.length) setHover(T.length - 1, "key");
  });
  canvas.addEventListener("blur", () => { if (hover && hover.how === "key") clearHover(); });

  askHistory();

  return {
    update(snap) {
      lastSnap = snap;
      poll = Math.max(1, Number(snap.source?.poll_s) || 5);
      // the node is now read at another pace, so the server's history comes in rows of another length
      if (hist && hist.step !== Math.max(5, Math.ceil(poll))) askHistory();
      const added = addReading(snap);
      if (!added) measure();
      readouts(snap);
      // the crosshair stays on its row; its numbers (and the newest row's time) may have moved on
      if (hover && !hover.gap) {
        const i = nearest(hover.t);
        if (i >= 0) setHover(i, hover.how, hover.cx, hover.cy, true);
      }
      if (shown && !stage.running) stage.frame();
      if (shown && !flowStage.running) flowStage.frame();
    },
    show() {
      shown = true;
      // a stale picture (the window was hidden or the computer slept): fill the hole from the server's history
      if (T.length && env.now() - T[T.length - 1] > step * 3) askHistory();
      scale = scaleWant;
      measureHud();
      if (moving(look)) { stage.start(); flowStage.start(); } else { stage.frame(); flowStage.frame(); }
    },
    hide() {
      shown = false;
      stage.stop();
      flowStage.stop();
      if (hover) { hover = null; env.tip.hideTip(); }
    },
    look(lk) {
      look = lk;
      colours();
      for (const l of lanes) l.filled = false;
      words();
      if (!shown) return;
      if (moving(lk)) { stage.start(); flowStage.start(); }
      else { stage.stop(); flowStage.stop(); stage.frame(); flowStage.frame(); }
    },
  };
}
