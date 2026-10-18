// Settings drawer, generated from the server's settings schema. Nothing is
// changed until SAVE is clicked (look settings preview right away and go back
// on UNDO).
import { $, el, clear, setText, setData } from "./dom.js";
import { api } from "./api.js";
import * as f from "./fmt.js";
import { isPrivate, plain, hasHidden } from "./privacy.js";

// settings that can hold an address, a folder or a login: shown with their private parts
// hidden and locked while privacy mode is on (the server refuses changes to them then)
const PRIVATE_TYPES = new Set(["text", "url", "path", "password"]);
const locked = (s) => isPrivate() && PRIVATE_TYPES.has(s.type);

const SECTIONS = [
  { id: "source", title: "DATA SOURCE", intro: [
    "Where the numbers come from: your node's RPC port. The unrestricted one shows everything. ",
    "A restricted one (--restricted-rpc, or the port from --rpc-restricted-bind-port) hides peers, bans, traffic and disk space, and monerod refuses its admin commands there (stop, ban, limits, mining and the like)."] },
  { id: "thresholds", title: "ALERT LIMITS", intro: ["When a lamp lights. Changing these only changes the dashboard."] },
  { id: "grace", title: "WAIT BEFORE ALERTING", intro: [
    "How long a problem has to last before it counts, so normal restarts (updates, network changes) don't set anything off."] },
  { id: "display", title: "LOOK", intro: ["How the screens look. Changes show right away as a preview; SAVE keeps them."] },
  { id: "panels", title: "PANELS", intro: ["Show or hide panels. Hidden panels stop drawing but the dashboard keeps watching."] },
  { id: "chain", title: "CHAIN", intro: ["The block tape in panel B."] },
  { id: "node", title: "NODE'S LOG", intro: [
    "Where monerod writes its log: its --log-file, else bitmonero.log in its data folder. The dashboard only reads it."] },
  { id: "server", title: "WINDOW", intro: ["How the app window talks to the part of the app that reads your node, on this computer only. Changes here take effect the next time the app starts."] },
  { id: "about", title: "ABOUT", intro: [] },
];

const ACCENT_SWATCH = { amber: "#ffb840", ice: "#6fd6e8", phosphor: "#7ef0a4", bone: "#e9dfc6", violet: "#b9aaff", monero: "#ff7a2e" };

let schema = [];
let saved = {};
let draft = {};
let errors = {};
let passwordSet = false;
let current = "source";
let onApply = () => {};
let onSaved = () => {};
let lastFocus = null;

export function initSettings(opts) {
  onApply = opts.onApply || onApply;
  onSaved = opts.onSaved || onSaved;
  $("open-settings").addEventListener("click", open);
  $("close-settings").addEventListener("click", close);
  $("scrim").addEventListener("click", close);
  $("set-save").addEventListener("click", save);
  $("set-revert").addEventListener("click", revert);
  $("settings").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    if (e.key === "Tab") trapTab(e);
  });
}

export function loadSettings(resp) {
  schema = Array.isArray(resp.schema) ? resp.schema : [];
  saved = { ...(resp.values || {}) };
  passwordSet = !!(resp.rpc_password_set ?? resp.values?.["source.rpc_password_set"]);
  draft = { ...saved };
  errors = {};
  if (!$("settings").hidden) render();
}

export function savedValues() {
  return saved;
}

// the sections the server has settings for, in their order, then ABOUT
function sectionsInUse() {
  const ids = new Set(schema.map((s) => s.section));
  return SECTIONS.filter((s) => ids.has(s.id) || s.id === "about");
}

function changedKeys() {
  return schema.filter((s) => s.type === "password" ? !!draft[s.key] : draft[s.key] !== saved[s.key]).map((s) => s.key);
}

function open() {
  lastFocus = document.activeElement;
  $("scrim").hidden = false;
  $("settings").hidden = false;
  render();
  focusSection();
}

