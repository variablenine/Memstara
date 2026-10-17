"""The RPC client: the allowlists and the write path, Digest login, plain-language errors."""

from __future__ import annotations

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers  # noqa: F401  (sets up sys.path)
from fake_monerod import FakeMonerod
from memstara import rpc
from memstara.rpc import READ_ALLOWED, WRITE_ALLOWED, RpcClient, RpcError, RpcNotAllowed

# core_rpc_server.h, v0.18.5.3: JSON-RPC methods (on /json_rpc) and plain paths
MONEROD_JSON_RPC = {
    "get_block_count", "on_get_block_hash", "get_block_template", "get_miner_data", "calc_pow", "add_aux_pow",
    "submit_block", "generateblocks", "get_last_block_header", "get_block_header_by_hash",
    "get_block_header_by_height", "get_block_headers_range", "get_block", "get_connections", "get_info",
    "hard_fork_info", "set_bans", "get_bans", "banned", "flush_txpool", "get_output_histogram", "get_version",
    "get_coinbase_tx_sum", "get_fee_estimate", "get_alternate_chains", "relay_tx", "sync_info",
    "get_txpool_backlog", "get_output_distribution", "prune_blockchain", "flush_cache", "rpc_access_info",
    "rpc_access_submit_nonce", "rpc_access_pay", "rpc_access_tracking", "rpc_access_data", "rpc_access_account"}
MONEROD_PATHS = {
    "get_height", "get_transactions", "get_alt_blocks_hashes", "is_key_image_spent", "send_raw_transaction",
    "start_mining", "stop_mining", "mining_status", "save_bc", "get_peer_list", "get_public_nodes",
    "set_log_hash_rate", "set_log_level", "set_log_categories", "get_transaction_pool",
    "get_transaction_pool_hashes", "get_transaction_pool_stats", "set_bootstrap_daemon", "stop_daemon",
    "get_info", "get_net_stats", "get_limit", "set_limit", "out_peers", "in_peers", "get_outs", "update",
    "pop_blocks"}

# methods monerod has but no console command (and not the poller) sends: never allowed
NEVER = ["submit_block", "submitblock", "generateblocks", "calc_pow", "add_aux_pow", "get_block_template",
         "send_raw_transaction", "sendrawtransaction", "get_outs", "rpc_access_pay",
         "rpc_access_submit_nonce", "rpc_access_tracking", "rpc_access_account", "get_public_nodes",
         "get_alt_blocks_hashes", "get_txpool_backlog", "get_miner_data", "getinfo", "getheight",
         "start_save_graph", "", None, 42]
# methods that change something: only call_write() sends them
WRITE_METHODS = ["set_bans", "stop_daemon", "out_peers", "in_peers", "set_limit", "relay_tx", "flush_txpool",
                 "set_log_level", "set_log_categories", "set_log_hash_rate", "start_mining", "stop_mining",
                 "pop_blocks", "update", "set_bootstrap_daemon", "prune_blockchain", "flush_cache", "save_bc"]


