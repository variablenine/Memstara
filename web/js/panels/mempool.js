// H · Mempool: transactions waiting to go into a block, and how long they
// have been waiting (monerod's own age histogram).
import { $, svg, clear, setText, setData, widthOf, focusedItem, rove } from "../dom.js";
import { attachTip } from "../tooltip.js";
import * as f from "../fmt.js";

let last = null;
let answering = true;
let pollS = null;

export function initMempool() {
  window.addEventListener("resize", () => last && drawHisto(last));
}

export function renderMempool(snap) {
  const mp = snap.mempool || {};
  last = mp;
  answering = snap.source?.answering !== false;
  pollS = snap.source?.poll_s ?? null;
  setText($("mp-txs"), f.num(mp.txs));
  setText($("mp-bytes"), f.isNum(mp.bytes) ? `${f.bytes(mp.bytes)} · FEES ${f.xmr(mp.fee_total, 5)}` : "—");
  setText($("mp-oldest"), f.isNum(mp.oldest_age_s) ? f.dur(mp.oldest_age_s) : mp.txs === 0 ? "EMPTY" : "—");
  setText($("mp-10m"), f.num(mp.over_10m));
  const ds = $("mp-ds");
  setText(ds, mp.double_spends ? f.flag("warn", `${f.num(mp.double_spends)} SEEN`) : f.isNum(mp.double_spends) ? "NONE" : "—");
  ds.classList.toggle("ds-on", !!mp.double_spends);
  // a restricted port only counts transactions already passed on, so this would always read 0: unknown, not 0
  setText($("mp-nr"), snap.source?.restricted ? "—" : f.num(mp.not_relayed));
  setText($("mempool-sub"), `FROM YOUR NODE · EVERY ${pollS ?? "—"} S · WAITING TO GO INTO A BLOCK`);
  drawHisto(mp);
}

function bucketRanges(mp, n) {
  // Mirrors monerod's tx_pool.cpp: with enough transactions the first 9 bins
  // split 0..histo_98pc evenly and the last holds the oldest 2%; otherwise all
  // bins split 0..oldest evenly.
  const p98 = mp.histo_98pc || 0;
  const span = p98 > 0 ? p98 : mp.oldest_age_s || 0;
  const bins = p98 > 0 ? n - 1 : n;
  return Array.from({ length: n }, (_, i) => {
    if (p98 > 0 && i === n - 1) return [p98, null];
    return [(i * span) / bins, ((i + 1) * span) / bins];
  });
}

function drawHisto(mp) {
  const host = $("mp-histo");
  const h = Array.isArray(mp.histogram) ? mp.histogram : [];
  const width = widthOf(host.parentElement, 320);
  const height = 92;
  const m = { l: 4, r: 4, t: 14, b: 16 };
  const was = focusedItem(host);
  clear(host);
  host.setAttribute("viewBox", `0 0 ${width} ${height}`);
  if (!h.length) {
    const why = !answering ? "NODE NOT ANSWERING" : mp.txs === 0 ? "POOL · EMPTY" : f.isNum(mp.txs) ? "NOT ENOUGH TRANSACTIONS FOR A CHART" : "—";
    host.append(svg("text", { x: width / 2, y: height / 2, "text-anchor": "middle", text: why }));
    rove(host, [], null);
    return;
  }
  const iw = width - m.l - m.r, ih = height - m.t - m.b;
  const maxT = Math.max(1, ...h.map((b) => b.txs || 0));
  const slot = iw / h.length;
  const bw = Math.min(24, slot - 2);
  const ranges = bucketRanges(mp, h.length);
  host.append(svg("line", { class: "h-axis", x1: m.l, x2: m.l + iw, y1: m.t + ih + 0.5, y2: m.t + ih + 0.5 }));
  const hits = [];
  h.forEach((b, i) => {
    const x = m.l + i * slot + (slot - bw) / 2;
    const bh = ((b.txs || 0) / maxT) * ih;
    const top = m.t + ih - bh;
    const [a, z] = ranges[i];
    const head = z === null ? `WAITING OVER ${f.wait(a)}` : `WAITING ${f.wait(a)} – ${f.wait(z)}`;
    const hit = svg("rect", {
      class: "h-hit", x: m.l + i * slot, y: m.t, width: slot, height: ih, tabindex: -1, role: "img", "data-key": String(i),
      "aria-label": `${head} · ${f.plural(b.txs, "TRANSACTION", "TRANSACTIONS")} · ${f.bytes(b.bytes)}`,
    });
    hits.push(hit);
    const r = Math.min(2, bh);
    const d = bh > 0 ? `M${x},${m.t + ih}V${top + r}Q${x},${top} ${x + r},${top}H${x + bw - r}Q${x + bw},${top} ${x + bw},${top + r}V${m.t + ih}Z` : "";
    const bar = svg("path", { class: "h-bar", d });
    attachTip(hit, () => ({
      head,
      rows: [{ label: "TRANSACTIONS", value: f.num(b.txs) }, { label: "SIZE", value: f.bytes(b.bytes) }],
    }));
    host.append(hit, bar);
    if (b.txs && b.txs === maxT) host.append(svg("text", { class: "h-val", x: x + bw / 2, y: top - 3, "text-anchor": "middle", text: f.num(b.txs) }));
  });
  host.append(svg("text", { x: m.l, y: height - 3, text: "JUST ARRIVED" }));
  const lastR = ranges[ranges.length - 1];
  host.append(svg("text", { x: m.l + iw, y: height - 3, "text-anchor": "end", text: lastR[1] === null ? `OVER ${f.wait(lastR[0])}` : f.wait(lastR[1]) }));
  setData(host, "n", h.length);
  rove(host, hits, was);
}
