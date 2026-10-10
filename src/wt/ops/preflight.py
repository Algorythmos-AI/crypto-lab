"""Checks every job runs before it does anything: only reviewed code, in the locked environment, with room to
write and a way to page.

A failed check is a *refusal*: the job alerts once and exits 0. The service manager must not treat a refusal as
a crash and restart it.

Origin: trading-lab-research `src/wt/ops/preflight.py` at 217a4eb (trimmed). Removed: the check for the stocks
desk's runtime files left in tracked paths. Every other check is unchanged.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from wt.core.config import ROOT
from wt.ops import thresholds

CODE_PATHS = ("src", "scripts", "deploy", "config", "requirements.lock.txt", "requirements-ml.lock.txt", "pyproject.toml", "Makefile")
MIN_FREE_GB = thresholds.DISK_FLOOR_GB
LOCK_STAMP = ".venv/.wt-lock.sha256"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def _git(root: Path, *args: str, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout, env=env,
                          stdin=subprocess.DEVNULL)


def lock_hash(root: Path) -> str:
    return hashlib.sha256((root / "requirements.lock.txt").read_bytes()).hexdigest()


def check_git(root: Path) -> list[Check]:
    out = []
    try:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        out.append(Check("on main", branch == "main", f"HEAD is on {branch!r}"))
        anc = _git(root, "merge-base", "--is-ancestor", "HEAD", "origin/main").returncode
        out.append(Check("reviewed commit", anc == 0,
                         "HEAD is in origin/main" if anc == 0 else "HEAD has commits that are not on origin/main"))
        dirty = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *CODE_PATHS).stdout.strip()
        n = len(dirty.splitlines()) if dirty else 0
        out.append(Check("clean code", n == 0, "no changes under code paths" if n == 0 else
                         f"{n} modified or untracked file(s) under {', '.join(CODE_PATHS[:4])}, …"))
    except (OSError, subprocess.SubprocessError) as e:
        out.append(Check("git", False, f"git unavailable: {e.__class__.__name__}"))
    return out


def check_venv(root: Path) -> Check:
    stamp = root / LOCK_STAMP
    try:
        ok = stamp.read_text().strip() == lock_hash(root)
    except OSError:
        return Check("venv matches lockfile", False, "no sync stamp; run `make deploy`")
    return Check("venv matches lockfile", ok, "synced" if ok else "requirements.lock.txt changed since the last sync")


def check_disk(root: Path, min_free_gb: float = MIN_FREE_GB) -> Check:
    free = thresholds.disk_free_gb(root)
    return Check("free disk", free >= min_free_gb, f"{free:.1f} GB free (floor {min_free_gb:.0f} GB)")


def check_env_mode(root: Path) -> Check:
    """The secrets file is private: .env on a development machine, WT_ENV_FILE (tmpfs, written at boot) on the host."""
    env_file = os.environ.get("WT_ENV_FILE")
    p = Path(env_file) if env_file else root / ".env"
    if not p.exists():
        return Check(".env private", False, f"{p.name} missing")
    mode = p.stat().st_mode & 0o777
    if env_file and p.stat().st_size == 0:
        return Check(".env private", False, f"{p.name} is empty (secrets not fetched yet?)")
    return Check(".env private", mode & 0o077 == 0, f"mode {mode:o}")


def check_paging(root: Path) -> Check:
    """On the host nothing else would tell the owner a job failed: the ntfy topic must be configured."""
    ok = bool(os.environ.get("NTFY_TOPIC"))
    return Check("paging configured", ok, "ntfy topic set" if ok else "NTFY_TOPIC is empty: alerts would be silent")


def run_checks(root: Path | None = None, min_free_gb: float = MIN_FREE_GB) -> list[Check]:
    root = root or ROOT
    checks = [*check_git(root), check_venv(root), check_disk(root, min_free_gb), check_env_mode(root)]
    from wt.ops.host import current
    if current().kind == "systemd":
        checks.append(check_paging(root))
    return checks


def failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok]
