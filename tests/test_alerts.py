"""Alert rules with grace periods and sticky periods, driven by a fake clock."""

from __future__ import annotations

import unittest

import helpers
from memstara import adapter
from memstara.alerts import DEFAULT_CFG, RPC_IDS, SPY_HOLD_S, TITLES, AlertEngine, empty_obs, tor_sending_active

MIN = 60


def healthy(**kw):
    o = empty_obs()
    o.update({"answering": True, "restricted": False, "out_peers": 22, "total_out": 25, "tor_out": 3,
              "tor_active": True, "synced": True, "height": 3780042, "target_height": 3780042,
              "block_seen_age": 60, "free_bytes": 400 * 10 ** 9, "reorgs": [], "double_spends": 0,
              "restarts": [], "out16": {"net": "100.66", "n": 2, "ips": ["100.66.1.9", "100.66.4.2"]},
              "in24": {"net": "100.70.3", "n": 1, "ips": ["100.70.3.4"]}, "spy": []})
    o.update(kw)
    return o


class Harness:
    def __init__(self, cfg=None):
        self.clock = helpers.FakeClock()
        self.engine = AlertEngine()
        self.cfg = dict(DEFAULT_CFG, **(cfg or {}))
        self.events = []

    def run(self, obs, advance=0):
        self.clock.advance(advance)
        alerts, events = self.engine.evaluate(obs, self.cfg, self.clock())
        self.events.extend(events)
        return {a["id"]: a for a in alerts}, alerts


class NodeDownTest(unittest.TestCase):
    def test_after_five_minutes(self):
        h = Harness()
        down = healthy(answering=False, error_short="connection refused")
        a, _ = h.run(down)
        self.assertEqual(a, {})
        a, _ = h.run(down, 4 * MIN + 59)
        self.assertEqual(a, {})
        a, _ = h.run(down, 1)
        self.assertEqual(a["node_down"]["level"], "bad")
        self.assertEqual(a["node_down"]["text"], "Your node isn't answering for 5 min (connection refused)")
        self.assertEqual(a["node_down"]["since"], int(helpers.T0))
        self.assertEqual(h.events[-1]["kind"], "alert")
        a, _ = h.run(healthy(), 5)
        self.assertEqual(a, {})
        self.assertEqual(h.events[-1], {"t": int(h.clock()), "level": "ok", "kind": "clear",
                                        "text": "Cleared: NODE NOT ANSWERING"})

    def test_only_node_down_while_not_answering(self):
        h = Harness()
        bad = healthy(answering=False, out_peers=0, total_out=0, free_bytes=1, synced=False,
                      double_spends=3, block_seen_age=5 * 3600)
        h.run(bad)
        a, _ = h.run(bad, 10 * MIN)
        self.assertEqual(set(a), {"node_down"})

    def test_paused_alerts_neither_clear_nor_reannounce(self):
        h = Harness()
        a, _ = h.run(healthy(free_bytes=4 * 10 ** 9))
        self.assertIn("disk", a)
        n = len(h.events)
        a, _ = h.run(healthy(answering=False), 10)
        self.assertNotIn("disk", a)
        self.assertEqual(len(h.events), n, "pausing must not add a 'cleared' event")
        a, _ = h.run(healthy(free_bytes=4 * 10 ** 9), 10)
        self.assertIn("disk", a)
        self.assertEqual(len(h.events), n, "coming back must not announce it again")
        h.run(healthy(answering=False), 10)
        h.run(healthy(), 10)
        self.assertEqual(h.events[-1]["text"], "Cleared: DISK ALMOST FULL")
        self.assertEqual(h.events[-1]["kind"], "clear")


