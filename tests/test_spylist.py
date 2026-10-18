"""The bundled MRL spy node list (memstara/spylist.py) and the files in third_party/."""

from __future__ import annotations

import calendar
import hashlib
import ipaddress
import os
import random
import tempfile
import unittest
from pathlib import Path

import helpers
from memstara import spylist
from memstara.lamps import compute as compute_lamps
from memstara.spylist import SpyList

THIRD_PARTY = helpers.ROOT / "third_party"
# every file in third_party/, with the sha256 of the copy at the upstream commit that
# third_party/README.md names: a file added, removed or changed there fails this test
BUNDLED = {
    "README.md": None,   # Memstara's own notes on where the files come from
    "mrl-ban-list/ban_list.txt": "07b136bb548b497e8125b48a2a501e933f50a43135cfc605e6e1935b45e5e407",
    "mrl-ban-list/sigs/Rucknium.sig": "baaac861a8d5279e749f79fe514fb82916cd38fd42664a102cdd3c09d8de31aa",
    "mrl-ban-list/sigs/boog900.sig": "a666b76b67e4220a043ac983ee8503cf0f1620dec15b3c4800847e9cc751d8f8",
    "mrl-ban-list/sigs/hinto-janai.sig": "9ddc84b369e05d9b78659f1069c25e3cdd9baae1ee523707f4c4d5ee240898bc",
    "mrl-ban-list/sigs/jeffro256.sig": "2828dc8b77081664e0227a780a0592328f64338f88c0dac7d7adbed6422e7435",
    "mrl-ban-list/sigs/syntheticbird.sig": "cc693f07ed2e91181daf0289d5f5aa6ed567cb6e9e9d95dd6ad8f111fe6b065a",
}


def stand_in(hosts=(), nets=()):
    """A SpyList holding made-up entries, for tests that need peers on the list."""
    s = SpyList.__new__(SpyList)
    s.path, s.state = None, "ok"
    s.hosts = frozenset(ipaddress.IPv4Address(h) for h in hosts)
    s.nets = frozenset(ipaddress.IPv4Network(n) for n in nets)
    s._bans = (None, None)
    return s


class BundledTest(unittest.TestCase):
    def test_third_party_holds_only_the_files_named(self):
        found = sorted(p.relative_to(THIRD_PARTY).as_posix() for p in THIRD_PARTY.rglob("*") if p.is_file())
        self.assertEqual(found, sorted(BUNDLED))
        for name, want in BUNDLED.items():
            if want:
                self.assertEqual(hashlib.sha256((THIRD_PARTY / name).read_bytes()).hexdigest(), want, name)

    def test_the_readme_names_every_hash(self):
        text = (THIRD_PARTY / "README.md").read_text(encoding="utf-8")
        for name, want in BUNDLED.items():
            if want:
                self.assertIn(want, text, name)
        self.assertIn("22ddf399a8dc7cbfc33519b2affafaca31b2510b", text)

    def test_the_pinned_hash_is_the_bundled_file(self):
        self.assertEqual(spylist.LIST_PATH, THIRD_PARTY / "mrl-ban-list" / "ban_list.txt")
        self.assertEqual(spylist.SHA256, BUNDLED["mrl-ban-list/ban_list.txt"])

    def test_loads(self):
        s = SpyList()
        self.assertEqual(s.state, "ok")
        self.assertEqual(len(s.hosts) + len(s.nets), 431)
        self.assertEqual(len(s.nets), 14)
        self.assertTrue(all(n.prefixlen == 24 for n in s.nets))

    def test_version_and_date_are_the_files_own(self):
        first = spylist.LIST_PATH.read_text(encoding="ascii").splitlines()[0]
        self.assertEqual(first, "# Monero Research Lab spy node ban list version %s, %s"
                         % (spylist.VERSION, spylist.DATE))

    def test_every_line_is_read_as_monerod_reads_it(self):
        # monerod's --ban-list (src/p2p/net_node.inl): '#' starts a comment, blank lines are
        # skipped, and every other line is an address or an IPv4 range
        lines = [ln.split("#", 1)[0].strip() for ln in spylist.LIST_PATH.read_text(encoding="ascii").splitlines()]
        lines = [ln for ln in lines if ln]
        s = SpyList()
        self.assertEqual(sorted(lines), sorted([str(h) for h in s.hosts] + [str(n) for n in s.nets]))


