// NETWORK screen hero: the node tree. Your node at the top; under it the
// three kinds of connection (outgoing, incoming, Tor); under those, peers on
// the same company's servers share a branch; every peer is a leaf. It turns
// slowly and can be dragged, pinched and zoomed.
//
// The chatter is real: each dot running along a branch is data the node
// actually moved with that peer over the last few seconds (monerod's per-peer
// byte counters in get_connections), toward your node for data coming in and
// away for data going out, and the branch it runs along flickers as it goes.
// Peers moving data are lit and breathe; quiet ones sit dim. A connection is
// drawn once it has lasted a few seconds (probes and failed handshakes come
// and go in one or two). Peers joining and leaving, new blocks, bans, the pool
// and, every ten seconds, the busiest talkers show in the LIVE CHATTER log
// beside it, newest at the bottom: with a reading every second or two each
// line comes out as it happens; with readings further apart, one by one over
// the time until the next.
// When a reading has no peer list (node not answering, a port that hides it,
// one failed call) nothing is guessed: counts read "—", the last list stays a
// few seconds, quiet and dimmed (so one missed reading doesn't fold the tree
// away and grow it back), then the tree shows only your node, and the log
// says nothing about peers until the list is back.
import { el, setText, setData } from "../dom.js";
import * as f from "../fmt.js";
import { hiddenRange } from "../privacy.js";
import { createStage, createCamera, attachOrbit, glowSprite, stamp, pick, moving, clamp, cssRGB, TAU } from "../fx/space.js";

const KIND_RGB = {           // edge and leaf colours by kind of connection
  out: null,                 // the accent (set from the look)
  in: "138, 120, 236",       // violet, the incoming series colour everywhere else
  tor: "233, 223, 198",      // bone; Tor leaves are diamonds too, so colour is never the only cue
};
const DATA_IN = "244, 244, 252";   // data arriving at your node
const DATA_OUT = "110, 216, 236";  // data your node sends
const BRANCH_ANGLE = { out: 90, in: 210, tor: 330 };
const BRANCH_NAME = { out: "OUTGOING", in: "INCOMING", tor: "TOR · I2P" };
const FEED_KEEP = 60;          // lines kept in the chatter log
const TALKERS = 5;            // busiest peers listed each time
const TALK_EVERY_S = 10;      // they're listed this often, by what they moved in that time
const TALK_MIN = 4096;        // bytes a peer must have moved in that time to be listed
const RATE_S = 5;             // a peer's rate is over about this long: its traffic comes in bursts
const SETTLE_S = 5;           // a connection is drawn (and its JOIN said) once it has lasted this long
const GRACE_S = 3;            // after the last reading with a peer list, it stays drawn this long
const HELD_ALPHA = 0.45;      // how bright that held list is drawn
const SPIN = 0.07;            // radians a second
const IDLE_RESUME_S = 8;      // spinning picks up again this long after the last drag
const DOT_CAP = 320;          // moving dots at most; past it every peer's share shrinks alike
const GAP_READINGS = 2.5;     // a reading this many poll intervals after the last one with a list is "after a gap"
const GAP_MIN_S = 10;         // and at least this long after it (one slow reading isn't a gap)
const SUMMARY_OVER = 4;       // more new blocks than this at once are one line; so are joins (or leaves) after a gap (three times as many otherwise)

const rad = (deg) => (deg * Math.PI) / 180;
// a reading carries a real peer list only when get_connections answered: the
// server sends out_by_kind with every real list, even an empty one, and null
// when the node isn't answering, the port hides the list or the call failed
const peerListKnown = (snap) => !!snap?.peers?.out_by_kind && Array.isArray(snap.peers.list);
const peerId = (p) => `p:${p.id || p.host}`;
// lasted long enough to be drawn (with a reading a second, connections that live a
// second or two would otherwise pop in and out and push their neighbours about)
const settled = (p) => !(f.isNum(p.age_s) && p.age_s < SETTLE_S);

function shortHost(p) {
  if (p.kind === "tor" || p.kind === "i2p") {
    const h = String(p.host || "");
    return h.length > 16 ? `${h.slice(0, 8)}…${h.slice(-10)}` : h || (p.kind === "tor" ? "TOR PEER" : "I2P PEER");
  }
  return p.ip || p.host || "?";
}
const branchOf = (p) => (p.kind === "tor" || p.kind === "i2p" ? "tor" : p.dir === "in" ? "in" : "out");
// a ban's host in plain words: 62.233.57.0/24 -> 62.233.57.x
function plainHost(h) {
  const hidden = hiddenRange(h);   // privacy mode: "⟦n24_..⟧.0/24" -> "⟦n24_..⟧.x"
  if (hidden) return hidden.text;
  const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/.exec(String(h || ""));
  if (!m) return String(h || "?");
  const bits = Number(m[5]);
  if (bits === 24) return `${m[1]}.${m[2]}.${m[3]}.x`;
  if (bits === 16) return `${m[1]}.${m[2]}.x.x`;
  if (bits === 8) return `${m[1]}.x.x.x`;
  return String(h);
}
// a steady 0..1 number per id (so each lit peer breathes at its own pace)
function hash01(id) {
  let x = 2166136261;
  for (let i = 0; i < id.length; i++) { x ^= id.charCodeAt(i); x = Math.imul(x, 16777619); }
  return ((x >>> 0) % 1000) / 1000;
}

