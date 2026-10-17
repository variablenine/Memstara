// Talks only to the dashboard's own server (same origin). Every call carries the
// window key the app window was opened with (the server answers nothing without
// it), and writes also carry the X-Memstara header the server requires.

const KEY = new URLSearchParams(location.search).get("key") || "";

async function request(method, path, body, guard = false, signal = undefined) {
  const opts = { method, headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin", signal };
  if (KEY) opts.headers["X-Memstara-Key"] = KEY;
  if (guard) opts.headers["X-Memstara"] = "1";
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-Memstara"] = "1";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const err = new Error((data && (data.error || data.message)) || `The dashboard server answered ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const api = {
  // with `after`: held by the server until there's a snapshot newer than that number
  // (or `wait` seconds pass), so each reading arrives the moment it's in
  snapshot: (after, wait, signal) => request("GET", after == null ? "/api/snapshot"
    : `/api/snapshot?after=${encodeURIComponent(after)}&wait=${encodeURIComponent(wait)}`, undefined, false, signal),
  history: (window) => request("GET", `/api/history?window=${encodeURIComponent(window)}`),
  settings: () => request("GET", "/api/settings"),
  saveSettings: (values) => request("PUT", "/api/settings", { values }),
  resetSection: (section) => request("POST", "/api/settings/reset", { section }),
  testConnection: (body) => request("POST", "/api/test-connection", body),
  // monerod's console commands. A change command carries the word the person
  // typed in the confirm dialog; the server checks it too.
  commands: () => request("GET", "/api/commands"),
  discover: () => request("GET", "/api/discover", undefined, true),
  command: (name, args = {}, confirm) => request("POST", "/api/command", confirm ? { name, args, confirm } : { name, args }),
  nodeLog: (source, lines) => request("GET", `/api/node-log?source=${encodeURIComponent(source)}&lines=${encodeURIComponent(lines)}`, undefined, true),
  // privacy mode: on at once; off only with the word typed in the confirm step (SHOW)
  privacy: (on, confirm) => request("POST", "/api/privacy", confirm ? { on, confirm } : { on }),
};
