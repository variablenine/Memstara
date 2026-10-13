"""The add-on mechanism (memstara/addons.py), with throwaway add-ons written to a temp
folder: one that works, one that fails at import, one whose poll() fails. Also the
first-run search for the node (app.find_node_on_first_run)."""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import WRITE, StubDiscover, StubHost, found_node, get_json, request
from fake_monerod import FakeMonerod
from memstara import addons as A
from memstara.app import App, find_node_on_first_run
from memstara.lamps import LAMPS as CORE_LAMPS
from memstara.settings import SECTIONS, Settings

GOOD = '''
from memstara.addons import Addon
from memstara.settings import entry


class Good(Addon):
    name = "good"
    title = "Good one"
    intro = "A test add-on that works."

    def __init__(self):
        self.polls = []

    def settings(self):
        return [entry("good.log_path", "path", "~/good.log", "Its log"),
                entry("good.level", "choice", "warn", "Level", choices=[("warn", "Warn"), ("bad", "Bad")])]

    def poll(self, settings, now, ctx):
        self.polls.append((now, settings["good.level"], ctx["node"]["height"]))
        settings["good.level"] = "changed"           # copies: nothing leaks back
        ctx["node"]["height"] = -1
        return {"n": len(self.polls), "level": self.polls[-1][1]}

    def lamps(self, data, ctx):
        mine = [a for a in ctx["alerts"] if a["source"] == "good"]
        return [{"id": "thing", "label": "Thing", "state": data["level"], "value": "%d alert" % len(mine),
                 "help": "A thing it watches."},
                {"id": "Bad Id?", "label": "x"}][:1]

    def alerts(self, data, ctx):
        return [{"id": "thing_" + data["level"], "level": data["level"], "text": "Thing is " + data["level"],
                 "title": "Thing trouble"},
                {"id": "disk", "level": "bad", "text": "a core id: ignored"}]

    def route(self, data, ctx):
        return [{"id": "box", "label": "BOX", "value": "OK", "level": "ok", "between": "peers-node",
                 "extra": "dropped"}]

    def command_notes(self, data):
        return {"save": "Good note.", "exit": "Alias note.", "nope": "Not a command."}

    def protected_hosts(self, data):
        return ["8.8.4.4", "not an address", "8.8.4.4"]

    def log_sources(self):
        return [{"id": "main", "title": "Good's log", "setting": "good.log_path", "summary": "What it writes."}]

    def web(self):
        return {"scripts": ["good.js"], "styles": ["css/good.css"]}


ADDON = Good()
'''

BROKEN = '''
raise RuntimeError("boom at import")
'''

FLAKY = '''
from memstara.addons import Addon

FAIL = {"poll": False, "lamps": False}


class Flaky(Addon):
    title = "Flaky"

    def poll(self, settings, now, ctx):
        if FAIL["poll"]:
            raise ValueError("boom in poll")
        return {"ok": True}

    def lamps(self, data, ctx):
        if FAIL["lamps"]:
            raise KeyError("lamp")
        return [{"id": "flake", "label": "Flake", "state": "ok", "value": "fine"}]

    def alerts(self, data, ctx):
        return [{"id": "flake_up", "level": "warn", "text": "Flaky says hi"}]


ADDON = Flaky()
'''


def write_addon(root: Path, name: str, code: str, web: dict | None = None):
    d = root / name
    d.mkdir(parents=True)
    (d / "addon.py").write_text(textwrap.dedent(code))
    for rel, text in (web or {}).items():
        f = d / "web" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return d


