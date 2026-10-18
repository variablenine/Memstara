"""The CHAIN screen's next block (web/js/heroes/fill.js), run under node when it's installed.

pick() is checked against monerod's own way of filling a block, written out again here
in Python from src/cryptonote_core/tx_pool.cpp (fill_block_template) and
src/cryptonote_basic/cryptonote_basic_impl.cpp (get_block_reward), release-v0.18.
pack() is checked for what the picture relies on: every tile's volume is its share, no
two tiles overlap, none stands outside the box's floor, and the stack stands about as high
as the box is full; stack() for the full-reward line.
"""

from __future__ import annotations

import json
import random
import struct
import shutil
import subprocess
import unittest

import helpers  # noqa: F401  (sets up sys.path)
from helpers import ROOT

FILL = ROOT / "web" / "js" / "heroes" / "fill.js"
NODE = shutil.which("node")

RUN = """
import * as fill from %s;
let s = "";
process.stdin.on("data", (d) => (s += d));
process.stdin.on("end", () => {
  const q = JSON.parse(s);
  const out = q.map(([fn, args]) => {
    if (fn === "pick") return fill.pick(args[0], args[1], args[2], args[3] ? new Set(args[3]) : null);
    if (fn === "pack") return fill.pack(...args);
    if (fn === "squeeze") return fill.squeeze(fill.pack(...args[0]), args[1], args[2]);
    return fill[fn](...args);
  });
  process.stdout.write(JSON.stringify(out));
});
""" % json.dumps(FILL.as_uri())


def run(*calls):
    """[(function, [args])] -> each one's result, from one node run."""
    res = subprocess.run([NODE, "--input-type=module", "-e", RUN], input=json.dumps(calls),
                         capture_output=True, text=True, timeout=60)
    if res.returncode:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


# ---- monerod's rules, in Python (integer arithmetic, as monerod does it)
RESERVED = 600   # CRYPTONOTE_COINBASE_BLOB_RESERVED_SIZE


def block_reward(base, median, weight):
    if weight <= median:
        return base
    if weight > 2 * median:
        return None
    return base * ((2 * median - weight) * weight) // median // median


def as_float32(n):
    """An amount as monerod's template_accept_threshold makes it: times a float 1.0f."""
    return int(struct.unpack("f", struct.pack("f", float(n)))[0])


def monerod_fill(rows, max_weight, base):
    """The ids fill_block_template takes (v5 and later): sorted by fee per byte, highest
    first, then the earliest received; each one added if it fits and the coinbase doesn't
    drop. A double spend seen (flag 1) doesn't count, as monerod doesn't look at it. Rows
    not relayed yet (flag 2) are left out as pick() leaves them out, because no other
    miner has them; monerod's own template on this node would still take one sent with
    do_not_relay (relay_method::none counts in its legacy category)."""
    median = max_weight // 2
    room = 2 * median - RESERVED
    cand = sorted((r for r in rows if not r[4] & 2), key=lambda r: (-(r[2] / r[3]), r[1], r[0]))
    total = fee = 0
    best = block_reward(base, median, 0)
    took = []
    for r in cand:
        if room < total + r[3]:
            continue
        reward = block_reward(base, median, total + r[3])
        if reward is None or reward + fee + r[2] < as_float32(best):
            continue
        took.append(r[0])
        total += r[3]
        fee += r[2]
        best = reward + fee
    return took


def rows_for(rng, n, fee_scale):
    """Made-up pool rows: [id, arrived, fee, weight, flags]."""
    out = []
    for i in range(n):
        w = rng.choice([1500, 1530, 2100, 2900, 4200, 6100, 14000, 52000])
        per = rng.choice([20, 20, 20, 80, 80, 320, 4000]) * fee_scale
        flags = rng.choice([0] * 30 + [1, 2, 4])
        out.append(["%016x" % rng.getrandbits(64), 1790000000 + rng.randrange(3600), w * per + rng.randrange(1000), w, flags])
    return out


