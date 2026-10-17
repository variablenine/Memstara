"""End to end: fake monerod -> poller -> web server -> /api/snapshot."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

import helpers
from helpers import WRITE, StubDiscover, StubHost, found_node, get_json, request
from fake_monerod import POOL_TX, FakeMonerod
from memstara.app import App
from memstara.host import HostReader
from memstara.lamps import LAMPS
from memstara.pool import FLAG_NOT_RELAYED, FLAG_TIME_GUESSED
from memstara.poller import BOOTSTRAP_FORWARDED, DISCOVER_EVERY_S, LOG_EVENTS_PER_READ, NODE_KEYS
from memstara.rpc import READ_ALLOWED
from memstara.settings import Settings

TOP_KEYS = ["v", "generated", "t", "level", "source", "node", "chain", "peers", "mempool", "traffic", "alerts",
            "lamps", "addons", "route", "host", "events", "hist", "clock_style", "seq"]
# everything the window gets about each waiting transaction (pool.PoolList.part)
POOL_LIST_KEYS = {"list", "list_wanted", "list_cut", "mined", "check"}
SOURCE_KEYS = {"mode", "rpc", "answering", "error", "latency_ms", "restricted", "last_ok", "poll_s",
               "host_name", "dashboard_version", "bootstrap"}
LAMP_IDS = ["node", "peers", "sync", "block", "disk", "reorg", "double_spend", "restarts", "spread_out", "spread_in",
            "tor", "flood"]
HOST_KEYS = {"ok", "error", "every_s", "t", "proc", "proc_error", "computer", "bind_if", "tor"}


def log_line(t, level, text, cat="global", loc="src/p2p/net_node.inl:340"):
    return "%s.123\t[P2P5]\t%s\t%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)), level, cat,
                                                 loc, text)


class Rig:
    """A fake node, the dashboard with no add-ons, and stand-ins for this computer's reads."""

    def __init__(self, restricted=False, found=None, cadence=(5, 10)):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.clock = helpers.FakeClock()
        self.fake = FakeMonerod(clock=self.clock, restricted=restricted).start()
        self.log = base / "bitmonero.log"
        self.log.write_text("")
        self.settings = Settings(base / "cfg" / "settings.json", log=lambda m: None).load()
        self.settings.update({"source.rpc_url": self.fake.url, "node.log_path": str(self.log)})
        if cadence:
            # the node every 5 s and the computer every 10 s: the cadence most tests count in
            # (OneSecondTest covers the defaults, every second)
            self.settings.update({"source.refresh_s": cadence[0], "local.every_s": cadence[1]})
        self.host = StubHost()
        self.discover = StubDiscover([found_node(self.fake.url, tx_proxy=True, tx_proxy_tor=True)]
                                     if found is None else found)
        self.logs = []
        self.app = App(self.settings, port=0, web_dir=base / "web", state_dir=base / "state", clock=self.clock,
                       log=self.logs.append, host_reader=self.host, discover=self.discover).start(poll=False)
        self.port = self.app.port

    def poll(self, advance=5):
        self.clock.advance(advance)
        self.app.poller.poll_once()
        st, snap = get_json(self.port, "/api/snapshot")
        assert st == 200
        return snap

    def write_log(self, *lines):
        with open(self.log, "a") as fh:
            fh.write("".join(lines))

    def close(self):
        self.app.stop()
        self.fake.stop()
        self.tmp.cleanup()


def alerts_by_id(snap):
    return {a["id"]: a for a in snap["alerts"]}


