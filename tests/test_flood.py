"""The black marble watch: wallet2's decoy picking, the estimate, reading the counts, the lamp and alert."""

from __future__ import annotations

import bisect
import math
import random
import unittest
from array import array

import helpers
from memstara import flood
from memstara.alerts import DEFAULT_CFG, AlertEngine, empty_obs
from memstara.flood import (BLOCKS_YEAR, CHUNK, REFETCH, RETRY_S, STALE_BLOCKS, WINDOW, FloodWatch, estimate,
                            gammp, picked_cdf)
from memstara.lamps import compute as compute_lamps
from memstara.rpc import RpcError
from memstara.settings import DEFAULTS, cross_field_errors

TOP = 3_780_000
MIN = 60


def wallet_pick(rng, per_output, base, cum, newest_i):
    """One decoy the way gamma_picker::pick does it (wallet2.cpp, v0.18.5.3): the index in cum
    of the block it lands in, -1 for a block before those, or None for a pick past the oldest
    output (the wallet draws again). cum[i]: outputs up to and including block i, base: before
    block 0; newest_i: the newest block a wallet can pick from."""
    x = math.exp(rng.gammavariate(flood.GAMMA_SHAPE, 1 / flood.GAMMA_RATE))
    if x > flood.UNLOCK_S:
        x -= flood.UNLOCK_S
    else:
        x = rng.randrange(flood.RECENT_S)
    n = int(x / per_output)
    total = cum[newest_i]
    if n >= total:
        return None
    index = total - 1 - n
    if index <= base:
        return -1                           # lower_bound lands on a block before cum[0]
    return bisect.bisect_left(cum, index, 0, newest_i + 1)


class MathTest(unittest.TestCase):
    def test_incomplete_gamma(self):
        for x in (0.1, 1.0, 3.0, 10.0):
            self.assertAlmostEqual(gammp(1, x), 1 - math.exp(-x), places=12)
            self.assertAlmostEqual(gammp(2, x), 1 - (1 + x) * math.exp(-x), places=12)
        self.assertEqual(gammp(19.28, 0), 0.0)
        # the series (below a + 1) and the continued fraction (above) meet
        a = flood.GAMMA_SHAPE
        self.assertAlmostEqual(gammp(a, a + 1 - 1e-9), gammp(a, a + 1 + 1e-9), places=8)
        self.assertAlmostEqual(gammp(a, 200.0), 1.0, places=12)

    def test_picked_ages(self):
        self.assertEqual(picked_cdf(0), 0.0)
        # one day and one week (checked against a simulation of wallet2's picker)
        self.assertAlmostEqual(picked_cdf(86400), 0.44219, places=4)
        self.assertAlmostEqual(picked_cdf(604800), 0.70859, places=4)
        # no jump where the recent-spend window ends
        self.assertAlmostEqual(picked_cdf(flood.RECENT_S - 1e-6), picked_cdf(flood.RECENT_S), places=8)
        ts = [1, 100, 1799, 1800, 3600, 86400, 10 ** 6, 10 ** 8, 10 ** 10]
        vals = [picked_cdf(t) for t in ts]
        self.assertEqual(vals, sorted(vals))
        # 1 in 2,000 picks is older than about 317 years (a numerical integration agrees)
        self.assertAlmostEqual(vals[-1], 0.99950, places=5)
        self.assertGreater(picked_cdf(1e30), 1 - 1e-12)

    def test_picked_ages_match_the_wallet(self):
        rng = random.Random(7)
        ages = []
        for _ in range(40000):
            x = math.exp(rng.gammavariate(flood.GAMMA_SHAPE, 1 / flood.GAMMA_RATE))
            ages.append(x - flood.UNLOCK_S if x > flood.UNLOCK_S else rng.randrange(flood.RECENT_S))
        ages.sort()
        for t in (600, 1800, 7200, 86400, 604800, 3 * 10 ** 7):
            seen = bisect.bisect_left(ages, t) / len(ages)
            self.assertAlmostEqual(seen, picked_cdf(t), delta=0.01, msg=t)


def chain(days_flooded=0.0, usual=50, extra=150, length=WINDOW + 50):
    """Counts for blocks TOP - length + 1 .. TOP: `usual` in each, plus `extra` in the newest
    days_flooded days' worth of blocks."""
    start = TOP - length + 1
    flooded = int(days_flooded * flood.DAY)
    counts = array("q", (usual + (extra if TOP - h < flooded else 0) for h in range(start, TOP + 1)))
    return counts, start