function close() {
  $("scrim").hidden = true;
  $("settings").hidden = true;
  // look settings go back to what's saved unless they were saved
  onApply(saved);
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

function revert() {
  draft = { ...saved };
  errors = {};
  onApply(saved);
  render();
  msg("CHANGES UNDONE", null);
}

async function save() {
  const keys = changedKeys();
  if (!keys.length) { msg("NOTHING TO SAVE", null); return; }
  const values = {};
  for (const k of keys) values[k] = draft[k];
  $("set-save").disabled = true;
  msg("SAVING…", null);
  try {
    const resp = await api.saveSettings(values);
    loadSettings(resp);
    render();
    onSaved(saved);
    msg(`SAVED · ${keys.length} ${keys.length === 1 ? "CHANGE" : "CHANGES"}`, "ok");
  } catch (e) {
    errors = (e.data && e.data.errors) || {};
    const first = Object.keys(errors)[0];
    const sec = schema.find((s) => s.key === first)?.section;
    if (sec) current = sec;
    render();
    msg(first ? "NOT SAVED · FIX THE MARKED SETTING" : "NOT SAVED · " + String(e.message).toUpperCase(), "bad");
  } finally {
    $("set-save").disabled = false;
  }
}

function msg(text, level) {
  const m = $("set-msg");
  setText(m, text || "");
  setData(m, "level", level);
}

function render() {
  renderNav();
  renderForm();
  const n = changedKeys().length;
  const st = $("set-state");
  setText(st, n ? `${n} UNSAVED ${n === 1 ? "CHANGE" : "CHANGES"}` : "ALL SAVED");
  setData(st, "dirty", n ? "yes" : null);
  // Rebuilding the nav or the form drops focus to <body>, where Escape and Tab
  // no longer reach the drawer: put it on the current section's button.
  if (!$("settings").hidden && !$("settings").contains(document.activeElement)) focusSection();
}

function focusSection() {
  const nav = $("set-nav");
  const b = nav.querySelector(`button[data-id="${CSS.escape(current)}"]`) || nav.querySelector("button");
  if (b) b.focus();
}

function renderNav() {
  const nav = $("set-nav");
  const hadFocus = nav.contains(document.activeElement);
  clear(nav);
  const changed = new Set(changedKeys().map((k) => schema.find((s) => s.key === k)?.section));
  sectionsInUse().forEach((s, i) => {
    const b = el("button", { type: "button", "data-id": s.id, "aria-current": String(s.id === current) },
      el("span", { class: "sn-id", text: String(i + 1).padStart(2, "0") }),
      el("span", { text: s.title }),
      changed.has(s.id) ? el("span", { class: "sn-dirty", text: "●", title: "Unsaved changes" }) : null);
    b.addEventListener("click", () => { current = s.id; render(); focusSection(); });
    nav.append(b);
  });
  if (hadFocus) focusSection();
}

function intro(parts) {
  const p = el("p", { class: "set-intro" });
  for (const part of parts) p.append(Array.isArray(part) ? el(part[0], { text: part[1] }) : part);
  return p;
}

function renderForm() {
  const form = clear($("set-form"));
  const sec = sectionsInUse().find((s) => s.id === current) || sectionsInUse()[0];
  if (!sec) return;
  if (sec.intro.length) form.append(intro(sec.intro));
  if (sec.id === "about") { form.append(about()); return; }

  // privacy mode has its own switch at the top of the window
  const fields = schema.filter((s) => s.section === sec.id && s.key !== "display.privacy");
  if (isPrivate() && fields.some(locked)) {
    form.append(el("p", { class: "privacy-note", text: "PRIVACY MODE IS ON · ADDRESSES, FOLDERS AND LOGINS ARE HIDDEN AND CAN'T BE CHANGED UNTIL IT'S OFF (THE PRIVACY SWITCH AT THE TOP)" }));
  }
  const group = el("div", { class: "set-group" });
  for (const s of fields) group.append(field(s));
  if (fields.length) form.append(group);

  const actions = el("div", { class: "set-actions" });
  if (sec.id === "source") {
    const res = el("div", { class: "testresult", hidden: true, role: "status" });
    const btn = el("button", { class: "btn", type: "button", text: "TEST CONNECTION" });
    btn.addEventListener("click", () => testConnection(btn, res));
    actions.append(btn);
    form.append(actions, res);
  }
  if (fields.length && sec.id !== "server") {
    const held = fields.some(locked);
    const reset = el("button", { class: "btn", type: "button", text: `RESET ${sec.title} TO DEFAULTS`, disabled: held,
      title: held ? "Turn privacy mode off first: this section holds addresses, folders or logins" : null });
    reset.addEventListener("click", async () => {
      try {
        const resp = await api.resetSection(sec.id);
        const keep = { ...draft };
        loadSettings(resp);
        for (const k of Object.keys(keep)) if (schema.find((s) => s.key === k)?.section !== sec.id) draft[k] = keep[k];
        onSaved(saved);
        onApply(draft);
        render();
        msg(`${sec.title} RESET TO DEFAULTS`, "ok");
      } catch (e) { msg("COULDN'T RESET · " + String(e.message).toUpperCase(), "bad"); }
    });
    const ra = el("div", { class: "set-actions" }, reset);
    form.append(ra);
  }
}

function defaultText(s) {
  if (s.type === "password") return "";
  if (s.type === "bool") return `DEFAULT ${s.default ? "ON" : "OFF"}`;
  if (s.type === "choice") {
    const c = (s.choices || []).find((c) => c.value === s.default);
    return `DEFAULT ${String(c ? c.label : s.default).toUpperCase()}`;
  }
  if (s.default === "" || s.default === undefined || s.default === null) return "";
  return `DEFAULT ${s.default}${s.unit ? " " + s.unit : ""}`;
}

function setDraft(key, value) {
  draft[key] = value;
  delete errors[key];
  if (key.startsWith("display.")) onApply(draft);
  // refresh only the chrome, not the form, so typing isn't interrupted
  const n = changedKeys().length;
  const st = $("set-state");
  setText(st, n ? `${n} UNSAVED ${n === 1 ? "CHANGE" : "CHANGES"}` : "ALL SAVED");
  setData(st, "dirty", n ? "yes" : null);
  renderNav();
  const fieldEl = $("set-form").querySelector(`[data-key="${CSS.escape(key)}"]`);
  if (fieldEl) {
    const s = schema.find((x) => x.key === key);
    const changed = s && (s.type === "password" ? !!draft[key] : draft[key] !== saved[key]);
    setData(fieldEl, "changed", changed ? "yes" : null);
    setData(fieldEl, "invalid", null);
    const fe = fieldEl.querySelector(".ferr");
    if (fe) fe.remove();
  }
}

function field(s) {
  const id = "f-" + s.key.replace(/[^a-z0-9]/gi, "-");
  const wrap = el("div", { class: "field", "data-key": s.key });
  const changed = s.type === "password" ? !!draft[s.key] : draft[s.key] !== saved[s.key];
  setData(wrap, "changed", changed ? "yes" : null);
  const top = el("div", { class: "field-top" });
  let input;

  if (s.type === "bool") {
    top.append(el("span", { class: "flabel", text: s.label }));
    const cb = el("input", { type: "checkbox", id });
    cb.checked = !!draft[s.key];
    cb.addEventListener("change", () => setDraft(s.key, cb.checked));
    input = el("label", { class: "toggle", for: id }, cb, el("span", { class: "tg" }), el("span", { text: cb.checked ? "ON" : "OFF" }));
    cb.addEventListener("change", () => setText(input.lastChild, cb.checked ? "ON" : "OFF"));
  } else if (s.type === "choice" && (s.choices || []).length <= 6) {
    top.append(el("span", { class: "flabel", id: id + "-l", text: s.label }));
    input = el("div", { class: "choices", role: "radiogroup", "aria-labelledby": id + "-l" });
    for (const c of s.choices || []) {
      const r = el("input", { type: "radio", name: id, value: String(c.value) });
      r.checked = String(draft[s.key]) === String(c.value);
      r.addEventListener("change", () => { if (r.checked) setDraft(s.key, c.value); });
      const sw = s.key === "display.accent" && ACCENT_SWATCH[c.value] ? el("span", { class: "swatch" }) : null;
      if (sw) sw.style.background = ACCENT_SWATCH[c.value];
      input.append(el("label", {}, r, sw, c.label));
    }
  } else if (s.type === "choice") {
    top.append(el("label", { for: id, text: s.label }));
    const sel = el("select", { id });
    for (const c of s.choices || []) {
      const o = el("option", { value: String(c.value), text: c.label });
      if (String(draft[s.key]) === String(c.value)) o.selected = true;
      sel.append(o);
    }
    sel.addEventListener("change", () => {
      const c = (s.choices || []).find((c) => String(c.value) === sel.value);
      setDraft(s.key, c ? c.value : sel.value);
    });
    input = el("div", { class: "finput" }, sel);
  } else if (s.type === "int" || s.type === "float") {
    top.append(el("label", { for: id, text: s.label }));
    const inp = el("input", { type: "number", id, min: s.min, max: s.max, step: s.step ?? (s.type === "int" ? 1 : "any"), inputmode: s.type === "int" ? "numeric" : "decimal" });
    inp.value = draft[s.key] ?? "";
    inp.addEventListener("input", () => {
      const v = inp.value.trim() === "" ? null : Number(inp.value);
      setDraft(s.key, v === null || Number.isNaN(v) ? inp.value : s.type === "int" ? Math.trunc(v) : v);
    });
    input = el("div", { class: "finput" }, inp, s.unit ? el("span", { class: "funit", text: s.unit }) : null);
  } else {
    // text, url, path, password
    top.append(el("label", { for: id, text: s.label }));
    const isPw = s.type === "password";
    const hold = locked(s);
    const inp = el("input", {
      type: isPw ? "password" : s.type === "url" ? "url" : "text", id, spellcheck: "false", autocomplete: isPw ? "new-password" : "off",
      placeholder: isPw ? (passwordSet ? "SAVED · LEAVE EMPTY TO KEEP IT" : "NONE") : "", readonly: hold,
    });
    inp.value = isPw ? (hold ? "" : draft[s.key] || "") : hold ? plain(String(draft[s.key] ?? "")) : draft[s.key] ?? "";
    if (hold) wrap.dataset.locked = "";
    else inp.addEventListener("input", () => setDraft(s.key, inp.value));
    input = el("div", { class: "finput" }, inp, s.unit ? el("span", { class: "funit", text: s.unit }) : null);
  }

  const def = defaultText(s);
  if (def) top.append(el("span", { class: "fdefault", text: def }));
  wrap.append(top, input);
  if (s.help) wrap.append(el("p", { class: "fhelp", text: s.help }));
  if (errors[s.key]) {
    setData(wrap, "invalid", "yes");
    wrap.append(el("p", { class: "ferr", text: "▲ " + errors[s.key] }));
  }
  return wrap;
}

async function testConnection(btn, res) {
  btn.disabled = true;
  res.hidden = false;
  setData(res, "level", null);
  clear(res).append(el("span", { text: "ASKING THE NODE…" }));
  const body = { rpc_url: draft["source.rpc_url"], rpc_user: draft["source.rpc_user"] || "" };
  if (draft["source.rpc_password"]) body.rpc_password = draft["source.rpc_password"];
  if (isPrivate() && hasHidden(JSON.stringify(body))) {
    setData(res, "level", "warn");
    clear(res).append(el("span", { text: "▲ PRIVACY MODE IS ON AND THIS ADDRESS OR LOGIN IS HIDDEN · TURN IT OFF TO TEST IT" }));
    btn.disabled = false;
    return;
  }
  try {
    const r = await api.testConnection(body);
    clear(res);
    if (r.ok) {
      setData(res, "level", "ok");
      res.append(el("span", { text: `● ANSWERED IN ${Math.round(r.latency_ms ?? 0)} ms · MONEROD ${r.version || "?"} · HEIGHT ${f.num(r.height)}` }));
      res.append(el("span", { text: r.restricted
        ? "RESTRICTED PORT · PEERS, BANS, TRAFFIC AND DISK SPACE WILL BE HIDDEN. SAVE TO USE IT, OR USE YOUR NODE'S UNRESTRICTED PORT TO SEE THEM."
        : "UNRESTRICTED PORT · THE DASHBOARD CAN SEE EVERYTHING IT NEEDS. SAVE TO USE IT." }));
    } else {
      setData(res, "level", "bad");
      res.append(el("span", { text: "■ NO ANSWER · " + String(r.error || "UNKNOWN REASON").toUpperCase() }));
    }
  } catch (e) {
    setData(res, "level", "bad");
    clear(res).append(el("span", { text: "■ COULDN'T TEST · " + String(e.message).toUpperCase() }));
  } finally {
    btn.disabled = false;
  }
}

function about() {
  const d = el("div", { class: "about" });
  const p = (...kids) => d.append(el("p", {}, ...kids));
  const h = (t) => d.append(el("h3", { text: t }));
  h("WHAT THIS IS");
  p("A console for one Monero node. It runs on the same computer as the node, or on another one reaching it over the network, and talks to monerod only through its normal RPC port, so Monero itself is never changed and updates to Monero don't need updates here.");
  h("WHAT IT READS");
  p("What monerod's RPC port answers, monerod's own log file (Settings → Node's log), its own settings and history, and the MRL spy node list it comes with (third_party/mrl-ban-list, unchanged). It also looks up this computer's name, your user name, your real name and your home folder, only so privacy mode can hide them. The full list, each point with a command to check it, is in ", el("code", { text: "SAFETY.md" }), ".");
  h("WHAT IT CAN CHANGE");
  p("Nothing on the node: every RPC method it can send is a read, listed in ", el("code", { text: "memstara/rpc.py" }), ". It never writes to monerod's files or anyone else's, and it loads nothing from the internet: fonts and code are all local.");
  h("SAFETY");
  p("It only listens on 127.0.0.1, so other devices can't open it. Its page and readings need a key only its own window has, so a website you visit can't use it either.");
  h("LOOK");
  p("The style follows real control-room conventions (annunciator lamp walls, \"grey is normal\" screens, event logs) and the general feel of cinematic sci-fi interface art such as the SCENESCAPES gallery on uispace.org. No code, artwork or wording from that gallery is used.");
  h("FONTS");
  p("IBM Plex Mono and IBM Plex Sans Condensed, under the SIL Open Font License 1.1 (licence files in web/fonts/).");
  return d;
}

function trapTab(e) {
  const focusables = [...$("settings").querySelectorAll("button, input, select, [tabindex='0']")].filter((n) => !n.disabled && n.offsetParent !== null);
  if (!focusables.length) return;
  const first = focusables[0], last = focusables[focusables.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

export function openSettings() { open(); }
export function isOpen() { return !$("settings").hidden; }
