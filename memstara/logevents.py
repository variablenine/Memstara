"""monerod's own log as events (read-only): the lines added since the last read, turned
into dashboard events as docs/DESIGN.md "monerod's log as events" lists them.

monerod writes its log with easylogging++ in the format MLOG_BASE_FORMAT from
contrib/epee/src/mlog.cpp:

    %datetime{%Y-%M-%d %H:%m:%s.%g}\\t%thread\\t%level\\t%logger\\t%loc\\t%msg
    2025-06-01 12:00:00.123	[P2P5]	INFO	global	src/p2p/net_node.inl:340	Host 203.0.113.9 blocked.

Times are UTC (external/easylogging++/ea_config.h defines ELPP_UTC_DATETIME). %thread
is a name ([P2P5], [RPC0], [SRV_MAIN]) or a thread id right-aligned in 16 spaces;
%level is INFO, WARNING, ERROR, FATAL, DEBUG, TRACE or VERBOSE; %logger is the log
category; %loc is file:line. A message holding newlines goes on over the next lines
with no prefix, so a log record is one header line plus any lines up to the next one.
Fields split by runs of spaces instead of tabs are accepted too.

    log = NodeLog(path)          # node.log_path; "~" is expanded
    log.poll(now) -> list        # new events, oldest first:
                                 #   {"t": unix seconds, "level": "info"|"warn"|"bad"|"ok",
                                 #    "kind": "log", "text": plain words}
    log.error                    # why the last poll couldn't read the file, else None

The first poll starts at the end of the file (nothing old is replayed). After that it
remembers the file's identity and offset: a new file at the path (monerod renamed the
full one to bitmonero.log-<date>) is read from its start, a shorter file (truncated)
too, and a partial last line waits for the next poll. At most 1 MB is read per poll;
when more than that is waiting, the older part is skipped. Changing log.path starts
over at the end of the new file. Like GET /api/node-log, a path that is a link to
somewhere outside the user's home folder, isn't a regular file, is inside the dashboard's
own settings or state folder, or whose name has no ".log" is refused.
"""

from __future__ import annotations

import calendar
import os
import re
import stat

from .util import log_path_refusal

MAX_READ = 1024 * 1024
TEXT_LIMIT = 200
REPEAT_S = 600            # the same warning (digits removed) at most once per 10 minutes
MAX_SEEN = 2000
MAX_REC_LINES = 20        # more of one record never changes its event

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

_HEAD = re.compile(
    r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)(?:[.,]\d+)?"         # %datetime, UTC
    r"[\t ]+(?:(\S+)[\t ]+)?"                                         # %thread (padded ids, too)
    r"(TRACE|DEBUG|INFO|WARNING|WARN|ERROR|FATAL|VERBOSE(?:-\d+)?)"    # %level
    r"[\t ]+(\S+)"                                                    # %logger: the category
    r"[\t ]+(\S+)"                                                    # %loc: file:line
    r"(?:[\t ](.*))?$")                                               # %msg: empty if it starts with a newline

LEVELS = {"WARNING": "warn", "WARN": "warn", "ERROR": "bad", "FATAL": "bad"}

# message texts, checked in monerod v0.18.5.3; matched at the start of the message's first
# line that isn't blank or a row of '*' (the "synchronized" banner starts with both)
_REORG = re.compile(r"###### REORGANIZE on height: (\d+) of (\d+)")          # blockchain.cpp
_REORG_SIZE = re.compile(r"alternative blockchain size: (\d+)")             # ... on the record's 2nd line
_REORG_CHECKPOINT = re.compile(r"checkpoint is found in alternative chain on height (\d+)")
_REORG_DONE = re.compile(r"REORGANIZE SUCCESS! on height: (\d+), new blockchain size: (\d+)")
_ALTERNATIVE = re.compile(r"----- BLOCK ADDED AS ALTERNATIVE ON HEIGHT (\d+)")
_BAN = re.compile(r"(Host|Subnet) (\S+) (blocked|unblocked)\.$")            # net_node.inl
_SYNCED = re.compile(r"You are now synchronized with the network")         # cryptonote_protocol_handler.inl


class LogError(Exception):
    pass


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:
        return False


def _open_log(path: str, home: str | None):
    """Open the log read-only -> (fd, fstat). Raises LogError with a plain reason."""
    if not path:
        raise LogError("No log file is set")
    p = os.path.abspath(os.path.expanduser(path))
    real = os.path.realpath(p)
    home_real = os.path.realpath(home or os.path.expanduser("~"))
    if real != p and not _inside(real, home_real):
        raise LogError("The log path is a link to somewhere outside your home folder, so it isn't read")
    why = log_path_refusal(real)
    if why == "own":
        raise LogError("The log path is in the dashboard's own settings or state folder, so it isn't read")
    if why == "name":
        raise LogError("The log path isn't a log file (its name has no .log), so it isn't read")
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(real, flags)   # never blocks on a pipe; never follows a link swapped in
    except FileNotFoundError:
        raise LogError("monerod's log isn't at %s" % p)
    except PermissionError:
        raise LogError("No permission to read monerod's log")
    except OSError:
        raise LogError("monerod's log can't be read")
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise LogError("monerod's log can't be read")
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise LogError("The log path isn't a regular file, so it isn't read")
    return fd, st


def _read_at(fd: int, offset: int, n: int) -> bytes:
    os.lseek(fd, offset, os.SEEK_SET)
    chunks, got = [], 0
    while got < n:
        c = os.read(fd, min(65536, n - got))
        if not c:
            break
        chunks.append(c)
        got += len(c)
    return b"".join(chunks)


