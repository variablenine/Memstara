"""Each waiting transaction's fee, weight and arrival time, read from the node (read-only).

get_transaction_pool_stats only gives totals and an age chart. To place each transaction
by the fee it paid and how long it has waited, PoolList keeps a list of the pool:

- /get_transaction_pool_hashes gives the hash of everything waiting. It is read again
  when the pool's totals change, while details are still owed, and at least every
  RECHECK_S (and no sooner than that after a read that failed).
- /get_transactions with "split": true gives, once for each new transaction, its pruned
  and prunable parts as hex, when the node got it (received_timestamp; 0 on a
  restricted port, where the time the dashboard first saw it stands in), whether it
  was passed on (relayed) and whether a double spend of it was seen.
- The fee is read from the pruned part: the transaction prefix, then the RingCT base,
  whose first fields are its type and txnFee (cryptonote_basic.h transaction_prefix,
  rctTypes.h serialize_rctsig_base). The weight is the size plus the bulletproof
  "clawback" for more than 2 outputs, worked out the way monerod does
  (cryptonote_format_utils.cpp get_transaction_weight, _clawback), with the number of
  padded outputs read from the range proof's L vector in the prunable part.

monerod's own totals check the reading: the fees add up to pool_stats.fee_total and the
weights to bytes_total (tx_pool.cpp get_transaction_stats adds meta.fee and meta.weight).

When a block arrives, get_block gives the hashes of the transactions in it, so the
window can send exactly those dots into the block.

All of this is checked in monerod v0.18.5.3 and against a regtest node of v0.18.4.5.
"""

from __future__ import annotations

from collections import deque

from .util import as_bool, as_int

RECHECK_S = 10            # read the hashes again at least this often (and wait this long after a failed read)
DETAILS_PER_POLL = 200    # new transactions read per poll at most (the rest on the next ones)
BATCH = 100               # transactions per get_transactions call (a restricted port's limit)
MAX_LIST = 5000           # the newest this many go into the snapshot
MINED_KEPT = 3            # the newest blocks whose transaction ids are kept
ID_LEN = 16               # hex characters of a hash the window gets
CHECK_RUNS = 3            # totals that differ this many reads in a row are reported

FLAG_DOUBLE_SPEND = 1     # a double spend of it was seen
FLAG_NOT_RELAYED = 2      # the node hasn't passed it on yet
FLAG_TIME_GUESSED = 4     # the node didn't say when it got it: the time is when the dashboard first saw it

RCT_TYPE_BULLETPROOF = 3
RCT_TYPE_BULLETPROOF2 = 4
RCT_TYPE_CLSAG = 5
RCT_TYPE_BULLETPROOF_PLUS = 6


class ParseError(ValueError):
    pass


class _Reader:
    __slots__ = ("b", "i")

    def __init__(self, b: bytes):
        self.b = b
        self.i = 0

    def byte(self) -> int:
        if self.i >= len(self.b):
            raise ParseError("ran out of bytes")
        v = self.b[self.i]
        self.i += 1
        return v

    def skip(self, n: int):
        if n < 0 or self.i + n > len(self.b):
            raise ParseError("ran out of bytes")
        self.i += n

    def varint(self) -> int:
        """Monero's varint: 7 bits a byte, low bits first, top bit set on all but the last."""
        v = shift = 0
        while True:
            c = self.byte()
            v |= (c & 0x7F) << shift
            if not c & 0x80:
                return v
            shift += 7
            if shift > 63:
                raise ParseError("varint too long")


