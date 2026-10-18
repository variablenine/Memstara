"""Privacy mode: what the window is sent while the screens are being shown to other people.

With display.privacy on, every /api/ answer passes through Redactor on its way out
(server.py), so nothing private ever reaches the window. Each private value becomes a
token, ⟦kind_hhhhhhhh⟧: what kind of thing it was ("ip", "port", "host", ...) and the
first 8 hex digits of an HMAC-SHA256 of the value, keyed with 32 random bytes made when
the app starts and never sent anywhere. The same value gets the same token for as long as
the app runs, so the screens can still tell peers apart, count a range's peers and follow
a waiting transaction into its block, but a token can't be turned back into its value.
The window draws tokens as glitching glyphs (web/js/privacy.js).

Private here means what could point at you, this computer or your node:
- IP addresses (IPv4 and IPv6, but not loopback or 0.0.0.0) and address ranges
  ("1.2.x.x", "1.2.3.0/24", and the net16/net24 keys), onion and I2P addresses, MAC
  addresses, the login in a URL, and a remote node's name in an RPC address;
- ports: every port key, "port 47213" in text, and a port written after a hidden address
  ("1.2.3.4:47213") or after 0.0.0.0 or :: ("0.0.0.0:47213", "[::]:47213": where monerod
  listens, so your node's own p2p port; the address itself is shown). Loopback addresses
  and their ports are shown too (the RPC address on this computer, 127.0.0.1:18081);
- this computer's name, your user name and your real name (as whole words), and your
  home folder (shown as ~);
- Monero addresses, transaction ids and key images (any run of 64 or more hex digits, so a
  whole transaction a log line prints goes too, except the block hashes the chain screens
  are made of), and peer ids.
A waiting transaction your node hasn't passed on yet is left out of the mempool list
(nobody else has it, so it could be yours) and the count of them is dropped.

Every string in an answer goes through the patterns, so a field added later still has its
addresses, names and paths caught; the key rules only cover what a pattern can't
recognise on its own (a port number, a range written "1.2", a peer id).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

OPEN, CLOSE = "\u27e6", "\u27e7"
TOKEN_RE = re.compile(OPEN + r"[a-z0-9]{1,6}_[0-9a-f]{8}" + CLOSE)
REVEAL_WORD = "SHOW"      # typed to turn privacy mode off (server.py checks it)

FLAG_NOT_RELAYED = 2      # pool.py: the node hasn't passed the transaction on yet

# keys (the last part of a dotted settings key counts too, see _str) and what they hold
PORT_KEYS = frozenset({"port", "p2p_port", "rpc_port", "local_port", "remote_port"})
NET_KEYS = frozenset({"net", "net16", "net24"})          # "1.2" (a /16) or "1.2.3" (a /24)
PEER_ID_KEYS = frozenset({"peer_id"})
USER_KEYS = frozenset({"rpc_user", "user", "username"})
URL_KEYS = frozenset({"rpc", "bootstrap_daemon_address", "bootstrap"})   # and any key ending in "url"
# 64-hex values left as they are: block hashes, public, what the chain screens are made of
BLOCK_HASH_KEYS = frozenset({"hash", "top_hash", "top_block_hash", "prev_hash", "block_hash", "prev_id",
                             "main_chain_parent_block", "block_hashes"})

# words too common to hide everywhere as a name (as a user name, the home folder still goes)
GENERIC_NAMES = frozenset({
    "root", "user", "users", "admin", "administrator", "guest", "test", "home", "ubuntu", "debian",
    "fedora", "arch", "linux", "localhost", "computer", "desktop", "laptop", "server", "node", "monero",
    "monerod", "xmr", "pi", "raspberrypi", "default", "none", "null", "unknown", "nobody", "daemon",
    "local", "main", "host", "machine", "workstation", "box", "pc", "the", "and", "you", "your",
})

_B58 = "1-9A-HJ-NP-Za-km-z"
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_NET16 = re.compile(r"\d{1,3}\.\d{1,3}")
_NET24 = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")

# the fixed patterns, in the order they're tried at each place in a text. A token already
# there is passed over whole, so nothing is ever found inside one.
_FIXED = [
    ("tok", OPEN + r"[a-z0-9]{1,6}_[0-9a-f]{8}" + CLOSE),
    ("login", r"(?<=://)[^\s/@:]+(?::[^\s/@]*)?@"),
    ("onion", r"(?<![a-z2-7])(?:[a-z2-7]{56}|[a-z2-7]{16})\.onion(?![\w-])"),
    ("i2p", r"(?<![\w.-])[a-z0-9][a-z0-9.-]*\.i2p(?![\w-])"),
    ("addr", r"(?<![%s])[%s]{95}(?:[%s]{11})?(?![%s])" % (_B58, _B58, _B58, _B58)),
    ("hex", r"(?<![0-9a-fA-F])[0-9a-fA-F]{64,}(?![0-9a-fA-F])"),   # an id, a key, or a whole transaction
    ("mac", r"(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}(?![0-9a-fA-F:])"),
    ("v6b", r"\[[0-9a-fA-F:.]+(?:%[\w.-]+)?\](?::\d{1,5}(?!\d))?"),
    ("v4", r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2}(?!\d)|:\d{1,5}(?!\d))?(?!\d|\.\d)"),
    ("v4x", r"(?<![\w.])\d{1,3}\.\d{1,3}(?:\.\d{1,3}\.[x*]|\.[x*]\.[x*])(?!\w|\.\w)"),
    ("v6", r"(?<![\w:.])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F.]*(?:%[\w.-]+)?(?::\d{1,5})?(?![\w:])"),
    # "port 47213", and monerod's "External port defined as 47213"
    ("port", r"(?i:\bports?\b(?: defined as)?)[ \t:#=]{0,3}\d{2,5}"
             r"(?:[ \t]*(?:,|\band\b)[ \t]*\d{2,5}|[ \t]+\d{4,5})*(?!\w|\.\d)"),
]
_DIGITS = re.compile(r"\d{2,5}")


def _ok_port(v) -> str | None:
    """A port number as text ("47213"), or None."""
    s = str(v).strip()
    if s.isdigit() and 0 < int(s) < 65536:
        return str(int(s))
    return None


def _unspecified(text: str) -> bool:
    """Whether an address is 0.0.0.0 or :: (also written ::ffff:0.0.0.0): every address
    this computer has, as a program listening on all of them writes it."""
    try:
        a = ipaddress.ip_address(text)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return a.is_unspecified


def _user_info():
    """(home folder, user name, real-name words) of the user the app runs as."""
    home = os.path.expanduser("~")
    user, real = "", ""
    try:
        import pwd
        pw = pwd.getpwuid(os.getuid())
        user, real = pw.pw_name or "", (pw.pw_gecos or "").split(",")[0]
    except (ImportError, KeyError, OSError):
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    return home, user, real


class Redactor:
    """Swaps private values in API answers for tokens. Thread-safe: the server answers
    several requests at once."""

    def __init__(self, *, home: str | None = None, user: str | None = None, real_name: str | None = None,
                 host_name: str | None = None, key: bytes | None = None):
        h, u, r = _user_info() if None in (home, user, real_name) else ("", "", "")
        self.home = (home if home is not None else h).rstrip("/")
        self.user = user if user is not None else u
        real = real_name if real_name is not None else r
        host = host_name if host_name is not None else socket.gethostname()
        self._key = key or secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._names = {}                  # whole words hidden everywhere: word -> kind (the first one given)
        for word in (host, host.split(".", 1)[0]):
            self._add_name("host", word)
        self._add_name("user", self.user)
        for word in re.split(r"[\s,]+", real or ""):
            self._add_name("name", word)
        self._cache = None                # (snapshot dict, its redacted JSON)
        self._compile()

    def _add_name(self, kind, word):
        w = (word or "").strip()
        if len(w) >= 3 and w.lower() not in GENERIC_NAMES and re.fullmatch(r"[\w.-]+", w):
            self._names.setdefault(w.lower(), kind)

    def _compile(self):
        parts = list(_FIXED)
        if self.home and self.home not in ("", "/"):
            parts.insert(1, ("home", re.escape(self.home) + r"(?=/|$|[^\w.-])"))
        if self._names:
            alts = "|".join(re.escape(w) for w in sorted(self._names, key=len, reverse=True))
            parts.append(("word", r"(?i:(?<![\w.-])(?:%s)(?![\w-]))" % alts))
        self._rx = re.compile("|".join("(?P<%s>%s)" % p for p in parts))

    # ------------------------------------------------------------------ tokens
    def token(self, kind: str, value) -> str:
        v = str(value).strip()
        if kind in ("host", "user", "name", "tor", "i2p", "hex", "tx", "mac", "id"):
            v = v.lower()
        digest = hmac.new(self._key, ("%s\0%s" % (kind, v)).encode("utf-8", "surrogatepass"), hashlib.sha256)
        return "%s%s_%s%s" % (OPEN, kind, digest.hexdigest()[:8], CLOSE)

    def _ip(self, text: str) -> str | None:
        """The token for one IP address, or None when it isn't one or is fine to show."""
        try:
            a = ipaddress.ip_address(text)
        except ValueError:
            return None
        if a.version == 6 and a.ipv4_mapped is not None:
            a = a.ipv4_mapped
        if a.is_loopback or a.is_unspecified or (a.version == 4 and a.packed[0] == 0):
            return None
        return self.token("ip" if a.version == 4 else "ip6", a.compressed)

    def _port_after(self, port: str) -> str:
        """The port written after a hidden address ("1.2.3.4:47213"), or after 0.0.0.0 or ::
        ("0.0.0.0:47213", where monerod listens on its own p2p port): hidden too."""
        p = _ok_port(port)
        return self.token("port", p) if p else port

    def _v4(self, m: str) -> str:
        addr, colon, port = m.partition(":")
        if colon:
            if _unspecified(addr):
                return addr + ":" + self._port_after(port)
            tok = self._v4(addr)
            return m if tok == addr else tok + ":" + self._port_after(port)
        addr, _, prefix = m.partition("/")
        try:
            octets = [int(o) for o in addr.split(".")]
        except ValueError:
            return m
        if any(o > 255 for o in octets) or octets[0] in (0, 127):
            return m                       # a version number ("0.18.5.3") or loopback
        addr = ".".join(str(o) for o in octets)     # "203.0.113.09" as 203.0.113.9
        if not prefix:
            return self._ip(addr) or m
        bits = int(prefix)
        if bits > 32:
            return m
        net = ipaddress.ip_network("%s/%d" % (addr, bits), strict=False)
        o = str(net.network_address).split(".")
        if bits == 16:
            return self.token("n16", ".".join(o[:2])) + ".0.0/16"
        if bits == 24:
            return self.token("n24", ".".join(o[:3])) + ".0/24"
        if bits == 32:
            return self.token("ip", addr) + "/32"
        return self.token("net", net.with_prefixlen)

    def _v4x(self, m: str) -> str:
        parts = m.split(".")
        if any(int(p) > 255 for p in parts if p.isdigit()) or parts[0] in ("0", "127"):
            return m
        if parts[2] in ("x", "*"):
            return self.token("n16", ".".join(parts[:2])) + "." + ".".join(parts[2:])
        return self.token("n24", ".".join(parts[:3])) + "." + parts[3]

    def _v6(self, m: str) -> str:
        # a sentence's full stop or a stray colon can end up on the end of the match
        for core in (m.rstrip("."), m.rstrip(".:")):
            addr, _, zone = core.partition("%")
            tok = ":" not in zone and self._ip(addr)     # a port after the zone: below
            if tok:
                return tok + m[len(core):]
        # an address and a port without brackets, as monerod writes where it listens over IPv6
        # ("2001:db8::5:47213", or ":::47213" on every address)
        core = m.rstrip(".")
        addr, _, port = core.rpartition(":")
        if _ok_port(port):
            if _unspecified(addr):
                return addr + ":" + self._port_after(port) + m[len(core):]
            tok = self._ip(addr.partition("%")[0])
            if tok:
                return tok + ":" + self._port_after(port) + m[len(core):]
        return m

    def _sub(self, m: re.Match) -> str:
        kind, s = m.lastgroup, m.group()
        if kind == "tok":
            return s
        if kind == "home":
            return "~"
        if kind == "login":
            return self.token("login", s) + "@"
        if kind in ("onion", "i2p"):
            return self.token("tor" if kind == "onion" else "i2p", s)
        if kind == "addr":
            return self.token("addr", s)
        if kind == "hex":
            return self.token("hex", s)
        if kind == "mac":
            return self.token("mac", s)
        if kind == "v6b":
            end = s.rindex("]")
            inner, port = s[1:end], s[end + 2:]
            red = self._v6(inner)
            if red == inner and not (port and _unspecified(inner)):
                return s
            return "[" + red + "]" + (":" + self._port_after(port) if port else "")
        if kind == "v4":
            return self._v4(s)
        if kind == "v4x":
            return self._v4x(s)
        if kind == "v6":
            return self._v6(s)
        if kind == "port":
            return _DIGITS.sub(lambda d: self.token("port", _ok_port(d.group()) or d.group()), s)
        if kind == "word":
            low = s.lower()
            return self.token(self._names.get(low, "name"), low)
        return s

    def scrub(self, text: str) -> str:
        """One text with everything private in it swapped for tokens."""
        if not text:
            return text
        return self._rx.sub(self._sub, text)

    # ------------------------------------------------------------------ learning
    def learn(self, snap: dict):
        """Notes the node's computer by name when a snapshot's RPC address names one
        ("node.lan:18081"), so it's hidden everywhere, as this computer's own name is
        (addresses go by the address patterns)."""
        rpc = (snap.get("source") or {}).get("rpc") if isinstance(snap, dict) else None
        if not isinstance(rpc, str):
            return
        try:
            h = urlsplit("//" + rpc).hostname or ""
        except ValueError:
            return
        try:
            ipaddress.ip_address(h)
            return                        # an address: the address patterns hide it
        except ValueError:
            pass
        if not h or h == "localhost":
            return
        with self._lock:
            n_names = len(self._names)
            self._add_name("host", h)
            if len(self._names) != n_names:
                self._compile()

    # ------------------------------------------------------------------ answers
    def redact(self, obj):
        """A redacted copy of any JSON-shaped answer (the original is left as it is)."""
        return self._walk(obj, None, ())

    def snapshot_json(self, snap: dict) -> bytes:
        """The snapshot as the window gets it in privacy mode, kept for repeat asks."""
        with self._lock:
            c = self._cache
        if c is not None and c[0] is snap:
            return c[1]
        self.learn(snap)
        out = self.redact(snap)
        if isinstance(out, dict):
            if not str(snap.get("clock_style") or "").endswith("-utc"):
                # made just before privacy mode came on, so its clock times are still local
                now = time.time()
                out = _map_strings(out, lambda s: utc_clocks(s, now))
            out["privacy"] = True
        data = json.dumps(out, separators=(",", ":")).encode()
        with self._lock:
            self._cache = (snap, data)
        return data

    def settings(self, public: dict) -> dict:
        """GET /api/settings: the values with paths, addresses and logins hidden, by the
        type each setting has in the schema; the schema itself is fixed text."""
        out = dict(public)
        types = {e.get("key"): e.get("type") for e in public.get("schema") or [] if isinstance(e, dict)}
        vals = {}
        for k, v in (public.get("values") or {}).items():
            t = types.get(k)
            if not isinstance(v, str) or not v:
                vals[k] = v
            elif t == "password":
                vals[k] = ""
            elif t == "url":
                vals[k] = self._url(v)
            elif k.rsplit(".", 1)[-1] in USER_KEYS:
                vals[k] = self.token("user", v)
            elif t in ("text", "path"):
                vals[k] = self.scrub(v)
            else:
                vals[k] = v
        out["values"] = vals
        out["privacy"] = True
        return out

    # ------------------------------------------------------------------ the walk
    def _walk(self, v, key, path):
        if isinstance(v, dict):
            if path and path[-1] == "mempool" and isinstance(v.get("list"), list):
                return self._mempool(v, path)
            return self._dict(v, path)
        if isinstance(v, list):
            if key in BLOCK_HASH_KEYS:
                return [x if isinstance(x, str) and _HEX64.fullmatch(x) else self._walk(x, key, path) for x in v]
            return [self._walk(x, key, path) for x in v]
        if isinstance(v, str):
            return self._str(key, v)
        if isinstance(v, bool) or not isinstance(v, int):
            return v
        return self._int(key, v)

    def _dict(self, d, path):
        peerish = any(k in d for k in ("host", "ip", "address"))
        out = {}
        for k, x in d.items():
            if k == "id" and peerish and isinstance(x, int) and not isinstance(x, bool):
                out[k] = self.token("id", x)          # get_peer_list's peer id
            elif k in BLOCK_HASH_KEYS and isinstance(x, str) and _HEX64.fullmatch(x):
                out[k] = x
            else:
                out[k] = self._walk(x, k, path + (k,))
        # an event ({t, kind, text}) keeps the text it was written with, local clock times and
        # all ("Node restarted at 9:30 AM"): those go to UTC like every other clock here
        t = d.get("t")
        if "kind" in d and isinstance(out.get("text"), str) and isinstance(t, (int, float)) and not isinstance(t, bool):
            out["text"] = utc_clocks(out["text"], t)
        return out

    def _mempool(self, m, path):
        out = self._dict({k: v for k, v in m.items() if k not in ("list", "mined")}, path)
        rows = []
        for e in m.get("list") or []:
            if not isinstance(e, list) or not e:
                continue
            flags = e[4] if len(e) > 4 and isinstance(e[4], int) else 0
            if flags & FLAG_NOT_RELAYED:
                continue                       # only this node has it: it could be yours
            row = [self.token("tx", e[0])] + list(e[1:])
            if len(row) > 4:
                row[4] = flags & ~FLAG_NOT_RELAYED
            rows.append(row)
        out["list"] = rows
        if "mined" in m:
            out["mined"] = [{**self._dict({k: v for k, v in b.items() if k != "ids"}, path),
                             "ids": [self.token("tx", i) for i in b.get("ids") or []]}
                            if isinstance(b, dict) else b for b in m.get("mined") or []]
        if "not_relayed" in out:
            out["not_relayed"] = None
        return out

    def _str(self, key, s):
        base = key.rsplit(".", 1)[-1] if isinstance(key, str) else None
        if not s or base is None:
            return self.scrub(s)
        if base in NET_KEYS:
            if _NET16.fullmatch(s):
                return self.token("n16", s)
            if _NET24.fullmatch(s):
                return self.token("n24", s)
        elif base in PORT_KEYS and "." not in key:
            p = _ok_port(s)
            if p:
                return self.token("port", p)
        elif base in PEER_ID_KEYS:
            return self.token("id", s.strip())
        elif base in USER_KEYS:
            return self.token("user", s.strip())
        elif base in URL_KEYS or base.endswith("url"):
            return self._url(s)
        return self.scrub(s)

    def _int(self, key, n):
        base = key.rsplit(".", 1)[-1] if isinstance(key, str) else None
        if base in PORT_KEYS and "." not in key and 0 < n < 65536:
            return self.token("port", n)
        if base == "ip" and 0 < n < 2 ** 32:
            # monerod's uint32 form: the first byte of the address is the lowest
            text = ".".join(str((n >> s) & 255) for s in (0, 8, 16, 24))
            return self._ip(text) or n
        if base in PEER_ID_KEYS:
            return self.token("id", n)
        return n

    def _url(self, s: str) -> str:
        """An RPC address: a host name other than this computer's goes, an address too."""
        try:
            parts = urlsplit(s if "://" in s else "//" + s)
            host = parts.hostname or ""
        except ValueError:
            return self.scrub(s)
        if host and host != "localhost":
            try:
                ipaddress.ip_address(host)
            except ValueError:
                # a name: swap it (whole, wherever it is in the address) before the patterns
                tok = self.token("host", host)
                s = re.sub(r"(?i)(?<![\w.-])%s(?![\w.-])" % re.escape(host), tok, s)
        return self.scrub(s)


