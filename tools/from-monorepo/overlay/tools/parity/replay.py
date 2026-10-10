#!/usr/bin/env python3
"""Replay the same market through one side's desk, for the parity proof (tools/parity.sh).

    PYTHONPATH=<root>/src WT_STATE=<out>/var TZ=UTC PYTHONHASHSEED=0 python replay.py --root <root> --out <out>

`<root>` is a checkout of either repository: this one, or the monorepo at the extraction commit. The script is
the same file for both and imports whatever `wt` the PYTHONPATH gives it, so the two runs differ in nothing but
the code and configuration under test. It runs, in simulated time:

  * the bar cycle (`wt.crypto.cycle.run`) every 15 minutes: the baseline, the tournament sleeves, the live
    challengers and the data harvest, with the owner's controls used along the way and a venue outage;
  * the daily challengers run (`wt.crypto.challengers.run`) and the daily learning run (`wt.crypto.learn.run`);
  * the snapshot publisher in dry-run (`wt.crypto.snapshot.main`), every two hours.

The market comes from the Kraken recordings under tests/fixtures/kraken. The baseline's three pairs replay the
recorded 15-minute and 1-minute bars and the recorded ticker as they were (the recordings are the AUD pairs; they
are served under the USD pairs' names). Everything the recordings do not hold (the longer history the 4-hour,
daily and hourly rules need, the other 27 coins, the bars after the recording ends) is drawn from the recorded
bars' own shapes with a fixed seed, so both sides see exactly the same bytes from the venue.

What is pinned here because it legitimately differs from run to run on either side (see tools/parity.sh):
record ids (uuid4), the wall clock, the snapshot's run-id suffix (os.urandom), the kill switch's file time, the
commit in a heartbeat, and the process's hash seed and time zone (set by the caller).

Standard library plus the `wt` under test. Writes only under --out.
"""
from __future__ import annotations

import argparse
import datetime as real_dt
import hashlib
import json
import os
import random
import shutil
import sys
import time
import types
import uuid
from pathlib import Path

C0 = 1_791_072_000                      # 2026-10-04 00:00:00 UTC: a 15-minute, 4-hour and daily boundary
STEP = 900
DAY = 86_400
HISTORY_DAYS = 64                       # before C0: 60 closed daily bars, 120 four-hour bars, 120 hourly bars
RECORDED = {"XBTUSD": "XBTAUD", "ETHUSD": "ETHAUD", "SOLUSD": "SOLAUD"}
SEED = 20261004


# ---- the pins --------------------------------------------------------------------------------------------------

class Clock:
    t = float(C0)


def pin_process() -> None:
    """Replace the three process-wide sources of difference before any `wt` module is imported."""
    count = iter(range(1, 10**9))
    uuid.uuid4 = lambda: uuid.UUID(int=next(count))                     # type: ignore[assignment]
    time.time = lambda: Clock.t                                         # type: ignore[assignment]
    seq = iter(range(1, 10**9))
    os.urandom = lambda n: hashlib.sha256(str(next(seq)).encode()).digest()[:n]   # type: ignore[assignment]


class _DateTime(real_dt.datetime):
    @classmethod
    def now(cls, tz=None):                                              # type: ignore[override]
        return cls.fromtimestamp(Clock.t, tz)


def pin_clock_in(modules: list[types.ModuleType]) -> int:
    """Give every loaded `wt` module a `datetime` whose `now()` is the simulated clock."""
    fake = types.ModuleType("datetime")
    fake.__dict__.update({k: v for k, v in real_dt.__dict__.items() if not k.startswith("__")})
    fake.datetime = _DateTime                                           # type: ignore[attr-defined]
    n = 0
    for m in modules:
        for name in ("dt", "datetime"):
            if getattr(m, name, None) is real_dt:
                setattr(m, name, fake)
                n += 1
    return n


# ---- the market ------------------------------------------------------------------------------------------------