def lamps_by_id(snap):
    return {l["id"]: l for l in snap["lamps"]}


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()

    def tearDown(self):
        self.rig.close()

    def test_shape(self):
        rig = self.rig
        rig.poll(0)
        rig.fake.add_block(rig.clock() + 5)
        snap = rig.poll(5)
        self.assertEqual(list(snap), TOP_KEYS)
        self.assertEqual(snap["v"], 1)
        self.assertEqual(snap["generated"], int(rig.clock()))
        self.assertEqual(snap["t"], round(rig.clock(), 3))
        self.assertEqual(snap["level"], "ok")
        src = snap["source"]
        self.assertEqual(set(src), SOURCE_KEYS)
        self.assertEqual((src["mode"], src["answering"], src["error"], src["restricted"]),
                         ("live", True, None, False))
        self.assertEqual(src["rpc"], "127.0.0.1:%d" % rig.fake.port)
        self.assertEqual(src["dashboard_version"], "0.1.0")
        self.assertTrue(src["host_name"])
        self.assertEqual(src["poll_s"], 5)
        node = snap["node"]
        self.assertEqual(list(node), NODE_KEYS)
        self.assertEqual(node["height"], rig.fake.top + 1)
        self.assertEqual(node["out_peers"], 22)
        self.assertEqual(node["tor_out"], 3)
        self.assertEqual(node["hf_version"], 16)
        self.assertEqual(node["rpc_version"], "3.16")
        self.assertEqual(node["limit_up"], 2048 * 1024)
        self.assertEqual(node["last_block_age_s"], 0)
        self.assertEqual(node["last_block_seen"], round(rig.clock(), 1), "the new block was seen arriving")
        chain = snap["chain"]
        self.assertEqual(len(chain["blocks"]), 60)
        self.assertEqual(chain["blocks"][-1]["height"], rig.fake.top)
        self.assertEqual(chain["reorg"], {"deepest_24h": 0, "last": None})
        # the black marble watch reads a year of output counts, 40,000 blocks a poll
        self.assertEqual(chain["flood"], {"state": "filling", "progress": 0.3})
        self.assertEqual(chain["median_weight"], 300000)
        self.assertEqual(set(snap["peers"]), {"list", "spread", "out_by_kind", "in_by_kind"})
        self.assertEqual(len(snap["peers"]["list"]), 54)
        self.assertIn("histo_98pc", snap["mempool"])
        self.assertLessEqual(POOL_LIST_KEYS, set(snap["mempool"]))
        self.assertEqual([e[0] for e in snap["mempool"]["list"]], [POOL_TX[:16]])
        # the rates are timed by when each total was read, so the few milliseconds a poll takes
        # (real time, against the test's made-up clock) can move them a hair
        self.assertAlmostEqual(snap["traffic"]["rate_in"], 60000, delta=600)
        self.assertAlmostEqual(snap["traffic"]["rate_out"], 140000, delta=1400)
        self.assertEqual((snap["addons"], snap["route"]), ({}, []), "no add-ons in the core tests")
        self.assertEqual(set(snap["host"]), HOST_KEYS)
        self.assertEqual(snap["host"]["computer"]["cpu_temp_c"], 61.0)
        self.assertEqual(snap["host"]["every_s"], 10)
        for peer in snap["peers"]["list"]:
            self.assertEqual(peer["rtt_ms"], None if peer["kind"] in ("tor", "i2p") else 42.0, peer)
        self.assertEqual(snap["alerts"], [])
        bans = rig.fake.results["get_bans"]["bans"]
        self.assertEqual(node["bans"], len(bans))
        self.assertEqual(node["bans_long"], sum(1 for b in bans if b["seconds"] > 86400))
        self.assertEqual(node["ban_list"][0], {"host": "100.95.48.136", "seconds": 77754 - 5},
                         "longest first, counted down since get_bans was read 5 s ago")
        self.assertEqual([b["seconds"] for b in node["ban_list"]],
                         sorted((b["seconds"] for b in node["ban_list"]), reverse=True))
        self.assertEqual([l["id"] for l in snap["lamps"]], LAMP_IDS)
        self.assertEqual([(l["id"], l["label"]) for l in snap["lamps"]], LAMPS)
        for l in snap["lamps"]:
            self.assertEqual(set(l), {"id", "label", "state", "value", "since"})
            self.assertIn(l["state"], ("ok", "warn", "bad", "off"))
            self.assertLessEqual(len(l["value"]), 16)
            self.assertEqual(l["value"], l["value"].upper())
        self.assertTrue(all(l["state"] == "ok" for l in snap["lamps"] if l["id"] != "flood"), lamps_by_id(snap))
        self.assertEqual((lamps_by_id(snap)["flood"]["state"], lamps_by_id(snap)["flood"]["value"]),
                         ("off", "READING 30%"))
        kinds = [e["kind"] for e in snap["events"]]
        self.assertIn("block", kinds)
        self.assertEqual(snap["events"][-1]["kind"], "source")       # newest first
        plain = {"/" + m for m, where in READ_ALLOWED.items() if where != "/json_rpc"}
        self.assertTrue(set(rig.fake.calls) <= set(READ_ALLOWED) | plain, "only read-only calls reach the node")
        st, hist = get_json(rig.port, "/api/history?window=900")
        self.assertEqual(hist["out"][-1], 22)
        self.assertEqual((hist["cpu_temp_c"][-1], hist["node_cpu_pct"][-1]), (61.0, 7.5))
        self.assertEqual(len(hist["cpu_temp_c"]), len(hist["t"]))
        # the row still filling comes with each snapshot, the same as the history's newest point
        row = snap["hist"]
        self.assertEqual((row["t"], row["step"]), (hist["t"][-1], hist["step"]))
        self.assertEqual({k: row[k] for k in ("out", "pool", "rate_in", "cpu_temp_c")},
                         {k: hist[k][-1] for k in ("out", "pool", "rate_in", "cpu_temp_c")})

    def test_alerts_follow_the_node(self):
        rig = self.rig
        rig.poll(0)
        # disk nearly full: no grace period
        rig.fake.info["free_space"] = 4_200_000_000
        snap = rig.poll()
        a = alerts_by_id(snap)
        self.assertEqual(a["disk"]["level"], "bad")
        self.assertEqual(lamps_by_id(snap)["disk"]["state"], "bad")
        self.assertEqual(lamps_by_id(snap)["disk"]["since"], int(rig.clock()))
        self.assertEqual(snap["level"], "bad")
        # a reorg of 4 blocks
        rig.fake.reorg(4)
        snap = rig.poll()
        a = alerts_by_id(snap)
        self.assertEqual(a["reorg"]["level"], "warn")
        self.assertEqual(snap["chain"]["reorg"]["deepest_24h"], 4)
        self.assertEqual(snap["chain"]["reorg"]["last"]["depth"], 4)
        self.assertIn("reorg", [e["kind"] for e in snap["events"]])
        # the node stops: after 5 minutes only node_down is up
        left = {b["host"]: b["seconds"] for b in snap["node"]["ban_list"]}
        rig.fake.refuse()
        snap = rig.poll()
        self.assertFalse(snap["source"]["answering"])
        self.assertIn("connection refused", snap["source"]["error"])
        self.assertEqual(snap["alerts"], [])
        self.assertEqual(lamps_by_id(snap)["node"]["state"], "warn")
        self.assertEqual(lamps_by_id(snap)["disk"]["state"], "off")
        self.assertIsNone(snap["node"]["out_peers"])
        self.assertEqual(snap["node"]["height"], rig.fake.top + 1, "last known height kept")
        snap = rig.poll(300)
        self.assertEqual([a["id"] for a in snap["alerts"]], ["node_down"])
        self.assertEqual({b["host"]: b["seconds"] for b in snap["node"]["ban_list"]},
                         {h: s - 305 for h, s in left.items() if s > 305},
                         "the last ban list read keeps counting down while the node is away")
        self.assertEqual(lamps_by_id(snap)["node"]["state"], "bad")
        st, hist = get_json(rig.port, "/api/history?window=900")
        self.assertIsNone(hist["out"][-1])
        # back again
        rig.fake.resume()
        snap = rig.poll()
        self.assertTrue(snap["source"]["answering"])
        self.assertEqual(set(alerts_by_id(snap)), {"disk", "reorg"})
        texts = [e["text"] for e in snap["events"]]
        self.assertIn("Node answering again", texts)
        self.assertIn("Cleared: NODE NOT ANSWERING", texts)

    def test_peers_double_spend_and_restarts(self):
        rig = self.rig
        rig.poll(0)
        rig.fake.info["outgoing_connections_count"] = 6
        rig.fake.results["get_transaction_pool_stats"]["pool_stats"]["num_double_spends"] = 1
        snap = rig.poll()
        self.assertEqual(set(alerts_by_id(snap)), {"double_spend"})
        snap = rig.poll(10 * 60)
        a = alerts_by_id(snap)
        self.assertEqual(a["peers"]["text"], "Only 6 of 12 outgoing connections for 10 min")
        self.assertEqual(lamps_by_id(snap)["peers"]["state"], "warn")
        # three restarts within the hour
        for i in range(3):
            rig.fake.info["start_time"] = int(rig.clock()) + 1
            snap = rig.poll(30)
        self.assertEqual(alerts_by_id(snap)["restarts"]["text"], "Node restarted 3 times in the last hour")
        self.assertEqual([e["kind"] for e in snap["events"]].count("restart"), 3)

    def test_change_command_through_the_api(self):
        rig = self.rig
        rig.poll(0)
        st, out = get_json(rig.port, "/api/commands")
        self.assertEqual(st, 200)
        n_bans = rig.fake.calls.count("get_bans")
        body = {"name": "ban", "args": {"hosts": "77.88.100.x", "seconds": 3600}, "confirm": " ban "}
        st, _, raw = request(rig.port, "POST", "/api/command", body, headers=WRITE)
        out = json.loads(raw)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["message"], "Banned 77.88.100.x for 1 hour. Connections from it were dropped.")
        st, _, raw = request(rig.port, "POST", "/api/command",
                             {"name": "limit_up", "args": {"kbps": 1000}, "confirm": "LIMIT"}, headers=WRITE)
        self.assertEqual(st, 200, raw)
        snap = rig.poll(5)            # well inside get_bans' 60 s: re-read because a change command ran
        self.assertEqual(rig.fake.calls.count("get_bans"), n_bans + 1)
        self.assertIn({"host": "77.88.100.0/24", "seconds": 3600}, snap["node"]["ban_list"])
        self.assertEqual(snap["node"]["limit_up"], 1000 * 1024)
        control = [e for e in snap["events"] if e["kind"] == "control"]
        self.assertEqual([e["text"] for e in control], ["Set limit-up to 1000 kB/s", out["message"]])
        self.assertIn("command ban: " + out["message"], rig.logs)
        # the node's own address (--p2p-bind-ip, found from its flags) can't be banned
        rig.discover.found[0]["p2p_bind"] = ["33.44.7.7", "2001:db8::7"]
        rig.poll(DISCOVER_EVERY_S)
        self.assertEqual(rig.app.poller.protected_addresses(), [("33.44.7.7", "node"), ("2001:db8::7", "node")])
        st, _, raw = request(rig.port, "POST", "/api/command",
                             {"name": "ban", "args": {"hosts": "8.8.8.8\n33.44.x.x"}, "confirm": "BAN"}, headers=WRITE)
        self.assertEqual(st, 400)
        self.assertEqual(json.loads(raw)["error"], "33.44.x.x holds this computer's own public address (the node "
                                                   "is bound to it with --p2p-bind-ip), so banning it would cut "
                                                   "your node off.")
        self.assertEqual(rig.fake.set_bans_seen[-1]["bans"][0]["host"], "77.88.100.0/24", "nothing sent")
        # without local reads the guard only knows non-public addresses
        rig.settings.update({"local.enabled": False})
        rig.poll()
        self.assertEqual(rig.app.poller.protected_addresses(), [])


