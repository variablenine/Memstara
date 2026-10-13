"""Local reads on the node's own computer (read-only, Linux only): finding the running
monerod and its settings, its process, the computer itself, the network interface the
node is bound to, Tor's SOCKS port and peers' round trips.

Everything comes from /proc and /sys, plus two iproute2 commands run with a fixed
argument list, no shell and a 3 s timeout: `ss -tinH state established` (round trips)
and `ip -j addr show` (only when the node has --p2p-bind-ip). Nothing is written
anywhere and no process is signalled or traced. A source that is missing gives null
and a short plain reason; nothing here raises.

    reader = HostReader(rpc_url, data_dir="~/.bitmonero", root="/", every_s=10)
    reader.read(now, clock=None) -> dict
        snapshot["host"], shaped as in docs/DESIGN.md "Local reads":
        {"ok", "error", "every_s", "t", "proc", "proc_error", "computer", "bind_if", "tor", "remote"}
        ("t": `now`, so a reading shown again is known for the same one).
        When the node runs on another computer (rpc_url's address isn't one of this
        computer's, or what holds the port here is a tunnel such as ssh), nothing about
        this computer is read: "ok" is false and "remote" says where the node is
        ({"how": "network" | "tunnel", "where": "host:port", "via": "ssh" or null}).
        On another OS, or without /proc: the same keys, "ok": false, the rest null.
        Stateful: CPU %, I/O, disk and interface rates are differences with the
        previous read (null on the first), each timed by `clock()` (seconds) when its
        counter is read, or by `now` without one; the node's pid is remembered and found
        again when it stops existing or stops holding the RPC socket.
    reader.annotate(peer_list) -> peer_list
        Sets "rtt_ms" and "retrans" on each snapshot peers.list entry (in place) from
        the last read's `ss` table; null for Tor/I2P peers and peers ss doesn't show.
    reader.rpc_url, reader.data_dir and reader.every_s may be changed between reads.

    discover(root="/", home=None) -> {"ok", "error", "found": [...]}
        Every monerod of the user's own uid, with the settings it runs with, read the
        way monerod reads them (command line, then its config file). See discover().
    match_node(found, pid, rpc_url) -> the one of those the dashboard reads, or None.

Pure helpers, used by the reader and the tests: parse_proc_net(text, byteorder),
parse_ss(text), annotate_peers(peer_list, rtts), mask_flags(argv), parse_args(argv),
parse_config(text), node_settings(argv, ...).

`root` prefixes every /proc and /sys path so tests can point the reader at a fake
tree; `run` replaces the subprocess call (tool name, args -> output text, or raises
CommandError with a plain reason).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
from urllib.parse import urlsplit

CMD_TIMEOUT_S = 3.0
SS_ARGS = ("-tinH", "state", "established")
IP_ARGS = ("-j", "addr", "show")
# the iproute2 tools are looked for only here (never through $PATH)
TOOL_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
MAX_FILE = 4 * 1024 * 1024        # cap for any one /proc or /sys read
MAX_OUTPUT = 16 * 1024 * 1024     # cap for a command's output
MASK = "••••"
TOR_SOCKS_PORT = 9050
TCP_ESTABLISHED = 0x01
TCP_LISTEN = 0x0A
WILDCARD = ("0.0.0.0", "::")

# how long a looked-up rpc_url host name is kept before it's looked up again
RESOLVE_S = 300

# programs that hold an RPC port here for a node somewhere else (an SSH tunnel, a relay)
TUNNELS = frozenset({"ssh", "autossh", "sshuttle", "socat", "stunnel", "stunnel4"})

# thermal zone types and hwmon chip names that are the CPU (Intel, AMD, ARM boards). Any zone
# whose type starts with one of ZONE_PREFIXES counts too: "cpu-thermal" on a Raspberry Pi and
# most other ARM boards, "package-thermal" and "bigcore0-thermal" on RK3588 boards ("soc-thermal"
# with Rockchip's own kernels)
ZONE_TYPES = ("x86_pkg_temp", "acpitz")
ZONE_PREFIXES = ("cpu", "soc", "package", "bigcore", "littlecore")
HWMON_NAMES = ("coretemp", "k10temp", "zenpower")
THROTTLE_FILES = ("core_throttle_count", "core_throttle_total_time_ms",
                  "package_throttle_count", "package_throttle_total_time_ms")


class CommandError(Exception):
    pass


def _run_tool(name: str, args) -> str:
    """Run one iproute2 tool with a fixed argument list. Raises CommandError."""
    exe = next((os.path.join(d, name) for d in TOOL_DIRS if os.access(os.path.join(d, name), os.X_OK)), None)
    if exe is None:
        raise CommandError("%s isn't installed" % name)
    try:
        r = subprocess.run([exe, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=CMD_TIMEOUT_S, shell=False,
                           env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    except subprocess.TimeoutExpired:
        raise CommandError("%s didn't answer in %g s" % (name, CMD_TIMEOUT_S))
    except OSError:
        raise CommandError("%s couldn't be run" % name)
    if r.returncode != 0:
        raise CommandError("%s failed (exit %d)" % (name, r.returncode))
    return r.stdout[:MAX_OUTPUT].decode("utf-8", "replace")


# ---- small readers (all return None instead of raising)

def _read_text(path: str, limit: int = MAX_FILE):
    try:
        with open(path, "rb") as fh:  # read-only, never written
            return fh.read(limit).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def _read_str(path: str):
    t = _read_text(path, 4096)
    return t.strip() if t is not None else None


def _read_int(path: str):
    t = _read_str(path)
    try:
        return int(t) if t is not None else None
    except ValueError:
        return None


def _listdir(path: str) -> list:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _clk_tck() -> int:
    try:
        return os.sysconf("SC_CLK_TCK") or 100
    except (ValueError, OSError, AttributeError):
        return 100


def _proc_stat(root: str, pid: int):
    """/proc/<pid>/stat after the command name (field 3 onwards), or None."""
    text = _read_text(os.path.join(root, "proc", str(pid), "stat"), 4096)
    if not text or ")" not in text:
        return None
    rest = text.rsplit(")", 1)[1].split()
    return rest if len(rest) >= 22 else None


def _proc_started(root: str, start_ticks, clk: int):
    """When a process started (unix seconds): boot time + its starttime in clock ticks."""
    for line in (_read_text(os.path.join(root, "proc", "stat")) or "").splitlines():
        if line.startswith("btime "):
            try:
                return int(int(line.split()[1]) + int(start_ticks) / clk)
            except (ValueError, IndexError):
                return None
    return None


def _proc_exe(root: str, pid: int):
    """(path of the running binary, True when it was replaced on disk since it started)."""
    try:
        exe = os.readlink(os.path.join(root, "proc", str(pid), "exe"))
    except OSError:
        return None, False
    if exe.endswith(" (deleted)"):
        return exe[:-len(" (deleted)")], True
    return exe, False


def _proc_argv(root: str, pid: int) -> list:
    argv = (_read_text(os.path.join(root, "proc", str(pid), "cmdline")) or "").split("\0")
    if argv and argv[-1] == "":
        argv.pop()
    return argv


def _own_pids(root: str, uid) -> list:
    """Pids of the processes owned by `uid` (others' /proc/<pid>/fd can't be read anyway)."""
    out = []
    for name in _listdir(os.path.join(root, "proc")):
        if not name.isdigit():
            continue
        try:
            if os.stat(os.path.join(root, "proc", name)).st_uid == uid:
                out.append(int(name))
        except OSError:
            continue
    return sorted(out)


def _rate(cur, prev, dt):
    """Per-second rate between two counter readings; null when unknown or the counter went back."""
    if cur is None or prev is None or not dt or dt <= 0 or cur < prev:
        return None
    return int(round((cur - prev) / dt))


# ---- addresses

def canon_ip(s: str) -> str:
    """One spelling per address: no brackets or %scope, IPv4-mapped IPv6 as plain IPv4."""
    s = (s or "").strip().strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return s.lower()
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _ip_or_none(s: str):
    try:
        return canon_ip(str(ipaddress.ip_address((s or "").strip("[]"))))
    except ValueError:
        return None


def _hex_ip(h: str, byteorder: str):
    """An address as /proc/net/tcp{,6} prints it: 32-bit words in host byte order."""
    try:
        raw = bytes.fromhex(h)
    except ValueError:
        return None
    if len(raw) not in (4, 16):
        return None
    if byteorder == "little":
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return canon_ip(str(ipaddress.ip_address(raw)))


def parse_proc_net(text: str, byteorder: str = sys.byteorder) -> list:
    """/proc/net/tcp or tcp6 -> [{"local": (ip, port), "remote": (ip, port), "state", "uid", "inode"}]."""
    out = []
    for line in (text or "").splitlines()[1:]:
        f = line.split()
        if len(f) < 10 or ":" not in f[1] or ":" not in f[2]:
            continue
        try:
            la, lp = f[1].split(":")
            ra, rp = f[2].split(":")
            local, remote = _hex_ip(la, byteorder), _hex_ip(ra, byteorder)
            if local is None or remote is None:
                continue
            out.append({"local": (local, int(lp, 16)), "remote": (remote, int(rp, 16)),
                        "state": int(f[3], 16), "uid": int(f[7]), "inode": int(f[9])})
        except ValueError:
            continue
    return out


def _resolve(host: str) -> list:
    return [ai[4][0].split("%", 1)[0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def parse_fib_trie(text: str) -> set:
    """This computer's own IPv4 addresses from /proc/net/fib_trie: each "|-- a.b.c.d" line
    followed by a "/32 host LOCAL" line."""
    out, last = set(), None
    for line in (text or "").splitlines():
        t = line.strip()
        if t.startswith("|--"):
            last = t[3:].strip()
        elif "host LOCAL" in t and last:
            ip = _ip_or_none(last)
            if ip:
                out.add(ip)
    return out


def parse_if_inet6(text: str) -> set:
    """This computer's own IPv6 addresses from /proc/net/if_inet6 (32 hex digits first on
    each line)."""
    out = set()
    for line in (text or "").splitlines():
        f = line.split()
        if f and len(f[0]) == 32:
            try:
                out.add(canon_ip(str(ipaddress.ip_address(bytes.fromhex(f[0])))))
            except ValueError:
                continue
    return out


def tor_socks_port(flags) -> int:
    """The Tor proxy port the node sends through: from its --tx-proxy tor,<ip:port>[,...]
    flag, else Tor's default (TOR_SOCKS_PORT)."""
    flags = flags if isinstance(flags, list) else []
    for i, fl in enumerate(flags):
        if not isinstance(fl, str):
            continue
        name, eq, val = fl.partition("=")
        if name != "--tx-proxy":
            continue
        v = val if eq else (flags[i + 1] if i + 1 < len(flags) and isinstance(flags[i + 1], str) else "")
        parts = v.strip().split(",")
        if len(parts) >= 2 and parts[0] == "tor":
            port = parts[1].rpartition(":")[2]
            if port.isdigit() and 0 < int(port) < 65536:
                return int(port)
    return TOR_SOCKS_PORT


