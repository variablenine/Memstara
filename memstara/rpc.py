"""monerod RPC client: one fixed list of reads, nothing else.

READ_ALLOWED below is every RPC method Memstara can send. Each one only reads: none of
them changes anything on the node. They are the calls monerod's read-only console commands
send (see memstara/commands.py, one entry per console command) plus four more that only
the screens use.
A method that isn't on the list raises RpcNotAllowed before any request is made, and
tests/test_safety.py fails if one of monerod's methods that change something is ever added.

Each entry gives the line in monerod v0.18.5.3's src/rpc/core_rpc_server.h that maps the
method to its handler. "hidden on a restricted port" marks the ones monerod only offers on
an unrestricted RPC port (the _IF(..., !m_restricted) lines there).

Supports HTTP Digest login (monerod --rpc-login). Errors come back as RpcError with a
short plain-language summary; the login never appears in any message.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import VERSION

# method -> endpoint ("/json_rpc" for JSON-RPC, otherwise the plain path)
READ_ALLOWED = {
    # the screens (get_last_block_header, get_version, get_transaction_pool_hashes and
    # get_output_distribution are used by no console command)
    "get_info": "/json_rpc",                        # line 167
    "get_connections": "/json_rpc",                 # line 166, hidden on a restricted port
    "get_transaction_pool_stats": "/get_transaction_pool_stats",    # line 129
    "get_transaction_pool_hashes": "/get_transaction_pool_hashes",  # line 128
    "get_net_stats": "/get_net_stats",              # line 134, hidden on a restricted port
    "get_block_headers_range": "/json_rpc",         # line 162
    "get_last_block_header": "/json_rpc",           # line 156
    "sync_info": "/json_rpc",                       # line 179, hidden on a restricted port
    "get_bans": "/json_rpc",                        # line 170, hidden on a restricted port
    "hard_fork_info": "/json_rpc",                  # line 168
    "get_fee_estimate": "/json_rpc",                # line 176
    "get_limit": "/get_limit",                      # line 135
    "get_version": "/json_rpc",                     # line 174
    "get_output_distribution": "/json_rpc",         # line 181; the black marble watch (flood.py)
    # read-only console commands
    "get_height": "/get_height",                    # line 101
    "mining_status": "/mining_status",              # line 119, hidden on a restricted port
    "get_block": "/json_rpc",                       # line 164
    "get_block_header_by_hash": "/json_rpc",        # line 158
    "get_alternate_chains": "/json_rpc",            # line 177, hidden on a restricted port
    "get_coinbase_tx_sum": "/json_rpc",             # line 175, hidden on a restricted port
    "get_output_histogram": "/json_rpc",            # line 173
    "get_transactions": "/get_transactions",        # line 111
    "is_key_image_spent": "/is_key_image_spent",    # line 114
    "get_transaction_pool": "/get_transaction_pool",    # line 126, hidden on a restricted port
    "get_peer_list": "/get_peer_list",              # line 121, hidden on a restricted port
    "banned": "/json_rpc",                          # line 171, hidden on a restricted port
    "rpc_access_data": "/json_rpc",                 # line 188, hidden on a restricted port
}


MAX_ANSWER_BYTES = 32 * 1024 * 1024


class RpcError(Exception):
    """A call failed. `summary` is a plain sentence, `short` a few words, `kind` a tag."""

    def __init__(self, summary: str, short: str | None = None, kind: str = "error", method: str | None = None):
        super().__init__(summary)
        self.summary = summary
        self.short = short or summary
        self.kind = kind
        self.method = method


class RpcNotAllowed(RpcError):
    def __init__(self, method, why=None):
        m = _clean(method, 60)
        super().__init__(why or "%s isn't on the dashboard's read-only list, so it was not sent" % m,
                         "not allowed", "not_allowed", m)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never follow a redirect (a login must not be sent anywhere else)


def _build_opener():
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),  # never use $http_proxy for the node
        urllib.request.HTTPSHandler(context=ctx),
        _NoRedirect(),
    )


def _clean(s, limit=120):
    s = str(s)
    s = "".join(ch for ch in s if ch >= " ")
    return s[:limit]


def _parse_challenges(values):
    """Parse WWW-Authenticate Digest challenges into dicts."""
    out = []
    for v in values or []:
        v = v.strip()
        if not v.lower().startswith("digest"):
            continue
        params = {}
        for m in re.finditer(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))', v[6:]):
            params[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
        out.append(params)
    return out


class _Digest:
    """Client side of HTTP Digest (RFC 7616 subset: MD5 / MD5-sess, qop=auth)."""

    def __init__(self, user, password):
        self.user = user
        self.password = password
        self.challenge = None
        self.nc = 0
        self.lock = threading.Lock()

    def set_challenge(self, challenges):
        best = None
        for ch in challenges:
            alg = (ch.get("algorithm") or "MD5").upper()
            if alg == "MD5":
                best = ch
                break
            if alg == "MD5-SESS" and best is None:
                best = ch
        if best is None:
            return False
        with self.lock:
            self.challenge = best
            self.nc = 0
        return True

    def header(self, method, uri):
        with self.lock:
            ch = self.challenge
            if ch is None:
                return None
            self.nc += 1
            nc = "%08x" % self.nc
        realm = ch.get("realm", "")
        nonce = ch.get("nonce", "")
        qop_opts = [q.strip() for q in (ch.get("qop") or "").split(",") if q.strip()]
        alg = (ch.get("algorithm") or "MD5").upper()
        cnonce = os.urandom(8).hex()
        md5 = lambda s: hashlib.md5(s.encode("utf-8")).hexdigest()
        ha1 = md5("%s:%s:%s" % (self.user, realm, self.password))
        if alg == "MD5-SESS":
            ha1 = md5("%s:%s:%s" % (ha1, nonce, cnonce))
        ha2 = md5("%s:%s" % (method, uri))
        if "auth" in qop_opts:
            resp = md5("%s:%s:%s:%s:auth:%s" % (ha1, nonce, nc, cnonce, ha2))
            parts = ['username="%s"' % self.user, 'realm="%s"' % realm, 'nonce="%s"' % nonce,
                     'uri="%s"' % uri, 'algorithm=%s' % ("MD5-sess" if alg == "MD5-SESS" else "MD5"),
                     'response="%s"' % resp, 'qop=auth', 'nc=%s' % nc, 'cnonce="%s"' % cnonce]
        else:
            resp = md5("%s:%s:%s" % (ha1, nonce, ha2))
            parts = ['username="%s"' % self.user, 'realm="%s"' % realm, 'nonce="%s"' % nonce,
                     'uri="%s"' % uri, 'response="%s"' % resp]
        if ch.get("opaque"):
            parts.append('opaque="%s"' % ch["opaque"])
        return "Digest " + ", ".join(parts)


class RpcClient:
    def __init__(self, url: str, user: str = "", password: str = "", timeout: float = 8.0):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("RPC address must be http(s)://host:port")
        self.scheme = parts.scheme
        self.host = parts.hostname
        try:
            port = parts.port
        except ValueError:
            raise ValueError("RPC address has a bad port")
        self.port = port or (443 if parts.scheme == "https" else 80)
        self.base_path = parts.path.rstrip("/")
        self.timeout = float(timeout)
        self.user = user or ""
        self._digest = _Digest(self.user, password or "") if self.user else None
        self._opener = _build_opener()
        self.last_ms = None

    @property
    def hostport(self) -> str:
        h = self.host
        if ":" in h:
            h = "[%s]" % h
        return "%s:%d" % (h, self.port)

    def _url(self, path):
        h = self.host if ":" not in self.host else "[%s]" % self.host
        return "%s://%s:%d%s%s" % (self.scheme, h, self.port, self.base_path, path)

    # ---- the one entry point
    def call(self, method: str, params: dict | None = None) -> dict:
        """Call a method on READ_ALLOWED and return its result object (status "OK")."""
        if not isinstance(method, str) or method not in READ_ALLOWED:
            raise RpcNotAllowed(method)
        return self._call(method, READ_ALLOWED[method], params)

    def _call(self, method, endpoint, params):
        if endpoint == "/json_rpc":
            body = {"jsonrpc": "2.0", "id": "0", "method": method}
            if params:
                body["params"] = params
        else:
            body = params or {}
        t0 = time.monotonic()
        raw = self._post(endpoint, json.dumps(body).encode("utf-8"), method)
        self.last_ms = int(round((time.monotonic() - t0) * 1000))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise RpcError("The answer from %s isn't JSON: is this monerod's RPC port?" % self.hostport,
                           "not monerod's RPC", "bad_json", method)
        if not isinstance(data, dict):
            raise RpcError("The answer from %s isn't what monerod sends" % self.hostport,
                           "unexpected answer", "bad_json", method)
        if endpoint == "/json_rpc":
            if "error" in data and data["error"]:
                err = data["error"] if isinstance(data["error"], dict) else {}
                msg = _clean(err.get("message", "error"), 100)
                code = err.get("code")
                if code == -32601 or msg.lower() == "method not found":
                    raise RpcError("%s doesn't offer %s (a restricted RPC port only offers some calls)"
                                   % (self.hostport, method), "not offered", "not_offered", method)
                raise RpcError("The node refused %s: %s" % (method, msg), msg, "rpc_error", method)
            result = data.get("result")
            if not isinstance(result, dict):
                raise RpcError("The answer from %s has no result" % self.hostport,
                               "unexpected answer", "bad_json", method)
        else:
            result = data
        status = result.get("status")
        if status != "OK":
            st = _clean(status if status is not None else "no status", 60)
            raise RpcError('The node answered "%s" to %s' % (st, method), st, "status", method)
        return result

    # ---- HTTP with Digest login and plain-language errors
    def _post(self, path, data, method):
        url = self._url(path)
        uri = self.base_path + path
        tries = 0
        while True:
            tries += 1
            headers = {"Content-Type": "application/json", "Accept": "application/json",
                       "User-Agent": "memstara/" + VERSION}
            if self._digest is not None:
                h = self._digest.header("POST", uri)
                if h:
                    headers["Authorization"] = h
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    body = resp.read(MAX_ANSWER_BYTES + 1)
                    if len(body) > MAX_ANSWER_BYTES:
                        raise RpcError("The answer from %s was too large" % self.hostport,
                                       "answer too large", "too_large", method)
                    return body
            except urllib.error.HTTPError as e:
                code = e.code
                challenges = _parse_challenges(e.headers.get_all("WWW-Authenticate") if e.headers else None)
                try:
                    e.read(65536)
                    e.close()
                except Exception:
                    pass
                if code == 401:
                    if not self.user:
                        raise RpcError("The node wants a login: set the RPC user and password "
                                       "(monerod runs with --rpc-login)", "login needed", "login", method)
                    stale = any((c.get("stale") or "").lower() == "true" for c in challenges)
                    if tries <= 2 and challenges and self._digest.set_challenge(challenges) and (tries == 1 or stale):
                        continue
                    raise RpcError("The node turned down the login: check the RPC user and password",
                                   "login refused", "login", method)
                if code == 404:
                    raise RpcError("%s doesn't offer %s (a restricted RPC port only offers some calls)"
                                   % (self.hostport, method), "not offered", "not_offered", method)
                if code == 403:
                    raise RpcError("%s refused the request (403)" % self.hostport, "refused (403)", "http", method)
                raise RpcError("%s answered with an error (HTTP %d)" % (self.hostport, code),
                               "HTTP %d" % code, "http", method)
            except RpcError:
                raise
            except urllib.error.URLError as e:
                raise self._from_os_error(e.reason, method)
            except (socket.timeout, TimeoutError) as e:
                raise self._from_os_error(e, method)
            except http.client.HTTPException as e:
                if isinstance(e, http.client.RemoteDisconnected):
                    raise RpcError("%s closed the connection without answering" % self.hostport,
                                   "closed without answer", "closed", method)
                raise RpcError("%s sent something that isn't a proper web answer: is this monerod's RPC port?"
                               % self.hostport, "not monerod's RPC", "protocol", method)
            except OSError as e:
                raise self._from_os_error(e, method)

    def _from_os_error(self, reason, method):
        hp = self.hostport
        if isinstance(reason, ConnectionRefusedError):
            return RpcError("Nothing is answering at %s (connection refused). Is monerod running?" % hp,
                            "connection refused", "refused", method)
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
            t = ("%g" % self.timeout)
            return RpcError("No answer from %s within %s s" % (hp, t), "no answer in %s s" % t, "timeout", method)
        if isinstance(reason, socket.gaierror):
            return RpcError("Can't find the host %s" % _clean(self.host, 80), "unknown host", "dns", method)
        if isinstance(reason, ssl.SSLError):
            why = "certificate not trusted" if isinstance(reason, ssl.SSLCertVerificationError) else "secure connection failed"
            return RpcError("Secure connection to %s failed (%s)" % (hp, why), why, "tls", method)
        if isinstance(reason, ConnectionResetError):
            return RpcError("%s dropped the connection" % hp, "connection dropped", "closed", method)
        if isinstance(reason, (http.client.RemoteDisconnected, ConnectionAbortedError)):
            return RpcError("%s closed the connection without answering" % hp,
                            "closed without answer", "closed", method)
        if isinstance(reason, OSError) and reason.strerror:
            return RpcError("Can't connect to %s: %s" % (hp, _clean(reason.strerror, 80).lower()),
                            "can't connect", "os", method)
        return RpcError("Can't connect to %s" % hp, "can't connect", "os", method)
