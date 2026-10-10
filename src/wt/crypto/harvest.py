"""The data harvest (DEC-0027): paper books whose job is to trade often and record everything, for the learning
data. Not evidence: no gate, tournament result or decision counts a harvest trade.

The harvest runs the registered rules (TREND, BREAK, DIP; numbers unchanged) on the 30 coins the model trains on,
through the same engine as the tournament sleeves (`wt.crypto.sleeves.step_pair` and `finish`), so a rule cannot
mean one thing here and another there. What differs is only what it is allowed to do:

  * Its own books, state and journal under `var/crypto/harvest/`. Nothing here is read by the tournament, its
    snapshot, its gates or the learning job's tests.
  * Its own limits (config/risk.yaml, key CH): small positions and room for many, so a signal is not refused
    because a book is full. The desk-wide limits (DEC-0019) are the tournament's and do not apply.
  * The desk's kill switch, evidence-chain flag and shadow role still stop its entries. Its own switch
    (`make crypto-harvest-off`) stops them too; exits are always managed.
  * Its own budget of public calls, spent after the baseline and the sleeves. The pairs are started at a
    different place each cycle, so a short budget does not always starve the same coins.

    python -m wt.crypto.harvest --status        the books: open positions, trades, equity
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from wt.core.config import load_yaml
from wt.core.desk import DESKS, Desk
from wt.crypto import risk, rules, signals, sleeves
from wt.crypto.book import Book, utc_day, write_atomic
from wt.crypto.data import Bar, DataError, KrakenPublic, PairInfo, Quote
from wt.ops.alerts import Alerts

STAGE = "harvest"
PREFIX = "h-"
OFF = "harvest_off"


def desk_of(desk: Desk | None = None) -> Desk:
    """The harvest's own corner of the crypto desk: its state and journal, the desk's kill switch and chain flag.
    The journal is one of the crypto desk's ledgers (wt.core.desk), so the nightly backup checks and anchors it."""
    d = desk or DESKS["crypto"]
    folder = d.state_dir / "harvest"
    return dataclasses.replace(d, state_dir=folder, ledgers=((f"{d.name}-harvest", folder / "harvest_journal.jsonl"),))


def off_file(desk: Desk) -> Path:
    """The owner's switch for the harvest (DEC-0027). `desk` is the crypto desk, not the harvest's own."""
    return desk.state_dir / "HARVEST_OFF"


def specs(cfg: dict[str, Any]) -> dict[str, rules.Spec]:
    """One spec per registered rule and harvest bar length: the rule's numbers unchanged, named `h-<rule>`, with
    `-<n>m` added for any bar length but the sleeves' own."""
    hv, sc = cfg["harvest"], cfg["sleeves"]
    base = rules.Common.of(sc["common"])
    out: dict[str, rules.Spec] = {}
    for tf in hv["timeframes"]:
        c = dataclasses.replace(base, timeframe_min=int(tf))
        for n in hv["rules"]:
            name = PREFIX + n + ("" if int(tf) == base.timeframe_min else f"-{int(tf)}m")
            out[name] = rules.Spec(name, n, c, dict(sc[n]), str(sc[n].get("hypothesis", "")))
    return out


def extras(cfg: dict[str, Any], sp: dict[str, rules.Spec]) -> dict[str, dict[str, Any]]:
    """What every harvest journal row carries: the stage says it is data collection, never evidence."""
    return {n: {"sleeve": n, "strategy": s.hypothesis, "tf": s.c.timeframe_min, "stage": STAGE,
                "decision": str(cfg["harvest"]["decision"]), "config": sleeves.sleeve_hash(cfg, s.base)}
            for n, s in sp.items()}


def pair_infos(api: KrakenPublic, state: dict[str, Any], pairs: list[str], day: str,
               budget: sleeves.Budget) -> dict[str, PairInfo]:
    """As the sleeves' daily lookup, but one pair Kraken does not know cannot cost the others theirs: when the
    one call for all of them fails, each is asked on its own (once a day)."""
    if state.get("info_day") != day:
        got: dict[str, PairInfo] = {}
        try:
            budget.spend()
            got = api.pair_infos(pairs)
        except DataError as e:
            if str(e) == "budget":
                pairs = []
            for p in pairs:
                try:
                    budget.spend()
                    got[p] = api.pair_info(p)
                except DataError as e2:
                    if str(e2) == "budget":
                        break
        if got:
            state["info"] = {**(state.get("info") or {}),
                             **{k: [v.lot_decimals, str(v.tick), str(v.order_min), str(v.cost_min)] for k, v in got.items()}}
            state["info_day"] = day
    return {k: PairInfo(int(v[0]), Decimal(v[1]), Decimal(v[2]), Decimal(v[3])) for k, v in (state.get("info") or {}).items()}


