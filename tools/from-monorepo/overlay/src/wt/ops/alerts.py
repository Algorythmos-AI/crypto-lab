"""Push alerts to the owner's phone through ntfy (plan R13).

Rules:
  * Messages carry R and % only. Dollar amounts and account numbers are scrubbed before sending.
  * Alerts fire on state *transitions*. `fire(key)` sends only when `key` was not already firing, and
    `resolve(key)` sends a recovery notice only when it was. `once_per_day` covers refusals.
  * Delivery never raises. When ntfy is unreachable, the message goes to a spool that the next call flushes.
    Spooled priority 1-3 messages older than a day are dropped rather than delivered late; priority 4-5 messages
    are kept for a week. The spool holds at most 200 messages (oldest dropped first). A message delivered late says
    when it was raised. Without a topic nothing is spooled: alerts are simply off.
  * Pager wraps Alerts for the trading loop: a bounded queue drained by one background thread, so a slow or
    offline ntfy can never delay an exit.

Priorities: 5 trading-critical (not flat, unknown or short position, latch) · 4 job failed ·
3 refusal / disk / late · 2 daily summary.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

from wt.core.config import STATE_DIR

ALERT_DIR = STATE_DIR / "alerts"
SPOOL_MAX_AGE = dt.timedelta(days=1)             # priority 1-3
SPOOL_MAX_AGE_URGENT = dt.timedelta(days=7)      # priority 4-5: trading-critical, kept until delivered
SPOOL_MAX_FILES = 200
LATE_AFTER = dt.timedelta(minutes=2)
_MONEY = re.compile(r"(?:US|A|AU)?\$\s?-?[\d,]+(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|AUD)\b", re.I)
# Alpaca paper account ids (PA + alphanumerics) and any bare number of 10+ digits; 8-digit dates survive.
_ACCOUNT = re.compile(r"\bPA[0-9A-Z]{6,}\b|\b\d{10,}\b")


def scrub(text: str) -> str:
    """Remove dollar amounts and long account-like numbers (R13: R and % only)."""
    return _ACCOUNT.sub("[id]", _MONEY.sub("[$]", text))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_state(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "state.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


_DAY_KEY = re.compile(r":(\d{4}-\d{2}-\d{2})$")
KEEP_DAYS = 14


def _prune(state: dict[str, Any]) -> dict[str, Any]:
    """Drop once-per-day keys older than two weeks so the state file stays small."""
    cutoff = (_now() - dt.timedelta(days=KEEP_DAYS)).date().isoformat()
    out = {}
    for k, v in state.items():
        m = _DAY_KEY.search(k)
        if m and m.group(1) < cutoff:
            continue
        out[k] = v
    return out


HISTORY_MAX_BYTES = 2_000_000


def _history(root: Path, key: str, event: str, title: str, priority: int | None = None) -> None:
    """Append a fired/resolved transition to history.jsonl (the dashboard's alert log). Never raises: a full disk
    or an odd path must not turn an alert into a crash. The file is cut to its newer half past 2 MB."""
    try:
        path = root / "history.jsonl"
        with open(path, "a") as fh:
            fh.write(json.dumps({"at": _now().isoformat(timespec="seconds"), "key": key[:80], "event": event,
                                 "title": scrub(title)[:120], "priority": priority}) + "\n")
        if path.stat().st_size > HISTORY_MAX_BYTES:
            lines = path.read_text().splitlines(keepends=True)
            tmp = root / f".history.{uuid.uuid4().hex}.tmp"
            try:
                tmp.write_text("".join(lines[len(lines) // 2:]))
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)                 # never leave a temp file behind (a full disk)
    except Exception:  # noqa: BLE001, S110 — history is best effort by design
        pass


def _write_state(root: Path, state: dict[str, Any]) -> None:
    state = _prune(state)
    tmp = root / f".state.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, root / "state.json")


def _send(msg: dict[str, Any], topic: str | None, server: str, timeout: float = 5.0) -> bool:
    if not topic:
        return False
    headers = {"Title": msg["title"], "Priority": str(msg["priority"])}
    if msg.get("tags"):
        headers["Tags"] = ",".join(msg["tags"])
    try:
        r = requests.post(f"{server.rstrip('/')}/{topic}", data=msg["message"].encode("utf-8"), headers=headers,
                          timeout=timeout)
        return 200 <= r.status_code < 300
    except requests.RequestException:
        return False


class Alerts:
    """ntfy delivery with a spool and transition state under ``root`` (default ``var/alerts``)."""

    def __init__(self, root: Path | None = None, topic: str | None = None, server: str | None = None) -> None:
        self.root = root or ALERT_DIR
        self.topic = topic if topic is not None else os.environ.get("NTFY_TOPIC") or None
        self.server = server or os.environ.get("NTFY_SERVER") or "https://ntfy.sh"

    # ---- delivery -------------------------------------------------------------------------------
    def notify(self, title: str, message: str, priority: int = 3, tags: tuple[str, ...] = ()) -> bool:
        """Send now (after flushing older spooled messages). Returns True when delivered."""
        msg: dict[str, Any] = {"id": uuid.uuid4().hex, "at": _now().isoformat(), "title": scrub(title)[:120],
               "message": scrub(message)[:1000], "priority": max(1, min(5, int(priority))), "tags": list(tags)}
        if not self.topic:
            return False                     # alerts are off; spooling would only grow without bound
        self.flush()
        if _send(msg, self.topic, self.server):
            return True
        spool = self.root / "spool"
        try:
            spool.mkdir(parents=True, exist_ok=True)
            # nanosecond prefix keeps the flush order equal to the send order, even within one second
            (spool / f"{time.time_ns():020d}-{msg['id']}.json").write_text(json.dumps(msg))
            for old in sorted(spool.glob("*.json"))[:-SPOOL_MAX_FILES]:
                old.unlink(missing_ok=True)
        except OSError:
            pass                             # a full disk must not turn an alert into a crash
        return False

    def flush(self) -> int:
        """Deliver spooled messages oldest first, dropping expired ones. Returns the number delivered."""
        spool = self.root / "spool"
        if not spool.exists() or not self.topic:
            return 0
        sent = 0
        for f in sorted(spool.glob("*.json")):
            try:
                msg = json.loads(f.read_text())
                raised = dt.datetime.fromisoformat(msg["at"])
                age = _now() - raised
                urgent = int(msg.get("priority", 3)) >= 4
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                f.unlink(missing_ok=True)
                continue
            if age > (SPOOL_MAX_AGE_URGENT if urgent else SPOOL_MAX_AGE):
                f.unlink(missing_ok=True)
                continue
            if age > LATE_AFTER:
                msg = {**msg, "message": f"(delayed: raised {raised:%Y-%m-%d %H:%M} UTC) {msg['message']}"}
            if not _send(msg, self.topic, self.server):
                break                       # still offline: keep order, try again next time
            f.unlink(missing_ok=True)
            sent += 1
        return sent

    # ---- transitions ----------------------------------------------------------------------------
    def fire(self, key: str, title: str, message: str, priority: int = 4, tags: tuple[str, ...] = ()) -> bool:
        """Alert when ``key`` starts firing. Repeats while it keeps firing are recorded but not sent."""
        with _locked(self.root):
            state = _read_state(self.root)
            cur = state.get(key) or {}
            if cur.get("firing"):
                cur["last_seen"] = _now().isoformat()
                state[key] = cur
                _write_state(self.root, state)
                return False
            state[key] = {"firing": True, "since": _now().isoformat(), "last_seen": _now().isoformat(),
                          "title": scrub(title)[:120]}
            _write_state(self.root, state)
            _history(self.root, key, "fired", title, priority)
        self.notify(title, message, priority, tags)
        return True

    def resolve(self, key: str, title: str, message: str, priority: int = 2) -> bool:
        """Send a recovery notice when ``key`` was firing; otherwise do nothing."""
        with _locked(self.root):
            state = _read_state(self.root)
            if not (state.get(key) or {}).get("firing"):
                return False
            state[key] = {"firing": False, "resolved": _now().isoformat()}
            _write_state(self.root, state)
            _history(self.root, key, "resolved", title, priority)
        self.notify(title, message, priority, ("white_check_mark",))
        return True

    def once_per_day(self, key: str, title: str, message: str, priority: int = 3, day: str | None = None) -> bool:
        """At most one alert per ``key`` per calendar day (Sydney date by default)."""
        day = day or dt.datetime.now(dt.UTC).astimezone().date().isoformat()
        return self.fire(f"{key}:{day}", title, message, priority)

    def firing(self) -> dict[str, Any]:
        """Keys currently firing (for the dashboard). A once-per-day key is an event of its day, and nothing ever
        resolves it, so it stops counting after the day that follows: without this, two weeks of daily summaries
        and refusals (and, after a host move, the old host's) all show as firing."""
        cutoff = (_now() - dt.timedelta(days=1)).date().isoformat()
        out = {}
        for k, v in _read_state(self.root).items():
            m = _DAY_KEY.search(k)
            if isinstance(v, dict) and v.get("firing") and not (m and m.group(1) < cutoff):
                out[k] = v
        return out


class Pager:
    """Alerts for a trading loop that must never wait on the network (audit L4).

    Calls return at once: they go on a bounded queue that one daemon thread delivers through ``Alerts`` (which
    takes the state lock, flushes the spool and posts to ntfy). If the queue is full the alert is dropped and
    counted; the job runner's end-of-session check still raises the same keys. ``alerts=None`` makes a silent
    pager (tests and dry runs)."""

    def __init__(self, alerts: Any = None, maxsize: int = 64) -> None:
        self.alerts = alerts
        self.dropped = 0
        self._q: queue.Queue[tuple[str, tuple[Any, ...]] | None] = queue.Queue(maxsize=maxsize)
        self._t: threading.Thread | None = None
        if alerts is not None:
            self._t = threading.Thread(target=self._drain, name="pager", daemon=True)
            self._t.start()

    def fire(self, key: str, title: str, message: str, priority: int = 4) -> None:
        self._put("fire", (key, title, message, priority))

    def resolve(self, key: str, title: str, message: str) -> None:
        self._put("resolve", (key, title, message))

    def once_per_day(self, key: str, title: str, message: str, priority: int = 3) -> None:
        self._put("once_per_day", (key, title, message, priority))

    def _put(self, method: str, args: tuple[Any, ...]) -> None:
        if self._t is None:
            return
        try:
            self._q.put_nowait((method, args))
        except queue.Full:
            self.dropped += 1

    def _drain(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            method, args = item
            try:
                getattr(self.alerts, method)(*args)
            except Exception:  # noqa: BLE001 — delivery must never take the pager thread down
                pass

    def close(self, timeout_s: float = 15.0) -> None:
        """Deliver what is queued, waiting at most ``timeout_s`` (called once, after the session)."""
        if self._t is None:
            return
        try:
            self._q.put(None, timeout=timeout_s)
        except queue.Full:
            return
        self._t.join(timeout_s)