class _Odd(BaseHTTPRequestHandler):
    """Answers with HTML, or with status BUSY, depending on the path."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path == "/get_limit":
            body = b'{"status": "BUSY", "untrusted": false}'
            ctype = "application/json"
        else:
            body = b"<html><body>router login</body></html>"
            ctype = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AllowlistTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMonerod().start()

    def tearDown(self):
        self.fake.stop()

    def test_write_methods_refused_without_any_request(self):
        client = RpcClient(self.fake.url, timeout=2)
        for m in WRITE_METHODS + NEVER:
            with self.assertRaises(RpcNotAllowed, msg=repr(m)):
                client.call(m)
        for m in NEVER:
            with self.assertRaises(RpcNotAllowed, msg=repr(m)):
                client.call_write(m)
        self.assertEqual(self.fake.calls, [], "a refused method must never reach the node")

    def test_refused_before_connecting(self):
        # nothing listens here: a refused method must not even try to connect
        client = RpcClient("http://127.0.0.1:%d" % helpers.closed_port(), timeout=2)
        with self.assertRaises(RpcNotAllowed) as cm:
            client.call("set_bans", {"bans": [{"host": "100.64.0.1", "ban": True}]})
        self.assertEqual(cm.exception.kind, "not_allowed")

    def test_allowlists(self):
        self.assertEqual(set(WRITE_ALLOWED), set(WRITE_METHODS))
        self.assertFalse(set(READ_ALLOWED) & set(WRITE_ALLOWED))
        for m in NEVER:
            self.assertNotIn(m, READ_ALLOWED)
            self.assertNotIn(m, WRITE_ALLOWED)
        # each endpoint is where monerod v0.18.5.3 maps the method (core_rpc_server.h)
        for m, ep in list(READ_ALLOWED.items()) + list(WRITE_ALLOWED.items()):
            if m in MONEROD_JSON_RPC:
                self.assertEqual(ep, "/json_rpc", m)
            else:
                self.assertIn(m, MONEROD_PATHS, m)
                self.assertEqual(ep, "/" + m, m)

    def test_write_path_only_for_the_commands_module(self):
        client = RpcClient(self.fake.url, timeout=2)
        ban = {"bans": [{"host": "77.88.100.0/24", "ip": 0, "ban": True, "seconds": 3600}]}
        # call() never sends set_bans, and call_write() refuses anyone but memstara.commands
        with self.assertRaises(RpcNotAllowed):
            client.call("set_bans", ban)
        for module in ("tests", "memstara.control", "memstara.server", "memstara.poller"):
            ns = {"__name__": module}
            exec("def go(c, m, p=None):\n    return c.call_write(m, p)\n", ns)
            with self.assertRaises(RpcNotAllowed, msg=module) as cm:
                ns["go"](client, "set_bans", ban)
            self.assertIn("confirmed command", cm.exception.summary)
        # even from the commands module, only WRITE_ALLOWED goes out through call_write
        ns = {"__name__": "memstara.commands"}
        exec("def go(c, m, p=None):\n    return c.call_write(m, p)\n", ns)
        for m in NEVER + list(READ_ALLOWED):
            with self.assertRaises(RpcNotAllowed, msg=repr(m)):
                ns["go"](client, m)
        self.assertEqual(self.fake.calls, [], "nothing refused reaches the node")
        self.assertEqual(ns["go"](client, "set_bans", ban)["status"], "OK")
        self.assertEqual(self.fake.requests, [("set_bans", ban)])

    def test_only_commands_py_uses_the_write_path(self):
        users = sorted(p.name for p in (helpers.ROOT / "memstara").rglob("*.py")
                       if "call_write(" in p.read_text() and p.name != "rpc.py")
        self.assertEqual(users, ["commands.py"])

    def test_every_allowed_method_answers(self):
        client = RpcClient(self.fake.url, timeout=5)
        top = self.fake.top
        params = {"get_block_headers_range": {"start_height": top - 2, "end_height": top},
                  "get_block": {"height": top}, "get_block_header_by_hash": {"hashes": []},
                  "get_coinbase_tx_sum": {"height": top, "count": 1}, "banned": {"address": "1.2.3.4"},
                  "get_output_distribution": {"amounts": [0], "from_height": top - 9, "to_height": top}}
        for m in READ_ALLOWED:
            res = client.call(m, params.get(m))
            self.assertEqual(res.get("status"), "OK", m)
        self.assertIsInstance(client.last_ms, int)
        self.assertIn("/get_net_stats", self.fake.calls)        # plain path, not JSON-RPC
        self.assertIn("get_info", self.fake.calls)              # JSON-RPC method


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMonerod(login=("monero", "s3cret-pass")).start()

    def tearDown(self):
        self.fake.stop()

    def test_digest_login(self):
        client = RpcClient(self.fake.url, "monero", "s3cret-pass", timeout=5)
        self.assertEqual(client.call("get_info")["status"], "OK")
        self.assertEqual(client.call("get_limit")["status"], "OK")   # reuses the challenge

    def test_wrong_login(self):
        client = RpcClient(self.fake.url, "monero", "wrong-pass", timeout=5)
        with self.assertRaises(RpcError) as cm:
            client.call("get_info")
        self.assertEqual(cm.exception.kind, "login")
        self.assertNotIn("wrong-pass", cm.exception.summary)
        self.assertNotIn("monero:", cm.exception.summary)

    def test_no_login(self):
        with self.assertRaises(RpcError) as cm:
            RpcClient(self.fake.url, timeout=5).call("get_info")
        self.assertEqual(cm.exception.short, "login needed")


class ErrorsTest(unittest.TestCase):
    def test_connection_refused(self):
        port = helpers.closed_port()
        with self.assertRaises(RpcError) as cm:
            RpcClient("http://127.0.0.1:%d" % port, timeout=2).call("get_info")
        e = cm.exception
        self.assertEqual(e.kind, "refused")
        self.assertEqual(e.short, "connection refused")
        self.assertIn("127.0.0.1:%d" % port, e.summary)

    def test_timeout(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)   # accepts, never answers
        try:
            with self.assertRaises(RpcError) as cm:
                RpcClient("http://127.0.0.1:%d" % srv.getsockname()[1], timeout=1).call("get_info")
            self.assertEqual(cm.exception.kind, "timeout")
            self.assertEqual(cm.exception.short, "no answer in 1 s")
        finally:
            srv.close()

    def test_not_json_and_bad_status(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Odd)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        try:
            client = RpcClient("http://127.0.0.1:%d" % httpd.server_address[1], timeout=3)
            with self.assertRaises(RpcError) as cm:
                client.call("get_info")
            self.assertEqual(cm.exception.kind, "bad_json")
            self.assertIn("isn't JSON", cm.exception.summary)
            with self.assertRaises(RpcError) as cm:
                client.call("get_limit")
            self.assertEqual(cm.exception.kind, "status")
            self.assertEqual(cm.exception.short, "BUSY")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_restricted_port(self):
        fake = FakeMonerod(restricted=True).start()
        try:
            client = RpcClient(fake.url, timeout=3)
            self.assertTrue(client.call("get_info")["restricted"])
            for m in ("get_connections", "get_bans", "sync_info", "get_net_stats"):
                with self.assertRaises(RpcError, msg=m) as cm:
                    client.call(m)
                self.assertEqual(cm.exception.kind, "not_offered", m)
        finally:
            fake.stop()

    def test_bad_address(self):
        for url in ("ftp://127.0.0.1:18091", "http://", "127.0.0.1:18091"):
            with self.assertRaises(ValueError):
                RpcClient(url)

    def test_never_uses_env_proxy(self):
        import os
        fake = FakeMonerod().start()
        old = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")}
        try:
            os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:%d" % helpers.closed_port()
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            self.assertEqual(RpcClient(fake.url, timeout=3).call("get_info")["status"], "OK")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            fake.stop()


if __name__ == "__main__":
    unittest.main()
