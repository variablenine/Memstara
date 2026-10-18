"""The safety claims in SAFETY.md, as tests.

Memstara only reads: every RPC method it can send is on rpc.READ_ALLOWED, and none of
monerod's methods that change something is on it. Adding a method means changing the
list in this file too, so a reviewer always sees it.

The rest look at the code itself (Python's own parser, nothing run): only rpc.py and
server.py use the network, nothing runs another program, and the only files opened for
writing are the app's own.
"""

from __future__ import annotations

import ast
import inspect
import re
import unittest

import helpers  # noqa: F401  (sets up sys.path)
from helpers import ROOT
from memstara import commands as C
from memstara import rpc

# Every method on rpc.READ_ALLOWED, written out again here on purpose.
READS = {
    "get_info", "get_connections", "get_transaction_pool_stats", "get_transaction_pool_hashes",
    "get_net_stats", "get_block_headers_range", "get_last_block_header", "sync_info", "get_bans",
    "hard_fork_info", "get_fee_estimate", "get_limit", "get_version", "get_output_distribution",
    "get_height", "mining_status", "get_block", "get_block_header_by_hash", "get_alternate_chains",
    "get_coinbase_tx_sum", "get_output_histogram", "get_transactions", "is_key_image_spent",
    "get_transaction_pool", "get_peer_list", "banned", "rpc_access_data",
}

# monerod v0.18.5.3's RPC methods that change something on the node or the network
# (src/rpc/core_rpc_server.h, with their old spellings). None may ever be on the list.
CHANGE_METHODS = {
    "send_raw_transaction", "sendrawtransaction",   # sends a transaction to the network
    "start_mining", "stop_mining",
    "save_bc",                                      # writes the blockchain to disk
    "set_log_hash_rate", "set_log_level", "set_log_categories",
    "set_bootstrap_daemon",
    "stop_daemon",
    "set_limit", "out_peers", "in_peers",           # even with set: false, out_peers and in_peers can set
    "update",                                       # can download a release
    "pop_blocks",
    "submit_block", "submitblock", "generateblocks",
    "set_bans",                                     # ban and unban
    "flush_txpool", "relay_tx",
    "prune_blockchain",                             # prunes, unless asked only to check
    "flush_cache",
    "rpc_access_submit_nonce", "rpc_access_pay", "rpc_access_tracking", "rpc_access_account",
}


class ReadOnlyTest(unittest.TestCase):
    def test_the_list_is_exactly_these_reads(self):
        self.assertEqual(set(rpc.READ_ALLOWED), READS)

    def test_no_method_that_changes_something(self):
        self.assertEqual(set(rpc.READ_ALLOWED) & CHANGE_METHODS, set())
        for method, endpoint in rpc.READ_ALLOWED.items():   # and each goes where its name says
            self.assertIn(endpoint, ("/json_rpc", "/" + method), method)

    def test_one_way_to_send(self):
        """call() is RpcClient's only public method, and rpc.py has no list of writes."""
        public = {n for n, _ in inspect.getmembers(rpc.RpcClient, inspect.isfunction) if not n.startswith("_")}
        self.assertEqual(public, {"call"})
        self.assertFalse(hasattr(rpc, "WRITE_ALLOWED"))

    def test_every_command_only_reads(self):
        self.assertEqual(C.check_registry(), [])
        self.assertLessEqual(C.registry_calls(), READS)
        for cmd in C.REGISTRY.values():
            self.assertEqual(set(cmd.public()) & {"kind", "confirm"}, set(), cmd.name)


# ---- the code itself

CODE = sorted((ROOT / "memstara").rglob("*.py"))


def _name(path):
    """A file's name inside memstara/ ("rpc.py", "sub/rpc.py"), so a subfolder never shares another file's allowance."""
    return path.relative_to(ROOT / "memstara").as_posix()

# modules that reach the network, and the files allowed to import them
NETWORK = {"socket", "ssl", "http", "http.client", "http.server", "urllib.request", "urllib.error",
           "socketserver", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "asyncio",
           "selectors", "select", "webbrowser"}
GI_NETWORK = ("Gio.Socket", "Gio.Network", "Gio.Resolver", "Gio.TlsClient", "Gio.Proxy")   # the same, in Gio
NETWORK_ALLOWED = {
    "rpc.py": {"socket", "ssl", "http.client", "urllib.request", "urllib.error"},   # asks monerod
    "server.py": {"socket", "http", "http.server"},                                 # answers its own window
    "privacy.py": {"socket"},                                                       # gethostname only, below
}
# modules and calls that run other programs or code from elsewhere
PROGRAMS = {"subprocess", "multiprocessing", "pty", "ctypes", "cffi", "concurrent.futures", "runpy",
            "importlib", "pickle", "marshal", "shelve", "code", "codeop", "sqlite3", "dbm"}
