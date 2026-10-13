"""The app's places on the desktop: the app grid (--install, --uninstall) and start at
login (--autostart on/off).

Both are plain desktop entry files (freedesktop.org Desktop Entry Specification) in your
own folders, ~/.local/share/applications and ~/.config/autostart, which GNOME, KDE and
the other Linux desktops all read. Nothing outside your home folder, no root needed.
Your settings and history are never touched here.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from .util import REPO_DIR, WEB_DIR, config_dir, state_dir

APP_ID = "org.memstara.Memstara"
NAME = "Memstara"
ICON = WEB_DIR / "img" / "app-icon.svg"

# characters that make an Exec argument need quotes (Desktop Entry Specification, "The Exec key")
_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def applications_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "applications"


def autostart_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "autostart"


def entry_path() -> Path:
    return applications_dir() / (APP_ID + ".desktop")


def autostart_path() -> Path:
    return autostart_dir() / (APP_ID + ".desktop")


def _plain(value: str) -> str:
    """A string value: backslashes doubled; control characters can't be written at all."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("a path with control characters can't go in a desktop entry")
    return value.replace("\\", "\\\\")


def exec_arg(arg: str) -> str:
    """One Exec argument, quoted when it needs to be (before the string escaping)."""
    if arg and not any(c in _RESERVED for c in arg):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


def exec_line(python: str, background: bool = False) -> str:
    args = [python, "-m", "memstara"] + (["--background"] if background else [])
    return _plain(" ".join(exec_arg(a) for a in args)).replace("%", "%%")


def entry_text(python: str | None = None, repo: Path | str | None = None, *,
               background: bool = False) -> str:
    """The desktop entry: the app grid's (background=False) or start at login's (True)."""
    python = python or sys.executable or "/usr/bin/python3"
    repo = str(repo or REPO_DIR)
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.5",
        "Name=" + NAME,
        "GenericName=Monero node dashboard",
        "Comment=Your Monero node's health, peers and chain, with monerod's console commands",
        "Exec=" + exec_line(python, background),
        "Path=" + _plain(repo),
        "Icon=" + _plain(str(Path(repo) / ICON.relative_to(REPO_DIR))),
        "Terminal=false",
        "Categories=Network;P2P;Monitor;",
        "Keywords=monero;xmr;node;monerod;p2p;dashboard;",
        "StartupNotify=" + ("false" if background else "true"),
        "StartupWMClass=" + APP_ID,
        "SingleMainWindow=true",
    ]
    if background:
        lines.append("X-GNOME-Autostart-enabled=true")
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    """Replace a file in one step (temp file, then rename), readable as desktop files are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _remove(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def install(python: str | None = None, repo: Path | str | None = None) -> Path:
    """Add the app to the app grid. Refreshes start at login too when it's on, so moving
    the folder and installing again fixes both (when a desktop tool turned it off, it
    stays off)."""
    path = entry_path()
    _write(path, entry_text(python, repo))
    if autostart_on():
        _write(autostart_path(), entry_text(python, repo, background=True))
    return path


def uninstall() -> list:
    """Take the app out of the app grid and out of start at login. Returns what was removed."""
    return [p for p in (entry_path(), autostart_path()) if _remove(p)]


def autostart_on() -> bool:
    """Start at login is on: the file is there and no desktop tool turned it off."""
    try:
        text = autostart_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    keys = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            keys[k.strip()] = v.strip().lower()
    return keys.get("Hidden") != "true" and keys.get("X-GNOME-Autostart-enabled") != "false"


def set_autostart(on: bool, python: str | None = None, repo: Path | str | None = None) -> None:
    if on:
        _write(autostart_path(), entry_text(python, repo, background=True))
    else:
        _remove(autostart_path())


def cli(args, out=print) -> int:
    """--install, --uninstall and --autostart from the command line."""
    if args.uninstall:
        gone = uninstall()
        out("Removed %s." % " and ".join(_home(p) for p in gone) if gone
            else "%s wasn't in your apps or in start at login." % NAME)
        out("Your settings and history are still in %s and %s." % (_home(config_dir()), _home(state_dir())))
        return 0
    if args.install:
        path = install()
        out("Added %s to your apps (%s). Open it from your desktop's app list, where you can "
            "also pin it to a dock or panel." % (NAME, _home(path)))
    if args.autostart == "on":
        set_autostart(True)
        out("%s will start in the background when you log in, so its history keeps filling in. "
            "Opening it from your apps shows the window." % NAME)
    elif args.autostart == "off":
        set_autostart(False)
        out("%s won't start when you log in any more." % NAME)
    from .window import missing_packages
    missing = missing_packages()
    if missing:
        out("Before it can open, the window needs PyGObject and WebKitGTK 6.0: %s" % missing)
    return 0


def _home(path) -> str:
    """A path with your home folder shown as ~, for messages."""
    p = os.path.expanduser(str(path))
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p == home or p.startswith(home + os.sep) else p


def shell_dir(path) -> str:
    """A folder as it goes in a command to paste: ~/... when that needs no quotes,
    else the whole path quoted."""
    short = _home(path)
    if short.startswith("~/") and shlex.quote(short[2:]) == short[2:]:
        return short
    return shlex.quote(os.path.expanduser(str(path)))
