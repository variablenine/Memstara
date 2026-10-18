"""The safety claims in SECURITY.md, as tests.

Memstara only reads: every RPC method it can send is on rpc.READ_ALLOWED, and none of
monerod's methods that change something is on it. Adding a method means changing the
list in this file too, so a reviewer always sees it.
"""

from __future__ import annotations

import inspect
import unittest

import helpers  # noqa: F401  (sets up sys.path)
from memstara import commands as C
from memstara import rpc

# Every method on rpc.READ_ALLOWED, written out again here on purpose.
READS = {
    "get_info", "get_connections", "get_transaction_pool_stats", "get_transaction_pool_hashes",
    "get_net_stats", "get_block_headers_range", "get_last_block_header", "sync_info", "get_bans",
    "hard_fork_info", "get_fee_estimate", "get_limit", "get_version", "get_output_distribution",
    "get_height", "mining_status", "get_block", "get_block_header_by_hash", "get_alternate_chains",
    "get_coinbase_tx_sum", "get_output_histogram", "get_transactions", "is_key_image_spent",
    "get_transaction_pool", "get_peer_list", "banned", "rpc_access_data",
}

# monerod v0.18.5.3's RPC methods that change something on the node or the network
# (src/rpc/core_rpc_server.h, with their old spellings). None may ever be on the list.
CHANGE_METHODS = {
    "send_raw_transaction", "sendrawtransaction",   # sends a transaction to the network
    "start_mining", "stop_mining",
    "save_bc",                                      # writes the blockchain to disk
    "set_log_hash_rate", "set_log_level", "set_log_categories",
    "set_bootstrap_daemon",
    "stop_daemon",
    "set_limit", "out_peers", "in_peers",           # even with set: false, out_peers and in_peers can set
    "update",                                       # can download a release
    "pop_blocks",
    "submit_block", "submitblock", "generateblocks",
    "set_bans",                                     # ban and unban
    "flush_txpool", "relay_tx",
    "prune_blockchain",                             # prunes, unless asked only to check
    "flush_cache",
    "rpc_access_submit_nonce", "rpc_access_pay", "rpc_access_tracking", "rpc_access_account",
}


class ReadOnlyTest(unittest.TestCase):
    def test_the_list_is_exactly_these_reads(self):
        self.assertEqual(set(rpc.READ_ALLOWED), READS)

    def test_no_method_that_changes_something(self):
        self.assertEqual(set(rpc.READ_ALLOWED) & CHANGE_METHODS, set())

    def test_one_way_to_send(self):
        """RpcClient.call() checks the list; nothing else in rpc.py sends a request on its own."""
        public = {n for n, _ in inspect.getmembers(rpc.RpcClient, inspect.isfunction) if not n.startswith("_")}
        self.assertEqual(public, {"call"})
        self.assertFalse(hasattr(rpc, "WRITE_ALLOWED"))

    def test_every_command_only_reads(self):
        self.assertEqual(C.check_registry(), [])
        self.assertLessEqual(C.registry_calls(), READS)
        for cmd in C.REGISTRY.values():
            self.assertEqual(set(cmd.public()) & {"kind", "confirm"}, set(), cmd.name)


if __name__ == "__main__":
    unittest.main()
