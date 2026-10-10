"""Defensive I/O for the status dashboard collector.

Every read here is bounded (size caps, subprocess timeouts, partial-file retries) and every write is
atomic and confined to one output root, so a collector run can never hang, crash on a half-written
file, or write outside ``build/dashboard``.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")
MAX_JSON_BYTES = 25 * 1024 * 1024
TAIL_BYTES = 64 * 1024


class SourceError(Exception):
    """A source could not be read. The message is short and safe to show on the page."""


class Locked(Exception):
    """Another collector run holds the lock."""


def run(cmd: list[str], timeout: float = 20.0, cwd: Path | None = None) -> str:
    """Run a command without a shell. Raises SourceError on timeout, missing binary or non-zero exit."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1",
           "GH_NO_UPDATE_NOTIFIER": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=max(1.0, timeout),
                           cwd=cwd, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise SourceError(f"{Path(cmd[0]).name} timed out after {timeout:.0f}s") from e
    except (FileNotFoundError, PermissionError) as e:
        raise SourceError(f"{Path(cmd[0]).name} not available") from e
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip().splitlines()
        raise SourceError(f"{Path(cmd[0]).name} {' '.join(cmd[1:3])} exited {p.returncode}: {err[-1][:160] if err else ''}")
    return p.stdout


def read_text(path: Path, max_bytes: int = MAX_JSON_BYTES) -> str:
    try:
        size = path.stat().st_size
    except FileNotFoundError as e:
        raise SourceError(f"missing {path.name}") from e
    if size > max_bytes:
        raise SourceError(f"{path.name} is {size / 1e6:.0f} MB, over the {max_bytes / 1e6:.0f} MB cap")
    return path.read_text(encoding="utf-8", errors="replace")


def read_json(path: Path, max_bytes: int = MAX_JSON_BYTES, retry_delay: float = 1.0) -> Any:
    """Parse a JSON file. A parse failure is retried once, because a live job may be mid-write."""
    for attempt in (1, 2):
        text = read_text(path, max_bytes)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            if attempt == 2:
                raise SourceError(f"{path.name} is not valid JSON (line {e.lineno})") from e
            time.sleep(retry_delay)
    raise AssertionError("unreachable")


def read_jsonl(path: Path, max_bytes: int = MAX_JSON_BYTES) -> tuple[list[dict], int]:
    """Rows of a JSONL file and the count of unparseable lines. A missing file is zero rows.

    A torn last line (a writer mid-append) is dropped silently; bad lines elsewhere are counted.
    """
    if not path.exists():
        return [], 0
    lines = read_text(path, max_bytes).splitlines()
    rows, bad = [], 0
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if i != len(lines) - 1:
                bad += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1
    return rows, bad


