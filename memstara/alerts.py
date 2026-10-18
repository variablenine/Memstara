"""Alert rules. Pure logic: the caller passes in observations and the time.

The rules read RPC. "Must last" grace periods are measured in poll
time: a condition's clock starts at the first poll that sees it, and the alert fires
once it has lasted the grace period.
"""

from __future__ import annotations

from .util import clock_style, clock_text

# every id the core raises itself, all from the node's RPC
RPC_IDS = ("node_down", "peers", "syncing", "no_blocks", "disk", "reorg",
           "double_spend", "restarts", "spread_out", "spread_in", "tor", "flood", "spy")

TITLES = {
    "node_down": "NODE NOT ANSWERING",
    "peers": "FEW OUTGOING",
    "syncing": "STILL SYNCING",
    "no_blocks": "NO NEW BLOCK",
    "disk": "DISK SPACE",
    "reorg": "BLOCKS REPLACED",
    "double_spend": "DOUBLE SPEND",
    "restarts": "RESTARTING A LOT",
    "spread_out": "ECLIPSE WATCH · OUT",
    "spread_in": "ECLIPSE WATCH · IN",
    "tor": "NO TOR CONNECTIONS",
    "flood": "BLACK MARBLE WATCH",
    "spy": "SPY NODE LIST",
}
SPY_HOLD_S = 600   # the spy-list alert stays up this long after the last listed peer left


def title_for(alert_id: str, level: str | None = None) -> str:
    if alert_id == "peers" and level == "bad":
        return "NO OUTGOING"
    if alert_id == "disk":
        return "DISK ALMOST FULL" if level == "bad" else "DISK GETTING FULL"
    return TITLES.get(alert_id) or str(alert_id).replace("_", " ").upper()[:30]


DEFAULT_CFG = {
    "out_wanted": 12, "low_peers": 8, "no_block_warn_min": 30, "no_block_bad_min": 60,
    "disk_warn_gb": 20, "disk_bad_gb": 5, "reorg_warn": 3, "reorg_bad": 10,
    "out16_warn": 5, "in24_warn": 4, "restart_warn": 3, "flood_warn": 11, "flood_bad": 8,
    "node_down_min": 5, "no_peers_min": 5, "peers_min": 10, "syncing_min": 20,
    "spread_min": 10, "tor_min": 10, "flood_min": 30,
    "clock": "12h",
}


def cfg_from_settings(values: dict) -> dict:
    cfg = dict(DEFAULT_CFG)
    for k in list(cfg):
        for sec in ("thresholds.", "grace."):
            if sec + k in values:
                cfg[k] = values[sec + k]
    cfg["clock"] = clock_style(values)
    return cfg


def tor_sending_active(setting: str, tor_seen: bool) -> bool:
    """Is Tor sending (monerod --tx-proxy tor,...) on, as far as the dashboard can tell?

    "on"/"off" are taken as given. "auto": on once outgoing Tor peers have been seen in
    get_connections since the node last started (tor_seen).
    """
    if setting == "on":
        return True
    if setting != "auto":
        return False
    return bool(tor_seen)


def flood_text(fl: dict) -> str:
    """The black marble watch's alert, from snapshot.chain.flood."""
    return ("A recent surge of new outputs would supply about %d%% of a new transaction's decoys. If one "
            "sender made them, its 16-member ring works like about %d" % (round(fl["share"] * 100), round(fl["rings"])))


def empty_obs() -> dict:
    return {
        "answering": False, "error_short": None, "restricted": False,
        "out_peers": None, "total_out": None, "tor_out": None, "tor_active": False,
        "synced": None, "height": None, "target_height": None,
        "block_seen_age": None, "free_bytes": None,
        "reorgs": [], "double_spends": None, "restarts": [],
        "out16": None, "in24": None,
        "flood": None,          # snapshot.chain.flood (flood.py)
        "spy": None,            # [(dir, ip)] of connected peers on the spy node list (spylist.py)
    }


LEVEL_RANK = {"bad": 0, "warn": 1}


