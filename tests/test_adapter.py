"""The adapter turns realistic monerod answers into the snapshot's shapes."""

from __future__ import annotations

import unittest

import helpers
from helpers import fixture
from memstara import adapter

NOW = helpers.T0


class InfoTest(unittest.TestCase):
    def test_unrestricted(self):
        info = fixture("get_info.json")
        n = adapter.parse_info(info, NOW, 32)
        self.assertEqual(n["height"], info["height"])
        self.assertEqual(info["target_height"], 0)            # monerod sends 0 once synced
        self.assertEqual(n["target_height"], info["height"])  # max(height, target_height)
        self.assertIs(n["synced"], True)
        self.assertEqual(n["sync_pct"], 100.0)
        self.assertEqual(n["version"], "0.18.5.3-release")
        self.assertEqual(n["nettype"], "mainnet")
        self.assertEqual(n["started"], info["start_time"])
        self.assertEqual(n["uptime_s"], int(NOW - info["start_time"]))
        self.assertEqual(n["out_peers"], info["outgoing_connections_count"])
        self.assertEqual(n["in_peers"], info["incoming_connections_count"])
        self.assertEqual(n["free_bytes"], info["free_space"])
        self.assertEqual(n["db_bytes"], info["database_size"])
        self.assertEqual(n["hashrate"], info["difficulty"] // 120)
        self.assertEqual(n["top_hash"], info["top_block_hash"])
        self.assertFalse(n["_restricted"])
        self.assertEqual(n["_median_weight"], 300000)

    def test_restricted_port(self):
        info = fixture("get_info_restricted.json")
        self.assertTrue(info["restricted"])
        n = adapter.parse_info(info, NOW, 32)
        self.assertTrue(n["_restricted"])
        self.assertIsNone(n["free_bytes"], "2^64-1 on a restricted port means unknown")
        self.assertIsNone(n["version"])
        self.assertIsNone(n["started"])
        self.assertIsNone(n["uptime_s"])
        for k in ("out_peers", "in_peers", "rpc_connections", "white_peers", "grey_peers", "alt_blocks"):
            self.assertIsNone(n[k], k)
        self.assertEqual(n["height"], info["height"])        # still known

    def test_syncing(self):
        n = adapter.parse_info(fixture("get_info_syncing.json"), NOW, 32)
        self.assertIs(n["synced"], False)
        self.assertGreater(n["target_height"], n["height"])
        self.assertLess(n["sync_pct"], 100.0)

    def test_answered_by_the_bootstrap_node(self):
        # what monerod sends on while it uses a bootstrap node (a public, restricted one here):
        # only height_without_bootstrap and bootstrap_daemon_address are the local node's
        info = dict(fixture("get_info_restricted.json"), untrusted=True, height_without_bootstrap=3775041,
                    bootstrap_daemon_address="node.example.org:18089", was_bootstrap_ever_used=True,
                    synchronized=True, target_height=0)
        n = adapter.parse_info(info, NOW, 32)
        self.assertEqual((n["height"], n["target_height"], n["synced"]), (3775041, info["height"], False))
        self.assertEqual(n["sync_pct"], round(3775041 * 100.0 / info["height"], 2))
        self.assertEqual((n["_bootstrap"], n["_restricted"], n["out_wanted"]), ("node.example.org:18089", False, 32))
        for k in ("version", "nettype", "started", "top_hash", "difficulty", "out_peers", "in_peers", "tx_pool",
                  "free_bytes", "db_bytes", "update_available", "_median_weight"):
            self.assertIsNone(n[k], k)
        self.assertEqual(set(n), set(adapter.parse_info(fixture("get_info.json"), NOW, 32)))
        self.assertIsNone(adapter.parse_info(fixture("get_info.json"), NOW, 32)["_bootstrap"])

    def test_missing_fields_are_null(self):
        n = adapter.parse_info({"status": "OK"}, NOW, 32)
        for k in ("height", "target_height", "synced", "version", "free_bytes", "difficulty", "hashrate"):
            self.assertIsNone(n[k], k)


class PeersTest(unittest.TestCase):
    def test_connections(self):
        raw = fixture("get_connections.json")
        conns = raw["connections"]
        p = adapter.parse_connections(raw)
        self.assertEqual(len(p["list"]), len(conns))
        keys = {"id", "dir", "kind", "ip", "host", "port", "net16", "net24", "age_s", "height", "state",
                "down_bps", "up_bps", "recv_bytes", "sent_bytes", "recv_idle_s", "send_idle_s", "pruned"}
        for peer, c in zip(p["list"], conns):
            self.assertEqual(set(peer), keys)
            self.assertEqual(peer["id"], c["connection_id"][:8])
            self.assertEqual(peer["dir"], "in" if c["incoming"] else "out")
            self.assertEqual(peer["down_bps"], c["current_download"] * 1024)
            self.assertEqual(peer["up_bps"], c["current_upload"] * 1024)
            self.assertEqual(peer["recv_bytes"], c["recv_count"])
            self.assertEqual(peer["sent_bytes"], c["send_count"])
            self.assertEqual(peer["recv_idle_s"], c["recv_idle_time"])
            self.assertEqual(peer["send_idle_s"], c["send_idle_time"])
            self.assertEqual(peer["age_s"], c["live_time"])
            self.assertEqual(peer["pruned"], c["pruning_seed"] != 0)
            self.assertIsInstance(peer["port"], int)
            if c["address_type"] == 4:
                self.assertEqual(peer["kind"], "tor")
                self.assertEqual(peer["ip"], "")
                self.assertTrue(peer["host"].endswith(".onion"))
                self.assertEqual(len(peer["host"]), 56 + len(".onion"))
                self.assertEqual(peer["port"], 18084)   # from "address", monerod leaves "port" empty
                self.assertEqual(peer["net16"], "")
            elif c["address_type"] == 2:
                self.assertEqual(peer["kind"], "ipv6")
                self.assertTrue(peer["ip"].startswith("2001:db8:"))
                self.assertEqual(peer["net24"], "")
            else:
                self.assertEqual(peer["kind"], "ipv4")
                self.assertTrue(peer["ip"].startswith("100."))
                self.assertEqual(peer["net16"], ".".join(peer["ip"].split(".")[:2]))
                self.assertEqual(peer["net24"], ".".join(peer["ip"].split(".")[:3]))
        self.assertEqual(sum(p["out_by_kind"].values()) + sum(p["in_by_kind"].values()), len(conns))
        self.assertGreater(p["out_by_kind"]["tor"], 0)
        self.assertEqual(set(p["out_by_kind"]), {"ipv4", "ipv6", "tor", "i2p"})
        for key in ("out16", "in24"):
            sp = p["spread"][key]
            self.assertEqual(set(sp), {"net", "n", "ips"})
            self.assertEqual(sp["n"], len(sp["ips"]))

    def test_spread_cluster(self):
        conns = [{"incoming": False, "address_type": 1, "host": "100.66.%d.9" % i, "ip": "100.66.%d.9" % i,
                  "port": "18080", "connection_id": "%032x" % i} for i in range(5)]
        conns += [{"incoming": True, "address_type": 1, "host": "100.70.3.%d" % i, "ip": "100.70.3.%d" % i,
                   "port": "51000", "connection_id": "%032x" % (100 + i)} for i in (4, 12, 30)]
        conns += [{"incoming": True, "address_type": 4, "host": "x" * 56 + ".onion", "ip": "",
                   "address": "x" * 56 + ".onion:18084", "port": "", "connection_id": "f" * 32}]
        sp = adapter.parse_connections({"connections": conns})["spread"]
        self.assertEqual(sp["out16"], {"net": "100.66", "n": 5,
                                       "ips": ["100.66.0.9", "100.66.1.9", "100.66.2.9", "100.66.3.9", "100.66.4.9"]})
        self.assertEqual(sp["in24"], {"net": "100.70.3", "n": 3, "ips": ["100.70.3.4", "100.70.3.12", "100.70.3.30"]})

    def test_empty_list_left_out(self):
        # epee leaves an empty list out of the answer entirely
        p = adapter.parse_connections({"status": "OK", "untrusted": False})
        self.assertEqual(p["list"], [])
        self.assertEqual(p["spread"]["out16"], {"net": "", "n": 0, "ips": []})


class PoolChainTest(unittest.TestCase):
    def test_pool(self):
        raw = fixture("get_transaction_pool_stats.json")
        ps = raw["pool_stats"]
        m = adapter.parse_pool(raw, NOW)
        self.assertEqual(m["txs"], ps["txs_total"])
        self.assertEqual(m["bytes"], ps["bytes_total"])
        self.assertEqual(m["oldest_age_s"], int(NOW - ps["oldest"]))
        self.assertEqual(m["histogram"], ps["histo"])
        self.assertEqual(m["histo_98pc"], ps["histo_98pc"])
        self.assertEqual(m["over_10m"], ps["num_10m"])
        self.assertEqual(m["double_spends"], 0)
        empty = adapter.parse_pool({"pool_stats": {"txs_total": 0, "oldest": 0}}, NOW)
        self.assertIsNone(empty["oldest_age_s"])
        self.assertEqual(empty["histogram"], [])

    def test_headers_and_tape(self):
        raw = fixture("get_block_headers_range.json")
        hs = adapter.parse_headers(raw)
        blocks = adapter.blocks_from_headers(hs, 60)
        self.assertEqual(len(blocks), len(raw["headers"]))
        self.assertEqual([b["height"] for b in blocks], sorted(b["height"] for b in blocks))
        self.assertIsNone(blocks[0]["interval_s"])
        for a, b in zip(blocks, blocks[1:]):
            self.assertEqual(b["interval_s"], b["time"] - a["time"])
        self.assertEqual(set(blocks[0]), {"height", "hash", "time", "interval_s", "seen", "seen_interval_s",
                                          "weight", "txs", "reward"})
        self.assertTrue(all(b["seen"] is None and b["seen_interval_s"] is None for b in blocks),
                        "when a block arrived is only known for blocks the dashboard saw arrive")
        # the arrival times given: the last two seen arriving, 95.5 s apart
        seen = {hs[-2]["hash"]: 1000.25, hs[-1]["hash"]: 1095.75}
        tape = adapter.blocks_from_headers(hs, 3, seen)
        self.assertEqual([(b["seen"], b["seen_interval_s"]) for b in tape],
                         [(None, None), (1000.2, None), (1095.8, 95.5)])
        self.assertEqual([b["interval_s"] for b in tape], [b["interval_s"] for b in blocks[-3:]],
                         "the miner's stamps stay as they are")
        self.assertEqual(len(adapter.blocks_from_headers(hs, 3)), 3)
        self.assertIsNotNone(adapter.avg_interval(blocks))
        self.assertEqual(adapter.wait_odds(0), 1.0)
        self.assertEqual(adapter.wait_odds(120), 0.368)

    def test_find_reorg(self):
        hs = adapter.parse_headers(fixture("get_block_headers_range.json"))
        stored = {h["height"]: h["hash"] for h in hs}
        self.assertEqual(adapter.find_reorg(stored, hs), (0, None))
        changed = [dict(h) for h in hs]
        for h in changed[-3:]:
            h["hash"] = "0" * 64
        depth, low = adapter.find_reorg(stored, changed)
        self.assertEqual((depth, low), (3, changed[-3]["height"]))

    def test_small_readings(self):
        self.assertEqual(adapter.rpc_version_text(fixture("get_version.json")["version"]), "3.16")
        self.assertEqual(adapter.parse_fees(fixture("get_fee_estimate.json")), [20000, 80000, 320000, 4000000])
        self.assertEqual(adapter.parse_limit(fixture("get_limit.json")), (2048 * 1024, 8192 * 1024))
        bans = fixture("get_bans.json")
        total, ranges = adapter.parse_bans(bans)
        self.assertEqual(total, len(bans["bans"]))
        self.assertEqual(ranges, sum(1 for b in bans["bans"] if "/" in b["host"]))
        self.assertEqual(adapter.parse_bans({"status": "OK"}), (0, 0))
        short, long_n = adapter.parse_ban_list(bans)
        secs = [b["seconds"] for b in bans["bans"]]
        self.assertEqual(long_n, sum(1 for x in secs if x > 86400))
        self.assertGreater(long_n, 40, "--ban-list entries (huge seconds) and the DNS blocklist count as long")
        self.assertEqual(len(short), sum(1 for x in secs if x <= 86400))
        self.assertEqual(short[0], {"host": "100.95.48.136", "seconds": 77754}, "longest first")
        self.assertIn({"host": "62.233.57.0/24", "seconds": 75600}, short, "ranges are listed too")
        self.assertEqual([b["seconds"] for b in short], sorted((b["seconds"] for b in short), reverse=True))
        self.assertTrue(all(set(b) == {"host", "seconds"} for b in short))
        # counted down since get_bans was read; gone at zero
        later, _ = adapter.parse_ban_list(bans, elapsed=6000)
        self.assertEqual(later[0]["seconds"], 77754 - 6000)
        self.assertNotIn("100.117.111.91", [b["host"] for b in later], "had 5,248 s left")
        self.assertEqual(adapter.parse_ban_list({"status": "OK"}), ([], 0))
        self.assertEqual(adapter.parse_ban_list({"bans": [{"host": "1.2.3.4"}, "junk", {"seconds": 5}]}), ([], 0))
        s = adapter.parse_sync_info(fixture("sync_info.json"))
        self.assertGreater(s["spans"], 0)
        self.assertGreater(s["blocks_queued"], 0)


if __name__ == "__main__":
    unittest.main()