class Rig:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.root = base / "addons"
        write_addon(self.root, "good", GOOD, {"good.js": "export const good = 1;", "css/good.css": "b{}",
                                              "secret.txt": "not listed"})
        write_addon(self.root, "broken", BROKEN, {"x.js": "nope"})
        write_addon(self.root, "flaky", FLAKY)
        (self.root / "_skipped").mkdir()
        (self.root / "_skipped" / "addon.py").write_text("raise SystemExit('never loaded')")
        (self.root / "notes").mkdir()                       # no addon.py: not an add-on
        (self.root / "README.md").write_text("not a folder")
        self.clock = helpers.FakeClock()
        self.fake = FakeMonerod(clock=self.clock).start()
        (base / "good.log").write_text("good line\n")
        self.settings = Settings(base / "cfg" / "settings.json", log=lambda m: None).load()
        self.logs = []
        self.app = App(self.settings, port=0, web_dir=base / "web", state_dir=base / "state", clock=self.clock,
                       log=self.logs.append, addons_dir=self.root, host_reader=StubHost(),
                       discover=StubDiscover([found_node(self.fake.url)])).start(poll=False)
        self.settings.update({"source.rpc_url": self.fake.url, "good.log_path": str(base / "good.log")})
        self.port = self.app.port
        self.guard = {"X-Memstara": "1", "Origin": "http://127.0.0.1:%d" % self.port}
        self.mods = {r.name: r for r in self.app.addons.items}

    def flaky(self):
        import sys
        return sys.modules["memstara_addon_flaky"]

    def poll(self, advance=5):
        self.clock.advance(advance)
        self.app.poller.poll_once()
        st, snap = get_json(self.port, "/api/snapshot")
        assert st == 200
        return snap

    def close(self):
        self.app.stop()
        self.fake.stop()
        self.tmp.cleanup()


def by_id(rows):
    return {r["id"]: r for r in rows}


