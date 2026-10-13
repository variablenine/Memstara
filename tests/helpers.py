"""Shared bits for the tests (test-only)."""

from __future__ import annotations

import copy
import http.client
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for p in (str(ROOT), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

# never touch the real ~/.config or ~/.local/state from a test
_SANDBOX = tempfile.mkdtemp(prefix="memstara-tests-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_SANDBOX, "config")
os.environ["XDG_STATE_HOME"] = os.path.join(_SANDBOX, "state")

T0 = 1791230700.0   # 2026-10-05: the made-up "now" the fixtures' times are set around


class FakeClock:
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s
        return self.t


def fixture(name):
    with open(TESTS / "fixtures" / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def closed_port() -> int:
    """A port with nothing listening (bound then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def request(port, method, path, body=None, headers=None, host=None, raw_body=None):
    """One HTTP request to the dashboard. Returns (status, headers dict, body bytes)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host if host is not None else "127.0.0.1:%d" % port}
    h.update(headers or {})
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items():
        if v is not None:
            c.putheader(k, v)
    if data is not None:
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data)
    r = c.getresponse()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
    c.close()
    return out


def get_json(port, path, **kw):
    st, hd, body = request(port, "GET", path, **kw)
    return st, json.loads(body) if body else None


WRITE = {"X-Memstara": "1", "Content-Type": "application/json"}


# ---- stand-ins for the local reads (host.HostReader and host.discover), so tests never
# depend on the machine they run on

HOST_READING = {
    "ok": True, "error": None, "every_s": 10,
    "proc": {"pid": 4242, "name": "monerod", "exe": "/usr/local/bin/monerod", "exe_replaced": False,
             "started": int(T0) - 100000, "cpu_pct": 7.5, "rss_bytes": 1900000000, "threads": 41,
             "open_files": 380, "read_bytes": 123, "write_bytes": 456, "read_rate": 1000, "write_rate": 2000,
             "flags": ["--p2p-bind-ip", "33.44.7.7", "--tx-proxy", "tor,127.0.0.1:9050,16"]},
    "proc_error": None,
    "computer": {"cpu_temp_c": 61.0, "on_ac": True, "battery_pct": 87, "battery_state": "Charging",
                 "load1": 0.8, "cores": 8, "mem_used_bytes": 6000000000, "mem_total_bytes": 16000000000,
                 "uptime_s": 99999, "disk": {"device": "nvme0n1", "read_rate": 0, "write_rate": 0, "busy_pct": 3.0}},
    "bind_if": None,
    "tor": {"socks_port": 9050, "socks_listening": True},
}


class StubHost:
    """Stands in for host.HostReader: answers a copy of `reading` and gives every clearnet
    peer the same round trip. Records each read as (now, rpc_url, data_dir, every_s)."""

    def __init__(self, reading=None, rtt_ms=42.0):
        self.reading = copy.deepcopy(HOST_READING if reading is None else reading)
        self.rtt_ms = rtt_ms
        self.reads = []
        self.rpc_url = self.data_dir = self.every_s = None

    def read(self, now, clock=None):
        self.reads.append((now, self.rpc_url, self.data_dir, self.every_s))
        self.clock = clock
        out = copy.deepcopy(self.reading)
        out["every_s"] = self.every_s
        out["t"] = round(now, 3)
        return out

    def annotate(self, peers):
        for p in peers:
            on = p.get("kind") in ("ipv4", "ipv6") and self.rtt_ms is not None
            p["rtt_ms"] = self.rtt_ms if on else None
            p["retrans"] = 0 if on else None
        return peers


class StubDiscover:
    """Stands in for host.discover: answers `found` (a list of discover() entries)."""

    def __init__(self, found=()):
        self.found = [dict(f) for f in found]
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return {"ok": True, "error": None if self.found else "No monerod is running under your user",
                "found": copy.deepcopy(self.found)}


def found_node(url, **kw):
    """One discover() entry for the node at `url`, shaped like host.discover's."""
    return dict({"pid": 4242, "exe": "/usr/local/bin/monerod", "started": int(T0) - 100000, "nettype": "mainnet",
                 "rpc_url": url, "restricted": False, "login_needed": False, "restricted_rpc_url": None,
                 "data_dir": "/home/u/.bitmonero", "log_file": "/home/u/.bitmonero/bitmonero.log",
                 "p2p_bind": [], "tx_proxy": False, "tx_proxy_tor": False, "out_peers": None}, **kw)
