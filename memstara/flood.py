"""Black marble watch: how much of a new transaction's decoys a surge of new outputs would supply.

A black marble flood is one sender making a great many outputs (paying themselves over and
over) so that other people's wallets pick theirs as decoys. Every decoy they own is one they
know isn't the coin being spent, so a 16-member ring hides it among fewer members. Nobody can
see who owns an output, so this can only be estimated. The estimate follows Rucknium's study
of the suspected flood of March 2024 ("March 2024 Suspected Black Marble Flooding Against
Monero: Privacy, User Experience, and Countermeasures", draft v0.3, 2024):

1. How many RingCT outputs each block made, from get_output_distribution (amount 0: the counts
   wallets read to pick decoys; a restricted RPC port offers it too).
2. A usual day: the median of the last 180 days (720 blocks each). Half a year follows steady
   growth in use closely enough that growth alone rarely lights it (with a year, 2020 and 2021
   would have lit for weeks), and a flood has to go on for about three months before it becomes
   the usual day.
3. Outputs above that rate in a stretch of blocks count as the surge's.
4. How often a wallet picks a decoy from each stretch, from how wallet2 picks them (gamma_picker
   in src/wallet/wallet2.cpp): an age drawn from a gamma distribution, turned into an output by
   the chain's average time per output over the last year, then a random output of the block
   holding that one.
5. The share p of picks that land on the surge; a 16-member ring then holds about
   1 + 15 x (1 - p) members the sender doesn't own (the real one among them).

The stretches are the newest 48 hours an hour (30 blocks) at a time, then a day at a time back to
a year. A stretch below the usual rate counts against the surge, so day-to-day wobble cancels
out, and the total is kept between 0 and 1. Outputs older than a year count as nobody's. It
can't tell a flood from a busy spell of ordinary use: both are outputs above a usual day.

It reads a year of counts once (about 263,000, 40,000 per poll), then only the newest blocks
when the top changes, and waits while the node syncs or answers through a bootstrap node.
"""

from __future__ import annotations

import math
from array import array

from .rpc import RpcError

# src/wallet/wallet2.cpp and src/cryptonote_config.h, v0.18.5.3
GAMMA_SHAPE = 19.28                     # GAMMA_SHAPE
GAMMA_RATE = 1.61                       # 1 / GAMMA_SCALE
TARGET_S = 120                          # DIFFICULTY_TARGET_V2
SPENDABLE_AGE = 10                      # CRYPTONOTE_DEFAULT_TX_SPENDABLE_AGE
UNLOCK_S = SPENDABLE_AGE * TARGET_S     # DEFAULT_UNLOCK_TIME
RECENT_S = 15 * TARGET_S                # RECENT_SPEND_WINDOW
BLOCKS_YEAR = 86400 * 365 // TARGET_S   # blocks_in_a_year in gamma_picker: 262,800
RING = 16                               # every ring since hard fork 15 (2022)

HOUR, DAY = 30, 720                     # blocks
RECENT_HOURS = 48                       # the newest stretches, an hour each
DAYS = 365                              # stretches go back this far (a year: where wallets pick)
USUAL_DAYS = 180                        # the days a usual day is the median of
NEWEST_OFF = SPENDABLE_AGE - 1          # wallets pick from blocks up to the top minus this
WINDOW = BLOCKS_YEAR + NEWEST_OFF       # counts the estimate needs, ending at the top
CHUNK = 40000                           # counts read per poll while filling
REFETCH = 30                            # newest blocks read again when the top changes
RETRY_S = 60                            # after a failed read
STALE_BLOCKS = 30                       # an estimate this far behind the top isn't shown
SHOW_DAYS = 30                          # daily totals the screens draw
TRIM_SLACK = 2000                       # extra counts kept before the oldest is dropped


def gammp(a: float, x: float) -> float:
    """The regularized lower incomplete gamma function P(a, x): a series below a + 1, a
    continued fraction (Lentz) for 1 - P above."""
    if x <= 0:
        return 0.0
    lg = a * math.log(x) - x - math.lgamma(a)
    if x < a + 1:
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(1000):
            ap += 1
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-15:
                break
        return min(1.0, s * math.exp(lg))
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return max(0.0, 1.0 - math.exp(lg) * h)


