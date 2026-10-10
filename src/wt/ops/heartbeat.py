"""Job heartbeats: the last run of each job (var/heartbeats/<job>.json) and an append-only run history.

The collector publishes both, so the dashboard shows what ran, when, on which commit, and how it ended.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path
from typing import Any

from wt.core.config import STATE_DIR

HEARTBEAT_DIR = STATE_DIR / "heartbeats"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _atomic(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(body, indent=1, sort_keys=True))
    os.replace(tmp, path)


class Heartbeat:
    def __init__(self, job: str, sha: str | None, root: Path | None = None) -> None:
        self.root = root or HEARTBEAT_DIR
        self.rec: dict[str, Any] = {"job": job, "run_id": uuid.uuid4().hex[:12], "sha": sha, "started": _now(),
                                    "ended": None, "status": "running", "exit": None, "detail": ""}
        self.write()

    def write(self) -> None:
        try:
            _atomic(self.root / f"{self.rec['job']}.json", self.rec)
        except OSError:
            pass                        # a heartbeat must never take a job down (e.g. disk full)

    def finish(self, status: str, exit_code: int | None, detail: str = "") -> dict[str, Any]:
        """status: ok | failed | refused | timeout | skipped."""
        self.rec.update(ended=_now(), status=status, exit=exit_code, detail=detail[:300])
        self.write()
        try:
            with open(self.root / "runs.jsonl", "a") as fh:
                fh.write(json.dumps(self.rec, sort_keys=True) + "\n")
        except OSError:
            pass
        return self.rec


TAIL_BYTES = 4_000_000        # about 20,000 runs: over a month at the busiest schedule, far more than any reader needs


def recent_runs(root: Path | None = None, max_bytes: int = TAIL_BYTES) -> list[dict[str, Any]]:
    """The newest runs in the history, oldest first, reading only the end of the file.

    runs.jsonl only ever grows (every publish appends a line). Reading it whole would one day pass the size a
    reader accepts, and both dashboards would stop on the same night. A reader never needs more than two weeks.
    """
    path = (root or HEARTBEAT_DIR) / "runs.jsonl"
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]                 # the first line is cut mid-record
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def ok_runs_since(job: str, since: dt.datetime, root: Path | None = None) -> int:
    """How many runs of `job` ended ok at or after `since`, from the run history."""
    n = 0
    for r in recent_runs(root, max_bytes=400_000):
        try:
            if r.get("job") == job and r.get("status") == "ok" and dt.datetime.fromisoformat(r["ended"]) >= since:
                n += 1
        except (ValueError, KeyError, TypeError):
            continue
    return n


def thin_runs(runs: list[dict[str, Any]], frequent: set[str], keep_last: int = 60) -> list[dict[str, Any]]:
    """The run list a dashboard draws: every run of the session jobs, and for a job that runs many times a day
    its newest `keep_last` runs plus, for each earlier day, the first run and every run that did not end ok.
    A day's worst run is what the heatmap shows, so no bad day is lost and one job cannot crowd out the rest."""
    last: dict[str, int] = {}
    for r in runs:
        if r.get("job") in frequent:
            last[str(r.get("job"))] = last.get(str(r.get("job")), 0) + 1
    seen: set[tuple[str, str]] = set()
    left = dict(last)
    out = []
    for r in runs:
        job = str(r.get("job"))
        if job not in frequent:
            out.append(r)
            continue
        left[job] -= 1
        day = (job, str(r.get("started", ""))[:10])
        if left[job] < keep_last or r.get("status") != "ok" or day not in seen:
            out.append(r)
        seen.add(day)
    return out


def last_runs(root: Path | None = None) -> dict[str, dict[str, Any]]:
    out = {}
    for f in sorted((root or HEARTBEAT_DIR).glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return out
