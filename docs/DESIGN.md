# Memstara: design and data contract

This file is the contract between the server (`memstara/`), the screens (`web/`)
and the tests (`tests/`). If code and this file disagree, fix one of them.

## Goals

- A desktop app for one Monero node, on the node's own machine or one that
  reaches it over the network, in its own window: never a browser, no address
  bar, no tabs. The window (GTK 4 with WebKitGTK) draws the screens in `web/`,
  and their data comes from the app's own server on `127.0.0.1`, which answers
  only that window (see **The app window**).
- **Monero itself is never modified.** The dashboard only talks to `monerod`
  through its public JSON-RPC interface, plus a read-only look at its log
  file (see **monerod's log: what RPC can't show**). Updating Monero never
  needs a dashboard change unless Monero changes its RPC.
- **Generic.** It works with any `monerod` on Linux and assumes nothing about
  the user's own scripts, services or network. It loads no code from outside
  `memstara/` and `web/`.
- **Read-only.** Nothing it sends can change the node. The RPC client has one
  list of methods (`READ_ALLOWED` in `memstara/rpc.py`), every one a read, and
  refuses anything else before a request is made; `tests/test_safety.py` names
  every method on it and fails if one of monerod's methods that change
  something is added. The command drawer has `monerod`'s read-only console
  commands, reading exactly what each one reads (Monero's console commands
  are thin wrappers around RPC methods, `src/daemon/rpc_command_executor.cpp`).
  The commands that change something (`ban`, `out_peers`, `stop_daemon` and
  the rest) are left to `monerod`'s own console. See **Commands**.
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

## Process model

`python3 -m memstara` (from the repo folder, or the app's icon) checks that
the window's packages are there (if not, it prints the `apt install` line and
stops), then asks the desktop whether a copy is already running. If one is, it
shows that copy's window and exits. Otherwise this copy:

1. Loads settings from `$XDG_CONFIG_HOME/memstara/settings.json`
   (default `~/.config/memstara/settings.json`), creating it with
   defaults on first run (file mode 0600, folder 0700), so a first run reads
   the node at `http://127.0.0.1:18081`, monerod's own default RPC address,
   until another one is typed in Settings. Every save sets a
   folder that was already there to 0700 too, when it's the user's own and
   not the home folder, a filesystem root or a sticky folder like `/tmp`.
2. Starts one **poller** thread that reads the node, and the lines monerod's
   log gained, every `source.refresh_s` seconds (every second by default),
   runs the alert rules, records
   history and events, and stores the latest snapshot. The window only ever
   reads that stored snapshot, so it never adds load on the node, and it gets
   each new one the moment the poll that made it ends (see **The snapshot**).
3. Serves the screens and the API on `127.0.0.1:<server.port>` (default
   18200), with a new window key (see **HTTP API and safety rules**).
4. Opens the window (not with `--background`).

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
`--port N` and `--config PATH`. `--install`, `--uninstall` and `--autostart`
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
  (`--port`, `--config`, `--safe-graphics`, `--debug`) don't reach the running copy, and the second start says so. If
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

The poller's reads (the command drawer's are under **Commands**; anything not
on `READ_ALLOWED` raises `RpcNotAllowed` before a request is made):

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
| `get_transaction_pool_hashes` | `/get_transaction_pool_hashes` | when the mempool's totals change, while details are owed, and at least every 10 s | every waiting transaction (`memstara/pool.py`) |
| `get_transactions` | `/get_transactions` | once for each new waiting transaction, up to 100 per call and 200 per poll | its fee, weight and arrival time |
| `get_block` | `/json_rpc` | when a block arrives | which waiting transactions it took |
| `get_output_distribution` | `/json_rpc` | a year of blocks once (40,000 per poll), then the newest 30 when the top changes | RingCT outputs per block for the black marble watch (see **Black marble watch**) |

Facts checked in Monero's source (`src/rpc/core_rpc_server.h`,
`core_rpc_server.cpp`, `core_rpc_server_commands_defs.h`, tag v0.18.5.3):

