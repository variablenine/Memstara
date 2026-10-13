// H · History: connections, mempool and traffic over 15 min / 1 h / 6 h / 24 h.
// One range control above all three charts. Their newest point moves every second.
import { $, setText } from "../dom.js";
import { lineChart } from "../charts.js";
import { api } from "../api.js";
import { addRow } from "../rows.js";
import * as f from "../fmt.js";

let windowS = 3600;      // the range picked
let data = null;         // the last answer; data.window says which range it covers
let busy = false;
let again = false;       // asked again while a fetch was running
let askedAt = 0;         // when the last fetch started (performance.now())
// everything the server keeps a history of (memstara/history.py FIELDS)
const FIELDS = ["out", "in", "tor", "pool", "rate_in", "rate_out", "latency", "cpu_temp_c", "node_cpu_pct"];

const LABEL = { 900: "15 MIN", 3600: "1 H", 21600: "6 H", 86400: "24 H" };

export function initHistory() {
  const ctl = $("hist-range");
  for (const b of ctl.querySelectorAll("button")) {
    b.addEventListener("click", () => {
      windowS = Number(b.dataset.w);
      for (const o of ctl.querySelectorAll("button")) o.setAttribute("aria-checked", String(o === b));
      refreshHistory();
    });
  }
  window.addEventListener("resize", draw);
}

export async function refreshHistory() {
  if (busy) { again = true; return; }
  busy = true;
  try {
    do {
      again = false;
      askedAt = performance.now();
      const want = windowS;
      const d = await api.history(want);
      // a quicker click may have picked another range meanwhile: fetch that one instead
      if (want !== windowS) { again = true; continue; }
      data = d;
      draw();
    } while (again);
  } catch {
    /* keep the last charts; the connection banner says what's wrong */
  } finally {
    busy = false;
  }
}

// Every reading. 15 MIN has a point per 5 s row, so the reading's row goes straight in
// and its newest point moves with each reading. Longer ranges average many rows into a
// point, which only the server can do, so while they're on show they're asked for
// again: every second for 1 H (a point every 10 s), every 5 s for 6 H and 24 H, whose
// newest point a second barely moves.
export function recordHistory(snap, on) {
  if (!data || !snap.hist) return;
  if (data.window === 900) {
    const did = addRow(data, snap.hist, FIELDS, 900);
    if (!on) return;
    if (did === "gap" || did === "no") refreshHistory();   // the server may have what the window missed
    else draw();
    return;
  }
  if (on && performance.now() - askedAt >= (data.step <= 10 ? 900 : 4900)) refreshHistory();
}

function draw() {
  if (!data) return;
  const t = data.t || [];
  const w = f.isNum(data.window) ? data.window : null;
  const shown = LABEL[w] || (w ? f.dur(w).toUpperCase() : "—");
  setText($("history-sub"), `KEPT FOR 24 H · SHOWING ${shown} · ${f.plural(t.length, "POINT", "POINTS")}${data.step ? " · ONE PER " + f.dur(data.step).toUpperCase() : ""}`);
  lineChart($("hc-peers"), {
    t, height: 130, label: "Connections over time",
    series: [
      { key: "s1", name: "OUTGOING", values: data.out || [] },
      { key: "s2", name: "INCOMING", values: data.in || [] },
    ],
  });
  lineChart($("hc-pool"), {
    t, height: 130, label: "Mempool transactions over time",
    series: [{ key: "s1", name: "WAITING", values: data.pool || [] }],
  });
  lineChart($("hc-traffic"), {
    t, height: 130, label: "Traffic over time",
    yFmt: (v) => f.bytes(v).replace(" ", "") + "/s",
    series: [
      { key: "s1", name: "DOWN", values: data.rate_in || [], fmt: f.rate },
      { key: "s2", name: "UP", values: data.rate_out || [], fmt: f.rate },
    ],
  });
}
