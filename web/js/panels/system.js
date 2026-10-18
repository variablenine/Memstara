// K · System: version, uptime, bans, peer lists, fees, limits, disk.
import { $, el, setText, setData, syncKV } from "../dom.js";
import * as f from "../fmt.js";

const TYPICAL_TX = 1500; // bytes; a normal 2-input, 2-output transaction is about this size

let diskLimits = { warn: 20, bad: 5 };
export function setSystemSettings(v) {
  diskLimits = { warn: v?.["thresholds.disk_warn_gb"] ?? 20, bad: v?.["thresholds.disk_bad_gb"] ?? 5 };
}

export function renderSystem(snap) {
  const node = snap.node || {};
  const src = snap.source || {};
  const rows = [];
  const row = (k, v, level) => rows.push([k, f.flag(level, v ?? "—"), level]);

  row("MONERO VERSION", node.version ? String(node.version) + (node.update_available ? " · UPDATE AVAILABLE" : "") : "—", node.update_available ? "warn" : null);
  row("RUNNING FOR", f.isNum(node.uptime_s) ? f.dur(node.uptime_s) : "—");
  row("STARTED", f.isNum(node.started) ? f.dayClock(node.started) : "—");
  row("NETWORK RULES", f.isNum(node.hf_version) ? `VERSION ${node.hf_version}` : "—");
  row("BLOCKED ADDRESSES", f.isNum(node.bans) ? `${f.num(node.bans)}${f.isNum(node.ban_ranges) ? ` · ${f.plural(node.ban_ranges, "RANGE", "RANGES")}` : ""}` : "—");
  row("PEERS IT KNOWS", f.isNum(node.white_peers) ? `${f.num(node.white_peers)} GOOD · ${f.num(node.grey_peers)} HEARD OF` : "—");
  row("APPS USING RPC", f.isNum(node.rpc_connections) ? `${f.num(node.rpc_connections)} · INCL. THIS DASHBOARD` : "—");
  const fees = Array.isArray(node.fee_per_byte) ? node.fee_per_byte : null;
  row("FEE FOR A TYPICAL SEND", fees && fees.length >= 2 ? `${f.xmr(fees[0] * TYPICAL_TX, 6)} LOW · ${f.xmr(fees[1] * TYPICAL_TX, 6)} NORMAL` : "—");
  row("ANSWER TIME", f.isNum(src.latency_ms) ? `${Math.round(src.latency_ms)} ms` : "—");
  syncKV($("sys-kv"), rows);
  setText($("system-sub"), `FROM YOUR NODE · EVERY ${src.poll_s ?? "—"} S · BANS AND FEES EVERY 60 S`);

  // disk: blockchain vs free, with the alert limits marked
  const disk = $("p-system").querySelector(".disk");
  const free = node.free_bytes, db = node.db_bytes;
  const bar = $("diskbar");
  const chain = bar.querySelector(".db-chain"), fr = bar.querySelector(".db-free");
  if (!f.isNum(free)) {
    limitTicks(bar, []);
    // nothing known: an empty bar, not the last reading's
    setText($("disk-text"), src.restricted ? "HIDDEN ON A RESTRICTED PORT" : "—");
    setData(disk, "level", null);
    chain.style.width = "0";
    fr.style.width = "0";
    return;
  }
  const level = free < diskLimits.bad * 1e9 ? "bad" : free < diskLimits.warn * 1e9 ? "warn" : null;
  setData(disk, "level", level);
  setText($("disk-text"), f.flag(level, `${level ? "LOW · " : ""}${f.gb(free)} FREE${f.isNum(db) ? " · BLOCKCHAIN " + f.gb(db) : ""}`));
  const total = (f.isNum(db) ? db : 0) + free;
  chain.style.width = total ? ((db || 0) / total) * 100 + "%" : "0";
  fr.style.width = total ? (free / total) * 100 + "%" : "0";
  // a limit is "this much free", so it sits that far from the right end
  limitTicks(bar, [diskLimits.warn, diskLimits.bad]
    .map((gb) => ({ gb, pos: +(100 - ((gb * 1e9) / total) * 100).toFixed(2) }))
    .filter((k) => k.pos > 0 && k.pos < 100));
}

// the alert limits marked on the disk bar, put back only when they move
function limitTicks(bar, ticks) {
  const key = ticks.map((k) => `${k.gb}@${k.pos}`).join(",");
  if (bar.dataset.ticks === key) return;
  bar.dataset.ticks = key;
  for (const t of bar.querySelectorAll(".db-limit")) t.remove();
  for (const k of ticks) {
    const tick = el("span", { class: "db-limit", title: `${k.gb} GB free` });
    tick.style.left = k.pos + "%";
    bar.append(tick);
  }
}
