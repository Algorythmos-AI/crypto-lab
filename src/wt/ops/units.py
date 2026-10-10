"""systemd units for the host, generated from `wt.ops.schedule.JOBS`.

    python -m wt.ops.units render --out DIR --root /home/wt/crypto-lab   # write cryptolab-<job>.service/.timer
    python -m wt.ops.units diff --root /home/wt/crypto-lab               # which installed units differ
    python -m wt.ops.units fingerprint --root /home/wt/crypto-lab        # what cryptolab-install-units installs, hashed

No automated contributor installs or reloads units: `cryptolab-deploy` renders them into var/units/ and the owner
installs them (`sudo cryptolab-install-units`).

Host identity (CRY-DEC-0001). The host is shared with the repository this desk was extracted from, whose units are
named `wt-<job>` and whose installer removes or re-enables anything in its own list. So everything this repository
puts on the host carries its own prefix (`wt.ops.host.UNIT_PREFIX`): the job units, the static units
(deploy/systemd), the helpers (deploy/bin) and the bookkeeping directory (`ETC_DIR`). The job names, and with them
the healthchecks slugs, are the ones the jobs always had. The secrets are still the shared host's
(`wt-secrets.service` and its tmpfs file): this repository reads them and never writes or restarts them.

Design, unchanged from the origin:
  * Jobs run as the unprivileged `wt` user. `Wants=` (not `Requires=`) the secrets unit, so restarting it for a
    key rotation never restarts a job.
  * Type=exec with RuntimeMaxSec above each job's own deadline: the jobs time themselves; systemd only catches a
    runaway. RestartPreventExitStatus=124 keeps a deadline kill from restarting.
  * An interval job never restarts, so it has no loop to bound (StartLimitIntervalSec=0); the clock-time jobs get
    at most 3 starts in 6 h.
  * KillMode=mixed + TimeoutStopSec=60: SIGTERM reaches the job runner, which forwards it to the job, waits,
    writes its heartbeat and pages (wt.ops.jobs); systemd kills the rest after 60 s.
  * OnFailure pages through the alert unit, which doesn't depend on the secrets unit.
  * AccuracySec=1s, Persistent=false: a missed start is not replayed after a reboot; the next bar cycle makes up
    a missed one, and the daily jobs run again the next day.

Origin: trading-lab-research `src/wt/ops/units.py` at 217a4eb (trimmed). Removed: the memory limits of the five
stocks jobs and the paper runner's OOM setting. Changed: the unit prefix, the description's product name, the
alert unit's name, the helper and static-unit directories (`deploy/bin`, `deploy/systemd`), and the files the
fingerprint covers. The unit text of the four crypto jobs is otherwise what the origin renders.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from wt.ops.host import UNIT_PREFIX, unit
from wt.ops.schedule import JOBS, PY, Job

UNIT_DIR = Path("/etc/systemd/system")
SECRETS_UNIT = "wt-secrets.service"           # the shared host's; read, never written (see the module docstring)
SECRETS_FILE = "/run/wt-secrets/env"
USER = "wt"
INSTALL_ROOT = Path("/home/wt/crypto-lab")
ETC_DIR = Path("/etc/cryptolab")              # installed-units.txt and installed.sha256 (cryptolab-install-units)
HELPER_DIR = Path("deploy/bin")               # installed to /usr/local/bin by cryptolab-install-units
STATIC_DIR = Path("deploy/systemd")           # installed to /etc/systemd/system by cryptolab-install-units
ALERT_UNIT = f"{UNIT_PREFIX}alert@%n.service"

# cgroup memory per job on the 4 GB host: (MemoryHigh, MemoryMax, MemorySwapMax). A job throttles at High and is
# killed at Max rather than pushing another platform's job, which may run at the same time, into the OOM killer.
MEMORY: dict[str, tuple[str | None, str, str | None]] = {
    "crypto": (None, "512M", "128M"),
    "dashboard-crypto": (None, "512M", "128M"),
    "crypto-challengers": ("1G", "1536M", "256M"),
    "crypto-learn": ("1536M", "2G", "512M"),
}


def service(job: Job, root: Path, user: str = USER) -> str:
    restart = "on-failure" if job.trading else "no"            # an interval job just runs again in 15 minutes
    lines = [
        "[Unit]",
        f"Description=Crypto Lab {job.name}: {job.what}",
        # time-sync.target (with chrony-wait enabled): a job never starts on an unsynced clock
        f"Wants={SECRETS_UNIT} network-online.target time-sync.target",
        f"After={SECRETS_UNIT} network-online.target time-sync.target",
        f"OnFailure={ALERT_UNIT}",
        # the limit counts timer starts too, so an interval job must not have one
        *(["StartLimitIntervalSec=0"] if job.interval_s else ["StartLimitIntervalSec=6h", "StartLimitBurst=3"]),
        "",
        "[Service]",
        "Type=exec",
        f"User={user}",
        f"Group={user}",
        f"WorkingDirectory={root}",
        f"EnvironmentFile={SECRETS_FILE}",
        f"Environment=WT_ENV_FILE={SECRETS_FILE}",
        "Environment=WT_HOST=systemd",
        "Environment=PYTHONPATH=src",
        "Environment=PYTHONUNBUFFERED=1",
        "Environment=TZ=UTC",
        f"ExecStart={root / PY} -m wt.ops.jobs run {job.name}",
        f"Restart={restart}",
        "RestartSec=60",
        "RestartPreventExitStatus=124",
        f"RuntimeMaxSec={int(job.runtime_max_h * 3600)}",
        "KillMode=mixed",
        "TimeoutStopSec=60",
        "LimitCORE=0",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectSystem=full",
    ]
    if job.name in MEMORY:
        high, cap, swap = MEMORY[job.name]
        lines += [f"MemoryHigh={high}"] if high else []
        lines += [f"MemoryMax={cap}"] + ([f"MemorySwapMax={swap}"] if swap else [])
    return "\n".join(lines) + "\n"


def timer(job: Job) -> str:
    if job.calendars:
        assert job.interval_s, f"{job.name}: a calendar job also states its interval"
        when = [f"OnCalendar={c}" for c in job.calendars]
    elif job.calendar:
        assert job.interval_s, f"{job.name}: a calendar job also states its interval"
        when = [f"OnCalendar={job.calendar} UTC"]
    elif job.interval_s:
        when = ["OnBootSec=2min", f"OnUnitActiveSec={job.interval_s}s"]
    else:
        assert job.et, f"{job.name} has no systemd schedule (Job.et)"
        when = [f"OnCalendar={job.et} America/New_York"]
    return "\n".join([
        "[Unit]",
        f"Description=Start Crypto Lab {job.name}",
        "",
        "[Timer]",
        *when,
        "AccuracySec=1s",
        "Persistent=false",
        f"Unit={unit(job)}.service",
        "",
        "[Install]",
        "WantedBy=timers.target",
    ]) + "\n"


def render_all(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for job in JOBS.values():
        out[f"{unit(job)}.service"] = service(job, root)
        out[f"{unit(job)}.timer"] = timer(job)
    return out


def installed_files(root: Path, install_root: Path | None = None) -> dict[str, bytes]:
    """Everything cryptolab-install-units puts outside the repo, by install path: the rendered job units, the
    static units and the helpers, read from the checkout at `root`. Their fingerprint tells a deploy whether a
    release needs the owner (the units or a helper changed) or can go in as code only. `install_root`: the
    checkout path the units point at (default `root`); a candidate commit checked out elsewhere is rendered for
    the live checkout."""
    out = {f"{UNIT_DIR}/{n}": t.encode() for n, t in render_all(install_root or root).items()}
    for f in sorted((root / STATIC_DIR).iterdir()):
        if f.is_file():
            out[f"{UNIT_DIR}/{f.name}"] = f.read_bytes()
    for f in sorted((root / HELPER_DIR).iterdir()):
        if f.is_file():
            out[f"/usr/local/bin/{f.name}"] = f.read_bytes()
    return out


def foreign_names(root: Path) -> list[str]:
    """Names this repository would install that do not carry its prefix. Must be empty: a name without the
    prefix could be another platform's on the shared host."""
    return sorted(Path(p).name for p in installed_files(root) if not Path(p).name.startswith(UNIT_PREFIX))