@unittest.skipUnless(NODE, "node isn't installed")
class PickTest(unittest.TestCase):
    BASE = 600000000000   # 0.6 XMR, the tail emission

    def test_takes_what_monerod_takes(self):
        rng = random.Random(5)
        cases = []
        for n, scale, cap in ((40, 1, 600000), (400, 1, 600000), (900, 1, 600000), (900, 25, 600000),
                              (700, 300, 600000), (1200, 1, 1200000), (300, 1000, 600000)):
            rows = rows_for(rng, n, scale)
            cases.append((rows, cap))
        got = run(*[("pick", [rows, cap, self.BASE]) for rows, cap in cases])
        for (rows, cap), res in zip(cases, got):
            want = monerod_fill(rows, cap, self.BASE)
            self.assertEqual([r[0] for r in res["inside"]], want, "n=%d cap=%d" % (len(rows), cap))
            self.assertLessEqual(res["weight"], cap - RESERVED)
            self.assertEqual(len(res["inside"]) + len(res["left"]) + len(res["out"]), len(rows))
            self.assertTrue(all(r[4] & 2 for r in res["out"]))

    def test_order_fee_per_byte_then_oldest(self):
        rows = [["a", 30, 2000, 1000, 0], ["b", 10, 4000, 2000, 0], ["c", 20, 9000, 3000, 0], ["d", 5, 900, 1000, 0]]
        (res,) = run(("pick", [rows, 600000, None]))
        self.assertEqual([r[0] for r in res["inside"]], ["c", "b", "a", "d"])

    def test_room_and_what_is_left(self):
        # past the room, a big one is passed over and a smaller one after it still goes in
        # (a tiny reward, so the fees easily pay for going past the full-reward line)
        rows = [["big", 1, 10 ** 10, 500000, 0], ["huge", 2, 10 ** 9, 200000, 0], ["small", 3, 10 ** 6, 2000, 0]]
        (res,) = run(("pick", [rows, 600000, 1000]))
        self.assertEqual([r[0] for r in res["inside"]], ["big", "small"])
        self.assertEqual([r[0] for r in res["left"]], ["huge"])

    def test_past_the_median_only_while_the_fee_pays(self):
        # 299 kB of high-fee transactions; then a cheap one past the median isn't worth
        # the penalty, a dear one is
        fill_rows = [["f%03d" % i, 1, 10 ** 10, 1000, 0] for i in range(299)]
        cheap = ["cheap", 2, 30000 * 20, 30000, 0]
        dear = ["dear", 3, 30000 * 300000, 30000, 0]
        (a, b) = run(("pick", [fill_rows + [cheap], 600000, self.BASE]), ("pick", [fill_rows + [dear], 600000, self.BASE]))
        self.assertNotIn("cheap", [r[0] for r in a["inside"]])
        self.assertIn("dear", [r[0] for r in b["inside"]])
        self.assertEqual([r[0] for r in a["inside"]], monerod_fill(fill_rows + [cheap], 600000, self.BASE))
        self.assertEqual([r[0] for r in b["inside"]], monerod_fill(fill_rows + [dear], 600000, self.BASE))

    def test_a_double_spend_seen_still_goes_in(self):
        # monerod marks the one already waiting when another tries to spend the same thing;
        # the marked one is still good and goes in. One not passed on yet is left out
        rows = [["seen", 1, 4 * 10 ** 7, 1500, 1], ["plain", 2, 3 * 10 ** 7, 1500, 0], ["mine", 3, 9 * 10 ** 7, 1500, 2]]
        (res,) = run(("pick", [rows, 600000, self.BASE]))
        self.assertEqual([r[0] for r in res["inside"]], ["seen", "plain"])
        self.assertEqual([r[0] for r in res["out"]], ["mine"])

    def test_the_threshold_is_a_float(self):
        # 199 x 1500 B, then 4000 B past the 300 kB line whose fee falls 1000 piconero short
        # of the reward it costs: under the exact best so far, not under it as a float
        rows = [["f%03d" % i, 1, 4 * 10 ** 7, 1500, 0] for i in range(199)] + [["edge", 2, 41665667, 4000, 0]]
        (res,) = run(("pick", [rows, 600000, self.BASE]))
        self.assertIn("edge", [r[0] for r in res["inside"]])
        self.assertEqual([r[0] for r in res["inside"]], monerod_fill(rows, 600000, self.BASE))

    def test_without_a_reward_it_stops_at_the_line(self):
        rows = [["t%03d" % i, i, 20000 * 1540, 1540, 0] for i in range(400)]
        (res,) = run(("pick", [rows, 600000, None]))
        self.assertLessEqual(res["weight"], 300000)
        self.assertGreater(res["weight"], 300000 - 1540)

    def test_skips_taken_and_bad_rows(self):
        rows = [["a", 1, 1000, 1000, 0], ["b", 1, 1000, 1000, 0], ["c", 1, 1000, 0, 0], "x", ["d", 1, -5, 1000, 0]]
        (res,) = run(("pick", [rows, 600000, None, ["b"]]))
        self.assertEqual([r[0] for r in res["inside"]], ["a"])

    def test_reward(self):
        got = run(("rewardAt", [self.BASE, 300000, 250000]), ("rewardAt", [self.BASE, 300000, 450000]),
                  ("rewardAt", [self.BASE, 300000, 600001]))
        self.assertEqual(got[0], self.BASE)
        self.assertAlmostEqual(got[1] / block_reward(self.BASE, 300000, 450000), 1, places=9)
        self.assertIsNone(got[2])


