"""The machine the jobs run on: systemd on the host, nothing on a development machine.

Everything host-specific the ops code needs goes through here:

  * running_jobs(jobs)  which jobs the service manager shows running right now;
  * loaded(job)         whether the job's unit is installed.

WT_HOST=systemd|local overrides the platform default (tests, and a Linux box that isn't the host). systemd
fails closed: when the service manager can't be asked, jobs count as running.

Origin: trading-lab-research `src/wt/ops/host.py` at 217a4eb (trimmed). Removed: the Mac's launchd host and
the swap reading for the stocks dashboard. Changed: the unit prefix (`UNIT_PREFIX`), so that no unit of this
repository shares a name with a unit of the repository it came from; a machine without systemd is `local`, which
runs no jobs.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Protocol

from wt.ops.schedule import Job

ACTIVE = {"active", "activating", "deactivating", "reloading"}
UNIT_PREFIX = "cryptolab-"         # every unit, helper and bookkeeping file of this repository starts with it


def unit(job: Job) -> str:
    return f"{UNIT_PREFIX}{job.name}"


class Host(Protocol):
    kind: str

    def running_jobs(self, jobs: list[Job]) -> dict[str, bool]: ...

    def loaded(self, job: Job) -> bool: ...


def _run(cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class LocalHost:
    """A development machine: no service manager runs this repository's jobs."""
    kind = "local"

    def running_jobs(self, jobs: list[Job]) -> dict[str, bool]:
        return {unit(j): False for j in jobs}

    def loaded(self, job: Job) -> bool:
        return False


class SystemdHost:
    kind = "systemd"

    def _state(self, name: str) -> str | None:
        try:
            r = _run(["systemctl", "show", "--property=ActiveState", "--value", f"{name}.service"])
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    def running_jobs(self, jobs: list[Job]) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for j in jobs:
            state = self._state(unit(j))
            if state is None:
                return {"systemctl": True}      # can't tell: treat as running (fail closed)
            out[unit(j)] = state in ACTIVE
        return out

    def loaded(self, job: Job) -> bool:
        try:
            return _run(["systemctl", "cat", f"{unit(job)}.service"]).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return True


def current() -> Host:
    kind = os.environ.get("WT_HOST") or ("systemd" if sys.platform.startswith("linux") else "local")
    return SystemdHost() if kind == "systemd" else LocalHost()
