"""The paper book: cash, open positions and realised P&L in the quote currency, in Decimal.

Kraken has no spot sandbox, so this simulator is what "paper" means on the crypto desk (ADR 0005). A buy fills at
the ask plus slippage, a sell at the given price less slippage; both pay the taker fee. Sizes are rounded down to
the pair's lot precision and refused below the venue's minimum order.

`outbox` holds journal records that have been decided but may not be in the journal yet. The book is saved first
(atomically), then the outbox is appended to the journal and cleared: a crash between the two is repaired on the
next start, and a record is never written twice (wt.crypto.cycle.flush).
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from wt.crypto.data import PairInfo

BPS = Decimal(10_000)
CENT = Decimal("0.01")


class Rejected(Exception):
    """The simulated venue would not take the order; args[0] is a code."""


@dataclass
class Position:
    pair: str
    qty: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    entry_t: int                # when it filled, epoch seconds
    entry_bar: int              # open time of the signal bar
    stop: Decimal
    target: Decimal
    checked_to: int             # 1-minute bars up to and including this open time have been examined for an exit

    @property
    def cost(self) -> Decimal:
        return self.qty * self.entry_price

    @property
    def risk(self) -> Decimal:  # the loss at the stop, before fees: the R unit
        return self.qty * (self.entry_price - self.stop)


def _dec(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


class Book:
    def __init__(self, cash: Decimal, start_equity: Decimal) -> None:
        self.cash, self.start_equity = cash, start_equity
        self.positions: dict[str, Position] = {}
        self.realised: dict[str, Decimal] = {}          # UTC day -> realised P&L after fees
        self.entries: dict[str, int] = {}               # UTC day -> entries
        self.orders: dict[str, int] = {}                # UTC day -> orders (buys and sells)
        self.outbox: list[dict[str, Any]] = []
        # Cycle bookkeeping kept in the same file, so one atomic write covers it: the last bar evaluated and the
        # last bar stored per pair, and the count of cycles in a row with no data.
        self.meta: dict[str, Any] = {"last_bar": {}, "stored_to": {}, "misses": 0}

    # ---- persistence -----------------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path, start_equity: Decimal) -> Book:
        if not path.exists():
            return cls(start_equity, start_equity)
        s = json.loads(path.read_text())
        b = cls(Decimal(s["cash"]), Decimal(s["start_equity"]))
        for k, p in s["positions"].items():
            b.positions[k] = Position(p["pair"], Decimal(p["qty"]), Decimal(p["entry_price"]), Decimal(p["entry_fee"]),
                                      int(p["entry_t"]), int(p["entry_bar"]), Decimal(p["stop"]), Decimal(p["target"]),
                                      int(p["checked_to"]))
        b.realised = {k: Decimal(v) for k, v in s["realised"].items()}
        b.entries, b.orders, b.outbox = dict(s["entries"]), dict(s["orders"]), list(s["outbox"])
        b.meta = {**b.meta, **s.get("meta", {})}
        return b

    def save(self, path: Path) -> None:
        body = {"cash": str(self.cash), "start_equity": str(self.start_equity),
                "positions": {k: {f: (str(v) if isinstance(v, Decimal) else v) for f, v in asdict(p).items()}
                              for k, p in self.positions.items()},
                "realised": {k: str(v) for k, v in self.realised.items()},
                "entries": self.entries, "orders": self.orders, "outbox": self.outbox, "meta": self.meta}
        write_atomic(path, json.dumps(body, indent=1, sort_keys=True))

    # ---- orders ----------------------------------------------------------------------------------
    def buy(self, pair: str, notional: Decimal, ask: float, info: PairInfo, fee_pct: float, slip_bps: float,
            t: int, bar_t: int, stop_pct: float, target_pct: float) -> Position:
        if pair in self.positions:
            raise Rejected("already_open")
        price = (_dec(ask) * (1 + _dec(slip_bps) / BPS)).quantize(info.tick)
        qty = (notional / price).quantize(Decimal(1).scaleb(-info.lot_decimals), rounding=ROUND_DOWN)
        cost = qty * price
        if qty < info.order_min or cost < info.cost_min:
            raise Rejected("below_minimum")
        fee = cost * _dec(fee_pct) / 100
        if cost + fee > self.cash:
            raise Rejected("insufficient_cash")
        self.cash -= cost + fee
        p = Position(pair, qty, price, fee, t, bar_t,
                     (price * (1 - _dec(stop_pct) / 100)).quantize(info.tick),
                     (price * (1 + _dec(target_pct) / 100)).quantize(info.tick), t - t % 60)
        self.positions[pair] = p
        day = utc_day(t)
        self.entries[day] = self.entries.get(day, 0) + 1
        self.orders[day] = self.orders.get(day, 0) + 1
        return p

    def sell(self, pair: str, price: float, fee_pct: float, slip_bps: float, t: int) -> dict[str, Any]:
        """Close the whole position. Returns what was realised (after both fees) and its R multiple."""
        p = self.positions.pop(pair)
        px = _dec(price) * (1 - _dec(slip_bps) / BPS)
        proceeds = p.qty * px
        fee = proceeds * _dec(fee_pct) / 100
        self.cash += proceeds - fee
        pnl = proceeds - fee - p.cost - p.entry_fee
        day = utc_day(t)
        self.realised[day] = self.realised.get(day, Decimal(0)) + pnl
        self.orders[day] = self.orders.get(day, 0) + 1
        return {"exit_price": str(px.quantize(Decimal("0.00000001"))), "fees": str((fee + p.entry_fee).quantize(CENT)),
                "pnl": str(pnl.quantize(CENT)), "r": round(float(pnl / p.risk), 3) if p.risk > 0 else None,
                "held_s": t - p.entry_t}

    # ---- views -----------------------------------------------------------------------------------
    def exposure(self) -> Decimal:
        return sum((p.cost for p in self.positions.values()), Decimal(0))

    def equity(self, marks: dict[str, float]) -> Decimal:
        """Cash plus positions at `marks` (bid), or at entry where a pair has no mark."""
        return self.cash + sum((p.qty * (_dec(marks[k]) if k in marks else p.entry_price)
                                for k, p in self.positions.items()), Decimal(0))

    def note(self, rec: dict[str, Any]) -> None:
        self.outbox.append({"id": uuid.uuid4().hex, **rec})


def utc_day(t: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.UTC).date().isoformat()


def write_atomic(path: Path, text: str) -> None:
    """Temp file, fsync, rename, fsync the directory: after a crash the file is the old one or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