def tail(path: Path, n_bytes: int = TAIL_BYTES) -> str:
    """The last ``n_bytes`` of a text file, decoded leniently, starting at a line boundary."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n_bytes))
            data = f.read()
    except FileNotFoundError as e:
        raise SourceError(f"missing {path.name}") from e
    text = data.decode("utf-8", errors="replace")
    if size > n_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def confined(path: Path, root: Path) -> Path:
    """``path`` resolved, provided it lies inside ``root``; otherwise raise."""
    p, r = path.resolve(), root.resolve()
    if p != r and not p.is_relative_to(r):
        raise PermissionError(f"refusing to write outside {r}: {p}")
    return p


def guard_out_dir(out: Path, forbidden: list[Path], allowed: list[Path] | None = None) -> Path:
    """Refuse an output directory inside any forbidden tree (the live checkout), except the allowed ones."""
    o = out.resolve()
    for a in allowed or []:
        ar = a.expanduser().resolve()
        if o == ar or o.is_relative_to(ar):
            return o
    for f in forbidden:
        fr = f.expanduser().resolve()
        if o == fr or o.is_relative_to(fr):
            raise PermissionError(f"output directory {o} is inside {fr}, which must never be written")
    return o


def atomic_write(path: Path, text: str, root: Path) -> None:
    """Write via a temp file in the same directory, then rename, so readers never see a torn file."""
    p = confined(path, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def atomic_replace(path: Path, write: Callable[[Path], None]) -> None:
    """``write(tmp)`` to a temp file next to ``path``, then rename it over ``path``: readers (another job sharing the
    cache) see the old file or the new one, never a torn one. For writers that need a path, such as parquet.
    The temp name starts with a dot and ends in .tmp, so no ``*.parquet`` glob picks it up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        write(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_cache(path: Path, read: Callable[[Path], T], empty: Callable[[], T]) -> T:
    """A shared cache's contents, or ``empty()`` if the file is missing. A torn or corrupt file (a writer killed
    before caches were written atomically) is treated as empty with a warning, and kept aside as ``<name>.corrupt``
    (``<name>.corrupt-<time>`` if that exists) so the next save neither crashes on it nor silently destroys it.
    Running out of memory or an I/O error (EMFILE, EIO ...) says nothing about the file: those are raised."""
    if not path.exists():
        return empty()
    try:
        return read(path)
    except MemoryError:
        raise
    except OSError as e:
        if e.errno is not None:
            raise
        err: Exception = e
    except Exception as e:  # noqa: BLE001 — any parse failure means the same thing: the cache is unusable
        err = e
    bad = path.with_name(path.name + ".corrupt")
    if bad.exists():
        bad = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    with contextlib.suppress(OSError):
        os.replace(path, bad)
    warnings.warn(f"cache {path.name} is unreadable ({err.__class__.__name__}); starting empty, kept as {bad.name}",
                  RuntimeWarning, stacklevel=2)
    return empty()


def append_capped(path: Path, line: str, root: Path, max_bytes: int = 1024 * 1024) -> None:
    """Append one line; when the file passes ``max_bytes`` keep only its newer half."""
    p = confined(path, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
    if p.stat().st_size > max_bytes:
        keep = tail(p, max_bytes // 2)
        atomic_write(p, keep, root)


@contextlib.contextmanager
def run_lock(path: Path, root: Path) -> Iterator[None]:
    """Non-blocking exclusive lock; raises Locked if another run holds it."""
    p = confined(path, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Locked(str(p)) from e
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


_SECRET_PATTERNS = [
    re.compile(r"\b(?:PK|AK|SK)[A-Z0-9]{16,}\b"),                      # Alpaca-style key ids
    re.compile(r"(?i)\b(APCA[-_][A-Z_-]*|api[-_]?key|secret|token|password)\s*[=:]\s*\S+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                      # GitHub tokens
]


# Environment variables that hold secrets, wherever they were loaded from (.env, the VM's tmpfs file, systemd).
SECRET_KEYS = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "DASHBOARD_INGEST_SECRET", "VERCEL_AUTOMATION_BYPASS_SECRET",
               "CRON_SECRET", "NTFY_TOPIC", "GH_TOKEN", "HC_PING_KEY", "WEBULL_APP_KEY", "WEBULL_APP_SECRET",
               "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "RESTIC_PASSWORD")


def secret_env_values(environ: dict[str, str] | None = None) -> set[str]:
    """Values of the known secret variables in the environment (8+ characters), used only to scrub output."""
    e = os.environ if environ is None else environ
    return {v for k in SECRET_KEYS if len(v := e.get(k, "")) >= 8}


def env_secret_values(env_files: list[Path]) -> set[str]:
    """Values from .env files (never their keys' names), used only to scrub output."""
    vals: set[str] = set()
    for f in env_files:
        try:
            text = f.expanduser().read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            v = line.split("=", 1)[1].strip().strip("'\"")
            if len(v) >= 8:
                vals.add(v)
    return vals


class Redactor:
    """Scrubs secrets and shortens the home directory to ``~`` in every string of a document."""

    def __init__(self, secrets: set[str], home: str | None = None):
        self.secrets = sorted(secrets, key=len, reverse=True)
        self.home = home if home is not None else str(Path.home())

    def text(self, s: str) -> str:
        if self.home and len(self.home) > 1:
            s = s.replace(self.home, "~")
        for v in self.secrets:
            if v in s:
                s = s.replace(v, "[redacted]")
        for pat in _SECRET_PATTERNS:
            s = pat.sub("[redacted]", s)
        return s

    def __call__(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self(v) for v in obj]
        return obj
