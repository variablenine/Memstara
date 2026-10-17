// Memstara: start-up, following snapshots and drawing. The screens only
// ever read the app's server's latest snapshot; the server does the node polling,
// and answers the window's open request the moment a new snapshot is in.
import { $ } from "./dom.js";
import { refreshTip } from "./tooltip.js";
import { api } from "./api.js";
import { setClockMode, setTempUnit, setUtc, zoneWord } from "./fmt.js";
import { startPrivacy, initPrivacySwitch, followPrivacy, learnHidden, setPrivacyMotion, arrive } from "./privacy.js";
import { renderHeader, renderServerLost, tickClock } from "./panels/header.js";
import { renderAlerts } from "./panels/alerts.js";
import { initLamps, renderLamps, setLampSettings, ackAll, lampTest } from "./panels/lamps.js";
import { initChain, renderChain, tickChain, setChainSettings } from "./panels/chain.js";
import { initPeers, renderPeers, setPeerSettings } from "./panels/peers.js";
import { renderRoute } from "./panels/route.js";
import { initMempool, renderMempool } from "./panels/mempool.js";
import { initTraffic, renderTraffic, seedTraffic, recordTraffic } from "./panels/traffic.js";
import { initHistory, refreshHistory, recordHistory } from "./panels/history.js";
import { renderSystem, setSystemSettings } from "./panels/system.js";
import { initFlood, renderFlood, setFloodSettings } from "./panels/flood.js";
import { renderEvents } from "./panels/events.js";
import { initComputer, renderComputer, seedComputer, recordComputer } from "./panels/computer.js";
import { initControls, renderControls, setControlSettings } from "./panels/controls.js";
import { initConfirm } from "./confirm.js";
import { initCommands, openCommands, commandsOpen, setCommandSettings } from "./commands.js";
import { initSettings, loadSettings, savedValues, openSettings, isOpen } from "./settings.js";
import { runBoot } from "./boot.js";
import { initTabs, selectScreen, setLayout, screenShown, screenOf, flagScreens, SCREENS } from "./tabs.js";
import { initHeroes, updateHeroes, showHeroes, lookHeroes } from "./heroes/index.js";
import { cssRGB } from "./fx/space.js";

// how long the server may hold a request for the next snapshot, and how long the window
// gives it before trying again (in case an answer never comes)
const WAIT_S = 20;
const GIVE_UP_MS = (WAIT_S + 10) * 1000;

const state = { snap: null, seq: null, offset: 0, lastGood: null, following: false, wake: null, failures: 0,
  mode: null, keys: false, drawnAt: 0, late: false,
  look: { motion: "full", glow: true, accent: "255, 184, 64", head: "107, 174, 200", ink: "232, 232, 240" } };

const now = () => Date.now() / 1000 + state.offset;

// with every screen on one page, a panel is drawn only while it is on screen or near it
const panelSeen = new Map();   // panel id -> in view (not known yet counts as yes)
const panelWatch = "IntersectionObserver" in window
  ? new IntersectionObserver((entries) => {
    let came = false;
    for (const e of entries) {
      const id = e.target.dataset.panel;
      if (e.isIntersecting && panelSeen.get(id) === false) {
        came = true;
        if (id === "history") refreshHistory();
      }
      panelSeen.set(id, e.isIntersecting);
    }
    if (came) queueDraw();   // one scrolled into view: draw it now, not at the next reading
  }, { rootMargin: "200px 0px" })
  : null;

// a panel is drawn only while it is turned on and its screen is on show
function panelOn(id) {
  const sec = document.querySelector(`[data-panel="${id}"]`);
  if (!sec || sec.hidden || !screenShown(screenOf(sec))) return false;
  return document.documentElement.dataset.layout !== "page" || panelSeen.get(id) !== false;
}

let drawQueued = false;
function queueDraw() {
  if (drawQueued || !state.snap) return;
  drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; if (state.snap) draw(state.snap); });
}

// one panel failing on some reading neither stops the others nor looks like the server
// going away; its warning is logged once (again only when the message changes)
const warned = new Map();
function guard(id, fn) {
  try {
    return fn();
  } catch (e) {
    const msg = `panel ${id}: ${e && e.message}`;
    if (warned.get(id) !== msg) { warned.set(id, msg); console.warn(msg); }
    return undefined;
  }
}

function heroesFollow() {
  showHeroes((key) => screenShown(key));
}