- `get_connections`, `get_bans`, `sync_info`, `get_net_stats`,
  `get_peer_list`, `get_transaction_pool`, `mining_status`, `banned`,
  `get_coinbase_tx_sum`, `get_alternate_chains` and `rpc_access_data` only
  exist on an **unrestricted** RPC port (`core_rpc_server.h`: `!m_restricted`
  in their map lines). On a
  restricted port `get_info` also zeroes the peer and RPC connection counts,
  `alt_blocks_count`, `start_time`, `update_available` and `version`, reports
  `free_space` as 2^64-1, rounds `database_size` up to a multiple of 5 GiB,
  and counts only relayed transactions in `tx_pool_size` (`on_get_info`); the
  adapter makes all of these null except `tx_pool`. The adapter detects
  `get_info.restricted == true` and reports it (see `source.restricted`), and
  the screens say, in grey since nothing is wrong, what that port hides and
  that the unrestricted port shows it. A restricted port is the safer choice:
  `monerod` refuses its admin commands there.
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
  `get_transaction_pool_hashes`, `get_transactions`, `get_block`,
  `get_output_distribution`, `get_fee_estimate`, `get_limit`,
  `hard_fork_info` and `get_version` on to that node and marks the answer
  `untrusted: true`
  (`use_bootstrap_daemon_if_necessary`); `get_info` then also carries
  `height_without_bootstrap` (the node's own height) and
  `bootstrap_daemon_address`. `get_connections`, `get_net_stats`, `get_bans`
  and `sync_info` are never passed on. While that lasts the poller takes only
  the height from `get_info` (`height_without_bootstrap`; the other node's
  height is the target, and synced is false) and leaves the rest of it `null`
  (outgoing and incoming counts come from `get_connections` instead). It
  doesn't send the passed-on calls: the block tape, reorg watch, mempool, fee,
  limits, rules version and RPC version keep the node's own last readings or
  stay `null`, and the black marble watch waits. The port isn't taken for restricted, `source.bootstrap` holds
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
  "t": 1759700000.123,                 // when this poll started, to the millisecond
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
    "last_block_seen": 1759699905.2,   // when the dashboard saw the top block arrive, else null
    "last_block_age_s": 94,            // generated - last_block_seen (else last_block_time), floored at 0
    "difficulty": 650000000000,
    "hashrate": 5416666666,            // difficulty / 120
    "out_peers": 22,                   // public (clearnet) outgoing, from get_info
    "out_wanted": 32,                  // thresholds.out_wanted if above 0, else monerod's default of 12
    "out_wanted_from": "setting",      // "setting" | "default"
    "in_peers": 31,
    "tor_out": 3,                      // outgoing .onion peers; null if Tor sending looks off
    "rpc_connections": 2,
    "white_peers": 1000, "grey_peers": 5000,
    "tx_pool": 34,
    "alt_blocks": 2,
    "free_bytes": 400000000000,        // null on a restricted port
    "db_bytes": 263000000000,          // null on a restricted port (monerod rounds it up to 5 GiB there)
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
    "max_weight": 600000,              // get_info.block_weight_limit (twice the median)
    "wait_odds": 0.43,                 // chance a block takes at least this long: exp(-age/120)
    "reorg": {"deepest_24h": 0, "last": null}, // last = {"time", "depth", "height"} or null;
                                       // deepest_24h is null until the node first answers
    "sync": null,                      // while syncing (sync_info): {"height", "target_height",
                                       // "spans", "blocks_queued", "peers", "overview"}; null when synced
    "flood": {"state": "ok", "rings": 14.7, ...}  // the black marble watch, see **Black marble watch**
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
       "spy": false}                   // its address is on the MRL spy node list (see **Spy node list**)
    ],
    "spread": {                        // biggest cluster each way (see the spread_out / spread_in alerts)
      "out16": {"net": "100.66", "n": 2, "ips": ["100.66.1.9", "100.66.4.2"]},
      "in24":  {"net": "100.70.3", "n": 1, "ips": ["100.70.3.4"]}
    },
    "out_by_kind": {"ipv4": 20, "ipv6": 2, "tor": 3, "i2p": 0},
    "in_by_kind":  {"ipv4": 31, "ipv6": 0, "tor": 0, "i2p": 0},
    "spy": {                           // the MRL spy node list (memstara/spylist.py)
      "state": "ok",                   // "ok" | "changed" (not the pinned file: not used) | "missing"
      "version": "2", "date": "2025-12-10",
      "entries": 431, "ranges": 14,    // 0 when not "ok"
      "node_bans": 431,                // how many of its entries get_bans shows; null on a restricted port
      "out": 0, "in": 0                // connected peers on it; null when the connections weren't read
    }
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
     "since": 1759699000, "source": "dashboard", "ips": null}
  ],
  "lamps": [                           // the annunciator wall: the 13 lamps in this order
    {"id": "node", "label": "NODE", "state": "ok", "value": "ANSWERING", "since": null}
  ],                                   // since = when it last left ok/off, null while ok/off
  "events": [                          // newest first, at most 200
    {"t": 1759699900, "level": "info", "kind": "block", "text": "New block 3,512,344 · 41 txs · 98 kB",
     "id": 812}                        // one up from the event before (since the dashboard started)
  ],
  "hist": {"t": 1759699995, "step": 5, "out": 22, "in": 31, ...},  // the history row still filling,
                                       // this reading included (see **History**)
  "clock_style": "12h",                // how its clock times are written: "12h" or "24h", "-utc" added in privacy mode
  "seq": 1234                          // this snapshot's number (see above)
}
```

### Lamps (annunciator wall)

`state` is one of `ok`, `warn`, `bad`, `off` (not set up / not known). Always
these 13, in this order. `value` is a
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
| `tor` | TOR SENDING | Tor sending on but 0 Tor peers for 10 min | RPC |
| `flood` | BLACK MARBLE | a new 16-member ring works like under 11 / 8 members for 30 min | RPC |
| `spy` | SPY NODE LIST | a connected peer is on the MRL spy node list, and 10 min after | RPC and the bundled list |

### Alerts

The dashboard computes every alert itself (`source: "dashboard"`).

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
| `flood` | warn / bad | the black marble watch's estimate: a new transaction's 16-member ring works like fewer than `flood_warn` 11 / `flood_bad` 8 members (see **Black marble watch**) | `grace.flood_min` 30 min | |
| `tor` | warn | `tor_sending` setting is `on` (or `auto` and Tor peers were seen in `get_connections` since the node last started) and 0 outgoing Tor peers | `grace.tor_min` 10 min | |
| `spy` | warn | a connected peer's address is on the MRL spy node list (`peers.list[].spy`) | | 10 min |

While the node isn't answering, only `node_down` is raised (the other rules
pause rather than firing on missing data).
`flood` also pauses while the watch has no estimate (the node syncing, using a
bootstrap node, or the year still being read), and keeps how long it has been
under its limits: the estimate moves slowly, so one that comes back still under
them carries on where it was, without a second "raised" event. `spy` pauses
while the connections can't be read (a restricted port) or the list isn't
usable. Alert texts are plain language.
Raising or clearing an alert adds an event.

Reorg detection: the poller keeps `height -> hash` for the last 100 blocks. When
the top block changes, it re-reads the shown range; every stored height whose
hash changed counts as replaced. The depth of a reorg is the number of replaced
heights found in one poll.

### Events

`{"t", "level": "info"|"warn"|"bad"|"ok", "kind", "text"}`. Kinds: `block`,
`reorg`, `peer_in`, `peer_out`, `peer_gone`, `spy`, `alert`, `clear`, `restart`,
`source`, `settings`, `sync` and
`log` (lines from monerod's log, see **monerod's log as events**). Peer join/leave events
are summarised ("3 joined · 1 left") instead of one line each: the first change
after a quiet spell at once, then at most one event per 5 seconds, and a peer
that leaves and comes back in between isn't counted. A `spy` event says a
peer on the spy node list connected, with its address and direction, at most
once an hour for each address. Kept in memory (last 200).

## Spy node list (`memstara/spylist.py`, `third_party/mrl-ban-list/`)

Memstara comes with the Monero Research Lab's spy node list, from
github.com/Boog900/monero-ban-list at commit `22ddf399`: `ban_list.txt` and
its five signatures, copied byte for byte into their own folder.
`third_party/README.md` gives the hashes and the commands to compare them with
the original and check the signatures. On 2026-10-18 the DNS blocklist
`monerod --enable-dns-blocklist` reads (`blocklist.moneropulse.*`) held the
same 431 entries.

- `SpyList` reads `ban_list.txt` once at start, read-only, and uses it only
  when its sha256 is `SHA256` in `spylist.py`. Otherwise `state` is `changed`
  (or `missing`), nothing is flagged and the lamp reads LIST CHANGED (or LIST
  MISSING).
- Lines are read as `monerod` reads a `--ban-list` file
  (`src/p2p/net_node.inl`, `handle_command_line`): text after `#` is a
  comment, blank lines are skipped, an entry with `/` is an IPv4 range.