def _clean(line: bytes) -> str:
    text = line.decode("utf-8", "replace").rstrip("\r")
    return _CONTROL.sub("", _ANSI.sub("", text))


def parse_head(line: str):
    """A header line -> (unix time, LEVEL, message on that line), or None for a continuation."""
    m = _HEAD.match(line)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.group(1, 2, 3, 4, 5, 6))
    if not (1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and s < 61):
        return None
    t = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    return t, m.group(8).split("-", 1)[0], m.group(11) or ""


def _num(s: str) -> str:
    return "{:,}".format(int(s))


class NodeLog:
    def __init__(self, path: str, home: str | None = None):
        self.path = path
        self.home = home            # tests only; the user's home folder otherwise
        self.error = None
        self._used_path = None      # path the state below belongs to
        self._ident = None          # (st_dev, st_ino) of the file being followed
        self._offset = 0
        self._buf = b""             # a last line still being written
        self._skip_line = False     # drop everything up to the next newline first
        self._rec = None            # the last record seen (more of it may come next poll)
        self._seen = {}             # warning text without digits -> when it was last shown

    def _reset(self):
        self._ident, self._offset, self._buf, self._skip_line, self._rec = None, 0, b"", False, None

    def poll(self, now: float) -> list:
        if self.path != self._used_path:
            self._reset()
            first = True
            self._used_path = self.path
        else:
            first = False
        try:
            fd, st = _open_log(self.path, self.home)
        except LogError as e:
            # not there at the start: when it shows up it is a new file, read from its start
            self.error = str(e)
            return []
        try:
            ident = (st.st_dev, st.st_ino)
            if first:
                self._ident, self._offset = ident, st.st_size
                self.error = None
                return []
            if ident != self._ident:
                self._reset()              # rotated: a new file at the path
                self._ident = ident
            elif st.st_size < self._offset:
                self._reset()              # truncated
                self._ident = ident
            pending = st.st_size - self._offset
            if pending > MAX_READ:
                # too far behind (a burst at a high log level): skip to the newest 1 MB
                self._offset, self._buf, self._rec = st.st_size - MAX_READ, b"", None
                self._skip_line = True
                pending = MAX_READ
            data = _read_at(fd, self._offset, pending) if pending > 0 else b""
        except OSError:
            self.error = "monerod's log can't be read"
            return []
        finally:
            os.close(fd)
        self.error = None
        self._offset += len(data)
        if self._skip_line:
            cut = data.find(b"\n")
            if cut < 0:
                return []
            data, self._skip_line = data[cut + 1:], False
        lines = (self._buf + data).split(b"\n")
        self._buf = lines.pop()
        if len(self._buf) > MAX_READ:      # one endless line: drop it
            self._buf, self._skip_line = b"", True
        return self._records([_clean(x) for x in lines], now)

    def _records(self, lines: list, now: float) -> list:
        events = []
        rec = self._rec                    # lines before the first header belong to the last record
        for line in lines:
            head = parse_head(line)
            if head is None:
                if rec is not None and not rec["done"] and len(rec["lines"]) < MAX_REC_LINES:
                    rec["lines"].append(line)
                continue
            if rec is not None:
                self._finish(rec, events, now)
            rec = {"t": head[0], "level": head[1], "lines": [head[2]], "done": False}
        if rec is not None:
            self._finish(rec, events, now)
        self._rec = rec
        return events

    def _finish(self, rec: dict, events: list, now: float) -> None:
        if rec["done"]:
            return
        ev = self._event(rec, now)
        if ev is not None:
            rec["done"] = True
            if ev["text"]:
                events.append(ev)

    def _event(self, rec: dict, now: float):
        """A record -> an event, an event with no text (seen, nothing to show) or None (wait for more)."""
        lines = [x.strip() for x in rec["lines"]]
        lines = [x for x in lines if x and x.strip("*")]
        level = LEVELS.get(rec["level"])
        if not lines:
            return None
        first, whole, t = lines[0], "\n".join(lines), rec["t"]

        def ev(lvl, text):
            return {"t": t, "level": lvl, "kind": "log", "text": text}

        m = _REORG.match(first)
        if m:
            n = _REORG_SIZE.search(whole)
            c = _REORG_CHECKPOINT.search(whole)
            why = (" (%s blocks)" % _num(n.group(1)) if n else
                   " (a checkpoint at %s required it)" % _num(c.group(1)) if c else "")
            return ev("warn", "monerod switched to another chain at height %s%s" % (_num(m.group(1)), why))
        m = _REORG_DONE.match(first)
        if m:
            return ev("info", "monerod finished switching chains at height %s (%s blocks now)"
                      % (_num(m.group(1)), _num(m.group(2))))
        m = _ALTERNATIVE.match(first)
        if m:
            return ev("info", "a competing block at %s lost (nothing was replaced)" % _num(m.group(1)))
        m = _BAN.match(first)
        if m:
            what = m.group(2)
            if m.group(3) == "blocked":
                return ev("info", "monerod banned %s" % what)
            return ev("info", "monerod lifted its ban on %s" % what)
        if _SYNCED.match(first):
            return ev("ok", "monerod is synchronized with the network")
        if level is None:
            return ev("info", "")          # an ordinary INFO line: nothing to show
        text = " ".join(lines)[:TEXT_LIMIT]
        if level == "warn":
            key = re.sub(r"\d", "", text)
            last = self._seen.get(key)
            if last is not None and 0 <= t - last < REPEAT_S:
                return ev("warn", "")      # shown less than 10 minutes ago
            self._seen[key] = t
            if len(self._seen) > MAX_SEEN:
                self._seen = {k: v for k, v in self._seen.items() if now - v < REPEAT_S}
        return ev(level, text)
