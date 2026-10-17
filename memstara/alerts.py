"""Alert rules. Pure logic: the caller passes in observations and the time.

The core's rules read RPC. Add-ons' alerts come in as they give them (see
evaluate()); an add-on alert with the id of a core alert is ignored, so the
dashboard's own reading always wins. "Must last" grace periods are measured in poll
time: a condition's clock starts at the first poll that sees it, and the alert fires
once it has lasted the grace period.
"""

from __future__ import annotations

from .util import clock_style, clock_text

# every id the core raises itself: from the node's RPC, then from this computer's own
# readings (local reads), which go on whether or not the node answers
RPC_IDS = ("node_down", "peers", "syncing", "no_blocks", "disk", "reorg",
           "double_spend", "restarts", "spread_out", "spread_in", "tor", "flood")
COMPUTER_IDS = ("cpu_temp", "memory", "power")
DASHBOARD_IDS = RPC_IDS + COMPUTER_IDS

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
    "cpu_temp": "CPU HOT",
    "memory": "MEMORY ALMOST FULL",
    "power": "ON BATTERY",
}


def title_for(alert_id: str, level: str | None = None) -> str:
    if alert_id == "peers" and level == "bad":
        return "NO OUTGOING"
    if alert_id == "disk":
        return "DISK ALMOST FULL" if level == "bad" else "DISK GETTING FULL"
    if alert_id == "cpu_temp" and level == "bad":
        return "CPU VERY HOT"
    if alert_id == "power" and level == "bad":
        return "BATTERY LOW"
    return TITLES.get(alert_id) or str(alert_id).replace("_", " ").upper()[:30]


DEFAULT_CFG = {
    "out_wanted": 12, "low_peers": 8, "no_block_warn_min": 30, "no_block_bad_min": 60,
    "disk_warn_gb": 20, "disk_bad_gb": 5, "reorg_warn": 3, "reorg_bad": 10,
    "out16_warn": 5, "in24_warn": 4, "restart_warn": 3, "flood_warn": 11, "flood_bad": 8,
    "node_down_min": 5, "no_peers_min": 5, "peers_min": 10, "syncing_min": 20,
    "spread_min": 10, "tor_min": 10, "flood_min": 30,
    # this computer (no settings for these; web/js/panels/computer.js draws the same limits):
    # most processors slow themselves down to cool off at about 95 to 105 °C
    "temp_warn_c": 85, "temp_bad_c": 95, "mem_warn_pct": 90, "battery_bad_pct": 20,
    "hot_min": 2, "memory_min": 5, "power_min": 1,
    "clock": "12h", "temp_unit": "auto",
}


def cfg_from_settings(values: dict) -> dict:
    cfg = dict(DEFAULT_CFG)
    for k in list(cfg):
        for sec in ("thresholds.", "grace."):
            if sec + k in values:
                cfg[k] = values[sec + k]
    cfg["clock"] = clock_style(values)
    cfg["temp_unit"] = values.get("display.temp_unit", "auto")
    return cfg


def temp_text(c: float, unit: str = "auto") -> str:
    """A temperature as the settings show it. "auto" is decided by the screens' language
    (the computer's), which the server doesn't see, so it gives both."""
    f = round(c * 9 / 5 + 32)
    if unit == "f":
        return "%d °F" % f
    if unit == "c":
        return "%d °C" % round(c)
    return "%d °F (%d °C)" % (f, round(c))


