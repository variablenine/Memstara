"""Settings: the schema, defaults, validation and the settings file.

The file is ~/.config/memstara/settings.json (or $XDG_CONFIG_HOME),
stored as nested sections ({"format": 2, "source": {"rpc_url": ...}, ...}), mode
0600 in a 0700 folder. The API speaks flat dotted keys ("thresholds.low_peers").
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .util import atomic_write, config_dir, ensure_private_dir

# the core panels, in grid order (A to K on the screens)
PANEL_IDS = ["lamps", "chain", "peers", "route", "bans", "traffic", "history", "mempool", "events",
             "computer", "system", "flood"]

SECTIONS = ["source", "local", "thresholds", "grace", "display", "panels", "chain", "node", "server"]


def entry(key, type_, default, label, help_="", min_=None, max_=None, step=None, choices=None, unit=None):
    """One schema entry. `choices` is a list of (value, label) pairs."""
    if choices is not None:
        choices = [dict(c) if isinstance(c, dict) else {"value": c[0], "label": c[1]} for c in choices]
    if step is None and type_ in ("int",):
        step = 1
    return {"key": key, "section": key.split(".", 1)[0], "label": label, "help": help_,
            "type": type_, "min": min_, "max": max_, "step": step, "choices": choices,
            "default": default, "unit": unit}


_e = entry


SCHEMA = [
    # ---- where the data comes from
    _e("source.rpc_url", "url", "http://127.0.0.1:18081", "Node RPC address",
       "monerod's RPC address. Use the unrestricted (admin) port, for example "
       "http://127.0.0.1:18081 (monerod's default); a restricted port hides peers, bans and traffic. "
       "FIND MY NODE reads it from the running monerod. For a node on another computer, an SSH tunnel "
       "to it keeps its answers private (see the README)."),
    _e("source.rpc_user", "text", "", "RPC user",
       "Only if monerod runs with --rpc-login."),
    _e("source.rpc_password", "password", "", "RPC password",
       "Only if monerod runs with --rpc-login. Never shown again once saved."),
    _e("source.refresh_s", "choice", 1, "Read the node every",
       "How often the dashboard asks the node. Each reading reaches the screen the moment it's in.",
       choices=[(1, "1 second"), (2, "2 seconds"), (5, "5 seconds"), (10, "10 seconds"), (30, "30 seconds")],
       unit="s"),
    _e("source.timeout_s", "int", 8, "Give up waiting after",
       "How long to wait for one answer from the node.", 2, 30, unit="s"),
    # ---- reads from this computer (see host.py)
    _e("local.enabled", "bool", True, "Read this computer",
       "monerod's log, its process, peers' round trips, CPU temperature, power, the network link "
       "the node is bound to and the Tor proxy. Read-only."),
    _e("local.every_s", "choice", 1, "Read the computer every", "",
       choices=[(1, "1 second"), (5, "5 seconds"), (10, "10 seconds"), (30, "30 seconds")], unit="s"),
    # ---- alert thresholds
    _e("thresholds.out_wanted", "int", 0, "Outgoing connections wanted",
       "0 uses the node's own: its --out-peers flag, else monerod's default of 12. If you change "
       "it with the out_peers command, put the new number here.", 0, 1000, unit="peers"),
    _e("thresholds.low_peers", "int", 8, "Few outgoing below",
       "Heads-up when fewer outgoing connections than this.", 0, 1000, unit="peers"),
    _e("thresholds.no_block_warn_min", "int", 30, "No new block: heads-up after",
       "", 5, 1440, unit="min"),
    _e("thresholds.no_block_bad_min", "int", 60, "No new block: problem after",
       "Must be longer than the heads-up time.", 5, 1440, unit="min"),
    _e("thresholds.disk_warn_gb", "int", 20, "Disk space: heads-up below",
       "Free space on the blockchain disk (1 GB = 1,000,000,000 bytes).", 0, 100000, unit="GB"),
    _e("thresholds.disk_bad_gb", "int", 5, "Disk space: problem below",
       "Must be less than the heads-up level.", 0, 100000, unit="GB"),
    _e("thresholds.reorg_warn", "int", 3, "Blocks replaced: heads-up at",
       "1 or 2 replaced at once happen normally.", 1, 1000, unit="blocks"),
    _e("thresholds.reorg_bad", "int", 10, "Blocks replaced: problem at",
       "10 is Monero's lock time for received coins. Must be more than the heads-up level.",
       1, 1000, unit="blocks"),
    _e("thresholds.out16_warn", "int", 5, "Eclipse watch: outgoing from one network",
       "Outgoing connections from one x.y network.", 2, 100, unit="peers"),
    _e("thresholds.in24_warn", "int", 4, "Eclipse watch: incoming from one network",
       "Incoming connections from one x.y.z network.", 2, 100, unit="peers"),
    _e("thresholds.restart_warn", "int", 3, "Restarting a lot: restarts in an hour",
       "", 2, 100, unit="restarts"),
    _e("thresholds.flood_warn", "int", 11, "Black marble: heads-up below",
       "Heads-up when a surge of new outputs would leave a new transaction's 16-member ring working like "
       "fewer members than this, if one sender made them all.", 2, 16, unit="of 16"),
    _e("thresholds.flood_bad", "int", 8, "Black marble: problem below",
       "Must be lower than the heads-up level.", 1, 15, unit="of 16"),
    _e("thresholds.tor_sending", "choice", "auto", "Tor sending",
       "Whether monerod sends your transactions over Tor (--tx-proxy tor,...). Auto: on when the running "
       "monerod has --tx-proxy tor,... (read from its flags); when it can't be found, once Tor connections "
       "are seen since it started.",
       choices=[("auto", "Auto"), ("on", "On"), ("off", "Off")]),
    # ---- how long a problem must last
    _e("grace.node_down_min", "int", 5, "Node not answering for", "", 0, 120, unit="min"),
    _e("grace.no_peers_min", "int", 5, "No outgoing connections for", "", 0, 120, unit="min"),
    _e("grace.peers_min", "int", 10, "Few outgoing connections for", "", 0, 120, unit="min"),
    _e("grace.syncing_min", "int", 20, "Still syncing for", "", 0, 240, unit="min"),
    _e("grace.spread_min", "int", 10, "Eclipse watch cluster for", "", 0, 120, unit="min"),
    _e("grace.tor_min", "int", 10, "No Tor connections for", "", 0, 120, unit="min"),
    _e("grace.flood_min", "int", 30, "Black marble watch below its limit for", "", 0, 1440, unit="min"),
    # ---- display
    _e("display.layout", "choice", "tabs", "Screens",
       "Tabs show one screen at a time; one page stacks every screen.",
       choices=[("tabs", "Tabs"), ("page", "All on one page")]),
    _e("display.accent", "choice", "amber", "Accent colour", "",
       choices=[("amber", "Amber"), ("phosphor", "Phosphor"), ("ice", "Ice"), ("bone", "Bone"),
                ("violet", "Violet"), ("monero", "Monero orange")]),
    _e("display.clock", "choice", "12h", "Clock", "",
       choices=[("12h", "12-hour"), ("24h", "24-hour")]),
    _e("display.scanlines", "bool", True, "Scanlines", ""),
    _e("display.glow", "bool", True, "Glow", "A soft glow on frames and lit parts that slowly breathes."),
    _e("display.motion", "choice", "full", "Motion",
       "Your desktop's reduce-animation setting always wins.",
       choices=[("full", "Full"), ("reduced", "Reduced"), ("off", "Off")]),
    _e("display.boot", "bool", True, "Start-up check", "Short start-up check sequence."),
    _e("display.density", "choice", "comfortable", "Density", "",
       choices=[("comfortable", "Comfortable"), ("compact", "Compact")]),
    _e("display.text_scale", "choice", 100, "Text size", "",
       choices=[(90, "90%"), (100, "100%"), (115, "115%"), (130, "130%")], unit="%"),
    _e("display.keys", "bool", False, "Keyboard shortcuts",
       "Off by default so a stray key press can't trigger them. 1 to 8 switch screens, S settings, C commands, "
       "A acknowledge, T lamp test."),
    _e("display.temp_unit", "choice", "auto", "Temperature",
       "Auto: °F when your computer's language is US English, °C otherwise.",
       choices=[("auto", "Auto (by language)"), ("f", "°F"), ("c", "°C")]),
    _e("display.privacy", "bool", False, "Privacy mode",
       "Hides addresses, ports, names, folders and transaction ids behind a glitch, so the screens can "
       "be shown to other people. The PRIVACY switch at the top turns it on and off."),
]
# kept by "reset to defaults": privacy mode only goes off through its own switch, which asks first
KEEP_ON_RESET = ("display.privacy",)
_PANEL_LABELS = {
    "lamps": "Lamp wall", "chain": "Chain", "peers": "Peer scope", "route": "Route", "bans": "Ban list",
    "traffic": "Traffic", "history": "History", "mempool": "Mempool", "events": "Log",
    "computer": "Computer", "system": "System", "flood": "Black marble watch",
}
SCHEMA += [_e("panels." + p, "bool", True, _PANEL_LABELS[p], "Show this panel.") for p in PANEL_IDS]
SCHEMA += [
    _e("chain.blocks_shown", "int", 60, "Blocks on the tape", "", 20, 200, unit="blocks"),
    _e("node.log_path", "path", "~/.bitmonero/bitmonero.log", "monerod's log",
       "monerod's own log file (--log-file), shown in the command drawer and read for the event log. "
       "Read-only. FIND MY NODE reads it from the running monerod. For a node on another computer, "
       "point it at a copy or a mounted folder."),
    _e("server.port", "int", 18200, "Window port",
       "The port the app window gets its readings over, on this computer only (127.0.0.1). "
       "Change it only if another program uses 18200. Takes effect the next time the app starts.",
       1024, 65535),
]

SCHEMA_BY_KEY = {e["key"]: e for e in SCHEMA}
DEFAULTS = {e["key"]: e["default"] for e in SCHEMA}

# The settings file's format, saved in it as "format". A file without one is format 1,
# whose defaults for how often to read (the node every 5 s, the computer every 10 s) move
# to the current ones once. A value someone picked themselves stays.
FORMAT = 2
OLD_DEFAULTS = {1: {"source.refresh_s": 5, "local.every_s": 10}}


def file_format(raw: dict) -> int:
    fmt = raw.get("format")
    return fmt if isinstance(fmt, int) and not isinstance(fmt, bool) and fmt >= 1 else 1


def migrate(raw: dict) -> dict | None:
    """A settings file's contents (nested, as saved) brought up to FORMAT, or None when
    it's already there. Only the values a migration names change: everything else stays
    as it was."""
    fmt = file_format(raw)
    if fmt >= FORMAT:
        return None
    out = {"format": FORMAT}
    out.update((k, dict(v) if isinstance(v, dict) else v) for k, v in raw.items() if k != "format")
    for old in range(fmt, FORMAT):
        for key, value in OLD_DEFAULTS.get(old, {}).items():
            sec, name = key.split(".", 1)
            inner = out.get(sec)
            if isinstance(inner, dict) and type(inner.get(name)) is int and inner[name] == value:
                inner[name] = DEFAULTS[key]
    return out


def node_log_path(values: dict, found: str | None) -> str:
    """monerod's log as the dashboard reads it: node.log_path, unless it was left at its default
    and the running node was found writing somewhere else (its --log-file or --data-dir)."""
    path = values.get("node.log_path") or DEFAULTS["node.log_path"]
    return found if found and path == DEFAULTS["node.log_path"] else path
PASSWORD_KEYS = [e["key"] for e in SCHEMA if e["type"] == "password"]



class SettingsError(Exception):
    def __init__(self, errors: dict):
        super().__init__("invalid settings")
        self.errors = errors


def _bad_text(s: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in s)


def validate_value(key: str, value, by_key: dict | None = None):
    """Return (normalised value, None) or (None, reason). `by_key` is a schema by key
    (a Settings' own); the core schema when left out."""
    entry = (SCHEMA_BY_KEY if by_key is None else by_key).get(key)
    if entry is None:
        return None, "unknown setting"
    t = entry["type"]
    if t == "int":
        v = value
        if isinstance(v, bool):
            return None, "must be a whole number"
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        elif isinstance(v, str) and re.fullmatch(r"\s*-?\d{1,9}\s*", v):
            v = int(v)
        if not isinstance(v, int):
            return None, "must be a whole number"
        if entry["min"] is not None and v < entry["min"]:
            return None, "must be at least %d" % entry["min"]
        if entry["max"] is not None and v > entry["max"]:
            return None, "must be at most %d" % entry["max"]
        return v, None
    if t == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "must be a number"
        v = float(value)
        if v != v:
            return None, "must be a number"
        if entry["min"] is not None and v < entry["min"]:
            return None, "must be at least %s" % entry["min"]
        if entry["max"] is not None and v > entry["max"]:
            return None, "must be at most %s" % entry["max"]
        return v, None
    if t == "bool":
        if not isinstance(value, bool):
            return None, "must be true or false"
        return value, None
    if t == "choice":
        for c in entry["choices"]:
            cv = c["value"]
            if value == cv and not isinstance(value, bool) and type(value) is type(cv):
                return cv, None
            if isinstance(value, str) and not isinstance(cv, str) and value.strip() == str(cv):
                return cv, None
        return None, "must be one of: " + ", ".join(str(c["value"]) for c in entry["choices"])
    if t in ("text", "password", "path"):
        if not isinstance(value, str):
            return None, "must be text"
        limit = 500 if t == "path" else 200
        if len(value) > limit:
            return None, "too long (at most %d characters)" % limit
        if _bad_text(value):
            return None, "contains control characters"
        return value, None
    if t == "url":
        if not isinstance(value, str):
            return None, "must be a web address"
        value = value.strip()
        if len(value) > 300:
            return None, "too long (at most 300 characters)"
        if _bad_text(value) or " " in value:
            return None, "contains spaces or control characters"
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            return None, "isn't a valid address (check the port number)"
        if parts.scheme not in ("http", "https"):
            return None, "must start with http:// or https://"
        if not parts.hostname:
            return None, "needs a host, like http://127.0.0.1:18081"
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return None, "put the login in the user and password settings, not in the address"
        if port is not None and not (1 <= port <= 65535):
            return None, "port must be 1 to 65535"
        return value, None
    return None, "unsupported type"


def cross_field_errors(values: dict, changed: set | None = None) -> dict:
    """Rules between settings. The error is reported on the key that was changed."""
    errors = {}
    rules = [
        ("thresholds.no_block_warn_min", "thresholds.no_block_bad_min", ">",
         "must be longer than the heads-up time (%s min)", "must be shorter than the problem time (%s min)"),
        ("thresholds.disk_warn_gb", "thresholds.disk_bad_gb", "<",
         "must be less than the heads-up level (%s GB)", "must be more than the problem level (%s GB)"),
        ("thresholds.reorg_warn", "thresholds.reorg_bad", ">",
         "must be more than the heads-up level (%s)", "must be less than the problem level (%s)"),
        ("thresholds.flood_warn", "thresholds.flood_bad", "<",
         "must be lower than the heads-up level (%s)", "must be higher than the problem level (%s)"),
    ]
    for warn_key, bad_key, op, bad_msg, warn_msg in rules:
        w, b = values.get(warn_key), values.get(bad_key)
        if not isinstance(w, int) or not isinstance(b, int):
            continue
        ok = b > w if op == ">" else b < w
        if ok:
            continue
        if changed is not None and warn_key in changed and bad_key not in changed:
            errors[warn_key] = warn_msg % b
        else:
            errors[bad_key] = bad_msg % w
    return errors


def nest(flat: dict) -> dict:
    out: dict = {}
    for k, v in flat.items():
        sec, name = k.split(".", 1)
        out.setdefault(sec, {})[name] = v
    return out


def flatten(nested: dict) -> dict:
    out = {}
    if not isinstance(nested, dict):
        return out
    for sec, inner in nested.items():
        if isinstance(inner, dict):
            for name, v in inner.items():
                out["%s.%s" % (sec, name)] = v
        elif isinstance(sec, str) and "." in sec:
            out[sec] = inner  # tolerate a flat file
    return out


class Settings:
    """Thread-safe settings holder. Saved values plus run-only overrides (CLI flags).

    Keys in the file that no schema entry knows are ignored and dropped at the next save.
    """

    def __init__(self, path: Path | str | None = None, *, save: bool = True, log=None):
        self.path = Path(path) if path else config_dir() / "settings.json"
        self._save_enabled = save
        self._lock = threading.RLock()
        self.schema = copy.deepcopy(SCHEMA)
        self.by_key = {e["key"]: e for e in self.schema}
        self.defaults = dict(DEFAULTS)
        self.sections = list(SECTIONS)
        self._values = dict(DEFAULTS)
        self._overrides: dict = {}
        self._listeners = []
        self._log = log or (lambda msg: print(msg, file=sys.stderr))
        self.file_keys: set = set()    # keys the file held at load (empty on a first run)
        self.load_warnings: list[str] = []

    # ---- loading and saving
    def load(self) -> "Settings":
        with self._lock:
            if not self.path.exists():
                self.file_keys = set()
                self._values = dict(self.defaults)
                if self._save_enabled:
                    self._save_locked()
                return self
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("not an object")
            except (OSError, ValueError) as e:
                bad = self.path.with_name(self.path.name + ".bad")
                self._warn("Settings file %s couldn't be read (%s); using defaults. "
                           "The old file was kept as %s." % (self.path, type(e).__name__, bad.name))
                try:
                    if self._save_enabled:
                        os.replace(self.path, bad)
                except OSError:
                    pass
                self.file_keys = set()
                self._values = dict(self.defaults)
                if self._save_enabled:
                    self._save_locked()
                return self
            moved = migrate(raw)
            if moved is not None:
                raw = moved
                if self._save_enabled:
                    try:
                        ensure_private_dir(self.path.parent)
                        atomic_write(self.path, (json.dumps(raw, indent=2) + "\n").encode("utf-8"), 0o600)
                    except OSError as e:
                        self._warn("Settings file %s couldn't be updated (%s)." % (self.path, type(e).__name__))
            flat = flatten(raw)
            self.file_keys = set(flat)
            values = dict(self.defaults)
            for k, v in flat.items():
                if k not in self.by_key:
                    continue
                nv, err = validate_value(k, v, self.by_key)
                if err:
                    self._warn("Setting %s in %s %s; using the default." % (k, self.path.name, err))
                    continue
                values[k] = nv
            for k in cross_field_errors(values):
                self._warn("Setting %s in %s conflicts with another; using defaults for that pair." % (k, self.path.name))
                values[k] = DEFAULTS[k]
                for pair in (("thresholds.no_block_warn_min", "thresholds.no_block_bad_min"),
                             ("thresholds.disk_warn_gb", "thresholds.disk_bad_gb"),
                             ("thresholds.reorg_warn", "thresholds.reorg_bad")):
                    if k in pair:
                        for p in pair:
                            values[p] = DEFAULTS[p]
            self._values = values
            if self._save_enabled:
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
        return self

    def _warn(self, msg):
        self.load_warnings.append(msg)
        self._log(msg)

    def _save_locked(self):
        if not self._save_enabled:
            return
        ensure_private_dir(self.path.parent)
        data = json.dumps(dict(format=FORMAT, **nest(self._values)), indent=2, sort_keys=False) + "\n"
        atomic_write(self.path, data.encode("utf-8"), 0o600)

    # ---- reading
    def get(self, key: str):
        with self._lock:
            if key in self._overrides:
                return self._overrides[key]
            return self._values[key]

    def __getitem__(self, key):
        return self.get(key)

    def values(self) -> dict:
        with self._lock:
            v = dict(self._values)
            v.update(self._overrides)
            return v

    def section(self, name: str) -> dict:
        with self._lock:
            pre = name + "."
            return {k[len(pre):]: self.get(k) for k in self._values if k.startswith(pre)}

    def password_set(self) -> bool:
        return bool(self.get("source.rpc_password"))

    def public(self) -> dict:
        """What GET /api/settings answers: passwords are never sent back."""
        vals = self.values()
        with self._lock:
            schema = copy.deepcopy(self.schema)
        for e in schema:
            if e["type"] == "password":
                vals[e["key"]] = ""
        return {"values": vals, "schema": schema, "rpc_password_set": self.password_set()}

    def validate(self, key: str, value):
        """validate_value against this Settings' schema."""
        return validate_value(key, value, self.by_key)

    # ---- changing
    def override(self, key: str, value) -> None:
        """A run-only value (from a command-line flag); never saved."""
        nv, err = validate_value(key, value, self.by_key)
        if err:
            raise SettingsError({key: err})
        with self._lock:
            self._overrides[key] = nv

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def update(self, partial: dict) -> list[str]:
        """Validate and save a partial set of flat values. Returns the keys that changed.

        Raises SettingsError({key: reason}) and changes nothing if any key is bad.
        A password of "" (what GET sends back) keeps the saved one; null clears it.
        """
        if not isinstance(partial, dict):
            raise SettingsError({"values": "must be an object of settings"})
        errors = {}
        clean = {}
        for k, v in partial.items():
            if not isinstance(k, str) or k not in self.by_key:
                errors[str(k)[:80]] = "unknown setting"
                continue
            if self.by_key[k]["type"] == "password":
                if v is None:
                    clean[k] = ""
                    continue
                if v == "":
                    continue  # keep the saved password
            nv, err = validate_value(k, v, self.by_key)
            if err:
                errors[k] = err
            else:
                clean[k] = nv
        with self._lock:
            if not errors:
                merged = self.values()
                merged.update(clean)
                errors.update(cross_field_errors(merged, set(clean)))
            if errors:
                raise SettingsError(errors)
            changed = []
            for k, v in clean.items():
                if self.get(k) != v or k in self._overrides:
                    changed.append(k)
                self._values[k] = v
                self._overrides.pop(k, None)
            if changed:
                self._save_locked()
        if changed:
            self._notify(changed)
        return changed

    def reset_section(self, section: str) -> list[str]:
        if section not in self.sections:
            raise SettingsError({"section": "unknown section"})
        with self._lock:
            changed = []
            for k, d in self.defaults.items():
                if k.split(".", 1)[0] != section or k in KEEP_ON_RESET:
                    continue
                if self.get(k) != d:
                    changed.append(k)
                self._values[k] = d
                self._overrides.pop(k, None)
            if changed:
                self._save_locked()
        if changed:
            self._notify(changed)
        return changed

    def _notify(self, changed):
        for fn in list(self._listeners):
            try:
                fn(changed)
            except Exception as e:  # a listener must never break saving
                self._log("settings listener failed: %s" % type(e).__name__)


def expand_path(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))
