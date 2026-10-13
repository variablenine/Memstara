"""Add-ons: parts made for one setup, kept out of the core so deleting a folder removes them.

An add-on is one folder, addons/<name>/, whose addon.py defines ADDON, an instance of a
subclass of Addon below. Every hook is optional and does nothing by default. The core
loads each addons/*/addon.py once at start (importlib, from the file's path; nothing is
put on sys.path), adds a `<name>.enabled` setting (bool, default true) plus the add-on's
own settings, and while it is enabled calls its hooks from the poller thread:

    poll(settings, now, ctx) -> dict         snapshot.addons[<name>]; JSON-safe
    lamps(data, ctx) -> [{id, label, state, value, help, screen?}]   (screen: the one that
                         explains it, "status", "network", "chain" or "mempool")
    alerts(data, ctx) -> [{id, level, text, title?, since?, ips?, hold?}] or None
    route(data, ctx) -> [{id, label, value, level, between?, link?, detail?, row_only?}]
    command_notes(data) -> {command: text}
    protected_hosts(data) -> [ip]
    log_sources() -> [{id, title, setting, summary?}]       (once, at load)
    web() -> {"scripts": [file], "styles": [file]}          (once, at load; files in its web/)

`settings` is every setting's value, flat (the add-on's own are `<name>.<key>`), with every
password blanked ("") except the add-on's own; `ctx` is
a copy of the core snapshot so far (lamps and route also see the merged "alerts"); `data`
is what its poll() returned last, or None when that failed (only command_notes and
protected_hosts are called then). Anything an add-on returns is checked and trimmed
before it reaches the snapshot. An exception, at load or in any hook, is caught: the add-on
shows its `error` in GET /api/addons, its lamps show as off, its alerts stay as they were,
a line goes to the log at most once a minute per add-on, and the core keeps running.
poll() runs in a worker thread: one that hasn't finished after POLL_TIMEOUT_S counts as
failed for that poll, and no new poll() of that add-on starts until it ends.
A disabled add-on's hooks are never called and nothing of it is in the snapshot.
"""

from __future__ import annotations

import copy
import importlib.util
import ipaddress
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from .settings import PASSWORD_KEYS, SECTIONS, check_entry, entry

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
ITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
WEB_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}(?:/[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}){0,4}$")
WEB_TYPES = {".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
             ".css": "text/css; charset=utf-8"}
# names an add-on can't take: the core's settings sections and words the URLs or API use
RESERVED = set(SECTIONS) | {"addons", "api", "core", "dashboard", "web", "memstara"}
LOG_EVERY_S = 60
POLL_TIMEOUT_S = 4        # an add-on's poll() still running after this counts as failed for that poll
STATES = ("ok", "warn", "bad", "off")
MAX_ITEMS = 40            # lamps, alerts or route items from one add-on per poll
LAMP_SCREENS = ("status", "network", "chain", "mempool")   # where a lamp's own screen can be


class Addon:
    """Base class for an add-on's ADDON. Override what you need; every hook is optional."""

    name = ""       # [a-z][a-z0-9_]*, the same as its folder's name (left empty: the folder's)
    title = ""      # display name
    intro = ""      # one or two plain sentences for its settings section

    def settings(self) -> list:
        return []

    def poll(self, settings: dict, now: float, ctx: dict) -> dict:
        return {}

    def lamps(self, data, ctx: dict) -> list:
        return []

    def alerts(self, data, ctx: dict):
        return []

    def route(self, data, ctx: dict) -> list:
        return []

    def command_notes(self, data) -> dict:
        return {}

    def protected_hosts(self, data) -> list:
        return []

    def log_sources(self) -> list:
        return []

    def web(self) -> dict:
        return {"scripts": [], "styles": []}


class AddonError(Exception):
    pass


