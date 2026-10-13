"""Every monerod console command, run through monerod's RPC exactly as the console does.

One entry per command in monerod's console (names, aliases and usage from
src/daemon/command_server.cpp, v0.18.5.3). Each entry lists the RPC calls its
executor makes (src/daemon/rpc_command_executor.cpp) and sends the same request
fields. The console itself is a thin wrapper around these RPC methods, so the
dashboard can read and change exactly what the console can, and nothing more.

Arguments are checked before anything is sent. A few commands have a safety limit
the console doesn't have; each one says so in its `help`. Commands that change
something need a typed confirm word and `controls.enabled`. Only this module may
send the RPC methods on rpc.WRITE_ALLOWED (RpcClient.call_write checks the caller).
"""

from __future__ import annotations

import ipaddress
import re
import sys
import threading
import time

from .host import canon_ip
from .rpc import READ_ALLOWED, WRITE_ALLOWED, RpcClient, RpcError, RpcNotAllowed

LONG_TIMEOUT_S = 120
P2P_IP_BLOCKTIME = 86400          # monerod's own bans, and the console's default for `ban`
HF_VERSION_PER_BYTE_FEE = 8       # cryptonote_config.h
WHITE_LIMIT, GRAY_LIMIT = 1000, 5000   # P2P_LOCAL_WHITE/GRAY_PEERLIST_LIMIT
MAX_BAN_LINES = 256
MAX_LIMIT_KBPS = 10_000_000
# ban: said on the form and in the answer when the node's --p2p-bind-ip couldn't be read
OWN_UNCHECKED = ("The node's own address (its --p2p-bind-ip) couldn't be checked, because local reads are off, "
                 "the node runs on another computer, or the running node wasn't found on this computer. Make "
                 "sure you don't ban it.")

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
INT_RE = re.compile(r"^[+-]?\d{1,20}$")
B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                     r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
LOG_CATEGORIES_RE = re.compile(r"^[A-Za-z0-9*:,+\-._]{1,500}$")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# ranges a ban may never cover: not public, so banning them could cut the node off
# from this computer itself, a VPN, or the local network (fixed list, so the answer is
# the same on every Python version)
NOT_PUBLIC = [ipaddress.IPv4Network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4")]


class CommandError(Exception):
    def __init__(self, status: int, error: str, field: str | None = None):
        super().__init__(error)
        self.status = status
        self.error = error
        self.field = field


class ArgError(ValueError):
    """A bad argument; the message is a plain sentence for the person."""


# ---------------------------------------------------------------- argument checks

def check_hash(v: str) -> str:
    if not HEX64.match(v):
        raise ArgError("must be 64 hex characters (0-9, a-f)")
    return v.lower()


def check_block(v: str):
    if re.fullmatch(r"\d{1,20}", v):
        return int(v)
    if HEX64.match(v):
        return v.lower()
    raise ArgError("type a block height or a 64-character block hash")


def range_label(host: str) -> str:
    """203.0.113.0/24 -> 203.0.113.x, 203.0.0.0/16 -> 203.0.x.x."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\.(\d+)/(\d+)", host or "")
    if not m:
        return host
    if m.group(5) == "24":
        return "%s.%s.%s.x" % m.group(1, 2, 3)
    if m.group(5) == "16":
        return "%s.%s.x.x" % m.group(1, 2)
    return host


def parse_range(text) -> tuple[str, ipaddress.IPv4Network]:
    """One ban target -> (host as monerod gets it, network).

    Accepts a.b.c.d, a.b.c.x (/24), a.b.x.x (/16) and a.b.c.d/N with 16 <= N <= 32
    (host bits are cleared). IPv4 only. Refuses anything that isn't a public address.
    """
    t = str(text or "").strip().lower().replace(".*", ".x")
    if not t:
        raise ArgError("type an address or a range")
    prefix = 32
    if "/" in t:
        t, p = t.split("/", 1)
        if not re.fullmatch(r"\d{1,2}", p):
            raise ArgError("after the / goes a number from 16 to 32")
        prefix = int(p)
    elif t.endswith(".x.x"):
        prefix, t = 16, t[:-4] + ".0.0"
    elif t.endswith(".x"):
        prefix, t = 24, t[:-2] + ".0"
    parts = t.split(".")
    if len(parts) != 4 or any(not re.fullmatch(r"\d{1,3}", x) or int(x) > 255 for x in parts):
        raise ArgError("%s isn't an IPv4 address or range (a.b.c.d, a.b.c.x, a.b.x.x or a.b.c.d/16 to /32)"
                       % _short(text))
    if prefix < 16:
        raise ArgError("ranges wider than a.b.x.x (/16, 65,536 addresses) aren't allowed")
    if prefix > 32:
        raise ArgError("after the / goes a number from 16 to 32")
    net = ipaddress.IPv4Network("%s/%d" % (".".join(str(int(x)) for x in parts), prefix), strict=False)
    if (any(net.overlaps(n) for n in NOT_PUBLIC) or not net.network_address.is_global
            or not net.broadcast_address.is_global):
        raise ArgError("%s isn't a public address (private, local, shared or reserved), so banning it "
                       "could cut the node off from your own machines" % _label(net))
    host = str(net.network_address) if prefix == 32 else str(net)
    return host, net


def _label(net: ipaddress.IPv4Network) -> str:
    return range_label(str(net.network_address) if net.prefixlen == 32 else str(net))


def check_ip_or_range(v: str) -> str:
    """unban: an address or range as monerod lists it (IPv4, IPv4/N or IPv6)."""
    t = v.strip()
    try:
        if "/" in t:
            return str(ipaddress.IPv4Network(t, strict=True))
        if t.startswith("[") and t.endswith("]"):
            ipaddress.IPv6Address(t[1:-1])
            return t
        ipaddress.ip_address(t)
        return t
    except ValueError:
        raise ArgError("type an address or range exactly as the ban list shows it")


def check_ip(v: str) -> str:
    t = v.strip()
    try:
        if t.startswith("[") and t.endswith("]"):
            ipaddress.IPv6Address(t[1:-1])
        else:
            ipaddress.ip_address(t)
    except ValueError:
        raise ArgError("type one IPv4 or IPv6 address")
    return t


def check_monero_address(v: str) -> str:
    t = v.strip()
    if not t or not set(t) <= B58:
        raise ArgError("that isn't a Monero address")
    if len(t) == 106:
        raise ArgError("use your standard address (95 characters, starts with 4), not an integrated one")
    if len(t) != 95:
        raise ArgError("a Monero address has 95 characters")
    if t[0] in "8B7":
        raise ArgError("monerod can't mine to a subaddress yet: use your main address (starts with 4)")
    if t[0] not in "49A5":
        raise ArgError("that isn't a Monero address (main addresses start with 4)")
    return t


def check_threads(v: str):
    t = v.strip().lower()
    if t in ("auto", "autodetect"):
        return 0
    if re.fullmatch(r"\d{1,3}", t) and 1 <= int(t) <= 256:
        return int(t)
    raise ArgError("type auto or a number from 1 to 256")


def _host_port(v: str, need_port: bool) -> str:
    t = v.strip()
    host, port = t, None
    if t.startswith("["):
        m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?", t)
        if not m:
            raise ArgError("type host:port")
        try:
            ipaddress.IPv6Address(m.group(1))
        except ValueError:
            raise ArgError("that IPv6 address isn't valid")
        port = m.group(2)
    else:
        if t.count(":") == 1:
            host, port = t.split(":")
        if not HOST_RE.match(host):
            raise ArgError("type host:port (a name or an IP address)")
    if port is not None and not (port.isdigit() and 1 <= int(port) <= 65535):
        raise ArgError("the port must be 1 to 65535")
    if need_port and port is None:
        raise ArgError("add the port: host:port")
    return t


def check_bootstrap(v: str) -> str:
    t = v.strip()
    if t in ("auto", "none"):
        return t
    return _host_port(t, need_port=False)


def check_proxy(v: str) -> str:
    return _host_port(v, need_port=True)


def check_categories(v: str) -> str:
    if not LOG_CATEGORIES_RE.match(v):
        raise ArgError("use letters, digits and * : , + - . _ only, for example *:WARNING,net:INFO")
    return v


# ---------------------------------------------------------------- registry

def P(name, label, type_, *, required=False, min_=None, max_=None, choices=None, placeholder="", help_="",
      default=None, check=None, maxlen=200):
    """One argument. Keys starting with _ never leave the server."""
    return {"name": name, "label": label, "type": type_, "required": required, "min": min_, "max": max_,
            "choices": [{"value": v, "label": l} for v, l in choices] if choices else None,
            "placeholder": placeholder, "help": help_, "default": default,
            "_check": check, "_maxlen": maxlen}


class Command:
    def __init__(self, fn, name, *, group, title, summary, usage, calls, kind="read", confirm=None, warn=None,
                 params=(), aliases=(), long=False, read_when=None, pre=None):
        self.fn = fn
        self.pre = pre   # checks across arguments, before any confirm word or call
        self.name = name
        self.group = group
        self.title = title
        self.summary = summary
        self.usage = usage
        self.calls = list(calls)
        self.kind = kind
        self.confirm = confirm
        self.warn = warn
        self.params = list(params)
        self.aliases = list(aliases)
        self.long = long
        self.read_when = read_when or {}   # {param: value}: with these arguments it only reads

    def public(self) -> dict:
        return {"name": self.name, "aliases": self.aliases, "group": self.group, "title": self.title,
                "summary": self.summary, "usage": self.usage, "kind": self.kind, "confirm": self.confirm,
                "warn": self.warn, "read_when": self.read_when or None, "calls": self.calls,
                "params": [{k: v for k, v in p.items() if not k.startswith("_")} for p in self.params]}

    def changes(self, args: dict) -> bool:
        if self.kind != "change":
            return False
        if self.read_when and all(args.get(k) == v for k, v in self.read_when.items()):
            return False
        return True


REGISTRY: dict = {}
ALIASES: dict = {}
GROUPS = ["Node", "Chain", "Transactions", "Network", "Bans", "Mining", "Logs"]


def command(name, **kw):
    def deco(fn):
        cmd = Command(fn, name, **kw)
        REGISTRY[name] = cmd
        for a in cmd.aliases:
            ALIASES[a] = name
        return fn
    return deco


def registry_calls() -> set:
    return {c for cmd in REGISTRY.values() for c in cmd.calls}


# ---------------------------------------------------------------- formatting

def _n(v) -> str:
    return format(v, ",") if isinstance(v, int) and not isinstance(v, bool) else str(v)


def _short(v, n=60) -> str:
    return CONTROL_CHARS.sub("", str(v))[:n]


def money(atomic) -> str:
    """Like cryptonote::print_money: 12 decimals."""
    try:
        a = int(atomic)
    except (TypeError, ValueError):
        return "?"
    return "%d.%012d" % divmod(a, 10 ** 12)


def _wide(res: dict, key: str) -> int:
    w = res.get("wide_" + key)
    if isinstance(w, str):
        try:
            return int(w, 16)
        except ValueError:
            pass
    v = res.get(key)
    return v if isinstance(v, int) else 0


def hash_speed(hs) -> str:
    hs = float(hs or 0)
    if hs < 1000:
        return "%.0f H/s" % hs
    for p in "kMGTPEZY":
        hs /= 1000.0
        if hs < 1000:
            return "%.2f %sH/s" % (hs, p)
    return "%.2f YH/s" % hs


def span(seconds) -> str:
    s = int(max(0, seconds or 0))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return "%dd %dh %dm %ds" % (d, h, m, s)


def for_how_long(seconds: int) -> str:
    if seconds % 86400 == 0 and seconds >= 2 * 86400:
        return "%d days" % (seconds // 86400)
    if seconds % 3600 == 0:
        h = seconds // 3600
        return "1 hour" if h == 1 else "%d hours" % h
    if seconds % 60 == 0:
        m = seconds // 60
        return "1 minute" if m == 1 else "%d minutes" % m
    return "1 second" if seconds == 1 else "%s seconds" % _n(seconds)


def human_bytes(n) -> str:
    """Like tools::get_human_readable_bytes (steps of 1024)."""
    n = float(n or 0)
    if n < 1024:
        return "%d bytes" % n
    for unit in ("kB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return "%.2f %s" % (n, unit)
    return "%.2f TB" % n


def sync_pct(height, target) -> float:
    target = target if target and target >= height else height
    if not target:
        return 0.0
    pc = 100.0 * height / target
    return 99.9 if height < target and pc > 99.9 else pc


# ================================================================ the commands
# ---- Node

@command("status", group="Node", title="Status", usage="status",
         summary="Height, sync, mining, network hash rate, rules version, connections and uptime.",
         calls=["get_info", "mining_status", "hard_fork_info"])
def _status(c, a):
    info = c.call("get_info")
    hf = c.call("hard_fork_info", {"version": 0})
    try:
        mres = c.call("mining_status")
        mining = (("smart " if mres.get("is_background_mining_enabled") else "") + "mining at "
                  + hash_speed(mres.get("speed"))) if mres.get("active") else "not mining"
    except RpcError:
        mining = "mining info unavailable"   # like the console on a restricted port
    h, t = info.get("height") or 0, info.get("target_height") or 0
    net = max(h, t)
    nettype = "testnet" if info.get("testnet") else "stagenet" if info.get("stagenet") else "mainnet"
    target = info.get("target") or 120
    bootstrap = ""
    if info.get("was_bootstrap_ever_used"):
        # like the console: while monerod passes get_info on to its bootstrap node, the numbers
        # are that node's and only the local height is this node's own
        bootstrap = ", bootstrapping from %s" % _short(info.get("bootstrap_daemon_address") or "", 200)
        if info.get("untrusted"):
            own = info.get("height_without_bootstrap") or 0
            bootstrap += ", local height: %d (%.1f%%)" % (own, sync_pct(own, net))
        else:
            bootstrap += " was used before"
    msg = "Height: %d/%d (%.1f%%) on %s%s, %s, net hash %s, v%s, %d(out)+%d(in) connections" % (
        h, net, sync_pct(h, t), nettype, bootstrap, mining, hash_speed(_wide(info, "difficulty") / target),
        hf.get("version"), info.get("outgoing_connections_count") or 0, info.get("incoming_connections_count") or 0)
    if info.get("start_time"):
        msg += ", uptime " + span(time.time() - info["start_time"])
    return msg


@command("print_status", group="Node", title="Is monerod running?", usage="print_status",
         summary="Checks that monerod answers.", calls=["get_info"])
def _print_status(c, a):
    try:
        c.call("get_info", keep=False)
    except RpcError as e:
        return "monerod is NOT running (%s)" % e.short
    return "monerod is running"


@command("version", group="Node", title="Version", usage="version", summary="monerod's version.",
         calls=["get_info"])
def _version(c, a):
    return str(c.call("get_info").get("version") or "unknown")


@command("print_height", group="Node", title="Height", usage="print_height",
         summary="The local blockchain height.", calls=["get_height"])
def _print_height(c, a):
    return str(c.call("get_height").get("height"))


@command("diff", group="Node", title="Difficulty", usage="diff", summary="The current difficulty.",
         calls=["get_info"])
def _diff(c, a):
    i = c.call("get_info")
    d = _wide(i, "difficulty")
    return "BH: %s, TH: %s, DIFF: %d, CUM_DIFF: %d, HR: %d H/s" % (
        i.get("height"), i.get("top_block_hash"), d, _wide(i, "cumulative_difficulty"), d // (i.get("target") or 120))


@command("sync_info", group="Node", title="Sync info", usage="sync_info",
         summary="Sync state: height, target, the peers it downloads from and the spans queued.",
         calls=["sync_info"])
def _sync_info(c, a):
    r = c.call("sync_info")
    h, t = r.get("height") or 0, r.get("target_height") or 0
    t = t if t >= h else h
    peers = r.get("peers") or []
    dl = sum((p.get("info") or {}).get("current_download") or 0 for p in peers)
    return "Height: %d, target: %d (%.1f%%) · downloading at %d kB/s · %d peers · %d spans" % (
        h, t, (100.0 * h / t) if t else 0, dl, len(peers), len(r.get("spans") or []))


@command("save", group="Node", title="Save the blockchain", usage="save",
         summary="Writes the blockchain to disk now.", kind="change", confirm="SAVE",
         calls=["save_bc"], long=True)
def _save(c, a):
    c.call("save_bc")
    return "Blockchain saved"


@command("stop_daemon", group="Node", title="Stop the node", usage="stop_daemon", aliases=["exit"],
         summary="Stops monerod.", kind="change", confirm="STOP", calls=["stop_daemon"],
         warn="Stops the node. If a service or script restarts it automatically, it comes back on its own.")
def _stop_daemon(c, a):
    c.call("stop_daemon")
    return "Stop signal sent"


@command("update", group="Node", title="Check for an update", usage="update (check|download)",
         summary="Asks monerod whether a newer release exists, or downloads it.", kind="change",
         confirm="DOWNLOAD", calls=["update"], long=True, read_when={"command": "check"},
         warn="Download only: monerod saves the release file, it doesn't install it.",
         params=[P("command", "What to do", "choice", required=True, default="check",
                   choices=[("check", "Check only"), ("download", "Download")])])
def _update(c, a):
    r = c.call("update", {"command": a["command"], "path": ""})
    if not r.get("update"):
        return "No update available"
    msg = "Update available: v%s: %s, hash %s" % (r.get("version"), r.get("user_uri"), r.get("hash"))
    if a["command"] == "download":
        msg += (". Downloaded to: %s" % r.get("path")) if r.get("path") else ". Download failed"
    return msg


# ---- Chain

def _print_bc_pre(a):
    b, e = a["begin"], a["end"]
    if b < 0 and e is not None:
        raise ArgError("leave the end empty with a negative begin", "end")
    if b >= 0 and e is not None and e < b:
        raise ArgError("the end height must be at least the begin height", "end")
    if b >= 0 and e is not None and e - b + 1 > 1000:
        raise ArgError("at most 1,000 blocks per request (dashboard limit)", "end")


@command("print_bc", group="Chain", title="Blocks in a range", usage="print_bc <begin_height> [<end_height>]",
         summary="Block headers from begin to end, or the last N blocks with a negative begin.",
         calls=["get_info", "get_block_headers_range"], pre=_print_bc_pre,
         params=[P("begin", "From height", "int", required=True, min_=-1000, placeholder="3512300 or -10",
                   help_="A negative number -N means the last N blocks."),
                 P("end", "To height", "int", min_=0, placeholder="same as begin",
                   help_="At most 1,000 blocks per request (dashboard limit).")])
def _print_bc(c, a):
    b, e = a["begin"], a["end"]
    if b < 0:
        height = c.call("get_info").get("height") or 0
        if -b >= height:
            raise ArgError("start offset is larger than blockchain height", "begin")
        start, end = height + b, height - 1
    else:
        start, end = b, b if e is None else e
    r = c.call("get_block_headers_range", {"start_height": start, "end_height": end, "fill_pow_hash": False})
    n = len(r.get("headers") or [])
    return "%d block%s, heights %s to %s" % (n, "" if n == 1 else "s", _n(start), _n(end))


@command("print_block", group="Chain", title="One block", usage="print_block <block_hash> | <block_height>",
         summary="One block: header, transactions and its JSON.", calls=["get_block"],
         params=[P("block", "Height or hash", "text", required=True, check=check_block, maxlen=64,
                   placeholder="3512345 or a 64-character hash"),
                 P("hex", "Include the raw block (+hex)", "bool", default=False)])
def _print_block(c, a):
    req = {"height": a["block"]} if isinstance(a["block"], int) else {"hash": a["block"]}
    req["fill_pow_hash"] = True
    r = c.call("get_block", req)
    if not a["hex"]:
        r.pop("blob", None)
    h = r.get("block_header") or {}
    return "Block %s · %s · %s tx%s" % (_n(h.get("height")), h.get("hash"), h.get("num_txes"),
                                        "" if h.get("num_txes") == 1 else "s")


@command("bc_dyn_stats", group="Chain", title="Recent chain stats", usage="bc_dyn_stats <last_block_count>",
         summary="Difficulty, fee, block times, transactions, rewards and versions over the last N blocks.",
         calls=["get_info", "get_fee_estimate", "hard_fork_info", "get_block_headers_range"],
         params=[P("last", "Last N blocks", "int", required=True, min_=1, max_=10000, default=100,
                   help_="1 to 10,000 (dashboard limit).")])
def _bc_dyn_stats(c, a):
    info = c.call("get_info")
    fee = c.call("get_fee_estimate", {"grace_blocks": 0})
    hf = c.call("hard_fork_info", {"version": HF_VERSION_PER_BYTE_FEE})
    height = info.get("height") or 0
    msg = "Height: %s, diff %d, cum. diff %d, target %s sec, dyn fee %s/%s" % (
        height, _wide(info, "difficulty"), _wide(info, "cumulative_difficulty"), info.get("target"),
        money(fee.get("fee")), "byte" if hf.get("enabled") else "kB")
    n = min(a["last"], height)
    if n <= 0:
        return msg
    r = c.call("get_block_headers_range", {"start_height": height - n, "end_height": height - 1,
                                           "fill_pow_hash": False})
    hs = r.get("headers") or []
    if not hs:
        return msg
    weights = sorted(h.get("block_weight") or 0 for h in hs)
    mid = len(weights) // 2
    median = weights[mid] if len(weights) % 2 else (weights[mid - 1] + weights[mid]) // 2
    times = [h.get("timestamp") or 0 for h in hs]
    msg += ". Last %d: avg. diff %d, %d avg sec/block, avg num txes %.2f, avg. reward %s, median block weight %d" % (
        n, sum(_wide(h, "difficulty") for h in hs) // n, (max(times) - min(times)) // n,
        sum(h.get("num_txes") or 0 for h in hs) / n, money(sum(h.get("reward") or 0 for h in hs) // n), median)
    return msg


@command("alt_chain_info", group="Chain", title="Alternative chains", usage="alt_chain_info [blockhash]",
         summary="Chains the node knows that branch off its main chain, or one of them in detail.",
         calls=["get_info", "get_alternate_chains", "get_block_header_by_hash"],
         params=[P("blockhash", "Tip of one chain", "text", check=check_hash, maxlen=64,
                   placeholder="empty = list them all",
                   help_="Details (block headers) for chains up to 1,000 blocks long (dashboard limit).")])
def _alt_chain_info(c, a):
    info = c.call("get_info")
    r = c.call("get_alternate_chains")
    chains = r.get("chains") or []
    height = info.get("height") or 0
    if not a["blockhash"]:
        return "%d alternate chain%s found" % (len(chains), "" if len(chains) == 1 else "s")
    chain = next((ch for ch in chains if ch.get("block_hash") == a["blockhash"]), None)
    if chain is None:
        return "Block hash %s is not the tip of any known alternate chain" % a["blockhash"]
    length = chain.get("length") or 0
    start = (chain.get("height") or 0) - length + 1
    msg = "Found alternate chain with tip %s: %d blocks long, from height %d (%d deep)" % (
        a["blockhash"], length, start, height - start - 1)
    hashes = list(chain.get("block_hashes") or [])
    if hashes and len(hashes) <= 1000 and chain.get("main_chain_parent_block"):
        c.call("get_block_header_by_hash", {"hashes": hashes + [chain["main_chain_parent_block"]],
                                            "fill_pow_hash": False})
    return msg


@command("hard_fork_info", group="Chain", title="Network rules version", usage="hard_fork_info <version>",
         summary="Hard fork voting: the current rules version, or whether a given version is on.",
         calls=["hard_fork_info"],
         params=[P("version", "Version", "int", min_=1, max_=255, placeholder="empty = current")])
def _hard_fork_info(c, a):
    r = c.call("hard_fork_info", {"version": a["version"] or 0})
    v = a["version"] or r.get("voting")
    return "version %s %s, %s/%s votes, threshold %s. Current version %s, voting for version %s" % (
        v, "enabled" if r.get("enabled") else "not enabled", r.get("votes"), r.get("window"), r.get("threshold"),
        r.get("version"), r.get("voting"))


@command("print_coinbase_tx_sum", group="Chain", title="Coinbase sum",
         usage="print_coinbase_tx_sum <start_height> [<block_count>]",
         summary="Coins created and fees paid in a range of blocks.", calls=["get_coinbase_tx_sum"], long=True,
         params=[P("start", "From height", "int", required=True, min_=0),
                 P("count", "Number of blocks", "int", required=True, min_=1, max_=100000, default=1000,
                   help_="1 to 100,000 (dashboard limit: big ranges keep the node busy).")])
def _coinbase(c, a):
    r = c.call("get_coinbase_tx_sum", {"height": a["start"], "count": a["count"]})
    em, fee = _wide(r, "emission_amount"), _wide(r, "fee_amount")
    return ("Sum of coinbase transactions between block heights [%d, %d) is %s consisting of %s in emissions, "
            "and %s in fees" % (a["start"], a["start"] + a["count"], money(em + fee), money(em), money(fee)))


@command("output_histogram", group="Chain", title="Output histogram",
         usage="output_histogram [@<amount>] <min_count> [<max_count>]",
         summary="How many outputs exist per amount.", calls=["get_output_histogram"], long=True,
         params=[P("amount", "Amount (atomic units)", "int", min_=0, max_=2 ** 64 - 1, placeholder="empty = all"),
                 P("min_count", "At least", "int", min_=0, default=3),
                 P("max_count", "At most", "int", min_=0, placeholder="0 = no limit")])
def _output_histogram(c, a):
    r = c.call("get_output_histogram", {
        "amounts": [a["amount"]] if a["amount"] is not None else [], "min_count": a["min_count"],
        "max_count": a["max_count"] or 0, "unlocked": False, "recent_cutoff": 0})
    n = len(r.get("histogram") or [])
    return "%d amount%s" % (n, "" if n == 1 else "s")


@command("check_blockchain_pruning", group="Chain", title="Is the chain pruned?",
         usage="check_blockchain_pruning", summary="Whether this node keeps a pruned blockchain.",
         calls=["prune_blockchain"])
def _check_pruning(c, a):
    r = c.call("prune_blockchain", {"check": True})
    return "Blockchain is pruned" if r.get("pruning_seed") else "Blockchain is not pruned"


@command("pop_blocks", group="Chain", title="Remove the newest blocks", usage="pop_blocks <nblocks>",
         summary="Removes blocks from the end of the blockchain.", kind="change", confirm="POP",
         calls=["pop_blocks"], long=True,
         warn="Removes the newest blocks; the node downloads them again.",
         params=[P("count", "Blocks to remove", "int", required=True, min_=1, max_=1000,
                   help_="1 to 1,000 (dashboard limit).")])
def _pop_blocks(c, a):
    r = c.call("pop_blocks", {"nblocks": a["count"]})
    return "new height: %s" % r.get("height")


@command("prune_blockchain", group="Chain", title="Prune the blockchain", usage="prune_blockchain [confirm]",
         summary="Prunes the blockchain (keeps about a third of the old data).", kind="change", confirm="PRUNE",
         calls=["prune_blockchain"], long=True,
         warn="Permanent: going back to a full chain means downloading it all again. Pruning from inside "
              "monerod doesn't shrink the database file; freed parts are reused instead. It runs inside "
              "monerod for a long time; the dashboard stops waiting after 120 s.")
def _prune(c, a):
    c.call("prune_blockchain", {"check": False})
    return "Blockchain pruned"


# ---- Transactions

@command("print_tx", group="Transactions", title="One transaction", usage="print_tx <transaction_hash> [+hex] [+json]",
         summary="Where a transaction is (pool or block) and, if asked, its raw data and JSON.",
         calls=["get_transactions"],
         params=[P("txid", "Transaction id", "text", required=True, check=check_hash, maxlen=64),
                 P("hex", "Include the raw transaction (+hex)", "bool", default=False),
                 P("json", "Include it as JSON (+json)", "bool", default=False)])
def _print_tx(c, a):
    r = c.call("get_transactions", {"txs_hashes": [a["txid"]], "decode_as_json": a["json"], "split": True,
                                    "prune": False})
    if not a["hex"]:
        r.pop("txs_as_hex", None)
        for t in r.get("txs") or []:
            for k in ("as_hex", "pruned_as_hex", "prunable_as_hex"):
                t.pop(k, None)
    txs = r.get("txs") or []
    if len(txs) != 1:
        return "Transaction wasn't found: %s" % a["txid"]
    t = txs[0]
    return "Found in pool" if t.get("in_pool") else "Found in blockchain at height %s" % t.get("block_height")


@command("is_key_image_spent", group="Transactions", title="Key image spent?", usage="is_key_image_spent <key_image>",
         summary="Whether a key image is in the spent set (in a block or in the pool).",
         calls=["is_key_image_spent"],
         params=[P("key_image", "Key image", "text", required=True, check=check_hash, maxlen=64)])
def _ki_spent(c, a):
    r = c.call("is_key_image_spent", {"key_images": [a["key_image"]]})
    st = r.get("spent_status") or []
    if len(st) != 1:
        return "key image status could not be determined"
    return "%s: %s%s" % (a["key_image"], "spent" if st[0] else "unspent", " (in pool)" if st[0] == 2 else "")


@command("print_pool", group="Transactions", title="Transaction pool", usage="print_pool", aliases=["print_pool_sh"],
         summary="Every transaction waiting in the pool, and the key images they spend.",
         calls=["get_transaction_pool"],
         params=[P("short", "Short format (print_pool_sh)", "bool", default=False)])
def _print_pool(c, a):
    r = c.call("get_transaction_pool")
    txs = r.get("transactions") or []
    if a["short"]:
        for t in txs:
            t.pop("tx_json", None)
            t.pop("tx_blob", None)
    if not txs and not r.get("spent_key_images"):
        return "Pool is empty"
    return "%d transaction%s in the pool" % (len(txs), "" if len(txs) == 1 else "s")


@command("print_pool_stats", group="Transactions", title="Pool stats", usage="print_pool_stats",
         summary="Size, fees, ages and double spends in the transaction pool.",
         calls=["get_transaction_pool_stats", "get_info"])
def _pool_stats(c, a):
    ps = (c.call("get_transaction_pool_stats").get("pool_stats") or {})
    c.call("get_info")
    return "%s transactions, %s bytes, %s double spends" % (
        _n(ps.get("txs_total") or 0), _n(ps.get("bytes_total") or 0), _n(ps.get("num_double_spends") or 0))


@command("relay_tx", group="Transactions", title="Relay a transaction", usage="relay_tx <txid>",
         summary="Sends a transaction from the pool to the network again.", kind="change", confirm="RELAY",
         calls=["relay_tx"],
         params=[P("txid", "Transaction id", "text", required=True, check=check_hash, maxlen=64)])
def _relay_tx(c, a):
    c.call("relay_tx", {"txids": [a["txid"]]})
    return "Transaction successfully relayed"


@command("flush_txpool", group="Transactions", title="Flush the pool", usage="flush_txpool [<txid>]",
         summary="Removes one transaction from the pool, or all of them.", kind="change", confirm="FLUSH",
         calls=["flush_txpool"], long=True,
         params=[P("txid", "Transaction id", "text", check=check_hash, maxlen=64,
                   placeholder="empty = the whole pool")])
def _flush_txpool(c, a):
    c.call("flush_txpool", {"txids": [a["txid"]] if a["txid"] else []})
    return "Pool successfully flushed"


# ---- Network

@command("print_cn", group="Network", title="Connections", usage="print_cn",
         summary="Every peer connected right now.", calls=["get_connections"])
def _print_cn(c, a):
    n = len(c.call("get_connections").get("connections") or [])
    return "%d connection%s" % (n, "" if n == 1 else "s")


def _peer_list_req():
    # print_pl leaves public_only at its zero value (false) and sets include_blocked
    return {"public_only": False, "include_blocked": True}


@command("print_pl", group="Network", title="Peer list", usage="print_pl [white] [gray] [pruned] [publicrpc] [<limit>]",
         summary="The peers this node knows about (white = seen working, gray = heard of).",
         calls=["get_peer_list"],
         params=[P("list", "Which list", "choice", default="all",
                   choices=[("all", "Both"), ("white", "White"), ("gray", "Gray")]),
                 P("pruned", "Only pruned nodes", "bool", default=False),
                 P("publicrpc", "Only nodes with public RPC", "bool", default=False),
                 P("limit", "At most (per list)", "int", min_=0, max_=100000, placeholder="0 = all")])
def _print_pl(c, a):
    r = c.call("get_peer_list", _peer_list_req())
    total = 0
    for key, which in (("white_list", "white"), ("gray_list", "gray")):
        if a["list"] not in ("all", which):
            r.pop(key, None)
            continue
        peers = r.get(key) or []
        if a["limit"]:   # as the console: the first <limit> of the list, then only those that fit
            peers = peers[: a["limit"]]
        peers = [p for p in peers
                 if (not a["pruned"] or p.get("pruning_seed")) and (not a["publicrpc"] or p.get("rpc_port"))]
        r[key] = peers
        total += len(peers)
    return "%d peer%s listed" % (total, "" if total == 1 else "s")


@command("print_pl_stats", group="Network", title="Peer list size", usage="print_pl_stats",
         summary="How full the white and gray peer lists are.", calls=["get_peer_list"])
def _print_pl_stats(c, a):
    r = c.call("get_peer_list", _peer_list_req())
    w, g = len(r.get("white_list") or []), len(r.get("gray_list") or [])
    return "White list size: %d/%d (%.1f%%) · Gray list size: %d/%d (%.1f%%)" % (
        w, WHITE_LIMIT, 100.0 * w / WHITE_LIMIT, g, GRAY_LIMIT, 100.0 * g / GRAY_LIMIT)


@command("print_net_stats", group="Network", title="Network stats", usage="print_net_stats",
         summary="Bytes received and sent since the node started, against the limits.",
         calls=["get_net_stats", "get_limit"])
def _print_net_stats(c, a):
    ns = c.call("get_net_stats")
    lim = c.call("get_limit")
    secs = max(0, int(time.time()) - (ns.get("start_time") or 0))
    out = []
    for word, key, lkey in (("Received", "total_bytes_in", "limit_down"), ("Sent", "total_bytes_out", "limit_up")):
        total = ns.get(key) or 0
        avg = total // secs if secs else 0
        limit = (lim.get(lkey) or 0) * 1024
        out.append("%s %s in %s, average %s/s = %.2f%% of the limit of %s/s" % (
            word, human_bytes(total), span(secs), human_bytes(avg), (100.0 * avg / limit) if limit else 0,
            human_bytes(limit)))
    return "; ".join(out)


_KBPS_HELP = "kB/s. -1 puts back the default. Empty only reads the limit. Lasts until the node restarts."


def _limit(c, a, down: bool, up: bool):
    k = a["kbps"]
    if k is None:
        r, verb = c.call("get_limit"), "is"
    else:
        r, verb = c.call("set_limit", {"limit_down": k if down else 0, "limit_up": k if up else 0}), "set to"
    parts = (["limit-down %s %s kB/s" % (verb, r.get("limit_down"))] if down else []) + \
            (["limit-up %s %s kB/s" % (verb, r.get("limit_up"))] if up else [])
    msg = ", ".join(parts)
    return ("Set " + msg.replace(" set to", " to")) if k is not None else msg


def check_kbps(v: int) -> int:
    if v == 0:
        raise ArgError("0 changes nothing: use -1 for the default, or leave it empty to just see the limit")
    return v


def _kbps_param():
    return P("kbps", "Limit", "int", min_=-1, max_=MAX_LIMIT_KBPS, placeholder="empty = show it",
             help_=_KBPS_HELP, check=check_kbps)


@command("limit", group="Network", title="Bandwidth limits", usage="limit [<kB/s>]",
         summary="Shows or sets both the download and upload limit.", kind="change", confirm="LIMIT",
         calls=["get_limit", "set_limit"], read_when={"kbps": None}, params=[_kbps_param()])
def _limit_both(c, a):
    return _limit(c, a, True, True)


@command("limit_up", group="Network", title="Upload limit", usage="limit_up [<kB/s>]",
         summary="Shows or sets the upload limit.", kind="change", confirm="LIMIT",
         calls=["get_limit", "set_limit"], read_when={"kbps": None}, params=[_kbps_param()])
def _limit_up(c, a):
    return _limit(c, a, False, True)


@command("limit_down", group="Network", title="Download limit", usage="limit_down [<kB/s>]",
         summary="Shows or sets the download limit.", kind="change", confirm="LIMIT",
         calls=["get_limit", "set_limit"], read_when={"kbps": None}, params=[_kbps_param()])
def _limit_down(c, a):
    return _limit(c, a, True, False)


def _peers(c, a, which):
    m = a["max"]
    r = c.call(which, {"set": m is not None, which: m if m is not None else 0})
    n = r.get(which)
    s = "unlimited" if n == 2 ** 32 - 1 else str(n)
    return ("Max number of %s peers set to %s" if m is not None else "Max number of %s peers is %s") % (
        which.split("_")[0], s)


def _peers_param(which):
    return P("max", "Most %s peers" % which, "int", min_=0, max_=1000, placeholder="empty = show it",
             help_="0 to 1,000 (dashboard limit). Empty only reads it. Lasts until the node restarts, "
                   "when its --out-peers / --in-peers flag (or monerod's default) applies again.")


@command("out_peers", group="Network", title="Outgoing peers", usage="out_peers <max_number>",
         summary="Sets (or shows) the most outgoing connections.", kind="change", confirm="PEERS",
         calls=["out_peers"], read_when={"max": None}, params=[_peers_param("outgoing")])
def _out_peers(c, a):
    return _peers(c, a, "out_peers")


@command("in_peers", group="Network", title="Incoming peers", usage="in_peers <max_number>",
         summary="Sets (or shows) the most incoming connections.", kind="change", confirm="PEERS",
         calls=["in_peers"], read_when={"max": None}, params=[_peers_param("incoming")])
def _in_peers(c, a):
    return _peers(c, a, "in_peers")


def _bootstrap_pre(a):
    addr, user, pw, proxy = a["address"], a["username"] or "", a["password"] or "", a["proxy"] or ""
    if addr in ("auto", "none") and (user or pw):
        raise ArgError("a user and password only go with host:port", "username")
    if bool(user) != bool(pw):
        raise ArgError("give both the user and the password, or neither", "password" if user else "username")
    if addr == "none" and proxy:
        raise ArgError("no proxy with none", "proxy")


@command("set_bootstrap_daemon", group="Network", title="Bootstrap node",
         usage="set_bootstrap_daemon (auto | none | host[:port] [username] [password]) [proxy_ip:proxy_port]",
         summary="A remote node that wallets using yours are sent to while yours is still syncing.",
         kind="change", confirm="BOOTSTRAP", calls=["set_bootstrap_daemon"], pre=_bootstrap_pre,
         warn="Wallets using your node get sent to that node while yours syncs.",
         params=[P("address", "Address", "text", required=True, check=check_bootstrap, maxlen=260,
                   placeholder="auto, none or host:port"),
                 P("username", "User", "text", maxlen=100, help_="Only with host:port."),
                 P("password", "Password", "text", maxlen=200, help_="Only with host:port."),
                 P("proxy", "Proxy", "text", check=check_proxy, maxlen=100, placeholder="ip:port",
                   help_="Not with none.")])
def _bootstrap(c, a):
    addr, user, pw, proxy = a["address"], a["username"] or "", a["password"] or "", a["proxy"] or ""
    c.call("set_bootstrap_daemon", {"address": "" if addr == "none" else addr, "username": user,
                                    "password": pw, "proxy": proxy})
    return "Successfully set bootstrap daemon address to %s" % ("none" if addr == "none" else addr)


# ---- Bans

@command("bans", group="Bans", title="Bans", usage="bans", summary="Every address and range banned now.",
         calls=["get_bans"])
def _bans(c, a):
    bans = c.call("get_bans").get("bans") or []
    return "%d ban%s" % (len(bans), "" if len(bans) == 1 else "s") if bans else "No IPs are banned"


@command("banned", group="Bans", title="Is it banned?", usage="banned <address>",
         summary="Whether one address is banned, and for how long.", calls=["banned"],
         params=[P("address", "Address", "text", required=True, check=check_ip, maxlen=60)])
def _banned(c, a):
    r = c.call("banned", {"address": a["address"]})
    if r.get("banned"):
        return "%s is banned for %s seconds" % (a["address"], r.get("seconds"))
    return "%s is not banned" % a["address"]


@command("ban", group="Bans", title="Ban", usage="ban [<IP>|@<filename>] [<seconds>]",
         summary="Refuses an address or range (and drops its connections) for a while.", kind="change",
         confirm="BAN", calls=["set_bans"],
         params=[P("hosts", "Addresses or ranges", "lines", required=True, maxlen=4000,
                   placeholder="203.0.113.x\n198.51.0.0/16",
                   help_="One per line, like the console's @file: a.b.c.d, a.b.c.x, a.b.x.x or a.b.c.d/16 to /32. "
                         "Refused (dashboard limit): IPv6, non-public addresses, ranges wider than /16, and "
                         "ranges holding the node's own address (--p2p-bind-ip) or one an add-on protects."),
                 P("seconds", "For (seconds)", "int", min_=1, max_=31_536_000, default=P2P_IP_BLOCKTIME,
                   help_="1 s to 1 year, default 24 h. monerod keeps bans only in memory. Bans last until they "
                         "run out or the node restarts; bans from --ban-list and the DNS blocklist come back "
                         "when it starts.")])
def _ban(c, a):
    hosts = a["hosts"]
    secs = a["seconds"]
    c.call("set_bans", {"bans": [{"host": h, "ip": 0, "ban": True, "seconds": secs} for h in hosts]})
    what = range_label(hosts[0]) if len(hosts) == 1 else "%d addresses or ranges" % len(hosts)
    return "Banned %s for %s. Connections from %s were dropped." % (
        what, for_how_long(secs), "it" if len(hosts) == 1 else "them")


@command("unban", group="Bans", title="Unban", usage="unban <address>",
         summary="Lifts a ban on an address or range.", kind="change", confirm="UNBAN", calls=["set_bans"],
         params=[P("address", "Address or range", "text", required=True, check=check_ip_or_range, maxlen=60,
                   help_="Exactly as the ban list shows it, for example 203.0.113.0/24.")])
def _unban(c, a):
    c.call("set_bans", {"bans": [{"host": a["address"], "ip": 0, "ban": False, "seconds": 0}]})
    return "Lifted any ban on %s." % range_label(a["address"])


def _flush_cache_pre(a):
    if not a["bad_blocks"]:
        raise ArgError("pick a cache to flush (bad blocks is the only one)", "bad_blocks")


@command("flush_cache", group="Bans", title="Flush a cache", usage="flush_cache [bad-txs] [bad-blocks]",
         summary="Forgets the blocks monerod marked as bad, so it can accept them again.", kind="change",
         confirm="FLUSH", calls=["flush_cache"], pre=_flush_cache_pre,
         params=[P("bad_blocks", "Bad blocks", "bool", default=True,
                   help_="The only cache monerod v0.18.5 can flush.")])
def _flush_cache(c, a):
    c.call("flush_cache", {"bad_blocks": True})
    return "Bad-blocks cache flushed"


# ---- Mining

@command("mining_status", group="Mining", title="Mining status", usage="mining_status",
         summary="Whether the node is mining, how fast and to which address.", calls=["mining_status"])
def _mining_status(c, a):
    m = c.call("mining_status")
    if not m.get("active"):
        return "Not currently mining"
    return "Mining at %s with %s threads" % (hash_speed(m.get("speed")), m.get("threads_count"))


@command("start_mining", group="Mining", title="Start mining",
         usage="start_mining <addr> [<threads>|auto] [do_background_mining] [ignore_battery]",
         summary="Mines on the node's computer, paying to an address.", kind="change", confirm="MINE",
         calls=["start_mining"],
         params=[P("address", "Pay to", "text", required=True, check=check_monero_address, maxlen=106,
                   help_="Your standard Monero address. Subaddresses don't work in monerod; integrated "
                         "addresses and OpenAlias names aren't taken here (dashboard limit)."),
                 P("threads", "Threads", "text", check=check_threads, maxlen=10, default="1",
                   placeholder="auto or 1-256"),
                 P("background", "Only when the computer is idle", "bool", default=False),
                 P("ignore_battery", "Also on battery", "bool", default=False)])
def _start_mining(c, a):
    c.call("start_mining", {"miner_address": a["address"], "threads_count": a["threads"],
                            "do_background_mining": a["background"], "ignore_battery": a["ignore_battery"]})
    return "Mining started in daemon"


@command("stop_mining", group="Mining", title="Stop mining", usage="stop_mining", summary="Stops mining.",
         kind="change", confirm="STOP", calls=["stop_mining"])
def _stop_mining(c, a):
    c.call("stop_mining")
    return "Mining stopped in daemon"


@command("show_hr", group="Mining", title="Show hash rate", usage="show_hr",
         summary="Starts writing the hash rate to monerod's log (only while mining).", kind="change",
         confirm="HR", calls=["set_log_hash_rate"])
def _show_hr(c, a):
    c.call("set_log_hash_rate", {"visible": True})
    return "Hash rate logging is on"


@command("hide_hr", group="Mining", title="Hide hash rate", usage="hide_hr",
         summary="Stops writing the hash rate to monerod's log.", kind="change", confirm="HR",
         calls=["set_log_hash_rate"])
def _hide_hr(c, a):
    c.call("set_log_hash_rate", {"visible": False})
    return "Hash rate logging is off"


# ---- Logs

def _set_log_pre(a):
    if (a["level"] is None) == (a["categories"] is None):
        raise ArgError("give a level or categories (one of the two)", "level")


@command("set_log", group="Logs", title="Log level", usage="set_log <level>|<{+,-,}categories>",
         summary="How much monerod writes to its log: a level 0-4, or categories.", kind="change",
         confirm="LOG", calls=["set_log_level", "set_log_categories"], pre=_set_log_pre,
         params=[P("level", "Level", "int", min_=0, max_=4, placeholder="0-4"),
                 P("categories", "Or categories", "text", check=check_categories, maxlen=500,
                   placeholder="*:WARNING,net.p2p:INFO", help_="Starting with + or - adds or removes.")])
def _set_log(c, a):
    if a["level"] is not None:
        c.call("set_log_level", {"level": a["level"]})
        return "Log level is now %d" % a["level"]
    r = c.call("set_log_categories", {"categories": a["categories"]})
    return "Log categories are now %s" % _short(r.get("categories"), 300)


@command("rpc_payments", group="Logs", title="RPC payments", usage="rpc_payments",
         summary="Clients paying for this node's RPC with mining (if RPC payment is on).",
         calls=["rpc_access_data"])
def _rpc_payments(c, a):
    r = c.call("rpc_access_data")
    entries = r.get("entries") or []
    bal = sum(e.get("balance") or 0 for e in entries)
    return "%d clients with a total of %d credits; aggregated client hash rate: %s" % (
        len(entries), bal, hash_speed(r.get("hashrate")))


# ---------------------------------------------------------------- running them

def _coerce(p: dict, v):
    t = p["type"]
    if t == "int":
        if isinstance(v, bool):
            raise ArgError("must be a whole number")
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if isinstance(v, str) and INT_RE.match(v.strip()):
            v = int(v.strip())
        if not isinstance(v, int):
            raise ArgError("must be a whole number")
        if p["min"] is not None and v < p["min"]:
            raise ArgError("must be at least %s" % _n(p["min"]))
        if p["max"] is not None and v > p["max"]:
            raise ArgError("must be at most %s" % _n(p["max"]))
        return v
    if t == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, int) and v in (0, 1):
            return bool(v)
        if isinstance(v, str) and v.strip().lower() in ("true", "yes", "1", "false", "no", "0"):
            return v.strip().lower() in ("true", "yes", "1")
        raise ArgError("must be true or false")
    if t == "choice":
        allowed = [ch["value"] for ch in p["choices"]]
        if not isinstance(v, str) or v not in allowed:
            raise ArgError("must be one of: %s" % ", ".join(allowed))
        return v
    if t == "lines":
        if isinstance(v, str):
            items = v.splitlines()
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            items = list(v)
        else:
            raise ArgError("must be text, one item per line")
        if sum(len(x) for x in items) > p["_maxlen"]:
            raise ArgError("is too long")
        out = []
        for line in items:
            line = line.split("#", 1)[0].strip()   # like the console's @file: # starts a comment
            if line:
                if CONTROL_CHARS.search(line):
                    raise ArgError("has characters that aren't allowed")
                out.append(line)
        return out
    # text
    if not isinstance(v, str):
        raise ArgError("must be text")
    v = v.strip()
    if len(v) > p["_maxlen"]:
        raise ArgError("must be at most %d characters" % p["_maxlen"])
    if CONTROL_CHARS.search(v):
        raise ArgError("has characters that aren't allowed")
    return v


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, list) and not v)


def validate_args(cmd: Command, args) -> dict:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise CommandError(400, "args must be an object")
    known = {p["name"] for p in cmd.params}
    for k in args:
        if k not in known:
            raise CommandError(400, "%s takes no argument called %s." % (cmd.name, _short(k, 40)), _short(k, 40))
    out = {}
    for p in cmd.params:
        name, v = p["name"], args.get(p["name"])
        if _empty(v):
            if p["required"]:
                raise CommandError(400, "%s is needed." % p["label"], name)
            if p["default"] is None:
                out[name] = False if p["type"] == "bool" else None
                continue
            v = p["default"]          # what the console uses when the argument is left out
        try:
            v = _coerce(p, v)
            if p["_check"] is not None:
                v = p["_check"](v)
        except ArgError as e:
            raise CommandError(400, "%s: %s." % (p["label"], e), name)
        if p["type"] == "lines" and not v:
            if p["required"]:
                raise CommandError(400, "%s is needed." % p["label"], name)
            v = None
        out[name] = v
    if cmd.pre is not None:
        try:
            cmd.pre(out)
        except ArgError as e:
            field = e.args[1] if len(e.args) > 1 else None
            raise CommandError(400, _sentence(e.args[0]), field)
    return out


def _sentence(s: str) -> str:
    s = str(s)
    return (s[:1].upper() + s[1:]).rstrip(".") + "."


def _ban_hosts(lines: list, own: list) -> list:
    """ban: every line -> the host monerod gets, refusing what the dashboard won't ban.

    own: addresses never to ban, as (ip, where it comes from) or plain ip strings: the
    node's own --p2p-bind-ip addresses and those add-ons protect (Poller.protected_addresses)."""
    if len(lines) > MAX_BAN_LINES:
        raise CommandError(400, "Addresses or ranges: at most %d lines at once." % MAX_BAN_LINES, "hosts")
    hosts, nets = [], []
    for line in lines:
        try:
            host, net = parse_range(line)
        except ArgError as e:
            raise CommandError(400, "Addresses or ranges: %s." % e, "hosts")
        for item in own:
            ip, src = item if isinstance(item, tuple) else (item, None)
            try:
                inside = ipaddress.IPv4Address(canon_ip(ip)) in net   # ::ffff:a.b.c.d is a.b.c.d
            except ValueError:
                continue
            if inside:
                raise CommandError(400, "%s holds this computer's own public address (%s), so banning it "
                                        "would cut your node off." % (_label(net), _own_from(src)), "hosts")
        if host not in hosts:
            hosts.append(host)
            nets.append(net)
    return hosts


def _own_from(src) -> str:
    if src == "node":
        return "the node is bound to it with --p2p-bind-ip"
    if src:
        return "from the %s add-on" % src
    return "the dashboard was told so"


class _Ctx:
    """What a command may do: only the RPC calls its entry lists. Results are kept by method."""

    def __init__(self, client: RpcClient, cmd: Command):
        self.client = client
        self.allowed = set(cmd.calls)
        self.calls: list = []
        self.result: dict = {}

    def call(self, method: str, params: dict | None = None, keep: bool = True) -> dict:
        if method not in self.allowed:
            raise RpcNotAllowed(method, "%s isn't one of this command's calls, so it was not sent" % method)
        self.calls.append(method)
        if method in WRITE_ALLOWED:
            res = self.client.call_write(method, params)
        else:
            res = self.client.call(method, params)
        if keep:
            self.result[method] = res
        return res


class Commands:
    """GET /api/commands and POST /api/command."""

    def __init__(self, settings, poller=None, log=None, notes=None):
        self.settings = settings
        self.poller = poller
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.notes = notes or (lambda: {})   # -> {command: text} from add-ons, added to its warn
        self._change_lock = threading.Lock()

    def listing(self) -> dict:
        order = {g: i for i, g in enumerate(GROUPS)}
        cmds = sorted(REGISTRY.values(), key=lambda c: order.get(c.group, 99))
        notes = {} if self._own_known() else {"ban": OWN_UNCHECKED}
        for name, text in (self.notes() or {}).items():
            name = ALIASES.get(name, name)
            if name in REGISTRY:
                notes[name] = (notes[name] + " " + text) if name in notes else text
        out = []
        for c in cmds:
            pub = c.public()
            if c.name in notes:
                pub["warn"] = (pub["warn"] + " " + notes[c.name]) if pub["warn"] else notes[c.name]
            out.append(pub)
        return {"commands": out}

    def run(self, body: dict) -> tuple[int, dict]:
        try:
            return 200, self._run(body if isinstance(body, dict) else {})
        except CommandError as e:
            out = {"ok": False, "error": e.error}
            if e.field:
                out["field"] = e.field
            return e.status, out

    def _run(self, body: dict) -> dict:
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise CommandError(400, "Say which command to run.", "name")
        alias = name if name in ALIASES else None
        cmd = REGISTRY.get(ALIASES.get(name, name))
        if cmd is None:
            raise CommandError(404, "There's no monerod command called %s." % _short(name, 40), "name")
        raw_args = body.get("args")
        if alias == "print_pool_sh" and isinstance(raw_args, (dict, type(None))):
            raw_args = dict(raw_args or {})
            raw_args.setdefault("short", True)
        args = validate_args(cmd, raw_args)
        if cmd.name == "ban":
            own = self.poller.protected_addresses() if self.poller is not None else []
            args["hosts"] = _ban_hosts(args["hosts"], own or [])
            if own is None:   # before the first poll
                raise CommandError(409, "The dashboard hasn't finished its first look at the node yet, so it "
                                        "can't check which addresses are this computer's own. Try again in a "
                                        "few seconds.")
        changes = cmd.changes(args)
        if changes:
            if self.settings.get("display.privacy") is True:
                raise CommandError(403, "Privacy mode is on. Turn it off to change anything on the node.")
            if not self.settings.get("controls.enabled"):
                raise CommandError(403, "Change commands are turned off in settings (Allow change commands).")
            word = body.get("confirm")
            if not isinstance(word, str) or word.strip().upper() != cmd.confirm:
                raise CommandError(400, "Type %s to confirm." % cmd.confirm, "confirm")
            if not self._change_lock.acquire(blocking=False):
                raise CommandError(409, "Another change command is still running. Try again when it's done.")
            try:
                return self._execute(cmd, args, True)
            finally:
                self._change_lock.release()
        return self._execute(cmd, args, False)

    def _own_known(self) -> bool:
        return self.poller is None or self.poller.own_address_known()

    def _client(self, cmd: Command) -> RpcClient:
        v = self.settings.values()
        timeout = LONG_TIMEOUT_S if cmd.long else v["source.timeout_s"]
        try:
            return RpcClient(v["source.rpc_url"], v["source.rpc_user"], v["source.rpc_password"], timeout)
        except ValueError:
            raise CommandError(502, "The node's RPC address in settings isn't valid.")

    def _execute(self, cmd: Command, args: dict, changes: bool) -> dict:
        ctx = _Ctx(self._client(cmd), cmd)
        try:
            message = cmd.fn(ctx, args)
        except ArgError as e:   # a check that needed the node's answer (print_bc with a negative begin)
            field = e.args[1] if len(e.args) > 1 else None
            raise CommandError(400, _sentence(e.args[0]), field)
        except RpcNotAllowed as e:
            self.log("command %s: refused an RPC call: %s" % (cmd.name, e.summary))
            raise CommandError(500, "The dashboard refused to send that call (a bug in the command list).")
        except RpcError as e:
            if changes:
                self.log("command %s failed: %s" % (cmd.name, e.summary))
            raise CommandError(502, e.summary)
        if cmd.name == "ban" and message and not self._own_known():
            message += " " + OWN_UNCHECKED
        out = {"ok": True, "name": cmd.name, "calls": ctx.calls, "result": ctx.result,
               "args": {k: ("" if k == "password" and v else v) for k, v in args.items()}}
        if message:
            out["message"] = _short(message, 2000)
        if changes:
            text = _short(message or "%s done" % cmd.name, 200)
            self.log("command %s: %s" % (cmd.name, text))
            if self.poller is not None:
                self.poller.add_event("control", text, "info")
                self.poller.reread(("get_bans", "get_limit"))
        return out


def check_registry() -> list:
    """Problems with the registry against the RPC allowlists (empty when all is well)."""
    problems = []
    allowed = set(READ_ALLOWED) | set(WRITE_ALLOWED)
    for cmd in REGISTRY.values():
        for c in cmd.calls:
            if c not in allowed:
                problems.append("%s calls %s, which isn't allowlisted" % (cmd.name, c))
        if cmd.kind == "change" and not cmd.confirm:
            problems.append("%s changes something but has no confirm word" % cmd.name)
    return problems
