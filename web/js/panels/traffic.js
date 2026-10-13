// G · Traffic: download / upload now, totals since the node started, and the
// last 15 minutes as a small chart (the server's history in 5 s points, the
// newest one moving with each reading).
import { $, setText, setFig } from "../dom.js";
import { lineChart } from "../charts.js";
import { addRow, addReading, meanOf } from "../rows.js";
import * as f from "../fmt.js";

const WINDOW = 900;
const FIELDS = ["rate_in", "rate_out"];
let buf = { t: [], rate_in: [], rate_out: [] };
const recent = [];   // the last minute of readings, for the big figures

export function seedTraffic(hist) {
  if (!hist || !Array.isArray(hist.t)) return;
  buf = { t: hist.t.slice(), step: hist.step, rate_in: (hist.rate_in || []).slice(), rate_out: (hist.rate_out || []).slice() };
}

// Every reading, whatever is on show. False when the chart can't simply take it in
// (points skipped, say after the computer slept) and wants the server's history again.
export function recordTraffic(snap) {
  const tr = snap.traffic || {};
  addReading(recent, snap.t ?? snap.generated, { rin: tr.rate_in, rout: tr.rate_out });
  if (!snap.hist) return true;
  const did = addRow(buf, snap.hist, FIELDS, WINDOW);
  return did !== "gap" && did !== "no";
}

export function renderTraffic(snap) {
  const tr = snap.traffic || {};
  // a second's rate jumps about with every block and batch of transactions: the big
  // figures are the last 5 seconds, the same as the chart's newest point
  setRate($("tr-in"), meanOf(recent, "rin"));
  setRate($("tr-out"), meanOf(recent, "rout"));
  setText($("tr-in-total"), f.isNum(tr.bytes_in) ? `${f.bytes(tr.bytes_in)} SINCE START` : "—");
  setText($("tr-out-total"), f.isNum(tr.bytes_out) ? `${f.bytes(tr.bytes_out)} SINCE START` : "—");
  const node = snap.node || {};
  // monerod's limits, in its own unit (kB/s of 1,024 bytes, as `limit` and the COMMANDS drawer show them)
  const kbps = (v) => (f.isNum(v) ? `${f.num(Math.round(v / 1024))} kB/s` : "—");
  const lim = f.isNum(node.limit_down) ? ` · LIMITS ${kbps(node.limit_down)} DOWN / ${kbps(node.limit_up)} UP` : "";
  const poll = snap.source?.poll_s;
  setText($("traffic-sub"), `FROM YOUR NODE · EVERY ${poll ?? "—"} S${f.isNum(poll) && poll < 5 ? " · RATES OVER 5 S" : ""}${lim}`);
  draw();
}

// big number with a smaller unit after it, so "65.7 kB/s" fits on a phone
const setRate = (node, v) => setFig(node, f.rate(v));

function draw() {
  lineChart($("tr-spark"), {
    t: buf.t, height: 110, label: "Traffic over the last 15 minutes", empty: "COLLECTING",
    yFmt: (v) => f.bytes(v).replace(" ", "") + "/s",
    series: [
      { key: "s1", name: "DOWN", values: buf.rate_in, fmt: f.rate },
      { key: "s2", name: "UP", values: buf.rate_out, fmt: f.rate },
    ],
  });
}

export function initTraffic() {
  window.addEventListener("resize", draw);
}