class Loaded:
    """One folder under addons/: its ADDON (None when it didn't load) and what the core keeps."""

    def __init__(self, name: str, folder: Path):
        self.name = name
        self.folder = folder
        self.addon = None
        self.title = name
        self.intro = ""
        self.load_error = None
        self.hook_errors: dict = {}   # hook -> plain message, while it keeps failing
        self.last_logged = None
        self.settings: list = []      # schema entries, <name>.enabled first
        self.logs: list = []          # [{"source", "title", "summary", "setting"}]
        self.scripts: list = []       # URLs
        self.styles: list = []
        self.static: dict = {}        # URL -> (real path, content type)
        self.data = None              # what poll() returned last; None when off or it failed
        self.polling = None           # the thread running its poll(), while one runs past the deadline
        self.lamps_seen: list = []    # [(id, label, help, screen)] from the last good lamps()
        self.protected_seen: list = []   # from the last good protected_hosts(), kept while it fails

    @property
    def error(self):
        if self.load_error:
            return self.load_error
        return next(iter(self.hook_errors.values()), None)


def _short(e: BaseException) -> str:
    text = str(e).replace("\n", " ").strip()
    return ("%s: %s" % (type(e).__name__, text) if text else type(e).__name__)[:200]


def _text(v, limit: int, default: str = "") -> str:
    if v is None or isinstance(v, (dict, list, tuple, set)):
        return default
    s = "".join(ch for ch in str(v) if ch >= " ")
    return s[:limit]


