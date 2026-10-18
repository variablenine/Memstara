"""The app window: a GTK 4 window that draws the screens with WebKitGTK.

The screens are the HTML, CSS and JavaScript in web/, drawn by WebKitGTK, the web engine
GNOME's own apps use (Help, for one), inside the app's own window: no browser, no address
bar or tabs, and it only ever shows the app's own screens. Their readings come from the
app's local server (server.py) on 127.0.0.1, which answers only this window: every start
makes a new window key, and only the window is given it.

Needs PyGObject and WebKitGTK 6.0 (GTK 4 comes with them): python3-gi and gir1.2-webkit-6.0
on Debian and Ubuntu. gi is imported here only, when the window opens, so the tests and
--help don't need it.

One copy runs at a time: starting the app again shows the running copy's window.
--background starts it with no window (start at login uses it), so the history keeps
filling in; opening the app then shows the window, and closing the window leaves it
running. Quit in the window's menu stops it. No keyboard shortcuts of the app's own (a stray key press does nothing).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import desktop
from .util import REPO_DIR, atomic_write, cache_dir, ensure_private_dir, state_dir as default_state_dir

APP_ID = desktop.APP_ID
TITLE = desktop.NAME
# the command that installs what the window needs, by the ID and ID_LIKE in /etc/os-release
INSTALLS = (
    (("debian", "ubuntu"), "sudo apt install -y python3-gi gir1.2-webkit-6.0"),
    (("fedora",), "sudo dnf install -y python3-gobject webkitgtk6.0"),
    (("arch",), "sudo pacman -S --needed python-gobject webkitgtk-6.0"),
)
INSTALL_ANY = "PyGObject (for GTK 4) and WebKitGTK 6.0 from your system's packages"
DEFAULT_SIZE = (1440, 900)
CRASH_WINDOW_S = 120   # the drawing process stopping more than twice in this long: stop retrying

Gtk = Gdk = Gio = GLib = WebKit = None   # set by _load()

# the web view's switches: only what the screens use is on (no WebGL, sound, video,
# camera, pop-ups, full screen, file access or developer tools). Unknown names are skipped,
# so an older or newer WebKitGTK that lacks one still starts.
WEB_SETTINGS = {
    "enable-javascript": True,
    "enable-developer-extras": False,
    "enable-write-console-messages-to-stdout": False,
    "javascript-can-open-windows-automatically": False,
    "javascript-can-access-clipboard": False,
    "allow-modal-dialogs": False,
    "enable-webgl": False,
    "enable-webaudio": False,
    "enable-media": False,
    "enable-media-stream": False,
    "enable-mediasource": False,
    "enable-encrypted-media": False,
    "enable-webrtc": False,
    "enable-fullscreen": False,
    "enable-site-specific-quirks": False,
    "enable-back-forward-navigation-gestures": False,
    "enable-page-cache": False,
    "allow-file-access-from-file-urls": False,
    "allow-universal-access-from-file-urls": False,
    "allow-top-navigation-to-data-urls": False,
}

# right-click menu: editing only (copy an address, paste into a form)
MENU_KEEP = ("COPY", "CUT", "PASTE", "PASTE_AS_PLAIN_TEXT", "DELETE", "SELECT_ALL")

CSS = """
window.memstara { background-color: #000000; color: #e6d6b4; }
window.memstara headerbar {
  background: #000000; background-image: none; color: #8aa3ad;
  border: none; border-bottom: 1px solid #20343c; box-shadow: none;
  min-height: 34px; padding: 0 6px;
}
window.memstara headerbar:backdrop { background: #000000; }
window.memstara headerbar button {
  color: #ffb840; background: transparent; background-image: none; border: none;
  box-shadow: none; min-height: 24px; min-width: 24px; padding: 2px; margin: 0 2px;
  border-radius: 4px;
}
window.memstara headerbar button:hover { background: rgba(255, 184, 64, 0.14); }
window.memstara headerbar button:active,
window.memstara headerbar button:checked { background: rgba(255, 184, 64, 0.26); }
window.memstara headerbar:backdrop button { color: #8a6a2e; }
popover.menu > contents {
  background: #050505; color: #e6d6b4; border: 1px solid #20343c; border-radius: 6px;
  box-shadow: 0 0 18px rgba(255, 184, 64, 0.10);
}
popover.menu > arrow { background: #050505; border: 1px solid #20343c; }
popover.menu modelbutton { color: #e6d6b4; border-radius: 4px; }
popover.menu modelbutton:hover, popover.menu modelbutton:focus { background: rgba(255, 184, 64, 0.16); color: #ffd590; }
popover.menu separator { background: #20343c; }
popover.menu check { color: #ffb840; }
.memstara-message { padding: 40px 48px; }
.memstara-message .headline { color: #ffb840; font-weight: bold; font-size: 16pt; letter-spacing: 2px; }
.memstara-message .body { color: #e6d6b4; font-size: 11.5pt; }
.memstara-message button {
  color: #ffb840; background: transparent; background-image: none; box-shadow: none;
  border: 1px solid #8a6a2e; border-radius: 3px; padding: 6px 18px; letter-spacing: 2px;
}
.memstara-message button:hover { background: rgba(255, 184, 64, 0.14); border-color: #ffb840; }
"""


class StartError(Exception):
    """The app couldn't start (its port is taken, for one): the message says what to do."""


def install_command(os_release: str | None = None) -> str:
    """How to install what the window needs on this Linux: a command where the system is
    known, else the packages' names in words."""
    if os_release is None:
        try:
            with open("/etc/os-release", encoding="utf-8", errors="replace") as fh:
                os_release = fh.read(65536)
        except OSError:
            os_release = ""
    ids = set()
    for line in os_release.splitlines():
        k, _, v = line.partition("=")
        if k.strip() in ("ID", "ID_LIKE"):
            ids.update(v.strip().strip("\"'").lower().split())
    for names, cmd in INSTALLS:
        if ids.intersection(names):
            return cmd
    return INSTALL_ANY


def missing_packages() -> str | None:
    """None when the window can open, else how to install what it needs (install_command)."""
    try:
        import gi
    except ImportError:
        return install_command()
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("WebKit", "6.0")
    except ValueError:
        return install_command()
    return None


def _load() -> None:
    global Gtk, Gdk, Gio, GLib, WebKit
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import GLib as _GLib
    # before GTK starts: the desktop matches the window to the app's icon by this name
    _GLib.set_prgname(APP_ID)
    _GLib.set_application_name(TITLE)
    from gi.repository import Gdk as _Gdk, Gio as _Gio, Gtk as _Gtk, WebKit as _WebKit
    Gtk, Gdk, Gio, GLib, WebKit = _Gtk, _Gdk, _Gio, _GLib, _WebKit


def run(make_app, *, background: bool = False, safe_graphics: bool = False, debug: bool = False,
        startup_only=(), log=None) -> int:
    """Open the window (or show the running copy's). make_app(key) builds the App with that
    window key, or raises StartError. startup_only: the options given that only count when
    the app starts (--port and the like), to say so if a copy is already running."""
    log = log or (lambda msg: print(msg, flush=True))
    missing = missing_packages()
    if missing:
        log("Memstara opens in its own window, which needs PyGObject and WebKitGTK 6.0. "
            "Install them with:\n\n    %s\n\nthen start it again." % missing)
        return 1
    if safe_graphics:
        # read when the drawing process starts: no GPU buffers, no hardware acceleration
        os.environ["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"
    _load()
    if not Gtk.init_check():
        log("There's no desktop here to open the window on. Start Memstara from your "
            "desktop: from your apps, or in a terminal on the desktop (not over SSH).")
        return 1
    return WindowApp(make_app, background=background, safe_graphics=safe_graphics,
                   debug=debug, startup_only=startup_only, log=log).run()


class WindowApp:
    """The GTK application: one copy, its window, its menu."""

    def __init__(self, make_app, *, background=False, safe_graphics=False, debug=False,
                 startup_only=(), state_dir=None, log=None):
        self.make_app = make_app
        self.background = background
        self.safe_graphics = safe_graphics
        self.debug = debug
        self.startup_only = list(startup_only)
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.log = log or (lambda msg: print(msg, flush=True))
        self.dash = None        # the App: poller, local server
        self.error = None       # why it couldn't start
        self.win = None         # the main window, while it's open
        self.error_win = None
        self.view = None
        self.session = None
        self.gapp = None
        self._quiet_start = background   # --background: the first activation opens no window
        self._crashes = []
        self._autostart = None

    # ---- start and stop
    def run(self) -> int:
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        app = self.gapp = Gtk.Application(application_id=APP_ID, flags=flags)
        app.connect("handle-local-options", self._local_options)
        app.connect("startup", self._startup)
        app.connect("activate", self._activate)
        app.connect("shutdown", self._shutdown)
        status = app.run([sys.argv[0] if sys.argv else "memstara"])
        return 1 if self.error is not None else status

    def _local_options(self, app, options):
        """First thing in run(): is a copy already running? Registering asks the session
        bus (with no bus there's nothing to ask, and this copy runs on its own). Returning
        0 or more ends here with that exit status; -1 carries on to the window."""
        try:
            app.register(None)
        except GLib.Error as e:
            # a copy holds the app's name but didn't answer: it's busy starting or stopping
            self.log("%s is already running but didn't answer (%s). Try again in a moment."
                     % (TITLE, e.message))
            return 1
        if not app.get_is_remote():
            return -1
        note = ""
        if self.startup_only:   # the running copy keeps the options it started with
            one = len(self.startup_only) == 1
            note = (" %s only %s when the app starts. To use %s, quit the app first (☰ → Quit), "
                    "then start it again." % (" and ".join(self.startup_only),
                                              "counts" if one else "count", "it" if one else "them"))
        if self.background:
            self.log("%s is already running.%s" % (TITLE, note))
            return 0
        self.log("%s is already running: showing its window.%s" % (TITLE, note))
        return -1   # the copy that's running shows its window

    def _startup(self, app):
        self._style()
        self._actions(app)
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)
        try:
            self.dash = self.make_app(secrets.token_urlsafe(32))
            self.dash.start()
        except StartError as e:
            self.error = str(e)
        except Exception as e:   # anything else: say so instead of vanishing
            self.error = "Something went wrong while starting (%s: %s)." % (type(e).__name__, e)
        if self.error is not None:
            self.dash = None
            self.log(self.error)
            return
        if self.background:
            app.hold()   # keep running with no window

    def _activate(self, app):
        if self.error is not None:
            self._show_error(app)
            return
        if self._quiet_start:
            self._quiet_start = False
            return
        if self.win is None:
            self.win = self._build_window(app)
        self.win.present()

    def _shutdown(self, app):
        if self.win is not None:
            self._save_window_state(self.win)
        self._stop_drawing()
        if self.dash is not None:
            self.log("Stopping...")
            try:
                self.dash.stop()   # saves the history
            except Exception as e:
                self.log("Stopping: %s" % type(e).__name__)
            self.dash = None

    def _on_signal(self, *_):
        self.gapp.quit()
        return False

    # ---- look and menu
    def _style(self):
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(CSS)          # GTK 4.12 and newer
        except AttributeError:
            provider.load_from_data(CSS, -1)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(display, provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        settings = Gtk.Settings.get_default()
        if settings is not None:   # dark menus, whatever the desktop's theme
            try:
                if hasattr(settings.props, "gtk_interface_color_scheme"):   # GTK 4.20 and newer
                    settings.props.gtk_interface_color_scheme = Gtk.InterfaceColorScheme.DARK
                else:
                    settings.props.gtk_application_prefer_dark_theme = True
            except (AttributeError, TypeError):
                pass

    def _actions(self, app):
        """The menu's items. No keyboard shortcuts on purpose, so a stray key press does nothing."""
        reload = Gio.SimpleAction.new("reload", None)
        reload.connect("activate", lambda *_: self._reload())
        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *_: app.quit())
        self._autostart = Gio.SimpleAction.new_stateful(
            "autostart", None, GLib.Variant.new_boolean(desktop.autostart_on()))
        self._autostart.connect("change-state", self._set_autostart)
        for action in (reload, quit_, self._autostart):
            app.add_action(action)

    def _menu(self):
        menu = Gio.Menu()
        for items in ((("Reload the screens", "app.reload"),),
                      (("Start when I log in", "app.autostart"),),
                      (("Quit", "app.quit"),)):
            section = Gio.Menu()
            for label, action in items:
                section.append(label, action)
            menu.append_section(None, section)
        return menu

    def _set_autostart(self, action, value):
        on = value.get_boolean()
        try:
            desktop.set_autostart(on)
        except (OSError, ValueError) as e:
            self.log("Couldn't change start at login: %s" % e)
            return
        action.set_state(value)
        self.log("Start at login: %s" % ("on" if on else "off"))

    # ---- the window
    def _build_window(self, app):
        win = Gtk.ApplicationWindow(application=app, title=TITLE)
        win.add_css_class("memstara")
        width, height, maximized = self._window_state()
        win.set_default_size(width, height)
        if maximized:
            win.maximize()
        header = Gtk.HeaderBar()
        header.set_title_widget(Gtk.Label())   # the screens show the name themselves
        button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self._menu())
        button.set_tooltip_text("Menu")
        header.pack_end(button)
        win.set_titlebar(header)
        if self._autostart is not None:   # it may have been changed outside the app
            self._autostart.set_state(GLib.Variant.new_boolean(desktop.autostart_on()))
        win.set_child(self._build_view())
        win.set_hide_on_close(self.background)
        win.connect("close-request", self._closing)
        return win

    def _closing(self, win):
        self._save_window_state(win)
        if self.background:
            # hidden, not closed: the same window and view come back on the next open. A
            # second view in one app can draw at 0x0, blank (seen with WebKitGTK on Wayland)
            return False
        self._stop_drawing()
        self.win = None
        return False   # close it, which also stops the app

    def _stop_drawing(self):
        """End the drawing process before its window goes. Left to exit on its own, it can
        crash on the way out (seen with some WebKitGTK versions drawing in software: heap
        errors in its exit-time cleanup). Nothing in it needs saving; the history lives in
        this process."""
        view, self.view = self.view, None
        if view is not None:
            try:
                view.terminate_web_process()
            except Exception:
                pass

    def _window_state(self):
        try:
            d = json.loads((self.state_dir / "window.json").read_text(encoding="utf-8"))
            w, h = int(d.get("width", 0)), int(d.get("height", 0))
            if 400 <= w <= 20000 and 300 <= h <= 20000:
                return w, h, bool(d.get("maximized", False))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return DEFAULT_SIZE[0], DEFAULT_SIZE[1], True   # first start: fill the screen

    def _save_window_state(self, win):
        try:
            w, h = win.get_default_size()
            data = {"width": int(w) or DEFAULT_SIZE[0], "height": int(h) or DEFAULT_SIZE[1],
                    "maximized": bool(win.is_maximized())}
            atomic_write(self.state_dir / "window.json", json.dumps(data).encode())
        except (OSError, TypeError, ValueError):
            pass

    # ---- the screens
    def _network_session(self):
        if self.session is None:
            # no web cache: WebKit's disk cache would keep the page under its address, window
            # key and all (set before the first view, so it applies to every view)
            WebKit.WebContext.get_default().set_cache_model(WebKit.CacheModel.DOCUMENT_VIEWER)
            data = self.state_dir / "window-data"   # remembers the open screen
            cache = cache_dir().parent / self.state_dir.name / "window-cache"
            for folder in (data, cache):
                ensure_private_dir(folder)
            self.session = WebKit.NetworkSession.new(str(data), str(cache))
            # straight to 127.0.0.1, whatever proxy the desktop is set to use
            self.session.set_proxy_settings(WebKit.NetworkProxyMode.NO_PROXY, None)
            self.session.connect("download-started", lambda _s, download: download.cancel())
        return self.session

    def _build_view(self):
        view = WebKit.WebView(network_session=self._network_session())
        settings = view.get_settings()
        switches = dict(WEB_SETTINGS)
        if self.debug:   # the screens' console messages in the terminal, and the inspector
            switches["enable-developer-extras"] = True
            switches["enable-write-console-messages-to-stdout"] = True
        for name, value in switches.items():
            if hasattr(settings.props, name.replace("-", "_")):
                settings.set_property(name, value)
        if self.safe_graphics:
            settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.NEVER)
        black = Gdk.RGBA()
        black.parse("#000000")
        view.set_background_color(black)   # no white flash while the screens load
        view.set_hexpand(True)
        view.set_vexpand(True)
        view.connect("decide-policy", self._decide)
        view.connect("permission-request", self._deny)
        view.connect("context-menu", self._context_menu)
        view.connect("web-process-terminated", self._drawing_stopped)
        view.connect("create", lambda *_: None)   # no new windows
        view.load_uri(self.dash.window_url)
        self.view = view
        return view

    def _ours(self, uri: str) -> bool:
        try:
            p = urlsplit(uri or "")
            return p.scheme == "http" and p.hostname == "127.0.0.1" and p.port == self.dash.port
        except ValueError:
            return False

    def _decide(self, view, decision, kind):
        """Only the app's own screens: any other address, a new window or a download is refused."""
        T = WebKit.PolicyDecisionType
        if kind == T.RESPONSE:
            if not decision.is_mime_type_supported():
                decision.ignore()   # something to save rather than show: never download
                return True
            return False
        uri = ""
        try:
            uri = decision.get_navigation_action().get_request().get_uri()
        except (AttributeError, TypeError):
            pass
        if kind == T.NAVIGATION_ACTION and self.dash is not None and self._ours(uri):
            decision.use()
        else:
            decision.ignore()
        return True

    def _deny(self, view, request):
        request.deny()   # no camera, microphone, location, notifications or the like
        return True

    def _context_menu(self, view, menu, *_):
        names = MENU_KEEP + (("INSPECT_ELEMENT",) if self.debug else ())
        keep = {getattr(WebKit.ContextMenuAction, n) for n in names if hasattr(WebKit.ContextMenuAction, n)}
        for item in list(menu.get_items()):
            if not item.is_separator() and item.get_stock_action() not in keep:
                menu.remove(item)
        last_was_separator = True
        for item in list(menu.get_items()):
            if item.is_separator():
                if last_was_separator:
                    menu.remove(item)
                last_was_separator = True
            else:
                last_was_separator = False
        items = menu.get_items()
        if items and items[-1].is_separator():
            menu.remove(items[-1])
        return menu.get_n_items() == 0   # True: no menu at all

    def _drawing_stopped(self, view, reason):
        nick = getattr(reason, "value_nick", str(reason))
        if nick == "terminated-by-api":   # _stop_drawing: the window is going
            return
        self.log("The window's drawing process stopped (%s)." % nick)
        if self.win is None:
            return
        now = time.monotonic()
        self._crashes = [t for t in self._crashes if now - t < CRASH_WINDOW_S] + [now]
        if len(self._crashes) <= 2:
            GLib.timeout_add_seconds(1, self._reload_after_crash)
            return
        # the message takes the view's place; TRY AGAIN (or Reload) puts the same view back
        self.win.set_child(self._message(
            "THE SCREENS STOPPED",
            "The part of the app that draws the screens stopped three times in a row. Your "
            "node isn't affected, and the history is still being kept.\n\n"
            "If it keeps happening, quit and start the app from a terminal with "
            "--safe-graphics, which draws without the graphics card's help:\n\n"
            "    cd %s && python3 -m memstara --safe-graphics" % desktop.shell_dir(REPO_DIR),
            [("TRY AGAIN", self._view_back)]))

    def _view_shown(self) -> bool:
        return self.win is not None and self.view is not None and self.win.get_child() is self.view

    def _reload_after_crash(self):
        if self._view_shown():
            self.view.reload()
        return False

    def _view_back(self, *_):
        """TRY AGAIN: the same view goes back in place of the message and starts over. Never
        a second view, which would come up blank on Wayland (see _closing)."""
        self._crashes = []
        if self.win is not None and self.view is not None:
            self.win.set_child(self.view)
            self.view.reload()

    def _reload(self):
        if self._view_shown():
            self.view.reload_bypass_cache()
        else:
            self._view_back()   # the crash message is up: same as TRY AGAIN

    # ---- messages
    def _message(self, headline: str, body: str, buttons):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.add_css_class("memstara-message")
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        title = Gtk.Label(label=headline, xalign=0)
        title.add_css_class("headline")
        text = Gtk.Label(label=body, xalign=0, wrap=True, selectable=True, max_width_chars=78)
        text.add_css_class("body")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for label, handler in buttons:
            b = Gtk.Button(label=label)
            b.connect("clicked", handler)
            row.append(b)
        for child in (title, text, row):
            box.append(child)
        first = row.get_first_child()
        if first is not None:   # focus on the button, not the text (focus would select all of it)
            box.connect("map", lambda *_: first.grab_focus())
        return box

    def _show_error(self, app):
        if self.error_win is None:
            win = Gtk.ApplicationWindow(application=app, title=TITLE)
            win.add_css_class("memstara")
            win.set_default_size(760, 360)
            header = Gtk.HeaderBar()
            header.set_title_widget(Gtk.Label())
            win.set_titlebar(header)
            win.set_child(self._message("MEMSTARA COULDN'T START", self.error,
                                        [("CLOSE", lambda *_: win.close())]))
            win.connect("close-request", self._error_closed)
            self.error_win = win
        self.error_win.present()

    def _error_closed(self, win):
        self.error_win = None
        return False   # closing it ends the app: nothing else is running
