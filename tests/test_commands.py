"""monerod's read-only console commands: the registry, argument checks, and the exact
requests each one sends (checked against the test-only fake monerod)."""

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
from memstara.rpc import READ_ALLOWED
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
# the console commands that change something, or that send a method that can (update check,
# check_blockchain_pruning, out_peers/in_peers without a number): none of them is here
LEFT_OUT = {
    "start_mining", "stop_mining", "show_hr", "hide_hr", "save", "set_log", "stop_daemon", "exit", "limit_up",
    "limit_down", "out_peers", "in_peers", "ban", "unban", "flush_txpool", "update", "relay_tx", "pop_blocks",
    "prune_blockchain", "check_blockchain_pruning", "set_bootstrap_daemon", "flush_cache"}


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


class RegistryTest(unittest.TestCase):
    def test_every_read_only_console_command(self):
        self.assertEqual(set(C.REGISTRY) | set(C.ALIASES), CONSOLE - LEFT_OUT)
        self.assertEqual(C.ALIASES, {"print_pool_sh": "print_pool"})
        self.assertEqual(len(C.REGISTRY), 26)

    def test_registry_matches_the_allowlist(self):
        self.assertEqual(C.check_registry(), [])
        self.assertEqual(C.registry_calls() | set(POLLER_CALLS), set(READ_ALLOWED),
                         "the allowlist is exactly the commands' calls plus the poller's")
        self.assertEqual(set(POLLER_CALLS) - C.registry_calls(),
                         {"get_last_block_header", "get_version", "get_transaction_pool_hashes",
                          "get_output_distribution"})
        for cmd in C.REGISTRY.values():
            self.assertTrue(cmd.calls, cmd.name)

    def test_listing_shape(self):
        out = C.Commands(make_settings("http://127.0.0.1:1")).listing()
        json.dumps(out)
        cmds = out["commands"]
        self.assertEqual(len(cmds), len(C.REGISTRY))
        keys = {"name", "aliases", "group", "title", "summary", "usage", "calls", "params"}
        pkeys = {"name", "label", "type", "required", "min", "max", "choices", "placeholder", "help", "default"}
        groups = []
        for c in cmds:
            self.assertEqual(set(c), keys, c["name"])
            if c["group"] not in groups:
                groups.append(c["group"])
            for p in c["params"]:
                self.assertEqual(set(p), pkeys, (c["name"], p["name"]))
                self.assertIn(p["type"], ("int", "text", "choice", "bool"))
        self.assertEqual(groups, C.GROUPS)
        by = {c["name"]: c for c in cmds}
        self.assertEqual(by["print_pool"]["aliases"], ["print_pool_sh"])
        self.assertEqual(by["limit"]["calls"], ["get_limit"])
        self.assertEqual(by["limit"]["params"], [])


class ArgumentTest(unittest.TestCase):
    """Bad arguments are refused before anything is sent: the node address here is closed,
    so any request would come back as 502, never 400."""

    @classmethod
    def setUpClass(cls):
        cls.cmds = C.Commands(make_settings("http://127.0.0.1:%d" % helpers.closed_port()), log=lambda m: None)

    def refused(self, name, args, field=None, status=400, why=None):
        st, out = self.cmds.run({"name": name, "args": args})
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
            ("print_tx", {"txid": 42}, "txid"),
            ("is_key_image_spent", {"key_image": "12"}, "key_image"),
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
            ("print_pl", {"list": "black"}, "list"),
            ("print_pl", {"limit": -1}, "limit"),
            ("limit", {"kbps": 100}, "kbps"),           # it only reads now: no number to give
            ("banned", {"address": "77.88.100.0/24"}, "address"),
            ("banned", {"address": "evil.com"}, "address"),
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

    def test_change_commands_are_not_there(self):
        for name in sorted(LEFT_OUT):
            out = self.refused(name, {}, "name", status=404)
            self.assertEqual(out["error"], "There's no read-only monerod command called %s." % name)


class FakeNodeTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMonerod().start()
        self.settings = make_settings(self.fake.url)
        self.logs = []
        self.cmds = C.Commands(self.settings, log=self.logs.append)

    def tearDown(self):
        self.fake.stop()

    def run_cmd(self, name, args=None, status=200):
        n = len(self.fake.requests)
        st, out = self.cmds.run({"name": name, "args": args or {}})
        self.assertEqual(st, status, (name, out))
        sent = self.fake.requests[n:]
        cmd = C.REGISTRY[C.ALIASES.get(name, name)]
        self.assertTrue({m for m, _ in sent} <= set(cmd.calls), (name, sent))
        return out, sent

    def test_every_command_runs(self):
        cases = [
            ("status", {}), ("print_status", {}), ("version", {}), ("print_height", {}), ("diff", {}),
            ("sync_info", {}), ("print_bc", {"begin": -3}), ("print_bc", {"begin": 3780030, "end": 3780035}),
            ("print_block", {"block": "3780030"}), ("bc_dyn_stats", {"last": 50}), ("alt_chain_info", {}),
            ("alt_chain_info", {"blockhash": "aa" * 32}), ("hard_fork_info", {}),
            ("print_coinbase_tx_sum", {"start": 3780000, "count": 10}), ("output_histogram", {}),
            ("print_tx", {"txid": CHAIN_TX}), ("is_key_image_spent", {"key_image": "22" * 32}),
            ("print_pool", {}), ("print_pool_sh", {}), ("print_pool_stats", {}), ("print_cn", {}),
            ("print_pl", {}), ("print_pl_stats", {}), ("print_net_stats", {}), ("limit", {}), ("bans", {}),
            ("banned", {"address": "100.117.111.91"}), ("mining_status", {}), ("rpc_payments", {}),
        ]
        ran = set()
        for name, args in cases:
            out, sent = self.run_cmd(name, args)
            ran.add(C.ALIASES.get(name, name))
            self.assertTrue(out["ok"])
            self.assertEqual(out["name"], C.ALIASES.get(name, name))
            self.assertEqual(out["calls"], [m for m, _ in sent], name)
            self.assertTrue(out["message"], name)
            json.dumps(out)
        self.assertEqual(ran, set(C.REGISTRY), "every command ran through the fake node")
        self.assertLessEqual({m for m, _ in self.fake.requests}, set(READ_ALLOWED))

    # ---- one group at a time, with the exact request fields the console sends
    def test_node(self):
        out, sent = self.run_cmd("status")
        self.assertEqual(sent, [("get_info", {}), ("hard_fork_info", {"version": 0}), ("mining_status", {})])
        self.assertTrue(out["message"].startswith("Height: 3780041/3780041 (100.0%) on mainnet, not mining"))
        self.assertEqual(set(out["result"]), {"get_info", "hard_fork_info", "mining_status"})
        out, sent = self.run_cmd("rpc_payments")
        self.assertEqual(sent, [("rpc_access_data", {})])

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
        out, sent = self.run_cmd("limit")
        self.assertEqual(sent, [("get_limit", {})])
        self.assertEqual(out["message"], "limit-down is 8192 kB/s, limit-up is 2048 kB/s")

    def test_bans(self):
        out, sent = self.run_cmd("banned", {"address": "100.117.111.91"})
        self.assertEqual(sent, [("banned", {"address": "100.117.111.91"})])
        self.assertEqual(out["message"], "100.117.111.91 is banned for 5248 seconds")
        out, sent = self.run_cmd("bans")
        self.assertEqual(sent, [("get_bans", {})])

    def test_mining(self):
        self.assertEqual(self.run_cmd("mining_status")[0]["message"], "Not currently mining")

    # ---- the rules around running
    def test_node_errors(self):
        out, _ = self.run_cmd("print_block", {"block": "99999999"}, status=502)
        self.assertIn("greater than current top block height", out["error"])
        self.fake.stop()
        out, _ = self.run_cmd("bans", status=502)
        self.assertIn("connection refused", out["error"])
        out, _ = self.run_cmd("print_status")
        self.assertEqual(out["message"], "monerod is NOT running (connection refused)")

    def test_restricted_port(self):
        fake = FakeMonerod(restricted=True).start()
        try:
            cmds = C.Commands(make_settings(fake.url), log=lambda m: None)
            st, out = cmds.run({"name": "bans", "args": {}})
            self.assertEqual(st, 502)
            self.assertIn("restricted RPC port", out["error"])
            st, out = cmds.run({"name": "status", "args": {}})
            self.assertEqual(st, 200, "mining info is optional, like the console")
            self.assertIn("mining info unavailable", out["message"])
        finally:
            fake.stop()

    def test_timeouts(self):
        self.settings.update({"source.timeout_s": 9})
        for name in ("print_coinbase_tx_sum", "output_histogram"):
            self.assertEqual(self.cmds._client(C.REGISTRY[name]).timeout, 120, name)
        for name in ("status", "print_bc", "bans"):
            self.assertEqual(self.cmds._client(C.REGISTRY[name]).timeout, 9, name)

    def test_a_command_only_sends_its_own_calls(self):
        cmd = C.REGISTRY["bans"]
        ctx = C._Ctx(self.cmds._client(cmd), cmd)
        with self.assertRaises(C.RpcNotAllowed):
            ctx.call("get_info")
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
        body = {"name": "print_height"}
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
        self.assertNotIn("get_height", self.fake.calls, "nothing reached the node")
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
        self.assertEqual((st, json.loads(raw)["field"]), (404, "name"))
        st, _, raw = request(self.port, "POST", "/api/command", raw_body=b"[]", headers=WRITE)
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