class PeersTest(unittest.TestCase):
    def test_few_outgoing_warn(self):
        h = Harness()
        few = healthy(out_peers=6, total_out=9)
        a, _ = h.run(few)
        a, _ = h.run(few, 9 * MIN)
        self.assertNotIn("peers", a)
        a, _ = h.run(few, 3 * MIN)
        self.assertEqual(a["peers"]["level"], "warn")
        self.assertEqual(a["peers"]["title"], "FEW OUTGOING")
        self.assertEqual(a["peers"]["text"], "Only 6 of 12 outgoing connections for 12 min")

    def test_none_outgoing_bad(self):
        h = Harness()
        none = healthy(out_peers=0, total_out=0, tor_out=0)
        h.run(none)
        a, _ = h.run(none, 4 * MIN)
        self.assertNotIn("peers", a)
        a, _ = h.run(none, 1 * MIN)
        self.assertEqual(a["peers"]["level"], "bad")
        self.assertEqual(a["peers"]["title"], "NO OUTGOING")
        self.assertEqual(a["peers"]["text"], "No outgoing connections for 5 min")

    def test_tor_counts_for_none_but_not_for_few(self):
        h = Harness()
        only_tor = healthy(out_peers=0, total_out=3, tor_out=3)
        h.run(only_tor)
        a, _ = h.run(only_tor, 10 * MIN)
        self.assertEqual(a["peers"]["level"], "warn")
        self.assertIn("except 3 over Tor", a["peers"]["text"])

    def test_grace_resets_when_recovered(self):
        h = Harness()
        h.run(healthy(out_peers=6))
        h.run(healthy(out_peers=6), 8 * MIN)
        h.run(healthy(), 1 * MIN)
        a, _ = h.run(healthy(out_peers=6), 3 * MIN)
        self.assertNotIn("peers", a)


class ChainRulesTest(unittest.TestCase):
    def test_syncing(self):
        h = Harness()
        s = healthy(synced=False, height=3778000, target_height=3780000)
        h.run(s)
        a, _ = h.run(s, 19 * MIN)
        self.assertNotIn("syncing", a)
        a, _ = h.run(s, 1 * MIN)
        self.assertEqual(a["syncing"]["text"], "Still syncing after 20 min (height 3,778,000 of 3,780,000)")

    def test_no_blocks(self):
        h = Harness()
        a, _ = h.run(healthy(block_seen_age=29 * MIN))
        self.assertNotIn("no_blocks", a)
        a, _ = h.run(healthy(block_seen_age=34 * MIN + 10))
        self.assertEqual((a["no_blocks"]["level"], a["no_blocks"]["text"]), ("warn", "No new block for 34 min"))
        a, _ = h.run(healthy(block_seen_age=60 * MIN))
        self.assertEqual((a["no_blocks"]["level"], a["no_blocks"]["text"]), ("bad", "No new block for 60 min"))

    def test_disk(self):
        h = Harness()
        a, _ = h.run(healthy(free_bytes=25 * 10 ** 9))
        self.assertNotIn("disk", a)
        a, _ = h.run(healthy(free_bytes=19_900_000_000))
        self.assertEqual((a["disk"]["level"], a["disk"]["text"]), ("warn", "Only 19 GB free on the blockchain disk"))
        a, _ = h.run(healthy(free_bytes=4_200_000_000))
        self.assertEqual((a["disk"]["level"], a["disk"]["text"]), ("bad", "Only 4 GB free on the blockchain disk"))
        a, _ = h.run(healthy(free_bytes=None))          # restricted port: unknown, so paused
        self.assertNotIn("disk", a)

    def test_reorg_depth_from_hash_map_and_24h(self):
        h = Harness()
        stored = {3780000 + i: "%064x" % i for i in range(100)}
        fresh = [{"height": k, "hash": v} for k, v in stored.items()]
        for b in fresh[-4:]:
            b["hash"] = "f" * 64
        depth, low = adapter.find_reorg(stored, fresh)
        self.assertEqual((depth, low), (4, 3780096))
        reorgs = [{"time": int(h.clock()), "depth": depth, "height": low}]
        a, _ = h.run(healthy(reorgs=reorgs))
        self.assertEqual(a["reorg"]["level"], "warn")
        self.assertTrue(a["reorg"]["text"].startswith("4 blocks replaced at height 3,780,096 ("))
        self.assertIn("wait for extra confirmations", a["reorg"]["text"])
        reorgs.append({"time": int(h.clock()) + 60, "depth": 12, "height": 3780088})
        a, _ = h.run(healthy(reorgs=reorgs), 60)
        self.assertEqual(a["reorg"]["level"], "bad")
        self.assertIn("deeper than the 10-block lock", a["reorg"]["text"])
        a, _ = h.run(healthy(reorgs=reorgs), 23 * 3600)
        self.assertEqual(a["reorg"]["level"], "bad", "stays up 24 h")
        a, _ = h.run(healthy(reorgs=reorgs), 3600)
        self.assertNotIn("reorg", a)
        a, _ = h.run(healthy(reorgs=[{"time": int(h.clock()), "depth": 2, "height": 1}]))
        self.assertNotIn("reorg", a, "1-2 blocks happen normally")

    def test_double_spend_sticky_30_min(self):
        h = Harness(cfg={"clock": "24h"})
        a, _ = h.run(healthy(double_spends=1))
        self.assertEqual(a["double_spend"]["level"], "warn")
        self.assertIn("1 double-spend attempt(s) seen in the mempool at ", a["double_spend"]["text"])
        a, _ = h.run(healthy(double_spends=0), 29 * MIN)
        self.assertIn("double_spend", a)
        a, _ = h.run(healthy(double_spends=0), 1 * MIN)
        self.assertNotIn("double_spend", a)
        self.assertEqual(h.events[-1]["kind"], "clear")

    def test_restarts_within_an_hour(self):
        h = Harness()
        now = h.clock()
        a, _ = h.run(healthy(restarts=[now - 50 * MIN, now - 10 * MIN]))
        self.assertNotIn("restarts", a)
        a, _ = h.run(healthy(restarts=[now - 50 * MIN, now - 10 * MIN, now - 1 * MIN]))
        self.assertEqual(a["restarts"]["text"], "Node restarted 3 times in the last hour")
        a, _ = h.run(healthy(restarts=[now - 50 * MIN, now - 10 * MIN, now - 1 * MIN]), 11 * MIN)
        self.assertNotIn("restarts", a)


