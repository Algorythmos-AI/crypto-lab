"""The tournament sleeves (DEC-0014, DEC-0015): one registered strategy and one paper book each, run by the bar
cycle after the baseline (`wt.crypto.cycle`) and never able to disturb it.

What a sleeve shares with the desk: the kill switch, the chain flag, the job and the one journal (each row names
its `sleeve`). What it owns: `var/crypto/sleeves/<name>/book.json`, its latch, its counts and its limits.

Rules it keeps:
  * No data, no action, and a budget. A pair whose data cannot be read is skipped. Fetching stops at the cycle's
    call and time budget: what is left is skipped and said so, and the books are always saved.
  * Exits before entries. Stops and targets are resolved on 1-minute bars; a trailed stop, a trend exit and a
    time stop are decided on a closed strategy bar only.
  * A strategy bar is evaluated once per sleeve and pair, and acted on only while it is fresh: the desk never
    enters on a signal it found late (after an outage, or on its first run).
  * One failing pair or sleeve never stops the others.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any

from wt.core.desk import Desk
from wt.crypto import risk, rules
from wt.crypto.book import BPS, Book, Position, Rejected, to_lot, to_tick, utc_day, write_atomic
from wt.crypto.data import Bar, DataError, KrakenPublic, PairInfo, Quote
from wt.crypto.quality import assess
from wt.crypto.strategy import find_exit
from wt.ops.alerts import Alerts

MAX_CALLS = 45                  # public calls in one cycle, the baseline's included
MAX_SECONDS = 200.0             # no new call after this long; the job is killed at 300 s
FRESH_S = 20 * 60               # a signal bar is acted on within this long of its close
NO_TARGET = Decimal("Infinity")
STAGE = "incubation"            # DEC-0014: until the strategy passes gate C1


class Budget:
    """The cycle's allowance of calls and time. `spend()` is called before every call."""

    def __init__(self, api: KrakenPublic, started: float, max_calls: int | None = None,
                 max_seconds: float | None = None) -> None:
        self.api, self.started = api, started
        self.max_calls = MAX_CALLS if max_calls is None else max_calls
        self.max_seconds = MAX_SECONDS if max_seconds is None else max_seconds

    def spend(self) -> None:
        if self.api.calls >= self.max_calls or time.monotonic() - self.started >= self.max_seconds:
            raise DataError("budget")


def sleeve_hash(cfg: dict[str, Any], name: str) -> str:
    """What the sleeve's frozen rules were when a record was made."""
    frozen = {"common": cfg["sleeves"]["common"], "rule": cfg["sleeves"][name], "costs": cfg["costs"],
              "quality": cfg["quality"]}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()[:12]


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")


def _append(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def _read_bars(path: Path) -> list[Bar]:
    """Stored bars, oldest first, one per open time (a line repeated after a crash is dropped)."""
    seen: dict[int, Bar] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
                seen[int(r["t"])] = Bar(int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]),
                                        float(r["vwap"]), float(r["v"]), int(r["n"]))
            except (ValueError, KeyError, TypeError):
                continue
    return [seen[t] for t in sorted(seen)]


