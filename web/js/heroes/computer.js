// COMPUTER screen hero: a row of round instrument gauges for the computer that
// runs the node and for the monerod process: CPU temperature, monerod's CPU
// use, memory in use, how busy the drive holding the chain is, and the free
// space on that disk.
//
// Every gauge is a 270° dial: a tick scale, a glowing arc filled up to the
// reading, and a thin needle on a pivot that eases to each new reading (it
// never wobbles on its own). The number sits big in the middle with its unit
// under it. A reading that isn't there leaves the dial dim with "—" and NO
// READING, never 0. The CPU temperature dial marks the same heads-up and
// problem temperatures as panel J; memory and the chain drive mark 90 % as
// panel J does, and disk free marks the disk-space alert limits. A dial turns
// to the heads-up or problem colour, with ▲ / ■ and a word, only while its
// reading is in those marks.
//
// Under each dial a small line shows the last 15 minutes: CPU temperature and
// monerod CPU come from the server's history (/api/history); memory, drive and
// disk free aren't kept there, so their lines start when this page opened.
// The soft light behind each dial breathes slowly in full motion; that is
// decoration, not a reading.
import { el, svg, clear, setText, setData, setAttr } from "../dom.js";
import * as f from "../fmt.js";
import { HOT, throttleText, remoteWhy } from "../panels/computer.js";   // °C: the CPU temperature marks
import { api } from "../api.js";
import { addReading, meanOf } from "../rows.js";
import { createStage, glowSprite, stamp, moving, clamp, cssRGB, TAU } from "../fx/space.js";

const WINDOW = 900;                   // seconds of history under each dial
const FULLISH = 90;                   // %: memory and chain drive get a heads-up from here, as in panel J
const START = 0.75 * Math.PI;         // where a scale starts: lower left (canvas angles run clockwise)
const SWEEP = 1.5 * Math.PI;          // 270°
const EASE_S = 0.8;                   // a needle's travel time to a new reading (under a second, so it rests between them)
const STEP = 5;                       // seconds a point of the lines under the dials covers, as in the server's history
const FLASH_GAP_MS = 10000;           // a dial's number lights up for a new reading at most this often
const WAKE_S = 1.8;                   // its first sweep up from the stop
const SW = 200, SH = 40;              // sparkline drawing box (stretched to fit)

const KEYS = ["temp", "cpu", "mem", "busy", "free"];
const LABEL = { temp: "CPU TEMP", cpu: "MONEROD CPU", mem: "MEMORY", busy: "CHAIN DRIVE BUSY", free: "DISK FREE" };
// which lines the server's history has; the others are collected here
const FROM_HISTORY = { temp: "cpu_temp_c", cpu: "node_cpu_pct" };

const easeIO = (u) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2);
const isNum = f.isNum;
// "6.8 GB" -> ["6.8", "GB"]
function split(s) {
  const i = String(s).lastIndexOf(" ");
  return i < 0 ? [String(s), ""] : [s.slice(0, i), s.slice(i + 1)];
}
// a heads-up or problem in words, for screen readers: ", problem: very hot"
const say = (level, word) => (level === "bad" ? `, problem: ${word}` : level ? `, heads-up: ${word}` : "");
// a short percent figure for the middle of a dial: 7.8, 42, 100
const shortPct = (v) => (v < 99.95 ? v.toFixed(1).replace(/\.0$/, "") : String(Math.round(v)));

// tick marks from min to max: majors every `major`, minors every `minor`
function ticks(min, max, major, minor) {
  const out = [];
  const n = Math.round((max - min) / minor);
  for (let i = 0; i <= n; i++) {
    const v = min + i * minor;
    const r = (v - min) / major;
    out.push({ v, major: Math.abs(r - Math.round(r)) < 1e-6 });
  }
  return out;
}

