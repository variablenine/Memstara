"""Every monerod console command: the registry, argument checks, confirm words, and
the exact requests each one sends (checked against the test-only fake monerod)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import WRITE, StubDiscover, StubHost, found_node, get_json, request
from fake_monerod import CHAIN_TX, POOL_TX, FakeMonerod
from memstara import commands as C
from memstara.app import App
from memstara.poller import POLLER_CALLS
from memstara.rpc import READ_ALLOWED, WRITE_ALLOWED
from memstara.settings import Settings

# src/daemon/command_server.cpp (v0.18.5.3), without help and apropos (the drawer's search box)
CONSOLE = {
    "print_height", "print_pl", "print_pl_stats", "print_cn", "print_net_stats", "print_bc", "print_block",
    "print_tx", "is_key_image_spent", "start_mining", "stop_mining", "mining_status", "print_pool",
    "print_pool_sh", "print_pool_stats", "show_hr", "hide_hr", "save", "set_log", "diff", "status", "stop_daemon",
    "exit", "print_status", "limit", "limit_up", "limit_down", "out_peers", "in_peers", "hard_fork_info", "bans",
    "ban", "unban", "banned", "flush_txpool", "output_histogram", "print_coinbase_tx_sum", "alt_chain_info",
    "bc_dyn_stats", "update", "relay_tx", "sync_info", "pop_blocks", "rpc_payments", "version",
    "prune_blockchain", "check_blockchain_pruning", "set_bootstrap_daemon", "flush_cache"}
MAIN = "4" + "A" * 94          # shape of a standard mainnet address (monerod checks the checksum)


_TMP = []


def tearDownModule():
    for tmp in _TMP:
        tmp.cleanup()


def make_settings(url, **values):
    tmp = tempfile.TemporaryDirectory()
    _TMP.append(tmp)
    s = Settings(Path(tmp.name) / "settings.json", log=lambda m: None).load()
    s.update(dict({"source.rpc_url": url}, **values))
    return s


class StubPoller:
    def __init__(self, own=(), own_known=True):
        self.own = None if own is None else list(own)
        self.own_known = own_known
        self.events = []
        self.rereads = []

    def protected_addresses(self):
        return None if self.own is None else list(self.own)

    def own_address_known(self):
        return self.own_known

    def add_event(self, kind, text, level="info", t=None):
        self.events.append((kind, level, text))

    def reread(self, methods=("get_bans", "get_limit")):
        self.rereads.append(tuple(methods))


class RegistryTest(unittest.TestCase):
    def test_every_console_command(self):
        self.assertEqual(set(C.REGISTRY) | set(C.ALIASES), CONSOLE)
        self.assertEqual(C.ALIASES, {"exit": "stop_daemon", "print_pool_sh": "print_pool"})

    def test_registry_matches_the_allowlist(self):
        self.assertEqual(C.check_registry(), [])
        self.assertEqual(C.registry_calls() | set(POLLER_CALLS), set(READ_ALLOWED) | set(WRITE_ALLOWED),
                         "the allowlist is exactly the commands' calls plus the poller's")
        self.assertEqual(set(POLLER_CALLS) - C.registry_calls(),
                         {"get_last_block_header", "get_version", "get_transaction_pool_hashes"})
        self.assertFalse(set(POLLER_CALLS) & set(WRITE_ALLOWED), "the poller only reads")
        for cmd in C.REGISTRY.values():
            self.assertTrue(cmd.calls, cmd.name)
            if cmd.kind == "change":
                self.assertRegex(cmd.confirm, r"^[A-Z]{2,10}$", cmd.name)
            else:
                self.assertIsNone(cmd.confirm, cmd.name)
                # a read command never needs a method that only call_write sends, except the
                # console's own read-only uses of prune_blockchain (check: true)
                writes = set(cmd.calls) & set(WRITE_ALLOWED)
                self.assertTrue(not writes or cmd.name == "check_blockchain_pruning", cmd.name)

    def test_listing_shape(self):
        out = C.Commands(make_settings("http://127.0.0.1:1")).listing()
        json.dumps(out)
        cmds = out["commands"]
        self.assertEqual(len(cmds), len(C.REGISTRY))
        keys = {"name", "aliases", "group", "title", "summary", "usage", "kind", "confirm", "warn", "read_when",
                "calls", "params"}
        pkeys = {"name", "label", "type", "required", "min", "max", "choices", "placeholder", "help", "default"}
        groups = []
        for c in cmds:
            self.assertEqual(set(c), keys, c["name"])
            self.assertIn(c["kind"], ("read", "change"))
            if c["group"] not in groups:
                groups.append(c["group"])
            for p in c["params"]:
                self.assertEqual(set(p), pkeys, (c["name"], p["name"]))
                self.assertIn(p["type"], ("int", "text", "choice", "bool", "lines"))
        self.assertEqual(groups, C.GROUPS)
        by = {c["name"]: c for c in cmds}
        self.assertEqual(by["ban"]["confirm"], "BAN")
        self.assertEqual(by["ban"]["calls"], ["set_bans"])
        self.assertEqual(by["limit"]["read_when"], {"kbps": None})
        self.assertEqual(by["update"]["read_when"], {"command": "check"})
        self.assertEqual(by["stop_daemon"]["aliases"], ["exit"])
        self.assertIsNone(by["bans"]["read_when"])

    def test_warnings(self):
        by = {c["name"]: c for c in C.Commands(make_settings("http://127.0.0.1:1")).listing()["commands"]}
        self.assertEqual(by["stop_daemon"]["warn"], "Stops the node. If a service or script restarts it "
                                                    "automatically, it comes back on its own.")
        self.assertEqual(by["update"]["warn"], "Download only: monerod saves the release file, it doesn't install it.")
        seconds = [p for p in by["ban"]["params"] if p["name"] == "seconds"][0]
        self.assertTrue(seconds["help"].endswith("Bans last until they run out or the node restarts; bans from "
                                                 "--ban-list and the DNS blocklist come back when it starts."))

    def test_add_on_notes_go_after_the_warning(self):
        notes = {"stop_daemon": "Note one.", "exit": "Note two.", "save": "Saved too.", "nope": "Dropped."}
        cmds = C.Commands(make_settings("http://127.0.0.1:1"), notes=lambda: notes)
        by = {c["name"]: c for c in cmds.listing()["commands"]}
        self.assertEqual(by["stop_daemon"]["warn"], "Stops the node. If a service or script restarts it "
                                                    "automatically, it comes back on its own. Note one. Note two.")
        self.assertEqual(by["save"]["warn"], "Saved too.", "a command with no warning of its own")
        self.assertIsNone(by["print_height"]["warn"])
        self.assertNotIn("Dropped.", json.dumps(by))

    def test_parse_range(self):
        ok = {"77.88.100.7": "77.88.100.7", "77.88.100.x": "77.88.100.0/24", "77.88.x.x": "77.88.0.0/16",
              "77.88.100.*": "77.88.100.0/24", " 77.88.100.77/24 ": "77.88.100.0/24",
              "77.88.100.77/32": "77.88.100.77", "77.88.255.1/16": "77.88.0.0/16", "33.44.7.0/28": "33.44.7.0/28",
              "8.8.8.8": "8.8.8.8"}
        for text, host in ok.items():
            self.assertEqual(C.parse_range(text)[0], host, text)
        refused = {
            "": "type an address", "77.88.x.x/16": "isn't an IPv4", "77.88.0.0/15": "wider than", "77.x.x.x": "isn't",
            "77.88.100.0/33": "16 to 32", "77.88.100.0/ab": "16 to 32", "77.88.100.256": "isn't an IPv4",
            "2001:db8::1": "isn't an IPv4", "abc.onion": "isn't an IPv4", "1.2.3": "isn't an IPv4",
            "10.1.2.3": "isn't a public", "192.168.1.x": "isn't a public", "127.0.0.1": "isn't a public",
            "100.64.13.x": "isn't a public", "10.20.30.40": "isn't a public", "169.254.1.1": "isn't a public",
            "172.16.x.x": "isn't a public", "224.0.0.1": "isn't a public", "240.1.2.3": "isn't a public",
            "255.255.255.255": "isn't a public", "0.1.2.3": "isn't a public", "203.0.113.x": "isn't a public",
            "198.51.0.0/16": "isn't a public", "192.0.0.9": "isn't a public", "198.18.5.5": "isn't a public",
        }
        for text, why in refused.items():
            with self.assertRaises(C.ArgError, msg=text) as cm:
                C.parse_range(text)
            self.assertIn(why, str(cm.exception), text)
        self.assertEqual(C.range_label("77.88.0.0/16"), "77.88.x.x")
        self.assertEqual(C.range_label("33.44.7.0/28"), "33.44.7.0/28")


class ArgumentTest(unittest.TestCase):
    """Bad arguments are refused before anything is sent: the node address here is closed,
    so any request would come back as 502, never 400."""

    @classmethod
    def setUpClass(cls):
        cls.cmds = C.Commands(make_settings("http://127.0.0.1:%d" % helpers.closed_port()),
                              StubPoller(own=[("33.44.7.7", "node")]), log=lambda m: None)

    def refused(self, name, args, field=None, confirm=None, status=400, why=None):
        body = {"name": name, "args": args}
        if confirm:
            body["confirm"] = confirm
        st, out = self.cmds.run(body)
        self.assertEqual(st, status, (name, args, out))
        self.assertFalse(out["ok"])
        if field:
            self.assertEqual(out.get("field"), field, (name, args, out))
        if why:
            self.assertIn(why, out["error"], (name, args))
        return out

    def test_table(self):
        h64 = "ab" * 32
        cases = [
            # (command, args, field)
            ("print_block", {}, "block"),
            ("print_block", {"block": "12x"}, "block"),
            ("print_block", {"block": h64[:-1]}, "block"),
            ("print_block", {"block": "-5"}, "block"),
            ("print_tx", {"txid": h64 + "a"}, "txid"),
            ("print_tx", {"txid": "zz" * 32}, "txid"),
            ("print_tx", {"txid": h64, "hex": "maybe"}, "hex"),
            ("is_key_image_spent", {"key_image": "12"}, "key_image"),
            ("relay_tx", {"txid": 42}, "txid"),
            ("flush_txpool", {"txid": "not-a-hash"}, "txid"),
            ("alt_chain_info", {"blockhash": "xyz"}, "blockhash"),
            ("print_bc", {}, "begin"),
            ("print_bc", {"begin": -1001}, "begin"),
            ("print_bc", {"begin": -10, "end": 5}, "end"),
            ("print_bc", {"begin": 100, "end": 99}, "end"),
            ("print_bc", {"begin": 100, "end": 1100}, "end"),
            ("print_bc", {"begin": 1.5}, "begin"),
            ("print_bc", {"begin": True}, "begin"),
            ("bc_dyn_stats", {"last": 0}, "last"),
            ("bc_dyn_stats", {"last": 10001}, "last"),
            ("hard_fork_info", {"version": 256}, "version"),
            ("hard_fork_info", {"version": 0}, "version"),
            ("print_coinbase_tx_sum", {"start": 1}, "count"),
            ("print_coinbase_tx_sum", {"start": 1, "count": 100001}, "count"),
            ("output_histogram", {"amount": -1}, "amount"),
            ("pop_blocks", {"count": 0}, "count"),
            ("pop_blocks", {"count": 1001}, "count"),
            ("update", {"command": "install"}, "command"),
            ("update", {}, "command"),
            ("print_pl", {"list": "black"}, "list"),
            ("print_pl", {"limit": -1}, "limit"),
            ("limit", {"kbps": 0}, "kbps"),
            ("limit", {"kbps": -2}, "kbps"),
            ("limit_up", {"kbps": C.MAX_LIMIT_KBPS + 1}, "kbps"),
            ("out_peers", {"max": 1001}, "max"),
            ("in_peers", {"max": -1}, "max"),
            ("set_bootstrap_daemon", {}, "address"),
            ("set_bootstrap_daemon", {"address": "bad host:1"}, "address"),
            ("set_bootstrap_daemon", {"address": "node.example:99999"}, "address"),
            ("set_bootstrap_daemon", {"address": "auto", "username": "u", "password": "p"}, "username"),
            ("set_bootstrap_daemon", {"address": "node.example:18089", "username": "u"}, "password"),
            ("set_bootstrap_daemon", {"address": "none", "proxy": "127.0.0.1:9050"}, "proxy"),
            ("set_bootstrap_daemon", {"address": "auto", "proxy": "127.0.0.1"}, "proxy"),
            ("banned", {"address": "77.88.100.0/24"}, "address"),
            ("banned", {"address": "evil.com"}, "address"),
            ("unban", {"address": "77.88.100.x"}, "address"),
            ("unban", {"address": "77.88.100.1/24"}, "address"),
            ("ban", {}, "hosts"),
            ("ban", {"hosts": "# just a comment\n\n"}, "hosts"),
            ("ban", {"hosts": "10.0.0.0/8"}, "hosts"),
            ("ban", {"hosts": "77.88.100.x\n192.168.1.5"}, "hosts"),
            ("ban", {"hosts": ["77.88.100.x", 5]}, "hosts"),
            ("ban", {"hosts": "2001:db8::1"}, "hosts"),
            ("ban", {"hosts": "\n".join("45.%d.1.1" % i for i in range(257))}, "hosts"),
            ("ban", {"hosts": "77.88.100.x", "seconds": 0}, "seconds"),
            ("ban", {"hosts": "77.88.100.x", "seconds": 31_536_001}, "seconds"),
            ("ban", {"hosts": "33.44.x.x"}, "hosts"),          # holds the node's own address
            ("start_mining", {}, "address"),
            ("start_mining", {"address": "8" + "A" * 94}, "address"),        # subaddress
            ("start_mining", {"address": "4" + "A" * 105}, "address"),       # integrated
            ("start_mining", {"address": "4" + "A" * 93}, "address"),
            ("start_mining", {"address": "4" + "0" * 94}, "address"),        # 0 isn't base58
            ("start_mining", {"address": "x" + "A" * 94}, "address"),
            ("start_mining", {"address": MAIN, "threads": "0"}, "threads"),
            ("start_mining", {"address": MAIN, "threads": "257"}, "threads"),
            ("set_log", {}, "level"),
            ("set_log", {"level": 2, "categories": "*:INFO"}, "level"),
            ("set_log", {"level": 5}, "level"),
            ("set_log", {"categories": "*:INFO; rm -rf"}, "categories"),
            ("flush_cache", {"bad_blocks": False}, "bad_blocks"),
            ("status", {"verbose": True}, "verbose"),
            ("print_cn", {"x\n": 1}, None),
        ]
        for name, args, field in cases:
            self.refused(name, args, field)

    def test_requests_that_are_not_commands(self):
        self.refused("nope", {}, "name", status=404)
        self.refused("help", {}, "name", status=404)
        self.refused("", {}, "name")
        self.refused("status", [1, 2])
        st, out = self.cmds.run({"args": {}})
        self.assertEqual(st, 400)

    def test_ban_messages(self):
        out = self.refused("ban", {"hosts": "33.44.x.x"})
        self.assertEqual(out["error"], "33.44.x.x holds this computer's own public address (the node is bound to "
                                       "it with --p2p-bind-ip), so banning it would cut your node off.")
        out = self.refused("ban", {"hosts": "77.88.100.x\n33.44.7.7"})
        self.assertTrue(out["error"].startswith("33.44.7.7 holds"), out["error"])
        cmds = C.Commands(make_settings("http://127.0.0.1:1"),
                          StubPoller(own=[("8.8.4.4", "Example"), "1.1.1.1"]), log=lambda m: None)
        st, out = cmds.run({"name": "ban", "args": {"hosts": "8.8.4.x"}, "confirm": "BAN"})
        self.assertEqual((st, out["error"]), (400, "8.8.4.x holds this computer's own public address (from the "
                                                   "Example add-on), so banning it would cut your node off."))
        st, out = cmds.run({"name": "ban", "args": {"hosts": "1.1.1.1"}, "confirm": "BAN"})
        self.assertIn("(the dashboard was told so)", out["error"])
        out = self.refused("ban", {"hosts": "192.168.1.x"})
        self.assertIn("192.168.1.x isn't a public address", out["error"])
        # an address given as IPv4-mapped IPv6 is the IPv4 address
        cmds = C.Commands(make_settings("http://127.0.0.1:1"), StubPoller(own=[("::ffff:9.9.9.9", "Example")]),
                          log=lambda m: None)
        st, out = cmds.run({"name": "ban", "args": {"hosts": "9.9.9.0/24"}, "confirm": "BAN"})
        self.assertEqual(st, 400)
        self.assertTrue(out["error"].startswith("9.9.9.x holds this computer's own public address"), out)

    def test_ban_waits_for_the_first_poll(self):
        cmds = C.Commands(make_settings("http://127.0.0.1:%d" % helpers.closed_port()), StubPoller(own=None),
                          log=lambda m: None)
        st, out = cmds.run({"name": "ban", "args": {"hosts": "77.88.100.x"}, "confirm": "BAN"})
        self.assertEqual((st, out["error"]), (409, "The dashboard hasn't finished its first look at the node yet, "
                                                   "so it can't check which addresses are this computer's own. "
                                                   "Try again in a few seconds."))
        st, out = cmds.run({"name": "ban", "args": {"hosts": "10.1.1.1"}, "confirm": "BAN"})
        self.assertEqual((st, out["field"]), (400, "hosts"), "a wrong address is still said first")

    def test_confirm_word_checked_before_anything_is_sent(self):
        for name, args, word in (("ban", {"hosts": "77.88.100.x"}, "BAN"), ("unban", {"address": "1.2.3.4"}, "UNBAN"),
                                 ("stop_daemon", {}, "STOP"), ("limit", {"kbps": 100}, "LIMIT"),
                                 ("update", {"command": "download"}, "DOWNLOAD"), ("pop_blocks", {"count": 1}, "POP")):
            for bad in (None, "", "BANANA", "yes", 1, ["BAN"]):
                body = {"name": name, "args": args}
                if bad is not None:
                    body["confirm"] = bad
                st, out = self.cmds.run(body)
                self.assertEqual((st, out), (400, {"ok": False, "error": "Type %s to confirm." % word,
                                                   "field": "confirm"}), (name, bad))
            # the right word gets as far as the node (closed here: 502)
            st, out = self.cmds.run({"name": name, "args": args, "confirm": "  %s " % word.lower()})
            self.assertEqual(st, 502, (name, out))
            self.assertIn("connection refused", out["error"])


class FakeNodeTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMonerod().start()
        self.settings = make_settings(self.fake.url)
        self.poller = StubPoller(own=[("100.64.1.2", "node")])
        self.logs = []
        self.cmds = C.Commands(self.settings, self.poller, log=self.logs.append)

    def tearDown(self):
        self.fake.stop()

    def run_cmd(self, name, args=None, confirm=None, status=200):
        body = {"name": name, "args": args or {}}
        if confirm:
            body["confirm"] = confirm
        n = len(self.fake.requests)
        st, out = self.cmds.run(body)
        self.assertEqual(st, status, (name, out))
        sent = self.fake.requests[n:]
        cmd = C.REGISTRY[C.ALIASES.get(name, name)]
        self.assertTrue({m for m, _ in sent} <= set(cmd.calls), (name, sent))
        return out, sent

    def test_every_command_runs(self):
        cases = [
            ("status", {}, None), ("print_status", {}, None), ("version", {}, None), ("print_height", {}, None),
            ("diff", {}, None), ("sync_info", {}, None), ("save", {}, "SAVE"), ("stop_daemon", {}, "STOP"),
            ("exit", {}, "STOP"), ("update", {"command": "check"}, None),
            ("update", {"command": "download"}, "DOWNLOAD"),
            ("print_bc", {"begin": -3}, None), ("print_bc", {"begin": 3780030, "end": 3780035}, None),
            ("print_block", {"block": "3780030"}, None), ("bc_dyn_stats", {"last": 50}, None),
            ("alt_chain_info", {}, None), ("alt_chain_info", {"blockhash": "aa" * 32}, None),
            ("hard_fork_info", {}, None), ("print_coinbase_tx_sum", {"start": 3780000, "count": 10}, None),
            ("output_histogram", {}, None), ("check_blockchain_pruning", {}, None),
            ("pop_blocks", {"count": 2}, "POP"), ("prune_blockchain", {}, "PRUNE"),
            ("print_tx", {"txid": CHAIN_TX}, None), ("is_key_image_spent", {"key_image": "22" * 32}, None),
            ("print_pool", {}, None), ("print_pool_sh", {}, None), ("print_pool_stats", {}, None),
            ("relay_tx", {"txid": POOL_TX}, "RELAY"), ("flush_txpool", {}, "FLUSH"),
            ("print_cn", {}, None), ("print_pl", {}, None), ("print_pl_stats", {}, None),
            ("print_net_stats", {}, None), ("limit", {}, None), ("limit", {"kbps": 4096}, "LIMIT"),
            ("limit_up", {"kbps": -1}, "LIMIT"), ("limit_down", {}, None), ("out_peers", {}, None),
            ("out_peers", {"max": 32}, "PEERS"), ("in_peers", {"max": 10}, "PEERS"),
            ("set_bootstrap_daemon", {"address": "auto"}, "BOOTSTRAP"), ("bans", {}, None),
            ("banned", {"address": "100.117.111.91"}, None), ("ban", {"hosts": "77.88.100.x"}, "BAN"),
            ("unban", {"address": "77.88.100.0/24"}, "UNBAN"), ("flush_cache", {}, "FLUSH"),
            ("mining_status", {}, None), ("start_mining", {"address": MAIN}, "MINE"), ("show_hr", {}, "HR"),
            ("hide_hr", {}, "HR"), ("stop_mining", {}, "STOP"), ("set_log", {"level": 1}, "LOG"),
            ("rpc_payments", {}, None),
        ]
        ran = set()
        for name, args, word in cases:
            out, sent = self.run_cmd(name, args, word)
            ran.add(C.ALIASES.get(name, name))
            self.assertTrue(out["ok"])
            self.assertEqual(out["name"], C.ALIASES.get(name, name))
            self.assertEqual(out["calls"], [m for m, _ in sent], name)
            self.assertTrue(out["message"], name)
            json.dumps(out)
        self.assertEqual(ran, set(C.REGISTRY), "every command ran through the fake node")

    # ---- one group at a time, with the exact request fields the console sends
    def test_node(self):
        out, sent = self.run_cmd("status")
        self.assertEqual(sent, [("get_info", {}), ("hard_fork_info", {"version": 0}), ("mining_status", {})])
        self.assertTrue(out["message"].startswith("Height: 3780041/3780041 (100.0%) on mainnet, not mining"))
        self.assertEqual(set(out["result"]), {"get_info", "hard_fork_info", "mining_status"})
        out, sent = self.run_cmd("update", {"command": "check"})
        self.assertEqual(sent, [("update", {"command": "check", "path": ""})])
        self.assertEqual(self.poller.events, [], "a check changes nothing: no event")
        out, sent = self.run_cmd("exit", confirm="stop")
        self.assertEqual(sent, [("stop_daemon", {})])
        self.assertTrue(self.fake.stopped)
        self.assertEqual(self.poller.events, [("control", "info", "Stop signal sent")])
        self.assertEqual(self.poller.rereads, [("get_bans", "get_limit")])
        self.assertIn("command stop_daemon: Stop signal sent", self.logs)

    def test_status_while_bootstrapping(self):
        # like the console (rpc_command_executor.cpp show_status): get_info answered by the bootstrap node
        self.fake.info.update({"untrusted": True, "height_without_bootstrap": 3775041, "was_bootstrap_ever_used": True,
                               "bootstrap_daemon_address": "node.example.org:18089"})
        out, _ = self.run_cmd("status")
        self.assertTrue(out["message"].startswith("Height: 3780041/3780041 (100.0%) on mainnet, bootstrapping from "
                                                  "node.example.org:18089, local height: 3775041 (99.9%), not mining"),
                        out["message"])
        self.fake.info["untrusted"] = False
        out, _ = self.run_cmd("status")
        self.assertIn("on mainnet, bootstrapping from node.example.org:18089 was used before, not mining",
                      out["message"])

    def test_chain(self):
        top = self.fake.top
        out, sent = self.run_cmd("print_bc", {"begin": "-3"})
        self.assertEqual(sent, [("get_info", {}), ("get_block_headers_range",
                                                   {"start_height": top - 2, "end_height": top, "fill_pow_hash": False})])
        self.assertEqual(len(out["result"]["get_block_headers_range"]["headers"]), 3)
        out, sent = self.run_cmd("print_block", {"block": str(top)})
        self.assertEqual(sent, [("get_block", {"height": top, "fill_pow_hash": True})])
        self.assertNotIn("blob", out["result"]["get_block"], "+hex not asked")
        h = self.fake.chain[top]["hash"]
        out, sent = self.run_cmd("print_block", {"block": h.upper(), "hex": True})
        self.assertEqual(sent, [("get_block", {"hash": h, "fill_pow_hash": True})])
        self.assertIn("blob", out["result"]["get_block"])
        out, sent = self.run_cmd("bc_dyn_stats", {"last": 10})
        self.assertEqual([m for m, _ in sent], ["get_info", "get_fee_estimate", "hard_fork_info",
                                                 "get_block_headers_range"])
        self.assertEqual(sent[1][1], {"grace_blocks": 0})
        self.assertEqual(sent[2][1], {"version": 8})
        self.assertEqual(sent[3][1], {"start_height": top + 1 - 10, "end_height": top, "fill_pow_hash": False})
        out, sent = self.run_cmd("output_histogram", {"amount": "0", "min_count": 1})
        self.assertEqual(sent, [("get_output_histogram", {"amounts": [0], "min_count": 1, "max_count": 0,
                                                          "unlocked": False, "recent_cutoff": 0})])
        out, sent = self.run_cmd("check_blockchain_pruning")
        self.assertEqual(sent, [("prune_blockchain", {"check": True})])
        self.assertEqual(out["message"], "Blockchain is not pruned")
        out, sent = self.run_cmd("pop_blocks", {"count": 2}, "POP")
        self.assertEqual(sent, [("pop_blocks", {"nblocks": 2})])
        self.assertEqual(out["message"], "new height: %d" % (top - 1))
        out, sent = self.run_cmd("prune_blockchain", confirm="PRUNE")
        self.assertEqual(sent, [("prune_blockchain", {"check": False})])
        self.assertEqual(self.run_cmd("check_blockchain_pruning")[0]["message"], "Blockchain is pruned")
        # a block that isn't there: monerod's own message, 502
        out, _ = self.run_cmd("print_block", {"block": "99999999"}, status=502)
        self.assertIn("greater than current top block height", out["error"])

    def test_transactions(self):
        out, sent = self.run_cmd("print_tx", {"txid": CHAIN_TX.upper(), "json": True})
        self.assertEqual(sent, [("get_transactions", {"txs_hashes": [CHAIN_TX], "decode_as_json": True,
                                                      "split": True, "prune": False})])
        tx = out["result"]["get_transactions"]["txs"][0]
        self.assertNotIn("pruned_as_hex", tx, "+hex not asked")
        self.assertIn("as_json", tx)
        self.assertEqual(out["message"], "Found in blockchain at height 3780000")
        out, sent = self.run_cmd("print_tx", {"txid": POOL_TX, "hex": "true"})
        self.assertFalse(sent[0][1]["decode_as_json"], "as the console, unless +json (see DESIGN)")
        self.assertIn("pruned_as_hex", out["result"]["get_transactions"]["txs"][0])
        self.assertEqual(out["message"], "Found in pool")
        out, sent = self.run_cmd("is_key_image_spent", {"key_image": "00" * 32})
        self.assertEqual(sent, [("is_key_image_spent", {"key_images": ["00" * 32]})])
        self.assertEqual(out["message"], "%s: unspent" % ("00" * 32))
        out, _ = self.run_cmd("print_pool_sh")
        self.assertNotIn("tx_json", out["result"]["get_transaction_pool"]["transactions"][0])
        out, _ = self.run_cmd("print_pool")
        self.assertIn("tx_json", out["result"]["get_transaction_pool"]["transactions"][0])
        out, sent = self.run_cmd("relay_tx", {"txid": POOL_TX}, "RELAY")
        self.assertEqual(sent, [("relay_tx", {"txids": [POOL_TX]})])
        out, sent = self.run_cmd("relay_tx", {"txid": "12" * 32}, "RELAY", status=502)
        self.assertIn("Transaction not found in pool", out["error"])
        out, sent = self.run_cmd("flush_txpool", confirm="FLUSH")
        self.assertEqual(sent, [("flush_txpool", {"txids": []})])
        self.assertEqual(self.fake.pool, [])

    def test_network(self):
        out, sent = self.run_cmd("print_pl", {"list": "white", "pruned": True, "limit": 2})
        self.assertEqual(sent, [("get_peer_list", {"public_only": False, "include_blocked": True})])
        r = out["result"]["get_peer_list"]
        self.assertNotIn("gray_list", r)
        # as the console: the first 2 of the list, then only the pruned ones among them
        self.assertEqual([p["host"] for p in r["white_list"]], ["33.44.1.1"])
        self.assertEqual(out["message"], "1 peer listed")
        out, _ = self.run_cmd("print_pl", {"publicrpc": True})
        self.assertEqual([p["host"] for p in out["result"]["get_peer_list"]["white_list"]], ["33.44.3.3", "33.44.6.6"])
        out, _ = self.run_cmd("print_pl", {"publicrpc": True, "limit": 3})
        r = out["result"]["get_peer_list"]
        self.assertEqual(([p["host"] for p in r["white_list"]], r["gray_list"]), (["33.44.3.3"], []))
        out, sent = self.run_cmd("limit_up", {"kbps": "1000"}, "LIMIT")
        self.assertEqual(sent, [("set_limit", {"limit_down": 0, "limit_up": 1000})])
        self.assertEqual(out["message"], "Set limit-up to 1000 kB/s")
        out, sent = self.run_cmd("limit")
        self.assertEqual(sent, [("get_limit", {})])
        self.assertEqual(out["message"], "limit-down is 8192 kB/s, limit-up is 1000 kB/s")
        out, sent = self.run_cmd("out_peers")
        self.assertEqual(sent, [("out_peers", {"set": False, "out_peers": 0})])
        self.assertEqual(out["message"], "Max number of out peers is 32")
        out, sent = self.run_cmd("in_peers", {"max": 0}, "PEERS")
        self.assertEqual(sent, [("in_peers", {"set": True, "in_peers": 0})])
        out, sent = self.run_cmd("set_bootstrap_daemon", {"address": "node.example.org:18089", "username": "me",
                                                          "password": "hunter2-pw", "proxy": "127.0.0.1:9050"},
                                 "BOOTSTRAP")
        self.assertEqual(sent, [("set_bootstrap_daemon", {"address": "node.example.org:18089", "username": "me",
                                                          "password": "hunter2-pw", "proxy": "127.0.0.1:9050"})])
        self.assertNotIn("hunter2-pw", json.dumps(out))
        self.assertNotIn("hunter2-pw", json.dumps(self.poller.events) + json.dumps(self.logs))
        out, sent = self.run_cmd("set_bootstrap_daemon", {"address": "none"}, "BOOTSTRAP")
        self.assertEqual(sent[0][1], {"address": "", "username": "", "password": "", "proxy": ""})
        self.assertEqual(out["message"], "Successfully set bootstrap daemon address to none")

    def test_bans(self):
        out, sent = self.run_cmd("ban", {"hosts": "77.88.100.x\n33.44.7.0/24 # from the scope\n77.88.100.9/24"},
                                 "BAN")
        self.assertEqual(sent, [("set_bans", {"bans": [
            {"host": "77.88.100.0/24", "ip": 0, "ban": True, "seconds": 86400},
            {"host": "33.44.7.0/24", "ip": 0, "ban": True, "seconds": 86400}]})])
        self.assertEqual(out["message"], "Banned 2 addresses or ranges for 24 hours. Connections from them were dropped.")
        self.assertEqual(out["args"]["hosts"], ["77.88.100.0/24", "33.44.7.0/24"])
        out, sent = self.run_cmd("banned", {"address": "100.117.111.91"})
        self.assertEqual(sent, [("banned", {"address": "100.117.111.91"})])
        self.assertEqual(out["message"], "100.117.111.91 is banned for 5248 seconds")
        out, sent = self.run_cmd("unban", {"address": "77.88.100.0/24"}, "UNBAN")
        self.assertEqual(sent, [("set_bans", {"bans": [{"host": "77.88.100.0/24", "ip": 0, "ban": False,
                                                        "seconds": 0}]})])
        hosts = [b["host"] for b in self.run_cmd("bans")[0]["result"]["get_bans"]["bans"]]
        self.assertNotIn("77.88.100.0/24", hosts)
        self.assertIn("33.44.7.0/24", hosts)
        out, sent = self.run_cmd("flush_cache", confirm="FLUSH")
        self.assertEqual(sent, [("flush_cache", {"bad_blocks": True})])

    def test_ban_says_when_the_nodes_own_address_wasnt_checked(self):
        by = {c["name"]: c for c in self.cmds.listing()["commands"]}
        self.assertNotIn("couldn't be checked", by["ban"]["warn"] or "")
        self.poller.own_known = False
        by = {c["name"]: c for c in self.cmds.listing()["commands"]}
        self.assertEqual(by["ban"]["warn"], C.OWN_UNCHECKED)
        out, _ = self.run_cmd("ban", {"hosts": "77.88.100.x"}, "BAN")
        self.assertEqual(out["message"], "Banned 77.88.100.x for 24 hours. Connections from it were dropped. "
                                         + C.OWN_UNCHECKED)

    def test_mining(self):
        out, _ = self.run_cmd("show_hr", confirm="HR", status=502)
        self.assertIn('"NOT MINING"', out["error"], "monerod's own answer")
        out, sent = self.run_cmd("start_mining", {"address": MAIN, "threads": "auto", "ignore_battery": True}, "MINE")
        self.assertEqual(sent, [("start_mining", {"miner_address": MAIN, "threads_count": 0,
                                                  "do_background_mining": False, "ignore_battery": True})])
        out, sent = self.run_cmd("show_hr", confirm="HR")
        self.assertEqual(sent, [("set_log_hash_rate", {"visible": True})])
        self.assertEqual(self.run_cmd("mining_status")[0]["message"], "Mining at 1.23 kH/s with 0 threads")
        self.run_cmd("stop_mining", confirm="STOP")
        self.assertIsNone(self.fake.mining)

    def test_logs(self):
        out, sent = self.run_cmd("set_log", {"level": "2"}, "LOG")
        self.assertEqual(sent, [("set_log_level", {"level": 2})])
        out, sent = self.run_cmd("set_log", {"categories": "-net.p2p"}, "LOG")
        self.assertEqual(sent, [("set_log_categories", {"categories": "-net.p2p"})])
        self.assertEqual(out["message"], "Log categories are now -net.p2p")
        out, sent = self.run_cmd("rpc_payments")
        self.assertEqual(sent, [("rpc_access_data", {})])

    # ---- the rules around running
    def test_controls_enabled_only_stops_changes(self):
        self.settings.update({"controls.enabled": False})
        n = len(self.fake.requests)
        for name, args, word in (("ban", {"hosts": "77.88.100.x"}, "BAN"), ("limit", {"kbps": 10}, "LIMIT"),
                                 ("update", {"command": "download"}, "DOWNLOAD")):
            st, out = self.cmds.run({"name": name, "args": args, "confirm": word})
            self.assertEqual(st, 403, name)
            self.assertIn("turned off in settings", out["error"])
        self.assertEqual(len(self.fake.requests), n, "nothing was sent")
        self.run_cmd("bans")
        self.run_cmd("limit")                          # without a value it only reads
        self.run_cmd("update", {"command": "check"})
        self.run_cmd("out_peers")

    def test_node_errors(self):
        out, _ = self.run_cmd("unban", {"address": "[2001:db8::1]"}, "UNBAN", status=502)
        self.assertIn("Unsupported host/subnet type", out["error"], "monerod's own message")
        self.assertIn("command unban failed", self.logs[-1])
        self.assertEqual(self.poller.events, [], "a failed change adds no event")
        self.fake.stop()
        out, _ = self.run_cmd("bans", status=502)
        self.assertIn("connection refused", out["error"])
        out, _ = self.run_cmd("print_status")
        self.assertEqual(out["message"], "monerod is NOT running (connection refused)")

    def test_restricted_port(self):
        fake = FakeMonerod(restricted=True).start()
        try:
            cmds = C.Commands(make_settings(fake.url), StubPoller(), log=lambda m: None)
            st, out = cmds.run({"name": "bans", "args": {}})
            self.assertEqual(st, 502)
            self.assertIn("restricted RPC port", out["error"])
            st, out = cmds.run({"name": "save", "args": {}, "confirm": "SAVE"})
            self.assertEqual(st, 502)
            st, out = cmds.run({"name": "status", "args": {}})
            self.assertEqual(st, 200, "mining info is optional, like the console")
            self.assertIn("mining info unavailable", out["message"])
        finally:
            fake.stop()

    def test_timeouts(self):
        self.settings.update({"source.timeout_s": 9})
        for name in ("prune_blockchain", "pop_blocks", "print_coinbase_tx_sum", "output_histogram", "flush_txpool",
                     "save"):
            self.assertEqual(self.cmds._client(C.REGISTRY[name]).timeout, 120, name)
        for name in ("status", "ban", "print_bc", "bans"):
            self.assertEqual(self.cmds._client(C.REGISTRY[name]).timeout, 9, name)

    def test_one_change_at_a_time(self):
        self.cmds._change_lock.acquire()
        try:
            st, out = self.cmds.run({"name": "save", "args": {}, "confirm": "SAVE"})
            self.assertEqual(st, 409)
            self.run_cmd("bans")                 # reads still run
        finally:
            self.cmds._change_lock.release()
        self.run_cmd("save", confirm="SAVE")

    def test_a_command_only_sends_its_own_calls(self):
        cmd = C.REGISTRY["bans"]
        ctx = C._Ctx(self.cmds._client(cmd), cmd)
        with self.assertRaises(C.RpcNotAllowed):
            ctx.call("set_bans", {"bans": []})
        self.assertEqual(self.fake.requests, [])


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeMonerod().start()
        cls.settings = make_settings(cls.fake.url)
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.app = App(cls.settings, port=0, web_dir=base / "web", state_dir=base / "state", log=lambda m: None,
                      host_reader=StubHost(), discover=StubDiscover([found_node(cls.fake.url)])).start(poll=False)
        cls.port = cls.app.port
        cls.app.poller.poll_once()   # `ban` waits for the first poll's list of the node's own addresses

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.fake.stop()
        cls.tmp.cleanup()

    def test_list(self):
        st, out = get_json(self.port, "/api/commands")
        self.assertEqual(st, 200)
        self.assertEqual(len(out["commands"]), len(C.REGISTRY))
        self.assertNotIn("_check", json.dumps(out))

    def test_guard(self):
        body = {"name": "ban", "args": {"hosts": "77.88.100.x"}, "confirm": "BAN"}
        st, _, _ = request(self.port, "POST", "/api/command", body, headers={"Content-Type": "application/json"})
        self.assertEqual(st, 403)
        for origin in ("http://evil.example", "null", "http://127.0.0.1:1"):
            st, _, _ = request(self.port, "POST", "/api/command", body, headers=dict(WRITE, Origin=origin))
            self.assertEqual(st, 403, origin)
        st, _, _ = request(self.port, "POST", "/api/command", body,
                           headers=dict(WRITE, **{"Sec-Fetch-Site": "cross-site"}))
        self.assertEqual(st, 403)
        st, _, _ = request(self.port, "POST", "/api/command", body, host="evil.example:%d" % self.port,
                           headers=WRITE)
        self.assertEqual(st, 421)
        self.assertNotIn("set_bans", self.fake.calls, "nothing reached the node")
        st, _, raw = request(self.port, "POST", "/api/command", body,
                             headers=dict(WRITE, Origin="http://127.0.0.1:%d" % self.port))
        self.assertEqual(st, 200, raw)
        st, _, _ = request(self.port, "GET", "/api/command")
        self.assertEqual(st, 405)

    def test_answers(self):
        st, _, raw = request(self.port, "POST", "/api/command", {"name": "print_height"}, headers=WRITE)
        out = json.loads(raw)
        self.assertEqual((st, out["ok"], out["name"], out["calls"]), (200, True, "print_height", ["get_height"]))
        self.assertEqual(out["message"], str(self.fake.top + 1))
        self.assertEqual(out["result"]["get_height"]["height"], self.fake.top + 1)
        st, _, raw = request(self.port, "POST", "/api/command", {"name": "pop_blocks", "args": {"count": 1}},
                             headers=WRITE)
        self.assertEqual((st, json.loads(raw)), (400, {"ok": False, "error": "Type POP to confirm.",
                                                       "field": "confirm"}))
        st, _, raw = request(self.port, "POST", "/api/command", {"name": "ban", "args": {"hosts": "10.1.1.1"},
                                                                 "confirm": "BAN"}, headers=WRITE)
        self.assertEqual(st, 400)
        self.assertEqual(json.loads(raw)["field"], "hosts")
        st, _, raw = request(self.port, "POST", "/api/command", raw_body=b"[]", headers=WRITE)
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
