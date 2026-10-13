// E · Controls: quick ban and unban, the same as monerod's `ban` and `unban`
// commands (set_bans), with the ranges at the one-company limits (too many
// connections from one company's servers) as suggestions.
// Every action goes through the typed-word confirm step. The command drawer
// has every other monerod command.
import { $, el, setText, setData } from "../dom.js";
import { api } from "../api.js";
import { askConfirm } from "../confirm.js";
import { commandInfo } from "../commands.js";
import * as f from "../fmt.js";
import { isPrivate, hiddenRange } from "../privacy.js";

let enabled = true;
let limits = { out16: 5, in24: 4 };
let onChanged = () => {};
let last = null;                           // latest snapshot
const pick = { hours: 24 };

export function setControlSettings(v) {
  enabled = v?.["controls.enabled"] !== false;
  limits = { out16: v?.["thresholds.out16_warn"] ?? 5, in24: v?.["thresholds.in24_warn"] ?? 4 };
}

// ---- ranges --------------------------------------------------------------

// "203.0.113.x", "203.0.x.x", "1.2.3.4", "1.2.3.0/24" -> {cidr, label} or {error}
export function parseRange(text) {
  let t = String(text || "").trim().toLowerCase().replace(/\.\*/g, ".x");
  if (!t) return { error: "Type an address or a range." };
  let prefix = 32;
  const xs = t.match(/^(\d+)\.(\d+)(\.(\d+|x))?(\.x)?$/);
  if (t.includes("/")) {
    const [ip, p] = t.split("/");
    if (!/^\d+$/.test(p)) return { error: "After the / goes a number from 16 to 32." };
    prefix = Number(p);
    t = ip;
  } else if (/\.x\.x$/.test(t)) { prefix = 16; t = t.replace(/\.x\.x$/, ".0.0"); }
  else if (/\.x$/.test(t)) { prefix = 24; t = t.replace(/\.x$/, ".0"); }
  else if (xs && !xs[3]) return { error: "Add the rest: a.b.x.x, a.b.c.x or a full address." };
  const parts = t.split(".");
  if (parts.length !== 4 || parts.some((p) => !/^\d{1,3}$/.test(p) || Number(p) > 255)) return { error: "That isn't an IPv4 address or range." };
  if (prefix < 16) return { error: "Ranges wider than a.b.x.x (65,536 addresses) aren't allowed." };
  if (prefix > 32) return { error: "After the / goes a number from 16 to 32." };
  const n = parts.reduce((a, p) => a * 256 + Number(p), 0);
  const size = 2 ** (32 - prefix);
  const base = n - (n % size);
  const ip = [24, 16, 8, 0].map((s) => Math.floor(base / 2 ** s) % 256).join(".");
  const cidr = prefix === 32 ? ip : `${ip}/${prefix}`;
  return { cidr, label: rangeLabel(cidr), size };
}

export function rangeLabel(host) {
  const s = String(host || "");
  const hidden = hiddenRange(s);   // privacy mode: "⟦n24_..⟧.0/24" -> "⟦n24_..⟧.x"
  if (hidden) return hidden.text;
  const m = s.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/);
  if (!m) return s;
  if (m[5] === "24") return `${m[1]}.${m[2]}.${m[3]}.x`;
  if (m[5] === "16") return `${m[1]}.${m[2]}.x.x`;
  if (m[5] === "32") return `${m[1]}.${m[2]}.${m[3]}.${m[4]}`;
  return s;
}

const sizeWords = (n) => (n === 1 ? "1 address" : `${f.num(n)} addresses`);

// ranges at the one-company limits right now
function flaggedNow(snap) {
  const list = Array.isArray(snap?.peers?.list) ? snap.peers.list : [];
  const out = new Map(), inn = new Map();
  for (const p of list) {
    if (p.kind !== "ipv4") continue;
    if (p.dir === "out" && p.net16) out.set(p.net16, (out.get(p.net16) || 0) + 1);
    if (p.dir === "in" && p.net24) inn.set(p.net24, (inn.get(p.net24) || 0) + 1);
  }
  const got = new Map();
  for (const [net, n] of out) if (n >= limits.out16) got.set(`${net}.0.0/16`, `${n} OUT NOW`);
  for (const [net, n] of inn) if (n >= limits.in24) got.set(`${net}.0/24`, `${n} IN NOW`);
  const banned = new Set((snap?.node?.ban_list || []).map((b) => b.host));
  return [...got].filter(([h]) => !banned.has(h)).map(([host, why]) => ({ host, why }));
}