PROGRAM_CALLS = {"os.system", "os.popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.posix_spawnp",
                 "os.startfile", "os.kill", "os.killpg", "eval", "exec", "compile", "__import__"}
PROGRAM_PREFIXES = ("os.exec", "os.spawn", "GLib.spawn", "Gio.Subprocess", "Gio.AppInfo", "Gio.DesktopAppInfo",
                    "Gio.bus_", "Gio.DBus", "Gtk.show_uri", "Gtk.UriLauncher", "Gtk.FileLauncher")
# calls that rename, remove or change files
FILE_CHANGES = {"os.replace", "os.rename", "os.renames", "os.unlink", "os.remove", "os.rmdir", "os.removedirs",
                "os.makedirs", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.link", "os.symlink", "os.utime",
                "os.fchmod", "os.fchown", "os.lchown", "os.lchmod", "os.mkfifo", "os.mknod", "os.setxattr",
                "os.removexattr", "GLib.file_set_contents"}
# os.open flags that only read
READ_FLAGS = {"os.O_RDONLY", "os.O_NONBLOCK", "os.O_NOFOLLOW", "os.O_CLOEXEC", "os.O_NOCTTY"}


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), str(path))


def _imports(tree):
    """Every module imported, and every name taken from one ("from os import system": os, os.system)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            out.add(n.module)
            out.update("%s.%s" % (n.module, a.name) for a in n.names)
    return out


def _local_names(tree):
    """What each imported name stands for: o -> os, system -> os.system, _GLib -> GLib."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update((a.asname, a.name) for a in n.names if a.asname)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            for a in n.names:
                out[a.asname or a.name] = a.name if n.module == "gi.repository" else "%s.%s" % (n.module, a.name)
    return out


def _calls(tree):
    """Each call and what it calls, with imported names written out in full."""
    names = _local_names(tree)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            head = n.func
            while isinstance(head, ast.Attribute):
                head = head.value
            name = ast.unparse(n.func)
            if isinstance(head, ast.Name) and head.id in names:
                name = names[head.id] + name[len(head.id):]
            f = n.func
            if isinstance(f, ast.Call) and ast.unparse(f.func) == "getattr" and len(f.args) >= 2 \
                    and isinstance(f.args[1], ast.Constant):   # getattr(os, "system")(...) is os.system
                mod = ast.unparse(f.args[0])
                name = "%s.%s" % (names.get(mod, mod), f.args[1].value)
            out.append((n, name))
    return out


