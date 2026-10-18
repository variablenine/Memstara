"""The RPC client: the read-only list, Digest login, plain-language errors (the list itself is
checked against monerod's change calls in test_safety.py)."""

from __future__ import annotations

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers  # noqa: F401  (sets up sys.path)
from fake_monerod import FakeMonerod
from memstara import rpc
from memstara.rpc import READ_ALLOWED, RpcClient, RpcError, RpcNotAllowed
from test_safety import CHANGE_METHODS

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

# methods monerod has that Memstara never sends (besides the ones that change something)
NEVER = ["submit_block", "submitblock", "generateblocks", "calc_pow", "add_aux_pow", "get_block_template",
         "send_raw_transaction", "sendrawtransaction", "get_outs", "rpc_access_pay",
         "rpc_access_submit_nonce", "rpc_access_tracking", "rpc_access_account", "get_public_nodes",
         "get_alt_blocks_hashes", "get_txpool_backlog", "get_miner_data", "getinfo", "getheight",
         "start_save_graph", "", None, 42]


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

    def test_refused_without_any_request(self):
        client = RpcClient(self.fake.url, timeout=2)
        for m in sorted(CHANGE_METHODS) + NEVER:
            with self.assertRaises(RpcNotAllowed, msg=repr(m)):
                client.call(m)
        self.assertEqual(self.fake.calls, [], "a refused method must never reach the node")

    def test_refused_before_connecting(self):
        # nothing listens here: a refused method must not even try to connect
        client = RpcClient("http://127.0.0.1:%d" % helpers.closed_port(), timeout=2)
        with self.assertRaises(RpcNotAllowed) as cm:
            client.call("set_bans", {"bans": [{"host": "100.64.0.1", "ban": True}]})
        self.assertEqual(cm.exception.kind, "not_allowed")

    def test_endpoints(self):
        for m in NEVER:
            self.assertNotIn(m, READ_ALLOWED)
        # each endpoint is where monerod v0.18.5.3 maps the method (core_rpc_server.h)
        for m, ep in READ_ALLOWED.items():
            if m in MONEROD_JSON_RPC:
                self.assertEqual(ep, "/json_rpc", m)
            else:
                self.assertIn(m, MONEROD_PATHS, m)
                self.assertEqual(ep, "/" + m, m)

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


class AddressTest(unittest.TestCase):
    """same_rpc and on_this_computer: only the addresses are looked at, nothing is sent."""

    def test_same_rpc(self):
        same = rpc.same_rpc
        self.assertTrue(same("http://127.0.0.1:18081", "http://127.0.0.1:18081/"))
        self.assertTrue(same("http://localhost:18081", "http://[::1]:18081"), "loopback names are the same host")
        self.assertTrue(same("http://NODE.lan:18081", "http://node.lan:18081"))
        self.assertTrue(same("http://192.0.2.5", "http://192.0.2.5:80"), "the scheme's own port")
        self.assertFalse(same("http://127.0.0.1:18081", "https://127.0.0.1:18081"))
        self.assertFalse(same("http://127.0.0.1:18081", "http://127.0.0.1:18089"))
        self.assertFalse(same("http://127.0.0.1:18081", "http://192.0.2.5:18081"))
        self.assertFalse(same("", "http://127.0.0.1:18081"))

    def test_on_this_computer(self):
        for url in ("http://127.0.0.1:18081", "http://127.8.9.10:18081", "http://[::1]:18081",
                    "http://localhost:18081", "https://LOCALHOST:18089/", "http://0.0.0.0:18081",
                    "http://[::]:18081"):
            self.assertTrue(rpc.on_this_computer(url), url)
        for url in ("http://192.0.2.10:18081", "http://node.lan:18081", "http://[2001:db8::5]:18081",
                    "http://localhost.example:18081", ""):
            self.assertFalse(rpc.on_this_computer(url), url)


if __name__ == "__main__":
    unittest.main()