- Each IPv4 peer in `get_connections` gets `spy`: its address is an entry or
  inside a range. Tor, I2P and IPv6 peers never are (the list has only IPv4).
- `node_bans` counts the entries `get_bans` covers: an address banned itself
  or inside a banned range, a range banned itself or inside a wider one.
  `monerod` lists each banned address and range on its own
  (`block_host`, `block_subnet`), so a node started with the list bans all
  431. `--ban-list` bans never end; DNS blocklist bans last 8 days and are
  renewed.
- A banned peer's connections are dropped, and it can't connect again, so a
  connected peer on the list means the node doesn't ban it. That's when the
  `spy` alert and the ▲ marks show. With the whole list banned, the lamp reads
  NODE BANS ALL.
- On a restricted port `get_connections` and `get_bans` are hidden, so the
  lamp is off (NO DATA).
- Updating the list is a change by hand: new files from a named commit, and
  the hashes in `third_party/README.md`, `spylist.py` and
  `tests/test_spylist.py`. Memstara never fetches it.

## Black marble watch (`memstara/flood.py`)

A black marble flood is one sender making a great many outputs (paying
themselves over and over) so that other people's wallets pick theirs as
decoys. Every decoy the sender owns is one they know isn't the coin being
spent, so a 16-member ring hides it among fewer members. Nobody can see who
owns an output, so the watch can only estimate: how much of a new
transaction's decoys a recent surge of new outputs would supply, and what its
ring works like if one sender made the whole surge. It follows Rucknium's
study of the suspected flood of March 2024, ["March 2024 Suspected Black Marble
Flooding Against Monero: Privacy, User Experience, and
Countermeasures"](https://github.com/Rucknium/misc-research/blob/main/Monero-Black-Marble-Flood/pdf/monero-black-marble-flood.pdf)
(draft v0.3, October 2024), which put a new ring at about 5.5 members within
five days, if one sender made the extra transactions. That study picked out
the suspected spam by its shape (1 input, 2 outputs, the lowest fee) against
the four weeks before; a watch running every day knows neither, so it counts
every new output above a usual day.

Each time the top block changes:

1. **Counts.** `get_output_distribution` with `amounts: [0]` and
   `cumulative: false` gives the RingCT outputs each block made: the counts
   wallets read to pick decoys (they ask for them from block 0, added up). A
   restricted RPC port answers it too: monerod limits restricted ports to
   amount 0, which is all the watch asks for. The first reading takes the last
   262,809 blocks (a year, plus the 9 newest, which a wallet can't pick from
   yet), 40,000 per poll; after that only the newest 30, which also catches
   blocks replaced at the top. Each answer's `base` (the outputs before its
   first block) must equal what was kept, or deeper blocks changed and the
   year is read again. After a few days it keeps 2,000 blocks beyond the year,
   so a chain that gets a little shorter (blocks popped) needs no new read.
2. **A usual day**: the median of the last 180 days' totals (720 blocks a
   day).
