"""Small shared helpers: XDG paths, atomic file writes, plain-language formatting."""

from __future__ import annotations

import os
import stat
import tempfile
import time
from pathlib import Path

APP_DIR_NAME = "memstara"
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
WEB_DIR = REPO_DIR / "web"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / APP_DIR_NAME


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / APP_DIR_NAME


def log_path_refusal(real: str, node_log: str | None = None):
    """Why a log reader won't read `real` (a path with its links resolved): "own" when it is
    inside the dashboard's own settings or state folder, "name" when its file name has no
    ".log"; None when it may be read. The log file host.discover found for the node
    (`node_log`) may always be read."""
    if node_log and real == os.path.realpath(os.path.expanduser(node_log)):
        return None
    for folder in (config_dir(), state_dir()):
        f = os.path.realpath(folder)
        try:
            if os.path.commonpath([real, f]) == f:
                return "own"
        except ValueError:
            pass
    if ".log" not in os.path.basename(real):
        return "name"
    return None


def ensure_private_dir(path: Path) -> None:
    """Create a folder (and parents) with mode 0700 for the last part. A folder that is
    already there is set to 0700 too when it's the user's own, unless it's a shared one:
    the home folder, a filesystem root or a sticky folder such as /tmp."""
    path = Path(path)
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
    try:
        st = os.stat(path)
        real = os.path.realpath(path)
        if (stat.S_IMODE(st.st_mode) != 0o700 and st.st_uid == os.getuid() and not st.st_mode & stat.S_ISVTX
                and real != os.path.realpath(os.path.expanduser("~")) and os.path.dirname(real) != real):
            os.chmod(path, 0o700)
    except (OSError, AttributeError):   # AttributeError: no os.getuid (not a Unix)
        pass


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write a file atomically: temp file in the same folder, fsync, rename. Mode 0600."""
    path = Path(path)
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clock_style(values: dict) -> str:
    """The clock_text style the settings ask for: "12h" or "24h", with "-utc" while privacy
    mode is on (the window shows UTC then, so a screenshot doesn't give the time zone away)."""
    return values.get("display.clock", "12h") + ("-utc" if values.get("display.privacy") is True else "")


def clock_text(t: float, clock: str = "12h") -> str:
    """Local time of day: 14:05 (24h) or 2:05 PM (12h); a "-utc" style ("12h-utc") gives
    UTC instead, marked so ("2:05 PM UTC" for 14:05 UTC)."""
    utc = clock.endswith("-utc")
    lt = time.gmtime(t) if utc else time.localtime(t)
    if clock.startswith("24h"):
        s = time.strftime("%H:%M", lt)
    else:
        hour = lt.tm_hour % 12 or 12
        s = "%d:%02d %s" % (hour, lt.tm_min, "AM" if lt.tm_hour < 12 else "PM")
    return s + " UTC" if utc else s


def duration_text(seconds: float) -> str:
    """Short plain duration: 45 s, 12 min, 3 h 5 min, 2 days 4 h."""
    s = max(0, int(seconds))
    if s < 60:
        return "%d s" % s
    m = s // 60
    if m < 60:
        return "%d min" % m
    h, m = divmod(m, 60)
    if h < 48:
        return "%d h %d min" % (h, m) if m else "%d h" % h
    d, h = divmod(h, 24)
    return "%d days %d h" % (d, h) if h else "%d days" % d


def as_int(v, default=None):
    """An int from a JSON value, or default. Bools are not numbers here."""
    if isinstance(v, bool) or v is None:
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            try:
                f = float(v.strip())
                return int(f) if f == f else default
            except ValueError:
                return default
    return default


def as_float(v, default=None):
    if isinstance(v, bool) or v is None:
        return default
    if isinstance(v, (int, float)):
        f = float(v)
        return f if f == f else default
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return default
    return default


def as_bool(v, default=None):
    if isinstance(v, bool):
        return v
    if v in ("true", "True", 1):
        return True
    if v in ("false", "False", 0):
        return False
    return default


def as_str(v, default=None, limit: int = 300):
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return default
    s = str(v)
    s = "".join(ch for ch in s if ch >= " " or ch == "\t")
    return s[:limit]