class RestrictedTest(unittest.TestCase):
    def test_restricted_port(self):
        rig = Rig(restricted=True)
        try:
            rig.poll(0)
            snap = rig.poll(10 * 60)
            self.assertTrue(snap["source"]["restricted"])
            self.assertTrue(snap["source"]["answering"])
            self.assertEqual(snap["peers"]["list"], [])
            self.assertIsNone(snap["peers"]["spread"])
            self.assertIsNone(snap["node"]["free_bytes"])
            self.assertIsNone(snap["node"]["bans"])
            self.assertEqual(snap["alerts"], [], "missing data must not fire peers/disk alerts")
            lamps = lamps_by_id(snap)
            self.assertEqual(lamps["node"]["value"], "RESTRICTED")
            self.assertEqual(lamps["peers"]["state"], "off")
            self.assertIn("restricted RPC port", " ".join(e["text"] for e in snap["events"]))
            self.assertNotIn("get_connections", rig.fake.calls)
            # a restricted port gives the RingCT output counts too (amount 0 only), so the black
            # marble watch works there
            for _ in range(6):
                snap = rig.poll(5)
            self.assertEqual(snap["chain"]["flood"]["state"], "ok")
            self.assertEqual(lamps_by_id(snap)["flood"]["value"], "RING ≈ 16 OF 16")
            self.assertTrue(all(p["amounts"] == [0] for m, p in rig.fake.requests if m == "get_output_distribution"))
        finally:
            rig.close()


class FloodTest(unittest.TestCase):
    def test_a_surge_of_outputs_lights_the_lamp_after_half_an_hour(self):
        rig = Rig()
        try:
            top = rig.fake.top
            rig.fake.floods.append((top - 3 * 720, top + 10 ** 6, 200))   # 3 days of 5 times the usual
            for _ in range(7):                                           # a year, 40,000 blocks a poll
                snap = rig.poll(5)
            fl = snap["chain"]["flood"]
            self.assertEqual((fl["state"], fl["height"], fl["volume_x"]), ("ok", top, 5.44))
            self.assertLess(fl["rings"], 8)
            self.assertEqual(lamps_by_id(snap)["flood"]["state"], "ok", "not lit before its 30 minutes")
            snap = rig.poll(30 * 60)
            a = alerts_by_id(snap)["flood"]
            self.assertEqual((a["level"], a["title"]), ("bad", "BLACK MARBLE WATCH"))
            self.assertTrue(a["text"].startswith("A recent surge of new outputs would supply about "), a["text"])
            lamp = lamps_by_id(snap)["flood"]
            self.assertEqual((lamp["state"], lamp["value"]), ("bad", "RING ≈ %d OF 16" % round(fl["rings"])))
            # the next block: only the newest counts are read again
            n = len(rig.fake.requests)
            rig.fake.add_block(rig.clock() + 5)
            snap = rig.poll(5)
            reads = [p for m, p in rig.fake.requests[n:] if m == "get_output_distribution"]
            self.assertEqual([(p["from_height"], p["to_height"]) for p in reads], [(top - 29, top + 1)])   # the newest 30 again
            self.assertEqual(snap["chain"]["flood"]["height"], top + 1)
        finally:
            rig.close()