class EstimateTest(unittest.TestCase):
    def test_a_steady_chain_has_nothing_to_find(self):
        counts, start = chain()
        e = estimate(counts, start, 50_000_000, TOP)
        self.assertEqual((e["share"], e["rings"], e["volume_x"]), (0.0, 16.0, 1.0))
        self.assertEqual((e["usual_day"], e["last_day"], e["height"]), (36000, 36000, TOP))
        self.assertEqual(e["days"], [36000] * flood.SHOW_DAYS)

    def test_a_flood(self):
        counts, start = chain(days_flooded=3, extra=200)
        e = estimate(counts, start, 50_000_000, TOP)
        self.assertEqual(e["volume_x"], 5.0)
        self.assertLess(e["rings"], 8)
        self.assertGreater(e["rings"], 2)
        self.assertAlmostEqual(e["rings"], 1 + 15 * (1 - e["share"]), places=1)
        self.assertEqual(e["days"][-3:], [180000] * 3)
        self.assertEqual(e["days"][:-3], [36000] * (flood.SHOW_DAYS - 3))

    def test_a_quiet_spell_is_not_negative(self):
        counts, start = chain(days_flooded=5, extra=-30)
        e = estimate(counts, start, 50_000_000, TOP)
        self.assertEqual((e["share"], e["rings"]), (0.0, 16.0))
        self.assertEqual(e["volume_x"], 0.4)

    def test_matches_simulated_wallets(self):
        # picks made the way wallet2 makes them land on the extra outputs as often as the
        # estimate says (the usual day is exact here, so only the decoy weighting is tested)
        counts, start = chain(days_flooded=2, extra=150)
        base = 40_000_000
        e = estimate(counts, start, base, TOP)
        cum, run = [], base
        for c in counts:
            run += c
            cum.append(run)
        year = cum[-1] - cum[-1 - BLOCKS_YEAR]
        per_output = flood.TARGET_S * BLOCKS_YEAR / year
        newest_i = len(counts) - 1 - flood.NEWEST_OFF
        rng = random.Random(11)
        hits = picks = 0
        while picks < 30000:
            i = wallet_pick(rng, per_output, base, cum, newest_i)
            if i is None:
                continue
            if i < 0:
                picks += 1                  # older than the counts: a usual output
                continue
            picks += 1
            extra = counts[i] - 50
            if extra > 0 and rng.random() < extra / counts[i]:
                hits += 1
        self.assertAlmostEqual(hits / picks, e["share"], delta=0.012)

    def test_needs_a_year(self):
        counts, start = chain(length=WINDOW - 1)
        with self.assertRaises(ValueError):
            estimate(counts, start, 0, TOP)


