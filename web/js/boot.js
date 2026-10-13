// Start-up check: a short list of real checks (server, settings, node, port
// type, add-ons), then a lamp test. Click or any key skips it.
import { $, el, clear } from "./dom.js";
import * as f from "./fmt.js";
import { addonList } from "./addons.js";

const STEP_MS = 170;

function line(label, value, level) {
  return el("li", {}, el("span", { text: label }), el("span", { class: "bl-dots" }), el("span", { class: `bl-${level || "ok"}`, text: f.flag(level, value) }));
}

export function runBoot(snapPromise, settings) {
  const box = $("boot");
  return new Promise((resolve) => {
    const finish = () => {
      if (box.hidden) return;
      box.hidden = true;
      window.removeEventListener("keydown", finish, true);
      resolve();
    };
    box.hidden = false;
    box.addEventListener("click", finish, { once: true });
    window.addEventListener("keydown", finish, true);
    const ol = clear($("boot-lines"));
    const show = (n) => new Promise((r) => setTimeout(() => { if (!box.hidden) ol.append(n); r(); }, STEP_MS));

    (async () => {
      await show(line("DASHBOARD SERVER", "ANSWERING", "ok"));
      await show(line("SETTINGS", settings ? "LOADED" : "DEFAULTS", settings ? "ok" : "warn"));
      let snap = null;
      try { snap = await snapPromise; } catch { snap = null; }
      if (!snap) {
        await show(line("FIRST READING", "NONE YET", "bad"));
      } else {
        const src = snap.source || {};
        await show(line("DATA SOURCE", `LIVE · ${src.rpc || "—"}`, "ok"));
        await show(line("NODE ANSWERS", src.answering === false ? `NO · ${String(src.error || "").toUpperCase()}` : `YES · ${f.isNum(src.latency_ms) ? Math.round(src.latency_ms) + " ms" : ""}`, src.answering === false ? "bad" : "ok"));
        if (src.answering !== false) await show(line("RPC PORT", src.restricted ? "RESTRICTED · LIMITED DATA" : "UNRESTRICTED · FULL DATA", src.restricted ? "warn" : "ok"));
        for (const a of addonList().filter((x) => x.enabled)) {
          await show(line(`ADD-ON · ${String(a.title || a.name).toUpperCase()}`, a.error ? "DIDN'T LOAD" : "LOADED", a.error ? "warn" : "ok"));
        }
        const lit = (snap.lamps || []).filter((l) => l.state === "warn" || l.state === "bad").length;
        await show(line("LAMP TEST", `${f.plural((snap.lamps || []).length, "LAMP", "LAMPS")} · ${lit} LIT`, lit ? "warn" : "ok"));
      }
      await show(line("READY", "", "ok"));
      setTimeout(finish, 450);
    })();
  });
}
