// Add-ons: optional parts for a particular setup, kept in addons/<name>/ so that
// deleting the folder removes them without touching anything here. The server
// lists the enabled ones; each script's default export gets `dash` and can add
// a panel. Lamps, alerts, route boxes, log views and command notes come from
// the server and need no script. See docs/DESIGN.md, "Core and add-ons".
import * as dom from "./dom.js";
import * as fmt from "./fmt.js";
import { api } from "./api.js";
import { screenShown, screenOf } from "./tabs.js";

let list = [];
const panels = [];   // {id, render}

export function addonList() {
  return list;
}

export function addonTitle(name) {
  return list.find((a) => a.name === name)?.title || String(name || "").toUpperCase();
}

// log views add-ons offer, for the command drawer
export function addonLogs() {
  const out = [];
  for (const a of list) {
    if (!a.enabled || a.error) continue;
    for (const l of a.logs || []) out.push({ ...l, addon: a.name, addonTitle: a.title });
  }
  return out;
}

// an add-on panel's letter, unless a panel already shows it (then "+")
function freeLetter(want) {
  const w = String(want || "").trim().toUpperCase().slice(0, 2);
  const used = new Set([...document.querySelectorAll("#screens .panel .pid")].map((n) => n.textContent.trim().toUpperCase()));
  return w && !used.has(w) ? w : "+";
}

function frame(a, spec) {
  const id = String(spec.id || a.name).replace(/[^a-z0-9_-]/gi, "-");
  const sec = dom.el("section", {
    class: `panel span-${spec.span === 1 || spec.span === 2 ? spec.span : "full"}`, "data-panel": id,
    "data-addon": a.name, id: `p-${id}`, "aria-labelledby": `h-${id}`,
  });
  const sub = dom.el("span", { class: "psub", text: "—" });
  sec.append(
    dom.el("header", { class: "ph" },
      dom.el("span", { class: "pid", text: freeLetter(spec.letter) }),
      dom.el("h2", { id: `h-${id}`, text: String(spec.title || a.title).toUpperCase() }),
      sub,
      dom.el("span", { class: "ph-addon", text: "ADD-ON", title: `From addons/${a.name}/. Delete that folder to remove it.` })),
    dom.el("div", { class: "pb" }));
  // on the screen it names (spec.tab), after a named panel (spec.after), else at the end of STATUS
  const screens = document.getElementById("screens");
  const grid = (spec.tab && document.getElementById(`grid-${String(spec.tab).replace(/[^a-z]/g, "")}`))
    || document.getElementById("grid-status");
  const after = spec.after && (spec.tab ? grid : screens).querySelector(`[data-panel="${spec.after}"]`);
  if (after) after.after(sec);
  else grid.append(sec);
  return { sec, id, body: sec.querySelector(".pb"), sub };
}

function makeDash(a) {
  return {
    name: a.name,
    helpers: { ...dom, fmt },
    addPanel(spec) {
      const f = frame(a, spec || {});
      const made = spec.build ? spec.build(f.body, f.sub) : null;
      if (made && typeof made.render === "function") panels.push({ id: f.id, name: a.name, render: made.render });
    },
  };
}

export async function loadAddons() {
  try {
    const r = await api.addons();
    list = Array.isArray(r) ? r : Array.isArray(r?.addons) ? r.addons : [];
  } catch {
    list = [];   // an older server, or none installed
  }
  for (const a of list) {
    if (!a.enabled || a.error) continue;
    for (const href of a.styles || []) document.head.append(dom.el("link", { rel: "stylesheet", href }));
    for (const src of a.scripts || []) {
      try {
        const mod = await import(src);
        if (typeof mod.default === "function") mod.default(makeDash(a));
      } catch (e) {
        a.error = `Its screen code didn't load (${src}): ${e.message}`;
        console.warn(a.error);
      }
    }
  }
  return list;
}

// draw every add-on panel that is showing; one failing panel never stops the rest, and
// its warning is logged once (again only when it changes), not with every reading
const warned = new Map();
export function renderAddonPanels(snap, now) {
  for (const p of panels) {
    const sec = document.getElementById(`p-${p.id}`);
    if (!sec || sec.hidden || !screenShown(screenOf(sec))) continue;
    try {
      p.render(snap, now);
    } catch (e) {
      const msg = `add-on ${p.name}: ${e.message}`;
      if (warned.get(p.id) !== msg) { warned.set(p.id, msg); console.warn(msg); }
    }
  }
}
