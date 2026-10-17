"""GET /api/node-log: the last lines of monerod's log. Read-only.

It reads node.log_path: at most the last 512 KB of a regular file, never written, never a symlink out of the user's home folder, and never
blocking on a pipe or device. A file inside the dashboard's own settings or state folder, or
one whose name has no ".log", isn't read either, unless it is the log file host.discover
found for the node (util.log_path_refusal).
"""

from __future__ import annotations

import os
import re
import stat

from .settings import DEFAULTS, node_log_path
from .util import log_path_refusal

MAX_BYTES = 512 * 1024
MAX_LINES = 1000
DEFAULT_LINES = 200
MAX_LINE_CHARS = 2000
SOURCES = {"monerod": "node.log_path"}
NAMES = {"monerod": "monerod's log"}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class LogError(Exception):
    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status = status
        self.error = error


def _home() -> str:
    return os.path.realpath(os.path.expanduser("~"))


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:
        return False


def resolve(configured: str) -> str:
    """The configured path -> the real file to open, or LogError."""
    path = os.path.abspath(os.path.expanduser(configured or ""))
    real = os.path.realpath(path)
    if real != path and not _inside(real, _home()):
        raise LogError(403, "%s is a link to somewhere outside your home folder, so it isn't read." % path)
    return real


def tail(configured: str, lines: int = DEFAULT_LINES, name: str = "the log", node_log: str | None = None) -> dict:
    """`node_log`: the log file host.discover found for the node, which may always be read."""
    path = os.path.abspath(os.path.expanduser(configured or ""))
    real = resolve(configured)
    why = log_path_refusal(real, node_log)
    if why == "own":
        raise LogError(403, "%s is in the dashboard's own settings or state folder, so it isn't read." % path)
    if why == "name":
        raise LogError(403, "%s isn't a log file (its name has no .log), so it isn't read." % path)
    try:
        st = os.stat(real)
    except FileNotFoundError:
        raise LogError(404, "%s isn't at %s." % (name, path))
    except OSError:
        raise LogError(403, "%s at %s can't be read." % (name, path))
    if not stat.S_ISREG(st.st_mode):
        raise LogError(403, "%s isn't a regular file, so it isn't read." % path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(real, flags)
    except OSError:
        raise LogError(403, "%s at %s can't be read." % (name, path))
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):   # swapped for something else since the check
            raise LogError(403, "%s isn't a regular file, so it isn't read." % path)
        size = fst.st_size
        start = max(0, size - MAX_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        data = b""
        while len(data) < size - start:
            chunk = os.read(fd, min(65536, size - start - len(data)))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    text = data.decode("utf-8", errors="replace")
    rows = text.splitlines()
    if start > 0 and rows:
        rows = rows[1:]            # the first line was cut in the middle
    rows = [_CONTROL.sub("", _ANSI.sub("", r))[:MAX_LINE_CHARS] for r in rows]
    return {"ok": True, "path": path, "size": size, "mtime": int(fst.st_mtime),
            "lines": rows[-lines:] if lines else []}


def read(settings, source: str, lines_text: str | None, node_log: str | None = None,
         remote: bool = False) -> tuple[int, dict]:
    """The API answer: (status, body). `node_log` is the log file host.discover found for the
    node (Poller.node_log_file()), or None. `remote`: the node runs on another computer
    (Poller.node_elsewhere()), so its log is read only from a path set by hand."""
    if source not in SOURCES:
        return 400, {"ok": False, "error": "source must be monerod"}
    n = DEFAULT_LINES
    if lines_text not in (None, ""):
        if not re.fullmatch(r"\d{1,4}", lines_text or "") or not 1 <= int(lines_text) <= MAX_LINES:
            return 400, {"ok": False, "error": "lines must be 1 to %d" % MAX_LINES}
        n = int(lines_text)
    setting, name = SOURCES[source], NAMES[source]
    path = settings.get(setting)
    if remote and path == DEFAULTS["node.log_path"]:
        return 404, {"ok": False, "error": "The node runs on another computer, so its log isn't on this one. "
                     "To read it here, copy it over or mount its folder, and set monerod's log in Settings."}
    path = node_log_path({"node.log_path": path}, node_log)
    try:
        out = tail(path, n, name, node_log)
    except LogError as e:
        return e.status, {"ok": False, "error": e.error}
    out["source"] = source
    return 200, out
