"""The crypto desk's entry guard (config/risk.yaml, key C). It only ever blocks entries: an exit is never refused.

The daily-loss latch is a file. Once a UTC day's realised loss reaches the limit it is written, and entries stay
off until the owner removes it, on any later day too.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from wt.core.config import load_yaml
from wt.core.desk import Desk
from wt.crypto.book import Book


@dataclass(frozen=True)
class Limits:
    allowlist: tuple[str, ...]
    max_notional: Decimal
    max_open_exposure: Decimal
    max_entries_per_day: int
    max_orders_per_day: int
    daily_loss_latch: Decimal


def load_limits(strategy: str = "C") -> Limits:
    c = load_yaml("risk.yaml")[strategy]
    return Limits(tuple(c["allowlist"]), Decimal(str(c["max_notional"])), Decimal(str(c["max_open_exposure"])),
                  int(c["max_entries_per_day"]), int(c["max_orders_per_day"]), Decimal(str(c["daily_loss_latch"])))


def latch_file(desk: Desk) -> Path:
    return desk.state_dir / "latch"


def update_latch(book: Book, day: str, desk: Desk, limits: Limits) -> bool:
    """Write the latch when today's realised loss has reached the limit. True when this call set it."""
    f = latch_file(desk)
    if f.exists() or book.realised.get(day, Decimal(0)) > -limits.daily_loss_latch:
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{day}: realised {book.realised[day]} reached the daily loss limit {limits.daily_loss_latch}\n")
    return True


def entry_blockers(pair: str, notional: Decimal, book: Book, day: str, desk: Desk, limits: Limits) -> list[str]:
    """Codes of everything that forbids this entry; empty = allowed."""
    why = []
    if desk.kill_file.exists():
        why.append("kill")
    if latch_file(desk).exists():
        why.append("latch")
    if desk.chain_flag.exists():
        why.append("chain_broken")
    if os.environ.get("WT_ROLE", "primary") == "shadow":
        why.append("shadow_role")
    if pair not in limits.allowlist:
        why.append("not_allowlisted")
    if pair in book.positions:
        why.append("already_open")
    if notional > limits.max_notional:
        why.append("notional")
    if book.exposure() + notional > limits.max_open_exposure:
        why.append("exposure")
    if book.entries.get(day, 0) >= limits.max_entries_per_day:
        why.append("entries_today")
    if book.orders.get(day, 0) >= limits.max_orders_per_day:
        why.append("orders_today")
    return why
