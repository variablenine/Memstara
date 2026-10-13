// J · Computer: what the computer running the node says about it, read
// straight from /proc, /sys and `ss` (none of this is in any RPC answer): the
// monerod process, the flags it was started with, CPU temperature, power,
// memory, the drive holding the chain, the network link the node is bound to
// and the Tor proxy.
import { $, el, clear, setText, setData, setFig, syncKV } from "../dom.js";
import { lineChart } from "../charts.js";
import { addRow, addReading, meanOf } from "../rows.js";
import * as f from "../fmt.js";

const WINDOW = 900;
// heads-up and problem marks for the CPU temperature, in °C: the same as the
// server's cpu_temp alert (memstara/alerts.py). Most processors slow themselves
// down to cool off at 95 to 105 °C.
export const HOT = { warn: 85, bad: 95 };
const FIELDS = ["cpu_temp_c", "node_cpu_pct"];
let buf = { t: [], cpu_temp_c: [], node_cpu_pct: [] };   // the server's 5 s history points
const recent = [];   // the last minute of computer readings, for the figures that are rates

// CPU throttle: is the processor slowing itself down because of heat? From the
// server's computer.throttle (memstara/host.py). A slowdown seen in the last 15
// minutes is a heads-up. -> {text, level}
export function throttleText(T, now) {
  if (!T || !T.source) return { text: "NOT REPORTED BY THIS CPU", level: null };
  const ago = f.isNum(T.last_t) && f.isNum(now) ? now - T.last_t : null;
  if (T.now) return { text: "SLOWING DOWN FROM HEAT NOW", level: "warn" };
  if (ago !== null && ago < WINDOW) return { text: `SLOWED DOWN FROM HEAT ${f.dur(ago)} AGO`, level: "warn" };
  if (T.source === "kernel") return { text: "NOT NOW · THIS CPU ONLY SHOWS THE KERNEL'S LIMITS", level: null };
  return { text: f.isNum(T.since_boot) && T.since_boot > 0
    ? `NOT NOW · ${f.plural(T.since_boot, "TIME", "TIMES")} SINCE THE COMPUTER STARTED`
    : "NOT SINCE THE COMPUTER STARTED", level: null };
}

// "2.1 OF 4.5 GHZ": the average speed of all cores now against the fastest they can run
export function speedText(T) {
  if (!T || !f.isNum(T.speed_mhz)) return "—";
  const ghz = (m) => (m / 1000).toFixed(1);
  return f.isNum(T.max_mhz) ? `${ghz(T.speed_mhz)} OF ${ghz(T.max_mhz)} GHZ` : `${ghz(T.speed_mhz)} GHZ`;
}

export function seedComputer(hist) {
  if (!hist || !Array.isArray(hist.t) || !Array.isArray(hist.cpu_temp_c)) return;
  buf = { t: hist.t.slice(), step: hist.step, cpu_temp_c: hist.cpu_temp_c.slice(), node_cpu_pct: (hist.node_cpu_pct || []).slice() };
}

// Every reading, whatever is on show. A computer reading comes once per its own
// interval (Settings), so one taken once counts once. False when the charts want the
// server's history again (points skipped).
export function recordComputer(snap) {
  const h = snap.host;
  if (h && h.ok !== false) {
    const p = h.proc || {}, L = h.computer || {}, d = L.disk || {}, v = h.bind_if || {};
    addReading(recent, h.t ?? snap.t ?? snap.generated, {
      cpu: p.cpu_pct, write: p.write_rate, read: p.read_rate,
      busy: d.busy_pct, dread: d.read_rate, dwrite: d.write_rate, rx: v.rx_rate, tx: v.tx_rate,
    });
  }
  if (!snap.hist) return true;
  const did = addRow(buf, snap.hist, FIELDS, WINDOW);
  return did !== "gap" && did !== "no";
}

// A rate as the figures show it: the last 5 seconds of readings (one second alone jumps
// about with every block written), or the newest reading when they come less often.
const avg = (key) => meanOf(recent, key);