# a time of day util.clock_text wrote in local time ("9:30 AM", "14:05"), not one already in UTC
_CLOCK12 = re.compile(r"(?<![\d:.])(1[0-2]|[1-9]):([0-5]\d) ([AP]M)(?! UTC)")
_CLOCK24 = re.compile(r"(?<![\d:.])([01]\d|2[0-3]):([0-5]\d)(?![\d:]| UTC| [AP]M)")


def utc_clocks(text: str, t: float) -> str:
    """The local times of day in an event's text, as UTC (on a computer two hours ahead of UTC,
    "at 9:30 AM" -> "at 7:30 AM UTC"), by this computer's time zone at the event's time `t`."""
    try:
        off = time.localtime(t).tm_gmtoff // 60
    except (OverflowError, OSError, ValueError):
        return text

    def utc(h, m):
        return divmod((h * 60 + m - off) % 1440, 60)

    def twelve(mo):
        h, m = utc(int(mo.group(1)) % 12 + (12 if mo.group(3) == "PM" else 0), int(mo.group(2)))
        return "%d:%02d %s UTC" % (h % 12 or 12, m, "AM" if h < 12 else "PM")

    def twenty_four(mo):
        return "%02d:%02d UTC" % utc(int(mo.group(1)), int(mo.group(2)))

    return _CLOCK24.sub(twenty_four, _CLOCK12.sub(twelve, text))


