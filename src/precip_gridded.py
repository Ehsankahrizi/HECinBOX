"""Gridded precipitation sources for rain on mesh.

Fetches spatially-varying rainfall and resamples it onto a grid in the
model's projected CRS, ready for
:func:`main.update_hdf_gridded_precipitation`.

Two sources, mirroring the BC forecast/hindcast split:

* **AORC** (hindcast) - NOAA Analysis Of Record for Calibration, the
  precipitation that drives the National Water Model.  ~800 m hourly,
  1979→present (~10-day lag), anonymous S3 **Zarr** (`APCP_surface`,
  units kg/m² ≡ mm).
* **HRRR** (forecast) - High-Resolution Rapid Refresh, 3 km hourly
  forecasts, read from the anonymous **hrrrzarr** S3 archive so no GRIB
  decoder is needed.

Both return ``GriddedPrecip``: an hourly cube already on a model-CRS
grid (north-up, row 0 = north) plus the grid geotransform - exactly what
the plan-HDF writer consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np


@dataclass
class GriddedPrecip:
    """Hourly precip on a model-CRS grid, ready for the HDF writer."""
    values: np.ndarray          # (n_times, rows, cols) mm/hr, row0 = north
    timestamps: list            # len n_times (pandas Timestamps)
    left: float                 # grid west edge (model CRS)
    top: float                  # grid north edge (model CRS)
    cellsize: float             # square cell size (model CRS units)
    units: str = "mm"
    source: str = ""


def _model_grid(model_wkt, bbox_xy, cellsize):
    """Build a model-CRS target grid covering ``bbox_xy`` (+5 % margin).

    Returns ``(left, top, cols, rows, lon2d, lat2d)`` where lon2d/lat2d
    are the WGS-84 coordinates of every grid cell centre (for sampling
    a lat/lon source).
    """
    from pyproj import CRS, Transformer
    xmin, ymin, xmax, ymax = bbox_xy
    padx = 0.05 * (xmax - xmin) + cellsize
    pady = 0.05 * (ymax - ymin) + cellsize
    left = xmin - padx
    top = ymax + pady
    cols = int(np.ceil((xmax + padx - left) / cellsize))
    rows = int(np.ceil((top - (ymin - pady)) / cellsize))
    # Cell-centre coordinates (row 0 = north).
    cx = left + (np.arange(cols) + 0.5) * cellsize
    cy = top - (np.arange(rows) + 0.5) * cellsize
    XX, YY = np.meshgrid(cx, cy)
    tr = Transformer.from_crs(
        CRS.from_wkt(model_wkt), CRS.from_epsg(4326), always_xy=True
    )
    lon2d, lat2d = tr.transform(XX, YY)
    return left, top, cols, rows, np.asarray(lon2d), np.asarray(lat2d)


def _sample_latlon_cube(cube, src_lat, src_lon, lat2d, lon2d):
    """Nearest-neighbour sample a (time, lat, lon) cube onto a grid.

    ``src_lat``/``src_lon`` are the source's 1-D coordinate axes;
    ``lat2d``/``lon2d`` are the target cell-centre coordinates.  Returns
    ``(n_times, rows, cols)``.
    """
    src_lat = np.asarray(src_lat)
    src_lon = np.asarray(src_lon)
    # Nearest index along each monotonic axis.
    lat_i = np.abs(src_lat[None, :] - lat2d.ravel()[:, None]).argmin(axis=1)
    lon_i = np.abs(src_lon[None, :] - lon2d.ravel()[:, None]).argmin(axis=1)
    rows, cols = lat2d.shape
    out = cube[:, lat_i, lon_i].reshape(cube.shape[0], rows, cols)
    return np.asarray(out, dtype=np.float32)


def fetch_aorc(model_wkt, bbox_xy, start, end, cellsize=1000.0, log=print):
    """Hindcast gridded precip from NOAA AORC (anonymous S3 Zarr).

    ``bbox_xy`` is ``(xmin, ymin, xmax, ymax)`` in the model CRS;
    ``start``/``end`` bound the simulation window.  Returns a
    :class:`GriddedPrecip` or raises on failure.
    """
    import s3fs
    import xarray as xr
    import pandas as pd

    left, top, cols, rows, lon2d, lat2d = _model_grid(
        model_wkt, bbox_xy, cellsize
    )
    lon_min, lon_max = float(lon2d.min()), float(lon2d.max())
    lat_min, lat_max = float(lat2d.min()), float(lat2d.max())

    t0 = pd.Timestamp(start).floor("h")
    t1 = pd.Timestamp(end).ceil("h")
    fs = s3fs.S3FileSystem(anon=True)

    parts = []
    for yr in range(t0.year, t1.year + 1):
        store = s3fs.S3Map(
            f"noaa-nws-aorc-v1-1-1km/{yr}.zarr", s3=fs
        )
        ds = xr.open_zarr(store, consolidated=True)
        sub = ds["APCP_surface"].sel(
            time=slice(t0, t1),
            latitude=slice(lat_min - 0.02, lat_max + 0.02),
            longitude=slice(lon_min - 0.02, lon_max + 0.02),
        )
        if sub.time.size:
            parts.append(sub.load())
    if not parts:
        raise RuntimeError("AORC returned no data for the window/bbox")
    da = xr.concat(parts, dim="time") if len(parts) > 1 else parts[0]
    da = da.sortby("time")

    cube = np.nan_to_num(np.asarray(da.values, dtype=np.float32))
    grid = _sample_latlon_cube(
        cube, da.latitude.values, da.longitude.values, lat2d, lon2d
    )
    stamps = list(pd.to_datetime(da.time.values))
    log(
        f"AORC: {len(stamps)} hrs × {rows}×{cols} grid "
        f"(src {cube.shape[1]}×{cube.shape[2]}); "
        f"max {float(grid.max()):.1f} mm/hr"
    )
    return GriddedPrecip(
        values=grid, timestamps=stamps, left=left, top=top,
        cellsize=cellsize, units="mm", source="AORC",
    )


def _sample_2d_nearest(values, src_lat2d, src_lon2d, lat2d, lon2d):
    """Nearest-neighbour sample a 2-D-georeferenced cube onto a grid.

    For a source whose lat/lon are 2-D (e.g. HRRR's Lambert grid), the
    nearest source pixel is found by brute force after the source is
    already cropped to the target bbox (so the point count is small).
    ``values`` is ``(n_times, ny, nx)``; returns ``(n_times, rows, cols)``.
    """
    sy = src_lat2d.ravel()
    sx = src_lon2d.ravel()
    flat = values.reshape(values.shape[0], -1)
    tgt_lat = lat2d.ravel()
    tgt_lon = lon2d.ravel()
    # cos-scaled longitude so degree distances are ~isotropic.
    coslat = np.cos(np.deg2rad(tgt_lat.mean()))
    idx = np.empty(tgt_lat.size, dtype=np.int64)
    for k in range(tgt_lat.size):
        d = (sy - tgt_lat[k]) ** 2 + ((sx - tgt_lon[k]) * coslat) ** 2
        idx[k] = int(d.argmin())
    out = flat[:, idx].reshape(values.shape[0], *lat2d.shape)
    return np.asarray(out, dtype=np.float32)


def fetch_hrrr(model_wkt, bbox_xy, start, end, cellsize=3000.0, log=print,
               progress=None):
    """Forecast gridded precip from the HRRR (official GRIB2 on AWS).

    Uses the most recent available HRRR cycle at/just before ``start``
    and reads the forecast hours covering the window.  Only the APCP
    message of each ``wrfsfc`` file is byte-range-fetched (via its
    ``.idx`` sidecar), de-accumulated to hourly depth, then resampled
    onto a model-CRS grid.  Returns a :class:`GriddedPrecip`.

    ``progress`` (optional) is called ``progress(i, n)`` before each
    forecast-hour read so a caller can surface "reading hour i/n" - the
    per-message GRIB decode is slow, and this proves the run isn't hung.
    """
    import os
    import tempfile
    import s3fs
    import xarray as xr
    import pandas as pd

    left, top, cols, rows, lon2d, lat2d = _model_grid(
        model_wkt, bbox_xy, cellsize
    )
    fs = s3fs.S3FileSystem(anon=True)

    t0 = pd.Timestamp(start).floor("h")
    t1 = pd.Timestamp(end).ceil("h")

    # Latest published cycle at/just before the window start (but never a
    # future cycle), stepping back hourly until its files exist.  HRRR
    # then forecasts forward from there to cover the window.
    _search0 = min(t0, pd.Timestamp.utcnow().tz_localize(None).floor("h"))
    cycle = None
    for back in range(0, 18):
        c = _search0 - pd.Timedelta(hours=back)
        key = (
            f"noaa-hrrr-bdp-pds/hrrr.{c:%Y%m%d}/conus/"
            f"hrrr.t{c:%H}z.wrfsfcf01.grib2"
        )
        if fs.exists(key + ".idx"):
            cycle = c
            break
    if cycle is None:
        raise RuntimeError("no available HRRR cycle near the start time")

    max_f = 48 if cycle.hour in (0, 6, 12, 18) else 18
    n_f = min(max_f, int((t1 - cycle) / pd.Timedelta("1h")) + 1)

    def _apcp(fxx):
        key = (
            f"noaa-hrrr-bdp-pds/hrrr.{cycle:%Y%m%d}/conus/"
            f"hrrr.t{cycle:%H}z.wrfsfcf{fxx:02d}.grib2"
        )
        if not fs.exists(key + ".idx"):
            return None
        lines = fs.cat(key + ".idx").decode().splitlines()
        for i, ln in enumerate(lines):
            if ":APCP:" in ln:
                s = int(ln.split(":")[1])
                e = (int(lines[i + 1].split(":")[1]) - 1
                     if i + 1 < len(lines) else None)
                with fs.open(key) as fo:
                    fo.seek(s)
                    data = fo.read((e - s + 1) if e else None)
                tf = tempfile.NamedTemporaryFile(suffix=".grib2",
                                                 delete=False)
                tf.write(data)
                tf.close()
                try:
                    ds = xr.open_dataset(
                        tf.name, engine="cfgrib",
                        backend_kwargs={"indexpath": ""},
                    )
                    var = list(ds.data_vars)[0]
                    # Eagerly materialise - cfgrib reads lazily, so the
                    # arrays must be loaded before the temp file is gone.
                    vals = np.nan_to_num(np.asarray(ds[var].values))
                    lat = np.asarray(ds.latitude.values)
                    lon = np.asarray(ds.longitude.values)
                    ds.close()
                    return vals, lat, lon
                finally:
                    os.unlink(tf.name)
        return None

    # Crop bbox once (use the f01 message for the grid geometry).
    pad = 0.1
    lon_lo, lon_hi = float(lon2d.min()) - pad, float(lon2d.max()) + pad
    lat_lo, lat_hi = float(lat2d.min()) - pad, float(lat2d.max()) + pad

    acc, stamps = [], []
    src_lat = src_lon = mask = None
    for fxx in range(0, n_f + 1):
        if progress is not None:
            progress(fxx, n_f)
        got = _apcp(fxx)
        if got is None:
            continue
        vals, lat, lon = got
        lon = np.where(lon > 180, lon - 360, lon)
        if mask is None:
            inb = (
                (lat >= lat_lo) & (lat <= lat_hi)
                & (lon >= lon_lo) & (lon <= lon_hi)
            )
            ys, xs = np.where(inb)
            r0, r1, c0, c1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
            mask = (slice(r0, r1), slice(c0, c1))
            src_lat = lat[mask]
            src_lon = lon[mask]
        acc.append(vals[mask])
        stamps.append(cycle + pd.Timedelta(hours=fxx))

    if len(acc) < 2:
        raise RuntimeError("HRRR returned too few forecast hours")
    acc = np.asarray(acc, np.float32)                # (n_f+1, ny, nx) run-total
    hourly = np.clip(np.diff(acc, axis=0), 0, None)  # de-accumulate
    valid = stamps[1:]                               # diff drops f00

    grid = _sample_2d_nearest(hourly, src_lat, src_lon, lat2d, lon2d)
    log(
        f"HRRR {cycle:%Y-%m-%d %Hz}: {len(valid)} fcst hrs × "
        f"{rows}×{cols} grid; max {float(grid.max()):.1f} mm/hr"
    )
    return GriddedPrecip(
        values=grid, timestamps=list(valid), left=left, top=top,
        cellsize=cellsize, units="mm", source=f"HRRR {cycle:%Hz}",
    )


# ── User-supplied DSS gridded precipitation ──────────────────────────
#
# Mirrors HEC-RAS's *Meteorological Data → Precipitation → Gridded →
# Source: DSS* workflow: the user already has gridded rainfall in a
# HEC-DSS file (radar/MRMS, AORC, or a prior study), so instead of
# fetching a remote source we read those grid records straight out of
# the DSS, resample them onto a model-CRS grid, and hand the cube to the
# same plan-HDF writer the remote sources use.  Reading uses the
# ``hecdss`` wheel already in the container (its ``GriddedData`` exposes
# the raster, cell size, origin, source CRS WKT, and units).


def _parse_hec_dt(token):
    """Parse a HEC date/time path part (``'01JAN2024:1300'``).

    Handles the HEC ``2400`` hour (midnight = 00:00 of the next day).
    Returns a ``pandas.Timestamp`` or ``None`` when ``token`` is blank.
    """
    import pandas as pd
    if not token:
        return None
    datepart, _, timepart = token.partition(":")
    timepart = (timepart or "0000").strip()
    base = pd.Timestamp(datetime.strptime(datepart.strip(), "%d%b%Y"))
    if timepart == "2400":
        return base + pd.Timedelta(days=1)
    hh = int(timepart[:2] or 0)
    mm = int(timepart[2:4] or 0)
    return base + pd.Timedelta(hours=hh, minutes=mm)


def list_dss_grid_paths(dss_file):
    """List the distinct gridded pathnames in a DSS file.

    Grid records aren't condensed like time series - each interval is its
    own record - so we group by the A/B/C/F parts (ignoring the D/E date
    block) and report how many time records and what span each pattern
    spans.  Returns a list of dicts ``{pattern, label, count, start,
    end}``, where ``pattern`` is the canonical ``/A/B/C///F/`` form fed
    back to :func:`fetch_dss_grid`.  Used to populate the UI *Path*
    dropdown (HEC-RAS's gridded-DSS selector equivalent).
    """
    from hecdss import HecDss
    from hecdss.record_type import RecordType

    dss = HecDss(str(dss_file))
    try:
        cat = dss.get_catalog()
        groups: dict = {}
        for p in cat:
            try:
                if cat.get_record_type(str(p)) != RecordType.Grid:
                    continue
            except Exception:
                continue
            key = (p.A, p.B, p.C, p.F)
            stamp = _parse_hec_dt(p.E) or _parse_hec_dt(p.D)
            g = groups.setdefault(key, {"count": 0, "stamps": []})
            g["count"] += 1
            if stamp is not None:
                g["stamps"].append(stamp)
        out = []
        for (a, b, c, fp), g in sorted(groups.items()):
            stamps = sorted(g["stamps"])
            span = ""
            if stamps:
                span = (f" · {stamps[0]:%Y-%m-%d %H:%M}"
                        f" → {stamps[-1]:%Y-%m-%d %H:%M}")
            out.append({
                "pattern": f"/{a}/{b}/{c}///{fp}/",
                "label": f"/{a}/{b}/{c}/ ({fp})  -  {g['count']} grids{span}",
                "count": g["count"],
                "start": stamps[0] if stamps else None,
                "end": stamps[-1] if stamps else None,
            })
        return out
    finally:
        dss.close()


def _sample_regular(arr, fc, fr, interp, nx, ny):
    """Sample a regular grid ``arr`` (row 0 = south) at fractional cell
    indices ``fc`` (col) / ``fr`` (row), filling out-of-bounds with 0.

    ``interp`` is ``'bilinear'`` (4-neighbour blend) or anything else
    (nearest).  ``fc``/``fr`` are target-shaped float arrays.
    """
    def _gv(rr, cc):
        inb = (rr >= 0) & (rr < ny) & (cc >= 0) & (cc < nx)
        v = arr[np.clip(rr, 0, ny - 1), np.clip(cc, 0, nx - 1)]
        return np.where(inb, v, 0.0)

    if str(interp).lower() == "bilinear":
        c0 = np.floor(fc).astype(np.int64)
        r0 = np.floor(fr).astype(np.int64)
        dc = fc - c0
        dr = fr - r0
        return (_gv(r0, c0) * (1 - dc) * (1 - dr)
                + _gv(r0, c0 + 1) * dc * (1 - dr)
                + _gv(r0 + 1, c0) * (1 - dc) * dr
                + _gv(r0 + 1, c0 + 1) * dc * dr)
    cc = np.rint(fc).astype(np.int64)
    rr = np.rint(fr).astype(np.int64)
    return _gv(rr, cc)


def fetch_dss_grid(dss_file, grid_pattern, model_wkt, bbox_xy, start, end,
                   cellsize=2000.0, interp="nearest", log=print):
    """Gridded precip read from a user-supplied HEC-DSS file.

    ``grid_pattern`` is the ``/A/B/C///F/`` form from
    :func:`list_dss_grid_paths`; every grid record matching those parts
    whose time falls in ``[start, end]`` is read, reprojected from its
    own CRS (``srsDefinition`` WKT - typically SHG/Albers) onto a
    model-CRS grid, and stacked into an hourly-style cube.  Returns a
    :class:`GriddedPrecip` ready for the plan-HDF writer.
    """
    import pandas as pd
    from pyproj import CRS, Transformer
    from hecdss import HecDss
    from hecdss.record_type import RecordType

    # Model-CRS target grid + its cell-centre coordinates (row 0 = north).
    left, top, cols, rows, _, _ = _model_grid(model_wkt, bbox_xy, cellsize)
    cx = left + (np.arange(cols) + 0.5) * cellsize
    cy = top - (np.arange(rows) + 0.5) * cellsize
    XX, YY = np.meshgrid(cx, cy)

    pp = str(grid_pattern).split("/")
    want = (pp[1], pp[2], pp[3], pp[6]) if len(pp) >= 7 else ("", "", "", "")

    t0 = pd.Timestamp(start).floor("h")
    t1 = pd.Timestamp(end).ceil("h")

    dss = HecDss(str(dss_file))
    try:
        cat = dss.get_catalog()
        recs = []
        for p in cat:
            try:
                if cat.get_record_type(str(p)) != RecordType.Grid:
                    continue
            except Exception:
                continue
            if (p.A, p.B, p.C, p.F) != want:
                continue
            stamp = _parse_hec_dt(p.E) or _parse_hec_dt(p.D)
            if stamp is None or not (t0 <= stamp <= t1):
                continue
            recs.append((stamp, str(p)))
        recs.sort()
        if not recs:
            raise RuntimeError(
                "no gridded DSS records matched the chosen path within the "
                "simulation window - check the Path and your Tab 2 dates"
            )

        # Build the model→source-CRS transformer once from the first grid.
        # A missing or malformed grid CRS (some writers truncate long WKT)
        # must not crash the run - fall back to the model CRS (identity),
        # which is correct when the grid is already in the model's CRS.
        first = dss.get(recs[0][1])
        src_wkt = (first.srsDefinition or "").strip()
        try:
            src_crs = CRS.from_wkt(src_wkt) if src_wkt else CRS.from_wkt(model_wkt)
        except Exception:
            log("WARNING: DSS grid CRS unreadable - assuming model CRS")
            src_crs = CRS.from_wkt(model_wkt)
        tr = Transformer.from_crs(
            CRS.from_wkt(model_wkt), src_crs, always_xy=True
        )
        Xs, Ys = tr.transform(XX, YY)

        cube = np.empty((len(recs), rows, cols), np.float32)
        stamps, nx, ny = [], 0, 0
        for k, (stamp, path) in enumerate(recs):
            gd = first if k == 0 else dss.get(path)
            arr = np.asarray(gd.data, np.float64)
            nv = gd.nullValue
            arr = np.where(
                ~np.isfinite(arr) | (arr <= -1e30) | (arr == nv), 0.0, arr
            )
            if str(gd.dataUnits or "MM").upper().startswith("IN"):
                arr = arr * 25.4            # inches → mm
            ny, nx = arr.shape
            ox = gd.xCoordOfGridCellZero + gd.lowerLeftCellX * gd.cellSize
            oy = gd.yCoordOfGridCellZero + gd.lowerLeftCellY * gd.cellSize
            fc = (Xs - ox) / gd.cellSize - 0.5
            fr = (Ys - oy) / gd.cellSize - 0.5
            cube[k] = _sample_regular(arr, fc, fr, interp, nx, ny)
            stamps.append(stamp)

        log(
            f"DSS grid: {len(recs)} steps × {rows}×{cols} grid "
            f"(src {ny}×{nx}, {interp}); max {float(cube.max()):.1f} mm"
        )
        return GriddedPrecip(
            values=cube, timestamps=stamps, left=left, top=top,
            cellsize=cellsize, units="mm",
            source=f"DSS {want[1]}/{want[2]}",
        )
    finally:
        dss.close()


def fetch_gridded(source, model_wkt, bbox_xy, start, end, **kw):
    """Dispatch to the requested gridded-precip source."""
    src = str(source).lower()
    # Only HRRR streams a per-hour progress callback; don't leak it to the
    # other fetchers (which don't accept it).
    progress = kw.pop("progress", None)
    if src == "aorc":
        return fetch_aorc(model_wkt, bbox_xy, start, end, **kw)
    if src == "hrrr":
        return fetch_hrrr(model_wkt, bbox_xy, start, end, progress=progress, **kw)
    if src == "dss":
        return fetch_dss_grid(
            kw["dss_file"], kw["grid_pattern"], model_wkt, bbox_xy,
            start, end, interp=kw.get("interp", "nearest"),
            log=kw.get("log", print),
        )
    raise ValueError(f"unknown gridded precip source: {source!r}")
