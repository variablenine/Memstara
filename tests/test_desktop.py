"""The app grid entry and start at login: the files, their quoting, install and uninstall."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401
from memstara import desktop


def parse_exec(value: str) -> list:
    """An Exec value as a desktop reads it (Desktop Entry Specification): the string escapes
    first, then the quoting, then %% back to %."""
    out, i = [], 0
    unescaped = ""
    while i < len(value):   # string escapes
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            unescaped += {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}.get(value[i + 1], "\\" + value[i + 1])
            i += 2
        else:
            unescaped += c
            i += 1
    arg, quoted, have, i = "", False, False, 0
    while i < len(unescaped):
        c = unescaped[i]
        if quoted:
            if c == "\\" and i + 1 < len(unescaped) and unescaped[i + 1] in '"`$\\':
                arg += unescaped[i + 1]
                i += 2
                continue
            if c == '"':
                quoted = False
            else:
                arg += c
        elif c == '"':
            quoted, have = True, True
        elif c == " ":
            if have or arg:
                out.append(arg)
            arg, have = "", False
        else:
            arg += c
        i += 1
    if have or arg:
        out.append(arg)
    return [a.replace("%%", "%") for a in out]


def keys(text: str) -> dict:
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]", lines[0]
    return dict(line.split("=", 1) for line in lines[1:] if line)


class EntryTest(unittest.TestCase):
    def test_app_grid_entry(self):
        k = keys(desktop.entry_text("/usr/bin/python3", "/home/alice/memstara"))
        self.assertEqual(k["Type"], "Application")
        self.assertEqual(k["Name"], "Memstara")
        self.assertEqual(k["Exec"], "/usr/bin/python3 -m memstara")
        self.assertEqual(k["Path"], "/home/alice/memstara", "python3 -m memstara runs from the repo folder")
        self.assertEqual(k["Icon"], "/home/alice/memstara/web/img/app-icon.svg")
        self.assertEqual(k["Terminal"], "false")
        self.assertEqual(k["StartupWMClass"], desktop.APP_ID, "the window's name, so the dock shows this icon")
        self.assertEqual(k["StartupNotify"], "true")
        self.assertNotIn("X-GNOME-Autostart-enabled", k)

    def test_start_at_login_entry(self):
        k = keys(desktop.entry_text("/usr/bin/python3", "/home/alice/memstara", background=True))
        self.assertEqual(parse_exec(k["Exec"]), ["/usr/bin/python3", "-m", "memstara", "--background"])
        self.assertEqual(k["StartupNotify"], "false", "no window at login, so no busy cursor waiting for one")
        self.assertEqual(k["X-GNOME-Autostart-enabled"], "true")
        self.assertNotIn("NoDisplay", k, "it stays listed in Startup Applications")

    def test_awkward_paths_survive(self):
        for python in ("/opt/my python/bin/python3", '/odd"quote/python3', "/dollar$HOME/python3",
                       "/back\\slash/python3", "/100%/python3", "/semi;colon/python3", "/tick`x`/py",
                       "/tilde~/py", "/hash#/py", "/star*/py", "/paren(1)/py", "/quote'/py"):
            k = keys(desktop.entry_text(python, "/r"))
            self.assertEqual(parse_exec(k["Exec"]), [python, "-m", "memstara"], python)
        for repo in ("/home/alice/my apps/memstara", "/home/x/back\\slash", "/home/x/semi;colon"):
            k = keys(desktop.entry_text("/usr/bin/python3", repo))
            self.assertEqual(k["Path"].replace("\\\\", "\\"), repo)
        self.assertEqual(desktop.exec_arg("/usr/bin/python3"), "/usr/bin/python3", "plain paths stay unquoted")
        self.assertEqual(desktop.exec_arg("/a b/py"), '"/a b/py"')

    def test_control_characters_refused(self):
        for bad in ("/home/x\n/py", "/home/\x07/py"):
            with self.assertRaises(ValueError):
                desktop.entry_text(bad, "/r")
            with self.assertRaises(ValueError):
                desktop.entry_text("/usr/bin/python3", bad)

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate isn't installed")
    def test_files_pass_the_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            for background in (False, True):
                path = Path(tmp) / (desktop.APP_ID + ".desktop")
                path.write_text(desktop.entry_text("/opt/my python/python3", "/home/alice/memstara",
                                                   background=background))
                r = subprocess.run(["desktop-file-validate", str(path)], capture_output=True, text=True)
                self.assertEqual((r.returncode, r.stdout + r.stderr), (0, ""))

    def test_glib_reads_it_the_same(self):
        try:
            import gi
            gi.require_version("GLib", "2.0")
            from gi.repository import GLib
        except (ImportError, ValueError):
            self.skipTest("no GLib for Python here")
        kf = GLib.KeyFile()
        python = '/opt/my "odd" $python\\/python3'
        text = desktop.entry_text(python, "/home/x/my apps")
        kf.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
        exec_ = kf.get_string("Desktop Entry", "Exec").replace("%%", "%")
        self.assertEqual(GLib.shell_parse_argv(exec_)[1], [python, "-m", "memstara"])
        self.assertEqual(kf.get_string("Desktop Entry", "Path"), "/home/x/my apps")


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(base / "data"),
                                            "XDG_CONFIG_HOME": str(base / "config")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.app_file = base / "data" / "applications" / (desktop.APP_ID + ".desktop")
        self.login_file = base / "config" / "autostart" / (desktop.APP_ID + ".desktop")

    def cli(self, **flags):
        args = SimpleNamespace(install=False, uninstall=False, autostart=None)
        args.__dict__.update(flags)
        said = []
        with mock.patch("memstara.window.missing_packages", return_value=None):
            self.assertEqual(desktop.cli(args, out=said.append), 0)
        return "\n".join(said)

    def test_install_and_uninstall(self):
        said = self.cli(install=True)
        self.assertTrue(self.app_file.is_file())
        self.assertEqual(oct(self.app_file.stat().st_mode & 0o777), "0o644")
        self.assertIn("Added Memstara to your apps", said)
        self.assertFalse(self.login_file.exists(), "install doesn't turn on start at login")
        self.assertFalse(list(self.app_file.parent.glob(".*.tmp")), "no temp files left")
        said = self.cli(uninstall=True)
        self.assertFalse(self.app_file.exists())
        self.assertIn("Removed", said)
        self.assertIn("settings and history are still in", said)
        self.assertIn("wasn't in your apps", self.cli(uninstall=True))

    def test_autostart_on_and_off(self):
        self.assertFalse(desktop.autostart_on())
        said = self.cli(autostart="on")
        self.assertTrue(desktop.autostart_on())
        self.assertIn("--background", keys(self.login_file.read_text())["Exec"])
        self.assertIn("start in the background when you log in", said)
        self.cli(autostart="off")
        self.assertFalse(self.login_file.exists())
        self.assertFalse(desktop.autostart_on())

    def test_autostart_turned_off_by_the_desktop(self):
        desktop.set_autostart(True)
        text = self.login_file.read_text()
        for off in ("X-GNOME-Autostart-enabled=false", "Hidden=true"):
            self.login_file.write_text(text.replace("X-GNOME-Autostart-enabled=true", off))
            self.assertFalse(desktop.autostart_on(), off)

    def test_install_refreshes_start_at_login(self):
        desktop.set_autostart(True, python="/old/python3", repo="/old/place")
        desktop.install(python="/usr/bin/python3", repo="/new/place")
        k = keys(self.login_file.read_text())
        self.assertEqual(k["Path"], "/new/place")
        self.assertEqual(parse_exec(k["Exec"]), ["/usr/bin/python3", "-m", "memstara", "--background"])
        self.assertEqual(desktop.uninstall(), [self.app_file, self.login_file], "uninstall removes both")

    def test_install_leaves_start_at_login_off(self):
        # turned off in a desktop tool (Startup Applications and the like): installing
        # again mustn't turn it back on
        desktop.set_autostart(True, python="/old/python3", repo="/old/place")
        text = self.login_file.read_text()
        for off in ("X-GNOME-Autostart-enabled=false", "Hidden=true"):
            turned_off = text.replace("X-GNOME-Autostart-enabled=true", off)
            self.login_file.write_text(turned_off)
            desktop.install(python="/usr/bin/python3", repo="/new/place")
            self.assertEqual(self.login_file.read_text(), turned_off, off)
            self.assertFalse(desktop.autostart_on(), off)

    def test_folders_for_commands(self):
        home = os.path.expanduser("~")
        self.assertEqual(desktop.shell_dir(os.path.join(home, "memstara")), "~/memstara")
        self.assertEqual(desktop.shell_dir(os.path.join(home, "my apps", "memstara")), "'%s/my apps/memstara'" % home)
        self.assertEqual(desktop.shell_dir("/opt/memstara"), "/opt/memstara")
        self.assertEqual(desktop.shell_dir("/opt/it's here"), "'/opt/it'\"'\"'s here'")

    def test_missing_packages_mentioned(self):
        args = SimpleNamespace(install=True, uninstall=False, autostart=None)
        said = []
        with mock.patch("memstara.window.missing_packages", return_value="sudo apt install -y python3-gi gir1.2-webkit-6.0"):
            desktop.cli(args, out=said.append)
        self.assertIn("sudo apt install -y python3-gi gir1.2-webkit-6.0", said[-1])


if __name__ == "__main__":
    unittest.main()
