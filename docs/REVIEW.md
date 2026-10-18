# Reading the code

A guide for anyone checking Memstara before they trust it. It lists the
files in the order worth reading, what each one does and what to look for.
[SAFETY.md](../SAFETY.md) says what Memstara can and can't do, with a command
to check each point.

Memstara was written with an AI assistant (Claude) and hasn't been
independently reviewed yet, so please don't take the comments' word for
anything: check the code under them.

Everything that decides what Memstara can send, read, write or run is in the
first two groups, about 4,500 lines of Python using only its standard library
(plus GTK and WebKit through PyGObject for the window), and two functions in
`poller.py` named in group 2. The rest turns the node's answers into numbers
and pictures, and can only reach the node, the disk or the network through
those files.

## 1. What it can send to the node

| File | Lines | What it does | Look for |
|---|---|---|---|
| `memstara/rpc.py` | 375 | The RPC client. `READ_ALLOWED` is every method it can send; `call()` refuses anything else. | Each entry on the list is a read. Its comment gives the line in `monerod`'s `src/rpc/core_rpc_server.h` (v0.18.5.3) that maps the method to the function answering it and shows whether a restricted port hides it. To see that the function only reads, read it in `src/rpc/core_rpc_server.cpp` of the same version (for `get_info` that's `on_get_info_json`, for `get_fee_estimate` it's `on_get_base_fee_estimate`). Nothing but `call()` reaches `_call()`, and nothing but `_call()` reaches `_post()`. No redirects, no proxy, the login only in the Digest header. |
| `memstara/commands.py` | 766 | The COMMANDS drawer: `monerod`'s read-only console commands, each listing the RPC calls it may make. | Every command's calls are on `READ_ALLOWED`: `check_registry()` checks this, and `tests/test_safety.py` runs it. When a command runs, `_Ctx.call` refuses a call the command doesn't list, and `RpcClient.call` refuses anything not on `READ_ALLOWED`. Arguments typed in the drawer are checked before anything is sent, and none of them can turn a read into a change. |
| `tests/test_safety.py` | 364 | The safety tests: the list, the change methods that must never be on it, and checks on the code itself (what each file imports and calls, read with Python's own parser). | That the change list is complete for the `monerod` version you run, and that the code checks would catch the kind of change you'd worry about. |

## 2. Its own window, the disk and this computer

| File | Lines | What it does | Look for |
|---|---|---|---|
| `memstara/server.py` | 515 | The local server the window talks to. | It listens on 127.0.0.1 only. The page and every `/api/` request need the window key (`_preflight`); only the screens' own scripts, styles, fonts and pictures don't (`_needs_no_key`). Writes and `GET /api/node-log` also need the `X-Memstara: 1` header, and an `Origin` or `Sec-Fetch-Site` header, when one is sent, must be this server's own (`_write_guard`). Which routes exist and what each can do: none sends anything but the reads above. TEST CONNECTION sends `get_info` to the address you type, with the saved login only when it's the same address. |
| `memstara/app.py`, `memstara/__main__.py` | 170 | Start-up: settings, the server, the poller, the window. | The server's address (`App`'s `host`, 127.0.0.1). `__main__.py` refuses a `--config` file that doesn't look like Memstara's settings (`not_settings_file`) and passes on the key the window makes. `App(key=None)` turns the key check off, which only the tests do. |
| `memstara/window.py` | 582 | The GTK window around WebKit. | `_startup`: the window key, `secrets.token_urlsafe(32)`, new at each start and given only to its own view. `WEB_SETTINGS` (what the page may use), `_decide` (only its own address; no downloads), `_deny` (no camera, microphone or location), the proxy turned off, no new windows. `_actions`: the menu's actions, which other programs running as you can also reach over D-Bus. |
| `memstara/nodelog.py`, `memstara/logevents.py` | 410 | Read `monerod`'s log file. | `os.open` with read-only flags and `O_NOFOLLOW`. A link is followed only when it ends inside your home folder (`nodelog.resolve`, `logevents._open_log`), and if the file itself is swapped for a link after that check, it isn't opened (`O_NOFOLLOW` guards only the last part of the path, not the folders above it). Only a regular file whose name has `.log` in it is read, never one in Memstara's own folders (`util.log_path_refusal`), and it never blocks on a pipe. |
| `memstara/poller.py`: `test_connection`, `_log_events` | 38 | The two parts of the poller that decide something. | `test_connection` sends `get_info` and nothing else. `_log_events` doesn't read the log at the default place when the RPC address isn't on this computer, the same rule `nodelog.read` applies to the LOG screen. |
| `memstara/util.py` | 184 | Where its own folders are, and `atomic_write`. | `atomic_write`: a temp file in the same folder, then a rename, mode 0600. `settings.py`, `history.py` and `window.py` write only through it. `ensure_private_dir` makes the folder it's given readable only by you when it makes it (folders above that it makes get your usual permissions) and never changes a folder that isn't Memstara's own. |
| `memstara/settings.py` | 555 | The settings file and its rules. | Written only through `atomic_write`, mode 0600. A file it can't read is renamed to `settings.json.bad`. The password never goes back to the screens (`public`). |
| `memstara/history.py` | 182 | The chart history file. | Written only through `atomic_write`. |
| `memstara/desktop.py` | 192 | The entry in your apps and the start-at-login entry. | Its own `_write` (a temp file, then a rename, mode 0644) writes only its two `.desktop` files, and `_remove` deletes only those, both only when asked. |
| `memstara/privacy.py` | 535 | Privacy mode. | It reads this computer's name, your names and your home folder only to hide them; check that nothing it learns is sent anywhere. |

## 3. Turning answers into numbers

These open no file or connection themselves. They ask the node through
`rpc.py`, get log lines through `logevents.py`, save the history through
`history.py` and hand the screens a snapshot. The two `poller.py` functions
named in group 2 are the only parts of them that decide what is sent or read.

| File | What it does |
|---|---|
| `memstara/poller.py` | One thread: asks the node on a timer, reads new log lines, runs the alert rules and keeps the latest snapshot. |
| `memstara/adapter.py` | Turns RPC answers into the snapshot's shape. |
| `memstara/pool.py` | The mempool and fee numbers. |
| `memstara/flood.py` | The black marble watch (output counts). |
| `memstara/alerts.py`, `memstara/lamps.py` | When an alert fires and what each lamp shows. |

## 4. The screens

`web/` is plain HTML, CSS and JavaScript modules, with no build step and
nothing from the internet. The screens can only show what `server.py` hands
them: the page's content policy (`CSP` in `server.py`) lets it load and
connect to its own server only, and the one `fetch()` is in `web/js/api.js`.
Look there first; the rest draws.

## Running the tests

    python3 -m unittest discover -s tests -v

They need Python 3.10 or newer and nothing else, and they never reach a real
node: `tests/fake_monerod.py` answers with canned replies in `monerod`'s own
format, filled with made-up data (no real node's peers).