class AlertEngine:
    def __init__(self):
        self.reset()

    def reset(self):
        self._since: dict = {}
        self._active: dict = {}
        self._ds_last = None   # (time, count) of the last double-spend sighting
        self._ds_first = None
        self._spy_last = None  # (time, [(dir, ip)]) of the last reading with a listed peer connected
        self._spy_first = None

    # condition clocks
    def _held(self, name, cond, now):
        if not cond:
            self._since.pop(name, None)
            return None
        start = self._since.setdefault(name, now)
        return now - start

    def _drop(self, *names):
        for n in names:
            self._since.pop(n, None)

    def evaluate(self, obs: dict, cfg: dict, now: float):
        """Returns (active alerts worst-first, events)."""
        mins = lambda secs: int(secs // 60)
        grace = lambda key: cfg[key] * 60.0
        found: dict = {}
        paused: set = set()

        def add(aid, level, text, since, ips=None):
            found[aid] = {"id": aid, "level": level, "title": title_for(aid, level), "text": text,
                          "since": int(since), "source": "dashboard", "ips": ips}

        # ---- node_down
        d = self._held("node_down", not obs.get("answering"), now)
        if d is not None and d >= grace("node_down_min"):
            reason = obs.get("error_short")
            add("node_down", "bad", "Your node isn't answering for %d min%s"
                % (mins(d), " (%s)" % reason if reason else ""), now - d)

        if not obs.get("answering"):
            # other RPC rules pause rather than firing on missing data
            paused.update(i for i in RPC_IDS if i != "node_down")
            self._drop("peers_zero", "peers_low", "syncing", "spread_out", "spread_in", "tor_zero", "disk")
        else:
            self._rpc_rules(obs, cfg, now, mins, grace, add, paused)

        # ---- events on raise / clear, paused alerts neither clear nor re-announce
        events = []
        prev = dict(self._active)
        new_active = dict(found)
        for aid in paused:
            if aid in prev and aid not in found:
                new_active[aid] = prev[aid]
        for aid, a in found.items():
            p = prev.get(aid)
            if p is None or p["level"] != a["level"]:
                events.append({"t": int(now), "level": a["level"], "kind": "alert",
                               "text": "%s: %s" % (a["title"], a["text"])})
        for aid, p in prev.items():
            if aid not in new_active:
                events.append({"t": int(now), "level": "ok", "kind": "clear",
                               "text": "Cleared: %s" % p["title"]})
        self._active = new_active
        out = [a for aid, a in new_active.items() if aid not in paused]
        out.sort(key=lambda a: (LEVEL_RANK.get(a["level"], 2), a["since"], a["id"]))
        return out, events

    def _rpc_rules(self, obs, cfg, now, mins, grace, add, paused):
        clock = cfg.get("clock", "12h")

        # ---- peers: none at all (bad), or few (heads-up)
        out = obs.get("out_peers")
        total = obs.get("total_out")
        if out is None:
            paused.add("peers")
            self._drop("peers_zero", "peers_low")
        else:
            if total is None:
                total = out
            z = self._held("peers_zero", total == 0, now)
            low = self._held("peers_low", out < cfg["low_peers"], now)
            if z is not None and z >= grace("no_peers_min"):
                add("peers", "bad", "No outgoing connections for %d min" % mins(z), now - z)
            elif low is not None and low >= grace("peers_min"):
                if out == 0 and total > 0:
                    text = "No outgoing connections except %d over Tor for %d min" % (total, mins(low))
                elif out == 0:
                    text = "No outgoing connections for %d min" % mins(low)
                else:
                    text = "Only %d of %d outgoing connections for %d min" % (out, cfg["out_wanted"], mins(low))
                add("peers", "warn", text, now - low)

        # ---- still syncing
        synced = obs.get("synced")
        if synced is None:
            paused.add("syncing")
            self._drop("syncing")
        else:
            s = self._held("syncing", not synced, now)
            if s is not None and s >= grace("syncing_min"):
                h, t = obs.get("height") or 0, obs.get("target_height") or 0
                add("syncing", "warn", "Still syncing after %d min (height %s of %s)"
                    % (mins(s), format(h, ","), format(t, ",")), now - s)

        # ---- no new block (time since this node last saw its top block change)
        age = obs.get("block_seen_age")
        if age is None:
            paused.add("no_blocks")
        else:
            m = mins(age)
            if age >= cfg["no_block_bad_min"] * 60.0:
                add("no_blocks", "bad", "No new block for %d min" % m, now - age)
            elif age >= cfg["no_block_warn_min"] * 60.0:
                add("no_blocks", "warn", "No new block for %d min" % m, now - age)

        # ---- disk space (GB = 10^9 bytes)
        free = obs.get("free_bytes")
        if free is None:
            paused.add("disk")
            self._drop("disk")
        else:
            gb = free // 1_000_000_000
            level = "bad" if free < cfg["disk_bad_gb"] * 1e9 else "warn" if free < cfg["disk_warn_gb"] * 1e9 else None
            d = self._held("disk", level is not None, now)
            if level:
                add("disk", level, "Only %d GB free on the blockchain disk" % gb, now - d)

        # ---- blocks replaced (stays up 24 h)
        recent = [r for r in obs.get("reorgs") or [] if now - r["time"] < 86400]
        if recent:
            deepest = max(recent, key=lambda r: (r["depth"], r["time"]))
            dep = deepest["depth"]
            base = "%d %s replaced at height %s (%s)" % (
                dep, "block" if dep == 1 else "blocks", format(deepest["height"] or 0, ","),
                clock_text(deepest["time"], clock))
            if dep >= cfg["reorg_bad"]:
                add("reorg", "bad", base + ": deeper than the 10-block lock, check recent incoming payments",
                    deepest["time"])
            elif dep >= cfg["reorg_warn"]:
                add("reorg", "warn", base + ": wait for extra confirmations on incoming payments", deepest["time"])

        # ---- double-spend attempts (stay up 30 min after the last sighting)
        ds = obs.get("double_spends")
        if ds:
            if self._ds_last is None or now - self._ds_last[0] >= 1800:
                self._ds_first = now
            self._ds_last = (now, ds)
        if self._ds_last is not None:
            if now - self._ds_last[0] < 1800:
                t, n = self._ds_last
                add("double_spend", "warn",
                    "%d double-spend attempt(s) seen in the mempool at %s: wait for extra confirmations "
                    "on incoming payments" % (n, clock_text(t, clock)), self._ds_first or t)
            else:
                self._ds_last = None
                self._ds_first = None

        # ---- restarts within the last hour
        rs = [t for t in obs.get("restarts") or [] if now - t < 3600]
        if len(rs) >= cfg["restart_warn"]:
            add("restarts", "warn", "Node restarted %d times in the last hour" % len(rs), min(rs))

        # ---- eclipse watch: clusters of peers from one network
        for aid, key, limit, text in (
                ("spread_out", "out16", cfg["out16_warn"],
                 "%d of your outgoing connections come from one company's servers: could be spy nodes"),
                ("spread_in", "in24", cfg["in24_warn"],
                 "%d incoming connections come from one company's servers: could be spy nodes")):
            cl = obs.get(key)
            if cl is None:
                paused.add(aid)
                self._drop(aid)
                continue
            n = cl.get("n") or 0
            d = self._held(aid, n >= limit, now)
            if d is not None and d >= grace("spread_min"):
                add(aid, "warn", text % n, now - d, list(cl.get("ips") or []))

        # ---- Tor sending on, but no Tor connections
        tor_out = obs.get("tor_out")
        if not obs.get("tor_active"):
            self._drop("tor_zero")
        elif tor_out is None:
            paused.add("tor")
            self._drop("tor_zero")
        else:
            d = self._held("tor_zero", tor_out == 0, now)
            if d is not None and d >= grace("tor_min"):
                add("tor", "warn", "No Tor connections for %d min: transactions you send will wait until "
                    "one connects" % mins(d), now - d)

        # ---- peers on the MRL spy node list: up while one is connected and SPY_HOLD_S after the
        # last one left, so a peer that keeps reconnecting is one alert, not one per visit
        spy = obs.get("spy")
        if spy is None:
            paused.add("spy")
        else:
            if spy:
                if self._spy_last is None or now - self._spy_last[0] >= SPY_HOLD_S:
                    self._spy_first = now
                self._spy_last = (now, list(spy))
            if self._spy_last is not None and now - self._spy_last[0] < SPY_HOLD_S:
                fix = ("Starting monerod with --enable-dns-blocklist (mainnet only, and with --proxy only "
                       "alongside --proxy-allow-dns-leaks) or with --ban-list and Memstara's copy bans the whole list")
                if spy:
                    n_out = sum(1 for d, _ in spy if d == "out")
                    bits = " · ".join(b for b in ("%d out" % n_out if n_out else "",
                                                  "%d in" % (len(spy) - n_out) if len(spy) > n_out else "") if b)
                    text = ("1 peer on the MRL spy node list is connected (%s). %s" % (bits, fix) if len(spy) == 1
                            else "%d peers on the MRL spy node list are connected (%s). %s" % (len(spy), bits, fix))
                else:
                    t, was = self._spy_last
                    text = "%s on the MRL spy node list %s connected at %s. %s" % (
                        "A peer" if len(was) == 1 else "Peers", "was" if len(was) == 1 else "were",
                        clock_text(t, clock), fix)
                add("spy", "warn", text, self._spy_first, [ip for _, ip in (spy or self._spy_last[1])])
            else:
                self._spy_last = self._spy_first = None

        # ---- black marble watch: a surge of new outputs that a new transaction's decoys would come from.
        # Without an estimate (the node catching up or restarting) its clocks are kept, not reset:
        # the estimate moves slowly, so one that comes back under the limit carries on as it was
        fl = obs.get("flood") or {}
        rings = fl.get("rings")
        if fl.get("state") != "ok" or not isinstance(rings, (int, float)):
            paused.add("flood")
        else:
            w = self._held("flood_warn", rings < cfg["flood_warn"], now)
            b = self._held("flood_bad", rings < cfg["flood_bad"], now)
            if b is not None and b >= grace("flood_min"):
                add("flood", "bad", flood_text(fl) + ": payments where privacy matters can wait for it to clear",
                    now - b)
            elif w is not None and w >= grace("flood_min"):
                add("flood", "warn", flood_text(fl), now - w)
