"""Bias correction of National Water Model streamflow at a boundary.

NWM streamflow can carry a systematic volume or peak bias at a given
reach (often an underestimate of flood peaks).  A hydraulic model fed
that flow cannot recover water that is missing from its inflow, and
compensating with Manning's n gives physically wrong roughness.  So
the correction is applied to the boundary flow itself, before it is
written to DSS.

The correction is trained by comparing, at the boundary reach, the
NWM v3.0 Retrospective simulation (hourly, 1979-02 to 2023-01, not
nudged to gauges) with a USGS gauge on the same reach: hourly means of
its instantaneous record where there is at least a year of it (flood
peaks of small, flashy reaches only show at that scale), otherwise its
daily means.

Methods
-------
* ``monthly``  - one multiplier per calendar month, mean(obs)/mean(NWM),
                 the volumetric adjustment of Kilicarslan et al. (2026,
                 J. Hydrol. Reg. Stud.), after Rahman et al. (2019).
* ``quantile`` - empirical quantile mapping: each NWM value is moved to
                 the observed flow with the same non-exceedance
                 probability, so low flows and flood peaks get their
                 own correction.
* ``anchor``   - optional, on top of either: the ratio between the
                 latest gauge reading and the (corrected) NWM value at
                 the forecast start, fading out with lead time
                 (e-folding time ``anchor_tau_h``).

Leakage guard: training days never fall inside the simulation window
(plus a 7-day buffer), so a hindcast inside the Retrospective period
is never scored against data it was trained on.

Training results are cached on disk per (COMID, gauge, period) so only
the first run for a boundary pays for the download.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests

RETRO_ZARR = "noaa-nwm-retrospective-3-0-pds/CONUS/zarr/chrtout.zarr"
RETRO_START = pd.Timestamp("1979-02-01")
RETRO_END = pd.Timestamp("2023-01-31 23:00")
USGS_DV_URL = "https://waterservices.usgs.gov/nwis/dv/"
USGS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
CFS_TO_CMS = 0.028316846592
METHODS = ("none", "monthly", "quantile")
# Probability grid for quantile mapping (non-exceedance, %).
_P_GRID = np.concatenate([
    np.arange(0.5, 10, 0.5), np.arange(10, 90, 1.0),
    np.arange(90, 99.5, 0.25), [99.5, 99.75, 99.9, 99.95, 99.99],
])
_LEAK_BUFFER = pd.Timedelta(days=7)
# Raw (uncorrected) NWM series of the last correction per BC hdf_key,
# so the pipeline can save it next to the corrected one.
LAST_RAW: dict[str, pd.DataFrame] = {}
# ... and the corrected flow (m³/s).  For a stage boundary the series
# that reaches the model is stage (via a rating curve), so the flow
# itself has to be kept separately for the Tab 6 inflow check.
LAST_CORR: dict[str, pd.DataFrame] = {}


def _cache_dir() -> Path:
    base = os.environ.get("HECINBOX_CACHE_DIR") or (
        Path.home() / ".cache" / "hecinbox"
    )
    d = Path(base) / "nwm_bias"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Data access ───────────────────────────────────────────────────────
def fetch_retro_cms(
    comid: int, start: pd.Timestamp, end: pd.Timestamp,
) -> pd.Series:
    """Hourly NWM v3.0 Retrospective streamflow (m³/s) at a reach."""
    cache = _cache_dir() / (
        f"retro_h_{comid}_{start:%Y%m%d}_{end:%Y%m%d}.csv.gz"
    )
    if cache.exists():
        s = pd.read_csv(cache, index_col=0, parse_dates=True).iloc[:, 0]
        return s.rename("nwm")

    import s3fs
    import xarray as xr

    fs = s3fs.S3FileSystem(anon=True)
    ds = xr.open_zarr(s3fs.S3Map(RETRO_ZARR, s3=fs), consolidated=True)
    fid = ds["feature_id"].values
    hit = np.nonzero(fid == int(comid))[0]
    if hit.size == 0:
        raise ValueError(
            f"COMID {comid} is not in the NWM v3.0 Retrospective."
        )
    q = ds["streamflow"].isel(feature_id=int(hit[0])).sel(
        time=slice(start, end)
    )
    s = q.to_series().astype(float).dropna().rename("nwm")
    s.index = pd.to_datetime(s.index)
    s.to_csv(cache)
    return s


def _usgs_iv_year(site: str, a: pd.Timestamp, b: pd.Timestamp):
    """One year of USGS instantaneous discharge as hourly means (cfs).
    Returns None when every attempt failed (vs. an empty series when
    the gauge simply has no record then)."""
    for attempt in range(4):
        try:
            r = requests.get(
                USGS_IV_URL,
                params={
                    "format": "json", "sites": site,
                    "parameterCd": "00060", "siteStatus": "all",
                    "startDT": f"{a:%Y-%m-%dT%H:%MZ}",
                    "endDT": f"{b:%Y-%m-%dT%H:%MZ}",
                },
                timeout=180,
            )
            r.raise_for_status()
            rows = []
            for ts in r.json().get("value", {}).get("timeSeries", []):
                for block in ts.get("values", []):
                    for it in block.get("value", []):
                        rows.append((it.get("dateTime"), it.get("value")))
            if not rows:
                return pd.Series(dtype=float)
            df = pd.DataFrame(rows, columns=["t", "q"])
            df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_localize(None)
            df["q"] = pd.to_numeric(df["q"], errors="coerce")
            df = df[df["q"] >= 0].dropna()
            return df.set_index("t")["q"].resample("1h").mean().dropna()
        except Exception as e:
            print(f"nwm_bias: USGS {site} hourly {a:%Y} "
                  f"(try {attempt + 1}/4): {e}")
            time.sleep(3 * (attempt + 1))
    return None


def fetch_usgs_hourly_cms(
    site: str, start: pd.Timestamp, end: pd.Timestamp,
) -> pd.Series:
    """Observed hourly-mean discharge (m³/s, UTC) from USGS instantaneous
    values, requested one year at a time (a few in parallel).  Empty
    where the gauge has no instantaneous record (common before ~2007).
    Cached only when every year downloaded."""
    from concurrent.futures import ThreadPoolExecutor

    cache = _cache_dir() / (
        f"usgs_h_{site}_{start:%Y%m%d}_{end:%Y%m%d}.csv.gz"
    )
    if cache.exists():
        s = pd.read_csv(cache, index_col=0, parse_dates=True).iloc[:, 0]
        return s.rename("obs")
    spans = []
    a = pd.Timestamp(start)
    while a <= end:
        b = min(a + pd.DateOffset(years=1) - pd.Timedelta(hours=1), end)
        spans.append((a, b))
        a = b + pd.Timedelta(hours=1)
    with ThreadPoolExecutor(max_workers=4) as ex:
        parts = list(ex.map(lambda ab: _usgs_iv_year(site, *ab), spans))
    complete = all(p is not None for p in parts)
    parts = [p for p in parts if p is not None and len(p)]
    s = (pd.concat(parts) * CFS_TO_CMS) if parts else pd.Series(
        dtype=float)
    s = s.rename("obs")
    if complete:
        s.to_csv(cache)
    else:
        print(f"nwm_bias: USGS {site}: some years failed to download; "
              f"training on the rest (not cached).")
    return s


def fetch_usgs_daily_cms(
    site: str, start: pd.Timestamp, end: pd.Timestamp,
) -> pd.Series:
    """Observed daily-mean discharge (m³/s) for a USGS gauge."""
    for attempt in range(4):
        try:
            r = requests.get(
                USGS_DV_URL,
                params={
                    "format": "json", "sites": site,
                    "parameterCd": "00060", "statCd": "00003",
                    "siteStatus": "all",
                    "startDT": f"{start:%Y-%m-%d}",
                    "endDT": f"{end:%Y-%m-%d}",
                },
                timeout=120,
            )
            r.raise_for_status()
            break
        except Exception as e:
            if attempt == 3:
                raise
            print(f"nwm_bias: USGS {site} daily values (try "
                  f"{attempt + 1}/4): {e}")
            time.sleep(5 * (attempt + 1))
    rows = []
    for ts in r.json().get("value", {}).get("timeSeries", []):
        for block in ts.get("values", []):
            for it in block.get("value", []):
                rows.append((it.get("dateTime"), it.get("value")))
    if not rows:
        raise ValueError(f"USGS {site}: no daily discharge in the period.")
    df = pd.DataFrame(rows, columns=["t", "q"])
    df["t"] = pd.to_datetime(df["t"].str[:10])
    df["q"] = pd.to_numeric(df["q"], errors="coerce")
    df = df[df["q"] >= 0].dropna()
    return (df.set_index("t")["q"] * CFS_TO_CMS).rename("obs")


def latest_usgs_cms(
    site: str, at: pd.Timestamp, window_h: int = 6,
) -> Optional[float]:
    """Latest gauge discharge (m³/s) at or before ``at`` (UTC, naive).

    Never a reading after ``at``: in a replayed (hindcast) forecast that
    would hand the correction data the forecaster could not have had.
    """
    try:
        r = requests.get(
            USGS_IV_URL,
            params={
                "format": "json", "sites": site, "parameterCd": "00060",
                "siteStatus": "all",
                "startDT": (at - pd.Timedelta(hours=window_h)).strftime(
                    "%Y-%m-%dT%H:%MZ"),
                "endDT": at.strftime("%Y-%m-%dT%H:%MZ"),
            },
            timeout=60,
        )
        r.raise_for_status()
        rows = []
        for ts in r.json().get("value", {}).get("timeSeries", []):
            for block in ts.get("values", []):
                for it in block.get("value", []):
                    rows.append((it.get("dateTime"), it.get("value")))
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["t", "q"])
        df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_localize(None)
        df["q"] = pd.to_numeric(df["q"], errors="coerce")
        df = df[df["q"] >= 0].dropna()
        df = df[df["t"] <= at]
        if df.empty:
            return None
        return float(df.sort_values("t")["q"].iloc[-1]) * CFS_TO_CMS
    except Exception as e:
        print(f"nwm_bias: latest gauge reading failed ({e}).")
        return None


# ── Training ──────────────────────────────────────────────────────────
def _training_days(
    obs: pd.Series, sim_start, sim_end, years: int,
) -> pd.Series:
    """Observed days used for training: inside the Retrospective, never
    within the simulation window (+/- 7 days), the latest ``years``
    worth of gauged days.  Gauges with gaps simply reach further back."""
    excl0 = (pd.Timestamp(sim_start) - _LEAK_BUFFER).normalize()
    excl1 = pd.Timestamp(sim_end) + _LEAK_BUFFER
    o = obs[(obs.index >= RETRO_START) & (obs.index <= RETRO_END)]
    o = o[(o.index < excl0) | (o.index > excl1)]
    return o.iloc[-int(365.25 * years):]


def _segments(idx: pd.DatetimeIndex, gap_days: int = 60):
    """Contiguous (start, end) spans of a daily index, split at gaps."""
    if len(idx) == 0:
        return []
    brk = np.nonzero(np.diff(idx.values).astype("timedelta64[D]")
                     .astype(int) > gap_days)[0]
    starts = [idx[0]] + [idx[i + 1] for i in brk]
    ends = [idx[i] for i in brk] + [idx[-1]]
    return list(zip(starts, ends))


def build_correction(
    comid: int, site: str, sim_start, sim_end, years: int = 10,
) -> dict:
    """Fit monthly and quantile-mapping corrections for one boundary."""
    obs_all = fetch_usgs_daily_cms(str(site), RETRO_START, RETRO_END)
    obs = _training_days(obs_all, sim_start, sim_end, years)
    if len(obs) < 365:
        raise ValueError(
            f"USGS {site} has only {len(obs)} daily values inside the "
            f"NWM Retrospective period (1979-2023) outside this run's "
            f"window; need at least a year."
        )
    t0, t1 = obs.index.min(), obs.index.max()
    key = _cache_dir() / (
        f"fit2_{comid}_{site}_{t0:%Y%m%d}_{t1:%Y%m%d}_{len(obs)}.json"
    )
    if key.exists():
        return json.loads(key.read_text())

    segs = _segments(obs.index)
    nwm_h = pd.concat([
        fetch_retro_cms(int(comid), a, b + pd.Timedelta(hours=23))
        for a, b in segs
    ])
    # Hourly training where the gauge has an instantaneous record:
    # flood peaks on small, flashy reaches only show at hourly scale,
    # and the correction is applied to hourly forecasts.
    obs_h = pd.concat([
        fetch_usgs_hourly_cms(str(site), a, b + pd.Timedelta(hours=23))
        for a, b in segs
    ])
    keep_days = set(obs.index.normalize())
    df_h = pd.concat([nwm_h, obs_h], axis=1).dropna()
    df_h = df_h[df_h.index.normalize().isin(keep_days)]
    if len(df_h) >= 365 * 24:
        df, resolution = df_h, "hourly"
    else:
        nwm_d = nwm_h.resample("1D").mean().dropna()
        df, resolution = pd.concat([nwm_d, obs], axis=1).dropna(), "daily"
    n_days = int(df.index.normalize().nunique())
    if n_days < 365:
        raise ValueError(
            f"only {n_days} paired days of NWM and USGS {site}; "
            f"need at least a year."
        )

    monthly = {}
    for m in range(1, 13):
        g = df[df.index.month == m]
        if g.index.normalize().nunique() >= 20 and g["nwm"].mean() > 0:
            monthly[m] = float(g["obs"].mean() / g["nwm"].mean())
        else:
            monthly[m] = 1.0
    pbias = float(100 * (df["nwm"].sum() - df["obs"].sum())
                  / df["obs"].sum())
    fit = {
        "comid": int(comid), "site": str(site),
        "train_start": f"{df.index.min():%Y-%m-%d}",
        "train_end": f"{df.index.max():%Y-%m-%d}",
        "n_days": n_days,
        "resolution": resolution,
        "pbias_raw_pct": round(pbias, 2),
        "monthly_factor": {str(k): round(v, 4) for k, v in monthly.items()},
        "qm_p": _P_GRID.round(3).tolist(),
        "qm_nwm": np.percentile(df["nwm"], _P_GRID).round(4).tolist(),
        "qm_obs": np.percentile(df["obs"], _P_GRID).round(4).tolist(),
    }
    key.write_text(json.dumps(fit))
    return fit


# ── Applying ──────────────────────────────────────────────────────────
def _qm(values: np.ndarray, fit: dict) -> np.ndarray:
    xn = np.asarray(fit["qm_nwm"], float)
    xo = np.asarray(fit["qm_obs"], float)
    # Keep the mapping monotone where NWM quantiles tie (zero flows).
    xn = np.maximum.accumulate(xn + np.arange(len(xn)) * 1e-9)
    out = np.interp(values, xn, xo)
    # Beyond the fitted range keep the end ratios (no hard clamp).
    hi, lo = values > xn[-1], values < xn[0]
    if xn[-1] > 0:
        out[hi] = values[hi] * (xo[-1] / xn[-1])
    if xn[0] > 0:
        out[lo] = values[lo] * (xo[0] / xn[0])
    return np.maximum(out, 0.0)


def apply_correction(
    q_df: pd.DataFrame, fit: dict, method: str,
    anchor: bool = False, anchor_tau_h: float = 24.0,
    ref_time=None,
) -> tuple[pd.DataFrame, str]:
    """Correct an NWM (datetime, value[m³/s]) frame; return it + a note."""
    out = q_df.copy()
    v = out["value"].to_numpy(float)
    if method == "monthly":
        f = np.array([
            fit["monthly_factor"][str(m)]
            for m in pd.to_datetime(out["datetime"]).dt.month
        ])
        v = v * f
        note = f"monthly x{f.min():.2f}-{f.max():.2f}"
    elif method == "quantile":
        v = _qm(v, fit)
        note = "quantile mapping"
    else:
        note = "none"
    if anchor and len(v):
        # Anchor at the forecast's issue time (a replayed cycle) or its
        # first value, using only readings available by then.
        t0 = pd.Timestamp(ref_time if ref_time is not None
                          else out["datetime"].iloc[0])
        q_obs = latest_usgs_cms(fit["site"], t0)
        if q_obs is not None and v[0] > 0:
            r0 = q_obs / v[0]
            lead_h = (
                pd.to_datetime(out["datetime"]) - t0
            ).dt.total_seconds().to_numpy() / 3600.0
            v = v * (1.0 + (r0 - 1.0) * np.exp(-lead_h / anchor_tau_h))
            note += f" + anchor x{r0:.2f} (tau {anchor_tau_h:g} h)"
        else:
            note += " + anchor skipped (no gauge reading)"
    out["value"] = np.maximum(v, 0.0)
    return out, note


def _manual_correction(q_df: pd.DataFrame, bc_config: dict) -> pd.DataFrame:
    """User-set correction: ``Q = factor * Q_NWM + offset`` (m³/s).

    For users who already know, from experience at their site, that NWM
    runs (say) 20 % low, or a fixed amount high.  No gauge or training
    data is involved; negative results are clipped to zero.  Applies to
    every NWM product, ``analysis_assim`` included, because it is the
    user's own call.
    """
    try:
        factor = float(bc_config.get("nwm_bias_factor", 1.0))
    except (TypeError, ValueError):
        factor = 1.0
    try:
        offset = float(bc_config.get("nwm_bias_offset_cms", 0.0))
    except (TypeError, ValueError):
        offset = 0.0
    out = q_df.copy()
    out["value"] = np.maximum(
        factor * out["value"].to_numpy(float) + offset, 0.0)
    pct = 100 * (out["value"].sum() / max(q_df["value"].sum(), 1e-9) - 1)
    unit = str(bc_config.get("nwm_bias_offset_unit") or "m³/s")
    shown = bc_config.get("nwm_bias_offset_shown", offset)
    summary = (
        f"manual: Q x {factor:g} {'+' if float(shown) >= 0 else '-'} "
        f"{abs(float(shown)):g} {unit}; volume change {pct:+.1f}%"
    )
    print(f"nwm_bias: {summary}")
    bc_config["_nwm_bias_note"] = summary
    LAST_RAW[str(bc_config.get("hdf_key", ""))] = q_df
    LAST_CORR[str(bc_config.get("hdf_key", ""))] = out
    return out


def correct_nwm(
    q_df: Optional[pd.DataFrame], bc_config: dict, sim_start, sim_end,
) -> Optional[pd.DataFrame]:
    """Pipeline hook: correct NWM Q per the BC's ``nwm_bias_*`` keys.

    Never raises: on any failure the raw NWM series is returned and the
    reason is logged.  The outcome is written back on ``bc_config`` as
    ``_nwm_bias_note`` so the run summary can report it.
    """
    method = str(bc_config.get("nwm_bias_method", "none")).lower()
    if q_df is None or q_df.empty or method not in (
        "monthly", "quantile", "manual",
    ):
        return q_df
    if method == "manual":
        return _manual_correction(q_df, bc_config)
    if str(bc_config.get("forecast_horizon")) == "analysis_assim":
        # The nowcast is already nudged toward USGS gauges; a correction
        # trained on the un-nudged Retrospective would undo that.
        print("nwm_bias: skipped for analysis_assim (already nudged to "
              "gauges) - using it as is.")
        bc_config["_nwm_bias_note"] = (
            "skipped: analysis_assim is already nudged to gauges")
        return q_df
    site = str(bc_config.get("nwm_bias_gauge", "")).strip()
    comid = bc_config.get("comid")
    if not site or not comid:
        print("nwm_bias: needs both the COMID and a USGS gauge - "
              "using raw NWM.")
        bc_config["_nwm_bias_note"] = "requested but no gauge - raw NWM"
        return q_df
    try:
        fit = build_correction(
            int(comid), site, sim_start, sim_end,
            int(bc_config.get("nwm_bias_years", 10) or 10),
        )
        _it = bc_config.get("forecast_issue_time") or None
        out, note = apply_correction(
            q_df, fit, method,
            anchor=bool(bc_config.get("nwm_bias_anchor", False)),
            anchor_tau_h=float(bc_config.get("nwm_bias_tau_h", 24) or 24),
            ref_time=pd.Timestamp(_it) if _it else None,
        )
    except Exception as e:
        print(f"nwm_bias: correction failed ({e}) - using raw NWM.")
        bc_config["_nwm_bias_note"] = f"failed ({e}) - raw NWM"
        return q_df
    pct = 100 * (out["value"].sum() / max(q_df["value"].sum(), 1e-9) - 1)
    summary = (
        f"{note}; trained on USGS {site} vs NWM Retrospective "
        f"{fit['train_start']} to {fit['train_end']} "
        f"({fit['n_days']} d {fit.get('resolution', 'daily')}, raw PBIAS {fit['pbias_raw_pct']:+.1f}%); "
        f"volume change {pct:+.1f}%"
    )
    print(f"nwm_bias: {summary}")
    bc_config["_nwm_bias_note"] = summary
    LAST_RAW[str(bc_config.get("hdf_key", ""))] = q_df
    LAST_CORR[str(bc_config.get("hdf_key", ""))] = out
    return out
