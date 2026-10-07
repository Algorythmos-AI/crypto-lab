"""One bar cycle of the crypto desk: data, quality, exits, entries, evidence. Run every 15 minutes by the job
runner (`python -m wt.ops.jobs run crypto`), a few seconds after each bar closes.

    python -m wt.crypto.cycle            one cycle against the live public API

Rules it keeps:
  * No data, no action. A pair whose data cannot be read is skipped; `stale_after_cycles` cycles in a row with
    nothing readable page as crypto:data-stale.
  * Exits before entries, and an exit is never blocked by the kill switch, the latch or a limit.
  * A bar is evaluated once. A second run for the same bar manages exits and does nothing else.
  * One atomic file (book.json) holds the book and the cycle's bookkeeping. Journal records go through its outbox,
    so a crash between "decided" and "journalled" is repaired on the next run and nothing is written twice.
  * Bars and observations are appended before the book is saved: after a crash a line may repeat, never go
    missing. Readers key on (pair, t).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from wt.core import ledger
from wt.core.config import load_yaml
from wt.core.desk import DESKS, Desk
from wt.crypto import challengers, features, risk, rules, sleeves
from wt.crypto.book import Book, Position, Rejected, utc_day
from wt.crypto.data import Bar, DataError, KrakenPublic, Quote
from wt.crypto.quality import assess
from wt.crypto.strategy import Params, entry, find_exit, read
from wt.ops.alerts import Alerts

SEEN_TAIL = 2000                        # journal lines checked for an id before an outbox record is appended


def config_hash(cfg: dict[str, Any]) -> str:
    """What the frozen config was when a record was made: any change to rules, costs, quality or pairs shows."""
    frozen = {k: cfg[k] for k in ("pairs", "timeframe_min", "bars", "strategy", "costs", "quality")}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()[:12]


def flush(book: Book, book_path: Path, journal: Path) -> int:
    """Append the outbox to the journal (skipping ids already there), then clear it. Returns lines written."""
    if not book.outbox:
        return 0
    seen: set[str] = set()
    if journal.exists():
        for line in journal.read_bytes().splitlines()[-SEEN_TAIL:]:
            try:
                seen.add(json.loads(line).get("id", ""))
            except (ValueError, AttributeError):
                continue
    n = 0
    for rec in book.outbox:
        if rec["id"] not in seen:
            ledger.append(journal, rec, fsync=True)
            n += 1
    book.outbox = []
    book.save(book_path)
    return n


def _append(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")


def store_bars(desk: Desk, kraken_pair: str, tf: int, bars: list[Bar], book: Book) -> None:
    done = int(book.meta["stored_to"].get(kraken_pair, 0))
    for b in bars:
        if b.t > done:
            _append(desk.state_dir / "bars" / f"{kraken_pair}-{tf}m.jsonl",
                    {"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "vwap": b.vwap, "v": b.v, "n": b.n})
    if bars:
        book.meta["stored_to"][kraken_pair] = max(done, bars[-1].t)


def manage_exit(name: str, kraken_pair: str, pos: Position, bars: list[Bar], quote: Quote, api: KrakenPublic,
                book: Book, now: float, p: Params, costs: dict[str, Any], tf: int, cfg_hash: str,
                alerts: Alerts) -> None:
    fee, slip = float(costs["taker_fee_pct"]), float(costs["slippage_bps"])
    minutes = [b for b in api.ohlc(kraken_pair, 1, since=pos.checked_to) if b.t > pos.checked_to]
    if minutes and minutes[0].t - pos.checked_to > 120:
        # Kraken keeps 12 hours of 1-minute bars: after a longer outage the path between is unknown.
        book.note({"kind": "exit_gap", "t": _iso(now), "pair": name, "from": pos.checked_to, "to": minutes[0].t,
                   "config": cfg_hash})
        alerts.fire(f"crypto:exit-gap:{name}", "Crypto: an open position has an unobserved gap",
                    f"{name}: 1-minute data is missing between the last check and now; the day is an incident.", 4)
    x = find_exit(minutes, float(pos.stop), float(pos.target))
    held_bars = (bars[-1].t - pos.entry_bar) // (tf * 60)
    if x is not None:
        reason, t = x.reason, x.t + 60
        fill = book.sell(name, x.price, fee, slip if x.reason == "stop" else 0.0, t)
    elif held_bars >= p.time_stop_bars:
        reason, t = "time", int(now)
        fill = book.sell(name, quote.bid, fee, slip, t)
    else:
        if minutes:
            pos.checked_to = minutes[-1].t
        return
    book.note({"kind": "exit", "t": _iso(t), "pair": name, "reason": reason, "qty": str(pos.qty),
               "entry_price": str(pos.entry_price), "entry_bar": pos.entry_bar, "config": cfg_hash, **fill})


def run(now: float | None = None, api: KrakenPublic | None = None, desk: Desk | None = None,
        cfg: dict[str, Any] | None = None, limits: risk.Limits | None = None, alerts: Alerts | None = None) -> int:
    now, started = time.time() if now is None else now, time.monotonic()
    api, desk, cfg = api or KrakenPublic(), desk or DESKS["crypto"], cfg or load_yaml("crypto.yaml")
    limits, alerts = limits or risk.load_limits(desk.strategy), alerts or Alerts()
    p, costs, qcfg, tf = Params.of(cfg["strategy"]), cfg["costs"], cfg["quality"], int(cfg["timeframe_min"])
    cfg_hash, day = config_hash(cfg), utc_day(now)
    if not desk.state_dir.exists():
        # The desk's first run on this host. It starts with its kill switch on: it records and publishes, and
        # opens nothing until the owner removes the file (DEC-0012).
        desk.state_dir.mkdir(parents=True)
        desk.kill_file.write_text(f"created with the desk on {day}; the owner removes it (make unkill DESK=crypto)\n")
    book_path = desk.state_dir / "book.json"
    book = Book.load(book_path, Decimal(str(cfg["account"]["start_equity"])))
    flush(book, book_path, desk.journal)                    # repair: anything decided last time is journalled now

    failed: dict[str, str] = {}
    seen: dict[str, dict[str, Any]] = {}
    marks: dict[str, float] = {}
    try:
        skew = abs(api.time() - now)
        if skew > float(qcfg["max_clock_skew_s"]):
            raise DataError(f"clock skew {skew:.0f}s")
    except DataError as e:
        failed = {name: f"clock:{e}"[:80] for name in cfg["pairs"]}

    for name, kraken_pair in ({} if failed else cfg["pairs"]).items():
        try:
            bars = api.ohlc(kraken_pair, tf)
            quote = api.ticker(kraken_pair)
            if not bars:
                raise DataError("no closed bars")
            store_bars(desk, kraken_pair, tf, bars, book)
            marks[name] = quote.bid
            if (pos := book.positions.get(name)) is not None:
                manage_exit(name, kraken_pair, pos, bars, quote, api, book, now, p, costs, tf, cfg_hash, alerts)
                if risk.update_latch(book, day, desk, limits):
                    book.note({"kind": "latch", "t": _iso(now), "day": day, "config": cfg_hash})
                    alerts.fire("crypto:latch", "Crypto: daily loss limit reached, entries off",
                                "Entries stay off until the owner resets the latch. Exits are still managed.", 4)
        except DataError as e:
            failed[name] = str(e)[:80]
            continue

        last = bars[-1]
        if book.meta["last_bar"].get(name) == last.t:
            continue                                        # this bar was evaluated by an earlier run
        window = bars[-int(cfg["bars"]):]
        q = assess(window, quote, now, tf, qcfg)
        r = read(window, p)
        fire, why = entry(r, p)
        seen[name] = {"bar": last.t, "tradable": q.tradable, "quality": list(q.reasons), "fire": fire}
        _append(desk.state_dir / "observations" / f"obs-{utc_day(last.t)}.jsonl",
                {"t": last.t, "pair": name, "close": last.c, "features": features.extract(window),
                 "would_fire": fire, "why_not": list(why), "tradable": q.tradable, "quality": list(q.reasons),
                 "traded_share": q.traded_share, "spread_pct": q.spread_pct, "config": cfg_hash})
        if fire:
            notional = limits.max_notional
            blockers = list(q.reasons) + risk.entry_blockers(name, notional, book, day, desk, limits)
            if not blockers:
                try:
                    pos = book.buy(name, notional, quote.ask, api.pair_info(kraken_pair), float(costs["taker_fee_pct"]),
                                   float(costs["slippage_bps"]), int(now), last.t, p.stop_loss_pct, p.take_profit_pct)
                    book.note({"kind": "entry", "t": _iso(now), "pair": name, "bar": last.t, "qty": str(pos.qty),
                               "price": str(pos.entry_price), "stop": str(pos.stop), "target": str(pos.target),
                               "fee": str(pos.entry_fee), "rsi": r.rsi, "config": cfg_hash})
                    seen[name]["entered"] = True
                except (Rejected, DataError) as e:
                    blockers = [str(e.args[0])[:40] if isinstance(e, Rejected) else "no_pair_info"]
            if blockers:
                book.note({"kind": "refused", "t": _iso(now), "pair": name, "bar": last.t, "why": blockers,
                           "config": cfg_hash})
                seen[name]["refused"] = blockers
        book.meta["last_bar"][name] = last.t

    nothing = len(failed) == len(cfg["pairs"])
    book.meta["misses"] = int(book.meta["misses"]) + 1 if nothing else 0
    if seen or failed:
        book.note({"kind": "cycle", "t": _iso(now), "config": cfg_hash, "pairs": seen, "failed": failed,
                   "kill": desk.kill_file.exists(), "open": sorted(book.positions),
                   "equity": str(book.equity(marks).quantize(Decimal("0.01")))})
    book.save(book_path)
    flush(book, book_path, desk.journal)
    if book.meta["misses"] >= int(qcfg["stale_after_cycles"]):
        alerts.fire("crypto:data-stale", "Crypto: no market data",
                    f"{book.meta['misses']} cycles in a row read nothing from the venue. No entries; open "
                    "positions are not being watched.", 4)
    elif not nothing:
        alerts.resolve("crypto:data-stale", "Crypto: market data is back", "The venue answers again.")
    print(f"cycle {_iso(now)}: evaluated {sorted(seen)} failed {sorted(failed)} open {sorted(book.positions)}")
    if cfg.get("sleeves") and not str(next(iter(failed.values()), "")).startswith("clock:"):
        # The tournament sleeves (DEC-0015) run after the baseline is saved and journalled, and nothing they do
        # can change its result: a fault in them is reported and the cycle still ends as the baseline left it.
        specs: dict[str, rules.Spec] = rules.registered(cfg)
        off: dict[str, str] = {}
        try:
            # Challengers that passed their backtest (DEC-0016, 5) trade beside the registered sleeves. A fault in
            # their record leaves them out of this cycle; it never stops the registered sleeves.
            more, off = challengers.active(desk, cfg)
            specs = {**specs, **more}
            alerts.resolve("crypto:challengers-failed", "Crypto: the challengers run again", "The fault has cleared.")
        except Exception as e:  # noqa: BLE001
            print(f"challengers left out ({e.__class__.__name__}: {e})", file=sys.stderr)
            alerts.fire("crypto:challengers-failed", "Crypto: the challengers did not run",
                        f"{e.__class__.__name__} reading the challengers' record. The registered sleeves ran. A "
                        "challenger's open position is not being watched until this is fixed.", 4)
        try:
            s = sleeves.run_all(now, api, desk, cfg, alerts, started, flush, specs=specs, off=off)
            print(f"sleeves: evaluated {s['evaluated']} failed {sorted(s['failed'])} open {s['open']}")
            alerts.resolve("crypto:sleeves-failed", "Crypto: the tournament sleeves run again", "The fault has cleared.")
        except Exception as e:  # noqa: BLE001
            print(f"sleeves failed ({e.__class__.__name__}); the baseline cycle is unaffected", file=sys.stderr)
            alerts.fire("crypto:sleeves-failed", "Crypto: the tournament sleeves did not run",
                        f"{e.__class__.__name__} in the sleeves' cycle. The baseline ran. Open sleeve positions "
                        "are not being watched until this is fixed.", 4)
    return 0


if __name__ == "__main__":
    sys.exit(run())