3. **Where wallets pick decoys** (`gamma_picker` in `src/wallet/wallet2.cpp`,
   v0.18.5.3): an age whose logarithm is gamma distributed (shape 19.28, rate
   1.61), less the 20-minute unlock time (an age under it becomes a random one
   in the newest 30 minutes). The age becomes a number of outputs back from
   the newest block a wallet can pick from (top − 9) at the chain's average
   time per output over the last year (`120 × 262,800 ÷ outputs in the last
   year` seconds), then a random output of the block holding that one. The
   watch uses this distribution exactly (`picked_cdf`) instead of drawing
   from it. About 54% of picks land within 2 days of the top, 75% within 10
   days, and under 4% further back than a year.
4. **The surge's share** `p`: the newest 48 hours an hour (30 blocks) at a
   time, then a day (720 blocks) at a time back to a year. In each stretch
   the outputs above the usual rate count as the surge's, so a pick landing
   there lands on one of them with chance `1 − usual ÷ outputs`; `p` adds that
   up, weighted by how often picks land in the stretch. A quiet stretch counts
   against the surge, so the normal ups and downs cancel out, and `p` is kept
   between 0 and 1. Outputs older than a year count as nobody's.
5. **The ring**: `1 + 15 × (1 − p)` members the sender doesn't own, the real
   coin among them (the study's step 5: one plus 15 times the chance that a
   decoy isn't the sender's).

`snapshot.chain.flood`:

```jsonc
{"state": "ok",                // "ok" | "filling" | "off" | "error"
 "height": 3783389,            // the top block it was worked out for
 "rings": 14.7,                // 1 + 15 × (1 − share), one decimal
 "share": 0.088,               // the surge's share of a new transaction's decoys, 0 to 1
 "usual_day": 69220,           // outputs on a usual day (the median of the last 180)
 "last_day": 80295,            // outputs in the newest 720 blocks
 "volume_x": 1.16,             // last_day ÷ usual_day
 "days": [68800, ...],         // the last 30 days' totals, oldest first
 "why": null}                  // with "ok": the last read's failure in a few words, if it failed
```

Without an estimate: `{"state": "filling", "progress": 0.46}` while the year
is read; `{"state": "off", "why": ...}` with `starting` (no reading yet),
`sync` (the node isn't synced), `bootstrap` (monerod answers through a
bootstrap node, so the counts would be that node's) or `short` (less than a
year of RingCT outputs, as on a new test network); `{"state": "error", "why":
"no answer in 8 s"}` when reading failed. After a failed read it tries again
in 60 s (10 min after `short`) and keeps showing the last estimate, with
`why`, until it is more than 30 blocks behind the node's top.

The BLACK MARBLE lamp reads `RING ≈ 15 OF 16`, or, unlit, `READING 46%`,
`WAITS FOR SYNC`, `CHAIN TOO SHORT`, `CAN'T READ` or `NO DATA`. The `flood`
alert is a heads-up under `thresholds.flood_warn` (11) and a problem under
`thresholds.flood_bad` (8), each after `grace.flood_min` (30 min): "A recent
surge of new outputs would supply about 63% of a new transaction's decoys. If
one sender made them, its 16-member ring works like about 6", and as a
problem, ": payments where privacy matters can wait for it to clear". Panel L
is on the CHAIN screen (see **Screens**).

Checked against Monero's main chain: the counts from block 1,400,000 (2017)
to 3,783,400 (October 2026), read from a public node with the same call, and
one estimate every 6 hours from September 19, 2018 (11,782 in all). Every year
is worked out as today's wallets would build a ring, with 16 members (rings had
11 until the network upgrade of August 13, 2022):

| year | under 11 | under 8 | lowest | |
|---|---|---|---|---|
| 2018 (from Sep 19) | 0% | 0% | 12.6 | |
| 2019 | 18.2% | 1.9% | 6.6 (May 4) | a surge through May and June; no published cause found |
| 2020 | 5.9% | 0% | 9.6 | short spells |
| 2021 | 0% | 0% | 11.0 | |
| 2022 | 0.5% | 0% | 9.4 (Aug 14) | August 13 and 14, around that upgrade |
| 2023 | 0.8% | 0% | 10.3 (Dec 22) | December 20 to 23; no published cause found |
| 2024 | 19.9% | 8.0% | 5.2 (Mar 26) | the suspected flood: under 11 from March 4, under 8 from March 6 to 27 (72% of decoys, 4.7 × a usual day at the lowest); more waves in April, May and June |
| 2025 | 0% | 0% | 13.1 | |
| 2026 (to Oct 12) | 0% | 0% | 12.0 | |

Why 180 days: in a run comparing usual days (an estimate every 12 hours from
October 2019), the median of 365 days lit the heads-up for 37.7% of 2020 and
24.9% of 2021, as use grew steadily, and 82.7% of March 4 to June 15, 2024
(49.5% at the problem level). 180 days: 6.7% and 0%, and 70.2% of the flood
(30.3%). 90 days: under 1% in every normal year, but only 40.9% of the flood
(21.2%), since a long flood soon becomes its own usual day.

What it can't do:

- Tell a flood from a busy spell of ordinary use, or see who made the
  outputs: both are outputs above a usual day. "If one sender made the surge"
  is the worst case, not a finding.
- Keep noticing a flood past about three months: by then it fills half of
  the 180 days and becomes the usual day.
