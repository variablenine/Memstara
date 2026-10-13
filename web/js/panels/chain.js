// B · Chain: height and sync, the new-block ring (live), and the block tape.
import { $, el, svg, clear, setText, setData, widthOf, focusedItem, rove } from "../dom.js";
import { attachTip } from "../tooltip.js";
import * as f from "../fmt.js";

const TARGET = 120; // Monero aims for one block every 2 minutes
const NEW_MS = 10000;      // the newest bar stays lit this long after it arrives
const STAMP_OFF_S = 30;    // a miner's stamp this far from the arrival is shown beside it
let lastTop = null;
let newAt = -Infinity;     // when the newest block came in (performance.now())
let lastSnap = null;
let tableKey = "";         // the blocks the table shows
let limits = { warn: 30, bad: 60 };

// When a block came in: when your node got it, as the dashboard saw it arrive; the
// time its miner gave it when the dashboard didn't (that one can read minutes early,
// since miners date a block when they start on it). The wait before it likewise.
const atOf = (b) => (f.isNum(b.seen) ? b.seen : b.time);
const waitOf = (b) => b.seen_interval_s ?? b.interval_s;

export function setChainSettings(v) {
  limits = { warn: v?.["thresholds.no_block_warn_min"] ?? 30, bad: v?.["thresholds.no_block_bad_min"] ?? 60 };
}

export function initChain() {
  // ring ticks where 50%, 90% and 99% of blocks have arrived (exponential waits)
  const g = $("br-ticks");
  for (const p of [50, 90, 99]) {
    const a = (p / 100) * 2 * Math.PI;
    const x1 = 60 + 44 * Math.cos(a), y1 = 60 + 44 * Math.sin(a);
    const x2 = 60 + 56 * Math.cos(a), y2 = 60 + 56 * Math.sin(a);
    g.append(svg("line", { class: "br-tick", x1, y1, x2, y2 }));
  }
  attachTip($("blockring"), () => ({
    head: "NEW BLOCK",
    rows: [{ note: "The ring fills with the chance that a block would have arrived by now. Half of all blocks come within 1m 23s, 9 in 10 within 4m 36s, and 99 in 100 within 9m 13s. Ticks mark those points." }],
  }));
  window.addEventListener("resize", () => lastSnap && drawTape(lastSnap, nowGuess()));
  // the table is only filled while it is open
  $("tape-table").closest("details").addEventListener("toggle", () => lastSnap && fillTable(lastSnap.chain?.blocks || []));
}

let clockOffset = 0;
const nowGuess = () => Date.now() / 1000 + clockOffset;

export function renderChain(snap, offset) {
  clockOffset = offset;
  lastSnap = snap;
  const node = snap.node || {};
  const chain = snap.chain || {};
  const now = nowGuess();

  setText($("chain-height"), f.num(node.height));
  const meter = $("chain-meter");
  const setMeter = (level, pct) => { setData(meter, "level", level); meter.firstElementChild.style.width = pct + "%"; };
  if (snap.source?.answering === false) {
    // the last height it gave is still shown, but nothing about catching up is known
    setText($("chain-sync"), "— · NODE NOT ANSWERING");
    setMeter(null, 0);
  } else if (node.synced === true) {
    setText($("chain-sync"), "SYNCED · CAUGHT UP WITH THE NETWORK");
    setMeter(null, 100);
  } else if (node.synced === false && f.isNum(node.height) && f.isNum(node.target_height)) {
    const left = Math.max(0, node.target_height - node.height);
    setText($("chain-sync"), `CATCHING UP · ${f.pct(node.sync_pct)} · ${f.plural(left, "BLOCK", "BLOCKS")} TO GO`);
    setMeter("warn", Math.max(0, Math.min(100, node.sync_pct || 0)));
  } else {
    setText($("chain-sync"), "—");
    setMeter(null, 0);
  }

  setText($("chain-avg"), f.isNum(chain.avg_interval_s) ? `${f.wait(chain.avg_interval_s)} · AIM 2m 00s` : "—");
  const r = chain.reorg || {};
  if (f.isNum(r.deepest_24h) && r.deepest_24h > 0) {
    const last = r.last && f.isNum(r.last.time) ? ` · LAST ${f.dayClock(r.last.time)}` : "";
    setText($("chain-reorg"), `${r.deepest_24h} AT ONCE${last}`);
  } else setText($("chain-reorg"), f.isNum(r.deepest_24h) ? "NONE" : "—");
  setText($("chain-hash"), f.hashrate(node.hashrate));
  const shown = (chain.blocks || []).length;
  setText($("chain-sub"), `FROM YOUR NODE · LAST ${f.plural(shown, "BLOCK", "BLOCKS")} · NEWEST ON THE RIGHT`);

  const top = node.top_hash || node.height;
  if (lastTop !== null && top !== lastTop) {
    newAt = performance.now();
    const ring = $("blockring");
    ring.classList.remove("pulse");
    void ring.getBoundingClientRect();
    ring.classList.add("pulse");
  }
  lastTop = top;
  tickChain();
  drawTape(snap, now);
  fillTable(chain.blocks || []);
}