class BootstrapTest(unittest.TestCase):
    """A node that syncs with help from a bootstrap node: monerod passes get_info and the chain,
    pool, limit, rules and version reads on to it and marks those answers untrusted."""

    def test_its_answers_are_not_shown_as_the_nodes(self):
        rig = Rig()
        try:
            snap = rig.poll(0)
            hf, limit_up = snap["node"]["hf_version"], snap["node"]["limit_up"]
            blocks = snap["chain"]["blocks"]
            remote = rig.fake.top + 1
            own = remote - 5000
            # what a public (restricted) bootstrap node answers, as monerod passes it on
            rig.fake.info.update({"untrusted": True, "height_without_bootstrap": own,
                                  "bootstrap_daemon_address": "node.example.org:18089",
                                  "was_bootstrap_ever_used": True, "restricted": True, "start_time": 0,
                                  "free_space": 2 ** 64 - 1, "version": "", "outgoing_connections_count": 0,
                                  "tx_pool_size": 7})
            n = len(rig.fake.requests)
            snap = rig.poll(10 * 60)                       # the slow readings are due again too
            sent = {m for m, _ in rig.fake.requests[n:]}
            self.assertEqual(sent & BOOTSTRAP_FORWARDED, set(), "nothing that would be the other node's answer")
            self.assertLessEqual({"get_info", "get_connections", "get_net_stats", "sync_info", "get_bans"}, sent)
            node, src = snap["node"], snap["source"]
            self.assertEqual((node["height"], node["target_height"], node["synced"]), (own, remote, False))
            self.assertEqual((src["bootstrap"], src["restricted"]), ("node.example.org:18089", False))
            ob, ib = snap["peers"]["out_by_kind"], snap["peers"]["in_by_kind"]
            self.assertEqual((node["out_peers"], node["in_peers"]), (ob["ipv4"] + ob["ipv6"], ib["ipv4"] + ib["ipv6"]),
                             "counted from get_connections, which is the node's own")
            for k in ("version", "started", "free_bytes", "tx_pool", "top_hash", "last_block_time"):
                self.assertIsNone(node[k], k)
            self.assertIsNone(snap["mempool"]["txs"])
            self.assertEqual((node["hf_version"], node["limit_up"]), (hf, limit_up), "its own last readings stay")
            self.assertEqual(snap["chain"]["blocks"], blocks, "the tape waits")
            texts = [e["text"] for e in snap["events"]]
            self.assertIn("Syncing with help from node.example.org:18089: its answers aren't shown as your node's",
                          texts)
            self.assertNotIn("restricted RPC port", " ".join(texts))
            self.assertEqual(lamps_by_id(snap)["node"]["value"], "ANSWERING")
            self.assertEqual(snap["chain"]["flood"], {"state": "off", "why": "bootstrap"})
            self.assertEqual(lamps_by_id(snap)["flood"]["value"], "WAITS FOR SYNC")
            # still syncing after the grace time: the alert fires; its height moving keeps no_blocks quiet
            for i in range(1, 5):
                rig.fake.info["height_without_bootstrap"] = own + 10 * i
                snap = rig.poll(10 * 60)
            a = alerts_by_id(snap)
            self.assertEqual(a["syncing"]["text"], "Still syncing after 40 min (height %s of %s)"
                             % (format(own + 40, ","), format(remote, ",")))
            self.assertNotIn("no_blocks", a)
            self.assertEqual(lamps_by_id(snap)["sync"]["value"], "SYNCING 99.9%")
            self.assertEqual([e for e in snap["events"] if e["kind"] == "reorg"], [])
            # its own answers again
            rig.fake.info = helpers.fixture("get_info.json")
            n = len(rig.fake.requests)
            snap = rig.poll(5)
            self.assertIsNone(snap["source"]["bootstrap"])
            self.assertEqual(snap["node"]["version"], "0.18.5.3-release")
            self.assertIn("get_last_block_header", {m for m, _ in rig.fake.requests[n:]})
            self.assertIn("No longer syncing with help from node.example.org:18089: showing your node's own answers "
                          "again", [e["text"] for e in snap["events"]])
        finally:
            rig.close()


