# What Memstara can and can't do

Memstara only reads. This page lists what it reads, what it writes and what it
never does, and each point comes with a command that checks it. Run them from
the Memstara folder. They need nothing but Python 3.10 or newer and the usual
shell tools.

Here "Memstara" means its own code, in `memstara/` and `web/`. Python, GTK and
WebKit come from your system's packages and do things of their own, which `ps`
and `strace` show; the last section says what.

`tests/test_safety.py` checks most of these points again every time the tests
run, by reading the code with Python's own parser. It catches the usual ways a
change could break them, but a test is a tripwire, not a proof: reading the
files named here is the real check. [docs/REVIEW.md](docs/REVIEW.md) says
which files to read first and what to look for.

Memstara was written with an AI assistant (Claude) and hasn't been
independently reviewed yet.

## What it asks your node

**Only the reads on one list.** Every RPC method Memstara can send is in
`READ_ALLOWED` in `memstara/rpc.py`, 27 of them. Each one only reads. Each
has a comment with its line in `monerod`'s `src/rpc/core_rpc_server.h`
(v0.18.5.3), which names the function that answers it in
`src/rpc/core_rpc_server.cpp`.

    python3 -c "from memstara import rpc; print(*sorted(rpc.READ_ALLOWED), sep='\n')"

**Nothing else can be sent.** `RpcClient.call()` is the only way to send, and
it refuses a method that isn't on the list before it connects. No other file
reaches past it into the client's insides; the second command prints nothing.

    grep -n "def call" -A4 memstara/rpc.py
    grep -rn "client\._" memstara/ --include='*.py'

**No change method is on the list.** The tests name every `monerod` method
that changes something (stop, ban, limits, peer counts, mining, log level,
pruning, flushing, relaying, sending transactions and blocks, and more) and
fail if one is ever added. Others fail if anything but `call()` reaches the
sending code, or if a file other than `rpc.py` and `server.py` starts using
the network.

    python3 -m unittest discover -s tests -p test_safety.py -v

**The login stays with the node.** Memstara never follows a redirect, never
uses a proxy from the environment for the node, and never puts the login in a
message. The screens never see the password, only whether one is saved. TEST
CONNECTION in Settings sends one `get_info` to the address you type, with the
saved password only when it's the saved node's address (same scheme, host and
port).

    grep -n "_NoRedirect\|ProxyHandler" memstara/rpc.py
    grep -n "same_rpc" memstara/server.py

## What it reads on this computer

