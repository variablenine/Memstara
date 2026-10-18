"""monerod's read-only console commands, run through monerod's RPC exactly as the console does.

One entry per read-only command in monerod's console (names, aliases and usage from
src/daemon/command_server.cpp, v0.18.5.3). Each entry lists the RPC calls its
executor makes (src/daemon/rpc_command_executor.cpp) and sends the same request
fields. Every one of those calls is on rpc.READ_ALLOWED, so no command here can
change anything on the node; check_registry() and the tests make sure of it.

Arguments are checked before anything is sent. A few commands have a safety limit
the console doesn't have; each one says so in its `help`.
"""

from __future__ import annotations

import ipaddress
import re
import sys
import time

from .rpc import READ_ALLOWED, RpcClient, RpcError, RpcNotAllowed

LONG_TIMEOUT_S = 120
HF_VERSION_PER_BYTE_FEE = 8       # cryptonote_config.h
WHITE_LIMIT, GRAY_LIMIT = 1000, 5000   # P2P_LOCAL_WHITE/GRAY_PEERLIST_LIMIT

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
INT_RE = re.compile(r"^[+-]?\d{1,20}$")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

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


# ---------------------------------------------------------------- registry

def P(name, label, type_, *, required=False, min_=None, max_=None, choices=None, placeholder="", help_="",
      default=None, check=None, maxlen=200):
    """One argument. Keys starting with _ never leave the server."""
    return {"name": name, "label": label, "type": type_, "required": required, "min": min_, "max": max_,
            "choices": [{"value": v, "label": l} for v, l in choices] if choices else None,
            "placeholder": placeholder, "help": help_, "default": default,
            "_check": check, "_maxlen": maxlen}


class Command:
    def __init__(self, fn, name, *, group, title, summary, usage, calls, params=(), aliases=(), long=False,
                 pre=None):
        self.fn = fn
        self.pre = pre   # checks across arguments, before any call
        self.name = name
        self.group = group
        self.title = title
        self.summary = summary
        self.usage = usage
        self.calls = list(calls)
        self.params = list(params)
        self.aliases = list(aliases)
        self.long = long

    def public(self) -> dict:
        return {"name": self.name, "aliases": self.aliases, "group": self.group, "title": self.title,
                "summary": self.summary, "usage": self.usage, "calls": self.calls,
                "params": [{k: v for k, v in p.items() if not k.startswith("_")} for p in self.params]}


REGISTRY: dict = {}
ALIASES: dict = {}
GROUPS = ["Node", "Chain", "Transactions", "Network", "Bans", "Mining"]


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


@command("limit", group="Network", title="Bandwidth limits", usage="limit",
         summary="The node's download and upload limits.", calls=["get_limit"])
def _limit(c, a):
    r = c.call("get_limit")
    return "limit-down is %s kB/s, limit-up is %s kB/s" % (r.get("limit_down"), r.get("limit_up"))


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


# ---- Mining

@command("mining_status", group="Mining", title="Mining status", usage="mining_status",
         summary="Whether the node is mining, how fast and to which address.", calls=["mining_status"])
def _mining_status(c, a):
    m = c.call("mining_status")
    if not m.get("active"):
        return "Not currently mining"
    return "Mining at %s with %s threads" % (hash_speed(m.get("speed")), m.get("threads_count"))


@command("rpc_payments", group="Mining", title="RPC payments", usage="rpc_payments",
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
    return v is None or (isinstance(v, str) and not v.strip())


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


class _Ctx:
    """What a command may do: only the RPC calls its entry lists, all of them reads. Results are kept
    by method."""

    def __init__(self, client: RpcClient, cmd: Command):
        self.client = client
        self.allowed = set(cmd.calls)
        self.calls: list = []
        self.result: dict = {}

    def call(self, method: str, params: dict | None = None, keep: bool = True) -> dict:
        if method not in self.allowed:
            raise RpcNotAllowed(method, "%s isn't one of this command's calls, so it was not sent" % method)
        self.calls.append(method)
        res = self.client.call(method, params)
        if keep:
            self.result[method] = res
        return res


class Commands:
    """GET /api/commands and POST /api/command."""

    def __init__(self, settings, log=None):
        self.settings = settings
        self.log = log or (lambda msg: print(msg, file=sys.stderr))

    def listing(self) -> dict:
        order = {g: i for i, g in enumerate(GROUPS)}
        cmds = sorted(REGISTRY.values(), key=lambda c: order.get(c.group, 99))
        return {"commands": [c.public() for c in cmds]}

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
            raise CommandError(404, "There's no read-only monerod command called %s." % _short(name, 40), "name")
        raw_args = body.get("args")
        if alias == "print_pool_sh" and isinstance(raw_args, (dict, type(None))):
            raw_args = dict(raw_args or {})
            raw_args.setdefault("short", True)
        return self._execute(cmd, validate_args(cmd, raw_args))

    def _client(self, cmd: Command) -> RpcClient:
        v = self.settings.values()
        timeout = LONG_TIMEOUT_S if cmd.long else v["source.timeout_s"]
        try:
            return RpcClient(v["source.rpc_url"], v["source.rpc_user"], v["source.rpc_password"], timeout)
        except ValueError:
            raise CommandError(502, "The node's RPC address in settings isn't valid.")

    def _execute(self, cmd: Command, args: dict) -> dict:
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
            raise CommandError(502, e.summary)
        out = {"ok": True, "name": cmd.name, "calls": ctx.calls, "result": ctx.result, "args": args}
        if message:
            out["message"] = _short(message, 2000)
        return out


def check_registry() -> list:
    """Problems with the registry against the RPC allowlist (empty when all is well)."""
    problems = []
    for cmd in REGISTRY.values():
        for c in cmd.calls:
            if c not in READ_ALLOWED:
                problems.append("%s calls %s, which isn't on the read-only list" % (cmd.name, c))
    return problems
