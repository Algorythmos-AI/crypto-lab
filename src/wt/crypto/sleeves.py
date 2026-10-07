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
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any

from wt.core.desk import Desk
from wt.crypto import risk, rules, signals
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
               "stop": str(pos.stop), "risk0": str(pos.unit.quantize(Decimal("0.01"))), **extra, **fill})


def manage(book: Book, sleeve: str, name: str, bars: list[Bar], minutes: list[Bar], quote: Quote, now: float,
           c: rules.Common, p: dict[str, Any], costs: dict[str, Any], info: PairInfo | None, extra: dict[str, Any],
           alerts: Alerts, step_s: int = 60) -> None:
    """Exits for one open position: stop or target on the fine-bar path first, then whatever a newly closed
    strategy bar decides. Never blocked by the kill switch, a latch or a limit. `step_s` is the length of the
    fine bars: 1-minute on the desk, hourly in the backtest."""
    pos = book.positions[name]
    fee, slip = float(costs["taker_fee_pct"]), float(costs["slippage_bps"])
    mins = [m for m in minutes if m.t > pos.checked_to]
    if mins and mins[0].t - pos.checked_to > 2 * step_s:
        book.note({"kind": "exit_gap", "t": _iso(now), "pair": name, "from": pos.checked_to, "to": mins[0].t, **extra})
        alerts.fire(f"crypto:exit-gap:{sleeve}:{name}", "Crypto: an open position has an unobserved gap",
                    f"{sleeve} {name}: fine-bar data is missing between the last check and now.", 4)
    x = find_exit(mins, float(pos.stop), float(pos.target))
    if x is not None:
        _sell(book, name, sleeve, pos, x.price, slip if x.reason == "stop" else 0.0, fee, x.t + step_s, x.reason, extra)
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


@dataclass
class Run:
    """Everything one cycle of the sleeves works with. The desk builds it from the venue and its files; the
    backtest builds it from history. Both then call `step_pair` and `finish`, so they cannot drift apart."""
    now: float
    desk: Desk
    specs: dict[str, rules.Spec]        # every sleeve this cycle runs, by name, in order
    costs: dict[str, Any]
    qcfg: dict[str, Any]
    lim: risk.SleeveLimits
    pairs: tuple[str, ...]
    books: dict[str, Book]
    infos: dict[str, PairInfo]
    extra: dict[str, dict[str, Any]]
    alerts: Alerts
    step_s: int = 60                    # length of the fine bars exits are resolved on
    marks: dict[str, float] = field(default_factory=dict)
    seen: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    touched: set[str] = field(default_factory=set)
    market: dict[str, float | None] = field(default_factory=dict)     # the wider market on this bar (signals.market)
    signalled: list[tuple[str, dict[str, Any]]] = field(default_factory=list)   # (sleeve, signal row) awaiting breadth
    # The entry rule, when it is not the registered one: the backtest's random-entry control sets it.
    entry: Callable[..., tuple[bool, tuple[str, ...], float | None]] | None = None
    off: dict[str, str] = field(default_factory=dict)     # sleeves that may not open a trade, with the reason code

    @property
    def day(self) -> str:
        return utc_day(self.now)

    @property
    def names(self) -> list[str]:
        return list(self.specs)


