// D · Route: a line diagram of how the node is connected: other nodes, the
// node, Tor sending and this dashboard. Parts the dashboard can't see are
// drawn dashed.
import { $, svg, clear, syncKV } from "../dom.js";
import * as f from "../fmt.js";
import { cut } from "../privacy.js";

const W = 320;
const FLOW_S = 1.6;      // one run of the dashes along a link (panels.css .m-flow)
let drawn = "";          // the diagram on show, as markup: drawn again only when it changes

function box(x, y, w, h, title, text, level, extraCls = "") {
  const g = svg("g", { class: "m-group", "data-level": level || "ok" });
  g.append(svg("rect", { class: `m-box ${extraCls}`, x, y, width: w, height: h, rx: 2, "data-level": level || "ok" }));
  g.append(svg("text", { class: "m-title", x: x + w / 2, y: y + 13, "text-anchor": "middle", text: fit(title, Math.floor(w / 6.5)) }));
  g.append(svg("text", { class: "m-text", x: x + w / 2, y: y + h - 7, "text-anchor": "middle", text: fit(f.flag(level, text), Math.floor(w / 5)) }));
  return g;
}

// shortened to n characters as drawn (privacy mode's hidden details count as their glyphs, never cut)
function fit(s, n) {
  return cut(s, n);
}

function link(d, level, label, lx, ly, anchor = "start") {
  const g = svg("g");
  g.append(svg("path", { class: "m-link", d, "data-level": level }));
  g.append(svg("path", { class: "m-flow", d, "data-level": level }));
  if (label) g.append(svg("text", { class: "m-linklabel", x: lx, y: ly, "text-anchor": anchor, text: label }));
  return g;
}

export function renderRoute(snap) {
  const node = snap.node || {};
  const src = snap.source || {};
  const answering = src.answering !== false;
  const alertIds = new Set((snap.alerts || []).map((a) => a.id));

  // a restricted port hides the connection counts: unknown, not zero
  const counted = answering && !src.restricted && f.isNum(node.out_peers);
  const outTotal = counted ? node.out_peers + (node.tor_out || 0) : null;
  const pLevel = !counted ? "off" : outTotal === 0 ? "bad" : alertIds.has("peers") ? "warn" : "ok";

  const inKnown = answering && !src.restricted && f.isNum(node.in_peers);
  const inN = inKnown ? node.in_peers : null;

  const counts = `▸ ${counted ? f.num(node.out_peers) : "—"} OUT  ◂ ${inKnown ? f.num(inN) : "—"} IN`;
  const nLevel = answering ? (snap.level === "bad" ? "warn" : "ok") : "bad";
  const nText = answering ? `OUT ${counted ? f.num(node.out_peers) : "—"} · IN ${inKnown ? f.num(inN) : "—"}${node.synced === false ? " · SYNCING" : ""}` : "NOT ANSWERING";

  // tor_out is null both when Tor sending is off and when the node can't say (a
  // restricted port, or no connection list this time)
  const torKnown = answering && !src.restricted && !!snap.peers?.out_by_kind;
  const torOn = torKnown && node.tor_out !== null && node.tor_out !== undefined;
  const sendLevel = !torOn ? "off" : node.tor_out === 0 ? "warn" : "ok";
  const tText = !torKnown ? "SENDING —" : torOn ? `SENDING · ${f.plural(node.tor_out, "PEER", "PEERS")}` : "SENDING OFF";

  // bottom row: dashboard and Tor
  const below = [
    { title: "DASHBOARD", text: location.host || "—", level: "ok", linkLevel: answering ? "ok" : "bad", linkLabel: f.isNum(src.latency_ms) ? `RPC ${Math.round(src.latency_ms)} ms` : "RPC" },
    { title: "TOR", text: tText, level: sendLevel, linkLevel: sendLevel },
  ];

  const pic = svg("g");
  const nodeY = 80;
  const rowY = nodeY + 72;
  const viewBox = `0 0 ${W} ${rowY + 54}`;

  // links first so boxes sit on top
  pic.append(link(`M160,32V${nodeY}`, pLevel, `${counts}${inKnown && inN === 0 ? " · NONE COMING IN" : ""}`, 166, 58));
  const n = below.length;
  const gap = 8, bw = (W - 8 - gap * (n - 1)) / n;
  below.forEach((b, i) => {
    const x = 4 + i * (bw + gap);
    const cx = x + bw / 2;
    const sx = Math.max(95, Math.min(225, cx));   // leave from the node's bottom edge
    const midY = nodeY + 38 + 16;
    pic.append(link(`M${sx.toFixed(1)},${nodeY + 38}V${midY}H${cx.toFixed(1)}V${rowY}`, b.linkLevel || b.level, b.linkLabel || "", cx + 5, rowY - 5));
  });

  pic.append(box(95, 2, 130, 30, "OTHER NODES", pLevel === "bad" ? "NO CONNECTIONS" : "THE MONERO NETWORK", pLevel));
  pic.append(box(85, nodeY, 150, 38, "YOUR NODE", nText, nLevel, "node"));
  below.forEach((b, i) => {
    const x = 4 + i * (bw + gap);
    pic.append(box(x, rowY, bw, 34, b.title, b.text, b.level));
  });

  // Drawn again only when something in it changed (a count, the answer time). The new
  // links' dashes pick up where all of them are in their run, so they keep flowing
  // instead of jumping back to the start.
  const markup = viewBox + pic.innerHTML;
  const svgEl = $("mimic");
  if (markup !== drawn || !svgEl.firstChild) {
    drawn = markup;
    svgEl.setAttribute("viewBox", viewBox);
    const lag = `${-Math.round(performance.now() % (FLOW_S * 1000))}ms`;
    for (const p of pic.querySelectorAll(".m-flow")) p.style.animationDelay = lag;
    clear(svgEl).append(...pic.childNodes);
  }

  // rows under the diagram
  const kvRows = [];
  const row = (k, v, level) => kvRows.push([k, f.flag(level, v), level]);
  row("COMING IN", inKnown ? f.num(inN) : "—", inN ? null : "off");
  row("TOR SENDING", !torKnown ? "—" : torOn ? f.plural(node.tor_out, "PEER", "PEERS") : "OFF", sendLevel === "ok" ? null : sendLevel);
  syncKV($("route-kv"), kvRows);
}
