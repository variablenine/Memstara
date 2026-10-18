"""Privacy mode (memstara/privacy.py and its use in server.py): with display.privacy on, no
API answer carries an address, port, name, folder or transaction id; the screens get
tokens that stay the same for the same value, and changes that need the real values are
refused until it is turned off with the typed word."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import WRITE, get_json, request
from fake_monerod import FakeMonerod
from memstara.app import App
from memstara.privacy import REVEAL_WORD, TOKEN_RE, Redactor, has_token, utc_clocks, utc_stamps
from memstara.settings import Settings
from memstara.util import clock_style, clock_text

HOME = "/home/alice"
ONION = "xmrabcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstu.onion"
ADDRESS = "4" + "8Aa1" * 23 + "Bc"          # 95 base58 characters, a standard address's shape
TXID = "5f2c" + "ab" * 30
BLOCK = "8f5c306dc08bf4464c21db13f5a5ccdc9e8fcad268a0ebbeaee3d4f956f9d669"


def redactor(**kw):
    return Redactor(**dict({"home": HOME, "user": "alice", "real_name": "Alice Example",
                            "host_name": "AliceTower", "key": b"k" * 32}, **kw))


def strings(v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield k
            yield from strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from strings(x)
    elif isinstance(v, str):
        yield v


class TokenTest(unittest.TestCase):
    def test_same_value_same_token_and_kinds_apart(self):
        r = redactor()
        a = r.token("ip", "100.90.146.87")
        self.assertRegex(a, TOKEN_RE)
        self.assertEqual(a, r.token("ip", "100.90.146.87"))
        self.assertNotEqual(a, r.token("ip", "100.79.232.34"))
        self.assertNotEqual(r.token("port", "18080"), r.token("id", "18080"), "the kind goes into the hash")
        self.assertNotEqual(a, Redactor(key=b"j" * 32, home=HOME, user="", real_name="", host_name="x").token("ip", "100.90.146.87"),
                            "another start (another key), other tokens")
        self.assertEqual(r.token("host", "AliceTower"), r.token("host", "alicetower"))

    def test_has_token(self):
        r = redactor()
        self.assertTrue(has_token({"hosts": ["1.2.3.4", r.token("ip", "5.6.7.8")]}))
        self.assertFalse(has_token({"hosts": ["1.2.3.4"], "n": 5}))


class ScrubTest(unittest.TestCase):
    def setUp(self):
        self.r = redactor()
        self.s = self.r.scrub
        self.t = self.r.token

    def test_addresses(self):
        t = self.t
        self.assertEqual(self.s("Banned 100.90.146.87 for 24 h."), "Banned %s for 24 h." % t("ip", "100.90.146.87"))
        self.assertEqual(self.s("[100.90.146.87:18080 OUT]"), "[%s:%s OUT]" % (t("ip", "100.90.146.87"), t("port", "18080")),
                         "a port after a hidden address goes with it")
        self.assertEqual(self.s("[2001:db8::1]:18080"), "[%s]:%s" % (t("ip6", "2001:db8::1"), t("port", "18080")))
        self.assertEqual(self.s("[2001:db8::1]"), "[%s]" % t("ip6", "2001:db8::1"))
        self.assertEqual(self.s("1.2.3.4:99999"), t("ip", "1.2.3.4") + ":99999", "not a port")
        self.assertEqual(self.s("peer 2001:DB8:0::5. done"), "peer %s. done" % t("ip6", "2001:db8::5"))
        self.assertEqual(self.s("fe80::1%wlan0"), t("ip6", "fe80::1"))
        self.assertEqual(self.s("fe80::1%wlan0:18080"), "%s:%s" % (t("ip6", "fe80::1"), t("port", "18080")))
        self.assertEqual(self.s("::ffff:100.90.146.87"), t("ip", "100.90.146.87"), "mapped: the IPv4 token")
        self.assertEqual(self.s("::ffff:100.90.146.87:18080"), "%s:%s" % (t("ip", "100.90.146.87"), t("port", "18080")))
        self.assertEqual(self.s("100.090.146.087"), t("ip", "100.90.146.87"), "leading zeros")
        self.assertEqual(self.s(ONION + ":18084"), t("tor", ONION) + ":18084")
        self.assertEqual(self.s("abcdefghijklmnop.onion"), t("tor", "abcdefghijklmnop.onion"))
        self.assertEqual(self.s("x" * 52 + ".b32.i2p"), t("i2p", "x" * 52 + ".b32.i2p"))
        self.assertEqual(self.s("aa:bb:cc:dd:ee:ff"), t("mac", "aa:bb:cc:dd:ee:ff"))

    def test_ranges_keep_their_shape(self):
        t = self.t
        self.assertEqual(self.s("100.110.0.0/16"), t("n16", "100.110") + ".0.0/16")
        self.assertEqual(self.s("62.233.57.0/24"), t("n24", "62.233.57") + ".0/24")
        self.assertEqual(self.s("62.233.57.7/24"), t("n24", "62.233.57") + ".0/24")
        self.assertEqual(self.s("1.2.3.4/32"), t("ip", "1.2.3.4") + "/32")
        self.assertEqual(self.s("62.233.057.0/24"), t("n24", "62.233.57") + ".0/24", "leading zeros")
        self.assertEqual(self.s("10.0.0.0/8"), t("net", "10.0.0.0/8"))
        self.assertEqual(self.s("100.110.x.x, 1.2.3.x and 5.6.*.*."),
                         "%s.x.x, %s.x and %s.*.*." % (t("n16", "100.110"), t("n24", "1.2.3"), t("n16", "5.6")))

    def test_left_alone(self):
        for text in ("Reading 127.0.0.1:18081", "monerod 0.18.5.3-release on Tor 0.4.8.12", "::1 and ::",
                     "23:00:34 12:34:56.789", "1.2.3.4.5", "300.1.1.1", "export 1234 transport 25 Portugal 99",
                     "THE MONERO NETWORK", "net.p2p::foo src/p2p/net_node.inl:1234", "RPC v3.16 · RULES v16"):
            self.assertEqual(self.s(text), text, text)

    def test_names_and_folders(self):
        t = self.t
        self.assertEqual(self.s("/home/alice/.bitmonero/bitmonero.log"), "~/.bitmonero/bitmonero.log")
        self.assertEqual(self.s("/home/alice"), "~")
        self.assertEqual(self.s("/home/alicex/a"), "/home/alicex/a", "someone else's home")
        self.assertEqual(self.s("/media/alice/USB"), "/media/%s/USB" % t("user", "alice"))
        self.assertEqual(self.s("AliceTower monerod[12]: Alice and ALICE"),
                         "%s monerod[12]: %s and %s" % (t("host", "alicetower"), t("user", "alice"), t("user", "alice")))
        self.assertEqual(self.s("Example"), t("name", "example"))
        self.assertEqual(self.s("Aliceland"), "Aliceland", "whole words only")
        r = Redactor(home="/home/user", user="user", real_name="", host_name="ubuntu", key=b"k" * 32)
        self.assertEqual(r.scrub("user ubuntu /home/user/x"), "user ubuntu ~/x", "common words stay; the home folder goes")

    def test_ports_and_ids(self):
        t = self.t
        self.assertEqual(self.s("relay-7 · PORT 47213"), "relay-7 · PORT %s" % t("port", "47213"),
                         "a name nothing has reported stays")
        self.assertEqual(self.s("on port 47213."), "on port %s." % t("port", "47213"))
        self.assertEqual(self.s("ports 18080, 47213 and 18089"),
                         "ports %s, %s and %s" % (t("port", "18080"), t("port", "47213"), t("port", "18089")))
        self.assertEqual(self.s("tx %s in" % TXID), "tx %s in" % t("hex", TXID))
        self.assertEqual(self.s("mining to " + ADDRESS), "mining to " + t("addr", ADDRESS))
        self.assertEqual(self.s("http://me:secret@127.0.0.1:18081"), "http://%s@127.0.0.1:18081" % t("login", "me:secret@"))

    def test_where_the_node_listens(self):
        t = self.t
        # a port after 0.0.0.0 or :: (every address here) is where monerod listens: its own p2p port
        self.assertEqual(self.s("Binding (IPv4) on 0.0.0.0:47213"), "Binding (IPv4) on 0.0.0.0:%s" % t("port", "47213"))
        self.assertEqual(self.s("on [::]:47213."), "on [::]:%s." % t("port", "47213"))
        self.assertEqual(self.s("[::ffff:0.0.0.0]:47213"), "[::ffff:0.0.0.0]:%s" % t("port", "47213"))
        self.assertEqual(self.s("Binding (IPv6) on :::47213"), "Binding (IPv6) on :::%s" % t("port", "47213"))
        self.assertEqual(self.s("::ffff:0.0.0.0:47213"), "::ffff:0.0.0.0:%s" % t("port", "47213"))
        self.assertEqual(self.s("External port defined as 47213"), "External port defined as %s" % t("port", "47213"))
        # an address of its own: hidden, and the port after it with it
        self.assertEqual(self.s("Binding (IPv6) on 2001:db8::5:47213"),
                         "Binding (IPv6) on %s:%s" % (t("ip6", "2001:db8::5"), t("port", "47213")))
        # the address itself stays, and so do loopback addresses with their ports (the RPC address
        # on this computer is shown on purpose)
        for text in ("0.0.0.0 and :: alone", "RPC on 127.0.0.1:18081", "[::1]:18081", "::1:18081", "127.0.0.2:18089",
                     "0.0.0.0:99999", "47213 alone"):
            self.assertEqual(self.s(text), text, text)

    def test_the_nodes_computer_by_name(self):
        r = self.r
        text = "The RPC address node-box.lan:18081 isn't on this computer, so the node may run on another one"
        self.assertIn("node-box.lan", r.scrub(text))
        r.learn({"source": {"rpc": "node-box.lan:18081"}})
        self.assertEqual(r.scrub(text), text.replace("node-box.lan", r.token("host", "node-box.lan")))
        for rpc in ("127.0.0.1:18081", "192.168.1.50:18081", "[2001:db8::5]:18081", "localhost:18081"):
            r = redactor()
            r.learn({"source": {"rpc": rpc}})
            self.assertEqual(len(r._names), 3, rpc)   # this computer's two names and the user's
        r.learn({"peers": {"list": [{"port": 18080}]}})
        self.assertEqual(r.scrub("peer on 18080"), "peer on 18080", "only the RPC address is learned from")

    def test_nothing_found_inside_a_token(self):
        r = self.r
        r.learn({"source": {"rpc": "node-box.lan:18081"}})
        tok = r.token("hex", "z")
        self.assertEqual(r.scrub("a %s b" % tok), "a %s b" % tok)
        for _ in range(3):
            once = r.scrub("PORT 47213 at 1.2.3.4 on AliceTower via node-box.lan and 0.0.0.0:18080")
            self.assertEqual(r.scrub(once), once, "scrubbing again changes nothing")


class AnswerTest(unittest.TestCase):
    def setUp(self):
        self.r = redactor()
        self.t = self.r.token

    def test_keys(self):
        t = self.t
        snap = {
            "source": {"rpc": "127.0.0.1:18081"},
            "node": {"top_hash": BLOCK, "ban_list": [{"host": "62.233.57.0/24", "seconds": 60}]},
            "chain": {"blocks": [{"hash": BLOCK, "height": 5}]},
            "peers": {"list": [{"id": "e547c9c2", "ip": "100.90.146.87", "host": "100.90.146.87", "port": 18080,
                                "net16": "100.79", "net24": "100.79.232"}],
                      "spread": {"out16": {"net": "100.79", "n": 1, "ips": ["100.90.146.87"]}}},
        }
        orig = copy.deepcopy(snap)
        out = json.loads(self.r.snapshot_json(snap))
        self.assertEqual(snap, orig, "the poller's snapshot is left as it was")
        self.assertIs(out["privacy"], True)
        self.assertEqual(out["source"], {"rpc": "127.0.0.1:18081"}, "the RPC address on this computer is shown")
        self.assertEqual(out["node"]["top_hash"], BLOCK, "block hashes are public")
        self.assertEqual(out["chain"]["blocks"][0]["hash"], BLOCK)
        self.assertEqual(out["node"]["ban_list"][0]["host"], t("n24", "62.233.57") + ".0/24")
        p = out["peers"]["list"][0]
        self.assertEqual(p, {"id": "e547c9c2", "ip": t("ip", "100.90.146.87"), "host": t("ip", "100.90.146.87"),
                             "port": t("port", "18080"), "net16": t("n16", "100.79"), "net24": t("n24", "100.79.232")})
        self.assertEqual(out["peers"]["spread"]["out16"]["net"], t("n16", "100.79"))
        # the window's range keys line up: a /24 built from net24 matches the ban list's
        self.assertEqual(p["net24"] + ".0/24", out["node"]["ban_list"][0]["host"].replace(t("n24", "62.233.57"), p["net24"]))
        self.assertIs(self.r.snapshot_json(snap), self.r.snapshot_json(snap), "made once per snapshot")

    def test_mempool(self):
        t = self.t
        snap = {"mempool": {"txs": 3, "not_relayed": 1, "list": [["aaaa1111bbbb2222", 100, 5, 9, 0],
                                                                 ["cccc3333dddd4444", 101, 6, 9, 2],
                                                                 ["eeee5555ffff6666", 102, 7, 9, 1 | 4]],
                            "mined": [{"height": 9, "ids": ["aaaa1111bbbb2222"]}]}}
        m = json.loads(self.r.snapshot_json(snap))["mempool"]
        self.assertEqual(m["list"], [[t("tx", "aaaa1111bbbb2222"), 100, 5, 9, 0], [t("tx", "eeee5555ffff6666"), 102, 7, 9, 5]],
                         "one only this node has (not passed on) is left out")
        self.assertEqual(m["mined"], [{"height": 9, "ids": [t("tx", "aaaa1111bbbb2222")]}])
        self.assertIsNone(m["not_relayed"])
        self.assertEqual(m["txs"], 3)

    def test_command_answers(self):
        t = self.t
        res = {"get_bans": {"bans": [{"host": "1.2.3.4", "ip": 67305985, "seconds": 9}]},
               "get_peer_list": {"white_list": [{"host": "5.6.7.8", "id": 123456789, "ip": 134678021, "port": 18080, "rpc_port": 18089}]},
               "get_connections": {"connections": [{"address": "5.6.7.8:18080", "peer_id": "74a03e3f67d67ebe",
                                                    "connection_id": "d1674bf06521540a3b179a3412b7c219"}]},
               "get_block": {"block_header": {"hash": BLOCK, "miner_tx_hash": TXID}, "tx_hashes": [TXID]},
               "mining_status": {"address": ADDRESS},
               "get_info": {"bootstrap_daemon_address": "node.example.com:18089", "top_block_hash": BLOCK}}
        out = self.r.redact({"ok": True, "result": res, "args": {"hosts": ["1.2.3.4"]}})["result"]
        self.assertEqual(out["get_bans"]["bans"][0], {"host": t("ip", "1.2.3.4"), "ip": t("ip", "1.2.3.4"), "seconds": 9},
                         "monerod's number form of an address gets the same token")
        pl = out["get_peer_list"]["white_list"][0]
        self.assertEqual(pl, {"host": t("ip", "5.6.7.8"), "id": t("id", 123456789), "ip": t("ip", "5.6.7.8"),
                              "port": t("port", 18080), "rpc_port": t("port", 18089)})
        c = out["get_connections"]["connections"][0]
        self.assertEqual(c["peer_id"], t("id", "74a03e3f67d67ebe"))
        self.assertEqual(c["connection_id"], "d1674bf06521540a3b179a3412b7c219", "a connection's own id is random")
        self.assertEqual(out["get_block"]["block_header"]["hash"], BLOCK)
        self.assertEqual(out["get_block"]["block_header"]["miner_tx_hash"], t("hex", TXID))
        self.assertEqual(out["get_block"]["tx_hashes"], [t("hex", TXID)])
        self.assertEqual(out["mining_status"]["address"], t("addr", ADDRESS))
        self.assertEqual(out["get_info"], {"bootstrap_daemon_address": t("host", "node.example.com") + ":18089",
                                           "top_block_hash": BLOCK})

    def test_settings_by_type(self):
        t = self.t
        pub = {"values": {"source.rpc_url": "http://node.example.com:18081", "source.rpc_user": "alice",
                          "source.rpc_password": "", "node.log_path": HOME + "/logs/monerod.log",
                          "example.url": "http://127.0.0.1:18090/readings.json", "server.port": 18200,
                          "display.accent": "amber", "thresholds.low_peers": 4},
               "schema": [{"key": "source.rpc_url", "type": "url", "help": "for example http://127.0.0.1:18081, port 18081"},
                          {"key": "source.rpc_user", "type": "text"}, {"key": "source.rpc_password", "type": "password"},
                          {"key": "node.log_path", "type": "path"}, {"key": "example.url", "type": "url"},
                          {"key": "server.port", "type": "int"}, {"key": "display.accent", "type": "choice"},
                          {"key": "thresholds.low_peers", "type": "int"}],
               "rpc_password_set": True}
        out = self.r.settings(pub)
        self.assertEqual(out["values"], {"source.rpc_url": "http://%s:18081" % t("host", "node.example.com"),
                                         "source.rpc_user": t("user", "alice"), "source.rpc_password": "",
                                         "node.log_path": "~/logs/monerod.log",
                                         "example.url": "http://127.0.0.1:18090/readings.json", "server.port": 18200,
                                         "display.accent": "amber", "thresholds.low_peers": 4})
        self.assertEqual(out["schema"], pub["schema"], "the schema is fixed text")

    def test_seeded_values_never_get_through(self):
        """Every private value planted anywhere in an answer, in keys the redactor knows and
        in free text it doesn't, is gone from the JSON that goes out."""
        r = self.r
        seeds = ["100.90.146.87", "100.79.232", "62.233.57", "2001:db8::77", ONION, "AliceTower", "alice",
                 "Alice", "/home/alice", "47213", ADDRESS, TXID, "aa:bb:cc:dd:ee:ff", "secretpw"]
        snap = {
            "events": [{"text": "Banned 100.90.146.87 (port 47213) for AliceTower"}],
            "alerts": [{"text": "x", "details": ["100.90.146.87", "[2001:db8::77]:18080"]}],
            "lamps": [{"value": "PORT 47213"}],
            "node": {"address": "2001:db8::77", "inbound": ["--anonymous-inbound", ONION + ":18084,127.0.0.1:18084"]},
            "something_new": {"deep": [["mining to " + ADDRESS, "tx " + TXID, "aa:bb:cc:dd:ee:ff", "/home/alice/x",
                                        "62.233.57.0/24", "100.79.232.x", "Alice's", "http://u:secretpw@1.2.3.4/"]]},
        }
        text = r.snapshot_json(snap).decode()
        for s in seeds:
            self.assertNotIn(s, text, s)