def age_cdf(s: float) -> float:
    """P(the age wallet2 draws is at most s seconds): exp of a gamma (shape 19.28, rate 1.61)."""
    return 0.0 if s <= 1 else gammp(GAMMA_SHAPE, GAMMA_RATE * math.log(s))


_G_UNLOCK = age_cdf(UNLOCK_S)


def picked_cdf(t: float) -> float:
    """P(the age wallet2 picks by is below t seconds). A drawn age over the unlock time has the
    unlock time taken off; one under it becomes a random age in the first RECENT_SPEND_WINDOW."""
    if t <= 0:
        return 0.0
    if t < RECENT_S:
        return age_cdf(t + UNLOCK_S) - _G_UNLOCK + _G_UNLOCK * t / RECENT_S
    return age_cdf(t + UNLOCK_S)


def _median(xs: list) -> float:
    s = sorted(xs)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def estimate(counts, start: int, base: int, top: int) -> dict:
    """The estimate for the chain up to block `top`.

    counts[i]: outputs in block start + i, for every block from top - WINDOW + 1 to top (more
    before is fine); base: outputs in all blocks before `start`.
    """
    lo_needed = top - WINDOW + 1
    if lo_needed < start or top - start + 1 > len(counts):
        raise ValueError("the counts don't cover the year before block %d" % top)

    def outs(lo, hi):                   # outputs in blocks lo..hi
        return sum(counts[lo - start:hi - start + 1])

    year = outs(top - BLOCKS_YEAR + 1, top)
    if year <= 0:
        raise ValueError("no outputs in the last year")
    per_output = TARGET_S * BLOCKS_YEAR / year         # average_output_time, seconds per output
    newest = top - NEWEST_OFF
    picked_total = picked_cdf((base + outs(start, newest)) * per_output)   # picks past the oldest are drawn again
    days = [outs(e - DAY + 1, e) for e in range(top, top - USUAL_DAYS * DAY, -DAY)]   # newest first
    usual = _median(days) / DAY                         # outputs per block on a usual day

    bins, hi = [], newest
    for _ in range(RECENT_HOURS):
        bins.append((hi - HOUR + 1, hi))
        hi -= HOUR
    while hi > newest - DAYS * DAY:
        bins.append((hi - DAY + 1, hi))
        hi -= DAY
    p, behind, edge = 0.0, 0, 0.0                       # newest first: outputs between it and the newest
    for lo, hi in bins:
        o = outs(lo, hi)
        if o <= 0:
            continue
        nxt = picked_cdf((behind + o) * per_output)
        p += (nxt - edge) / picked_total * (1.0 - usual * (hi - lo + 1) / o)
        behind += o
        edge = nxt
    p = min(1.0, max(0.0, p))
    return {
        "height": top,
        "rings": round(1 + (RING - 1) * (1 - p), 1),
        "share": round(p, 3),
        "usual_day": int(round(usual * DAY)),
        "last_day": days[0],
        "volume_x": round(days[0] / (usual * DAY), 2) if usual > 0 else None,
        "days": days[:SHOW_DAYS][::-1],                 # oldest first
    }