def _flags(expr, tree):
    """The os.O_* names in an os.open flags argument: os.O_X, getattr(os, "O_X", 0), or a
    name set to those anywhere in the file."""
    exprs = [expr]
    if isinstance(expr, ast.Name):
        exprs = [n.value for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                 and n.value is not None and any(isinstance(t, ast.Name) and t.id == expr.id
                         for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    out = set()
    for e in exprs:
        for x in ast.walk(e):
            if isinstance(x, ast.Attribute) and ast.unparse(x).startswith("os."):
                out.add(ast.unparse(x))
            elif isinstance(x, ast.Call) and ast.unparse(x.func) == "getattr" and len(x.args) >= 2:
                if isinstance(x.args[1], ast.Constant):
                    out.add("%s.%s" % (ast.unparse(x.args[0]), x.args[1].value))
                else:
                    out.add("?")
            elif isinstance(x, ast.Name) and x.id not in ("os", "getattr"):
                out.add(x.id)   # anything else is unknown, so it fails
            elif isinstance(x, ast.Constant) and type(x.value) is int and x.value:
                out.add(repr(x.value))   # a number in place of a name (O_WRONLY is 1), so it fails
    return out


class CodeTest(unittest.TestCase):
    def test_only_rpc_and_server_use_the_network(self):
        for path in CODE:
            tree = _tree(path)
            allowed = NETWORK_ALLOWED.get(_name(path), set())
            used = {m for m in _imports(tree) if m in NETWORK or m.split(".")[0] in NETWORK - {"http"}}
            self.assertLessEqual(used, allowed, _name(path))
            # not even through a module another file imported ("import urllib", then urllib.request)
            banned = tuple(m + "." for m in NETWORK - allowed if not any(a.startswith(m + ".") for a in allowed))
            used = {ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                    and ast.unparse(n).startswith(banned)}
            self.assertEqual(used, set(), _name(path))
            used = {name for _, name in _calls(tree) if name.startswith(GI_NETWORK)}
            self.assertEqual(used, set(), _name(path))

    def test_only_call_reaches_the_sender(self):
        """In rpc.py only call() uses _call(), only _call() uses _post() and only _post() uses the
        opener; no other file touches _post or the opener, or makes its own kind of RpcClient."""
        tree = _tree(ROOT / "memstara" / "rpc.py")
        sites = {}
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for n, name in _calls(fn):
                    if name in ("self._call", "self._post"):
                        sites.setdefault(name, []).append(fn.name)
        self.assertEqual(sites, {"self._call": ["call"], "self._post": ["_call"]})
        uses = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) for n in ast.walk(fn)
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and ast.unparse(n) == "self._opener"}
        self.assertEqual(uses, {"_post"}, "only _post() sends through the opener")
        for path in CODE:
            for n in ast.walk(_tree(path)):
                where = "%s:%d" % (_name(path), getattr(n, "lineno", 0))
                if isinstance(n, ast.Attribute) and n.attr in ("_post", "_opener", "_call"):
                    self.assertTrue(isinstance(n.value, ast.Name) and n.value.id == "self", where)
                    self.assertTrue(n.attr == "_call" or _name(path) == "rpc.py", where)   # poller's own _call
                if isinstance(n, ast.ClassDef) and _name(path) != "rpc.py":
                    self.assertNotIn("RpcClient", " ".join(ast.unparse(b) for b in n.bases), where)
                self.assertFalse(isinstance(n, ast.Constant) and n.value in ("_post", "_opener", "_call"), where)

    def test_privacy_only_asks_the_computer_its_name(self):
        """privacy.py imports socket to learn this computer's name, which it hides."""
        tree = _tree(ROOT / "memstara" / "privacy.py")
        used = {ast.unparse(n) for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "socket"}
        self.assertEqual(used, {"socket.gethostname"})
        self.assertEqual({m for m in _imports(tree) if m.startswith("socket")}, {"socket"})   # no "from socket import"
        self.assertNotIn("socket", _local_names(tree).values())                                # no "import socket as s"

    def test_the_window_uses_only_its_libraries(self):
        """GObject libraries: the window's five, so no Soup (network) and the like."""
        for path in CODE:
            got = {m for m in _imports(_tree(path)) if m.startswith("gi.repository.")}
            self.assertLessEqual(got, {"gi.repository." + x for x in ("GLib", "Gdk", "Gio", "Gtk", "WebKit")}, _name(path))

    def test_nothing_runs_another_program(self):
        for path in CODE:
            tree = _tree(path)
            bad = {m for m in _imports(tree) if m in PROGRAMS or m.split(".")[0] in PROGRAMS}
            self.assertEqual(bad, set(), _name(path))
            for n, name in _calls(tree):
                self.assertFalse(name in PROGRAM_CALLS or name.startswith(PROGRAM_PREFIXES),
                                 "%s:%d %s" % (_name(path), n.lineno, name))

    def test_files_opened_for_writing_are_its_own(self):
        """open() with a writing mode only in desktop._write (its two entries) and
        util.atomic_write (settings, history, window size); os.open only ever reads."""
        writers = {"desktop.py": "_write", "util.py": "atomic_write"}
        for path in CODE:
            tree = _tree(path)
            inside = {id(n) for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                      and fn.name == writers.get(_name(path)) for n in ast.walk(fn)}
            for n, name in _calls(tree):
                where = "%s:%d" % (_name(path), n.lineno)
                if name in ("open", "os.fdopen", "io.open"):
                    mode = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "mode"), None)
                    text = mode.value if isinstance(mode, ast.Constant) else ("r" if mode is None else "?")
                    if set(text) & set("wax+?"):
                        self.assertIn(id(n), inside, where)
                elif name == "os.open":
                    flags = _flags(n.args[1], _tree(path))
                    self.assertTrue(flags, where)
                    self.assertLessEqual(flags, READ_FLAGS, where)
                elif name.endswith((".write_text", ".write_bytes")) or (
                        name.endswith(".open") and name not in ("os.open", "self._opener.open")):   # rpc.py's request
                    self.fail("%s writes a file outside util.atomic_write" % where)

    def test_files_moved_or_removed_are_its_own(self):
        """Making, renaming, removing or changing a file's mode only in the functions that keep
        the app's own files: util.ensure_private_dir and util.atomic_write (settings, history,
        window size), desktop._write and desktop._remove (its two entries) and
        settings.Settings.load (a settings file it can't read is kept aside as settings.json.bad).
        GLib only for the few calls the window makes, none of which touches a file."""
        changers = {("util.py", "ensure_private_dir"), ("util.py", "atomic_write"), ("desktop.py", "_write"),
                    ("desktop.py", "_remove"), ("settings.py", "load")}
        glib = {"GLib.set_prgname", "GLib.set_application_name", "GLib.unix_signal_add", "GLib.Variant.new_boolean",
                "GLib.timeout_add_seconds"}
        for path in CODE:
            tree = _tree(path)
            # the innermost function a call is in (ast.walk reaches the outer ones first)
            owner = {id(x): fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) for x in ast.walk(fn)}
            mods = {m.split(".")[0] for m in _imports(tree)}
            self.assertNotIn("shutil", mods, _name(path))
            self.assertNotIn("logging", mods, _name(path))   # FileHandler and basicConfig(filename=) write files
            for n, name in _calls(tree):
                where = "%s:%d %s" % (_name(path), n.lineno, name)
                if name.startswith("GLib."):
                    self.assertIn(name, glib, where)
                if name in FILE_CHANGES or name.startswith("tempfile.") or name.endswith((".unlink", ".rmdir", ".mkdir",
                                                                                          ".touch", ".chmod", ".rename",
                                                                                          ".symlink_to", ".hardlink_to")) \
                        or (name.endswith(".replace") and len(n.args) == 1) \
                        or name.startswith("Gio.File"):   # Path.replace renames; str.replace takes two
                    self.assertIn((_name(path), owner.get(id(n))), changers, where)

    def test_only_its_own_files_use_the_writers(self):
        """util's writers only from settings.py, history.py and window.py (for the files in
        SAFETY.md); desktop.py's _write and _remove only inside desktop.py, for its two entries."""
        users = {"atomic_write": {"settings.py", "history.py", "window.py"},
                 "ensure_private_dir": {"settings.py", "window.py"}, "_write": set(), "_remove": set()}
        for path in CODE:
            if _name(path) in ("util.py", "desktop.py"):
                continue
            tree = _tree(path)
            names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
            names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            for name, allowed in users.items():
                if name in names:
                    self.assertIn(_name(path), allowed, "%s uses %s" % (_name(path), name))
        args = {ast.unparse(n.args[0]) for n, name in _calls(_tree(ROOT / "memstara" / "desktop.py"))
                if name in ("_write", "_remove")}
        self.assertEqual(args, {"path", "p", "autostart_path()"})   # path = entry_path(); p in (entry_path(), autostart_path())