function applySettings(v) {
  if (!v) return;
  const root = document.documentElement;
  root.dataset.accent = v["display.accent"] || "amber";
  root.dataset.scanlines = v["display.scanlines"] === false ? "off" : "on";
  root.dataset.glow = v["display.glow"] === false ? "off" : "on";
  root.dataset.motion = v["display.motion"] || "full";
  root.dataset.density = v["display.density"] || "comfortable";
  root.style.setProperty("--scale", String((Number(v["display.text_scale"]) || 100) / 100));
  setClockMode(v["display.clock"]);
  setTempUnit(v["display.temp_unit"] || "auto");
  state.keys = v["display.keys"] === true;
  const bg = getComputedStyle(root).getPropertyValue("--bg").trim();
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta && bg) meta.setAttribute("content", bg);
  for (const sec of document.querySelectorAll("[data-panel]")) sec.hidden = v[`panels.${sec.dataset.panel}`] === false;
  setLampSettings(v);
  setChainSettings(v);
  setPeerSettings(v);
  setSystemSettings(v);
  setFloodSettings(v);
  setControlSettings(v);
  setCommandSettings(v);
  setLayout(v["display.layout"]);
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "reduced" : (v["display.motion"] || "full");
  setPrivacyMotion(motion);
  state.look = { motion, glow: v["display.glow"] !== false,
    accent: getComputedStyle(root).getPropertyValue("--accent-rgb").trim() || state.look.accent,
    head: cssRGB("--head", "107, 174, 200"), ink: cssRGB("--ink", "232, 232, 240") };
  lookHeroes(state.look);
  heroesFollow();
  if (state.snap) draw(state.snap);
}

function draw(snap) {
  state.drawnAt = performance.now();
  guard("header", () => renderHeader(snap));
  guard("alerts", () => renderAlerts(snap, now()));
  guard("tabs", () => flagScreens(snap.alerts));
  updateHeroes(snap, now());   // (each hero is guarded on its own)
  const panel = (id, fn) => { if (panelOn(id)) guard(id, fn); };
  panel("lamps", () => renderLamps(snap));
  panel("chain", () => renderChain(snap, state.offset));
  panel("peers", () => renderPeers(snap));
  panel("route", () => renderRoute(snap));
  panel("mempool", () => renderMempool(snap));
  panel("traffic", () => renderTraffic(snap));
  panel("system", () => renderSystem(snap));
  panel("flood", () => renderFlood(snap));
  panel("events", () => renderEvents(snap));
  panel("computer", () => renderComputer(snap));
  panel("controls", () => renderControls(snap));
  guard("tooltip", refreshTip);   // details left open on a lamp, a row or a dot move with the rest
}

function markStale(on) {
  for (const p of document.querySelectorAll(".panel")) p.classList.toggle("is-stale", on);
}

// the server's clock against this window's (the same computer's, so near 0): each reading
// gets here a little after it was taken, so every estimate is a little low and the
// highest recent one is nearest; it drifts down slowly, and a big change (a clock set) is
// taken at once
function setOffset(snap) {
  const t = typeof snap.t === "number" ? snap.t : snap.generated;
  if (typeof t !== "number") return;
  const off = t - Date.now() / 1000;
  state.offset = Math.abs(off - state.offset) > 3 ? off : Math.max(off, state.offset - 0.002);
}

// a new reading
function onSnapshot(snap) {
  // privacy mode switched in another window: this one starts again in the new mode, and
  // this reading isn't drawn
  if (followPrivacy(snap)) return;
  learnHidden(snap);
  if (typeof snap.seq === "number") state.seq = snap.seq;
  setOffset(snap);
  const modeChanged = state.mode !== null && state.mode !== snap.source?.mode;
  state.mode = snap.source?.mode ?? null;
  state.snap = snap;
  state.lastGood = snap.generated;
  state.failures = 0;
  state.late = false;
  markStale(false);
  // the small charts keep every reading, whatever is on show
  const a = guard("traffic", () => recordTraffic(snap));
  const b = guard("computer", () => recordComputer(snap));
  if (a === false || b === false) seedPanels();
  guard("history", () => recordHistory(snap, panelOn("history")));
  draw(snap);
  if (modeChanged) refreshHistory();
}

// the server answered again with the reading already drawn (after an error): only the
// error goes; the screens already show this reading
function recovered(snap) {
  state.failures = 0;
  state.late = false;
  markStale(false);
  guard("header", () => renderHeader(snap));
}

// the small 15-minute charts from the server's history (at start, after the window was
// hidden, and when the readings can't simply be added on)
let seededAt = -Infinity;
function seedPanels(force = false) {
  if (!force && Date.now() - seededAt < 15000) return;
  seededAt = Date.now();
  api.history(900).then((h) => {
    seedTraffic(h);
    seedComputer(h);
    if (!state.snap) return;
    guard("traffic", () => recordTraffic(state.snap));
    guard("computer", () => recordComputer(state.snap));
    if (panelOn("traffic")) guard("traffic", () => renderTraffic(state.snap));
    if (panelOn("computer")) guard("computer", () => renderComputer(state.snap));
  }).catch(() => {});
}

// a pause that wake() ends early
function pause(ms) {
  return new Promise((resolve) => {
    const t = setTimeout(() => { state.wake = null; resolve(); }, ms);
    state.wake = () => { clearTimeout(t); state.wake = null; resolve(); };
  });
}

