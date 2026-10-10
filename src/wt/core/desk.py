"""The desk: what the crypto desk owns on a host, in one place.

A desk owns its runtime state, kill switch, journal, alert keys and snapshot. This repository runs one desk,
`crypto`; the record keeps the shape it had when two desks shared a repository, so the desk's own code is
unchanged.

Origin: trading-lab-research `src/wt/core/desk.py` at c5d9830 (trimmed). Removed: the `stocks` desk record and
its paths, and `moved_marker` and `moved` (the old repository's guard for a desk that has left it; this is
where the desk went). Changed: `desk_of_alert` answers `host` (not `stocks`) for a key that carries no desk's
prefix, and `installed` no longer has a desk that is always installed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wt.core.config import STATE_DIR

Session = Literal["us_equity", "always_open"]
HOST = "host"                           # the owner of an alert key no desk claims (a failed job, a backup)


@dataclass(frozen=True)
class Desk:
    name: str
    strategy: str                       # the key in config/risk.yaml and the tag in order ids
    state_dir: Path
    kill_file: Path                     # while it exists the desk makes no new entries; exits are still managed
    ledgers: tuple[tuple[str, Path], ...]   # (name in the backup anchor, hash-chained file); file names are unique
    chain_flag: Path                    # written by the backup when one of the ledgers' chains is broken
    session: Session
    alert_prefix: str | None            # alert keys that belong to this desk
    snapshot_schema: str

    @property
    def journal(self) -> Path:
        return self.ledgers[-1][1]


CRYPTO_DIR = STATE_DIR / "crypto"

DESKS: dict[str, Desk] = {d.name: d for d in (
    # Its journal has its own file name: the restore test finds ledgers by name. The data harvest's journal
    # (DEC-0027, wt.crypto.harvest) is listed first so the backup checks and anchors it too; the desk's own
    # journal stays last, which is what `journal` returns.
    Desk("crypto", "C", CRYPTO_DIR, CRYPTO_DIR / "KILL",
         (("crypto-harvest", CRYPTO_DIR / "harvest" / "harvest_journal.jsonl"),
          ("crypto", CRYPTO_DIR / "crypto_journal.jsonl")),
         CRYPTO_DIR / "chain-broken", "always_open", "crypto:", "trading-lab/crypto-snapshot"),
)}


def desk_of_alert(key: str) -> str:
    """The desk an alert key belongs to: the one whose prefix it carries, else `host`."""
    for d in DESKS.values():
        if d.alert_prefix and key.startswith(d.alert_prefix):
            return d.name
    return HOST


def installed(desk: Desk) -> bool:
    """A desk exists on this host once its state directory does."""
    return desk.state_dir.is_dir()
