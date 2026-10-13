# Memstara: design and data contract

This file is the contract between the server (`memstara/`), the screens (`web/`)
and the tests (`tests/`). If code and this file disagree, fix one of them.

## Goals

- A desktop app for one Monero node, on the node's own machine, in its own
  window: never a browser, no address bar, no tabs. The window (GTK 4 with
  WebKitGTK) draws the screens in `web/`, and their data comes from the app's
  own server on `127.0.0.1`, which answers only that window (see **The app
  window**).
- **Monero itself is never modified.** The dashboard only talks to `monerod`
  through its public JSON-RPC interface, plus read-only looks at the machine
  it runs on (see **Local reads**). Updating Monero never needs a dashboard
  change unless Monero changes its RPC.
- **A generic core, with anything setup-specific in removable add-ons.** The
  core works with any `monerod` on Linux and assumes nothing about the user's
  own scripts. Things that only make sense on one person's machine live in
  `addons/<name>/`, and deleting that folder removes them completely. See
  **Core and add-ons**.
- **A GUI for monerod's own commands, nothing more.** Every command you can
  type into `monerod`'s console (or run as `monerod <command>`) is available,
  reading and changing exactly what that command reads and changes. Monero's
  console commands are themselves thin wrappers around RPC methods
  (`src/daemon/rpc_command_executor.cpp`: each one either sends that RPC
  request or calls the same handler in-process), so the unrestricted RPC port
  gives the dashboard the same view and the same powers as the console. The RPC
  client only sends methods that some console command sends, plus two
  read-only ones the poller uses. Changes need a typed confirm word. See
  **Commands**.
- **Live from the start.** There is no pretend node. The dashboard reads the
  real `monerod` at `source.rpc_url`. Tests use a small canned-answer fake in
  `tests/` that never ships as a feature.
- **Few dependencies.** Python 3.10+ standard library only on the server. The
  window needs PyGObject with GTK 4 and WebKitGTK 6.0 (`python3-gi` and
  `gir1.2-webkit-6.0` on Debian and Ubuntu, `python3-gobject` and
  `webkitgtk6.0` on Fedora, `python-gobject` and `webkitgtk-6.0` on Arch), imported by
  `memstara/window.py` only. The screens are plain HTML, CSS and JavaScript
  modules with no build step, no framework, and no requests to any other host
  (fonts are bundled).

## Core and add-ons

The core is everything in `memstara/`, `web/`, `docs/` and `tests/`. It must
work, and its tests must pass, with the `addons/` folder deleted. No core file
names an add-on, reads an add-on's settings, or has wording about one
person's setup (no particular machine, script, service or VPN, and no fixed
ports other than monerod's own defaults).

An add-on is one folder, `addons/<name>/`:

```
addons/<name>/
  addon.py        server side: defines ADDON (see below)
  web/            screens: ES modules and CSS, served at /addons/<name>/web/...
  tests/          its own tests (unittest), run with the core's
  README.md       what it reads, how to set it up, how to remove it
```

Removing it: delete the folder and restart the dashboard. Its settings stay
in the settings file untouched (ignored) until the next save drops them.