@contextlib.contextmanager
def zone(tz):
    """This process's time zone set to `tz` for a while."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


class ClockTest(unittest.TestCase):
    """Privacy mode shows every clock in UTC, so a screenshot doesn't give the time zone away."""

    T = 1760292000   # 2025-10-12 18:00 UTC, 8:00 PM in Berlin (summer time, UTC+2)

    def test_clock_style(self):
        self.assertEqual(clock_style({}), "12h")
        self.assertEqual(clock_style({"display.clock": "24h"}), "24h")
        self.assertEqual(clock_style({"display.clock": "24h", "display.privacy": True}), "24h-utc")
        self.assertEqual(clock_style({"display.privacy": "yes"}), "12h", "only a real true turns it on")
        with zone("Europe/Berlin"):
            self.assertEqual(clock_text(self.T, "12h"), "8:00 PM")
            self.assertEqual(clock_text(self.T, "12h-utc"), "6:00 PM UTC")
            self.assertEqual(clock_text(self.T, "24h-utc"), "18:00 UTC")

    def test_event_texts_go_to_utc(self):
        with zone("Europe/Berlin"):
            self.assertEqual(utc_clocks("Node restarted at 6:58 AM", self.T), "Node restarted at 4:58 AM UTC")
            self.assertEqual(utc_clocks("restarted at 23:10, again at 0:05", self.T), "restarted at 21:10 UTC, again at 0:05")
            self.assertEqual(utc_clocks("at 12:30 AM and 11:45 PM", self.T), "at 10:30 PM UTC and 9:45 PM UTC")
            for same in ("already 6:58 AM UTC", "v0.18.5.3", "height 3,456,789", "ratio 1:30:45", "12:345"):
                self.assertEqual(utc_clocks(same, self.T), same)
            winter = 1765562400   # 2025-12-12 18:00 UTC: Berlin is UTC+1 then
            self.assertEqual(utc_clocks("at 12:00 PM", winter), "at 11:00 AM UTC")
            r = redactor()
            out = r.redact({"events": [{"t": self.T, "kind": "restart", "level": "info", "text": "Node restarted at 6:58 AM"}],
                            "lamps": [{"value": "SEEN 6:58 AM"}]})
            self.assertEqual(out["events"][0]["text"], "Node restarted at 4:58 AM UTC")
            self.assertEqual(out["lamps"][0]["value"], "SEEN 6:58 AM", "only events, the rest is written in UTC already")

    def test_a_snapshot_from_before_the_switch(self):
        """The first snapshot after turning it on can still have been written in local time."""
        with zone("Europe/Berlin"):
            r = redactor()
            stale = json.loads(r.snapshot_json({"clock_style": "12h", "lamps": [{"value": "SEEN 6:58 AM"}]}))
            self.assertRegex(stale["lamps"][0]["value"], r"^SEEN \d{1,2}:58 [AP]M UTC$")
            fresh = json.loads(r.snapshot_json({"clock_style": "12h-utc", "lamps": [{"value": "SEEN 6:58 AM UTC"}]}))
            self.assertEqual(fresh["lamps"][0]["value"], "SEEN 6:58 AM UTC")

    def test_log_stamps(self):
        with zone("Europe/Berlin"):
            self.assertEqual(utc_stamps("  [backup 2025-10-12 20:00:05] copied the wallet folder"),
                             "  [backup 2025-10-12 18:00:05 UTC] copied the wallet folder")
            self.assertEqual(utc_stamps("2026-01-01 00:30 done"), "2025-12-31 23:30 UTC done")
            for same in ("2025-10-12 18:00:05.123\tI monerod's own line (UTC)", "2025-10-12T18:00:05Z",
                         "2025-10-12 18:00:05 UTC", "2025-10-12 18:00:05+02:00", "no stamp", ""):
                self.assertEqual(utc_stamps(same), same)


