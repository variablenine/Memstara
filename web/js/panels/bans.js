// E · Ban list: what your node is banning right now (monerod's get_bans, the
// console's `bans`), the ranges at the one-company limits (too many
// connections from one company's servers) and the MRL spy node list. Read-only:
// Memstara can't ban or unban. monerod's own console can (`ban` and `unban`).
import { $, el, setText, setData } from "../dom.js";
import * as f from "../fmt.js";
import { hiddenRange } from "../privacy.js";

let limits = { out16: 5, in24: 4 };

export function setBanSettings(v) {
  limits = { out16: v?.["thresholds.out16_warn"] ?? 5, in24: v?.["thresholds.in24_warn"] ?? 4 };
}

// "203.0.113.0/24" -> "203.0.113.x", "203.0.0.0/16" -> "203.0.x.x"
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
// the BAN CLOCK's highlight on a row survives every snapshot. Only text that
// changed is rewritten; rows come and go only when their host does.
const updaters = new WeakMap();   // row -> its update(item)

function syncRows(ul, items, make) {
  const old = new Map();
  for (const li of ul.children) old.set(li.dataset.key, li);
  const want = items.map((it) => {
    const li = old.get(it.key) || make(it);
    li.dataset.key = it.key;
    updaters.get(li)(it);
    return li;
  });
  const keep = new Set(want);
  for (const li of [...ul.children]) if (!keep.has(li)) li.remove();
  want.forEach((li, i) => { if (ul.children[i] !== li) ul.insertBefore(li, ul.children[i] || null); });
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
  const host = el("span", { class: "cl-host" }, el("span", { class: "cl-glyph", "aria-hidden": "true", text: "▲ " }), label);
  const why = el("span", { class: "cl-why" });
  const li = el("li", { "data-level": "warn" }, host, why);
  updaters.set(li, (g) => {
    li.dataset.host = g.host;
    setText(label, rangeLabel(g.host));
    setText(why, `AT THE LIMIT · ${g.why}`);
  });
  return li;
}

function banRow() {
  const host = el("span", { class: "cl-host" });
  const why = el("span", { class: "cl-why" });
  const li = el("li", {}, host, why);
  updaters.set(li, (b) => {
    li.dataset.host = b.host;
    setText(host, rangeLabel(b.host));
    setText(why, `${f.dur(b.seconds)} LEFT${b.stale ? " · AS LAST READ" : ""}`);
  });
  return li;
}

export function renderBans(snap) {
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

  renderSpy(snap);
}

// The MRL spy node list (memstara/spylist.py): the bundled list, how much of it the node
// bans (get_bans), and how many of its peers are on it now (get_connections)
function renderSpy(snap) {
  const sp = snap.peers?.spy;
  const live = snap.source?.answering !== false && !snap.source?.restricted;
  const put = (id, text, level = null) => { const d = $(id); setText(d, text); setData(d, "level", level); };
  if (!sp) {
    put("ban-spy-list", "—"); put("ban-spy-bans", "—"); put("ban-spy-now", "—");
    return;
  }
  if (sp.state !== "ok") {
    put("ban-spy-list", f.flag("warn", sp.state === "changed" ? "CHANGED · NOT USED" : "MISSING · NOT USED"), "warn");
    put("ban-spy-bans", "—", "off"); put("ban-spy-now", "—", "off");
    return;
  }
  put("ban-spy-list", `VERSION ${sp.version} · ${sp.date} · ${f.plural(sp.entries, "ENTRY", "ENTRIES")} (${f.plural(sp.ranges, "RANGE", "RANGES")})`);
  const why = snap.source?.restricted ? "NOT ON A RESTRICTED PORT" : "—";
  if (f.isNum(sp.node_bans)) put("ban-spy-bans", `${f.num(sp.node_bans)} OF ${f.num(sp.entries)}${sp.node_bans >= sp.entries ? " · ALL OF IT" : ""}`);
  else put("ban-spy-bans", why, snap.source?.restricted ? "off" : null);
  if (live && f.isNum(sp.out) && f.isNum(sp.in)) {
    const n = sp.out + sp.in;
    const bits = [sp.out ? `${sp.out} OUT` : "", sp.in ? `${sp.in} IN` : ""].filter(Boolean).join(" · ");
    put("ban-spy-now", n ? f.flag("warn", `${f.plural(n, "PEER", "PEERS")} (${bits})`) : "NONE", n ? "warn" : null);
  } else put("ban-spy-now", why, snap.source?.restricted ? "off" : null);
}

export function initBans({ openCommands } = {}) {
  if (openCommands) $("ban-all").addEventListener("click", () => openCommands());
}
