"""The web server's safety rules and API shapes."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import WRITE, get_json, request
from fake_monerod import FakeMonerod
from memstara.app import App
from memstara.server import CSP, _same, build_static_map
from memstara.settings import Settings


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        web = base / "web"
        (web / "fonts").mkdir(parents=True)
        (web / "index.html").write_text("<!doctype html><title>Memstara</title>")
        (web / "app.js").write_text("export const x = 1;")
        (web / "style.css").write_text("body{}")
        (web / "icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        (web / "fonts" / "Mono.woff2").write_bytes(b"wOF2fake")
        (web / "fonts" / "OFL.txt").write_text("SIL Open Font License")
        (web / ".secret").write_text("hidden")
        (web / "notes.exe").write_bytes(b"MZ")
        (base / "outside.txt").write_text("outside web")
        os.symlink(base / "outside.txt", web / "link.txt")
        cls.fake = FakeMonerod().start()
        cls.settings = Settings(base / "cfg" / "settings.json", log=lambda m: None).load()
        cls.settings.update({"source.rpc_url": cls.fake.url})
        cls.app = App(cls.settings, port=0, web_dir=web, state_dir=base / "state", log=lambda m: None)
        cls.app.start(poll=False)
        cls.app.poller.poll_once()
        cls.port = cls.app.port

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.fake.stop()
        cls.tmp.cleanup()

    # ---- Host check
    def test_wrong_host_421(self):
        for host in ("evil.example:%d" % self.port, "127.0.0.1", "127.0.0.1:1", "localhost.evil.com:%d" % self.port):
            st, _, _ = request(self.port, "GET", "/api/snapshot", host=host)
            self.assertEqual(st, 421, host)
        for host in ("127.0.0.1:%d", "localhost:%d", "[::1]:%d", "LOCALHOST:%d"):
            st, _, _ = request(self.port, "GET", "/api/health", host=host % self.port)
            self.assertEqual(st, 200, host)

    def test_missing_host_421(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"GET /api/health HTTP/1.0\r\n\r\n")
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        self.assertTrue(data.startswith(b"HTTP/1.0 421"), data[:40])

    def test_only_loopback_names(self):
        for name in ("node.lan", "192.168.1.5", "0.0.0.0"):
            st, _, _ = request(self.port, "GET", "/api/health", host="%s:%d" % (name, self.port))
            self.assertEqual(st, 421, name)

    # ---- write guard
    def test_put_without_header_403(self):
        st, _, body = request(self.port, "PUT", "/api/settings", {"values": {"thresholds.low_peers": 9}},
                              headers={"Content-Type": "application/json"})
        self.assertEqual(st, 403)
        self.assertEqual(self.settings.get("thresholds.low_peers"), 8)

    def test_origin_must_match(self):
        body = {"values": {"thresholds.low_peers": 9}}
        for origin in ("http://evil.example", "null", "http://127.0.0.1:1", "https://127.0.0.1:%d" % self.port):
            st, _, _ = request(self.port, "PUT", "/api/settings", body, headers=dict(WRITE, Origin=origin))
            self.assertEqual(st, 403, origin)
        st, _, _ = request(self.port, "POST", "/api/settings/reset", {"section": "thresholds"},
                           headers=dict(WRITE, **{"Sec-Fetch-Site": "cross-site"}))
        self.assertEqual(st, 403)
        self.assertEqual(self.settings.get("thresholds.low_peers"), 8)
        st, _, _ = request(self.port, "PUT", "/api/settings", body,
                           headers=dict(WRITE, Origin="http://127.0.0.1:%d" % self.port))
        self.assertEqual(st, 200)
        self.settings.reset_section("thresholds")

    def test_oversized_body_413(self):
        big = json.dumps({"values": {"source.rpc_user": "x" * (70 * 1024)}}).encode()
        st, _, _ = request(self.port, "PUT", "/api/settings", raw_body=big, headers=WRITE)
        self.assertEqual(st, 413)

    def test_bad_bodies(self):
        st, _, _ = request(self.port, "PUT", "/api/settings", raw_body=b"{nope", headers=WRITE)
        self.assertEqual(st, 400)
        st, _, _ = request(self.port, "PUT", "/api/settings", raw_body=b"[1,2]", headers=WRITE)
        self.assertEqual(st, 400)

    # ---- static files
    def test_static_files_and_types(self):
        cases = {"/": "text/html; charset=utf-8", "/index.html": "text/html; charset=utf-8",
                 "/app.js": "text/javascript; charset=utf-8", "/style.css": "text/css; charset=utf-8",
                 "/icon.svg": "image/svg+xml", "/fonts/Mono.woff2": "font/woff2",
                 "/fonts/OFL.txt": "text/plain; charset=utf-8"}
        for path, ctype in cases.items():
            st, hd, _ = request(self.port, "GET", path)
            self.assertEqual((st, hd.get("content-type")), (200, ctype), path)
        for path in ("/.secret", "/notes.exe", "/link.txt", "/missing.js"):
            st, _, _ = request(self.port, "GET", path)
            self.assertEqual(st, 404, path)

    def test_path_traversal(self):
        for path in ("/../memstara/settings.py", "/%2e%2e/memstara/settings.py", "/%2E%2E%2Fmemstara%2Fsettings.py",
                     "/fonts/../../memstara/settings.py", "/..%2f..%2fetc%2fpasswd", "//etc/passwd",
                     "/web/../memstara/server.py", "/fonts/%2e%2e/%2e%2e/tests/helpers.py",
                     "/" + str(helpers.ROOT / "memstara" / "settings.py")):
            st, _, body = request(self.port, "GET", path)
            self.assertEqual(st, 404, path)
            self.assertNotIn(b"import", body, path)

    def test_static_map_only_lists_web(self):
        m = build_static_map(Path(self.tmp.name) / "web")
        self.assertEqual(sorted(m), ["/app.js", "/fonts/Mono.woff2", "/fonts/OFL.txt", "/icon.svg",
                                     "/index.html", "/style.css"])
        self.assertEqual(build_static_map(Path(self.tmp.name) / "nope"), {})

    # ---- headers
    def test_security_headers_everywhere(self):
        for method, path, host in (("GET", "/", None), ("GET", "/api/snapshot", None), ("GET", "/nope", None),
                                   ("GET", "/api/nope", None), ("GET", "/api/health", "evil:1"),
                                   ("DELETE", "/api/settings", None)):
            st, hd, _ = request(self.port, method, path, host=host)
            self.assertEqual(hd.get("content-security-policy"), CSP, path)
            self.assertEqual(hd.get("x-content-type-options"), "nosniff", path)
            self.assertEqual(hd.get("referrer-policy"), "no-referrer", path)
            self.assertFalse([k for k in hd if k.startswith("access-control-")], "no CORS headers, ever")
        _, hd, _ = request(self.port, "GET", "/api/snapshot")
        self.assertEqual(hd.get("cache-control"), "no-store")
        self.assertIn("frame-ancestors 'none'", CSP)
        _, hd, _ = request(self.port, "GET", "/api/health")
        self.assertNotIn("Python", hd.get("server", ""))

    # ---- API
    def test_api_basics(self):
        st, body = get_json(self.port, "/api/health")
        self.assertEqual(body, {"ok": True, "version": "0.1.0"})
        st, _, _ = request(self.port, "GET", "/api/demo/scenario")
        self.assertEqual(st, 404, "no demo mode: only a real node is read")
        st, _, _ = request(self.port, "POST", "/api/demo/scenario", {"scenario": "reorg"}, headers=WRITE)
        self.assertEqual(st, 404)
        st, _, _ = request(self.port, "DELETE", "/api/settings")
        self.assertEqual(st, 405)
        st, _, _ = request(self.port, "OPTIONS", "/api/settings")
        self.assertEqual(st, 405)
        st, _ = get_json(self.port, "/api/history?window=123")
        self.assertEqual(st, 400)
        st, body = get_json(self.port, "/api/history?window=900")
        self.assertEqual(st, 200)
        self.assertEqual(set(body), {"window", "step", "t", "out", "in", "tor", "pool", "rate_in", "rate_out",
                                     "latency"})
        st, body = get_json(self.port, "/api/history")
        self.assertEqual(body["window"], 3600)

    def test_no_addons(self):
        for path in ("/api/addons", "/addons/example/web/panel.js", "/addons/", "/addons/x/addon.py"):
            st, _, _ = request(self.port, "GET", path)
            self.assertEqual(st, 404, path)

    def test_no_discover(self):
        """Nothing reads other processes to find the node: there's no /api/discover."""
        good = {"X-Memstara": "1", "Origin": "http://127.0.0.1:%d" % self.port}
        for headers in ({}, good):
            st, body = get_json(self.port, "/api/discover", headers=headers)
            self.assertEqual((st, body), (404, {"error": "No such API"}), headers)
        st, _, _ = request(self.port, "POST", "/api/discover", {}, headers=WRITE)
        self.assertEqual(st, 404)

    def test_settings_api(self):
        st, body = get_json(self.port, "/api/settings")
        self.assertEqual(st, 200)
        self.assertEqual(set(body), {"values", "schema", "rpc_password_set"})
        self.assertEqual(body["values"]["source.rpc_password"], "")
        st, _, raw = request(self.port, "PUT", "/api/settings",
                             {"values": {"source.rpc_password": "pw-should-not-echo", "thresholds.low_peers": 7}},
                             headers=WRITE)
        self.assertEqual(st, 200)
        self.assertNotIn(b"pw-should-not-echo", raw)
        body = json.loads(raw)
        self.assertTrue(body["rpc_password_set"])
        self.assertEqual(body["values"]["thresholds.low_peers"], 7)
        st, _, raw = request(self.port, "PUT", "/api/settings",
                             {"values": {"thresholds.no_block_bad_min": 10, "bogus": 1}}, headers=WRITE)
        self.assertEqual(st, 400)
        self.assertEqual(set(json.loads(raw)["errors"]), {"bogus"})
        st, _, raw = request(self.port, "PUT", "/api/settings", {"values": {"thresholds.no_block_bad_min": 10}},
                             headers=WRITE)
        self.assertEqual(json.loads(raw), {"errors": {"thresholds.no_block_bad_min":
                                                      "must be longer than the heads-up time (30 min)"}})
        st, _, raw = request(self.port, "POST", "/api/settings/reset", {"section": "thresholds"}, headers=WRITE)
        self.assertEqual(json.loads(raw)["values"]["thresholds.low_peers"], 8)
        st, _, _ = request(self.port, "POST", "/api/settings/reset", {"section": "nope"}, headers=WRITE)
        self.assertEqual(st, 400)
        self.settings.update({"source.rpc_password": None})

    def test_test_connection(self):
        st, _, raw = request(self.port, "POST", "/api/test-connection",
                             {"rpc_url": self.fake.url, "rpc_user": ""}, headers=WRITE)
        body = json.loads(raw)
        self.assertEqual(st, 200)
        self.assertEqual(set(body), {"ok", "version", "height", "restricted", "latency_ms", "error"})
        self.assertTrue(body["ok"])
        self.assertEqual(body["height"], self.fake.top + 1)
        self.assertFalse(body["restricted"])
        st, _, raw = request(self.port, "POST", "/api/test-connection",
                             {"rpc_url": "http://127.0.0.1:%d" % helpers.closed_port()}, headers=WRITE)
        body = json.loads(raw)
        self.assertFalse(body["ok"])
        self.assertIn("connection refused", body["error"])
        st, _, _ = request(self.port, "POST", "/api/test-connection", {"rpc_url": "file:///etc/passwd"},
                           headers=WRITE)
        self.assertEqual(st, 400)

    def test_test_connection_sends_the_saved_password_only_to_the_saved_node(self):
        self.settings.update({"source.rpc_password": "pw-saved"})
        self.addCleanup(self.settings.update, {"source.rpc_password": None})
        port = self.fake.port
        cases = {"http://127.0.0.1:%d" % port: "pw-saved", "http://localhost:%d" % port: "pw-saved",
                 "http://[::1]:%d" % port: "pw-saved", "http://127.0.0.1:%d" % (port + 1): "",
                 "https://127.0.0.1:%d" % port: "", "http://192.0.2.1:%d" % port: ""}
        for url, want in cases.items():
            with mock.patch.object(self.app, "test_connection", return_value={"ok": False}) as tc:
                st, _, _ = request(self.port, "POST", "/api/test-connection", {"rpc_url": url, "rpc_user": "u"},
                                   headers=WRITE)
            self.assertEqual(st, 200, url)
            self.assertEqual(tc.call_args[0][2], want, url)
        with mock.patch.object(self.app, "test_connection", return_value={"ok": False}) as tc:
            request(self.port, "POST", "/api/test-connection", {"rpc_url": "http://192.0.2.1:18081", "rpc_user": "u",
                                                                "rpc_password": "typed"}, headers=WRITE)
        self.assertEqual(tc.call_args[0][2], "typed", "a password typed in the form goes where it's tested")



