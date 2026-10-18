"""The app window's parts that don't need a desktop: the command line, the missing-package
message, which addresses the window may show, and the remembered window size."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401
from memstara import __main__ as cli
from memstara import window


class CommandLineTest(unittest.TestCase):
    def test_flags(self):
        a = cli.parse_args([])
        self.assertFalse(a.background or a.install or a.uninstall or a.safe_graphics or a.debug)
        self.assertIsNone(a.autostart)
        a = cli.parse_args(["--background", "--safe-graphics", "--port", "18201"])
        self.assertTrue(a.background and a.safe_graphics)
        self.assertEqual(a.port, 18201)
        self.assertEqual(cli.parse_args(["--autostart", "on"]).autostart, "on")

    def test_no_browser_flags(self):
        for flags in (["--host", "0.0.0.0"], ["--allow-remote"], ["--autostart", "maybe"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parse_args(flags)
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            cli.parse_args(["--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("It opens in its own window", text)
        self.assertNotIn("browser", text)

    def test_install_flags_go_to_desktop(self):
        with mock.patch("memstara.desktop.cli", return_value=0) as d, \
                mock.patch("memstara.window.run") as run:
            self.assertEqual(cli.main(["--install"]), 0)
            self.assertEqual(cli.main(["--autostart", "off"]), 0)
        self.assertEqual(d.call_count, 2)
        run.assert_not_called()

    def test_window_options_passed_on(self):
        with mock.patch("memstara.window.run", return_value=0) as run:
            self.assertEqual(cli.main(["--background", "--safe-graphics"]), 0)
        kw = run.call_args.kwargs
        self.assertEqual((kw["background"], kw["safe_graphics"], kw["debug"]), (True, True, False))

    def test_bad_port(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--port", "0"]), 2)


class MissingPackagesTest(unittest.TestCase):
    def test_without_gi(self):
        apt = "sudo apt install -y python3-gi gir1.2-webkit-6.0"
        with mock.patch.dict(sys.modules, {"gi": None}), \
                mock.patch.object(window, "install_command", return_value=apt):   # import gi then fails
            self.assertEqual(window.missing_packages(), apt)
            said = []
            self.assertEqual(window.run(lambda key: None, log=said.append), 1)
        self.assertIn("its own window", said[0])
        self.assertIn(apt, said[0])

    def test_install_command_by_system(self):
        ic = window.install_command
        self.assertEqual(ic('NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n'), "sudo apt install -y python3-gi gir1.2-webkit-6.0")
        self.assertEqual(ic("ID=linuxmint\nID_LIKE=\"ubuntu debian\"\n"), "sudo apt install -y python3-gi gir1.2-webkit-6.0")
        self.assertEqual(ic("ID=debian\n"), "sudo apt install -y python3-gi gir1.2-webkit-6.0")
        self.assertEqual(ic("ID=fedora\n"), "sudo dnf install -y python3-gobject webkitgtk6.0")
        self.assertEqual(ic("ID=endeavouros\nID_LIKE=arch\n"), "sudo pacman -S --needed python-gobject webkitgtk-6.0")
        self.assertEqual(ic("ID=gentoo\n"), window.INSTALL_ANY)
        self.assertEqual(ic(""), window.INSTALL_ANY)

    def test_without_webkit(self):
        gi = SimpleNamespace(require_version=mock.Mock(side_effect=ValueError("Namespace WebKit not available")))
        with mock.patch.dict(sys.modules, {"gi": gi}):
            self.assertIn("gir1.2-webkit-6.0", window.missing_packages())


class WindowAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = window.WindowApp(lambda key: None, state_dir=Path(self.tmp.name), log=lambda m: None)
        self.c.dash = SimpleNamespace(port=18200)

    def test_only_its_own_screens(self):
        ours = ("http://127.0.0.1:18200/?key=abc", "http://127.0.0.1:18200/?key=abc#network",
                "http://127.0.0.1:18200/index.html")
        others = ("http://127.0.0.1:18201/", "https://127.0.0.1:18200/", "http://localhost:18200/",
                  "http://example.com/", "file:///etc/passwd", "about:blank", "data:text/html,hi",
                  "http://127.0.0.1:18200@evil.example/", "", "http://[::1]:18200/", "javascript:alert(1)")
        for uri in ours:
            self.assertTrue(self.c._ours(uri), uri)
        for uri in others:
            self.assertFalse(self.c._ours(uri), uri)

    def test_window_size_remembered(self):
        self.assertEqual(self.c._window_state(), (1440, 900, True), "first start: fill the screen")
        win = SimpleNamespace(get_default_size=lambda: (1280, 760), is_maximized=lambda: False)
        self.c._save_window_state(win)
        saved = Path(self.tmp.name) / "window.json"
        self.assertEqual(json.loads(saved.read_text()), {"width": 1280, "height": 760, "maximized": False})
        self.assertEqual(oct(saved.stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.c._window_state(), (1280, 760, False))

    def test_window_size_bad_file(self):
        saved = Path(self.tmp.name) / "window.json"
        for text in ("not json", "[]", '{"width": 10, "height": 10}', '{"width": "x"}', '{"width": 99999, "height": 800}'):
            saved.write_text(text)
            self.assertEqual(self.c._window_state(), (1440, 900, True), text)

    def fake_window(self, child=None):
        win = SimpleNamespace(shown=[child], get_default_size=lambda: (1280, 760), is_maximized=lambda: False)
        win.set_child = win.shown.append
        win.get_child = lambda: win.shown[-1]
        return win

    def fake_view(self):
        view = mock.Mock(spec=["reload", "reload_bypass_cache", "terminate_web_process"])
        return view

    def test_reload_after_the_crash_message(self):
        # the message takes the view's place; Reload (or TRY AGAIN) puts the same view back and
        # starts it over. Never a second view: on Wayland that one comes up blank
        view = self.fake_view()
        self.c.view = view
        self.c.win = win = self.fake_window(view)
        crashed = SimpleNamespace(value_nick="crashed")
        later = []
        with mock.patch.object(window, "GLib", SimpleNamespace(timeout_add_seconds=lambda s, f: later.append(f))), \
                mock.patch.object(self.c, "_message", return_value="the message") as message, \
                mock.patch.object(self.c, "_build_view") as build:
            for _ in range(3):
                self.c._drawing_stopped(view, crashed)
            self.assertEqual(win.shown, [view, "the message"])
            self.assertIs(self.c.view, view, "kept for TRY AGAIN")
            for f in later:   # the reloads the first two stops asked for
                f()
            self.assertEqual(view.reload.call_count, 0, "no reloading out of sight")
            self.c._reload()
            self.assertEqual(win.shown, [view, "the message", view])
            self.assertEqual(view.reload.call_count, 1)
            self.assertEqual(self.c._crashes, [])
            self.c._reload()   # with the view back, Reload is a plain reload
            self.assertEqual(view.reload_bypass_cache.call_count, 1)
            build.assert_not_called()
        self.assertIn("python3 -m memstara --safe-graphics", message.call_args.args[1])

    def test_closing_stops_the_app_and_its_drawing_process(self):
        view = self.fake_view()
        self.c.view = view
        self.c.win = win = self.fake_window(view)
        self.assertFalse(self.c._closing(win))
        view.terminate_web_process.assert_called_once_with()
        self.assertIsNone(self.c.win)
        self.assertIsNone(self.c.view)
        self.assertTrue((Path(self.tmp.name) / "window.json").exists(), "size saved")

    def test_closing_in_the_background_only_hides(self):
        # started at login: the same window and view come back on the next open
        self.c.background = True
        view = self.fake_view()
        self.c.view = view
        self.c.win = win = self.fake_window(view)
        self.assertFalse(self.c._closing(win), "GTK hides it (hide-on-close)")
        view.terminate_web_process.assert_not_called()
        self.assertIs(self.c.win, win)
        self.assertIs(self.c.view, view)

    def test_quit_stops_the_drawing_process_first(self):
        view = self.fake_view()
        self.c.view = view
        self.c.win = self.fake_window(view)
        self.c.dash = None
        self.c._shutdown(None)
        view.terminate_web_process.assert_called_once_with()
        self.assertIsNone(self.c.view)
        self.c._shutdown(None)   # nothing left to stop
        view.terminate_web_process.assert_called_once_with()

    def test_the_apps_own_stop_is_quiet(self):
        said = []
        self.c.log = said.append
        view = self.fake_view()
        self.c.view = view
        self.c.win = self.fake_window(view)
        self.c._drawing_stopped(view, SimpleNamespace(value_nick="terminated-by-api"))
        self.assertEqual(said, [])
        self.assertEqual(self.c._crashes, [])

    def test_right_click_menu(self):
        A = SimpleNamespace(COPY=1, CUT=2, PASTE=3, PASTE_AS_PLAIN_TEXT=4, DELETE=5, SELECT_ALL=6,
                            RELOAD=7, INSPECT_ELEMENT=8, OPEN_LINK=9)

        class Item:
            def __init__(self, action):
                self.action = action

            def is_separator(self):
                return self.action is None

            def get_stock_action(self):
                return self.action

        class Menu:
            def __init__(self, actions):
                self.items = [Item(a) for a in actions]

            def get_items(self):
                return list(self.items)

            def remove(self, item):
                self.items.remove(item)

            def get_n_items(self):
                return len(self.items)

        with mock.patch.object(window, "WebKit", SimpleNamespace(ContextMenuAction=A)):
            for debug, kept in ((False, [1, None, 6]), (True, [1, None, 6, 8])):
                self.c.debug = debug
                m = Menu([7, None, 1, 9, None, 6, 8, None])
                self.assertFalse(self.c._context_menu(None, m))
                self.assertEqual([i.action for i in m.items], kept, "debug %s" % debug)
            self.c.debug = False
            self.assertTrue(self.c._context_menu(None, Menu([9, None, 7])), "nothing left: no menu at all")


class GError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class FakeGApp:
    """Just enough of Gtk.Application for the start-up check and run()."""

    def __init__(self, remote=False, fail=None, **_):
        self.remote, self.fail, self.handlers = remote, fail, {}

    def connect(self, name, handler):
        self.handlers[name] = handler

    def register(self, cancellable):
        if self.fail:
            raise GError(self.fail)
        return True

    def get_is_remote(self):
        return self.remote

    def run(self, argv):
        return 0


class StartUpTest(unittest.TestCase):
    """Is a copy already running? Asked before anything starts (handle-local-options)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def check(self, app, **kw):
        said = []
        c = window.WindowApp(lambda key: None, state_dir=Path(self.tmp.name), log=said.append, **kw)
        with mock.patch.object(window, "GLib", SimpleNamespace(Error=GError)):
            return c._local_options(app, None), said

    def test_the_first_copy_carries_on(self):
        self.assertEqual(self.check(FakeGApp()), (-1, []))
        self.assertEqual(self.check(FakeGApp(), background=True), (-1, []))

    def test_a_second_copy_shows_the_first_ones_window(self):
        status, said = self.check(FakeGApp(remote=True))
        self.assertEqual(status, -1, "carries on: GApplication asks the running copy to show its window")
        self.assertEqual(said, ["Memstara is already running: showing its window."])

    def test_a_second_background_copy_just_ends(self):
        self.assertEqual(self.check(FakeGApp(remote=True), background=True),
                         (0, ["Memstara is already running."]))

    def test_startup_options_dont_reach_a_running_copy(self):
        status, said = self.check(FakeGApp(remote=True), startup_only=["--safe-graphics", "--port"])
        self.assertEqual(status, -1)
        self.assertEqual(said, ["Memstara is already running: showing its window. --safe-graphics "
                                "and --port only count when the app starts. To use them, quit the app "
                                "first (☰ → Quit), then start it again."])
        status, said = self.check(FakeGApp(remote=True), background=True, startup_only=["--port"])
        self.assertEqual(status, 0)
        self.assertIn("running. --port only counts when the app starts. To use it,", said[0])
        self.assertEqual(self.check(FakeGApp(), startup_only=["--port"]), (-1, []), "the first copy uses them")

    def test_a_copy_that_doesnt_answer(self):
        status, said = self.check(FakeGApp(fail="Timeout was reached"))
        self.assertEqual(status, 1, "never a second copy on the same port")
        self.assertIn("didn't answer (Timeout was reached)", said[0])

    def test_exit_status(self):
        gio = SimpleNamespace(ApplicationFlags=SimpleNamespace(DEFAULT_FLAGS=0, FLAGS_NONE=0))
        with mock.patch.object(window, "Gtk", SimpleNamespace(Application=FakeGApp)), \
                mock.patch.object(window, "Gio", gio):
            c = window.WindowApp(lambda key: None, state_dir=Path(self.tmp.name), log=lambda m: None)
            self.assertEqual(c.run(), 0)
            self.assertIn("handle-local-options", c.gapp.handlers)
            c.error = "The app's window port is in use."
            self.assertEqual(c.run(), 1, "a start that failed doesn't exit as if all went well")

    def test_startup_options_passed_on(self):
        with mock.patch("memstara.window.run", return_value=0) as run:
            cli.main(["--port", "18201", "--safe-graphics", "--background"])
        self.assertEqual(run.call_args.kwargs["startup_only"], ["--safe-graphics", "--port"])
        with mock.patch("memstara.window.run", return_value=0) as run:
            cli.main([])
        self.assertEqual(run.call_args.kwargs["startup_only"], [])


if __name__ == "__main__":
    unittest.main()