// Follows the snapshots: one request at a time, each held by the server until the next
// snapshot is in, then drawn at once. Stops while the window is hidden (nothing is drawn
// then) and starts again when it shows.
async function follow() {
  if (state.following) return;
  state.following = true;
  try {
    while (!document.hidden) {
      const asked = Date.now();
      const stop = new AbortController();
      const giveUp = setTimeout(() => stop.abort(), GIVE_UP_MS);
      try {
        const snap = await api.snapshot(state.seq ?? -1, WAIT_S, stop.signal);
        if (snap.seq !== state.seq) onSnapshot(snap);
        else {
          if (state.failures) recovered(snap);
          if (Date.now() - asked < 250) await pause(1000);   // answered at once with nothing new: don't spin
        }
      } catch {
        state.failures++;
        // two errors in a row, or a whole wait with no answer at all (a server that takes
        // the request but never answers): the server is lost
        if (state.failures >= 2 || Date.now() - asked >= GIVE_UP_MS - 100) {
          renderServerLost(state.lastGood);
          markStale(true);
        }
        await pause(Math.min(5000, 1000 * state.failures));
      } finally {
        clearTimeout(giveUp);
      }
    }
  } finally {
    state.following = false;
  }
}

// something just changed (a command, saved settings): the server reads the node again at
// once and its answer comes on the open request; this only cuts short a pause after an error
function refresh() {
  if (state.wake) state.wake();
  follow();
}

// once a second, just after each whole second of the server's clock (so the clock never
// skips or repeats one): the clock, the ages that count up between readings, and a
// stale mark when the readings have stopped coming (the server's polling stuck)
let ticker = 0;
function tick() {
  clearTimeout(ticker);
  guard("clock", () => tickClock(now()));
  const snap = state.snap;
  if (snap) {
    if (performance.now() - state.drawnAt > 500) {   // a reading drawn just now already moved these on
      guard("alerts", () => renderAlerts(snap, now()));
      if (panelOn("chain")) guard("chain", () => tickChain());
    }
    const age = now() - (typeof snap.t === "number" ? snap.t : snap.generated);
    const late = age > Math.max(10, 3 * (Number(snap.source?.poll_s) || 1));
    if (late !== state.late) {
      state.late = late;
      if (!state.failures) markStale(late);
    }
  }
  ticker = setTimeout(tick, 1015 - ((now() * 1000) % 1000));
}

function onKey(e) {
  if (!state.keys || isOpen() || commandsOpen() || document.getElementById("confirm").open || e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = (e.target && e.target.tagName) || "";
  if (/INPUT|SELECT|TEXTAREA/.test(tag)) return;
  const k = e.key.toLowerCase();
  if (k === "s") { e.preventDefault(); openSettings(); }
  else if (k === "c") { e.preventDefault(); openCommands(); }
  else if (k === "a") ackAll();
  else if (k === "t") lampTest();
  else if (/^[1-8]$/.test(k)) selectScreen(SCREENS[Number(k) - 1], { scroll: true });
}

function bootWanted(v) {
  if (!v || v["display.boot"] === false || v["display.motion"] === "off") return false;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return false;
  try {
    if (sessionStorage.getItem("memstara-booted")) return false;
    sessionStorage.setItem("memstara-booted", "1");
  } catch { /* storage blocked: show it */ }
  return true;
}

// a screen came into view: draw its panels now (charts measure their width) and wake its hero
function onScreen() {
  heroesFollow();
  if (!state.snap) return;
  draw(state.snap);
  if (panelOn("history")) refreshHistory();
}

async function start() {
  // privacy mode first: it decides how everything after this is drawn
  let settings = null;
  try { settings = await api.settings(); } catch { /* the banner will say the server isn't answering */ }
  const privacy = settings?.values?.["display.privacy"] === true;
  startPrivacy(privacy);
  setUtc(privacy);
  $("clock").querySelector(".clock-tag").textContent = zoneWord();
  arrive();

  initTabs({ onChange: onScreen, layout: "tabs" });
  initLamps();
  initChain();
  initPeers();
  initMempool();
  initTraffic();
  initHistory();
  initComputer();
  initFlood();
  initConfirm();
  initCommands({ changed: () => refresh() });
  initControls({ changed: () => refresh(), openCommands: () => openCommands() });
  initSettings({
    onApply: (v) => applySettings(v),
    onSaved: (v) => { applySettings(v); refresh(); refreshHistory(); },
  });

  initHeroes({ now, look: () => state.look, goScreen: (id) => selectScreen(id, { scroll: true }) });
  initPrivacySwitch();

  if (settings) {
    try {
      loadSettings(settings);
      applySettings(savedValues());
    } catch { /* drawn with the defaults */ }
  }

  // the first real reading (a server that has just started has none yet: it comes within a second or so)
  const first = api.snapshot(0, 3);
  const booted = bootWanted(savedValues());
  if (booted) await runBoot(first, settings);
  try { onSnapshot(await first); } catch { renderServerLost(null); }
  if (booted && panelOn("lamps")) lampTest();

  seedPanels(true);
  refreshHistory();
  if (panelWatch) for (const sec of document.querySelectorAll("[data-panel]")) panelWatch.observe(sec);

  follow();
  setInterval(() => { if (!document.hidden && panelOn("history")) refreshHistory(); }, 30000);
  document.addEventListener("visibilitychange", () => {
    heroesFollow();
    // back from hidden (nothing was read meanwhile): the charts fill the time in from the server
    if (!document.hidden) { refresh(); refreshHistory(); seedPanels(true); }
  });
  window.addEventListener("keydown", onKey);
  tick();
}

start();