class SpreadTorTest(unittest.TestCase):
    def test_spread_clusters(self):
        h = Harness()
        ips = ["100.66.%d.9" % i for i in range(5)]
        o = healthy(out16={"net": "100.66", "n": 5, "ips": ips},
                    in24={"net": "100.70.3", "n": 4, "ips": ["100.70.3.%d" % i for i in (4, 5, 6, 7)]})
        h.run(o)
        a, _ = h.run(o, 9 * MIN)
        self.assertNotIn("spread_out", a)
        a, _ = h.run(o, 1 * MIN)
        self.assertEqual(a["spread_out"]["ips"], ips)
        self.assertEqual(a["spread_out"]["text"],
                         "5 of your outgoing connections come from one company's servers: could be spy nodes")
        self.assertEqual(a["spread_in"]["text"], "4 incoming connections come from one company's servers: "
                                                 "could be spy nodes")
        a, _ = h.run(healthy(in24={"net": "100.70.3", "n": 3, "ips": []}), 60)
        self.assertNotIn("spread_in", a)

    def test_tor_rule(self):
        h = Harness()
        o = healthy(tor_out=0, tor_active=True)
        h.run(o)
        a, _ = h.run(o, 10 * MIN)
        self.assertEqual(a["tor"]["text"], "No Tor connections for 10 min: transactions you send will wait "
                                           "until one connects")
        a, _ = h.run(healthy(tor_out=0, tor_active=False), 60)
        self.assertNotIn("tor", a)

    def test_tor_sending_modes(self):
        self.assertTrue(tor_sending_active("on", False))
        self.assertFalse(tor_sending_active("off", True))
        self.assertFalse(tor_sending_active("auto", False), "no Tor peers seen since the node started")
        self.assertTrue(tor_sending_active("auto", True), "Tor peers seen since the node started")


FIX = ("Starting monerod with --enable-dns-blocklist (mainnet only, and with --proxy only alongside "
       "--proxy-allow-dns-leaks) or with --ban-list and Memstara's copy bans the whole list")


