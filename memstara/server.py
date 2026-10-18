"""The local server behind the app window: the screens from web/ and the JSON API, on
127.0.0.1 only. The window (window.py) is its only client.

Safety rules (see docs/DESIGN.md): Host check (421), the window key (the app makes a
fresh one at every start and only its own window has it: the page and every /api/ call
need it, else 403, so no browser or other program can use the dashboard), write guard for
PUT/POST and for GET /api/node-log (X-Memstara: 1 and a matching Origin, else 403), 64 KB
body limit (413), static files only from a list built at start-up (by
walking web/), security headers on every answer,
no CORS headers ever, and no stack traces or logins in answers.
"""

from __future__ import annotations

import hmac
import json
import os
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import VERSION, nodelog
from .history import WINDOWS
from .privacy import REVEAL_WORD, has_token, utc_stamps
from .rpc import same_rpc
from .settings import SettingsError, validate_value

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
MAX_BODY = 64 * 1024
KEY_HEADER = "X-Memstara-Key"
OWN_WINDOW = ("Memstara opens in its own window, not in a browser. "
              "Open it from your apps, or with: python3 -m memstara")
# settings that can hold an address, a folder or a login: left alone while privacy mode is on,
# since the window only has their hidden form
PRIVATE_TYPES = ("text", "url", "path", "password")
PRIVACY_ON = "Privacy mode is on. Turn it off to change this."

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}


def build_static_map(web_dir) -> dict:
    """URL path -> (absolute file path, content type), from walking web/ once.

    Hidden files and folders are skipped, symlinks are never followed out of web/,
    and only known file types are served.
    """
    out = {}
    root = Path(web_dir)
    if not root.is_dir():
        return out
    real_root = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            ctype = CONTENT_TYPES.get(os.path.splitext(name)[1].lower())
            if ctype is None:
                continue
            full = os.path.join(dirpath, name)
            real = os.path.realpath(full)
            if os.path.commonpath([real_root, real]) != real_root or not os.path.isfile(real):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out["/" + rel] = (real, ctype)
    return out


def _int_or_none(text):
    try:
        return int(text) if text is not None and len(text) <= 20 else None
    except ValueError:
        return None


def _seconds(text) -> float:
    """A wait in seconds from a query string: 0 when it isn't a plain, finite number."""
    try:
        v = float(text) if len(text) <= 20 else 0.0
    except ValueError:
        return 0.0
    return v if v == v and v not in (float("inf"), float("-inf")) and v > 0 else 0.0