- Count anything older than a year (wallets rarely pick there).
- Help after Monero's planned FCMP++ upgrade, which proves a spend against
  every output on the chain instead of a 16-member ring ([Monero's FCMP++
  post](https://www.getmonero.org/2024/04/27/fcmps.html): "from an immediate
  anonymity set of 16 to 100,000,000"). The watch has nothing to measure
  then.

monerod keeps one cache of these counts for every caller
(`RpcHandler::get_output_distribution` in `src/rpc/rpc_handler.cpp`) and
replaces it with each request for a different range. The watch asks once a
block, so a wallet building a transaction through the same node right after
makes it count from block 0 again instead of adding the newest blocks to its
cache.

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
 "rate_in": [60000, ...], "rate_out": [140000, ...], "latency": [14, ...]}
```

The file's columns are matched by name, so a history file from a version with
other fields still loads: a field it doesn't have is `null` for the older
samples, and one that is no longer kept is left out.

## Settings

`GET /api/settings` returns `{"values": {...}, "schema": [...]}`. Passwords are
never sent back: the value is `""` plus `"rpc_password_set": true|false`.
`PUT /api/settings` takes a partial `values` object, validates every key
against the schema, saves atomically (temp file + rename, mode 0600), and
answers with the same shape as GET (or `400 {"errors": {"key": "reason"}}`).
`POST /api/settings/reset` with `{"section": "thresholds"}` restores one
section's defaults.

The file carries `"format": 2`. A file without it is from before reads every
second: when it loads, `source.refresh_s` 5 (the old default) becomes 1,
once, and the file is saved with `"format": 2`. Any other
value someone picked stays, and everything else in the file is written back as
it was (keys nobody knows are dropped at the next save).

Schema entries: `{"key": "thresholds.low_peers", "section": "thresholds",
"label": "Few outgoing below", "help": "...", "type":
"int"|"float"|"bool"|"choice"|"text"|"password"|"url"|"path", "min", "max",
"step", "choices": [{"value", "label"}], "default", "unit"}`.

| key | type | default | notes |
|---|---|---|---|
| `source.rpc_url` | url | `http://127.0.0.1:18081` | http/https only; the default is monerod's own default RPC address |
| `source.rpc_user` | text | `""` | only if monerod has `--rpc-login` |
| `source.rpc_password` | password | `""` | |
| `source.refresh_s` | choice 1/2/5/10/30 | 1 | seconds between reads of the node |
| `source.timeout_s` | int 2-30 | 8 | |
| `thresholds.out_wanted` | int 0-1000 | 0 | 0 = monerod's default of 12; if monerod runs with `--out-peers` (or it was changed with monerod's own `out_peers` command), that number goes here |
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
| `thresholds.flood_warn` | int 2-16 | 11 | black marble heads-up: a new ring works like fewer members than this, of 16 |
| `thresholds.flood_bad` | int 1-15 | 8 | must be < warn |
| `thresholds.tor_sending` | choice auto/on/off | `auto` | |
| `grace.node_down_min` | int 0-120 | 5 | |
| `grace.no_peers_min` | int 0-120 | 5 | no outgoing at all |
| `grace.peers_min` | int 0-120 | 10 | few outgoing |
| `grace.syncing_min` | int 0-240 | 20 | |
| `grace.spread_min` | int 0-120 | 10 | |
| `grace.tor_min` | int 0-120 | 10 | |
| `grace.flood_min` | int 0-1440 | 30 | |
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
| `display.privacy` | bool | false | privacy mode; only `POST /api/privacy` changes it (see **Privacy mode**) |
| `panels.<id>` | bool | true | show or hide each panel (ids below) |
| `chain.blocks_shown` | int 20-200 | 60 | |
| `node.log_path` | path | `~/.bitmonero/bitmonero.log` | monerod's own log (its `--log-file`, else `bitmonero.log` in its data folder), read-only. Left at the default while the RPC address isn't on this computer, it isn't read (see **An RPC address not on this computer**) |
| `server.port` | int 1024-65535 | 18200 | the window's local port; takes effect the next time the app starts |

Panel ids: `lamps`, `chain`, `peers`, `route`, `bans`, `traffic`,
`history`, `mempool`, `events`, `system`, `flood`.

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
| `GET /api/node-log` | the last lines of monerod's log (guarded) |
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
- **Write guard (CSRF):** `PUT`/`POST` and `GET /api/node-log` (it reads a
  file on this computer) need the header `X-Memstara: 1` and, when an
  `Origin` header is present, it must match the Host (a `Sec-Fetch-Site: cross-site` is refused too);
  otherwise `403`. No CORS headers are ever sent.
- Static files are served only from a list built at start-up by walking `web/`
  (no path joins from the URL), with
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
| `port` | every port key, "port 47213" in text, a port after a hidden address (`⟦ip⟧:⟦port⟧`), and a port after 0.0.0.0 or :: (`0.0.0.0:⟦port⟧`, `[::]:⟦port⟧`, `:::⟦port⟧`, also `[::ffff:0.0.0.0]:⟦port⟧`) with the address shown: that's how monerod writes where it listens ("Binding (IPv4) on 0.0.0.0:…" in its log), so the port is your node's own p2p port |
| `tor`, `i2p` | onion and I2P addresses |
| `host`, `user`, `name` | this computer's name, your user name and your real name, as whole words, and the node's computer's name when its RPC address gives one (`node.lan:18081`) |
| `login` | the login in an address (`http://me:pw@…`) |
| `hex`, `tx` | any run of 64 or more hex digits (transaction ids, key images, a whole transaction in a log line), except block hashes under their own keys |
| `id` | peer ids |
| `addr` | Monero addresses |
| `mac` | hardware addresses |