class FakeNode:
    """Answers get_output_distribution like monerod (amount 0, per block): 50 + h % 7 outputs in block h."""

    def __init__(self, top=TOP):
        self.top = top
        self.calls = []
        self.fail = None             # an RpcError to raise
        self.start_at = None         # the first block with RingCT outputs, when a test sets one
        self.answer = None           # a function that changes the answer

    @staticmethod
    def per_block(h):
        return 50 + h % 7

    @staticmethod
    def before(h):
        """Outputs in blocks 0 to h - 1."""
        return 50 * h + 21 * (h // 7) + sum(k % 7 for k in range(h - h % 7, h))

    def __call__(self, method, params):
        self.calls.append((method, params["from_height"], params["to_height"]))
        assert method == "get_output_distribution" and params["amounts"] == [0] and params["cumulative"] is False
        if self.fail:
            raise self.fail
        frm, to = params["from_height"], params["to_height"]
        assert to <= self.top
        start = max(frm, self.start_at or 0)
        res = {"distributions": [{"amount": 0, "base": self.before(start), "start_height": start,
                                  "distribution": [self.per_block(h) for h in range(start, to + 1)],
                                  "binary": False, "compress": False}], "status": "OK"}
        return self.answer(res) if self.answer else res


def fill(w, node, now=0.0, top_hash="t0", synced=True):
    """Polls until the watch has read the year (or stopped asking)."""
    for _ in range(WINDOW // CHUNK + 3):
        n = len(node.calls)
        w.update(node, node.top, top_hash, synced, now)
        if len(node.calls) == n:
            break
    return w.part()


class WatchTest(unittest.TestCase):
    def test_reads_the_year_a_chunk_a_poll(self):
        node, w = FakeNode(), FloodWatch()
        self.assertEqual(w.part(), {"state": "filling", "progress": 0.0})
        w.update(node, TOP, "t0", True, 0.0)
        self.assertEqual(node.calls, [("get_output_distribution", TOP - WINDOW + 1, TOP - WINDOW + CHUNK)])
        self.assertEqual(w.part(), {"state": "filling", "progress": round(CHUNK / WINDOW, 2)})
        part = fill(w, node)
        self.assertEqual(len(node.calls), math.ceil(WINDOW / CHUNK))
        self.assertEqual(part["state"], "ok")
        self.assertEqual(part["height"], TOP)
        self.assertIsNone(part["why"])
        self.assertGreater(part["rings"], 15)
        # the same top again: nothing to read
        w.update(node, TOP, "t0", True, 5.0)
        self.assertEqual(len(node.calls), math.ceil(WINDOW / CHUNK))

    def test_new_blocks_and_replaced_ones(self):
        node, w = FakeNode(), FloodWatch()
        fill(w, node)
        n = len(node.calls)
        node.top = TOP + 1
        w.update(node, TOP + 1, "t1", True, 120.0)
        self.assertEqual(node.calls[n:], [("get_output_distribution", TOP - REFETCH + 1, TOP + 1)])
        self.assertEqual(w.part()["height"], TOP + 1)
        # the top replaced at the same height: the newest blocks again
        w.update(node, TOP + 1, "t1b", True, 125.0)
        self.assertEqual(node.calls[-1], ("get_output_distribution", TOP + 1 - REFETCH + 1, TOP + 1))
        # more blocks, then a shorter chain (blocks popped): the newest again
        node.top = TOP + 100
        w.update(node, TOP + 100, "t2", True, 130.0)
        self.assertEqual(node.calls[-1], ("get_output_distribution", TOP + 1 - REFETCH + 1, TOP + 100))
        node.top = TOP + 95
        w.update(node, TOP + 95, "t3", True, 135.0)
        self.assertEqual(node.calls[-1], ("get_output_distribution", TOP + 95 - REFETCH + 1, TOP + 95))
        self.assertEqual(w.part()["height"], TOP + 95)
        self.assertEqual(w._filled_to(), TOP + 95)
        self.assertEqual(w.part()["rings"], fill(FloodWatch(), FakeNode(TOP + 95))["rings"])

    def test_popped_below_the_oldest_count(self):
        # right after the first read nothing older than a year back is kept, so a shorter
        # chain needs the year again
        node, w = FakeNode(), FloodWatch()
        fill(w, node)
        node.top = TOP - 5
        w.update(node, TOP - 5, "t1", True, 120.0)
        self.assertEqual(node.calls[-1], ("get_output_distribution", TOP - 5 - WINDOW + 1, TOP - 5 - WINDOW + CHUNK))
        self.assertEqual(w.part(), {"state": "filling", "progress": round(CHUNK / WINDOW, 2)})
        self.assertEqual(fill(w, node, 125.0, "t1")["height"], TOP - 5)

    def test_keeps_only_what_it_needs_and_the_sums_stay_right(self):
        node, w = FakeNode(), FloodWatch()
        fill(w, node)
        for k in range(1, 4):
            node.top = TOP + k * 1500       # past TRIM_SLACK: the oldest counts go
            w.update(node, node.top, "t%d" % k, True, k * 1000.0)
        self.assertLessEqual(len(w.counts), WINDOW + flood.TRIM_SLACK)
        self.assertEqual(w.base, node.before(w.start))
        fresh = estimate(array("q", (node.per_block(h) for h in range(node.top - WINDOW + 1, node.top + 1))),
                         node.top - WINDOW + 1, node.before(node.top - WINDOW + 1), node.top)
        self.assertEqual(dict(w.part(), state=None, why=None), dict(fresh, state=None, why=None))

    def test_counts_that_dont_join_start_again(self):
        logs = []
        node, w = FakeNode(), FloodWatch(logs.append)
        fill(w, node)
        node.answer = lambda res: dict(res, distributions=[dict(res["distributions"][0],
                                                                base=res["distributions"][0]["base"] + 1)])
        node.top = TOP + 1
        w.update(node, TOP + 1, "t1", True, 120.0)
        self.assertIn("reading the year again", logs[-1])
        self.assertEqual(w.part(), {"state": "filling", "progress": 0.0})
        node.answer = None
        self.assertEqual(fill(w, node, 125.0, "t1")["state"], "ok")

    def test_waits_while_syncing_or_bootstrapping(self):
        node, w = FakeNode(), FloodWatch()
        w.update(node, TOP, "t0", False, 0.0)
        self.assertEqual(w.part(), {"state": "off", "why": "sync"})
        w.update(node, TOP, "t0", None, 0.0)
        self.assertEqual(w.part(), {"state": "off", "why": "sync"})
        w.update(node, TOP, "t0", True, 0.0, bootstrap=True)
        self.assertEqual(w.part(), {"state": "off", "why": "bootstrap"})
        self.assertEqual(node.calls, [])
        fill(w, node)
        w.update(node, TOP, "t0", False, 5.0)
        self.assertEqual(w.part(), {"state": "off", "why": "sync"}, "the last estimate isn't shown while catching up")
        w.update(node, TOP, "t0", True, 10.0)
        self.assertEqual(w.part()["state"], "ok")

    def test_a_short_chain(self):
        node, w = FakeNode(top=WINDOW - 2), FloodWatch()
        w.update(node, node.top, "t0", True, 0.0)
        self.assertEqual(w.part(), {"state": "off", "why": "short"})
        self.assertEqual(node.calls, [])
        # RingCT outputs starting after the block asked for: off, and asked again in 10 min
        node, w = FakeNode(), FloodWatch()
        node.start_at = TOP - WINDOW + 100
        w.update(node, TOP, "t0", True, 0.0)
        self.assertEqual(w.part(), {"state": "off", "why": "short"})
        w.update(node, TOP, "t0", True, 10 * RETRY_S - 1)
        self.assertEqual(len(node.calls), 1)
        self.assertEqual(w.part(), {"state": "off", "why": "short"})
        node.start_at = None
        w.update(node, TOP, "t0", True, 10 * RETRY_S)
        self.assertEqual(len(node.calls), 2)

    def test_failures(self):
        node, w = FakeNode(), FloodWatch()
        node.fail = RpcError("No answer within 8 s", "no answer in 8 s", "timeout")
        w.update(node, TOP, "t0", True, 0.0)
        self.assertEqual(w.part(), {"state": "error", "why": "no answer in 8 s"})
        w.update(node, TOP, "t0", True, RETRY_S - 1)
        self.assertEqual(len(node.calls), 1, "not asked again before RETRY_S")
        node.fail = None
        self.assertEqual(fill(w, node, RETRY_S)["state"], "ok")
        # a failure after an estimate keeps it, with the reason, until it's too far behind
        node.fail = RpcError("refused", "refused (403)", "http")
        node.top = TOP + 1
        w.update(node, TOP + 1, "t1", True, 500.0)
        part = w.part()
        self.assertEqual((part["state"], part["why"], part["height"]), ("ok", "refused (403)", TOP))
        node.top = TOP + STALE_BLOCKS + 1
        w.update(node, node.top, "t2", True, 500.0 + RETRY_S)
        self.assertEqual(w.part(), {"state": "error", "why": "refused (403)"})

    def test_answers_that_dont_add_up(self):
        for bad in ({"status": "OK"}, {"distributions": []}, {"distributions": [{"amount": 5}]},
                    {"distributions": [{"amount": 0, "start_height": TOP - WINDOW + 1, "base": 0,
                                        "distribution": [1, 2]}]},
                    {"distributions": [{"amount": 0, "start_height": TOP - WINDOW + 1, "base": 0,
                                        "distribution": [-1] * CHUNK}]},
                    {"distributions": [{"amount": 0, "start_height": TOP - WINDOW + 1, "base": 0,
                                        "distribution": [True] * CHUNK}]},
                    {"distributions": [{"amount": 0, "start_height": TOP - WINDOW, "base": 0,
                                        "distribution": [1] * CHUNK}]}):
            node, w = FakeNode(), FloodWatch()
            node.answer = lambda res, bad=bad: bad
            w.update(node, TOP, "t0", True, 0.0)
            self.assertEqual(w.part()["state"], "error", bad)


class LampAndAlertTest(unittest.TestCase):
    def lamp(self, fl, answering=True):
        rows = compute_lamps({"answering": answering, "node": {}, "chain": {"flood": fl}, "alerts": {}})
        return {i: (s, v) for i, s, v in rows}["flood"]

    def test_lamp_words(self):
        self.assertEqual(self.lamp({"state": "ok", "rings": 13.6}), ("ok", "RING ≈ 14 OF 16"))
        self.assertEqual(self.lamp({"state": "ok", "rings": 16.0}), ("ok", "RING ≈ 16 OF 16"))
        self.assertEqual(self.lamp({"state": "filling", "progress": 0.46}), ("off", "READING 46%"))
        self.assertEqual(self.lamp({"state": "off", "why": "sync"}), ("off", "WAITS FOR SYNC"))
        self.assertEqual(self.lamp({"state": "off", "why": "bootstrap"}), ("off", "WAITS FOR SYNC"))
        self.assertEqual(self.lamp({"state": "off", "why": "short"}), ("off", "CHAIN TOO SHORT"))
        self.assertEqual(self.lamp({"state": "error", "why": "x"}), ("off", "CAN'T READ"))
        self.assertEqual(self.lamp({"state": "off", "why": "starting"}), ("off", "NO DATA"))
        self.assertEqual(self.lamp({"state": "ok", "rings": 9.0}, answering=False), ("off", "NO DATA"))

    def run_alerts(self, h, fl, advance):
        h["clock"].advance(advance)
        obs = empty_obs()
        obs.update({"answering": True, "synced": True, "out_peers": 12, "total_out": 12, "flood": fl})
        alerts, events = h["engine"].evaluate(obs, DEFAULT_CFG, h["clock"]())
        h["events"].extend(events)
        return {a["id"]: a for a in alerts}

    def test_alert_after_half_an_hour(self):
        h = {"clock": helpers.FakeClock(), "engine": AlertEngine(), "events": []}
        warn = {"state": "ok", "rings": 10.4, "share": 0.373}
        self.assertNotIn("flood", self.run_alerts(h, warn, 0))
        self.assertNotIn("flood", self.run_alerts(h, warn, 29 * MIN))
        a = self.run_alerts(h, warn, MIN)["flood"]
        self.assertEqual((a["level"], a["title"]), ("warn", "BLACK MARBLE WATCH"))
        self.assertEqual(a["text"], "A recent surge of new outputs would supply about 37% of a new transaction's "
                                    "decoys. If one sender made them, its 16-member ring works like about 10")
        # worse: a problem once that has lasted half an hour too
        bad = {"state": "ok", "rings": 5.2, "share": 0.72}
        self.assertEqual(self.run_alerts(h, bad, MIN)["flood"]["level"], "warn")
        a = self.run_alerts(h, bad, 30 * MIN)["flood"]
        self.assertEqual(a["level"], "bad")
        self.assertTrue(a["text"].endswith("works like about 5: payments where privacy matters can wait for it "
                                           "to clear"), a["text"])
        # no estimate for a while (the node catching up): held, neither shown nor cleared, and
        # back as it was when the estimate comes back
        n = len(h["events"])
        self.assertNotIn("flood", self.run_alerts(h, {"state": "off", "why": "sync"}, 20 * MIN))
        self.assertEqual(self.run_alerts(h, bad, MIN)["flood"]["level"], "bad")
        self.assertEqual(len(h["events"]), n, "no clear or new raise for the pause")
        # gone: cleared
        self.assertEqual(self.run_alerts(h, {"state": "ok", "rings": 15.0, "share": 0.07}, 31 * MIN), {})
        self.assertEqual(h["events"][-1]["text"], "Cleared: BLACK MARBLE WATCH")

    def test_limits_are_settings(self):
        self.assertEqual((DEFAULTS["thresholds.flood_warn"], DEFAULTS["thresholds.flood_bad"],
                          DEFAULTS["grace.flood_min"]), (11, 8, 30))
        v = dict(DEFAULTS, **{"thresholds.flood_bad": 11})
        self.assertEqual(cross_field_errors(v, {"thresholds.flood_bad"}),
                         {"thresholds.flood_bad": "must be lower than the heads-up level (11)"})
        v = dict(DEFAULTS, **{"thresholds.flood_warn": 8})
        self.assertEqual(cross_field_errors(v, {"thresholds.flood_warn"}),
                         {"thresholds.flood_warn": "must be higher than the problem level (8)"})


if __name__ == "__main__":
    unittest.main()