export default function create(body, env) {
  let look = env.look();
  let shown = false;
  let lastSnap = null;
  let disk = { warn: 20, bad: 5 };     // GB, the disk-space alert limits (read from the settings)
  let col = {};                         // canvas colours, from the look
  // the last 15 minutes, raw units: this page's own readings, and the server's
  // history for the lines it keeps (temperature, monerod CPU)
  const live = { t: [], temp: [], cpu: [], mem: [], busy: [], free: [] };
  let acc = null;                       // the point of `live` still filling: {t, sum, n}
  let pushedT = null;                   // the last reading put in
  const recent = [];                    // the last minute of computer readings, for the rates
  let hist = null;
  let tipKey = null;                    // the gauge whose tooltip is open
  let tipAt = null;                     // where it was opened

  // ---- DOM
  const row = el("div", { class: "cg-row" });
  const foot = el("div", { class: "cg-foot" });
  body.append(el("div", { class: "cg" }, row, foot));

  const gauges = KEYS.map((key, i) => makeGauge(key, i));
  for (const g of gauges) row.append(g.cell);

  function makeGauge(key, i) {
    const canvas = el("canvas", { class: "cg-canvas", role: "img", "aria-label": `${LABEL[key]}: no reading yet` });
    const num = el("b", { class: "cg-num", text: "—" });
    const unit = el("span", { class: "cg-unit", text: "NO READING" });
    const lo = el("span", { class: "cg-end cg-end-lo", "aria-hidden": "true" });
    const hi = el("span", { class: "cg-end cg-end-hi", "aria-hidden": "true" });
    const dial = el("div", { class: "cg-dial" },
      el("div", { class: "cg-glow", "aria-hidden": "true" }), canvas,
      el("div", { class: "cg-read" }, num, unit), lo, hi);
    const label = el("div", { class: "cg-label", text: LABEL[key] });
    const state = el("div", { class: "cg-state", text: "—" });
    const gradId = `cg-grad-${key}`;
    const area = svg("path", { class: "cg-sp-area", d: "", fill: `url(#${gradId})` });
    const line = svg("path", { class: "cg-sp-line", d: "" });
    const base = svg("line", { class: "cg-sp-base", x1: 0, x2: SW, y1: SH - 1, y2: SH - 1 });
    const spark = svg("svg", { class: "cg-sp-svg", viewBox: `0 0 ${SW} ${SH}`, preserveAspectRatio: "none", "aria-hidden": "true" },
      svg("defs", {}, svg("linearGradient", { id: gradId, x1: 0, x2: 0, y1: 0, y2: 1 },
        svg("stop", { class: "cg-stop-a", offset: "0" }), svg("stop", { class: "cg-stop-b", offset: "1" }))),
      base, area, line);
    const dot = el("span", { class: "cg-sp-dot", hidden: true });
    const capL = el("span", { text: "LAST 15 MIN" });
    const capR = el("span", { class: "cg-sp-range" });
    const sparkBox = el("div", { class: "cg-sp" }, el("div", { class: "cg-sp-plot" }, spark, dot), el("div", { class: "cg-sp-cap" }, capL, capR));
    const cell = el("div", { class: "cg-g", tabindex: "0", role: "group", "aria-label": LABEL[key], "data-key": key, "data-none": "" },
      dial, label, state, sparkBox);
    const g = {
      key, i, cell, canvas, num, unit, lo, hi, state, area, line, dot, capL, capR, base,
      r: null,                 // the current reading (see readings())
      cur: null, anim: null, woke: false, dirty: true,
    };
    g.stage = createStage(canvas, { draw: (ctx, w, h) => drawGauge(g, ctx, w, h), maxFps: 30 });
    env.tip.attachTip(cell, () => tipOf(g));
    cell.addEventListener("pointermove", (e) => { tipKey = key; tipAt = { x: e.clientX, y: e.clientY }; });
    cell.addEventListener("pointerleave", () => { if (tipKey === key) tipKey = null; });
    cell.addEventListener("focus", () => { tipKey = key; tipAt = null; });
    cell.addEventListener("blur", () => { if (tipKey === key) tipKey = null; });
    cell.addEventListener("keydown", (e) => { if (e.key === "Escape") { env.tip.hideTip(); tipKey = null; } });
    return g;
  }

  // ---- readings: what each dial shows, from one snapshot
  function readings(snap) {
    const h = snap.host;
    const src = snap.source || {};
    const node = snap.node || {};
    const off = !h ? "COMPUTER READS ARE OFF" : h.remote ? "NODE ON ANOTHER COMPUTER"
      : h.ok === false ? "CAN'T READ THIS COMPUTER" : null;
    const L = (!off && h.computer) || {};
    const p = !off ? h.proc : null;
    const out = {};

    // CPU temperature, in the reader's unit
    {
      const c = L.cpu_temp_c;
      const F = f.tempUnit() === "°F";
      const sc = F ? { min: 60, max: 240, major: 20, minor: 10 } : { min: 20, max: 110, major: 10, minor: 5 };
      const r = { ...sc, lo: String(sc.min), hi: `${sc.max}°`, zones: [
        { from: f.tempValue(HOT.warn), to: f.tempValue(HOT.bad), level: "warn" },
        { from: f.tempValue(HOT.bad), to: sc.max, level: "bad" }] };
      if (isNum(c)) {
        const level = c >= HOT.bad ? "bad" : c >= HOT.warn ? "warn" : null;
        Object.assign(r, { has: true, v: f.tempValue(c), num: String(Math.round(f.tempValue(c))), unit: f.tempUnit(), level,
          say: say(level, level === "bad" ? "very hot" : "hot"),
          state: f.flag(level, `${level === "bad" ? "VERY HOT · " : level ? "HOT · " : ""}HOTTEST SENSOR`) });
      } else Object.assign(r, { has: false, state: off || "NO SENSOR FOUND", why: off ? offWhy(h) : "This computer has no temperature sensor the dashboard can read." });
      r.aria = r.has ? `CPU temperature ${f.temp(c)}${r.say}; heads-up from ${f.temp(HOT.warn)}, problem from ${f.temp(HOT.bad)}` : "CPU temperature: no reading";
      out.temp = r;
    }

    // monerod's CPU use, as a share of one core; the dial goes up to every core
    {
      const cores = isNum(L.cores) && L.cores > 0 ? L.cores : null;
      const max = cores ? cores * 100 : 100;
      const major = !cores ? 10 : cores <= 2 ? 50 : 100 * Math.ceil(cores / 10);
      const minor = !cores ? 5 : cores <= 2 ? 10 : major / (cores <= 4 ? 4 : 2);
      const r = { min: 0, max, major, minor, lo: "0", hi: `${f.num(max)}%`, zones: [], cores };
      // a second's CPU use jumps about with every block: the dial shows the last 5 seconds
      const v = p ? meanOf(recent, "cpu") : null;
      if (isNum(v)) {
        Object.assign(r, { has: true, v, num: shortPct(v), unit: "%", level: null,
          state: cores ? `OF ONE CORE · ${f.plural(cores, "CORE", "CORES")}` : "OF ONE CORE" });
      } else {
        const why = off ? offWhy(h) : !p ? (h.proc_error || (src.answering === false ? `Nothing is answering on ${src.rpc || "the RPC port"}.` : `No monerod found behind ${src.rpc || "the RPC port"}.`))
          : "monerod's CPU use needs two reads in a row; it shows after the next one.";
        Object.assign(r, { has: false, why, state: off || (!p ? (src.answering === false ? "NODE NOT ANSWERING" : "MONEROD NOT FOUND") : "OF ONE CORE") });
      }
      r.aria = r.has ? `monerod CPU ${f.pct(v)} of one core${cores ? `, dial up to ${cores} cores (${max}%)` : ""}` : "monerod CPU: no reading";
      out.cpu = r;
    }

    // memory in use, of all of it
    {
      const used = L.mem_used_bytes, tot = L.mem_total_bytes;
      const r = { min: 0, max: 100, major: 10, minor: 5, lo: "0", hi: "100%", zones: [{ from: FULLISH, to: 100, level: "warn" }] };
      if (isNum(used) && isNum(tot) && tot > 0) {
        const pc = (used / tot) * 100;
        const level = pc >= FULLISH ? "warn" : null;
        const [n, u] = split(f.gb(used));
        Object.assign(r, { has: true, v: pc, num: n, unit: u, level, used, tot, say: say(level, "nearly full"),
          state: f.flag(level, level ? `NEARLY FULL · ${Math.round(pc)}%` : `OF ${f.gb(tot)} · ${Math.round(pc)}%`) });
      } else Object.assign(r, { has: false, state: off || "NOT READ", why: off ? offWhy(h) : "The dashboard couldn't read how much memory is in use." });
      r.aria = r.has ? `memory ${f.gb(used)} in use of ${f.gb(tot)}, ${Math.round(r.v)}%${r.say}` : "memory: no reading";
      out.mem = r;
    }

    // how busy the drive holding the chain is
    {
      const d = L.disk || null;
      const v = d ? meanOf(recent, "busy") : null;
      const r = { min: 0, max: 100, major: 10, minor: 5, lo: "0", hi: "100%", zones: [{ from: FULLISH, to: 100, level: "warn" }], d };
      const dev = d && d.device ? String(d.device).toUpperCase() : null;
      if (isNum(v)) {
        const level = v >= FULLISH ? "warn" : null;
        Object.assign(r, { has: true, v, num: shortPct(v), unit: "%", level, say: say(level, "very busy"),
          state: f.flag(level, `${level ? "VERY BUSY · " : ""}${dev ? "DRIVE " + dev : "OF THE TIME"}`) });
      } else Object.assign(r, { has: false, state: off || (d ? (dev ? "DRIVE " + dev : "NOT READ") : "DRIVE NOT FOUND"),
        why: off ? offWhy(h) : d ? "The drive was found but its activity isn't known yet (it needs two reads in a row)." : "The drive holding the blockchain wasn't found." });
      r.aria = r.has ? `chain drive busy ${f.pct(v)} of the time${r.say}` : "chain drive busy: no reading";
      out.busy = r;
    }

    // free space on the chain's disk, as a share of free space plus the chain
    {
      const free = node.free_bytes, db = node.db_bytes;
      // the face says what its 100% is (free space plus the chain, not the whole disk);
      // without the chain's size the scale is just the free space
      const r = { min: 0, max: 100, major: 10, minor: 5, lo: "0", hi: isNum(free) && !isNum(db) ? "100%" : "FREE+CHAIN", zones: [], free, db };
      if (isNum(free)) {
        const total = free + (isNum(db) ? db : 0);
        const pc = total > 0 ? (free / total) * 100 : 0;
        const level = free < disk.bad * 1e9 ? "bad" : free < disk.warn * 1e9 ? "warn" : null;
        if (total > 0) {
          const w = clamp((disk.warn * 1e9 / total) * 100, 0, 100), b = clamp((disk.bad * 1e9 / total) * 100, 0, 100);
          r.zones = [{ from: 0, to: b, level: "bad" }, { from: b, to: w, level: "warn" }];
        }
        const [n, u] = split(f.bytes(free));
        Object.assign(r, { has: true, v: pc, num: n, unit: u, level, total, say: say(level, "low"),
          state: f.flag(level, `${level ? "LOW · " : ""}${isNum(db) ? "CHAIN TAKES " + f.bytes(db) : Math.round(pc) + "% FREE"}`) });
      } else {
        const why = src.answering === false ? "Your node isn't answering." : src.restricted ? "This RPC port is restricted, and a restricted port doesn't say how much disk is free." : "Your node didn't say how much disk is free.";
        Object.assign(r, { has: false, why, state: src.answering === false ? "NODE NOT ANSWERING" : src.restricted ? "RESTRICTED PORT" : "NOT REPORTED" });
      }
      r.aria = r.has ? `disk free ${f.bytes(free)}${isNum(db) ? `, the blockchain takes ${f.bytes(db)}` : ""}${r.say}` : "disk free: no reading";
      out.free = r;
    }
    return out;
  }

  function offWhy(h) {
    return !h ? "Computer reads are off. Turn them on in Settings → Computer reads."
      : h.remote ? String(h.error || "Your node runs on another computer, so this computer's readings would say nothing about it.")
        : `The dashboard can't read this computer: ${h.error || "unknown reason"}.`;
  }

  // ---- tooltips: the exact numbers
  function tipOf(g) {
    const r = g.r;
    if (!r) return { head: LABEL[g.key], rows: [{ note: "No reading yet." }] };
    const rows = [];
    const add = (label, value) => rows.push({ label, value });
    const st = stats(g.key);
    const snap = lastSnap || {};
    const p = snap.host && snap.host.ok !== false ? snap.host.proc : null;
    if (g.key === "temp") {
      if (r.has) {
        const c = snap.host.computer.cpu_temp_c;
        add("NOW", f.tempUnit() === "°F" ? `${f.tempValue(c).toFixed(1)} °F · ${c.toFixed(1)} °C` : `${c.toFixed(1)} °C`);
      }
      if (st) add("15 MIN LOW · HIGH", `${Math.round(f.tempValue(st.lo))} · ${f.temp(st.hi)}`);
      add("HEADS-UP ▲ FROM", f.temp(HOT.warn));
      add("PROBLEM ■ FROM", f.temp(HOT.bad));
      rows.push({ note: r.has ? `The hottest of this computer's temperature sensors. Most processors slow themselves down to cool off at about ${f.temp(95)} to ${f.temp(105)}.` : r.why });
    } else if (g.key === "cpu") {
      if (r.has) {
        add(overFive() ? "LAST 5 S" : "NOW", `${r.v.toFixed(1)}% OF ONE CORE`);
        if (r.cores) add("OF THE WHOLE COMPUTER", `${(r.v / r.cores).toFixed(1)}% OF ${f.plural(r.cores, "CORE", "CORES")}`);
      }
      if (st) add("15 MIN AVERAGE · PEAK", `${f.pct(st.avg)} · ${f.pct(st.hi)}`);
      if (p) add("PROCESS", `PID ${p.pid}${isNum(p.threads) ? ` · ${f.plural(p.threads, "THREAD", "THREADS")}` : ""}`);
      rows.push({ note: r.has ? `100% is one processor core kept busy all the time; the dial goes up to ${r.cores ? `all ${f.plural(r.cores, "core", "cores")} (${f.num(r.max)}%)` : "100% (the number of cores isn't known)"}.` : r.why });
    } else if (g.key === "mem") {
      if (r.has) {
        add("IN USE", `${(r.used / 1e9).toFixed(2)} GB · ${r.v.toFixed(1)}%`);
        add("AVAILABLE", `${((r.tot - r.used) / 1e9).toFixed(2)} GB`);
        add("TOTAL", `${(r.tot / 1e9).toFixed(2)} GB`);
      }
      if (p && isNum(p.rss_bytes)) add("MONEROD HOLDS", f.bytes(p.rss_bytes));
      if (st) add("15 MIN LOW · HIGH", `${f.gb(st.lo)} · ${f.gb(st.hi)}`);
      add("HEADS-UP ▲ FROM", `${FULLISH}% IN USE`);
      rows.push({ note: r.has ? "What programs hold; the rest is free or a cache the computer can hand back. The line under the dial starts when this page opened." : r.why });
    } else if (g.key === "busy") {
      const d = r.d;
      const five = overFive() ? " · LAST 5 S" : "";
      if (r.has) add("BUSY" + five, `${r.v.toFixed(1)}% OF THE TIME`);
      if (d && d.device) add("DRIVE", String(d.device).toUpperCase());
      const dr = d ? meanOf(recent, "dread") : null, pw = p ? meanOf(recent, "write") : null;
      if (isNum(dr)) add("READING · WRITING" + five, `${f.rate(dr)} · ${f.rate(meanOf(recent, "dwrite"))}`);
      if (isNum(pw)) add("MONEROD WRITES" + five, f.rate(pw));
      add("HEADS-UP ▲ FROM", `${FULLISH}% BUSY`);
      rows.push({ note: r.has ? "How much of the time the drive holding the blockchain was working. The line under the dial starts when this page opened." : r.why });
    } else if (g.key === "free") {
      if (r.has) {
        add("FREE", `${(r.free / 1e9).toFixed(2)} GB`);
        if (isNum(r.db)) add("THE BLOCKCHAIN TAKES", `${(r.db / 1e9).toFixed(2)} GB`);
        add("FREE ON THE DIAL", `${r.v.toFixed(1)}% OF FREE + CHAIN`);
      }
      add("HEADS-UP ▲ BELOW", `${disk.warn} GB`);
      add("PROBLEM ■ BELOW", `${disk.bad} GB`);
      rows.push({ note: r.has ? "Free space on the disk that holds the blockchain, from your node. The dial shows it as a share of the free space plus what the chain already takes. The line under it starts when this page opened." : r.why });
    }
    return { head: `${LABEL[g.key]}${r.has ? "" : " · NO READING"}`, rows };
  }

  // a gauge's last 15 minutes as [t, value] points (null where unknown, a gap):
  // the server's history up to this page's first reading, then this page's
  function series(key, now) {
    const out = [];
    const t0 = now - WINDOW;
    const firstLive = live.t.length ? live.t[0] : Infinity;
    const hv = hist && hist[key];
    if (hv) for (let i = 0; i < hist.t.length; i++) {
      const t = hist.t[i];
      if (!(t >= t0) || t >= firstLive) continue;
      out.push(isNum(hv[i]) ? [t, hv[i]] : null);
    }
    for (let i = 0; i < live.t.length; i++) if (live.t[i] >= t0) out.push(isNum(live[key][i]) ? [live.t[i], live[key][i]] : null);
    return out;
  }
  const nowOf = () => (lastSnap && isNum(lastSnap.generated) ? lastSnap.generated : env.now());

  // low, high and average of a gauge's last 15 minutes (raw units), or null
  function stats(key) {
    const vs = series(key, nowOf()).filter(Boolean).map((p) => p[1]);
    if (!vs.length) return null;
    let lo = Infinity, hi = -Infinity, sum = 0;
    for (const v of vs) { lo = Math.min(lo, v); hi = Math.max(hi, v); sum += v; }
    return { lo, hi, avg: sum / vs.length };
  }

  // ---- this page's readings
  // the rates' readings, one per computer read (a read shown again by the next snapshot counts once)
  function note(snap) {
    const h = snap.host && snap.host.ok !== false ? snap.host : null;
    if (!h) return;
    const p = h.proc || {}, d = h.computer?.disk || {};
    addReading(recent, h.t ?? snap.t ?? snap.generated, {
      cpu: p.cpu_pct, write: p.write_rate, busy: d.busy_pct, dread: d.read_rate, dwrite: d.write_rate,
    });
  }
  // rates over 5 s: readings come more often than that
  const overFive = () => {
    const e = lastSnap?.host?.every_s;
    return isNum(e) && e < STEP;
  };

  // the lines' points: one per 5 s, the average of the readings in it, as the server's
  // history keeps them (so the two join up, and a line isn't a jagged band of seconds)
  function push(snap, rs) {
    const t = isNum(snap.t) ? snap.t : snap.generated;
    if (!isNum(t) || t === pushedT) return;   // the same reading drawn again
    if (pushedT !== null && t < pushedT) { for (const k of ["t", ...KEYS]) live[k].length = 0; acc = null; }   // the clock went back
    pushedT = t;
    const h = snap.host && snap.host.ok !== false ? snap.host : null;
    const vals = {
      temp: h && isNum(h.computer?.cpu_temp_c) ? h.computer.cpu_temp_c : null,
      cpu: h && isNum(h.proc?.cpu_pct) ? h.proc.cpu_pct : null,
      mem: rs.mem.has ? rs.mem.used : null,
      busy: h && isNum(h.computer?.disk?.busy_pct) ? h.computer.disk.busy_pct : null,
      free: rs.free.has ? rs.free.free : null,
    };
    const at = Math.floor(t / STEP) * STEP;
    if (!acc || acc.t !== at) {
      acc = { t: at, sum: {}, n: {} };
      live.t.push(at);
      for (const k of KEYS) live[k].push(null);
    }
    const i = live.t.length - 1;
    for (const k of KEYS) {
      if (!isNum(vals[k])) continue;
      acc.sum[k] = (acc.sum[k] || 0) + vals[k];
      acc.n[k] = (acc.n[k] || 0) + 1;
      live[k][i] = acc.sum[k] / acc.n[k];
    }
    while (live.t.length && live.t[0] < t - WINDOW) for (const k of ["t", ...KEYS]) live[k].shift();
  }
  // the server's history fills in the past for the lines it keeps
  function seed(h) {
    if (!h || !Array.isArray(h.t)) return;
    hist = { t: h.t };
    for (const [k, field] of Object.entries(FROM_HISTORY)) hist[k] = Array.isArray(h[field]) ? h[field] : null;
    for (const g of gauges) g.dirty = true;
    if (shown) paintAll();
  }
  api.history(WINDOW).then(seed).catch(() => {});

  // ---- sparkline under a dial
  function spark(g, now) {
    const conv = g.key === "temp" ? f.tempValue : g.key === "mem" || g.key === "free" ? (v) => v / 1e9 : (v) => v;
    const t0 = now - WINDOW;
    let lo = Infinity, hi = -Infinity, first = null, n = 0;
    const pts = series(g.key, now).map((p) => (p ? [p[0], conv(p[1])] : null));
    for (const p of pts) {
      if (!p) continue;
      lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]);
      if (first === null) first = p[0];
      n++;
    }
    g.cell.toggleAttribute("data-spark-none", n < 2);
    if (!n) {
      g.line.setAttribute("d", ""); g.area.setAttribute("d", "");
      g.dot.hidden = true;
      setText(g.capL, FROM_HISTORY[g.key] ? "NO HISTORY YET" : "NO READINGS YET");
      setText(g.capR, "");
      return;
    }
    // the value range: a little air, a minimum height so a steady line sits flat, percents from 0
    const span = { temp: 6, cpu: 4, mem: 0.4, busy: 4, free: 2 }[g.key];
    let a = lo, b = hi;
    if (g.key === "cpu" || g.key === "busy") a = 0;
    if (b - a < span) { const m = (a + b) / 2; a = (g.key === "cpu" || g.key === "busy") ? 0 : m - span / 2; b = a + Math.max(span, b - a); }
    const pad = (b - a) * 0.12;
    a -= g.key === "cpu" || g.key === "busy" ? 0 : pad; b += pad;
    // the line spans the time it has: 15 minutes once there is that much, less
    // while a line collected here is still filling up (its caption says how long)
    const from = Math.max(t0, Math.min(first, now - 10));
    const X = (t) => clamp((t - from) / Math.max(1, now - from), 0, 1) * SW;
    const Y = (v) => SH - 3 - ((v - a) / (b - a)) * (SH - 7);
    let d = "", area = "", seg = [];
    const flush = () => {
      if (!seg.length) return;
      d += seg.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
      area += `M${seg[0][0].toFixed(1)},${SH}` + seg.map(([x, y]) => `L${x.toFixed(1)},${y.toFixed(1)}`).join("") + `L${seg[seg.length - 1][0].toFixed(1)},${SH}Z`;
      seg = [];
    };
    let last = null;
    for (const p of pts) {
      if (!p) { flush(); continue; }
      last = [X(p[0]), Y(p[1])];
      seg.push(last);
    }
    flush();
    g.line.setAttribute("d", n > 1 ? d : "");
    g.area.setAttribute("d", n > 1 ? area : "");
    g.dot.hidden = !last || n < 2 || !g.r?.has;
    if (last) { g.dot.style.left = `${(last[0] / SW) * 100}%`; g.dot.style.top = `${(last[1] / SH) * 100}%`; }
    const mins = (now - first) / 60;
    setText(g.capL, mins >= 14 ? "LAST 15 MIN" : mins >= 1 ? `LAST ${Math.round(mins)} MIN` : "COLLECTING");
    // the line's low and high, as one value when it is flat
    if (g.key === "free") {
      // the same wording as the dial ("420 MB", "3.2 GB", "12.4 TB"), the unit once when both ends share it
      const [a1, ua] = split(f.bytes(lo * 1e9)), [b1, ub] = split(f.bytes(hi * 1e9));
      setText(g.capR, a1 === b1 && ua === ub ? `${a1} ${ua}` : ua === ub ? `${a1}–${b1} ${ua}` : `${a1} ${ua}–${b1} ${ub}`);
      return;
    }
    const [one, unit] = {
      temp: [(x) => String(Math.round(x)), ` ${f.tempUnit()}`],
      cpu: [shortPct, "%"],
      busy: [shortPct, "%"],
      mem: [(x) => x.toFixed(1), " GB"],
    }[g.key];
    const a1 = one(lo), b1 = one(hi);
    setText(g.capR, (a1 === b1 ? a1 : `${a1}–${b1}`) + unit);
  }

  // ---- one dial's text and state
  function apply(g, r, animate) {
    const was = g.r;
    g.r = r;
    g.cell.toggleAttribute("data-none", !r.has);
    setData(g.cell, "level", r.level || null);
    setText(g.num, r.has ? r.num : "—");
    g.num.toggleAttribute("data-long", r.has && r.num.length > 3);
    setText(g.unit, r.has ? r.unit : "NO READING");
    setText(g.lo, r.lo);
    setText(g.hi, r.hi);
    setState(g, r.state);
    setData(g.state, "level", r.level || null);
    setAttr(g.canvas, "aria-label", `${LABEL[g.key]} dial: ${r.aria}`);
    // the focusable gauge's own name carries the heads-up or problem too, as screen readers announce it
    setAttr(g.cell, "aria-label", `${LABEL[g.key]}: ${r.has ? `${r.num} ${r.unit}${r.say || ""}` : "no reading"}`);
    // the number lights up briefly when a new reading changes it (now and then: readings
    // come every second, and a number lighting up every second would only flicker)
    if (animate && was && was.has && r.has && was.num !== r.num && performance.now() - (g.flashAt || -Infinity) > FLASH_GAP_MS) {
      g.flashAt = performance.now();
      g.num.classList.remove("is-new");
      void g.num.offsetWidth;
      g.num.classList.add("is-new");
    }
    // the needle: eases to a new reading; the first time, it sweeps up from the stop
    const target = r.has ? clamp(r.v, r.min, r.max) : null;
    if (target === null) { g.cur = null; g.anim = null; return; }
    if (!animate) { g.cur = target; g.anim = null; if (shown) g.woke = true; return; }
    if (!g.woke) {
      g.woke = true;
      g.anim = { from: r.min, to: target, t0: performance.now() / 1000 + g.i * 0.09, d: WAKE_S };
      g.cur = r.min;
    } else if (g.cur === null) {
      g.anim = { from: r.min, to: target, t0: performance.now() / 1000, d: EASE_S };
      g.cur = r.min;
    } else if (Math.abs(target - (g.anim ? g.anim.to : g.cur)) > 1e-9) {
      g.anim = { from: g.cur, to: target, t0: performance.now() / 1000, d: EASE_S };
    }
  }

  // the state line under a dial, one span per " · " phrase: a narrow cell then
  // breaks the line between phrases ("■ LOW ·" / "CHAIN TAKES 256 GB"), not inside one
  function setState(g, s) {
    if (g.stateText === s) return;
    g.stateText = s;
    clear(g.state);
    // the "·" rides at the end of the phrase before it (a line may break after an
    // inline-block whatever follows it), so no line starts with one
    const phs = String(s).split(" · ");
    phs.forEach((ph, i) => {
      if (i) g.state.append(" ");
      g.state.append(el("span", { class: "cg-ph", text: i < phs.length - 1 ? `${ph}\u00a0·` : ph }));
    });
  }

  // ---- drawing one dial
  function drawGauge(g, ctx, w, h) {
    const r = g.r;
    ctx.clearRect(0, 0, w, h);
    if (!r) return;
    // where the needle is now
    if (g.anim) {
      const u = clamp((performance.now() / 1000 - g.anim.t0) / g.anim.d, 0, 1);
      g.cur = g.anim.from + (g.anim.to - g.anim.from) * easeIO(u);
      if (u >= 1) g.anim = null;
    }
    // the box is a little shorter than wide (the bottom of a 270° dial is open): sizes go by the width
    const S = Math.min(w, h / 0.92);
    const cx = w / 2, cy = S * 0.5, R = S * 0.41;
    const has = r.has && g.cur !== null;
    const dim = has ? 1 : 0.42;
    const frac = (v) => clamp((v - r.min) / (r.max - r.min), 0, 1);
    const ang = (v) => START + SWEEP * frac(v);
    const lvRGB = r.level === "bad" ? col.bad : r.level === "warn" ? col.warn : null;
    const fillRGB = lvRGB || col.accent;
    const needleRGB = lvRGB || col.head;
    const curA = has ? ang(g.cur) : START;

    // bezel: a thin cool arc a little wider than the scale, with end caps
    ctx.lineCap = "butt";
    ctx.lineWidth = 1;
    ctx.strokeStyle = `rgba(${col.head}, ${0.24 * dim})`;
    ctx.beginPath(); ctx.arc(cx, cy, R * 1.12, START - 0.12, START + SWEEP + 0.12); ctx.stroke();
    for (const a of [START - 0.12, START + SWEEP + 0.12]) {
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * R * 1.07, cy + Math.sin(a) * R * 1.07);
      ctx.lineTo(cx + Math.cos(a) * R * 1.17, cy + Math.sin(a) * R * 1.17);
      ctx.stroke();
    }
    // the hub ring round the pivot
    ctx.strokeStyle = `rgba(${col.head}, ${0.16 * dim})`;
    ctx.beginPath(); ctx.arc(cx, cy, R * 0.16, 0, TAU); ctx.stroke();

    // heads-up and problem marks: a thin band just outside the ticks
    // (quieter while the reading is outside them, full strength while it is in one)
    ctx.lineWidth = 1.6;
    for (const z of r.zones) {
      if (!(z.to > z.from)) continue;
      ctx.strokeStyle = `rgba(${z.level === "bad" ? col.bad : col.warn}, ${(z.level === r.level ? 0.9 : 0.5) * (has ? 1 : 0.6)})`;
      ctx.beginPath(); ctx.arc(cx, cy, R * 1.035, ang(z.from), Math.max(ang(z.to), ang(z.from) + 0.012)); ctx.stroke();
    }

    // ticks: lit up to the reading, dim beyond it; inside a mark, in its colour
    for (const tk of r.ticks || (r.ticks = ticks(r.min, r.max, r.major, r.minor))) {
      const a = ang(tk.v);
      const zone = r.zones.find((z) => z.to > z.from && tk.v >= z.from - 1e-9 && tk.v <= z.to + 1e-9);
      const lit = has && a <= curA + 1e-6;
      let rgb, al;
      if (zone) { rgb = zone.level === "bad" ? col.bad : col.warn; al = (tk.major ? 0.95 : 0.7) * (zone.level === r.level ? 1 : 0.62) * (has ? 1 : 0.6); }
      else if (lit) { rgb = fillRGB; al = tk.major ? 0.95 : 0.7; }
      else { rgb = col.ink; al = (tk.major ? 0.62 : 0.26) * dim; }
      const r0 = R * (tk.major ? 0.86 : 0.92);
      ctx.strokeStyle = `rgba(${rgb}, ${al})`;
      ctx.lineWidth = tk.major ? 1.5 : 1;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
      ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
      ctx.stroke();
    }

    // the track, then the arc filled up to the reading, glowing
    const TR = R * 0.76;
    ctx.lineWidth = 3;
    ctx.strokeStyle = `rgba(${col.head}, ${0.13 * dim})`;
    ctx.beginPath(); ctx.arc(cx, cy, TR, START, START + SWEEP); ctx.stroke();
    if (has && curA > START + 0.004) {
      const glow = look.glow ? 1 : 0.45;
      ctx.globalCompositeOperation = "lighter";
      ctx.lineCap = "round";
      for (const [lw, al] of [[11, 0.07], [6, 0.16]]) {
        ctx.lineWidth = lw;
        ctx.strokeStyle = `rgba(${fillRGB}, ${al * glow})`;
        ctx.beginPath(); ctx.arc(cx, cy, TR, START, curA); ctx.stroke();
      }
      ctx.globalCompositeOperation = "source-over";
      // the core of the arc brightens toward its head
      let stroke = `rgba(${fillRGB}, 0.95)`;
      if (typeof ctx.createConicGradient === "function") {
        const cg = ctx.createConicGradient(START, cx, cy);
        const u = (curA - START) / TAU;
        cg.addColorStop(0, `rgba(${fillRGB}, 0.35)`);
        cg.addColorStop(Math.max(0.001, u), `rgba(${fillRGB}, 1)`);
        cg.addColorStop(Math.min(1, u + 0.001), `rgba(${fillRGB}, 0)`);
        cg.addColorStop(1, `rgba(${fillRGB}, 0)`);
        stroke = cg;
      }
      ctx.lineWidth = 2.6;
      ctx.lineCap = "butt";
      ctx.strokeStyle = stroke;
      ctx.beginPath(); ctx.arc(cx, cy, TR, START, curA); ctx.stroke();
      // a spark of light at the head of the arc
      ctx.globalCompositeOperation = "lighter";
      stamp(ctx, glowSprite(fillRGB, 24, 0.55 * glow + 0.15), cx + Math.cos(curA) * TR, cy + Math.sin(curA) * TR, R * 0.34);
      ctx.globalCompositeOperation = "source-over";
    }

    // the needle and its pivot
    if (has) {
      const tip = R * 0.95, tail = R * 0.17;
      const ca = Math.cos(curA), sa = Math.sin(curA);
      ctx.lineCap = "round";
      ctx.globalCompositeOperation = "lighter";
      ctx.lineWidth = 4.5;
      ctx.strokeStyle = `rgba(${needleRGB}, ${look.glow ? 0.16 : 0.07})`;
      ctx.beginPath(); ctx.moveTo(cx - ca * tail, cy - sa * tail); ctx.lineTo(cx + ca * tip, cy + sa * tip); ctx.stroke();
      ctx.globalCompositeOperation = "source-over";
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = `rgba(${needleRGB}, 0.95)`;
      ctx.beginPath(); ctx.moveTo(cx - ca * tail, cy - sa * tail); ctx.lineTo(cx + ca * tip, cy + sa * tip); ctx.stroke();
      ctx.globalCompositeOperation = "lighter";
      stamp(ctx, glowSprite(col.accent, 20, 0.6), cx, cy, R * (look.glow ? 0.42 : 0.26));
      ctx.globalCompositeOperation = "source-over";
    }
    ctx.fillStyle = `rgba(${col.accent}, ${has ? 1 : 0.35})`;
    ctx.beginPath(); ctx.arc(cx, cy, Math.max(2.5, R * 0.045), 0, TAU); ctx.fill();
    ctx.strokeStyle = `rgba(${col.accent}, ${has ? 0.5 : 0.2})`;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, Math.max(4.5, R * 0.085), 0, TAU); ctx.stroke();

    // done moving: stop the frame loop until the next reading
    if (!g.anim && g.stage.running) g.stage.stop();
  }

  // ---- the strip of key facts under the dials: written in place, so a PID or a time
  // can be selected; made again only when which facts it shows changes
  function renderFoot(snap) {
    const items = [];   // [label, words, level]; a label of null is a whole-line note
    footItems(snap, items);
    const shape = items.map((it) => it[0] ?? "~").join("|");
    if (foot.dataset.shape !== shape) {
      foot.dataset.shape = shape;
      clear(foot);
      for (const [k] of items) {
        foot.append(k === null ? el("span", { class: "cg-fnote" })
          : el("span", { class: "cg-fi" }, el("span", { class: "cg-fk", text: k }), el("b")));
      }
    }
    items.forEach(([k, v, level], i) => {
      const node = k === null ? foot.children[i] : foot.children[i].lastElementChild;
      setText(node, f.flag(level, v));
      setData(node, "level", level || null);
    });
  }

  function footItems(snap, items) {
    const h = snap.host;
    const item = (k, v, level) => items.push([k, v, level || null]);
    if (!h) {
      item(null, "COMPUTER READS ARE OFF · TURN THEM ON IN SETTINGS → COMPUTER READS · DISK FREE STILL COMES FROM YOUR NODE");
      return;
    }
    if (h.remote) {
      item(null, `NODE ON ANOTHER COMPUTER · ${String(h.remote.where || "").toUpperCase()} · ${remoteWhy(h.remote)} · DISK FREE STILL COMES FROM YOUR NODE`);
      return;
    }
    if (h.ok === false) {
      item(null, `CAN'T READ THIS COMPUTER · ${String(h.error || "UNKNOWN REASON").toUpperCase()}`, "warn");
      return;
    }
    const L = h.computer || {};
    const p = h.proc;
    const loadHigh = isNum(L.load1) && isNum(L.cores) && L.load1 > L.cores;
    item("CPU LOAD", isNum(L.load1) ? `${L.load1.toFixed(2)}${isNum(L.cores) ? ` ON ${f.plural(L.cores, "CORE", "CORES")}` : ""}${loadHigh ? " · BUSY" : ""}` : "—", loadHigh ? "warn" : null);
    const th = throttleText(L.throttle, snap.generated);
    item("CPU THROTTLE", th.text, th.level);
    item("COMPUTER ON FOR", f.dur(L.uptime_s));
    let pw = "—", pwLevel = null;
    if (L.on_ac === true) pw = "PLUGGED IN";
    else if (L.on_ac === false) { pw = "ON BATTERY"; pwLevel = "warn"; }
    if (isNum(L.battery_pct)) {
      pw += ` · BATTERY ${Math.round(L.battery_pct)}%`;
      if (L.on_ac === false && L.battery_pct < 20) pwLevel = "bad";
    }
    item("POWER", pw, pwLevel);
    const now = isNum(snap.t) ? snap.t : snap.generated;
    if (p) item("MONEROD", `PID ${p.pid}${isNum(p.started) && isNum(now) ? " · UP " + f.dur(now - p.started) : ""}${isNum(p.rss_bytes) ? " · HOLDS " + f.bytes(p.rss_bytes) : ""}`);
    else item("MONEROD", "NOT FOUND", "warn");
  }

  function renderSub(snap) {
    const h = snap.host;
    setText(env.sub, !h ? "DISK FREE FROM YOUR NODE · COMPUTER READS ARE OFF"
      : h.remote ? "NODE ON ANOTHER COMPUTER · DISK FREE FROM YOUR NODE"
      : h.ok === false ? "CAN'T READ THIS COMPUTER · DISK FREE FROM YOUR NODE"
        : `READ FROM THIS COMPUTER · EVERY ${f.num(h.every_s || 10)} S${overFive() ? " · RATES OVER 5 S" : ""} · DISK FREE FROM YOUR NODE · LINES: LAST 15 MIN`);
    setData(env.sub, "level", h && h.ok === false && !h.remote ? "warn" : null);
  }

  // ---- painting
  function paintAll() {
    const now = nowOf();
    for (const g of gauges) {
      if (g.dirty) { spark(g, now); g.dirty = false; }
      if (!g.r) continue;
      if (g.anim && moving(look)) g.stage.start();
      else { if (g.anim) { g.cur = g.anim.to; g.anim = null; } g.stage.frame(); }
    }
  }

  // re-show an open tooltip with the new numbers
  function refreshTip() {
    if (!tipKey) return;
    const g = gauges.find((x) => x.key === tipKey);
    if (!g) return;
    const t = tipOf(g);
    if (tipAt) env.tip.showTip(tipAt.x, tipAt.y, t.head, t.rows);
    else if (document.activeElement === g.cell) {
      const b = g.cell.getBoundingClientRect();
      env.tip.showTip(b.left + b.width / 2, b.top + b.height / 2, t.head, t.rows);
    }
  }

  function readColours(lk) {
    col = {
      accent: lk.accent || cssRGB("--accent-rgb"),
      head: lk.head || cssRGB("--head", "107, 174, 200"),
      ink: lk.ink || cssRGB("--ink", "232, 232, 240"),
      warn: cssRGB("--warn-rgb", "255, 112, 64"),
      bad: cssRGB("--bad-rgb", "240, 32, 110"),
    };
  }
  readColours(look);

  // the disk-space alert limits live in the settings
  let limitsAsked = 0, limitsTimer = 0;
  function readLimits() {
    clearTimeout(limitsTimer);   // settings previews can come in bursts: ask once they settle
    limitsTimer = setTimeout(askLimits, 1200);
  }
  function askLimits() {
    const ask = ++limitsAsked;
    api.settings().then((s) => {
      if (ask !== limitsAsked) return;
      const v = (s && s.values) || {};
      const w = Number(v["thresholds.disk_warn_gb"]), b = Number(v["thresholds.disk_bad_gb"]);
      disk = { warn: Number.isFinite(w) ? w : 20, bad: Number.isFinite(b) ? b : 5 };
      if (lastSnap) redo(false);
    }).catch(() => {});
  }
  askLimits();

  // read a snapshot into every dial (animate: ease the needles to it)
  function redo(animate) {
    const rs = readings(lastSnap);
    for (const g of gauges) { apply(g, rs[g.key], animate); g.dirty = true; }
    return rs;
  }

  return {
    update(snap) {
      lastSnap = snap;
      note(snap);
      const rs = redo(shown && moving(look));
      push(snap, rs);
      renderFoot(snap);
      renderSub(snap);
      if (shown) { paintAll(); refreshTip(); }
    },
    show() {
      shown = true;
      if (lastSnap && moving(look)) {
        // the first time on show, the needles sweep up from their stops
        for (const g of gauges) if (!g.woke && g.r && g.r.has) apply(g, g.r, true);
      }
      paintAll();
    },
    hide() {
      shown = false;
      for (const g of gauges) {
        g.stage.stop();
        if (g.anim) { g.cur = g.anim.to; g.anim = null; }
      }
      env.tip.hideTip();
      tipKey = null;
    },
    look(lk) {
      look = lk;
      readColours(lk);
      readLimits();
      if (!lastSnap) return;
      redo(false);       // the temperature unit may have changed
      if (shown) paintAll();
    },
  };
}