Your home folder becomes `~`. This computer's name, your user name, your real name and
your home folder are read (from the system, at start) only to hide them. Every string
goes through the patterns, so a field added later still has its addresses, names and
paths caught; the key rules cover what a pattern can't recognise alone (a bare port
number, a range written "1.2").
A waiting transaction your node hasn't passed on
yet is left out of the mempool list (only your node has it, so it could be yours), and
the count of those is dropped. The snapshot gets `"privacy": true`.

**Clocks show UTC**, so a screenshot doesn't give the time zone away: the window's
clocks (the top bar says UTC, in the narrow layout too), the times the server writes into texts
(`clock_style` gets `-utc`; the snapshot says which it was written with in
`clock_style`), events written before the switch (their local times are turned into UTC
by their own `t`), the first snapshot after the switch, and local times in a log line
("2025-06-01 09:30:00" in a line another program wrote; monerod's own lines are UTC
already and carry fractions of a second).

**Locked while it's on:** settings that hold an address, a folder or a login (types
`text`, `url`, `path`, `password`) show their hidden form, read-only, and their sections
can't be reset; any value from the window with a token in it is refused; TEST
CONNECTION refuses hidden values.

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
came), block heights and hashes, fees, loopback addresses and their ports (the RPC
address on this computer, `127.0.0.1:18081`), and anything you type yourself, as you
type it. A transaction you send shows up as a new dot on the mempool screen once your
node passes it on, so with someone watching the screen at that moment, the timing could
point at it.

## Commands (monerod's read-only console commands)

`memstara/commands.py` holds one entry per read-only `monerod` console command
(names and usage from `src/daemon/command_server.cpp`, v0.18.5.3), with the
RPC calls its executor makes (`src/daemon/rpc_command_executor.cpp`). Every
call is on `READ_ALLOWED`; `check_registry()` and `tests/test_safety.py` fail
if one isn't.

`GET /api/commands` → `{"commands": [{"name", "aliases", "group", "title",
"summary", "usage", "calls": ["get_bans"], "params": [{"name", "label",
"type": "int"|"text"|"choice"|"bool", "required", "min", "max", "choices",
"placeholder", "help"}]}]}`. Metadata only.

`POST /api/command` `{"name", "args": {...}}` → `200 {"ok": true, "name",
"calls": [...], "result": {"<rpc method>": <its result object>, ...},
"message"?: "<plain sentence>"}`; bad arguments `400 {"ok": false, "error",
"field"?}`; a name that isn't one of these `404` ("There's no read-only
monerod command called X."); node errors `502` with monerod's own error
message. The write guard (header + Origin) applies to `POST /api/command`.

Arguments are validated before anything is sent (types, ranges, 64-hex
hashes). A few commands get a safety limit the console doesn't have; each is
noted below and in the command's `help`.

| group | command (aliases) | RPC calls | arguments, notes |
|---|---|---|---|
| Node | `status` | getinfo, mining_status, hard_fork_info | like the console: ", bootstrapping from X, local height: H (P%)" while get_info comes from a bootstrap node, " was used before" after. On a restricted port mining_status isn't there, so it says "mining info unavailable" |
| Node | `print_status` | (getinfo, to see it answers) | |
| Node | `version` | getinfo | |
| Node | `print_height` | /getheight | |
| Node | `diff` | getinfo | |
| Node | `sync_info` | sync_info | |
| Chain | `print_bc` | getinfo, getblockheadersrange | `begin`, `end`; at most 1,000 blocks per request (dashboard limit) |
| Chain | `print_block` | getblock | `block`: height or 64-hex hash |
| Chain | `bc_dyn_stats` | getinfo, get_fee_estimate, hard_fork_info, getblockheadersrange | `last` 1-10,000 (dashboard limit) |
| Chain | `alt_chain_info` | getinfo, get_alternate_chains, getblockheaderbyhash | optional `blockhash`; block headers for chains up to 1,000 blocks long (dashboard limit) |
| Chain | `hard_fork_info` | hard_fork_info | optional `version` |
| Chain | `print_coinbase_tx_sum` | get_coinbase_tx_sum | `start`, `count` 1-100,000 (dashboard limit; big ranges keep the node busy) |
| Chain | `output_histogram` | get_output_histogram | optional `amount` (atomic units), `min_count`, optional `max_count` |
| Transactions | `print_tx` | /gettransactions | `txid` 64-hex, `hex` bool, `json` bool. Differs from the console here: the console always sends `decode_as_json: false` and turns the raw transaction into JSON itself (C++ `parse_and_validate_tx_from_blob`), which the dashboard can't do in plain Python, so with `json` on it sends `decode_as_json: true` and shows monerod's own JSON (`txs[].as_json`) |
| Transactions | `is_key_image_spent` | /is_key_image_spent | `key_image` 64-hex |
| Transactions | `print_pool` (`print_pool_sh`) | /get_transaction_pool | |
| Transactions | `print_pool_stats` | /get_transaction_pool_stats, getinfo | |
| Network | `print_cn` | get_connections | |
| Network | `print_pl` | /get_peer_list (include_blocked: true) | `list`: all/white/gray, `pruned` bool, `publicrpc` bool, `limit`. As the console (rpc_command_executor.cpp): `limit` takes the first N of each list, then `pruned` / `publicrpc` keep only those among them that fit, so fewer than N can show |
| Network | `print_pl_stats` | /get_peer_list | |
| Network | `print_net_stats` | /get_net_stats, /get_limit | |
| Network | `limit` | /get_limit | no value: the console's `limit` with a value sets the limits, which this one can't |
| Bans | `bans` | get_bans | |
| Bans | `banned` | banned | `address` |
| Mining | `mining_status` | /mining_status | |
| Mining | `rpc_payments` | rpc_access_data | |