// a reading never splits from its unit at a line end ("2.1 MB/s")
const keep = (s) => String(s).replace(/ /g, "\u00a0");
const homeShort = (p) => String(p || "").replace(/^\/home\/[^/]+\//, "~/").replace(/^\/root\//, "~/");
const tempLevel = (c) => (!f.isNum(c) ? null : c >= HOT.bad ? "bad" : c >= HOT.warn ? "warn" : null);
const memShare = (L) => (f.isNum(L.mem_used_bytes) && L.mem_total_bytes > 0 ? L.mem_used_bytes / L.mem_total_bytes : null);
const memLevel = (frac) => (frac !== null && frac >= 0.9 ? "warn" : null);
// unplugged is a heads-up; unplugged with under 20% left is a problem
const powerLevel = (L) => (L.on_ac !== false ? null : f.isNum(L.battery_pct) && L.battery_pct < 20 ? "bad" : "warn");

// "--flag value" pairs, one per line
function flagLines(flags) {
  const out = [];
  for (const a of flags) {
    const s = String(a);
    if (s.startsWith("-") || !out.length) out.push(s);
    else out[out.length - 1] += " " + s;
  }
  return out;
}

// a row for syncKV, with ▲ / ■ before a heads-up or a problem
const kvRow = (rows, k, v, level) => rows.push([k, f.flag(level, v ?? "—"), level]);

// a thin bar filled to a share of its width, with optional tick marks
function meter(id, frac, level, ticks = []) {
  const m = $(id);
  const fill = m.querySelector(".mt-fill");
  const w = f.isNum(frac) ? Math.max(0, Math.min(1, frac)) * 100 + "%" : "0";
  if (fill.style.width !== w) fill.style.width = w;
  setData(m, "level", level);
  const key = ticks.join(",");
  if (m.dataset.ticks === key) return;
  m.dataset.ticks = key;
  for (const t of m.querySelectorAll(".mt-tick")) t.remove();
  for (const t of ticks) {
    const tick = el("span", { class: "mt-tick" });
    tick.style.left = t * 100 + "%";
    m.append(tick);
  }
}

// the flags monerod was started with, one per line; written again only when they change
function setFlags(lines) {
  const fl = $("comp-flags");
  const key = JSON.stringify(lines);
  if (fl.dataset.key === key) return;
  fl.dataset.key = key;
  clear(fl);
  if (lines && !lines.length) fl.append(el("li", { text: "NO FLAGS" }));
  for (const l of lines || []) fl.append(el("li", { text: l }));
}

// why a node elsewhere has no readings here, for the off line
export function remoteWhy(r) {
  return r.how === "tunnel"
    ? `REACHED THROUGH ${String(r.via || "A TUNNEL").toUpperCase()}; RUN MEMSTARA ON THE NODE'S COMPUTER FOR ITS READINGS`
    : "RUN MEMSTARA ON THE NODE'S COMPUTER FOR ITS READINGS";
}

export function renderComputer(snap) {
  const h = snap.host;
  const sub = $("computer-sub");
  const body = $("comp-body"), off = $("comp-off");
  if (!h || h.ok === false) {
    body.hidden = true;
    off.hidden = false;
    // a node on another computer is fine, not a fault: said calmly
    setText(off, !h ? "COMPUTER READS ARE OFF · SETTINGS → COMPUTER READS"
      : h.remote ? `NODE ON ANOTHER COMPUTER · ${String(h.remote.where || "").toUpperCase()} · ${remoteWhy(h.remote)}`
        : `CAN'T READ THIS COMPUTER · ${String(h.error || "UNKNOWN REASON").toUpperCase()}`);
    setText(sub, "READ FROM THIS COMPUTER, NOT RPC");
    setData(sub, "level", h && !h.remote ? "warn" : null);
    return;
  }
  body.hidden = false;
  off.hidden = true;
  const over = f.isNum(h.every_s) && h.every_s < 5 ? " · RATES OVER 5 S" : "";
  setText(sub, `READ FROM THIS COMPUTER · EVERY ${h.every_s ?? "—"} S${over}${h.error ? " · " + String(h.error).toUpperCase() : ""}`);
  setData(sub, "level", h.error ? "warn" : null);

  // ---- the monerod process ----
  const p = h.proc;
  const pk = [];
  if (!p) {
    setFig($("comp-cpu"), "—");
    setFig($("comp-rss"), "—");
    setFig($("comp-io"), "—");
    setText($("comp-io-note"), "WRITING");
    // the server says why when it knows (h.proc_error); otherwise the usual reason
    const why = h.proc_error || (snap.source?.answering === false
      ? `NOTHING IS ANSWERING ON ${snap.source?.rpc || "THE RPC PORT"}`
      : `NOT FOUND BEHIND ${snap.source?.rpc || "THE RPC PORT"}`);
    kvRow(pk, "MONEROD", String(why).toUpperCase(), "warn");
    setFlags(null);
  } else {
    const cpu = avg("cpu"), rd = avg("read");
    setFig($("comp-cpu"), f.isNum(cpu) ? f.pct(cpu) : "—");
    setFig($("comp-rss"), f.bytes(p.rss_bytes));
    setFig($("comp-io"), f.rate(avg("write")));
    setText($("comp-io-note"), f.isNum(rd) ? `WRITING · READING ${keep(f.rate(rd))}` : "WRITING");
    const now = snap.t ?? snap.generated;
    kvRow(pk, "PROCESS", `PID ${p.pid}${f.isNum(p.started) && f.isNum(now) ? " · RUNNING " + f.dur(now - p.started) : ""}`);
    kvRow(pk, "PROGRAM", p.exe_replaced ? `${homeShort(p.exe)} · REPLACED ON DISK, RESTART TO USE THE NEW ONE` : homeShort(p.exe),
      p.exe_replaced ? "warn" : null);
    kvRow(pk, "WRITTEN · READ SINCE START", `${f.bytes(p.write_bytes)} · ${f.bytes(p.read_bytes)}`);
    kvRow(pk, "THREADS · OPEN FILES", `${f.num(p.threads)} · ${f.num(p.open_files)}`);
    setFlags(Array.isArray(p.flags) ? flagLines(p.flags) : []);
  }
  syncKV($("comp-proc-kv"), pk);

  // ---- the computer ----
  const L = h.computer || {};
  const tEl = $("comp-temp");
  setFig(tEl, f.temp(L.cpu_temp_c));
  const tLevel = tempLevel(L.cpu_temp_c);
  setData(tEl.parentElement, "level", tLevel);
  setText($("comp-temp-note"), !f.isNum(L.cpu_temp_c) ? "NO SENSOR FOUND"
    : tLevel ? f.flag(tLevel, `${tLevel === "bad" ? "VERY HOT" : "HOT"} · HOTTEST SENSOR`) : "HOTTEST SENSOR");
  setText($("comp-temp-title"), `CPU TEMP · 15 MIN · ${f.tempUnit()}`);
  setText($("computer-cap"), "Read straight from this computer (/proc, /sys and the ss tool), because RPC doesn't report any of it. Nothing here changes anything. " +
    `The temperature gets a heads-up ▲ at ${f.temp(HOT.warn)} and a problem ■ at ${f.temp(HOT.bad)}; most processors slow themselves down to cool off at about ${f.temp(95)} to ${f.temp(105)}. ` +
    "CPU THROTTLE shows those slowdowns: Intel processors report each one, while other makes may only show the limits the kernel sets, not the ones their own firmware makes.");

  // power: plugged in or not, battery level
  const pw = $("comp-power");
  let pwText = "—", pwNote = "";
  const pwLevel = powerLevel(L);
  if (L.on_ac === true) pwText = "PLUGGED IN";
  else if (L.on_ac === false) pwText = "ON BATTERY";
  if (f.isNum(L.battery_pct)) {
    pwNote = `BATTERY ${Math.round(L.battery_pct)}%${L.battery_state ? " · " + String(L.battery_state).toUpperCase() : ""}`;
  } else if (L.on_ac !== undefined && L.on_ac !== null) pwNote = "NO BATTERY";
  setText(pw, pwText);
  setData(pw.parentElement, "level", pwLevel);
  setText($("comp-power-note"), f.flag(pwLevel, pwNote || "—"));

  // meters: memory, chain drive
  const memFrac = memShare(L);
  const memLvl = memLevel(memFrac);
  meter("mt-mem", memFrac, memLvl);
  setText($("mt-mem-v"), memFrac !== null ? f.flag(memLvl, `${f.gb(L.mem_used_bytes)} OF ${f.gb(L.mem_total_bytes)}${memLvl ? " · NEARLY FULL" : ""}`) : "—");
  const d = L.disk || null;
  const busy = d ? avg("busy") : null, dread = d ? avg("dread") : null;
  const diskLevel = busy >= 90 ? "warn" : null;
  meter("mt-disk", f.isNum(busy) ? busy / 100 : null, diskLevel);
  setText($("mt-disk-v"), f.isNum(busy) ? f.flag(diskLevel, `${f.pct(busy, 0)} BUSY`) : "—");
  setText($("mt-disk-k"), d && d.device ? `CHAIN DRIVE · ${String(d.device).toUpperCase()}` : "CHAIN DRIVE");
  setText($("mt-disk-note"), f.isNum(dread) ? `READING ${keep(f.rate(dread))} · WRITING ${keep(f.rate(avg("dwrite")))}` : "");

  const lk = [];
  const loadHigh = f.isNum(L.load1) && f.isNum(L.cores) && L.load1 > L.cores;
  kvRow(lk, "CPU LOAD", f.isNum(L.load1) ? `${L.load1.toFixed(2)} ON ${f.plural(L.cores, "CORE", "CORES")}${loadHigh ? " · BUSY" : ""}` : "—",
    loadHigh ? "warn" : null);
  const th = throttleText(L.throttle, snap.generated);
  kvRow(lk, "CPU THROTTLE", th.text, th.level);
  kvRow(lk, "CPU SPEED", speedText(L.throttle));
  kvRow(lk, "COMPUTER ON FOR", f.dur(L.uptime_s));
  // the interface the node's peer-to-peer port is bound to, if it's bound to one
  const v = h.bind_if;
  if (v) {
    const word = v.kind === "vpn" ? "VPN" : "NETWORK";
    kvRow(lk, `${word} LINK`, `${String(v.name || "?").toUpperCase()} · ${v.up ? "UP" : "DOWN"}${v.address ? " · " + v.address : ""}`, v.up ? null : "bad");
    const rx = avg("rx");
    kvRow(lk, `${word} TRAFFIC`, f.isNum(rx) ? `DOWN ${f.rate(rx)} · UP ${f.rate(avg("tx"))}` : "—");
  } else kvRow(lk, "NETWORK LINK", "ANY NETWORK", "off");   // no one interface: it listens on every network
  const tor = h.tor || {};
  kvRow(lk, "TOR PROXY", tor.socks_listening === true ? `LISTENING ON ${tor.socks_port ?? 9050}` : tor.socks_listening === false ? "NOT LISTENING" : "—",
    tor.socks_listening === false ? "warn" : null);
  syncKV($("comp-kv"), lk);
  draw();
}

function draw() {
  lineChart($("comp-temp-spark"), {
    t: buf.t, height: 96, label: "CPU temperature over the last 15 minutes", empty: buf.cpu_temp_c.some(f.isNum) ? "COLLECTING" : "NO SENSOR",
    yFmt: (x) => `${Math.round(x)}°`,
    series: [{ key: "s1", name: "CPU TEMP", values: buf.cpu_temp_c.map(f.tempValue), fmt: (x) => `${Math.round(x)} ${f.tempUnit()}` }],
  });
  lineChart($("comp-cpu-spark"), {
    t: buf.t, height: 96, label: "monerod CPU use over the last 15 minutes", empty: "COLLECTING", minTop: 10,
    yFmt: (x) => `${Math.round(x)}%`,
    series: [{ key: "s2", name: "MONEROD CPU", values: buf.node_cpu_pct, fmt: (x) => f.pct(x) }],
  });
}

export function initComputer() {
  window.addEventListener("resize", () => { if (!$("comp-body").hidden) draw(); });
}