// Called every second so the wait counts up between polls.
export function tickChain() {
  const snap = lastSnap;
  if (!snap) return;
  const node = snap.node || {};
  const answering = snap.source?.answering !== false;
  const lb = node.last_block_seen ?? node.last_block_time;
  const age = f.isNum(lb) ? Math.max(0, nowGuess() - lb) : null;
  const ring = $("blockring");
  if (age === null || !answering) {
    setText($("br-age"), "—");
    $("br-arc").style.strokeDasharray = "0 100";
    setText($("chain-odds"), "—");
    setText($("br-note"), "AGO");
    setData(ring, "level", null);
    return;
  }
  setText($("br-age"), f.wait(age));
  const p = Math.exp(-age / TARGET);
  $("br-arc").style.strokeDasharray = `${((1 - p) * 100).toFixed(2)} 100`;
  setText($("chain-odds"), f.odds(p));
  const level = age >= limits.bad * 60 ? "bad" : age >= limits.warn * 60 ? "warn" : null;
  setData(ring, "level", level);
  setText($("br-note"), level ? `AGO · ${f.flag(level, "LATE")}` : "AGO");
  const wait = document.querySelector("#tape .t-wait");
  if (wait) setData(wait, "level", level);
}

function drawTape(snap, now) {
  const blocks = (snap.chain?.blocks || []).filter((b) => f.isNum(atOf(b)));
  const isNew = performance.now() - newAt < NEW_MS;
  const host = $("tape");
  const width = widthOf(host.parentElement, 700);
  const height = 130;
  const m = { l: 6, r: 44, t: 18, b: 18 };
  const iw = width - m.l - m.r, ih = height - m.t - m.b;
  const was = focusedItem(host);
  clear(host);
  host.setAttribute("viewBox", `0 0 ${width} ${height}`);
  if (blocks.length < 2) {
    host.append(svg("text", { x: width / 2, y: height / 2, "text-anchor": "middle", text: "WAITING FOR BLOCKS" }));
    rove(host, [], null);
    return;
  }
  const t0 = Math.min(...blocks.map(atOf));
  const t1 = Math.max(now, ...blocks.map(atOf));
  const span = Math.max(1, t1 - t0);
  const x = (t) => m.l + ((t - t0) / span) * iw;
  const maxW = Math.max(1, ...blocks.map((b) => b.weight || 0));
  const y = (w) => m.t + ih - (Math.max(0, w) / maxW) * ih;
  const bw = Math.max(2, Math.min(7, (iw / blocks.length) * 0.45));

  // grid: baseline + half
  host.append(svg("line", { class: "t-grid", x1: m.l, x2: m.l + iw, y1: m.t + ih / 2 + 0.5, y2: m.t + ih / 2 + 0.5 }));
  host.append(svg("line", { class: "t-axis", x1: m.l, x2: m.l + iw, y1: m.t + ih + 0.5, y2: m.t + ih + 0.5 }));
  host.append(svg("text", { x: m.l + iw + 6, y: m.t + 8, text: f.bytes(maxW) }));
  host.append(svg("text", { x: m.l + iw + 6, y: m.t + ih / 2 + 4, text: f.bytes(maxW / 2) }));

  // the wait for the next block, growing live
  const last = blocks[blocks.length - 1];
  const wx = x(atOf(last)) + bw / 2;
  host.append(svg("rect", { class: "t-wait", x: wx, y: m.t, width: Math.max(0, m.l + iw - wx), height: ih }));
  host.append(svg("line", { class: "t-now", x1: m.l + iw + 0.5, x2: m.l + iw + 0.5, y1: m.t - 4, y2: m.t + ih }));

  const label = `${f.plural(blocks.length, "BLOCK", "BLOCKS")} · ${f.dur(span)}`;
  host.append(svg("text", { class: "t-label", x: m.l, y: 10, text: label }));
  host.append(svg("text", { x: m.l + iw, y: 10, "text-anchor": "end", text: "NOW" }));
  host.append(svg("text", { x: m.l, y: height - 3, text: f.clock(t0, false) }));
  host.append(svg("text", { x: m.l + iw, y: height - 3, "text-anchor": "end", text: f.clock(now, false) }));

  const hits = [];
  blocks.forEach((b, i) => {
    const bx = x(atOf(b)) - bw / 2;
    const by = y(b.weight || 0);
    const h = Math.max(1, m.t + ih - by);
    const came = f.isNum(b.seen) ? `ARRIVED ${f.dayClock(b.seen)}` : `MINER'S TIME ${f.dayClock(b.time)}`;
    const hit = svg("rect", {
      class: "t-hit", x: bx - 3, y: m.t, width: bw + 6, height: ih, tabindex: -1, role: "img", "data-key": String(b.height),
      "aria-label": `BLOCK ${f.num(b.height)} · ${came} · ${f.plural(b.txs, "TRANSACTION", "TRANSACTIONS")} · ${f.bytes(b.weight)}`,
    });
    hits.push(hit);
    const r = 2;
    const top = m.t + ih - h;
    // rounded data end, square at the baseline
    const d = h > r
      ? `M${bx},${m.t + ih}V${top + r}Q${bx},${top} ${bx + r},${top}H${bx + bw - r}Q${bx + bw},${top} ${bx + bw},${top + r}V${m.t + ih}Z`
      : `M${bx},${m.t + ih}V${top}H${bx + bw}V${m.t + ih}Z`;
    const bar = svg("path", { class: "t-bar" + (isNew && i === blocks.length - 1 ? " is-new" : ""), d });
    attachTip(hit, () => ({ head: `BLOCK ${f.num(b.height)}`, rows: blockRows(b) }));
    host.append(hit, bar);
  });
  rove(host, hits, was, { start: "last" });
}

