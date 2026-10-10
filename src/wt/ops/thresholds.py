"""Operational thresholds for the jobs.

Disk is measured one way everywhere: free bytes on the volume that holds the repository, in decimal gigabytes
(1 GB = 10^9 bytes).

Origin: trading-lab-research `src/wt/ops/thresholds.py` at 217a4eb (trimmed). Removed: the stocks dashboard's
thresholds (swap, minimum trades and sessions, the loss-limit tone) and the table generated for the dashboard.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from wt.core.config import ROOT

DISK_FLOOR_GB = 3.0        # below this the jobs refuse
DISK_TARGET_GB = 15.0      # keep at least this free


def disk_free_gb(path: Path | None = None) -> float:
    """Free space in decimal GB on the volume holding ``path`` (default: the repository)."""
    return shutil.disk_usage(path or ROOT).free / 1e9


def disk_total_gb(path: Path | None = None) -> float:
    return shutil.disk_usage(path or ROOT).total / 1e9


def disk_tone(free_gb: float) -> str:
    """good | warn | bad."""
    if free_gb < DISK_FLOOR_GB:
        return "bad"
    return "warn" if free_gb < DISK_TARGET_GB else "good"