def decimals_for(price: float) -> int:
    return 1 if price >= 10_000 else 2 if price >= 100 else 3 if price >= 10 else 4 if price >= 1 else 5 if price >= 0.01 else 8


class Market:
    """Fifteen-minute bars for every pair over the whole replay, from the recordings' own shapes."""

    def __init__(self, fixtures: Path, pairs: dict[str, str], end: int) -> None:
        self.fix = fixtures
        self.start = C0 - HISTORY_DAYS * DAY
        self.end = end
        self.rec15 = {k: self._closed(f"ohlc15_{v}.json", v) for k, v in RECORDED.items()}
        self.rec1 = {k: self._closed(f"ohlc1_{v}.json", v) for k, v in RECORDED.items()}
        self.ticker = json.loads((fixtures / "ticker.json").read_text())["result"]
        self.assets = json.loads((fixtures / "assetpairs.json").read_text())["result"]
        self.rec_time = int(json.loads((fixtures / "time.json").read_text())["result"]["unixtime"])
        shapes = []
        for rows in self.rec15.values():
            for prev, row in zip(rows, rows[1:], strict=False):
                c0 = float(prev[4])
                shapes.append((float(row[1]) / c0, float(row[2]) / c0, float(row[3]) / c0, float(row[4]) / c0,
                               float(row[5]) / c0, float(row[6]), int(row[7])))
        self.shapes = shapes
        self.names = dict(pairs)                                        # ours -> the venue's
        self.venue_pairs = list(dict.fromkeys(pairs.values()))
        self.base: dict[str, list[tuple[int, float, float, float, float, float, float, int]]] = {}
        self.dec: dict[str, int] = {}
        for k, vp in enumerate(self.venue_pairs):
            self.base[vp] = self._series(vp, k)
        self.agg: dict[tuple[str, int], list[tuple[int, float, float, float, float, float, float, int]]] = {}

    def _closed(self, name: str, key: str) -> list[list]:
        res = json.loads((self.fix / name).read_text())["result"]
        return [r for r in res[key] if int(r[0]) <= int(res["last"])]

    def _walk(self, rng: random.Random, t0: int, n: int, price: float, k: int, forward: bool) -> list[tuple]:
        """`n` bars of 15 minutes drawn from the recorded shapes, with a slow swing so that trends and breakouts
        happen. Forward from `price`, or backward so that the last bar closes at `price`."""
        out = []
        p = price
        for i in range(n):
            t = t0 + i * STEP if forward else t0 - (i + 1) * STEP
            o, h, lo, c, vw, v, cnt = self.shapes[rng.randrange(len(self.shapes))]
            phase = (t // DAY + k) % 7
            drift = 1 + (0.0006 if phase in (0, 1, 4) else -0.0005 if phase in (2, 5) else 0.0)
            c *= drift
            h, lo = max(h, c, o), min(lo, c, o)
            if forward:
                bar = (t, p * o, p * h, p * lo, p * c, p * vw, v, cnt)
                p = p * c
            else:
                q = p / c                                               # the close before this bar
                bar = (t, q * o, q * h, q * lo, p, q * vw, v, cnt)
                p = q
            out.append(bar)
        return out if forward else out[::-1]

    def _series(self, vp: str, k: int) -> list[tuple]:
        rng = random.Random(f"{SEED}|{vp}")
        if vp in self.rec15:
            rec = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6]), int(r[7]))
                   for r in self.rec15[vp]]
            before = self._walk(rng, rec[0][0], (rec[0][0] - self.start) // STEP, rec[0][1], k, forward=False)
            after = self._walk(rng, rec[-1][0] + STEP, max(0, (self.end - rec[-1][0]) // STEP), rec[-1][4], k, forward=True)
            self.dec[vp] = int(self.assets[RECORDED[vp]]["pair_decimals"])
            return before + rec + after
        price = [61.0, 2.4, 0.62, 0.19, 14.2, 11.5, 84.0, 410.0, 3.9, 7.1][k % 10] * (1 + (k // 10) * 0.37)
        self.dec[vp] = decimals_for(price)
        return self._walk(rng, self.start, (self.end - self.start) // STEP + 1, price, k, forward=True)

    def bars(self, vp: str, tf_min: int) -> list[tuple]:
        """Every bar of a length, aggregated from the 15-minute bars on UTC boundaries."""
        key = (vp, tf_min)
        if key not in self.agg:
            step = tf_min * 60
            if tf_min == 15:
                self.agg[key] = self.base[vp]
            else:
                out: list[tuple] = []
                cur: list[tuple] = []
                for b in self.base[vp]:
                    if cur and b[0] // step != cur[0][0] // step:
                        out.append(self._merge(cur, step))
                        cur = []
                    cur.append(b)
                if cur:
                    out.append(self._merge(cur, step))
                self.agg[key] = out
        return self.agg[key]

    @staticmethod
    def _merge(part: list[tuple], step: int) -> tuple:
        v = sum(b[6] for b in part)
        vw = sum(b[5] * b[6] for b in part) / v if v > 0 else 0.0
        return (part[0][0] // step * step, part[0][1], max(b[2] for b in part), min(b[3] for b in part), part[-1][4],
                vw, v, sum(b[7] for b in part))

    def minutes(self, vp: str, since: int, now: float) -> list[list]:
        """One-minute rows after `since`: the recorded ones where the recording has them, else each 15-minute
        bar walked from its open to its close, touching its high and its low on the way."""
        out: list[list] = []
        lo = max(since + 60, int(now) - 12 * 3600) // 60 * 60
        rec = {int(r[0]): r for r in self.rec1.get(vp, [])}
        by_t = {b[0]: b for b in self.base[vp]}
        d = self.dec[vp]
        t = lo
        while t <= int(now):
            if t in rec:
                out.append(list(rec[t]))
            else:
                b = by_t.get(t // STEP * STEP)
                if b is not None:
                    k = (t - b[0]) // 60
                    o = b[1] + (b[4] - b[1]) * k / 15
                    c = b[1] + (b[4] - b[1]) * (k + 1) / 15
                    hi_at, lo_at = b[0] // STEP % 15, (b[0] // STEP + 7) % 15
                    h = b[2] if k == hi_at else max(o, c)
                    low = b[3] if k == lo_at else min(o, c)
                    out.append([t, f"{o:.{d}f}", f"{max(h, o, c):.{d}f}", f"{min(low, o, c):.{d}f}", f"{c:.{d}f}",
                                f"{b[5]:.{d}f}", f"{b[6] / 15:.8f}", b[7] // 15])
            t += 60
        return out


class Venue:
    """Answers Kraken's four public endpoints from the market, as of the simulated clock."""

    def __init__(self, market: Market, missing: tuple[str, ...] = ()) -> None:
        self.m, self.down, self.missing, self.calls = market, False, missing, 0

    def get(self, url: str, params: dict) -> dict:
        self.calls += 1
        if self.down:
            raise OSError("down")
        now = Clock.t
        name = url.rsplit("/", 1)[1]
        if name == "Time":
            return {"error": [], "result": {"unixtime": int(now)}}
        if name == "AssetPairs":
            return {"error": [], "result": {vp: self.asset(vp) for vp in str(params["pair"]).split(",") if vp not in self.missing}}
        vp = str(params["pair"])
        if vp not in self.m.base:
            return {"error": ["EQuery:Unknown asset pair"]}
        d = self.m.dec[vp]
        if name == "Ticker":
            if vp in RECORDED and abs(now - self.m.rec_time) < STEP:
                return {"error": [], "result": {vp: self.m.ticker[RECORDED[vp]]}}
            last = [b for b in self.m.base[vp] if b[0] + STEP <= now][-1][4]
            half = last * [0.0001, 0.0002, 0.0003, 0.0004, 0.0006, 0.0010][self.m.venue_pairs.index(vp) % 6]
            bid, ask = f"{last - half:.{d}f}", f"{last + half:.{d}f}"
            return {"error": [], "result": {vp: {"a": [ask, "1", "1.000"], "b": [bid, "1", "1.000"], "c": [ask, "0.1"]}}}
        if name == "OHLC":
            tf = int(params["interval"])
            since = params.get("since")
            if tf == 1:
                rows = self.m.minutes(vp, int(since) if since is not None else int(now) - 12 * 3600, now)
                closed = [r for r in rows if int(r[0]) + 60 <= now]
                return {"error": [], "result": {vp: rows, "last": int(closed[-1][0]) if closed else int(since or 0)}}
            step = tf * 60
            every = [b for b in self.m.bars(vp, tf) if b[0] <= now]
            closed = [b for b in every if b[0] + step <= now][-720:]
            forming = [b for b in every if b[0] + step > now][:1]
            rows = [[b[0], f"{b[1]:.{d}f}", f"{b[2]:.{d}f}", f"{b[3]:.{d}f}", f"{b[4]:.{d}f}", f"{b[5]:.{d}f}",
                     f"{b[6]:.8f}", b[7]] for b in [*closed, *forming] if since is None or b[0] > int(since)]
            return {"error": [], "result": {vp: rows, "last": closed[-1][0] if closed else 0}}
        raise AssertionError(name)

    def asset(self, vp: str) -> dict:
        if vp in RECORDED:
            return {**self.m.assets[RECORDED[vp]], "altname": vp, "status": "online"}
        d = self.m.dec[vp]
        return {"altname": vp, "status": "online", "lot_decimals": 8, "pair_decimals": d, "tick_size": f"{10 ** -d:.{d}f}",
                "ordermin": "0.5" if d >= 3 else "0.01", "costmin": "0.5"}


# ---- the scenario ----------------------------------------------------------------------------------------------

def manifest(root: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.endswith(".lock") and ".tmp" not in p.name:
            data = p.read_bytes()
            out[p.relative_to(root).as_posix()] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cycles", type=int, default=240)
    ap.add_argument("--controls", type=int, default=3)
    a = ap.parse_args()
    root, out = a.root.resolve(), a.out.resolve()
    state = Path(os.environ["WT_STATE"]).resolve()
    assert state.is_relative_to(out) and not state.exists(), "WT_STATE must be a new directory under --out"
    assert os.environ.get("TZ") == "UTC" and os.environ.get("PYTHONHASHSEED") == "0", "run with TZ=UTC PYTHONHASHSEED=0"
    for name in ("NTFY_TOPIC", "HC_PING_KEY", "DASHBOARD_INGEST_URL", "DASHBOARD_INGEST_SECRET", "WT_ENV_FILE", "WT_ROLE"):
        os.environ.pop(name, None)
    time.tzset()
    pin_process()

    import wt
    from wt.core import ledger
    from wt.core.config import load_yaml
    from wt.core.desk import DESKS
    from wt.crypto import challengers, control, cycle, learn, snapshot
    from wt.crypto.data import Bar, KrakenPublic, PairInfo
    from wt.ops.heartbeat import Heartbeat
    assert Path(wt.__file__).resolve().is_relative_to(root / "src"), f"wt comes from {wt.__file__}, not {root}"
    try:
        from wt.ops import init as desk_init
    except ImportError:
        desk_init = None                                               # the monorepo: its first cycle makes the desk
    import importlib
    import pkgutil
    import wt.crypto
    for info in pkgutil.iter_modules(wt.crypto.__path__):               # every module of the desk, before the pin
        importlib.import_module(f"wt.crypto.{info.name}")
    for name in ("alerts", "heartbeat", "locks", "publish", "safeio", "schedule"):
        importlib.import_module(f"wt.ops.{name}")
    importlib.import_module("wt.research.trials")
    pinned = pin_clock_in([m for n, m in list(sys.modules.items()) if n == "wt" or n.startswith("wt.")])

    cfg = load_yaml("crypto.yaml")
    desk = DESKS["crypto"]
    assert desk.state_dir == state / "crypto", desk.state_dir
    pairs = {**cfg["pairs"], **cfg["sleeves"]["common"]["pairs"], **(cfg.get("harvest") or {}).get("pairs", {})}
    end = C0 + a.cycles * STEP + DAY
    market = Market(root / "tests" / "fixtures" / "kraken", pairs, end)
    missing = tuple(v for v in market.venue_pairs if v.startswith("SHIB"))          # one coin the venue does not list
    venue = Venue(market, missing)
    snaps = out / "snapshots"
    snaps.mkdir(parents=True)
    print(f"replay: wt from {root.name}; {len(market.venue_pairs)} pairs; {a.cycles} cycles from {C0}; clock pinned in {pinned} modules")

    def api() -> KrakenPublic:
        return KrakenPublic(get=venue.get, min_interval_s=0)

    def hourly(pair: str, start: int, stop: int) -> list:
        vp = market.names.get(pair)
        return [Bar(*b) for b in market.bars(vp, 60) if start <= b[0] < stop and b[0] + 3600 <= Clock.t] if vp else []

    def stamp_kill() -> None:
        """The snapshot publishes the kill switch's file time: give it the simulated time it was written at."""
        if desk.kill_file.exists():
            os.utime(desk.kill_file, (Clock.t, Clock.t))

    def as_job(name: str, work) -> int:
        hb = Heartbeat(name, "parity0")
        code = work()
        Clock.t += 3
        hb.finish("ok" if code == 0 else "failed", code)
        return code

    # The history a challenger's backtest and its confirmation run on: four of the tournament's pairs, hourly,
    # over the registered experiment's own span and the span before it, from the same recorded shapes.
    exp = challengers.experiment(cfg) if "learning" in cfg else None
    gate_market = earlier = None
    if exp is not None:
        span = int(exp["end"]) - int(exp["start"])
        names = [n for n in ("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD") if n in cfg["sleeves"]["common"]["pairs"]]

        def history(first: int, last: int, salt: str) -> dict:
            got = {}
            for k, n in enumerate(names):
                rng = random.Random(f"{SEED}|{salt}|{n}")
                p, bars, t = [30_000.0, 2_000.0, 90.0, 0.6][k], [], first // 3600 * 3600
                while t < last:
                    o = p
                    hi = lo = p
                    v = cnt = 0.0
                    for _ in range(4):
                        so, sh, sl, sc, _vw, sv, sn = market.shapes[rng.randrange(len(market.shapes))]
                        swing = 1 + (0.0004 if (t // (9 * DAY) + k) % 3 == 0 else -0.0003 if (t // (9 * DAY) + k) % 3 == 1 else 0.0)
                        hi, lo = max(hi, p * sh), min(lo, p * sl)
                        p = p * sc * swing
                        hi, lo = max(hi, p), min(lo, p)
                        v, cnt = v + sv, cnt + sn
                    bars.append(Bar(t, o, hi, lo, p, (o + p) / 2 if v > 0 else 0.0, v, int(cnt)))
                    t += 3600
                got[n] = bars
            return got
        infos = {cfg["sleeves"]["common"]["pairs"][n]: PairInfo(8, __import__("decimal").Decimal("0.01"),
                                                                __import__("decimal").Decimal("0.0001"),
                                                                __import__("decimal").Decimal("0.5")) for n in names}
        warm = 80 * DAY
        gate_market = (history(int(exp["start"]) - warm, int(exp["end"]), "gate"), infos)
        earlier = (history(int(exp["start"]) - span - warm, int(exp["start"]), "earlier"), infos)

    codes: dict[str, list[int]] = {"cycle": [], "snapshot": [], "challengers": [], "learn": [], "control": []}
    for k in range(a.cycles):
        close = C0 + k * STEP
        Clock.t = close + 10.0
        venue.down = k in (40, 41, 42, 43)
        if k == 0 and desk_init is not None:
            # This repository: a desk is made on purpose. The monorepo's first cycle makes the same directory itself.
            desk_init.create(desk, real_dt.datetime.fromtimestamp(Clock.t, real_dt.UTC))
        if k == 8:
            codes["control"].append(control.main(["unkill"]))
        if k == 60:
            codes["control"].append(control.main(["learning-off"]))
        if k == 70:
            codes["control"].append(control.main(["learning-on"]))
        if k == 100:                                                    # `make kill`
            desk.kill_file.write_text("paused by the replay (2026-10-05 01:00 UTC)\n")
            stamp_kill()
        if k == 104:
            codes["control"].append(control.main(["unkill"]))
        if cfg.get("harvest") and k in (120, 130):
            codes["control"].append(control.main(["harvest-off" if k == 120 else "harvest-on"]))
        if k == 150:
            codes["control"].append(control.main(["reset-latch"]))
        print(f"--- cycle {k} at {int(Clock.t)}" + (" (venue down)" if venue.down else ""))
        codes["cycle"].append(as_job("crypto", lambda: cycle.run(now=Clock.t, api=api())))
        if k == 0:
            stamp_kill()
            if desk_init is not None:
                desk_init.settle(desk)                                  # what the job runner does after the first cycle
            (out / "after-first-cycle.json").write_text(json.dumps(manifest(state / "crypto"), indent=1, sort_keys=True))
        tod = close % DAY
        if exp is not None and tod == 7 * 3600 + 1800:                   # 03:30 New York in October
            Clock.t = close + 30.0
            print(f"--- challengers at {int(Clock.t)}")
            codes["challengers"].append(as_job("crypto-challengers", lambda: challengers.run(
                now=Clock.t, market=gate_market, earlier=earlier, controls=a.controls)))
        if exp is not None and tod == 8 * 3600 + 1800:
            Clock.t = close + 30.0
            print(f"--- learn at {int(Clock.t)}")
            codes["learn"].append(as_job("crypto-learn", lambda: learn.run(
                now=Clock.t, load=hourly, trainer=lambda d: (None, "no_environment"))))
        if k % 8 == 7 or k == a.cycles - 1:
            Clock.t = close + 60.0
            codes["snapshot"].append(as_job("dashboard-crypto", lambda: snapshot.main(["--dry-run"])))
            shutil.copyfile(state / "dashboard-crypto" / "outbox" / "snapshot.json", snaps / f"{k:04d}.json")

    rows = [json.loads(x) for x in desk.journal.read_text().splitlines()]
    kinds: dict[str, int] = {}
    for r in rows:
        key = str(r.get("kind")) + (f":{r['event']}" if r.get("kind") in ("challenger", "model") else "")
        kinds[key] = kinds.get(key, 0) + 1
    summary = {
        "cycles": a.cycles, "exit_codes": {k: sorted(set(v)) for k, v in codes.items()}, "runs": {k: len(v) for k, v in codes.items()},
        "venue_calls": venue.calls, "journal_rows": len(rows), "journal_kinds": dict(sorted(kinds.items())),
        "journal_chain_breaks": ledger.verify_chain(desk.journal), "journal_head": ledger.head(desk.journal),
        "ledgers": {name: {"head": ledger.head(path), "breaks": ledger.verify_chain(path)} for name, path in desk.ledgers},
        "snapshots": len(list(snaps.glob("*.json"))),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    (out / "manifest.json").write_text(json.dumps({"state": manifest(state), "snapshots": manifest(snaps)}, indent=1, sort_keys=True))
    print("replay: done; " + json.dumps({k: summary[k] for k in ("journal_rows", "journal_kinds", "snapshots")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
