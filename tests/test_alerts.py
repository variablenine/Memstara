"""Alert rules with grace periods and sticky periods, driven by a fake clock."""

from __future__ import annotations

import unittest

import helpers
from memstara import adapter, host
from memstara.alerts import DEFAULT_CFG, AlertEngine, empty_obs, tor_sending_active

MIN = 60


def healthy(**kw):
    o = empty_obs()
    o.update({"answering": True, "restricted": False, "out_peers": 22, "total_out": 25, "tor_out": 3,
              "tor_active": True, "synced": True, "height": 3780042, "target_height": 3780042,
              "block_seen_age": 60, "free_bytes": 400 * 10 ** 9, "reorgs": [], "double_spends": 0,
              "restarts": [], "out16": {"net": "100.66", "n": 2, "ips": ["100.66.1.9", "100.66.4.2"]},
              "in24": {"net": "100.70.3", "n": 1, "ips": ["100.70.3.4"]}, "addon_alerts": {}})
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
        self.assertTrue(tor_sending_active("on", False, None))
        self.assertFalse(tor_sending_active("off", True, 3))
        self.assertFalse(tor_sending_active("auto", False, None), "the node's flags weren't found")
        self.assertFalse(tor_sending_active("auto", False, False), "found, without --tx-proxy tor,...")
        self.assertTrue(tor_sending_active("auto", False, True), "found with --tx-proxy tor,...")
        self.assertTrue(tor_sending_active("auto", True, None), "Tor peers seen since the node started")
        self.assertFalse(tor_sending_active("auto", True, False), "found: its flags decide")
        self.assertFalse(tor_sending_active("auto", False, "yes"), "only True counts")

    def test_tor_alert_not_for_an_i2p_only_proxy(self):
        argv = ["--tx-proxy", "i2p,127.0.0.1:4447", "--anonymous-inbound", "x.b32.i2p,127.0.0.1:18085"]
        ns = host.node_settings(argv, "/home/u", read_file=lambda p: None)
        self.assertEqual((ns["tx_proxy"], ns["tx_proxy_tor"]), (True, False))
        h = Harness()
        o = healthy(tor_out=0, tor_active=tor_sending_active("auto", False, ns["tx_proxy_tor"]))
        h.run(o)
        a, _ = h.run(o, 11 * MIN)
        self.assertNotIn("tor", a)
        ns = host.node_settings(["--tx-proxy", "tor,127.0.0.1:9050,16", "--tx-proxy", "i2p,127.0.0.1:4447"], "/home/u",
                                read_file=lambda p: None)
        self.assertEqual((ns["tx_proxy"], ns["tx_proxy_tor"]), (True, True))


def chain(level="bad", **kw):
    return dict({"id": "chain", "level": level, "text": "check failed", "title": "EXAMPLE CHECK"}, **kw)


def pc(**kw):
    c = {"cpu_temp_c": 55.0, "on_ac": True, "battery_pct": 100, "mem_used_bytes": 4 * 10 ** 9,
         "mem_total_bytes": 16 * 10 ** 9}
    c.update(kw)
    return c