# ---- the screens

# ways a web page can reach the network or leave its own address
WEB_NETWORK = re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts|SharedWorker|"
                         r"RTCPeerConnection)\b|\b(Worker|import)\s*\(|\bwindow\.open\b|"
                         r"\blocation\.(assign|replace)\b|\blocation(\.href)?\s*=[^=]")


class ScreensTest(unittest.TestCase):
    def test_only_api_js_asks_the_server(self):
        """The one fetch() is in web/js/api.js, and every path it's given is the app's own /api/."""
        found = []
        for path in sorted(p for p in (ROOT / "web").rglob("*") if p.suffix in (".js", ".mjs")):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if WEB_NETWORK.search(re.sub(r"(^|\s)//.*", "", line)):   # a comment, not "//" in a string
                    found.append("%s:%d" % (path.relative_to(ROOT), i))
        self.assertEqual(found, ["web/js/api.js:16"])
        api = (ROOT / "web" / "js" / "api.js").read_text(encoding="utf-8")
        paths = re.findall(r'request\("[A-Z]+", (?:after == null \? )?[`"]([^`"?]+)', api)
        self.assertEqual(len(paths), api.count("request(") - 1, "every call's path is written out (one is the definition)")
        for path in paths:
            self.assertTrue(path.startswith("/api/"), path)

    def test_nothing_loads_from_elsewhere(self):
        """No screen file names an address on the internet (SVG's own name tag aside)."""
        for path in sorted((ROOT / "web").rglob("*")):
            if path.suffix in (".js", ".mjs", ".html", ".htm", ".css", ".svg", ".json", ".map", ".webmanifest"):
                # with a scheme, or without one ("//host/x" means the same scheme as the page)
                for url in re.findall(r"\b(?:https?|wss?|ftp)://[^\s\"'`)]+|(?<=[\"'(=`])//[\w-]+\.[\w.-]+",
                                      path.read_text(encoding="utf-8")):
                    self.assertEqual(url, "http://www.w3.org/2000/svg", path.relative_to(ROOT))

    def test_the_window_shows_only_its_own_screens(self):
        """The server tells the window to load and connect to itself only."""
        from memstara.server import CSP
        rules = [r.strip().split(" ", 1) for r in CSP.split(";")]
        names = [name.lower() for name, _ in rules]
        self.assertEqual(len(names), len(set(names)), "a repeated rule: the browser keeps the first")
        self.assertEqual({name.lower(): value for name, value in rules}, {
            "default-src": "'self'", "img-src": "'self' data:", "style-src": "'self'", "script-src": "'self'",
            "connect-src": "'self'", "font-src": "'self'", "frame-ancestors": "'none'", "base-uri": "'none'",
            "form-action": "'none'"})


if __name__ == "__main__":
    unittest.main()
