"""pool.py: each waiting transaction's fee and weight read from its split hex, and the list kept
from poll to poll. The transactions are real ones made on a regtest node (tests/fixtures/
pool_txs.json), with the fee, weight and size that node's own get_transaction_pool gave."""

from __future__ import annotations

import hashlib
import unittest
import unittest.mock

import helpers
from memstara import pool
from memstara.pool import (FLAG_DOUBLE_SPEND, FLAG_NOT_RELAYED, FLAG_TIME_GUESSED, ParseError, PoolList,
                          clawback, padded_outputs, parse_tx_base, tx_details)

REAL = helpers.fixture("pool_txs.json")["txs"]


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def fake_hash(i) -> str:
    return hashlib.sha256(b"tx%d" % i).hexdigest()


class ReadOneTest(unittest.TestCase):
    def test_real_transactions(self):
        for tx in REAL:
            d = tx_details(tx["pruned_as_hex"], tx["prunable_as_hex"])
            self.assertEqual((d["fee"], d["weight"], d["bytes"]), (tx["fee"], tx["weight"], tx["blob_size"]),
                             tx["tx_hash"])
        shapes = [(d["n_in"], d["n_out"]) for d in (tx_details(t["pruned_as_hex"], t["prunable_as_hex"])
                                                     for t in REAL)]
        self.assertIn((2, 5), shapes, "one with 5 outputs: its range proof covers 8")
        self.assertIn((2, 3), shapes)

    def test_padded_outputs_and_clawback(self):
        five = next(t for t in REAL if tx_details(t["pruned_as_hex"], t["prunable_as_hex"])["n_out"] == 5)
        base = parse_tx_base(bytes.fromhex(five["pruned_as_hex"]))
        self.assertEqual((base["version"], base["rct_type"], base["ring"]), (2, pool.RCT_TYPE_BULLETPROOF_PLUS, 16))
        self.assertEqual(padded_outputs(bytes.fromhex(five["prunable_as_hex"]), base["rct_type"]), 8)
        # cryptonote_format_utils.cpp get_transaction_weight_clawback, worked by hand
        self.assertEqual(clawback(pool.RCT_TYPE_BULLETPROOF_PLUS, 2), 0)
        self.assertEqual(clawback(pool.RCT_TYPE_BULLETPROOF_PLUS, 4), 460)
        self.assertEqual(clawback(pool.RCT_TYPE_BULLETPROOF_PLUS, 8), 1433)
        self.assertEqual(clawback(pool.RCT_TYPE_CLSAG, 4), (32 * 23 // 2 * 4 - 32 * 25) * 4 // 5)
        self.assertIsNone(padded_outputs(b"", 0), "no range proof to read")
        self.assertIsNone(padded_outputs(bytes.fromhex(five["prunable_as_hex"])[:100], 6), "cut short")

    def test_hand_made(self):
        # version 1: the fee is what goes in less what comes out
        v1 = (varint(1) + varint(0) + varint(1) + b"\x02" + varint(1000) + varint(2) + varint(5) + varint(9)
              + b"\x11" * 32 + varint(1) + varint(900) + b"\x02" + b"\x22" * 32 + varint(0))
        self.assertEqual(parse_tx_base(v1)["fee"], 100)
        # a miner's reward: one txin_gen, a tagged output, RingCT type 0 (no fee)
        gen = (varint(2) + varint(60) + varint(1) + b"\xff" + varint(3780000) + varint(1) + varint(600000000000)
               + b"\x03" + b"\x33" * 33 + varint(3) + b"\x01\x02\x03" + b"\x00")
        self.assertEqual(parse_tx_base(gen), {"version": 2, "n_in": 1, "n_out": 1, "ring": 0, "rct_type": 0,
                                              "fee": 0})
        self.assertEqual(tx_details(gen.hex(), "")["weight"], len(gen))
        for bad in (b"", varint(3), gen[:20], varint(2) + varint(0) + varint(1) + b"\x07"):
            with self.assertRaises(ParseError, msg=bad.hex()):
                parse_tx_base(bad)
        with self.assertRaises(ValueError):
            tx_details("zz", "")
        with self.assertRaises(ParseError, msg="a RingCT transaction whose range proof can't be read"):
            tx_details(REAL[0]["pruned_as_hex"], REAL[0]["prunable_as_hex"][:300])


class StubNode:
    """Answers PoolList's three calls from a dict of hash -> fixture transaction."""

    def __init__(self):
        self.pool: dict = {}            # hash -> (fixture index, received_timestamp, relayed, double spend)
        self.calls: list = []
        self.fail = False
        self.untrusted = False
        self.blocks: dict = {}

    def add(self, h, i=0, t=1000, relayed=True, ds=False):
        self.pool[h] = (i, t, relayed, ds)

    def totals(self):
        txs = [REAL[v[0]] for v in self.pool.values()]
        return {"txs": len(txs), "bytes": sum(t["weight"] for t in txs), "fee_total": sum(t["fee"] for t in txs),
                "double_spends": sum(1 for v in self.pool.values() if v[3])}

    def __call__(self, method, params):
        self.calls.append((method, params))
        if self.fail:
            raise OSError("down")
        if method == "get_transaction_pool_hashes":
            r = {"status": "OK", "untrusted": self.untrusted}
            if self.pool:
                r["tx_hashes"] = list(self.pool)
            return r
        if method == "get_transactions":
            txs = []
            for h in params["txs_hashes"]:
                if h in self.pool:
                    i, t, relayed, ds = self.pool[h]
                    txs.append({"tx_hash": h, "pruned_as_hex": REAL[i]["pruned_as_hex"],
                                "prunable_as_hex": REAL[i]["prunable_as_hex"], "in_pool": True,
                                "received_timestamp": t, "relayed": relayed, "double_spend_seen": ds})
            return {"status": "OK", "untrusted": self.untrusted, "txs": txs}
        if method == "get_block":
            r = {"status": "OK", "untrusted": self.untrusted}
            if self.blocks.get(params["height"]):
                r["tx_hashes"] = self.blocks[params["height"]]
            return r
        raise AssertionError(method)


class PoolListTest(unittest.TestCase):
    def setUp(self):
        self.node = StubNode()
        self.logs = []
        self.pl = PoolList(self.logs.append)

    def asked(self, method):
        return [p for m, p in self.node.calls if m == method]

    def test_read_once_each_and_checked_against_the_totals(self):
        node, pl = self.node, self.pl
        for i in range(6):
            node.add(fake_hash(i), i, t=1000 + i)
        pl.update(node, node.totals(), 2000)
        part = pl.part()
        self.assertEqual(part["check"], "ok")
        self.assertEqual([e[0] for e in part["list"]], [fake_hash(i)[:16] for i in range(6)], "oldest first")
        self.assertEqual(part["list"][0], [fake_hash(0)[:16], 1000, REAL[0]["fee"], REAL[0]["weight"], 0])
        self.assertEqual((part["list_wanted"], part["list_cut"], part["mined"]), (0, 0, []))
        # nothing new: the hashes aren't read again until RECHECK_S has passed
        node.calls.clear()
        pl.update(node, node.totals(), 2001)
        self.assertEqual(node.calls, [])
        pl.update(node, node.totals(), 2000 + pool.RECHECK_S)
        self.assertEqual([m for m, _ in node.calls], ["get_transaction_pool_hashes"])
        # one more: only it is read
        node.calls.clear()
        node.add(fake_hash(9), 2, t=0, relayed=False)
        pl.update(node, node.totals(), 2011)
        self.assertEqual(self.asked("get_transactions"), [{"txs_hashes": [fake_hash(9)], "split": True}])
        e = pl.part()["list"][-1]
        self.assertEqual((e[0], e[1], e[4]), (fake_hash(9)[:16], 2011, FLAG_NOT_RELAYED | FLAG_TIME_GUESSED),
                         "no arrival time from the node (a restricted port): when the dashboard first saw it")
        # gone (mined or dropped): off the list without another read
        node.calls.clear()
        del node.pool[fake_hash(0)]
        pl.update(node, node.totals(), 2012)
        self.assertEqual(self.asked("get_transactions"), [])
        self.assertNotIn(fake_hash(0)[:16], [e[0] for e in pl.part()["list"]])
        self.assertEqual(pl.part()["check"], "ok")

    def test_a_double_spend_reads_the_flags_again(self):
        node, pl = self.node, self.pl
        for i in range(3):
            node.add(fake_hash(i), i)
        pl.update(node, node.totals(), 2000)
        node.pool[fake_hash(1)] = (1, 1000, True, True)
        node.calls.clear()
        pl.update(node, node.totals(), 2001)
        self.assertEqual(sorted(self.asked("get_transactions")[0]["txs_hashes"]), sorted(fake_hash(i) for i in range(3)))
        flags = {e[0]: e[4] for e in pl.part()["list"]}
        self.assertEqual(flags[fake_hash(1)[:16]], FLAG_DOUBLE_SPEND)
        node.calls.clear()
        pl.update(node, node.totals(), 2002)
        self.assertEqual(node.calls, [], "only once per double spend")

    def test_a_double_spend_in_a_big_pool_reads_the_flags_over_several_polls(self):
        node, pl = self.node, self.pl
        n = pool.DETAILS_PER_POLL + 150
        for i in range(n):
            node.add(fake_hash(i), i % 6, t=1000 + i)
        pl.update(node, node.totals(), 2000)
        pl.update(node, node.totals(), 2001)
        self.assertEqual(len(pl.part()["list"]), n)
        last = fake_hash(n - 1)
        node.pool[last] = node.pool[last][:3] + (True,)
        node.add(fake_hash(n), 0, t=3000)                 # and a new one meanwhile
        node.calls.clear()
        pl.update(node, node.totals(), 2002)
        first = self.asked("get_transactions")[0]["txs_hashes"]
        self.assertEqual(first[0], fake_hash(n), "the one not read at all goes first")
        part = pl.part()
        self.assertEqual(part["list_wanted"], 0, "the ones read again are listed already")
        self.assertEqual({e[0]: e[4] for e in part["list"]}[last[:16]], 0, "not reached yet")
        pl.update(node, node.totals(), 2003)
        self.assertEqual({e[0]: e[4] for e in pl.part()["list"]}[last[:16]], FLAG_DOUBLE_SPEND,
                         "the rest are read at the next poll")
        node.calls.clear()
        pl.update(node, node.totals(), 2004)
        self.assertEqual(node.calls, [], "and then it's done")

    def test_a_big_pool_is_read_over_several_polls(self):
        node, pl = self.node, self.pl
        n = pool.DETAILS_PER_POLL + 50
        for i in range(n):
            node.add(fake_hash(i), i % 6, t=1000 + i)
        pl.update(node, node.totals(), 2000)
        sizes = [len(p["txs_hashes"]) for p in self.asked("get_transactions")]
        self.assertEqual(sizes, [pool.BATCH, pool.BATCH], "at most BATCH a call (a restricted port's limit)")
        part = pl.part()
        self.assertEqual((len(part["list"]), part["list_wanted"], part["check"]), (pool.DETAILS_PER_POLL, 50, None))
        pl.update(node, node.totals(), 2001)
        part = pl.part()
        self.assertEqual((len(part["list"]), part["list_wanted"], part["check"]), (n, 0, "ok"))
        with unittest.mock.patch("memstara.pool.MAX_LIST", 100):
            part = pl.part()
        self.assertEqual((len(part["list"]), part["list_cut"]), (100, n - 100))
        self.assertEqual(part["list"][-1][0], fake_hash(n - 1)[:16], "the newest are kept")

    def test_totals_that_dont_add_up_are_reported(self):
        node, pl = self.node, self.pl
        node.add(fake_hash(0), 0)
        totals = dict(node.totals(), fee_total=1)
        for k in range(pool.CHECK_RUNS):
            pl.update(node, dict(totals, bytes=totals["bytes"] + k), 2000 + k)   # changed: read each time
        self.assertEqual(pl.part()["check"], "differs")
        self.assertEqual(len([m for m in self.logs if "don't add up" in m]), 1)
        pl.update(node, node.totals(), 2010)
        self.assertEqual(pl.part()["check"], "ok")

    def test_failures_wait_and_untrusted_answers_are_skipped(self):
        node, pl = self.node, self.pl
        node.add(fake_hash(0), 0)
        node.fail = True
        pl.update(node, node.totals(), 2000)
        node.fail = False
        node.calls.clear()
        pl.update(node, node.totals(), 2001)
        self.assertEqual(node.calls, [], "a failed read waits RECHECK_S before the next try")
        pl.update(node, node.totals(), 2000 + pool.RECHECK_S)
        self.assertEqual(len(pl.part()["list"]), 1)
        # a bootstrap node's answers (untrusted) aren't this node's pool
        pl2 = PoolList()
        node.untrusted = True
        pl2.update(node, node.totals(), 3000)
        self.assertEqual(pl2.part()["list"], [])
        pl2.block(node, 5)
        self.assertEqual(pl2.part()["mined"], [])

    def test_unreadable_ones_are_given_up_on(self):
        node, pl = self.node, self.pl
        node.add(fake_hash(0), 0)
        real = REAL[0]["prunable_as_hex"]
        try:
            REAL[0]["prunable_as_hex"] = real[:300]
            for k in range(5):
                pl.update(node, node.totals(), 2000 + k * pool.RECHECK_S)
        finally:
            REAL[0]["prunable_as_hex"] = real
        self.assertEqual(len(self.asked("get_transactions")), 3, "three tries, then left out")
        part = pl.part()
        self.assertEqual((part["list"], part["list_wanted"], part["list_unreadable"]), ([], 0, 1))
        self.assertEqual(len(self.logs), 1, "said once")

    def test_mined(self):
        node, pl = self.node, self.pl
        node.blocks[7] = [fake_hash(1), fake_hash(2)]
        for h in range(5, 10):
            pl.block(node, h)
        mined = pl.part()["mined"]
        self.assertEqual([m["height"] for m in mined], [7, 8, 9], "the newest MINED_KEPT")
        self.assertEqual(mined[0]["ids"], [fake_hash(1)[:16], fake_hash(2)[:16]])
        self.assertEqual(mined[1]["ids"], [], "no tx_hashes: a block with only the miner's reward")


if __name__ == "__main__":
    unittest.main()
