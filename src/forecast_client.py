"""Forecast-data clients for the v3.0.0 NWM / STOFS integration.

Provides four concrete data products that can drive HEC-RAS boundary
conditions from publicly-available forecast feeds:

1. **NWMClient**            - National Water Model channel routing
                              (CHRTOUT) streamflow Q at any NHDPlus
                              `feature_id` (a.k.a. COMID).  Drives
                              upstream/inland *flow* boundaries.

2. **STOFSClient**          - Surge and Tide Operational Forecast
                              System (STOFS-3D Atlantic / Pacific)
                              Total Water Level (TWL) at a NOAA tide
                              station.  Drives coastal/tidal *stage*
                              boundaries - astronomical tide + surge
                              + steric + wave setup combined.

3. **USGSRatingCurve**      - Pulls the active USGS stage-discharge
                              rating curve for a gauge and exposes a
                              vectorised ``q_to_stage(Q)`` interpolator.
                              Used to convert NWM Q → stage at an
                              inland *stage* boundary (Path A).

4. **HANDRatingCurve**      - Pulls the NOAA NWPS synthetic rating
                              curve, derived from Height-Above-Nearest-
                              Drainage (HAND), for any NHDPlus reach.
                              Used as the Path B fallback when no USGS
                              gauge is co-located with the boundary.

Each client uses graceful network-error handling - a failed fetch
returns ``None`` and is logged, so the caller can fall back to the
model-bundled boundary record without raising.  All time series come
back as ``pandas.DataFrame`` with the same shape as USGSClient /
NOAAClient (`datetime`, `value`) so they slot into the existing
boundary-condition pipeline unchanged.

Public data sources:

* NWM:    s3://noaa-nwm-pds/  (anonymous, no AWS account required)
* STOFS:  s3://noaa-nws-stofs3d-atlantic-pds/  (anonymous)
*         and Pacific: s3://noaa-nws-stofs3d-pacific-pds/
* USGS:   https://waterdata.usgs.gov/nwisweb/get_ratings (no auth)
* HAND:   https://api.water.noaa.gov/nwps/v1/  (no auth)
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests


# ── Constants ─────────────────────────────────────────────────────────
NWM_S3_BUCKET = "noaa-nwm-pds"
NWM_S3_REGION = "us-east-1"
STOFS_ATL_BUCKET = "noaa-nws-stofs3d-atlantic-pds"
STOFS_PAC_BUCKET = "noaa-nws-stofs3d-pacific-pds"
USGS_RATING_URL = (
    "https://waterdata.usgs.gov/nwisweb/get_ratings"
    "?site_no={site}&file_type=exsa"
)
NWPS_SRC_URL = (
    "https://api.water.noaa.gov/nwps/v1/products/"
    "synthetic-rating-curve/{reach_id}"
)


# ── 1. NWM streamflow client ──────────────────────────────────────────
class NWMClient:
    """National Water Model channel-routing (CHRTOUT) streamflow.

    Reads NetCDF forecast files from the public NOAA NWM AWS Open Data
    bucket (`s3://noaa-nwm-pds/`).  The streamflow variable is indexed
    by `feature_id`, which is the NHDPlusV2 COMID.

    Forecast products supported:
        * "analysis_assim"  - current state, hourly, 3-h look-back
        * "short_range"     - 18-hour forecast, hourly, updated hourly
        * "medium_range"    - 10-day forecast, 3-hourly, 7 ensemble
                              members, updated 4×/day
        * "long_range"      - 30-day forecast, 6-hourly, 4 ensemble
                              members, updated 4×/day
    """

    def __init__(
        self,
        product: str = "medium_range",
        ensemble_member: int = 1,
    ) -> None:
        self.product = product
        self.ensemble_member = ensemble_member

    # --- public ------------------------------------------------------
    def fetch_q_cms(
        self,
        comid: int,
        start: datetime,
        end: datetime,
    ) -> Optional[pd.DataFrame]:
        """Fetch streamflow Q (m³/s) for one COMID over [start, end].

        Returns a DataFrame with columns `datetime` (UTC) and
        `value` (Q in m³/s), or `None` on any failure (logged).
        Conversion to cfs is left to the caller; HECinBOX's
        DSS writer handles unit factors per-boundary.
        """
        try:
            import s3fs
            import xarray as xr
        except ImportError as e:
            print(
                f"NWMClient: required packages not installed "
                f"(s3fs, xarray): {e}.  Add to requirements.txt."
            )
            return None

        fs = s3fs.S3FileSystem(anon=True)
        cycle_files = self._enumerate_files(start, end)
        if not cycle_files:
            print(f"NWMClient: no cycle files in window {start}-{end}")
            return None

        records: list[tuple[datetime, float]] = []
        for s3_path in cycle_files:
            try:
                with fs.open(s3_path) as fh:
                    ds = xr.open_dataset(fh, engine="h5netcdf")
                    # `feature_id` is the NHDPlus COMID
                    if "feature_id" not in ds.coords:
                        continue
                    mask = ds["feature_id"].values == int(comid)
                    if not mask.any():
                        continue
                    q = float(ds["streamflow"].values[mask][0])
                    t = pd.to_datetime(ds["time"].values[0]).to_pydatetime()
                    records.append((t, q))
            except Exception as e:
                print(f"NWMClient: failed to read {s3_path}: {e}")
                continue

        if not records:
            print(
                f"NWMClient: no records for COMID {comid} - "
                f"check that the COMID exists in NWM and that the "
                f"window covers an issued forecast cycle."
            )
            return None
        df = pd.DataFrame(records, columns=["datetime", "value"])
        df = df.sort_values("datetime").reset_index(drop=True)
        return df

    # --- internal ----------------------------------------------------
    def _enumerate_files(
        self, start: datetime, end: datetime
    ) -> list[str]:
        """Build the list of S3 paths covering [start, end].

        File-naming convention (NWM v3.x):
          nwm.<YYYYMMDD>/<product>/nwm.t<HH>z.<product>.channel_rt.
          f<HHH>.conus.nc
        For ensemble products an extra `mem_<N>` segment appears.
        """
        files: list[str] = []
        start_utc = start.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = end.astimezone(timezone.utc).replace(tzinfo=None)
        cursor = start_utc.replace(minute=0, second=0, microsecond=0)
        while cursor <= end_utc:
            ymd = cursor.strftime("%Y%m%d")
            hh = cursor.strftime("%H")
            if self.product == "analysis_assim":
                key = (
                    f"{NWM_S3_BUCKET}/nwm.{ymd}/analysis_assim/"
                    f"nwm.t{hh}z.analysis_assim.channel_rt.tm00."
                    "conus.nc"
                )
            elif self.product == "short_range":
                key = (
                    f"{NWM_S3_BUCKET}/nwm.{ymd}/short_range/"
                    f"nwm.t{hh}z.short_range.channel_rt.f001."
                    "conus.nc"
                )
            elif self.product == "medium_range":
                mem = f"mem{self.ensemble_member}"
                key = (
                    f"{NWM_S3_BUCKET}/nwm.{ymd}/medium_range_{mem}/"
                    f"nwm.t{hh}z.medium_range.channel_rt_{mem}."
                    "f003.conus.nc"
                )
            else:
                key = (
                    f"{NWM_S3_BUCKET}/nwm.{ymd}/long_range_mem"
                    f"{self.ensemble_member}/nwm.t{hh}z.long_range."
                    f"channel_rt_{self.ensemble_member}.f006."
                    "conus.nc"
                )
            files.append(key)
            cursor += timedelta(hours=1)
        return files


# ── 2. STOFS-3D Total Water Level client ──────────────────────────────
class STOFSClient:
    """STOFS-3D Total Water Level (TWL) at NOAA tide stations.

    STOFS combines astronomical tide, storm surge, steric effects,
    and wave setup into a single TWL signal - exactly the boundary
    condition a coastal HEC-RAS model expects at its tidal outflow.

    Two domains:
        * "atlantic"  - STOFS-3D-Atlantic (East Coast + Gulf of Mexico)
        * "pacific"   - STOFS-3D-Pacific (West Coast)
    """

    def __init__(self, domain: str = "atlantic") -> None:
        domain = domain.lower()
        if domain not in ("atlantic", "pacific"):
            raise ValueError(
                f"STOFS domain must be 'atlantic' or 'pacific'; "
                f"got {domain!r}"
            )
        self.domain = domain
        self.bucket = (
            STOFS_ATL_BUCKET if domain == "atlantic" else STOFS_PAC_BUCKET
        )

    def fetch_twl_m(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
    ) -> Optional[pd.DataFrame]:
        """Fetch Total Water Level (m, MSL) at one NOAA tide station.

        Returns DataFrame(datetime, value) or None.
        """
        try:
            import s3fs
            import xarray as xr
        except ImportError as e:
            print(
                f"STOFSClient: required packages not installed "
                f"(s3fs, xarray): {e}.  Add to requirements.txt."
            )
            return None

        fs = s3fs.S3FileSystem(anon=True)
        # STOFS writes one TWL station file per cycle
        # (e.g. stofs_3d_atl.t12z.points.cwl.nc) at the station mesh.
        records: list[tuple[datetime, float]] = []
        cursor = start.astimezone(timezone.utc).replace(tzinfo=None)
        cursor = cursor.replace(minute=0, second=0, microsecond=0)
        end_utc = end.astimezone(timezone.utc).replace(tzinfo=None)
        while cursor <= end_utc:
            ymd = cursor.strftime("%Y%m%d")
            for hh in ("00", "06", "12", "18"):
                key = (
                    f"{self.bucket}/stofs_3d_atl.{ymd}/"
                    f"stofs_3d_atl.t{hh}z.points.cwl.nc"
                ) if self.domain == "atlantic" else (
                    f"{self.bucket}/stofs_3d_pac.{ymd}/"
                    f"stofs_3d_pac.t{hh}z.points.cwl.nc"
                )
                try:
                    with fs.open(key) as fh:
                        ds = xr.open_dataset(fh, engine="h5netcdf")
                        if "station_name" not in ds.coords:
                            continue
                        names = [
                            (n.decode() if isinstance(n, bytes) else n)
                            .strip()
                            for n in ds["station_name"].values
                        ]
                        if station_id not in names:
                            continue
                        idx = names.index(station_id)
                        for t, v in zip(
                            ds["time"].values,
                            ds["zeta"].values[:, idx],
                        ):
                            records.append(
                                (pd.to_datetime(t).to_pydatetime(),
                                 float(v))
                            )
                except Exception as e:
                    print(f"STOFSClient: skip {key}: {e}")
                    continue
            cursor += timedelta(days=1)

        if not records:
            print(
                f"STOFSClient: no TWL records for station "
                f"{station_id!r} in window {start}-{end}."
            )
            return None
        df = (
            pd.DataFrame(records, columns=["datetime", "value"])
            .drop_duplicates(subset="datetime")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
        # Clip to requested window
        df = df[(df["datetime"] >= start) & (df["datetime"] <= end)]
        return df.reset_index(drop=True)


# ── 3. USGS rating curve (Path A) ─────────────────────────────────────
@dataclass
class _RatingTable:
    stage_ft: np.ndarray  # independent (INDEP)
    q_cfs: np.ndarray     # dependent   (DEP)

    def q_to_stage_ft(self, q_cfs: np.ndarray) -> np.ndarray:
        """Convert discharge (cfs) → stage (ft) by interpolation.

        Extrapolation beyond the rating-curve table is **flat** -
        Q above the highest tabulated value clamps to the highest
        tabulated stage.  Flagged in the log so the operator knows.
        """
        sorted_idx = np.argsort(self.q_cfs)
        q_sorted = self.q_cfs[sorted_idx]
        s_sorted = self.stage_ft[sorted_idx]
        clipped = np.clip(q_cfs, q_sorted.min(), q_sorted.max())
        n_oob = int(np.sum(
            (q_cfs < q_sorted.min()) | (q_cfs > q_sorted.max())
        ))
        if n_oob:
            print(
                f"USGSRatingCurve: clamped {n_oob} of {len(q_cfs)} "
                f"discharge values that fell outside the rating-curve "
                f"range [{q_sorted.min():.1f}, {q_sorted.max():.1f}] cfs."
            )
        return np.interp(clipped, q_sorted, s_sorted)


class USGSRatingCurve:
    """USGS active stage-discharge rating curve for a single gauge.

    Fetches the EXSA-format table from the USGS rating service, which
    returns a tab-separated text file with header lines starting with
    '#' and three columns: INDEP (stage, ft), DEP (discharge, cfs),
    and STOR (storage offset).  We use the empirically-calibrated
    relationship, not a hydraulic model.
    """

    def __init__(self, site_no: str) -> None:
        self.site_no = str(site_no).strip()
        self._table: Optional[_RatingTable] = None

    def load(self) -> Optional[_RatingTable]:
        if self._table is not None:
            return self._table
        url = USGS_RATING_URL.format(site=self.site_no)
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            print(
                f"USGSRatingCurve: HTTP error for site "
                f"{self.site_no}: {e}"
            )
            return None
        stages, qs = [], []
        for line in r.text.splitlines():
            if not line or line.startswith("#") or line.startswith("EXSA"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            try:
                stages.append(float(parts[0]))
                qs.append(float(parts[1]))
            except ValueError:
                continue
        if not stages:
            print(
                f"USGSRatingCurve: empty table returned for "
                f"site {self.site_no} - no published rating?"
            )
            return None
        self._table = _RatingTable(
            stage_ft=np.asarray(stages, dtype=np.float64),
            q_cfs=np.asarray(qs, dtype=np.float64),
        )
        print(
            f"USGSRatingCurve: loaded {len(stages)} points for site "
            f"{self.site_no} (Q range "
            f"[{self._table.q_cfs.min():.1f}, "
            f"{self._table.q_cfs.max():.1f}] cfs)."
        )
        return self._table

    def convert(
        self, q_df: pd.DataFrame, q_units: str = "cfs"
    ) -> Optional[pd.DataFrame]:
        """Apply this rating curve to a discharge DataFrame.

        Input  : DataFrame(datetime, value)  with value in q_units.
        Output : DataFrame(datetime, value)  with value = stage (ft).
        """
        table = self.load()
        if table is None:
            return None
        q = q_df["value"].to_numpy()
        if q_units.lower() == "cms":
            q = q * 35.3147  # m³/s → cfs
        stages = table.q_to_stage_ft(q)
        out = q_df.copy()
        out["value"] = stages
        return out


# ── 4. HAND synthetic rating curve (Path B) ───────────────────────────
class HANDRatingCurve:
    """NOAA NWPS synthetic stage-discharge rating from HAND.

    Coverage is universal across NHDPlus reaches - any reach with an
    NWM `feature_id` has a HAND synthetic rating - but the rating is
    *modelled* (from terrain + Manning's roughness), not empirically
    calibrated, so it is the recommended Path B *fallback* for reaches
    where no USGS rating exists.
    """

    def __init__(self, reach_id: int) -> None:
        self.reach_id = int(reach_id)
        self._table: Optional[_RatingTable] = None

    def load(self) -> Optional[_RatingTable]:
        if self._table is not None:
            return self._table
        url = NWPS_SRC_URL.format(reach_id=self.reach_id)
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            print(
                f"HANDRatingCurve: failed to fetch synthetic rating "
                f"for reach {self.reach_id}: {e}"
            )
            return None
        # NWPS returns a list of {stage, discharge} objects (units
        # may be metric); spec is evolving - be defensive.
        rows = data.get("data") or data.get("ratingCurve") or []
        stages, qs = [], []
        for row in rows:
            s = row.get("stage") or row.get("stageFt")
            q = (
                row.get("discharge") or row.get("dischargeCfs")
                or row.get("dischargeCms")
            )
            if s is None or q is None:
                continue
            # If discharge is in cms, convert to cfs for symmetry
            # with USGSRatingCurve's stored units.
            if "Cms" in (
                row.get("dischargeUnit") or
                "cms" if row.get("dischargeCms") else ""
            ):
                q = q * 35.3147
            try:
                stages.append(float(s))
                qs.append(float(q))
            except (TypeError, ValueError):
                continue
        if not stages:
            print(
                f"HANDRatingCurve: empty synthetic rating returned "
                f"for reach {self.reach_id}."
            )
            return None
        self._table = _RatingTable(
            stage_ft=np.asarray(stages, dtype=np.float64),
            q_cfs=np.asarray(qs, dtype=np.float64),
        )
        return self._table

    def convert(
        self, q_df: pd.DataFrame, q_units: str = "cfs"
    ) -> Optional[pd.DataFrame]:
        table = self.load()
        if table is None:
            return None
        q = q_df["value"].to_numpy()
        if q_units.lower() == "cms":
            q = q * 35.3147
        stages = table.q_to_stage_ft(q)
        out = q_df.copy()
        out["value"] = stages
        return out


# ── 5. Convenience dispatcher used by the main pipeline ───────────────
def fetch_forecast_series(
    bc_config: dict,
    sim_start: datetime,
    sim_end: datetime,
) -> Optional[pd.DataFrame]:
    """Top-level dispatcher for the `forecast` source.

    Reads the per-boundary `forecast_product` field on `bc_config`
    and routes to the appropriate client.  Returns a unified
    DataFrame(datetime, value) in the units HEC-RAS expects for the
    boundary type, or None on any failure.

    Recognised `forecast_product` values (set from the Tab 3 UI):
      * "nwm_q"               - NWM streamflow Q in cms (flow BCs)
      * "nwm_q_usgs_rating"   - NWM Q + USGS rating → stage in ft
                                 (Path A: requires `rating_site_no`)
      * "nwm_q_hand_rating"   - NWM Q + HAND synthetic → stage in ft
                                 (Path B: requires `hand_reach_id`)
      * "stofs_twl"           - STOFS-3D TWL in m at a NOAA station
                                 (requires `stofs_station`, `stofs_domain`)
    """
    prod = str(bc_config.get("forecast_product", "")).lower()
    if not prod:
        print("forecast: no forecast_product specified on BC config.")
        return None

    horizon = str(bc_config.get("forecast_horizon", "medium_range"))
    member = int(bc_config.get("forecast_member", 1))
    comid = bc_config.get("comid")

    if prod == "nwm_q":
        if comid is None:
            print("forecast nwm_q: missing `comid` on BC config.")
            return None
        nwm = NWMClient(product=horizon, ensemble_member=member)
        return nwm.fetch_q_cms(int(comid), sim_start, sim_end)

    if prod == "nwm_q_usgs_rating":
        site = str(bc_config.get("rating_site_no", "")).strip()
        if not comid or not site:
            print(
                "forecast nwm_q_usgs_rating: need both `comid` "
                "and `rating_site_no`."
            )
            return None
        nwm = NWMClient(product=horizon, ensemble_member=member)
        q_df = nwm.fetch_q_cms(int(comid), sim_start, sim_end)
        if q_df is None:
            return None
        rating = USGSRatingCurve(site_no=site)
        return rating.convert(q_df, q_units="cms")

    if prod == "nwm_q_hand_rating":
        reach = bc_config.get("hand_reach_id") or comid
        if not comid or not reach:
            print(
                "forecast nwm_q_hand_rating: need `comid` and "
                "`hand_reach_id` (or one COMID for both)."
            )
            return None
        nwm = NWMClient(product=horizon, ensemble_member=member)
        q_df = nwm.fetch_q_cms(int(comid), sim_start, sim_end)
        if q_df is None:
            return None
        rating = HANDRatingCurve(reach_id=int(reach))
        return rating.convert(q_df, q_units="cms")

    if prod == "stofs_twl":
        station = str(bc_config.get("stofs_station", "")).strip()
        domain = str(bc_config.get("stofs_domain", "atlantic"))
        if not station:
            print("forecast stofs_twl: missing `stofs_station`.")
            return None
        stofs = STOFSClient(domain=domain)
        return stofs.fetch_twl_m(station, sim_start, sim_end)

    print(f"forecast: unrecognised forecast_product {prod!r}.")
    return None
