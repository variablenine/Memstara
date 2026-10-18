// The server keeps its history in rows of a few seconds (5 s), and every snapshot
// carries the row still filling (snap.hist: {t: its start, step, field: the average
// so far}). These keep a chart's copy of /api/history current between requests, so
// its newest point moves with every reading, whatever screen is on show.

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

// Puts a snapshot's row into h ({t: [point starts], step, field: [values], ...}), at the
// point holding it: the newest point again is updated in place; a later one goes on the
// end, with an empty point for each one in between that never came (a gap stays a gap);
// points older than windowS fall off. Points longer than the row (readings every 10 or
// 30 s) take its values whole, which is right while each point holds one reading.
// Says what it did: "same" (the newest point again), "next" (the point after it),
// "first" (h was empty), "gap" (points skipped, or so many that h started again) or
// "no" (left alone: an older row, or points it doesn't fit). The last two are a cue to
// fetch the history again, which may have what the window missed.
export function addRow(h, row, fields, windowS) {
  if (!h || !row || !isNum(row.t)) return "no";
  const rstep = isNum(row.step) && row.step > 0 ? row.step : 5;
  const step = isNum(h.step) && h.step > 0 ? h.step : rstep;
  if (step % rstep) return "no";
  h.step = step;
  if (!Array.isArray(h.t)) h.t = [];
  for (const k of fields) if (!Array.isArray(h[k])) h[k] = h.t.map(() => null);
  const t = h.t;
  const at = Math.floor(row.t / step) * step;   // the start of the point holding the row
  const put = (i) => { for (const k of fields) h[k][i] = isNum(row[k]) ? row[k] : null; };
  const n = t.length;
  if (n && at === t[n - 1]) { put(n - 1); return "same"; }
  if (n && at < t[n - 1]) return "no";   // an older row (the clock went back): the next request sorts it out
  const did = !n ? "first" : at === t[n - 1] + step ? "next" : "gap";
  if (n && at - t[n - 1] > windowS) { t.length = 0; for (const k of fields) h[k].length = 0; }
  for (let s = t.length ? t[t.length - 1] + step : at; s <= at + 1e-6; s += step) {
    t.push(s);
    for (const k of fields) h[k].push(null);
  }
  put(t.length - 1);
  let drop = 0;
  while (drop < t.length - 1 && t[drop] <= at - windowS) drop++;
  if (drop) { t.splice(0, drop); for (const k of fields) h[k].splice(0, drop); }
  return did;
}

// The last minute of readings, newest last: {t, ...values}. The same reading twice
// (the window drew it again) is kept once.
export function addReading(list, t, vals, keepS = 60) {
  if (!isNum(t)) return;
  const last = list[list.length - 1];
  if (last && t < last.t) list.length = 0;           // the server's clock went back: start again
  else if (last && t === last.t) list.pop();
  list.push({ t, ...vals });
  while (list.length && list[0].t < t - keepS) list.shift();
}

// A reading as the big figures show it: the readings of the last `span` seconds
// averaged (one second's rate alone jumps about too much to read). Null while the
// newest reading has none (the node not answering).
export function meanOf(list, key, span = 5) {
  const last = list[list.length - 1];
  if (!last || !isNum(last[key])) return null;
  let sum = 0, n = 0;
  for (let i = list.length - 1; i >= 0 && list[i].t > last.t - span + 0.5; i--) {
    if (isNum(list[i][key])) { sum += list[i][key]; n++; }
  }
  return n ? sum / n : null;
}