def tor_sending_active(setting: str, tor_seen: bool, tx_proxy_tor) -> bool:
    """Is Tor sending (monerod --tx-proxy tor,...) on, as far as the dashboard can tell?

    "on"/"off" are taken as given. "auto": when the running monerod was found, its flags
    decide (tx_proxy_tor True or False, from its flags or config file; an I2P-only --tx-proxy
    is False); when it wasn't (None), on once outgoing Tor peers have been seen since the
    node last started (tor_seen).
    """
    if setting == "on":
        return True
    if setting != "auto":
        return False
    if tx_proxy_tor is not None:
        return tx_proxy_tor is True
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
        "computer": None,       # snapshot.host.computer, or None when this computer isn't read
        "addon_alerts": {},     # {add-on name: [alert] or None}; see AlertEngine.evaluate
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
        self._addon_first: dict = {}   # (add-on, id) -> first seen, for alerts that bring no `since`

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
        """Returns (active alerts worst-first, events).

        obs["addon_alerts"] is {add-on name: list or None}, as checked by addons.Addons.alerts.
        An add-on's alert is shown as given (`source` is the add-on's name); one with the id of
        a core alert, or of an alert another add-on (earlier in name order) raised, is ignored.
        None means the add-on can't tell right now: its alerts stay as they are, neither shown
        nor cleared. {"id": X, "hold": true} does the same for one alert. An add-on missing from
        the dict (turned off, removed) loses its alerts without "Cleared" events.
        """
        mins = lambda secs: int(secs // 60)
        grace = lambda key: cfg[key] * 60.0
        found: dict = {}
        paused: set = set()

        def add(aid, level, text, since, ips=None, source="dashboard", title=None):
            found[aid] = {"id": aid, "level": level, "title": title or title_for(aid, level), "text": text,
                          "since": int(since), "source": source, "ips": ips}

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
        self._computer_rules(obs.get("computer"), cfg, now, mins, grace, add, paused)

        # ---- add-ons' alerts, shown as they give them
        from_addons = obs.get("addon_alerts") or {}
        for name, items in from_addons.items():
            mine = {aid for aid, a in self._active.items() if a["source"] == name}
            if items is None:
                paused.update(mine)          # first-seen times are kept meanwhile
                continue
            seen = set()
            for a in items:
                aid = a.get("id")
                if not aid or aid in DASHBOARD_IDS:
                    continue
                if a.get("hold"):
                    if aid in mine and aid not in found:
                        paused.add(aid)
                        seen.add(aid)
                    continue
                level = a.get("level") if a.get("level") in ("warn", "bad") else "warn"
                if aid in found and (found[aid]["source"] != name
                                     or LEVEL_RANK[found[aid]["level"]] <= LEVEL_RANK[level]):
                    continue
                if (aid in self._active and self._active[aid]["source"] not in (name, "dashboard")
                        and self._active[aid]["source"] in from_addons):
                    continue                 # another add-on raised this id first
                seen.add(aid)
                since = a.get("since")
                if not isinstance(since, (int, float)):
                    since = self._addon_first.setdefault((name, aid), now)
                add(aid, level, a.get("text") or "", since, a.get("ips"), name, a.get("title"))
            for key in [k for k in self._addon_first if k[0] == name and k[1] not in seen]:
                del self._addon_first[key]

        # ---- events on raise / clear, paused alerts neither clear nor re-announce
        events = []
        prev = {aid: a for aid, a in self._active.items()
                if a["source"] == "dashboard" or a["source"] in from_addons}
        for key in [k for k in self._addon_first if k[0] not in from_addons]:
            del self._addon_first[key]       # an add-on turned off: its alerts go quietly
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

    def _computer_rules(self, pc, cfg, now, mins, grace, add, paused):
        """This computer's own readings: CPU temperature, memory and power."""
        if pc is None:
            paused.update(COMPUTER_IDS)
            self._drop("hot", "hot_bad", "memory", "power", "power_low")
            return
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)

        # ---- CPU temperature (the hottest sensor)
        c = pc.get("cpu_temp_c")
        hot = self._held("hot", num(c) and c >= cfg["temp_warn_c"], now)
        very = self._held("hot_bad", num(c) and c >= cfg["temp_bad_c"], now)
        if very is not None and very >= grace("hot_min"):
            add("cpu_temp", "bad", "The CPU has been at or above %s for %d min and reads %s now: most "
                "processors slow themselves down to cool off at this heat" % (
                    temp_text(cfg["temp_bad_c"], cfg["temp_unit"]), mins(very), temp_text(c, cfg["temp_unit"])),
                now - very)
        elif hot is not None and hot >= grace("hot_min"):
            add("cpu_temp", "warn", "The CPU has been at or above %s for %d min and reads %s now" % (
                temp_text(cfg["temp_warn_c"], cfg["temp_unit"]), mins(hot), temp_text(c, cfg["temp_unit"])),
                now - hot)

        # ---- memory in use
        used, total = pc.get("mem_used_bytes"), pc.get("mem_total_bytes")
        share = used / total * 100 if num(used) and num(total) and total > 0 else None
        m = self._held("memory", share is not None and share >= cfg["mem_warn_pct"], now)
        if m is not None and m >= grace("memory_min"):
            add("memory", "warn", "%d%% of this computer's memory has been in use for %d min" % (share, mins(m)),
                now - m)

        # ---- power: unplugged is a heads-up, a low battery while unplugged a problem
        pct = pc.get("battery_pct")
        unplugged = pc.get("on_ac") is False
        u = self._held("power", unplugged, now)
        low = self._held("power_low", unplugged and num(pct) and pct < cfg["battery_bad_pct"], now)
        if low is not None and low >= grace("power_min"):
            add("power", "bad", "This computer is on battery with %d%% left: the node stops when it runs out"
                % pct, now - low)
        elif u is not None and u >= grace("power_min"):
            add("power", "warn", "This computer has been on battery for %d min%s" % (
                mins(u), " (%d%% left)" % pct if num(pct) else ""), now - u)

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