def _split_hostport(s: str):
    """ss's "1.2.3.4:18080", "[2001:db8::1]:18080" or "192.0.2.1%eth0:18080" -> (ip, port)."""
    host, sep, port = (s or "").rpartition(":")
    if not sep or not port.isdigit():
        return None
    return canon_ip(host), int(port)


def parse_ss(text: str) -> list:
    """`ss -tinH state established` -> [{"local", "peer", "rtt_ms", "retrans"}].

    With one state selected ss leaves out the State column, so a connection line is
    Recv-Q Send-Q Local:Port Peer:Port [Process]; the -i details follow on an
    indented line (rtt:<srtt>/<rttvar> in ms; retrans:<now>/<total>, only when not 0)."""
    rows = []
    cur = None
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if line[0] in " \t":
            if cur is None:
                continue
            if cur["retrans"] is None:
                cur["retrans"] = 0
            for tok in line.split():
                if tok.startswith("rtt:"):
                    try:
                        cur["rtt_ms"] = round(float(tok[4:].split("/", 1)[0]), 1)
                    except ValueError:
                        pass
                elif tok.startswith("retrans:"):
                    try:
                        cur["retrans"] = int(tok[8:].split("/")[-1])
                    except ValueError:
                        pass
            continue
        f = line.split()
        if f and not f[0].isdigit():
            f = f[1:]  # an ss that prints the State column anyway
        cur = None
        if len(f) < 4:
            continue
        local, peer = _split_hostport(f[2]), _split_hostport(f[3])
        if local and peer:
            cur = {"local": local, "peer": peer, "rtt_ms": None, "retrans": None}
            rows.append(cur)
    return rows


