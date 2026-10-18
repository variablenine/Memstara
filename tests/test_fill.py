"""The CHAIN screen's next block (web/js/heroes/fill.js), run under node when it's installed.

pick() is checked against monerod's own way of filling a block, written out again here
in Python from src/cryptonote_core/tx_pool.cpp (fill_block_template) and
src/cryptonote_basic/cryptonote_basic_impl.cpp (get_block_reward), release-v0.18.
pack() is checked for what the picture relies on: every tile's volume is its share, no
two tiles overlap, and none stands outside the box's floor.
"""

from __future__ import annotations

import json
import random
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


def monerod_fill(rows, max_weight, base):
    """The ids fill_block_template takes (v5 and later): sorted by fee per byte, highest
    first, then the earliest received; each one added if it fits and the coinbase doesn't drop."""
    median = max_weight // 2
    room = 2 * median - RESERVED
    cand = sorted((r for r in rows if not r[4] & 3), key=lambda r: (-(r[2] / r[3]), r[1], r[0]))
    total = fee = 0
    best = block_reward(base, median, 0)
    took = []
    for r in cand:
        if room < total + r[3]:
            continue
        reward = block_reward(base, median, total + r[3])
        if reward is None or reward + fee + r[2] < best:
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
            self.assertTrue(all(r[4] & 3 for r in res["out"]))

    def test_order_fee_per_byte_then_oldest(self):
        rows = [["a", 30, 2000, 1000, 0], ["b", 10, 4000, 2000, 0], ["c", 20, 9000, 3000, 0], ["d", 5, 900, 1000, 0]]
        (res,) = run(("pick", [rows, 600000, None]))
        self.assertEqual([r[0] for r in res["inside"]], ["c", "b", "a", "d"])

    def test_room_and_what_is_left(self):
        # past the room, a big one is passed over and a smaller one after it still goes in
        rows = [["big", 1, 10 ** 10, 500000, 0], ["huge", 2, 10 ** 9, 200000, 0], ["small", 3, 10 ** 6, 2000, 0]]
        (res,) = run(("pick", [rows, 600000, None]))
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

    def test_squeeze(self):
        (res,) = run(("squeeze", [[[0.3, 0.3, 0.3, 0.3], 5], 0, 1]))
        self.assertLessEqual(max(t["y1"] for t in res["tiles"]), 1 + 1e-9)


if __name__ == "__main__":
    unittest.main()