def parse_tx_base(pruned: bytes) -> dict:
    """The pruned part of a transaction -> {"version", "n_in", "n_out", "ring", "rct_type", "fee"}.

    transaction_prefix: varint version, varint unlock_time, vin, vout, extra. Each vin
    starts with a one-byte tag: 0x02 txin_to_key (varint amount, key_offsets as a count
    and varints, a 32-byte key image), 0xff txin_gen (varint height). Each vout is a
    varint amount and a tagged target: 0x02 txout_to_key (32-byte key), 0x03
    txout_to_tagged_key (32-byte key and a 1-byte view tag). extra is a count and bytes.
    Version 2 goes on with the RingCT base: a one-byte type, then (unless 0) txnFee as a
    varint. Raises ParseError on anything else.
    """
    r = _Reader(pruned)
    version = r.varint()
    if version not in (1, 2):
        raise ParseError("version %d" % version)
    r.varint()                      # unlock_time
    n_in = r.varint()
    if n_in > 10000:
        raise ParseError("too many inputs")
    ring = 0
    amounts_in = 0
    for _ in range(n_in):
        tag = r.byte()
        if tag == 0x02:
            amounts_in += r.varint()
            k = r.varint()
            if k > 1000:
                raise ParseError("too many ring members")
            for _ in range(k):
                r.varint()
            ring = max(ring, k)
            r.skip(32)
        elif tag == 0xFF:
            r.varint()
        else:
            raise ParseError("input type %d" % tag)
    n_out = r.varint()
    if n_out > 10000:
        raise ParseError("too many outputs")
    amounts_out = 0
    for _ in range(n_out):
        amounts_out += r.varint()
        tag = r.byte()
        if tag == 0x02:
            r.skip(32)
        elif tag == 0x03:
            r.skip(33)
        else:
            raise ParseError("output type %d" % tag)
    r.skip(r.varint())              # extra
    if version == 1:
        # before RingCT the fee is what goes in less what comes out
        return {"version": 1, "n_in": n_in, "n_out": n_out, "ring": ring, "rct_type": None,
                "fee": max(0, amounts_in - amounts_out)}
    rct_type = r.byte()
    fee = r.varint() if rct_type != 0 else 0
    return {"version": 2, "n_in": n_in, "n_out": n_out, "ring": ring, "rct_type": rct_type, "fee": fee}


def padded_outputs(prunable: bytes, rct_type) -> int | None:
    """How many outputs the range proofs cover (rct::n_bulletproof_plus_max_amounts and
    n_bulletproof_max_amounts): each proof's L vector holds 6 + log2 of that. Read from the
    start of the prunable part; None for other types or anything unexpected."""
    if rct_type not in (RCT_TYPE_BULLETPROOF, RCT_TYPE_BULLETPROOF2, RCT_TYPE_CLSAG, RCT_TYPE_BULLETPROOF_PLUS):
        return None
    r = _Reader(prunable)
    try:
        if rct_type == RCT_TYPE_BULLETPROOF:
            nbp = int.from_bytes(bytes(r.byte() for _ in range(4)), "little")   # a plain uint32 then
        else:
            nbp = r.varint()
        if not 0 < nbp <= 16:
            return None
        # before L: A, A1, B, r1, s1, d1 (bulletproof+) or A, S, T1, T2, taux, mu; after R: a, b, t
        tail = 0 if rct_type == RCT_TYPE_BULLETPROOF_PLUS else 3
        total = 0
        for _ in range(nbp):
            r.skip(32 * 6)
            n_l = r.varint()
            if not 6 <= n_l <= 12:
                return None
            r.skip(32 * n_l)
            n_r = r.varint()
            if n_r != n_l:
                return None
            r.skip(32 * n_r)
            r.skip(32 * tail)
            total += 1 << (n_l - 6)
        return total
    except ParseError:
        return None


def clawback(rct_type, n_padded) -> int:
    """cryptonote_format_utils.cpp get_transaction_weight_clawback."""
    if not n_padded or n_padded <= 2:
        return 0
    plus = rct_type == RCT_TYPE_BULLETPROOF_PLUS
    bp_base = (32 * ((6 if plus else 9) + 7 * 2)) // 2
    nlr = 0
    while (1 << nlr) < n_padded:
        nlr += 1
    nlr += 6
    bp_size = 32 * ((6 if plus else 9) + 2 * nlr)
    if bp_base * n_padded < bp_size:
        return 0
    return (bp_base * n_padded - bp_size) * 4 // 5