def fingerprint(root: Path, install_root: Path | None = None) -> str:
    """One line per installed file ("<sha256>  <path>"), sorted: the same text for the same install."""
    return "".join(f"{hashlib.sha256(b).hexdigest()}  {p}\n"
                   for p, b in sorted(installed_files(root, install_root).items()))


def diff(rendered: dict[str, str], unit_dir: Path = UNIT_DIR) -> list[str]:
    out = []
    for name, text in sorted(rendered.items()):
        p = unit_dir / name
        if not p.exists():
            out.append(f"{name}: not installed")
        elif p.read_text() != text:
            out.append(f"{name}: differs from the code")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.ops.units")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--root", type=Path, default=Path.cwd())
    d = sub.add_parser("diff")
    d.add_argument("--root", type=Path, default=Path.cwd())
    d.add_argument("--unit-dir", type=Path, default=UNIT_DIR)
    f = sub.add_parser("fingerprint", help="print what cryptolab-install-units would install, hashed")
    f.add_argument("--root", type=Path, default=Path.cwd())
    f.add_argument("--install-root", type=Path, help="the checkout the units run from (default: --root)")
    a = ap.parse_args(argv)
    root = a.root if a.root.is_absolute() else a.root.resolve()               # keep /home as given
    if a.cmd == "fingerprint":
        sys.stdout.write(fingerprint(root, a.install_root))
        return 0
    units = render_all(root)
    if a.cmd == "render":
        a.out.mkdir(parents=True, exist_ok=True)
        for name, text in units.items():
            (a.out / name).write_text(text)
        print(f"rendered {len(units)} units into {a.out}")
        return 0
    changes = diff(units, a.unit_dir)
    print("\n".join(changes) if changes else "systemd units match the code")
    return 1 if changes else 0


if __name__ == "__main__":
    sys.exit(main())