# a date and time written in local time ("2025-06-01 09:30:00"), as a line some other program
# wrote into the log may carry;
# monerod's own lines carry fractions of a second ("09:30:00.100") and
# are UTC already (logevents.py), so those are left alone
_STAMP = re.compile(r"(?<![\d-])(\d{4})-(\d\d)-(\d\d)([ T])(\d\d):(\d\d)(?::(\d\d))?(?![\d.,:]| ?(?:UTC|GMT|Z\b|[+-]\d\d:?\d\d))")


def utc_stamps(text: str) -> str:
    """A log line's local dates and times as UTC (two hours ahead of UTC, "2025-06-01 09:30:00"
    -> "2025-06-01 07:30:00 UTC"), by this computer's time zone at that moment."""
    def conv(m):
        y, mon, d, h, mi = (int(m.group(i)) for i in (1, 2, 3, 5, 6))
        sec = m.group(7)
        try:
            t = time.mktime((y, mon, d, h, mi, int(sec or 0), 0, 0, -1))
            g = time.gmtime(t)
        except (OverflowError, OSError, ValueError):
            return m.group(0)
        out = "%04d-%02d-%02d%s%02d:%02d" % (g.tm_year, g.tm_mon, g.tm_mday, m.group(4), g.tm_hour, g.tm_min)
        return out + (":%02d" % g.tm_sec if sec is not None else "") + " UTC"
    return _STAMP.sub(conv, text) if text else text


def _map_strings(v, fn):
    if isinstance(v, dict):
        return {k: _map_strings(x, fn) for k, x in v.items()}
    if isinstance(v, list):
        return [_map_strings(x, fn) for x in v]
    return fn(v) if isinstance(v, str) else v


def has_token(value) -> bool:
    """Whether a value from the window (a setting, a command's argument) has a hidden value
    in it; those are refused, since the window doesn't know what they stand for."""
    if isinstance(value, str):
        return OPEN in value
    if isinstance(value, dict):
        return any(has_token(k) or has_token(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_token(v) for v in value)
    return False
