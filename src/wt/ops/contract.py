"""The snapshot contract: the schema the dashboard validates the desk's snapshot with, and a deploy's check of it.

    python -m wt.ops.contract             print the contract's files with their sha256 (what contracts/SHA256SUMS holds)
    python -m wt.ops.contract --check     exit 1 when contracts/SHA256SUMS does not match the files

The dashboard lives in another repository. It vendors `contracts/crypto.schema.json` (and the fixture beside it)
with a hash check, and its ingest rejects any field the schema does not list. So a publisher whose schema the
dashboard does not have yet would have every snapshot refused.

A deploy is therefore allowed when the target's schema is byte for byte the live checkout's, and otherwise only
when the owner names the new schema's hash: `WT_DEPLOY_CONTRACT=<sha256, at least 12 hex digits>`. The owner reads
that hash from the dashboard's own vendored copy, which is the statement that the dashboard already has it. This
takes the place of the origin's check of the live dashboard's build commit, which needed both in one repository.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from wt.core.config import ROOT

DIR = "contracts"
SCHEMA = f"{DIR}/crypto.schema.json"
FIXTURE = f"{DIR}/crypto.v1.json"
SUMS = f"{DIR}/SHA256SUMS"
FILES = (SCHEMA, FIXTURE)
ACK_ENV = "WT_DEPLOY_CONTRACT"
MIN_ACK = 12


@dataclass(frozen=True)
class Agreed:
    ok: bool
    why: str
    live: str | None            # sha256 of the live checkout's schema (None: it has none yet)
    target: str | None          # sha256 of the target commit's schema
    acknowledged: bool          # the schema changes and the owner named the new hash


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sums(root: Path = ROOT) -> str:
    """The text of contracts/SHA256SUMS for the files as they are on disk (`shasum -a 256 -c` reads it)."""
    return "".join(f"{sha256((root / f).read_bytes())}  {Path(f).name}\n" for f in FILES)


def _show(root: Path, rev: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{path}"], capture_output=True, timeout=60)
    return r.stdout if r.returncode == 0 else None


def guard(root: Path = ROOT, target: str = "origin/main", ack: str | None = None) -> Agreed:
    """May `target` be deployed over the live checkout (HEAD), as far as the snapshot contract goes?"""
    new = _show(root, target, SCHEMA)
    if new is None:
        return Agreed(False, f"{target[:12]} has no {SCHEMA}: the desk cannot publish without its contract", None, None, False)
    old = _show(root, "HEAD", SCHEMA)
    live, want = (None if old is None else sha256(old)), sha256(new)
    if live == want:
        return Agreed(True, "", live, want, False)
    said = (os.environ.get(ACK_ENV, "") if ack is None else ack).strip().lower()
    if len(said) >= MIN_ACK and want.startswith(said):
        return Agreed(True, "", live, want, True)
    return Agreed(False,
                  f"the snapshot contract changes with this deploy ({SCHEMA}: {str(live)[:12]} -> {want[:12]}). The "
                  "dashboard rejects fields it does not know, so it must be deployed with the new schema first. Then "
                  f"run the deploy with {ACK_ENV}=<the sha256 the dashboard's vendored copy records>", live, want, False)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    text = sums()
    if args == ["--check"]:
        try:
            ok = (ROOT / SUMS).read_text() == text
        except OSError:
            ok = False
        print(f"{SUMS} matches the contract's files" if ok else f"{SUMS} is stale or missing: run `make contracts`")
        return 0 if ok else 1
    if args:
        print("usage: python -m wt.ops.contract [--check]", file=sys.stderr)
        return 2
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