def annotate_peers(peer_list: list, rtts) -> list:
    """Set "rtt_ms" and "retrans" on snapshot peers (in place). rtts: {(ip, port): {"rtt_ms", "retrans"}}
    or None when unknown. Tor and I2P peers go through the local proxy, so theirs stay null."""
    for p in peer_list or []:
        if not isinstance(p, dict):
            continue
        r = None
        if rtts and p.get("kind") in ("ipv4", "ipv6") and p.get("ip") and p.get("port"):
            try:
                r = rtts.get((canon_ip(p["ip"]), int(p["port"])))
            except (TypeError, ValueError):
                r = None
        p["rtt_ms"] = r["rtt_ms"] if r else None
        p["retrans"] = r["retrans"] if r else None
    return peer_list


# ---- command line

# options whose value is secret: --rpc-login and --bootstrap-daemon-login take user:password
SECRET_OPTIONS = ("rpc-login", "bootstrap-daemon-login", "rpc-access-control-origins")
# boost (monerod's option parser) also takes the start of an option's name when it fits only
# one option (allow_guessing), so --rpc-logi is --rpc-login; starts this long or longer count
MIN_GUESS = 5
# a login inside a value: --proxy / --tx-proxy take socks5://user:password@ip:port
_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@")


def _is_secret_flag(name: str) -> bool:
    n = name.lower().lstrip("-")
    return ("password" in n or "login" in n
            or any(o == n or (len(n) >= MIN_GUESS and o.startswith(n)) for o in SECRET_OPTIONS))


def mask_flags(argv) -> list:
    """monerod's arguments with secret values replaced by MASK (both `--flag value` and `--flag=value`),
    and any user:password@ inside a scheme://... value too."""
    out = []
    mask_next = False
    for a in argv:
        if mask_next:
            out.append(MASK)
            mask_next = False
            continue
        if a.startswith("-"):
            name, eq, _ = a.partition("=")
            if _is_secret_flag(name):
                if eq:
                    out.append(name + "=" + MASK)
                else:
                    out.append(a)
                    mask_next = True
                continue
        out.append(_USERINFO.sub(lambda m: m.group(1) + MASK + "@", a))
    return out


LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _rpc_target(url: str):
    parts = urlsplit(url or "")
    try:
        port = parts.port
    except ValueError:
        port = None
    return (parts.hostname or "").lower(), port or (443 if parts.scheme == "https" else 80)


def _hostport_text(host: str, port: int) -> str:
    return "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)


# ---- finding the node: the running monerod's own settings

# monerod options read here that take a value; any other option is a switch or not needed
# here (a bare word after a switch is a console command, monerod's positional argument)
VALUE_OPTIONS = {
    "config-file", "data-dir", "log-file", "rpc-bind-ip", "rpc-bind-port", "rpc-bind-ipv6-address",
    "rpc-restricted-bind-ip", "rpc-restricted-bind-port", "rpc-restricted-bind-ipv6-address",
    "rpc-login", "rpc-ssl", "p2p-bind-ip", "p2p-bind-ipv6-address", "p2p-bind-port", "tx-proxy",
    "out-peers", "in-peers",
}
MULTI_OPTIONS = {"tx-proxy"}       # may be given more than once
DEFAULT_RPC_PORTS = {"mainnet": 18081, "testnet": 28081, "stagenet": 38081, "regtest": 18081}
# monerod adds this folder to the data dir (any data dir) for each network
NET_SUBDIR = {"testnet": "testnet", "stagenet": "stagenet", "regtest": "fake"}
MAX_CONFIG = 256 * 1024


def parse_args(argv) -> dict:
    """monerod's arguments (after argv[0]) -> {option: value}. Switches are True;
    `--opt value` and `--opt=value` both work; a repeated option keeps the first (monerod
    refuses repeats) except --tx-proxy, which collects a list."""
    out: dict = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if not a.startswith("--") or a == "--":
            continue
        name, eq, val = a[2:].partition("=")
        if name not in VALUE_OPTIONS and len(name) >= MIN_GUESS and "rpc-login".startswith(name):
            name = "rpc-login"           # boost takes a start of the name that fits one option
        if not eq:
            if name in VALUE_OPTIONS:
                if i >= len(argv):
                    continue
                val = argv[i]
                i += 1
            else:
                val = True
        if name in MULTI_OPTIONS:
            out.setdefault(name, []).append(val)
        elif name not in out:
            out[name] = val
    return out


def parse_config(text: str) -> dict:
    """monerod's config file (boost's format: `key=value` lines, '#' starts a comment)."""
    out: dict = {}
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k in MULTI_OPTIONS:
            out.setdefault(k, []).append(v)
        elif k and k not in out:
            out[k] = v
    return out


def _on(v) -> bool:
    """A switch: present on the command line, or `name=1|true|yes|on` (or empty) in the file."""
    if v is True:
        return True
    return isinstance(v, str) and v.strip().lower() in ("", "1", "true", "yes", "on")


def _url_host(ip: str, default: str) -> str:
    """The address to reach a server bound to `ip` (a wildcard means: through loopback)."""
    ip = (ip or "").strip().strip("[]")
    if ip in ("", "0.0.0.0"):
        ip = "127.0.0.1"
    elif ip == "::":
        ip = "::1"
    if not _ip_or_none(ip):
        ip = default
    return "[%s]" % ip if ":" in ip else ip


