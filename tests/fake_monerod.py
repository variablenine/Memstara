"""TEST-ONLY fake monerod. Not part of the dashboard and never used by it.

A tiny http.server on 127.0.0.1 that answers every method on the dashboard's
allowlist (the poller's reads and every console command's calls) from the canned
answers in tests/fixtures/ (captured in monerod v0.18.5.x shapes) plus a little
state (limits, peer limits, mining, log level, pool, bans, chain), with a few knobs
tests can turn: change get_info fields, add a block, replace the last N blocks (a
reorg), refuse connections (node stopped), act like a restricted RPC port, or
require a Digest login. The outputs each block made (get_output_distribution) come
from a formula, so a year of them costs nothing; `floods` adds extra outputs to a
range of blocks. The pool's transactions are real ones (tests/fixtures/
pool_txs.json, made on a regtest node) under made-up hashes; with live_pool on, the
pool totals are worked out from them the way monerod does, and mine() puts some in a
new block. Every request is recorded in `requests` as (name, params),
where name is the JSON-RPC method or the path without its slash.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REALM = "monero-rpc"
# core_rpc_server.h: what a restricted RPC port leaves out
UNRESTRICTED_ONLY = {"get_connections", "get_bans", "sync_info", "set_bans", "banned", "flush_txpool",
                     "get_coinbase_tx_sum", "get_alternate_chains", "relay_tx", "prune_blockchain",
                     "flush_cache", "rpc_access_data"}
PLAIN_UNRESTRICTED_ONLY = {"/get_net_stats", "/mining_status", "/save_bc", "/get_peer_list",
                           "/set_log_hash_rate", "/set_log_level", "/set_log_categories", "/get_transaction_pool",
                           "/set_bootstrap_daemon", "/stop_daemon", "/set_limit", "/out_peers", "/in_peers",
                           "/update", "/pop_blocks", "/start_mining", "/stop_mining"}
JSON_RPC = {"get_info", "get_connections", "get_bans", "sync_info", "hard_fork_info", "get_fee_estimate",
            "get_version", "get_last_block_header", "get_block_headers_range", "set_bans", "get_block",
            "get_block_header_by_hash", "get_alternate_chains", "get_coinbase_tx_sum", "get_output_histogram",
            "get_output_distribution", "banned", "rpc_access_data", "prune_blockchain", "relay_tx", "flush_txpool",
            "flush_cache"}
POOL_TX = "c0ffee" + "0" * 58           # in the pool
CHAIN_TX = "ab" * 32                    # in block 3,780,000
POOL_FIXTURE = "pool_txs.json"          # six real transactions: their split hex, fee and weight


def fixture(name: str):
    with open(FIXTURES / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    server_version = "Epee-based"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, extra=()):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._go()

    def do_POST(self):
        self._go()

    def _go(self):
        fake: FakeMonerod = self.server.fake  # type: ignore[attr-defined]
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = self.path.split("?", 1)[0]
        if fake.login and not fake.check_digest(self.command, self.headers.get("Authorization") or ""):
            nonce = os.urandom(8).hex()
            fake.nonces.add(nonce)
            return self._send(401, None, [
                ("WWW-Authenticate", 'Digest qop="auth",algorithm=MD5-sess,realm="%s",nonce="%s",stale=false' % (REALM, nonce)),
                ("WWW-Authenticate", 'Digest qop="auth",algorithm=MD5,realm="%s",nonce="%s",stale=false' % (REALM, nonce))])
        if path == "/json_rpc":
            try:
                req = json.loads(body)
                method, params, rid = req.get("method"), req.get("params") or {}, req.get("id", "0")
            except (ValueError, AttributeError):
                return self._send(200, {"error": {"code": -32700, "message": "Parse error"}, "id": 0, "jsonrpc": "2.0"})
            fake.calls.append(method)
            fake.requests.append((method, params))
            res = fake.answer(method, params)
            if "error" in res:
                return self._send(200, {"error": res["error"], "id": rid, "jsonrpc": "2.0"})
            return self._send(200, {"id": rid, "jsonrpc": "2.0", "result": res["result"]})
        fake.calls.append(path)
        try:
            params = json.loads(body) if body.strip() else {}
        except ValueError:
            params = {}
        fake.requests.append((path.lstrip("/"), params))
        res = fake.plain(path, params if isinstance(params, dict) else {})
        if res is None:
            return self._send(404)
        return self._send(200, res)


class FakeMonerod:
    def __init__(self, *, clock=time.time, restricted=False, login=None, chain_len=300):
        self.clock = clock
        self.restricted = restricted
        self.login = login
        self.nonces: set = set()
        self.calls: list = []
        self.requests: list = []
        self.set_bans_seen: list = []
        # state the change commands act on
        self.limit = {"limit_down": 8192, "limit_up": 2048}
        self.peer_max = {"out_peers": 32, "in_peers": 64}
        self.mining = None                       # dict while mining
        self.hr_visible = False
        self.log_level = 0
        self.log_categories = "*:WARNING,net:FATAL,net.http:FATAL"
        self.bootstrap = ""
        self.pruning_seed = 0
        self.pool = [POOL_TX]
        self.pool_txs: dict = {}                 # hash -> what get_transactions and the totals need
        self.real_txs = fixture(POOL_FIXTURE)["txs"]
        self.live_pool = False                   # pool totals from self.pool instead of the fixture
        self.block_txs: dict = {}                # height -> the hashes mine() put in that block
        self.stopped = False
        self.saved = 0
        self.bad_blocks_flushed = 0
        self.update_answer = {"update": False, "version": "", "user_uri": "", "auto_uri": "", "hash": "",
                              "path": ""}
        self.lock = threading.RLock()
        self.results = {m: fixture(m + ".json") for m in
                        ("get_connections", "get_bans", "sync_info", "hard_fork_info", "get_fee_estimate",
                         "get_version", "get_transaction_pool_stats", "get_net_stats")}
        self.info = fixture("get_info_restricted.json" if restricted else "get_info.json")
        self.t0 = clock()
        self.rate_in, self.rate_out = 60000, 140000
        tpl = fixture("get_last_block_header.json")["block_header"]
        self.template = tpl
        top = tpl["height"]
        self.salt = {}
        self.chain = {}
        for h in range(top - chain_len + 1, top + 1):
            self.chain[h] = self._header(h, tpl["timestamp"] - (top - h) * 120)
        self.chain[top]["hash"] = tpl["hash"]
        self.floods: list = []                   # (first block, last block, extra outputs in each)
        self.distribution_error = None           # an error message get_output_distribution answers with
        self._httpd = None
        self.port = 0
        self.add_pool_tx(POOL_TX, 0, keep=True)

    # ---- pool knobs
    def add_pool_tx(self, h=None, i=0, t=None, relayed=True, double_spend=False, keep=False):
        """Put real transaction i of the fixture in the pool under hash h (a made-up one by default)."""
        with self.lock:
            src = self.real_txs[i % len(self.real_txs)]
            h = h or hashlib.sha256(("pool:%d:%d" % (len(self.pool_txs), i)).encode()).hexdigest()
            self.pool_txs[h] = {"pruned": src["pruned_as_hex"], "prunable": src["prunable_as_hex"],
                                "fee": src["fee"], "weight": src["weight"], "bytes": src["blob_size"],
                                "t": int(self.clock() if t is None else t), "relayed": relayed,
                                "double_spend": double_spend}
            if not keep or h not in self.pool:
                self.pool.append(h)
            return h

    def mine(self, hashes, ts=None):
        """A new block holding these pool transactions: they leave the pool."""
        with self.lock:
            self.add_block(ts)
            self.block_txs[self.top] = list(hashes)
            self.chain[self.top]["num_txes"] = len(hashes)
            self.pool = [h for h in self.pool if h not in hashes]

    def pool_stats(self):
        """get_transaction_pool_stats worked out from the pool, as tx_pool.cpp get_transaction_stats does."""
        now = int(self.clock())
        metas = [self.pool_txs[h] for h in self.pool if h in self.pool_txs]
        ws = [m["weight"] for m in metas]
        st = {"bytes_max": max(ws, default=0), "bytes_med": sorted(ws)[len(ws) // 2] if ws else 0,
              "bytes_min": min(ws, default=0), "bytes_total": sum(ws), "fee_total": sum(m["fee"] for m in metas),
              "histo_98pc": 0, "num_10m": sum(1 for m in metas if m["t"] < now - 600),
              "num_double_spends": sum(1 for m in metas if m["double_spend"]), "num_failing": 0,
              "num_not_relayed": sum(1 for m in metas if not m["relayed"]),
              "oldest": min((m["t"] for m in metas), default=0), "txs_total": len(metas)}
        if len(metas) > 1:
            oldest = st["oldest"]
            span = max(1, now - oldest)
            n = min(10, len(metas))
            histo = [{"txs": 0, "bytes": 0} for _ in range(n)]
            for m in metas:
                i = min(n - 1, (now - m["t"]) * n // span)
                histo[i]["txs"] += 1
                histo[i]["bytes"] += m["weight"]
            st["histo"] = histo
        return {"credits": 0, "pool_stats": st, "status": "OK", "top_hash": "", "untrusted": False}

    # ---- chain knobs
    def _header(self, h, ts):
        hd = dict(self.template)
        hd["height"] = h
        hd["timestamp"] = int(ts)
        hd["hash"] = hashlib.sha256(("%d:%d" % (h, self.salt.get(h, 0))).encode()).hexdigest()
        hd["num_txes"] = 5 + h % 60
        hd["block_weight"] = hd["block_size"] = 30000 + (h % 50) * 4000
        return hd

    @property
    def top(self):
        return max(self.chain)

    def add_block(self, ts=None):
        with self.lock:
            h = self.top + 1
            self.chain[h] = self._header(h, ts if ts is not None else self.clock())
            self.chain[h]["prev_hash"] = self.chain[h - 1]["hash"]

    def reorg(self, depth):
        """Replace the last `depth` blocks with new ones (new hashes), then add one more."""
        with self.lock:
            top = self.top
            for h in range(top - depth + 1, top + 1):
                self.salt[h] = self.salt.get(h, 0) + 1
                self.chain[h] = self._header(h, self.chain[h]["timestamp"])
            self.add_block()

    # ---- outputs per block (RingCT, amount 0)
    def outputs_in(self, h):
        n = 40 + (7 * h) % 11
        for lo, hi, extra in self.floods:
            if lo <= h <= hi:
                n += extra
        return n

    def outputs_before(self, h):
        """Outputs in blocks 0 to h - 1 (7 * h % 11 goes through 0 to 10 once every 11 blocks)."""
        full = h // 11
        n = 40 * h + 55 * full + sum((7 * k) % 11 for k in range(full * 11, h))
        for lo, hi, extra in self.floods:
            a, b = max(lo, 0), min(hi, h - 1)
            if b >= a:
                n += extra * (b - a + 1)
        return n

    def _distribution(self, params, top):
        """Like core_rpc_server::on_get_output_distribution with Blockchain::get_output_distribution."""
        amounts = params.get("amounts") or []
        if self.restricted and amounts != [0]:
            return {"error": {"code": -19, "message": "Restricted RPC can only get output distribution for rct "
                                                      "outputs. Use your own node."}}
        if self.distribution_error:
            return {"error": {"code": -5, "message": self.distribution_error}}
        frm = int(params.get("from_height") or 0)
        to = int(params.get("to_height") or 0) or top
        dists = []
        for amount in amounts:
            if amount != 0 or to < frm or frm > top or to > top:
                return {"error": {"code": -5, "message": "Failed to get output distribution"}}
            counts = [self.outputs_in(h) for h in range(frm, to + 1)]
            base = self.outputs_before(frm)
            if params.get("cumulative"):
                run = base
                for i, c in enumerate(counts):
                    run += c
                    counts[i] = run
            dists.append({"amount": 0, "base": base, "binary": False, "compress": False, "distribution": counts,
                          "start_height": frm})
        return self._ok(distributions=dists, credits=0, top_hash="")

    # ---- connection knobs
    def start(self):
        httpd = _Server(("127.0.0.1", self.port), _Handler)
        httpd.fake = self  # type: ignore[attr-defined]
        self.port = httpd.server_address[1]
        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        return self

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    refuse = stop          # node stopped: connection refused
    resume = start         # back on the same port

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    # ---- answers
    def answer(self, method, params):
        with self.lock:
            if method not in JSON_RPC or (self.restricted and method in UNRESTRICTED_ONLY):
                return {"error": {"code": -32601, "message": "Method not found"}}
            top = self.top
            if method == "get_info":
                r = copy.deepcopy(self.info)
                r["height"] = top + 1
                r["top_block_hash"] = self.chain[top]["hash"]
                return {"result": r}
            if method == "get_last_block_header":
                hd = dict(self.chain[top], depth=0)
                return {"result": {"block_header": hd, "credits": 0, "status": "OK", "top_hash": "", "untrusted": False}}
            if method == "get_block_headers_range":
                s, e = params.get("start_height"), params.get("end_height")
                if not isinstance(s, int) or not isinstance(e, int) or s > e or e > top or s < min(self.chain):
                    return {"error": {"code": -2, "message": "Invalid start/end heights."}}
                hs = [dict(self.chain[h], depth=top - h) for h in range(s, e + 1)]
                return {"result": {"credits": 0, "headers": hs, "status": "OK", "top_hash": "", "untrusted": False}}
            if method == "set_bans":
                return self._set_bans(params)
            if method in self.results:
                return {"result": copy.deepcopy(self.results[method])}
            return self._more(method, params, top)

    def _ok(self, **kw):
        kw.setdefault("status", "OK")
        kw.setdefault("untrusted", False)
        return {"result": kw}

    def _more(self, method, params, top):
        """The commands' other JSON-RPC methods."""
        if method == "get_block":
            h = params.get("height")
            if "hash" in params and params["hash"]:
                h = next((k for k, v in self.chain.items() if v["hash"] == params["hash"]), None)
                if h is None:
                    return {"error": {"code": -5, "message": "Internal error: can't get block by hash. Hash = %s."
                                                            % params["hash"]}}
            if not isinstance(h, int) or h > top or h < min(self.chain):
                return {"error": {"code": -2, "message": "Requested block height: %s greater than current top "
                                                        "block height: %d" % (h, top)}}
            hd = dict(self.chain[h], depth=top - h, pow_hash="ff" * 32 if params.get("fill_pow_hash") else "")
            more = {"tx_hashes": list(self.block_txs[h])} if self.block_txs.get(h) else {}   # left out when none
            return self._ok(blob="0e0e" + "00" * 40, block_header=hd, credits=0, json='{"major_version": 16}',
                            miner_tx_hash=hd["miner_tx_hash"], top_hash="", **more)
        if method == "get_block_header_by_hash":
            hs = [dict(v, depth=top - k) for k, v in self.chain.items() if v["hash"] in (params.get("hashes") or [])]
            return self._ok(block_headers=hs, credits=0, top_hash="")
        if method == "get_alternate_chains":
            parent = self.chain[top - 2]
            return self._ok(chains=[{"block_hash": "aa" * 32, "block_hashes": ["aa" * 32, "bb" * 32],
                                     "difficulty": 562960223709, "difficulty_top64": 0, "height": top - 0,
                                     "length": 2, "main_chain_parent_block": parent["hash"],
                                     "wide_difficulty": "0x83130b4ddd"}])
        if method == "get_coinbase_tx_sum":
            n = int(params.get("count") or 0)
            em, fee = 600_000_000_000 * n, 1_000_000 * n
            return self._ok(emission_amount=em, wide_emission_amount=hex(em), emission_amount_top64=0,
                            fee_amount=fee, wide_fee_amount=hex(fee), fee_amount_top64=0, credits=0, top_hash="")
        if method == "get_output_distribution":
            return self._distribution(params, top)
        if method == "get_output_histogram":
            return self._ok(histogram=[{"amount": 0, "total_instances": 99_000_000, "unlocked_instances": 98_000_000,
                                        "recent_instances": 0}], credits=0, top_hash="")
        if method == "banned":
            b = next((x for x in self.results["get_bans"].get("bans") or [] if x["host"] == params.get("address")), None)
            return self._ok(banned=b is not None, seconds=b["seconds"] if b else 0)
        if method == "rpc_access_data":
            return self._ok(entries=[], hashrate=0)
        if method == "prune_blockchain":
            if not params.get("check"):
                self.pruning_seed = 0x181
            return self._ok(pruned=bool(self.pruning_seed), pruning_seed=self.pruning_seed)
        if method == "relay_tx":
            for t in params.get("txids") or []:
                if t not in self.pool:
                    return {"error": {"code": -1, "message": "Transaction not found in pool: %s" % t}}
            return self._ok()
        if method == "flush_txpool":
            ids = params.get("txids") or []
            self.pool = [t for t in self.pool if ids and t not in ids]
            return self._ok()
        if method == "flush_cache":
            if params.get("bad_blocks"):
                self.bad_blocks_flushed += 1
            return self._ok()
        return {"error": {"code": -32601, "message": "Method not found"}}

    def _set_bans(self, params):
        """Like core_rpc_server::on_set_bans: a host with / is a subnet, else one address."""
        self.set_bans_seen.append(copy.deepcopy(params))
        bans = self.results["get_bans"].setdefault("bans", [])
        for b in params.get("bans") or []:
            host = b.get("host") or ""
            if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+(/\d+)?", host):
                return {"error": {"code": -1, "message": "Unsupported host/subnet type"}}
            bans[:] = [x for x in bans if x["host"] != host]
            if b.get("ban"):
                bans.append({"host": host, "ip": 0, "seconds": int(b.get("seconds") or 0)})
        if not bans:
            del self.results["get_bans"]["bans"]          # epee leaves empty lists out
        return {"result": {"status": "OK", "untrusted": False}}

    def plain(self, path, params=None):
        params = params or {}
        with self.lock:
            if self.restricted and path in PLAIN_UNRESTRICTED_ONLY:
                return None
            if path == "/get_transaction_pool_stats":
                if self.live_pool:
                    return self.pool_stats()
                return copy.deepcopy(self.results["get_transaction_pool_stats"])
            if path == "/get_limit":
                return dict(self.limit, status="OK", untrusted=False)
            if path == "/get_net_stats":
                r = copy.deepcopy(self.results["get_net_stats"])
                dt = max(0.0, self.clock() - self.t0)
                r["total_bytes_in"] += int(self.rate_in * dt)
                r["total_bytes_out"] += int(self.rate_out * dt)
                return r
            res = self._plain_more(path, params)
            if res is not None:
                res.setdefault("status", "OK")
                res.setdefault("untrusted", False)
            return res

    def _plain_more(self, path, p):
        top = self.top
        if path == "/get_height":
            return {"height": top + 1, "hash": self.chain[top]["hash"]}
        if path == "/mining_status":
            m = self.mining or {}
            return {"active": bool(self.mining), "speed": 1234 if self.mining else 0,
                    "threads_count": m.get("threads_count", 0), "address": m.get("miner_address", ""),
                    "pow_algorithm": "RandomX", "is_background_mining_enabled": m.get("do_background_mining", False),
                    "bg_idle_threshold": 0, "bg_min_idle_seconds": 0, "bg_ignore_battery": False, "bg_target": 0,
                    "block_target": 120, "block_reward": 600_000_000_000, "difficulty": 562960223709,
                    "wide_difficulty": "0x83130b4ddd", "difficulty_top64": 0}
        if path == "/get_transaction_pool_hashes":
            # available on a restricted port too; the list is left out when the pool is empty
            return {"tx_hashes": list(self.pool), "credits": 0, "top_hash": ""} if self.pool else \
                {"credits": 0, "top_hash": ""}
        if path == "/get_transactions":
            txs, missed = [], []
            mined = {x: hh for hh, xs in self.block_txs.items() for x in xs}
            for h in p.get("txs_hashes") or []:
                if h not in self.pool and h != CHAIN_TX and h not in mined:
                    missed.append(h)
                    continue
                in_pool = h in self.pool
                meta = self.pool_txs.get(h)
                t = {"tx_hash": h, "as_hex": "", "pruned_as_hex": meta["pruned"] if meta else "02" * 50,
                     "prunable_as_hex": meta["prunable"] if meta else "03" * 80,
                     "prunable_hash": "44" * 32, "in_pool": in_pool,
                     "double_spend_seen": bool(in_pool and meta and meta["double_spend"]),
                     "relayed": bool(in_pool and meta and meta["relayed"]),
                     # tx_pool.cpp: a restricted port doesn't say when the node got it
                     "received_timestamp": meta["t"] if in_pool and meta and not self.restricted else 0,
                     "block_height": 2 ** 64 - 1 if in_pool else mined.get(h, 3780000),
                     "block_timestamp": 2 ** 64 - 1 if in_pool else 1791225000, "output_indices": []}
                if p.get("decode_as_json"):
                    t["as_json"] = '{"version": 2}'
                txs.append(t)
            out = {"txs": txs, "txs_as_hex": [], "credits": 0, "top_hash": ""}
            if missed:
                out["missed_tx"] = missed
            return out
        if path == "/is_key_image_spent":
            kis = p.get("key_images") or []
            return {"spent_status": [0 if k.startswith("00") else 2 if k.startswith("22") else 1 for k in kis],
                    "credits": 0, "top_hash": ""}
        if path == "/get_transaction_pool":
            return {"transactions": [{"id_hash": t, "tx_json": '{"version": 2}', "tx_blob": "02" * 100,
                                      "blob_size": 1500, "weight": 1500, "fee": 30_000_000, "receive_time": 1791230000,
                                      "relayed": True, "double_spend_seen": False, "kept_by_block": False,
                                      "max_used_block_height": 0, "max_used_block_id_hash": "",
                                      "last_failed_height": 0, "last_failed_id_hash": "",
                                      "last_relayed_time": 1791230000, "do_not_relay": False}
                                     for t in self.pool],
                    "spent_key_images": [{"id_hash": "11" * 32, "txs_hashes": list(self.pool)}] if self.pool else [],
                    "credits": 0, "top_hash": ""}
        if path == "/get_peer_list":
            peer = lambda i, seed, rpc: {"host": "33.44.%d.%d" % (i, i), "ip": 0, "port": 18080, "id": 1000 + i,
                                         "last_seen": 1791230000 - i, "pruning_seed": seed, "rpc_port": rpc,
                                         "rpc_credits_per_hash": 0}
            return {"white_list": [peer(i, 0x181 if i % 2 else 0, 18089 if i % 3 == 0 else 0) for i in range(1, 7)],
                    "gray_list": [peer(i, 0, 0) for i in range(10, 14)]}
        if path == "/save_bc":
            self.saved += 1
            return {}
        if path == "/stop_daemon":
            self.stopped = True
            return {}
        if path == "/update":
            if p.get("command") not in ("check", "download", "update"):
                return {"status": "unknown command: '%s'" % p.get("command")}
            return dict(self.update_answer)
        if path == "/pop_blocks":
            n = int(p.get("nblocks") or 0)
            for h in sorted(self.chain)[-n:]:
                del self.chain[h]
            return {"height": self.top + 1}
        if path == "/set_limit":
            for k in ("limit_down", "limit_up"):
                v = int(p.get(k) or 0)
                if v < 0 and v != -1:
                    return {"status": "Invalid parameter"}
                if v > 0:
                    self.limit[k] = v
                elif v == -1:
                    self.limit[k] = {"limit_down": 8192, "limit_up": 2048}[k]
            return dict(self.limit)
        if path in ("/out_peers", "/in_peers"):
            k = path[1:]
            if p.get("set", True):
                self.peer_max[k] = int(p.get(k) or 0)
            return {k: self.peer_max[k]}
        if path == "/set_bootstrap_daemon":
            self.bootstrap = p.get("address", "")
            return {}
        if path == "/start_mining":
            if self.mining:
                return {"status": "Already mining"}
            self.mining = dict(p)
            return {}
        if path == "/stop_mining":
            if not self.mining:
                return {"status": "Mining never started"}
            self.mining = None
            return {}
        if path == "/set_log_hash_rate":
            if not self.mining:
                return {"status": "NOT MINING"}
            self.hr_visible = bool(p.get("visible"))
            return {}
        if path == "/set_log_level":
            lvl = p.get("level")
            if not isinstance(lvl, int) or not 0 <= lvl <= 4:
                return {"status": "Error: log level not valid"}
            self.log_level = lvl
            return {}
        if path == "/set_log_categories":
            self.log_categories = p.get("categories", "")
            return {"categories": self.log_categories}
        return None

    # ---- Digest login (like monerod --rpc-login)
    def check_digest(self, method, header):
        if not header.lower().startswith("digest "):
            return False
        p = dict((m.group(1).lower(), m.group(2) if m.group(2) is not None else m.group(3))
                 for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))', header[7:]))
        user, password = self.login
        if p.get("nonce") not in self.nonces or p.get("username") != user:
            return False
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
        ha1 = md5("%s:%s:%s" % (user, REALM, password))
        if (p.get("algorithm") or "MD5").upper() == "MD5-SESS":
            ha1 = md5("%s:%s:%s" % (ha1, p["nonce"], p.get("cnonce", "")))
        ha2 = md5("%s:%s" % (method, p.get("uri", "")))
        want = md5("%s:%s:%s:%s:auth:%s" % (ha1, p["nonce"], p.get("nc", ""), p.get("cnonce", ""), ha2))
        return want == p.get("response")