class LoaderTest(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()

    def tearDown(self):
        self.rig.close()

    def test_what_loaded(self):
        rig = self.rig
        self.assertEqual([r.name for r in rig.app.addons.items], ["broken", "flaky", "good"],
                         "name order; _skipped and folders without addon.py are left alone")
        self.assertIsNone(rig.mods["broken"].addon)
        self.assertEqual(rig.mods["broken"].error, "It didn't load: RuntimeError: boom at import")
        self.assertIn("add-on broken: It didn't load: RuntimeError: boom at import", rig.logs)
        self.assertNotIn("never loaded", json.dumps(rig.logs))
        self.assertEqual(rig.mods["flaky"].title, "Flaky")
        self.assertEqual(rig.mods["flaky"].addon.name, "", "a blank name takes the folder's")
        st, out = get_json(rig.port, "/api/addons")
        self.assertEqual(st, 200)
        self.assertEqual(out, {"addons": [
            {"name": "broken", "title": "broken", "intro": "", "enabled": False,
             "error": "It didn't load: RuntimeError: boom at import", "scripts": [], "styles": [], "logs": []},
            {"name": "flaky", "title": "Flaky", "intro": "", "enabled": True, "error": None, "scripts": [],
             "styles": [], "logs": []},
            {"name": "good", "title": "Good one", "intro": "A test add-on that works.", "enabled": True,
             "error": None, "scripts": ["/addons/good/web/good.js"], "styles": ["/addons/good/web/css/good.css"],
             "logs": [{"source": "good.main", "title": "Good's log", "summary": "What it writes."}]}]})

    def test_settings(self):
        rig = self.rig
        st, body = get_json(rig.port, "/api/settings")
        keys = [e["key"] for e in body["schema"]]
        self.assertEqual([k for k in keys if k.split(".")[0] in ("good", "flaky", "broken")],
                         ["flaky.enabled", "good.enabled", "good.log_path", "good.level"])
        en = by_id([dict(e, id=e["key"]) for e in body["schema"]])["good.enabled"]
        self.assertEqual((en["type"], en["default"], en["section"], en["label"]), ("bool", True, "good",
                                                                                   "Use this add-on"))
        self.assertEqual(body["values"]["good.level"], "warn")
        st, _, raw = request(rig.port, "PUT", "/api/settings", {"values": {"good.level": "nope"}}, headers=WRITE)
        self.assertEqual(st, 400)
        self.assertIn("good.level", json.loads(raw)["errors"])
        st, _, raw = request(rig.port, "POST", "/api/settings/reset", {"section": "good"}, headers=WRITE)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(raw)["values"]["good.log_path"], "~/good.log")

    def test_snapshot(self):
        rig = self.rig
        snap = rig.poll(0)
        good = rig.mods["good"].addon
        self.assertEqual(good.polls[-1][1:], ("warn", snap["node"]["height"]),
                         "flat settings and the core snapshot so far")
        self.assertGreater(snap["node"]["height"], 0, "ctx and settings are copies")
        self.assertEqual(rig.settings.get("good.level"), "warn")
        self.assertEqual(snap["addons"], {"flaky": {"ok": True}, "good": {"n": 1, "level": "warn"}})
        lamps = snap["lamps"]
        self.assertEqual([l["id"] for l in lamps], [i for i, _ in CORE_LAMPS] + ["flaky.flake", "good.thing"])
        self.assertEqual(by_id(lamps)["good.thing"], {"id": "good.thing", "label": "THING", "state": "warn",
                                                      "value": "1 ALERT", "since": int(rig.clock()),
                                                      "addon": "good", "help": "A thing it watches."})
        self.assertEqual(by_id(lamps)["flaky.flake"]["help"], "")
        a = by_id(snap["alerts"])
        self.assertEqual(set(a), {"thing_warn", "flake_up"}, "a core id from an add-on is ignored")
        self.assertEqual((a["thing_warn"]["source"], a["thing_warn"]["title"]), ("good", "THING TROUBLE"))
        self.assertEqual(a["flake_up"]["source"], "flaky")
        self.assertEqual(snap["route"], [{"id": "box", "label": "BOX", "value": "OK", "level": "ok",
                                          "between": "peers-node", "addon": "good"}])
        self.assertEqual(rig.app.poller.protected_addresses(), [("8.8.4.4", "Good one")])
        st, out = get_json(rig.port, "/api/commands")
        cmds = {c["name"]: c for c in out["commands"]}
        self.assertEqual(cmds["save"]["warn"], "Good note.")
        self.assertTrue(cmds["stop_daemon"]["warn"].endswith(" Alias note."))
        self.assertNotIn("Not a command.", json.dumps(out))
        # an add-on setting change reaches poll()
        rig.settings.update({"good.level": "bad"})
        snap = rig.poll()
        a = by_id(snap["alerts"])
        self.assertIn("thing_bad", a)
        self.assertNotIn("thing_warn", a)
        self.assertIn("Cleared: THING TROUBLE", [e["text"] for e in snap["events"]])

    def test_static_files(self):
        rig = self.rig
        st, hd, body = request(rig.port, "GET", "/addons/good/web/good.js")
        self.assertEqual((st, hd["content-type"], body), (200, "text/javascript; charset=utf-8",
                                                          b"export const good = 1;"))
        st, hd, _ = request(rig.port, "GET", "/addons/good/web/css/good.css")
        self.assertEqual((st, hd["content-type"]), (200, "text/css; charset=utf-8"))
        self.assertEqual(hd.get("x-content-type-options"), "nosniff")
        for path in ("/addons/good/web/secret.txt", "/addons/good/addon.py", "/addons/broken/web/x.js",
                     "/addons/good/web/../addon.py", "/addons/good/web/%2e%2e/addon.py", "/addons/good/web/",
                     "/addons/flaky/web/x.js"):
            st, _, _ = request(rig.port, "GET", path)
            self.assertEqual(st, 404, path)

    def test_log_source(self):
        rig = self.rig
        st, out = get_json(rig.port, "/api/node-log?source=good.main", headers=rig.guard)
        self.assertEqual((st, out["lines"], out["source"]), (200, ["good line"], "good.main"))
        st, out = get_json(rig.port, "/api/node-log?source=broken.main", headers=rig.guard)
        self.assertEqual((st, out["error"]), (400, "source must be monerod or good.main"))
        rig.settings.update({"good.enabled": False})
        st, out = get_json(rig.port, "/api/node-log?source=good.main", headers=rig.guard)
        self.assertEqual((st, out["error"]), (404, "That log comes from the Good one add-on, which is turned off."))

    def test_disabled(self):
        rig = self.rig
        rig.poll(0)
        good = rig.mods["good"].addon
        n = len(good.polls)
        rig.settings.update({"good.enabled": False})
        snap = rig.poll()
        self.assertEqual(len(good.polls), n, "a turned-off add-on isn't called")
        self.assertNotIn("good", snap["addons"])
        self.assertNotIn("good.thing", [l["id"] for l in snap["lamps"]])
        self.assertEqual(snap["route"], [])
        self.assertNotIn("thing_warn", by_id(snap["alerts"]))
        self.assertNotIn("Cleared: THING TROUBLE", [e["text"] for e in snap["events"]], "it goes quietly")
        self.assertEqual(rig.app.poller.protected_addresses(), [])
        st, out = get_json(rig.port, "/api/commands")
        self.assertNotIn("Good note.", json.dumps(out))
        rig.settings.update({"good.enabled": True})
        snap = rig.poll()
        self.assertIn("thing_warn", by_id(snap["alerts"]))

    def test_a_failing_poll(self):
        rig = self.rig
        snap = rig.poll(0)
        self.assertIn("flake_up", by_id(snap["alerts"]))
        fail = rig.flaky().FAIL
        fail["poll"] = True
        snap = rig.poll()
        self.assertNotIn("flaky", snap["addons"])
        self.assertEqual(by_id(snap["lamps"])["flaky.flake"],
                         {"id": "flaky.flake", "label": "FLAKE", "state": "off", "value": "ADD-ON ERROR",
                          "since": None, "addon": "flaky", "help": ""})
        self.assertNotIn("flake_up", by_id(snap["alerts"]), "held: not shown...")
        self.assertEqual([e for e in snap["events"] if e["kind"] == "clear"], [], "...and not cleared")
        # the core and the other add-ons carry on
        self.assertEqual(snap["source"]["answering"], True)
        self.assertEqual(snap["addons"]["good"]["n"], 2)
        st, out = get_json(rig.port, "/api/addons")
        flaky = [x for x in out["addons"] if x["name"] == "flaky"][0]
        self.assertEqual((flaky["enabled"], flaky["error"]), (True, "poll() failed: ValueError: boom in poll"))
        # logged at most once a minute
        for _ in range(5):
            rig.poll(10)
        lines = [l for l in rig.logs if l.startswith("add-on flaky:")]
        self.assertEqual(lines, ["add-on flaky: poll() failed: ValueError: boom in poll"])
        rig.poll(30)
        self.assertEqual(len([l for l in rig.logs if l.startswith("add-on flaky:")]), 2)
        # it recovers: the error goes, the held alert is back without a new announcement
        fail["poll"] = False
        snap = rig.poll()
        self.assertIn("flake_up", by_id(snap["alerts"]))
        self.assertEqual(by_id(snap["lamps"])["flaky.flake"]["value"], "FINE")
        st, out = get_json(rig.port, "/api/addons")
        self.assertIsNone([x for x in out["addons"] if x["name"] == "flaky"][0]["error"])
        self.assertEqual(len([e for e in snap["events"] if e["text"].endswith(": Flaky says hi")]), 1)

    def test_a_failing_lamps_hook(self):
        rig = self.rig
        rig.poll(0)
        rig.flaky().FAIL["lamps"] = True
        snap = rig.poll()
        self.assertEqual(by_id(snap["lamps"])["flaky.flake"]["value"], "ADD-ON ERROR")
        self.assertEqual(snap["addons"]["flaky"], {"ok": True}, "poll() still worked")
        self.assertIn("flake_up", by_id(snap["alerts"]))
        self.assertEqual(rig.mods["flaky"].error, "lamps() failed: KeyError: 'lamp'")
        rig.flaky().FAIL["lamps"] = False
        rig.poll()
        self.assertIsNone(rig.mods["flaky"].error)


