"""The learning job (DEC-0016, 2 to 4): once a day, in this order.

    python -m wt.crypto.learn            (the daily job `crypto-learn`)

  1. outcomes   every recorded signal is followed to its result with its sleeve's own exits and costs
  2. the tests  at a checkpoint (every 60 finished signals) the model in force is promoted, held or demoted;
                a drift in its inputs suspends it
  3. retrain    once a week the models are trained and compared again, in the machine-learning environment,
                by the method the charter fixes. A model that beats taking every signal is registered in
                shadow. If none does, there is no model in force and the desk says so.

This process is the trading side: it imports no machine-learning library. Training runs as a separate process
under a time limit, and its failure changes nothing on the desk. The owner's switch stops steps 2 and 3;
outcomes are still recorded, because they are a record and not an action.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from wt.core.config import ROOT, load_yaml
from wt.core.desk import DESKS, Desk
from wt.crypto import outcomes, promotion, risk
from wt.crypto.book import write_atomic
from wt.crypto.data import Bar

DAY = 86_400
ML_PYTHON = ".venv-ml/bin/python"
RETRAIN_DAYS = 7
TRAIN_TIMEOUT_S = 75 * 60


def last_train(desk: Desk) -> dict[str, Any]:
    try:
        got = json.loads((promotion.models_dir(desk) / "last_train.json").read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def retrain_due(desk: Desk, now: float) -> bool:
    try:
        last = dt.datetime.fromisoformat(str(last_train(desk).get("t"))).timestamp()
    except ValueError:
        return True
    return now - last >= RETRAIN_DAYS * DAY - 3600          # an hour's grace: the job starts at the same minute each day


def train(desk: Desk, root: Path = ROOT, python: str | Path | None = None, timeout_s: float = TRAIN_TIMEOUT_S) -> tuple[dict[str, Any] | None, str]:
    """Run the trainer in the ML environment. Returns (its summary, "") or (None, why not)."""
    exe = Path(python) if python is not None else root / ML_PYTHON
    if not exe.exists():
        return None, "no_environment"
    models = promotion.models_dir(desk)
    summary = models / "train-run.json"
    summary.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
    env.update(PYTHONPATH=str(root / "src"), PYTHONDONTWRITEBYTECODE="1", MODE="backtest")
    try:
        r = subprocess.run([str(exe), "-m", "wt.ml.train", "--register", "--summary", str(summary)], env=env, cwd=root,
                           capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as e:
        return None, f"failed ({e.__class__.__name__})"
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
    print(tail)
    if r.returncode != 0:
        return None, f"exit {r.returncode}"
    try:
        got = json.loads(summary.read_text())
    except (OSError, ValueError):
        return None, "no summary"
    return (got, "") if isinstance(got, dict) else (None, "no summary")


def retire_pointer(desk: Desk, now: float) -> bool:
    """A retraining found no model that beats taking every signal: none is in force any more."""
    cur = promotion.models_dir(desk) / "current.json"
    if not cur.exists():
        return False
    cur.rename(cur.with_name(f"retired-{dt.datetime.fromtimestamp(now, dt.UTC):%Y%m%dT%H%M%S}.json"))
    return True


def _load(pair: str, start: int, end: int) -> list[Bar]:
    from wt.crypto import history
    from wt.crypto.data import CoinbasePublic
    return history.load_hourly(pair, start, end, CoinbasePublic())


def run(now: float | None = None, desk: Desk | None = None, cfg: dict[str, Any] | None = None, alerts: Any = None,
        load: Any = None, trainer: Any = None) -> int:
    from wt.ops.alerts import Alerts
    now = dt.datetime.now(dt.UTC).timestamp() if now is None else now
    desk, cfg = desk or DESKS["crypto"], cfg or load_yaml("crypto.yaml")
    alerts = alerts or Alerts()
    if desk.chain_flag.exists():
        print("Refusing: the crypto journal's hash chain is flagged broken")
        return 2
    code = 0

    # 1. Outcomes. A pair whose history cannot be read today is followed tomorrow.
    rows = outcomes.read_journal(desk)
    try:
        found = outcomes.run(desk, cfg, now, rows, load or _load)
        print(f"outcomes: {len(found)} signals finished, {len(outcomes.pending(rows)) - len(found)} still open")
        rows += found
    except Exception as e:  # noqa: BLE001 — the tests below still run on what is already recorded
        print(f"outcomes failed ({e.__class__.__name__}: {e})", file=sys.stderr)
        code = 1

    if risk.learning_file(desk).exists():
        print("learning is switched off: no test and no retraining")
        return code

    # 2. The checkpoint tests, on the model that scored the signals.
    code = max(code, judge(desk, cfg, rows, now, alerts))

    # 3. The weekly retraining.
    if retrain_due(desk, now):
        summary, why = (trainer or train)(desk)
        if summary is None:
            print(f"retraining did not run: {why}")
            if why != "no_environment":
                alerts.fire("crypto:learn-train-failed", "Crypto: the weekly model training failed",
                            f"{why}. The model in force is unchanged; trading is unaffected.", 3)
                code = 1
        else:
            alerts.resolve("crypto:learn-train-failed", "Crypto: the model training runs again", "The fault has cleared.")
            models = promotion.models_dir(desk)
            models.mkdir(parents=True, exist_ok=True)
            # When this run started, not when training ended: the next one is due a week from the job's own hour.
            summary["t"] = dt.datetime.fromtimestamp(now, dt.UTC).isoformat(timespec="seconds")
            write_atomic(models / "last_train.json", json.dumps(summary, indent=1, sort_keys=True))
            if summary.get("chosen") is None and retire_pointer(desk, now):
                print("no model beat taking every signal: none is in force")
            print(f"retrained: chosen {summary.get('chosen')} on {summary.get('examples')} signals")
            code = max(code, judge(desk, cfg, outcomes.read_journal(desk), now, alerts))    # a new lineage starts in shadow
    return code


def judge(desk: Desk, cfg: dict[str, Any], rows: list[dict[str, Any]], now: float, alerts: Any) -> int:
    """Run the promotion state machine and record what changed. The state file is written after the journal:
    a crash between the two repeats the events, never loses them."""
    try:
        state, events = promotion.evaluate(desk, cfg, rows, now)
        for e in events:
            outcomes.append(desk, e)
            print(f"model {e['event']}: {e.get('lineage')} {({k: e[k] for k in ('checkpoint', 'spread', 'lower') if k in e})}")
            if e["event"] in ("promoted", "demoted", "suspended"):
                text = {"promoted": "passed its promotion test and now acts: it skips or halves weak signals of the three "
                                    "registered sleeves. It never adds or enlarges a trade.",
                        "demoted": "failed its rolling test and no longer acts. Signals are traded as their rules say.",
                        "suspended": "is suspended: recent signals no longer look like its training data. It acts "
                                     "again after the next retraining."}[e["event"]]
                alerts.once_per_day(f"crypto:model-{e['event']}", f"Crypto: the model {e['event']}", f"{e.get('lineage')} {text}", 3)
        promotion.save_state(desk, state)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"the model's tests failed ({e.__class__.__name__}: {e})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
