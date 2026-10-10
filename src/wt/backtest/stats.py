"""Performance statistics with the plan's G1 gate metrics."""
from __future__ import annotations

import math

import numpy as np
from scipy import stats as st


BLOCKS = ("trade", "day")


def bootstrap_ci(r: np.ndarray, seed: int = 7, n_boot: int = 5000, block: str = "trade", days=None) -> list[float]:
    """Percentile 95% CI of the mean R.

    block="trade" resamples trades independently (the recorded method, kept as the default). block="day"
    (DEC-0011 M-STAT) resamples whole sessions: trades on one day share that day's market, so treating them as
    independent understates the variance of the mean. `days` labels each trade's session (same length as r)."""
    r = np.asarray(r, dtype=float)
    rng = np.random.default_rng(seed)
    if block == "trade":
        boots = rng.choice(r, size=(n_boot, len(r)), replace=True).mean(axis=1)
    elif block == "day":
        if days is None or len(days) != len(r):
            raise ValueError("block='day' needs one day label per trade")
        _, day_of = np.unique(np.asarray(days).astype(str), return_inverse=True)
        sums, counts = np.bincount(day_of, weights=r), np.bincount(day_of)
        pick = rng.integers(0, len(sums), size=(n_boot, len(sums)))
        boots = sums[pick].sum(axis=1) / counts[pick].sum(axis=1)     # trade-weighted mean of the resampled days
    else:
        raise ValueError(f"block must be one of {BLOCKS}, got {block!r}")
    return [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]


def summarize(r: np.ndarray, seed: int = 7, n_boot: int = 5000, block: str = "trade", days=None) -> dict:
    r = np.asarray(r, dtype=float)
    n = len(r)
    if n == 0:
        return {"n": 0}
    wins, losses = r[r > 0], r[r <= 0]
    ci = bootstrap_ci(r, seed=seed, n_boot=n_boot, block=block, days=days)
    eq = np.cumsum(r)
    dd = float((eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min()) if n else 0.0
    sharpe = float(r.mean() / r.std(ddof=1)) if n > 1 and r.std(ddof=1) > 0 else 0.0
    return {
        "n": n, "expectancy_R": float(r.mean()), "win_rate": float((r > 0).mean()),
        "avg_win_R": float(wins.mean()) if len(wins) else 0.0, "avg_loss_R": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else math.inf,
        "ci95_expectancy": ci,
        "max_drawdown_R": dd, "per_trade_sharpe": sharpe,
        "skew": float(st.skew(r)) if n > 2 else 0.0, "kurtosis": float(st.kurtosis(r, fisher=False)) if n > 3 else 3.0,
    } | ({"ci95_block": block} if block != "trade" else {})    # recorded summaries keep their exact keys


def deflated_sharpe_prob(sr: float, n: int, n_trials: int, skew: float, kurt: float, sr_var_trials: float = None) -> float:
    """Bailey & Lopez de Prado (2014) Deflated Sharpe Ratio probability (per-trade Sharpe)."""
    if n < 3 or n_trials < 1:
        return 0.0
    emc = 0.5772156649
    v = sr_var_trials if sr_var_trials else 1.0 / n
    sr0 = math.sqrt(v) * ((1 - emc) * st.norm.ppf(1 - 1 / n_trials) + emc * st.norm.ppf(1 - 1 / (n_trials * math.e))) if n_trials > 1 else 0.0
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    return float(st.norm.cdf((sr - sr0) * math.sqrt(n - 1) / denom))


def random_control_pvalue(real_mean: float, control_means: np.ndarray) -> float:
    return float((1 + (control_means >= real_mean).sum()) / (1 + len(control_means)))