class FloodWatch:
    """Keeps a year of per-block output counts from the node, and the estimate. The poller's
    thread only."""

    def __init__(self, log=None):
        self.log = log or (lambda msg: None)
        self.node_top = None            # the node's top at the last poll
        self.reset()

    def reset(self):
        self.start = None               # height of counts[0]
        self.base = 0                   # outputs before self.start
        self.counts = array("q")
        self.top = None                 # the top the counts were last read up to, and its hash
        self.top_hash = None
        self.result = None
        self.off = None                 # why it isn't watching: "sync", "bootstrap" or "short"
        self.short = False              # the node said its RingCT outputs start too late
        self.error = None               # the last read's failure, in a few words
        self.retry_at = None

    # ---- each poll
    def update(self, call, top_height, top_hash, synced, now, bootstrap=False):
        """Read what's new. call(method, params) is the RPC client's read call; top_height and
        top_hash are the node's top block; synced and bootstrap come from get_info."""
        if bootstrap or synced is not True:
            self.off = "bootstrap" if bootstrap else "sync"
            return
        if not isinstance(top_height, int) or top_height - WINDOW + 1 < 0:
            self.off = "short"          # less than a year of blocks (a test chain)
            return
        self.node_top = top_height
        self.off = "short" if self.short else None
        if self.retry_at is not None and now < self.retry_at:
            return
        if self.top == top_height and self.top_hash == top_hash and self._filled_to() >= top_height:
            return
        try:
            self._read(call, top_height, top_hash)
        except RpcError as e:
            self.error = e.short
            self.retry_at = now + RETRY_S
            return
        except _Short:
            self.short = True
            self.off = "short"
            self.retry_at = now + 10 * RETRY_S
            return
        except _Gap as e:
            self.log("black marble watch: %s, reading the year again" % e)
            self.reset()
            return
        self.error = None
        self.short = False
        self.off = None
        self.retry_at = None

    def _filled_to(self) -> int:
        return -1 if self.start is None else self.start + len(self.counts) - 1

    def _read(self, call, top_height, top_hash):
        want_from = top_height - WINDOW + 1
        have = self._filled_to()
        if self.start is None or self.start > want_from or have < want_from - 1:
            # the first read, or too far behind to catch up: a year back from the top
            self.reset()
            base, dist = self._fetch(call, want_from, min(top_height, want_from + CHUNK - 1))
            self.start, self.base = want_from, base
            self.counts.extend(dist)
        else:
            if self.top is None and have < top_height:
                frm = have + 1                          # still filling
            else:
                frm = max(self.start, min(have, top_height) - REFETCH + 1)   # the newest again
            base, dist = self._fetch(call, frm, min(top_height, frm + CHUNK - 1))
            kept = self.base + sum(self.counts[:frm - self.start])
            if base != kept:
                raise _Gap("the outputs before block %d changed (%d, was %d)" % (frm, base, kept))
            del self.counts[frm - self.start:]
            self.counts.extend(dist)
        if self._filled_to() < top_height:
            return                                      # more on the next poll
        self.top, self.top_hash = top_height, top_hash
        drop = want_from - self.start - TRIM_SLACK      # the oldest counts no longer needed
        if drop > 0:
            self.base += sum(self.counts[:drop])
            del self.counts[:drop]
            self.start += drop
        try:
            self.result = estimate(self.counts, self.start, self.base, top_height)
        except ValueError as e:
            self.log("black marble watch: %s" % e)
            self.result = None
            raise _Short() from None

    def _fetch(self, call, frm, to):
        """(outputs before block frm, [outputs in each block frm..to])."""
        res = call("get_output_distribution", {"amounts": [0], "from_height": frm, "to_height": to,
                                               "cumulative": False, "binary": False, "compress": False})
        dists = res.get("distributions") if isinstance(res, dict) else None
        d = dists[0] if isinstance(dists, list) and dists and isinstance(dists[0], dict) else None
        if d is None or d.get("amount", 0) != 0:
            raise RpcError("The node's answer to get_output_distribution has no counts", "no counts",
                           "bad_json", "get_output_distribution")
        start, base, dist = d.get("start_height"), d.get("base"), d.get("distribution")
        if isinstance(start, int) and start > frm:
            raise _Short()                              # RingCT outputs start after this block
        if (start != frm or not isinstance(base, int) or base < 0 or not isinstance(dist, list)
                or len(dist) != to - frm + 1
                or not all(isinstance(n, int) and not isinstance(n, bool) and 0 <= n < 1 << 40 for n in dist)):
            raise RpcError("The node's answer to get_output_distribution doesn't add up", "counts don't add up",
                           "bad_json", "get_output_distribution")
        return base, dist

    # ---- for the snapshot
    def part(self) -> dict:
        """{"state": "ok" | "filling" | "off" | "error", ...}: with "ok" the estimate's fields
        (see estimate()); "filling" has "progress" (0 to 1); "off" and "error" have "why"."""
        if self.off:
            return {"state": "off", "why": self.off}
        r = self.result
        if r is not None and self.node_top is not None and self.node_top - r["height"] <= STALE_BLOCKS:
            return dict(r, state="ok", why=self.error)
        if self.error:
            return {"state": "error", "why": self.error}
        return {"state": "filling", "progress": round(min(1.0, len(self.counts) / WINDOW), 2)}


class _Gap(Exception):
    """The counts read don't join the ones kept (blocks deeper than REFETCH were replaced)."""


class _Short(Exception):
    """The chain has less than a year of RingCT outputs."""
