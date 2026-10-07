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


@dataclass(frozen=True)
class SleeveLimits:
    """Limits of a tournament sleeve (config/risk.yaml, key CT), as shares of the sleeve's own book."""
    risk_pct: Decimal
    max_position_pct: Decimal
    max_exposure_pct: Decimal
    max_positions: int
    max_entries_per_day: int
    max_orders_per_day: int
    daily_loss_latch_pct: Decimal


def load_sleeve_limits(key: str = "CT") -> SleeveLimits:
    c = load_yaml("risk.yaml")[key]
    return SleeveLimits(Decimal(str(c["risk_pct"])), Decimal(str(c["max_position_pct"])),
                        Decimal(str(c["max_exposure_pct"])), int(c["max_positions"]), int(c["max_entries_per_day"]),
                        int(c["max_orders_per_day"]), Decimal(str(c["daily_loss_latch_pct"])))


def sleeve_dir(desk: Desk, sleeve: str) -> Path:
    return desk.state_dir / "sleeves" / sleeve


def size(equity: Decimal, cash: Decimal, price: Decimal, stop: Decimal, fee_pct: float, lim: SleeveLimits) -> Decimal:
    """Quantity for one trade, before lot rounding: the smallest of the risk rule, the position cap and what the
    cash can pay for with its fee. Zero when the stop is not below the price."""
    if price <= 0 or stop >= price:
        return Decimal(0)
    by_risk = equity * lim.risk_pct / 100 / (price - stop)
    by_cap = equity * lim.max_position_pct / 100 / price
    by_cash = cash / (price * (1 + Decimal(str(fee_pct)) / 100))
    return max(Decimal(0), min(by_risk, by_cap, by_cash))


def update_sleeve_latch(book: Book, day: str, equity: Decimal, folder: Path, lim: SleeveLimits) -> bool:
    """Write the sleeve's latch when today's realised loss has reached its share of the book. True when set now."""
    f = folder / "latch"
    limit = equity * lim.daily_loss_latch_pct / 100
    if f.exists() or book.realised.get(day, Decimal(0)) > -limit:
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{day}: realised {book.realised[day]} reached the daily loss limit {limit:.2f}\n")
    return True


def sleeve_blockers(pair: str, notional: Decimal, equity: Decimal, book: Book, day: str, desk: Desk, folder: Path,
                    pairs: tuple[str, ...], lim: SleeveLimits) -> list[str]:
    """Codes of everything that forbids this sleeve's entry; empty = allowed. The desk's kill switch, chain flag
    and shadow role apply to every sleeve; the latch and the counts are the sleeve's own."""
    why = []
    if desk.kill_file.exists():
        why.append("kill")
    if (folder / "latch").exists():
        why.append("latch")
    if desk.chain_flag.exists():
        why.append("chain_broken")
    if os.environ.get("WT_ROLE", "primary") == "shadow":
        why.append("shadow_role")
    if pair not in pairs:
        why.append("not_allowlisted")
    if pair in book.positions:
        why.append("already_open")
    if len(book.positions) >= lim.max_positions:
        why.append("positions")
    if book.exposure() + notional > equity * lim.max_exposure_pct / 100:
        why.append("exposure")
    if book.entries.get(day, 0) >= lim.max_entries_per_day:
        why.append("entries_today")
    if book.orders.get(day, 0) >= lim.max_orders_per_day:
        why.append("orders_today")
    return why


def latch_file(desk: Desk) -> Path:
    return desk.state_dir / "latch"


def learning_file(desk: Desk) -> Path:
    """The owner's switch for everything that learns (DEC-0016, 6). While it exists no model acts, no challenger
    is drawn or admitted, and live challengers open nothing. The baseline and the registered sleeves trade on."""
    return desk.state_dir / "LEARNING_OFF"


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
