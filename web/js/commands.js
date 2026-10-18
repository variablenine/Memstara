// Command drawer: monerod's read-only console commands, with a form for their
// arguments and their answers drawn as tables. The list comes from the server
// (GET /api/commands), so it always matches what the server will run. None of
// them changes anything on the node.
import { $, el, clear, setText } from "./dom.js";
import { api } from "./api.js";
import * as f from "./fmt.js";

const GROUPS = ["Node", "Chain", "Transactions", "Network", "Bans", "Mining"];
// the one log file: monerod's own
function logViews() {
  return [
    { name: "log:monerod", title: "monerod's log", group: "Log files", kind: "file", source: "monerod",
      summary: "The last lines of monerod's own log file (Settings → Node's log sets where it is). Read-only." },
  ];
}
const SKIP_KEYS = new Set(["status", "untrusted", "credits", "top_hash", "id", "jsonrpc"]);
const MAX_ROWS = 500;

let list = null, current = "status", lastFocus = null;
const drafts = {};          // per command: argument values typed so far
const answers = {};         // per command: last answer {at, data} or {at, error}

export function initCommands() {
  $("open-commands").addEventListener("click", () => openCommands());
  $("close-commands").addEventListener("click", close);
  $("cmd-scrim").addEventListener("click", close);
  $("commands").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    if (e.key === "Tab") trapTab(e);
  });
  $("cmd-find").addEventListener("input", drawList);
}

export async function openCommands(name) {
  lastFocus = document.activeElement;
  $("cmd-scrim").hidden = false;
  $("commands").hidden = false;
  if (name) current = name;
  if (!list) {
    clear($("cmd-main")).append(el("p", { class: "set-intro", text: "LOADING THE COMMAND LIST…" }));
    try {
      const r = await api.commands();
      list = (Array.isArray(r?.commands) ? r.commands : []).concat(logViews());
    } catch (e) {
      clear($("cmd-main")).append(el("p", { class: "ferr", text: "■ COULDN'T LOAD THE COMMAND LIST · " + String(e.message).toUpperCase() }));
      $("close-commands").focus();   // focus must not stay behind the open drawer
      return;
    }
  }
  drawList();
  select(current);
  $("cmd-find").focus();
}

export function commandsOpen() { return !$("commands").hidden; }

