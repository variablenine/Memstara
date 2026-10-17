"""Ties the pieces together: settings, the poller thread and the local server that feeds
the app window."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from . import host
from .commands import Commands
from .poller import Poller, test_connection
from .privacy import Redactor
from .server import DashboardServer, build_static_map
from .util import WEB_DIR, state_dir as default_state_dir


class App:
    def __init__(self, settings, *, host: str = "127.0.0.1", port: int | None = None,
                 web_dir: Path | str = WEB_DIR, state_dir: Path | str | None = None,
                 clock=time.time, log=None, host_reader=None, discover=None, node_log=None,
                 key: str | None = None):
        """host_reader, discover and node_log replace the local reads (tests).
        key: the window key (see server.py); the app window gets it in window_url."""
        self.settings = settings
        self.clock = clock
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        sdir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.poller = Poller(settings, clock=clock, state_dir=sdir, log=self.log,
                             host_reader=host_reader, discover=discover, node_log=node_log)
        self.commands = Commands(settings, self.poller, self.log)
        self.web_dir = Path(web_dir)
        self.static = build_static_map(self.web_dir)
        self.privacy = Redactor()   # privacy mode's tokens (server.py uses it while display.privacy is on)
        self.key = key
        self.httpd = DashboardServer(self, host, settings.get("server.port") if port is None else port, key=key)
        self._thread = None
        settings.add_listener(self._settings_changed)

    @property
    def port(self) -> int:
        return self.httpd.port

    @property
    def url(self) -> str:
        h = self.httpd.bind_host
        if h in ("0.0.0.0", "::", ""):
            h = "127.0.0.1"
        elif ":" in h:
            h = "[%s]" % h.strip("[]")
        return "http://%s:%d/" % (h, self.port)

    @property
    def window_url(self) -> str:
        """The address the app window opens: url plus the window key."""
        return self.url + ("?key=" + self.key if self.key else "")

    def start(self, poll: bool = True):
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="memstara-http", daemon=True)
        self._thread.start()
        if poll:
            self.poller.start()
        return self

    def stop(self):
        self.poller.stop()
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()

    def _settings_changed(self, changed):
        shown = [k for k in changed if not k.startswith(("display.", "panels."))]
        if "display.privacy" in changed:
            on = self.settings.get("display.privacy") is True
            self.poller.add_event("settings", "Privacy mode %s" % ("on" if on else "off"), "info")
        if shown:
            self.poller.add_event("settings", "Settings changed: " + ", ".join(sorted(shown)), "info")
        self.poller.wake()   # takes effect on the next poll, which starts now

    def test_connection(self, url, user, password):
        return test_connection(url, user, password, self.settings.get("source.timeout_s"))


def find_node_on_first_run(settings, discover=host.discover, log=None):
    """First run (the settings file has no source.rpc_url yet): when exactly one running
    monerod has an unrestricted RPC port, save its address and log file. Returns the
    monerod used, or None (the defaults stay; FIND MY NODE in Settings lists what's there)."""
    log = log or (lambda msg: print(msg, file=sys.stderr))
    if "source.rpc_url" in settings.file_keys:
        return None
    try:
        found = discover().get("found") or []
    except Exception:
        return None
    usable = [f for f in found if f.get("rpc_url") and not f.get("restricted")]
    if len(usable) != 1:
        return None
    node = usable[0]
    values = {"source.rpc_url": node["rpc_url"]}
    if node.get("log_file") and not settings.validate("node.log_path", node["log_file"])[1]:
        values["node.log_path"] = node["log_file"]
    try:
        settings.update(values)
    except Exception as e:   # a strange address: keep the defaults
        log("Found monerod (pid %s) but couldn't use its settings: %s" % (node.get("pid"), type(e).__name__))
        return None
    log("Found monerod (pid %s): reading %s%s" % (node.get("pid"), node["rpc_url"],
                                                   " (it needs the RPC login in Settings)"
                                                   if node.get("login_needed") else ""))
    return node