class ComputerTest(unittest.TestCase):
    def test_cpu_heat_after_two_minutes(self):
        h = Harness({"temp_unit": "f"})
        warm = healthy(computer=pc(cpu_temp_c=88.0))
        h.run(warm)
        a, _ = h.run(warm, 2 * MIN - 1)
        self.assertNotIn("cpu_temp", a)
        a, _ = h.run(warm, 1)
        self.assertEqual((a["cpu_temp"]["level"], a["cpu_temp"]["title"]), ("warn", "CPU HOT"))
        self.assertEqual(a["cpu_temp"]["text"], "The CPU has been at or above 185 °F for 2 min and reads 190 °F now")
        hot = healthy(computer=pc(cpu_temp_c=97.2))
        h.run(hot, 5)
        a, _ = h.run(hot, 2 * MIN)
        self.assertEqual((a["cpu_temp"]["level"], a["cpu_temp"]["title"]), ("bad", "CPU VERY HOT"))
        self.assertTrue(a["cpu_temp"]["text"].startswith("The CPU has been at or above 203 °F for 2 min and reads 207 °F"))
        a, _ = h.run(healthy(computer=pc()), 10)
        self.assertNotIn("cpu_temp", a)
        self.assertEqual(h.events[-1]["text"], "Cleared: CPU VERY HOT")

    def test_temperature_words_follow_the_setting(self):
        h = Harness({"temp_unit": "auto", "hot_min": 0})
        a, _ = h.run(healthy(computer=pc(cpu_temp_c=86.0)))
        self.assertEqual(a["cpu_temp"]["text"],
                         "The CPU has been at or above 185 °F (85 °C) for 0 min and reads 187 °F (86 °C) now")
        h = Harness({"temp_unit": "c", "hot_min": 0})
        a, _ = h.run(healthy(computer=pc(cpu_temp_c=86.0)))
        self.assertIn("at or above 85 °C", a["cpu_temp"]["text"])

    def test_memory_and_power(self):
        h = Harness()
        o = healthy(computer=pc(mem_used_bytes=15 * 10 ** 9, on_ac=False, battery_pct=64))
        h.run(o)
        a, _ = h.run(o, 1 * MIN)
        self.assertEqual(a["power"]["text"], "This computer has been on battery for 1 min (64% left)")
        self.assertNotIn("memory", a)
        a, _ = h.run(o, 4 * MIN)
        self.assertEqual(a["memory"]["text"], "93% of this computer's memory has been in use for 5 min")
        low = healthy(computer=pc(on_ac=False, battery_pct=12))
        h.run(low, 5)
        a, _ = h.run(low, 1 * MIN)
        self.assertEqual((a["power"]["level"], a["power"]["title"]), ("bad", "BATTERY LOW"))
        a, _ = h.run(healthy(computer=pc(on_ac=None, battery_pct=None)), 5)
        self.assertNotIn("power", a, "no battery or no reading: nothing to say")

    def test_goes_on_while_the_node_is_away_and_pauses_without_readings(self):
        h = Harness({"hot_min": 0})
        a, _ = h.run(healthy(answering=False, computer=pc(cpu_temp_c=99.0)))
        self.assertEqual(a["cpu_temp"]["level"], "bad")
        n = len(h.events)
        a, _ = h.run(healthy(computer=None), 5)
        self.assertNotIn("cpu_temp", a)
        self.assertEqual(len(h.events), n, "no readings (local reads off): paused, not cleared")