function close() {
  $("cmd-scrim").hidden = true;
  $("commands").hidden = true;
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

function trapTab(e) {
  const focusables = [...$("commands").querySelectorAll("button, input, select, textarea, summary, [tabindex='0']")].filter((n) => !n.disabled && n.offsetParent !== null);
  if (!focusables.length) return;
  const first = focusables[0], last = focusables[focusables.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

// ---- list ----------------------------------------------------------------

function matches(c, q) {
  if (!q) return true;
  const hay = [c.name, c.title, c.summary, ...(c.aliases || [])].join(" ").toLowerCase();
  return q.split(/\s+/).every((w) => hay.includes(w));
}

function drawList() {
  const box = clear($("cmd-list"));
  if (!list) return;
  const q = $("cmd-find").value.trim().toLowerCase();
  const groups = [...GROUPS, ...new Set(list.map((c) => c.group).filter((g) => !GROUPS.includes(g)))];
  let shown = 0;
  for (const g of groups) {
    const items = list.filter((c) => c.group === g && matches(c, q));
    if (!items.length) continue;
    box.append(el("div", { class: "cmd-group", text: g.toUpperCase() }));
    for (const c of items) {
      shown++;
      box.append(el("button", { type: "button", "data-name": c.name, "aria-current": String(c.name === current), onclick: () => select(c.name) },
        el("span", { class: "cmd-t", text: c.title || c.name }),
        el("span", { class: "cmd-n", text: c.name.startsWith("log:") ? "file" : c.name })));
    }
  }
  if (!shown) box.append(el("p", { class: "cmd-none", text: "NOTHING MATCHES" }));
}

function select(name) {
  const c = list?.find((x) => x.name === name) || list?.[0];
  if (!c) return;
  current = c.name;
  for (const b of $("cmd-list").querySelectorAll("button")) b.setAttribute("aria-current", String(b.dataset.name === c.name));
  drawCommand(c);
  const needs = (c.params || []).some((p) => p.required);
  if (!needs && !answers[c.name]) run(c);
}

// ---- one command ---------------------------------------------------------

function drawCommand(c) {
  const main = clear($("cmd-main"));
  const head = el("div", { class: "cmd-head" },
    el("h3", { text: (c.title || c.name).toUpperCase() }),
    c.name.startsWith("log:") ? null : el("code", { class: "cmd-name", text: [c.name, ...(c.aliases || [])].join(" · ") }),
    el("span", { class: "cmd-kind", id: "cmd-kind" }));
  main.append(head);
  if (c.summary) main.append(el("p", { class: "set-intro", text: c.summary }));

  const params = c.kind === "file" ? logParams() : (c.params || []);
  const d = (drafts[c.name] ||= defaults(params));
  if (params.length) {
    const form = el("div", { class: "set-group cmd-form" });
    for (const p of params) form.append(paramField(c, p, d));
    main.append(form);
  }
  const runBtn = el("button", { type: "button", id: "cmd-run", class: "btn btn-primary", onclick: () => run(c) });
  setText(runBtn, c.kind === "file" ? "READ IT" : "RUN");
  main.append(el("div", { class: "set-actions" }, runBtn, el("span", { class: "cmd-calls", text: c.calls?.length ? "RPC · " + c.calls.join(" · ") : "" })));
  main.append(el("div", { class: "cmd-out", id: "cmd-out", role: "status", "aria-live": "polite" }));
  const tag = $("cmd-kind");
  tag.dataset.kind = c.kind === "file" ? "file" : "read";
  setText(tag, "READS ONLY");
  drawAnswer(c);
}

function logParams() {
  return [
    { name: "lines", label: "Lines", type: "choice", choices: [100, 200, 500, 1000].map((v) => ({ value: v, label: String(v) })), default: 200 },
    { name: "only_problems", label: "Only errors and warnings", type: "bool", default: false },
    { name: "find", label: "Only lines with", type: "text", placeholder: "word or address" },
  ];
}

function defaults(params) {
  const d = {};
  for (const p of params) d[p.name] = p.default ?? (p.type === "bool" ? false : "");
  return d;
}

function paramField(c, p, d) {
  const id = `cp-${c.name.replace(/[^a-z0-9]/gi, "-")}-${p.name}`;
  const wrap = el("div", { class: "field" });
  const top = el("div", { class: "field-top" });
  let input;
  if (p.type === "bool") {
    top.append(el("span", { class: "flabel", text: p.label }));
    const cb = el("input", { type: "checkbox", id });
    cb.checked = !!d[p.name];
    const state = el("span", { text: cb.checked ? "YES" : "NO" });
    cb.addEventListener("change", () => { d[p.name] = cb.checked; setText(state, cb.checked ? "YES" : "NO"); });
    input = el("label", { class: "toggle", for: id }, cb, el("span", { class: "tg" }), state);
  } else if (p.type === "choice") {
    top.append(el("span", { class: "flabel", id: id + "-l", text: p.label }));
    input = el("div", { class: "choices", role: "radiogroup", "aria-labelledby": id + "-l" });
    for (const ch of p.choices || []) {
      const r = el("input", { type: "radio", name: id, value: String(ch.value) });
      r.checked = String(d[p.name]) === String(ch.value);
      r.addEventListener("change", () => { if (r.checked) d[p.name] = ch.value; });
      input.append(el("label", {}, r, String(ch.label ?? ch.value)));
    }
  } else {
    top.append(el("label", { for: id, text: p.label + (p.required ? "" : " (optional)") }));
    const isNum = p.type === "int";
    const inp = el("input", { id, type: isNum ? "number" : "text", spellcheck: "false", autocomplete: "off", placeholder: p.placeholder || "", min: p.min, max: p.max, step: isNum ? 1 : undefined, inputmode: isNum ? "numeric" : undefined });
    inp.value = d[p.name] ?? "";
    inp.addEventListener("input", () => { d[p.name] = inp.value; });
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); run(c); } });
    input = el("div", { class: "finput" }, inp, p.unit ? el("span", { class: "funit", text: p.unit }) : null);
  }
  wrap.append(top, input);
  if (p.help) wrap.append(el("p", { class: "fhelp", text: p.help }));
  return wrap;
}

function argsFor(c) {
  const d = drafts[c.name] || {};
  const out = {};
  for (const p of c.params || []) {
    let v = d[p.name];
    if (v === "" || v === null || v === undefined) continue;
    if (p.type === "int") v = Number(v);
    out[p.name] = v;
  }
  return out;
}

