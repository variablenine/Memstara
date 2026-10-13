"""Local reads (host.py): the node's process, the computer, its network link and peers'
round trips, read from a fake /proc and /sys tree built under a temp folder."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import helpers
from memstara import host
from memstara.host import CommandError, HostReader

LOCAL = helpers.TESTS / "fixtures" / "local"
NOW = helpers.T0
BTIME = int(NOW) - 100000
NODE_PID = 4242
NODE_START_TICKS = 5000000          # 50,000 s after boot at 100 ticks/s
NODE_ARGV = ["/opt/monero/monerod", "--data-dir", "/home/u/.bitmonero", "--rpc-bind-port", "18091",
             "--rpc-login", "monero:hunter2", "--p2p-bind-ip", "10.8.0.2", "--p2p-bind-port", "18080",
             "--out-peers", "32", "--bootstrap-daemon-login=boot:secret", "--rpc-access-control-origins",
             "http://evil.example", "--tx-proxy", "tor,127.0.0.1:9050,16"]
SYS_NVME = "devices/pci0000:00/0000:00:01.0/0000:01:00.0/nvme/nvme0/nvme0n1"


def local(name):
    return (LOCAL / name).read_text(encoding="utf-8")


def ss_text():
    return local("ss_established.txt")


class FakeRoot:
    """A fake / with just the /proc and /sys files host.py reads."""

    def __init__(self, uid=None, other_uid=None):
        self.dir = tempfile.mkdtemp(prefix="memstara-host-")
        self.uid = os.getuid() if uid is None else uid
        self.other_uid = other_uid

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, rel):
        return os.path.join(self.dir, rel)

    def write(self, rel, data):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)

    def link(self, rel, target):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if os.path.lexists(p):
            os.unlink(p)
        os.symlink(target, p)

    def tcp(self, rpc_uid=None, drop_rpc=False):
        rpc_uid = self.uid if rpc_uid is None else rpc_uid
        lines = []
        for line in local("proc_net_tcp.txt").splitlines(True):
            if " 31337 " in line:
                if drop_rpc:
                    continue
                line = line.replace("{uid}", str(rpc_uid))
            lines.append(line.replace("{uid}", str(self.uid)))
        self.write("proc/net/tcp", "".join(lines))
        self.write("proc/net/tcp6", local("proc_net_tcp6.txt").replace("{uid}", str(self.uid)))

    def process(self, pid, comm, fds, argv, utime=0, stime=0, start=1000, threads=3,
                rss_kb=1000, io=(0, 0), exe=None):
        d = "proc/%d" % pid
        self.write(d + "/comm", comm + "\n")
        rest = ["S", "1", str(pid), str(pid), "0", "-1", "4194560", "1234", "0", "0", "0",
                str(utime), str(stime), "0", "0", "20", "0", str(threads), "0", str(start),
                "3000000000", str(rss_kb // 4)] + ["0"] * 30
        self.write(d + "/stat", "%d (%s) %s\n" % (pid, comm, " ".join(rest)))
        self.write(d + "/status", "Name:\t%s\nUmask:\t0002\nState:\tS (sleeping)\nPid:\t%d\n"
                   "VmPeak:\t 3100000 kB\nVmRSS:\t %d kB\nThreads:\t%d\n" % (comm, pid, rss_kb, threads))
        self.write(d + "/io", "rchar: 999999\nwchar: 888888\nsyscr: 10\nsyscw: 20\nread_bytes: %d\n"
                   "write_bytes: %d\ncancelled_write_bytes: 0\n" % io)
        self.write(d + "/cmdline", "\0".join(argv) + "\0")
        if exe:
            self.link(d + "/exe", exe)
        for n, target in enumerate(fds):
            self.link("%s/fd/%d" % (d, n), target)

    def node(self, pid=NODE_PID, utime=1000, stime=500, io=(1000, 2000), extra_fds=0, argv=NODE_ARGV):
        fds = ["/dev/null", "/var/log/monero/monerod.out", "socket:[31337]", "socket:[31338]",
               "socket:[31339]", "socket:[50001]", "socket:[50002]", "socket:[50003]", "socket:[50004]",
               "/home/u/.bitmonero/lmdb/data.mdb", "anon_inode:[eventpoll]", "pipe:[9191]"]
        fds += ["socket:[%d]" % (90000 + i) for i in range(extra_fds)]
        self.process(pid, "monerod", fds, argv, utime=utime, stime=stime, start=NODE_START_TICKS,
                     threads=41, rss_kb=1850000, io=io, exe="/opt/monero/monerod (deleted)")

    def counters(self, disk=(9000000, 24000000, 350000), net=(1000000, 2000000)):
        sr, sw, ticks = disk
        self.write("proc/diskstats", "".join([
            " 259       0 nvme0n1 120000 3000 %d 40000 300000 20000 %d 900000 0 %d 940000 0 0 0 0 5000 1200\n"
            % (sr, sw, ticks),
            " 259       1 nvme0n1p1 300 0 20000 100 2 0 8 1 0 120 101 0 0 0 0 0 0\n",
            " 259       2 nvme0n1p2 110000 3000 8000000 39000 290000 20000 23000000 890000 0 340000 930000 0 0 0 0 0 0\n",
            " 253       0 dm-0 109000 0 7900000 41000 310000 0 23500000 990000 0 345000 1031000 0 0 0 0 0 0\n",
        ]))
        self.write("sys/class/net/wg0/uevent", "DEVTYPE=wireguard\nINTERFACE=wg0\nIFINDEX=7\n")
        self.write("sys/class/net/wg0/type", "65534\n")
        self.write("sys/class/net/wg0/statistics/rx_bytes", "%d\n" % net[0])
        self.write("sys/class/net/wg0/statistics/tx_bytes", "%d\n" % net[1])


def standard_tree(**kw) -> FakeRoot:
    r = FakeRoot(**kw)
    r.write("proc/stat", "cpu  10132153 290696 3084719 46828483 16683 0 25195 0 0 0\nintr 1\n"
                         "ctxt 1990473\nbtime %d\nprocesses 2343\n" % BTIME)
    r.write("proc/meminfo", local("meminfo.txt"))
    r.write("proc/loadavg", "0.80 0.62 0.55 2/1234 99999\n")
    r.write("proc/uptime", "99999.42 700000.10\n")
    r.write("proc/self/mountinfo", local("mountinfo.txt"))
    r.tcp()
    r.node()
    # another app's own monerod: same name, same user, a different RPC port
    r.process(5151, "monerod", ["socket:[4444]", "socket:[60001]"],
              ["/opt/otherapp/monerod", "--rpc-bind-port", "18081"], exe="/opt/otherapp/monerod")
    r.process(777, "bash", ["/dev/pts/0", "/dev/pts/0"], ["-bash"], exe="/usr/bin/bash")
    # the computer
    r.write("sys/devices/system/cpu/online", "0-7\n")
    for n, (typ, temp) in enumerate([("acpitz", 27800), ("x86_pkg_temp", 61000), ("wifi_1", 45000),
                                     ("board", 20000)]):
        r.write("sys/class/thermal/thermal_zone%d/type" % n, typ + "\n")
        r.write("sys/class/thermal/thermal_zone%d/temp" % n, "%d\n" % temp)
    r.write("sys/class/hwmon/hwmon3/name", "coretemp\n")
    r.write("sys/class/hwmon/hwmon3/temp1_input", "60000\n")
    r.write("sys/class/hwmon/hwmon3/temp2_input", "58000\n")
    r.write("sys/class/hwmon/hwmon4/name", "nvme\n")        # hotter, but not the CPU
    r.write("sys/class/hwmon/hwmon4/temp1_input", "70000\n")
    r.write("sys/class/power_supply/AC/type", "Mains\n")
    r.write("sys/class/power_supply/AC/online", "1\n")
    for k, v in (("type", "Battery"), ("status", "Charging"), ("capacity", "87"), ("present", "1")):
        r.write("sys/class/power_supply/BAT0/" + k, v + "\n")
    for k, v in (("type", "Battery"), ("scope", "Device"), ("status", "Discharging"), ("capacity", "20")):
        r.write("sys/class/power_supply/mouse_battery_0/" + k, v + "\n")   # a wireless mouse
    # the disk: 259:2 is a partition of nvme0n1; dm-0 is LUKS on nvme0n1p3
    for p in ("nvme0n1p1", "nvme0n1p2", "nvme0n1p3"):
        r.write("sys/%s/%s/partition" % (SYS_NVME, p), p[-1] + "\n")
        r.link("sys/class/block/" + p, "../../%s/%s" % (SYS_NVME, p))
    r.write("sys/%s/size" % SYS_NVME, "1000000000\n")
    r.link("sys/dev/block/259:2", "../../%s/nvme0n1p2" % SYS_NVME)
    r.link("sys/devices/virtual/block/dm-0/slaves/nvme0n1p3", "../../../../%s/nvme0n1p3" % SYS_NVME[len("devices/"):])
    r.link("sys/class/block/dm-0", "../../devices/virtual/block/dm-0")
    r.link("dev/mapper/luks-0000", "../dm-0")
    r.counters()
    return r


def fake_run(ss=None, ip=None):
    """Stands in for running ss / ip: text, or an exception to raise."""
    calls = []

    def run(name, args):
        calls.append((name, tuple(args)))
        out = {"ss": ss_text() if ss is None else ss, "ip": local("ip_addr.json") if ip is None else ip}[name]
        if isinstance(out, Exception):
            raise out
        return out
    run.calls = calls
    return run


def reader_for(tree, url="http://127.0.0.1:18091", dev=(259, 2), data_dir="/home/u/.bitmonero", **kw):
    r = HostReader(url, data_dir=data_dir, root=tree.dir, run=kw.pop("run", None) or fake_run(),
                   home=tree.path("home/u"))
    r.uid = tree.uid
    r._clk = 100
    r._stat = lambda p: SimpleNamespace(st_dev=os.makedev(*dev))
    return r


class ParseTest(unittest.TestCase):
    def test_proc_net_tcp_little_endian(self):
        rows = host.parse_proc_net(local("proc_net_tcp.txt").replace("{uid}", "1000"), "little")
        by_inode = {r["inode"]: r for r in rows}
        self.assertEqual(by_inode[31337]["local"], ("127.0.0.1", 18091))
        self.assertEqual(by_inode[31337]["state"], host.TCP_LISTEN)
        self.assertEqual(by_inode[31337]["uid"], 1000)
        self.assertEqual(by_inode[31338]["local"], ("10.8.0.2", 18080))
        self.assertEqual(by_inode[50002]["remote"], ("100.125.29.164", 40151))
        self.assertEqual(by_inode[50001]["state"], host.TCP_ESTABLISHED)
        self.assertEqual(by_inode[2222]["uid"], 120)
        self.assertEqual(len(rows), 10)

    def test_proc_net_tcp6(self):
        rows = host.parse_proc_net(local("proc_net_tcp6.txt").replace("{uid}", "1000"), "little")
        by_inode = {r["inode"]: r for r in rows}
        self.assertEqual(by_inode[31339]["local"], ("::1", 18091))
        self.assertEqual(by_inode[31339]["remote"], ("::", 0))
        self.assertEqual(by_inode[50004]["local"], ("2001:db8:1::5", 45678))
        self.assertEqual(by_inode[50004]["remote"], ("2001:db8:5ce2:7d72::ab9", 18080))
        # IPv4-mapped addresses on a v6 socket come out as plain IPv4
        self.assertEqual(by_inode[7778]["local"], ("127.0.0.1", 18200))

    def test_big_endian_and_junk(self):
        text = "header\n   0: 7F000001:46AB 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 5 1\n"
        self.assertEqual(host.parse_proc_net(text, "big")[0]["local"], ("127.0.0.1", 18091))
        self.assertEqual(host.parse_proc_net(text, "little")[0]["local"], ("1.0.0.127", 18091))
        self.assertEqual(host.parse_proc_net("header\n0: zz:1 00:0 0A x x x x x x\nshort line\n"), [])
        self.assertEqual(host.parse_proc_net(""), [])

    def test_ss(self):
        rows = host.parse_ss(ss_text())
        self.assertEqual(len(rows), 6)
        first = rows[0]
        self.assertEqual(first["local"], ("10.8.0.2", 39834))
        self.assertEqual(first["peer"], ("100.90.146.87", 18080))
        self.assertEqual(first["rtt_ms"], 35.1)
        self.assertEqual(first["retrans"], 2)           # retrans:<now>/<total>: the total
        self.assertEqual(rows[1]["retrans"], 0)         # no retrans: field means none
        self.assertEqual(rows[1]["rtt_ms"], 208.4)      # not minrtt or rcv_rtt
        v6 = rows[4]
        self.assertEqual(v6["peer"], ("2001:db8:5ce2:7d72::ab9", 18080))
        self.assertEqual(v6["retrans"], 7)
        self.assertEqual(rows[5]["local"], ("127.0.0.1", 18200))

    def test_ss_odd_lines(self):
        text = ("ESTAB 0 0 10.0.0.1%wg0:5000 1.2.3.4:18080\n\t cubic rtt:1.25/0.5\n"
                "garbage\n\t rtt:9/1\n0 0 1.2.3.4:x 5.6.7.8:1\n0 0 1.1.1.1:1 2.2.2.2:2\n")
        rows = host.parse_ss(text)
        self.assertEqual([r["local"] for r in rows], [("10.0.0.1", 5000), ("1.1.1.1", 1)])
        self.assertEqual(rows[0]["rtt_ms"], 1.2)
        self.assertIsNone(rows[1]["rtt_ms"])
        self.assertIsNone(rows[1]["retrans"])            # no -i line at all: unknown
        self.assertEqual(host.parse_ss(""), [])

    def test_mask_flags(self):
        m = host.MASK
        self.assertEqual(host.mask_flags(NODE_ARGV[1:]), [
            "--data-dir", "/home/u/.bitmonero", "--rpc-bind-port", "18091", "--rpc-login", m,
            "--p2p-bind-ip", "10.8.0.2", "--p2p-bind-port", "18080", "--out-peers", "32",
            "--bootstrap-daemon-login=" + m, "--rpc-access-control-origins", m,
            "--tx-proxy", "tor,127.0.0.1:9050,16"])
        self.assertEqual(host.mask_flags(["--rpc-login=a:b", "--some-PASSWORD", "x", "--wallet-password=y"]),
                         ["--rpc-login=" + m, "--some-PASSWORD", m, "--wallet-password=" + m])
        self.assertEqual(host.mask_flags(["--rpc-login"]), ["--rpc-login"])   # nothing to hide
        self.assertEqual(host.mask_flags([]), [])
        # a login inside a proxy address, in any argument
        self.assertEqual(host.mask_flags(["--tx-proxy", "tor,socks5://alice:s3cret@127.0.0.1:9050,16",
                                          "--proxy=socks5://bob:hunter2@127.0.0.1:9050", "--proxy", "127.0.0.1:9050"]),
                         ["--tx-proxy", "tor,socks5://%s@127.0.0.1:9050,16" % m,
                          "--proxy=socks5://%s@127.0.0.1:9050" % m, "--proxy", "127.0.0.1:9050"])
        # boost takes a start of an option's name that fits only one option: --rpc-logi is --rpc-login
        self.assertEqual(host.mask_flags(["--rpc-logi", "carol:pw123", "--bootstrap-daemon-l=boot:pw",
                                          "--rpc-access", "http://x", "--rpc-b", "18081"]),
                         ["--rpc-logi", m, "--bootstrap-daemon-l=" + m, "--rpc-access", m, "--rpc-b", "18081"])
        self.assertTrue(host.node_settings(["--rpc-logi", "carol:pw"], "/home/u", read_file=lambda p: None)
                        ["login_needed"])
        self.assertFalse(host.node_settings(["--rpc-l"], "/home/u", read_file=lambda p: None)["login_needed"],
                         "a switch-like start without a value isn't a login")

    def test_annotate_peers(self):
        peers = [{"kind": "ipv4", "ip": "100.90.146.87", "port": 18080},
                 {"kind": "ipv6", "ip": "2001:DB8:5ce2:7d72:0::ab9", "port": 18080},
                 {"kind": "tor", "ip": "", "host": "abc.onion", "port": 18091},
                 {"kind": "ipv4", "ip": "9.9.9.9", "port": 1}, "junk"]
        rtts = {("100.90.146.87", 18080): {"rtt_ms": 35.1, "retrans": 2},
                ("2001:db8:5ce2:7d72::ab9", 18080): {"rtt_ms": 88.8, "retrans": 7}}
        host.annotate_peers(peers, rtts)
        self.assertEqual((peers[0]["rtt_ms"], peers[0]["retrans"]), (35.1, 2))
        self.assertEqual(peers[1]["rtt_ms"], 88.8)
        self.assertIsNone(peers[2]["rtt_ms"])
        self.assertIsNone(peers[3]["retrans"])
        host.annotate_peers(peers, None)
        self.assertTrue(all(p["rtt_ms"] is None for p in peers[:4]))


class TorPortTest(unittest.TestCase):
    def test_from_the_tx_proxy_flag(self):
        tp = host.tor_socks_port
        self.assertEqual(tp(["--tx-proxy", "tor,127.0.0.1:9150,16"]), 9150)
        self.assertEqual(tp(["--tx-proxy=tor,127.0.0.1:9152,disable_noise"]), 9152)
        self.assertEqual(tp(["--tx-proxy", "i2p,127.0.0.1:4447", "--tx-proxy", "tor,127.0.0.1:9151"]), 9151)
        for flags in (None, [], ["--tx-proxy", "i2p,127.0.0.1:4447"], ["--tx-proxy"], ["--tx-proxy", "tor,x:0"]):
            self.assertEqual(tp(flags), 9050, flags)


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.tree = standard_tree()
        self.addCleanup(self.tree.close)

    def test_full_read(self):
        r = reader_for(self.tree)
        h = r.read(NOW)
        self.assertEqual(set(h), {"ok", "error", "every_s", "t", "proc", "proc_error", "computer", "bind_if", "tor", "remote"})
        self.assertTrue(h["ok"])
        self.assertIsNone(h["error"])
        self.assertIsNone(h["proc_error"])
        self.assertEqual(h["every_s"], 10)
        self.assertEqual(h["t"], NOW)
        p = h["proc"]
        self.assertEqual(p["pid"], NODE_PID)       # not another app's monerod (pid 5151)
        self.assertEqual(p["name"], "monerod")
        self.assertEqual(p["exe"], "/opt/monero/monerod")
        self.assertTrue(p["exe_replaced"])
        self.assertEqual(p["started"], BTIME + NODE_START_TICKS // 100)
        self.assertEqual(p["rss_bytes"], 1850000 * 1024)
        self.assertEqual(p["threads"], 41)
        self.assertEqual(p["open_files"], 12)
        self.assertEqual((p["read_bytes"], p["write_bytes"]), (1000, 2000))
        self.assertIsNone(p["cpu_pct"])            # needs two reads
        self.assertIsNone(p["read_rate"])
        self.assertIn("--rpc-login", p["flags"])
        self.assertNotIn("monero:hunter2", p["flags"])
        self.assertNotIn("http://evil.example", p["flags"])
        self.assertFalse(any("secret" in f for f in p["flags"]))
        self.assertEqual(p["flags"][:2], ["--data-dir", "/home/u/.bitmonero"])

        lap = h["computer"]
        self.assertEqual(lap["cpu_temp_c"], 61.0)
        self.assertEqual((lap["on_ac"], lap["battery_pct"], lap["battery_state"]), (True, 87, "Charging"))
        self.assertEqual((lap["load1"], lap["cores"]), (0.8, 8))
        self.assertEqual(lap["mem_total_bytes"], 16000000 * 1024)
        self.assertEqual(lap["mem_used_bytes"], (16000000 - 9800000) * 1024)
        self.assertEqual(lap["uptime_s"], 99999)
        self.assertEqual(lap["disk"], {"device": "nvme0n1", "read_rate": None, "write_rate": None, "busy_pct": None})

        self.assertEqual(h["bind_if"], {"name": "wg0", "kind": "vpn", "up": True, "address": "10.8.0.2",
                                       "rx_bytes": 1000000, "tx_bytes": 2000000, "rx_rate": None, "tx_rate": None})
        self.assertEqual(h["tor"], {"socks_port": 9050, "socks_listening": True})
        # fixed argument lists only
        self.assertEqual(sorted(set(r._run.calls)), [("ip", ("-j", "addr", "show")),
                                                       ("ss", ("-tinH", "state", "established"))])
        json.dumps(h)   # all plain JSON

    def test_rates_on_second_read(self):
        r = reader_for(self.tree)
        r.read(NOW)
        self.tree.node(utime=1060, stime=515, io=(11000, 22000))
        self.tree.counters(disk=(9002000, 24040000, 350300), net=(1030000, 2040000))
        h = r.read(NOW + 10)
        p = h["proc"]
        self.assertEqual(p["cpu_pct"], 7.5)        # 75 ticks of 1/100 s in 10 s
        self.assertEqual((p["read_rate"], p["write_rate"]), (1000, 2000))
        self.assertEqual(h["computer"]["disk"], {"device": "nvme0n1", "read_rate": 102400,
                                               "write_rate": 2048000, "busy_pct": 3.0})
        self.assertEqual((h["bind_if"]["rx_rate"], h["bind_if"]["tx_rate"]), (3000, 4000))
        # counters that went backwards (a reset) give null, not a negative rate
        self.tree.counters(disk=(1, 1, 1), net=(5, 5))
        h = r.read(NOW + 20)
        self.assertIsNone(h["computer"]["disk"]["read_rate"])
        self.assertIsNone(h["computer"]["disk"]["busy_pct"])
        self.assertIsNone(h["bind_if"]["rx_rate"])

    def test_rates_timed_when_each_counter_is_read(self):
        # the poller's clock, not `now` (when the poll started): reads 5 s apart in a
        # poll 10 s after the last give rates over 5 s
        r = reader_for(self.tree)
        r.read(NOW, clock=lambda: 50.0)
        self.tree.node(utime=1060, stime=515, io=(11000, 22000))
        self.tree.counters(disk=(9002000, 24040000, 350300), net=(1030000, 2040000))
        h = r.read(NOW + 10, clock=lambda: 55.0)
        self.assertEqual(h["t"], NOW + 10)
        p = h["proc"]
        self.assertEqual(p["cpu_pct"], 15.0)       # 75 ticks of 1/100 s in 5 s
        self.assertEqual((p["read_rate"], p["write_rate"]), (2000, 4000))
        self.assertEqual(h["computer"]["disk"], {"device": "nvme0n1", "read_rate": 204800,
                                               "write_rate": 4096000, "busy_pct": 6.0})
        self.assertEqual((h["bind_if"]["rx_rate"], h["bind_if"]["tx_rate"]), (6000, 8000))
        # a clock that didn't move on gives no rate rather than a division by zero
        h = r.read(NOW + 20, clock=lambda: 55.0)
        self.assertIsNone(h["proc"]["cpu_pct"])
        self.assertIsNone(h["computer"]["disk"]["busy_pct"])
        self.assertIsNone(h["bind_if"]["rx_rate"])

    def test_round_trips_only_from_the_nodes_own_sockets(self):
        r = reader_for(self.tree)
        r.read(NOW)
        peers = [{"kind": "ipv4", "ip": "100.90.146.87", "port": 18080, "dir": "out"},
                 {"kind": "ipv4", "ip": "100.125.29.164", "port": 40151, "dir": "in"},
                 {"kind": "ipv6", "ip": "2001:db8:5ce2:7d72::ab9", "port": 18080, "dir": "out"},
                 {"kind": "tor", "ip": "", "port": 18091, "dir": "out"}]
        r.annotate(peers)
        # another app's monerod talks to the same peer (rtt 12.5); the node's own connection wins
        self.assertEqual((peers[0]["rtt_ms"], peers[0]["retrans"]), (35.1, 2))
        self.assertEqual((peers[1]["rtt_ms"], peers[1]["retrans"]), (208.4, 0))
        self.assertAlmostEqual(peers[2]["rtt_ms"], 88.75, delta=0.06)
        self.assertEqual(peers[2]["retrans"], 7)
        self.assertIsNone(peers[3]["rtt_ms"])
        self.assertNotIn(("127.0.0.1", 18200), r._rtts)    # the dashboard's own sockets are not the node's

    def test_ss_missing(self):
        r = reader_for(self.tree, run=fake_run(ss=CommandError("ss isn't installed")))
        h = r.read(NOW)
        self.assertTrue(h["ok"])
        self.assertEqual(h["error"], "ss isn't installed, so peer round trips aren't shown")
        self.assertIsNotNone(h["proc"])
        peers = [{"kind": "ipv4", "ip": "100.90.146.87", "port": 18080}]
        r.annotate(peers)
        self.assertIsNone(peers[0]["rtt_ms"])

    def test_ip_missing_or_garbled(self):
        h = reader_for(self.tree, run=fake_run(ip=CommandError("ip didn't answer in 3 s"))).read(NOW)
        self.assertIsNone(h["bind_if"])
        self.assertIn("ip didn't answer in 3 s, so the node's network link isn't shown", h["error"])
        h = reader_for(self.tree, run=fake_run(ip="not json")).read(NOW)
        self.assertIsNone(h["bind_if"])
        self.assertIn("ip's answer", h["error"])

    def test_vpn_down_and_no_bind_ip(self):
        ifaces = [i for i in json.loads(local("ip_addr.json")) if i["ifname"] != "wg0"]
        h = reader_for(self.tree, run=fake_run(ip=json.dumps(ifaces))).read(NOW)
        self.assertEqual(h["bind_if"]["name"], None)
        self.assertFalse(h["bind_if"]["up"])
        self.assertEqual(h["bind_if"]["address"], "10.8.0.2")
        argv = [a for a in NODE_ARGV if a not in ("--p2p-bind-ip", "10.8.0.2")]
        self.tree.node(argv=argv)
        r = reader_for(self.tree)
        self.assertIsNone(r.read(NOW)["bind_if"])
        self.assertEqual(r._run.calls, [("ss", host.SS_ARGS)])   # ip isn't even run

    def test_pid_found_again_when_it_stops_existing(self):
        r = reader_for(self.tree)
        self.assertEqual(r.read(NOW)["proc"]["pid"], NODE_PID)
        shutil.rmtree(self.tree.path("proc/%d" % NODE_PID))
        self.tree.node(pid=6000, utime=5, stime=5)
        h = r.read(NOW + 10)
        self.assertEqual(h["proc"]["pid"], 6000)
        self.assertIsNone(h["proc"]["cpu_pct"])     # a new process: no delta yet
        # pid reuse: same pid, different start time -> found again, rates restart
        r.read(NOW + 20)
        stat = self.tree.path("proc/6000/stat")
        with open(stat) as fh:
            text = fh.read().replace(" %d " % NODE_START_TICKS, " %d " % (NODE_START_TICKS + 1))
        with open(stat, "w") as fh:
            fh.write(text)
        self.assertIsNone(r.read(NOW + 30)["proc"]["cpu_pct"])

    def test_nothing_listening(self):
        self.tree.tcp(drop_rpc=True)
        h = reader_for(self.tree).read(NOW)
        self.assertTrue(h["ok"])
        self.assertIsNone(h["proc"])
        self.assertEqual(h["proc_error"], "Nothing is listening on 127.0.0.1:18091")
        self.assertIsNone(h["bind_if"])
        self.assertIsNotNone(h["computer"])
        # ss still runs; without the node's sockets the table isn't filtered
        r = reader_for(self.tree)
        r.read(NOW)
        self.assertIn(("100.90.146.87", 18080), r._rtts)

    def test_other_users_listener(self):
        self.tree.tcp(rpc_uid=self.tree.uid + 1)
        h = reader_for(self.tree).read(NOW)
        self.assertIsNone(h["proc"])
        self.assertEqual(h["proc_error"],
                         "Something listens on 127.0.0.1:18091 but it isn't one of your user's processes")

    def test_listener_without_a_visible_process(self):
        shutil.rmtree(self.tree.path("proc/%d" % NODE_PID))
        h = reader_for(self.tree).read(NOW)
        self.assertIsNone(h["proc"])
        self.assertEqual(h["proc_error"], "Something listens on 127.0.0.1:18091 but its process wasn't found")

    def test_ipv6_localhost_and_wildcard_urls(self):
        for url in ("http://[::1]:18091", "http://localhost:18091", "http://monerod.lan:18091"):
            h = reader_for(self.tree, url=url).read(NOW)
            self.assertEqual(h["proc"] and h["proc"]["pid"], NODE_PID, url)
        h = reader_for(self.tree, url="http://192.168.1.50:18091").read(NOW)
        self.assertIsNone(h["proc"])
        self.assertEqual(h["proc_error"], "Nothing is listening on 192.168.1.50:18091")
        # a node bound to 0.0.0.0 still matches 127.0.0.1
        text = local("proc_net_tcp.txt").replace("{uid}", str(self.tree.uid))
        self.tree.write("proc/net/tcp", text.replace("0100007F:46AB", "00000000:46AB"))
        self.assertEqual(reader_for(self.tree).read(NOW)["proc"]["pid"], NODE_PID)

    def test_not_linux_and_no_proc(self):
        with mock.patch.object(host.sys, "platform", "darwin"):
            h = reader_for(self.tree).read(NOW)
        self.assertEqual(h, {"ok": False, "error": "Local reads only work on Linux", "every_s": 10, "t": NOW,
                             "proc": None, "proc_error": None, "computer": None, "bind_if": None, "tor": None,
                             "remote": None})
        empty = tempfile.mkdtemp(prefix="memstara-noproc-")
        self.addCleanup(shutil.rmtree, empty, True)
        h = HostReader("http://127.0.0.1:18091", root=empty, run=fake_run()).read(NOW)
        self.assertFalse(h["ok"])
        self.assertEqual(h["error"], "Can't read /proc on this computer")

    def test_missing_pieces_are_null(self):
        for rel in ("sys", "proc/net", "proc/meminfo", "proc/loadavg", "proc/uptime", "proc/diskstats"):
            p = self.tree.path(rel)
            shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)
        h = reader_for(self.tree).read(NOW)
        self.assertTrue(h["ok"])
        self.assertIsNone(h["proc"])
        self.assertEqual(h["proc_error"], "Can't read the connection table to find monerod")
        self.assertIn("Can't read the connection table (/proc/net/tcp)", h["error"])
        self.assertIsNone(h["tor"])
        lap = h["computer"]
        for k in ("cpu_temp_c", "on_ac", "battery_pct", "battery_state", "load1", "mem_used_bytes",
                  "mem_total_bytes", "uptime_s", "disk", "cores"):
            self.assertIsNone(lap[k], k)

    def test_disk_through_btrfs_and_luks(self):
        # an anonymous device (btrfs: major 0) -> the mount's source -> dm-0 -> its one slave -> the disk
        r = reader_for(self.tree, dev=(0, 45), data_dir="/home/u/.bitmonero")
        if not os.path.realpath("/home/u/.bitmonero").startswith("/home/"):
            self.skipTest("/home is a link on this machine")
        self.assertEqual(r.read(NOW)["computer"]["disk"]["device"], "nvme0n1")
        # octal escapes in mount points (\040 is a space)
        r = reader_for(self.tree, dev=(0, 99), data_dir="/home/u/My Drive/bitmonero")
        self.assertEqual(r.read(NOW)["computer"]["disk"]["device"], "nvme0n1")
        # an unknown device: no disk, nothing else breaks
        r = reader_for(self.tree, dev=(8, 17), data_dir="/srv/elsewhere")
        h = r.read(NOW)
        self.assertIsNone(h["computer"]["disk"])
        self.assertIsNotNone(h["proc"])

    def test_power_variants(self):
        r = reader_for(self.tree)
        shutil.rmtree(self.tree.path("sys/class/power_supply/AC"))
        self.assertEqual(r.read(NOW)["computer"]["on_ac"], True)          # from the battery: Charging
        self.tree.write("sys/class/power_supply/BAT0/status", "Discharging\n")
        self.assertEqual(r.read(NOW)["computer"]["on_ac"], False)
        # two batteries (some laptops): combined from energy_now / energy_full
        for bat, now_, full in (("BAT0", 20000000, 40000000), ("BAT1", 10000000, 20000000)):
            for k, v in (("type", "Battery"), ("status", "Discharging"), ("capacity", "1"),
                         ("energy_now", str(now_)), ("energy_full", str(full))):
                self.tree.write("sys/class/power_supply/%s/%s" % (bat, k), v + "\n")
        lap = r.read(NOW)["computer"]
        self.assertEqual((lap["battery_pct"], lap["battery_state"]), (50, "Discharging"))
        # a desktop: no batteries, mains only
        shutil.rmtree(self.tree.path("sys/class/power_supply"))
        self.tree.write("sys/class/power_supply/ACAD/type", "Mains\n")
        self.tree.write("sys/class/power_supply/ACAD/online", "0\n")
        lap = r.read(NOW)["computer"]
        self.assertEqual((lap["on_ac"], lap["battery_pct"], lap["battery_state"]), (False, None, None))

    def test_temperature_variants(self):
        r = reader_for(self.tree)
        shutil.rmtree(self.tree.path("sys/class/thermal"))
        self.assertEqual(r.read(NOW)["computer"]["cpu_temp_c"], 60.0)     # coretemp hwmon
        shutil.rmtree(self.tree.path("sys/class/hwmon"))
        self.tree.write("sys/class/hwmon/hwmon0/name", "k10temp\n")       # AMD
        self.tree.write("sys/class/hwmon/hwmon0/temp1_input", "71250\n")
        self.tree.write("sys/class/thermal/thermal_zone0/type", "cpu-thermal\n")   # ARM boards
        self.tree.write("sys/class/thermal/thermal_zone0/temp", "-273000\n")      # a dead sensor
        self.assertEqual(r.read(NOW)["computer"]["cpu_temp_c"], 71.2)
        # a single-board computer: thermal zones only
        shutil.rmtree(self.tree.path("sys/class/hwmon"))
        shutil.rmtree(self.tree.path("sys/class/thermal"))
        for n, (typ, temp) in enumerate([("package-thermal", 48100), ("bigcore0-thermal", 49800),
                                         ("gpu-thermal", 55000)]):
            self.tree.write("sys/class/thermal/thermal_zone%d/type" % n, typ + "\n")
            self.tree.write("sys/class/thermal/thermal_zone%d/temp" % n, "%d\n" % temp)
        self.assertEqual(r.read(NOW)["computer"]["cpu_temp_c"], 49.8)    # not the GPU's 55

    def test_throttle(self):
        r = reader_for(self.tree)
        t = r.read(NOW)["computer"]["throttle"]          # the standard tree has none of it
        self.assertEqual(t, {"source": None, "now": None, "last_t": None, "since_boot": None,
                             "speed_mhz": None, "max_mhz": None})

        def cpu(n, core, core_ms, pkg, pkg_ms, cur_khz=2100000):
            d = "sys/devices/system/cpu/cpu%d/" % n
            for f, v in (("core_throttle_count", core), ("core_throttle_total_time_ms", core_ms),
                         ("package_throttle_count", pkg), ("package_throttle_total_time_ms", pkg_ms)):
                self.tree.write(d + "thermal_throttle/" + f, "%d\n" % v)
            self.tree.write(d + "cpufreq/scaling_cur_freq", "%d\n" % cur_khz)
            self.tree.write(d + "cpufreq/cpuinfo_max_freq", "4500000\n")

        # Intel: counters since boot; the first read only knows the totals
        cpu(0, 2, 900, 5, 1500)
        cpu(1, 1, 400, 5, 1500, cur_khz=900000)
        t = r.read(NOW)["computer"]["throttle"]
        self.assertEqual(t, {"source": "intel", "now": False, "last_t": None, "since_boot": 5,
                             "speed_mhz": 1500, "max_mhz": 4500})
        self.assertFalse(r.read(NOW + 10)["computer"]["throttle"]["now"])    # nothing changed
        cpu(1, 2, 400, 5, 1500)                                               # a core slowdown starts
        t = r.read(NOW + 20)["computer"]["throttle"]
        self.assertEqual((t["now"], t["last_t"]), (True, NOW + 20))
        t = r.read(NOW + 30)["computer"]["throttle"]
        self.assertEqual((t["now"], t["last_t"]), (False, NOW + 20))          # still remembered
        cpu(0, 2, 900, 6, 2700)                                               # a package one ends
        t = r.read(NOW + 40)["computer"]["throttle"]
        self.assertEqual((t["now"], t["last_t"], t["since_boot"]), (True, NOW + 40, 6))
        # the kernel's own limit on the processor counts as slowing now
        self.tree.write("sys/class/thermal/cooling_device0/type", "Processor\n")
        self.tree.write("sys/class/thermal/cooling_device0/cur_state", "2\n")
        self.assertTrue(r.read(NOW + 50)["computer"]["throttle"]["now"])

        # no Intel counters (an AMD CPU): only the kernel's limit is visible
        shutil.rmtree(self.tree.path("sys/devices/system/cpu/cpu0"))
        shutil.rmtree(self.tree.path("sys/devices/system/cpu/cpu1"))
        self.tree.write("sys/class/thermal/cooling_device0/cur_state", "0\n")
        self.tree.write("sys/class/thermal/cooling_device1/type", "Fan\n")
        self.tree.write("sys/class/thermal/cooling_device1/cur_state", "3\n")   # a fan isn't a slowdown
        t = r.read(NOW + 60)["computer"]["throttle"]
        self.assertEqual((t["source"], t["now"], t["since_boot"], t["speed_mhz"]), ("kernel", False, None, None))

    def test_rpc_url_change_refinds(self):
        r = reader_for(self.tree)
        self.assertEqual(r.read(NOW)["proc"]["pid"], NODE_PID)
        r.rpc_url = "http://127.0.0.1:18081"       # another app's node now
        self.assertEqual(r.read(NOW + 10)["proc"]["pid"], 5151)


class InterfaceKindTest(unittest.TestCase):
    def setUp(self):
        self.tree = standard_tree()
        self.addCleanup(self.tree.close)

    def kind(self):
        return reader_for(self.tree).read(NOW)["bind_if"]["kind"]

    def test_kinds(self):
        self.assertEqual(self.kind(), "vpn")                          # WireGuard (uevent, type 65534)
        os.unlink(self.tree.path("sys/class/net/wg0/uevent"))
        self.assertEqual(self.kind(), "vpn")                          # type 65534 alone
        self.tree.write("sys/class/net/wg0/type", "1\n")
        self.assertEqual(self.kind(), "other")                        # an ordinary Ethernet/Wi-Fi card
        self.tree.write("sys/class/net/wg0/tun_flags", "0x1001\n")
        self.assertEqual(self.kind(), "vpn")                          # tun/tap (OpenVPN)


ALL_FLAGS_ARGV = ["/opt/monero/monerod", "--data-dir", "/home/u/.bitmonero", "--rpc-bind-port", "18091",
            "--rpc-restricted-bind-port", "18089", "--p2p-bind-ip", "10.8.0.2", "--p2p-bind-port", "18080",
            "--tx-proxy", "tor,127.0.0.1:9050,16", "--rpc-login", "monero:hunter2", "--out-peers", "32"]


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.tree = FakeRoot()
        self.addCleanup(self.tree.close)
        self.tree.write("proc/stat", "cpu  1 2 3 4\nbtime %d\n" % BTIME)
        self.home = self.tree.path("home/u")
        os.makedirs(os.path.join(self.home, ".bitmonero"))

    def monerod(self, pid, argv, comm="monerod", cwd="/home/u", env=("HOME=/home/u", "LANG=C.UTF-8")):
        self.tree.process(pid, comm, ["/dev/null"], argv, start=NODE_START_TICKS, exe=argv[0])
        self.tree.link("proc/%d/cwd" % pid, cwd)
        self.tree.write("proc/%d/environ" % pid, "\0".join(env) + "\0")

    def conf(self, rel, text):
        self.tree.write("home/u/" + rel, text)

    def find(self, **kw):
        kw.setdefault("uid", self.tree.uid)
        out = host.discover(root=self.tree.dir, home=self.home, **kw)
        json.dumps(out)
        return out

    def test_command_line_setup(self):
        self.monerod(NODE_PID, ALL_FLAGS_ARGV)
        self.monerod(900, ["/usr/bin/bash"], comm="bash")              # not monerod: skipped
        out = self.find()
        self.assertEqual((out["ok"], out["error"]), (True, None))
        clk = host._clk_tck()
        self.assertEqual(out["found"], [{
            "pid": NODE_PID, "exe": "/opt/monero/monerod",
            "started": BTIME + NODE_START_TICKS // clk, "nettype": "mainnet",
            "rpc_url": "http://127.0.0.1:18091", "restricted": False, "login_needed": True,
            "restricted_rpc_url": "http://127.0.0.1:18089", "data_dir": "/home/u/.bitmonero",
            "log_file": "/home/u/.bitmonero/bitmonero.log", "p2p_bind": ["10.8.0.2"], "tx_proxy": True,
            "tx_proxy_tor": True, "out_peers": 32}])
        self.assertNotIn("hunter2", json.dumps(out))                  # the login is never copied

    def test_config_file_only(self):
        self.conf(".bitmonero/bitmonero.conf",
                  "# monerod settings\n"
                  "data-dir=/srv/monero\n"
                  "log-file = monerod.log   # a bare name: in the data dir\n"
                  "rpc-bind-port=18089\n"
                  "restricted-rpc=1\n"
                  "rpc-login=user:secretpw\n"
                  "p2p-bind-ip=0.0.0.0\n"
                  "p2p-bind-ipv6-address=[2001:db8:1::5]\n"
                  "tx-proxy=i2p,127.0.0.1:4447\n"
                  "rpc-ssl=enabled\n")
        self.monerod(NODE_PID, ["monerod", "--detach"])
        f = self.find()["found"][0]
        self.assertEqual(f["nettype"], "mainnet")
        self.assertEqual(f["data_dir"], "/srv/monero")
        self.assertEqual(f["log_file"], "/srv/monero/monerod.log")
        self.assertEqual(f["rpc_url"], "https://127.0.0.1:18089")
        self.assertTrue(f["restricted"])
        self.assertTrue(f["login_needed"])
        self.assertIsNone(f["restricted_rpc_url"])
        self.assertEqual(f["p2p_bind"], ["2001:db8:1::5"])           # 0.0.0.0 is "any", not an address
        self.assertTrue(f["tx_proxy"])
        self.assertFalse(f["tx_proxy_tor"], "an I2P proxy isn't Tor sending")
        self.assertNotIn("secretpw", json.dumps(f))
        # the command line wins over the file, option by option
        self.monerod(NODE_PID, ["monerod", "--rpc-bind-port=18095", "--restricted-rpc", "--log-file",
                                "logs/monerod.log"], cwd="/srv/run")
        f = self.find()["found"][0]
        self.assertEqual(f["rpc_url"], "https://127.0.0.1:18095")
        self.assertEqual(f["log_file"], "/srv/run/logs/monerod.log")   # relative with a folder: from cwd
        self.assertEqual(f["data_dir"], "/srv/monero")

    def test_out_peers(self):
        for argv, conf, expect in ((["monerod"], "", None),                       # monerod's default applies
                                   (["monerod", "--out-peers", "-1"], "", None),  # -1 is "use the default"
                                   (["monerod", "--out-peers=0"], "", 0),
                                   (["monerod"], "out-peers=32\n", 32),
                                   (["monerod", "--out-peers", "16"], "out-peers=32\n", 16),
                                   (["monerod", "--out-peers", "many"], "", None)):
            self.conf(".bitmonero/bitmonero.conf", conf)
            self.monerod(NODE_PID, argv)
            self.assertEqual(self.find()["found"][0]["out_peers"], expect, argv)

    def test_switch_values_in_the_config_file(self):
        for value, expect in (("1", True), ("true", True), ("yes", True), ("", True), ("0", False),
                              ("false", False), ("no", False)):
            self.conf(".bitmonero/bitmonero.conf", "restricted-rpc=%s\n" % value)
            self.monerod(NODE_PID, ["monerod"])
            self.assertEqual(self.find()["found"][0]["restricted"], expect, value)

    def test_testnet(self):
        self.conf(".bitmonero/bitmonero.conf", "rpc-bind-port=11111\n")             # mainnet's: not read
        self.conf(".bitmonero/testnet/bitmonero.conf", "rpc-bind-ip=0.0.0.0\nconfirm-external-bind=1\n")
        self.monerod(NODE_PID, ["monerod", "--testnet", "--data-dir", "/srv/xmr"])
        f = self.find()["found"][0]
        self.assertEqual(f["nettype"], "testnet")
        self.assertEqual(f["rpc_url"], "http://127.0.0.1:28081")
        self.assertEqual(f["data_dir"], "/srv/xmr/testnet")           # any data dir gets testnet/
        self.assertEqual(f["log_file"], "/srv/xmr/testnet/bitmonero.log")
        self.assertFalse(f["restricted"])

    def test_stagenet_and_regtest_defaults(self):
        self.monerod(NODE_PID, ["monerod", "--stagenet"])
        f = self.find()["found"][0]
        self.assertEqual((f["nettype"], f["rpc_url"]), ("stagenet", "http://127.0.0.1:38081"))
        self.assertEqual(f["data_dir"], os.path.join(self.home, ".bitmonero", "stagenet"))
        self.assertEqual(f["log_file"], os.path.join(self.home, ".bitmonero", "stagenet", "bitmonero.log"))
        self.monerod(NODE_PID, ["monerod", "--regtest", "--offline"])
        f = self.find()["found"][0]
        self.assertEqual((f["nettype"], f["rpc_url"]), ("regtest", "http://127.0.0.1:18081"))
        self.assertTrue(f["data_dir"].endswith("/.bitmonero/fake"))

    def test_explicit_config_file_and_ipv6_rpc(self):
        self.tree.write("srv/run/my.conf", "rpc-use-ipv6=1\nrpc-ignore-ipv4=1\nrpc-bind-ipv6-address=::\n")
        self.monerod(NODE_PID, ["monerod", "--config-file", "my.conf",
                                "--rpc-restricted-bind-port", "18089", "--rpc-restricted-bind-ip", "192.168.1.10"],
                     cwd=self.tree.path("srv/run"))
        f = self.find()["found"][0]
        self.assertEqual(f["rpc_url"], "http://[::1]:18081")
        self.assertEqual(f["restricted_rpc_url"], "http://192.168.1.10:18089")

    def test_login_from_the_environment(self):
        self.monerod(NODE_PID, ["monerod"], env=("HOME=/home/u", "RPC_LOGIN=u:topsecret"))
        out = self.find()
        self.assertTrue(out["found"][0]["login_needed"])
        self.assertNotIn("topsecret", json.dumps(out))
        self.monerod(NODE_PID, ["monerod"], env=("RPC_LOGIN=",))     # set but empty: no login
        self.assertFalse(self.find()["found"][0]["login_needed"])

    def test_several_and_none(self):
        self.monerod(5151, ["/opt/otherapp/monerod", "--rpc-bind-port", "18081", "--data-dir", "/home/u/.otherapp"])
        self.monerod(NODE_PID, ALL_FLAGS_ARGV)
        out = self.find()
        self.assertEqual([f["pid"] for f in out["found"]], [NODE_PID, 5151])
        self.assertEqual(out["found"][1]["rpc_url"], "http://127.0.0.1:18081")
        # other users' processes are never candidates
        out = self.find(uid=self.tree.uid + 1)
        self.assertEqual(out, {"ok": True, "error": "No monerod is running under your user", "found": []})

    def test_not_linux_and_no_proc(self):
        with mock.patch.object(host.sys, "platform", "win32"):
            self.assertEqual(self.find(), {"ok": False, "error": "Finding the node only works on Linux",
                                           "found": []})
        empty = tempfile.mkdtemp(prefix="memstara-noproc-")
        self.addCleanup(shutil.rmtree, empty, True)
        self.assertFalse(host.discover(root=empty, home=self.home)["ok"])

    def test_parse_args(self):
        self.assertEqual(host.parse_args(["--detach", "status", "--rpc-bind-port=18091", "--tx-proxy", "a",
                                          "--tx-proxy=b", "--out-peers", "32", "--rpc-bind-port", "1", "-x",
                                          "--data-dir"]),
                         {"detach": True, "rpc-bind-port": "18091", "tx-proxy": ["a", "b"], "out-peers": "32"})
        self.assertEqual(host.parse_config("[section]\n# c\nkey = v # x\nnoequals\nkey=second\n"), {"key": "v"})

    def test_bound_address_from_the_config_file(self):
        tree = standard_tree()
        self.addCleanup(tree.close)
        argv = [a for a in NODE_ARGV if a not in ("--p2p-bind-ip", "10.8.0.2")]
        tree.node(argv=argv)
        tree.write("home/u/.bitmonero/bitmonero.conf", "p2p-bind-ip=10.8.0.2\n")
        h = reader_for(tree).read(NOW)
        self.assertEqual((h["bind_if"]["name"], h["bind_if"]["address"]), ("wg0", "10.8.0.2"))


class MatchNodeTest(unittest.TestCase):
    FOUND = [{"pid": 10, "rpc_url": "http://127.0.0.1:18081"}, {"pid": 20, "rpc_url": "http://[::1]:28081"},
             {"pid": 30, "rpc_url": None}]

    def test_by_pid_first(self):
        self.assertEqual(host.match_node(self.FOUND, 20, "http://127.0.0.1:18081")["pid"], 20)

    def test_by_address(self):
        m = host.match_node
        self.assertEqual(m(self.FOUND, None, "http://127.0.0.1:18081")["pid"], 10)
        self.assertEqual(m(self.FOUND, None, "http://localhost:18081/")["pid"], 10, "loopback names are the same")
        self.assertEqual(m(self.FOUND, 99, "http://127.0.0.1:28081")["pid"], 20, "unknown pid: by address")
        self.assertIsNone(m(self.FOUND, None, "http://127.0.0.1:18089"))
        self.assertIsNone(m(self.FOUND, None, "http://192.168.1.5:18081"), "another machine")
        self.assertIsNone(m([], None, "http://127.0.0.1:18081"))


FIB_TRIE = """Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 127.0.0.0/8 2 0 2
        |-- 127.0.0.1
           /32 host LOCAL
     +-- 192.168.1.0/24 2 0 2
        |-- 192.168.1.0
           /24 link UNICAST
        |-- 192.168.1.20
           /32 host LOCAL
