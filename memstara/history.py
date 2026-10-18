"""24-hour history of a few readings, for the charts.

Readings are averaged into one row per 5 seconds (BUCKET_S), however often the node is
read, kept in memory for 24 hours and saved to
$XDG_STATE_HOME/memstara/history.json at most once a minute so charts
survive a restart. The row still filling counts too, so the newest point moves with
every reading. Answers are averaged down to at most 360 points.
"""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from pathlib import Path

from .util import atomic_write

FIELDS = ("out", "in", "tor", "pool", "rate_in", "rate_out", "latency")
WINDOWS = (900, 3600, 21600, 86400)
KEEP_S = 86400
MAX_POINTS = 360
SAVE_EVERY_S = 60
BUCKET_S = 5          # one stored row per this many seconds
INT_FIELDS = {"rate_in", "rate_out", "latency"}


class History:
    def __init__(self, path: Path | str | None = None, keep_s: int = KEEP_S):
        self.path = Path(path) if path else None
        self.keep_s = keep_s
        self._rows: deque = deque()   # finished rows: (bucket start, averages...)
        self._cur = None              # the row still filling: [bucket start, sums, counts]
        self._lock = threading.Lock()
        self._last_save = 0.0
        self._dirty = False

    def __len__(self):
        return len(self._rows) + (1 if self._cur else 0)

    def clear(self):
        with self._lock:
            self._rows.clear()
            self._cur = None
            self._dirty = True

    def record(self, t: float, sample: dict) -> None:
        t = float(t)
        vals = tuple(_num(sample.get(f)) for f in FIELDS)
        start = t - t % BUCKET_S
        with self._lock:
            cur = self._cur
            last = cur[0] if cur else self._rows[-1][0] if self._rows else None
            if last is not None and t < last:
                return  # clock went backwards; keep the series ordered
            if cur and start > cur[0]:
                self._rows.append(_close(cur))
                cur = None
            if not cur:
                cur = self._cur = [start, [0.0] * len(FIELDS), [0] * len(FIELDS)]
            for j, v in enumerate(vals):
                if v is not None:
                    cur[1][j] += v
                    cur[2][j] += 1
            cutoff = t - self.keep_s
            while self._rows and self._rows[0][0] < cutoff:
                self._rows.popleft()
            self._dirty = True

    def current(self) -> dict | None:
        """The row still filling, as the window gets it with each snapshot (snapshot["hist"]):
        {"t": its start, "step": BUCKET_S, field: the average so far, ...}, so a chart's newest
        point can move with every reading between the window's history requests."""
        with self._lock:
            if not self._cur:
                return None
            row = _close(self._cur)
        out = {"t": int(row[0]) if float(row[0]).is_integer() else row[0], "step": BUCKET_S}
        for f, v in zip(FIELDS, row[1:]):
            out[f] = None if v is None else int(round(v)) if f in INT_FIELDS else round(v, 2)
        return out

    def _all_rows(self, start: float = float("-inf")) -> list:
        """Finished rows from `start` on, plus the one still filling. Call with the lock held."""
        rows = [r for r in self._rows if r[0] >= start]
        if self._cur and self._cur[0] >= start:
            rows.append(_close(self._cur))
        return rows

    def query(self, window: int, now: float, poll_s: float = 5) -> dict:
        window = int(window)
        step = max(int(math.ceil(window / MAX_POINTS)), int(math.ceil(poll_s or 1)), BUCKET_S)
        n = min(MAX_POINTS, max(1, window // step))
        end = int(now // step) * step          # start of the bucket holding `now`
        start = end - (n - 1) * step
        sums = [[0.0] * len(FIELDS) for _ in range(n)]
        counts = [[0] * len(FIELDS) for _ in range(n)]
        with self._lock:
            rows = self._all_rows(start)
        for r in rows:
            i = int((r[0] - start) // step)
            if not 0 <= i < n:
                continue
            for j, v in enumerate(r[1:]):
                if v is not None:
                    sums[i][j] += v
                    counts[i][j] += 1
        out = {"window": window, "step": step, "t": [start + i * step for i in range(n)]}
        for j, f in enumerate(FIELDS):
            col = []
            for i in range(n):
                if counts[i][j]:
                    avg = sums[i][j] / counts[i][j]
                    col.append(int(round(avg)) if f in INT_FIELDS else round(avg, 2))
                else:
                    col.append(None)
            out[f] = col
        return out

    # ---- persistence
    def load(self, now: float) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            fields = [str(f) for f in raw.get("fields")]
            rows = raw.get("rows")
            if not isinstance(rows, list) or not set(fields) & set(FIELDS):
                return
        except (OSError, ValueError, AttributeError, TypeError):
            return
        # columns by name, so a file from a version with other fields still loads
        where = [fields.index(f) + 1 if f in fields else None for f in FIELDS]
        cutoff = now - self.keep_s
        good = []
        for r in rows:
            if not isinstance(r, list) or len(r) != len(fields) + 1:
                continue
            t = _num(r[0])
            if t is None or t < cutoff or t > now + 60:
                continue
            good.append((float(t),) + tuple(_num(r[i]) if i is not None else None for i in where))
        good.sort(key=lambda x: x[0])
        with self._lock:
            self._rows = deque(good)
            self._cur = None
            self._dirty = False

    def maybe_save(self, now: float, force: bool = False) -> bool:
        if not self.path:
            return False
        if not force and (not self._dirty or now - self._last_save < SAVE_EVERY_S):
            return False
        with self._lock:
            rows = [[int(r[0]) if r[0].is_integer() else r[0]] + list(r[1:]) for r in self._all_rows()]
            self._dirty = False
        self._last_save = now
        data = json.dumps({"v": 1, "saved": int(now), "fields": list(FIELDS), "rows": rows},
                          separators=(",", ":"))
        try:
            atomic_write(self.path, data.encode("utf-8"), 0o600)
        except OSError:
            return False
        return True


def _close(cur) -> tuple:
    """A filling row [start, sums, counts] as a finished row: (start, averages...)."""
    start, sums, counts = cur
    return (start,) + tuple(round(sums[j] / counts[j], 2) if counts[j] else None for j in range(len(FIELDS)))


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            return None
        return v
    return None