class BadAddonTest(unittest.TestCase):
    """Each of these stays off with a plain reason; the others still load."""

    def load(self, files: dict):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, code in files.items():
            write_addon(root, name, code, {"ok.js": "1", "ok.css": "b{}"})
        logs = []
        return {r.name: r for r in A.Addons(root, logs.append).items}, logs

    def test_reasons(self):
        head = "from memstara.addons import Addon\nfrom memstara.settings import entry\n"
        cases = {
            "Bad_Name": (head + "ADDON = Addon()", "its folder name must be lowercase"),
            "api": (head + "ADDON = Addon()", "api is a name the dashboard itself uses"),
            "source": (head + "ADDON = Addon()", "source is a name the dashboard itself uses"),
            "noaddon": ("X = 1", "addon.py must define ADDON"),
            "notaddon": ("ADDON = object()", "addon.py must define ADDON"),
            "mismatch": (head + "class M(Addon):\n    name = 'other'\nADDON = M()", "must be its folder's name"),
            "exits": ("raise SystemExit(3)", "It didn't load: SystemExit: 3"),
            "syntax": ("def (:", "It didn't load: SyntaxError"),
            "badkey": (head + "class M(Addon):\n    def settings(self):\n"
                              "        return [entry('other.x', 'int', 1, 'X', min_=0, max_=5)]\nADDON = M()",
                       "badkey."),
            "twice": (head + "class M(Addon):\n    def settings(self):\n"
                             "        return [entry('twice.x', 'bool', True, 'X'), entry('twice.x', 'bool', True, 'X')]\n"
                             "ADDON = M()", "defined twice"),
            "baddefault": (head + "class M(Addon):\n    def settings(self):\n"
                                  "        return [entry('baddefault.x', 'int', 9, 'X', min_=0, max_=5)]\nADDON = M()",
                           "baddefault.x"),
            "badlog": (head + "class M(Addon):\n    def settings(self):\n"
                              "        return [entry('badlog.n', 'int', 1, 'N', min_=0, max_=5)]\n"
                              "    def log_sources(self):\n"
                              "        return [{'id': 'x', 'title': 'X', 'setting': 'badlog.n'}]\nADDON = M()",
                       "must name one of its own path settings"),
            "corelog": (head + "class M(Addon):\n    def log_sources(self):\n"
                               "        return [{'id': 'x', 'title': 'X', 'setting': 'node.log_path'}]\nADDON = M()",
                        "must name one of its own path settings"),
            "escape": (head + "class M(Addon):\n    def web(self):\n"
                              "        return {'scripts': ['../addon.py']}\nADDON = M()",
                       "isn't a plain path inside its web/ folder"),
            "missing": (head + "class M(Addon):\n    def web(self):\n"
                               "        return {'scripts': ['gone.js']}\nADDON = M()", "isn't in addons/missing/web/"),
            "wrongtype": (head + "class M(Addon):\n    def web(self):\n"
                                 "        return {'styles': ['ok.js']}\nADDON = M()", "styles are .css"),
            "fine": (head + "class M(Addon):\n    def web(self):\n"
                            "        return {'scripts': ['ok.js'], 'styles': ['ok.css']}\nADDON = M()", None),
        }
        recs, logs = self.load({k: v[0] for k, v in cases.items()})
        for name, (_, why) in cases.items():
            rec = recs[name]
            if why is None:
                self.assertIsNotNone(rec.addon, name)
                self.assertIsNone(rec.error, name)
                continue
            self.assertIsNone(rec.addon, name)
            self.assertTrue(rec.error.startswith("It didn't load: "), (name, rec.error))
            self.assertIn(why, rec.error, name)
            self.assertEqual((rec.settings, rec.logs, rec.scripts, rec.static), ([], [], [], {}), name)
        self.assertEqual(len(logs), len(cases) - 1, "one line each")
        self.assertNotIn("memstara_addon_exits", __import__("sys").modules)

    def test_no_folder(self):
        self.assertEqual(A.Addons(None).items, [])
        self.assertEqual(A.Addons("/nonexistent/addons").items, [])

    def test_checks_on_what_hooks_return(self):
        self.assertEqual(A._check_lamps([{"id": "a", "label": "x" * 40, "state": "purple", "value": "y" * 30}]),
                         [{"id": "a", "label": "X" * 24, "state": "off", "value": "Y" * 16, "help": "", "screen": None}])
        self.assertEqual([l["screen"] for l in A._check_lamps([{"id": "a", "screen": "chain"}, {"id": "b", "screen": "nope"}])],
                         ["chain", None], "only a screen there is")
        with self.assertRaises(A.AddonError):
            A._check_lamps([{"id": "A B"}])
        with self.assertRaises(A.AddonError):
            A._check_data({"x": float("nan")})
        with self.assertRaises(A.AddonError):
            A._check_data({"x": object()})
        with self.assertRaises(A.AddonError):
            A._check_data([1])
        self.assertEqual(A._check_alerts([{"id": "x", "hold": True, "level": "bad"}]), [{"id": "x", "hold": True}])
        self.assertIsNone(A._check_alerts(None))
        got = A._check_alerts([{"id": "x", "level": "fatal", "text": "t", "since": True, "ips": ["1.2.3.4"]}])
        self.assertEqual(got, [{"id": "x", "level": "warn", "title": None, "text": "t", "since": None,
                                "ips": ["1.2.3.4"]}])
        self.assertEqual(A._check_route([{"id": "r", "between": "node-sky", "link": {"label": "L"}, "row_only": 1}]),
                         [{"id": "r", "label": "R", "value": "", "level": "off", "link": {"label": "L", "level": "off"}}])
        self.assertEqual(A._check_ips(["1.1.1.1", " 2001:db8::1 ", "x", "1.1.1.1"]), ["1.1.1.1", "2001:db8::1"])
        self.assertEqual(A._check_notes({"save": "  ", "ban": "B", 3: "x", "stop_daemon": None}), {"ban": "B"})
        self.assertEqual(len(A._check_lamps([{"id": "l%d" % i} for i in range(100)])), A.MAX_ITEMS)