class SpyListTest(unittest.TestCase):
    def test_up_while_connected_and_ten_minutes_after(self):
        h = Harness(cfg={"clock": "24h"})
        a, _ = h.run(healthy())
        self.assertNotIn("spy", a)
        a, _ = h.run(healthy(spy=[("in", "100.70.9.1")]), 5)
        self.assertEqual(a["spy"]["level"], "warn")
        self.assertEqual(a["spy"]["title"], "SPY NODE LIST")
        self.assertEqual(a["spy"]["text"], "1 peer on the MRL spy node list is connected (1 in). " + FIX)
        self.assertEqual(a["spy"]["ips"], ["100.70.9.1"])
        first = a["spy"]["since"]
        self.assertEqual(h.events[-1]["kind"], "alert")
        a, _ = h.run(healthy(spy=[("out", "100.66.9.2"), ("in", "100.70.9.1"), ("out", "100.66.9.3")]), 5)
        self.assertEqual(a["spy"]["text"], "3 peers on the MRL spy node list are connected (2 out · 1 in). " + FIX)
        self.assertEqual(a["spy"]["since"], first)
        a, _ = h.run(healthy(spy=[]), 5)
        self.assertTrue(a["spy"]["text"].startswith("Peers on the MRL spy node list were connected at "))
        self.assertEqual(a["spy"]["ips"], ["100.66.9.2", "100.70.9.1", "100.66.9.3"])
        n_events = len(h.events)
        # back again within the ten minutes: the same alert, no new event
        a, _ = h.run(healthy(spy=[("in", "100.70.9.1")]), SPY_HOLD_S - 60)
        self.assertEqual(a["spy"]["since"], first)
        a, _ = h.run(healthy(spy=[]), 60)
        self.assertTrue(a["spy"]["text"].startswith("A peer on the MRL spy node list was connected at "))
        self.assertEqual(len(h.events), n_events)
        a, _ = h.run(healthy(spy=[]), SPY_HOLD_S - 65)
        self.assertIn("spy", a)
        a, _ = h.run(healthy(spy=[]), 5)
        self.assertNotIn("spy", a)
        self.assertEqual(h.events[-1], {"t": int(h.clock()), "level": "ok", "kind": "clear",
                                        "text": "Cleared: SPY NODE LIST"})

    def test_paused_without_the_connections(self):
        h = Harness()
        a, _ = h.run(healthy(spy=[("out", "100.66.9.2")]))
        self.assertIn("spy", a)
        # a restricted port, a list that couldn't be read, or the bundled list not usable
        a, _ = h.run(healthy(spy=None), SPY_HOLD_S * 3)
        self.assertNotIn("spy", a)
        self.assertNotEqual(h.events[-1]["kind"], "clear", "paused, not cleared")

    def test_a_sighting_after_a_long_pause_is_a_new_alert(self):
        h = Harness()
        a, _ = h.run(healthy(spy=[("out", "100.66.9.2")]))
        first = a["spy"]["since"]
        h.run(healthy(spy=None), 8 * 3600)          # the node was down overnight
        a, _ = h.run(healthy(spy=[("out", "100.66.9.2")]), 5)
        self.assertEqual(a["spy"]["since"], int(h.clock()))
        self.assertGreater(a["spy"]["since"], first)
        self.assertEqual(h.events[-1]["kind"], "alert", "raised again, so it's in the log again")


class OrderTest(unittest.TestCase):
    def test_every_alert_is_from_rpc(self):
        self.assertEqual(set(TITLES), set(RPC_IDS), "nothing about this computer itself")

    def test_worst_first_then_oldest(self):
        h = Harness()
        h.run(healthy(double_spends=1))
        _, alerts = h.run(healthy(double_spends=0, free_bytes=19 * 10 ** 9), 10)
        self.assertEqual([a["id"] for a in alerts], ["double_spend", "disk"])
        for a in alerts:
            self.assertEqual(set(a), {"id", "level", "title", "text", "since", "source", "ips"})
            self.assertEqual(a["source"], "dashboard")


if __name__ == "__main__":
    unittest.main()
