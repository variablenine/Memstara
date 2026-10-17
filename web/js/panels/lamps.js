// A · Lamp wall (annunciator). Lit with ▲ = heads-up, ■ = problem (each in its own colour). A newly lit
// lamp flashes until acknowledged. Acknowledging only stops the flashing on
// this screen; it changes nothing on the node.
import { $, el, clear, setText, setData } from "../dom.js";
import { attachTip } from "../tooltip.js";
import * as f from "../fmt.js";

const acked = new Map();   // lamp id -> the `since` value that was acknowledged
let lamps = [];
let nodes = new Map();
let settingsRef = {};
let wanted = { n: null, from: null };   // the outgoing count the node aims for (snapshot)
const WANTED_FROM = { setting: "your setting", node: "the node's --out-peers", default: "monerod's default" };

// What each lamp watches, in the current limits' words.
export function describe(id) {
  const v = settingsRef;
  const g = (k, d) => (v && v[k] !== undefined ? v[k] : d);
  switch (id) {
    case "node": return `Whether your node answers the dashboard. Problem after ${g("grace.node_down_min", 5)} min without an answer.`;
    case "peers": return `Outgoing connections. Heads-up under ${g("thresholds.low_peers", 8)}${wanted.n !== null ? ` of ${wanted.n} (${WANTED_FROM[wanted.from] || "wanted"})` : ""} for ${g("grace.peers_min", 10)} min; problem at 0 for ${g("grace.no_peers_min", 5)} min.`;
    case "sync": return `Heads-up when the node is still catching up after ${g("grace.syncing_min", 20)} min.`;
    case "block": return `Heads-up after ${g("thresholds.no_block_warn_min", 30)} min with no new block, problem after ${g("thresholds.no_block_bad_min", 60)} min.`;
    case "disk": return `Free space where the blockchain lives. Heads-up under ${g("thresholds.disk_warn_gb", 20)} GB, problem under ${g("thresholds.disk_bad_gb", 5)} GB.`;
    case "reorg": return `Blocks replaced at once in the last 24 h (a "reorg"). Heads-up at ${g("thresholds.reorg_warn", 3)}, problem at ${g("thresholds.reorg_bad", 10)}. 1 or 2 happen normally.`;
    case "double_spend": return "Someone tried to spend the same coins twice. Stays lit for 30 min.";
    case "restarts": return `${g("thresholds.restart_warn", 3)} or more node restarts within an hour. An update or a network change normally causes one.`;
    case "spread_out": return `${g("thresholds.out16_warn", 5)} or more outgoing connections on one company's servers for ${g("grace.spread_min", 10)} min (a possible spy cluster).`;
    case "spread_in": return `${g("thresholds.in24_warn", 4)} or more incoming connections from one company's servers for ${g("grace.spread_min", 10)} min.`;
    case "tor": return `Tor sending is on but your node has had no Tor connections for ${g("grace.tor_min", 10)} min. Transactions you send wait until one connects.`;
    case "flood": return `Black marble watch: what a new transaction's 16-member ring works like if one sender made the recent surge of new outputs (CHAIN screen, L). Heads-up under ${g("thresholds.flood_warn", 11)}, problem under ${g("thresholds.flood_bad", 8)}, for ${g("grace.flood_min", 30)} min. It can't tell a flood from a busy spell of ordinary use.`;
    default: return "";   // add-on lamps bring their own help text
  }
}

function lit(l) {
  return l.state === "warn" || l.state === "bad";
}

const STATE_WORD = { ok: "OK", warn: "HEADS-UP", bad: "PROBLEM", off: "OFF OR NOT KNOWN" };

export function setLampSettings(values) {
  settingsRef = values || {};
}