// a block's details: when it arrived, and its miner's own time when that reads far from it
function blockRows(b) {
  const now = nowGuess();
  const when = (t) => `${f.dayClock(t)} · ${f.wait(Math.max(0, now - t))} AGO`;
  const off = f.isNum(b.seen) && f.isNum(b.time) ? b.time - b.seen : null;
  const far = f.isNum(off) && Math.abs(off) > STAMP_OFF_S;
  const iv = waitOf(b);
  const rows = [];
  if (f.isNum(b.seen)) rows.push({ label: "ARRIVED", value: when(b.seen) });
  if (!f.isNum(b.seen) || far) rows.push({ label: "MINER'S TIME", value: f.isNum(b.time) ? when(b.time) : "—" });
  rows.push(
    { label: "WAIT BEFORE IT", value: f.isNum(iv) ? (iv < 0 ? "EARLIER STAMP" : f.wait(iv)) : "—" },
    { label: "TRANSACTIONS", value: f.num(b.txs) },
    { label: "SIZE", value: f.bytes(b.weight) },
    { label: "REWARD", value: f.xmr(b.reward) },
  );
  if (!f.isNum(b.seen)) {
    rows.push({ note: "The dashboard didn't see this block arrive (it came before the dashboard was watching, or with several at once), so its time is the one its miner gave it. Miners date a block when they start on it, so it can read minutes early." });
  } else if (far && off < 0) {
    rows.push({ note: "Miners date a block when they start on it, so its own time can read minutes early." });
  } else if (far) {
    rows.push({ note: "Its miner's clock was ahead, so its own time reads later than when it arrived." });
  }
  return rows;
}

// the table view: filled only while it is open, and again only when the blocks change,
// so a height or a time can be selected and copied
function fillTable(blocks) {
  const table = $("tape-table");
  if (!table.closest("details").open) { tableKey = ""; return; }
  const key = f.clock(0) + JSON.stringify(blocks.map((b) => [b.height, b.seen, b.time, b.seen_interval_s ?? b.interval_s, b.txs]));
  if (key === tableKey) return;
  tableKey = key;
  const tb = clear(table.tBodies[0]);
  for (const b of blocks.slice().reverse()) {
    const iv = waitOf(b);
    tb.append(el("tr", {},
      el("td", { text: f.num(b.height) }),
      el("td", { text: f.isNum(b.seen) ? f.dayClock(b.seen) : f.isNum(b.time) ? `${f.dayClock(b.time)} (MINER'S)` : "—" }),
      el("td", { text: f.isNum(iv) ? f.wait(Math.max(0, iv)) : "—" }),
      el("td", { text: f.num(b.txs) }),
      el("td", { text: f.bytes(b.weight) }),
      el("td", { text: f.xmr(b.reward) })));
  }
}