@unittest.skipUnless(NODE, "node isn't installed")
class PackTest(unittest.TestCase):
    def check(self, fracs, res, g, lid=0.0):
        vol = 0
        for t in res["tiles"]:
            self.assertGreaterEqual(t["x0"], 0)
            self.assertLessEqual(t["x1"], 1 + 1e-9)
            self.assertGreaterEqual(t["z0"], 0)
            self.assertLessEqual(t["z1"], 1 + 1e-9)
            self.assertGreaterEqual(t["y0"], lid - 1e-9)
            # each tile stands on whole columns
            for v in (t["x0"], t["x1"], t["z0"], t["z1"]):
                self.assertAlmostEqual(v * g, round(v * g), places=9)
            vol += (t["x1"] - t["x0"]) * (t["z1"] - t["z0"]) * (t["y1"] - t["y0"])
        self.assertAlmostEqual(vol, sum(fracs[:res["placed"]]), places=9)
        ts = res["tiles"]
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                a, b = ts[i], ts[j]
                over = all(min(a[k + "1"], b[k + "1"]) - max(a[k + "0"], b[k + "0"]) > 1e-9 for k in "xzy")
                self.assertFalse(over, (i, j))

    def test_tiles_keep_their_volume_and_never_overlap(self):
        rng = random.Random(9)
        for n, scale in ((5, 0.2), (60, 0.01), (250, 0.003), (30, 0.05)):
            fracs = [rng.uniform(0.2, 1.8) * scale for _ in range(n)]
            (res,) = run(("pack", [fracs, 5]))
            self.assertEqual(res["placed"], n)
            self.check(fracs, res, 5)

    def test_a_big_one_gets_a_wide_floor(self):
        (res,) = run(("pack", [[0.25], 5]))
        t = res["tiles"][0]
        self.assertLessEqual(t["y1"] - t["y0"], 1.5 * (t["x1"] - t["x0"]) + 1e-9)

    def test_above_the_lid_and_the_stop(self):
        fracs = [0.01] * 400
        (res,) = run(("pack", [fracs, 5, 1, 1.5]))
        self.assertLess(res["placed"], 400)
        self.assertTrue(all(t["y0"] < 1.5 for t in res["tiles"]))
        self.check(fracs, res, 5, lid=1)

    def test_the_stack_stands_about_as_high_as_the_box_is_full(self):
        # a big transaction lies wider rather than standing tall (it would poke past the
        # full-reward line in a box a third full)
        for fracs in ([52000 / 600000] + [1500 / 600000] * 100, [1500 / 600000] * 100 + [52000 / 600000],
                      [150000 / 600000], [0.0035, 0.0025, 0.007, 0.0048, 0.0101, 0.0233] * 25):
            (res,) = run(("pack", [fracs, 5]))
            self.check(fracs, res, 5)
            self.assertLess(res["top"], sum(fracs) * 1.25 + 0.03, (fracs[:3], res["top"], sum(fracs)))

    def test_a_block_under_the_line_stands_about_as_high_as_it_is_full(self):
        # a third full, two big ones taken after the small ones: stood on the ring once
        ws = [1500, 1530, 1530, 1500, 2900, 1530, 1530, 1540, 2100, 1530, 2100, 2100, 1540, 2100, 1530,
              41220, 111237, 2900]
        cases = [[w / 600000 for w in ws]]
        rng = random.Random(8)
        for _ in range(300):
            cap = rng.choice([600000, 1200000])
            fr, full, target = [], 0, rng.uniform(0.01, 0.5)
            while True:
                u = rng.random()
                w = rng.uniform(1500, 2900) if u < 0.8 else rng.uniform(4000, 14000) if u < 0.96 else rng.uniform(30000, 150000)
                if full + w / cap > target:
                    break
                fr.append(w / cap)
                full += w / cap
            if fr:
                cases.append(fr)
        res = run(*[("stack", [fr, 5, 0.5]) for fr in cases])
        self.assertLess(res[0]["top"], 0.4)
        kept = []
        for fr, r in zip(cases, res):
            ts = r["tiles"]
            self.assertLessEqual(r["top"], min(0.5, sum(fr) + 0.1) + 1e-9)
            # in the order given: every tile is its share, squeezed by the same amount
            ks = [(t["x1"] - t["x0"]) * (t["z1"] - t["z0"]) * (t["y1"] - t["y0"]) / f for t, f in zip(ts, fr)]
            self.assertAlmostEqual(min(ks), max(ks), places=6)
            kept.append(ks[0])
            for i in range(len(ts)):
                for j in range(i + 1, len(ts)):
                    a, b = ts[i], ts[j]
                    self.assertFalse(all(min(a[k + "1"], b[k + "1"]) - max(a[k + "0"], b[k + "0"]) > 1e-9 for k in "xzy"))
        self.assertGreater(sum(kept) / len(kept), 0.95, "hardly ever squeezed much")

    def test_stack_splits_at_the_line(self):
        rng = random.Random(4)
        fracs = [rng.choice([1500, 2100, 2900, 14000, 52000]) / 600000 for _ in range(140)]
        cum, n = 0, 0
        while n < len(fracs) and cum + fracs[n] <= 0.5:
            cum += fracs[n]
            n += 1
        self.assertLess(n, len(fracs))
        (res,) = run(("stack", [fracs, 5, 0.5]))
        self.assertEqual(res["placed"], len(fracs))
        self.assertLessEqual(max(t["y1"] for t in res["tiles"][:n]), 0.5 + 1e-9)
        self.assertGreaterEqual(min(t["y0"] for t in res["tiles"][n:]), max(t["y1"] for t in res["tiles"][:n]) - 1e-9)
        self.assertLessEqual(res["top"], 1 + 1e-9)

    def test_squeeze(self):
        (res,) = run(("squeeze", [[[0.3, 0.3, 0.3, 0.3], 5], 0, 1]))
        self.assertLessEqual(max(t["y1"] for t in res["tiles"]), 1 + 1e-9)


if __name__ == "__main__":
    unittest.main()
