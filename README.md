# Memstara

A dashboard for a Monero node, as a desktop app in its own window. It runs on
the same computer as `monerod`, or on another one that reaches the node over
the network. It shows the node's health, its connections, the
chain, the mempool, traffic and history, with a lamp wall that lights up when
something needs a look, a settings menu for every limit and display choice,
and a command drawer with every `monerod` console command.

It sits on top of `monerod` and never changes Monero's code. Changes to the
node only go through the node's own RPC port, the same way `monerod`'s own
console commands do (each one is a front end for an RPC request, see
`src/daemon/rpc_command_executor.cpp` in Monero's source). So the dashboard
can change exactly what the console can, nothing more, and Monero updates
don't need dashboard updates. Reads go a little further: it also looks at
`monerod`'s log file and at the computer itself, read-only, for things RPC
doesn't report.

![the app window on the NETWORK screen: a 3D tree of the node's connections with live chatter beside it](docs/screens.png)

(The screenshot shows made-up test data, not a real node's peers.)

## What's on the screen

Eight screens, one per tab across the top (or all on one page, in Settings →
LOOK). Each opens with an animated picture drawn from the node's real
readings, then the panels for that part of the node. The ground is pure black
(pixels off on an OLED screen), with a slow breathing glow; a screen with a
problem pulses in the problem colour and its tab shows ■ (▲ for a heads-up).

| Screen | The picture at the top | Panels |
|---|---|---|
| STATUS | A ring with one segment per thing being watched, lit when something needs a look, with the height, time since the last block and quick readouts in the middle. Click a segment to open the screen that explains it. | A · Lamp wall, and add-on panels |
| NETWORK | A 3D tree of every connection you can turn and zoom: outgoing, incoming and Tor, with peers on one company's servers grouped. The moving dots are the data each peer really sent and received. A live chatter log beside it. | C · Peer scope, D · Route |
| CHAIN | The last blocks as boxes receding into the distance, sized by weight and spaced by the wait before each, with the next block's timer. | B · Chain, K · System |
| MEMPOOL | One dot per waiting transaction, in bands by how long it has waited. | H · Mempool |
| TRAFFIC | A scope of download and upload over the last 15 minutes. | F · Traffic, G · History |
| COMPUTER | Dials for CPU temperature (°C or °F), monerod's CPU, memory, how busy the chain's drive is, and disk free. | J · Computer |
| CONTROLS | A clock of the short bans, each ring draining as its time runs out. | E · Controls |
| LOG | Lanes of what happened in the last hour, with the newest typed out underneath. | I · Log |

| Panel | Shows |
|---|---|
| A · Lamp wall | One lamp per thing being watched. Grey is fine, a lit ▲ is a heads-up, a lit ■ is a problem. A new lamp flashes until you click ACK. TEST lights them all for 2 seconds. |
| B · Chain | Height, how long since the last block (and how unusual that wait is), the last 60 blocks drawn as a timeline, average gap, blocks replaced, network hashrate. |
| C · Peer scope | Every connection as a dot. Peers from the same company's servers stack at the same angle, so a spy cluster shows up as a pile-up. Distance from the middle is how long it's been connected. |
| D · Route | How the node is connected: other nodes, the network link it's bound to (a VPN, if you use one), your node, Tor and the dashboard. |
| E · Controls | Ban or unban a range in two clicks, with the ranges at the eclipse limits listed. Every action asks you to type a word first. |
| F · Traffic | Download and upload now, and the last 15 minutes. |
| G · History | Connections, mempool and traffic over 15 min, 1 h, 6 h or 24 h, kept for 24 h. |
| H · Mempool | Transactions waiting, how long they've waited, double spends. |
| I · Log | Things that changed, newest first, including what `monerod`'s own log reports (reorgs, bans, errors). |
| J · Computer | The `monerod` process (CPU, memory, disk, the flags it runs with, "restart to use the new version"), CPU temperature, power and battery, memory, the drive holding the chain, the network link and the Tor proxy. |
| K · System | Monero version, uptime, network rules version, bans, fees, disk space. |

The pictures only move while their screen is open and in view, and Settings →
LOOK → Motion (or your desktop's reduce-animation setting) stills
them; they still update with every reading.

**Privacy mode.** The PRIVACY switch at the top hides addresses, ports, names,
folders and transaction ids behind a glitch, and shows every clock in UTC, so the
screens can be shown to other people or put in a screenshot. The hiding happens in the
app's server, so the real values never reach the window. Changes to the node are off
while it's on, and turning it off asks you to type SHOW.

Every chart has a table view or a hover readout with the exact numbers.
Add-ons can add panels, lamps and boxes in the route diagram (see
[Add-ons](#add-ons)).

## Run it

It needs Linux with a desktop (Wayland or X11), Python 3.10 or newer, and
two things for the window: PyGObject (GTK 4) and WebKitGTK 6.0, the web engine
GNOME's own apps draw with. The screens are drawn inside the app's own window:
there's no browser, address bar or tabs, and the window can't go anywhere but
its own screens.

Run it on the computer where `monerod` runs, or on another one that reaches
the node over the network (see
[A node on another computer](#a-node-on-another-computer-or-a-raspberry-pi)).

So far it has been tested on Ubuntu with GNOME. Other distributions and
desktops should work, but nobody has checked yet: reports are welcome.

**One time: get the window's packages.** In a terminal on that computer:

| System | Command |
|---|---|
| Debian, Ubuntu, Linux Mint | `sudo apt install -y git python3-gi gir1.2-webkit-6.0` |
| Fedora | `sudo dnf install -y git python3-gobject webkitgtk6.0` |
| Arch, Manjaro, EndeavourOS | `sudo pacman -S --needed git python-gobject webkitgtk-6.0` |

**One time: get the code and add it to your apps.**

```bash
git clone https://github.com/variablenine/Memstara ~/memstara
cd ~/memstara && python3 -m memstara --install
```

**Start it.** With the node running, open **Memstara** from your desktop's
app list (you can pin it to a dock or panel there). Or from a terminal:

```bash
cd ~/memstara && python3 -m memstara
```

Closing the window stops it. Stopping it never affects the node. Opening it
again while it runs just brings its window back.

| If the window | Do this |
|---|---|
| doesn't open, and the terminal says it needs PyGObject and WebKitGTK 6.0 | run the install command it prints (the one for your system above), then start it again |
| opens but stays blank or flickers | quit it (☰ → Quit), then start it with `python3 -m memstara --safe-graphics`, which draws without the graphics card's help |
| stays blank on Ubuntu 24.04, and the terminal says `bwrap: setting up uid map: Permission denied` | see [Ubuntu 24.04](#ubuntu-2404) below |
| says the app's window port is in use | another copy has port 18200: stop it with Ctrl+C in its terminal, or start with `--port 18201` |

On the first start it finds your node by itself: it reads the running
`monerod`'s own flags and config file for its unrestricted RPC port and its log
file. If it finds more than one `monerod`, or none, open Settings → Data source
→ FIND MY NODE.

What to expect:

- The badge at the top says **LIVE** and your node's RPC address.
- Height and the outgoing and incoming counts match `monerod status`.

If something's off:

| What the screen says | What it means | What to do |
|---|---|---|
| THE NODE ISN'T ANSWERING | Nothing answered at the RPC address in Settings. | Check the node is running. It takes a minute or two to answer after a restart. Settings → Data source → FIND MY NODE shows where it listens. |
| THIS IS A RESTRICTED RPC PORT (badge shows LIMITED) | The address points at a restricted port, which hides peers, bans and disk space. | Settings → Data source → FIND MY NODE, then USE THIS on the unrestricted one, then SAVE. |
| NEEDS A LOGIN (in FIND MY NODE) | `monerod` runs with `--rpc-login`. | Type the user and password in Settings → Data source. |

**Get updates later:**

```bash
cd ~/memstara && git pull
```

then ☰ (top right of the window) → **Quit**, and open the app again.

To start it automatically when you log in (in the background, so the history
keeps filling in), use ☰ → **Start when I log in** in the window, or see
[docs/START-AT-LOGIN.md](docs/START-AT-LOGIN.md). Started that way, closing
the window only hides it, and ☰ → **Quit** stops it.

`python3 -m memstara --uninstall` takes it out of your apps and out of start at
login again; your settings and history stay.

### Ubuntu 24.04

WebKitGTK always draws inside a sandbox (bubblewrap). Ubuntu 24.04 limits the
kind of sandbox it uses unless an AppArmor profile allows it, and that
profile ships with 24.04 but isn't switched on. Without it the window stays
blank. The fix reported for other apps hitting the same error is to switch on
that profile (untested with Memstara so far):

```bash
sudo apt install -y apparmor-profiles
sudo install -m 0644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
```

Memstara never turns WebKitGTK's sandbox off, and you shouldn't either.

### A node on another computer (or a Raspberry Pi)

Memstara can read a node running somewhere else: a Raspberry Pi, a home
server, another PC. It talks to the node only through RPC, so everything RPC
answers works the same. What it can't see is that other computer itself (its
CPU temperature, memory, drive and the `monerod` process), so the COMPUTER
screen says NODE ON ANOTHER COMPUTER instead of showing dials, and the log
views stay empty unless you point Settings → Node's log at a copy.

The node's unrestricted RPC port can change the node (ban peers, stop it), so
don't open it to your network. Reach it through an SSH tunnel instead, which
keeps it private and encrypted. On the computer running Memstara:

```bash
ssh -N -L 18081:127.0.0.1:18081 you@node-computer
```

Leave that running (or use `autossh` to keep it up), and set Settings → Data
source → Node RPC address to `http://127.0.0.1:18081`. Memstara notices the
port is an SSH tunnel and treats the node as remote. If a node already runs on
this computer on 18081, pick another local port, for example
`-L 18091:127.0.0.1:18081` and `http://127.0.0.1:18091`.

If you do bind the node's RPC to your local network instead
(`--rpc-bind-ip <its LAN address> --confirm-external-bind`), also start it with
`--rpc-login name:password` and put the same in Settings → Data source. Plain
`http` on a network can still be read by anyone on it, and Memstara says so
once in the log.

A Raspberry Pi with a desktop can also run Memstara itself (Raspberry Pi OS is
Debian-based, so the Debian packages above apply, and its CPU temperature
sensor is read), but that hasn't been tested yet.

## Commands

**COMMANDS** at the top opens every `monerod` console command, grouped and
searchable: `status`, `print_cn`, `print_pl`, `sync_info`, `print_pool`,
`print_block`, `print_tx`, `bans`, `ban`, `unban`, `out_peers`, `limit`,
`flush_txpool`, `set_log`, `pop_blocks`, `prune_blockchain` and the rest. Each
one has a form for its arguments and draws the node's answer as tables (the
raw answer is one click away). It also shows the last lines of `monerod`'s
log.

Commands that change something ask you to type a word first (BAN, UNBAN,
PEERS, FLUSH and so on), so a stray key press can't set one off. A few have
a safety limit the console doesn't:

- Bans only take public addresses, ranges no wider than `a.b.x.x`, and never a
  range holding your node's own address. `monerod` keeps bans in memory, so a
  ban lasts until it runs out or the node restarts.
- `print_bc` and `pop_blocks` take at most 1,000 blocks at a time.

Panel E has the two you'll use most, ban and unban, with the ranges at the
eclipse limits ready to ban in two clicks. Settings → Controls can turn all
change commands off; reading always works.

## Reads beyond RPC

Some things about the node aren't in any RPC answer, so the dashboard reads
them from the computer itself, read-only (Settings → Computer reads turns this
off):

- `monerod`'s log file: reorgs, blocks that lost a race, bans as they happen,
  errors and warnings.
- The `monerod` process (`/proc`): CPU, memory, disk reads and writes, the
  flags it was started with (passwords hidden), and whether its program file
  was replaced since it started (an update waiting for a restart).
- Each peer's round trip and resent packets (the `ss` tool). Tor and I2P peers
  are left out, since their delay is Tor's, not theirs.
- The computer (`/sys`, `/proc`): CPU temperature, power and battery, memory,
  how busy the drive holding the chain is, the network link the node's
  peer-to-peer port is bound to, and whether the Tor proxy is listening.

Not read: the chain database itself (RPC already serves what's in it, and its
layout changes between Monero versions), the ZMQ feed (with the node read every second,
it would only show new blocks a moment sooner) and `p2pstate.bin` (the same peer lists
`print_pl` shows).

## Add-ons

Anything made for one particular setup lives in an add-on, a folder in
`addons/<name>/`, never in the core. None ship with Memstara; git ignores
everything in `addons/` except its guide, so an add-on you make for your own
setup never ends up in a commit by accident. Each add-on's folder has a
`README.md` saying what it reads and how to set it up. The ones loaded show in the status line at the bottom of the
screen (ADD-ONS) and each has its own section in Settings, where it can be
turned off. To remove one, delete its folder and restart the dashboard;
nothing else changes. Start the dashboard with `--no-addons` to load none.

How to write one: [addons/README.md](addons/README.md), and
[docs/DESIGN.md](docs/DESIGN.md), "Core and add-ons".

## Safety

- Only its own window can use it. The window gets its readings from the
  app's own server on `127.0.0.1`, and that server answers only requests
  carrying a key that is new at every start and that only the window gets. A
  browser, or any other program or user on the computer, is turned away.
- It only sends the RPC requests `monerod`'s own console commands send, plus
  two read-only ones for the panels. Anything else is refused before a
  request is made.
- Every change also needs a header only its own screens send, and requests
  from web pages are refused.
- The window makes no requests to the internet and can't go to any other
  address. Fonts are bundled. Camera, microphone, location, downloads, pop-up
  windows and WebGL are all off.
- It never writes to `monerod`'s files. Settings live in
  `~/.config/memstara/settings.json` (only your user can read it),
  history in `~/.local/state/memstara/`.
- Keyboard shortcuts are off by default, so stray key presses do nothing, and
  the window adds none of its own.

## For developers

- `docs/DESIGN.md`: the data contract between the server and the screens,
  the RPC calls and why, alert rules, local reads, add-ons, controls and
  settings.
- `docs/STYLE.md`: look and wording rules.
- `memstara/`: the server (Python standard library only) and the app window
  (`window.py`, GTK 4 and WebKitGTK through PyGObject; `desktop.py`, the app
  grid and start-at-login files). `web/`: the screens (plain HTML, CSS and
  JavaScript modules, no build step). `addons/`: add-ons.
- Tests: `python3 -m unittest discover -s tests -v` (Python 3.10 or newer,
  nothing to install). They run against
  `tests/fake_monerod.py`, a test-only stand-in that answers with recorded
  `monerod` replies, and against fake `/proc` and `/sys` trees.

## Credits

The look takes cues from the sci-fi interface designs collected at
[uispace.org](https://uispace.org/) (SCENESCAPES by Annas Rahman), in
particular the mood of its "second party" scene. Everything here was drawn and
written from scratch: no code, images, wording or assets from that site are
used, and it has no license that would allow reuse. Fonts are IBM Plex Mono
and IBM Plex Sans Condensed under the SIL Open Font License 1.1 (see
`web/fonts/`).

## License

Memstara is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It comes with no warranty. The full text is in [LICENSE](LICENSE).

Security problems: please report them privately, as [SECURITY.md](SECURITY.md)
says, not in a public issue.
