"""Writes dashboard/test/fixtures/crypto.v1.json: a crypto desk snapshot built by the real builder
(wt.crypto.snapshot.collect) from a synthetic, seeded desk state. No real prices, trades or account data.

    python scripts/gen_crypto_fixture.py            # rewrite the fixture
    python scripts/gen_crypto_fixture.py --check    # exit 1 if the committed fixture is stale (CI)
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import random
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from wt.core import ledger  # noqa: E402
from wt.core.desk import DESKS  # noqa: E402
from wt.crypto import cycle, features, snapshot  # noqa: E402
from wt.crypto.book import Book  # noqa: E402
from wt.crypto.data import PairInfo  # noqa: E402
from wt.ops import publish  # noqa: E402

FIXTURE = ROOT / "dashboard" / "test" / "fixtures" / "crypto.v1.json"
NOW = dt.datetime(2026, 10, 2, 20, 0, 40, tzinfo=dt.UTC)
BASE = {"BTC/USD": 120_000.0, "ETH/USD": 3_900.0, "SOL/USD": 175.0}
INFO = PairInfo(8, Decimal("0.01"), Decimal("0.00001"), Decimal("1"))


def synthetic(state: Path, days: int = 5, seed: int = 7) -> None:
    """A desk state on disk: observations every 15 minutes, a handful of closed trades, one open position."""
    rng = random.Random(seed)
    cfg = yaml.safe_load((ROOT / "config" / "crypto.yaml").read_text())
    h = cycle.config_hash(cfg)
    journal = state / "crypto_journal.jsonl"
    book = Book(Decimal(10000), Decimal(10000))
    start = int((NOW - dt.timedelta(days=days)).timestamp()) // 900 * 900
    price = dict(BASE)
    for i in range(days * 96):
        t = start + i * 900
        when = dt.datetime.fromtimestamp(t + 910, dt.UTC)
        seen: dict[str, Any] = {}
        for pair in BASE:
            price[pair] *= 1 + rng.gauss(0, 0.0015)
            thin = pair != "BTC/USD" and rng.random() < (0.45 if pair == "ETH/USD" else 0.3)
            spread = {"BTC/USD": 0.08, "ETH/USD": 0.24, "SOL/USD": 0.31}[pair] * rng.uniform(0.7, 1.4)
            fv = {k: None for k in features.COLUMNS}
            fv.update(rsi=round(min(95, max(5, rng.gauss(50, 14))), 2), atr_pct=round(abs(rng.gauss(0.12, 0.05)), 4),
                      vwap_distance_pct=round(rng.gauss(0, 0.25), 4), ema8_distance_pct=round(rng.gauss(0, 0.1), 4),
                      volume_ratio=None if thin else round(abs(rng.gauss(1, 0.6)), 3),
                      hour_utc=float(when.hour), day_of_week=float(when.weekday()))
            fire = fv["rsi"] < 32 and fv["vwap_distance_pct"] > 0 and fv["ema8_distance_pct"] > 0
            quality = (["bar_untraded"] if thin else []) + (["spread_wide"] if spread > 0.15 else [])
            snapshot_obs = {"t": t, "pair": pair, "close": round(price[pair], 2), "features": fv, "would_fire": fire,
                            "why_not": [] if fire else ["rsi_high"], "tradable": not quality, "quality": quality,
                            "traded_share": 0.9, "spread_pct": round(spread, 4), "config": h}
            f = state / "observations" / f"obs-{dt.datetime.fromtimestamp(t, dt.UTC).date()}.jsonl"
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a") as fh:
                fh.write(json.dumps(snapshot_obs, sort_keys=True) + "\n")
            seen[pair] = {"bar": t, "tradable": not quality, "quality": quality, "fire": fire}
        if i % 60 == 20 and "BTC/USD" not in book.positions:                # a trade every 15 hours or so
            pos = book.buy("BTC/USD", Decimal(50), price["BTC/USD"], INFO, 0.40, 5, t + 910, t, 0.5, 1.0)
            ledger.append(journal, {"id": f"e{i}", "kind": "entry", "t": when.isoformat(), "pair": "BTC/USD", "bar": t,
                                    "qty": str(pos.qty), "price": str(pos.entry_price), "config": h})
        elif i % 60 == 27 and "BTC/USD" in book.positions and i < days * 96 - 60:
            pos = book.positions["BTC/USD"]
            win = rng.random() < 0.45
            fill = book.sell("BTC/USD", float(pos.target if win else pos.stop), 0.40, 0 if win else 5, t + 910)
            ledger.append(journal, {"id": f"x{i}", "kind": "exit", "t": when.isoformat(), "pair": "BTC/USD",
                                    "reason": "target" if win else "stop", "config": h, **fill})
        elif i % 97 == 50:
            ledger.append(journal, {"id": f"r{i}", "kind": "refused", "t": when.isoformat(), "pair": "ETH/USD",
                                    "bar": t, "why": ["spread_wide", "kill"][: 1 + i % 2], "config": h})
        ledger.append(journal, {"id": f"c{i}", "kind": "cycle", "t": when.isoformat(), "config": h, "pairs": seen,
                                "failed": {} if i % 131 else {"SOL/USD": "HTTP 502"}, "kill": True,
                                "open": sorted(book.positions),
                                "equity": str(book.equity({k: v for k, v in price.items()}).quantize(Decimal("0.01")))})
    book.save(state / "book.json")
    (state / "KILL").write_text("fixture\n")
    import os
    stamp = (NOW - dt.timedelta(days=2)).timestamp()
    os.utime(state / "KILL", (stamp, stamp))


def build() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "crypto"
        state.mkdir()
        synthetic(state)
        desk = dataclasses.replace(DESKS["crypto"], state_dir=state, kill_file=state / "KILL",
                                   ledgers=(("crypto", state / "crypto_journal.jsonl"),),
                                   chain_flag=state / "chain-broken")
        from wt.ops import alerts, heartbeat
        alerts.ALERT_DIR = heartbeat.HEARTBEAT_DIR = Path(tmp) / "none"   # nothing from this machine's own state
        raw = snapshot.collect(NOW, desk)
        raw["jobs"] = {"last": {"crypto": {"status": "ok", "exit": 0, "started": "2026-10-02T20:00:10+00:00",
                                           "ended": "2026-10-02T20:00:19+00:00", "sha": "abc1234"},
                                "dashboard-crypto": {"status": "ok", "exit": 0, "started": "2026-10-02T19:46:00+00:00",
                                                     "ended": "2026-10-02T19:46:04+00:00", "sha": "abc1234"}},
                       "runs": []}
        raw["alerts"] = {"firing": []}
    san = publish.Sanitizer(lambda x: x, None, strict=False)
    snap = snapshot.build(raw, san, "20261002T200040Z-fixture", NOW)
    problems = snapshot.validate(snap)
    assert not problems, problems
    return snap


def main(argv: list[str]) -> int:
    text = json.dumps(build(), indent=1, sort_keys=True) + "\n"
    if "--check" in argv:
        if not FIXTURE.exists() or FIXTURE.read_text() != text:
            print(f"{FIXTURE.relative_to(ROOT)} is stale: run python scripts/gen_crypto_fixture.py")
            return 1
        return 0
    FIXTURE.write_text(text)
    print(f"wrote {FIXTURE.relative_to(ROOT)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