class ReadTest(unittest.TestCase):
    def copy(self, data: bytes):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "ban_list.txt"
        p.write_bytes(data)
        return p

    def test_a_changed_list_is_not_used(self):
        data = spylist.LIST_PATH.read_bytes()
        some = next(ln for ln in data.decode().splitlines() if ln and not ln.startswith("#") and "/" not in ln)
        for changed in (data + b"203.0.113.9\n", data.replace(some.encode() + b"\n", b"")):
            s = SpyList(self.copy(changed))
            self.assertEqual(s.state, "changed")
            self.assertFalse(s.on_list("203.0.113.9"))
            self.assertFalse(s.on_list(some))
            peers = [{"kind": "ipv4", "ip": some, "dir": "out"}]
            s.mark(peers)
            self.assertIsNone(peers[0]["spy"], "not known either way, so not 'not on it'")
            part = s.part([{"dir": "out", "spy": False}], {"bans": []})
            self.assertEqual((part["state"], part["entries"], part["node_bans"], part["out"], part["in"]),
                             ("changed", 0, None, None, None))

    def test_a_missing_list_is_not_used(self):
        listed = str(sorted(SpyList().hosts)[0])
        s = SpyList(Path(tempfile.gettempdir()) / "memstara-no-such-list" / "ban_list.txt")
        self.assertEqual(s.state, "missing")
        self.assertFalse(s.on_list(listed))

    def test_the_file_is_only_opened_to_read(self):
        p = self.copy(spylist.LIST_PATH.read_bytes())
        os.chmod(p, 0o444)
        before = p.stat()
        self.assertEqual(SpyList(p).state, "ok")
        after = p.stat()
        self.assertEqual((before.st_mtime_ns, before.st_size, before.st_mode), (after.st_mtime_ns, after.st_size, after.st_mode))

    def test_parse(self):
        hosts, nets = spylist.parse("# a comment\n\n  203.0.113.5  # trailing\n198.51.100.0/24\nnot an address\n"
                                    "2001:db8::1\n198.51.100.300\n")
        self.assertEqual(hosts, {ipaddress.IPv4Address("203.0.113.5")})
        self.assertEqual(nets, {ipaddress.IPv4Network("198.51.100.0/24")})


class MatchTest(unittest.TestCase):
    def test_on_list(self):
        s = stand_in(["203.0.113.5"], ["198.51.100.0/24"])
        self.assertTrue(s.on_list("203.0.113.5"))
        self.assertTrue(s.on_list("198.51.100.77"))
        for no in ("203.0.113.6", "192.0.2.77", "", "x", "2001:db8::1", "abc.onion", "203.0.113.5:18080"):
            self.assertFalse(s.on_list(no), no)

    def test_the_real_list(self):
        s = SpyList()
        one = sorted(s.hosts)[0]
        net = sorted(s.nets)[0]
        self.assertTrue(s.on_list(str(one)))
        self.assertTrue(s.on_list(str(net.network_address + 200)))
        self.assertFalse(s.on_list("203.0.113.5"))

    def test_mark(self):
        s = stand_in(["203.0.113.5"], ["198.51.100.0/24"])
        peers = [{"kind": "ipv4", "ip": "203.0.113.5", "dir": "out"}, {"kind": "ipv4", "ip": "198.51.100.9", "dir": "in"},
                 {"kind": "ipv4", "ip": "203.0.113.8", "dir": "out"}, {"kind": "tor", "ip": "", "dir": "out"},
                 {"kind": "ipv6", "ip": "2001:db8::5", "dir": "in"}]
        s.mark(peers)
        self.assertEqual([p["spy"] for p in peers], [True, True, False, False, False])
        part = s.part(peers, None)
        self.assertEqual((part["out"], part["in"], part["node_bans"]), (1, 1, None))
        self.assertEqual(s.part(None, None)["out"], None)