def step_pair(run: Run, name: str, kraken_pair: str, bars_of: Callable[[int], list[Bar]], daily: list[Bar],
              quote_of: Callable[[], Quote], minutes_of: Callable[[int], list[Bar]],
              observe: Callable[[dict[str, Any]], None] | None = None) -> None:
    """One pair, every sleeve: exits for those that hold it, then each sleeve's newest closed bar as a possible
    entry. `bars_of(timeframe_min)` gives the closed bars of a sleeve's own bar length; `quote_of` and
    `minutes_of(since)` are asked only when needed. A DataError for the whole pair is the caller's; a sleeve
    whose own bars cannot be had is skipped and named."""
    now = run.now
    cache: dict[int, list[Bar]] = {}

    def bars_for(spec: rules.Spec) -> list[Bar]:
        tf = spec.c.timeframe_min
        if tf not in cache:
            cache[tf] = bars_of(tf)
        got = cache[tf][-spec.c.bars:]
        if len(got) < spec.c.bars:
            raise DataError(f"{tf}m bars: too few")
        return got

    holders = [n for n in run.names if name in run.books[n].positions]
    quote: Quote | None = None
    if holders:
        quote = quote_of()
        run.marks[name] = quote.bid
        minutes = minutes_of(min(run.books[n].positions[name].checked_to for n in holders))
        for n in holders:
            try:
                spec = run.specs[n]
                manage(run.books[n], spec.base, name, bars_for(spec), minutes, quote, now, spec.c, spec.p, run.costs,
                       run.infos.get(kraken_pair), run.extra[n], run.alerts, run.step_s)
                run.touched.add(n)
            except DataError as e:
                if str(e) == "budget":
                    raise
                run.failed[f"{n}:{name}"] = str(e)[:60]
            except Exception as e:  # noqa: BLE001 — one position's fault must not stop the others
                run.failed[f"{n}:{name}"] = e.__class__.__name__
    for n in run.names:
        book, spec = run.books[n], run.specs[n]
        c = spec.c
        try:
            bars = bars_for(spec)
            last = bars[-1]
            if book.meta["last_bar"].get(name) == last.t:
                continue                        # this bar was evaluated by an earlier run
            fresh = 0 <= now - (last.t + c.timeframe_min * 60) <= FRESH_S
            held = any(name in run.books[m].positions for m in run.names if m != n)
            fire, why, atr = rules.evaluate(spec, bars, daily, run.market, held, run.entry)
            row: dict[str, Any] = {"bar": last.t, "fire": fire, "why": list(why)}
            if observe is not None:
                observe({"t": last.t, "pair": name, "sleeve": n, "tf": c.timeframe_min, "close": last.c,
                         "would_fire": fire, "why_not": list(why),
                         "atr_pct": round(atr / last.c * 100, 4) if atr and last.c > 0 else None,
                         "config": run.extra[n]["config"]})
            if fire and atr is not None:
                if not fresh:
                    refused = ["late_bar"]
                elif n in run.off:
                    refused = [run.off[n]]      # a retired challenger, or the owner's learning switch: exits only
                else:
                    if quote is None:
                        quote = quote_of()
                        run.marks[name] = quote.bid
                    q = assess(bars, quote, now, c.timeframe_min, run.qcfg)
                    refused = enter(book, n, name, last, atr, quote, run.infos.get(kraken_pair), list(q.reasons),
                                    now, run.day, run.desk, risk.sleeve_dir(run.desk, n), run.pairs,
                                    book.equity(run.marks), c, spec.p, run.costs, run.lim, run.extra[n])
                if refused:
                    book.note({"kind": "refused", "t": _iso(now), "pair": name, "bar": last.t, "why": refused,
                               **run.extra[n]})
                    row["refused"] = refused
                else:
                    row["entered"] = True
                    if run.step_s != 60:
                        # Coarser fine bars: the one that opens at the signal bar's close is the first to examine.
                        book.positions[name].checked_to = int(now) - run.step_s
                run.signalled.append((n, _signal(run, n, name, last, bars, atr, quote, refused)))
            run.seen.setdefault(n, {})[name] = row
            book.meta["last_bar"][name] = last.t
            run.touched.add(n)
        except DataError as e:
            if str(e) == "budget" or not cache:
                raise                           # out of budget, or the pair has no bars at all: the caller's
            run.failed[f"{n}:{name}"] = str(e)[:60]
        except Exception as e:  # noqa: BLE001 — as above
            run.failed[f"{n}:{name}"] = e.__class__.__name__


