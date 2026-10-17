"""python3 -m memstara: open Memstara in its own window (from the repo folder; besides
Python it needs only PyGObject and WebKitGTK 6.0, see window.install_command)."""

from __future__ import annotations

import argparse
import sys

from . import VERSION
from .settings import Settings, SettingsError


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="python3 -m memstara",
        description="Memstara: a desktop app for your Monero node. It opens in its own "
                    "window; starting it again shows the window of the copy that's running.")
    p.add_argument("--background", action="store_true",
                   help="start without a window and keep collecting history (start at login uses "
                        "this); opening the app then shows the window")
    p.add_argument("--install", action="store_true", help="add the app to your apps, then exit")
    p.add_argument("--uninstall", action="store_true",
                   help="take the app out of your apps and out of start at login, then exit "
                        "(settings and history stay)")
    p.add_argument("--autostart", choices=("on", "off"),
                   help="start in the background when you log in (on) or not (off), then exit")
    p.add_argument("--safe-graphics", action="store_true",
                   help="draw without the graphics card's help, for a window that stays blank or flickers")
    p.add_argument("--port", type=int, metavar="N",
                   help="the window's local port for this run (default: setting server.port, 18200)")
    p.add_argument("--config", metavar="PATH",
                   help="settings file (default ~/.config/memstara/settings.json)")
    p.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)   # the screens' console in the terminal
    p.add_argument("--version", action="version", version="memstara " + VERSION)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.port is not None and not 1 <= args.port <= 65535:
        print("--port must be 1 to 65535", file=sys.stderr)
        return 2
    if args.install or args.uninstall or args.autostart:
        from . import desktop
        return desktop.cli(args)

    from . import window

    def make_app(key):
        """Runs once the window can open, in the first copy only (a second start just
        shows the first copy's window)."""
        from .app import App, find_node_on_first_run   # imported late so --help stays instant
        from .poller import _hostport
        try:
            settings = Settings(args.config).load()
        except OSError as e:
            raise window.StartError("The settings file couldn't be saved (%s). Check that your "
                                    "home folder isn't full or read-only." % (e.strerror or type(e).__name__)) from None
        try:
            app = App(settings, port=args.port, key=key)
        except OSError as e:
            port = args.port if args.port is not None else settings.get("server.port")
            raise window.StartError(
                "The app's window port, %s on this computer, is in use (%s). If another copy of "
                "Memstara is running in a terminal, stop it there with Ctrl+C and start the app "
                "again. If another program uses that port, start the app with another one: "
                "python3 -m memstara --port 18201" % (port, e.strerror or type(e).__name__)) from None
        find_node_on_first_run(settings, log=lambda msg: print(msg, flush=True))
        print("Memstara %s: reading %s" % (VERSION, _hostport(settings.get("source.rpc_url"))),
              flush=True)
        if not app.static:
            print("Note: web/ has no screens yet; the window will be empty.", file=sys.stderr)
        return app

    # options that only count when the app starts (a copy already running keeps its own)
    given = (("--safe-graphics", args.safe_graphics), ("--port", args.port is not None),
             ("--config", args.config), ("--debug", args.debug))
    return window.run(make_app, background=args.background, safe_graphics=args.safe_graphics,
                      debug=args.debug, startup_only=[flag for flag, on in given if on])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SettingsError as e:
        print("Settings problem: %s" % e.errors, file=sys.stderr)
        sys.exit(2)
