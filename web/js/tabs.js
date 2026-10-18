// Screens: the tab bar under the top bar. One screen shows at a time
// (display.layout "tabs"), or all of them stack on one page ("page").
// The open screen is kept in the address (#network) so a reload comes back
// to it, and in the window's storage so the app opens on it next time.
import { $ } from "./dom.js";

export const SCREENS = ["status", "network", "chain", "mempool", "traffic", "bans", "log"];

// which screen an alert belongs to; STATUS also shows the worst of all of them
const ALERT_SCREEN = {
  node_down: "status", restarts: "status",
  peers: "network", spread_out: "network", spread_in: "network", tor: "network", spy: "network",
  syncing: "chain", no_blocks: "chain", reorg: "chain", disk: "chain", flood: "chain",
  double_spend: "mempool",
};
const STORE = "memstara-screen";

const state = { current: "status", layout: "tabs", onChange: () => {} };
let spyLock = 0;        // a tab click scrolled the page: the scroll-spy waits until then
let spyQueued = false;

function fromHash() {
  const h = decodeURIComponent(location.hash.replace(/^#/, "")).toLowerCase();
  return SCREENS.includes(h) ? h : null;
}

function stored() {
  try {
    const v = localStorage.getItem(STORE);
    return SCREENS.includes(v) ? v : null;
  } catch {
    return null;
  }
}

const motionOn = () => document.documentElement.dataset.motion === "full"
  && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function currentScreen() {
  return state.current;
}

// is this screen on show (always, when every screen is on one page)
export function screenShown(id) {
  return state.layout === "page" || id === state.current;
}

// the screen a panel or element lives on
export function screenOf(node) {
  return node?.closest?.(".screen")?.dataset.screen || null;
}

// the tab bar scrolls sideways on a narrow screen: keep the open tab in the
// middle of it (only the bar scrolls, never the page)
function revealTab(smooth) {
  const bar = $("tabs");
  if (bar.scrollWidth <= bar.clientWidth + 1) return;
  const tab = $(`tab-${state.current}`);
  const b = bar.getBoundingClientRect(), r = tab.getBoundingClientRect();
  const left = bar.scrollLeft + (r.left - b.left) - (bar.clientWidth - r.width) / 2;
  bar.scrollTo({ left: Math.max(0, left), behavior: smooth && motionOn() ? "smooth" : "auto" });
}

// a soft fade at an edge of the tab bar while more tabs are hidden past it
function markMore() {
  const bar = $("tabs");
  const more = [];
  if (bar.scrollLeft > 2) more.push("left");
  if (bar.scrollLeft + bar.clientWidth < bar.scrollWidth - 2) more.push("right");
  const v = more.join(" ");
  if ((bar.dataset.more || "") !== v) {
    if (v) bar.dataset.more = v;
    else delete bar.dataset.more;
  }
}

function paint({ smooth = false } = {}) {
  for (const id of SCREENS) {
    const on = id === state.current;
    const tab = $(`tab-${id}`);
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
    $(`screen-${id}`).hidden = state.layout === "tabs" && !on;
  }
  document.documentElement.dataset.screen = state.current;
  revealTab(smooth);
}

// all on one page: bring a screen's top to just under the sticky bars
// (.screen's scroll-margin-top keeps it clear of them). The first screen goes
// to the very top, so the alerts strip above it stays in view.
function scrollToScreen(id) {
  spyLock = performance.now() + 600;
  if (id === SCREENS[0]) window.scrollTo({ top: 0 });
  else $(`screen-${id}`).scrollIntoView({ block: "start" });
}

export function selectScreen(id, { focus = false, scroll = false } = {}) {
  if (!SCREENS.includes(id)) return;
  const changed = id !== state.current;
  state.current = id;
  paint({ smooth: true });
  try { localStorage.setItem(STORE, id); } catch { /* storage blocked: the address still has it */ }
  if (fromHash() !== id) history.replaceState(null, "", `#${id}`);
  if (focus) $(`tab-${id}`).focus({ preventScroll: true });
  if (state.layout === "page" && scroll) scrollToScreen(id);
  else if (state.layout === "tabs" && scroll) window.scrollTo({ top: 0 });
  if (changed || state.layout === "page") state.onChange(id);
}

export function setLayout(layout) {
  const next = layout === "page" ? "page" : "tabs";
  if (next === state.layout) return;
  state.layout = next;
  document.documentElement.dataset.layout = next;
  paint();
  state.onChange(state.current);
  // keep the screen that was open in view across the switch
  if (next === "page") scrollToScreen(state.current);
  else window.scrollTo({ top: 0 });
}

// All on one page: the lit tab and the address follow the screen under the
// tab bar as the page scrolls. Only the tab bar is repainted; every panel is
// already drawn, so nothing else needs to run.
function spy() {
  spyQueued = false;
  if (state.layout !== "page" || performance.now() < spyLock) return;
  const top = $("tabs").getBoundingClientRect().bottom;
  const line = top + Math.max(40, (window.innerHeight - top) * 0.25);
  // at the very end of the page the last screens can't scroll up that far:
  // the lowest screen showing at all is the one being read
  const atEnd = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
  const reach = atEnd ? window.innerHeight - 40 : line;
  let pick = SCREENS[0];
  for (const id of SCREENS) {
    if ($(`screen-${id}`).getBoundingClientRect().top <= reach) pick = id;
    else break;
  }
  if (pick === state.current) return;
  state.current = pick;
  paint({ smooth: true });
  if (fromHash() !== pick) history.replaceState(null, "", `#${pick}`);
}

// ▲ / ■ on a tab when an alert on that screen is up (never colour alone).
// local: {screen: "warn" | "bad"} for readings a screen flags itself that no
// alert covers yet (they light that screen's tab only, not STATUS).
export function flagScreens(alerts, local = {}) {
  const worst = {};
  const rank = { warn: 1, bad: 2 };
  for (const a of alerts || []) {
    const lvl = rank[a.level] || 0;
    if (!lvl) continue;
    const scr = ALERT_SCREEN[a.id] || "status";
    for (const s of scr === "status" ? ["status"] : [scr, "status"]) worst[s] = Math.max(worst[s] || 0, lvl);
  }
  for (const [s, level] of Object.entries(local || {})) {
    if (SCREENS.includes(s) && rank[level]) worst[s] = Math.max(worst[s] || 0, rank[level]);
  }
  for (const id of SCREENS) {
    const tab = $(`tab-${id}`);
    const lvl = worst[id] || 0;
    const word = lvl === 2 ? "bad" : lvl === 1 ? "warn" : null;
    const scr = $(`screen-${id}`);
    if (word) {
      if (tab.dataset.level !== word) tab.dataset.level = word;
      if (scr.dataset.level !== word) scr.dataset.level = word;
    } else { delete tab.dataset.level; delete scr.dataset.level; }
    const flag = tab.querySelector(".tab-flag");
    const glyph = lvl === 2 ? "■" : lvl === 1 ? "▲" : "";
    if (flag.textContent !== glyph) flag.textContent = glyph;
    // screen readers hear the state with the tab name
    const label = tab.querySelector(".tab-label").textContent;
    const name = word ? `${label}, ${word === "bad" ? "problem" : "heads-up"}` : label;
    if (tab.getAttribute("aria-label") !== name) tab.setAttribute("aria-label", name);
  }
  markMore();
}

export function initTabs({ onChange, layout }) {
  state.onChange = onChange || state.onChange;
  state.layout = layout === "page" ? "page" : "tabs";
  document.documentElement.dataset.layout = state.layout;
  state.current = fromHash() || stored() || "status";
  const bar = $("tabs");
  bar.addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (tab) selectScreen(tab.dataset.tab, { scroll: true });
  });
  // arrow keys move along the tabs and open each one (they switch instantly)
  bar.addEventListener("keydown", (e) => {
    const i = SCREENS.indexOf(state.current);
    let next = null;
    if (e.key === "ArrowRight") next = SCREENS[(i + 1) % SCREENS.length];
    else if (e.key === "ArrowLeft") next = SCREENS[(i - 1 + SCREENS.length) % SCREENS.length];
    else if (e.key === "Home") next = SCREENS[0];
    else if (e.key === "End") next = SCREENS[SCREENS.length - 1];
    if (!next) return;
    e.preventDefault();
    selectScreen(next, { focus: true });
  });
  window.addEventListener("hashchange", () => {
    const h = fromHash();
    if (h && h !== state.current) selectScreen(h, { scroll: true });
  });
  bar.addEventListener("scroll", markMore, { passive: true });
  window.addEventListener("scroll", () => {
    if (state.layout !== "page" || spyQueued) return;
    spyQueued = true;
    requestAnimationFrame(spy);
  }, { passive: true });
  paint();
  if (fromHash() !== state.current) history.replaceState(null, "", `#${state.current}`);
  // the tab bar sticks just under the top bar, whatever height the top bar wraps
  // to; a screen scrolled to on the one-page layout stops just under both
  const top = $("topbar");
  const root = document.documentElement.style;
  const fitTop = () => root.setProperty("--chrome-h", `${top.offsetHeight}px`);
  const fitBar = () => { root.setProperty("--tabs-h", `${bar.offsetHeight}px`); revealTab(false); markMore(); };
  new ResizeObserver(fitTop).observe(top);
  new ResizeObserver(fitBar).observe(bar);
  fitTop();
  fitBar();
}