def _signal(run: Run, sleeve: str, name: str, last: Bar, bars: list[Bar], atr: float, quote: Quote | None,
            refused: list[str]) -> dict[str, Any]:
    """The record of one signal (DEC-0016, 2): its inputs on the signal bar and the levels a trade at this moment
    has, whether or not it was bought. A bought signal carries the position's own levels."""
    pos = run.books[sleeve].positions.get(name) if not refused else None
    if pos is not None:
        price, stop = float(pos.entry_price), float(pos.stop)
        target = float(pos.target) if pos.target.is_finite() else None
    else:
        price = (quote.ask if quote is not None else last.c) * (1 + float(run.costs["slippage_bps"]) / 10_000)
        stop, target, _ = rules.levels(price, atr, run.specs[sleeve].c, run.specs[sleeve].p)
    held = any(name in run.books[m].positions for m in run.names if m != sleeve)
    return {"kind": "signal", "sid": f"{sleeve}|{name}|{last.t}", "t": _iso(run.now), "pair": name, "bar": last.t,
            "taken": not refused, "why": list(refused), "price": round(price, 8), "stop": round(stop, 8),
            "target": None if target is None else round(target, 8), "atr": round(atr, 8),
            "inputs": signals.inputs(bars, price, stop, atr, None if quote is None else quote.spread_pct,
                                     run.market, held, run.specs[sleeve].c.timeframe_min),
            **run.extra[sleeve]}


def finish(run: Run, flush: Callable[[Book, Path, Path], Any], save: bool = True) -> None:
    """End of a cycle for every sleeve: the signals' rows, the loss latch, the cycle's own row, the book saved,
    the outbox written."""
    for n, rec in run.signalled:
        # How many pairs signalled for this sleeve on the same bar: known only now that all have been looked at.
        rec["inputs"]["breadth"] = float(sum(1 for v in run.seen.get(n, {}).values()
                                             if v.get("fire") and v.get("bar") == rec["bar"]))
        run.books[n].note(rec)
    run.signalled = []
    for n in run.names:
        book, folder = run.books[n], risk.sleeve_dir(run.desk, n)
        equity = book.equity(run.marks)
        if n in run.touched and risk.update_sleeve_latch(book, run.day, equity, folder, run.lim):
            book.note({"kind": "latch", "t": _iso(run.now), "day": run.day, **run.extra[n]})
            run.alerts.fire(f"crypto:latch:{n}", f"Crypto: sleeve {n} reached its daily loss limit, entries off",
                            "Entries stay off for this sleeve until the owner resets the latch. Exits are managed.", 4)
        if run.seen.get(n) or book.outbox:
            book.note({"kind": "sleeve", "t": _iso(run.now), "pairs": run.seen.get(n, {}),
                       "open": sorted(book.positions), "equity": str(equity.quantize(Decimal("0.01"))),
                       "kill": run.desk.kill_file.exists(), **run.extra[n]})
        if save:
            book.save(folder / "book.json")
        flush(book, folder / "book.json", run.desk.journal)


def extras(cfg: dict[str, Any], specs: dict[str, rules.Spec]) -> dict[str, dict[str, Any]]:
    """What every journal row of a sleeve carries. A registered sleeve's `config` is the hash of its frozen
    rules; a challenger's name is already the hash of its dials."""
    return {n: {"sleeve": n, "strategy": s.hypothesis, "tf": s.c.timeframe_min, "stage": STAGE,
                "config": sleeve_hash(cfg, n) if n in cfg["sleeves"] else n} for n, s in specs.items()}


