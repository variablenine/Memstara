"""GET /api/node-log: the tail of monerod's log (memstara/nodelog.py read/tail). Read-only,
regular files only, no symlinks out of the home folder."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import WRITE, get_json, request
from memstara import nodelog
from memstara.app import App
from memstara.settings import Settings
from memstara.util import cache_dir, config_dir, state_dir


class StubSettings:
    def __init__(self, **values):
        self.values = values

    def get(self, key):
        return self.values.get(key)


def log_line(i):
    return "2026-10-05 21:00:%02d.%03d\t[P2P%d]\tINFO\tglobal\tsrc/p2p/net_node.inl:%d\tline %06d" % (
        i % 60, i % 1000, i % 9, 1000 + i, i)


class TailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name)) / "home"
        (self.home / ".bitmonero").mkdir(parents=True)
        self.outside = Path(os.path.realpath(self.tmp.name)) / "elsewhere"
        self.outside.mkdir()
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        self.log = self.home / ".bitmonero" / "bitmonero.log"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def read(self, source="monerod", lines=None, path="~/.bitmonero/bitmonero.log", rpc_url="http://127.0.0.1:18081"):
        return nodelog.read(StubSettings(**{"node.log_path": path, "source.rpc_url": rpc_url}), source, lines)

    def test_an_rpc_address_not_on_this_computer(self):
        self.log.write_text(log_line(1) + "\n")
        for url in ("http://192.0.2.10:18081", "http://node.lan:18081", "https://[2001:db8::5]:18089"):
            st, out = self.read(rpc_url=url)
            self.assertEqual((st, out), (404, {"ok": False, "error": "The node's RPC address isn't on this computer, "
                                               "so the log at the default place may be another node's. To read the "
                                               "node's own, set monerod's log in Settings."}), url)
        self.assertEqual(self.read(path="", rpc_url="http://192.0.2.10:18081")[0], 404, "left empty: the default")
        st, out = self.read(path=str(self.log), rpc_url="http://192.0.2.10:18081")
        self.assertEqual((st, out["lines"]), (200, [log_line(1)]), "the same file set by hand")
        copy = self.outside / "node-copy.log"
        copy.write_text(log_line(2) + "\n")
        st, out = self.read(path=str(copy), rpc_url="http://192.0.2.10:18081")
        self.assertEqual((st, out["path"]), (200, str(copy)), "a copy or mounted folder set by hand")
        for url in ("http://127.0.0.1:18081", "http://127.0.0.2:18081", "http://[::1]:18081", "http://localhost:18081"):
            st, out = self.read(rpc_url=url)
            self.assertEqual((st, out["path"]), (200, str(self.log)), url)

    def test_an_empty_path_reads_the_default(self):
        self.log.write_text(log_line(1) + "\n")
        st, out = self.read(path="")
        self.assertEqual((st, out["path"]), (200, str(self.log)))

    def test_last_lines(self):
        self.log.write_text("".join(log_line(i) + "\n" for i in range(300)))
        st, out = self.read()
        self.assertEqual(st, 200)
        self.assertEqual(set(out), {"ok", "path", "size", "mtime", "lines", "source"})
        self.assertEqual(out["path"], str(self.log))
        self.assertEqual(out["source"], "monerod")
        self.assertEqual(out["size"], self.log.stat().st_size)
        self.assertEqual(out["mtime"], int(self.log.stat().st_mtime))
        self.assertEqual(len(out["lines"]), nodelog.DEFAULT_LINES)
        self.assertEqual(out["lines"][-1], log_line(299))
        st, out = self.read(lines="5")
        self.assertEqual(out["lines"], [log_line(i) for i in range(295, 300)])
        st, out = self.read(lines="1000")
        self.assertEqual(len(out["lines"]), 300)
        self.assertEqual(out["lines"][0], log_line(0))
        json.dumps(out)

    def test_only_monerods_log(self):
        st, out = self.read("other")
        self.assertEqual((st, out["error"]), (400, "source must be monerod"))

    def test_only_the_last_512_kb(self):
        with open(self.log, "w") as fh:
            for i in range(9000):                      # ~1.1 MB of 120-byte lines
                fh.write("%06d " % i + "x" * 113 + "\n")
        st, out = self.read(lines="1000")
        self.assertEqual(st, 200)
        rows = out["lines"]
        self.assertEqual(len(rows), 1000)
        self.assertEqual(rows[-1], "008999 " + "x" * 113)
        self.assertTrue(all(len(r) == 120 for r in rows), "no cut line at the start")
        # a file smaller than 1000 lines' worth but bigger than 512 KB: the cut first line is dropped
        with open(self.log, "w") as fh:
            fh.write("y" * (nodelog.MAX_BYTES + 50) + "\nlast\n")
        st, out = self.read(lines="1000")
        self.assertEqual(out["lines"], ["last"])

    def test_cleaned(self):
        self.log.write_bytes(b"\x1b[1;31mred\x1b[0m warning\x07\r\n" + b"a" * 5000 + b"\n" + b"bad \xff byte\n")
        st, out = self.read()
        self.assertEqual(out["lines"][0], "red warning")
        self.assertEqual(len(out["lines"][1]), nodelog.MAX_LINE_CHARS)
        self.assertEqual(out["lines"][2], "bad � byte")

    def test_never_writes(self):
        self.log.write_text("one\ntwo\n")
        os.utime(self.log, (1700000000, 1700000000))
        before = sorted(os.listdir(self.log.parent)), self.log.read_bytes()
        for _ in range(3):
            self.assertEqual(self.read()[0], 200)
        self.assertEqual((sorted(os.listdir(self.log.parent)), self.log.read_bytes()), before)
        self.assertEqual(int(self.log.stat().st_mtime), 1700000000)

    def test_empty_and_missing(self):
        self.log.write_text("")
        st, out = self.read()
        self.assertEqual((st, out["lines"], out["size"]), (200, [], 0))
        self.log.unlink()
        st, out = self.read()
        self.assertEqual(st, 404)
        self.assertEqual(out, {"ok": False, "error": "monerod's log isn't at %s." % self.log})

    def test_symlinks(self):
        real = self.home / "logs" / "monerod.log"
        real.parent.mkdir()
        real.write_text("inside\n")
        self.log.symlink_to(real)
        st, out = self.read()
        self.assertEqual((st, out["lines"]), (200, ["inside"]), "a link inside home is fine")
        self.log.unlink()
        secret = self.outside / "secret.txt"
        secret.write_text("not for the browser\n")
        self.log.symlink_to(secret)
        st, out = self.read()
        self.assertEqual(st, 403)
        self.assertIn("outside your home folder", out["error"])
        self.assertNotIn("not for the browser", json.dumps(out))
        # a linked folder on the way counts too
        self.log.unlink()
        (self.home / "linkdir").symlink_to(self.outside)
        st, out = self.read(path="~/linkdir/secret.txt")
        self.assertEqual(st, 403)
        # and a chain of links that ends outside
        (self.home / "hop").symlink_to(self.home / "linkdir" / "secret.txt")
        st, out = self.read(path="~/hop")
        self.assertEqual(st, 403)

    def test_not_regular_files(self):
        self.assertEqual(self.read(path="~/.bitmonero")[0], 403)
        (self.home / "old.log").mkdir()
        st, out = self.read(path="~/old.log")
        self.assertEqual(st, 403)
        self.assertIn("isn't a regular file", out["error"])
        for dev in ("/dev/zero", "/dev/urandom"):
            if os.path.exists(dev):
                st, out = self.read(path=dev)
                self.assertEqual(st, 403, dev)
        fifo = self.home / "pipe.log"
        os.mkfifo(fifo)
        got = []
        t = threading.Thread(target=lambda: got.append(self.read(path=str(fifo))), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "a pipe must never block the reader")
        self.assertEqual(got[0][0], 403)
        # a link to a pipe inside home: still refused
        (self.home / "pipe-link").symlink_to(fifo)
        self.assertEqual(self.read(path="~/pipe-link")[0], 403)

    def test_only_log_files_outside_the_dashboards_own_folders(self):
        notes = self.home / "notes.txt"
        notes.write_text("not a log\n")
        st, out = self.read(path="~/notes.txt")
        self.assertEqual((st, out["error"]), (403, "%s isn't a log file (its name has no .log), so it isn't read."
                                              % notes))
        (self.home / "notes.log").symlink_to(notes)
        self.assertEqual(self.read(path="~/notes.log")[0], 403, "the name of the file a link leads to counts")
        # the dashboard's own folders (settings with the RPC password, saved history, the window's
        # cache): never, whatever the name
        for folder in (config_dir(), state_dir(), cache_dir()):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "old.log").write_text("secret\n")
            st, out = self.read(path=str(folder / "old.log"))
            self.assertEqual((st, out["error"]), (403, "%s is in one of the dashboard's own folders, so "
                                                       "it isn't read." % (folder / "old.log")))

    def test_bad_requests(self):
        self.log.write_text("x\n")
        for lines in ("0", "1001", "-5", "abc", "1e3", " 5", "99999"):
            st, out = self.read(lines=lines)
            self.assertEqual(st, 400, lines)
            self.assertEqual(out["error"], "lines must be 1 to 1000")
        for source in ("", "MONEROD", "../etc/passwd", "other", "nope", None):
            st, out = self.read(source=source)
            self.assertEqual(st, 400, source)
        self.assertEqual(self.read(lines="")[0], 200)
        self.assertEqual(self.read(lines=None)[0], 200)


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(cls.tmp.name))
        cls.home = base / "home"
        (cls.home / ".bitmonero").mkdir(parents=True)
        (cls.home / ".bitmonero" / "bitmonero.log").write_text("".join(log_line(i) + "\n" for i in range(20)))
        cls.env = mock.patch.dict(os.environ, {"HOME": str(cls.home)})
        cls.env.start()
        settings = Settings(base / "cfg" / "settings.json", log=lambda m: None).load()
        settings.update({"source.rpc_url": "http://127.0.0.1:%d" % helpers.closed_port()})
        cls.app = App(settings, port=0, web_dir=base / "web", state_dir=base / "state", log=lambda m: None)
        cls.app.start(poll=False)
        cls.port = cls.app.port

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def test_guarded(self):
        st, _, body = request(self.port, "GET", "/api/node-log")
        self.assertEqual(st, 403, "reads a file on this computer: needs the write guard")
        self.assertNotIn(b"line 0000", body)
        for origin in ("http://evil.example", "null"):
            st, _, _ = request(self.port, "GET", "/api/node-log", headers={"X-Memstara": "1", "Origin": origin})
            self.assertEqual(st, 403, origin)
        st, _, _ = request(self.port, "GET", "/api/node-log",
                           headers={"X-Memstara": "1", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 403)
        st, _, _ = request(self.port, "GET", "/api/node-log", headers={"X-Memstara": "1"},
                           host="evil.example:%d" % self.port)
        self.assertEqual(st, 421)

    def test_answers(self):
        st, out = get_json(self.port, "/api/node-log?lines=3", headers={"X-Memstara": "1"})
        self.assertEqual(st, 200)
        self.assertEqual(out["lines"], [log_line(i) for i in (17, 18, 19)])
        self.assertEqual(out["source"], "monerod")
        st, out = get_json(self.port, "/api/node-log?source=monerod", headers=dict(WRITE, Origin="http://127.0.0.1:%d"
                                                                                % self.port))
        self.assertEqual((st, len(out["lines"])), (200, 20))
        st, out = get_json(self.port, "/api/node-log?source=start", headers={"X-Memstara": "1"})
        self.assertEqual((st, out), (400, {"ok": False, "error": "source must be monerod"}), "monerod's log is the only one")
        st, _ = get_json(self.port, "/api/node-log?lines=5000", headers={"X-Memstara": "1"})
        self.assertEqual(st, 400)
        st, _, _ = request(self.port, "POST", "/api/node-log", {}, headers=WRITE)
        self.assertEqual(st, 405)


if __name__ == "__main__":
    unittest.main()
