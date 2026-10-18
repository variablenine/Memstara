"""History: 24 h ring buffer, down-sampling to at most 360 points, save at most once a minute."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import helpers
from memstara.history import FIELDS, History


def sample(v):
    return {"out": v, "in": v + 10, "tor": 3, "pool": 30, "rate_in": 60000, "rate_out": 140000, "latency": 12}


class HistoryTest(unittest.TestCase):
    def test_query_shapes(self):
        h = History()
        now = 1791230700
        for i in range(0, 86400, 5):
            t = now - 86400 + i
            h.record(t, sample(20) if (i // 5) % 50 else {f: None for f in FIELDS})
        for window, step, n in ((900, 5, 180), (3600, 10, 360), (21600, 60, 360), (86400, 240, 360)):
            q = h.query(window, now, poll_s=5)
            self.assertEqual(q["window"], window)
            self.assertEqual(q["step"], step, window)
            self.assertEqual(len(q["t"]), n, window)
            self.assertLessEqual(len(q["t"]), 360)
            for f in FIELDS:
                self.assertEqual(len(q[f]), n)
            self.assertEqual(q["t"], sorted(q["t"]))
            self.assertEqual(q["t"][1] - q["t"][0], step)
        q = h.query(3600, now, poll_s=5)
        vals = [v for v in q["out"] if v is not None]
        self.assertTrue(all(v == 20 for v in vals))
        self.assertIsInstance(q["rate_in"][-2], int)

    def test_averaging_and_gaps(self):
        h = History()
        now = 1791230700 - 1791230700 % 10 + 9
        h.record(now - 9, sample(10))
        h.record(now - 4, sample(20))
        h.record(now - 30, {f: None for f in FIELDS})     # not answering
        q = h.query(3600, now, poll_s=5)
        self.assertEqual(q["out"][-1], 15)                  # averaged
        self.assertIsNone(q["out"][-4])                     # node wasn't answering
        self.assertIsNone(q["out"][0])                      # dashboard wasn't running

    def test_one_second_readings_make_five_second_rows(self):
        h = History()
        now = 1791230700
        for i in range(3600):                         # an hour, one reading a second
            h.record(now - 3600 + i, sample(10 if i % 2 else 20))
        self.assertEqual(len(h), 720, "one row per 5 s, the last one still filling")
        q = h.query(900, now, poll_s=1)
        self.assertEqual((q["step"], len(q["t"])), (5, 180), "never finer than a stored row")
        self.assertTrue(all(v in (14, 16) for v in q["out"][:-1]), "each row is its readings' average")

    def test_newest_point_moves_with_every_reading(self):
        h = History()
        start = 1791230700                            # a multiple of 5: a row starts here
        h.record(start, sample(10))
        self.assertEqual(h.query(900, start, poll_s=1)["out"][-1], 10)
        h.record(start + 1, sample(20))
        self.assertEqual(h.query(900, start + 1, poll_s=1)["out"][-1], 15, "the row still filling counts")
        h.record(start + 5, sample(40))               # the next row
        q = h.query(900, start + 5, poll_s=1)
        self.assertEqual((q["out"][-2], q["out"][-1]), (15, 40))

    def test_saves_the_row_still_filling(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.json"
            h = History(path)
            now = 1791230700
            for i in range(12):
                h.record(now + i, sample(i))
            self.assertTrue(h.maybe_save(now + 12, force=True))
            rows = json.loads(path.read_text())["rows"]
            self.assertEqual([r[0] for r in rows], [now, now + 5, now + 10])
            self.assertEqual([r[1] for r in rows], [2, 7, 10.5])
            h2 = History(path)
            h2.load(now + 13)
            self.assertEqual(len(h2), 3)
            h2.record(now + 14, sample(100))          # same 5 s as a loaded row: both count
            self.assertEqual(h2.query(900, now + 14, poll_s=1)["out"][-1], 55.25)

    def test_keeps_24h(self):
        h = History()
        h.record(1000, sample(1))
        h.record(1000 + 86400 + 1, sample(2))
        self.assertEqual(len(h), 1)

    def test_save_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state" / "history.json"
            h = History(path)
            now = 1791230700
            h.record(now - 10, sample(22))
            self.assertTrue(h.maybe_save(now))
            h.record(now - 5, sample(23))
            self.assertFalse(h.maybe_save(now + 30), "at most once a minute")
            self.assertTrue(h.maybe_save(now + 61))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            raw = json.loads(path.read_text())
            self.assertEqual(raw["fields"], list(FIELDS))
            h2 = History(path)
            h2.load(now + 100)
            self.assertEqual(len(h2), 2)
            h3 = History(path)
            h3.load(now + 86400 * 2)
            self.assertEqual(len(h3), 0, "old samples are dropped")
            path.write_text("garbage")
            h4 = History(path)
            h4.load(now)
            self.assertEqual(len(h4), 0)

    def test_a_file_with_the_cpu_fields(self):
        # a file from a version that also kept this computer's CPU readings: they're left out
        self.assertEqual(FIELDS, ("out", "in", "tor", "pool", "rate_in", "rate_out", "latency"))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.json"
            now = 1791230700
            old = ["out", "in", "tor", "pool", "rate_in", "rate_out", "latency", "cpu_temp_c", "node_cpu_pct"]
            path.write_text(json.dumps({"v": 1, "fields": old,
                                        "rows": [[now - 10, 22, 31, 3, 34, 60000, 140000, 14, 61.0, 7.5],
                                                 [now - 5, 23, 31, 3, 34, 60000, 140000, 14, None, None],
                                                 [now - 4, 23, 31, 3, 34, 60000, 140000, 14]]}))
            h = History(path)
            h.load(now)
            self.assertEqual(len(h), 2, "a row of the wrong length is skipped")
            h.record(now, sample(24))
            q = h.query(900, now, poll_s=5)
            self.assertEqual([v for v in q["out"] if v is not None], [22, 23, 24])
            self.assertEqual([v for v in q["latency"] if v is not None], [14, 14, 12])
            self.assertNotIn("cpu_temp_c", q)
            self.assertTrue(h.maybe_save(now, force=True))
            self.assertEqual(json.loads(path.read_text())["fields"], list(FIELDS), "saved without them")

    def test_a_file_from_before_a_field(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.json"
            now = 1791230700
            path.write_text(json.dumps({"fields": ["out", "in"], "rows": [[now - 10, 22, 31]]}))
            h = History(path)
            h.load(now)
            q = h.query(900, now, poll_s=5)
            self.assertEqual([v for v in q["out"] if v is not None], [22])
            self.assertEqual([v for v in q["latency"] if v is not None], [], "a field it didn't have: no points")


if __name__ == "__main__":
    unittest.main()