def in_memory(addon, name):
    """Addons holding one add-on object made in the test (no folder, no settings of its own)."""
    adds = A.Addons(None, log=lambda m: None)
    rec = A.Loaded(name, Path("."))
    rec.addon = addon
    adds.items.append(rec)
    return adds, rec


class HookTest(unittest.TestCase):
    def test_a_poll_that_hangs_doesnt_hold_up_the_poller(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class Slow(A.Addon):
            calls = 0

            def poll(self, settings, now, ctx):
                self.calls += 1
                if self.calls == 2:
                    release.wait(10)
                return {"n": self.calls}

            def lamps(self, data, ctx):
                return [{"id": "x", "label": "X", "state": "ok", "value": "fine"}]

        slow = Slow()
        adds, rec = in_memory(slow, "slow")
        on = {"slow.enabled": True}
        with mock.patch.object(A, "POLL_TIMEOUT_S", 0.3):
            self.assertEqual(adds.poll(on, 1, {}), {"slow": {"n": 1}})
            adds.lamps(on, {})
            t0 = time.monotonic()
            self.assertEqual(adds.poll(on, 2, {}), {}, "given up at the deadline")
            self.assertLess(time.monotonic() - t0, 2)
            self.assertEqual(rec.error, "poll() didn't finish in 0.3 s")
            self.assertEqual(adds.lamps(on, {})[0]["value"], "ADD-ON ERROR")
            self.assertEqual(adds.alerts(on, {}), {"slow": None}, "its alerts are held")
            self.assertEqual(adds.poll(on, 3, {}), {})
            self.assertEqual(slow.calls, 2, "no new poll() while the last one still runs")
            self.assertEqual(rec.error, "poll() still hasn't finished an earlier call (it took over 0.3 s)")
            release.set()
            rec.polling.join(5)
            self.assertEqual(adds.poll(on, 4, {}), {"slow": {"n": 3}}, "the late answer isn't used")
            self.assertIsNone(rec.error)

    def test_poll_gets_no_password_but_its_own(self):
        seen = []

        class Peek(A.Addon):
            def poll(self, settings, now, ctx):
                seen.append(settings)
                return {}

        adds, rec = in_memory(Peek(), "peek")
        rec.settings = [A.entry("peek.token", "password", "", "Token"),
                        A.entry("peek.url", "url", "http://127.0.0.1:1/", "URL")]
        other = A.Loaded("other", Path("."))
        other.settings = [A.entry("other.secret", "password", "", "Secret")]
        adds.items.append(other)
        values = {"peek.enabled": True, "source.rpc_password": "rpc-pw", "source.rpc_user": "admin",
                  "peek.token": "mine", "other.secret": "theirs", "peek.url": "http://127.0.0.1:1/"}
        adds.poll(values, 1, {})
        self.assertEqual(seen[0], dict(values, **{"source.rpc_password": "", "other.secret": ""}))
        self.assertEqual(values["source.rpc_password"], "rpc-pw", "the core's own values are untouched")

    def test_protected_hosts_kept_while_the_hook_fails(self):
        class Guard(A.Addon):
            fail = False

            def protected_hosts(self, data):
                if self.fail:
                    raise RuntimeError("broken")
                return ["33.44.7.7"]

        guard = Guard()
        adds, rec = in_memory(guard, "guard")
        on = {"guard.enabled": True}
        self.assertEqual(adds.protected(on), [("33.44.7.7", "guard")])
        guard.fail = True
        self.assertEqual(adds.protected(on), [("33.44.7.7", "guard")], "the last good list stays")
        self.assertEqual(rec.error, "protected_hosts() failed: RuntimeError: broken")
        adds.poll({"guard.enabled": False}, 1, {})
        self.assertEqual(adds.protected(on), [], "turned off and on again: nothing left from before")


class CoreWithoutAddonsTest(unittest.TestCase):
    def test_no_addons_folder(self):
        """--no-addons, or a copy of the dashboard with addons/ deleted."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        for addons_dir in (None, base / "addons-deleted"):
            s = Settings(base / ("s%d.json" % (addons_dir is None)), log=lambda m: None).load()
            app = App(s, port=0, web_dir=base / "web", state_dir=base / "state", log=lambda m: None,
                      addons_dir=addons_dir, host_reader=StubHost(), discover=StubDiscover())
            try:
                self.assertEqual(app.addons.items, [])
                self.assertEqual(app.addons.listing(s.values()), {"addons": []})
                self.assertLessEqual({k.split(".")[0] for k in s.values()}, set(SECTIONS))
            finally:
                app.httpd.server_close()


class FirstRunTest(unittest.TestCase):
    def settings(self, content=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "settings.json"
        if content is not None:
            path.write_text(json.dumps(content))
        return Settings(path, log=lambda m: None).load()

    def test_one_node_found(self):
        s = self.settings()
        logs = []
        node = found_node("http://127.0.0.1:28081", log_file="/var/log/monero/monerod.log")
        got = find_node_on_first_run(s, StubDiscover([node]), logs.append)
        self.assertEqual(got["pid"], 4242)
        self.assertEqual((s.get("source.rpc_url"), s.get("node.log_path")),
                         ("http://127.0.0.1:28081", "/var/log/monero/monerod.log"))
        self.assertEqual(logs, ["Found monerod (pid 4242): reading http://127.0.0.1:28081"])
        saved = json.loads(s.path.read_text())
        self.assertEqual(saved["source"]["rpc_url"], "http://127.0.0.1:28081")
        # the next start: the file has source.rpc_url now, so it isn't looked for again
        s2 = Settings(s.path, log=lambda m: None).load()
        d = StubDiscover([found_node("http://127.0.0.1:38081")])
        self.assertIsNone(find_node_on_first_run(s2, d, logs.append))
        self.assertEqual((d.calls, s2.get("source.rpc_url")), (0, "http://127.0.0.1:28081"))

    def test_only_on_a_first_run(self):
        d = StubDiscover([found_node("http://127.0.0.1:28081")])
        s = self.settings({"source": {"rpc_url": "http://127.0.0.1:18081"}})
        self.assertIsNone(find_node_on_first_run(s, d, lambda m: None))
        self.assertEqual(d.calls, 0)
        # a file with other settings but no node address yet: still looked for
        s = self.settings({"display": {"clock": "24h"}})
        self.assertIsNotNone(find_node_on_first_run(s, d, lambda m: None))
        self.assertEqual((s.get("source.rpc_url"), s.get("display.clock")), ("http://127.0.0.1:28081", "24h"))

    def test_not_exactly_one(self):
        defaults = ("http://127.0.0.1:18081", "~/.bitmonero/bitmonero.log")
        for found in ([], [found_node("http://127.0.0.1:1"), found_node("http://127.0.0.1:2", pid=7)],
                      [found_node("http://127.0.0.1:18089", restricted=True)],
                      [found_node(None, rpc_url=None)]):
            s = self.settings()
            self.assertIsNone(find_node_on_first_run(s, StubDiscover(found), lambda m: None), found)
            self.assertEqual((s.get("source.rpc_url"), s.get("node.log_path")), defaults)
        # a restricted port next to an unrestricted one: the unrestricted one
        s = self.settings()
        find_node_on_first_run(s, StubDiscover([found_node("http://127.0.0.1:18089", restricted=True, pid=8),
                                                found_node("http://127.0.0.1:18081")]), lambda m: None)
        self.assertEqual(s.get("source.rpc_url"), "http://127.0.0.1:18081")

    def test_odd_answers(self):
        def boom():
            raise OSError("ps failed")
        s = self.settings()
        self.assertIsNone(find_node_on_first_run(s, boom, lambda m: None))
        logs = []
        s = self.settings()
        find_node_on_first_run(s, StubDiscover([found_node("http://127.0.0.1:28081", log_file=None,
                                                           login_needed=True)]), logs.append)
        self.assertEqual(s.get("node.log_path"), "~/.bitmonero/bitmonero.log", "no log file: the default stays")
        self.assertEqual(logs, ["Found monerod (pid 4242): reading http://127.0.0.1:28081 (it needs the RPC login "
                                "in Settings)"])
        s = self.settings()
        self.assertIsNone(find_node_on_first_run(s, StubDiscover([found_node("ftp://x")]), lambda m: None))
        self.assertEqual(s.get("source.rpc_url"), "http://127.0.0.1:18081")


if __name__ == "__main__":
    unittest.main()