def rotated(pairs: dict[str, str], now: float) -> list[tuple[str, str]]:
    """The pairs in config order, started at a place that moves by one every cycle."""
    items = list(pairs.items())
    k = int(now // 900) % len(items) if items else 0
    return items[k:] + items[:k]


def run(now: float, api: KrakenPublic, desk: Desk, cfg: dict[str, Any], alerts: Alerts, started: float,
        flush: Any) -> dict[str, Any]:
    """One cycle of every harvest book. `desk` is the crypto desk; `flush` the cycle's journal writer. The bars
    store is the desk's own (`bars/`), shared with the sleeves: a series one of them fetched is not fetched again."""
    hv = cfg["harvest"]
    hd = desk_of(desk)
    sp = specs(cfg)
    names = list(sp)
    base = rules.Common.of(cfg["sleeves"]["common"])
    pairs: dict[str, str] = dict(hv["pairs"])
    budget = sleeves.Budget(api, started, max_calls=api.calls + int(hv["max_calls"]),
                            max_seconds=float(hv["max_seconds"]))
    state_path = hd.state_dir / "data.json"
    state: dict[str, Any] = {"stored_to": {}, "info_day": "", "info": {}, "marks": {}}
    if state_path.exists():
        try:
            state = {**state, **json.loads(state_path.read_text())}
        except ValueError:
            pass
    start = Decimal(str(hv["start_equity"]))
    books = {n: Book.load(risk.sleeve_dir(hd, n) / "book.json", start) for n in names}
    for n in names:
        flush(books[n], risk.sleeve_dir(hd, n) / "book.json", hd.journal)
    off = {n: OFF for n in names} if off_file(desk).exists() else {}
    run_ = sleeves.Run(now, hd, sp, cfg["costs"], cfg["quality"], risk.load_sleeve_limits(str(hv["limits"])),
                       tuple(pairs), books, pair_infos(api, state, list(pairs.values()), utc_day(now), budget),
                       extras(cfg, sp), alerts, off=off, desk_lim=None)
    keep: dict[int, int] = {base.daily_min: base.daily_bars}
    for s in sp.values():
        keep[s.c.timeframe_min] = max(keep.get(s.c.timeframe_min, 0), s.c.bars)

    def observe(rec: dict[str, Any]) -> None:
        sleeves._append(hd.state_dir / "observations" / f"harvest-{utc_day(rec['t'])}.jsonl", rec)

    def series(kraken_pair: str, tf: int) -> list[Bar]:
        # The desk's bar store, with the harvest's own "stored up to" marks.
        return sleeves.series(api, desk, state, kraken_pair, tf, keep[tf], now, budget)

    try:
        if "BTC/USD" in pairs:
            btc = series(pairs["BTC/USD"], base.daily_min)
            run_.market = signals.market(btc[-base.daily_bars:])
    except Exception:  # noqa: BLE001 — a signal without its market inputs is still a signal
        run_.market = {}
    try:
        for name, kraken_pair in rotated(pairs, now):
            def quote_of(kraken_pair: str = kraken_pair) -> Quote:
                budget.spend()
                return api.ticker(kraken_pair)

            def minutes_of(since: int, kraken_pair: str = kraken_pair) -> list[Bar]:
                budget.spend()
                return api.ohlc(kraken_pair, 1, since=since)

            def bars_of(tf: int, kraken_pair: str = kraken_pair) -> list[Bar]:
                return series(kraken_pair, tf)
            try:
                daily = bars_of(base.daily_min)[-base.daily_bars:]
                sleeves.step_pair(run_, name, kraken_pair, bars_of, daily, quote_of, minutes_of, observe)
            except DataError as e:
                run_.failed[name] = str(e)[:80]
                if str(e) == "budget":
                    break
            except Exception as e:  # noqa: BLE001 — a pair's fault must not stop the other pairs
                run_.failed[name] = e.__class__.__name__
    finally:
        sleeves.finish(run_, flush)
        state["marks"] = {**(state.get("marks") or {}), **{k: [v, int(now)] for k, v in run_.marks.items()}}
        state_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(state_path, json.dumps(state, sort_keys=True))
    return {"evaluated": sum(len(v) for v in run_.seen.values()), "failed": len(run_.failed),
            "open": sum(len(b.positions) for b in books.values())}


def status(desk: Desk | None = None, cfg: dict[str, Any] | None = None) -> str:
    """The harvest books in a few lines: open positions, closed trades, equity at cost. Counts and money only."""
    d, cfg = desk or DESKS["crypto"], cfg or load_yaml("crypto.yaml")
    hd = desk_of(d)
    lines = [f"Data harvest (DEC-0027): {'OFF' if off_file(d).exists() else 'on'}"
             f"{', desk kill switch ON' if d.kill_file.exists() else ''}"]
    rows = []
    if hd.journal.exists():
        for line in hd.journal.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    for n in specs(cfg):
        book = Book.load(risk.sleeve_dir(hd, n) / "book.json", Decimal(str(cfg["harvest"]["start_equity"])))
        mine = [r for r in rows if r.get("sleeve") == n]
        k = {kind: sum(1 for r in mine if r.get("kind") == kind) for kind in ("signal", "entry", "exit")}
        lines.append(f"  {n:<14} signals {k['signal']:>5}  entries {k['entry']:>5}  exits {k['exit']:>5}  "
                     f"open {len(book.positions):>2}  cash {book.cash:.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.crypto.harvest")
    ap.add_argument("--status", action="store_true")
    ap.parse_args(argv)
    print(status())
    return 0


if __name__ == "__main__":
    sys.exit(main())