def _read_regular(path: str, limit: int):
    """A regular file's text, or None (never blocks on a pipe)."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def node_settings(argv, home: str, cwd: str | None = None, read_file=None, env_login: bool = False) -> dict:
    """The settings a monerod with these arguments runs with, worked out as monerod does
    (src/daemon/main.cpp, command_line_args.h, rpc/core_rpc_server.cpp, rpc_args.cpp):

    - the config file is --config-file, else <home>/.bitmonero/bitmonero.conf
      (testnet/stagenet: one folder deeper), never under --data-dir; the command line wins
      over the file for each option;
    - the data dir is --data-dir (default <home>/.bitmonero), plus testnet/, stagenet/ or
      fake/ for the other networks;
    - the log is --log-file, else bitmonero.log in the data dir; a bare file name is in
      the data dir, other relative paths are from monerod's working folder;
    - the RPC port is --rpc-bind-port, default 18081/28081/38081 by network; it is
      restricted only with --restricted-rpc; --rpc-restricted-bind-port adds a second,
      always restricted, port on --rpc-restricted-bind-ip (default 127.0.0.1);
    - a login is needed with --rpc-login (or a start of that name boost takes, like
      --rpc-logi), or RPC_LOGIN in monerod's environment (env_login). The login's value is
      never copied anywhere;
    - tx_proxy: any --tx-proxy (Tor or I2P sending); tx_proxy_tor: one for Tor (tor,...);
    - out_peers is --out-peers when given as 0 or more, else None (monerod then uses its
      default, P2P_DEFAULT_CONNECTIONS_COUNT = 12 in cryptonote_config.h).
    """
    home = home or os.path.expanduser("~")
    base = os.path.join(home, ".bitmonero")
    cmd = parse_args(argv)

    def absolute(p):
        return p if os.path.isabs(p) or not cwd else os.path.normpath(os.path.join(cwd, p))

    # the config file's place depends on the network flags given on the command line only
    cmd_net = "testnet" if _on(cmd.get("testnet")) else "stagenet" if _on(cmd.get("stagenet")) else None
    if isinstance(cmd.get("config-file"), str):
        config_file = absolute(cmd["config-file"])
    else:
        config_file = os.path.join(base, cmd_net, "bitmonero.conf") if cmd_net else os.path.join(base, "bitmonero.conf")
    text = read_file(config_file) if read_file else _read_regular(config_file, MAX_CONFIG)
    opts = parse_config(text) if text is not None else {}
    opts.update(cmd)

    nettype = ("testnet" if _on(opts.get("testnet")) else "stagenet" if _on(opts.get("stagenet"))
               else "regtest" if _on(opts.get("regtest")) else "mainnet")
    data_dir = opts["data-dir"] if isinstance(opts.get("data-dir"), str) and opts["data-dir"] else base
    data_dir = absolute(data_dir)
    if nettype in NET_SUBDIR:
        data_dir = os.path.join(data_dir, NET_SUBDIR[nettype])
    log = opts.get("log-file")
    if isinstance(log, str) and log:
        log_file = os.path.join(data_dir, log) if not os.path.dirname(log) else absolute(log)
    else:
        log_file = os.path.join(data_dir, "bitmonero.log")

    scheme = "https" if str(opts.get("rpc-ssl", "")).strip().lower() == "enabled" else "http"
    port = str(opts.get("rpc-bind-port") or DEFAULT_RPC_PORTS[nettype]).strip()
    if _on(opts.get("rpc-use-ipv6")) and _on(opts.get("rpc-ignore-ipv4")):
        host = _url_host(str(opts.get("rpc-bind-ipv6-address") or "::1"), "::1")
    else:
        host = _url_host(str(opts.get("rpc-bind-ip") or "127.0.0.1"), "127.0.0.1")
    rpc_url = "%s://%s:%s" % (scheme, host, port) if port.isdigit() else None
    rport = opts.get("rpc-restricted-bind-port")
    restricted_url = None
    if isinstance(rport, str) and rport.strip().isdigit():
        rhost = _url_host(str(opts.get("rpc-restricted-bind-ip") or "127.0.0.1"), "127.0.0.1")
        restricted_url = "%s://%s:%s" % (scheme, rhost, rport.strip())

    p2p = []
    for key, wild in (("p2p-bind-ip", "0.0.0.0"), ("p2p-bind-ipv6-address", "::")):
        ip = _ip_or_none(str(opts.get(key) or "").strip().strip("[]"))
        if ip and ip != wild and ip not in p2p:
            p2p.append(ip)
    login = opts.get("rpc-login")
    try:
        out_peers = int(str(opts.get("out-peers")).strip())
    except ValueError:
        out_peers = None
    return {
        "nettype": nettype,
        "rpc_url": rpc_url,
        "restricted": _on(opts.get("restricted-rpc")),
        "login_needed": bool(isinstance(login, str) and login) or env_login,
        "restricted_rpc_url": restricted_url,
        "data_dir": data_dir,
        "log_file": log_file,
        "p2p_bind": p2p,
        "tx_proxy": any(isinstance(v, str) and v.strip() for v in opts.get("tx-proxy") or []),
        # Tor sending only: --tx-proxy tor,<socks ip:port>[,...] (an i2p,... one sends over I2P)
        "tx_proxy_tor": any(isinstance(v, str) and v.strip().startswith("tor,") for v in opts.get("tx-proxy") or []),
        "out_peers": out_peers if out_peers is not None and 0 <= out_peers < 2 ** 32 else None,
    }


def _proc_cwd(root: str, pid: int):
    try:
        return os.readlink(os.path.join(root, "proc", str(pid), "cwd"))
    except OSError:
        return None


def _env_set(root: str, pid: int, key: str) -> bool:
    """Whether a process's environment has `key` set to something (the value isn't kept)."""
    data = _read_text(os.path.join(root, "proc", str(pid), "environ"), 256 * 1024) or ""
    return any(e.startswith(key + "=") and len(e) > len(key) + 1 for e in data.split("\0"))


def discover(root: str = "/", home: str | None = None, uid=None) -> dict:
    """Every monerod running under the user's own uid, with its settings.

    -> {"ok", "error", "found": [{"pid", "exe", "started", "nettype", "rpc_url", "restricted",
        "login_needed", "restricted_rpc_url", "data_dir", "log_file", "p2p_bind": [ip],
        "tx_proxy": bool, "tx_proxy_tor": bool, "out_peers": int|None}]}, oldest pid first.
    "ok" is false (with a plain reason) on another OS or without /proc; an empty "found"
    comes with a plain reason in "error".
    Read-only: /proc/<pid>/{comm,cmdline,cwd,exe,stat,environ} of the user's own
    processes and monerod's config file. Only the presence of RPC_LOGIN is taken from the
    environment, never its value."""
    if not sys.platform.startswith("linux"):
        return {"ok": False, "error": "Finding the node only works on Linux", "found": []}
    root = root or "/"
    if _read_text(os.path.join(root, "proc", "stat"), 1) is None:
        return {"ok": False, "error": "Can't read /proc on this computer", "found": []}
    if uid is None:
        uid = os.getuid()
    home = home or os.path.expanduser("~")
    clk = _clk_tck()
    found = []
    for pid in _own_pids(root, uid):
        d = os.path.join(root, "proc", str(pid))
        if _read_str(os.path.join(d, "comm")) != "monerod":
            continue
        argv = _proc_argv(root, pid)
        rest = _proc_stat(root, pid)
        if not argv or rest is None:
            continue   # gone already
        cwd = _proc_cwd(root, pid)
        exe, _ = _proc_exe(root, pid)
        item = {"pid": pid, "exe": exe, "started": _proc_started(root, rest[19], clk)}
        item.update(node_settings(argv[1:], home, cwd, env_login=_env_set(root, pid, "RPC_LOGIN")))
        found.append(item)
    return {"ok": True, "error": None if found else "No monerod is running under your user", "found": found}


def match_node(found: list, pid, rpc_url: str):
    """Which of discover()'s monerods the dashboard reads: the one whose pid local reads saw
    holding the RPC port, else the one whose RPC address is rpc_url's (127.0.0.1, ::1 and
    localhost count as the same). None when none of them is."""
    if pid is not None:
        for f in found or []:
            if f.get("pid") == pid:
                return f
    host, port = _rpc_target(rpc_url)
    for f in found or []:
        h, p = _rpc_target(f.get("rpc_url") or "")
        if p == port and (h == host or (h in LOOPBACK and host in LOOPBACK)):
            return f
    return None


def same_rpc(a: str, b: str) -> bool:
    """Whether two RPC URLs are the same node's: same scheme, host and port (127.0.0.1, ::1
    and localhost count as the same host)."""
    (ha, pa), (hb, pb) = _rpc_target(a), _rpc_target(b)
    return (urlsplit(a or "").scheme.lower() == urlsplit(b or "").scheme.lower() and pa == pb
            and (ha == hb or (ha in LOOPBACK and hb in LOOPBACK)))


class HostReader:
    def __init__(self, rpc_url: str, data_dir: str = "~/.bitmonero", root: str = "/",
                 every_s: int = 10, run=None, home: str | None = None):
        self.rpc_url = rpc_url
        self.data_dir = data_dir
        self.root = root or "/"
        self.home = home              # for monerod's default config file; the user's home if None
        self.every_s = every_s
        self.hostname = socket.gethostname()   # tests may change it
        self.resolve = _resolve                # host name -> its addresses; tests may change it
        self._names = {}                       # host name -> (when looked up, its addresses)
        self._run = run or _run_tool
        self._stat = os.stat          # tests swap these two for fakes
        self.uid = os.getuid() if hasattr(os, "getuid") else None
        self._clk = _clk_tck()
        self._pid = None              # (pid, starttime ticks) of the node
        self._p2p_bind = []           # the node's own addresses (--p2p-bind-ip, flag or config file)
        self._prev_proc = None        # (pid, start, t, cpu ticks, read_bytes, write_bytes)
        self._prev_disk = None        # (device, t, sectors read, sectors written, io_ticks)
        self._prev_if = None          # (name, t, rx, tx)
        self._prev_throttle = None    # {cpu: (counts and ms totals)} from the last read
        self._throttle_t = None       # when a heat slowdown was last seen, None if not yet
        self._disk_key = None         # (data_dir, st_dev) -> device name, cached
        self._disk_name = None
        self._rtts = None             # last ss table {(ip, port): {...}}, None when unknown
        self._clock = None            # times each counter as it is read (see read())
        self._via = None              # the tunnel holding the RPC port (see _node), else None

    def _p(self, *parts) -> str:
        return os.path.join(self.root, *parts)

    @staticmethod
    def empty(error: str | None, every_s=None, t=None) -> dict:
        return {"ok": False, "error": error, "every_s": every_s, "t": t, "proc": None, "proc_error": None,
                "computer": None, "bind_if": None, "tor": None, "remote": None}

    def _elsewhere(self, how: str, where: str, via, t) -> dict:
        """The reading when the node runs on another computer: nothing about this one."""
        self._rtts = None
        self._pid = None
        self._prev_proc = self._prev_disk = self._prev_if = None
        if how == "tunnel":
            text = ("Your node is reached through %s on %s, so it runs on another computer. This "
                    "computer's readings would say nothing about it, so they're left out; run the app on "
                    "the node's own computer to see them." % (via, where))
        else:
            text = ("Your node runs on another computer (%s). This computer's readings would say nothing "
                    "about it, so they're left out; run the app on the node's own computer to see them." % where)
        out = self.empty(text, self.every_s, t)
        out["remote"] = {"how": how, "where": where, "via": via}
        return out

    def _own_addresses(self) -> set:
        return (parse_fib_trie(_read_text(self._p("proc", "net", "fib_trie")))
                | parse_if_inet6(_read_text(self._p("proc", "net", "if_inet6"))))

    def _elsewhere_by_address(self):
        """rpc_url's host when it plainly isn't this computer: an address none of this
        computer's interfaces holds, or a name other than this computer's own. None when it
        is (or may be) this computer."""
        host, _ = _rpc_target(self.rpc_url)
        if not host or host in LOOPBACK:
            return None
        ip = _ip_or_none(host)
        if ip is not None:
            addrs = {ip}
        else:
            names = {n.lower() for n in (self.hostname, self.hostname.split(".", 1)[0]) if n}
            names |= {n + ".local" for n in set(names)}
            if host.lower() in names:
                return None
            addrs = self._resolved(host)
            if not addrs:
                return None          # the name doesn't resolve: can't be told, read as before
        if any(ipaddress.ip_address(a).is_loopback or ipaddress.ip_address(a).is_unspecified for a in addrs):
            return None
        own = self._own_addresses()
        # with neither file readable, it can't be told: read as before
        return None if not own or addrs & own else host

    def _resolved(self, host: str):
        """host's addresses (canonical text), looked up at most every RESOLVE_S seconds; the
        RPC client looks the same name up for every call anyway."""
        now = self._clock()
        hit = self._names.get(host)
        if hit and now - hit[0] < RESOLVE_S:
            return hit[1]
        try:
            addrs = {canon_ip(a) for a in self.resolve(host)}
        except (OSError, UnicodeError, ValueError):
            addrs = set()
        self._names[host] = (now, addrs)
        return addrs

    # ---- the one call the poller makes

    def read(self, now: float, clock=None) -> dict:
        # A rate is the change in a counter over the time between two reads of it. The
        # poller's `now` is when its poll started, and the node's answers before this
        # read take a different time each poll, so timing by `now` makes a rate read
        # every second jump about; the poller's clock times each counter as it is read.
        self._clock = clock or (lambda: now)
        t = round(now, 3)
        if not sys.platform.startswith("linux"):
            self._rtts = None
            return self.empty("Local reads only work on Linux", self.every_s, t)
        if _read_text(self._p("proc", "stat"), 1) is None:
            self._rtts = None
            return self.empty("Can't read /proc on this computer", self.every_s, t)
        host, port = _rpc_target(self.rpc_url)
        elsewhere = self._elsewhere_by_address()
        if elsewhere is not None:
            return self._elsewhere("network", _hostport_text(host, port), None, t)
        notes = []
        out = self.empty(None, self.every_s, t)
        out["ok"] = True
        socks = self._sockets()
        if socks is None:
            notes.append("Can't read the connection table (/proc/net/tcp)")
        node_inodes = None
        self._via = None
        try:
            out["proc"], out["proc_error"], node_inodes = self._node(socks, now)
        except Exception:
            out["proc_error"] = "monerod's process couldn't be read"
        if self._via:
            return self._elsewhere("tunnel", _hostport_text(host or "127.0.0.1", port), self._via, t)
        try:
            self._rtts = self._read_rtts(socks, node_inodes, notes)
        except Exception:
            self._rtts = None
            notes.append("Peer round trips couldn't be read")
        try:
            out["computer"] = self._computer(now)
        except Exception:
            notes.append("The computer's readings couldn't be read")
        try:
            out["bind_if"] = self._bind_if(out["proc"], now, notes)
        except Exception:
            notes.append("The node's network link (its --p2p-bind-ip) couldn't be read")
        if socks is not None:
            tor_port = tor_socks_port((out["proc"] or {}).get("flags"))
            out["tor"] = {"socks_port": tor_port, "socks_listening": any(
                s["state"] == TCP_LISTEN and s["local"][1] == tor_port
                and s["local"][0] in ("127.0.0.1", "0.0.0.0") for s in socks)}
        out["error"] = "; ".join(notes) or None
        return out

    def annotate(self, peer_list: list) -> list:
        return annotate_peers(peer_list, self._rtts)

    # ---- which process is the node

    def _sockets(self):
        """Both TCP tables, or None when neither can be read."""
        rows, seen = [], False
        for name in ("tcp", "tcp6"):
            text = _read_text(self._p("proc", "net", name))
            if text is not None:
                seen = True
                rows += parse_proc_net(text)
        return rows if seen else None

    def _rpc_listeners(self, socks, host: str, port: int) -> list:
        listen = [s for s in socks if s["state"] == TCP_LISTEN and s["local"][1] == port and s["inode"]]
        if host == "localhost":
            want = {"127.0.0.1", "::1"}
        else:
            ip = _ip_or_none(host)
            if ip is None:
                return listen  # a host name: no DNS lookups here, the port alone decides
            want = {ip}
        return [s for s in listen if s["local"][0] in want or s["local"][0] in WILDCARD]

    def _fd_sockets(self, pid: int):
        """(socket inodes, number of open files) of one process, or None when it can't be read."""
        fd_dir = self._p("proc", str(pid), "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            return None
        inodes = set()
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                try:
                    inodes.add(int(target[8:-1]))
                except ValueError:
                    pass
        return inodes, len(fds)

    def _stat_fields(self, pid: int):
        return _proc_stat(self.root, pid)

    def _find_pid(self, inodes: set):
        """Scan the user's own processes for one holding a socket in `inodes`.
        monerod processes are tried first, so the scan is usually one process long."""
        cands = []
        for pid in _own_pids(self.root, self.uid):
            comm = _read_str(self._p("proc", str(pid), "comm"))
            cands.append((0 if comm == "monerod" else 1, pid))
        for _, pid in sorted(cands):
            got = self._fd_sockets(pid)
            if got and got[0] & inodes:
                return pid, got
        return None

    def _node(self, socks, now: float):
        """-> (proc dict or None, proc_error or None, the node's socket inodes or None)."""
        host, port = _rpc_target(self.rpc_url)
        where = _hostport_text(host or "127.0.0.1", port)
        if socks is None:
            return None, "Can't read the connection table to find monerod", None
        listeners = self._rpc_listeners(socks, host, port)
        if not listeners:
            self._pid = None
            return None, "Nothing is listening on %s" % where, None
        inodes = {s["inode"] for s in listeners}
        if all(s["uid"] != self.uid for s in listeners):
            self._pid = None
            return None, "Something listens on %s but it isn't one of your user's processes" % where, None
        found = None
        if self._pid is not None:
            pid, start = self._pid
            rest = self._stat_fields(pid)
            if rest is not None and rest[19] == start:
                got = self._fd_sockets(pid)
                if got and got[0] & inodes:
                    found = pid, got
        if found is None:
            self._pid = None
            found = self._find_pid(inodes)
        if found is None:
            return None, "Something listens on %s but its process wasn't found" % where, None
        pid, (sock_inodes, nfiles) = found
        comm = _read_str(self._p("proc", str(pid), "comm"))
        if comm in TUNNELS:
            self._pid = None
            self._via = comm         # read() turns this into the "another computer" reading
            return None, None, None
        proc = self._proc_info(pid, nfiles, now)
        if proc is None:
            self._pid = None
            return None, "monerod's process ended while it was being read", None
        return proc, None, sock_inodes

    def _proc_info(self, pid: int, nfiles: int, now: float):
        rest = self._stat_fields(pid)
        if rest is None:
            return None
        read_t = self._clock()
        start = rest[19]
        self._pid = (pid, start)
        ticks = int(rest[11]) + int(rest[12])        # utime + stime
        threads = int(rest[17])
        started = _proc_started(self.root, start, self._clk)

        status = {}
        for line in (_read_text(self._p("proc", str(pid), "status")) or "").splitlines():
            k, _, v = line.partition(":")
            status[k] = v.split()
        rss = int(status["VmRSS"][0]) * 1024 if status.get("VmRSS") else None
        if status.get("Threads"):
            threads = int(status["Threads"][0])

        io = {}
        for line in (_read_text(self._p("proc", str(pid), "io")) or "").splitlines():
            k, _, v = line.partition(":")
            if v.strip().isdigit():
                io[k.strip()] = int(v)
        rb, wb = io.get("read_bytes"), io.get("write_bytes")

        exe, replaced = _proc_exe(self.root, pid)
        argv = _proc_argv(self.root, pid)
        try:
            self._p2p_bind = node_settings(argv[1:], self.home, _proc_cwd(self.root, pid))["p2p_bind"]
        except Exception:
            self._p2p_bind = []

        cpu = read_rate = write_rate = None
        prev = self._prev_proc
        if prev and prev[0] == pid and prev[1] == start:
            dt = read_t - prev[2]
            if dt > 0 and ticks >= prev[3]:
                cpu = round((ticks - prev[3]) / self._clk / dt * 100, 1)
            read_rate, write_rate = _rate(rb, prev[4], dt), _rate(wb, prev[5], dt)
        self._prev_proc = (pid, start, read_t, ticks, rb, wb)

        return {
            "pid": pid, "name": _read_str(self._p("proc", str(pid), "comm")),
            "exe": exe, "exe_replaced": replaced, "started": started, "cpu_pct": cpu,
            "rss_bytes": rss, "threads": threads, "open_files": nfiles,
            "read_bytes": rb, "write_bytes": wb, "read_rate": read_rate, "write_rate": write_rate,
            "flags": mask_flags(argv[1:]),
        }

    # ---- peers' round trips

    def _read_rtts(self, socks, node_inodes, notes: list):
        try:
            text = self._run("ss", SS_ARGS)
        except CommandError as e:
            notes.append("%s, so peer round trips aren't shown" % e)
            return None
        rows = parse_ss(text)
        if node_inodes is not None and socks is not None:
            # only the node's own connections (another monerod may talk to the same peer)
            mine = {(s["local"], s["remote"]) for s in socks
                    if s["inode"] in node_inodes and s["state"] == TCP_ESTABLISHED}
            rows = [r for r in rows if (r["local"], r["peer"]) in mine]
        return {r["peer"]: {"rtt_ms": r["rtt_ms"], "retrans": r["retrans"]} for r in rows}

    # ---- the computer itself

    def _computer(self, now: float) -> dict:
        mem = {}
        for line in (_read_text(self._p("proc", "meminfo")) or "").splitlines():
            k, _, v = line.partition(":")
            if v.split() and v.split()[0].isdigit():
                mem[k] = int(v.split()[0]) * 1024
        total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
        load = (_read_str(self._p("proc", "loadavg")) or "").split()
        up = _float(((_read_str(self._p("proc", "uptime")) or "").split() or [None])[0])
        out = {"cpu_temp_c": self._cpu_temp(), "throttle": self._throttle(now)}
        out.update(self._power())
        out.update({
            "load1": _float(load[0]) if load else None,
            "cores": self._cores(),
            "mem_used_bytes": total - avail if total is not None and avail is not None else None,
            "mem_total_bytes": total,
            "uptime_s": int(up) if up is not None else None,
            "disk": self._disk(now),
        })
        return out

    def _cpu_temp(self):
        temps = []
        tz = self._p("sys", "class", "thermal")
        for name in _listdir(tz):
            if not name.startswith("thermal_zone"):
                continue
            typ = (_read_str(os.path.join(tz, name, "type")) or "").lower()
            if typ in ZONE_TYPES or typ.startswith(ZONE_PREFIXES):
                temps.append(_read_int(os.path.join(tz, name, "temp")))
        hw = self._p("sys", "class", "hwmon")
        for name in _listdir(hw):
            if (_read_str(os.path.join(hw, name, "name")) or "").lower() not in HWMON_NAMES:
                continue
            for f in _listdir(os.path.join(hw, name)):
                if f.startswith("temp") and f.endswith("_input"):
                    temps.append(_read_int(os.path.join(hw, name, f)))
        # millidegrees; absent sensors read 0 or nonsense like -273000
        temps = [t / 1000 for t in temps if t is not None and -30000 < t < 150000 and t != 0]
        return round(max(temps), 1) if temps else None

    def _throttle(self, now: float) -> dict:
        """Is the CPU slowing itself down because of heat?

        Intel CPUs report each slowdown in /sys/devices/system/cpu/cpuN/thermal_throttle
        (drivers/thermal/intel/therm_throt.c): *_throttle_count goes up when one starts and
        *_throttle_total_time_ms when one ends. Any change since the last read is a slowdown
        seen now. The kernel's own "Processor" cooling devices (ACPI, any make of CPU) show
        a limit it is applying right now as cur_state > 0. Slowdowns the firmware does on
        its own (common on AMD) show in neither. Speeds are the average scaling_cur_freq
        over the online CPUs against the highest cpuinfo_max_freq."""
        base = self._p("sys", "devices", "system", "cpu")
        counts, cur, top = {}, [], []
        for name in _listdir(base):
            if not (name.startswith("cpu") and name[3:].isdigit()):
                continue
            tt = os.path.join(base, name, "thermal_throttle")
            row = tuple(_read_int(os.path.join(tt, f)) for f in THROTTLE_FILES)
            if any(v is not None for v in row):
                counts[name] = row
            fq = os.path.join(base, name, "cpufreq")
            c, m = _read_int(os.path.join(fq, "scaling_cur_freq")), _read_int(os.path.join(fq, "cpuinfo_max_freq"))
            if c and c > 0:
                cur.append(c)
            if m and m > 0:
                top.append(m)
        tz = self._p("sys", "class", "thermal")
        limits = []
        for name in _listdir(tz):
            if name.startswith("cooling_device") and \
                    (_read_str(os.path.join(tz, name, "type")) or "").lower() == "processor":
                limits.append(_read_int(os.path.join(tz, name, "cur_state")))
        limits = [v for v in limits if v is not None]
        source = "intel" if counts else "kernel" if limits else None
        prev, self._prev_throttle = self._prev_throttle, counts or None
        seen = None
        if source:
            seen = any(v > 0 for v in limits)
            if prev is not None and counts:
                seen = seen or any(prev.get(k) != v for k, v in counts.items())
            if seen:
                self._throttle_t = now
        pkg = [r[2] for r in counts.values() if r[2] is not None]
        core = [r[0] for r in counts.values() if r[0] is not None]
        since = max(pkg) if pkg else max(core) if core else None
        return {"source": source, "now": seen, "last_t": self._throttle_t, "since_boot": since,
                "speed_mhz": int(round(sum(cur) / len(cur) / 1000)) if cur else None,
                "max_mhz": int(round(max(top) / 1000)) if top else None}

    def _power(self) -> dict:
        base = self._p("sys", "class", "power_supply")
        mains, bats = [], []
        for name in _listdir(base):
            d = os.path.join(base, name)
            typ = _read_str(os.path.join(d, "type"))
            if typ == "Mains":
                mains.append(_read_int(os.path.join(d, "online")))
            elif typ == "Battery":
                if (_read_str(os.path.join(d, "scope")) or "").lower() == "device":
                    continue  # a mouse's or headset's battery, not the computer's
                bats.append(d)
        pct = state = None
        if bats:
            states = [_read_str(os.path.join(d, "status")) for d in bats]
            state = next((s for s in ("Discharging", "Charging") if s in states), states[0])
            now_full = []
            for d in bats:
                for kind in ("energy", "charge"):
                    n, f = _read_int(os.path.join(d, kind + "_now")), _read_int(os.path.join(d, kind + "_full"))
                    if n is not None and f:
                        now_full.append((n, f))
                        break
            if len(now_full) == len(bats) and len(bats) > 1:
                pct = int(round(100 * sum(n for n, _ in now_full) / sum(f for _, f in now_full)))
            else:
                caps = [c for c in (_read_int(os.path.join(d, "capacity")) for d in bats) if c is not None]
                pct = int(round(sum(caps) / len(caps))) if caps else None
            if pct is not None:
                pct = max(0, min(100, pct))
        if any(m == 1 for m in mains):
            on_ac = True
        elif any(m == 0 for m in mains):
            on_ac = False
        elif state in ("Charging", "Full", "Not charging"):
            on_ac = True
        elif state == "Discharging":
            on_ac = False
        else:
            on_ac = None
        return {"on_ac": on_ac, "battery_pct": pct, "battery_state": state}

    def _cores(self):
        n = 0
        for part in (_read_str(self._p("sys", "devices", "system", "cpu", "online")) or "").split(","):
            a, _, b = part.partition("-")
            try:
                n += (int(b) - int(a) + 1) if b else (1 if a.strip() else 0)
            except ValueError:
                n = 0
                break
        if n:
            return n
        text = _read_text(self._p("proc", "cpuinfo"))
        if text:
            n = sum(1 for line in text.splitlines() if line.startswith("processor"))
            if n:
                return n
        return os.cpu_count() if self.root == "/" else None

    # ---- the disk under the data folder

    def _block_device(self, major: int, minor: int, path: str):
        """Kernel name of the whole disk under `path` (nvme0n1, sda, mmcblk0), or None."""
        real = None
        if major:
            link = self._p("sys", "dev", "block", "%d:%d" % (major, minor))
            if os.path.exists(link):
                real = os.path.realpath(link)
        if real is None and not major:
            # btrfs and other filesystems with an anonymous device (major 0): use the mount's source
            src = self._mount_source(path)
            if not src:
                return None
            name = os.path.basename(os.path.realpath(self._p(src.lstrip("/"))))
            link = self._p("sys", "class", "block", name)
            if not os.path.exists(link):
                return None
            real = os.path.realpath(link)
        if real is None:
            return None
        for _ in range(8):
            slaves = _listdir(os.path.join(real, "slaves"))
            if os.path.exists(os.path.join(real, "partition")):
                real = os.path.dirname(real)      # a partition: its parent is the disk
            elif len(slaves) == 1:
                real = os.path.realpath(os.path.join(real, "slaves", slaves[0]))   # dm-crypt / LVM
            else:
                break
        return os.path.basename(real) or None

    def _mount_source(self, path: str):
        """Source device (/dev/...) of the mount holding `path`, from /proc/self/mountinfo."""
        best, src = -1, None
        for line in (_read_text(self._p("proc", "self", "mountinfo")) or "").splitlines():
            left, sep, right = line.partition(" - ")
            f, r = left.split(), right.split()
            if not sep or len(f) < 5 or len(r) < 2:
                continue
            mnt = f[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
            inside = path == mnt or path.startswith(mnt.rstrip("/") + "/")
            if inside and len(mnt) > best and r[1].startswith("/dev/"):
                best, src = len(mnt), r[1]
        return src

    def _disk(self, now: float):
        path = os.path.realpath(os.path.expanduser(self.data_dir or "~/.bitmonero"))
        try:
            dev = self._stat(path).st_dev
        except OSError:
            return None
        if self._disk_key != (path, dev):
            self._disk_key = (path, dev)
            self._disk_name = self._block_device(os.major(dev), os.minor(dev), path)
        name = self._disk_name
        if not name:
            return None
        row = None
        for line in (_read_text(self._p("proc", "diskstats")) or "").splitlines():
            f = line.split()
            if len(f) >= 13 and f[2] == name:
                row = f
                break
        if row is None:
            return {"device": name, "read_rate": None, "write_rate": None, "busy_pct": None}
        sr, sw, ticks = int(row[5]), int(row[9]), int(row[12])   # sectors are always 512 bytes here
        read_t = self._clock()
        out = {"device": name, "read_rate": None, "write_rate": None, "busy_pct": None}
        prev = self._prev_disk
        if prev and prev[0] == name:
            dt = read_t - prev[1]
            r, w = _rate(sr, prev[2], dt), _rate(sw, prev[3], dt)
            out["read_rate"] = r * 512 if r is not None else None
            out["write_rate"] = w * 512 if w is not None else None
            if dt > 0 and ticks >= prev[4]:
                out["busy_pct"] = round(min(100.0, (ticks - prev[4]) / (dt * 1000) * 100), 1)
        self._prev_disk = (name, read_t, sr, sw, ticks)
        return out

    # ---- the interface holding --p2p-bind-ip

    def _bind_if(self, proc, now: float, notes: list):
        bind = self._p2p_bind[0] if proc and self._p2p_bind else None
        if not bind:
            self._prev_if = None
            return None
        out = {"name": None, "kind": None, "up": False, "address": bind,
               "rx_bytes": None, "tx_bytes": None, "rx_rate": None, "tx_rate": None}
        try:
            ifaces = json.loads(self._run("ip", IP_ARGS))
        except CommandError as e:
            notes.append("%s, so the node's network link isn't shown" % e)
            return None
        except ValueError:
            notes.append("ip's answer couldn't be understood")
            return None
        for i in ifaces if isinstance(ifaces, list) else []:
            if not isinstance(i, dict):
                continue
            addrs = [canon_ip(str(a.get("local", ""))) for a in i.get("addr_info") or [] if isinstance(a, dict)]
            if bind in addrs:
                flags = i.get("flags") or []
                out["name"] = str(i.get("ifname") or "") or None
                # WireGuard and tun report operstate UNKNOWN; UP + LOWER_UP is what counts
                out["up"] = "UP" in flags and ("LOWER_UP" in flags or i.get("operstate") == "UP")
                break
        if not out["name"]:
            self._prev_if = None   # nothing holds the address: that link is down
            return out
        out["kind"] = self._if_kind(out["name"])
        st = self._p("sys", "class", "net", out["name"], "statistics")
        rx, tx = _read_int(os.path.join(st, "rx_bytes")), _read_int(os.path.join(st, "tx_bytes"))
        read_t = self._clock()
        out["rx_bytes"], out["tx_bytes"] = rx, tx
        prev = self._prev_if
        if prev and prev[0] == out["name"]:
            dt = read_t - prev[1]
            out["rx_rate"], out["tx_rate"] = _rate(rx, prev[2], dt), _rate(tx, prev[3], dt)
        self._prev_if = (out["name"], read_t, rx, tx)
        return out


    def _if_kind(self, name: str) -> str:
        """"vpn" for a tun/tap or WireGuard interface, else "other"."""
        d = self._p("sys", "class", "net", name)
        if os.path.exists(os.path.join(d, "tun_flags")):
            return "vpn"                                  # tun/tap (OpenVPN, most VPN apps)
        uevent = _read_text(os.path.join(d, "uevent"), 4096) or ""
        if "DEVTYPE=wireguard" in uevent.splitlines():
            return "vpn"
        if _read_int(os.path.join(d, "type")) == 65534:  # ARPHRD_NONE: tun and WireGuard
            return "vpn"
        return "other"


def _float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None