class Addons:
    """Every add-on found in one folder. With no folder (tests, --no-addons) there are none."""

    def __init__(self, folder=None, log=None, clock=time.time):
        self.folder = Path(folder) if folder else None
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.clock = clock
        self._lock = threading.Lock()
        self.items: list = []
        if self.folder is not None and self.folder.is_dir():
            for d in sorted(self.folder.iterdir(), key=lambda p: p.name):
                if d.name.startswith((".", "_")) or not d.is_dir() or not (d / "addon.py").is_file():
                    continue
                rec = Loaded(d.name, d)
                self.items.append(rec)
                try:
                    self._load(rec, d / "addon.py")
                except (Exception, SystemExit) as e:   # anything at all: this one stays off, the rest load
                    rec.addon = None
                    rec.settings, rec.logs, rec.scripts, rec.styles, rec.static = [], [], [], [], {}
                    rec.load_error = ("It didn't load: %s" % (e if isinstance(e, AddonError) else _short(e)))[:300]
                    self.log("add-on %s: %s" % (rec.name, rec.load_error))

    # ---------------------------------------------------------------- loading
    def _load(self, rec: Loaded, path: Path):
        name = rec.name
        if not NAME_RE.match(name):
            raise AddonError("its folder name must be lowercase letters, digits and _ (starting with a letter)")
        if name in RESERVED:
            raise AddonError("%s is a name the dashboard itself uses" % name)
        mod_name = "memstara_addon_" + name
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:
            raise AddonError("addon.py can't be imported")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(mod_name, None)
            raise
        a = getattr(module, "ADDON", None)
        if not isinstance(a, Addon):
            raise AddonError("addon.py must define ADDON, an instance of memstara.addons.Addon")
        if (a.name or name) != name:
            raise AddonError("its name (%s) must be its folder's name (%s)" % (_text(a.name, 40), name))
        rec.title = _text(a.title, 60) or name
        rec.intro = _text(a.intro, 400)

        entries = [entry(name + ".enabled", "bool", True, "Use this add-on",
                         "Off: the dashboard doesn't call it, and its lamps, alerts, route boxes and panel go "
                         "away. Delete addons/%s/ to remove it for good." % name)]
        entries[0]["section"] = name
        for e in a.settings() or []:
            try:
                clean = check_entry(e, name)
            except ValueError as err:
                raise AddonError(str(err))
            if any(x["key"] == clean["key"] for x in entries):
                raise AddonError("setting %s is defined twice" % clean["key"])
            entries.append(clean)
        by_key = {e["key"]: e for e in entries}

        logs = []
        for s in a.log_sources() or []:
            sid = s.get("id") if isinstance(s, dict) else None
            if not isinstance(sid, str) or not ITEM_ID_RE.match(sid):
                raise AddonError("a log source needs an id of lowercase letters, digits, _ and -")
            setting = s.get("setting")
            if setting not in by_key or by_key[setting]["type"] != "path":
                raise AddonError("log source %s must name one of its own path settings" % sid)
            logs.append({"source": "%s.%s" % (name, sid), "title": _text(s.get("title"), 60) or sid,
                         "summary": _text(s.get("summary"), 300), "setting": setting})

        web = a.web() or {}
        if not isinstance(web, dict):
            raise AddonError("web() must return {\"scripts\": [...], \"styles\": [...]}")
        web_root = rec.folder / "web"
        real_root = os.path.realpath(web_root)
        for kind in ("scripts", "styles"):
            for f in web.get(kind) or []:
                if not isinstance(f, str) or not WEB_FILE_RE.match(f) or ".." in f.split("/"):
                    raise AddonError("web file %r isn't a plain path inside its web/ folder" % _text(f, 80))
                ext = os.path.splitext(f)[1].lower()
                if (kind == "scripts" and ext not in (".js", ".mjs")) or (kind == "styles" and ext != ".css"):
                    raise AddonError("web file %s: scripts are .js or .mjs, styles are .css" % f)
                real = os.path.realpath(web_root / f)
                if os.path.commonpath([real_root, real]) != real_root or not os.path.isfile(real):
                    raise AddonError("web file %s isn't in addons/%s/web/" % (f, name))
                url = "/addons/%s/web/%s" % (name, f)
                rec.static[url] = (real, WEB_TYPES[ext])
                (rec.scripts if kind == "scripts" else rec.styles).append(url)

        rec.addon = a
        rec.settings = entries
        rec.logs = logs

    def register_settings(self, settings) -> None:
        """Add every loaded add-on's settings section (call once, before the poller starts)."""
        for rec in self.items:
            if rec.addon is not None:
                settings.extend(rec.settings, rec.name)

    def static_map(self) -> dict:
        out = {}
        for rec in self.items:
            out.update(rec.static)
        return out

    # ---------------------------------------------------------------- calling hooks
    def _fail(self, rec: Loaded, hook: str, e: BaseException):
        self._failed(rec, hook, "%s() failed: %s" % (hook, _short(e)))

    def _failed(self, rec: Loaded, hook: str, msg: str):
        with self._lock:
            rec.hook_errors[hook] = msg
            now = self.clock()
            log = rec.last_logged is None or now - rec.last_logged >= LOG_EVERY_S
            if log:
                rec.last_logged = now
        if log:
            self.log("add-on %s: %s" % (rec.name, msg))

    def _ok(self, rec: Loaded, hook: str):
        if hook in rec.hook_errors:
            with self._lock:
                rec.hook_errors.pop(hook, None)

    def _call(self, rec: Loaded, hook: str, *args, check=None):
        """Call one hook; any exception (also from `check` on its answer) -> None and an error."""
        try:
            out = getattr(rec.addon, hook)(*args)
            if check is not None:
                out = check(out)
        except (Exception, SystemExit) as e:
            self._fail(rec, hook, e)
            return None
        self._ok(rec, hook)
        return out

    def enabled(self, values: dict) -> list:
        return [r for r in self.items if r.addon is not None and values.get(r.name + ".enabled") is True]

    def poll(self, values: dict, now: float, ctx: dict) -> dict:
        """Poll every enabled add-on -> snapshot.addons ({name: data})."""
        out = {}
        on = self.enabled(values)
        secret = set(PASSWORD_KEYS) | {e["key"] for r in self.items for e in r.settings if e["type"] == "password"}
        for rec in self.items:
            if rec not in on:
                rec.data = None
                rec.protected_seen = []
                continue
            mine = rec.name + "."
            vals = {k: "" if k in secret and not k.startswith(mine) else v for k, v in values.items()}
            rec.data = self._poll_one(rec, vals, now, copy.deepcopy(ctx))
            if rec.data is not None:
                out[rec.name] = rec.data
        return out

    def _poll_one(self, rec: Loaded, values: dict, now: float, ctx: dict):
        """One add-on's poll(), in a worker thread so a hook that hangs can't stop the poller.
        Waited for POLL_TIMEOUT_S at most; one still running after that goes on by itself, its
        answer unused, and no new poll() starts until it ends. Meanwhile the add-on counts as
        failed (None: its alerts are held and its lamps show ADD-ON ERROR)."""
        if rec.polling is not None and rec.polling.is_alive():
            self._failed(rec, "poll", "poll() still hasn't finished an earlier call (it took over %g s)"
                         % POLL_TIMEOUT_S)
            return None
        box: dict = {}

        def run():
            try:
                box["data"] = _check_data(rec.addon.poll(values, now, ctx))
            except BaseException as e:   # anything at all: reported below, like any hook's failure
                box["error"] = e

        t = threading.Thread(target=run, name="memstara-addon-%s" % rec.name, daemon=True)
        rec.polling = t
        t.start()
        t.join(POLL_TIMEOUT_S)
        if t.is_alive():
            self._failed(rec, "poll", "poll() didn't finish in %g s" % POLL_TIMEOUT_S)
            return None
        rec.polling = None
        if "data" not in box:
            self._fail(rec, "poll", box.get("error") or AddonError("no answer"))
            return None
        self._ok(rec, "poll")
        return box["data"]

    def alerts(self, values: dict, ctx: dict) -> dict:
        """{name: [alert] or None (hold: its alerts stay as they are)} for the enabled add-ons."""
        out = {}
        for rec in self.enabled(values):
            if rec.data is None:
                out[rec.name] = None
                continue
            out[rec.name] = self._call(rec, "alerts", rec.data, copy.deepcopy(ctx), check=_check_alerts)
        return out

    def lamps(self, values: dict, ctx: dict) -> list:
        """Lamp rows after the core's: {id: "<name>.<id>", label, state, value, addon, help}."""
        rows = []
        for rec in self.enabled(values):
            got = None if rec.data is None else self._call(rec, "lamps", rec.data, copy.deepcopy(ctx),
                                                           check=_check_lamps)
            if got is None:
                # its parts show as off, under the names they had
                got = [{"id": i, "label": label, "state": "off", "value": "ADD-ON ERROR", "help": h, "screen": sc}
                       for i, label, h, sc in rec.lamps_seen]
            else:
                rec.lamps_seen = [(l["id"], l["label"], l["help"], l["screen"]) for l in got]
            for l in got:
                rows.append({"id": "%s.%s" % (rec.name, l["id"]), "label": l["label"], "state": l["state"],
                             "value": l["value"], "addon": rec.name, "help": l["help"], "screen": l["screen"]})
        return rows

    def route(self, values: dict, ctx: dict) -> list:
        out = []
        for rec in self.enabled(values):
            if rec.data is None:
                continue
            for item in self._call(rec, "route", rec.data, copy.deepcopy(ctx), check=_check_route) or []:
                item["addon"] = rec.name
                out.append(item)
        return out

    def protected(self, values: dict) -> list:
        """[(ip, add-on title)]: addresses `ban` must never cover. When protected_hosts() fails,
        the addresses it gave last time stay."""
        out = []
        for rec in self.enabled(values):
            got = self._call(rec, "protected_hosts", rec.data, check=_check_ips)
            if got is not None:
                rec.protected_seen = got
            out.extend((ip, rec.title) for ip in rec.protected_seen)
        return out

    def command_notes(self, values: dict) -> dict:
        """{command: text} from every enabled add-on, joined when several have one."""
        out: dict = {}
        for rec in self.enabled(values):
            notes = self._call(rec, "command_notes", copy.deepcopy(rec.data), check=_check_notes) or {}
            for cmd, text in notes.items():
                out[cmd] = (out[cmd] + " " + text) if cmd in out else text
        return out

    def log_sources(self, values: dict) -> dict:
        """{"<name>.<id>": {"setting", "title", "enabled", "addon"}} for every loaded add-on."""
        out = {}
        for rec in self.items:
            if rec.addon is None:
                continue
            on = values.get(rec.name + ".enabled") is True
            for l in rec.logs:
                out[l["source"]] = {"setting": l["setting"], "title": l["title"], "enabled": on,
                                    "addon": rec.title}
        return out

    def listing(self, values: dict) -> dict:
        """GET /api/addons."""
        out = []
        for rec in self.items:
            out.append({
                "name": rec.name, "title": rec.title, "intro": rec.intro,
                "enabled": rec.addon is not None and values.get(rec.name + ".enabled") is True,
                "error": rec.error, "scripts": list(rec.scripts), "styles": list(rec.styles),
                "logs": [{"source": l["source"], "title": l["title"], "summary": l["summary"]} for l in rec.logs],
            })
        return {"addons": out}