// ---- drawing -------------------------------------------------------------

// A list whose rows are kept from one reading to the next (keyed by host), so
// a focused UNBAN… or BAN… button, the confirm dialog's opener and the BANS
// centrepiece's highlight survive every snapshot. Only text that changed is
// rewritten; rows come and go only when their host does.
const updaters = new WeakMap();   // row -> its update(item)

function syncRows(ul, items, make) {
  const old = new Map();
  for (const li of ul.children) old.set(li.dataset.key, li);
  const focusedKey = ul.contains(document.activeElement) ? document.activeElement.closest("li")?.dataset.key : null;
  const focusedAt = focusedKey ? [...ul.children].findIndex((li) => li.dataset.key === focusedKey) : -1;
  const want = items.map((it) => {
    const li = old.get(it.key) || make(it);
    li.dataset.key = it.key;
    updaters.get(li)(it);
    return li;
  });
  const keep = new Set(want);
  for (const li of [...ul.children]) if (!keep.has(li)) li.remove();
  // put the rows in order, moving as few as possible (a moved row loses focus)
  want.forEach((li, i) => { if (ul.children[i] !== li) ul.insertBefore(li, ul.children[i] || null); });
  // the focused row went away (or had to move): focus its button again, or the
  // one now in its place, so a keyboard user keeps their place in the list
  if (focusedKey && !ul.contains(document.activeElement) && want.length) {
    const li = want.find((x) => x.dataset.key === focusedKey) || want[Math.min(focusedAt, want.length - 1)];
    li?.querySelector("button")?.focus({ preventScroll: true });
  }
}

function emptyRow() {
  const span = el("span", { class: "cl-empty" });
  const li = el("li", {}, span);
  updaters.set(li, (it) => setText(span, it.text));
  return li;
}

function flaggedRow() {
  // never colour alone: the heads-up glyph and a word go with the colour
  const label = el("span");
  const host = el("span", { class: "cl-host" }, el("span", { class: "cl-glyph", "aria-hidden": "true", text: "▲\u00a0" }), label);
  const why = el("span", { class: "cl-why" });
  const btn = el("button", { class: "key", type: "button" });
  const li = el("li", { "data-level": "warn" }, host, why, btn);
  btn.addEventListener("click", () => confirmBan(li.dataset.host));
  updaters.set(li, (g) => {
    li.dataset.host = g.host;
    setText(label, rangeLabel(g.host));
    setText(why, `AT THE LIMIT · ${g.why}`);
    setText(btn, `BAN ${pick.hours} H…`);
    btn.disabled = isPrivate();
  });
  return li;
}

function banRow() {
  const host = el("span", { class: "cl-host" });
  const why = el("span", { class: "cl-why" });
  const btn = el("button", { class: "key", type: "button", text: "UNBAN…" });
  const li = el("li", {}, host, why, btn);
  btn.addEventListener("click", () => confirmUnban(li.dataset.host));
  updaters.set(li, (b) => {
    li.dataset.host = b.host;
    setText(host, rangeLabel(b.host));
    setText(why, `${f.dur(b.seconds)} LEFT${b.stale ? " · AS LAST READ" : ""}`);
    btn.disabled = isPrivate();
  });
  return li;
}

