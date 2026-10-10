"""Hash-chained append-only ledgers: the forward ledger and the paper journal (plan R3).

Every chained line carries `prev_sha256` (expected_prev): once the chain has started, the sha256 of every line
since the previous chained line, that line included, joined by newlines exactly as written. In a fully chained
file that is simply the previous line. Before the chain starts (a legacy prefix) it is the previous line only, as
it always was, so existing ledgers keep verifying; a fresh file's first line carries GENESIS. A *break* is a
chained line whose prev_sha256 doesn't match: what an edit, a deletion or a reordering produces, and the only
thing verify_chain reports.

Lines without prev_sha256 (written by older code after a rollback) and torn lines (a writer killed mid-line) are
not breaks: the next chained line commits to them and to the chained line before them, so none of those lines can
be changed or dropped afterwards without a break (second review of plan R3). They are listed by notes(). Flagging them would turn entries
off for good over an event nobody can undo (review of plan R3). Like any hash chain, an edit to the very last line
is invisible here: that is what the daily off-host anchor of `head()` is for (wt.ops.backup).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _read_locked(path: Path) -> bytes:
    """The file's bytes under a shared flock, so a reader never sees append()'s half-written line."""
    with open(path, "rb") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        try:
            return fh.read()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _chained(line: bytes) -> bool:
    try:
        r = json.loads(line)
    except ValueError:
        return False
    return isinstance(r, dict) and r.get("prev_sha256") is not None


def expected_prev(lines: list[bytes]) -> str:
    """The prev_sha256 a line appended after `lines` (the file's non-empty lines, as written) must carry."""
    if not lines:
        return GENESIS
    for j in range(len(lines) - 1, -1, -1):
        if _chained(lines[j]):
            return hashlib.sha256(b"\n".join(lines[j:])).hexdigest()
    return hashlib.sha256(lines[-1]).hexdigest()          # no chain yet: the legacy rule


def inspect(path: Path) -> tuple[list[str], list[str]]:
    """(breaks, notes). Breaks: chained lines whose prev_sha256 doesn't match the previous line. Notes: torn lines
    and unchained lines after the chain started (see the module docstring)."""
    if not path.exists():
        return [], []
    breaks: list[str] = []
    notes: list[str] = []
    lines = [x for x in _read_locked(path).split(b"\n") if x]
    last_chained: int | None = None
    chained = False
    for i, line in enumerate(lines, 1):
        k = i - 1
        if k == 0:
            want = GENESIS
        elif last_chained is None:
            want = hashlib.sha256(lines[k - 1]).hexdigest()
        else:
            want = hashlib.sha256(b"\n".join(lines[last_chained:k])).hexdigest()
        try:
            r = json.loads(line)
        except ValueError:
            r = None
        if not isinstance(r, dict):
            notes.append(f"line {i}: not a JSON object (a torn line?)")
            continue
        if r.get("prev_sha256") is None:
            if chained:
                notes.append(f"line {i}: unchained line after the chain started (older code?)")
            continue
        chained = True
        last_chained = k
        if r["prev_sha256"] != want:
            breaks.append(f"line {i}: prev_sha256 does not match line {i - 1}" if i > 1 else f"line {i}: not genesis")
    return breaks, notes


def verify_chain(path: Path) -> list[str]:
    """Breaks in the file's hash chain; empty when intact (or when the file doesn't exist)."""
    return inspect(path)[0]


def notes(path: Path) -> list[str]:
    return inspect(path)[1]


def head(path: Path) -> tuple[int, str | None]:
    """(line count, sha256 of the last line): what the daily anchor records. (0, None) for a missing/empty file."""
    if not path.exists():
        return 0, None
    lines = [x for x in _read_locked(path).split(b"\n") if x]
    return len(lines), (hashlib.sha256(lines[-1]).hexdigest() if lines else None)


def head_at(path: Path, n: int) -> str | None:
    """sha256 of the file's n-th line (1-based): what its head was when it had n lines. None if it is shorter.
    An append-only file that held (n, head) on some earlier day still has that line n today."""
    if n <= 0 or not path.exists():
        return None
    lines = [x for x in _read_locked(path).split(b"\n") if x]
    return hashlib.sha256(lines[n - 1]).hexdigest() if len(lines) >= n else None


TAIL = 65_536


def _next_prev(fh: Any) -> tuple[str, bool]:
    """(prev_sha256 for the next line, whether the file ends with a newline). Reads the last TAIL bytes, and the
    whole file only when no chained line starts inside them (a long unchained run, or a very long line)."""
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    if size == 0:
        return GENESIS, True
    start = max(0, size - TAIL)
    fh.seek(start)
    chunk = fh.read()
    ends = chunk.endswith(b"\n")
    lines = [x for x in chunk.split(b"\n") if x]
    if start > 0:
        lines = lines[1:]                                   # the first piece may start mid-line
        if not any(_chained(x) for x in lines):
            fh.seek(0)
            lines = [x for x in fh.read().split(b"\n") if x]
    return expected_prev(lines), ends


def append(path: Path, rec: dict[str, Any], fsync: bool = False) -> str:
    """Append `rec` as one JSON line carrying prev_sha256 (expected_prev; GENESIS on an empty file). Holds an
    exclusive flock on the file while it reads the tail and writes. A torn last line (a writer killed
    mid-line) is ended first, so it stays a line of its own that verify_chain reports. Returns the line written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            prev, ends = _next_prev(fh)
            line = json.dumps({**rec, "prev_sha256": prev}, default=str)
            fh.seek(0, os.SEEK_END)
            fh.write((b"" if ends else b"\n") + line.encode() + b"\n")
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return line