export default function create(body, env) {
  // ---- DOM: the picture, its corner readouts, the selected-node card, the chatter list
  const canvas = el("canvas", {
    class: "nt-canvas", tabindex: "0", role: "img",
    "aria-label": "Node tree: your node, the kinds of connection under it, and one leaf per connected peer. Arrow keys step through them; Shift and the arrow keys turn it; plus and minus zoom.",
  });
  const hudTL = el("div", { class: "hero-hud nt-hud-tl" });
  const hudBL = el("div", { class: "hero-hud nt-legend" },
    el("span", { class: "nt-lg nt-lg-out", text: "OUTGOING" }),
    el("span", { class: "nt-lg nt-lg-in", text: "INCOMING" }),
    el("span", { class: "nt-lg nt-lg-tor", text: "TOR" }),
    el("span", { class: "nt-lg nt-lg-down", text: "DATA IN" }),
    el("span", { class: "nt-lg nt-lg-up", text: "DATA OUT" }),
    el("span", { class: "nt-lg nt-lg-block", text: "NEW BLOCK" }));
  const hint = el("div", { class: "hero-hud nt-hint", text: "DRAG TO TURN · CLICK, THEN SCROLL TO ZOOM · TAP A DOT" });
  // the picked node's card is built once; each reading only changes its text,
  // so keyboard focus on × and a screen reader's place in it survive
  const cardHead = el("div", { class: "nt-card-head" });
  const cardList = el("dl", { class: "kv" });
  const cardNotes = el("div", { class: "nt-card-notes" });
  const cardX = el("button", { class: "key nt-card-x", type: "button", text: "×", "aria-label": "Close" });
  cardX.addEventListener("click", () => { choose(null); canvas.focus(); });
  const card = el("div", { class: "nt-card", hidden: true }, cardHead, cardList, cardNotes, cardX);
  const empty = el("div", { class: "nt-empty", hidden: true });
  const hudConn = el("b", { text: "—" });
  const hudKinds = el("b", { text: "—" });
  const hudRates = el("b", { text: "—" });
  hudTL.append(
    el("div", { class: "nt-hud-big" }, el("span", { text: "CONNECTIONS" }), hudConn),
    el("div", { class: "nt-hud-row" }, el("span", { text: "OUT · IN · TOR" }), hudKinds),
    el("div", { class: "nt-hud-row" }, el("span", { text: "DOWN · UP" }), hudRates));
  const view = el("div", { class: "nt-view" }, canvas, hudTL, hudBL, hint, card, empty);
  const feedList = el("ol", { class: "nt-feed-list" });
  const cursor = el("li", { class: "nt-cursor", "aria-hidden": "true" }, el("span", { class: "nt-cursor-b" }));
  const feedEmpty = el("li", { class: "nt-feed-empty", text: "LISTENING · PEERS, DATA, BLOCKS AND BANS SHOW HERE AS THEY HAPPEN" });
  feedList.append(feedEmpty, cursor);
  const feed = el("aside", { class: "nt-feed", "aria-label": "Live chatter, newest at the bottom" },
    el("div", { class: "nt-feed-head" }, el("span", { text: "LIVE CHATTER" }), el("span", { class: "nt-feed-sub", text: "FROM YOUR NODE · NEWEST AT THE BOTTOM" })),
    el("div", { class: "nt-feed-scroll" }, feedList));
  const say = el("p", { class: "sr-only", "aria-live": "polite" });
  body.append(el("div", { class: "nt" }, view, feed), say);

  // header tools: spin, zoom, reset
  const spinBtn = el("button", { class: "key", type: "button", "aria-pressed": "true", text: "SPIN", title: "Turn slowly on its own" });
  const zoomIn = el("button", { class: "key", type: "button", text: "+", title: "Zoom in", "aria-label": "Zoom in" });
  const zoomOut = el("button", { class: "key", type: "button", text: "−", title: "Zoom out", "aria-label": "Zoom out" });
  const resetBtn = el("button", { class: "key", type: "button", text: "RESET", title: "Put the view back" });
  if (env.tools) env.tools.append(spinBtn, zoomOut, zoomIn, resetBtn);

  // ---- state
  let look = env.look();
  let shown = false;
  let spin = true;
  let lastTouch = -1e9;       // when someone last turned it (seconds, stage clock)
  let clockT = 0;
  const nodes = new Map();    // id -> node
  let order = [];             // drawing / stepping order
  let leaves = [];            // the peers' leaves, in that order
  let spawnFrom = 0;          // where this frame's round of new dots starts
  let particles = [];
  let samples = new Map();    // peer id -> its byte counters at the last few readings, [{t, recv, sent}]
  let prevHeight = null;
  let prevBans = null;        // ban hosts at the last reading that had a ban list
  let knownPeers = null;      // peer id -> drawn peer at the last reading that had a peer list
  let knownAt = null;         // when that reading was
  let listSnap = null;        // and that reading
  let listKnown = false;      // does the latest reading carry a peer list?
  let holding = false;        // it doesn't, but the last list is still drawn for a moment
  let lastT = null;           // when the last reading taken in was (the server's clock, seconds)
  let pollS = 5;              // seconds between readings
  let prevAnswering = null;
  let selected = null;
  let hovered = null;
  let tipAt = null;           // where the hovered node's tooltip was put (clientX, clientY)
  let lastSnap = null;
  let poolSaid = null;          // the pool count the log last mentioned
  let talkSums = new Map();     // peer id -> {p, dr, ds}: bytes it moved since talkFrom
  let talkFrom = null;          // when the busiest talkers were last listed
  let topTalkers = new Set();   // the leaves listed then get a name tag
  let warned = { out: new Map(), in: new Map() };   // ip -> the ECLIPSE WATCH alert naming it
  let warnRGB = cssRGB("--warn-rgb", "255, 112, 64");
  let hudBoxes = null;          // the corner readouts' boxes on the canvas, measured when they change
  const queue = [];             // chatter lines waiting to come out
  let dripTimer = 0;
  let dripGap = 0.8;

  // home view: the three kinds of connection spread across the width, 120°
  // apart on screen (outgoing left, incoming right, Tor in front and below your
  // node, so no label sits on another), pulled back far enough that the lowest
  // leaves stay clear of the legend as it turns
  const cam = createCamera({ yaw: 2.1, pitch: 0.36, dist: 13, fov: 0.8, target: [0, -0.3, 0], fitWidth: 1.3 });
  const stage = createStage(canvas, { draw, maxFps: 30, onResize: () => { hudBoxes = null; } });   // 30 fps: smooth enough for drifting light, half the work of 60
  const orbit = attachOrbit(canvas, cam, {
    minDist: 4, maxDist: 30,
    onInteract: () => { lastTouch = clockT; },
    onChange: () => { if (!stage.running) stage.frame(); },
    onTap: (x, y) => choose(pick(order, x, y, 16)),
    onHover: (x, y, e) => {
      const n = pick(order, x, y, 12);
      if (n !== hovered) { hovered = n; if (!stage.running) stage.frame(); }
      tipAt = n && n !== selected ? [e.clientX, e.clientY] : null;
      if (tipAt) env.tip.showTip(tipAt[0], tipAt[1], n.head(), n.rows());
      else env.tip.hideTip();
      canvas.style.cursor = n ? "pointer" : "grab";
    },
    onLeave: () => { hovered = null; tipAt = null; env.tip.hideTip(); if (!stage.running) stage.frame(); },
  });
  // the corner readouts change size with their text (and come and go): measure them again then
  const hudSizes = new ResizeObserver(() => { hudBoxes = null; if (shown && !stage.running) stage.frame(); });
  for (const e of [hudTL, hudBL, hint, card, empty]) hudSizes.observe(e);

  spinBtn.addEventListener("click", () => {
    spin = !spin;
    spinBtn.setAttribute("aria-pressed", spin ? "true" : "false");
    lastTouch = -1e9;
  });
  zoomIn.addEventListener("click", () => orbit.zoom(1 / 1.2));
  zoomOut.addEventListener("click", () => orbit.zoom(1.2));
  resetBtn.addEventListener("click", () => { orbit.reset(); choose(null); });

  // keyboard: arrows step through the nodes in tree order, Escape lets go
  canvas.addEventListener("keydown", (e) => {
    if (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
    const live = order.filter((n) => !n.gone);
    if (!live.length) return;
    let i = selected ? live.indexOf(selected) : -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") i = (i + 1) % live.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") i = (i - 1 + live.length) % live.length;
    else if (e.key === "Home") i = 0;
    else if (e.key === "End") i = live.length - 1;
    else if (e.key === "Escape") { choose(null); return; }
    else return;
    e.preventDefault();
    choose(live[i]);
  });

  // ---- nodes
  function makeNode(id, type, parent) {
    const p = parent ? parent.pos : [0, 3, 0];
    const n = {
      id, type, parent, kind: parent?.kind || null, pos: [...p], target: [...p], alpha: 0, born: clockT, gone: false,
      peer: null, label: "", sub: "", act: 0, rateIn: 0, rateOut: 0, spawnIn: 0, spawnOut: 0, n: 0, warn: null,
      head: () => headOf(n), rows: () => rowsOf(n),
    };
    nodes.set(id, n);
    return n;
  }

  function headOf(n) {
    if (n.type === "root") return "YOUR NODE";
    if (n.type === "branch") return BRANCH_NAME[n.kind];
    if (n.type === "group") return `ONE COMPANY'S SERVERS · ${n.label}`;
    return `${n.peer.dir === "in" ? "INCOMING" : "OUTGOING"} · ${shortHost(n.peer)}`;
  }

  function rowsOf(n) {
    const s = lastSnap || {};
    if (n.type === "root") {
      const node = s.node || {};
      return [
        { label: "HEIGHT", value: f.num(node.height) },
        { label: "OUTGOING", value: `${f.num(node.out_peers)} / ${f.num(node.out_wanted)}` },
        { label: "INCOMING", value: f.num(node.in_peers) },
        { label: "DOWN / UP NOW", value: `${f.rate(s.traffic?.rate_in)} / ${f.rate(s.traffic?.rate_out)}` },
        { note: listKnown
          ? "Each moving dot is data your node really moved with one peer in the last few seconds. Busier peers send more dots, but not in proportion."
          : `${s.source?.answering === false ? "Your node isn't answering, so its connections can't be seen right now." : "This reading has no list of connections."} ${
            holding ? "The last list stays, dimmed, for a few seconds." : "Only your node is drawn."}` },
      ];
    }
    if (n.type === "branch" || n.type === "group") {
      return [
        { label: "CONNECTIONS", value: f.num(n.n) },
        { label: "DOWN / UP NOW", value: `${f.rate(n.rateIn)} / ${f.rate(n.rateOut)}` },
        n.warn ? warnNote(n.warn) : null,
        n.type === "group" ? { note: `${f.plural(n.n, "connection")} on one company's servers. The ECLIPSE WATCH lamps warn when too many of your connections share one company.` } : null,
      ].filter(Boolean);
    }
    const p = n.peer;
    const myH = s.node?.height;
    const lag = f.isNum(p.height) && f.isNum(myH) && p.height > 0 ? p.height - myH : null;
    const rows = [
      { label: "CONNECTED FOR", value: f.dur(p.age_s) },
      { label: "DOWN / UP NOW", value: `${f.rate(n.rateIn)} / ${f.rate(n.rateOut)}` },
      { label: "RECEIVED / SENT", value: `${f.bytes(p.recv_bytes)} / ${f.bytes(p.sent_bytes)}` },
      { label: "ITS HEIGHT", value: f.isNum(p.height) && p.height > 0 ? `${f.num(p.height)}${lag ? ` (${lag > 0 ? "+" : f.MINUS}${f.num(Math.abs(lag))})` : ""}` : "—" },
      { label: "STATE", value: String(p.state || "—").toUpperCase() },
    ];
    if (p.kind === "ipv4" && p.net16) rows.push({ label: "COMPANY RANGE", value: `${p.net16}.x.x` });
    if (p.pruned) rows.push({ label: "KEEPS", value: "PART OF THE CHAIN (PRUNED)" });
    if (n.warn) rows.push(warnNote(n.warn));
    return rows;
  }
  // an active ECLIPSE WATCH alert, said on the card of each peer (and group) it names
  const warnNote = (a) => ({ note: f.flag("warn", `HEADS-UP · ${a.title || "ECLIPSE WATCH"}: ${a.text || ""}`), level: "warn" });

  // which peers the dashboard's own ECLIPSE WATCH heads-ups name right now
  function readWarnings(snap) {
    warned = { out: new Map(), in: new Map() };
    for (const a of Array.isArray(snap.alerts) ? snap.alerts : []) {
      const dir = a?.id === "spread_out" ? "out" : a?.id === "spread_in" ? "in" : null;
      if (!dir || !Array.isArray(a.ips)) continue;
      for (const ip of a.ips) warned[dir].set(String(ip), a);
    }
  }

  // ---- tree from the snapshot whose peer list is drawn (this one, or for a
  // moment the last one that had a list)
  function rebuild(snap) {
    // no peer list to draw: only your node is drawn (no branch reads 0)
    const list = snap ? snap.peers.list.filter(settled) : [];
    const seen = new Set();
    let root = nodes.get("root");
    if (!root) { root = makeNode("root", "root", null); root.alpha = 1; }
    seen.add("root");
    root.target = [0, 3.2, 0];

    const byBranch = { out: [], in: [], tor: [] };
    for (const p of list) byBranch[branchOf(p)].push(p);
    // a branch's count is all its connections, those too new to draw yet too
    const count = { out: 0, in: 0, tor: 0 };
    for (const p of snap ? snap.peers.list : []) count[branchOf(p)]++;

    for (const kind of snap ? ["out", "in", "tor"] : []) {
      const peers = byBranch[kind];
      if (!count[kind] && kind === "tor" && !(snap.node?.tor_out > 0)) continue;
      const bid = `b:${kind}`;
      const b = nodes.get(bid) || makeNode(bid, "branch", root);
      b.kind = kind;
      b.gone = false;
      seen.add(bid);
      const a = rad(BRANCH_ANGLE[kind]);
      b.target = [Math.cos(a) * 3.4, 1.0, Math.sin(a) * 3.4];
      b.n = count[kind];

      // peers on the same company's servers (first two parts of an IPv4) share a branch
      const groups = new Map();
      const singles = [];
      if (kind !== "tor") {
        for (const p of peers) {
          const key = p.kind === "ipv4" && p.net16 ? p.net16 : null;
          if (key) { if (!groups.has(key)) groups.set(key, []); groups.get(key).push(p); }
          else singles.push(p);
        }
        for (const [k, ps] of [...groups]) if (ps.length < 2) { singles.push(...ps); groups.delete(k); }
      } else singles.push(...peers);

      // children of the branch: groups first (biggest first), then single peers by address
      const kids = [
        ...[...groups.entries()].sort((x, y) => y[1].length - x[1].length || (x[0] < y[0] ? -1 : 1)).map(([k, ps]) => ({ g: k, ps })),
        ...singles.sort((x, y) => (shortHost(x) < shortHost(y) ? -1 : 1)).map((p) => ({ p })),
      ];
      const ring = place(kids.length, a);
      kids.forEach((k, i) => {
        const [dx, dz, dy] = ring[i];
        const pos = [b.target[0] + dx, b.target[1] - 2.3 + dy, b.target[2] + dz];
        if (k.g) {
          const gid = `g:${kind}:${k.g}`;
          const g = nodes.get(gid) || makeNode(gid, "group", b);
          g.parent = b; g.kind = kind; g.gone = false; g.label = `${k.g}.x.x`; g.n = k.ps.length; g.warn = null;
          seen.add(gid);
          g.target = pos;
          const sub = place(k.ps.length, a + i * 0.7, 0.42, 14);
          k.ps.sort((x, y) => (x.ip < y.ip ? -1 : 1)).forEach((p, j) => {
            const [ex, ez, ey] = sub[j];
            const lf = leaf(p, g, [pos[0] + ex, pos[1] - 1.35 + ey, pos[2] + ez], seen);
            if (lf.warn) g.warn = lf.warn;   // the group holds a peer an ECLIPSE WATCH heads-up names
          });
        } else leaf(k.p, b, pos, seen);
      });
    }
    // anything not seen any more fades out toward its parent (while the
    // screen is away there is nothing to fade, so it goes at once)
    for (const n of nodes.values()) {
      if (seen.has(n.id) || n.gone) continue;
      n.gone = true;   // a flag, not a time: it must hold even before the first frame is drawn
      if (n === selected) choose(null);
      if (n.parent) n.target = [...n.parent.pos];
    }
    if (!shown) for (const n of [...nodes.values()]) if (n.gone) { nodes.delete(n.id); if (n === hovered) hovered = null; }
    order = [...nodes.values()].sort((x, y) => typeRank(x) - typeRank(y) || (x.id < y.id ? -1 : 1));
    leaves = order.filter((n) => n.type === "leaf");
  }

  const typeRank = (n) => ({ root: 0, branch: 1, group: 2, leaf: 3 }[n.type]);

  // positions for n children around their parent: a ring for a few, a flat
  // sunflower disc for many (so a node with 60 incoming peers stays tidy)
  function place(n, start, spacing = 0.62, ringMax = 16) {
    const out = [];
    if (n <= 0) return out;
    if (n === 1) return [[0, 0, 0]];
    if (n <= ringMax) {
      const r = Math.max(0.55, (n * spacing) / TAU);
      for (let i = 0; i < n; i++) {
        const t = start + (i / n) * TAU;
        out.push([Math.cos(t) * r, Math.sin(t) * r, 0]);
      }
      return out;
    }
    const c = spacing * 0.55;
    for (let i = 0; i < n; i++) {
      const r = c * Math.sqrt(i + 0.6);
      const t = start + i * 2.39996;   // the golden angle
      out.push([Math.cos(t) * r, Math.sin(t) * r, -r * 0.18]);
    }
    return out;
  }

  function leaf(p, parent, pos, seen) {
    const id = peerId(p);
    const n = nodes.get(id) || makeNode(id, "leaf", parent);
    n.parent = parent; n.kind = branchOf(p); n.peer = p; n.gone = false; n.target = pos;
    n.warn = p.kind === "ipv4" || p.kind === "ipv6" ? warned[p.dir === "in" ? "in" : "out"].get(String(p.ip)) || null : null;
    seen.add(id);
    return n;
  }

  // ---- chatter: per-peer rates, joins, leaves, blocks, bans, the busiest talkers
  // Only what a reading really shows: with no peer list in it, nothing is
  // said about peers (no made-up LEFT and JOIN) and the last list is kept to
  // compare with when one comes again; with no ban list, nothing about bans.
  // Each reading comes here once; t is when it was taken.
  function chatter(snap, t) {
    const gapS = Math.max(pollS * GAP_READINGS, GAP_MIN_S);
    const gapped = lastT !== null && t - lastT > gapS;
    const lines = [];
    const add = (tag, text, kind, at = t) => lines.push({ t: at, tag, text, kind });
    if (gapped || talkFrom === null) { talkSums = new Map(); talkFrom = t; }

    // the node going quiet, and coming back
    const ans = snap.source?.answering;
    if (ans === true || ans === false) {
      if (ans === false && prevAnswering === true) add("NODE", f.flag("bad", "YOUR NODE ISN'T ANSWERING"), "bad");
      else if (ans === true && prevAnswering === false) add("NODE", "ANSWERING AGAIN", "back");
      prevAnswering = ans;
    }

    if (listKnown) {
      const list = snap.peers.list;
      const next = new Map();
      for (const p of list) {
        const id = peerId(p);
        const n = nodes.get(id);
        let rin = p.down_bps || 0;   // monerod's own figure until two readings can be compared
        let rout = p.up_bps || 0;
        if (f.isNum(p.recv_bytes)) {
          const recv = p.recv_bytes, sent = p.sent_bytes || 0;
          let s = samples.get(id);
          const prev = s && s[s.length - 1];
          // the bytes since the last reading go toward the busiest-talker list, unless
          // that reading was before a gap or the counters went back (then start over)
          if (prev && t > prev.t && t - prev.t <= gapS && recv >= prev.recv && sent >= prev.sent) {
            if (recv + sent > prev.recv + prev.sent) {
              const k = talkSums.get(id) || { dr: 0, ds: 0 };
              k.p = p; k.dr += recv - prev.recv; k.ds += sent - prev.sent;
              talkSums.set(id, k);
            }
          } else s = [];
          // the rate is over the last RATE_S or so, not since the last reading: a second
          // at a time a peer's traffic is all bursts, and its leaf would blink
          s.push({ t, recv, sent });
          while (s.length > 1 && t - s[1].t >= RATE_S) s.shift();
          if (s.length > 1) { rin = (recv - s[0].recv) / (t - s[0].t); rout = (sent - s[0].sent) / (t - s[0].t); }
          next.set(id, s);
        }
        if (n) { n.rateIn = rin; n.rateOut = rout; }
      }
      samples = next;

      // joins and leaves of drawn peers since the last reading that had a list; after
      // a gap (node down, port hiding it, a failed call) a crowd of them is one line
      const ids = new Map(list.filter(settled).map((p) => [peerId(p), p]));
      if (knownPeers) {
        const gap = knownAt !== null && (t < knownAt || t - knownAt > gapS);
        const joined = [...ids].filter(([id]) => !knownPeers.has(id));
        const left = [...knownPeers].filter(([id]) => !ids.has(id));
        // a crowd at once (or after a gap) is one line, so the log isn't flooded
        const crowd = (k) => k > (gap ? SUMMARY_OVER : SUMMARY_OVER * 3);
        const when = gap ? "SINCE THE LIST WAS LAST SEEN" : "AT ONCE";
        if (crowd(joined.length)) add("JOIN", `${f.plural(joined.length, "PEER", "PEERS")} NEW ${when}`, "join");
        else for (const [, p] of joined) add("JOIN", `${shortHost(p)} · ${p.dir === "in" ? "INCOMING" : "OUTGOING"}${p.kind === "tor" ? " · TOR" : p.kind === "i2p" ? " · I2P" : ""}`, "join");
        if (crowd(left.length)) add("LEFT", `${f.plural(left.length, "PEER", "PEERS")} GONE ${when}`, "leave");
        else for (const [, p] of left) add("LEFT", `${shortHost(p)} · AFTER ${f.dur(p.age_s)}`, "leave");
        for (const [id] of joined) { const n = nodes.get(id); if (n) n.flash = clockT; }
      }
      knownPeers = ids;
      knownAt = t;
      listSnap = snap;
    } else for (const n of leaves) { n.rateIn = null; n.rateOut = null; }   // a held list: what they move now isn't known
    // branch and group totals (unknown when there is no list)
    for (const n of nodes.values()) if (n.type !== "leaf") { n.rateIn = listKnown ? 0 : null; n.rateOut = listKnown ? 0 : null; }
    if (listKnown) for (const n of nodes.values()) {
      if (n.type !== "leaf" || n.gone) continue;
      for (let a = n.parent; a; a = a.parent) { a.rateIn += n.rateIn; a.rateOut += n.rateOut; }
    }

    // new blocks, each at the time your node got it, or when that isn't known (after
    // a gap), at this reading's. Never the block's own time: miners stamp a block when
    // they start work on it, so that can be minutes earlier and out of order here.
    // A crowd at once is one line, and so is a catch-up, once it's done (not a line
    // every reading while it runs). The wave is for a block landing now, not one
    // that came while the window was away.
    const h = snap.node?.height;
    const catchingUp = snap.node?.synced === false;
    if (f.isNum(h) && f.isNum(prevHeight) && h > prevHeight && !catchingUp) {
      const fresh = (snap.chain?.blocks || []).filter((b) => f.isNum(b?.height) && b.height >= prevHeight);
      const top = fresh[fresh.length - 1];
      if (h - prevHeight > SUMMARY_OVER) add("BLOCK", `${f.plural(h - prevHeight, "NEW BLOCK", "NEW BLOCKS")} · UP TO ${f.num(h - 1)}`, "block");
      else if (!fresh.length) add("BLOCK", f.num(h - 1), "block");
      else for (const b of fresh) {
        const bits = [f.num(b.height)];
        if (f.isNum(b.txs)) bits.push(f.plural(b.txs, "TX", "TXS"));
        if (f.isNum(b.weight)) bits.push(f.bytes(b.weight));
        add("BLOCK", bits.join(" · "), "block", f.isNum(b.seen) ? b.seen : t);
      }
      if (f.isNum(top?.seen) ? t - top.seen <= gapS : !gapped) blockWave();
    }
    if (f.isNum(h) && !catchingUp) prevHeight = h;
    // bans the node added or dropped (a reading without a ban list says nothing)
    const banList = snap.node?.ban_list;
    if (Array.isArray(banList)) {
      const bans = new Set(banList.map((b) => b?.host).filter(Boolean));
      if (prevBans) {
        for (const b of bans) if (!prevBans.has(b)) add("BAN", plainHost(b), "ban");
        for (const b of prevBans) if (!bans.has(b)) add("UNBAN", `${plainHost(b)} · BAN ENDED`, "ban");
      }
      prevBans = bans;
    }
    // the pool, when it empties or moves by a fair amount since the log last
    // said (it changes a little at nearly every reading; that would drown the rest)
    const pool = snap.mempool?.txs;
    if (f.isNum(pool)) {
      if (poolSaid === null) poolSaid = pool;
      else {
        const d = pool - poolSaid;
        if ((pool === 0 && poolSaid > 0) || Math.abs(d) >= Math.max(10, poolSaid * 0.25)) {
          add("POOL", `${pool === 0 ? "EMPTY" : `${f.plural(pool, "TX", "TXS")} WAITING`} · ${f.num(Math.abs(d))} ${d > 0 ? "MORE" : "FEWER"}`, "pool");
          poolSaid = pool;
        }
      }
    }
    // the busiest few still drawn, by what they moved since they were last listed
    // (named every reading, they'd bury everything else and their tags would hop)
    if (t - talkFrom >= TALK_EVERY_S - 0.5) {
      if (listKnown) {
        const talk = [...talkSums].filter(([id, k]) => knownPeers.has(id) && k.dr + k.ds >= TALK_MIN);
        talk.sort(([, x], [, y]) => (y.dr + y.ds) - (x.dr + x.ds));
        topTalkers = new Set();
        for (const [id, k] of talk.slice(0, TALKERS)) {
          add("DATA", `${shortHost(k.p)} ↓${f.bytes(k.dr)} ↑${f.bytes(k.ds)}`, "talk");
          topTalkers.add(id);
        }
      }
      talkSums = new Map();
      talkFrom = t;
    }
    // this reading's lines in the order they happened (a block may have come just before it)
    queue.push(...lines.sort((x, y) => x.t - y.t));
    // spread them over the time until the next reading (when it's more than a second or two away)
    dripGap = clamp((pollS * 0.85) / Math.max(1, queue.length), 0.3, 1.5);
    // more waiting than the log keeps: the busiest-talker lines go first
    while (queue.length > FEED_KEEP) queue.splice(Math.max(0, queue.findIndex((it) => it.kind === "talk")), 1);
  }

  // one chatter line into the log (newest at the bottom, like a terminal)
  function emit(it, animate) {
    feedEmpty.remove();
    const li = el("li", { class: "nt-chat", "data-kind": it.kind },
      el("span", { class: "nt-chat-t", text: f.clock(it.t).replace(/\s?[AP]M$/i, "") }),
      el("span", { class: "nt-chat-g", text: `[${it.tag}]` }),
      el("span", { class: "nt-chat-x", text: it.text }));
    if (animate) li.classList.add("is-new");
    feedList.insertBefore(li, cursor);
    while (feedList.children.length > FEED_KEEP + 1) feedList.firstChild.remove();
  }
  // lines come out one at a time while the screen is on show and moving and
  // the readings are more than a second or two apart; otherwise all at once,
  // as they happen, so the log is never behind
  const atOnce = () => !shown || !moving(look) || pollS <= 2;
  function drip() {
    clearTimeout(dripTimer);
    dripTimer = 0;
    if (!queue.length) return;
    if (atOnce()) {
      for (const it of queue.splice(0)) emit(it, shown && moving(look));
      return;
    }
    emit(queue.shift(), true);
    if (queue.length) dripTimer = setTimeout(drip, dripGap * 1000);
  }
  function renderFeed() {
    if (atOnce()) { drip(); return; }
    if (!dripTimer) drip();
  }

  // ---- particles
  function pathUp(n) {   // leaf -> ... -> root
    const out = [];
    for (let a = n; a; a = a.parent) out.push(a);
    return out;
  }
  function spawn(n, dir, kind = "data") {
    if (particles.length >= DOT_CAP) return;
    const path = pathUp(n);
    if (dir === "out") path.reverse();
    particles.push({ path, t: 0, speed: kind === "block" ? 1.25 : 0.62 + Math.random() * 0.25, dir, kind });
  }
  // a new block: a ring of dots runs out from your node to every peer, and the
  // root kicks. Only while the screen is on show; a wave saved up while it was
  // away would play an old block as if it were landing now.
  function blockWave() {
    if (!shown) return;
    const r = nodes.get("root");
    if (r) r.flash = clockT;
    if (!moving(look)) return;
    for (const n of nodes.values()) if (n.type === "leaf" && !n.gone) spawn(n, "out", "block");
  }
  // dots a second along a peer's branch, from the bytes a second it is moving:
  // any traffic at all shows (about one dot a second at 1 kB/s), and a busy
  // peer shows more without flooding the picture
  const perSecond = (bps) => (bps > 0 ? Math.min(4, 0.35 + Math.log2(1 + bps / 1024) * 0.6) : 0);

  // ---- drawing
  const tmp = {};
  // motes: specks of light drifting slowly through the space (decoration, for depth)
  const fr = (x) => x - Math.floor(x);
  const motes = Array.from({ length: 110 }, (_, i) => {
    const u = Math.sin(i * 12.9898) * 43758.5453; const v = Math.sin(i * 78.233) * 12345.678; const w = Math.sin(i * 39.425) * 24634.6345;
    const th = fr(u) * TAU, ph = Math.acos(2 * fr(v) - 1), r = 5 + fr(w) * 10;
    return {
      p: [r * Math.sin(ph) * Math.cos(th), r * Math.cos(ph) * 0.6, r * Math.sin(ph) * Math.sin(th)],
      v: [(fr(u * 3) - 0.5) * 0.12, (fr(v * 5) - 0.5) * 0.05, (fr(w * 7) - 0.5) * 0.12],
      cool: fr(u * 11) < 0.4, size: 0.6 + fr(v * 13) * 1.6, a: 0.14 + fr(w * 17) * 0.31, ph: fr(u * 7) * TAU,
    };
  });

  function rgbOf(n) {
    return n.kind === "in" ? KIND_RGB.in : n.kind === "tor" ? KIND_RGB.tor : look.accent;
  }

  function draw(ctx, w, h, t, dt) {
    clockT = t;
    const live = moving(look);
    const gm = look.glow === false ? 0.35 : 1;   // glow off in the settings: much fainter light, no breathing
    // spin unless someone is turning it
    if (live && spin && t - lastTouch > IDLE_RESUME_S && !orbit.dragging) cam.yaw += SPIN * dt;
    cam.target[1] = w / h < 1.1 ? 0.8 : -0.3;   // on a tall, narrow screen the tree sits lower, clear of the readout
    cam.prepare(w, h);

    // ease every node toward where it belongs; fade in and out (and dim while a list is held)
    const k = live ? 1 - Math.pow(0.02, dt || 1) : 1;
    for (const n of [...nodes.values()]) {
      for (let i = 0; i < 3; i++) n.pos[i] += (n.target[i] - n.pos[i]) * k;
      const goal = n.gone ? 0 : holding && n.type !== "root" ? HELD_ALPHA : 1;
      n.alpha += (goal - n.alpha) * (live ? Math.min(1, dt * 3) : 1);
      if (n.gone && (n.alpha < 0.02 || !live)) { nodes.delete(n.id); if (n === hovered) hovered = null; }
    }
    order = order.filter((n) => nodes.has(n.id));

    ctx.clearRect(0, 0, w, h);
    const acc = look.accent;
    // a faint light behind the root
    const rootP = nodes.get("root") ? cam.project(nodes.get("root").pos, {}) : null;
    if (rootP) {
      const g = glowSprite(acc, 128, 0.10);
      ctx.globalAlpha = gm;
      stamp(ctx, g, rootP.x, rootP.y + 40, Math.min(w, 560));
      ctx.globalAlpha = 1;
    }

    // motes, for depth when it turns
    const cool = look.head || "107, 174, 200";
    const twinkle = live && gm === 1;
    for (const m of motes) {
      if (live) {
        for (let i = 0; i < 3; i++) {
          m.p[i] += m.v[i] * dt;
          const lim = i === 1 ? 7 : 15;
          if (m.p[i] > lim) m.p[i] -= lim * 2; else if (m.p[i] < -lim) m.p[i] += lim * 2;
        }
      }
      const p = cam.project(m.p, tmp);
      if (!p) continue;
      const a = m.a * clamp(1.3 - (p.d - cam.dist + 4) / 22, 0.15, 1) * (twinkle ? 0.75 + 0.25 * Math.sin(t * 0.7 + m.ph) : 0.85);
      const sz = m.size * clamp(p.s / 60, 0.6, 1.5);
      ctx.fillStyle = `rgba(${m.cool ? cool : acc}, ${a.toFixed(3)})`;
      if (sz < 1.4) ctx.fillRect(p.x - sz, p.y - sz, sz * 2, sz * 2);   // a tiny square reads as a dot and is cheaper
      else { ctx.beginPath(); ctx.arc(p.x, p.y, sz, 0, TAU); ctx.fill(); }
    }

    // the floor: rings and spokes under the tree
    ctx.lineWidth = 1;
    const floorY = -4.1;
    for (const r of [2, 4, 6]) {
      ctx.beginPath();
      for (let i = 0; i <= 72; i++) {
        const a = (i / 72) * TAU;
        const p = cam.project([Math.cos(a) * r, floorY, Math.sin(a) * r], tmp);
        if (!p) continue;
        if (i === 0) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y);
      }
      ctx.strokeStyle = `rgba(${cool}, ${r === 6 ? 0.12 : 0.07})`;
      ctx.stroke();
    }
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * TAU;
      const p1 = cam.project([Math.cos(a) * 1, floorY, Math.sin(a) * 1], {});
      const p2 = cam.project([Math.cos(a) * 6, floorY, Math.sin(a) * 6], {});
      if (!p1 || !p2) continue;
      ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y);
      ctx.strokeStyle = `rgba(${cool}, 0.05)`;
      ctx.stroke();
    }

    // project the nodes
    for (const n of nodes.values()) {
      const p = cam.project(n.pos, {});
      n.proj = p;
      if (!p) { n.sx = undefined; continue; }
      n.sx = p.x; n.sy = p.y;
      n.fog = clamp(1.25 - (p.d - cam.dist + 6) / 16, 0.25, 1);
      n.sr = n.type === "root" ? 10 : n.type === "branch" ? 7 : n.type === "group" ? 5 : 3.5;
      n.hidden = n.alpha < 0.2;
      // activity: how busy this node is right now (drives brightness when nothing moves)
      const busy = Math.min(1, Math.log2(1 + (n.rateIn + n.rateOut) / 4096) / 6);
      n.act += (busy - n.act) * (live ? Math.min(1, dt * 2) : 1);
    }

    // selected path lights up
    const lit = new Set();
    for (let a = selected; a; a = a.parent) lit.add(a);

    // branches (edges): dim when quiet, brighter when busy, and a short
    // flicker each time data starts along one
    for (const n of nodes.values()) {
      if (!n.parent || !n.proj || !n.parent.proj) continue;
      const rgb = rgbOf(n);
      const on = lit.has(n);
      const fl = live && n.flick ? Math.max(0, 1 - (t - n.flick) / 0.9) : 0;
      const a = (on ? 0.95 : 0.13 + 0.42 * n.act + 0.45 * fl) * n.alpha * n.fog;
      ctx.beginPath();
      ctx.moveTo(n.parent.proj.x, n.parent.proj.y);
      ctx.lineTo(n.proj.x, n.proj.y);
      ctx.strokeStyle = `rgba(${rgb}, ${Math.min(1, a).toFixed(3)})`;
      ctx.lineWidth = (on ? 1.8 : n.type === "leaf" ? 0.9 : 1.3) + 1.1 * fl;
      ctx.stroke();
    }

    // chatter: spawn from real rates, then move along the branches. When the
    // dots would pass the cap, every peer's dots thin by the same share, so a
    // busier peer still shows more of them whatever its place in the list.
    if (live) {
      let want = 0;
      for (const n of leaves) {
        if (n.gone || !n.parent) continue;
        n.spawnIn += perSecond(n.rateIn) * dt;
        n.spawnOut += perSecond(n.rateOut) * dt;
        want += Math.floor(n.spawnIn) + Math.floor(n.spawnOut);
      }
      if (want > 0) {
        // same odds for every wanted dot, and the round starts at a different
        // peer each frame, so no place in the list is favoured near the cap
        const share = Math.min(1, Math.max(0, DOT_CAP - particles.length) / want);
        const L = leaves.length;
        const from = spawnFrom++ % L;
        for (let k = 0; k < L; k++) {
          const n = leaves[(from + k) % L];
          if (n.gone || !n.parent) continue;
          if (n.spawnIn >= 1 || n.spawnOut >= 1) { n.flick = t; n.parent.flick = t; }   // the branch flickers whether or not its dot fits
          while (n.spawnIn >= 1) { n.spawnIn -= 1; if (share >= 1 || Math.random() < share) spawn(n, "in"); }
          while (n.spawnOut >= 1) { n.spawnOut -= 1; if (share >= 1 || Math.random() < share) spawn(n, "out"); }
        }
      }
      ctx.globalCompositeOperation = "lighter";
      const next = [];
      for (const q of particles) {
        q.t += dt * q.speed;
        const segs = q.path.length - 1;
        if (q.t >= 1 || segs < 1 || q.path.some((n) => !nodes.has(n.id))) {
          if (q.kind !== "block" && q.dir === "in" && q.t >= 1) { const r = nodes.get("root"); if (r) r.hit = t; }
          continue;
        }
        next.push(q);
        const x = q.t * segs;
        const i = Math.min(segs - 1, Math.floor(x));
        const u = x - i;
        const A = q.path[i].pos, B = q.path[i + 1].pos;
        const p = cam.project([A[0] + (B[0] - A[0]) * u, A[1] + (B[1] - A[1]) * u, A[2] + (B[2] - A[2]) * u], tmp);
        if (!p) continue;
        // a new block's dots wear the accent and run larger, so they never pass for data coming in
        const rgb = q.kind === "block" ? acc : q.dir === "in" ? DATA_IN : DATA_OUT;
        const fade = Math.min(1, q.t * 6, (1 - q.t) * 6);
        const size = (q.kind === "block" ? 20 : 10) * clamp(p.s / 60, 0.5, 1.4) * (gm < 1 ? 0.75 : 1);
        ctx.globalAlpha = fade * (q.kind === "block" ? 0.9 : 0.75);
        stamp(ctx, glowSprite(rgb, 16, 0.9), p.x, p.y, size);
        ctx.globalAlpha = 1;
      }
      ctx.globalCompositeOperation = "source-over";
      particles = next;
    } else particles = [];

    // nodes, far ones first
    const drawn = [...nodes.values()].filter((n) => n.proj).sort((x, y) => y.proj.d - x.proj.d);
    for (const n of drawn) drawNode(ctx, n, t, lit.has(n), live, gm);

    // labels. Nothing is printed over anything else: first the corner
    // readouts and the marks of your node and its branches claim their room,
    // then your node's label, the picked one, the branches (nudged down or up
    // when there's no room, as they carry the counts), groups (once zoomed in,
    // or when a heads-up names them; skipped when there's no room), and last
    // small name tags under the busiest peers (all of them once zoomed in close)
    ctx.textBaseline = "top";
    taken.length = 0;
    if (!hudBoxes) measureHud();
    for (const b of hudBoxes) taken.push(b);
    const marks = [];
    const tags = [];
    for (const n of drawn) {
      if (n.alpha < 0.3) continue;
      if (n.type === "root" || n.type === "branch") marks.push(n);
      else if (n.type === "leaf" && n !== selected && n !== hovered && (topTalkers.has(n.id) || n.proj.s >= 95)) tags.push(n);
    }
    for (const n of marks) {
      const r = (n.type === "root" ? 16 : 9) * clamp(n.proj.s / 55, 0.45, 1.6);
      taken.push([n.sx - r, n.sy - r, n.sx + r, n.sy + r, n]);   // its own label may touch it
    }
    const labelled = drawn.filter((n) => n.alpha >= 0.3 && (n.type === "root" || n.type === "branch"
      || n === selected || n === hovered || (n.type === "group" && (n.warn || n.proj.s >= 80))));
    const rank = (n) => (n.type === "root" ? 0 : n === selected ? 1 : n === hovered ? 2 : n.type === "branch" ? 3 : n.warn ? 4 : 5);
    labelled.sort((x, y) => rank(x) - rank(y) || x.proj.d - y.proj.d);
    for (const n of labelled) label(ctx, n, w, h, rank(n) <= 3);
    // name tags never pile on each other: the busiest go first, the rest only where there's room
    tags.sort((x, y) => y.act - x.act);
    for (const n of tags) tag(ctx, n, w, h);
    if (!live) particles = [];
  }

  // where the corner readouts (and the card, and the empty-state note) sit on
  // the canvas; measured only after they change, never every frame
  function measureHud() {
    hudBoxes = [];
    const c = canvas.getBoundingClientRect();
    for (const e of [hudTL, hudBL, hint, card, empty]) {
      if (e.hidden || getComputedStyle(e).visibility === "hidden") continue;
      const r = e.getBoundingClientRect();
      if (!r.width || !r.height) continue;   // not displayed at this width
      hudBoxes.push([r.left - c.left - 4, r.top - c.top - 4, r.right - c.left + 4, r.bottom - c.top + 4]);
    }
  }

  function drawNode(ctx, n, t, on, live, gm) {
    const p = n.proj;
    const rgb = n.type === "root" || n.type === "branch" && n.kind === "out" ? look.accent : rgbOf(n);
    const s = clamp(p.s / 55, 0.45, 1.6);
    const a = n.alpha * n.fog;
    const focus = n === selected || n === hovered;   // the white ring: what the keyboard or pointer is on
    ctx.globalCompositeOperation = "lighter";
    ctx.globalAlpha = gm;
    if (n.type === "root") {
      const breathe = live && gm === 1 ? 0.5 + 0.5 * Math.sin(t * 0.9) : 0.6;
      const kick = n.flash ? Math.max(0, 1 - (t - n.flash) / 1.6) : 0;
      const hit = n.hit ? Math.max(0, 1 - (t - n.hit) / 0.4) : 0;
      stamp(ctx, glowSprite(rgb, 64, 0.55), p.x, p.y, (70 + 30 * breathe + 90 * kick + 10 * hit) * s);
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
      ring(ctx, p.x, p.y, 13 * s, `rgba(${rgb}, ${0.9 * a})`, 1.5);
      ring(ctx, p.x, p.y, (19 + 4 * breathe) * s, `rgba(${rgb}, ${0.25 * a})`, 1);
      if (kick > 0) ring(ctx, p.x, p.y, (20 + 120 * (1 - kick)) * s, `rgba(${rgb}, ${0.6 * kick})`, 1.2);
      dot(ctx, p.x, p.y, 5.5 * s, `rgba(${rgb}, ${a})`);
      if (focus) ring(ctx, p.x, p.y, 25 * s, `rgba(255, 255, 255, ${0.85 * a})`, 1.2);
      return;
    }
    if (n.type === "branch") {
      stamp(ctx, glowSprite(rgb, 40, 0.5), p.x, p.y, (34 + 30 * n.act) * s);
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
      // a hexagon, so branches read as different from peers without colour
      ctx.beginPath();
      for (let i = 0; i < 6; i++) {
        const ang = (i / 6) * TAU + Math.PI / 6;
        const x = p.x + Math.cos(ang) * 8 * s, y = p.y + Math.sin(ang) * 8 * s;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.strokeStyle = `rgba(${rgb}, ${0.95 * a})`;
      ctx.lineWidth = on ? 2 : 1.4;
      ctx.stroke();
      dot(ctx, p.x, p.y, 2.6 * s, `rgba(${rgb}, ${a})`);
      if (focus) ring(ctx, p.x, p.y, 8 * s + 6, `rgba(255, 255, 255, ${0.85 * a})`, 1.2);
      return;
    }
    if (n.type === "group") {
      stamp(ctx, glowSprite(rgb, 30, 0.45), p.x, p.y, (22 + 22 * n.act) * s);
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
      ring(ctx, p.x, p.y, 5 * s, `rgba(${rgb}, ${0.95 * a})`, on ? 2 : 1.3);
      // a heads-up names peers in this group: a steady ring in the heads-up colour (the label carries ▲ and the word)
      if (n.warn) ring(ctx, p.x, p.y, 5 * s + 3.5, `rgba(${warnRGB}, ${0.95 * a})`, 1.6);
      if (focus) ring(ctx, p.x, p.y, 5 * s + 7, `rgba(255, 255, 255, ${0.85 * a})`, 1.2);
      return;
    }
    // a peer: lit while it is moving data (and breathing at its own pace), dim when quiet
    const busy = n.act;
    const litNow = busy > 0.04;
    if (n.pace === undefined) { const h = hash01(n.id); n.pace = 0.55 + h * 0.55; n.phase = h * TAU; }
    const br = litNow && live && gm === 1 ? 1 + 0.16 * Math.sin(t * n.pace + n.phase) : 1;
    const r = (2.3 + busy * 2.4) * s * br;
    const strength = litNow ? 0.62 + 0.38 * busy : 0.45;
    stamp(ctx, glowSprite(rgb, 24, 0.55), p.x, p.y, (litNow ? 12 + 26 * busy : 7) * s * (0.6 + 0.4 * a));
    ctx.globalAlpha = 1;
    const fl = n.flash ? Math.max(0, 1 - (t - n.flash) / 2.2) : 0;
    ctx.globalCompositeOperation = "source-over";
    if (fl > 0) ring(ctx, p.x, p.y, (4 + 22 * (1 - fl)) * s, `rgba(${rgb}, ${0.8 * fl})`, 1.2);
    if (n.kind === "tor") {
      ctx.beginPath();
      ctx.moveTo(p.x, p.y - r * 1.5); ctx.lineTo(p.x + r * 1.5, p.y); ctx.lineTo(p.x, p.y + r * 1.5); ctx.lineTo(p.x - r * 1.5, p.y);
      ctx.closePath();
      ctx.fillStyle = `rgba(${rgb}, ${strength * a})`;
      ctx.fill();
    } else if (n.kind === "in") {
      ring(ctx, p.x, p.y, r + 0.8, `rgba(${rgb}, ${strength * a})`, 1.3);   // incoming: hollow, like the peer scope
    } else {
      ring(ctx, p.x, p.y, r + 1.6, `rgba(${rgb}, ${strength * 0.85 * a})`, 1);   // outgoing: a thin ring round a bright centre
      dot(ctx, p.x, p.y, Math.max(1.1, r * 0.55), `rgba(${rgb}, ${Math.min(1, strength + 0.25) * a})`);
    }
    if (n.warn) ring(ctx, p.x, p.y, r + 3.4, `rgba(${warnRGB}, ${0.9 * a})`, 1.3);   // named by a heads-up: steady, in the heads-up colour
    if (focus) ring(ctx, p.x, p.y, r + 6, `rgba(255, 255, 255, ${0.85 * a})`, 1.2);
  }

  // a node's label: two lines under it (or, when that spot is taken, a line
  // or two lower, or above it). `must` labels (your node, the picked one, the
  // branches with their counts) still print when every spot is taken; the rest
  // are skipped then. Monospaced, so a line's width is its length.
  const LABEL_FONT = { root: [11.5, 2.6], other: [10, 1.8], sub: [9.5, 0.8] };
  function label(ctx, n, w, h, must) {
    const p = n.proj;
    const top = n.type === "root" ? Math.max(26, 25 * clamp(p.s / 55, 0.45, 1.6) + 3) : n.type === "branch" ? 14 : 10;   // the root's clear of its focus ring
    let main = "", sub = "";
    let subRGB = null;
    const s = lastSnap || {};
    if (n.type === "root") {
      main = "YOUR NODE";
      sub = f.isNum(s.node?.height) ? `HEIGHT ${f.num(s.node.height)}` : "";
    } else if (n.type === "branch") {
      main = BRANCH_NAME[n.kind];
      sub = n.kind === "out" && f.isNum(s.node?.out_wanted) ? `${f.num(n.n)} / ${f.num(s.node.out_wanted)}` : f.num(n.n);
    } else if (n.type === "group") {
      main = n.label;
      sub = n.warn ? f.flag("warn", `HEADS-UP · ${n.n} · ONE COMPANY`) : `${n.n} · ONE COMPANY`;
      if (n.warn) subRGB = warnRGB;
    } else {
      main = shortHost(n.peer);
      sub = n.warn ? f.flag("warn", "HEADS-UP · ONE COMPANY") : `↓ ${f.rate(n.rateIn)} ↑ ${f.rate(n.rateOut)}`;
      if (n.warn) subRGB = warnRGB;
    }
    const [fs, gap] = n.type === "root" ? LABEL_FONT.root : LABEL_FONT.other;
    const [sfs, sgap] = LABEL_FONT.sub;
    const half = Math.max(main.length * (fs * 0.6 + gap), sub.length * (sfs * 0.6 + sgap)) / 2 + 4;
    const tall = sub ? 27 : 14;
    const x = clamp(p.x, half + 4, w - half - 4);
    const r = n.type === "root" ? 25 * clamp(p.s / 55, 0.45, 1.6) + 1 : 8;
    // spots to try, as the y of the first line: below, then (your node) above
    // it before lower, or (the rest) a label lower, two lower, then above
    const below = p.y + top, above = p.y - r - tall - 2;
    const spots = n.type === "root" ? [below, above, below + tall, below + tall * 2] : [below, below + tall, below + tall * 2, above];
    let y = null;
    for (const cand of spots) {
      if (cand - 2 >= 0 && cand + tall <= h && !clash(x - half, cand - 2, x + half, cand + tall, n)) { y = cand; break; }
    }
    if (y === null) {
      // nowhere free: a must-have label still prints over other labels, but never under a corner readout or the card
      if (!must || hudBoxes.some((b) => x - half < b[2] && x + half > b[0] && below - 2 < b[3] && below + tall > b[1])) return;
      y = below;
    }
    taken.push([x - half, y - 2, x + half, y + tall]);
    const a = Math.min(1, n.alpha) * (n.type === "group" ? n.fog : 1);
    const ink = look.ink || "232, 232, 240";
    ctx.textAlign = "center";
    ctx.font = `600 ${fs}px "Plex Mono", ui-monospace, monospace`;
    ctx.fillStyle = `rgba(${ink}, ${(n.type === "root" ? 0.97 : 0.9) * a})`;   // text stays ink; the mark beside it carries the kind
    ctx.strokeStyle = `rgba(0, 0, 0, ${0.9 * a})`;
    ctx.lineWidth = 4;
    ctx.lineJoin = "round";
    spaced(ctx, main, x, y, gap);
    if (sub) {
      ctx.font = `${subRGB ? 600 : 400} ${sfs}px "Plex Mono", ui-monospace, monospace`;
      ctx.fillStyle = subRGB ? `rgba(${subRGB}, ${a})` : `rgba(${ink}, ${0.6 * a})`;
      spaced(ctx, sub, x, y + 14, sgap);
    }
  }

  // a peer's small name tag: dim, under the dot, like a label on a chart
  const taken = [];   // boxes already claimed this frame
  const clash = (x0, y0, x1, y1, self) => taken.some((b) => !(self && b[4] === self) && x0 < b[2] && x1 > b[0] && y0 < b[3] && y1 > b[1]);
  function tag(ctx, n, w, h) {
    const p = n.proj;
    const text = shortHost(n.peer);
    const y = p.y + 8 * clamp(p.s / 55, 0.6, 1.4);
    const half = (text.length * (9 * 0.6 + 0.6)) / 2 + 2;
    const x = clamp(p.x, half + 4, w - half - 4);
    if (Math.abs(x - p.x) > half || y + 11 > h) return;   // the dot is too near the edge for its tag to sit under it
    if (clash(x - half, y - 1, x + half, y + 11)) return;
    taken.push([x - half, y - 1, x + half, y + 11]);
    const a = Math.min(1, n.alpha) * n.fog * (topTalkers.has(n.id) ? 0.85 : 0.5);
    ctx.textAlign = "center";
    ctx.font = `500 9px "Plex Mono", ui-monospace, monospace`;
    ctx.fillStyle = `rgba(${look.ink || "232, 232, 240"}, ${a})`;
    ctx.strokeStyle = `rgba(0, 0, 0, ${0.85 * a})`;
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    spaced(ctx, text, x, y, 0.6);
  }

  // letter-spaced uppercase text (canvas letterSpacing is not everywhere yet)
  function spaced(ctx, text, x, y, gap) {
    const sp = "letterSpacing" in ctx;
    if (sp) ctx.letterSpacing = `${gap}px`;
    const dx = sp ? gap / 2 : 0;
    ctx.strokeText(text, x + dx, y);
    ctx.fillText(text, x + dx, y);
    if (sp) ctx.letterSpacing = "0px";
  }

  function ring(ctx, x, y, r, color, lw) {
    ctx.beginPath(); ctx.arc(x, y, Math.max(0.5, r), 0, TAU); ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.stroke();
  }
  function dot(ctx, x, y, r, color) {
    ctx.beginPath(); ctx.arc(x, y, Math.max(0.5, r), 0, TAU); ctx.fillStyle = color; ctx.fill();
  }

  // ---- picking a node shows its card and says it for screen readers
  function choose(n) {
    selected = n && !n.gone ? n : null;
    env.tip.hideTip();
    tipAt = null;
    hudBoxes = null;   // the card came or went: labels make room for it
    if (!selected) {
      card.hidden = true;
      setData(view, "card", null);
      if (!stage.running) stage.frame();
      return;
    }
    fillCard(selected);
    card.hidden = false;
    setData(view, "card", cardSide(selected));
    const rows = selected.rows().filter((r) => !r.note).map((r) => `${r.label} ${r.value}`).join(", ");
    say.textContent = `${selected.head()}. ${rows}`;
    if (!stage.running) stage.frame();
  }

  // On a phone the card covers a strip of the picture: it goes to the bottom
  // (the legend steps aside) when the picked node sits above where it lands,
  // else to the top (over the corner readout), so the node stays in sight.
  function cardSide(n) {
    const h = stage.h || canvas.clientHeight || 0;
    const ch = card.offsetHeight || 0;
    if (n.sy === undefined || !h) return "bottom";
    if (n.sy + 16 < h - 12 - ch) return "bottom";
    if (n.sy - 16 > 12 + ch) return "top";
    return n.sy > h / 2 ? "top" : "bottom";
  }

  // the card's text, changed in place (its rows and notes are reused, never rebuilt)
  function fillCard(n) {
    setText(cardHead, n.head());
    const all = n.rows();
    const rows = all.filter((r) => !r.note);
    const notes = all.filter((r) => r.note);
    while (cardList.children.length < rows.length) cardList.append(el("div", {}, el("dt"), el("dd")));
    while (cardList.children.length > rows.length) cardList.lastChild.remove();
    rows.forEach((r, i) => {
      const [dt, dd] = cardList.children[i].children;
      setText(dt, r.label);
      setText(dd, r.value);
    });
    while (cardNotes.children.length < notes.length) cardNotes.append(el("p", { class: "nt-card-note" }));
    while (cardNotes.children.length > notes.length) cardNotes.lastChild.remove();
    notes.forEach((r, i) => {
      const p = cardNotes.children[i];
      setText(p, r.note);
      setData(p, "level", r.level || null);
    });
  }

  // ---- corner readout and the empty states
  function hud(snap) {
    const src = snap.source || {};
    const node = snap.node || {};
    const tr = snap.traffic || {};
    const n = listKnown ? snap.peers.list.length : null;   // unknown is "—", never 0
    setText(hudConn, f.num(n));
    setText(hudKinds, `${f.num(node.out_peers)} / ${f.num(node.out_wanted)} · ${f.num(node.in_peers)} · ${f.num(node.tor_out)}`);
    setText(hudRates, `${f.rate(tr.rate_in)} · ${f.rate(tr.rate_out)}`);
    const every = f.num(src.poll_s || 5);
    const drawnNote = holding ? "The last list stays, dimmed, for a few seconds." : "Only your node is drawn.";
    let why = null, level = null, sub, aria;
    if (src.answering === false) {
      why = "YOUR NODE ISN'T ANSWERING · THE TREE COMES BACK WHEN IT DOES"; level = "bad";
      sub = "FROM YOUR NODE · NOT ANSWERING";
      aria = `Node tree: your node isn't answering, so its connections can't be seen. ${drawnNote}`;
    } else if (src.restricted) {
      why = "THIS RPC PORT HIDES THE PEER LIST · POINT THE DASHBOARD AT THE UNRESTRICTED PORT (SETTINGS → DATA SOURCE)";
      sub = "FROM YOUR NODE · PEER LIST HIDDEN BY THIS PORT";
      aria = `Node tree: this port hides the list of connections. ${drawnNote}`;
    } else if (!listKnown) {
      why = "THE PEER LIST DIDN'T COME THROUGH THIS TIME · TRYING AGAIN";
      sub = `FROM YOUR NODE · NO PEER LIST THIS READING · EVERY ${every} S`;
      aria = `Node tree: the list of connections didn't come through this time. ${drawnNote}`;
    } else {
      if (!n) {
        const alert = (Array.isArray(snap.alerts) ? snap.alerts : []).find((x) => x?.id === "peers");
        level = alert?.level === "bad" || alert?.level === "warn" ? alert.level : null;
        why = f.flag(level, "NO CONNECTIONS RIGHT NOW");
      }
      sub = `FROM YOUR NODE · ${f.plural(n, "CONNECTION", "CONNECTIONS")} · EVERY ${every} S · DOTS ARE REAL TRAFFIC`;
      aria = `Node tree: ${f.plural(n, "connection")}, ${f.num(node.out_peers)} outgoing, ${f.num(node.in_peers)} incoming, ${f.num(node.tor_out)} over Tor. Moving dots are data really moved with each peer.`;
    }
    if (holding) { why = null; level = null; }   // the last list is still drawn: no banner unless this lasts
    empty.hidden = !why;
    setText(empty, why ? f.flag(level, why) : "");
    setData(empty, "level", level);
    setText(env.sub, sub);
    const label = `${aria} Arrow keys step through what's drawn; Shift and the arrow keys turn it; plus and minus zoom.`;
    if (canvas.getAttribute("aria-label") !== label) canvas.setAttribute("aria-label", label);
  }

  return {
    update(snap, now) {
      // when the reading was taken, to the millisecond (in whole seconds from a server without it)
      const t = f.isNum(snap.t) ? snap.t : snap.generated || now;
      lastSnap = snap;
      pollS = Number(snap.source?.poll_s) || 5;
      listKnown = peerListKnown(snap);
      holding = !listKnown && !!listSnap && t >= knownAt && t - knownAt <= GRACE_S;
      readWarnings(snap);
      rebuild(listKnown ? snap : holding ? listSnap : null);
      // only a reading newer than the last one taken in has anything new to say: the
      // same one comes again when its screen is drawn again or after an error, and an
      // older one only when the clock was set back (the next reading goes on from it)
      if (lastT === null || t > lastT) chatter(snap, t);
      lastT = t;
      hud(snap);
      renderFeed();
      if (selected) { if (nodes.has(selected.id) && !selected.gone) fillCard(selected); else choose(null); }
      // a tooltip under a still pointer keeps up with the numbers too, and goes with its node
      if (tipAt) {
        if (hovered && hovered !== selected && !hovered.gone) env.tip.showTip(tipAt[0], tipAt[1], hovered.head(), hovered.rows());
        else { tipAt = null; env.tip.hideTip(); }
      }
      if (shown && !stage.running) stage.frame();
    },
    show() {
      shown = true;
      if (moving(look)) stage.start(); else stage.frame();
      renderFeed();
    },
    hide() {
      shown = false;
      stage.stop();
      env.tip.hideTip();
      particles = [];   // nothing in flight is kept to replay later
      hovered = null;
      tipAt = null;
      for (const n of [...nodes.values()]) if (n.gone) nodes.delete(n.id);
      order = order.filter((n) => nodes.has(n.id));
      leaves = leaves.filter((n) => nodes.has(n.id));
      drip();   // let the waiting lines out at once
    },
    look(lk) {
      look = lk;
      KIND_RGB.out = lk.accent;
      warnRGB = cssRGB("--warn-rgb", "255, 112, 64");
      spinBtn.hidden = !moving(lk);
      if (!moving(lk)) drip();
      if (!shown) return;
      if (moving(lk)) stage.start(); else { stage.stop(); stage.frame(); }
    },
  };
}