def run_all(now: float, api: KrakenPublic, desk: Desk, cfg: dict[str, Any], alerts: Alerts, started: float,
            flush: Any, specs: dict[str, rules.Spec] | None = None, off: dict[str, str] | None = None) -> dict[str, Any]:
    """One cycle of every sleeve. `flush(book, book_path, journal)` is the cycle's own journal writer. `specs` are
    the sleeves to run (the registered three when not given); `off` names those among them that may not open a
    trade. Returns a summary for the cycle's log line."""
    common = cfg["sleeves"]["common"]
    specs = specs or rules.registered(cfg)
    names = list(specs)
    base = rules.Common.of(common)
    pairs: dict[str, str] = dict(common["pairs"])
    budget = Budget(api, started)
    state_path = desk.state_dir / "sleeves" / "data.json"
    state: dict[str, Any] = {"stored_to": {}, "info_day": "", "info": {}, "marks": {}}
    if state_path.exists():
        try:
            state = {**state, **json.loads(state_path.read_text())}
        except ValueError:
            pass
    books = {n: Book.load(risk.sleeve_dir(desk, n) / "book.json", Decimal(str(common["start_equity"]))) for n in names}
    for n in names:
        flush(books[n], risk.sleeve_dir(desk, n) / "book.json", desk.journal)        # repair, as the baseline does
    run = Run(now, desk, specs, cfg["costs"], cfg["quality"], risk.load_sleeve_limits(str(common["limits"])),
              tuple(pairs), books, pair_infos(api, state, list(pairs.values()), utc_day(now), budget),
              extras(cfg, specs), alerts, off=dict(off or {}))
    # How many closed bars of each length the sleeves need. Daily bars also feed the dip rule's filter and the
    # market inputs, whatever the sleeves' own bar lengths are.
    keep: dict[int, int] = {base.daily_min: base.daily_bars}
    for s in specs.values():
        keep[s.c.timeframe_min] = max(keep.get(s.c.timeframe_min, 0), s.c.bars)

    def observe(rec: dict[str, Any]) -> None:
        _append(desk.state_dir / "observations" / f"sleeve-{utc_day(rec['t'])}.jsonl", rec)

    try:
        # The wider market for the signals' inputs: Bitcoin's daily bars, already in the store on most cycles.
        if "BTC/USD" in pairs:
            btc = series(api, desk, state, pairs["BTC/USD"], base.daily_min, keep[base.daily_min], now, budget)
            run.market = signals.market(btc[-base.daily_bars:])
    except Exception:  # noqa: BLE001 — a signal without its market inputs is still a signal
        run.market = {}
    try:
        for name, kraken_pair in pairs.items():
            def quote_of(kraken_pair: str = kraken_pair) -> Quote:
                budget.spend()
                return api.ticker(kraken_pair)

            def minutes_of(since: int, kraken_pair: str = kraken_pair) -> list[Bar]:
                budget.spend()
                return api.ohlc(kraken_pair, 1, since=since)

            def bars_of(tf: int, kraken_pair: str = kraken_pair) -> list[Bar]:
                return series(api, desk, state, kraken_pair, tf, keep[tf], now, budget)
            try:
                bars_of(base.timeframe_min)                 # no bars of the desk's own length: the pair has no data
                daily = bars_of(base.daily_min)[-base.daily_bars:]
                step_pair(run, name, kraken_pair, bars_of, daily, quote_of, minutes_of, observe)
            except DataError as e:
                run.failed[name] = str(e)[:80]
                if str(e) == "budget":
                    break
            except Exception as e:  # noqa: BLE001 — a pair's fault must not stop the other pairs
                run.failed[name] = e.__class__.__name__
    finally:
        finish(run, flush)
        # The last bid read for each pair, for the snapshot to value open positions with (it never calls the venue).
        state["marks"] = {**(state.get("marks") or {}), **{k: [v, int(now)] for k, v in run.marks.items()}}
        write_atomic(state_path, json.dumps(state, sort_keys=True))
    return {"evaluated": {n: sorted(run.seen[n]) for n in names if run.seen.get(n)}, "failed": run.failed,
            "open": {n: sorted(books[n].positions) for n in names if books[n].positions}}
