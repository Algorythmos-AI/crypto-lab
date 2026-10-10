"""Off-host protection of the desk's evidence.

    python -m wt.ops.backup nightly             # verify chains -> restic backup to R2 -> write-once daily anchor
    python -m wt.ops.backup restore-test        # restore this repository's latest snapshot and verify it
    python -m wt.ops.backup extends <key>       # do the ledgers on disk still extend the anchor stored at <key>?

Why each piece:
  * The desk's journal and the harvest's journal are the evidence its gates rest on, and live on one disk. restic
    keeps encrypted, deduplicated snapshots in Cloudflare R2.
  * A hash chain can't show an edit to its last line, or a truncated tail. The daily anchor writes the line count
    and head hash to a bucket that is locked (write-once, If-None-Match: *), so tomorrow's chain must extend it.
  * A broken chain turns the desk's entries off (a flag file the cycle checks) and pages; exits keep being managed.
  * `extends` is for the day the state directory moves between checkouts: it shows that the journal now on disk
    is the one the last anchor of the old checkout described, grown and not rewritten.

Its own identity (CRY-DEC-0001). The restic repository and the anchor bucket are shared with the repository the
desk was extracted from, which keeps backing up its own state on the same host. So this repository's snapshots
carry their own restic host (`<host>-crypto-lab`) and its anchors their own path (`anchors/crypto-lab/`): neither
backup restores, tests or anchors the other's files. The day in an anchor's name is the UTC day, the desk's own.

Environment: RESTIC_REPOSITORY, RESTIC_PASSWORD, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (restic
reads them as AWS_* credentials); R2_ANCHOR_BUCKET (default wt-anchors).

Origin: trading-lab-research `src/wt/ops/backup.py` at 217a4eb (trimmed). Removed: the stocks ledgers, their
chain flag and digest, the weekday rule for the market-data caches and `prune` (run from the owner's machine for
the whole repository). Changed: the restic host, the anchor path and its day, the healthchecks slug, the alert
titles. Unchanged: `extends`, `anchor`, the restic arguments, and the restore test's rule that every ledger that
had lines must come back intact. Added: the `extends` command.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from wt.core import ledger
from wt.core.config import ROOT, STATE_DIR
from wt.core.desk import DESKS, Desk, installed
from wt.ops import hc
from wt.ops.alerts import Alerts
from wt.ops.r2 import R2

PLATFORM = "crypto-lab"
DIGEST = STATE_DIR / "backup" / "evidence_digest.json"
ANCHOR_BUCKET = os.environ.get("R2_ANCHOR_BUCKET", "wt-anchors")
ANCHOR_PREFIX = f"anchors/{PLATFORM}/"
NIGHTLY = ["var", "logs"]
WEEKLY = ["data"]                                          # the history cache: quick to refetch, not evidence
WEEKLY_DAY = 4                                             # Friday (the backup timer fires every day)
HC_SLUG = "wt-crypto-backup"


def host() -> str:
    """The restic host of this repository's snapshots: the machine's name with the platform's, so that the other
    repository's backup on the same machine (restic host = the machine's name) never restores or forgets these."""
    return f"{os.environ.get('WT_HOST_ID') or socket.gethostname().split('.')[0]}-{PLATFORM}"


def role() -> str:
    return os.environ.get("WT_ROLE", "primary")


def anchor_key(day: dt.date) -> str:
    key = f"{ANCHOR_PREFIX}{day.isoformat()}.json"
    return key if role() != "shadow" else f"shadow/{host()}/{key}"


def restic(*args: str, timeout: float = 3600) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "AWS_ACCESS_KEY_ID": os.environ.get("R2_ACCESS_KEY_ID", ""),
           "AWS_SECRET_ACCESS_KEY": os.environ.get("R2_SECRET_ACCESS_KEY", ""), "AWS_DEFAULT_REGION": "auto"}
    return subprocess.run(["restic", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)


def desk_ledgers() -> list[tuple[Desk, str, Path]]:
    """Every hash-chained ledger of the desk, when the desk is on this host."""
    return [(d, name, path) for d in DESKS.values() if installed(d) for name, path in d.ledgers]


def chains() -> dict[str, dict[str, Any]]:
    out = {}
    for _, name, path in desk_ledgers():
        n, head = ledger.head(path)
        out[name] = {"lines": n, "head": head, "problems": ledger.verify_chain(path)[:5]}
    return out


def flag_chain(problems: list[str], alerts: Alerts, desk: Desk) -> None:
    """A broken chain switches off entries on the desk that owns the ledger."""
    desk.chain_flag.parent.mkdir(parents=True, exist_ok=True)
    desk.chain_flag.write_text("\n".join(problems) + "\n")
    alerts.fire(f"{desk.alert_prefix}evidence-chain", f"Evidence chain broken: {desk.name} entries off",
                f"{len(problems)} problem(s) in the ledger hash chain; exits are still managed. First: {problems[0]}", 4)


def extends(anchored: Any, paths: dict[str, Path]) -> bool:
    """True when every anchored (lines, head) is still line `lines` of its ledger and the ledger's chain is
    intact: the file only grew since. The head line alone is not enough: an earlier line can be rewritten without
    touching it, and only the chain shows that."""
    if not isinstance(anchored, dict) or not anchored:
        return False
    for name, a in anchored.items():
        if not isinstance(a, dict) or name not in paths or ledger.verify_chain(paths[name]):
            return False
        n, head = a.get("lines"), a.get("head")
        if n == 0 and head is None:
            continue                                        # anchored empty: anything since is an append
        if not isinstance(n, int) or ledger.head_at(paths[name], n) != head:
            return False
    return True


def anchor(day: dt.date, state: dict[str, dict[str, Any]], client: R2 | None = None,
           paths: dict[str, Path] | None = None) -> tuple[bool, str]:
    """Write-once anchor of today's chain heads. A second write of an anchored day is fine when the heads are the
    same, or when the ledgers have only grown since: every anchored (lines, head) is still line `lines` of its
    ledger and the ledger's chain is intact (`paths` names the files; without them only "the same" passes).
    Anything else means history was rewritten.

    The journal grows every 15 minutes, so a second backup of a day is normal, not an alarm."""
    c = client or R2()
    if not c.configured:
        return False, "R2 not configured"
    body = json.dumps({k: {"lines": v["lines"], "head": v["head"]} for k, v in state.items()}, sort_keys=True)
    key = anchor_key(day)
    w = c.put(ANCHOR_BUCKET, key, body.encode(), if_none_match="*")
    if w.status in (200, 201):
        return True, f"anchored {key}"
    if w.status in (409, 412):
        # 412: the object exists (If-None-Match). 409: the locked bucket refuses to overwrite an object under
        # retention, which is the same fact told another way. Either way the answer is in what is stored: read it
        # back and compare.
        existing = c.get(ANCHOR_BUCKET, key)
        if existing.status != 200:
            return False, f"anchor write answered HTTP {w.status} and {key} could not be read back (HTTP {existing.status})"
        try:
            was = json.loads(existing.body) if existing.status == 200 else None
        except ValueError:
            was = None
        if was is not None and was == json.loads(body):
            return True, f"{key} already anchored"
        if was is not None and paths and extends(was, paths):
            return True, f"{key} already anchored; the ledger has only grown since"
        return False, f"{key} already anchored with DIFFERENT heads"
    return False, f"anchor write answered HTTP {w.status}"


def nightly(alerts: Alerts | None = None, weekly: bool | None = None, client: R2 | None = None,
            today: dt.date | None = None) -> int:
    alerts = alerts or Alerts()
    today = today or dt.datetime.now(dt.UTC).date()
    ledgers = desk_ledgers()
    if not ledgers:
        # CRY-DEC-0001: with no desk on this host there is nothing to protect, and saying "ok" would hide that.
        print("Refusing: the desk has no state on this host; nothing to back up or anchor", file=sys.stderr)
        alerts.fire("backup", "Crypto Lab: backup found no desk", "The desk's state directory is missing.", 4)
        hc.ping(HC_SLUG, "fail", "no desk state")
        return 2
    state = chains()
    problems = [f"{k}: {p}" for k, v in state.items() for p in v["problems"]]
    for desk in DESKS.values():
        mine = [f"{n}: {p}" for d, n, _ in ledgers if d is desk for p in state[n]["problems"]]
        if mine:
            flag_chain(mine, alerts, desk)
    code = 0
    paths = NIGHTLY + (WEEKLY if (weekly if weekly is not None else today.weekday() == WEEKLY_DAY) else [])
    r = restic("backup", "--host", host(), "--tag", "nightly", "--tag", f"role:{role()}", "--tag", PLATFORM,
               "--exclude-caches", *[p for p in paths if (ROOT / p).exists()])
    if r.returncode not in (0, 3):                       # 3 = snapshot made, some files unreadable
        alerts.fire("backup", "Crypto Lab: backup failed", f"restic exit {r.returncode}; see the backup log.", 4)
        print((r.stderr or r.stdout)[-800:], file=sys.stderr)
        code = 1
    elif r.returncode == 3:
        print("restic: snapshot saved; some files were unreadable (see above)", file=sys.stderr)
    ok, why = anchor(today, state, client, {name: path for _, name, path in ledgers})
    print(f"anchor: {why}")
    if not ok:
        alerts.fire("anchor", "Crypto Lab: daily evidence anchor failed", why, 4 if "DIFFERENT" in why else 3)
        code = code or 1
    DIGEST.parent.mkdir(parents=True, exist_ok=True)
    DIGEST.write_text(json.dumps({"day": today.isoformat(), "verified": not problems, "anchored": ok,
                                  **{k: {"lines": v["lines"], "head": v["head"]} for k, v in state.items()}},
                                 indent=1))
    if ok:
        alerts.resolve("anchor", "Crypto Lab: daily evidence anchor ok", why)
    if code == 0 and not problems:
        alerts.resolve("backup", "Crypto Lab: backup ok", "Backed up and anchored.")
    hc.ping(HC_SLUG, "" if code == 0 and not problems else "fail", f"anchor: {why}")
    return code


def restore_test() -> int:
    """Restore the latest snapshot's ledgers into a temp dir and check their chains (weekly)."""
    tmp = Path(tempfile.mkdtemp(prefix="cryptolab-restore-"))
    try:
        # By file name: restic stores a snapshot's paths relative to the backed-up directories' common parent,
        # so the ledgers' absolute paths match nothing.
        names = [a for _, _, path in desk_ledgers() for a in ("--include", path.name)]
        r = restic("restore", "latest", "--host", host(), "--target", str(tmp), *names)   # this repository's own latest
        if r.returncode != 0:
            print((r.stderr or r.stdout)[-800:], file=sys.stderr)
            return 1
        restored = {p.name: p for p in tmp.rglob("*.jsonl")}
        bad = {n: ledger.verify_chain(p)[:3] for n, p in restored.items() if ledger.verify_chain(p)}
        # every ledger that had lines when it was backed up must come back: a missing one is not "intact"
        missing = sorted(path.name for _, _, path in desk_ledgers() if path.exists() and path.name not in restored)
        print(f"restore-test: {len(restored)} ledger(s) restored; " + (f"BROKEN {bad}" if bad else "chains intact")
              + (f"; MISSING {missing}" if missing else ""))
        return 1 if bad or missing or not restored else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_extends(key: str, client: R2 | None = None) -> int:
    """Read the anchor stored at `key` (any anchor of this desk's ledgers, whichever checkout wrote it) and say
    whether the ledgers on disk still extend it. Reads only."""
    c = client or R2()
    if not c.configured:
        print("R2 not configured: cannot read the anchor")
        return 2
    got = c.get(ANCHOR_BUCKET, key)
    if got.status != 200:
        print(f"{key} could not be read (HTTP {got.status})")
        return 2
    try:
        was = json.loads(got.body)
    except ValueError:
        print(f"{key} is not JSON")
        return 2
    paths = {name: path for _, name, path in desk_ledgers()}
    for name, a in sorted(was.items()) if isinstance(was, dict) else []:
        n, head = (a.get("lines"), a.get("head")) if isinstance(a, dict) else (None, None)
        now_n = ledger.head(paths[name])[0] if name in paths else None
        print(f"  {name}: anchored {n} lines, head {str(head)[:12]}; on disk {now_n} lines")
    ok = extends(was, paths)
    print(f"{key}: the ledgers on disk " + ("EXTEND it (only grown, chains intact)" if ok else "DO NOT extend it"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.ops.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("nightly")
    sub.add_parser("restore-test")
    e = sub.add_parser("extends")
    e.add_argument("key", help="an anchor's key in the anchor bucket, e.g. anchors/crypto/2026-10-12.json")
    a = ap.parse_args(argv)
    if a.cmd == "nightly":
        return nightly()
    return restore_test() if a.cmd == "restore-test" else check_extends(a.key)


if __name__ == "__main__":
    sys.exit(main())
