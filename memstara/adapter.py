"""Turns monerod's RPC answers into the snapshot's shapes (pure functions).

Field names follow monerod v0.18.5.x (core_rpc_server_commands_defs.h and
cryptonote_protocol_defs.h). Anything missing becomes null, never a made-up number.
"""

from __future__ import annotations

import math

from .util import as_bool, as_int, as_str

UINT64_MAX = 2 ** 64 - 1
ADDRESS_TYPES = {1: "ipv4", 2: "ipv6", 3: "i2p", 4: "tor"}
KINDS = ("ipv4", "ipv6", "tor", "i2p")


def parse_info(info: dict, now: float, out_wanted: int) -> dict:
    """get_info -> the node fields that come from get_info.

    While a node syncs with help from a bootstrap node (set_bootstrap_daemon or
    --bootstrap-daemon-address) and is behind, monerod passes get_info on to that node and
    marks the answer untrusted (core_rpc_server.cpp on_get_info). Then only
    height_without_bootstrap and bootstrap_daemon_address are this node's own: the height
    is height_without_bootstrap, the target is the other node's height, synced is false,
    everything else is null, and "_bootstrap" holds that node's address (else None).
    """
    if as_bool(info.get("untrusted"), False):
        return _parse_bootstrapped(info, out_wanted)
    restricted = as_bool(info.get("restricted"), False)
    height = as_int(info.get("height"))
    raw_target = as_int(info.get("target_height"), 0) or 0
    target = max(height or 0, raw_target) if height is not None else None
    synced = None
    if height is not None:
        synced = bool(as_bool(info.get("synchronized"), False)
                      and not as_bool(info.get("busy_syncing"), False)
                      and height >= raw_target)
    sync_pct = None
    if height is not None and target:
        sync_pct = round(min(100.0, height * 100.0 / target), 2)
    start = as_int(info.get("start_time"), 0) or None
    free = as_int(info.get("free_space"))
    if restricted or free is None or free >= UINT64_MAX:
        free = None
    version = as_str(info.get("version"), "") or None
    difficulty = as_int(info.get("difficulty"))
    wide = info.get("wide_difficulty")
    if isinstance(wide, str) and wide.startswith("0x"):
        try:
            difficulty = int(wide, 16)
        except ValueError:
            pass
    target_s = as_int(info.get("target"), 120) or 120
    return {
        "version": version,
        "nettype": as_str(info.get("nettype"), None),
        "started": start,
        "uptime_s": max(0, int(now - start)) if start else None,
        "height": height,
        "target_height": target,
        "synced": synced,
        "sync_pct": sync_pct,
        "top_hash": as_str(info.get("top_block_hash"), None) or None,
        "difficulty": difficulty,
        "hashrate": int(difficulty // target_s) if difficulty is not None else None,
        "out_peers": None if restricted else as_int(info.get("outgoing_connections_count")),
        "out_wanted": out_wanted,
        "in_peers": None if restricted else as_int(info.get("incoming_connections_count")),
        "rpc_connections": None if restricted else as_int(info.get("rpc_connections_count")),
        "white_peers": None if restricted else as_int(info.get("white_peerlist_size")),
        "grey_peers": None if restricted else as_int(info.get("grey_peerlist_size")),
        "tx_pool": as_int(info.get("tx_pool_size")),
        "alt_blocks": None if restricted else as_int(info.get("alt_blocks_count")),
        "free_bytes": free,
        "db_bytes": None if restricted else as_int(info.get("database_size")),   # rounded up to 5 GiB there
        "update_available": None if restricted else as_bool(info.get("update_available")),
        "_restricted": restricted,
        "_median_weight": as_int(info.get("block_weight_median")) or as_int(info.get("block_size_median")),
        # the biggest block the next one may be: twice the median (blockchain.cpp)
        "_max_weight": as_int(info.get("block_weight_limit")) or as_int(info.get("block_size_limit")),
        "_target_s": target_s,
        "_bootstrap": None,
    }


def _parse_bootstrapped(info: dict, out_wanted: int) -> dict:
    """An untrusted get_info (answered by the bootstrap node): see parse_info."""
    out = {k: None for k in parse_info({}, 0, out_wanted)}
    height = as_int(info.get("height_without_bootstrap"))
    if height is not None:
        target = max(height, as_int(info.get("height"), 0) or 0, as_int(info.get("target_height"), 0) or 0)
        out.update(height=height, target_height=target, synced=False,
                   sync_pct=round(min(100.0, height * 100.0 / target), 2) if target else None)
    out.update(out_wanted=out_wanted, _restricted=False, _target_s=120,
               _bootstrap=as_str(info.get("bootstrap_daemon_address"), "", 200) or "another node")
    return out


def rpc_version_text(v) -> str | None:
    v = as_int(v)
    if v is None:
        return None
    return "%d.%d" % (v >> 16, v & 0xFFFF)


def parse_fees(res: dict):
    fees = res.get("fees")
    if isinstance(fees, list) and fees and all(as_int(f) is not None for f in fees):
        return [as_int(f) for f in fees]
    return None


def parse_limit(res: dict):
    up, down = as_int(res.get("limit_up")), as_int(res.get("limit_down"))
    return (up * 1024 if up is not None else None, down * 1024 if down is not None else None)


def parse_bans(res: dict):
    bans = res.get("bans") or []  # monerod leaves an empty list out
    if not isinstance(bans, list):
        return None, None
    ranges = sum(1 for b in bans if isinstance(b, dict) and "/" in str(b.get("host", "")))
    return len(bans), ranges


BAN_LIST_MAX_S = 86400   # bans with 24 h or less left are listed one by one


def parse_ban_list(res: dict, elapsed: float = 0):
    """get_bans -> (ban_list, bans_long).

    ban_list: bans with 24 h or less left (monerod's own and ones you made), longest
    first, as {host, seconds}; `elapsed` is how long ago get_bans was read, so the
    seconds count down between reads. bans_long: how many have more than 24 h left
    (ban list file, DNS blocklist).
    """
    bans = res.get("bans") or []
    if not isinstance(bans, list):
        return None, None
    short, long_n = [], 0
    for b in bans:
        if not isinstance(b, dict):
            continue
        host = as_str(b.get("host"), "", 80)
        secs = as_int(b.get("seconds"))
        if not host or secs is None:
            continue
        if secs > BAN_LIST_MAX_S:
            long_n += 1
            continue
        left = secs - int(max(0, elapsed))
        if left > 0:
            short.append({"host": host, "seconds": left})
    short.sort(key=lambda b: (-b["seconds"], b["host"]))
    return short, long_n


def _port_from_address(address: str):
    if not address or ":" not in address:
        return None
    return as_int(address.rsplit(":", 1)[1])


def parse_peer(c: dict) -> dict:
    host = as_str(c.get("host"), "") or ""
    kind = ADDRESS_TYPES.get(as_int(c.get("address_type"), 0))
    if kind is None:
        if host.endswith(".onion"):
            kind = "tor"
        elif host.endswith(".i2p"):
            kind = "i2p"
        elif ":" in host:
            kind = "ipv6"
        else:
            kind = "ipv4"
    if kind in ("tor", "i2p"):
        ip = ""
    elif kind == "ipv4":
        ip = as_str(c.get("ip"), "") or host
    else:
        ip = host.strip("[]")
    port = as_int(c.get("port")) if c.get("port") not in (None, "") else None
    if port is None:
        port = _port_from_address(as_str(c.get("address"), "") or "")
    net16 = net24 = ""
    if kind == "ipv4" and ip.count(".") == 3:
        o = ip.split(".")
        net16, net24 = ".".join(o[:2]), ".".join(o[:3])
    cid = as_str(c.get("connection_id"), "") or ""
    down = as_int(c.get("current_download"))
    up = as_int(c.get("current_upload"))
    return {
        "id": cid[:8],
        "dir": "in" if as_bool(c.get("incoming"), False) else "out",
        "kind": kind,
        "ip": ip,
        "host": host,
        "port": port,
        "net16": net16,
        "net24": net24,
        "age_s": as_int(c.get("live_time")),
        "height": as_int(c.get("height")),
        "state": as_str(c.get("state"), None),
        "down_bps": down * 1024 if down is not None else None,
        "up_bps": up * 1024 if up is not None else None,
        "recv_bytes": as_int(c.get("recv_count")),      # totals since the connection opened
        "sent_bytes": as_int(c.get("send_count")),
        "recv_idle_s": as_int(c.get("recv_idle_time")),  # seconds since the peer last sent anything
        "send_idle_s": as_int(c.get("send_idle_time")),
        "pruned": (as_int(c.get("pruning_seed"), 0) or 0) != 0,
    }


def _top_cluster(peers, direction, key):
    groups: dict = {}
    for p in peers:
        if p["dir"] != direction or p["kind"] != "ipv4" or not p[key]:
            continue
        groups.setdefault(p[key], []).append(p["ip"])
    if not groups:
        return {"net": "", "n": 0, "ips": []}
    net = sorted(groups, key=lambda n: (-len(groups[n]), n))[0]
    ips = sorted(groups[net], key=lambda ip: tuple(int(x) if x.isdigit() else 0 for x in ip.split(".")))
    return {"net": net, "n": len(ips), "ips": ips}


def parse_connections(res: dict) -> dict:
    conns = res.get("connections") or []  # monerod leaves an empty list out
    peers = [parse_peer(c) for c in conns if isinstance(c, dict)]
    out_by = {k: 0 for k in KINDS}
    in_by = {k: 0 for k in KINDS}
    for p in peers:
        (out_by if p["dir"] == "out" else in_by)[p["kind"]] += 1
    return {
        "list": peers,
        "spread": {"out16": _top_cluster(peers, "out", "net16"),
                   "in24": _top_cluster(peers, "in", "net24")},
        "out_by_kind": out_by,
        "in_by_kind": in_by,
    }


def empty_peers() -> dict:
    return {"list": [], "spread": None, "out_by_kind": None, "in_by_kind": None}


def parse_pool(res: dict, now: float) -> dict:
    ps = res.get("pool_stats")
    if not isinstance(ps, dict):
        ps = {}
    txs = as_int(ps.get("txs_total"), 0)
    oldest = as_int(ps.get("oldest"), 0)
    histo = []
    for h in ps.get("histo") or []:
        if isinstance(h, dict):
            histo.append({"txs": as_int(h.get("txs"), 0), "bytes": as_int(h.get("bytes"), 0)})
    return {
        "txs": txs,
        "bytes": as_int(ps.get("bytes_total"), 0),
        "fee_total": as_int(ps.get("fee_total"), 0),
        "oldest_age_s": max(0, int(now - oldest)) if txs and oldest else None,
        "double_spends": as_int(ps.get("num_double_spends"), 0),
        "failing": as_int(ps.get("num_failing"), 0),
        "not_relayed": as_int(ps.get("num_not_relayed"), 0),
        "over_10m": as_int(ps.get("num_10m"), 0),
        "histogram": histo,
        "histo_98pc": as_int(ps.get("histo_98pc"), 0),
    }


def empty_pool() -> dict:
    """No pool reading. list, list_wanted, list_cut, mined and check are pool.PoolList's."""
    return {"txs": None, "bytes": None, "fee_total": None, "oldest_age_s": None,
            "double_spends": None, "failing": None, "not_relayed": None, "over_10m": None,
            "histogram": [], "histo_98pc": None,
            "list": [], "list_wanted": 0, "list_cut": 0, "mined": [], "check": None}


def parse_headers(res: dict) -> list[dict]:
    """get_block_headers_range -> raw header dicts sorted by height (oldest first)."""
    hs = [h for h in (res.get("headers") or []) if isinstance(h, dict) and as_int(h.get("height")) is not None]
    hs.sort(key=lambda h: as_int(h.get("height")))
    return hs


def blocks_from_headers(headers: list[dict], shown: int, seen: dict | None = None) -> list[dict]:
    """The block tape: up to `shown` blocks, oldest first.

    time is the miner's own stamp (the block header's timestamp), and interval_s the
    difference from the block before. Miners stamp a block when they start work on it, so
    a block can arrive minutes after its stamp. seen is when this node got the block (unix
    s, from `seen`: block hash -> time, filled in by the poller as blocks arrive; None when
    not known, as for blocks from before the dashboard started) and seen_interval_s the
    time since the block before arrived, when both are known.
    """
    seen = seen or {}
    out = []
    prev_time = prev_height = prev_seen = None
    for h in headers:
        height = as_int(h.get("height"))
        t = as_int(h.get("timestamp"))
        block_hash = as_str(h.get("hash"), None)
        s = seen.get(block_hash) if block_hash else None
        follows = prev_height is not None and height is not None and prev_height == height - 1
        interval = t - prev_time if follows and prev_time is not None and t is not None else None
        seen_interval = round(s - prev_seen, 1) if follows and s is not None and prev_seen is not None else None
        out.append({
            "height": height,
            "hash": block_hash,
            "time": t,
            "interval_s": interval,
            "seen": round(s, 1) if s is not None else None,
            "seen_interval_s": seen_interval,
            "weight": as_int(h.get("block_weight")) or as_int(h.get("block_size")),
            "txs": as_int(h.get("num_txes")),
            "reward": as_int(h.get("reward")),
        })
        prev_time, prev_height, prev_seen = t, height, s
    return out[-shown:] if shown > 0 else out


def avg_interval(blocks: list[dict]):
    vals = [b["interval_s"] for b in blocks if b.get("interval_s") is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def wait_odds(age_s, target_s: int = 120):
    if age_s is None:
        return None
    return round(math.exp(-max(0, age_s) / float(target_s)), 3)


def parse_sync_info(res: dict) -> dict:
    spans = [s for s in (res.get("spans") or []) if isinstance(s, dict)]
    peers = [p for p in (res.get("peers") or []) if isinstance(p, dict)]
    return {
        "height": as_int(res.get("height")),
        "target_height": as_int(res.get("target_height")),
        "spans": len(spans),
        "blocks_queued": sum(as_int(s.get("nblocks"), 0) for s in spans),
        "peers": len(peers),
        "overview": as_str(res.get("overview"), "", 400),
    }


def find_reorg(stored: dict, headers: list[dict]):
    """Compare stored height->hash with fresh headers.

    Returns (depth, lowest replaced height) where depth is how many stored heights
    now have a different hash; (0, None) when nothing was replaced.
    """
    changed = []
    for h in headers:
        height = as_int(h.get("height"))
        old = stored.get(height)
        new = h.get("hash")
        if old is not None and new and old != new:
            changed.append(height)
    if not changed:
        return 0, None
    return len(changed), min(changed)
