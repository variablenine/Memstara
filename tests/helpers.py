"""Shared bits for the tests (test-only)."""

from __future__ import annotations

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

# never touch the real ~/.config, ~/.local/state or ~/.cache from a test
_SANDBOX = tempfile.mkdtemp(prefix="memstara-tests-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_SANDBOX, "config")
os.environ["XDG_STATE_HOME"] = os.path.join(_SANDBOX, "state")
os.environ["XDG_CACHE_HOME"] = os.path.join(_SANDBOX, "cache")

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
