"""The poller: one thread that reads the node, runs the alert rules, records history
and events, and keeps the latest snapshot. The app window only ever reads that snapshot.

Each poll: RPC (every source.refresh_s), the lines monerod's log gained since the last
poll (logevents.py), then the alert rules, the lamps and history. The black marble
watch (flood.py) reads a year of output counts once, a part per poll, then only the
newest blocks.

Each new snapshot gets the next number (snapshot["seq"]). The window asks for the one
after the number it has (snapshot_json_after), and that answer goes out the moment the
poll that makes it ends, so a reading reaches the screen as soon as it's in.

A new block is dated by when this node got it (between the poll that first shows it
and the one before, so within a poll's time), not only by the miner's stamp in its
header: miners stamp a block when they start work on it, which can be minutes before
they find it (monerod's create_block_template sets the timestamp to time(NULL)).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from pathlib import Path

from . import VERSION, adapter
from .alerts import AlertEngine, cfg_from_settings, empty_obs, tor_sending_active
from .flood import FloodWatch
from .history import History
from .lamps import LampTracker, compute as compute_lamps
from .logevents import NodeLog
from .pool import MINED_KEPT, PoolList
from .rpc import RpcClient, RpcError, on_this_computer
from .settings import DEFAULTS, node_log_path
from .spylist import SpyList
from .util import clock_style, duration_text

SLOW_CALLS = {  # method -> seconds between calls
    "get_bans": 60,
    "get_fee_estimate": 60,
    "hard_fork_info": 600,
    "get_limit": 600,
    "get_version": 600,
}
# every RPC call the poller makes (all read-only); the commands add their own. Then three that
# read each waiting transaction and which ones a new block took (pool.py), and the outputs per
# block for the black marble watch (flood.py)
POLLER_CALLS = ("get_info", "get_last_block_header", "get_block_headers_range", "get_connections",
                "get_transaction_pool_stats", "get_net_stats", "sync_info") + tuple(SLOW_CALLS) + (
                "get_transaction_pool_hashes", "get_transactions", "get_block", "get_output_distribution")
# the ones monerod passes on to its bootstrap node while it uses one (use_bootstrap_daemon_if_necessary
# in core_rpc_server.cpp): not sent then, since the answer would be that node's, not yours
BOOTSTRAP_FORWARDED = {"get_last_block_header", "get_block_headers_range", "get_transaction_pool_stats",
                       "hard_fork_info", "get_fee_estimate", "get_limit", "get_version",
                       "get_transaction_pool_hashes", "get_transactions", "get_block", "get_output_distribution"}
REORG_MAP_SIZE = 100
ARRIVAL_MAX_NEW = 3         # more new (or replaced) blocks than this at once is catching up: no arrival times
STEADY_MIN_S = 5            # arrival times only when the poll before answered within max(this, 3 polls)
MAX_EVENTS = 200
PEER_EVENTS_EVERY_S = 5     # peers joining and leaving go in one event per this many seconds at most
SPY_EVENT_EVERY_S = 3600    # a peer on the spy node list connecting: one event per address per hour at most
SPY_LONG_S = 120            # one first read after this long connected "is connected (for ...)", not "connected"
MAX_WAIT_S = 20             # the longest the window's request for a newer snapshot is held
LOG_EVENTS_PER_READ = 20    # monerod's log lines shown as events per poll; the rest are counted
MONEROD_OUT_PEERS = 12      # monerod's own default (P2P_DEFAULT_CONNECTIONS_COUNT, cryptonote_config.h)

NODE_KEYS = ["version", "nettype", "started", "uptime_s", "height", "target_height", "synced", "sync_pct",
             "top_hash", "last_block_time", "last_block_seen", "last_block_age_s", "difficulty", "hashrate",
             "out_peers",
             "out_wanted", "out_wanted_from", "in_peers", "tor_out", "rpc_connections", "white_peers",
             "grey_peers", "tx_pool", "alt_blocks", "free_bytes", "db_bytes", "update_available", "hf_version",
             "rpc_version", "bans", "ban_ranges", "ban_list", "bans_long", "fee_per_byte", "limit_up", "limit_down"]
# readings that only mean something while the node answers
LIVE_NODE_KEYS = {"uptime_s", "synced", "out_peers", "in_peers", "tor_out", "rpc_connections", "tx_pool"}


def source_url(values: dict) -> str:
    return values["source.rpc_url"]


def test_connection(url: str, user: str, password: str, timeout: float) -> dict:
    """POST /api/test-connection: one get_info, nothing else."""
    out = {"ok": False, "version": None, "height": None, "restricted": None, "latency_ms": None, "error": None}
    try:
        client = RpcClient(url, user, password, timeout)
    except ValueError as e:
        out["error"] = str(e)
        return out
    try:
        info = client.call("get_info")
    except RpcError as e:
        out["error"] = e.summary
        return out
    out.update(ok=True, version=info.get("version") or None, height=info.get("height"),
               restricted=bool(info.get("restricted")), latency_ms=client.last_ms)
    return out


class Poller:
    def __init__(self, settings, *, clock=time.time, state_dir: Path | None = None, log=None,
                 node_log=None, spy=None):
        self.settings = settings
        self.clock = clock
        self.state_dir = Path(state_dir) if state_dir else None
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.node_log = node_log or NodeLog(settings.get("node.log_path"))   # tests pass a stand-in
        self.spy = spy or SpyList()   # the bundled spy node list (spylist.py), read once
        self._lock = threading.Lock()
        self._new_snapshot = threading.Condition(self._lock)
        self._seq = 0                 # the number of the latest snapshot
        self._mono0 = time.monotonic()   # when the poll under way started, on a clock that never jumps
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.events: deque = deque(maxlen=MAX_EVENTS)
        self._event_n = 0
        self.engine = AlertEngine()
        self.lamps = LampTracker()
        self.history = History(self.state_dir / "history.json" if self.state_dir else None)
        self.history.load(self.clock())
        self._source_key = None
        self._reset_source()
        snap = self._starting_snapshot()
        snap["seq"] = 0
        self._snapshot = snap
        self._snapshot_json = json.dumps(snap, separators=(",", ":")).encode()

    # ------------------------------------------------------------------ public
    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot

    def snapshot_json(self) -> bytes:
        with self._lock:
            return self._snapshot_json

    def snapshot_pair(self) -> tuple[dict, bytes]:
        """The latest snapshot and its JSON, from the same poll."""
        with self._lock:
            return self._snapshot, self._snapshot_json

    def snapshot_after(self, seq, wait: float) -> tuple[dict, bytes]:
        """The latest snapshot (and its JSON) once its number isn't `seq` any more: at once
        if it already isn't (or `seq` is None), else as soon as the next poll ends, or after
        `wait` seconds (at most MAX_WAIT_S) with the same one."""
        deadline = time.monotonic() + max(0.0, min(float(wait), MAX_WAIT_S))
        with self._new_snapshot:
            while seq is not None and self._seq == seq and not self._stop.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._new_snapshot.wait(left)
            return self._snapshot, self._snapshot_json

    def snapshot_json_after(self, seq, wait: float) -> bytes:
        return self.snapshot_after(seq, wait)[1]

    def history_query(self, window: int) -> dict:
        return self.history.query(window, self.clock(), self.settings.get("source.refresh_s"))

    def add_event(self, kind: str, text: str, level: str = "info", t: float | None = None):
        self._push_event({"t": int(t if t is not None else self.clock()), "level": level,
                          "kind": kind, "text": text})

    def _push_event(self, ev: dict):
        """Newest first, each numbered one up from the last, so the window can add only
        the new ones to its list (two can share a second and a text)."""
        self._event_n += 1
        self.events.appendleft(dict(ev, id=self._event_n))

    def wake(self):
        self._wake.set()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="memstara-poller", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        with self._new_snapshot:
            self._new_snapshot.notify_all()   # let held requests for a newer snapshot go
        # save first: a poll waiting on a slow node can't hold it up, and at logout the
        # desktop gives the app only a few seconds before ending it
        self._save_history()
        if self._thread is not None:
            self._thread.join(timeout=2)   # a daemon thread: one still stuck doesn't keep the app open
            self._thread = None
        self._save_history()   # with what the last poll added

    def _save_history(self):
        try:
            self.history.maybe_save(self.clock(), force=True)
        except Exception:
            pass

    def _run(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.poll_once()
            except Exception as e:  # never let one bad answer stop the poller
                self.log("poller: unexpected %s while reading the node" % type(e).__name__)
            refresh = float(self.settings.get("source.refresh_s") or 1)
            wait = max(0.2, refresh - (time.monotonic() - t0))
            self._wake.wait(wait)
            self._wake.clear()

    # ------------------------------------------------------------------ state
    def _reset_source(self):
        self.client = None
        self.elsewhere_said = False      # the notes for an RPC address not on this computer, once per node
        self.nologin_said = False        # and the one for its answering with no login, once it answers
        self.answering_prev = None
        self.restricted_prev = None
        self.last_ok = None
        self.start_prev = None
        self.restarts: list = []
        self.net_prev = None
        self.chain_map: dict = {}
        self.top_hash_prev = None
        self.top_height_prev = None
        self.seen_time = None
        self.block_seen: dict = {}       # block hash -> when this node got it (only blocks seen arriving)
        self.pool = PoolList(self.log)   # each waiting transaction
        self.flood = FloodWatch(self.log)   # outputs per block, for the black marble watch
        self.headers: list = []
        self.blocks_shown_prev = None
        self.reorgs: list = []
        self.slow: dict = {}
        self.slow_at: dict = {}
        self.slow_t: dict = {}
        self.tor_seen = False
        self.peer_ids_prev = None
        self.peer_joined: set = set()    # joined and left since the last peer event
        self.peer_left: set = set()
        self.peer_event_t = None
        self.spy_prev: set = set()      # addresses on the spy node list connected at the last reading
        self.spy_said: dict = {}        # address -> when its last spy-list event went out
        self.synced_prev = None
        self.bootstrap_prev = None     # the bootstrap node answering get_info last time, or None
        self.node_last: dict = {}

    # ------------------------------------------------------------------ one poll
    def poll_once(self, now: float | None = None) -> dict:
        now = float(self.clock() if now is None else now)
        vals = self.settings.values()
        url = source_url(vals)
        user, password = vals["source.rpc_user"], vals["source.rpc_password"]
        key = (url, user, password, vals["source.timeout_s"])
        if key != self._source_key:
            first = self._source_key is None
            same_node = not first and self._source_key[0] == url
            if not same_node:
                # another node: forget everything learnt about the old one
                self._reset_source()
                self.engine.reset()
                self.lamps.reset()
                self.add_event("source", "%s %s" % ("Reading" if first else "Now reading", _hostport(url)),
                               "info", now)
            self._source_key = key
            try:
                self.client = RpcClient(url, user, password, vals["source.timeout_s"])
            except ValueError:
                self.client = None

        self._mono0 = time.monotonic()
        snap_parts = self._read_node(vals, now)
        snap = self._finish(vals, now, url, snap_parts)
        snap["clock_style"] = clock_style(vals)   # how its clock times were written (privacy.py checks it)
        seq = self._seq + 1               # only this thread changes it
        snap["seq"] = seq
        data = json.dumps(snap, separators=(",", ":")).encode()
        with self._new_snapshot:
            self._snapshot = snap
            self._snapshot_json = data
            self._seq = seq
            self._new_snapshot.notify_all()
        self.history.maybe_save(now)
        return snap

    def _call(self, method, params=None):
        return self.client.call(method, params)

    def _since_start(self) -> float:
        """Seconds since this poll started (its `now`), on a clock that never jumps."""
        return max(0.0, time.monotonic() - self._mono0)

    def _read_node(self, vals, now):
        """All RPC reading for one poll. Returns a dict of parts."""
        parts = {"answering": False, "error": None, "error_short": None, "latency": None,
                 "restricted": False, "node": None, "peers": adapter.empty_peers(),
                 "mempool": adapter.empty_pool(), "traffic": {"bytes_in": None, "bytes_out": None,
                                                              "rate_in": None, "rate_out": None},
                 "sync": None, "bootstrap": None}
        if self.client is None:
            parts["error"] = "The RPC address isn't valid"
            parts["error_short"] = "bad address"
            return parts
        try:
            info = self._call("get_info")
        except RpcError as e:
            parts["error"] = e.summary
            parts["error_short"] = e.short
            self._went_quiet(now, e)
            return parts
        parts["answering"] = True
        parts["latency"] = self.client.last_ms
        # the poll before answered recently: a block that's new now arrived since then
        refresh = float(vals["source.refresh_s"] or 1)
        steady = self.last_ok is not None and 0 <= now - self.last_ok <= max(STEADY_MIN_S, 3 * refresh)
        self.last_ok = now
        node = adapter.parse_info(info, now, vals["thresholds.out_wanted"])
        restricted = node.pop("_restricted")
        median_weight = node.pop("_median_weight")
        max_weight = node.pop("_max_weight")
        target_s = node.pop("_target_s")
        bootstrap = node.pop("_bootstrap")
        if bootstrap is not None:
            restricted = bool(self.restricted_prev)   # that node's answer says nothing about this port
        parts["restricted"] = restricted
        parts["median_weight"] = median_weight
        parts["max_weight"] = max_weight
        parts["target_s"] = target_s
        parts["bootstrap"] = bootstrap
        if self.answering_prev is None:
            pass
        elif self.answering_prev is False:
            self.add_event("source", "Node answering again", "ok", now)
            self.slow_at = {}   # refresh the slow readings after an outage
        self.answering_prev = True
        self._bootstrap_events(bootstrap, now)
        if restricted and not self.restricted_prev:
            self.add_event("source", "This is a restricted RPC port, so monerod hides peers, bans, traffic and "
                                     "disk space here. To see them, use its unrestricted port", "info", now)
        if bootstrap is None:
            self.restricted_prev = restricted

        # ---- top block
        shown = vals["chain.blocks_shown"]
        top_height = (node["height"] or 1) - 1
        if bootstrap is not None:
            # every block read would come from the bootstrap node: only the height is this node's,
            # and the tape and reorg watch wait until it reads its own chain again
            if self.seen_time is None or top_height != self.top_height_prev:
                self.seen_time = now
            self.top_hash_prev, self.top_height_prev = None, top_height
        else:
            top_hash, top_time = node.get("top_hash"), None
            try:
                lh = self._call("get_last_block_header")
                lh = (lh.get("block_header") or {}) if _own(lh) else {}
                if isinstance(lh, dict) and lh.get("hash"):
                    top_hash = lh.get("hash")
                    top_time = lh.get("timestamp")
                    if isinstance(lh.get("height"), int):
                        top_height = lh["height"]
            except RpcError:
                pass
            node["top_hash"] = top_hash

            # ---- headers: when the top changes, on start, or when the tape length changes
            if (top_hash != self.top_hash_prev or not self.headers or shown != self.blocks_shown_prev
                    or top_height != self.top_height_prev):
                self._read_headers(top_height, shown, now, parts, vals,
                                   steady and node.get("synced") is not False and self.synced_prev is not False)
            if top_time is None and self.headers:
                top_time = self.headers[-1].get("timestamp")
            node["last_block_time"] = top_time
            # how long since the last block: from when this node got it, when the dashboard saw
            # it arrive, else from the miner's stamp
            seen = self.block_seen.get(top_hash) if top_hash else None
            node["last_block_seen"] = round(seen, 1) if seen is not None else None
            ref = seen if seen is not None else top_time
            node["last_block_age_s"] = max(0, int(now - ref)) if ref else None
            if self.top_hash_prev is None or top_hash != self.top_hash_prev or top_height != self.top_height_prev:
                if self.seen_time is None and top_time:
                    self.seen_time = min(now, float(top_time))
                else:
                    self.seen_time = now
            self.top_hash_prev, self.top_height_prev = top_hash, top_height
            self.blocks_shown_prev = shown

        # ---- peers
        if not restricted:
            try:
                parts["peers"] = adapter.parse_connections(self._call("get_connections"))
                self.spy.mark(parts["peers"]["list"])
                self._peer_events(parts["peers"]["list"], now)
                self._spy_events(parts["peers"]["list"], now)
            except RpcError:
                self._forget_peers()
        if bootstrap is not None and parts["peers"]["out_by_kind"]:
            # get_info's counts were the bootstrap node's; get_connections is this node's own
            ob, ib = parts["peers"]["out_by_kind"], parts["peers"]["in_by_kind"]
            node["out_peers"], node["in_peers"] = ob["ipv4"] + ob["ipv6"], ib["ipv4"] + ib["ipv6"]
        # ---- mempool: the totals, then each waiting transaction (what changed since the last poll)
        if bootstrap is None:
            try:
                res = self._call("get_transaction_pool_stats")
                if _own(res):
                    parts["mempool"] = adapter.parse_pool(res, now)
            except RpcError:
                pass
            if parts["mempool"].get("txs") is not None:
                self.pool.update(self._call, parts["mempool"], now)
                parts["mempool"].update(self.pool.part())
        # ---- traffic: the rate is timed by when each total was read (the middle of its call),
        # not by when the poll started, so a slow read earlier in a poll doesn't make it jump
        if not restricted:
            try:
                t_ask = self._since_start()
                ns = self._call("get_net_stats")
                t_net = now + (t_ask + self._since_start()) / 2
                bi, bo = ns.get("total_bytes_in"), ns.get("total_bytes_out")
                st = ns.get("start_time")
                tr = parts["traffic"]
                tr["bytes_in"], tr["bytes_out"] = bi, bo
                p = self.net_prev
                if (p and isinstance(bi, int) and isinstance(bo, int) and p[3] == st and t_net > p[0]
                        and bi >= p[1] and bo >= p[2]):
                    dt = t_net - p[0]
                    tr["rate_in"] = int(round((bi - p[1]) / dt))
                    tr["rate_out"] = int(round((bo - p[2]) / dt))
                self.net_prev = (t_net, bi, bo, st) if isinstance(bi, int) and isinstance(bo, int) else None
            except RpcError:
                self.net_prev = None
        # ---- sync progress while not synced
        if node["synced"] is False and not restricted:
            try:
                parts["sync"] = adapter.parse_sync_info(self._call("sync_info"))
            except RpcError:
                parts["sync"] = None
        # ---- slow readings
        for method, every in SLOW_CALLS.items():
            if method in ("get_bans",) and restricted:
                continue
            if bootstrap is not None and method in BOOTSTRAP_FORWARDED:
                continue          # the last reading of the node's own stays; read again once it's back
            if now - self.slow_at.get(method, -1e18) < every:
                continue
            self.slow_at[method] = now
            try:
                res = self._call(method)
                if _own(res):
                    self.slow[method] = res
                    self.slow_t[method] = now
            except RpcError:
                pass
        hf = self.slow.get("hard_fork_info") or {}
        node["hf_version"] = hf.get("version") if isinstance(hf.get("version"), int) else None
        node["rpc_version"] = adapter.rpc_version_text((self.slow.get("get_version") or {}).get("version"))
        if "get_bans" in self.slow and not restricted:
            node["bans"], node["ban_ranges"] = adapter.parse_bans(self.slow["get_bans"])
            node["ban_list"], node["bans_long"] = adapter.parse_ban_list(
                self.slow["get_bans"], now - self.slow_t.get("get_bans", now))
        else:
            node["bans"] = node["ban_ranges"] = node["ban_list"] = node["bans_long"] = None
        node["fee_per_byte"] = adapter.parse_fees(self.slow.get("get_fee_estimate") or {})
        lim = self.slow.get("get_limit")
        node["limit_up"], node["limit_down"] = adapter.parse_limit(lim) if lim else (None, None)

        # ---- restarts: start_time changed
        started = node.get("started")
        if started and self.start_prev and started != self.start_prev:
            self.restarts.append(min(float(started), now))
            self.add_event("restart", "Node restarted at %s" % _clock(started, vals), "info", now)
            self.tor_seen = False     # Tor peers from before say nothing about how it runs now
        if started:
            self.start_prev = started
        self.restarts = [t for t in self.restarts if now - t < 86400]

        # ---- sync events
        synced = node["synced"]
        if synced is not None:
            if self.synced_prev is None and not synced:
                self.add_event("sync", "Syncing: height %s of %s" % (_n(node["height"]), _n(node["target_height"])),
                               "info", now)
            elif self.synced_prev is True and not synced:
                self.add_event("sync", "Fell behind: syncing again (height %s of %s)"
                               % (_n(node["height"]), _n(node["target_height"])), "warn", now)
            elif self.synced_prev is False and synced:
                self.add_event("sync", "Synced at height %s" % _n(node["height"]), "ok", now)
            self.synced_prev = synced

        # ---- black marble watch: outputs per block, only what's new (it waits out syncing)
        self.flood.update(self._call, top_height, node.get("top_hash"), synced, now, bootstrap=bootstrap is not None)
        parts["node"] = node
        return parts

    def _bootstrap_events(self, address, now):
        prev = self.bootstrap_prev
        if address is not None and address != prev:
            self.add_event("source", "Syncing with help from %s: its answers aren't shown as your node's"
                           % address, "info", now)
        elif address is None and prev is not None:
            self.add_event("source", "No longer syncing with help from %s: showing your node's own answers "
                                     "again" % prev, "info", now)
        self.bootstrap_prev = address

    def _went_quiet(self, now, err):
        if self.answering_prev is True:
            self.add_event("source", "Node stopped answering: %s" % err.short, "warn", now)
        elif self.answering_prev is None:
            self.add_event("source", "Node not answering: %s" % err.short, "warn", now)
        self.answering_prev = False
        self.net_prev = None
        self._forget_peers()

    def _read_headers(self, top_height, shown, now, parts, vals, steady=False):
        """The tape's headers. Blocks new since the last read (or replacing one) are dated
        `now` when `steady` (the node answered the poll before and isn't syncing) and only a
        few came at once; otherwise when they arrived isn't known."""
        n = max(shown + 1, REORG_MAP_SIZE)
        start = max(0, top_height - n + 1)
        try:
            res = self._call("get_block_headers_range", {"start_height": start, "end_height": top_height})
        except RpcError:
            return
        if not _own(res):
            return
        hs = adapter.parse_headers(res)
        if not hs:
            return
        depth, low = adapter.find_reorg(self.chain_map, hs)
        if depth:
            rec = {"time": int(now), "depth": depth, "height": low}
            self.reorgs.append(rec)
            level = "warn" if depth >= vals["thresholds.reorg_warn"] else "info"
            self.add_event("reorg", "Reorg: %d block%s replaced at height %s" % (depth, "" if depth == 1 else "s",
                                                                               _n(low)), level, now)
        # new blocks since the last poll
        prev_top = self.top_height_prev
        if prev_top is not None and self.chain_map:
            new = [h for h in hs if h["height"] > prev_top]
            fresh = [h for h in hs if h.get("hash") and (h["height"] > prev_top or (
                h["height"] in self.chain_map and self.chain_map[h["height"]] != h["hash"]))]
            if steady and 0 < len(fresh) <= ARRIVAL_MAX_NEW:
                for h in fresh:
                    self.block_seen[h["hash"]] = now
            if self.synced_prev is not False and 0 < len(fresh) <= 5:
                # which waiting transactions each took, so the window can send exactly those into it
                for h in fresh[-MINED_KEPT:]:
                    self.pool.block(self._call, h["height"])
            if self.synced_prev is not False and 0 < len(new) <= 5:
                for h in new:
                    w = h.get("block_weight") or h.get("block_size") or 0
                    self.add_event("block", "New block %s · %d tx%s · %d kB" % (
                        _n(h["height"]), h.get("num_txes") or 0, "" if h.get("num_txes") == 1 else "s",
                        int(round(w / 1000.0))), "info", now)
            elif len(new) > 5 and self.synced_prev is not False:
                self.add_event("block", "%d new blocks, now at height %s" % (len(new), _n(new[-1]["height"])),
                               "info", now)
        for h in hs:
            self.chain_map[h["height"]] = h.get("hash")
        live = {h.get("hash") for h in hs}
        for old in [k for k in self.block_seen if k not in live]:
            del self.block_seen[old]    # off the tape (or replaced)
        for old in [k for k in self.chain_map if k > top_height]:
            del self.chain_map[old]   # the chain got shorter
        if len(self.chain_map) > REORG_MAP_SIZE:
            for old in sorted(self.chain_map)[: len(self.chain_map) - REORG_MAP_SIZE]:
                del self.chain_map[old]
        self.headers = hs

    def _forget_peers(self):
        """The peer list couldn't be read: start over from the next one that can."""
        self.peer_ids_prev = None
        self.peer_joined, self.peer_left = set(), set()
        self.spy_prev = set()

    def _peer_events(self, peers, now):
        """Peers joining and leaving, as one event per PEER_EVENTS_EVERY_S at most, so
        reading every second doesn't fill the event list with one-peer events."""
        ids = {(p["dir"], p["id"], p["host"], p["port"]) for p in peers}
        prev = self.peer_ids_prev
        self.peer_ids_prev = ids
        if prev is None:
            return
        # a peer that left and came back (or the other way round) since the last event cancels out
        for x in ids - prev:
            if x in self.peer_left:
                self.peer_left.discard(x)
            else:
                self.peer_joined.add(x)
        for x in prev - ids:
            if x in self.peer_joined:
                self.peer_joined.discard(x)
            else:
                self.peer_left.add(x)
        if self.peer_event_t is not None and 0 <= now - self.peer_event_t < PEER_EVENTS_EVERY_S - 0.5:
            return
        joined, left = self.peer_joined, self.peer_left
        if not joined and not left:
            return
        self.peer_joined, self.peer_left, self.peer_event_t = set(), set(), now
        j_out = sum(1 for x in joined if x[0] == "out")
        j_in = len(joined) - j_out
        bits = []
        if joined:
            detail = " · ".join(s for s in ("%d out" % j_out if j_out else "", "%d in" % j_in if j_in else "") if s)
            bits.append("%d joined (%s)" % (len(joined), detail))
        if left:
            bits.append("%d left" % len(left))
        kind = "peer_out" if j_out else "peer_in" if j_in else "peer_gone"
        self.add_event(kind, " · ".join(bits), "info", now)

    def _spy_events(self, peers, now):
        """An event when a peer on the spy node list connects: once per address an hour at most,
        so one that keeps reconnecting doesn't fill the event list."""
        here = {}
        for p in peers:
            if p.get("spy"):
                here.setdefault(p["ip"], (p["dir"], p.get("age_s")))
        self.spy_said = {ip: t for ip, t in self.spy_said.items() if 0 <= now - t < SPY_EVENT_EVERY_S}
        for ip, (d, age) in sorted(here.items()):
            if ip in self.spy_prev or ip in self.spy_said:
                continue
            self.spy_said[ip] = now
            way = "outgoing" if d == "out" else "incoming"
            # one first read after it had been connected a while (at start, or after a gap) says for how long
            if isinstance(age, int) and age >= SPY_LONG_S:
                text = "A peer on the MRL spy node list is connected: %s (%s, for %s)" % (ip, way, duration_text(age))
            else:
                text = "A peer on the MRL spy node list connected: %s (%s)" % (ip, way)
            self.add_event("spy", text + ". Your node doesn't ban it", "warn", now)
        self.spy_prev = set(here)

    def _elsewhere_notes(self, vals, now, url, answering):
        """Once per node: that its RPC address isn't on this computer, and what anyone else on
        the network between could do when it's reached without a tunnel (the no-login note
        waits for a poll the node answers)."""
        if on_this_computer(url):
            return
        if not self.elsewhere_said:
            self.elsewhere_said = True
            self.add_event("source", "The RPC address %s isn't on this computer, so the node may run on another "
                           "one: its log is read only from a path set in Settings" % _hostport(url), "info", now)
            if url.lower().startswith("http://"):
                self.add_event("source", "The node is read over plain http across the network: anyone on that "
                               "network can read its answers, and on its unrestricted port could send it "
                               "commands. An SSH tunnel keeps it private (see the README)", "warn", now)
        if answering and not vals["source.rpc_user"] and not self.nologin_said:
            self.nologin_said = True
            self.add_event("source", "The node answers with no login. Anyone who can reach its unrestricted "
                           "port can change it: start monerod with --rpc-login and put the same name and "
                           "password in Settings", "warn", now)

    def _out_wanted(self, vals):
        """How many outgoing connections the node aims for, and where that number came from:
        the setting when it's above 0 ("setting"), else monerod's default ("default"). A limit
        set with --out-peers or the out_peers command isn't in the RPC answers; set the number
        in Settings then."""
        n = vals["thresholds.out_wanted"]
        if n:
            return n, "setting"
        return MONEROD_OUT_PEERS, "default"

    # ------------------------------------------------------------------ monerod's log
    def _log_events(self, vals, now):
        """monerod's log lines since the last poll, as events. While the RPC address isn't on this
        computer, the log at the default place may be another node's, so the log is read only from
        a path set by hand (a copy or a mounted folder)."""
        path = node_log_path(vals)
        if path == DEFAULTS["node.log_path"] and not on_this_computer(source_url(vals)):
            return
        self.node_log.path = path
        try:
            evs = self.node_log.poll(now)
        except Exception as e:   # logevents never raises; this is a last guard
            self.log("monerod's log: unexpected %s" % type(e).__name__)
            return
        extra = len(evs) - LOG_EVENTS_PER_READ
        if extra > 0:
            evs = evs[-LOG_EVENTS_PER_READ:]
        for ev in evs:              # oldest first, so the newest ends up on top
            self._push_event(ev)
        if extra > 0:
            self.add_event("log", "%d more lines from monerod's log aren't shown here (COMMANDS has the "
                                  "whole log)" % extra, "info", now)

    # ------------------------------------------------------------------ snapshot
    def _finish(self, vals, now, url, parts):
        answering = parts["answering"]
        restricted = parts["restricted"]
        node_new = parts["node"]
        if answering and node_new is not None:
            node = {k: node_new.get(k) for k in NODE_KEYS}
            self.node_last = dict(node)
        else:
            node = {k: (None if k in LIVE_NODE_KEYS else self.node_last.get(k)) for k in NODE_KEYS}
            ref = node.get("last_block_seen") or node.get("last_block_time")
            if ref:
                node["last_block_age_s"] = max(0, int(now - ref))
            if node.get("ban_list") is not None and "get_bans" in self.slow:
                # the last ban list read keeps counting down, so ended bans drop off
                node["ban_list"], node["bans_long"] = adapter.parse_ban_list(
                    self.slow["get_bans"], now - self.slow_t.get("get_bans", now))

        # ---- monerod's log
        self._log_events(vals, now)
        self._elsewhere_notes(vals, now, url, answering)
        node["out_wanted"], node["out_wanted_from"] = self._out_wanted(vals)

        # ---- Tor sending
        peers = parts["peers"]
        listed = peers.get("out_by_kind") is not None    # get_connections was read this time
        peers["spy"] = self.spy.part(peers["list"] if answering and listed else None,
                                     self.slow.get("get_bans") if not restricted else None)
        tor_count = peers["out_by_kind"]["tor"] if peers.get("out_by_kind") else None
        i2p_count = peers["out_by_kind"]["i2p"] if peers.get("out_by_kind") else 0
        if tor_count:
            self.tor_seen = True
        tor_active = tor_sending_active(vals["thresholds.tor_sending"], self.tor_seen)
        node["tor_out"] = tor_count if (answering and tor_active and tor_count is not None) else None

        # ---- chain
        shown = vals["chain.blocks_shown"]
        blocks = adapter.blocks_from_headers(self.headers, shown, self.block_seen) if self.headers else []
        recent = [r for r in self.reorgs if now - r["time"] < 86400]
        chain = {
            "blocks": blocks,
            "avg_interval_s": adapter.avg_interval(blocks),
            "median_weight": parts.get("median_weight") if answering else None,
            "max_weight": parts.get("max_weight") if answering else None,
            "wait_odds": adapter.wait_odds(node.get("last_block_age_s"), parts.get("target_s") or 120),
            # unknown (None) until the node has answered once: "none replaced" would be a guess
            "reorg": {"deepest_24h": max([r["depth"] for r in recent], default=0) if self.last_ok else None,
                      "last": dict(self.reorgs[-1]) if self.reorgs else None},
            "sync": parts.get("sync"),
            "flood": self.flood.part(),
        }
        source = {
            "mode": "live", "rpc": _hostport(url), "answering": answering,
            "error": None if answering else parts["error"], "latency_ms": parts["latency"],
            "restricted": restricted, "last_ok": int(self.last_ok) if self.last_ok else None,
            "poll_s": vals["source.refresh_s"], "dashboard_version": VERSION,
            "bootstrap": parts["bootstrap"] if answering else None,
        }

        # ---- alerts
        out_peers = node.get("out_peers") if answering and not restricted else None
        total_out = None
        if out_peers is not None:
            total_out = out_peers + (tor_count or 0) + (i2p_count or 0) if tor_count is not None else out_peers
        spread = peers.get("spread")
        obs = empty_obs()
        obs.update({
            "answering": answering, "error_short": parts["error_short"], "restricted": restricted,
            "out_peers": out_peers, "total_out": total_out,
            "tor_out": tor_count if answering else None, "tor_active": tor_active,
            "synced": node.get("synced") if answering else None,
            "height": node.get("height"), "target_height": node.get("target_height"),
            "block_seen_age": (now - self.seen_time) if (answering and self.seen_time is not None) else None,
            "free_bytes": node.get("free_bytes") if answering else None,
            "reorgs": self.reorgs, "double_spends": parts["mempool"].get("double_spends") if answering else None,
            "restarts": self.restarts if not restricted else [],
            "out16": spread["out16"] if spread else None, "in24": spread["in24"] if spread else None,
            "flood": chain["flood"] if answering else None,
            "spy": ([(p["dir"], p["ip"]) for p in peers["list"] if p.get("spy")]
                    if answering and listed and self.spy.state == "ok" else None),
        })
        cfg = cfg_from_settings(vals)
        cfg["out_wanted"] = node["out_wanted"]
        alerts, events = self.engine.evaluate(obs, cfg, now)
        for e in events:
            self._push_event(e)
        level = "bad" if any(a["level"] == "bad" for a in alerts) else "warn" if alerts else "ok"

        # ---- lamps
        rows = compute_lamps({
            "answering": answering, "restricted": restricted, "node": node, "chain": chain, "peers": peers,
            "alerts": {a["id"]: a for a in alerts if a["source"] == "dashboard"}, "tor_active": tor_active,
            "block_seen_age": obs["block_seen_age"], "clock": clock_style(vals),
            "restarts_1h": sum(1 for t in self.restarts if now - t < 3600), "now": now,
        })
        lamps = self.lamps.build(rows, now)

        # ---- history
        self.history.record(now, {
            "out": node.get("out_peers") if answering else None,
            "in": node.get("in_peers") if answering else None,
            "tor": node.get("tor_out") if answering else None,
            "pool": parts["mempool"].get("txs") if answering else None,
            "rate_in": parts["traffic"]["rate_in"], "rate_out": parts["traffic"]["rate_out"],
            "latency": parts["latency"],
        })

        return {
            "v": 1,
            "generated": int(now),
            "t": round(now, 3),           # when this poll started, to the millisecond
            "level": level,
            "source": source,
            "node": node,
            "chain": chain,
            "peers": peers,
            "mempool": parts["mempool"],
            "traffic": parts["traffic"],
            "alerts": alerts,
            "lamps": lamps,
            "events": list(self.events),
            # the history row still filling, this reading included: a chart's newest point
            "hist": self.history.current(),
        }

    def _starting_snapshot(self) -> dict:
        vals = self.settings.values()
        node = {k: None for k in NODE_KEYS}
        node["out_wanted"], node["out_wanted_from"] = self._out_wanted(vals)
        rows = compute_lamps({"starting": True, "answering": False, "node": node, "alerts": {}})
        return {
            "v": 1, "generated": int(self.clock()), "t": round(self.clock(), 3), "level": "ok",
            "source": {"mode": "live", "rpc": _hostport(source_url(vals)), "answering": False,
                       "error": "Starting: the first reading hasn't come in yet", "latency_ms": None,
                       "restricted": False, "last_ok": None, "poll_s": vals["source.refresh_s"],
                       "dashboard_version": VERSION, "bootstrap": None},
            "node": node,
            "chain": {"blocks": [], "avg_interval_s": None, "median_weight": None, "max_weight": None, "wait_odds": None,
                      "reorg": {"deepest_24h": None, "last": None}, "sync": None,
                      "flood": {"state": "off", "why": "starting"}},
            "peers": adapter.empty_peers(),
            "mempool": adapter.empty_pool(),
            "traffic": {"bytes_in": None, "bytes_out": None, "rate_in": None, "rate_out": None},
            "alerts": [],
            "lamps": LampTracker().build(rows, self.clock()),
            "events": [],
            "hist": None,
        }


def _own(res) -> bool:
    """Whether an answer is the node's own: monerod marks one its bootstrap node gave as untrusted."""
    return not (isinstance(res, dict) and res.get("untrusted") is True)


def _hostport(url: str) -> str:
    try:
        return RpcClient(url).hostport
    except ValueError:
        return ""


def _n(v):
    return format(v, ",") if isinstance(v, int) else "?"


def _clock(t, vals):
    from .util import clock_text
    return clock_text(t, clock_style(vals))
