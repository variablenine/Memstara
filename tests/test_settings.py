"""Settings: types, ranges, cross-field rules, passwords, unknown keys, the file."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import helpers  # noqa: F401
from memstara.settings import (DEFAULTS, FORMAT, PANEL_IDS, SCHEMA, Settings, SettingsError, migrate)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "cfg" / "memstara"
        self.path = self.dir / "settings.json"
        self.s = Settings(self.path, log=lambda m: None).load()

    def tearDown(self):
        self.tmp.cleanup()

    def errors(self, values):
        with self.assertRaises(SettingsError) as cm:
            self.s.update(values)
        return cm.exception.errors

    def test_created_with_defaults_private(self):
        self.assertTrue(self.path.exists())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        on_disk = json.loads(self.path.read_text())
        self.assertEqual(on_disk["source"]["rpc_url"], "http://127.0.0.1:18081")
        self.assertEqual(self.s.values(), DEFAULTS)
        self.assertEqual(self.s.file_keys, set(), "a first run: nothing was in the file yet")
        again = Settings(self.path, log=lambda m: None).load()
        self.assertIn("source.rpc_url", again.file_keys)

    def test_an_existing_folder_made_private_on_save(self):
        os.chmod(self.dir, 0o755)    # made earlier with mkdir -p, say
        self.s.update({"source.rpc_password": "pw"})
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        home = Path(self.tmp.name) / "home"
        home.mkdir(mode=0o751)
        with unittest.mock.patch.dict(os.environ, {"HOME": str(home)}):
            Settings(home / "settings.json", log=lambda m: None).load().update({"source.rpc_password": "pw"})
        self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o751, "the home folder is left as it is")
        self.assertEqual(stat.S_IMODE(os.stat(home / "settings.json").st_mode), 0o600)

    def test_defaults_match_contract(self):
        d = DEFAULTS
        self.assertEqual(d["source.rpc_url"], "http://127.0.0.1:18081", "monerod's own default port")
        self.assertEqual(d["display.accent"], "amber")
        self.assertEqual((d["local.enabled"], d["local.every_s"], d["display.temp_unit"]), (True, 1, "auto"))
        self.assertEqual(d["source.refresh_s"], 1, "the node is read every second")
        by_key = {e["key"]: e for e in SCHEMA}
        self.assertEqual([c["value"] for c in by_key["local.every_s"]["choices"]], [1, 5, 10, 30])
        self.assertEqual([c["value"] for c in by_key["source.refresh_s"]["choices"]], [1, 2, 5, 10, 30])
        self.assertEqual([(c["value"], c["label"]) for c in by_key["display.temp_unit"]["choices"]],
                         [("auto", "Auto (by language)"), ("f", "°F"), ("c", "°C")])
        self.assertEqual([c["value"] for c in next(e for e in SCHEMA if e["key"] == "display.accent")["choices"]],
                         ["amber", "phosphor", "ice", "bone", "violet", "monero"])
        self.assertEqual([k[7:] for k in d if k.startswith("panels.")], PANEL_IDS)
        self.assertIn("route", PANEL_IDS)
        self.assertEqual(PANEL_IDS, ["lamps", "chain", "peers", "route", "controls", "traffic", "history", "mempool",
                                     "events", "computer", "system", "flood"])
        self.assertEqual(by_key["panels.computer"]["label"], "Computer")
        self.assertEqual(by_key["panels.controls"]["label"], "Controls")
        self.assertEqual(by_key["controls.enabled"]["label"], "Allow change commands")
        self.assertEqual(by_key["controls.enabled"]["help"],
                         "Ban, unban and the other commands that change something. Reading always works.")
        self.assertIs(d["controls.enabled"], True)
        self.assertEqual(d["node.log_path"], "~/.bitmonero/bitmonero.log")
        self.assertEqual((by_key["node.log_path"]["type"], by_key["node.log_path"]["section"]), ("path", "node"))
        for k, v in {"thresholds.low_peers": 8, "thresholds.no_block_warn_min": 30, "thresholds.no_block_bad_min": 60,
                     "thresholds.disk_warn_gb": 20, "thresholds.disk_bad_gb": 5, "thresholds.reorg_warn": 3,
                     "thresholds.reorg_bad": 10, "thresholds.out16_warn": 5, "thresholds.in24_warn": 4,
                     "grace.node_down_min": 5, "grace.peers_min": 10, "source.refresh_s": 1}.items():
            self.assertEqual(d[k], v, k)
        keys = {"key", "section", "label", "help", "type", "min", "max", "step", "choices", "default", "unit"}
        for e in SCHEMA:
            self.assertEqual(set(e), keys, e["key"])

    def test_types(self):
        e = self.errors({"thresholds.low_peers": "abc", "display.glow": "yes", "source.refresh_s": 7,
                         "display.accent": "plaid", "source.timeout_s": True, "chain.blocks_shown": 1.5,
                         "source.rpc_user": 5, "node.log_path": ["x"], "local.every_s": 7})
        self.assertEqual(set(e), {"thresholds.low_peers", "display.glow", "source.refresh_s", "display.accent",
                                  "source.timeout_s", "chain.blocks_shown", "source.rpc_user", "node.log_path",
                                  "local.every_s"})
        self.assertEqual(self.s.get("thresholds.low_peers"), 8, "nothing changes when any key is bad")

    def test_lenient_numbers(self):
        self.s.update({"source.refresh_s": "10", "thresholds.low_peers": 12.0, "display.text_scale": 115})
        self.assertEqual(self.s.get("source.refresh_s"), 10)
        self.assertEqual(self.s.get("thresholds.low_peers"), 12)
        self.assertIsInstance(self.s.get("thresholds.low_peers"), int)

    def test_ranges(self):
        e = self.errors({"source.timeout_s": 1, "chain.blocks_shown": 201, "server.port": 80,
                         "grace.syncing_min": -1})
        self.assertEqual(e["source.timeout_s"], "must be at least 2")
        self.assertEqual(e["chain.blocks_shown"], "must be at most 200")
        self.assertIn("server.port", e)
        self.assertIn("grace.syncing_min", e)

    def test_urls(self):
        for bad in ("ftp://127.0.0.1:18091", "127.0.0.1:18091", "http://user:pw@127.0.0.1:18091",
                    "http://127.0.0.1:99999", "http://", "http://bad host:1"):
            self.assertIn("source.rpc_url", self.errors({"source.rpc_url": bad}), bad)
        self.s.update({"source.rpc_url": "https://node.local:18089/"})
        self.assertEqual(self.s.get("source.rpc_url"), "https://node.local:18089/")

    def test_cross_field_rules(self):
        e = self.errors({"thresholds.no_block_bad_min": 30})
        self.assertEqual(e, {"thresholds.no_block_bad_min": "must be longer than the heads-up time (30 min)"})
        e = self.errors({"thresholds.no_block_warn_min": 90})
        self.assertEqual(list(e), ["thresholds.no_block_warn_min"])
        e = self.errors({"thresholds.disk_bad_gb": 20})
        self.assertIn("thresholds.disk_bad_gb", e)
        e = self.errors({"thresholds.reorg_bad": 3})
        self.assertIn("thresholds.reorg_bad", e)
        # both changed together, consistently: fine
        self.s.update({"thresholds.no_block_warn_min": 90, "thresholds.no_block_bad_min": 120})
        self.assertEqual(self.s.get("thresholds.no_block_bad_min"), 120)

    def test_unknown_keys_rejected(self):
        e = self.errors({"source.mode": "demo", "thresholds.nope": 1, "low_peers": 3})
        self.assertEqual(set(e), {"source.mode", "thresholds.nope", "low_peers"})
        self.assertTrue(all(v == "unknown setting" for v in e.values()))

    def test_password_never_returned(self):
        self.s.update({"source.rpc_user": "monero", "source.rpc_password": "hunter2-long"})
        pub = self.s.public()
        self.assertEqual(pub["values"]["source.rpc_password"], "")
        self.assertTrue(pub["rpc_password_set"])
        self.assertNotIn("hunter2-long", json.dumps(pub))
        self.assertEqual(self.s.get("source.rpc_password"), "hunter2-long")
        self.s.update({"source.rpc_password": ""})            # what the screen sends back: keep it
        self.assertEqual(self.s.get("source.rpc_password"), "hunter2-long")
        self.s.update({"source.rpc_password": None})          # null clears it
        self.assertFalse(self.s.public()["rpc_password_set"])

    def test_saved_atomically_and_reloaded(self):
        changed = self.s.update({"thresholds.low_peers": 10, "display.clock": "24h"})
        self.assertEqual(sorted(changed), ["display.clock", "thresholds.low_peers"])
        self.assertEqual(sorted(os.listdir(self.dir)), ["settings.json"], "no temp files left behind")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        again = Settings(self.path, log=lambda m: None).load()
        self.assertEqual(again.get("thresholds.low_peers"), 10)
        self.assertEqual(again.get("display.clock"), "24h")

    def test_reset_section(self):
        self.s.update({"thresholds.low_peers": 10, "grace.peers_min": 3})
        self.s.reset_section("thresholds")
        self.assertEqual(self.s.get("thresholds.low_peers"), 8)
        self.assertEqual(self.s.get("grace.peers_min"), 3)
        with self.assertRaises(SettingsError):
            self.s.reset_section("demo")

    def test_listeners(self):
        seen = []
        self.s.add_listener(seen.append)
        self.s.update({"thresholds.low_peers": 9})
        self.s.update({"thresholds.low_peers": 9})   # no change, no call
        self.assertEqual(seen, [["thresholds.low_peers"]])

    def test_load_tolerates_old_and_bad_values(self):
        self.path.write_text(json.dumps({
            "source": {"mode": "demo", "rpc_url": "http://127.0.0.1:18081", "refresh_s": 3},
            "demo": {"speed": 20}, "thresholds": {"no_block_warn_min": 70, "no_block_bad_min": 60}}))
        s = Settings(self.path, log=lambda m: None).load()
        self.assertEqual(s.get("source.rpc_url"), "http://127.0.0.1:18081")
        self.assertEqual(s.get("source.refresh_s"), 1)               # bad value -> default
        self.assertEqual(s.get("thresholds.no_block_warn_min"), 30)  # conflicting pair -> defaults
        self.assertEqual(s.get("thresholds.no_block_bad_min"), 60)
        self.assertTrue(s.load_warnings)

    def test_old_file_moves_to_every_second_once(self):
        # a file from before reads every second (no "format"), with the old defaults it was
        # first saved with and two sections nobody knows any more
        self.path.write_text(json.dumps({
            "source": {"rpc_url": "http://127.0.0.1:28081", "refresh_s": 5, "timeout_s": 8},
            "local": {"enabled": True, "every_s": 10},
            "someaddon": {"enabled": True, "data_file": "~/x"}, "gone": {"a": 1}}))
        s = Settings(self.path, log=lambda m: None).load()
        self.assertEqual((s.get("source.refresh_s"), s.get("local.every_s")), (1, 1))
        self.assertEqual(s.get("source.rpc_url"), "http://127.0.0.1:28081")
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["format"], FORMAT)
        self.assertEqual((raw["source"]["refresh_s"], raw["local"]["every_s"]), (1, 1))
        self.assertEqual(raw["someaddon"], {"enabled": True, "data_file": "~/x"},
                         "moving the file to the new format leaves unknown sections as they were")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        # once moved, a 5 picked later stays 5
        s.update({"source.refresh_s": 5})
        self.assertEqual(Settings(self.path, log=lambda m: None).load().get("source.refresh_s"), 5)

    def test_old_file_keeps_values_someone_picked(self):
        self.path.write_text(json.dumps({"source": {"refresh_s": 10}, "local": {"every_s": 30}}))
        s = Settings(self.path, log=lambda m: None).load()
        self.assertEqual((s.get("source.refresh_s"), s.get("local.every_s")), (10, 30))
        self.assertEqual(json.loads(self.path.read_text())["format"], FORMAT)

    def test_migrate_leaves_a_current_file_alone(self):
        self.assertIsNone(migrate({"format": FORMAT, "source": {"refresh_s": 5}}))
        self.assertEqual(migrate({"format": True, "source": {"refresh_s": 5}})["source"]["refresh_s"], 1,
                         "a format that isn't a number counts as the first one")
        self.assertEqual(migrate({"source": {"refresh_s": 5.0}})["source"]["refresh_s"], 5.0,
                         "only the exact old default moves")

    def test_old_file_not_written_when_saving_is_off(self):
        self.path.write_text(json.dumps({"source": {"refresh_s": 5}}))
        before = self.path.read_text()
        s = Settings(self.path, save=False, log=lambda m: None).load()
        self.assertEqual(s.get("source.refresh_s"), 1)
        self.assertEqual(self.path.read_text(), before)

    def test_load_garbage_file(self):
        self.path.write_text("{not json")
        s = Settings(self.path, log=lambda m: None).load()
        self.assertEqual(s.values(), DEFAULTS)
        self.assertTrue((self.dir / "settings.json.bad").exists())

    def test_controls_section(self):
        self.s.update({"controls.enabled": False, "node.log_path": "/var/log/monero/monero.log"})
        self.assertIn("controls.enabled", self.errors({"controls.enabled": "no"}))
        self.s.reset_section("controls")
        self.assertIs(self.s.get("controls.enabled"), True)
        self.assertEqual(self.s.get("node.log_path"), "/var/log/monero/monero.log", "other sections kept")
        self.s.reset_section("node")
        self.assertEqual(self.s.get("node.log_path"), "~/.bitmonero/bitmonero.log")

    def test_unknown_keys_dropped(self):
        """A key the app doesn't know (here server.allowed_hosts) is dropped: the file still loads, without it."""
        self.path.write_text(json.dumps({"server": {"port": 18201, "allowed_hosts": "node.lan"}}))
        s = Settings(self.path, log=lambda m: None).load()
        self.assertEqual(s.get("server.port"), 18201)
        self.assertNotIn("server.allowed_hosts", s.values())
        self.assertIn("server.allowed_hosts", self.errors({"server.allowed_hosts": "node.lan"}))

    def test_unknown_sections_dropped_at_the_next_save(self):
        self.path.write_text(json.dumps({"demo": {"mode": "b"}}))
        s = Settings(self.path, log=lambda m: None).load()
        self.assertNotIn("demo.mode", s.values())
        self.assertIn("demo", json.loads(self.path.read_text()), "loading alone never rewrites the file")
        s.update({"thresholds.low_peers": 9})
        self.assertNotIn("demo", json.loads(self.path.read_text()))


if __name__ == "__main__":
    unittest.main()
