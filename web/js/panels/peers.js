// C · Peer scope. Each connection is a blip: the angle comes from its network
// (peers on the same company's servers stack at one angle), the distance from
// the centre from how long it has been connected. Tor and I2P peers get their
// own wedge at the top because they have no network address to place them by.
import { $, el, svg, clear, setText, setData, setAttr, focusedItem, rove } from "../dom.js";
import { attachTip } from "../tooltip.js";
import * as f from "../fmt.js";

const R = 100;
const ANON = { from: -102, to: -78 };   // degrees, 0 = right, -90 = top
// one blip per connection, kept while it lasts and moved as it ages, so a blip being
// read keeps its details (filled in again each second) and its keyboard focus
const blips = new Map();   // peer key -> {g, mark, hit, shape, p}
let drawnOnce = false;
const rowsShown = new Map();   // peer key -> its table row
let tableOrder = "";
let lastList = [];
const GRACE_S = 3;         // a reading without the connection list keeps the last one drawn this long, dimmed (as the NETWORK screen's tree)
let listAt = null;         // {list, t}: the last reading that carried the list
let limits = { out16: 5, in24: 4, low: 8 };

const keyOf = (p) => p.id || `${p.host}:${p.port}:${p.dir}`;

export function setPeerSettings(v) {
  limits = {
    out16: v?.["thresholds.out16_warn"] ?? 5,
    in24: v?.["thresholds.in24_warn"] ?? 4,
    low: v?.["thresholds.low_peers"] ?? 8,
  };
}