def series(api: KrakenPublic, desk: Desk, state: dict[str, Any], kraken_pair: str, tf: int, keep: int, now: float,
           budget: Budget) -> list[Bar]:
    """The last `keep` closed bars of a timeframe, from the desk's own store, fetched only when a new one is due.

    Stored per pair and timeframe; the "stored up to" mark is keyed by both. Raises DataError when the bars are
    too few or have a hole: an indicator over a gap is not the indicator.
    """
    path = desk.state_dir / "bars" / f"{kraken_pair}-{tf}m.jsonl"
    step, key = tf * 60, f"{kraken_pair}|{tf}"
    have = _read_bars(path)
    if not have or now >= have[-1].t + 2 * step:            # the bar after the newest stored one has closed
        budget.spend()
        done = max(int(state["stored_to"].get(key, 0)), have[-1].t if have else 0)
        fresh = [b for b in api.ohlc(kraken_pair, tf) if b.t > done]
        for b in fresh:
            _append(path, {"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "vwap": b.vwap, "v": b.v, "n": b.n})
        if fresh:
            state["stored_to"][key] = fresh[-1].t
            have += fresh
    out = have[-keep:]
    if len(out) < keep or any(b.t - a.t != step for a, b in zip(out, out[1:], strict=False)):
        raise DataError(f"{tf}m bars: too few or not contiguous")
    if now - (out[-1].t + step) > 2 * step:
        raise DataError(f"{tf}m bars: stale")
    return out


def pair_infos(api: KrakenPublic, state: dict[str, Any], pairs: list[str], day: str, budget: Budget) -> dict[str, PairInfo]:
    """Lot, tick and minimums for every pair: one call a day, kept in the sleeves' state file. When the
    call fails, yesterday's answer is used; a pair with none has its entries refused."""
    if state.get("info_day") != day:
        try:
            budget.spend()
            got = api.pair_infos(pairs)
            state["info"] = {k: [v.lot_decimals, str(v.tick), str(v.order_min), str(v.cost_min)] for k, v in got.items()}
            state["info_day"] = day
        except DataError:
            pass
    return {k: PairInfo(int(v[0]), Decimal(v[1]), Decimal(v[2]), Decimal(v[3])) for k, v in (state.get("info") or {}).items()}


def _sell(book: Book, name: str, sleeve: str, pos: Position, price: float, slip: float, fee: float, t: int,
          reason: str, extra: dict[str, Any]) -> None:
    fill = book.sell(name, price, fee, slip, t)
    book.note({"kind": "exit", "t": _iso(t), "pair": name, "reason": reason, "qty": str(pos.qty),
               "entry_price": str(pos.entry_price), "entry_t": _iso(pos.entry_t), "entry_bar": pos.entry_bar,
               "stop": str(pos.stop), **extra, **fill})


def manage(book: Book, sleeve: str, name: str, bars: list[Bar], minutes: list[Bar], quote: Quote, now: float,
           c: rules.Common, p: dict[str, Any], costs: dict[str, Any], info: PairInfo | None, extra: dict[str, Any],
           alerts: Alerts) -> None:
    """Exits for one open position: stop or target on the 1-minute path first, then whatever a newly closed
    strategy bar decides. Never blocked by the kill switch, a latch or a limit."""
    pos = book.positions[name]
    fee, slip = float(costs["taker_fee_pct"]), float(costs["slippage_bps"])
    mins = [m for m in minutes if m.t > pos.checked_to]
    if mins and mins[0].t - pos.checked_to > 120:
        book.note({"kind": "exit_gap", "t": _iso(now), "pair": name, "from": pos.checked_to, "to": mins[0].t, **extra})
        alerts.fire(f"crypto:exit-gap:{sleeve}:{name}", "Crypto: an open position has an unobserved gap",
                    f"{sleeve} {name}: 1-minute data is missing between the last check and now.", 4)
    x = find_exit(mins, float(pos.stop), float(pos.target))
    if x is not None:
        _sell(book, name, sleeve, pos, x.price, slip if x.reason == "stop" else 0.0, fee, x.t + 60, x.reason, extra)
        return
    if mins:
        pos.checked_to = mins[-1].t
    last = bars[-1]
    if last.t <= pos.bar_checked:
        return
    reason, stop, high = rules.bar_exit(sleeve, bars, pos.entry_bar, float(pos.stop), float(pos.atr), float(pos.high), c, p)
    pos.bar_checked, pos.high = last.t, max(pos.high, Decimal(str(high)))
    new_stop = Decimal(str(stop))
    if info is not None:
        new_stop = to_tick(new_stop, info.tick, ROUND_DOWN)
    pos.stop = max(pos.stop, new_stop)
    if reason is not None:
        _sell(book, name, sleeve, pos, quote.bid, slip, fee, int(now), reason, extra)


def enter(book: Book, sleeve: str, name: str, last: Bar, atr: float, quote: Quote, info: PairInfo | None,
          blockers: list[str], now: float, day: str, desk: Desk, folder: Path, pairs: tuple[str, ...],
          equity: Decimal, c: rules.Common, p: dict[str, Any], costs: dict[str, Any], lim: risk.SleeveLimits,
          extra: dict[str, Any]) -> list[str]:
    """Try the entry for a signal bar. Returns the codes that refused it; empty when the position was opened."""
    if info is None:
        return [*blockers, "no_pair_info"]
    fee = float(costs["taker_fee_pct"])
    price = to_tick(Decimal(str(quote.ask)) * (1 + Decimal(str(costs["slippage_bps"])) / BPS), info.tick, ROUND_UP)
    stop_f, target_f, skip = rules.levels(float(price), atr, c, p)
    if skip is not None:
        return [*blockers, skip]
    stop = to_tick(Decimal(str(stop_f)), info.tick, ROUND_DOWN)
    target = NO_TARGET if target_f is None else to_tick(Decimal(str(target_f)), info.tick, ROUND_DOWN)
    qty = to_lot(risk.size(equity, book.cash, price, stop, fee, lim), info.lot_decimals)
    why = blockers + risk.sleeve_blockers(name, qty * price, equity, book, day, desk, folder, pairs, lim)
    if why:
        return why
    try:
        pos = book.buy_qty(name, qty, price, info, fee, int(now), last.t, stop, target, sleeve, Decimal(str(atr)))
    except Rejected as e:
        return [str(e.args[0])[:40]]
    book.note({"kind": "entry", "t": _iso(now), "pair": name, "bar": last.t, "qty": str(pos.qty),
               "price": str(pos.entry_price), "stop": str(pos.stop),
               "target": None if target == NO_TARGET else str(pos.target), "fee": str(pos.entry_fee),
               "atr": round(atr, 8), "risk": str(pos.risk.quantize(Decimal("0.01"))), **extra})
    return []


def run_all(now: float, api: KrakenPublic, desk: Desk, cfg: dict[str, Any], alerts: Alerts, started: float,
            flush: Any) -> dict[str, Any]:
    """One cycle of every sleeve. `flush(book, book_path, journal)` is the cycle's own journal writer.
    Returns a summary for the cycle's log line."""
    sc = cfg["sleeves"]
    common, costs, qcfg = sc["common"], cfg["costs"], cfg["quality"]
    c = rules.Common.of(common)
    names = [n for n in rules.NAMES if n in sc]
    pairs: dict[str, str] = dict(common["pairs"])
    lim = risk.load_sleeve_limits(str(common["limits"]))
    day, budget = utc_day(now), Budget(api, started)
    root = desk.state_dir / "sleeves"
    state_path = root / "data.json"
    state: dict[str, Any] = {"stored_to": {}, "info_day": "", "info": {}}
    if state_path.exists():
        try:
            state = {**state, **json.loads(state_path.read_text())}
        except ValueError:
            pass
    books = {n: Book.load(risk.sleeve_dir(desk, n) / "book.json", Decimal(str(common["start_equity"]))) for n in names}
    for n in names:
        flush(books[n], risk.sleeve_dir(desk, n) / "book.json", desk.journal)        # repair, as the baseline does
    extra = {n: {"sleeve": n, "strategy": str(sc[n]["hypothesis"]), "tf": c.timeframe_min, "stage": STAGE,
                 "config": sleeve_hash(cfg, n)} for n in names}
    infos = pair_infos(api, state, list(pairs.values()), day, budget)
    failed: dict[str, str] = {}
    marks: dict[str, float] = {}
    seen: dict[str, dict[str, Any]] = {n: {} for n in names}
    touched: set[str] = set()

    try:
        for name, kraken_pair in pairs.items():
            try:
                bars = series(api, desk, state, kraken_pair, c.timeframe_min, c.bars, now, budget)
                daily = series(api, desk, state, kraken_pair, c.daily_min, c.daily_bars, now, budget)
                holders = [n for n in names if name in books[n].positions]
                quote: Quote | None = None
                if holders:
                    budget.spend()
                    quote = api.ticker(kraken_pair)
                    marks[name] = quote.bid
                    budget.spend()
                    minutes = api.ohlc(kraken_pair, 1, since=min(books[n].positions[name].checked_to for n in holders))
                    for n in holders:
                        try:
                            manage(books[n], n, name, bars, minutes, quote, now, c, sc[n], costs,
                                   infos.get(kraken_pair), extra[n], alerts)
                            touched.add(n)
                        except Exception as e:  # noqa: BLE001 — one position's fault must not stop the others
                            failed[f"{n}:{name}"] = e.__class__.__name__
                last = bars[-1]
                fresh = 0 <= now - (last.t + c.timeframe_min * 60) <= FRESH_S
                for n in names:
                    book = books[n]
                    if book.meta["last_bar"].get(name) == last.t:
                        continue                            # this bar was evaluated by an earlier run
                    try:
                        fire, why, atr = rules.entry(n, bars, daily, c, sc[n])
                        row: dict[str, Any] = {"bar": last.t, "fire": fire, "why": list(why)}
                        _append(desk.state_dir / "observations" / f"sleeve-{utc_day(last.t)}.jsonl",
                                {"t": last.t, "pair": name, "sleeve": n, "tf": c.timeframe_min, "close": last.c,
                                 "would_fire": fire, "why_not": list(why),
                                 "atr_pct": round(atr / last.c * 100, 4) if atr and last.c > 0 else None,
                                 "config": extra[n]["config"]})
                        if fire and atr is not None:
                            if not fresh:
                                refused = ["late_bar"]
                            else:
                                if quote is None:
                                    budget.spend()
                                    quote = api.ticker(kraken_pair)
                                    marks[name] = quote.bid
                                q = assess(bars, quote, now, c.timeframe_min, qcfg)
                                refused = enter(book, n, name, last, atr, quote, infos.get(kraken_pair),
                                                list(q.reasons), now, day, desk, risk.sleeve_dir(desk, n),
                                                tuple(pairs), book.equity(marks), c, sc[n], costs, lim, extra[n])
                            if refused:
                                book.note({"kind": "refused", "t": _iso(now), "pair": name, "bar": last.t,
                                           "why": refused, **extra[n]})
                                row["refused"] = refused
                            else:
                                row["entered"] = True
                        seen[n][name] = row
                        book.meta["last_bar"][name] = last.t
                        touched.add(n)
                    except DataError:
                        raise
                    except Exception as e:  # noqa: BLE001 — as above
                        failed[f"{n}:{name}"] = e.__class__.__name__
            except DataError as e:
                failed[name] = str(e)[:80]
                if str(e) == "budget":
                    break
            except Exception as e:  # noqa: BLE001 — a pair's fault must not stop the other pairs
                failed[name] = e.__class__.__name__
    finally:
        for n in names:
            book, folder = books[n], risk.sleeve_dir(desk, n)
            equity = book.equity(marks)
            if n in touched and risk.update_sleeve_latch(book, day, equity, folder, lim):
                book.note({"kind": "latch", "t": _iso(now), "day": day, **extra[n]})
                alerts.fire(f"crypto:latch:{n}", f"Crypto: sleeve {n} reached its daily loss limit, entries off",
                            "Entries stay off for this sleeve until the owner resets the latch. Exits are managed.", 4)
            if seen[n] or book.outbox:
                book.note({"kind": "sleeve", "t": _iso(now), "pairs": seen[n], "open": sorted(book.positions),
                           "equity": str(equity.quantize(Decimal("0.01"))), "kill": desk.kill_file.exists(),
                           **extra[n]})
            book.save(folder / "book.json")
            flush(book, folder / "book.json", desk.journal)
        write_atomic(state_path, json.dumps(state, sort_keys=True))
    return {"evaluated": {n: sorted(seen[n]) for n in names if seen[n]}, "failed": failed,
            "open": {n: sorted(books[n].positions) for n in names if books[n].positions}}
