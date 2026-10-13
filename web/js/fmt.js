// Plain-language number, size and time formats. Sizes use decimal units
// (1 GB = 1,000,000,000 bytes). Unknown is "—".

const DASH = "—";
const MINUS = "−";

let clockMode = "12h";
export function setClockMode(mode) {
  clockMode = mode === "24h" ? "24h" : "12h";
}

// Privacy mode shows every clock in UTC, so a screenshot doesn't give away the time zone
let utc = false;
export function setUtc(on) {
  utc = !!on;
  formats.clear();
}
export const zoneWord = () => (utc ? "UTC" : "LOCAL");
// seconds to add to a UTC time to get the shown clock's time of day (for day and hour lines)
export function zoneOffset(t) {
  return utc ? 0 : -new Date(t * 1000).getTimezoneOffset() * 60;
}

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

export function num(v, digits = 0) {
  if (!isNum(v)) return DASH;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }).replace("-", MINUS);
}

// 3 significant digits, at most 1 decimal for >= 10
function sig(v) {
  if (v >= 100) return Math.round(v).toLocaleString("en-US");
  if (v >= 10) return v.toFixed(1).replace(/\.0$/, "");
  return v.toFixed(2).replace(/0$/, "").replace(/\.0$/, "");
}

export function bytes(v) {
  if (!isNum(v)) return DASH;
  const units = ["B", "kB", "MB", "GB", "TB", "PB"];
  let i = 0;
  let x = Math.abs(v);
  while (x >= 1000 && i < units.length - 1) { x /= 1000; i++; }
  return (v < 0 ? MINUS : "") + (i === 0 ? String(Math.round(x)) : sig(x)) + " " + units[i];
}

export function rate(v) {
  return isNum(v) ? bytes(v) + "/s" : DASH;
}

export function gb(v) {
  return isNum(v) ? sig(v / 1e9) + " GB" : DASH;
}

// "42 s", "12 min", "3 h 12 min", "3 d 04 h"
export function dur(s) {
  if (!isNum(s)) return DASH;
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${String(m % 60).padStart(2, "0")} min`;
  const d = Math.floor(h / 24);
  return `${d} d ${String(h % 24).padStart(2, "0")} h`;
}

// Block wait: "1m 42s" under an hour, then like dur()
export function wait(s) {
  if (!isNum(s)) return DASH;
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return dur(s);
}

// one formatter per kind of time, made once (screens write a lot of times every second)
const formats = new Map();
function format(key, opts) {
  let fm = formats.get(key);
  if (!fm) formats.set(key, fm = new Intl.DateTimeFormat("en-US", utc ? { ...opts, timeZone: "UTC" } : opts));
  return fm;
}

export function clock(t, withSeconds = true) {
  if (!isNum(t)) return DASH;
  const h12 = clockMode === "12h";
  const opts = { hour: "numeric", minute: "2-digit", hour12: h12 };
  if (withSeconds) opts.second = "2-digit";
  return format(`time|${h12}|${withSeconds}`, opts).format(new Date(t * 1000));
}

export function dayClock(t) {
  if (!isNum(t)) return DASH;
  const d = new Date(t * 1000);
  const today = new Date();
  const sameDay = utc ? d.toISOString().slice(0, 10) === today.toISOString().slice(0, 10) : d.toDateString() === today.toDateString();
  const time = clock(t, false);
  if (sameDay) return time;
  return format("weekday", { weekday: "short" }).format(d) + " " + time;
}

export function ago(t, now) {
  if (!isNum(t)) return DASH;
  return dur(now - t) + " ago";
}

export function xmr(atomic, digits = 4) {
  if (!isNum(atomic)) return DASH;
  const v = atomic / 1e12;
  return v.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "") + " XMR";
}

export function hashrate(h) {
  if (!isNum(h)) return DASH;
  const units = ["H/s", "kH/s", "MH/s", "GH/s", "TH/s"];
  let i = 0;
  while (h >= 1000 && i < units.length - 1) { h /= 1000; i++; }
  return sig(h) + " " + units[i];
}

export function pct(v, digits = 1) {
  if (!isNum(v)) return DASH;
  if (v >= 99.95 && v < 100) return "99.9%";
  return v.toFixed(digits).replace(/\.0$/, "") + "%";
}

// Sensors report Celsius. display.temp_unit picks °F or °C; "auto" uses °F
// where the computer's language is US English (navigator.language, which
// WebKitGTK takes from the locale) and °C everywhere else.
let tempF = false;
export function setTempUnit(mode) {
  const lang = (typeof navigator !== "undefined" && navigator.language) || "";
  tempF = mode === "f" || (mode !== "c" && /^en-US(?![a-z0-9])/i.test(lang));   // "en-US", "en-US@posix"
}
export const tempUnit = () => (tempF ? "°F" : "°C");
export const tempValue = (c) => (isNum(c) ? (tempF ? (c * 9) / 5 + 32 : c) : null);
export function temp(c) {
  return isNum(c) ? `${Math.round(tempValue(c))} ${tempUnit()}` : DASH;
}
setTempUnit("auto");

export function ms(v) {
  if (!isNum(v)) return DASH;
  return v < 10 ? `${v.toFixed(1).replace(/\.0$/, "")} ms` : `${num(Math.round(v))} ms`;
}

// Chance of waiting at least this long for a block, in words.
export function odds(p) {
  if (!isNum(p)) return DASH;
  if (p >= 0.5) return "USUAL";
  const n = 1 / p;
  if (n < 1000) return `1 IN ${Math.round(n)}`;
  if (n < 1e6) return `1 IN ${num(Number(n.toPrecision(2)))}`;
  if (n < 1e9) return `1 IN ${sig(n / 1e6)} MILLION`;
  if (n < 1e12) return `1 IN ${sig(n / 1e9)} BILLION`;
  return "PRACTICALLY NEVER";
}

export function plural(n, one, many = one + "s") {
  return `${num(n)} ${n === 1 ? one : many}`;
}

// Heads-up and problem colours never go alone: "▲ " in front for a heads-up, "■ " for a
// problem (left as it is when the text already starts with one).
export function flag(level, text) {
  const s = String(text ?? DASH);
  const g = level === "bad" ? "■" : level === "warn" ? "▲" : "";
  return g && !/^[▲■]/.test(s) ? `${g} ${s}` : s;
}

export { DASH, MINUS, isNum };
