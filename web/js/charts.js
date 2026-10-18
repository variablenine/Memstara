// Small SVG line charts: hairline grid, 2px lines, 10% area wash for a single
// series, end dot + end label, crosshair tooltip listing every series.
import { svg, clear, widthOf, refocusing } from "./dom.js";
import { showTip, hideTip } from "./tooltip.js";
import * as f from "./fmt.js";

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

// The drawing in each chart's slot, kept from one reading to the next: only the lines
// and labels inside are drawn again, so the reading under the pointer (or at the
// keyboard's place) stays open and is filled in again with each new reading.
const kept = new WeakMap();   // container -> {root, body, cross, hit, st}
let reading = null;           // the chart whose reading is open, if any

// opts: { t: [unix], series: [{key: "s1", name, values: [], fmt}], height, yFmt, empty, label }
// The chart is one Tab stop: focus shows the newest reading, the arrow keys
// (Home / End) move it, and a tap or click reads the moment under it. A
// redraw keeps focus and the moment being read.
export function lineChart(container, opts) {
  const width = widthOf(container, 360);
  const height = opts.height || 120;
  const t = opts.t || [];
  const n = t.length;

  let maxV = 0;
  for (const s of opts.series) for (const v of s.values) if (f.isNum(v) && v > maxV) maxV = v;
  const top = niceMax(maxV);
  const hasData = n > 1 && opts.series.some((s) => s.values.some(f.isNum));
  const yFmt = opts.yFmt || ((v) => f.num(v));
  const ticks = [0, 0.5, 1].map((frac) => ({ frac, text: yFmt(top * frac) }));
  // room on the left for the longest tick ("2.5MB/s"): 9.5px mono is about 5.8px a character
  const longest = hasData ? Math.max(...ticks.map((k) => String(k.text).length)) : 0;
  const m = { l: Math.max(40, Math.ceil(longest * 5.8) + 10), r: 50, t: 8, b: 18 };
  const iw = Math.max(40, width - m.l - m.r);
  const ih = Math.max(30, height - m.t - m.b);
  const label = opts.label || "Chart";
  const x = (i) => m.l + (n <= 1 ? iw : (i / (n - 1)) * iw);
  const y = (v) => m.t + ih - (v / top) * ih;

  let c = kept.get(container);
  if (!c || c.root.parentNode !== container) c = build(container);
  const { root, body, cross, hit, st } = c;
  // children stay reachable (a role="img" would hide the focusable reading from screen readers)
  setAttr(root, "viewBox", `0 0 ${width} ${height}`);
  setAttr(root, "height", height);
  setAttr(root, "aria-label", label);
  const parts = [];

  // grid + y ticks (no tick numbers on an empty chart: they'd be made up)
  for (const k of ticks) {
    const yy = Math.round(m.t + ih - k.frac * ih) + 0.5;
    parts.push(svg("line", { class: k.frac === 0 ? "c-axis" : "c-grid", x1: m.l, x2: m.l + iw, y1: yy, y2: yy }));
    if (hasData) parts.push(svg("text", { class: "c-tick", x: m.l - 6, y: yy + 3, "text-anchor": "end", text: k.text }));
  }

  if (!hasData) {
    parts.push(svg("text", { class: "c-empty", x: m.l + iw / 2, y: m.t + ih / 2 + 4, "text-anchor": "middle", text: opts.empty || "NO DATA YET" }));
    body.replaceChildren(...parts);
    st.n = 0;
    if (reading === c) { reading = null; hideTip(); }
    cross.setAttribute("visibility", "hidden");
    hit.setAttribute("display", "none");
    return;
  }

  // x ticks: start, middle, end
  for (const i of [0, Math.floor((n - 1) / 2), n - 1]) {
    const anchor = i === 0 ? "start" : i === n - 1 ? "end" : "middle";
    parts.push(svg("text", { class: "c-tick", x: x(i), y: height - 4, "text-anchor": anchor, text: f.clock(t[i], false) }));
  }

  const single = opts.series.length === 1;
  const endLabels = [];
  for (const s of opts.series) {
    // split into runs so gaps (null) stay gaps
    let d = "";
    let area = "";
    let run = [];
    const flush = () => {
      if (run.length) {
        d += run.map(([xx, yy], k) => `${k ? "L" : "M"}${xx.toFixed(1)},${yy.toFixed(1)}`).join("");
        if (single && run.length > 1) {
          area += `M${run[0][0].toFixed(1)},${(m.t + ih).toFixed(1)}` + run.map(([xx, yy]) => `L${xx.toFixed(1)},${yy.toFixed(1)}`).join("") + `L${run[run.length - 1][0].toFixed(1)},${(m.t + ih).toFixed(1)}Z`;
        }
      }
      run = [];
    };
    s.values.forEach((v, i) => {
      if (f.isNum(v)) run.push([x(i), y(v)]);
      else flush();
    });
    flush();
    if (area) parts.push(svg("path", { class: `c-area ${s.key}`, d: area }));
    parts.push(svg("path", { class: `c-line ${s.key}`, d }));
    // end dot + label at the last known value
    for (let i = n - 1; i >= 0; i--) {
      if (f.isNum(s.values[i])) {
        parts.push(svg("circle", { class: `c-end ${s.key}`, cx: x(i), cy: y(s.values[i]), r: 4 }));
        endLabels.push({ y: y(s.values[i]), x: x(i), text: (s.fmt || f.num)(s.values[i]) });
        break;
      }
    }
  }
  // end labels: if two collide, keep the legend + tooltip instead of stacking
  const collide = endLabels.length === 2 && Math.abs(endLabels[0].y - endLabels[1].y) < 11;
  if (!collide) {
    for (const l of endLabels) parts.push(svg("text", { class: "c-endlabel", x: l.x + 8, y: l.y + 3.5, text: l.text }));
  }
  body.replaceChildren(...parts);

  // crosshair + tooltip
  const valueAt = (i) => opts.series.map((s) => ({ key: s.key, label: s.name, value: f.isNum(s.values[i]) ? (s.fmt || f.num)(s.values[i]) : "—" }));
  // the newest moment with a number in it (the very last one can still be empty)
  let newest = n - 1;
  while (newest > 0 && !opts.series.some((s) => f.isNum(s.values[newest]))) newest--;
  const latest = valueAt(newest).map((v) => `${v.label} ${v.value}`).join(", ");
  setAttr(cross, "y1", m.t);
  setAttr(cross, "y2", m.t + ih);
  hit.removeAttribute("display");
  for (const [k, v] of [["x", m.l], ["y", m.t], ["width", iw], ["height", ih]]) setAttr(hit, k, v);
  setAttr(hit, "aria-label", `${label}. At ${f.clock(t[newest], false)}: ${latest}. The arrow keys move the reading.`);
  Object.assign(st, { n, t, width, height, m, iw, x, valueAt, newest });
  if (st.cur >= n) st.cur = newest;

  // the reading on show moves with the new numbers: under a still pointer, the moment
  // there; from the keyboard or a tap, the same moment (or the newest, if that was it).
  // A chart no longer on screen (its screen changed under a still finger) closes it.
  if (reading === c && cross.getAttribute("visibility") === "visible") {
    if (!container.getClientRects().length) c.hide();
    else if (st.pointer) c.fromPointer(st.pointer);
    else c.showAt(c.recalled());
  } else if (document.activeElement === hit) st.cur = c.recalled();
}