class NodeBansTest(unittest.TestCase):
    def test_counts_what_get_bans_covers(self):
        s = stand_in(["192.0.2.1", "192.0.2.2", "198.51.100.9"], ["203.0.113.0/26", "203.0.113.128/26"])
        self.assertEqual(s.banned_by_node({"status": "OK"}), 0)   # monerod leaves an empty list out
        self.assertEqual(s.banned_by_node(None), None)
        bans = {"bans": [{"host": "192.0.2.1", "seconds": 600}, {"host": "198.51.100.0/24", "seconds": 600},
                         {"host": "203.0.113.0/26", "seconds": 600}, {"host": "203.0.113.128/25", "seconds": 600},
                         {"host": "abc.onion", "seconds": 9}, {"host": 7}, "x"]}
        # 192.0.2.1 itself, 198.51.100.9 in a banned range, 203.0.113.0/26 itself, 203.0.113.128/26 in a
        # wider one; 192.0.2.2 isn't banned
        self.assertEqual(s.banned_by_node(bans), 4)

    def test_the_count_matches_the_plain_rule(self):
        # the quick count against the slow, plain one: an entry is banned when a banned host
        # is it, or a banned range holds all of it
        rng = random.Random(3)
        for _ in range(40):
            pick = lambda: "10.%d.%d.%d" % (rng.randrange(4), rng.randrange(4), rng.randrange(256))
            hosts = {pick() for _ in range(30)}
            nets = {str(ipaddress.IPv4Network("%s/%d" % (pick(), rng.choice([16, 22, 24, 26, 30])), strict=False))
                    for _ in range(8)}
            bans = [{"host": pick()} for _ in range(20)] + [{"host": h} for h in rng.sample(sorted(hosts), 5)]
            bans += [{"host": str(ipaddress.IPv4Network("%s/%d" % (pick(), rng.choice([8, 14, 20, 24, 25, 28, 32])),
                                                          strict=False))} for _ in range(6)]
            s = stand_in(hosts, nets)
            banned_hosts = {ipaddress.IPv4Address(b["host"]) for b in bans if "/" not in b["host"]}
            banned_nets = [ipaddress.IPv4Network(b["host"]) for b in bans if "/" in b["host"]]
            want = sum(1 for h in s.hosts if h in banned_hosts or any(h in w for w in banned_nets))
            want += sum(1 for r in s.nets if any(r.subnet_of(w) for w in banned_nets))
            self.assertEqual(s.banned_by_node({"bans": bans}), want)

    def test_the_same_answer_is_counted_once(self):
        s = stand_in(["192.0.2.1"])
        res = {"bans": [{"host": "192.0.2.1"}]}
        self.assertEqual(s.banned_by_node(res), 1)
        s.hosts = frozenset()   # (only to show the count isn't redone for the same answer)
        self.assertEqual(s.banned_by_node(res), 1)
        self.assertEqual(s.banned_by_node({"bans": [{"host": "192.0.2.1"}]}), 0)

    def test_a_node_using_the_list_bans_all_of_it(self):
        # what get_bans shows for monerod started with --ban-list on this file (or the DNS
        # blocklist, which serves the same entries): each one as monerod writes its host
        s = SpyList()
        bans = {"bans": [{"host": str(h), "ip": 0, "seconds": 2503736695} for h in s.hosts]
                + [{"host": str(n), "ip": 0, "seconds": 2503736695} for n in s.nets]}
        self.assertEqual(s.banned_by_node(bans), 431)
        self.assertEqual(s.part([], bans)["node_bans"], 431)


class LampTest(unittest.TestCase):
    def lamp(self, spy, alert=None, clock="12h-utc"):
        rows = compute_lamps({"answering": True, "node": {}, "chain": {}, "clock": clock,
                              "peers": {"spy": spy}, "alerts": {"spy": alert} if alert else {}})
        return {i: (st, v) for i, st, v in rows}["spy"]

    def test_lamp_words(self):
        ok = {"state": "ok", "entries": 431, "node_bans": 0, "out": 0, "in": 0}
        self.assertEqual(self.lamp(dict(ok, out=1, **{"in": 1}), {"level": "warn", "since": 0}), ("warn", "2 CONNECTED"))
        self.assertEqual(self.lamp(ok), ("ok", "NONE CONNECTED"))
        self.assertEqual(self.lamp(dict(ok, node_bans=431)), ("ok", "NODE BANS ALL"))
        self.assertEqual(self.lamp(dict(ok, out=None, **{"in": None})), ("off", "NO DATA"))
        self.assertEqual(self.lamp({"state": "changed"}), ("off", "LIST CHANGED"))
        self.assertEqual(self.lamp({"state": "missing"}), ("off", "LIST MISSING"))

    def test_seen_fits_the_lamp(self):
        # held up after the last one left: when it was seen, without "SEEN" where both wouldn't
        # fit in a lamp's 16 characters
        ok = {"state": "ok", "entries": 431, "node_bans": 0, "out": 0, "in": 0}
        morning, night = calendar.timegm((2025, 10, 12, 6, 58, 0)), calendar.timegm((2025, 10, 12, 22, 5, 0))
        for t, clock, want in ((morning, "12h-utc", "SEEN 6:58 AM UTC"), (night, "12h-utc", "10:05 PM UTC"),
                               (night, "24h-utc", "SEEN 22:05 UTC")):
            self.assertEqual(self.lamp(ok, {"level": "warn", "since": t}, clock), ("warn", want))


if __name__ == "__main__":
    unittest.main()
