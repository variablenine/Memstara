// Top bar, connection banner and the bottom status bar / nameplate.
import { $, setText, setData, clear, el } from "../dom.js";
import * as f from "../fmt.js";

let lastRpc = null;   // the node address the readings came from, for the chip when the server is lost

// the chip saying where the numbers come from: "LIVE · 127.0.0.1:18081" (solid,
// in the accent) only while the node is answering; otherwise dashed and in words
function setMode(mode, word, where, note, title) {
  const chip = $("mode");
  setData(chip, "mode", mode);
  setText(chip.querySelector(".mode-word"), word);
  setText(chip.querySelector(".mode-where"), where ? ` · ${where}` : "");
  setText(chip.querySelector(".mode-note"), note ? ` · ${note}` : "");
  chip.title = title;
}

export function renderHeader(snap) {
  const src = snap.source || {};
  const node = snap.node || {};
  const parts = [];
  if (node.nettype) parts.push(String(node.nettype).toUpperCase());
  if (f.isNum(node.uptime_s)) parts.push("NODE UP " + f.dur(node.uptime_s));
  if (node.version) parts.push("MONEROD " + String(node.version).replace(/-release$/, ""));
  // a part never breaks inside itself ("NODE UP 3 d 06 h"), only between parts
  setText($("tb-sub"), parts.map((p) => p.replace(/ /g, "\u00a0")).join(" · ") || "WAITING FOR THE NODE");

  // overall state
  const ov = $("overall");
  const alerts = snap.alerts || [];
  const bad = alerts.filter((a) => a.level === "bad").length;
  const warn = alerts.filter((a) => a.level === "warn").length;
  const level = snap.level || (bad ? "bad" : warn ? "warn" : "ok");
  setData(ov, "level", level);
  const [glyph, word, count] =
    level === "bad" ? ["■", "PROBLEM", bad > 1 ? `× ${bad}` : ""] :
    level === "warn" ? ["▲", "HEADS-UP", warn > 1 ? `× ${warn}` : ""] :
    ["●", "ALL CLEAR", ""];
  setText(ov.querySelector(".ov-glyph"), glyph);
  setText(ov.querySelector(".ov-word"), word);
  setText(ov.querySelector(".ov-count"), count);

  // where the numbers come from. One missed answer among readings every second isn't
  // the node going away: the chip and the banner wait until it has missed 3 s of them
  const t = f.isNum(snap.t) ? snap.t : snap.generated;
  const lost = src.answering === false && !(f.isNum(src.last_ok) && src.last_ok > 0 && t - src.last_ok < 3);
  lastRpc = src.rpc || lastRpc;
  if (lost) setMode("lost", "NOT ANSWERING", src.rpc || "—", "", "Your node isn't answering, so nothing here is live");
  else setMode("live", "LIVE", src.rpc || "—", src.restricted ? "LIMITED" : "",
    src.restricted ? "A restricted RPC port: peers, bans, traffic and disk space are hidden" : "Reading your node");

  // status bar: its words are written in place, so the address can be selected and copied
  const bits = [
    ["RPC", src.rpc || "—"],
    ["ANSWERED IN", f.isNum(src.latency_ms) ? `${Math.round(src.latency_ms)} ms` : "—"],
    ["POLL", f.isNum(src.poll_s) ? `${src.poll_s} S` : "—"],
    ["LAST", f.clock(t)],
  ];
  const vit = $("sb-vitals");
  const labels = bits.map((b) => b[0]).join("|");
  if (vit.dataset.labels !== labels) {
    vit.dataset.labels = labels;
    clear(vit);
    bits.forEach(([k], i) => { if (i) vit.append(" · "); vit.append(el("span", { text: k + " " }), el("b")); });
  }
  const vals = vit.querySelectorAll("b");
  bits.forEach(([, v], i) => setText(vals[i], v));

  const plate = [
    `MEMSTARA v${src.dashboard_version || "—"}`,
    node.version ? `MONEROD ${node.version}` : null,
    f.isNum(node.hf_version) ? `RULES v${node.hf_version}` : null,
    node.rpc_version ? `RPC v${node.rpc_version}` : null,
  ].filter(Boolean);
  setText($("sb-plate"), plate.join(" · "));

  // connection banner (node side): a problem as an alert; a restricted port as a calm note,
  // which screen readers read when they get to it
  const banner = $("conn-banner");
  const note = $("conn-note");
  banner.hidden = !lost;
  note.hidden = lost || !src.restricted;
  if (lost) {
    setData(banner, "level", "bad");
    const since = f.isNum(src.last_ok) && src.last_ok > 0 ? ` · LAST ANSWER ${f.dayClock(src.last_ok)}` : "";
    setText(banner, `■ THE NODE ISN'T ANSWERING${src.error ? " · " + String(src.error).toUpperCase() : ""}${since}`);
  } else if (src.restricted) {
    setText(note, "· A RESTRICTED RPC PORT: MONEROD HIDES PEERS, BANS, TRAFFIC AND DISK SPACE HERE. TO SEE THEM, PUT YOUR NODE'S UNRESTRICTED PORT IN SETTINGS → DATA SOURCE.");
  }
}

export function renderServerLost(lastGood) {
  const banner = $("conn-banner");
  $("conn-note").hidden = true;
  banner.hidden = false;
  setData(banner, "level", "bad");
  setText(banner, `■ THE DASHBOARD SERVER ISN'T ANSWERING${lastGood ? " · LAST DATA " + f.dayClock(lastGood) : ""} · IS python3 -m memstara STILL RUNNING?`);
  const ov = $("overall");
  setData(ov, "level", "lost");
  setText(ov.querySelector(".ov-glyph"), "◌");
  setText(ov.querySelector(".ov-word"), "NO DATA");
  setText(ov.querySelector(".ov-count"), "");
  setMode("lost", "NOT LIVE", lastRpc, "", "The dashboard server isn't answering, so nothing here is live");
}

export function tickClock(nowServer) {
  setText($("clock").querySelector(".clock-time"), f.clock(nowServer));
}