class OrderAndAddonTest(unittest.TestCase):
    def test_worst_first_then_oldest(self):
        h = Harness()
        h.run(healthy(double_spends=1))
        _, alerts = h.run(healthy(double_spends=0, free_bytes=19 * 10 ** 9, addon_alerts={"demo": [chain()]}), 10)
        self.assertEqual([a["id"] for a in alerts], ["chain", "double_spend", "disk"])
        self.assertEqual(alerts[0]["source"], "demo")
        self.assertEqual(alerts[0]["title"], "EXAMPLE CHECK")
        for a in alerts:
            self.assertEqual(set(a), {"id", "level", "title", "text", "since", "source", "ips"})

    def test_core_ids_win(self):
        h = Harness()
        a, _ = h.run(healthy(addon_alerts={"demo": [{"id": "disk", "level": "bad", "text": "Only 1 GB free"},
                                                   {"id": "link_down", "level": "warn", "text": "x"}]}))
        self.assertNotIn("disk", a)
        self.assertEqual(a["link_down"]["source"], "demo")
        self.assertEqual(a["link_down"]["title"], "LINK DOWN", "no title given: from the id")

    def test_first_add_on_wins_an_id(self):
        h = Harness()
        a, _ = h.run(healthy(addon_alerts={"aaa": [chain("warn")], "bbb": [chain("bad", text="other")]}))
        self.assertEqual((a["chain"]["source"], a["chain"]["level"]), ("aaa", "warn"))
        a, _ = h.run(healthy(addon_alerts={"aaa": [chain("warn"), chain("bad")]}), 5)
        self.assertEqual(a["chain"]["level"], "bad", "the worse of one add-on's duplicates")

    def test_since_given_or_first_seen(self):
        h = Harness()
        t0 = int(h.clock())
        a, _ = h.run(healthy(addon_alerts={"demo": [chain(), {"id": "x", "text": "y", "since": t0 - 300}]}))
        self.assertEqual((a["chain"]["since"], a["x"]["since"], a["x"]["level"]), (t0, t0 - 300, "warn"))
        a, _ = h.run(healthy(addon_alerts={"demo": [chain()]}), 60)
        self.assertEqual(a["chain"]["since"], t0, "kept while it stays up")

    def test_none_holds_everything_from_that_add_on(self):
        h = Harness()
        t0 = int(h.clock())
        h.run(healthy(addon_alerts={"demo": [chain()], "other": [{"id": "x", "text": "y"}]}))
        n = len(h.events)
        a, _ = h.run(healthy(addon_alerts={"demo": None, "other": [{"id": "x", "text": "y"}]}), 5)
        self.assertEqual(set(a), {"x"}, "held: not shown")
        self.assertEqual(len(h.events), n, "and not cleared")
        a, _ = h.run(healthy(addon_alerts={"demo": [chain()], "other": [{"id": "x", "text": "y"}]}), 5)
        self.assertEqual(a["chain"]["since"], t0, "back as it was, no new event")
        self.assertEqual(len(h.events), n)

    def test_hold_one_alert_and_raise_another(self):
        h = Harness()
        a, _ = h.run(healthy(addon_alerts={"demo": [chain()]}))
        n = len(h.events)
        stale = {"id": "reading_stale", "level": "warn", "text": "The reading is 11 min old", "since": int(h.clock()) - 100}
        a, _ = h.run(healthy(addon_alerts={"demo": [{"id": "chain", "hold": True}, stale]}), 5)
        self.assertEqual(set(a), {"reading_stale"})
        self.assertEqual([x["kind"] for x in h.events[n:]], ["alert"], "chain is held, not cleared")
        a, _ = h.run(healthy(answering=False, addon_alerts={"demo": [{"id": "chain", "hold": True}, stale]}), 600)
        self.assertIn("reading_stale", a, "add-on alerts go on while the node is down")
        n = len(h.events)
        a, _ = h.run(healthy(addon_alerts={"demo": [chain()]}), 5)
        self.assertEqual(set(a), {"chain"})
        self.assertEqual([x["text"] for x in h.events[n:]], ["Cleared: READING STALE"])
        # holding an alert that isn't up does nothing
        a, _ = h.run(healthy(addon_alerts={"demo": [{"id": "nope", "hold": True}]}), 5)
        self.assertEqual(a, {})

    def test_turned_off_add_on_goes_quietly(self):
        h = Harness()
        h.run(healthy(addon_alerts={"demo": [chain()]}))
        n = len(h.events)
        a, _ = h.run(healthy(addon_alerts={}), 5)
        self.assertEqual(a, {})
        self.assertEqual(len(h.events), n, "no Cleared event for an add-on that was turned off")
        a, _ = h.run(healthy(addon_alerts={"demo": [chain()]}), 5)
        self.assertEqual(a["chain"]["since"], int(h.clock()), "back on: a new alert")
        self.assertEqual(h.events[-1]["kind"], "alert")

    def test_empty_list_clears(self):
        h = Harness()
        h.run(healthy(addon_alerts={"demo": [chain()]}))
        a, _ = h.run(healthy(addon_alerts={"demo": []}), 5)
        self.assertEqual(a, {})
        self.assertEqual(h.events[-1]["text"], "Cleared: EXAMPLE CHECK")


if __name__ == "__main__":
    unittest.main()