def tx_details(pruned_hex: str, prunable_hex: str) -> dict:
    """One transaction from get_transactions' split hex -> {"fee", "weight", "bytes", "n_in", "n_out"}.
    Raises ParseError (or ValueError on bad hex)."""
    pruned = bytes.fromhex(pruned_hex)
    prunable = bytes.fromhex(prunable_hex or "")
    base = parse_tx_base(pruned)
    size = len(pruned) + len(prunable)
    weight = size
    if base["version"] == 2 and base["rct_type"] in (RCT_TYPE_BULLETPROOF, RCT_TYPE_BULLETPROOF2, RCT_TYPE_CLSAG,
                                                      RCT_TYPE_BULLETPROOF_PLUS):
        n = padded_outputs(prunable, base["rct_type"])
        if n is None:
            raise ParseError("range proof not readable")
        weight = size + clawback(base["rct_type"], n)
    return {"fee": base["fee"], "weight": weight, "bytes": size, "n_in": base["n_in"], "n_out": base["n_out"]}


def _hash_ok(h) -> bool:
    return isinstance(h, str) and len(h) == 64 and all(c in "0123456789abcdef" for c in h.lower())


class PoolList:
    """The pool, one entry per waiting transaction. update() once per poll."""

    def __init__(self, log=None):
        self.log = log or (lambda msg: None)
        self.reset()

    def reset(self):
        self.known: dict = {}           # hash -> entry
        self.wanted: list = []          # hashes whose details are still to be read (in the node's order)
        self.hashes_at = None           # when the hashes were last read
        self.totals = None              # (txs, bytes, fee, double spends) when they were
        self.mined: deque = deque(maxlen=MINED_KEPT)
        self.failed: dict = {}          # hash -> times its details couldn't be read
        self.check = None               # "ok" | "differs" | None
        self._differs = 0
        self._warned = False
        self.ds_seen = 0
        self.fail_at = None             # when a read last failed

    # ---------------------------------------------------------------- reading
    def update(self, call, stats: dict | None, now: float) -> None:
        """Read what changed. `call(method, params)` is the RPC client's call; `stats` is this
        poll's parse_pool() answer (None when get_transaction_pool_stats failed)."""
        totals = None
        if stats and isinstance(stats.get("txs"), int):
            totals = (stats["txs"], stats.get("bytes"), stats.get("fee_total"), stats.get("double_spends"))
        ds_more = bool(totals and isinstance(totals[3], int) and totals[3] > self.ds_seen)
        if self.fail_at is not None and 0 <= now - self.fail_at < RECHECK_S:
            return                       # the last read failed: keep what is known and try again later
        due = (self.hashes_at is None or totals != self.totals or self.wanted or ds_more
               or not 0 <= now - self.hashes_at < RECHECK_S)
        if not due:
            return
        try:
            res = call("get_transaction_pool_hashes", None)
        except Exception:
            res = None
        if not isinstance(res, dict) or res.get("untrusted") is True:
            self.fail_at = now           # (an untrusted answer is a bootstrap node's pool, not this node's)
            return
        self.fail_at = None
        # monerod leaves the list out when the pool is empty
        hashes = [h.lower() for h in (res.get("tx_hashes") or []) if _hash_ok(h)]
        self.hashes_at = now
        self.totals = totals
        live = set(hashes)
        for h in [h for h in self.known if h not in live]:
            del self.known[h]
        for h in [h for h in self.failed if h not in live]:
            del self.failed[h]
        self.wanted = [h for h in hashes if h not in self.known and self.failed.get(h, 0) < 3]
        if ds_more:
            # a double spend was seen since: read the flags of everything again
            self.wanted += [h for h in hashes if h in self.known]
            self.ds_seen = totals[3]
        elif totals and isinstance(totals[3], int):
            self.ds_seen = min(self.ds_seen, totals[3])
        todo, self.wanted = self.wanted[:DETAILS_PER_POLL], self.wanted[DETAILS_PER_POLL:]
        for i in range(0, len(todo), BATCH):
            if not self._read_details(call, todo[i:i + BATCH], now):
                self.wanted = todo[i:] + self.wanted     # later
                self.fail_at = now
                break
        self._check(totals)

    def _read_details(self, call, batch, now) -> bool:
        """One get_transactions call for up to BATCH hashes. False when it failed."""
        try:
            res = call("get_transactions", {"txs_hashes": batch, "split": True})
        except Exception:
            return False
        if not isinstance(res, dict) or res.get("untrusted") is True:
            return False
        got = set()
        for e in res.get("txs") or []:
            if not isinstance(e, dict):
                continue
            h = str(e.get("tx_hash") or "").lower()
            if h not in batch:
                continue
            got.add(h)
            if not as_bool(e.get("in_pool"), False):
                self.known.pop(h, None)           # mined (or gone) since the hashes were read
                continue
            try:
                d = tx_details(str(e.get("pruned_as_hex") or ""), str(e.get("prunable_as_hex") or ""))
            except ValueError as err:
                self.failed[h] = self.failed.get(h, 0) + 1
                if not self._warned:
                    self._warned = True
                    self.log("pool: a transaction couldn't be read (%s)" % err)
                continue
            old = self.known.get(h)
            t = as_int(e.get("received_timestamp"), 0) or 0
            flags = 0
            if t <= 0:
                t = old["t"] if old else int(now)
                flags |= FLAG_TIME_GUESSED
            if as_bool(e.get("double_spend_seen"), False):
                flags |= FLAG_DOUBLE_SPEND
            if not as_bool(e.get("relayed"), True):
                flags |= FLAG_NOT_RELAYED
            d.update(t=t, flags=flags)
            self.known[h] = d
        for h in batch:
            if h not in got:
                self.known.pop(h, None)           # missed: not in the pool any more
        return True

    def _check(self, totals):
        """Do the fees and weights read add up to monerod's own totals?"""
        if not totals or totals[0] != len(self.known) or self.wanted:
            return
        fee = sum(e["fee"] for e in self.known.values())
        weight = sum(e["weight"] for e in self.known.values())
        if (totals[1], totals[2]) == (weight, fee):
            self._differs = 0
            self.check = "ok"
            return
        self._differs += 1
        if self._differs >= CHECK_RUNS:
            if self.check != "differs":
                self.log("pool: the fees and weights read (%d, %d) don't add up to the node's totals (%s, %s)"
                         % (fee, weight, totals[2], totals[1]))
            self.check = "differs"

    def block(self, call, height: int) -> None:
        """A block that just arrived: remember which transactions it holds."""
        try:
            res = call("get_block", {"height": int(height)})
        except Exception:
            return
        if not isinstance(res, dict) or res.get("untrusted") is True:
            return
        # monerod leaves tx_hashes out of a block with no transactions but its reward
        ids = [h.lower()[:ID_LEN] for h in (res.get("tx_hashes") or []) if _hash_ok(h)]
        self.mined.append({"height": int(height), "ids": ids})

    # ---------------------------------------------------------------- the snapshot's part
    def part(self) -> dict:
        """mempool.list: [id, arrived (unix s), fee (piconero), weight (bytes), flags], oldest
        first, at most MAX_LIST (the newest); list_wanted: transactions waiting whose details
        aren't read yet; mined: the newest blocks' transaction ids; check: the totals check."""
        items = sorted(self.known.items(), key=lambda kv: (kv[1]["t"], kv[0]))
        if len(items) > MAX_LIST:
            items = items[-MAX_LIST:]
        return {
            "list": [[h[:ID_LEN], e["t"], e["fee"], e["weight"], e["flags"]] for h, e in items],
            "list_wanted": len(self.wanted),
            "list_cut": max(0, len(self.known) - MAX_LIST),
            "mined": list(self.mined),
            "check": self.check,
        }
