"""The annunciator wall: 12 lamps, always in this order.

state: ok | warn | bad | off. A lamp fed by an active alert carries that alert's
level. `since` is when the lamp last left ok/off (null while ok or off).
"""

from __future__ import annotations

from .util import clock_text

LAMPS = [
    ("node", "NODE"), ("peers", "OUTGOING"), ("sync", "SYNCED"), ("block", "NEW BLOCK"),
    ("disk", "DISK SPACE"), ("reorg", "BLOCKS REPLACED"), ("double_spend", "DOUBLE SPEND"),
    ("restarts", "RESTARTS"), ("spread_out", "ECLIPSE · OUT"), ("spread_in", "ECLIPSE · IN"),
    ("tor", "TOR SENDING"), ("flood", "BLACK MARBLE"),
]
LAMP_IDS = [i for i, _ in LAMPS]
# lamp id -> alert ids that light it
LAMP_ALERTS = {
    "node": ("node_down",), "peers": ("peers",), "sync": ("syncing",), "block": ("no_blocks",),
    "disk": ("disk",), "reorg": ("reorg",), "double_spend": ("double_spend",), "restarts": ("restarts",),
    "spread_out": ("spread_out",), "spread_in": ("spread_in",), "tor": ("tor",), "flood": ("flood",),
}
RANK = {"bad": 3, "warn": 2, "ok": 1, "off": 0}


def age_text(s) -> str:
    s = max(0, int(s))
    if s < 60:
        return "%d S AGO" % s
    if s < 120 * 60:
        return "%d MIN AGO" % (s // 60)
    if s < 48 * 3600:
        return "%d H AGO" % (s // 3600)
    return "%d DAYS AGO" % (s // 86400)


def _gb_text(b):
    gb = b // 1_000_000_000
    if gb >= 10000:
        return "%d TB FREE" % (gb // 1000)
    return "%d GB FREE" % gb


def compute(ctx: dict) -> list[tuple[str, str, str]]:
    """Return [(id, state, value)] in order. ctx holds the pieces of the snapshot.

    ctx keys: answering, restricted, node, chain, peers, alerts (dict id -> alert),
    tor_active, block_seen_age, restarts_1h, clock, now.
    """
    A = ctx.get("alerts") or {}
    node = ctx.get("node") or {}
    answering = ctx.get("answering")
    restricted = ctx.get("restricted")
    out = {}

    def alert_level(lamp_id):
        best = None
        for aid in LAMP_ALERTS[lamp_id]:
            a = A.get(aid)
            if a and (best is None or RANK[a["level"]] > RANK[best]):
                best = a["level"]
        return best

    def put(lamp_id, state, value):
        lvl = alert_level(lamp_id)
        if lvl and RANK[lvl] > RANK.get(state, 0):
            state = lvl
        out[lamp_id] = (state, value[:16])

    # ---- node
    if ctx.get("starting"):
        put("node", "off", "STARTING")
    elif not answering:
        put("node", "warn", "NOT ANSWERING")
    elif restricted:
        put("node", "ok", "RESTRICTED")
    else:
        put("node", "ok", "ANSWERING")

    live = answering and not ctx.get("starting")

    # ---- peers
    op, ow = node.get("out_peers"), node.get("out_wanted")
    if not live or op is None:
        put("peers", "off", "NO DATA")
    else:
        put("peers", "ok", "%d OF %d" % (op, ow or 0))

    # ---- sync
    if not live or node.get("synced") is None:
        put("sync", "off", "NO DATA")
    elif node.get("synced"):
        put("sync", "ok", "SYNCED")
    else:
        pct = node.get("sync_pct")
        put("sync", "ok", "SYNCING %.1f%%" % pct if pct is not None else "SYNCING")

    # ---- new block (same measure as the no_blocks alert)
    age = ctx.get("block_seen_age")
    if not live or age is None:
        put("block", "off", "NO DATA")
    else:
        put("block", "ok", age_text(age))

    # ---- disk
    free = node.get("free_bytes")
    if not live or free is None:
        put("disk", "off", "NO DATA")
    else:
        put("disk", "ok", _gb_text(free))

    # ---- reorgs
    chain = ctx.get("chain") or {}
    if not live:
        put("reorg", "off", "NO DATA")
    else:
        deep = ((chain.get("reorg") or {}).get("deepest_24h")) or 0
        put("reorg", "ok", "DEEPEST %d" % deep if deep else "NONE IN 24H")

    # ---- double spend
    if not live:
        put("double_spend", "off", "NO DATA")
    else:
        a = A.get("double_spend")
        put("double_spend", "ok", "SEEN %s" % clock_text(a["since"], ctx.get("clock", "12h")) if a else "NONE")

    # ---- restarts
    if not live or restricted:
        put("restarts", "off", "NO DATA")
    else:
        put("restarts", "ok", "%d IN 1 H" % (ctx.get("restarts_1h") or 0))

    # ---- eclipse watch
    spread = (ctx.get("peers") or {}).get("spread")
    for lamp_id, key in (("spread_out", "out16"), ("spread_in", "in24")):
        cl = (spread or {}).get(key) if spread else None
        if not live or cl is None:
            put(lamp_id, "off", "NO DATA")
        else:
            n = cl.get("n") or 0
            put(lamp_id, "ok", "MOST %d FROM ONE" % n if n else "NONE")   # one company's servers

    # ---- Tor sending
    tor_out = node.get("tor_out")
    if not ctx.get("tor_active"):
        put("tor", "off", "OFF")
    elif not live or tor_out is None:
        put("tor", "off", "NO DATA")
    else:
        put("tor", "ok", "%d TOR PEER%s" % (tor_out, "" if tor_out == 1 else "S"))

    # ---- black marble watch: what a new transaction's 16-member ring works like (flood.py)
    fl = chain.get("flood") or {}
    st = fl.get("state")
    if not live:
        put("flood", "off", "NO DATA")
    elif st == "ok" and isinstance(fl.get("rings"), (int, float)):
        put("flood", "ok", "RING ≈ %d OF 16" % round(fl["rings"]))
    elif st == "filling":
        put("flood", "off", "READING %d%%" % round((fl.get("progress") or 0) * 100))
    elif st == "off" and fl.get("why") in ("sync", "bootstrap"):
        put("flood", "off", "WAITS FOR SYNC")
    elif st == "off" and fl.get("why") == "short":
        put("flood", "off", "CHAIN TOO SHORT")
    elif st == "error":
        put("flood", "off", "CAN'T READ")
    else:
        put("flood", "off", "NO DATA")

    return [(i, out[i][0], out[i][1]) for i in LAMP_IDS]


class LampTracker:
    """Remembers when each lamp last left ok/off, for the `since` field."""

    def __init__(self):
        self._since: dict = {}

    def reset(self):
        self._since = {}

    def build(self, rows, now) -> list[dict]:
        """rows: (id, state, value) from compute()."""
        labels = dict(LAMPS)
        return [{"id": lamp_id, "label": labels[lamp_id], "state": state, "value": value,
                 "since": self._track(lamp_id, state, now)} for lamp_id, state, value in rows]

    def _track(self, lamp_id, state, now):
        if state in ("warn", "bad"):
            return self._since.setdefault(lamp_id, int(now))
        self._since.pop(lamp_id, None)
        return None