# ---------------------------------------------------------------- checking what hooks return

def _check_data(v):
    if not isinstance(v, dict):
        raise AddonError("poll() must return a dict")
    try:
        json.dumps(v, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as e:
        raise AddonError("poll() returned something that can't go in the snapshot (%s)" % _short(e))
    return v


def _level(v, allowed=STATES, default="off"):
    return v if v in allowed else default


def _check_lamps(v):
    if not isinstance(v, list):
        raise AddonError("lamps() must return a list")
    out, seen = [], set()
    for l in v[:MAX_ITEMS]:
        if not isinstance(l, dict) or not isinstance(l.get("id"), str) or not ITEM_ID_RE.match(l["id"]):
            raise AddonError("a lamp needs an id of lowercase letters, digits, _ and -")
        if l["id"] in seen:
            continue
        seen.add(l["id"])
        out.append({"id": l["id"], "label": _text(l.get("label"), 24).upper() or l["id"].upper(),
                    "state": _level(l.get("state")), "value": _text(l.get("value"), 16).upper(),
                    "help": _text(l.get("help"), 400),
                    "screen": l.get("screen") if l.get("screen") in LAMP_SCREENS else None})
    return out


def _check_alerts(v):
    if v is None:
        return None
    if not isinstance(v, list):
        raise AddonError("alerts() must return a list or None")
    out = []
    for a in v[:MAX_ITEMS]:
        if not isinstance(a, dict) or not isinstance(a.get("id"), str) or not ITEM_ID_RE.match(a["id"]):
            raise AddonError("an alert needs an id of lowercase letters, digits, _ and -")
        if a.get("hold") is True:
            out.append({"id": a["id"], "hold": True})
            continue
        since = a.get("since")
        ips = a.get("ips")
        out.append({"id": a["id"], "level": _level(a.get("level"), ("warn", "bad"), "warn"),
                    "title": _text(a.get("title"), 40).upper() or None, "text": _text(a.get("text"), 400),
                    "since": int(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else None,
                    "ips": [_text(i, 60) for i in ips[:50]] if isinstance(ips, list) else None})
    return out


def _check_route(v):
    if not isinstance(v, list):
        raise AddonError("route() must return a list")
    out = []
    for r in v[:MAX_ITEMS]:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not ITEM_ID_RE.match(r["id"]):
            raise AddonError("a route item needs an id of lowercase letters, digits, _ and -")
        item = {"id": r["id"], "label": _text(r.get("label"), 24) or r["id"].upper(),
                "value": _text(r.get("value"), 40), "level": _level(r.get("level"))}
        if r.get("between") == "peers-node":
            item["between"] = "peers-node"
        link = r.get("link")
        if isinstance(link, dict) and link.get("label"):
            item["link"] = {"label": _text(link.get("label"), 40), "level": _level(link.get("level"))}
        if r.get("detail"):
            item["detail"] = _text(r.get("detail"), 120)
        if r.get("row_only") is True:
            item["row_only"] = True
        out.append(item)
    return out


def _check_ips(v):
    if not isinstance(v, list):
        raise AddonError("protected_hosts() must return a list of addresses")
    out = []
    for x in v[:MAX_ITEMS]:
        try:
            ip = str(ipaddress.ip_address(str(x).strip()))
        except ValueError:
            continue
        if ip not in out:
            out.append(ip)
    return out


def _check_notes(v):
    if not isinstance(v, dict):
        raise AddonError("command_notes() must return a dict")
    return {k: _text(t, 400) for k, t in v.items() if isinstance(k, str) and isinstance(t, str) and t.strip()}