async function run(c) {
  if (c.kind === "file") return readLog(c);
  const args = argsFor(c);
  const btn = $("cmd-run");
  if (btn) btn.disabled = true;
  showBusy();
  try {
    answers[c.name] = { at: Date.now() / 1000, data: await api.command(c.name, args) };
  } catch (e) {
    answers[c.name] = { at: Date.now() / 1000, error: e.message };
  } finally {
    if (btn && current === c.name) btn.disabled = false;
  }
  if (current === c.name) drawAnswer(c);
}

function showBusy() {
  const out = $("cmd-out");
  if (out) clear(out).append(el("p", { class: "cmd-meta", text: "ASKING THE NODE…" }));
}

// ---- answers -------------------------------------------------------------

function drawAnswer(c) {
  const out = $("cmd-out");
  if (!out) return;
  clear(out);
  const a = answers[c.name];
  if (!a) return;
  if (a.error) {
    out.append(el("p", { class: "cmd-err", text: "■ " + a.error }));
    return;
  }
  if (c.kind === "file") { drawLog(out, a.data, drafts[c.name] || {}); return; }
  const r = a.data || {};
  out.append(el("p", { class: "cmd-meta", text: `ANSWERED ${f.dayClock(a.at)}${r.calls?.length ? " · " + r.calls.join(" · ") : ""}` }));
  if (r.message) out.append(el("p", { class: "cmd-msg", text: r.message }));
  const res = r.result && typeof r.result === "object" ? r.result : {};
  const keys = Object.keys(res);
  for (const k of keys) {
    const sec = el("section", { class: "cmd-sec" });
    if (keys.length > 1) sec.append(el("h4", { text: k }));
    sec.append(renderValue(res[k], k));
    out.append(sec);
  }
  const raw = el("details", { class: "cmd-raw" }, el("summary", { text: "RAW ANSWER (JSON)" }), el("pre", { text: JSON.stringify(res, null, 2) }));
  out.append(raw);
}

// objects become key/value lists, lists of objects become tables
function renderValue(v, key) {
  if (Array.isArray(v)) return renderArray(v, key);
  if (v && typeof v === "object") {
    const box = el("div", { class: "cmd-obj" });
    const kv = el("dl", { class: "kv cmd-kv" });
    const later = [];
    for (const [k, x] of Object.entries(v)) {
      if (SKIP_KEYS.has(k) && (x === "OK" || x === false || x === 0 || k === "id" || k === "jsonrpc")) continue;
      if (x && typeof x === "object") { later.push([k, x]); continue; }
      kv.append(el("div", {}, el("dt", { text: label(k) }), el("dd", { text: cell(k, x), title: typeof x === "string" && x.length > 40 ? x : null })));
    }
    if (kv.childElementCount) box.append(kv);
    for (const [k, x] of later) {
      const sub = el("section", { class: "cmd-sub" }, el("h5", { text: label(k) + (Array.isArray(x) ? ` · ${f.num(x.length)}` : "") }));
      sub.append(renderValue(x, k));
      box.append(sub);
    }
    if (!box.childElementCount) box.append(el("p", { class: "cmd-meta", text: "NOTHING TO SHOW · THE NODE SAID OK" }));
    return box;
  }
  return el("p", { text: cell(key, v) });
}

function renderArray(arr, key) {
  if (!arr.length) return el("p", { class: "cmd-meta", text: "NONE" });
  if (arr.every((x) => x === null || typeof x !== "object")) {
    return el("p", { class: "cmd-list-plain", text: arr.slice(0, MAX_ROWS).map((x) => cell(key, x)).join(" · ") + (arr.length > MAX_ROWS ? ` · … ${f.num(arr.length - MAX_ROWS)} MORE` : "") });
  }
  const cols = [];
  for (const row of arr.slice(0, 50)) for (const k of Object.keys(row || {})) if (!cols.includes(k) && !(row[k] && typeof row[k] === "object")) cols.push(k);
  // get_bans repeats each host as a number in "ip" (0 for ranges): the host column already says it
  const numIp = cols.includes("host") && arr.every((r) => typeof r?.ip === "number");
  const used = cols.filter((k) => !(numIp && k === "ip") && arr.some((r) => r && r[k] !== null && r[k] !== undefined && r[k] !== ""));
  const table = el("table", {},
    el("thead", {}, el("tr", {}, ...used.map((k) => el("th", { scope: "col", text: label(k) })))));
  const tb = el("tbody");
  for (const row of arr.slice(0, MAX_ROWS)) {
    tb.append(el("tr", {}, ...used.map((k) => {
      const x = row?.[k];
      return el("td", { text: cell(k, x), title: typeof x === "string" && x.length > 24 ? x : null });
    })));
  }
  table.append(tb);
  const wrap = el("div", {}, el("div", { class: "tablewrap cmd-tblwrap" }, table));
  if (arr.length > MAX_ROWS) wrap.append(el("p", { class: "cmd-meta", text: `SHOWING ${MAX_ROWS} OF ${f.num(arr.length)} · THE RAW ANSWER HAS ALL OF THEM` }));
  return wrap;
}