- **`monerod`'s log file**, read-only, for the LOG screen and a few events RPC
  doesn't report. Only a regular file whose name has `.log` in it, never one
  in Memstara's own folders, never through a link that leads out of your home
  folder, never when the file itself is swapped for a link after that check
  (`O_NOFOLLOW` covers the file's own name, not the folders above it), and
  never a pipe. Each reader opens it with
  `os.O_RDONLY`, plus only `O_NOFOLLOW`, `O_NONBLOCK` and `O_CLOEXEC`: no flag
  that writes, creates or empties a file. When the RPC address isn't on this
  computer, the log at the default place isn't read, since it may be another
  node's.

      grep -n "flags = " -A3 memstara/nodelog.py memstara/logevents.py
      grep -n "not on_this_computer" memstara/nodelog.py memstara/poller.py

- **Its own files** (below) and the screens in `web/`.
- **This computer's name, your user name, your real name and your home
  folder**, only so privacy mode can hide them.

      grep -n "gethostname\|getpwuid\|expanduser" memstara/privacy.py

- **`/etc/os-release`**, only when the window's packages are missing, to print
  the right install command.

      grep -n "os-release" memstara/window.py

Nothing else: not the chain database, not `monerod`'s config or command line,
not `/proc` or `/sys`.

## What it writes

Only its own files:

| File | What |
|---|---|
| `~/.config/memstara/settings.json` | your settings, readable only by you (a file it can't read is kept as `settings.json.bad`) |
| `~/.local/state/memstara/history.json` | the last 24 hours of numbers, for the charts |
| `~/.local/state/memstara/window.json` | the window's size |
| `~/.local/state/memstara/window-data/` | the window's own storage (which screen was open, the mempool chart's unit) |
| `~/.cache/memstara/window-cache/` | the window's cache |
| `~/.local/share/applications/org.memstara.Memstara.desktop` | the entry in your apps, only after `--install` |
| `~/.config/autostart/org.memstara.Memstara.desktop` | only while "start when I log in" is on |

These are the usual places. When `$XDG_CONFIG_HOME`, `$XDG_STATE_HOME`,
`$XDG_CACHE_HOME` or `$XDG_DATA_HOME` is set, it takes the place of
`~/.config`, `~/.local/state`, `~/.cache` or `~/.local/share`.

`--config PATH` keeps the settings at PATH instead. PATH must not exist yet or
look like a Memstara settings file (JSON with its `"format"` number, a
`"source"` section and only sections beside them): anything else stops the
app at once and is left as it is. The folder it goes in, when Memstara has to
make it, is readable only by you (any folders above that it makes get your
usual permissions); a folder that's already there is never changed.

It never writes to `monerod`'s files or anyone else's.

    grep -rnE "open\([^)]*['\"][wax]|atomic_write\(|ensure_private_dir\(|_write\(|_remove\(|os\.(replace|chmod)|\.unlink\(" memstara/ --include='*.py'
    grep -n "not_settings_file" memstara/__main__.py

## What it never does

**Run another program.** None of Memstara's files starts a program, a shell
or a browser, or stops one. The one exception is WebKit's own drawing process
for its window, which Memstara ends when that window closes for good or the
app quits (`_stop_drawing` in `window.py`; the second command shows the one
call). The first prints nothing:

    grep -rniE "subprocess|multiprocessing|ctypes|os\.system|popen|os\.exec|spawn|fork\(|os\.kill|webbrowser|show_uri|launcher|appinfo" memstara/ --include='*.py'
    grep -rn "terminate_web_process" memstara/ --include='*.py'

**Use the network beyond your node and its own window.** Only `rpc.py` (your
node) and `server.py` (its own window, on `127.0.0.1`) use the network; the
window's web view is what talks to `server.py`. `privacy.py`
imports `socket` too, only to ask this computer its name
(`socket.gethostname()`), so this lists it as well:

    grep -rlnE "^\s*(import|from)\s+(socket|ssl|http\b|urllib\.(request|error))|from urllib import (request|error)" memstara/ --include='*.py'

**Answer anything but its own window.** The server listens on `127.0.0.1`
only. Its page and every `/api/` request need a key that is new at every start
and that only its window gets, so a browser or another program on the
computer gets neither the dashboard nor any reading, setting or log line. Only
the screens' own scripts, styles, fonts and pictures from `web/` are served
without the key; there's nothing private in them.

    grep -n '"127.0.0.1"' memstara/app.py
    grep -n "token_urlsafe" memstara/window.py
    grep -n "def _needs_no_key" -A5 memstara/server.py

Like any GTK app, Memstara also puts its name, `org.memstara.Memstara`, on
your session bus (D-Bus). That's how a second start finds the copy that's
running. Through it, a program running as you can do what the menu does: show
or reload the window, quit, and turn start at login on or off (which writes or
deletes the autostart file above). The only thing a program can read through
it is whether start at login is on, which the autostart file shows anyway. It
doesn't reach the node. The second command, run while Memstara is open, lists
what the bus offers:

    grep -n "Gio.SimpleAction" memstara/window.py
    gdbus call --session --dest org.memstara.Memstara --object-path /org/memstara/Memstara --method org.gtk.Actions.DescribeAll

**Load anything from the internet.** The screens come only from `web/`, fonts
included. The window is told to load and connect to its own server only, and
it refuses any other address, pop-up windows and downloads.

    grep -n "^CSP" -A1 memstara/server.py
    grep -rnE "https?://" web/ --include='*.js' --include='*.html' --include='*.css' --include='*.svg' | grep -v "w3.org/2000/svg"
    grep -rn "fetch(" web/

The second command prints nothing, and the third only `web/js/api.js`.

**Change Monero's code.** Memstara doesn't include, patch or start `monerod`.

## What it can't promise

- **A restricted RPC port is a second lock, kept by `monerod`.** Point
  Memstara at one (`--restricted-rpc`, or the port from
  `--rpc-restricted-bind-port`) and `monerod` itself refuses its admin
  commands there, so you don't have to trust Memstara's list for those. It
  still takes what wallets and miners send to any node, such as a transaction
  or a mined block (and, on a node that sells paid RPC access, payments for
  it). Memstara can't send those either. A restricted port also hides some
  readings, such as peers, bans, traffic and disk space (the README lists
  them).
- **Plain `http` across a network can be read and changed** by anyone in
  between. They see your user name and every answer, and can try to guess a
  weak password from the login. On an unrestricted port they can also turn one
  of Memstara's reads into a command, because `monerod`'s login checks who
  sent a request, not what it asks for. For a node on another computer, use an
  SSH tunnel (see the README).
- **These checks cover Memstara's own code.** Python, GTK and WebKit come from
  your system's packages and do things of their own: WebKit draws the page in
  separate helper processes, which `ps --forest` shows under Memstara
  (`WebKitWebProcess` and `WebKitNetworkProcess`, plus WebKit's sandbox
  launcher `bwrap` and usually `xdg-dbus-proxy`, which WebKit starts itself),
  the libraries read system files such as
  ones in `/proc` and `/sys`, and Python keeps compiled copies of Memstara's
  code in `memstara/__pycache__/`.

Found something that doesn't hold? Please report it privately, as
[SECURITY.md](SECURITY.md) says.