export function renderControls(snap) {
  last = snap;
  $("ctl-off").hidden = enabled;
  for (const b of $("p-controls").querySelectorAll(".ctl-block")) b.hidden = !enabled;
  if (!enabled) return;

  // flagged now
  const flagged = flaggedNow(snap);
  syncRows($("ban-flagged"), flagged.length
    ? flagged.map((g) => ({ key: "f:" + g.host, ...g }))
    : [{ key: "empty", text: "NONE · NOTHING AT THE LIMITS" }],
  (it) => (it.key === "empty" ? emptyRow() : flaggedRow()));

  // bans with 24 h or less left (while the node isn't answering, its last list, still counted down)
  const node = snap.node || {};
  const bans = Array.isArray(node.ban_list) ? node.ban_list : [];
  const stale = snap.source?.answering === false;
  syncRows($("ban-list"), bans.length
    ? bans.map((b) => ({ key: "b:" + b.host, ...b, stale }))
    : [{ key: "empty", text: f.isNum(node.bans) ? "NONE" : "—" }],
  (it) => (it.key === "empty" ? emptyRow() : banRow()));
  setText($("ban-long"), f.isNum(node.bans_long) && node.bans_long > 0
    ? `Not listed: ${f.plural(node.bans_long, "ban")} with more than 24 hours left (a --ban-list file, the DNS blocklist or bans set by hand).` : "");
}

function note(text, level) {
  const m = $("ctl-msg");
  setText(m, text);
  setData(m, "level", level);
}

async function confirmBan(host) {
  if (isPrivate()) return;
  const r = parseRange(host);
  if (r.error) { note(r.error, "bad"); return; }
  const h = pick.hours;
  // the server's own note on `ban` (an add-on can add to it)
  const warn = (await commandInfo("ban"))?.warn;
  askConfirm({
    title: `BAN ${r.label} FOR ${h} H`,
    body: `Drops every connection from ${r.label} (${sizeWords(r.size)}) now and refuses new ones for ${h} ${h === 1 ? "hour" : "hours"}, ` +
      "or until the node restarts. UNBAN undoes it any time." + (warn ? " " + warn : "") + "\n\nSame as the console: ban " + r.cidr + " " + h * 3600,
    word: "BAN", action: "BAN",
    run: async (word) => (await api.command("ban", { hosts: r.cidr, seconds: h * 3600 }, word)).message,
    done: (msg) => { note(msg || "Banned.", "ok"); onChanged(); },
  });
}

function confirmUnban(host) {
  if (isPrivate()) return;
  const label = rangeLabel(host);
  askConfirm({
    title: `UNBAN ${label}`,
    body: `Lets ${label} connect to your node again right away.\n\nSame as the console: unban ${host}`,
    word: "UNBAN", action: "UNBAN",
    run: async (word) => (await api.command("unban", { address: host }, word)).message,
    done: (msg) => { note(msg || "Unbanned.", "ok"); onChanged(); },
  });
}

// ---- wiring --------------------------------------------------------------

function wireGroup(groupId, key, attr) {
  $(groupId).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.disabled) return;
    pick[key] = Number(b.dataset[attr]);
    for (const x of $(groupId).querySelectorAll("button")) x.setAttribute("aria-checked", String(x === b));
    if (last) renderControls(last);
  });
}

export function initControls({ changed, openCommands } = {}) {
  onChanged = changed || onChanged;
  wireGroup("ban-hours", "hours", "h");

  const host = $("ban-host"), hint = $("ban-hint");
  const defaultHint = hint.textContent;
  // privacy mode: the addresses here are hidden, so nothing can be banned or unbanned until it's off
  if (isPrivate()) {
    $("ctl-private").hidden = false;
    host.disabled = true;
    $("ban-go").disabled = true;
  }
  host.addEventListener("input", () => {
    const t = host.value.trim();
    const r = t ? parseRange(t) : null;
    host.setAttribute("aria-invalid", String(!!(r && r.error)));
    setData(hint, "level", r && r.error ? "warn" : null);
    setText(hint, !r ? defaultHint : r.error ? r.error : `${r.label} · ${sizeWords(r.size)}`);
  });
  const go = () => {
    const r = parseRange(host.value);
    if (r.error) { setData(hint, "level", "warn"); setText(hint, r.error); host.focus(); return; }
    confirmBan(r.cidr);
  };
  $("ban-go").addEventListener("click", go);
  host.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  if (openCommands) $("ctl-all").addEventListener("click", () => openCommands());
}