// FNV-1a plus a final mix (so "100.64" and "100.65" land far apart) -> [0, 1)
function hash01(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  h ^= h >>> 16; h = Math.imul(h, 0x85ebca6b);
  h ^= h >>> 13; h = Math.imul(h, 0xc2b2ae35);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

function netKey(p) {
  if (p.kind === "ipv4") return p.net16 || p.ip.split(".").slice(0, 2).join(".");
  if (p.kind === "ipv6") return (p.ip || p.host || "").split(":").slice(0, 2).join(":");
  return null;
}

function angleOf(p) {
  if (p.kind === "tor" || p.kind === "i2p") {
    const span = ANON.to - ANON.from;
    return ANON.from + 3 + hash01(p.host || p.id || "") * (span - 6);
  }
  const key = netKey(p) || p.ip || p.id || "";
  // 336 degrees outside the anon wedge
  const base = ANON.to + 4 + hash01(key) * (360 - (ANON.to - ANON.from) - 8);
  const jitter = (hash01((p.ip || "") + "j") - 0.5) * 4;
  return base + jitter;
}

const LOG_MAX = Math.log10(1 + 21600 / 60);
function radiusOf(age) {
  const a = Math.max(0, age || 0);
  const t = Math.min(1, Math.log10(1 + a / 60) / LOG_MAX);
  return R * (0.14 + 0.82 * t);
}
const ringFor = (sec) => radiusOf(sec);

const rad = (deg) => (deg * Math.PI) / 180;
const pt = (deg, r) => [r * Math.cos(rad(deg)), r * Math.sin(rad(deg))];

function arcPath(deg, r, half) {
  const [x1, y1] = pt(deg - half, r);
  const [x2, y2] = pt(deg + half, r);
  return `M${x1.toFixed(2)},${y1.toFixed(2)}A${r},${r} 0 0 1 ${x2.toFixed(2)},${y2.toFixed(2)}`;
}

let frame = null;
function buildFrame(host) {
  clear(host);
  const defs = svg("defs", {},
    svg("linearGradient", { id: "sweepgrad", x1: "0", y1: "0", x2: "1", y2: "0" },
      svg("stop", { offset: "0", "stop-color": "currentColor", "stop-opacity": "0" }),
      svg("stop", { offset: "1", "stop-color": "currentColor", "stop-opacity": ".10" })));
  host.append(defs);
  host.append(svg("circle", { class: "sc-ring-outer", r: R }));
  // anon wedge
  const [ax1, ay1] = pt(ANON.from, R), [ax2, ay2] = pt(ANON.to, R);
  host.append(svg("path", { class: "sc-tor-wedge", d: `M0,0L${ax1},${ay1}A${R},${R} 0 0 1 ${ax2},${ay2}Z` }));
  const [tx, ty] = pt(-90, R + 6);
  host.append(svg("text", { class: "sc-label", x: tx, y: ty, "text-anchor": "middle", text: "TOR · I2P" }));
  for (const [sec, label] of [[600, "10 MIN"], [3600, "1 H"], [21600, "6 H+"]]) {
    const r = ringFor(sec);
    host.append(svg("circle", { class: "sc-ring", r }));
    const [lx, ly] = pt(22, r);
    host.append(svg("text", { class: "sc-label", x: lx + 1.5, y: ly + 6, text: label }));
  }
  for (let d = 0; d < 360; d += 45) {
    const [x, y] = pt(d, R);
    host.append(svg("line", { class: "sc-spoke", x1: 0, y1: 0, x2: x, y2: y }));
  }
  const sweep = svg("path", { class: "sc-sweep", d: `M0,0L${R},0A${R},${R} 0 0 0 ${pt(-28, R)[0]},${pt(-28, R)[1]}Z` });
  sweep.style.color = "var(--accent)";
  host.append(sweep);
  const clusters = svg("g", { class: "clusters" });
  const blips = svg("g", { class: "blips" });
  host.append(clusters, blips);
  frame = { clusters, blips };
}

function groupCount(list, keyFn) {
  const m = new Map();
  for (const p of list) {
    const k = keyFn(p);
    if (!k) continue;
    const g = m.get(k) || { key: k, peers: [] };
    g.peers.push(p);
    m.set(k, g);
  }
  return [...m.values()].sort((a, b) => b.peers.length - a.peers.length);
}

export function renderPeers(snap) {
  const node = snap.node || {};
  const peers = snap.peers || {};
  const list = Array.isArray(peers.list) ? peers.list : [];
  const src = snap.source || {};
  const answering = src.answering !== false;
  // the connection list itself: missing on a restricted port, or when the node didn't give it this time
  const listKnown = answering && !src.restricted && !!peers.out_by_kind;
  // one reading without it (a call that failed, a moment without an answer) keeps the
  // last list a few seconds, dimmed, instead of emptying the scope and filling it again
  const t = f.isNum(snap.t) ? snap.t : snap.generated;
  if (listKnown) listAt = { list, t };
  const holding = !listKnown && !!listAt && f.isNum(t) && t >= listAt.t && t - listAt.t <= GRACE_S;
  const drawn = listKnown ? list : holding ? listAt.list : [];

  // counts
  const out = node.out_peers;
  const outEl = $("peers-out");
  setText(outEl, answering ? f.num(out) : "—");
  setText($("peers-want"), f.isNum(node.out_wanted) ? ` / ${node.out_wanted}` : "");
  const outLevel = !answering || !f.isNum(out) ? null : (out + (node.tor_out || 0)) === 0 ? "bad" : out < limits.low ? "warn" : null;
  setData(outEl.parentElement, "level", outLevel);
  setText($("peers-out-note"), outLevel === "bad" ? f.flag("bad", "NONE AT ALL") : outLevel === "warn" ? f.flag("warn", `FEWER THAN ${limits.low}`) : "");
  setText($("peers-in"), answering ? f.num(node.in_peers) : "—");
  // tor_out is null both when Tor sending is off and when the node can't say
  // (not answering, or a restricted port that hides the connections)
  const torEl = $("peers-tor");
  const torKnown = listKnown;
  if (!torKnown) { setText(torEl, "—"); setData(torEl.parentElement, "level", null); }
  else if (node.tor_out === null || node.tor_out === undefined) { setText(torEl, "OFF"); setData(torEl.parentElement, "level", "off"); }
  else { setText(torEl, f.num(node.tor_out)); setData(torEl.parentElement, "level", node.tor_out === 0 ? "warn" : null); }
  setText($("peers-tor-note"), torKnown && node.tor_out === 0 ? f.flag("warn", "NO TOR PEERS") : "");

  // segments against out_wanted
  const segs = $("peers-segs");
  const want = Math.min(48, Math.max(1, node.out_wanted || 12));
  if (segs.childElementCount !== want) { clear(segs); for (let i = 0; i < want; i++) segs.append(el("i")); }
  const on = Math.round(Math.min(want, (out || 0) * (want / Math.max(1, node.out_wanted || want))));
  [...segs.children].forEach((s, i) => s.classList.toggle("on", i < on));
  setData(segs, "level", outLevel);

  setText($("peers-sub"), listKnown
    ? `FROM YOUR NODE · EVERY ${src.poll_s ?? "—"} S · ${f.plural(list.length, "CONNECTION", "CONNECTIONS")}`
    : answering && !src.restricted ? "FROM YOUR NODE · CONNECTION LIST NOT READ THIS TIME" : "FROM YOUR NODE");

  // scope
  const host = $("scope");
  if (!frame) buildFrame(host);
  setData(host, "holding", holding ? "on" : null);
  const was = focusedItem(host);
  const hits = [];
  clear(frame.clusters);
  const keys = new Set();
  for (const p of drawn) {
    const key = keyOf(p);
    if (keys.has(key)) continue;   // the same connection listed twice: one blip
    keys.add(key);
    const anon = p.kind === "tor" || p.kind === "i2p";
    const shape = anon ? `tor-${p.dir}` : p.dir;
    let b = blips.get(key);
    if (b && b.shape !== shape) { b.g.remove(); b = null; }
    if (!b) {
      // a connection that came while the scope was showing flashes in
      b = { shape, p };
      b.g = svg("g", { class: "blip" + (drawnOnce ? " is-new" : "") });
      b.mark = anon ? svg("path", { class: "blip-tor" + (p.dir === "in" ? " in" : "") })
        : svg("circle", { class: p.dir === "out" ? "blip-out" : "blip-in", r: p.dir === "out" ? 3.2 : 2.8 });
      b.hit = svg("circle", { class: "blip-hit", r: 7, tabindex: -1, role: "img", "data-key": key });
      attachTip(b.hit, () => peerTip(b.p));
      b.g.append(b.mark, b.hit);
      frame.blips.append(b.g);
      blips.set(key, b);
    }
    b.p = p;
    const [x, y] = pt(angleOf(p), radiusOf(p.age_s)).map((v) => +v.toFixed(2));
    if (anon) {
      const s = 3.4;
      setAttr(b.mark, "d", `M${x},${y - s}L${x + s},${y}L${x},${y + s}L${x - s},${y}Z`);
    } else {
      setAttr(b.mark, "cx", x);
      setAttr(b.mark, "cy", y);
    }
    setAttr(b.hit, "cx", x);
    setAttr(b.hit, "cy", y);
    const where = anon ? `${String(p.kind).toUpperCase()} ${shortOnion(p.host)}` : `${p.ip}${p.port ? ":" + p.port : ""}`;
    setAttr(b.hit, "aria-label", `${p.dir === "out" ? "OUTGOING" : "INCOMING"} · ${where} · CONNECTED FOR ${f.dur(p.age_s)}`);
    hits.push(b.hit);
  }
  for (const [key, b] of blips) if (!keys.has(key)) { b.g.remove(); blips.delete(key); }
  drawnOnce = keys.size > 0;   // after an empty list, the blips coming back don't all flash in
  rove(host, hits, was);

  // clusters: outgoing by x.y network (a.b.x.x), incoming by x.y.z network (a.b.c.x)
  const outs = groupCount(drawn.filter((p) => p.dir === "out" && p.kind === "ipv4"), (p) => p.net16);
  const ins = groupCount(drawn.filter((p) => p.dir === "in" && p.kind === "ipv4"), (p) => p.net24);
  const marks = [];
  for (const g of outs) {
    const n = g.peers.length;
    if (n >= Math.max(2, limits.out16 - 1)) marks.push({ dir: "OUT", n, net: g.key, at: n >= limits.out16, angle: angleOf(g.peers[0]) });
  }
  for (const g of ins) {
    const n = g.peers.length;
    if (n >= Math.max(2, limits.in24 - 1)) marks.push({ dir: "IN", n, net: g.key, at: n >= limits.in24, angle: angleOf(g.peers[0]) });
  }
  // how far past the ring a label may run before the panel cuts it off (scope units)
  const sw = host.getBoundingClientRect().width;
  const room = sw > 0 ? R + 10 + Math.max(0, (host.parentElement.clientWidth - sw) / 2) / (sw / 220) - 2 : Infinity;
  for (const c of marks) {
    const cls = c.at ? "" : " near";
    frame.clusters.append(svg("path", { class: "cluster-arc" + cls, d: arcPath(c.angle, R + 2.5, 6) }));
    const [lx, ly] = pt(c.angle, R + 12);
    const anchor = Math.cos(rad(c.angle)) > 0.2 ? "start" : Math.cos(rad(c.angle)) < -0.2 ? "end" : "middle";
    const text = `${c.n} ${c.dir} · ${c.net}${c.dir === "OUT" ? ".x.x" : ".x"}`;
    const label = svg("text", { class: "cluster-label" + cls, x: lx, y: ly + 2, "text-anchor": anchor, text: c.at ? f.flag("warn", text) : text });
    frame.clusters.append(label);
    // slide a label that would run off the side back in
    try {
      const bb = label.getBBox();
      const shift = bb.x + bb.width > room ? room - (bb.x + bb.width) : bb.x < -room ? -room - bb.x : 0;
      if (shift) label.setAttribute("x", lx + shift);
    } catch { /* not drawn yet */ }
  }

  // biggest groups, in words
  const sp = $("spread");
  const o = outs[0], i = ins[0];
  const atLimit = (o && o.peers.length >= limits.out16) || (i && i.peers.length >= limits.in24);
  const bits = [];
  bits.push(o ? `OUT ${o.peers.length} FROM ${o.key}.x.x` : "OUT —");
  bits.push(i ? `IN ${i.peers.length} FROM ${i.key}.x` : "IN —");
  const spText = `MOST FROM ONE COMPANY'S SERVERS · ${bits.join(" · ")} · LIMITS ${limits.out16} OUT / ${limits.in24} IN`;
  setText(sp, atLimit ? f.flag("warn", "AT THE LIMIT · " + spText) : spText);
  setData(sp, "level", atLimit ? "warn" : null);

  fillTable(drawn);
}

function peerTip(p) {
  const anon = p.kind === "tor" || p.kind === "i2p";
  return {
    head: `${p.dir === "out" ? "▸ OUTGOING" : "◂ INCOMING"} · ${String(p.kind || "").toUpperCase()}`,
    rows: [
      { label: "ADDRESS", value: anon ? shortOnion(p.host) : `${p.ip}${p.port ? ":" + p.port : ""}` },
      { label: "CONNECTED FOR", value: f.dur(p.age_s) },
      { label: "ITS HEIGHT", value: f.num(p.height) },
      { label: "STATE", value: String(p.state || "—").toUpperCase() },
      { label: "DOWN / UP", value: `${f.rate(p.down_bps)} / ${f.rate(p.up_bps)}` },
      { label: "PRUNED NODE", value: p.pruned ? "YES" : "NO" },
    ],
  };
}

function shortOnion(h) {
  const s = String(h || "");
  return s.length > 22 ? s.slice(0, 10) + "…" + s.slice(-12) : s;
}

// The table view: filled only while it is open. A row per connection, kept while it
// lasts, with only the cells that changed written, so an address can be selected
// and copied; rows move only when a connection comes, goes or changes places.
function fillTable(list) {
  lastList = list;
  const table = $("peers-table");
  if (!table.closest("details").open) return;
  const tb = table.tBodies[0];
  const sorted = list.slice().sort((a, b) => (a.dir === b.dir ? (b.age_s || 0) - (a.age_s || 0) : a.dir === "out" ? -1 : 1));
  const keys = [];
  for (const p of sorted) {
    const key = keyOf(p);
    if (keys.includes(key)) continue;
    keys.push(key);
    let tr = rowsShown.get(key);
    if (!tr) {
      tr = el("tr", {}, ...Array.from({ length: 7 }, (_, i) => el("td", { class: i === 1 ? "l" : null })));
      rowsShown.set(key, tr);
    }
    const anon = p.kind === "tor" || p.kind === "i2p";
    const cells = [
      p.dir === "out" ? "▸ OUT" : "◂ IN",
      anon ? shortOnion(p.host) : `${p.ip}${p.port ? ":" + p.port : ""}`,
      f.dur(p.age_s), f.num(p.height), String(p.state || "—").toUpperCase(),
      f.rate(p.down_bps), f.rate(p.up_bps),
    ];
    cells.forEach((v, i) => setText(tr.children[i], v));
  }
  for (const [key, tr] of rowsShown) if (!keys.includes(key)) { tr.remove(); rowsShown.delete(key); }
  const order = keys.join("\n");
  if (order !== tableOrder || tb.children.length !== keys.length) {
    tableOrder = order;
    tb.append(...keys.map((k) => rowsShown.get(k)));
  }
}

export function initPeers() {
  $("peers-table").closest("details").addEventListener("toggle", () => fillTable(lastList));
}