function setAttr(node, k, v) {
  const s = String(v);
  if (node.getAttribute(k) !== s) node.setAttribute(k, s);
}

// The parts that stay: the svg, the lines' group, the crosshair and the hit area with
// its listeners, which read the newest drawing's numbers from st.
function build(container) {
  const root = svg("svg", { role: "group" });
  const body = svg("g");
  const cross = svg("line", { class: "c-cross", x1: 0, x2: 0, visibility: "hidden" });
  const hit = svg("rect", { class: "c-hit", tabindex: 0, role: "img" });
  root.append(body, cross, hit);
  const st = { n: 0, cur: 0, pointer: null };
  const c = { root, body, cross, hit, st };
  const at = (clientX) => {
    const r = root.getBoundingClientRect();
    const px = ((clientX - r.left) / r.width) * st.width;
    return Math.max(0, Math.min(st.n - 1, Math.round(((px - st.m.l) / st.iw) * (st.n - 1))));
  };
  // the moment being read, kept on the container so a redraw can go back to it
  const remember = (i) => { container.dataset.readT = i >= st.newest ? "end" : String(st.t[i]); };
  c.recalled = () => {
    const rt = Number(container.dataset.readT);
    if (!container.dataset.readT || container.dataset.readT === "end" || !Number.isFinite(rt)) return st.newest;
    const i = st.t.findIndex((v) => v >= rt);
    return i < 0 ? st.newest : i;
  };
  const show = (i, cx, cy) => {
    st.cur = i;
    setAttr(cross, "x1", st.x(i));
    setAttr(cross, "x2", st.x(i));
    setAttr(cross, "visibility", "visible");
    reading = c;
    showTip(cx, cy, f.clock(st.t[i], false), st.valueAt(i));
  };
  c.showAt = (i) => {
    const r = root.getBoundingClientRect();
    show(i, r.left + (st.x(i) / st.width) * r.width, r.top + (st.m.t / st.height) * r.height);
  };
  const hide = c.hide = () => {
    st.pointer = null;
    cross.setAttribute("visibility", "hidden");
    if (reading === c) { reading = null; hideTip(); }
  };
  let downAt = -Infinity;
  c.fromPointer = (e) => {
    if (!st.n) return 0;
    st.pointer = { clientX: e.clientX, clientY: e.clientY };
    const i = at(e.clientX);
    show(i, e.clientX, e.clientY);
    return i;
  };
  hit.addEventListener("pointermove", c.fromPointer);
  // a tap on a touch screen has no pointermove: read where it lands
  hit.addEventListener("pointerdown", (e) => { downAt = performance.now(); remember(c.fromPointer(e)); if (e.pointerType === "touch") st.pointer = null; });
  hit.addEventListener("click", (e) => { const touch = !st.pointer; remember(c.fromPointer(e)); if (touch) st.pointer = null; });
  // a finger lifting counts as leaving; keep the reading until focus moves on
  hit.addEventListener("pointerleave", (e) => {
    if (e.pointerType !== "touch") hide();
    else st.pointer = null;
  });
  hit.addEventListener("focus", () => {
    if (refocusing || performance.now() - downAt < 1000) return;   // a click or tap shows its own spot
    c.showAt(c.recalled());
  });
  hit.addEventListener("blur", () => { hide(); delete container.dataset.readT; });
  hit.addEventListener("keydown", (e) => {
    const next = { ArrowRight: st.cur + 1, ArrowLeft: st.cur - 1, Home: 0, End: st.n - 1 }[e.key];
    if (next === undefined || !st.n) return;
    e.preventDefault();
    st.pointer = null;
    const i = Math.max(0, Math.min(st.n - 1, next));
    remember(i);
    c.showAt(i);
  });
  clear(container).append(root);
  kept.set(container, c);
  return c;
}