`help` and `apropos` are the drawer's search box.

Left out, because they change something: `start_mining`, `stop_mining`,
`show_hr`, `hide_hr`, `save`, `set_log`, `stop_daemon` (`exit`), `limit` with
a value, `limit_up`, `limit_down`, `out_peers`, `in_peers`, `ban`, `unban`,
`flush_txpool`, `update`, `relay_tx`, `pop_blocks`, `prune_blockchain`,
`check_blockchain_pruning` (it sends `prune_blockchain`, which prunes unless
asked only to check), `set_bootstrap_daemon` and `flush_cache`. Their RPC
methods aren't on `READ_ALLOWED`, so they can't be sent either.

Facts checked in the source, for the ban screens: monerod's automatic bans
last 24 h (`P2P_IP_BLOCKTIME`), DNS blocklist bans 8 days, `--ban-list` file
bans forever; a host with a `/` is an IPv4 subnet ban (`block_subnet` drops
every connection in it). monerod keeps bans only in memory, so bans run out
or end when the node restarts; bans from `--ban-list` and the DNS blocklist
come back when it starts.

### monerod's log (read-only)

`GET /api/node-log?source=monerod&lines=N` (N ≤ 1,000, default
200; `source` defaults to `monerod`) returns the last lines of
`node.log_path` (default `~/.bitmonero/bitmonero.log`), read from the last 512 KB of the file:
`{"ok", "path", "size", "mtime", "lines": [...], "source"}`. Never writes,
never follows symlinks out of the user's home, refuses anything but a regular
file (`403`), and never blocks on a pipe. It also refuses (`403`) a file
inside one of the dashboard's own folders
(`~/.config/memstara`, `~/.local/state/memstara`, `~/.cache/memstara`, or
their `$XDG_*` places) and one whose name (after following links) has no
`.log` ("X isn't a log file (its name has no .log), so it isn't read.").
monerod's log as events (`logevents.py`) follows the same rules. Any
other source is `400`
("source must be monerod"), and a missing file `404` ("monerod's log isn't
at PATH."). While the RPC address isn't on this computer, the default path is
`404` too ("The node's RPC address isn't on this computer, so the log at the
default place may be another node's. To read the node's own, set monerod's log
in Settings."; see **An RPC address not on this computer**).

## monerod's log: what RPC can't show

Some things about the node aren't in any RPC answer (blocks replaced and
blocks that lost a race the moment monerod sees them, bans, errors), so the
dashboard also reads monerod's own log file, `node.log_path`. That's all it
reads besides RPC and its own files: nothing about the computer itself
(privacy mode only looks up this computer's name, your user name, your real
name and your home folder, to hide them, and when the window's packages are
missing, `/etc/os-release` is read to print the right install command).
The log is only read, as the user, and one that can't be read leaves a short reason instead. Every poll, `memstara/logevents.py` reads
the lines it gained and the poller turns them into events; the command drawer
shows its last lines (see **monerod's log (read-only)**).

### An RPC address not on this computer

The dashboard can't see where the node runs, only its RPC address. When the
host in `source.rpc_url` isn't this computer (127.0.0.0/8, `::1`, `localhost`,
or `0.0.0.0` and `::`, which Linux takes as this computer too; `on_this_computer`
in `memstara/rpc.py`), the node may run on
another computer, and the log at the default place may be another node's. So
while `node.log_path` is still its default, monerod's log isn't read: no log
events, and `/api/node-log` answers `404` with why. A path set by hand (a copy,
or a mounted folder) is read as usual. `source` events say once per node that
the RPC address isn't on this computer, that plain `http` across a network can
be read and changed by others (on the unrestricted port, a read can be turned
into a command; an SSH tunnel keeps it private, see the README), and, when
the node answers with no login, that anyone who reaches its unrestricted port
can change it (start monerod with `--rpc-login`). An SSH tunnel's near end is
on this computer (`127.0.0.1`), so a node read through one counts as here.

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
| `Host X blocked.` / `Subnet X blocked.` | net_node.inl | info: "monerod banned X" |
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

## Screens (web/)

One page with a tab bar under the top bar. Each tab is a screen; one shows at
a time (`display.layout` "tabs"), or every screen stacks on one page ("page").
The open screen is kept in the address (`#network`) and in the window's
storage, so the app opens on it next time. Keys 1 to 7 switch screens when keyboard shortcuts are on. A tab shows
▲ or ■ when an alert belongs to its screen (STATUS shows the worst of all), and
that screen's centrepiece frame glows in the heads-up colour, or pulses in the
problem colour.

Each screen opens with its **centrepiece** ("hero", numbered 01 to 07), an
animated picture drawn from real readings, then the older lettered panels, in
a responsive grid (three columns wide, two below 1400 px, one when narrow).

| Tab | Centrepiece | Panels after it |
|---|---|---|
| 01 STATUS | NODE STATUS: a ring with one segment per lamp (lit ▲ / ■ when not ok; click one to open the screen that explains it), arcs for outgoing peers, sync and disk, the height and the time since the last block in the middle, and quick readouts that open their screens | A lamp wall |
| 02 NETWORK | NODE TREE: a 3D tree you can turn and zoom. Your node at the top, then outgoing / incoming / Tor, then peers on one company's servers grouped, then every peer. Dots running along the branches are the bytes each peer really moved since the last reading; busy peers are lit, quiet ones dim. LIVE CHATTER beside it is a log of peers joining and leaving, blocks, bans, the busiest peers and the pool, newest at the bottom | C peer scope, D route |
| 03 CHAIN | BLOCK STREAM: the last blocks as 3D boxes receding into the distance, sized by weight and spaced by the wait before each, with a ghost NEXT BLOCK and its timer; blocks replaced flash ▲ | B chain, K system, L black marble watch |
| 04 MEMPOOL | TRANSACTION POOL: one dot per waiting transaction in bands by how long it has waited; dots drop in as they arrive and leave in a block-shaped sweep | H mempool |
| 05 TRAFFIC | LIVE TRAFFIC: a scope of download and upload over 15 minutes, with the node's limits when they are near, and flow lanes whose dots speed up with the data | F traffic, G history |
| 06 BANS | BAN CLOCK: a clock with one ring per ban of 24 h or less, each draining as its time runs out; click one to find it in the ban list | E ban list |
| 07 LOG | EVENT TIMELINE: lanes of what happened in the last hour (blocks, peers, the node, alerts, monerod's log, blocks replaced), with the newest typed out underneath | I log |

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
  name with the network, the node's uptime and its version, overall status lamp (OK / HEADS-UP / PROBLEM), LIVE
  badge with the RPC address, local clock, PRIVACY, COMMANDS and SETTINGS buttons.
  **Alerts strip** below it: active alerts, problems first. **Status bar** at
  the bottom: RPC address, answer time, poll.
- **A · Lamp wall** (`lamps`): the 13 lamps. Lit in
  the heads-up or problem colour with ▲ / ■ when not ok. New problem lamps
  flash until clicked (acknowledging is only a screen state and changes
  nothing on the node). Each lamp's tooltip says what it watches.
- **B · Chain** (`chain`, two wide): height, synced %, time since the last
  block with a ring that fills toward 2 minutes, "chance of a wait this long"
  in plain words; the block tape: the last N blocks as bars (height = weight,
  spacing = time between blocks).
- **C · Peer scope** (`peers`): a round radar. Every connection is a blip; the
  angle comes from its network (so peers from the same company's servers stack
  up at the same angle), distance from the centre is how long it has been
  connected, filled = outgoing, ring = incoming, diamond = Tor. Clusters at or
  over the eclipse limits are highlighted and named. Each blip stays put from
  one reading to the next and only moves as its connection ages, so one being
  read keeps its details; a reading without the list keeps the last one drawn,
  dimmed, for 3 s (as the node tree does). A peer on the spy node list gets a
  ▲ over its blip, a line under the scope and ▲ SPY LIST in the table. The
  table is filled only while open.
- **D · Route** (`route`): other nodes → your node → Tor sending and the
  dashboard (with the RPC answer time), from the connection counts and the Tor
  peers in `get_connections`.
- **E · Ban list** (`bans`): the short bans (24 h or less left) with their
  time left, a count of the longer ones, the ranges at the eclipse limits
  right now, and the spy node list: its version, how much of it the node bans
  and how many of its peers are connected. Read-only: banning is left to
  `monerod`'s own console. A button opens the command drawer.
- **F · Traffic** (`traffic`): download / upload over the last 5 s and since
  start, last 15 minutes in the server's 5 s rows (the newest one moving with
  each reading).
- **G · History** (`history`, two wide): 15 min / 1 h / 6 h / 24 h charts of
  peers, mempool, traffic.
- **H · Mempool** (`mempool`): waiting transactions, size, oldest, double
  spends, how long they've waited.
- **I · Log** (`events`, full width): the event list, newest first; lines from
  monerod's log carry a MONEROD LOG tag.
- **K · System** (`system`): version, uptime, disk free / blockchain size,
  bans, network rules version, fee estimate, RPC answer time.
- **L · Black marble watch** (`flood`, full width): what a new 16-member ring
  works like if one sender made the recent surge of new outputs, as a number
  and as 16 dots (the real coin, the decoys from a usual day's outputs, and
  hollow ones for the surge's), then the share of decoys from the surge, the
  last 24 hours against a usual day, the limits, and the last 30 days of new
  outputs as bars split at a usual day (with a table view). While the year is
  being read it shows how far along it is; otherwise why there's no reading.
- **Command drawer**: monerod's read-only console commands, grouped,
  searchable, each with a form for its arguments and its answer drawn as
  tables; plus the last lines of monerod's log.

The settings menu slides in from the right and is generated from the settings
schema, with TEST CONNECTION in the data source section.

Visual rules: see `docs/STYLE.md`.