def _same(given: str, key: str) -> bool:
    """The given key is the window key (an empty key matches nothing)."""
    return bool(key) and hmac.compare_digest(given.encode("utf-8", "replace"), key.encode("utf-8", "replace"))


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, app, host: str, port: int, key: str | None = None):
        """key: the window key every API call and the page itself must carry (None: no
        key needed, for tests)."""
        self.app = app
        self.bind_host = host
        self.key = key
        h = host.strip("[]")
        if ":" in h:
            self.address_family = socket.AF_INET6
        super().__init__((h, port), Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def allowed_hosts(self) -> set:
        p = self.port
        names = {"127.0.0.1", "localhost", "[::1]"}
        bh = self.bind_host
        if bh not in ("0.0.0.0", "::", "[::]", ""):
            names.add("[%s]" % bh.strip("[]") if ":" in bh else bh)
        return {("%s:%d" % (n, p)).lower() for n in names}


class Handler(BaseHTTPRequestHandler):
    server_version = "memstara"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 30
    error_content_type = "text/plain; charset=utf-8"
    error_message_format = "%(code)d %(message)s\n"

    # ---- plumbing
    def log_message(self, fmt, *args):
        pass  # no access log: nothing about requests (or logins) ends up in a log

    def end_headers(self):
        # on every answer, including errors
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if getattr(self, "_is_api", False):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler's own errors (bad request line, ...): plain, with our headers
        self._is_api = False
        try:
            short = HTTPStatus(code).phrase
        except ValueError:
            short = "Error"
        body = ("%d %s\n" % (code, short)).encode()
        self.close_connection = True
        try:
            self.send_response(code, short)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD" and code >= 200 and code not in (204, 304):
                self.wfile.write(body)
        except OSError:
            pass

    def _json(self, code, obj, extra_headers=None, safe=False):
        # privacy mode: every API answer has its private values swapped for tokens on the way
        # out (privacy.py); `safe` marks one already done that way or made of fixed text
        if not safe and getattr(self, "_is_api", False) and self._private():
            if isinstance(obj, (bytes, bytearray)):
                obj = json.loads(obj)
            self.app.privacy.learn(self.app.poller.snapshot())   # the node's host name, before any answer
            obj = self.app.privacy.redact(obj)
        body = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj, separators=(",", ":")).encode()
        self._raw(code, body, "application/json", extra_headers)

    def _private(self) -> bool:
        return self.app.settings.get("display.privacy") is True

    def _raw(self, code, body, ctype, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except OSError:
                pass

    def _text(self, code, text):
        self._raw(code, (text + "\n").encode(), "text/plain; charset=utf-8")

    @property
    def app(self):
        return self.server.app

    # ---- checks that apply to every request
    def _preflight(self) -> bool:
        parts = urlsplit(self.path)
        path = parts.path or "/"
        self._is_api = path.startswith("/api/") or path == "/api"
        host = (self.headers.get("Host") or "").strip().lower()
        if not host or host not in self.server.allowed_hosts():
            self.close_connection = True
            if self._is_api:
                self._json(421, {"error": "This address isn't allowed. " + OWN_WINDOW})
            else:
                self._text(421, "Misdirected request. " + OWN_WINDOW)
            return False
        key = self.server.key
        if key is not None:
            if self._is_api:
                given = self.headers.get(KEY_HEADER) or ""
            elif self._needs_no_key(path):
                return True   # the screens' own scripts, styles, fonts and pictures: nothing private
            else:
                given = (parse_qs(parts.query or "").get("key") or [""])[0]
            if not _same(given, key):
                self.close_connection = True
                if self._is_api:
                    self._json(403, {"error": OWN_WINDOW})
                else:
                    self._text(403, OWN_WINDOW)
                return False
        return True

    def _needs_no_key(self, path: str) -> bool:
        """One of the screens' own files that isn't a page. Pages, and anything that isn't
        one of the files, need the key. The name is decoded the way _static reads it, so
        /%69ndex.html needs the key just as /index.html does."""
        entry = self.app.static.get(unquote(path))
        return entry is not None and not entry[1].startswith("text/html")

    def _write_guard(self) -> bool:
        if self.headers.get("X-Memstara") != "1":
            self._json(403, {"error": "Changes need the X-Memstara: 1 header"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").strip().lower()
            if origin.strip().lower() != "http://" + host:
                self._json(403, {"error": "Changes are only accepted from the dashboard's own page"})
                return False
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site and site not in ("same-origin", "none"):
            self._json(403, {"error": "Changes are only accepted from the dashboard's own page"})
            return False
        return True

    def _read_json(self):
        """Returns (obj, None) or (None, already-sent)."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._json(411, {"error": "Send a Content-Length"})
            return None, True
        raw_len = self.headers.get("Content-Length")
        try:
            n = int(raw_len) if raw_len not in (None, "") else 0
            if n < 0:
                raise ValueError
        except ValueError:
            self.close_connection = True
            self._json(400, {"error": "Bad Content-Length"})
            return None, True
        if n > MAX_BODY:
            self.close_connection = True
            self._json(413, {"error": "Request body too large (at most 64 KB)"})
            return None, True
        data = self.rfile.read(n) if n else b""
        if not data.strip():
            return {}, False
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"error": "The request body isn't valid JSON"})
            return None, True
        if not isinstance(obj, dict):
            self._json(400, {"error": "The request body must be a JSON object"})
            return None, True
        return obj, False

    # ---- methods
    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    API = {
        "/api/snapshot": ("GET",),
        "/api/history": ("GET",),
        "/api/settings": ("GET", "PUT"),
        "/api/settings/reset": ("POST",),
        "/api/test-connection": ("POST",),
        "/api/commands": ("GET",),
        "/api/command": ("POST",),
        "/api/node-log": ("GET",),
        "/api/health": ("GET",),
        "/api/privacy": ("POST",),
    }
    # this one reads a file on this computer: same guard as a change
    GUARDED_GETS = ("/api/node-log",)

    def _handle(self, method):
        try:
            if not self._preflight():
                return
            parts = urlsplit(self.path)
            path = parts.path or "/"
            if self._is_api:
                allowed = self.API.get(path)
                if allowed is None:
                    return self._json(404, {"error": "No such API"})
                m = "GET" if method == "HEAD" else method
                if m not in allowed:
                    return self._json(405, {"error": "Method not allowed"}, [("Allow", ", ".join(allowed))])
                if m in ("PUT", "POST"):
                    if not self._write_guard():
                        return
                    body, sent = self._read_json()
                    if sent:
                        return
                else:
                    if path in self.GUARDED_GETS and not self._write_guard():
                        return
                    body = None
                return self._api(m, path, parts.query, body)
            if method not in ("GET", "HEAD"):
                return self._text(405, "Method not allowed")
            return self._static(path)
        except Exception as e:  # never send a stack trace
            self.app.log("server: %s while answering %s" % (type(e).__name__, self.command))
            try:
                self._json(500, {"error": "Something went wrong on the dashboard's side"}, safe=True)
            except Exception:
                pass

    # ---- static files
    def _static(self, path):
        self._is_api = False
        key = "/index.html" if path == "/" else unquote(path)
        entry = self.app.static.get(key)
        if entry is None:
            if path == "/":
                return self._text(404, "The screens (web/index.html) are missing.")
            return self._text(404, "Not found")
        real, ctype = entry
        try:
            with open(real, "rb") as fh:
                data = fh.read()
        except OSError:
            return self._text(404, "Not found")
        self._raw(200, data, ctype, [("Cache-Control", "no-cache")])

    # ---- API
    def _api(self, method, path, query, body):
        app = self.app
        if path == "/api/snapshot":
            # ?after=N&wait=S: answer once there's a snapshot newer than number N, or after
            # S seconds (at most 20) with the same one. Without `after`: the latest, at once.
            q = parse_qs(query or "")
            after = _int_or_none((q.get("after") or [None])[0])
            if after is None:
                snap, data = app.poller.snapshot_pair()
            else:
                snap, data = app.poller.snapshot_after(after, _seconds((q.get("wait") or ["0"])[0]))
            if self._private():
                data = app.privacy.snapshot_json(snap)   # made once per snapshot
            return self._json(200, data, safe=True)
        if path == "/api/health":
            return self._json(200, {"ok": True, "version": VERSION}, safe=True)
        if path == "/api/history":
            q = parse_qs(query or "")
            w = (q.get("window") or ["3600"])[0]
            try:
                window = int(w)
            except ValueError:
                window = -1
            if window not in WINDOWS:
                return self._json(400, {"error": "window must be one of " + ", ".join(str(x) for x in WINDOWS)})
            return self._json(200, app.poller.history_query(window))
        if path == "/api/settings":
            if method == "GET":
                return self._settings_answer()
            values = body.get("values") if isinstance(body, dict) else None
            if not isinstance(values, dict):
                return self._json(400, {"errors": {"values": "send {\"values\": {...}}"}})
            if "display.privacy" in values:
                return self._json(400, {"errors": {"display.privacy": "Use the PRIVACY switch at the top of the window."}})
            if self._private():
                by_key = app.settings.by_key
                refused = {str(k)[:80]: PRIVACY_ON for k, v in values.items()
                           if (by_key.get(k) or {}).get("type") in PRIVATE_TYPES or has_token(v)}
                if refused:
                    return self._json(403, {"errors": refused})
            try:
                app.settings.update(values)
            except SettingsError as e:
                return self._json(400, {"errors": e.errors})
            except OSError:
                return self._json(500, {"error": "The settings file couldn't be saved"})
            return self._settings_answer()
        if path == "/api/settings/reset":
            section = str(body.get("section", ""))
            if self._private() and any(e.get("section") == section and e.get("type") in PRIVATE_TYPES
                                       for e in app.settings.by_key.values()):
                return self._json(403, {"error": "Privacy mode is on. Turn it off to reset this section."})
            try:
                app.settings.reset_section(section)
            except SettingsError as e:
                return self._json(400, {"errors": e.errors})
            except OSError:
                return self._json(500, {"error": "The settings file couldn't be saved"})
            return self._settings_answer()
        if path == "/api/privacy":
            return self._privacy(body)
        if path == "/api/test-connection":
            if self._private() and has_token(body):
                return self._json(400, {"error": PRIVACY_ON})
            url = body.get("rpc_url")
            nv, err = validate_value("source.rpc_url", url)
            if err:
                return self._json(400, {"errors": {"rpc_url": err}})
            user = body.get("rpc_user", "")
            if user is None:
                user = ""
            uv, err = validate_value("source.rpc_user", user)
            if err:
                return self._json(400, {"errors": {"rpc_user": err}})
            if body.get("rpc_password") not in (None, ""):
                pw, err = validate_value("source.rpc_password", body["rpc_password"])
                if err:
                    return self._json(400, {"errors": {"rpc_password": err}})
            elif same_rpc(nv, app.settings.get("source.rpc_url")):
                pw = app.settings.get("source.rpc_password")   # missing or "" means "use the saved one"
            else:
                pw = ""   # the saved password only ever goes to the saved node
            return self._json(200, app.test_connection(nv, uv, pw))
        if path == "/api/commands":
            return self._json(200, app.commands.listing(), safe=True)   # fixed text: the commands and their help
        if path == "/api/command":
            status, out = app.commands.run(body)
            return self._json(status, out)
        if path == "/api/node-log":
            q = parse_qs(query or "")
            status, out = nodelog.read(app.settings, (q.get("source") or ["monerod"])[0],
                                       (q.get("lines") or [None])[0])
            if self._private() and isinstance(out, dict) and isinstance(out.get("lines"), list):
                # privacy mode shows UTC: local times in the log too
                out = dict(out, lines=[utc_stamps(x) if isinstance(x, str) else x for x in out["lines"]])
            return self._json(status, out)
        return self._json(404, {"error": "No such API"})

    def _settings_answer(self):
        pub = self.app.settings.public()
        if self._private():
            self.app.privacy.learn(self.app.poller.snapshot())
            pub = self.app.privacy.settings(pub)
        return self._json(200, pub, safe=True)

    def _privacy(self, body):
        """POST /api/privacy {"on": true} turns privacy mode on at once; {"on": false} needs
        the typed word too, since everything private comes back on screen."""
        on = body.get("on") if isinstance(body, dict) else None
        if not isinstance(on, bool):
            return self._json(400, {"error": "Send {\"on\": true} or {\"on\": false, \"confirm\": \"%s\"}" % REVEAL_WORD})
        if not on and self._private():
            word = body.get("confirm")
            if not isinstance(word, str) or word.strip().upper() != REVEAL_WORD:
                return self._json(400, {"error": "Type %s to confirm." % REVEAL_WORD, "field": "confirm"})
        try:
            self.app.settings.update({"display.privacy": on})
        except SettingsError as e:
            return self._json(400, {"errors": e.errors})
        except OSError:
            return self._json(500, {"error": "The settings file couldn't be saved"})
        return self._json(200, {"ok": True, "on": on}, safe=True)