Its tests are its `tests/test*.py` files. The core's
`tests/test_addons_suite.py` imports each one by path (as module
`addontests_<name>_<file>`, with the add-on's `tests/` folder on `sys.path`),
so `python3 -m unittest discover -s tests` runs the core's and every
add-on's tests together. A helper module in an add-on's `tests/` needs a name
no other add-on uses (for example `_<name>_paths.py`). With `addons/`
deleted that file adds nothing.

### Server side (`addon.py`)

`memstara/addons.py` loads every `addons/*/addon.py` at start with
`importlib` (path from the repo root, or `--addons DIR`; `--no-addons` loads
none). Each file defines `ADDON`, an instance of a subclass of
`memstara.addons.Addon`. Every hook is optional and has a do-nothing default.
Any exception from an add-on, at load or in a hook, is caught, logged once
per minute at most, and shown in `/api/addons` as `error`; the core keeps
running and the add-on's parts show as off. `poll()` runs in a worker thread
and is waited for 4 s at most (`POLL_TIMEOUT_S`): one still running then
counts as a failed `poll()` for that poll ("poll() didn't finish in 4 s"),
its answer is never used, and no new `poll()` of that add-on starts until it
ends ("poll() still hasn't finished an earlier call ..."). Meanwhile its
`command_notes` and `protected_hosts` may be called while that `poll()` still
runs.

| hook | returns | used for |
|---|---|---|
| `name`, `title`, `intro` | str | id (`[a-z][a-z0-9_]*`, the folder's name; left empty it takes the folder's), display name, one or two sentences for its settings section. Names the core uses (its settings sections, `addons`, `api`, `core`, `dashboard`, `web`, `memstara`) are refused |
| `settings()` | list of schema entries (`memstara.settings.entry(...)`), keys `<name>.<key>`, section `<name>` | the add-on's section in the settings menu; `<name>.enabled` (bool, default true, "Use this add-on") is added by the core first. Each entry is checked at load (key pattern, type, a valid default) |
| `poll(settings, now, ctx)` | dict (JSON-safe) | `snapshot.addons.<name>`; called every poll. `settings` is every setting's value, flat (a copy), with every password-type setting blanked (`""`) except the add-on's own `<name>.*` ones, so `source.rpc_password` never reaches an add-on; `ctx` is a copy of the core snapshot so far (node, chain, peers, mempool, traffic, host, ...) |
| `lamps(data, ctx)` | list of `{id, label, state, value, help, screen?}` | appended to the lamp wall after the core lamps; ids are prefixed `<name>.` by the core; `label` ≤ 24 and `value` ≤ 16 characters, uppercased; `screen` (`status`, `network`, `chain` or `mempool`) is the screen a click on it opens, else one is guessed from its id and label. `ctx` also has the merged `alerts` |
| `alerts(data, ctx)` | list of `{id, level, text, title?, since?, ips?}`, or `None` | merged into `snapshot.alerts` with `source: <name>` (see Alerts). `None` = can't tell right now: its alerts stay as they are; an item `{id, hold: true}` does the same for one alert |
| `route(data, ctx)` | list of `{id, label, value, level, between?, link?: {label, level}, detail?, row_only?}` | `snapshot.route` (each item also gets `addon`): extra boxes in the route diagram. `between: "peers-node"` puts the box on the link from other nodes to the node (something the node's traffic passes through) and `link` labels the link below it; other boxes hang below the node (at most 2 drawn); `row_only` items only add a line under the diagram; `detail` is that line's text when it differs from `value` |
| `command_notes(data)` | `{command: text}` | appended to that command's `warn` in `/api/commands` and in its confirm text (an alias such as `exit` counts as its command) |
| `protected_hosts(data)` | list of IP strings | `ban` refuses any range holding one of these (an IPv4-mapped IPv6 address counts as its IPv4 address). When it fails, the addresses it gave last time stay protected (until the add-on is turned off) |
| `log_sources()` | list of `{id, title, setting, summary?}` | extra log views in the command drawer; `GET /api/node-log?source=<name>.<id>` reads the path in that setting, which must be one of the add-on's own `path` settings (same rules as the core log) |
| `web()` | `{"scripts": [...], "styles": [...]}` | files under its `web/` the screens load (`.js`/`.mjs` scripts, `.css` styles) |

`log_sources()` and `web()` are called once, at load. `data` is what the
add-on's `poll()` returned last. When `poll()` failed it is `None`, and then
only `command_notes` and `protected_hosts` are called (with `None`), its lamps
show `off` with value `ADD-ON ERROR` under the names they had, and its
alerts are held. Everything a hook returns is checked and trimmed before it
reaches the snapshot (at most 40 lamps, alerts or route items per add-on per
poll). A turned-off add-on (`<name>.enabled` false) isn't called at all: its
lamps, route items, notes, protected hosts and `snapshot.addons.<name>` go
away, and its alerts are dropped without "Cleared" events.

`GET /api/addons` → `{"addons": [{name, title, intro, enabled, error, scripts: [url], styles: [url], logs: [{source, title, summary}]}]}`
(`intro`: one or two plain sentences for its settings section; `logs[].source`
is `<name>.<id>` as `/api/node-log` takes it). Every folder with an
`addon.py` is listed, in name order, including one that didn't load
(`enabled: false`, `error: "It didn't load: ..."`, nothing else). `error` is
otherwise the latest hook failure (`"poll() failed: ValueError: ..."`) while
it keeps failing, else `null`. Script and style URLs are
`/addons/<name>/web/<file>`. Not guarded: it only lists what's loaded.
Static files for an add-on are added to the server's allowlist at start, from
its `web()` list only. Folders starting with `.` or `_` are skipped.

### Screens side

After loading settings, `main.js` fetches `/api/addons` and `import()`s each
enabled add-on's scripts (same origin, so the CSP still holds) and adds its
styles. A script's default export is `register(dash)`, where `dash` offers:

- `dash.addPanel({id, letter, title, span, tab, after, build})`: the core
  makes the `<section class="panel">` frame (header, pid letter, title,
  subtitle slot, an ADD-ON tag) on screen `tab` (`status`, `network`,
  `chain`, `mempool`, `traffic`, `computer`, `controls` or `log`; STATUS if
  unknown), after panel `after` when that panel is there, and calls
  `build(body, sub)` once. The letter is used unless a panel already shows
  it, else the panel gets "+". The panel shows while the add-on is on
  (`<name>.enabled`); `build` returns `{render(snap, now)}`, called on
  every snapshot with `snap.addons[<name>]` available.
- `dash.helpers`: the core's `dom.js` and `fmt.js` exports, so an add-on
  writes text the same safe way (textContent only) and formats numbers the
  same way.

Lamps, alerts, route boxes, log views and command notes come from the
server, so they need no add-on script.

### What's core

| part | where |
|---|---|
| RPC panels, lamps 1 to 11, alerts from RPC, history, commands, controls | core |
| Local reads (process, log, computer, network link, Tor proxy, round trips) | core |
| Finding the node: the running `monerod`'s own flags and config file give the unrestricted RPC address, data dir, log file and `--out-peers` | core |
| Anything that only makes sense with files or services only one setup has | an add-on in `addons/<name>/`, described in its own `README.md` |

## Process model

`python3 -m memstara` (from the repo folder, or the app's icon) checks that
the window's packages are there (if not, it prints the `apt install` line and
stops), then asks the desktop whether a copy is already running. If one is, it
shows that copy's window and exits. Otherwise this copy:

1. Loads settings from `$XDG_CONFIG_HOME/memstara/settings.json`
   (default `~/.config/memstara/settings.json`), creating it with
   defaults on first run (file mode 0600, folder 0700). Every save sets a
   folder that was already there to 0700 too, when it's the user's own and
   not the home folder, a filesystem root or a sticky folder like `/tmp`.
2. Loads the add-ons (`addons/` next to `memstara/`, see **Core and add-ons**)
   and adds their settings. Once the server is up it prints one line per
   add-on: on, turned off, or why it didn't load.
3. On a first run (the settings file had no `source.rpc_url`), looks for the
   running `monerod` (see **Finding the node**) and saves its address and log
   file when exactly one is usable.
4. Starts one **poller** thread that reads the node every
   `source.refresh_s` seconds (and this computer every `local.every_s`; both
   every second by default), runs the alert rules, calls the add-ons, records
   history and events, and stores the latest snapshot. The window only ever
   reads that stored snapshot, so it never adds load on the node, and it gets
   each new one the moment the poll that made it ends (see **The snapshot**).
5. Serves the screens and the API on `127.0.0.1:<server.port>` (default
   18200), with a new window key (see **HTTP API and safety rules**).
6. Opens the window (not with `--background`).

Closing the window stops the app (it saves the history first, without waiting
more than 2 seconds for a reading still under way, since at logout the desktop
allows only a few), unless it was started with `--background`. SIGINT (Ctrl+C in its terminal) and SIGTERM stop
it the same way. Started with `--background`, closing only hides the window
(GTK's hide-on-close), and the next open shows the same window and view: a
second web view in one app can draw at 0×0, blank, on Wayland, so the app
only ever makes one. While hidden the page is `document.hidden` and draws
nothing. Closing for good and quitting end WebKit's drawing process first
(`terminate_web_process`): left to exit on its own, it can crash on the way out
when drawing in software (heap errors in its exit-time cleanup).

Command-line flags: `--background` (no window; start at login uses it),
`--install` / `--uninstall` (the app's entry in the app grid), `--autostart
on|off` (start at login), `--safe-graphics` (draw without the graphics card),
`--port N`, `--config PATH`, `--addons DIR` (load add-ons from another folder)
or `--no-addons` (load none). `--install`, `--uninstall` and `--autostart`
do their job and exit. Hidden: `--debug` (the screens' console messages in the
terminal, and WebKit's inspector on right-click).

Changing settings through the API takes effect on the next poll without a
restart.

## The app window (`memstara/window.py`, `memstara/desktop.py`)

A `Gtk.Application` (id `org.memstara.Memstara`) with one `Gtk.ApplicationWindow` holding a `WebKit.WebView`.
The view loads `http://127.0.0.1:<port>/?key=<window key>` and nothing else.

- **One copy.** A second start finds the first over D-Bus (in
  `handle-local-options`, before anything starts) and shows its window instead
  (with `--background` it just exits). Options that only count at start
  (`--port`, `--config`, `--safe-graphics`, `--addons`, `--no-addons`,
  `--debug`) don't reach the running copy, and the second start says so. If
  the running copy doesn't answer (it's busy starting or stopping), the second
  start says so and exits with status 1, rather than becoming a second copy.
  With no session bus it runs on its own.
- **`--background`**: the app holds itself open with no window, so the poller
  keeps collecting history. Opening the app shows the window; closing the
  window then leaves it running. **Quit** in the menu stops it.
- **Header bar**: black, with one menu button (Reload the screens, Start when
  I log in, Quit) and the desktop's window buttons. The screens show the name
  themselves, so the bar has no title. No keyboard shortcuts of the app's own
  (a stray key press does nothing).
- **What the view can do**: navigation only to its own origin (any other
  address, a new window or a download is refused in `decide-policy`), no
  permission requests (camera, microphone, location, notifications), no
  developer tools, and these WebKit settings off: WebGL, Web Audio, media,
  media streams, MediaSource, encrypted media, WebRTC, full screen,
  JavaScript-opened windows and clipboard access, modal dialogs, file URL
  access, site-specific quirks, page cache. The right-click menu keeps only
  copy, cut, paste, delete and select all.
- **Its data**: a persistent `WebKit.NetworkSession` in
  `$XDG_STATE_HOME/memstara/window-data` (the remembered screen)
  and `$XDG_CACHE_HOME/memstara/window-cache`, straight to
  `127.0.0.1` whatever proxy the desktop uses. The web cache is off
  (`CacheModel.DOCUMENT_VIEWER`), so the window key never lands on disk.
  The window's size and maximized state go in `window.json` next to the
  history; the first start fills the screen.
- **When drawing stops**: if WebKit's drawing process ends, the view reloads
  after a second. A third time within two minutes shows a message with
  TRY AGAIN and the `--safe-graphics` hint instead (the message stands in for
  the view; TRY AGAIN, or the menu's Reload the screens, puts the same view
  back and starts it over).
  The poller and history carry on.
- **When it can't start** (the port is taken, say), a small window says why
  and how to fix it, instead of the app vanishing, and it exits with status 1.
- **`--safe-graphics`**: sets `WEBKIT_DISABLE_DMABUF_RENDERER=1` and the view's
  hardware acceleration policy to never, for a window that stays blank or
  flickers with some graphics drivers.
- **Desktop entries** (`desktop.py`, Desktop Entry Specification 1.5):
  `--install` writes `~/.local/share/applications/org.memstara.Memstara.desktop`
  (`Exec` is the Python that ran `--install` with `-m memstara`, `Path` the repo
  folder, `Icon` the repo's `web/img/app-icon.svg`, `StartupWMClass` the app
  id so the dock shows the icon). `--autostart on` (or the menu's Start when
  I log in) writes the same with `--background` to
  `~/.config/autostart/`; off removes it, and a desktop tool that turned it
  off (`X-GNOME-Autostart-enabled=false` or `Hidden=true`) counts as off.
  `--install` refreshes the start-at-login file when start at login is on, so
  moving the folder and installing again fixes both (one a desktop tool turned
  off stays off). `--uninstall` removes both and
  never touches settings or history.

Known limits: WebKitGTK 6.0 always runs its drawing process in a bubblewrap
sandbox. Where AppArmor limits unprivileged user namespaces and nothing allows
bwrap (Ubuntu 24.04 out of the box: its profile is in `apparmor-profiles` but
not loaded), the sandbox fails with `bwrap: setting up uid map: Permission
denied` and the window stays blank; the README has the reported fix. The app
never turns the sandbox off. The window needs a desktop session (Wayland or
X11); over SSH it says so and exits.

## Monerod RPC: what is called, and how often

All calls go to `source.rpc_url` (default `http://127.0.0.1:18081`, monerod's own default RPC port). Optional HTTP Digest login
(`source.rpc_user` / `source.rpc_password`) for nodes started with
`--rpc-login`. Timeout `source.timeout_s` (default 8).

Read allowlist (anything not on this list or the write list below raises
`RpcNotAllowed` before a request is made):

| Method | Endpoint | Cadence | Used for |
|---|---|---|---|
| `get_info` | `/json_rpc` | every poll | height, sync, peers count, mempool count, disk, version, start time |
| `get_connections` | `/json_rpc` | every poll | peer list, spread checks, Tor sending, traffic per peer |
| `get_transaction_pool_stats` | `/get_transaction_pool_stats` | every poll | mempool size, bytes, double spends, fee histogram |
| `get_net_stats` | `/get_net_stats` | every poll | total bytes in/out (rates come from differences) |
| `get_block_headers_range` | `/json_rpc` | when the top block changes, and on start | last `chain.blocks_shown` blocks (default 60): times, sizes, tx counts, reward; reorg detection |
| `get_last_block_header` | `/json_rpc` | every poll | top block hash and time (cheap reorg/new-block check) |
| `sync_info` | `/json_rpc` | every poll while not synced | download spans and peer heights while syncing |
| `get_bans` | `/json_rpc` | every 60 s | ban count, ranges vs single addresses |
| `hard_fork_info` | `/json_rpc` | every 10 min | current network rules version |
| `get_fee_estimate` | `/json_rpc` | every 60 s | fee per byte, 4 priority tiers |
| `get_limit` | `/get_limit` | every 10 min | bandwidth limits |
| `get_version` | `/json_rpc` | on start, then every 10 min | RPC version number |

Facts checked in Monero's source (`src/rpc/core_rpc_server.h`,
`core_rpc_server.cpp`, `core_rpc_server_commands_defs.h`, tag v0.18.5.3):

- `get_connections`, `get_bans`, `sync_info`, `get_net_stats` and
  `get_alternate_chains` only exist on an **unrestricted** RPC port. On a
  restricted port `get_info` also zeroes the peer counts, `start_time`,
  `version`, and reports `free_space` as 2^64-1. The adapter detects
  `get_info.restricted == true` and reports it (see `source.restricted`), and
  the screens tell the user to point the dashboard at the admin port.
- `connection_info.address_type`: 0 invalid, 1 IPv4, 2 IPv6, **3 I2P, 4 Tor**.
  Tor peers also have a `host` ending in `.onion`.
- `connection_info.state` strings: `before_handshake`, `synchronizing`,
  `standby`, `idle`, `normal`.
- `connection_info.avg_download` etc. are kB/s; `live_time` is seconds.
- `get_bans` entries: `{host, ip, seconds}`; a range ban has a host like
  `203.0.113.0/24`.
- `get_transaction_pool_stats.pool_stats`: `bytes_total, bytes_min, bytes_max,
  bytes_med, fee_total, oldest, txs_total, num_failing, num_10m,
  num_not_relayed, histo_98pc, histo[{txs, bytes}], num_double_spends`.
- ZMQ publishing needs `--zmq-pub`, which monerod doesn't turn on by
  default, so version 1 polls JSON-RPC only.
- While a node syncs with help from a bootstrap node (`set_bootstrap_daemon`
  or `--bootstrap-daemon-address`) and is more than 10 blocks behind it,
  monerod passes `get_info`, `get_last_block_header`,
  `get_block_headers_range`, `get_transaction_pool_stats`,
  `get_fee_estimate`, `get_limit`, `hard_fork_info` and `get_version` on to
  that node and marks the answer `untrusted: true`
  (`use_bootstrap_daemon_if_necessary`); `get_info` then also carries
  `height_without_bootstrap` (the node's own height) and
  `bootstrap_daemon_address`. `get_connections`, `get_net_stats`, `get_bans`
  and `sync_info` are never passed on. While that lasts the poller takes only
  the height from `get_info` (`height_without_bootstrap`; the other node's
  height is the target, and synced is false) and leaves the rest of it `null`
  (outgoing and incoming counts come from `get_connections` instead). It
  doesn't send the passed-on calls: the block tape, reorg watch, mempool, fee,
  limits, rules version and RPC version keep the node's own last readings or
  stay `null`. The port isn't taken for restricted, `source.bootstrap` holds
  that node's address, and a `source` event says when it starts ("Syncing
  with help from X: its answers aren't shown as your node's") and stops. The
  new-block clock (`no_blocks`, the NEW BLOCK lamp) runs from the last change
  of the node's own height meanwhile. Any other answer marked untrusted is
  ignored too.

## The snapshot (`GET /api/snapshot`)

Each new snapshot gets the next number, `seq` (the first, before any reading,
is 0). `GET /api/snapshot?after=<seq>&wait=<seconds>` answers as soon as there
is a snapshot whose number isn't `after`: at once if there already is, else
the moment the next poll ends, or after `wait` seconds (at most 20) with the
same one. The window keeps one such request open, so each reading reaches the
screen as soon as it's in. Without `after` the latest snapshot comes back at
once. A `wait` that isn't a plain positive number counts as 0.

All times are Unix seconds (UTC). All sizes are bytes. All rates are bytes per
second. A value the dashboard doesn't know is `null`, never a made-up number.

```jsonc
{
  "v": 1,
  "generated": 1759700000,
  "level": "ok",                       // "ok" | "warn" | "bad": worst active alert
  "source": {
    "mode": "live",                    // always "live"
    "rpc": "127.0.0.1:18081",          // host:port only, never the login
    "answering": true,                 // the last poll got a valid get_info
    "error": null,                     // short plain-language reason when not answering
    "latency_ms": 14,                  // get_info round trip
    "restricted": false,               // true = pointed at a restricted port
    "last_ok": 1759700000,             // last time the node answered
    "poll_s": 1,                       // source.refresh_s
    "host_name": "node-box",           // the machine the dashboard runs on
    "dashboard_version": "0.1.0",
    "bootstrap": null                  // while monerod passes reads on to its bootstrap node: that
                                       // node's address (see "Facts checked"), else null
  },
  "node": {
    "version": "0.18.5.1-release",
    "nettype": "mainnet",
    "started": 1759600000,             // get_info.start_time, null if 0
    "uptime_s": 100000,
    "height": 3512345,                 // get_info.height (blocks in the chain = top block + 1)
    "target_height": 3512345,          // max(height, get_info.target_height)
    "synced": true,                    // get_info.synchronized && !busy_syncing && height >= target
    "sync_pct": 100.0,
    "top_hash": "9f3c…",
    "last_block_time": 1759699900,     // timestamp of the top block
    "last_block_age_s": 100,           // generated - last_block_time, floored at 0
    "difficulty": 650000000000,
    "hashrate": 5416666666,            // difficulty / 120
    "out_peers": 22,                   // public (clearnet) outgoing, from get_info
    "out_wanted": 32,                  // thresholds.out_wanted if above 0, else the node's --out-peers, else 12
    "out_wanted_from": "node",         // "setting" | "node" | "default"
    "in_peers": 31,
    "tor_out": 3,                      // outgoing .onion peers; null if Tor sending looks off
    "rpc_connections": 2,
    "white_peers": 1000, "grey_peers": 5000,
    "tx_pool": 34,
    "alt_blocks": 2,
    "free_bytes": 400000000000,        // null on a restricted port
    "db_bytes": 263000000000,
    "update_available": false,
    "hf_version": 16,
    "rpc_version": "3.15",             // get_version.version as major.minor
    "bans": 431,                       // total entries in get_bans
    "ban_ranges": 120,                 // of those, how many are ranges
    "ban_list": [                      // only bans with 24 h or less left (yours and
      {"host": "203.0.113.0/24", "seconds": 75600}   // monerod's own); longest first. Counted
    ],                                 // down from when get_bans was read, also while the node
                                       // isn't answering (its last list; ended bans drop off)
    "bans_long": 420,                  // bans with more than 24 h left (ban list file, DNS blocklist)
    "fee_per_byte": [20000, 80000, 320000, 4000000],  // get_fee_estimate.fees, or null
    "limit_up": 2048000, "limit_down": 8192000        // bytes/s from get_limit (kB/s * 1024), or null
  },
  "chain": {
    "blocks": [                        // oldest first, up to chain.blocks_shown
      {"height": 3512344, "hash": "9f3c…", "time": 1759699900, "interval_s": 131,
       "weight": 98000, "txs": 41, "reward": 600000000000}
    ],
    "avg_interval_s": 119.4,           // over the blocks listed
    "median_weight": 300000,           // get_info.block_weight_median
    "wait_odds": 0.43,                 // chance a block takes at least this long: exp(-age/120)
    "reorg": {"deepest_24h": 0, "last": null}, // last = {"time", "depth", "height"} or null;
                                       // deepest_24h is null until the node first answers
    "sync": null                       // while syncing (sync_info): {"height", "target_height",
                                       // "spans", "blocks_queued", "peers", "overview"}; null when synced
  },
  "peers": {
    "list": [
      {"id": "a1b2c3d4",               // first 8 hex of connection_id (stable per connection)
       "dir": "out",                   // "out" | "in"
       "kind": "ipv4",                 // "ipv4" | "ipv6" | "tor" | "i2p"
       "ip": "100.64.12.7",            // "" for tor/i2p
       "host": "100.64.12.7",          // the onion name for tor
       "port": 18080,
       "net16": "100.64",              // first two parts of an IPv4, "" otherwise
       "net24": "100.64.12",
       "age_s": 3600,                  // live_time
       "height": 3512345,
       "state": "normal",
       "down_bps": 1200, "up_bps": 3400,   // current_download/upload kB/s * 1024
       "recv_bytes": 25000000,         // recv_count: bytes received since the connection opened
       "sent_bytes": 98000000,         // send_count
       "recv_idle_s": 4,               // recv_idle_time: seconds since the peer last sent anything
       "send_idle_s": 14,              // send_idle_time
       "pruned": false,                // pruning_seed != 0
       "rtt_ms": 42.0, "retrans": 0}   // kernel round trip and retransmits (see Local reads);
                                       // null for Tor/I2P, when local reads are off, or not matched
    ],
    "spread": {                        // biggest cluster each way (see the spread_out / spread_in alerts)
      "out16": {"net": "100.66", "n": 2, "ips": ["100.66.1.9", "100.66.4.2"]},
      "in24":  {"net": "100.70.3", "n": 1, "ips": ["100.70.3.4"]}
    },
    "out_by_kind": {"ipv4": 20, "ipv6": 2, "tor": 3, "i2p": 0},
    "in_by_kind":  {"ipv4": 31, "ipv6": 0, "tor": 0, "i2p": 0}
  },
  "mempool": {
    "txs": 34, "bytes": 120000, "fee_total": 1200000000,
    "oldest_age_s": 420,               // generated - pool_stats.oldest, null if empty
    "double_spends": 0, "failing": 0, "not_relayed": 0, "over_10m": 3,
    "histogram": [{"txs": 4, "bytes": 9000}], // pool_stats.histo as given
    "histo_98pc": 204                  // pool_stats.histo_98pc: when not 0, the bars split 0 to this many
                                       // seconds evenly and the last bar holds the oldest 2%
  },
  "traffic": {
    "bytes_in": 9000000000, "bytes_out": 21000000000,  // totals since the node started
    "rate_in": 60000, "rate_out": 140000                 // from the last two polls, null at first
  },
  "alerts": [                          // active alerts, worst first, then oldest first
    {"id": "peers", "level": "warn", "title": "FEW OUTGOING",
     "text": "Only 6 of 32 outgoing connections for 12 min",
     "since": 1759699000, "source": "dashboard", "ips": null}   // source: "dashboard" or an add-on's name
  ],
  "lamps": [                           // the annunciator wall: the 11 core lamps in this order, then add-on lamps
    {"id": "node", "label": "NODE", "state": "ok", "value": "ANSWERING", "since": null},
    {"id": "example.thing", "label": "THING", "state": "warn", "value": "2 ALERTS", "since": 1759699000,
     "addon": "example", "help": "What it watches."}
  ],                                   // since = when it last left ok/off, null while ok/off;
                                       // add-on lamps also carry "addon": "<name>" and "help": "<what it watches>"
  "addons": {"<name>": { }},           // each enabled add-on's poll() result ({} with no add-ons)
  "route": [                           // add-on route items (see Core and add-ons); [] with no add-ons
    {"id": "relay", "label": "RELAY", "value": "UP", "level": "ok", "between": "peers-node",
     "link": {"label": "CHECKED · OK", "level": "ok"}, "addon": "example"}
  ],
  "host": { },                         // local reads, see "Local reads"; null when off
  "events": [                          // newest first, at most 200
    {"t": 1759699900, "level": "info", "kind": "block", "text": "New block 3,512,344 · 41 txs · 98 kB",
     "id": 812}                        // one up from the event before (since the dashboard started)
  ],
  "clock_style": "12h",                // how its clock times are written: "12h" or "24h", "-utc" added in privacy mode
  "seq": 1234                          // this snapshot's number (see above)
}
```

### Lamps (annunciator wall)

`state` is one of `ok`, `warn`, `bad`, `off` (not set up / not known). Always
these 11, in this order, then each enabled add-on's lamps (ids
`<name>.<id>`, in add-on name order; see **Core and add-ons**). `value` is a
short plain-language reading (at most about 14 characters, uppercase).

| id | label | lit when | source |
|---|---|---|---|
| `node` | NODE | not answering | RPC |
| `peers` | OUTGOING | few / none outgoing | RPC |
| `sync` | SYNCED | still syncing | RPC |
| `block` | NEW BLOCK | no block for 30 / 60 min | RPC |
| `disk` | DISK SPACE | under 20 / 5 GB | RPC |
| `reorg` | BLOCKS REPLACED | 3+ / 10+ replaced in 24 h | RPC |
| `double_spend` | DOUBLE SPEND | attempt seen in the last 30 min | RPC |
| `restarts` | RESTARTS | 3+ restarts in an hour | RPC |
| `spread_out` | ECLIPSE · OUT | 5+ outgoing from one network for 10 min | RPC |
| `spread_in` | ECLIPSE · IN | 4+ incoming from one network for 10 min | RPC |
| `tor` | TOR SENDING | Tor sending on but 0 Tor peers for 10 min | RPC, node flags |

An add-on lamp's `since` is tracked the same way; when an add-on is turned
off its lamps go and their `since` starts afresh if it comes back.

### Alerts

The dashboard computes the ids below itself (`source: "dashboard"`). Add-ons
add their own (`source: "<name>"`, see **Core and add-ons**), merged like this:

- An add-on alert is shown as given: `level` (warn or bad), `text`, `title`
  (default: its id in capitals, `_` as spaces), `ips`, and `since` (default:
  when the dashboard first saw it).
- An add-on alert with the id of a core alert is ignored (the core's own
  reading wins). If two add-ons raise the same id, the one that raised it
  first keeps it.
- An add-on whose `alerts()` answers `None` (or whose `poll()` failed) can't
  tell right now. Its alerts stay as they were: they aren't shown and aren't
  cleared, and they come back without a second "raised" event. An item
  `{id, hold: true}` does the same for one alert.
- An alert missing from an add-on's next answer is cleared, with an event.
- Turning an add-on off, or deleting it, drops its alerts quietly, without
  "Cleared" events.

Rules (thresholds are settings):

| id | level | rule (default numbers) | must last | stays up after clearing |
|---|---|---|---|---|
| `node_down` | bad | RPC not answering | `grace.node_down_min` 5 min | |
| `peers` | bad | 0 outgoing (clearnet + Tor) | `grace.no_peers_min` 5 min | |
| `peers` | warn | outgoing < `thresholds.low_peers` (8) | `grace.peers_min` 10 min | |
| `syncing` | warn | not synced | `grace.syncing_min` 20 min | |
| `no_blocks` | warn / bad | no new block for `no_block_warn_min` 30 / `no_block_bad_min` 60 | | |
| `disk` | warn / bad | free < `disk_warn_gb` 20 / `disk_bad_gb` 5 (GB = 10^9 bytes) | | |
| `reorg` | warn / bad | blocks replaced at once ≥ `reorg_warn` 3 / `reorg_bad` 10 | | 24 h |
| `double_spend` | warn | `num_double_spends` > 0 | | 30 min |
| `restarts` | warn | ≥ `restart_warn` 3 changes of `start_time` within 1 h | | |
| `spread_out` | warn | ≥ `out16_warn` 5 outgoing IPv4 peers in one x.y network | `grace.spread_min` 10 min | |
| `spread_in` | warn | ≥ `in24_warn` 4 incoming IPv4 peers in one x.y.z network | `grace.spread_min` 10 min | |
| `tor` | warn | `tor_sending` setting is `on` (or `auto` and: when the running node is found, it was started with `--tx-proxy tor,...`, read from its flags, see **Finding the node**; when it isn't found, Tor peers were seen since the node last started) and 0 outgoing Tor peers | `grace.tor_min` 10 min | |
| `cpu_temp` | warn / bad | hottest CPU sensor ≥ 85 / 95 °C (185 / 203 °F) | 2 min | |
| `memory` | warn | ≥ 90% of the computer's memory in use | 5 min | |
| `power` | warn / bad | on battery / on battery with < 20% left | 1 min | |

The last three come from this computer's own readings (`host.computer`, see
**Local reads: what RPC can't show**), not RPC, and have no settings: they are the limits the
COMPUTER panel and dial draw. They go on while the node isn't answering, and
pause (neither shown nor cleared) while local reads are off or failing. Their
texts give temperatures in the `display.temp_unit` setting; under `auto` the
server can't see the screens' language, so it gives both, °F first.

While the node isn't answering, only `node_down` and the computer's own alerts
are raised (other RPC rules pause rather than firing on missing data). Alert
texts are plain language.
Raising or clearing an alert adds an event.

Reorg detection: the poller keeps `height -> hash` for the last 100 blocks. When
the top block changes, it re-reads the shown range; every stored height whose
hash changed counts as replaced. The depth of a reorg is the number of replaced
heights found in one poll.

### Events

`{"t", "level": "info"|"warn"|"bad"|"ok", "kind", "text"}`. Kinds: `block`,
`reorg`, `peer_in`, `peer_out`, `peer_gone`, `alert`, `clear`, `restart`,
`source`, `settings`, `sync`, `control` (a change command that succeeded) and
`log` (lines from monerod's log, see **Local reads**). Peer join/leave events
are summarised ("3 joined · 1 left") instead of one line each: the first change
after a quiet spell at once, then at most one event per 5 seconds, and a peer
that leaves and comes back in between isn't counted. Kept
in memory (last 200).

## History (`GET /api/history?window=<seconds>`)

The poller averages its readings into one row per 5 seconds, however often it
reads the node, keeps 24 hours of rows in memory, and saves them to
`$XDG_STATE_HOME/memstara/history.json` (default `~/.local/state/...`)
at most once a minute, so charts survive a restart. The row still filling is
part of every answer, so the newest point moves with each reading. A step is
never shorter than 5 seconds or the poll. `window` is one of 900, 3600, 21600, 86400 (default 3600). The answer
is down-sampled to at most 360 points by averaging (`null` where the node
wasn't answering):

```json
{"window": 3600, "step": 10, "t": [1759696400, ...],
 "out": [22, ...], "in": [31, ...], "tor": [3, ...], "pool": [34, ...],
 "rate_in": [60000, ...], "rate_out": [140000, ...], "latency": [14, ...],
 "cpu_temp_c": [61.0, ...], "node_cpu_pct": [7.5, ...]}
```

`cpu_temp_c` and `node_cpu_pct` come from the local reads (`null` when they
are off or unknown). A history file saved before a field existed still loads;
that field is `null` for the older samples.

## Settings

`GET /api/settings` returns `{"values": {...}, "schema": [...]}`. Passwords are
never sent back: the value is `""` plus `"rpc_password_set": true|false`.
`PUT /api/settings` takes a partial `values` object, validates every key
against the schema, saves atomically (temp file + rename, mode 0600), and
answers with the same shape as GET (or `400 {"errors": {"key": "reason"}}`).
`POST /api/settings/reset` with `{"section": "thresholds"}` restores one
section's defaults.

The file carries `"format": 2`. A file without it is from before reads every
second: when it loads, `source.refresh_s` 5 and `local.every_s` 10 (the old
defaults) become 1, once, and the file is saved with `"format": 2`. Any other
value someone picked stays, and everything else in the file, add-ons' sections
included, is written back as it was.

Schema entries: `{"key": "thresholds.low_peers", "section": "thresholds",
"label": "Few outgoing below", "help": "...", "type":
"int"|"float"|"bool"|"choice"|"text"|"password"|"url"|"path", "min", "max",
"step", "choices": [{"value", "label"}], "default", "unit"}`.

| key | type | default | notes |
|---|---|---|---|
| `source.rpc_url` | url | `http://127.0.0.1:18081` | http/https only; on first run, filled from FIND MY NODE when exactly one unrestricted monerod is found |
| `source.rpc_user` | text | `""` | only if monerod has `--rpc-login` |
| `source.rpc_password` | password | `""` | |
| `source.refresh_s` | choice 1/2/5/10/30 | 1 | seconds between reads of the node |
| `source.timeout_s` | int 2-30 | 8 | |
| `thresholds.out_wanted` | int 0-1000 | 0 | 0 = the node's own: `--out-peers` (flag or config file), else monerod's default of 12 |
| `thresholds.low_peers` | int 0-1000 | 8 | |
| `thresholds.no_block_warn_min` | int 5-1440 | 30 | |
| `thresholds.no_block_bad_min` | int 5-1440 | 60 | must be > warn |
| `thresholds.disk_warn_gb` | int 0-100000 | 20 | |
| `thresholds.disk_bad_gb` | int 0-100000 | 5 | must be < warn |
| `thresholds.reorg_warn` | int 1-1000 | 3 | |
| `thresholds.reorg_bad` | int 1-1000 | 10 | must be > warn |
| `thresholds.out16_warn` | int 2-100 | 5 | |
| `thresholds.in24_warn` | int 2-100 | 4 | |
| `thresholds.restart_warn` | int 2-100 | 3 | |
| `thresholds.tor_sending` | choice auto/on/off | `auto` | |
| `grace.node_down_min` | int 0-120 | 5 | |
| `grace.no_peers_min` | int 0-120 | 5 | no outgoing at all |
| `grace.peers_min` | int 0-120 | 10 | few outgoing |
| `grace.syncing_min` | int 0-240 | 20 | |
| `grace.spread_min` | int 0-120 | 10 | |
| `grace.tor_min` | int 0-120 | 10 | |
| `display.layout` | choice tabs/page | `tabs` | one screen at a time, or every screen on one page |
| `display.accent` | choice | `amber` | the look: `amber` (gold light, cyan headings), `phosphor` (green), `ice` (cyan), `bone` (warm white), `violet`, `monero` (orange). The heads-up and problem colours change with it so they never clash with its accent (see docs/STYLE.md) |
| `display.clock` | choice 12h/24h | `12h` | |
| `display.scanlines` | bool | true | |
| `display.glow` | bool | true | |
| `display.motion` | choice full/reduced/off | `full` | the desktop's reduce-animation setting always wins |
| `display.boot` | bool | true | short start-up check sequence |
| `display.density` | choice comfortable/compact | `comfortable` | |
| `display.text_scale` | choice 90/100/115/130 | 100 | percent |
| `display.keys` | bool | false | keyboard shortcuts; off by default so a stray key press can't trigger them |
| `display.temp_unit` | choice auto/f/c | `auto` | auto: °F when the computer's language is en-US (WebKit's `navigator.language`, from the locale), °C otherwise |
| `panels.<id>` | bool | true | show or hide each panel (ids below) |
| `chain.blocks_shown` | int 20-200 | 60 | |
| `controls.enabled` | bool | true | allows change commands (panel E and the command drawer); read commands always work |
| `node.log_path` | path | `~/.bitmonero/bitmonero.log` | monerod's own log; on first run, from FIND MY NODE. Left at the default, the log file the running node was found writing (its `--log-file` or data dir) is read instead |
| `local.enabled` | bool | true | computer reads (see Local reads) |
| `local.every_s` | choice 1/5/10/30 | 1 | |
| `<addon>.enabled` | bool | true | added by the core for each add-on; each add-on's own keys are `<addon>.*` |
| `server.port` | int 1024-65535 | 18200 | the window's local port; takes effect the next time the app starts |

Panel ids (core): `lamps`, `chain`, `peers`, `route`, `controls`, `traffic`,
`history`, `mempool`, `events`, `computer`, `system`. Add-on panels are shown
or hidden with their add-on's `<addon>.enabled`.

## HTTP API and safety rules

| Method + path | Answer |
|---|---|
| `GET /?key=<window key>` and static files under `web/` | the screens |
| `GET /api/snapshot` | snapshot (above); `?after=<seq>&wait=<s>` waits for a newer one (at most 20 s) |
| `GET /api/history?window=` | history (above) |
| `GET /api/settings` | settings + schema |
| `PUT /api/settings` | update settings |
| `POST /api/settings/reset` | reset a section |
| `POST /api/test-connection` | `{"rpc_url", "rpc_user", "rpc_password"?}` → tries `get_info` once and answers `{ok, version, height, restricted, latency_ms, error}`; a missing password means "use the saved one", but only when `rpc_url` has the same scheme, host and port as the saved `source.rpc_url` (127.0.0.1, ::1 and localhost count as the same host); any other address is tried with no password |
| `GET /api/commands` | the command list, see Commands |
| `POST /api/command` | runs one command, see Commands |
| `GET /api/node-log` | the last lines of monerod's log or of a log an add-on offers (guarded) |
| `GET /api/discover` | the running `monerod`s and their settings, see Finding the node (guarded) |
| `GET /api/addons` | the add-ons, see Core and add-ons |
| `GET /addons/<name>/web/<file>` | an add-on's screen files, only those its `web()` lists |
| `GET /api/health` | `{"ok": true, "version": "<dashboard version>"}` |
| `POST /api/privacy` | `{"on": true}` turns privacy mode on at once; `{"on": false, "confirm": "SHOW"}` turns it off (without the word: `400` with `field: "confirm"`). See Privacy mode |

Safety rules the server enforces:

- Binds to `127.0.0.1` only.
- **Host check (DNS-rebinding guard):** every request's `Host` header must be
  `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`; otherwise `421`.
- **Window key:** every start makes a new key (`secrets.token_urlsafe(32)`)
  that only the app window is given, in its address. The page needs it as
  `?key=` and every `/api/` request as the header `X-Memstara-Key`
  (`web/js/api.js` copies it from the address), compared in constant time;
  otherwise `403` with "opens in its own window". Only the screens' own
  scripts, styles, fonts and images need no key (nothing private in them):
  pages and any path that isn't one of those files need it, and the path is
  checked decoded, the way it's read (`/%69ndex.html` is the page too). So a
  browser, or another program or user on the computer, gets nothing. The key
  is never logged or written to disk: the window's web cache is off, because
  WebKit's disk cache would keep the page under its keyed address. A program
  running as you could still dig it out of the app's memory, but such a
  program can already read your settings and reach the node's RPC itself.
  The hidden `--debug` can print it (WebKit starts some console lines with
  the page's address). (`App(key=None)`, in the tests, turns the check off;
  an empty key matches nothing.)
- **Write guard (CSRF):** `PUT`/`POST`, `GET /api/node-log` and
  `GET /api/discover` (they read files and other processes on this computer)
  need the header `X-Memstara: 1` and, when an `Origin` header is present, it
  must match the Host (a `Sec-Fetch-Site: cross-site` is refused too);
  otherwise `403`. No CORS headers are ever sent.
- Static files are served only from a list built at start-up by walking `web/`
  plus each loaded add-on's `web()` files (no path joins from the URL), with
  correct `Content-Type`.
- Headers on every answer: `Content-Security-Policy: default-src 'self';
  img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src
  'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none';
  form-action 'none'`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store` for `/api/*`.
- Request bodies over 64 KB are refused (`413`).
- Errors from the node are summarised in plain words; no stack traces or the
  RPC login ever reach the screens or the log.

## Privacy mode (`memstara/privacy.py`, `web/js/privacy.js`)

The PRIVACY switch in the top bar hides everything that could point at you, this
computer or your node, so the screens can be shown to other people or put in a
screenshot. It is the setting `display.privacy`, which only `POST /api/privacy`
changes (`PUT /api/settings` refuses it, and "reset to defaults" leaves it).

**The server hides, the window only draws.** While it's on, every `/api/` answer
passes through `Redactor` on its way out, so the real values never reach the window.
Each one becomes a token, `⟦kind_hhhhhhhh⟧`: its kind and the first 8 hex digits of an
HMAC-SHA256 of the value, keyed with 32 random bytes made at each start and never sent
anywhere. The same value gets the same token until the app restarts, so peers can still
be told apart, a range's peers counted, and a waiting transaction followed into its
block, but no token can be turned back into its value. Hidden:

| Kind | What |
|---|---|
| `ip`, `ip6` | IPv4 and IPv6 addresses (not loopback or 0.0.0.0), and monerod's number form of one |
| `n16`, `n24`, `net` | ranges, keeping their shape: `⟦n16⟧.0.0/16`, `⟦n24⟧.0/24`, `⟦n24⟧.x`, `⟦ip⟧/32` |
| `port` | every port key, "port 47213" in text, a port after a hidden address (`⟦ip⟧:⟦port⟧`), and this node's own ports anywhere once learned (monerod's `--p2p-bind-port` flags, the add-ons' readings) |
| `tor`, `i2p` | onion and I2P addresses |
| `host`, `user`, `name` | this computer's name, your user name and your real name, as whole words |
| `login` | the login in an address (`http://me:pw@…`) |
| `srv` | server names an add-on reports under a `server` key, and anywhere else they turn up once learned |
| `hex`, `tx` | any run of 64 or more hex digits (transaction ids, key images, a whole transaction in a log line), except block hashes under their own keys |
| `id` | peer ids |
| `addr` | Monero addresses |
| `mac` | hardware addresses |

Your home folder becomes `~`. Every string goes through the patterns, so a field added
later (or an add-on's) still has its addresses, names and paths caught; the key rules
cover what a pattern can't recognise alone (a bare port number, a range written "1.2").
An add-on that reports a private value should use the usual key names (`port`,
`server`, `host_name`, `peer_id`, ...). A waiting transaction your node hasn't passed on
yet is left out of the mempool list (only your node has it, so it could be yours), and
the count of those is dropped. The snapshot gets `"privacy": true`.

**Clocks show UTC**, so a screenshot doesn't give the time zone away: the window's
clocks (the top bar says UTC, in the narrow layout too), the times the server writes into texts
(`clock_style` gets `-utc`; the snapshot says which it was written with in
`clock_style`), events written before the switch (their local times are turned into UTC
by their own `t`), the first snapshot after the switch, and local times in a log line
("2025-06-01 09:30:00" in an add-on's log; monerod's own lines are UTC already and carry
fractions of a second).

**Locked while it's on:** settings that hold an address, a folder or a login (types
`text`, `url`, `path`, `password`) show their hidden form, read-only, and their sections
can't be reset; any value from the window with a token in it is refused; every command
that changes the node (ban and unban included) answers `403`; TEST CONNECTION and USE
THIS refuse hidden values.

**The window** (`privacy.js`) draws each token as a run of block glyphs in the shape of
what it hides (four groups for an address, five characters for a port), the same glyphs
for the same token, and an address inside a hidden range starts with that range's
glyphs (learned from the peer list), so one company's peers still look alike. Screen
readers hear "hidden address", "hidden port" and so on. Text set through `dom.js` or
`textContent` becomes glyph spans (SVG tspans in charts); text added any other way is
swapped by a MutationObserver before it's drawn; `aria-label` and `alt` get the spoken
words, `title` and `placeholder` the glyphs; canvas text draws the glyphs. With Motion
on full, one hidden detail at a time tears and scrambles for a moment (canvas ones every
8 to 13 s each); Motion off or Reduce Animation stills it.

**Switching** reloads the window, so nothing drawn before stays on screen: on is at
once (the screen tears and goes dark, then comes back hidden); off asks you to type
SHOW first. A window that gets a snapshot whose `privacy` doesn't match its own mode (the
switch was used in another window) reloads before drawing it.

Not hidden: the counts and timings (how many peers, from how many ranges, when blocks
came), block heights and hashes, fees, the RPC address on this computer (`127.0.0.1`),
and anything you type yourself, as you type it. A transaction you send shows up as a
new dot on the mempool screen once your node passes it on, so with someone watching the
screen at that moment, the timing could point at it.

## Commands (every monerod console command)

`memstara/commands.py` holds one entry per `monerod` console command (names and
usage from `src/daemon/command_server.cpp`, v0.18.5.3), with the RPC calls its
executor makes (`src/daemon/rpc_command_executor.cpp`). The RPC client's
allowlist is exactly the union of the calls below plus the read-only calls the
poller makes (two of which, `get_last_block_header` and `get_version`, no
console command uses). Nothing else can be sent.

`GET /api/commands` → `{"commands": [{"name", "aliases", "group", "title",
"summary", "kind": "read"|"change", "confirm": "BAN"|null, "warn": "..."|null,
"calls": ["set_bans"], "params": [{"name", "label", "type":
"int"|"text"|"choice"|"bool"|"lines", "required", "min", "max", "choices",
"placeholder", "help"}]}]}`. Metadata only. Notes from enabled add-ons'
`command_notes()` are appended to `warn` (see Core and add-ons).

`POST /api/command` `{"name", "args": {...}, "confirm"?}` → `200 {"ok": true,
"name", "calls": [...], "result": {"<rpc method>": <its result object>, ...},
"message"?: "<plain sentence>"}`; bad arguments `400 {"ok": false, "error",
"field"?}`; a change command without the right confirm word `400 {"ok":
false, "error": "Type BAN to confirm."}`; change commands while
`controls.enabled` is false `403`; node errors `502` with monerod's own error
message. Read commands need no confirm word. Every change command that
succeeds adds an event (`kind: "control"`) and a stderr line, and makes the
next poll re-read bans and limits. The write guard (header + Origin) applies to
`POST /api/command`.

Arguments are validated before anything is sent (types, ranges, 64-hex hashes,
Monero addresses by prefix and length). A few commands get a safety limit the
console doesn't have; each is noted below and in the command's `help`.

| group | command (aliases) | kind / confirm | RPC calls | arguments, notes |
|---|---|---|---|---|
| Node | `status` | read | getinfo, mining_status, hard_fork_info | like the console: ", bootstrapping from X, local height: H (P%)" while get_info comes from a bootstrap node, " was used before" after |
| Node | `print_status` | read | (getinfo, to see it answers) | |
| Node | `version` | read | getinfo | |
| Node | `print_height` | read | /getheight | |
| Node | `diff` | read | getinfo | |
| Node | `sync_info` | read | sync_info | |
| Node | `save` | change SAVE | /save_bc | |
| Node | `stop_daemon` (`exit`) | change STOP | /stop_daemon | warn: "Stops the node. If a service or script restarts it automatically, it comes back on its own." |
| Node | `update` | read for `check`, change DOWNLOAD for `download` | /update | `command`: check/download. warn: "Download only: monerod saves the release file, it doesn't install it." |
| Chain | `print_bc` | read | getblockheadersrange | `begin`, `end`; at most 1,000 blocks per request (dashboard limit) |
| Chain | `print_block` | read | getblock | `block`: height or 64-hex hash |
| Chain | `bc_dyn_stats` | read | getinfo, get_fee_estimate, getblockheadersrange, hard_fork_info | `last` 1-10,000 |
| Chain | `alt_chain_info` | read | getinfo, get_alternate_chains, getblockheaderbyhash | optional `blockhash` |
| Chain | `hard_fork_info` | read | hard_fork_info | optional `version` |
| Chain | `print_coinbase_tx_sum` | read | get_coinbase_tx_sum | `start`, `count` 1-100,000 (dashboard limit; big ranges keep the node busy) |
| Chain | `output_histogram` | read | get_output_histogram | optional `amount` (atomic units), `min_count`, optional `max_count` |
| Chain | `check_blockchain_pruning` | read | prune_blockchain (check: true) | |
| Chain | `pop_blocks` | change POP | /pop_blocks | `count` 1-1,000 (dashboard limit). warn: removes the newest blocks; the node downloads them again |
| Chain | `prune_blockchain` | change PRUNE | prune_blockchain (check: false) | warn: permanent; going back to a full chain means downloading it all again. Runs inside monerod for a long time; the dashboard stops waiting after 120 s |
| Transactions | `print_tx` | read | /gettransactions | `txid` 64-hex, `hex` bool, `json` bool. Differs from the console here: the console always sends `decode_as_json: false` and turns the raw transaction into JSON itself (C++ `parse_and_validate_tx_from_blob`), which the dashboard can't do in plain Python, so with `json` on it sends `decode_as_json: true` and shows monerod's own JSON (`txs[].as_json`) |
| Transactions | `is_key_image_spent` | read | /is_key_image_spent | `key_image` 64-hex |
| Transactions | `print_pool` (`print_pool_sh`) | read | /get_transaction_pool | |
| Transactions | `print_pool_stats` | read | /get_transaction_pool_stats, getinfo | |
| Transactions | `relay_tx` | change RELAY | relay_tx | `txid` 64-hex |
| Transactions | `flush_txpool` | change FLUSH | flush_txpool | optional `txid` (empty = the whole pool) |
| Network | `print_cn` | read | get_connections | |
| Network | `print_pl` | read | /get_peer_list (include_blocked: true) | `list`: all/white/gray, `pruned` bool, `publicrpc` bool, `limit`. As the console (rpc_command_executor.cpp): `limit` takes the first N of each list, then `pruned` / `publicrpc` keep only those among them that fit, so fewer than N can show |
| Network | `print_pl_stats` | read | /get_peer_list | |
| Network | `print_net_stats` | read | /get_net_stats, /get_limit | |
| Network | `limit` / `limit_up` / `limit_down` | read without a value, change LIMIT with one | /get_limit, /set_limit | `kbps` (-1 = back to default). Lasts until the node restarts |
| Network | `out_peers` / `in_peers` | change PEERS | /out_peers, /in_peers | `max` 0-1,000. Lasts until the node restarts, when its `--out-peers` / `--in-peers` flag (or monerod's default) applies again |
| Network | `set_bootstrap_daemon` | change BOOTSTRAP | /set_bootstrap_daemon | `address` (auto, none, host[:port]), `username`, `password`, `proxy`. warn: wallets using your node get sent to that node while yours syncs |
| Bans | `bans` | read | get_bans | |
| Bans | `banned` | read | banned | `address` |
| Bans | `ban` | change BAN | set_bans | `hosts`: one address or range per line (the console's `@file` form), `seconds` 1 to 31,536,000, default 86,400. Refused: IPv6, non-public addresses and ranges wider than /16 (dashboard limit), and ranges holding the node's own address (`--p2p-bind-ip`, from its flags) or an address an add-on protects (`protected_hosts`): "X holds this computer's own public address (the node is bound to it with --p2p-bind-ip / from the T add-on), so banning it would cut your node off." Before the first poll has worked those out, 409 "The dashboard hasn't finished its first look at the node yet, so it can't check which addresses are this computer's own. Try again in a few seconds." When the node's flags couldn't be read (local reads off, or the node not found), its `warn` and its answer end with "The node's own address (its --p2p-bind-ip) couldn't be checked, because local reads are off or the running node wasn't found on this computer. Make sure you don't ban it." |
| Bans | `unban` | change UNBAN | set_bans (ban: false) | `address` |
| Bans | `flush_cache` | change FLUSH | flush_cache | `bad_blocks` bool (default true; it must be on). monerod's request has only `bad_blocks` (core_rpc_server_commands_defs.h): the console's usage line still lists `bad-txs`, but the console takes only `bad-blocks` (command_parser_executor.cpp), so the form has no bad-transactions box |
| Mining | `mining_status` | read | /mining_status | |
| Mining | `start_mining` | change MINE | /start_mining | `address` (Monero address), `threads` 1-256 or auto, `background` bool, `ignore_battery` bool |
| Mining | `stop_mining` | change STOP | /stop_mining | |
| Mining | `show_hr` / `hide_hr` | change HR | /set_log_hash_rate | only while mining |
| Logs | `set_log` | change LOG | /set_log_level or /set_log_categories | `level` 0-4, or `categories` (letters, digits, `*:,+-._`) |
| Logs | `rpc_payments` | read | rpc_access_data | |

`help` and `apropos` are the drawer's search box. Facts checked in the source:
monerod's automatic bans last 24 h (`P2P_IP_BLOCKTIME`), DNS blocklist bans
8 days, `--ban-list` file bans forever; a host with a `/` is tried as an IPv4
subnet first (`block_subnet` drops every connection in it). monerod keeps
bans only in memory: "Bans last until they run out or the node restarts;
bans from --ban-list and the DNS blocklist come back when it starts." The ban
form says so.

Panel E's ban and unban buttons are shortcuts to the `ban` and `unban`
commands above, with the ranges at the eclipse limits as one-click
suggestions.

### monerod's log (read-only)

`GET /api/node-log?source=monerod|<addon>.<id>&lines=N` (N ≤ 1,000, default
200; `source` defaults to `monerod`) returns the last lines of
`node.log_path` (default `~/.bitmonero/bitmonero.log`), or of the path in the
setting an add-on's log source names, read from the last 512 KB of the file:
`{"ok", "path", "size", "mtime", "lines": [...], "source"}`. Never writes,
never follows symlinks out of the user's home, refuses anything but a regular
file (`403`), and never blocks on a pipe. It also refuses (`403`) a file
inside the dashboard's own settings or state folder
(`~/.config/memstara`, `~/.local/state/memstara`, or
their `$XDG_*` places) and one whose name (after following links) has no
`.log`, unless it is the `log_file` **Finding the node** found for the node
the dashboard reads ("X isn't a log file (its name has no .log), so it isn't
read."). monerod's log as events (`logevents.py`) follows the same rules. An
unknown source is `400`
("source must be monerod or ..."), a missing file `404` ("monerod's log isn't
at PATH."), and a source whose add-on is turned off `404` ("That log comes
from the T add-on, which is turned off.").

## Local reads: what RPC can't show

Changes only ever go through monerod's RPC commands (above). Reads also come
from the computer itself, because some things about the node aren't in any RPC
answer. Everything here is read-only, Linux-only (`/proc`, `/sys`, iproute2),
runs as the user, and degrades to `null` with a short reason when a source is
missing. `memstara/host.py` (finding the node, process, peers' round trips,
computer, network link) and `memstara/logevents.py` (log events) collect it;
the poller calls them every `local.every_s` seconds (default 1, timed from
the last read, so with the node read every 5 s and this every 10 s, it reads
every other poll) and puts the result in the snapshot. Each rate is timed by when its
counter was read (the poll's start moved on by a clock that never jumps), not by when
the poll started: the node's answers before it take a different time each poll, which
would make a rate read every second jump about. With `local.enabled` off none of it runs: `snapshot.host` is
`null`, every peer's `rtt_ms` / `retrans` is `null`, no log events, no
`cpu_temp_c` / `node_cpu_pct` in history, and the node's flags aren't read
(so the `ban` guard and the `tor` alert's `auto` rule go without them).

### Finding the node (`GET /api/discover`, guarded)

So nobody has to know their own ports, the core reads the running
`monerod`'s settings the same way `monerod` does:

- Candidates: processes of the user's own uid whose `/proc/<pid>/comm` is
  `monerod` (other users' processes can't be read anyway).
- Flags from `/proc/<pid>/cmdline`, then the config file (`--config-file`, or
  `~/.bitmonero/bitmonero.conf` if it exists; `key=value` lines, `#`
  comments); a flag on the command line wins over the file, as in monerod.
  The default config file stays at `~/.bitmonero/bitmonero.conf` even when
  `--data-dir` points elsewhere (src/daemon/command_line_args.h), but the
  default log is `bitmonero.log` in the data dir, and a bare `--log-file`
  name is in the data dir too (src/daemon/main.cpp).
- Network: `--testnet` / `--stagenet` change the default ports (28081 /
  38081) and put the default data dir, config file and log one folder
  deeper (`~/.bitmonero/testnet/...`, `~/.bitmonero/stagenet/...`).
- Main RPC: `--rpc-bind-ip` (default 127.0.0.1), `--rpc-bind-port` (default
  18081). It's restricted only with `--restricted-rpc`.
  `--rpc-restricted-bind-port` adds a second, restricted port.
  `--rpc-login` means a user and password are needed (the dashboard never
  copies the password from the command line; you type it in Settings).
  monerod also takes the start of an option's name when only one option
  starts that way, so `--rpc-logi` (5 letters or more) counts as `--rpc-login`.
- `--data-dir` (default `~/.bitmonero`), `--log-file` (default
  `<data dir>/bitmonero.log`), `--p2p-bind-ip` / `--p2p-bind-ipv6-address`
  (the node's own addresses: `ban` refuses ranges holding them), `--tx-proxy`
  (Tor or I2P sending is on; a value starting `tor,` means Tor sending, so the
  `tor` alert applies without guessing, and an I2P-only node has no `tor`
  alert).

Answer: `{ok, error, found: [{pid, exe, started, nettype, rpc_url,
restricted, login_needed, restricted_rpc_url, data_dir, log_file, p2p_bind:
[ip], tx_proxy: bool, tx_proxy_tor: bool}]}`. `tx_proxy` is any `--tx-proxy`;
`tx_proxy_tor` only a `--tx-proxy tor,...`.

First run (no `source.rpc_url` in the settings file yet): if exactly one
candidate has an unrestricted RPC port, its `rpc_url` and `log_file` are
saved as `source.rpc_url` and `node.log_path`, and a line says so ("Found
monerod (pid N): reading URL", plus "(it needs the RPC login in Settings)"
with `--rpc-login`). Otherwise the defaults stay (`http://127.0.0.1:18081`,
`~/.bitmonero/bitmonero.log`) and Settings → Data source has a FIND MY NODE
button listing what it found.

While running, the poller looks again at most once a minute, or sooner when
the node's pid changes (a restart), and uses the entry for the node it reads
(same pid, else the same RPC host:port, `localhost` and `127.0.0.1` counting
as the same). From it: `p2p_bind` feeds the `ban` guard, `tx_proxy_tor` the `tor`
alert's `auto` rule, `log_file` the log reads, and `data_dir` the disk read. `GET /api/discover` always
looks afresh.

### Which process is the node

The `monerod` process whose listening socket is the RPC port in
`source.rpc_url`: find the LISTEN entry for that host:port in `/proc/net/tcp`
(or `tcp6`), take its inode, then scan `/proc/<pid>/fd/*` of the user's own
processes for `socket:[inode]`. Other `monerod`s on the computer (a wallet
app's own node, say) never match. Re-found when the pid stops existing.

### A node on another computer

Reading this computer only makes sense when the node runs on it. Before
anything else, `host.py` checks where `source.rpc_url` points:

- an IP address none of this computer's interfaces holds (`/proc/net/fib_trie`
  "host LOCAL" entries and `/proc/net/if_inet6`; loopback and `0.0.0.0` are
  this computer) is another computer;
- a host name other than this computer's own (`socket.gethostname()`, short or
  full, or with `.local`) is looked up (at most every 5 minutes; the RPC client
  looks the same name up for every call anyway), and is another computer when
  none of its addresses is this computer's. A name that doesn't resolve, or
  address lists that can't be read, leave it read as this computer;
- a loopback address whose port is held by `ssh`, `autossh`, `sshuttle`,
  `socat` or `stunnel` (the process found as in "Which process is the node") is
  a tunnel to another computer.

Then nothing about this computer is read: `snapshot.host` is the empty shape
with `"ok": false`, a plain-sentence `error`, and `"remote": {"how": "network"
| "tunnel", "where": "host:port", "via": "ssh" | null}` (null when the node is
here). The poller skips finding the node's process (so `ban` says the node's
own address couldn't be checked), reads monerod's log only from a
`node.log_path` set by hand (a copy or a mounted folder; `/api/node-log` says
why it can't otherwise), and writes `source` events once per place: that the
node is elsewhere, and, without a tunnel, that plain `http` across a network
can be read by others and that a node answering with no login can be changed
by anyone who reaches it. The COMPUTER screen and panel J say NODE ON ANOTHER
COMPUTER, calmly (it isn't a fault).

### `snapshot.host` (null when local reads are off or not on Linux)

```jsonc
"host": {
  "ok": true, "error": null, "every_s": 10,
  "t": 1759700000.25,                  // when this reading was taken (shown again until the next one)
  "proc": {                            // the monerod process
    "pid": 4242, "name": "monerod", "exe": "/usr/local/bin/monerod",
    "exe_replaced": false,             // /proc/<pid>/exe ends in " (deleted)": the binary on disk changed
                                       // since it started (an update is waiting for a restart)
    "started": 1759600000,             // from /proc/<pid>/stat starttime + btime
    "cpu_pct": 7.5,                    // of one core, from utime+stime deltas between reads
    "rss_bytes": 1900000000, "threads": 41, "open_files": 380,
    "read_bytes": 123, "write_bytes": 456,          // /proc/<pid>/io totals
    "read_rate": 1000, "write_rate": 2000,          // bytes/s between reads
    "flags": ["--rpc-bind-port", "18081", "--out-peers", "32", ...]
                                       // /proc/<pid>/cmdline; the value after --rpc-login,
                                       // --bootstrap-daemon-login, --rpc-access-control-origins
                                       // (or a start of one of them, 5 letters or more, as
                                       // monerod takes) and any *password* or *login* flag is
                                       // replaced with "••••"; a flag=value form is masked too,
                                       // and so is user:password@ in any scheme://... value
  },
  "computer": {
    "cpu_temp_c": 61.0,                // hottest /sys/class/thermal zone of type x86_pkg_temp,
                                       // cpu*, acpitz or coretemp hwmon, else null
    "throttle": {                      // is the CPU slowing itself down because of heat?
      "source": "intel",               // "intel": /sys/devices/system/cpu/cpuN/thermal_throttle
                                       // counters; "kernel": only "Processor" cooling devices
                                       // in /sys/class/thermal; null: neither (nothing to show)
      "now": false,                    // a counter changed since the last read, or a Processor
                                       // cooling device has cur_state > 0; null when no source
      "last_t": 1700000000,            // when this dashboard last saw one (null: not since it started)
      "since_boot": 5,                 // slowdowns counted since the computer started (Intel only)
      "speed_mhz": 2100, "max_mhz": 4500   // average scaling_cur_freq, highest cpuinfo_max_freq
    },
    "on_ac": true, "battery_pct": 87, "battery_state": "Charging",   // /sys/class/power_supply
    "load1": 0.8, "cores": 8,
    "mem_used_bytes": 6e9, "mem_total_bytes": 16e9,                  // /proc/meminfo (MemTotal - MemAvailable)
    "uptime_s": 99999,                                                // /proc/uptime
    "disk": {"device": "nvme0n1", "read_rate": 0, "write_rate": 0, "busy_pct": 3.0}
                                       // the block device under the node's data dir (its --data-dir,
                                       // else ~/.bitmonero; st_dev → /sys/dev/block/M:m),
                                       // rates from /proc/diskstats deltas, busy from io_ticks
  },
  "bind_if": {                          // the interface holding the node's --p2p-bind-ip address
    "name": "wg0", "kind": "vpn", "up": true, "address": "10.8.0.2",   // kind: "vpn" (tun/tap/WireGuard) or "other"
    "rx_bytes": 1, "tx_bytes": 2, "rx_rate": 3, "tx_rate": 4   // /sys/class/net/<if>/statistics
  },
  "tor": {"socks_port": 9050,          // from the node's --tx-proxy tor,... flag, else Tor's default
          "socks_listening": true},    // something listens on 127.0.0.1:<socks_port> (/proc/net/tcp)
  "remote": null                       // the node runs elsewhere: {how, where, via}, see "A node on another computer"
  // "proc": null with "proc_error": "<plain sentence>" when the node process isn't found
}
```

History (`/api/history`) also carries `cpu_temp_c` and `node_cpu_pct` per
sample (null when unknown), for the 15-minute charts in panel J.

### Peer round trips (`peers.list[].rtt_ms`, `retrans`)

`ss -tinH state established` (iproute2, on nearly every Linux system) gives the kernel's
smoothed round-trip time and retransmit count for every TCP connection. Each
clearnet peer from `get_connections` is matched by remote address and port
(`get_connections` gives the peer's own remote port for incoming peers too,
not the node's listening port: cryptonote_protocol_handler.inl). Tor and I2P peers go through
the local Tor proxy, so their round trip is `null`. Run with a 3 s timeout;
missing `ss` → every `rtt_ms` is `null` and `host.error` says so.

### monerod's log as events (`logevents.py`)

Reads `node.log_path` incrementally (remembers inode and offset; starts at the
end on first run, handles rotation to `bitmonero.log-<date>` and truncation;
at most 1 MB per read). monerod writes UTC timestamps
(`2026-10-05 21:00:00.123	<thread>	<LEVEL>	<category>	<file:line>	<text>`).
Lines it turns into events (`kind: "log"`), with the text checked in
monerod v0.18.5.3's source. At most 20 per read reach the event list (the
newest); when there are more, one more line says "N more lines from monerod's
log aren't shown here (COMMANDS has the whole log)".

| log text (start of message) | where | event |
|---|---|---|
| `###### REORGANIZE on height: H of T ... alternative blockchain size: N` | blockchain.cpp | warn: "monerod switched to another chain at height H (N blocks)" |
| `REORGANIZE SUCCESS! on height: H, new blockchain size: S` | blockchain.cpp | info |
| `----- BLOCK ADDED AS ALTERNATIVE ON HEIGHT H` | blockchain.cpp | info: "a competing block at H lost (nothing was replaced)" |
| `Host X blocked.` / `Subnet X blocked.` | net_node.inl | info: "monerod banned X" (bans from the dashboard are already logged as control events; this catches monerod's own) |
| `Host X unblocked.` / `Subnet X unblocked.` | net_node.inl | info |
| `You are now synchronized with the network` | cryptonote_protocol_handler.inl | ok |
| any line at level `ERROR` or `FATAL` | any | bad, first 200 characters |
| any line at level `WARNING` | any | warn, first 200 characters; the same text (digits removed) at most once per 10 minutes |

These are visible at monerod's default log level 0 (`global:INFO` and
`*:WARNING`, from contrib/epee/src/mlog.cpp). `+++++ BLOCK SUCCESSFULLY ADDED`
is not (category `blockchain`, INFO), so new blocks keep coming from RPC.

### Not read, and why

- **monerod's database (LMDB) directly:** needs a library outside Python's,
  the layout changes between Monero versions (it would break on updates), and
  a reader that misbehaves can pin the live database. RPC already serves
  everything in it that the console can show.
- **ZMQ live feed:** needs monerod started with `--zmq-pub` and a ZMQ client;
  with the node read every second it would only make new blocks and pool
  transactions show up less than a second sooner.
- **p2pstate.bin:** a binary archive of the same peer lists `print_pl` shows.

Settings: `local.enabled` (bool, default true), `local.every_s` (choice
1/5/10/30, default 1), and `panels.computer` (bool, default true) like the other
panels.

## Screens (web/)

One page with a tab bar under the top bar. Each tab is a screen; one shows at
a time (`display.layout` "tabs"), or every screen stacks on one page ("page").
The open screen is kept in the address (`#network`) and in the window's
storage, so the app opens on it next time. Keys 1 to 8 switch screens when keyboard shortcuts are on. A tab shows
▲ or ■ when an alert belongs to its screen (STATUS shows the worst of all), and
that screen's centrepiece frame glows in the heads-up colour, or pulses in the
problem colour.

Each screen opens with its **centrepiece** ("hero", numbered 01 to 08), an
animated picture drawn from real readings, then the older lettered panels, in
a responsive grid (three columns wide, two below 1400 px, one when narrow).

| Tab | Centrepiece | Panels after it |
|---|---|---|
| 01 STATUS | NODE STATUS: a ring with one segment per lamp (lit ▲ / ■ when not ok; click one to open the screen that explains it), arcs for outgoing peers, sync and disk, the height and the time since the last block in the middle, and quick readouts that open their screens | A lamp wall, add-on panels |
| 02 NETWORK | NODE TREE: a 3D tree you can turn and zoom. Your node at the top, then outgoing / incoming / Tor, then peers on one company's servers grouped, then every peer. Dots running along the branches are the bytes each peer really moved since the last reading; busy peers are lit, quiet ones dim. LIVE CHATTER beside it is a log of peers joining and leaving, blocks, bans, the busiest peers and the pool, newest at the bottom | C peer scope, D route |
| 03 CHAIN | BLOCK STREAM: the last blocks as 3D boxes receding into the distance, sized by weight and spaced by the wait before each, with a ghost NEXT BLOCK and its timer; blocks replaced flash ▲ | B chain, K system |
| 04 MEMPOOL | TRANSACTION POOL: one dot per waiting transaction in bands by how long it has waited; dots drop in as they arrive and leave in a block-shaped sweep | H mempool |
| 05 TRAFFIC | LIVE TRAFFIC: a scope of download and upload over 15 minutes, with the node's limits when they are near, and flow lanes whose dots speed up with the data | F traffic, G history |
| 06 COMPUTER | GAUGES: dials for CPU temperature, monerod's CPU, memory, how busy the chain's drive is and disk free, each with its last 15 minutes where kept | J computer |
| 07 CONTROLS | BANS: a clock with one ring per ban of 24 h or less, each draining as its time runs out; click one to find it in the controls | E controls |
| 08 LOG | EVENT TIMELINE: lanes of what happened in the last hour (blocks, peers, the node, alerts, monerod's log, blocks replaced), with the newest typed out underneath | I log |

**Centrepiece contract** (`web/js/heroes/index.js`): each module's default
export is `create(body, env)` returning `{update(snap, now), show(), hide(),
look(look)}`. `update` runs on every snapshot whether shown or not (keep state
current, draw only if shown); `show` / `hide` start and stop all animation
(a centrepiece counts as shown only while its screen is open and it is on
screen; the page being hidden, `document.hidden`, hides it too); `look` gets `{motion:
"full" | "reduced" | "off", glow, accent, head, ink}` (colours as "r, g, b").
`env` has the subtitle and header-tools slots, `now()`, `look()`,
`goScreen(id)` and the shared tooltip. One centrepiece failing never stops the
others or the panels. Shared drawing helpers (canvas stage with a frame cap,
orbit camera with drag, pinch, zoom and keys, glow sprites, picking) are in
`web/js/fx/space.js`. With reduced motion (the setting, or the desktop's) they
draw still pictures that update with each reading.

The older panels, by letter:

- **Top bar** (in the screens, under the window's header bar): dashboard
  name and node name, overall status lamp (OK / HEADS-UP / PROBLEM), LIVE
  badge with the RPC address, local clock, COMMANDS and SETTINGS buttons.
  **Alerts strip** below it: active alerts, problems first. **Status bar** at
  the bottom: RPC address, answer time, poll, add-ons.
- **A · Lamp wall** (`lamps`): the 11 core lamps, then add-on lamps. Lit in
  the heads-up or problem colour with ▲ / ■ when not ok. New problem lamps
  flash until clicked (acknowledging is only a screen state and changes
  nothing on the node). Each lamp's tooltip says what it watches (add-on lamps
  bring that text as `help`).
- **B · Chain** (`chain`, two wide): height, synced %, time since the last
  block with a ring that fills toward 2 minutes, "chance of a wait this long"
  in plain words; the block tape: the last N blocks as bars (height = weight,
  spacing = time between blocks).
- **C · Peer scope** (`peers`): a round radar. Every connection is a blip; the
  angle comes from its network (so peers from the same company's servers stack
  up at the same angle), distance from the centre is how long it has been
  connected, filled = outgoing, ring = incoming, diamond = Tor. Clusters at or
  over the eclipse limits are highlighted and named. Tooltips and the table
  show each peer's round trip (`rtt_ms`, `retrans`). Each blip stays put from
  one reading to the next and only moves as its connection ages, so one being
  read keeps its details; a reading without the list keeps the last one drawn,
  dimmed, for 3 s (as the node tree does). The table is filled only while open.
- **D · Route** (`route`): other nodes → the network link the node's p2p port
  is bound to, if any (from `host.bind_if`, titled VPN when it's a tun/WireGuard
  interface) → your node → Tor and the dashboard. An add-on route item with
  `between: "peers-node"` takes the middle spot; others hang below the node.
- **E · Controls** (`controls`): ban a range (the ranges flagged right now as
  one-click suggestions) and the short bans (24 h or less left) with UNBAN,
  plus a button to the command drawer.
- **F · Traffic** (`traffic`): download / upload over the last 5 s and since
  start, last 15 minutes in the server's 5 s rows (the newest one moving with
  each reading).
- **G · History** (`history`, two wide): 15 min / 1 h / 6 h / 24 h charts of
  peers, mempool, traffic.
- **H · Mempool** (`mempool`): waiting transactions, size, oldest, double
  spends, how long they've waited.
- **I · Log** (`events`, two wide): the event list, newest first; lines from
  monerod's log carry a MONEROD LOG tag.
- **J · Computer** (`computer`, two wide): from `snapshot.host`. The monerod
  process (CPU, memory, disk writes, a 15-minute CPU chart, PID and running
  time, program path with "replaced on disk, restart to use the new one",
  threads, open files, the flags it was started with) and the computer (CPU
  temperature with a 15-minute chart, power and battery, memory and
  chain-drive meters, CPU load, uptime, network link and traffic, Tor proxy).
  Temperature colours are display-only: heads-up from 85 °C, problem from
  95 °C; `display.temp_unit` picks °F or °C.
- **K · System** (`system`): version, uptime, disk free / blockchain size,
  bans, network rules version, fee estimate, RPC answer time.
- **Command drawer**: every monerod console command, grouped, searchable,
  each with a form for its arguments and its answer drawn as tables; plus log
  views (monerod's, and any an add-on offers). Change commands open a confirm
  dialog that needs a typed word and shows the arguments being sent.
- **Add-on panels**: placed on the screen their `tab` names (STATUS if none),
  framed by the core with an ADD-ON tag in the header (see Core and add-ons).

The settings menu slides in from the right and is generated from the settings
schema, with TEST CONNECTION and FIND MY NODE in the data source section and
one section per add-on.

Visual rules: see `docs/STYLE.md`.