class LocalReadsTest(unittest.TestCase):
    """host.py, logevents.py and host.discover wired into the poller (with stand-ins)."""

    def setUp(self):
        self.rig = Rig()

    def tearDown(self):
        self.rig.close()

    def test_read_every_local_every_s(self):
        rig = self.rig
        rig.poll(0)
        for _ in range(5):
            rig.poll(5)
        self.assertEqual(len(rig.host.reads), 3, "every 10 s with a poll every 5 s")
        self.assertEqual(rig.host.reads[0][1:], (rig.fake.url, "~/.bitmonero", 10))
        self.assertEqual(rig.host.reads[-1][2], "/home/u/.bitmonero", "the node's own data dir once it's found")
        rig.settings.update({"local.every_s": 30})
        n = len(rig.host.reads)
        for _ in range(6):
            rig.poll(5)
        self.assertEqual(len(rig.host.reads), n + 1)
        self.assertEqual(rig.poll(30)["host"]["every_s"], 30)

    def test_off(self):
        rig = self.rig
        rig.settings.update({"local.enabled": False})
        rig.write_log(log_line(rig.clock(), "ERROR", "not read"))
        self.assertIsNone(rig.app.poller.protected_addresses(), "`ban` waits for the first poll")
        snap = rig.poll(0)
        self.assertEqual(rig.app.poller.protected_addresses(), [])
        self.assertFalse(rig.app.poller.own_address_known(), "the node's --p2p-bind-ip wasn't read")
        snap = rig.poll(30)
        self.assertIsNone(snap["host"])
        self.assertEqual(rig.host.reads, [])
        self.assertEqual(rig.discover.calls, 0, "finding the node is a local read too")
        self.assertTrue(all(p["rtt_ms"] is None and p["retrans"] is None for p in snap["peers"]["list"]))
        st, hist = get_json(rig.port, "/api/history?window=900")
        self.assertIsNone(hist["cpu_temp_c"][-1])
        self.assertNotIn("log", [e["kind"] for e in snap["events"]])
        rig.settings.update({"local.enabled": True})
        self.assertEqual(rig.poll()["host"]["computer"]["cores"], 8)
        self.assertTrue(rig.app.poller.own_address_known())

    def test_log_events(self):
        rig = self.rig
        rig.write_log(log_line(rig.clock() - 60, "ERROR", "old: never replayed"))
        rig.poll(0)
        t = int(rig.clock())
        rig.write_log(log_line(t, "INFO", "Host 203.0.113.9 blocked."),
                      log_line(t, "WARNING", "Peer 9 dropped 3 times", cat="net.p2p"),
                      log_line(t, "INFO", "just chatter"))
        snap = rig.poll(10)
        logs = [e for e in snap["events"] if e["kind"] == "log"]
        self.assertEqual([{k: v for k, v in e.items() if k != "id"} for e in logs], [
            {"t": t, "level": "warn", "kind": "log", "text": "Peer 9 dropped 3 times"},
            {"t": t, "level": "info", "kind": "log", "text": "monerod banned 203.0.113.9"}], "newest first")
        # numbered one up from the event before, so the window adds only the new ones
        ids = [e["id"] for e in snap["events"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(logs[0]["id"], logs[1]["id"] + 1)
        rig.write_log(log_line(t + 5, "WARNING", "Peer 4 dropped 7 times", cat="net.p2p"))
        snap = rig.poll(10)
        self.assertEqual(len([e for e in snap["events"] if e["kind"] == "log"]), 2, "the same warning: once per 10 min")

    def test_a_node_on_another_computer(self):
        rig = self.rig
        rig.host.reading = dict(HostReader.empty("Your node runs on another computer (node.lan:18081)."),
                                remote={"how": "network", "where": "node.lan:18081", "via": None})
        rig.write_log(log_line(rig.clock(), "ERROR", "old"))
        snap = rig.poll(0)
        self.assertFalse(snap["host"]["ok"])
        self.assertEqual(snap["host"]["remote"]["where"], "node.lan:18081")
        self.assertEqual(rig.discover.calls, 0, "no process here to find")
        self.assertFalse(rig.app.poller.own_address_known())
        self.assertTrue(rig.app.poller.node_elsewhere())
        notes = [e for e in snap["events"] if e["kind"] == "source" and "Reading" not in e["text"]]
        self.assertEqual([e["level"] for e in notes], ["warn", "warn", "info"], "newest first")
        self.assertIn("with no login", notes[0]["text"])
        self.assertIn("plain http", notes[1]["text"])
        self.assertIn("another computer (node.lan:18081)", notes[2]["text"])
        snap = rig.poll(10)
        self.assertEqual(len([e for e in snap["events"] if e["kind"] == "source"]), 4, "said once")
        # its log: read only from a path set by hand (this rig sets one)
        t = int(rig.clock())
        rig.write_log(log_line(t, "WARNING", "Peer 9 dropped 3 times", cat="net.p2p"))
        snap = rig.poll(10)
        self.assertEqual([e["text"] for e in snap["events"] if e["kind"] == "log"], ["Peer 9 dropped 3 times"])
        guard = {"X-Memstara": "1"}
        st, out = get_json(rig.port, "/api/node-log", headers=guard)
        self.assertEqual(st, 200)
        rig.settings.update({"node.log_path": "~/.bitmonero/bitmonero.log"})
        st, out = get_json(rig.port, "/api/node-log", headers=guard)
        self.assertEqual(st, 404)
        self.assertIn("another computer", out["error"])
        # through an SSH tunnel: said again, without the network warnings
        rig.host.reading["remote"] = {"how": "tunnel", "where": "127.0.0.1:18081", "via": "ssh"}
        snap = rig.poll(10)
        self.assertIn("through ssh on 127.0.0.1:18081", snap["events"][0]["text"])
        self.assertEqual(snap["events"][0]["level"], "info")
        # back on this computer
        rig.host.reading = helpers.HOST_READING
        snap = rig.poll(10)
        self.assertTrue(snap["host"]["ok"])
        self.assertFalse(rig.app.poller.node_elsewhere())
        self.assertEqual(rig.discover.calls, 1)

    def test_a_log_without_log_in_its_name_only_when_the_node_was_found_with_it(self):
        rig = self.rig
        odd = rig.log.parent / "monerod-output"            # --log-file monerod-output
        odd.write_text("")
        rig.settings.update({"node.log_path": str(odd)})
        guard = {"X-Memstara": "1"}
        rig.poll(0)
        st, out = get_json(rig.port, "/api/node-log", headers=guard)
        self.assertEqual(st, 403)
        self.assertIn("its name has no .log", out["error"])
        self.assertIn("its name has no .log", rig.app.poller.node_log.error)
        rig.discover.found = [found_node(rig.fake.url, log_file=str(odd))]
        rig.poll(DISCOVER_EVERY_S)
        with open(odd, "a") as fh:
            fh.write(log_line(rig.clock(), "ERROR", "from the node's own log"))
        snap = rig.poll(10)
        self.assertIn("from the node's own log", [e["text"] for e in snap["events"] if e["kind"] == "log"])
        st, out = get_json(rig.port, "/api/node-log", headers=guard)
        self.assertEqual(st, 200)
        self.assertTrue(out["lines"][-1].endswith("from the node's own log"))

    def test_a_flood_of_log_lines_is_counted(self):
        rig = self.rig
        rig.poll(0)
        rig.write_log(*(log_line(rig.clock(), "ERROR", "boom %d" % i) for i in range(LOG_EVENTS_PER_READ + 7)))
        snap = rig.poll(10)
        logs = [e for e in snap["events"] if e["kind"] == "log"]
        self.assertEqual(len(logs), LOG_EVENTS_PER_READ + 1)
        self.assertEqual(logs[0]["text"], "7 more lines from monerod's log aren't shown here (COMMANDS has the "
                                          "whole log)")
        self.assertEqual(logs[1]["text"], "boom %d" % (LOG_EVENTS_PER_READ + 6))

    def test_discovery_is_cached(self):
        rig = self.rig
        rig.poll(0)
        for _ in range(5):
            rig.poll(10)
        self.assertEqual(rig.discover.calls, 1, "at most once a minute")
        rig.poll(DISCOVER_EVERY_S)
        self.assertEqual(rig.discover.calls, 2)
        rig.host.reading["proc"]["pid"] = 5555          # the node restarted
        rig.poll(10)
        self.assertEqual(rig.discover.calls, 3, "and again when the node's pid changes")
        rig.poll(10)
        self.assertEqual(rig.discover.calls, 3)
        # GET /api/discover always looks afresh (guarded like a change)
        st, _ = get_json(rig.port, "/api/discover")
        self.assertEqual(st, 403)
        st, out = get_json(rig.port, "/api/discover", headers={"X-Memstara": "1",
                                                                 "Origin": "http://127.0.0.1:%d" % rig.port})
        self.assertEqual((st, out["ok"], out["found"][0]["rpc_url"]), (200, True, rig.fake.url))
        self.assertEqual(rig.discover.calls, 4)
        st, _ = get_json(rig.port, "/api/discover", headers={"X-Memstara": "1", "Origin": "http://evil.example"})
        self.assertEqual(st, 403)

    def test_tor_auto_from_the_nodes_flags(self):
        rig = Rig(found=[])
        try:
            rig.fake.results["get_connections"]["connections"] = [
                c for c in rig.fake.results["get_connections"]["connections"] if not c["address"].endswith(".onion")
                and c.get("address_type") != 4]
            snap = rig.poll(0)
            self.assertIsNone(snap["node"]["tor_out"], "auto: no Tor peers and the node wasn't found")
            self.assertEqual(lamps_by_id(snap)["tor"]["value"], "OFF")
            rig.discover.found = [found_node(rig.fake.url, tx_proxy=True, tx_proxy_tor=True)]
            snap = rig.poll(DISCOVER_EVERY_S)
            self.assertEqual(snap["node"]["tor_out"], 0, "--tx-proxy: Tor sending is on")
            snap = rig.poll(10 * 60)
            self.assertEqual(alerts_by_id(snap)["tor"]["level"], "warn")
        finally:
            rig.close()

    def test_tor_auto_from_tor_peers_until_the_node_restarts(self):
        rig = Rig(found=[])
        try:
            conns = rig.fake.results["get_connections"]["connections"]
            snap = rig.poll(0)
            self.assertEqual(snap["node"]["tor_out"], 3, "auto: Tor peers seen and the node wasn't found")
            rig.fake.results["get_connections"]["connections"] = [
                c for c in conns if not c["address"].endswith(".onion") and c.get("address_type") != 4]
            snap = rig.poll(5)
            self.assertEqual(snap["node"]["tor_out"], 0, "still on: Tor peers were seen since it started")
            rig.fake.info["start_time"] = int(rig.clock()) + 1
            snap = rig.poll(5)
            self.assertIsNone(snap["node"]["tor_out"], "restarted: the Tor peers from before don't count")
            rig.fake.results["get_connections"]["connections"] = conns
            rig.discover.found = [found_node(rig.fake.url)]
            snap = rig.poll(DISCOVER_EVERY_S)
            self.assertIsNone(snap["node"]["tor_out"], "found without --tx-proxy tor,...: its flags decide")
        finally:
            rig.close()


class OutWantedTest(unittest.TestCase):
    def test_from_the_setting_the_node_or_monerods_default(self):
        rig = Rig(found=[])
        try:
            snap = rig.poll(0)
            self.assertEqual((snap["node"]["out_wanted"], snap["node"]["out_wanted_from"]), (12, "default"))
            rig.discover.found = [found_node(rig.fake.url, out_peers=32)]
            snap = rig.poll(DISCOVER_EVERY_S)
            self.assertEqual((snap["node"]["out_wanted"], snap["node"]["out_wanted_from"]), (32, "node"))
            self.assertEqual(lamps_by_id(snap)["peers"]["value"], "%d OF 32" % snap["node"]["out_peers"])
            rig.settings.update({"thresholds.out_wanted": 40})
            snap = rig.poll(5)
            self.assertEqual((snap["node"]["out_wanted"], snap["node"]["out_wanted_from"]), (40, "setting"))
        finally:
            rig.close()


class OneSecondTest(unittest.TestCase):
    """The defaults: the node and this computer read every second, and each new snapshot
    sent to the window the moment it's in."""

    def setUp(self):
        self.rig = Rig(cadence=None)

    def tearDown(self):
        self.rig.close()

    def test_reads_every_second(self):
        rig = self.rig
        for _ in range(10):
            snap = rig.poll(1)
        self.assertEqual((snap["source"]["poll_s"], snap["host"]["every_s"]), (1, 1))
        self.assertEqual(len(rig.host.reads), 10, "this computer every second too")
        # its counters timed by when each is read: the poll's time moved on by a clock
        # that never jumps
        now = rig.host.reads[-1][0]
        self.assertTrue(now <= rig.host.clock() < now + 5)
        self.assertEqual(snap["host"]["t"], now)
        self.assertEqual(snap["seq"], 10, "each snapshot numbered")
        self.assertLessEqual(len(rig.app.poller.history), 3, "history keeps one row per 5 s")
        st, hist = get_json(rig.port, "/api/history?window=900")
        self.assertEqual((hist["step"], hist["out"][-1]), (5, 22))

    def test_peers_coming_and_going_make_one_event_per_5_s(self):
        rig = self.rig
        rig.poll(1)
        conns = rig.fake.results["get_connections"]["connections"]

        def peer_events(snap):
            return [e["text"] for e in snap["events"] if e["kind"].startswith("peer")]
        for i in range(4):                       # one more peer gone each second
            rig.fake.results["get_connections"]["connections"] = conns[: len(conns) - 1 - i]
            snap = rig.poll(1)
        self.assertEqual(peer_events(snap), ["1 left"], "the first change shows at once")
        snap = rig.poll(1)
        self.assertEqual(peer_events(snap), ["1 left"], "the next ones wait for the 5 s to end")
        snap = rig.poll(1)
        self.assertEqual(peer_events(snap), ["3 left", "1 left"])
        # one leaving and coming back inside the 5 s cancels out
        rig.fake.results["get_connections"]["connections"] = conns[:2]
        rig.poll(1)
        rig.fake.results["get_connections"]["connections"] = conns[: len(conns) - 4]
        for _ in range(6):
            snap = rig.poll(1)
        self.assertEqual(peer_events(snap), ["3 left", "1 left"])

    def test_window_gets_the_next_snapshot_the_moment_its_in(self):
        rig = self.rig
        seq = rig.poll(1)["seq"]
        got = {}

        def ask():
            t0 = time.monotonic()
            st, snap = get_json(rig.port, "/api/snapshot?after=%d&wait=10" % seq)
            got.update(st=st, seq=snap["seq"], took=time.monotonic() - t0)
        th = threading.Thread(target=ask)
        th.start()
        time.sleep(0.3)
        self.assertTrue(th.is_alive(), "held until there's a newer one")
        rig.clock.advance(1)
        rig.app.poller.poll_once()
        th.join(5)
        self.assertEqual((got["st"], got["seq"]), (200, seq + 1))
        self.assertLess(got["took"], 3)

    def test_a_held_request_ends_after_its_wait(self):
        rig = self.rig
        seq = rig.poll(1)["seq"]
        t0 = time.monotonic()
        st, snap = get_json(rig.port, "/api/snapshot?after=%d&wait=0.4" % seq)
        self.assertEqual((st, snap["seq"]), (200, seq), "the same one when nothing newer came")
        self.assertGreaterEqual(time.monotonic() - t0, 0.35)
        with unittest.mock.patch("memstara.poller.MAX_WAIT_S", 0.3):
            t0 = time.monotonic()
            get_json(rig.port, "/api/snapshot?after=%d&wait=1000" % seq)
            self.assertLess(time.monotonic() - t0, 2, "never held longer than MAX_WAIT_S")
        for q in ("after=%d&wait=0" % seq, "after=%d&wait=nan" % seq, "after=%d&wait=-5" % seq,
                  "after=%d&wait=1e999" % (seq + 1), "after=x&wait=10", "after=%d&wait=10" % (seq + 100),
                  "after=%d" % seq, "after=%d&wait=%s" % (seq, "9" * 30)):
            t0 = time.monotonic()
            st, snap = get_json(rig.port, "/api/snapshot?" + q)
            self.assertEqual(st, 200, q)
            self.assertLess(time.monotonic() - t0, 0.3, q)

    def test_stopping_lets_held_requests_go(self):
        rig = self.rig
        seq = rig.poll(1)["seq"]
        done = threading.Event()

        def ask():
            try:
                get_json(rig.port, "/api/snapshot?after=%d&wait=15" % seq)
            except OSError:
                pass
            done.set()
        threading.Thread(target=ask, daemon=True).start()
        time.sleep(0.3)
        rig.app.poller.stop()
        self.assertTrue(done.wait(3), "a held request ends when the app stops")


class PoolAndArrivalTest(unittest.TestCase):
    """Each waiting transaction (pool.py) through to the snapshot, and new blocks dated by
    when the node got them rather than by the miner's stamp."""

    def setUp(self):
        self.rig = Rig(cadence=None)             # every second, the app's own cadence
        self.rig.fake.live_pool = True
        self.real = helpers.fixture("pool_txs.json")["txs"]

    def tearDown(self):
        self.rig.close()

    def test_each_transaction(self):
        rig = self.rig
        t0 = int(rig.clock())
        hs = [rig.fake.add_pool_tx(i=i, t=t0 - 60 * i) for i in range(1, 6)]
        mp = rig.poll(1)["mempool"]
        self.assertEqual((mp["txs"], mp["check"], mp["list_wanted"]), (6, "ok", 0),
                         "the fees and weights read add up to the node's own totals")
        got = {e[0]: e[1:] for e in mp["list"]}
        for i, h in enumerate([POOL_TX] + hs):
            t, fee, weight, flags = got[h[:16]]
            self.assertEqual((fee, weight, flags), (self.real[i]["fee"], self.real[i]["weight"], 0), i)
        self.assertEqual(got[hs[4][:16]][0], t0 - 300, "when the node got it")
        self.assertEqual([e[1] for e in mp["list"]], sorted(e[1] for e in mp["list"]), "oldest first")
        n = len(rig.fake.requests)
        h7 = rig.fake.add_pool_tx(i=2, relayed=False)
        mp = rig.poll(1)["mempool"]
        self.assertEqual([p for m, p in rig.fake.requests[n:] if m == "get_transactions"],
                         [{"txs_hashes": [h7], "split": True}], "only the new one is read")
        self.assertEqual(mp["list"][-1][0::4], [h7[:16], FLAG_NOT_RELAYED])
        n = len(rig.fake.requests)
        rig.poll(1)
        self.assertNotIn("get_transaction_pool_hashes", [m for m, _ in rig.fake.requests[n:]],
                         "nothing changed: not read again until RECHECK_S")

    def test_restricted_port(self):
        rig = Rig(restricted=True, cadence=None)
        try:
            rig.fake.live_pool = True
            rig.fake.add_pool_tx(i=1, t=int(rig.clock()) - 500)
            mp = rig.poll(1)["mempool"]
            self.assertEqual([e[1::3] for e in mp["list"]], [[int(rig.clock()), FLAG_TIME_GUESSED]] * 2,
                             "a restricted port doesn't say when the node got them")
            self.assertEqual(mp["check"], "ok")
        finally:
            rig.close()

    def test_new_blocks_dated_by_arrival(self):
        rig = self.rig
        hs = [rig.fake.add_pool_tx(i=i) for i in range(1, 6)]
        rig.poll(1)
        rig.poll(1)
        stamp = int(rig.clock()) - 150            # the miner stamped it 2.5 min before it was found
        rig.fake.mine(hs[:3], ts=stamp)
        snap = rig.poll(1)
        now = rig.clock()
        top = snap["chain"]["blocks"][-1]
        self.assertEqual((top["height"], top["time"], top["seen"]), (rig.fake.top, stamp, round(now, 1)))
        node = snap["node"]
        self.assertEqual((node["last_block_time"], node["last_block_seen"], node["last_block_age_s"]),
                         (stamp, round(now, 1), 0), "the age counts from when the node got it")
        self.assertEqual(snap["chain"]["wait_odds"], 1.0)
        mp = snap["mempool"]
        self.assertEqual(mp["mined"], [{"height": rig.fake.top, "ids": [h[:16] for h in hs[:3]]}],
                         "which waiting transactions the block took")
        self.assertEqual(sorted(e[0] for e in mp["list"]), sorted(h[:16] for h in [POOL_TX] + hs[3:]))
        self.assertEqual(mp["check"], "ok")
        self.assertEqual(snap["chain"]["blocks"][-2]["seen"], None, "from before: not known")
        # the next one 10 s later
        for _ in range(9):
            rig.poll(1)
        rig.fake.mine([], ts=stamp + 30)
        snap = rig.poll(1)
        top = snap["chain"]["blocks"][-1]
        self.assertEqual((top["seen_interval_s"], top["interval_s"]), (10.0, 30))
        self.assertEqual(snap["mempool"]["mined"][-1]["ids"], [])
        for _ in range(4):
            snap = rig.poll(1)
        self.assertEqual(snap["node"]["last_block_age_s"], 4)
        # the node away for 10 minutes (or the computer asleep): when a block that's new after
        # that arrived isn't known, so its age is from the miner's stamp
        rig.fake.refuse()
        rig.poll(600)
        rig.fake.resume()
        rig.fake.add_block(rig.clock() - 100)
        snap = rig.poll(1)
        self.assertEqual((snap["chain"]["blocks"][-1]["seen"], snap["node"]["last_block_seen"]), (None, None))
        self.assertEqual(snap["node"]["last_block_age_s"], 101)
        # four at once is catching up, not arriving
        for _ in range(4):
            rig.fake.add_block(rig.clock())
        snap = rig.poll(1)
        self.assertEqual([b["seen"] for b in snap["chain"]["blocks"][-4:]], [None] * 4)
        # a block replaced (a reorg of one) and one more on top: both arrived just now
        rig.fake.reorg(1)
        snap = rig.poll(1)
        self.assertEqual([b["seen"] for b in snap["chain"]["blocks"][-3:]], [None] + [round(rig.clock(), 1)] * 2)


class ThreadTest(unittest.TestCase):
    def test_real_poll_thread(self):
        tmp = tempfile.TemporaryDirectory()
        fake = FakeMonerod().start()
        base = Path(tmp.name)
        s = Settings(base / "settings.json", log=lambda m: None).load()
        s.update({"source.rpc_url": fake.url, "source.refresh_s": 2})
        app = App(s, port=0, web_dir=base / "web", state_dir=base / "state", log=lambda m: None,
                  host_reader=StubHost(), discover=StubDiscover()).start()
        try:
            deadline = time.time() + 10
            snap = None
            while time.time() < deadline:
                st, snap = get_json(app.port, "/api/snapshot")
                if snap["source"]["answering"]:
                    break
                time.sleep(0.1)
            self.assertTrue(snap["source"]["answering"])
            n = len(fake.calls)
            s.update({"thresholds.low_peers": 9})          # wakes the poller at once
            deadline = time.time() + 5
            while time.time() < deadline and len(fake.calls) == n:
                time.sleep(0.05)
            self.assertGreater(len(fake.calls), n)
        finally:
            app.stop()
            fake.stop()
        self.assertTrue((base / "state" / "history.json").exists(), "history saved on stop")
        tmp.cleanup()

    def test_stop_doesnt_wait_for_a_stuck_poll(self):
        # at logout the desktop gives the app only a few seconds to stop: a poll stuck on a
        # slow read mustn't hold that up, nor the history saved on the way out
        stuck, inside, release = threading.Event(), threading.Event(), threading.Event()

        class SlowHost(StubHost):
            def annotate(self, peers):
                if stuck.is_set():
                    inside.set()
                    release.wait(30)
                return super().annotate(peers)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = FakeMonerod().start()
        self.addCleanup(fake.stop)
        self.addCleanup(release.set)
        base = Path(tmp.name)
        s = Settings(base / "settings.json", log=lambda m: None).load()
        s.update({"source.rpc_url": fake.url, "source.refresh_s": 2})
        app = App(s, port=0, web_dir=base / "web", state_dir=base / "state", log=lambda m: None,
                  host_reader=SlowHost(), discover=StubDiscover()).start()
        deadline = time.time() + 10
        while time.time() < deadline and not get_json(app.port, "/api/snapshot")[1]["source"]["answering"]:
            time.sleep(0.1)
        stuck.set()
        s.update({"thresholds.low_peers": 9})   # wakes the poller into the stuck read
        self.assertTrue(inside.wait(10), "the poller got stuck")
        t0 = time.monotonic()
        app.stop()
        self.assertLess(time.monotonic() - t0, 4)
        self.assertTrue((base / "state" / "history.json").exists(), "history saved anyway")


if __name__ == "__main__":
    unittest.main()
