"""The Monero Research Lab's spy node list, bundled untouched.

third_party/mrl-ban-list/ban_list.txt is a copy of the list published at
github.com/Boog900/monero-ban-list; third_party/README.md names the commit and says how to
check the copy and its signatures. Memstara reads it once at start, read-only, to mark the
connected peers whose address is on it and to count how much of it the node already bans.
It never changes the file, fetches another one or hands it to monerod: banning is
monerod's job (--enable-dns-blocklist bans the same list, or --ban-list with this file).

The file must match SHA256 byte for byte. If it doesn't, or can't be read, the list isn't
used at all and the screens say so.
"""

from __future__ import annotations

import hashlib
import ipaddress

from .util import REPO_DIR

LIST_PATH = REPO_DIR / "third_party" / "mrl-ban-list" / "ban_list.txt"
SHA256 = "07b136bb548b497e8125b48a2a501e933f50a43135cfc605e6e1935b45e5e407"
VERSION = "2"            # the list's first line: "... spy node ban list version 2, 2025-12-10"
DATE = "2025-12-10"
MAX_BYTES = 1 << 20


def parse(text: str):
    """The list's lines -> (addresses, ranges), read as monerod reads a --ban-list file: text
    after a '#' is a comment, blank lines are skipped, and an entry with a '/' is an IPv4 range."""
    hosts, nets = set(), set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            if "/" in line:
                nets.add(ipaddress.IPv4Network(line, strict=False))
            else:
                hosts.add(ipaddress.IPv4Address(line))
        except ValueError:
            continue
    return frozenset(hosts), frozenset(nets)


def _v4(text):
    try:
        return ipaddress.IPv4Address(text)
    except ValueError:
        return None


class SpyList:
    def __init__(self, path=LIST_PATH):
        self.path = path
        self.state, self.hosts, self.nets = self._load()
        self._bans = (None, None)   # the last get_bans answer counted, and its count

    def _load(self):
        try:
            with open(self.path, "rb") as fh:
                data = fh.read(MAX_BYTES + 1)
        except OSError:
            return "missing", frozenset(), frozenset()
        if hashlib.sha256(data).hexdigest() != SHA256:
            return "changed", frozenset(), frozenset()
        hosts, nets = parse(data.decode("ascii"))
        return "ok", hosts, nets

    def on_list(self, ip: str) -> bool:
        """Whether an IPv4 address (as get_connections writes it) is on the list or in one of its ranges."""
        a = _v4(ip) if self.state == "ok" else None
        if a is None:
            return False
        return a in self.hosts or any(a in n for n in self.nets)

    def mark(self, peers: list) -> None:
        """Sets `spy` on each peer of adapter.parse_connections: True when its address is on the
        list, None when the list isn't used (changed or missing: not known either way)."""
        for p in peers:
            p["spy"] = (p.get("kind") == "ipv4" and self.on_list(p.get("ip") or "")) if self.state == "ok" else None

    def banned_by_node(self, res) -> int | None:
        """How many of the list's entries get_bans shows the node banning: an address banned
        itself or inside a banned range, a range banned itself or inside a wider one."""
        bans = res.get("bans") if isinstance(res, dict) else None
        if self.state != "ok" or res is None:
            return None
        if self._bans[0] is res:
            return self._bans[1]   # the same answer as last time (it's read once a minute, polled every second)
        if not isinstance(bans, list):
            bans = []          # monerod leaves an empty list out
        hosts, nets = set(), []
        for b in bans:
            host = b.get("host") if isinstance(b, dict) else None
            if not isinstance(host, str):
                continue
            if "/" in host:
                try:
                    nets.append(ipaddress.IPv4Network(host, strict=False))
                except ValueError:
                    pass
            else:
                a = _v4(host)
                if a is not None:
                    hosts.add(a)
        # the banned ranges by prefix length, as their network bits: an address or a range is
        # inside one when its own first bits are there (a few lookups each, however many bans)
        by_len = {}
        for w in nets:
            by_len.setdefault(w.prefixlen, set()).add(int(w.network_address) >> (32 - w.prefixlen))

        def inside(addr: int, plen: int) -> bool:
            return any(ln <= plen and (addr >> (32 - ln)) in bits for ln, bits in by_len.items())

        n = sum(1 for h in self.hosts if h in hosts or inside(int(h), 32))
        n += sum(1 for r in self.nets if inside(int(r.network_address), r.prefixlen))
        self._bans = (res, n)
        return n

    def part(self, peers: list | None, bans_res) -> dict:
        """The snapshot's peers.spy. peers: the connections with `spy` set (None when they
        couldn't be read); bans_res: the last get_bans answer (None when it couldn't be read)."""
        out = inn = None
        if peers is not None and self.state == "ok":
            out = sum(1 for p in peers if p.get("spy") and p.get("dir") == "out")
            inn = sum(1 for p in peers if p.get("spy") and p.get("dir") != "out")
        return {"state": self.state, "version": VERSION, "date": DATE,
                "entries": len(self.hosts) + len(self.nets), "ranges": len(self.nets),
                "node_bans": self.banned_by_node(bans_res), "out": out, "in": inn}