const label = (k) => String(k).replace(/_/g, " ").toUpperCase();

// format one value by what its name says it is
function cell(k, x) {
  if (x === null || x === undefined || x === "") return "—";
  if (typeof x === "boolean") return x ? "YES" : "NO";
  const key = String(k).toLowerCase();
  if (typeof x === "number") {
    // "oldest" (pool stats) is when the oldest waiting transaction arrived; 0 = the pool is empty
    if (key === "oldest" && x === 0) return "—";
    if (/(^|_)(time|timestamp|last_seen|start_time|receive_time|last_relayed_time|last_failed_time|oldest)$/.test(key) && x > 1e9 && x < 1e11) return `${f.dayClock(x)} (${f.ago(x, Date.now() / 1000)})`;
    // counts whose names end in "size" (get_info): transactions and addresses, not bytes
    if (key === "tx_pool_size" || /_peerlist_size$/.test(key)) return f.num(x);
    if (/(^|_)(size|weight|bytes|free_space|database_size|blob_size|block_size|block_weight|block_size_limit|block_weight_limit|block_size_median|block_weight_median|total_bytes_in|total_bytes_out|bytes_total|bytes_med|bytes_min|bytes_max)$/.test(key)) return f.bytes(x);
    // all 12 decimals when needed: a fee per byte is far below 0.0001 XMR
    if (/(^|_)(fee|fees|reward|amount|emission_amount|fee_amount|fee_total|base_reward)$/.test(key) && Number.isInteger(x) && x >= 1000) return f.xmr(x, 12);
    if (/(^|_)(avg_download|avg_upload|current_download|current_upload|limit_up|limit_down)$/.test(key)) return `${f.num(x)} kB/s`;
    // ban time left; --ban-list bans never end, and monerod reports a huge number for them
    if (key === "seconds") return x > 5 * 365 * 86400 ? "NO END SET" : f.dur(x);
    if (/^(live_time|recv_idle_time|send_idle_time)$/.test(key)) return f.dur(x);
    return f.num(x);
  }
  const s = String(x);
  if (/^[0-9a-f]{64}$/i.test(s)) return s.slice(0, 12) + "…" + s.slice(-6);
  return s;
}

// ---- log files -----------------------------------------------------------

async function readLog(c) {
  const d = drafts[c.name] || {};
  showBusy();
  try {
    answers[c.name] = { at: Date.now() / 1000, data: await api.nodeLog(c.source, d.lines || 200) };
  } catch (e) {
    answers[c.name] = { at: Date.now() / 1000, error: e.message };
  }
  if (current === c.name) drawAnswer(c);
}

const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;

function drawLog(out, r, d) {
  const lines = Array.isArray(r?.lines) ? r.lines : [];
  out.append(el("p", { class: "cmd-meta", text: `${r?.path || "—"} · ${f.isNum(r?.size) ? f.bytes(r.size) : "—"} · CHANGED ${f.isNum(r?.mtime) ? f.dayClock(r.mtime) : "—"}` }));
  const find = String(d.find || "").trim().toLowerCase();
  const pre = el("pre", { class: "logview" });
  let n = 0;
  for (const raw of lines) {
    const line = String(raw).replace(ANSI, "");
    const level = /\b(FATAL|ERROR)\b/.test(line) ? "bad" : /\bWARN(ING)?\b/.test(line) ? "warn" : null;
    if (d.only_problems && !level) continue;
    if (find && !line.toLowerCase().includes(find)) continue;
    pre.append(el("span", { "data-level": level, text: line + "\n" }));
    n++;
  }
  if (!n) pre.append(el("span", { text: lines.length ? "NO LINES MATCH" : "THE LOG IS EMPTY" }));
  out.append(pre);
  pre.scrollTop = pre.scrollHeight;
}