function build(list) {
  const ol = clear($("lamps"));
  nodes = new Map();
  list.forEach((l, i) => {
    // a button: clicking it (or Enter / Space) acknowledges it
    const node = el("li", { class: "lamp", tabindex: 0, role: "button", "data-id": l.id },
      el("span", { class: "lamp-no", text: String(i + 1).padStart(2, "0") }),
      el("span", { class: "lamp-dot", "aria-hidden": "true" }),
      el("span", { class: "lamp-label", text: l.label }),
      el("span", { class: "lamp-value", text: l.value || "" }));
    node.addEventListener("click", () => ackOne(l.id));
    node.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      ackOne(l.id);
    });
    attachTip(node, () => {
      const cur = lamps.find((x) => x.id === l.id) || l;
      const word = { ...STATE_WORD, ok: "NORMAL" }[cur.state] || cur.state;
      const rows = [{ label: "STATE", value: word }, { label: "READING", value: cur.value || "—" }];
      if (f.isNum(cur.since)) rows.push({ label: "LIT SINCE", value: f.dayClock(cur.since) });
      rows.push({ note: cur.help || describe(cur.id) });
      if (cur.addon) rows.push({ label: "FROM", value: `ADD-ON · ${String(cur.addon).toUpperCase()}` });
      return { head: `${String(i + 1).padStart(2, "0")} · ${cur.label}`, rows };
    });
    nodes.set(l.id, node);
    ol.append(node);
  });
}

function ackOne(id) {
  const l = lamps.find((x) => x.id === id);
  if (l && lit(l)) acked.set(id, l.since ?? true);
  paint();
}

export function ackAll() {
  for (const l of lamps) if (lit(l)) acked.set(l.id, l.since ?? true);
  paint();
}

export function lampTest() {
  const ol = $("lamps");
  ol.classList.add("testing");
  setTimeout(() => ol.classList.remove("testing"), 2000);
}

function paint() {
  let first = null;
  for (const l of lamps) {
    if (lit(l) && f.isNum(l.since) && (!first || l.since < first.since)) first = l;
  }
  const litCount = lamps.filter(lit).length;
  let litN = 0, badN = 0, warnN = 0, offN = 0, unack = 0;
  for (const l of lamps) {
    const n = nodes.get(l.id);
    if (!n) continue;
    setData(n, "state", l.state);
    setText(n.querySelector(".lamp-value"), l.value || (l.state === "off" ? "OFF" : "—"));
    // lit lamps carry ▲ / ■, so heads-up and problem aren't told apart by colour alone
    setText(n.querySelector(".lamp-dot"), f.flag(l.state, "").trim());
    const isLit = lit(l);
    if (!isLit) acked.delete(l.id);
    const isAcked = isLit && acked.has(l.id) && acked.get(l.id) === (l.since ?? true);
    setData(n, "flash", isLit && !isAcked ? "on" : null);
    setData(n, "first", first && first.id === l.id && litCount > 1 ? "on" : null);
    const word = STATE_WORD[l.state] || String(l.state || "").toUpperCase();
    // set only when it changed: a screen reader reads a focused lamp's label out again when it's set
    const label = `${l.label} · ${l.value || "—"} · ${word}${isLit && !isAcked ? " · FLASHING, PRESS TO ACKNOWLEDGE" : ""}`;
    if (n.getAttribute("aria-label") !== label) n.setAttribute("aria-label", label);
    if (isLit) litN++;
    if (l.state === "bad") badN++;
    if (l.state === "warn") warnN++;
    if (l.state === "off") offN++;
    if (isLit && !isAcked) unack++;
  }
  const parts = [`LIT ${litN} / ${lamps.length}`, `PROBLEMS ${badN}`, `HEADS-UPS ${warnN}`];
  if (offN) parts.push(`OFF ${offN}`);
  parts.push(unack ? `NOT ACKNOWLEDGED ${unack}` : "ALL ACKNOWLEDGED");
  setText($("lamps-foot"), parts.join(" · "));
  setText($("lamps-sub"), `${f.plural(lamps.length, "LAMP", "LAMPS")} · CLICK ONE OR ACK TO STOP FLASHING`);
}

export function renderLamps(snap) {
  const list = Array.isArray(snap.lamps) ? snap.lamps : [];
  const n = snap.node?.out_wanted;
  wanted = { n: Number.isFinite(n) ? n : null, from: snap.node?.out_wanted_from || null };
  const ids = list.map((l) => l.id).join(",");
  if (ids !== lamps.map((l) => l.id).join(",")) build(list);
  lamps = list;
  paint();
}

export function initLamps() {
  $("lamp-ack").addEventListener("click", ackAll);
  $("lamp-test").addEventListener("click", lampTest);
}