class WindowKeyTest(unittest.TestCase):
    """The app window's key: without it the page and the API answer 403, so a browser or
    any other program on the computer can't use the dashboard; with it, the usual rules."""

    KEY = "k3y-for-the-window_only"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        web = base / "web"
        (web / "js").mkdir(parents=True)
        (web / "index.html").write_text("<!doctype html><title>Memstara</title>")
        (web / "js" / "main.js").write_text("export const x = 1;")
        cls.fake = FakeMonerod().start()
        cls.settings = Settings(base / "cfg" / "settings.json", log=lambda m: None).load()
        cls.settings.update({"source.rpc_url": cls.fake.url})
        cls.app = App(cls.settings, port=0, web_dir=web, state_dir=base / "state", log=lambda m: None, key=cls.KEY)
        cls.app.start(poll=False)
        cls.app.poller.poll_once()
        cls.port = cls.app.port

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.fake.stop()
        cls.tmp.cleanup()

    def key(self, value=None):
        return {"X-Memstara-Key": self.KEY if value is None else value}

    def test_window_url_carries_the_key(self):
        self.assertEqual(self.app.window_url, "http://127.0.0.1:%d/?key=%s" % (self.port, self.KEY))
        self.assertEqual(self.app.url, "http://127.0.0.1:%d/" % self.port, "url itself has no key")

    def test_page_needs_the_key(self):
        for path in ("/", "/index.html", "/?key=", "/?key=wrong", "/?key=%s" % self.KEY[:-1], "/?other=%s" % self.KEY):
            st, hd, body = request(self.port, "GET", path)
            self.assertEqual(st, 403, path)
            self.assertIn(b"its own window", body, path)
            self.assertIn("default-src 'self'", hd.get("content-security-policy", ""), path)
        for path in ("/?key=%s" % self.KEY, "/index.html?key=%s" % self.KEY, "/?key=%s#network" % self.KEY):
            st, _, body = request(self.port, "GET", path)
            self.assertEqual(st, 200, path)
            self.assertIn(b"<title>Memstara</title>", body)

    def test_screens_files_need_no_key(self):
        st, _, body = request(self.port, "GET", "/js/main.js")
        self.assertEqual(st, 200, "the screens' own code: nothing private in it")
        self.assertEqual(body, b"export const x = 1;")
        st, _, body = request(self.port, "GET", "/js/%6Dain.js")
        self.assertEqual((st, body), (200, b"export const x = 1;"), "an encoded name is the same file")

    def test_encoded_page_names_need_the_key(self):
        # the server reads the page under its decoded name, so the key check does too
        for path in ("/%69ndex.html", "/index%2Ehtml", "/index.htm%6C", "/%2Findex.html"):
            st, _, body = request(self.port, "GET", path)
            self.assertEqual(st, 403, path)
            self.assertIn(b"its own window", body, path)
        st, _, body = request(self.port, "GET", "/%69ndex.html?key=" + self.KEY)
        self.assertEqual(st, 200)
        self.assertIn(b"<title>Memstara</title>", body)

    def test_unknown_paths_need_the_key(self):
        st, _, _ = request(self.port, "GET", "/nope.txt")
        self.assertEqual(st, 403, "without the key there's nothing to learn, not even what's missing")
        st, _, _ = request(self.port, "GET", "/nope.txt?key=%s" % self.KEY)
        self.assertEqual(st, 404)

    def test_an_empty_key_matches_nothing(self):
        self.assertFalse(_same("", ""))
        self.assertFalse(_same("x", ""))
        self.assertFalse(_same("", self.KEY))
        self.assertTrue(_same(self.KEY, self.KEY))

    def test_api_needs_the_key(self):
        for headers in ({}, self.key(""), self.key("wrong"), self.key(self.KEY.upper()), self.key(self.KEY + "x")):
            for path in ("/api/health", "/api/snapshot", "/api/settings", "/api/history?window=3600", "/api/nope"):
                st, hd, body = request(self.port, "GET", path, headers=headers)
                self.assertEqual(st, 403, (path, headers))
                self.assertEqual(hd.get("content-type"), "application/json")
                self.assertIn("its own window", json.loads(body)["error"])
        st, _, body = request(self.port, "GET", "/api/snapshot", headers=self.key())
        self.assertEqual(st, 200)
        self.assertIn("node", json.loads(body))
        # the key in the address is for the page only: the API wants the header
        st, _, _ = request(self.port, "GET", "/api/health?key=%s" % self.KEY)
        self.assertEqual(st, 403)

    def test_changes_need_the_key_and_the_write_guard(self):
        body = {"values": {"thresholds.low_peers": 9}}
        st, _, _ = request(self.port, "PUT", "/api/settings", body, headers=WRITE)
        self.assertEqual(st, 403, "no key")
        st, _, _ = request(self.port, "PUT", "/api/settings", body,
                           headers=dict(self.key(), **{"Content-Type": "application/json"}))
        self.assertEqual(st, 403, "key but no X-Memstara header")
        self.assertEqual(self.settings.get("thresholds.low_peers"), 8)
        st, _, _ = request(self.port, "PUT", "/api/settings", body, headers=dict(WRITE, **self.key()))
        self.assertEqual(st, 200)
        self.assertEqual(self.settings.get("thresholds.low_peers"), 9)
        self.settings.reset_section("thresholds")

    def test_host_check_comes_first(self):
        st, _, body = request(self.port, "GET", "/api/health", headers=self.key(), host="evil.example:%d" % self.port)
        self.assertEqual(st, 421)
        self.assertIn("its own window", json.loads(body)["error"])
        st, _, body = request(self.port, "GET", "/?key=%s" % self.KEY, host="evil.example:%d" % self.port)
        self.assertEqual(st, 421)
        self.assertIn(b"its own window", body)


if __name__ == "__main__":
    unittest.main()
