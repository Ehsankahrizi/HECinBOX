"""TEEHR-inspired evaluation utilities for HECinBOX (v3.1.0).

This module adds rigorous, standardized verification on top of the
basic goodness-of-fit metrics in :class:`visualization.Metrics`.  It is
inspired by RTI's **TEEHR** toolkit
(https://rtiinternational.github.io/teehr/): we reimplement the
*methods* - Kling-Gupta efficiency, bootstrapped confidence intervals,
and CRPS ensemble verification - as a lightweight, NumPy-only layer
rather than pulling in TEEHR's Apache Spark / Iceberg stack, which
would be far too heavy for the container.

Three capabilities:

1. **Kling-Gupta Efficiency (KGE)** and its decomposition
   ``(r, alpha, beta)`` - the modern hydrologic skill metric, reported
   alongside NSE.
2. **Bootstrapped confidence intervals** - quantify the *uncertainty*
   of each metric with a moving-block bootstrap that respects the
   serial correlation of hydrologic time series.
3. **CRPS** (Continuous Ranked Probability Score) - verification for
   *ensemble* forecasts (e.g. the 7-member NWM medium-range ensemble),
   so HECinBOX can score the spread of an ensemble, not just one
   member.

Everything here is pure NumPy + (optionally) pandas; no heavy deps.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Sequence, Tuple

import numpy as np


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _finite_pairs(
    model: Sequence[float], observed: Sequence[float]
) -> Tuple[np.ndarray, np.ndarray]:
    """Return aligned, finite-only ``(model, observed)`` arrays."""
    m = np.asarray(model, dtype=float)
    o = np.asarray(observed, dtype=float)
    mask = np.isfinite(m) & np.isfinite(o)
    return m[mask], o[mask]


# ----------------------------------------------------------------------
# Kling-Gupta Efficiency  (Gupta et al., 2009)
# ----------------------------------------------------------------------
def kling_gupta_efficiency(
    model: Sequence[float], observed: Sequence[float]
) -> Tuple[float, float, float, float]:
    """Return ``(KGE, r, alpha, beta)``.

    ``KGE = 1 - sqrt((r-1)^2 + (alpha-1)^2 + (beta-1)^2)``

    * ``r``     - Pearson correlation (timing / shape agreement)
    * ``alpha`` - ``std(model) / std(observed)`` (variability ratio)
    * ``beta``  - ``mean(model) / mean(observed)`` (bias ratio)

    Perfect skill = 1.  Note: ``beta`` (a *ratio* of means) is designed
    for zero-bounded variables like streamflow.  For a **stage** series
    referenced to an arbitrary vertical datum (e.g. ~628 ft NGVD29),
    the mean is large and ``beta`` is always ≈1, so KGE is then driven
    almost entirely by ``r`` and ``alpha`` - interpret accordingly.
    """
    m, o = _finite_pairs(model, observed)
    if m.size < 2:
        return float("nan"), float("nan"), float("nan"), float("nan")

    m_mean, o_mean = float(m.mean()), float(o.mean())
    m_std, o_std = float(m.std(ddof=0)), float(o.std(ddof=0))

    num = float(np.sum((m - m_mean) * (o - o_mean)))
    den = float(np.sqrt(np.sum((m - m_mean) ** 2) * np.sum((o - o_mean) ** 2)))
    r = num / den if den > 0 else float("nan")
    alpha = (m_std / o_std) if o_std > 0 else float("nan")
    beta = (m_mean / o_mean) if o_mean != 0 else float("nan")

    if not (np.isfinite(r) and np.isfinite(alpha) and np.isfinite(beta)):
        return float("nan"), r, alpha, beta
    kge = 1.0 - float(
        np.sqrt((r - 1.0) ** 2 + (alpha - 1.0) ** 2 + (beta - 1.0) ** 2)
    )
    return kge, r, alpha, beta


# ----------------------------------------------------------------------
# Scalar metric functions (operate on already-paired finite arrays)
# ----------------------------------------------------------------------
def _nse(m: np.ndarray, o: np.ndarray) -> float:
    o_mean = o.mean()
    ss_tot = float(np.sum((o - o_mean) ** 2))
    if ss_tot <= 0:
        return float("nan")
    return 1.0 - float(np.sum((m - o) ** 2)) / ss_tot


def _rmse(m: np.ndarray, o: np.ndarray) -> float:
    return float(np.sqrt(np.mean((m - o) ** 2)))


def _mae(m: np.ndarray, o: np.ndarray) -> float:
    return float(np.mean(np.abs(m - o)))


def _bias(m: np.ndarray, o: np.ndarray) -> float:
    return float(np.mean(m - o))


def _pearson(m: np.ndarray, o: np.ndarray) -> float:
    m_mean, o_mean = m.mean(), o.mean()
    num = float(np.sum((m - m_mean) * (o - o_mean)))
    den = float(np.sqrt(np.sum((m - m_mean) ** 2) * np.sum((o - o_mean) ** 2)))
    return num / den if den > 0 else float("nan")


def _kge(m: np.ndarray, o: np.ndarray) -> float:
    return kling_gupta_efficiency(m, o)[0]


# Registry used by the bootstrap.  Order = display order.
METRIC_FNS: Dict[str, Callable[[np.ndarray, np.ndarray], float]] = {
    "NSE": _nse,
    "KGE": _kge,
    "RMSE": _rmse,
    "MAE": _mae,
    "Bias": _bias,
    "Pearson r": _pearson,
}


# ----------------------------------------------------------------------
# Moving-block bootstrap confidence intervals
# ----------------------------------------------------------------------
def bootstrap_cis(
    model: Sequence[float],
    observed: Sequence[float],
    n_boot: int = 1000,
    ci: float = 0.95,
    block_len: int | None = None,
    seed: int = 0,
) -> Dict[str, Tuple[float, float]]:
    """Return ``{metric: (lo, hi)}`` confidence intervals.

    Uses a **moving-block bootstrap**: instead of resampling individual
    timesteps (which assumes independence and badly *under*-estimates
    the CI width for autocorrelated hydrographs), it resamples
    contiguous blocks of length ``block_len`` and stitches them back to
    length ``n``.  This preserves short-range serial correlation, so
    the resulting intervals are honest.

    ``block_len`` defaults to ``round(n ** (1/3))`` - a standard rule of
    thumb (Hall, Horowitz & Jing, 1995).
    """
    m, o = _finite_pairs(model, observed)
    n = m.size
    if n < 4:
        return {k: (float("nan"), float("nan")) for k in METRIC_FNS}

    if block_len is None:
        block_len = max(1, int(round(n ** (1.0 / 3.0))))
    block_len = max(1, min(block_len, n))

    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block_len))
    start_hi = n - block_len  # inclusive upper bound for a block start

    samples: Dict[str, List[float]] = {k: [] for k in METRIC_FNS}
    for _ in range(int(n_boot)):
        starts = rng.integers(0, start_hi + 1, size=n_blocks)
        idx = np.concatenate(
            [np.arange(s, s + block_len) for s in starts]
        )[:n]
        mb, ob = m[idx], o[idx]
        for k, fn in METRIC_FNS.items():
            try:
                samples[k].append(fn(mb, ob))
            except Exception:
                samples[k].append(np.nan)

    lo_q = (1.0 - ci) / 2.0 * 100.0
    hi_q = (1.0 + ci) / 2.0 * 100.0
    out: Dict[str, Tuple[float, float]] = {}
    for k, vals in samples.items():
        arr = np.asarray(vals, dtype=float)
        arr = arr[np.isfinite(arr)]
        if arr.size:
            out[k] = (
                float(np.percentile(arr, lo_q)),
                float(np.percentile(arr, hi_q)),
            )
        else:
            out[k] = (float("nan"), float("nan"))
    return out


# ----------------------------------------------------------------------
# CRPS - ensemble verification (Hersbach, 2000 empirical estimator)
# ----------------------------------------------------------------------
def crps_ensemble(
    ensemble: np.ndarray, observed: Sequence[float]
) -> Tuple[float, np.ndarray]:
    """Continuous Ranked Probability Score for an ensemble forecast.

    Parameters
    ----------
    ensemble : array, shape ``(n_times, n_members)``
        One column per ensemble member (e.g. NWM medium-range members
        1-7), one row per timestep.
    observed : array, shape ``(n_times,)``
        The verifying observation at each timestep.

    Returns
    -------
    ``(mean_crps, per_time_crps)`` - lower is better.  CRPS reduces to
    the Mean Absolute Error when the ensemble collapses to a single
    deterministic member, so it is directly comparable in the data's
    own units.

    Uses the empirical estimator
    ``CRPS = E|X - y| - 0.5 * E|X - X'|`` (Hersbach, 2000), where ``X``
    and ``X'`` are independent draws from the ensemble.
    """
    ens = np.asarray(ensemble, dtype=float)
    if ens.ndim == 1:
        ens = ens[:, None]
    obs = np.asarray(observed, dtype=float)
    n = ens.shape[0]

    per_time = np.full(n, np.nan)
    for t in range(n):
        x = ens[t]
        x = x[np.isfinite(x)]
        y = obs[t]
        if x.size == 0 or not np.isfinite(y):
            continue
        term1 = float(np.mean(np.abs(x - y)))
        # mean over the full m×m matrix == (1/m^2) ΣΣ |xi - xj|
        term2 = float(np.mean(np.abs(x[:, None] - x[None, :])))
        per_time[t] = term1 - 0.5 * term2

    valid = per_time[np.isfinite(per_time)]
    mean_crps = float(valid.mean()) if valid.size else float("nan")
    return mean_crps, per_time


def crps_from_member_frames(
    frames: Sequence["object"],
    model_col: str = "model",
    obs_col: str = "observed",
) -> dict:
    """Compute CRPS from a list of per-member comparison DataFrames.

    Each frame is a ``model_vs_observed`` export (time-indexed, with a
    ``model`` and an ``observed`` column) from one ensemble-member run.
    Frames are aligned on the **intersection** of their timestamps; the
    ensemble matrix is built from the per-member ``model`` columns and
    verified against the ``observed`` column (taken as the mean across
    members at each time, since they should be identical).

    Returns a dict with ``mean_crps``, ``n_times``, ``n_members``,
    ``per_time`` (list) and ``index`` (list of ISO timestamps).
    """
    import pandas as pd

    cols = []
    obs_cols = []
    common_index = None
    for fr in frames:
        if model_col not in fr.columns or obs_col not in fr.columns:
            continue
        s = fr[model_col].dropna()
        cols.append(s)
        obs_cols.append(fr[obs_col])
        common_index = (
            s.index if common_index is None
            else common_index.intersection(s.index)
        )

    if not cols or common_index is None or len(common_index) == 0:
        return {
            "mean_crps": float("nan"),
            "n_times": 0,
            "n_members": len(cols),
            "per_time": [],
            "index": [],
        }

    common_index = common_index.sort_values()
    ens = np.column_stack(
        [s.reindex(common_index).to_numpy(dtype=float) for s in cols]
    )
    obs = (
        pd.concat([c.reindex(common_index) for c in obs_cols], axis=1)
        .mean(axis=1)
        .to_numpy(dtype=float)
    )

    mean_crps, per_time = crps_ensemble(ens, obs)
    return {
        "mean_crps": mean_crps,
        "n_times": int(len(common_index)),
        "n_members": int(ens.shape[1]),
        "per_time": [None if not np.isfinite(v) else float(v) for v in per_time],
        "index": [str(ts) for ts in common_index],
    }