class ServerPrivacyTest(unittest.TestCase):
    """The whole API with privacy mode on, against the stand-in node."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(cls.tmp.name))
        cls.home = base / "home" / "alice"
        (cls.home / ".bitmonero").mkdir(parents=True)
        cls.log = cls.home / ".bitmonero" / "bitmonero.log"
        cls.log.write_text("2026-10-12 21:00:00.000\t[P2P1]\tINFO\tnet.p2p\tsrc/p2p/net_node.inl:1\t"
                           "[100.90.146.87:18080 OUT] Binding on 10.20.30.40:47213 for AliceTower "
                           "data %s\n" % (cls.home / ".bitmonero")
                           + "  [backup 2026-10-12 16:05:00] copied the node's settings\n"
                           + "2026-10-12 21:00:01.000\t[P2P1]\tINFO\tnet.p2p\tsrc/p2p/net_node.inl:1\t"
                             "Binding (IPv4) on 0.0.0.0:47213\n")
        cls.fake = FakeMonerod().start()
        cls.settings = Settings(base / "cfg" / "settings.json", log=lambda m: None).load()
        cls.settings.update({"source.rpc_url": cls.fake.url, "node.log_path": str(cls.log)})
        cls.app = App(cls.settings, port=0, state_dir=base / "state", log=lambda m: None)
        cls.app.privacy = Redactor(home=str(cls.home), user="alice", real_name="Alice", host_name="AliceTower")
        cls.app.start(poll=False)
        cls.app.poller.poll_once()
        cls.port = cls.app.port
        conns = helpers.fixture("get_connections.json")
        conns = conns.get("result", conns)["connections"]
        cls.seeds = sorted({c["host"] for c in conns} | {c["peer_id"] for c in conns if c.get("peer_id")}
                           | {"10.20.30.40", "47213", "AliceTower", "alice", str(cls.home)})

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.fake.stop()
        cls.tmp.cleanup()

    def tearDown(self):
        self.settings.update({"display.privacy": False})

    def privacy(self, on, word=None):
        body = {"on": on} if word is None else {"on": on, "confirm": word}
        st, _, raw = request(self.port, "POST", "/api/privacy", body=body, headers=WRITE)
        return st, json.loads(raw)

    def answers(self):
        """Every answer the window can ask for, as text."""
        out = {}
        for path in ("/api/snapshot", "/api/snapshot?after=-1&wait=0", "/api/settings", "/api/history?window=3600",
                     "/api/commands"):
            st, _, raw = request(self.port, "GET", path)
            self.assertEqual(st, 200, path)
            out[path] = raw.decode()
        st, _, raw = request(self.port, "GET", "/api/node-log?source=monerod&lines=50", headers=WRITE)
        self.assertEqual(st, 200, raw[:200])
        out["/api/node-log?source=monerod&lines=50"] = raw.decode()
        for name in ("print_cn", "bans", "sync_info", "print_pl", "status", "version", "print_pool_sh", "hard_fork_info"):
            st, _, raw = request(self.port, "POST", "/api/command", body={"name": name}, headers=WRITE)
            self.assertEqual(st, 200, (name, raw[:200]))
            out["command " + name] = raw.decode()
        st, _, raw = request(self.port, "POST", "/api/test-connection", body={"rpc_url": self.fake.url}, headers=WRITE)
        out["test-connection"] = raw.decode()
        return out

    def test_off_shows_everything(self):
        text = "\n".join(self.answers().values())
        self.assertIn("100.90.146.87", text)
        self.assertIn("47213", text)
        self.assertNotIn("\\u27e6", text)
        self.assertNotIn("⟦", text)

    def test_on_hides_everything(self):
        self.assertEqual(self.privacy(True), (200, {"ok": True, "on": True}))
        answers = self.answers()
        for path, text in answers.items():
            for s in self.seeds:
                self.assertNotIn(s, text, "%s in %s" % (s, path))
        snap = json.loads(answers["/api/snapshot"])
        self.assertIs(snap["privacy"], True)
        self.assertTrue(any(TOKEN_RE.fullmatch(p["host"]) for p in snap["peers"]["list"]))
        self.assertTrue(json.loads(answers["/api/settings"])["values"]["display.privacy"])
        lines = json.loads(answers["/api/node-log?source=monerod&lines=50"])["lines"]
        self.assertTrue(any(TOKEN_RE.search(line) for line in lines), lines)
        self.assertTrue(any(re.search(r"Binding \(IPv4\) on 0\.0\.0\.0:" + TOKEN_RE.pattern, line) for line in lines),
                        "where the node listens: the address shown, its p2p port hidden")
        self.assertTrue(any(line.startswith("2026-10-12 21:00:00.000") for line in lines), "monerod's own times are UTC")
        self.assertTrue(any(re.search(r"\[backup 2026-10-12 \d\d:05:00 UTC\]", line) for line in lines), lines)

    def test_turning_off_needs_the_word(self):
        self.privacy(True)
        st, out = self.privacy(False)
        self.assertEqual((st, out.get("field")), (400, "confirm"))
        st, out = self.privacy(False, "yes")
        self.assertEqual(st, 400)
        self.assertTrue(self.settings.get("display.privacy"))
        self.assertEqual(self.privacy(False, " %s " % REVEAL_WORD.lower()), (200, {"ok": True, "on": False}))
        self.assertFalse(self.settings.get("display.privacy"))
        st, snap = get_json(self.port, "/api/snapshot")
        self.assertNotIn("privacy", snap)
        st, out = self.privacy("yes")
        self.assertEqual(st, 400)

    def test_changes_refused_while_on(self):
        self.privacy(True)
        st, _, raw = request(self.port, "PUT", "/api/settings", body={"values": {"node.log_path": "~/x.log"}}, headers=WRITE)
        self.assertEqual(st, 403, raw)
        self.assertIn("node.log_path", json.loads(raw)["errors"])
        st, _, raw = request(self.port, "PUT", "/api/settings", body={"values": {"display.accent": "ice"}}, headers=WRITE)
        self.assertEqual(st, 200, "the look can still change")
        self.settings.update({"display.accent": "amber"})
        st, _, raw = request(self.port, "PUT", "/api/settings", body={"values": {"display.privacy": False}}, headers=WRITE)
        self.assertEqual(st, 400, "only the switch turns it off")
        self.assertTrue(self.settings.get("display.privacy"))
        st, _, raw = request(self.port, "POST", "/api/settings/reset", body={"section": "node"}, headers=WRITE)
        self.assertEqual(st, 403)
        st, _, raw = request(self.port, "POST", "/api/settings/reset", body={"section": "display"}, headers=WRITE)
        self.assertEqual(st, 200)
        self.assertTrue(self.settings.get("display.privacy"), "reset to defaults keeps privacy mode on")
        st, _, raw = request(self.port, "POST", "/api/command", headers=WRITE, body={"name": "bans"})
        self.assertEqual(st, 200, "the read-only commands still work, with their answers hidden")
        st, _, raw = request(self.port, "POST", "/api/test-connection", headers=WRITE,
                             body={"rpc_url": "http://⟦host_0011aabb⟧:18081"})
        self.assertEqual(st, 400)

    def test_a_failure_sends_nothing(self):
        self.privacy(True)
        with mock.patch.object(self.app.privacy, "redact", side_effect=RuntimeError("boom")):
            st, _, raw = request(self.port, "GET", "/api/history?window=3600")
        self.assertEqual(st, 500)
        self.assertEqual(json.loads(raw), {"error": "Something went wrong on the dashboard's side"})


if __name__ == "__main__":
    unittest.main()