Local:
  +-- 0.0.0.0/0 3 0 5
     |-- 192.168.1.20
        /32 host LOCAL
"""
IF_INET6 = ("00000000000000000000000000000001 01 80 10 80       lo\n"
            "20010db8000100000000000000000014 02 40 00 00     eth0\n"
            "fe80000000000000a00027fffe4e66a1 02 40 20 80     eth0\n")


class RemoteNodeTest(unittest.TestCase):
    """A node on another computer: reached across the network, or through an SSH tunnel."""

    def setUp(self):
        self.tree = standard_tree()
        self.addCleanup(self.tree.close)
        self.tree.write("proc/net/fib_trie", FIB_TRIE)
        self.tree.write("proc/net/if_inet6", IF_INET6)

    def test_own_addresses(self):
        self.assertEqual(host.parse_fib_trie(FIB_TRIE), {"127.0.0.1", "192.168.1.20"})
        self.assertEqual(host.parse_if_inet6(IF_INET6), {"::1", "2001:db8:1::14", "fe80::a000:27ff:fe4e:66a1"})
        self.assertEqual(host.parse_fib_trie(""), set())
        self.assertEqual(host.parse_if_inet6("junk\n"), set())

    def test_another_computers_address(self):
        r = reader_for(self.tree)
        self.assertIsNone(r.read(NOW)["remote"])
        self.assertIsNotNone(r._rtts)
        for url, where in (("http://192.168.1.50:18081", "192.168.1.50:18081"),
                           ("http://[2001:db8:1::99]:18081", "[2001:db8:1::99]:18081")):
            r.rpc_url = url
            h = r.read(NOW + 1)
            self.assertEqual(h["remote"], {"how": "network", "where": where, "via": None}, url)
            self.assertFalse(h["ok"])
            self.assertIn("another computer", h["error"])
            self.assertIsNone(h["computer"])
            self.assertIsNone(h["proc"])
            self.assertIsNone(r._rtts)
        self.assertEqual(set(h), {"ok", "error", "every_s", "t", "proc", "proc_error", "computer", "bind_if",
                                  "tor", "remote"})

    def test_this_computers_own_addresses_are_local(self):
        for url in ("http://192.168.1.20:18091", "http://[2001:db8:1::14]:18091", "http://0.0.0.0:18091",
                    "http://127.0.0.2:18091"):
            h = reader_for(self.tree, url=url).read(NOW)
            self.assertIsNone(h["remote"], url)
            self.assertTrue(h["ok"], url)

    def test_unknown_when_the_address_lists_cant_be_read(self):
        os.remove(self.tree.path("proc/net/fib_trie"))
        os.remove(self.tree.path("proc/net/if_inet6"))
        h = reader_for(self.tree, url="http://192.168.1.50:18091").read(NOW)
        self.assertIsNone(h["remote"])
        self.assertEqual(h["proc_error"], "Nothing is listening on 192.168.1.50:18091")

    def test_names(self):
        r = reader_for(self.tree, url="http://node.lan:18081")
        looked = []

        def resolve(name, answer=["192.168.1.50"]):
            looked.append(name)
            if isinstance(answer, Exception):
                raise answer
            return answer
        r.resolve = resolve
        self.assertEqual(r.read(NOW)["remote"], {"how": "network", "where": "node.lan:18081", "via": None})
        r.read(NOW + 1)
        self.assertEqual(looked, ["node.lan"])      # looked up once, kept a while
        r.read(NOW + host.RESOLVE_S + 1)
        self.assertEqual(looked, ["node.lan"] * 2)
        # a name for this computer, or one that doesn't resolve: read as this computer
        for answer in (["127.0.1.1"], ["fe80::1%eth0", "192.168.1.20"], OSError("no such name"), []):
            r = reader_for(self.tree, url="http://node.lan:18091")
            r.resolve = lambda name, a=answer: resolve(name, a)
            self.assertIsNone(r.read(NOW)["remote"], answer)
        r = reader_for(self.tree, url="http://MyBox.local:18091")
        r.hostname = "mybox"
        r.resolve = lambda name: self.fail("this computer's own name isn't looked up")
        self.assertIsNone(r.read(NOW)["remote"])

    def test_ssh_tunnel(self):
        for comm in ("ssh", "autossh", "socat"):
            self.tree.write("proc/%d/comm" % NODE_PID, comm + "\n")
            r = reader_for(self.tree)
            h = r.read(NOW)
            self.assertEqual(h["remote"], {"how": "tunnel", "where": "127.0.0.1:18091", "via": comm})
            self.assertFalse(h["ok"])
            self.assertIn(comm, h["error"])
            self.assertIsNone(h["proc"])
            self.assertIsNone(h["computer"])
            self.assertIsNone(r.node_pid() if hasattr(r, "node_pid") else r._pid)
        # back to the node itself
        self.tree.write("proc/%d/comm" % NODE_PID, "monerod\n")
        h = r.read(NOW + 1)
        self.assertIsNone(h["remote"])
        self.assertEqual(h["proc"]["pid"], NODE_PID)


class RealSystemTest(unittest.TestCase):
    def test_this_computer(self):
        """The real /proc and /sys of whatever runs the tests: never raises, always the full shape."""
        port = helpers.closed_port()
        r = HostReader("http://127.0.0.1:%d" % port)
        h = r.read(time.time())
        h2 = r.read(time.time() + 1)
        self.assertEqual(set(h2), {"ok", "error", "every_s", "t", "proc", "proc_error", "computer", "bind_if", "tor", "remote"})
        if sys.platform.startswith("linux") and os.path.exists("/proc/stat"):
            self.assertTrue(h["ok"])
            self.assertIsNone(h["proc"])
            self.assertEqual(h["proc_error"], "Nothing is listening on 127.0.0.1:%d" % port)
            self.assertIsInstance(h["computer"]["mem_total_bytes"], int)
        else:
            self.assertFalse(h["ok"])
        json.dumps(h2)


if __name__ == "__main__":
    unittest.main()
