"""HECinBOX - automated 2D unsteady HEC-RAS pipeline (Linux container).

Two independent entry points
-----------------------------
``run``       Fetch BCs, patch model files, run the HEC-RAS engine, extract
              raw WSE results, and copy the results HDF to the output dir.
``validate``  Fetch observed-gage data, compare with the saved model results,
              compute metrics (NSE, RMSE, …) and save plots / CSV.

The Streamlit UI invokes each entry point as a subprocess so progress bars
work without blocking the event loop.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import warnings

import h5py
import numpy as np
import pandas as pd

# Expected, harmless: all-NaN reductions on dry cells/timesteps.
warnings.filterwarnings("ignore", message="All-NaN slice encountered")
warnings.filterwarnings("ignore", message="Mean of empty slice")
from pyproj import CRS, Transformer

from config_loader import load_config
from noaa_client import NOAAClient
from run_hecras import run_hecras_linux
from usgs_client import USGSClient
from visualization import Metrics, ResultsWriter

CFS_TO_CMS = 0.028316846592
FT_TO_M = 0.3048
CMS_TO_CFS = 1.0 / CFS_TO_CMS   # 35.3147 - m³/s → cfs
M_TO_FT = 1.0 / FT_TO_M         # 3.28084 - m → ft


def _seed_plan_hdf(
    geom_hdf: Path, plan_hdf: Path, sim_start, sim_end
) -> None:
    """Create a minimal plan HDF from a geometry HDF for first-time runs."""
    shutil.copy2(geom_hdf, plan_hdf)
    with h5py.File(plan_hdf, "a") as f:
        if "Plan Data" not in f:
            f.create_group("Plan Data")
        start_str = sim_start.strftime("%d%b%Y %H:%M:%S")
        end_str = sim_end.strftime("%d%b%Y %H:%M:%S")
        attrs = f["Plan Data"].attrs
        attrs["Plan Information/Simulation Start Time"] = start_str.encode()
        attrs["Plan Information/Simulation End Time"] = end_str.encode()
        attrs["Plan Information/Time Window"] = (
            f"{start_str} to {end_str}"
        ).encode()
        bc_root = "Event Conditions/Unsteady/Boundary Conditions"
        if bc_root not in f:
            f.create_group(bc_root)


def _progress(pct: int, msg: str) -> None:
    print(f"PROGRESS|{pct}|{msg}", flush=True)


def _save_run_artifacts(
    *,
    output_dir,
    wse,
    coords,
    model_time,
    min_elev=None,
    vel=None,
    cell_fp=None,
    fp_xy=None,
    bc_provenance: list[dict] | None = None,
    model_is_si: bool = True,
    model_dir=None,
    proj_wkt: str = "",
) -> None:
    """Save a human-browseable artifact bundle into the run folder.

    Files produced::

        summary.txt                        - metadata + key stats
        peak_wse_map.png                   - peak WSE filled-cell map
        peak_depth_map.png                 - peak depth map (if min_elev present)
        peak_velocity_map.png              - peak velocity map (if vel present)
        peak_cell_<id>_timeseries.csv      - time series at the deepest cell

    Maps use a matplotlib ``PolyCollection`` over the model mesh
    (cell_fp + fp_xy) so cells look filled and contiguous - same look
    as the inundation map in Tab 5.  Falls back to a scatter plot if
    the polygon data is unavailable.  Pure matplotlib, no map tiles
    needed - works offline.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    import pandas as _pd

    from units import unit_labels
    _u = unit_labels(model_is_si)
    _len, _vel = _u["length"], _u["velocity"]

    output_dir = Path(output_dir)
    wse = np.asarray(wse, dtype=np.float32)
    coords = np.asarray(coords)
    peak_wse = np.nanmax(wse, axis=0)
    finite_mask = np.isfinite(peak_wse)

    has_depth = min_elev is not None
    has_vel = vel is not None
    if has_depth:
        depth_all = np.clip(wse - np.asarray(min_elev), 0.0, None)
        peak_depth = np.nanmax(depth_all, axis=0)
        wet_mask = (peak_depth > 0.05) & finite_mask
    else:
        depth_all = None
        peak_depth = None
        wet_mask = finite_mask

    if has_vel:
        vel_arr = np.asarray(vel, dtype=np.float32)
        peak_vel = np.nanmax(vel_arr, axis=0)
    else:
        vel_arr = None
        peak_vel = None

    n_t, n_c = int(wse.shape[0]), int(wse.shape[1])
    ts = _pd.to_datetime(model_time)

    # ── summary.txt ─────────────────────────────────────────────────
    lines = [
        "HECinBOX simulation summary",
        "=" * 40,
        f"Output folder : {output_dir}",
        f"Timesteps     : {n_t:,}",
        f"Mesh cells    : {n_c:,}",
        f"Window start  : {ts[0]}",
        f"Window end    : {ts[-1]}",
        "",
        f"Min WSE  ({_len}) : {float(np.nanmin(peak_wse)):>10.3f}",
        f"Max WSE  ({_len}) : {float(np.nanmax(peak_wse)):>10.3f}  "
        f"(cell {int(np.nanargmax(peak_wse))})",
        f"Mean WSE ({_len}) : {float(np.nanmean(peak_wse)):>10.3f}",
    ]
    if has_depth:
        lines += [
            "",
            f"Max depth ({_len}): {float(np.nanmax(peak_depth)):>10.3f}  "
            f"(cell {int(np.nanargmax(peak_depth))})",
            f"Wet cells     : {int(np.sum(wet_mask)):,} of {n_c:,}",
        ]
    if has_vel:
        lines += [
            "",
            f"Max velocity  : {float(np.nanmax(peak_vel)):>10.3f} {_vel}  "
            f"(cell {int(np.nanargmax(peak_vel))})",
        ]

    # ── BC provenance block ─────────────────────────────────────────
    # Shows, per boundary, whether the engine ran on freshly fetched
    # data (USGS / NOAA + station ID) or fell back to the model-bundled
    # calibration values.  This is the single most useful sanity check
    # the user can do without re-opening the UI: if you see "FALLBACK
    # (model defaults)" here, the run is not reflecting real-time data.
    if bc_provenance is not None:
        lines += ["", "Boundary conditions", "-" * 40]
        if not bc_provenance:
            lines.append(
                "(none - model defaults used; results will be identical "
                "for every date range)"
            )
        else:
            name_w = max((len(p.get("name", "")) for p in bc_provenance),
                         default=0)
            for p in bc_provenance:
                name = p.get("name", "?").ljust(name_w)
                status = p.get("status", "fallback")
                src = p.get("source", "")
                station = p.get("station", "")
                n_pts = p.get("n_points", 0)
                if status == "fetched":
                    lines.append(
                        f"{name}  FRESH  {src.upper()} {station}"
                        f"  ({n_pts:,} pts)"
                    )
                elif status == "constant":
                    detail = p.get("detail", "")
                    lines.append(
                        f"{name}  CONSTANT  {detail}"
                    )
                elif status == "gridded":
                    detail = p.get("detail", "")
                    lines.append(f"{name}  GRIDDED  {detail}")
                elif status in ("off", "disabled", "gridded-failed"):
                    detail = p.get("detail", "")
                    lines.append(f"{name}  {detail}")
                elif status == "forecast":
                    _prod = p.get("station", "?")
                    lines.append(
                        f"{name}  FORECAST  {_prod}"
                        f"  ({n_pts:,} pts)"
                    )
                elif status == "replayed":
                    detail = p.get("detail", "")
                    lines.append(
                        f"{name}  REPLAYED (model's own data)  "
                        f"{detail}  ({n_pts:,} pts)"
                    )
                else:
                    detail = p.get("detail", "")
                    suffix = f" - {detail}" if detail else ""
                    lines.append(
                        f"{name}  FALLBACK (model defaults){suffix}"
                    )

    (output_dir / "summary.txt").write_text("\n".join(lines) + "\n")

    # ── Peak maps (filled cells where mesh polygons exist) ──────────
    ids = np.nonzero(finite_mask)[0]
    has_polys = (
        cell_fp is not None and fp_xy is not None
        and cell_fp.size > 0 and fp_xy.size > 0
    )

    # Pre-build the mesh polygons once and reuse them for all three
    # maps - also use them to compute the data extent so each figure
    # is sized to the watershed's aspect ratio (no more wasted white
    # space next to a tall colour-bar).
    polys: list[np.ndarray] | None = None
    poly_vids_arr: np.ndarray | None = None
    if has_polys and ids.size:
        cf = np.asarray(cell_fp)
        fp = np.asarray(fp_xy)
        _polys: list[np.ndarray] = []
        _vids: list[int] = []
        for ci in ids:
            idx = cf[ci]
            idx = idx[idx >= 0]
            if len(idx) < 3:
                continue
            _polys.append(np.column_stack([fp[idx, 0], fp[idx, 1]]))
            _vids.append(ci)
        if _polys:
            polys = _polys
            poly_vids_arr = np.asarray(_vids, dtype=np.int64)

    if polys:
        all_xs = np.concatenate([p[:, 0] for p in polys])
        all_ys = np.concatenate([p[:, 1] for p in polys])
    else:
        all_xs = coords[ids, 0] if ids.size else np.array([0.0, 1.0])
        all_ys = coords[ids, 1] if ids.size else np.array([0.0, 1.0])
    xmin, xmax = float(all_xs.min()), float(all_xs.max())
    ymin, ymax = float(all_ys.min()), float(all_ys.max())
    padx = (xmax - xmin) * 0.03 + 1.0
    pady = (ymax - ymin) * 0.03 + 1.0
    xmin, xmax = xmin - padx, xmax + padx
    ymin, ymax = ymin - pady, ymax + pady

    # Figure size matches the data aspect - wider for east-west
    # watersheds (very common), taller for north-south basins.
    aspect = (xmax - xmin) / max(1e-6, ymax - ymin)
    fw = 12.0
    fh = max(4.5, min(10.0, fw / max(0.35, aspect)))
    # Add a touch of headroom for the title.
    fh += 0.6

    def _render_map(values, cmap, label, fname):
        if ids.size == 0:
            return
        fig, ax = plt.subplots(figsize=(fw, fh), dpi=130)
        artist = None

        if polys is not None and poly_vids_arr is not None:
            pc = PolyCollection(
                polys, cmap=cmap, edgecolors="none", alpha=0.95,
            )
            pc.set_array(np.asarray(values[poly_vids_arr]))
            ax.add_collection(pc)
            artist = pc
        else:
            artist = ax.scatter(
                coords[ids, 0], coords[ids, 1],
                s=6, c=values[ids], cmap=cmap, linewidths=0,
                alpha=0.95,
            )

        # Colour-bar sized to the actual plot height (fraction= keeps
        # it proportional regardless of figure size) and placed snug.
        cbar = fig.colorbar(
            artist, ax=ax, fraction=0.035, pad=0.012, aspect=28,
        )
        cbar.set_label(label, fontweight="bold")
        cbar.ax.tick_params(labelsize=9)

        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.set_aspect("equal")
        ax.set_xlabel("X (projected, model CRS)")
        ax.set_ylabel("Y (projected, model CRS)")
        ax.set_title(f"HECinBOX - {label}", fontweight="bold")
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(output_dir / fname, dpi=130, bbox_inches="tight")
        plt.close(fig)

    _render_map(
        peak_wse, "viridis", f"Peak Water Surface Elevation ({_len})",
        "peak_wse_map.png",
    )
    if has_depth:
        _render_map(
            peak_depth, "Blues", f"Peak Water Depth ({_len})",
            "peak_depth_map.png",
        )
    if has_vel:
        _render_map(
            peak_vel, "turbo", f"Peak Velocity ({_vel})",
            "peak_velocity_map.png",
        )

    # ── Peak-cell time series CSV ───────────────────────────────────
    target_cell = (
        int(np.nanargmax(peak_depth)) if has_depth
        else int(np.nanargmax(peak_wse))
    )
    _ls = _u["len_suffix"]
    _vs = "ms" if model_is_si else "fts"
    cols = {
        "time": ts,
        f"wse_{_ls}": wse[:, target_cell],
    }
    if has_depth:
        cols[f"depth_{_ls}"] = depth_all[:, target_cell]
    if has_vel:
        cols[f"velocity_{_vs}"] = vel_arr[:, target_cell]
    csv_name = f"peak_cell_{target_cell}_timeseries.csv"
    _pd.DataFrame(cols).to_csv(output_dir / csv_name, index=False)

    saved = ["summary.txt", "peak_wse_map.png"]
    if has_depth:
        saved.append("peak_depth_map.png")
    if has_vel:
        saved.append("peak_velocity_map.png")
    saved.append(csv_name)

    # ── Smooth (RAS-Mapper-style) peak-depth raster overlay ──────────
    # Samples peak WSE back onto the fine terrain DEM so the inundation
    # edge follows the topography instead of the blocky mesh cells.
    # Optional: needs the model's terrain DEM + a projection.  Failure
    # here must never break the run, so it returns None and is skipped.
    if model_dir is not None:
        from raster_render import render_smooth_maps
        _peak_vel = (
            np.nanmax(vel, axis=0) if has_vel else None
        )
        _smooth = render_smooth_maps(
            output_dir=output_dir,
            model_dir=model_dir,
            peak_wse=peak_wse,
            peak_vel=_peak_vel,
            coords=coords,
            cell_fp=cell_fp,
            fp_xy=fp_xy,
            proj_wkt=proj_wkt,
            model_is_si=model_is_si,
        )
        if _smooth:
            saved.append("smooth_maps (depth/WSE/velocity)")

    print(f"Saved {', '.join(saved)} to {output_dir}")


def _unit_factor(
    bc_type: str, source: str, units: str, model_is_si: bool = True
) -> float:
    """Factor to convert fetched USGS/NOAA data into the model's native units.

    ``units`` is the *source data* unit (USGS discharge is always cfs;
    NOAA tide/stage can be requested in ``english`` ft or ``metric`` m).
    ``model_is_si`` is the *target* (HEC-RAS .prj) unit system.
    """
    if bc_type == "flow":
        # USGS discharge (00060) is always cfs.
        return CFS_TO_CMS if model_is_si else 1.0
    # Stage / elevation BCs.  Source is feet unless NOAA was asked for metric.
    data_is_metric = source == "noaa" and str(units).lower() == "metric"
    if model_is_si:
        return 1.0 if data_is_metric else FT_TO_M       # → metres
    return M_TO_FT if data_is_metric else 1.0           # → feet


# ========================================
# SIMULATION-TIME PATCHING
# ========================================
def update_simulation_time(u01_path, start, end):
    if not Path(u01_path).exists():
        return
    start_str = start.strftime("%d%b%Y").upper()
    end_str = end.strftime("%d%b%Y").upper()

    with open(u01_path, "r") as f:
        lines = f.readlines()

    out = []
    for line in lines:
        if line.startswith("Simulation Date="):
            out.append(f"Simulation Date={start_str},0000,{end_str},2300\n")
        else:
            out.append(line)

    with open(u01_path, "w") as f:
        f.writelines(out)

    print(f"Simulation time updated in unsteady file ({start_str} -> {end_str})")


def update_plan_time(p01_path, start, end):
    if not Path(p01_path).exists():
        return
    start_str = start.strftime("%d%b%Y").upper()
    end_str = end.strftime("%d%b%Y").upper()

    with open(p01_path, "r") as f:
        lines = f.readlines()

    with open(p01_path, "w") as f:
        for line in lines:
            if line.startswith("Simulation Date="):
                f.write(f"Simulation Date={start_str},0000,{end_str},2300\n")
            else:
                f.write(line)

    print("Plan time updated in plan file")


def update_plan_hdf_time(hdf_path, start, end):
    start_str = start.strftime("%d%b%Y %H:%M:%S")
    end_str = end.strftime("%d%b%Y %H:%M:%S")
    window_str = f"{start_str} to {end_str}"

    with h5py.File(hdf_path, "a") as f:
        attrs = f["Plan Data"].attrs
        attrs["Plan Information/Simulation Start Time"] = start_str.encode()
        attrs["Plan Information/Simulation End Time"] = end_str.encode()
        attrs["Plan Information/Time Window"] = window_str.encode()

    print(f"Plan HDF dates updated: {start_str} -> {end_str}")


def update_plan_hdf_boundary_data(hdf_path, injections, sim_start):
    with h5py.File(hdf_path, "a") as f:
        for inj in injections:
            key = inj["hdf_key"]
            df = inj["df"]
            factor = inj["unit_factor"]
            label = inj["label"]

            if key not in f:
                print(f"WARNING: BC key not in plan HDF, skipping: {key}")
                continue
            if df is None or df.empty:
                print(f"WARNING: no data fetched for {label}, leaving as-is")
                continue

            ts = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
            vals = pd.to_numeric(df["value"], errors="coerce").values * factor
            days = (ts - sim_start).dt.total_seconds().values / 86400.0
            data = np.column_stack([days, vals]).astype(np.float32)

            attrs_backup = dict(f[key].attrs)
            del f[key]
            ds = f.create_dataset(key, data=data)
            for k, v in attrs_backup.items():
                ds.attrs[k] = v

            # HEC-RAS expects "DDMMMYYYY HHMM".  An earlier version wrote
            # the literal "2400" suffix here, which is RAS shorthand for
            # *end-of-day* (i.e. next-day 00:00) and silently offsets the
            # BC trace by one day from ``sim_start``.  Use the real time
            # so the BC anchor matches the first ``(0.0, value)`` row.
            ds.attrs["Start Date"] = sim_start.strftime("%d%b%Y %H%M").encode()
            ds.attrs["End Date"] = ts.iloc[-1].strftime("%d%b%Y %H%M").encode()

            print(
                f"Plan HDF BC updated: {label} - {len(data)} records "
                f"(×{factor:g})"
            )


# ========================================
# "LEAVE UNCHANGED" BC REPLAY
# ========================================
# A "Leave unchanged" boundary keeps the model's own forcing.  That
# data is date-anchored (DSS records / hydrograph tables carry absolute
# 20XX timestamps), so the moment the user re-times the simulation
# window the engine finds zero overlapping records and the boundary
# effectively goes dark.  The fix: when the window differs from the
# model's native one, read the native series from the plan HDF's BC
# snapshot, shift it onto the new window (same pattern, new anchor),
# and inject it through the normal fresh-DSS pipeline.  When the window
# matches the native one, nothing is touched - true "unchanged".


def _parse_ras_dt(date_str: str, time_str: str):
    """Parse RAS 'DDMMMYYYY','HHMM' - '2400' means next-day 00:00."""
    date_str = (date_str or "").strip()
    time_str = (time_str or "").strip() or "0000"
    if time_str == "2400":
        return (
            pd.to_datetime(date_str, format="%d%b%Y")
            + pd.Timedelta(days=1)
        )
    return pd.to_datetime(
        f"{date_str} {time_str}", format="%d%b%Y %H%M"
    )


def parse_native_sim_window(*paths):
    """Native (start, end) from the first 'Simulation Date=' line found.

    RAS keeps the line in the plan file (``.p01``); some models carry a
    copy in the ``.u01`` too, so pass both - the first hit wins.
    Returns ``(None, None)`` if no candidate has a parsable line.
    Must be called BEFORE ``update_plan_time`` /
    ``update_simulation_time`` rewrite those lines to the new window.
    """
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    if line.startswith("Simulation Date="):
                        parts = [
                            p.strip()
                            for p in line.split("=", 1)[1].split(",")
                        ]
                        if len(parts) < 4:
                            break
                        return (
                            _parse_ras_dt(parts[0], parts[1]),
                            _parse_ras_dt(parts[2], parts[3]),
                        )
        except (OSError, ValueError) as exc:
            print(
                f"Replay: could not parse Simulation Date in "
                f"{path.name} ({exc})"
            )
    return None, None


def replay_unchanged_bcs(
    boundary_conditions,
    u01_path,
    p01_path,
    plan_hdf,
    sim_start,
    sim_end,
    injections,
    bc_provenance,
):
    """Time-shift native data for 'Leave unchanged' BCs on a re-timed run.

    Appends one injection per replayable BC (native series re-anchored
    to ``sim_start``) so the standard DSS-write + .u01-repoint pipeline
    treats it like any other source.  Updates ``bc_provenance`` rows in
    place from ``fallback`` to ``replayed``.
    """
    unchanged = [
        bc for bc in boundary_conditions
        if str(bc.get("source", "none")).lower() in ("none", "unchanged")
        and bc.get("hdf_key")
    ]
    if not unchanged:
        return

    native_start, native_end = parse_native_sim_window(p01_path, u01_path)
    if native_start is None:
        print(
            "Replay: native Simulation Date unreadable - 'Leave "
            "unchanged' BCs left untouched (their data must cover the "
            "run window)."
        )
        return
    if native_start == sim_start and (
        native_end is None or native_end.date() == pd.Timestamp(sim_end).date()
    ):
        # Window matches the native run - the engine can keep reading
        # the model's own (preserved) data.  Truly unchanged.
        return

    shift = pd.Timestamp(sim_start) - native_start
    plan_hdf = Path(plan_hdf)
    if not plan_hdf.exists():
        print(
            "Replay: plan HDF missing - cannot read native BC tables; "
            "'Leave unchanged' BCs left untouched."
        )
        return

    print(
        f"Replay: window re-timed ({native_start:%d%b%Y} → "
        f"{pd.Timestamp(sim_start):%d%b%Y}, shift {shift.days:+d} d) - "
        f"time-shifting {len(unchanged)} 'Leave unchanged' BC(s) onto "
        f"the run window."
    )

    with h5py.File(plan_hdf, "r") as f:
        for bc in unchanged:
            key = bc["hdf_key"]
            name = (
                bc.get("name")
                or key.rsplit("/", 1)[-1]
            )
            if key not in f:
                print(f"Replay: {name}: key not in plan HDF - skipped")
                continue
            data = f[key][()]
            if (
                data is None or getattr(data, "ndim", 0) != 2
                or data.shape[0] < 2 or data.shape[1] < 2
            ):
                print(
                    f"Replay: {name}: no usable native table - skipped"
                )
                continue

            # Column 0 = days since the table's own anchor.  Prefer the
            # dataset's "Start Date" attr; fall back to the native sim
            # start (they coincide for GUI-computed models).
            anchor = native_start
            raw_sd = f[key].attrs.get("Start Date")
            if raw_sd is not None:
                try:
                    txt = (
                        raw_sd.decode()
                        if isinstance(raw_sd, bytes) else str(raw_sd)
                    )
                    d, _, t = txt.strip().partition(" ")
                    anchor = _parse_ras_dt(d, t)
                except (ValueError, AttributeError):
                    pass

            days = data[:, 0].astype(float)
            vals = data[:, 1].astype(float)
            times = (
                anchor + shift
                + pd.to_timedelta(days, unit="D")
            )
            df = pd.DataFrame({"datetime": times, "value": vals})
            injections.append({
                "hdf_key": key,
                "df": df,
                "unit_factor": 1.0,  # native table = model native units
                "label": f"{name} (replayed)",
                "bc_type": bc.get("bc_type"),
            })
            print(
                f"Replay: {name}: {len(vals)} native points "
                f"{times[0]} → {times[-1]} (model units, ×1)"
            )
            for row in bc_provenance:
                if (
                    row.get("name") == name
                    and row.get("status") == "fallback"
                ):
                    row.update({
                        "status": "replayed",
                        "source": "model",
                        "n_points": int(len(vals)),
                        "detail": (
                            f"native series time-shifted "
                            f"{shift.days:+d} d to the run window"
                        ),
                    })
                    break


# ========================================
# RAIN ON MESH (CONSTANT PRECIPITATION)
# ========================================
# HEC-RAS treats precipitation as mesh-wide meteorological forcing, not
# a perimeter BC line.  The Linux engine reads it from the plan HDF's
# ``Event Conditions/Meteorology/Precipitation`` group (the .b01 carries
# no precip lines - verified by diffing a GUI-computed constant-rain
# model against the baseline), so that write is the one the engine
# consumes; the .u01 text and .u01.hdf are patched too so the archived
# work model re-opens in the HEC-RAS GUI with rain already enabled.


def update_u01_precipitation(u01_path, value, units):
    """Enable Constant-mode precipitation in the .u01 text file."""
    u01_path = Path(u01_path)
    if not u01_path.exists():
        return
    with open(u01_path, "r") as f:
        lines = f.readlines()

    drop = (
        "Met BC=Precipitation|Mode=",
        "Met BC=Precipitation|Constant Value=",
        "Met BC=Precipitation|Constant Units=",
    )
    lines = [ln for ln in lines if not ln.startswith(drop)]

    new_block = [
        "Met BC=Precipitation|Mode=Constant\n",
        f"Met BC=Precipitation|Constant Value={value:g}\n",
        f"Met BC=Precipitation|Constant Units={units}\n",
    ]
    out = []
    injected = False
    has_mode_line = False
    for ln in lines:
        if ln.startswith("Precipitation Mode="):
            ln = "Precipitation Mode=Enable\n"
            has_mode_line = True
        if not injected and ln.startswith("Met BC=Precipitation|"):
            out.extend(new_block)
            injected = True
        out.append(ln)
    if not injected:
        if not has_mode_line:
            out.append("Precipitation Mode=Enable\n")
        out.extend(new_block)

    with open(u01_path, "w") as f:
        f.writelines(out)
    print(f"Unsteady file: constant precipitation enabled ({value:g} {units})")


def disable_precipitation(u01_path, plan_hdf, u01_hdf):
    """Turn OFF rain on mesh - even if the model ships with it enabled.

    The "Enable rain on mesh" toggle is the master switch: when it is
    off, the run must contain **no** mesh-wide precipitation, regardless
    of what the imported model defines.  Because HECinBOX runs
    RasUnsteady directly on the (already-computed) plan HDF rather than
    letting the GUI regenerate it from the .u01, a model that shipped
    with constant rain keeps flooding unless we strip the precipitation
    out of the plan HDF itself.

    Sets ``Precipitation Mode=Disable`` in the .u01 text file and, in
    both the plan HDF and the .u01.hdf, flips the Precipitation group's
    ``Enabled`` flag off and removes it from the Meteorology *Attributes*
    registry (the list RAS consults for active met forcings) plus its
    Timestamp/Values/2D-Flow-Areas data.
    """
    u01_path = Path(u01_path)
    if u01_path.exists():
        with open(u01_path, "r") as f:
            lines = f.readlines()
        out = []
        for ln in lines:
            if ln.startswith("Precipitation Mode="):
                ln = "Precipitation Mode=Disable\n"
            out.append(ln)
        with open(u01_path, "w") as f:
            f.writelines(out)
        print("Unsteady file: precipitation disabled")

    # The engine treats a *present* Precipitation group as active and
    # then fails ("Precipitation values not found") if its data is half
    # removed - so the group must be deleted outright, matching a model
    # that never had rain (whose plan HDF has no Meteorology group).
    for hp in (plan_hdf, u01_hdf):
        hp = Path(hp) if hp is not None else None
        if hp is None or not hp.exists():
            continue
        try:
            with h5py.File(hp, "a") as f:
                met = f.get("Event Conditions/Meteorology")
                if met is None:
                    continue
                if "Precipitation" in met:
                    del met["Precipitation"]
                # Drop Precipitation from the active-met registry too.
                if "Attributes" in met:
                    del met["Attributes"]
            print(f"{hp.name}: precipitation removed")
        except Exception as e:
            print(f"WARNING: could not disable precip in {hp.name}: {e}")


def update_hdf_precipitation(hdf_path, value, units, *, plan_style,
                             sim_start=None, sim_end=None):
    """Write Constant-mode precipitation into a RAS HDF.

    ``plan_style=False`` mirrors the .u01.hdf layout the GUI writes
    (``Enabled`` flag + constant attrs); ``plan_style=True`` mirrors the
    plan HDF layout RasUnsteady consumes - including the compute-time
    expansion the Windows GUI performs before launching the engine:
    hourly ``Timestamp``/``Values`` arrays across the simulation window
    and an every-cell/every-face → raster-cell-0 mapping per 2D flow
    area.  RasUnsteady refuses to run on the attrs alone
    (``READ_UN_MET_PRECIP_DATA: Precipitation values not found``).
    Attribute dtypes match a GUI-computed constant-rain model
    bit-for-bit - the engine's Fortran HDF reader is strict about them.
    """
    hdf_path = Path(hdf_path)
    if not hdf_path.exists():
        return
    with h5py.File(hdf_path, "a") as f:
        met = f.require_group("Event Conditions/Meteorology")
        g = met.require_group("Precipitation")
        # String attrs must be fixed-length (|S), not vlen - that's how
        # the GUI writes them and what the Fortran reader expects.
        g.attrs.create("Mode", np.bytes_("Constant"))
        g.attrs["Constant Value"] = np.float32(value)
        g.attrs.create("Constant Units", np.bytes_(units))
        if plan_style:
            g.attrs.create("Data Type", np.bytes_("PER-CUM"))
            # Depth unit of the rate: 'mm/hr' → 'mm', 'in/hr' → 'in'.
            g.attrs.create("Units", np.bytes_(units.split("/")[0]))
            # Raster placeholders exactly as RAS writes them for
            # non-gridded (constant) precipitation.
            g.attrs["Raster Cellsize"] = np.float64(3.5953862697246315e+307)
            g.attrs["Raster Cols"] = np.int32(1)
            g.attrs["Raster Rows"] = np.int32(1)
            g.attrs["Raster Left"] = np.float64(-1.7976931348623158e+307)
            g.attrs["Raster Top"] = np.float64(1.7976931348623158e+307)

            # Hourly stamps across the window; PER-CUM at a 1-hour
            # interval means each value is the depth accumulated that
            # hour, which for an <unit>/hr rate is the rate itself.
            stamps = pd.date_range(
                pd.Timestamp(sim_start).floor("h"),
                pd.Timestamp(sim_end).ceil("h"),
                freq="1h",
            )
            ts = np.array(
                [t.strftime("%d%b%Y %H:%M:%S.000").encode()
                 for t in stamps],
                dtype="S22",
            )
            vals = np.full((len(stamps), 1), value, dtype=np.float32)
            for name in ("Timestamp", "Values", "2D Flow Areas"):
                if name in g:
                    del g[name]
            g.create_dataset("Timestamp", data=ts)
            g.create_dataset("Values", data=vals)

            # Constant mode = a single virtual raster cell (index 0)
            # covering every cell and face of every 2D flow area with
            # weight 1.  Cell/face counts come from the Geometry group
            # embedded in the plan HDF ("Cell Count" excludes the
            # perimeter ghost cells; the full Faces table is used).
            fa_root = f.get("Geometry/2D Flow Areas")
            areas = g.create_group("2D Flow Areas")
            recs = (
                fa_root["Attributes"][...]
                if fa_root is not None and "Attributes" in fa_root
                else []
            )
            for rec in recs:
                area_name = rec["Name"].decode().strip()
                ag = fa_root.get(area_name)
                if ag is None:
                    continue
                try:
                    n_cells = int(rec["Cell Count"])
                except (KeyError, ValueError):
                    n_cells = int(ag["Cells Center Coordinate"].shape[0])
                n_faces = int(ag["Faces Cell Indexes"].shape[0])
                sub = areas.create_group(area_name)
                sub.create_dataset(
                    "Cell Indexes", data=np.zeros(n_cells, np.int32))
                sub.create_dataset(
                    "Cell Info",
                    data=np.column_stack([
                        np.arange(n_cells, dtype=np.int32),
                        np.ones(n_cells, np.int32),
                    ]))
                sub.create_dataset(
                    "Cell Weights", data=np.ones(n_cells, np.float32))
                sub.create_dataset(
                    "Face Indexes", data=np.zeros(n_faces, np.int32))
                sub.create_dataset(
                    "Face Info",
                    data=np.column_stack([
                        np.arange(n_faces, dtype=np.int32),
                        np.ones(n_faces, np.int32),
                    ]))
                sub.create_dataset(
                    "Face Weights", data=np.ones(n_faces, np.float32))
        else:
            g.attrs["Enabled"] = np.uint8(1)
        # Registry of enabled met variables - RAS uses this dataset to
        # know which Meteorology subgroups are active.
        rec = np.array(
            [(b"Precipitation",
              b"Event Conditions/Meteorology/Precipitation")],
            dtype=[("Variable", "S32"), ("Group", "S42")],
        )
        if "Attributes" in met:
            del met["Attributes"]
        met.create_dataset("Attributes", data=rec)
    print(
        f"{hdf_path.name}: constant precipitation written "
        f"({value:g} {units})"
    )


def update_hdf_gridded_precipitation(
    plan_hdf, *, grid_values, grid_left, grid_top, cellsize,
    timestamps, units="mm",
):
    """Write Gridded-mode precipitation into a plan HDF.

    The general case of :func:`update_hdf_precipitation` (which is the
    Cols=Rows=1 special case): instead of one virtual raster cell, a real
    raster grid is written and every mesh cell / face is mapped to the
    raster cell that contains it.

    The precip raster MUST already be expressed in the model's projected
    CRS (resample the source grid onto a model-CRS grid first), so the
    cell→raster mapping is a direct geotransform lookup - no per-runtime
    reprojection.  RasUnsteady consumes the precomputed
    ``Cell/Face Indexes`` mapping (proven by the constant-mode path), so
    it never needs the raster's own projection at compute time.

    Parameters
    ----------
    grid_values : (n_times, rows, cols) float array
        Per-hour accumulated depth (``units``) on a north-up grid whose
        top-left corner is (``grid_left``, ``grid_top``) with square
        ``cellsize`` (model CRS units).  Row 0 = north edge.
    timestamps : sequence of datetime-like
        One stamp per time slice (len == n_times).
    """
    plan_hdf = Path(plan_hdf)
    if not plan_hdf.exists():
        return
    gv = np.asarray(grid_values, dtype=np.float32)
    n_t, rows, cols = gv.shape
    n_raster = rows * cols
    # Row-major flatten: raster index = row * cols + col, row 0 = north.
    vals = gv.reshape(n_t, n_raster)

    def _to_idx(x, y):
        col = np.floor((x - grid_left) / cellsize).astype(np.int64)
        row = np.floor((grid_top - y) / cellsize).astype(np.int64)
        col = np.clip(col, 0, cols - 1)
        row = np.clip(row, 0, rows - 1)
        return (row * cols + col).astype(np.int32)

    with h5py.File(plan_hdf, "a") as f:
        met = f.require_group("Event Conditions/Meteorology")
        g = met.require_group("Precipitation")
        g.attrs.create("Mode", np.bytes_("Gridded"))
        g.attrs.create("Data Type", np.bytes_("PER-CUM"))
        g.attrs.create("Units", np.bytes_(units))
        g.attrs.create("Gridded Source", np.bytes_("DSS"))
        g.attrs["Raster Cellsize"] = np.float64(cellsize)
        g.attrs["Raster Cols"] = np.int32(cols)
        g.attrs["Raster Rows"] = np.int32(rows)
        g.attrs["Raster Left"] = np.float64(grid_left)
        g.attrs["Raster Top"] = np.float64(grid_top)

        ts = np.array(
            [pd.Timestamp(t).strftime("%d%b%Y %H:%M:%S.000").encode()
             for t in timestamps],
            dtype="S22",
        )
        for name in ("Timestamp", "Values", "2D Flow Areas"):
            if name in g:
                del g[name]
        g.create_dataset("Timestamp", data=ts)
        g.create_dataset("Values", data=vals)

        fa_root = f.get("Geometry/2D Flow Areas")
        areas = g.create_group("2D Flow Areas")
        recs = (
            fa_root["Attributes"][...]
            if fa_root is not None and "Attributes" in fa_root else []
        )
        for rec in recs:
            area_name = rec["Name"].decode().strip()
            ag = fa_root.get(area_name)
            if ag is None:
                continue
            try:
                n_cells = int(rec["Cell Count"])
            except (KeyError, ValueError):
                n_cells = int(ag["Cells Center Coordinate"].shape[0])
            centers = np.asarray(ag["Cells Center Coordinate"][:n_cells])
            cell_idx = _to_idx(centers[:, 0], centers[:, 1])

            # Face location = midpoint of its adjacent cell centres
            # (single cell for boundary faces).  Good enough to map a
            # face to a precip-raster cell.
            fci = np.asarray(ag["Faces Cell Indexes"])
            n_faces = fci.shape[0]
            all_c = np.asarray(ag["Cells Center Coordinate"])
            a = fci[:, 0].clip(0)
            b = fci[:, 1]
            b_valid = b >= 0
            fx = np.where(b_valid,
                          (all_c[a, 0] + all_c[np.where(b_valid, b, a), 0]) / 2,
                          all_c[a, 0])
            fy = np.where(b_valid,
                          (all_c[a, 1] + all_c[np.where(b_valid, b, a), 1]) / 2,
                          all_c[a, 1])
            face_idx = _to_idx(fx, fy)

            sub = areas.create_group(area_name)
            sub.create_dataset("Cell Indexes", data=cell_idx)
            sub.create_dataset(
                "Cell Info",
                data=np.column_stack([
                    np.arange(n_cells, dtype=np.int32),
                    np.ones(n_cells, np.int32),
                ]))
            sub.create_dataset(
                "Cell Weights", data=np.ones(n_cells, np.float32))
            sub.create_dataset("Face Indexes", data=face_idx)
            sub.create_dataset(
                "Face Info",
                data=np.column_stack([
                    np.arange(n_faces, dtype=np.int32),
                    np.ones(n_faces, np.int32),
                ]))
            sub.create_dataset(
                "Face Weights", data=np.ones(n_faces, np.float32))

        rec = np.array(
            [(b"Precipitation",
              b"Event Conditions/Meteorology/Precipitation")],
            dtype=[("Variable", "S32"), ("Group", "S42")],
        )
        if "Attributes" in met:
            del met["Attributes"]
        met.create_dataset("Attributes", data=rec)
    print(
        f"{plan_hdf.name}: gridded precipitation written "
        f"({rows}×{cols} grid, {n_t} steps, {units})"
    )


def _parse_unsteady_bc_types(u_path: Path) -> list[dict]:
    """Parse boundary condition types from the unsteady text file."""
    bcs: list[dict] = []
    if not u_path.exists():
        return bcs
    with open(u_path, "r", errors="replace") as fh:
        lines = fh.readlines()

    i = 0
    while i < len(lines):
        if lines[i].startswith("Boundary Location="):
            bc_type = None
            slope = 0.002
            first_val = 1000.0
            j = i + 1
            while j < len(lines) and not lines[j].startswith("Boundary Location="):
                ln = lines[j]
                if ln.startswith("Flow Hydrograph="):
                    bc_type = "flow"
                    k = j + 1
                    if k < len(lines):
                        try:
                            first_val = float(lines[k].split()[0])
                        except (ValueError, IndexError):
                            pass
                elif ln.startswith("Stage Hydrograph="):
                    bc_type = "stage"
                    k = j + 1
                    if k < len(lines):
                        try:
                            first_val = float(lines[k].split()[0])
                        except (ValueError, IndexError):
                            pass
                elif ln.startswith("Friction Slope="):
                    bc_type = "normal_depth"
                    try:
                        slope = float(ln.split("=")[1].strip())
                    except ValueError:
                        pass
                elif ln.startswith("Flow Hydrograph Slope="):
                    try:
                        slope = float(ln.split("=")[1].strip())
                    except ValueError:
                        pass
                j += 1

            if bc_type:
                bcs.append({"type": bc_type, "first_val": first_val, "slope": slope})
        i += 1
    return bcs


def _fmt_b_hydrograph_section(bcs: list[dict]) -> str:
    """Format the Hydrograph Data section of a .b file.

    Uses 2 constant data points per BC (matching the pattern used by
    HEC-RAS example models).  The actual time-varying data comes from
    the plan HDF, not from this text file.
    """
    if not bcs:
        return "Hydrograph Data\n       0\n"

    lines = [f"Hydrograph Data\n       {len(bcs)}\n"]

    for idx, bc in enumerate(bcs):
        if bc["type"] == "flow":
            v = int(bc["first_val"])
            lines.append("       F       F       T       F       F               F\n")
            lines.append(
                "Upstream Flow Hydrograph"
                " - River: Fake River  Reach: Fake Reach  RS: 100\n"
            )
            lines.append("       2\n")
            lines.append(f"       0{v:>8d}    8760{v:>8d}\n")
            lines.append(" 3.4E+38\n")

        elif bc["type"] == "stage":
            v = bc["first_val"]
            lines.append("       F       T       F       F       F\n")
            lines.append("Downstream Stage Hydrograph\n")
            lines.append("       2\n")
            lines.append(f"       0{v:>8.2f}    8760{v:>8.2f}\n")

        elif bc["type"] == "normal_depth":
            lines.append("       F       F       F       T       F\n")
            lines.append("Downstream Normal Depth\n")
            lines.append(f"    {bc['slope']}\n")

    return "".join(lines)


def _generate_boundary_file(b_path, plan_path, start, end,
                            u_path=None, model_dir=None, base_name=None):
    """Generate a .b boundary file from plan + unsteady text files.

    The HEC-RAS engine requires a .bNN file matching the plan suffix.
    When the data release omits it, we synthesize one from the plan's
    computation settings and the unsteady file's boundary conditions.
    """
    start_str = start.strftime("%d%b%Y")
    end_str = end.strftime("%d%b%Y")

    comp_interval = "5SEC"
    hydro_interval = "1MIN"
    detail_interval = "1HOUR"
    map_interval = "10MIN"
    proj_title = "HECinBOX"
    plan_title = ""
    plan_short = ""
    # The plan's "DSS File=" is often a bare placeholder ("dss").  The
    # Fortran reader chokes on it unless DSS output is enabled with a
    # valid <name>.dss filename, so synthesize one from the project base.
    dss_name = f"{base_name}.dss" if base_name else "output.dss"
    bc_interval_min = 5.0

    if Path(plan_path).exists():
        with open(plan_path, "r", errors="replace") as fh:
            for line in fh:
                if line.startswith("Computation Interval="):
                    comp_interval = line.split("=", 1)[1].strip()
                elif line.startswith("Output Interval="):
                    hydro_interval = line.split("=", 1)[1].strip()
                elif line.startswith("Instantaneous Interval="):
                    detail_interval = line.split("=", 1)[1].strip()
                elif line.startswith("Mapping Interval="):
                    map_interval = line.split("=", 1)[1].strip()
                elif line.startswith("Plan Title="):
                    plan_title = line.split("=", 1)[1].strip()
                elif line.startswith("Short Identifier="):
                    plan_short = line.split("=", 1)[1].strip()

    bcs: list[dict] = []
    if u_path and Path(u_path).exists():
        bcs = _parse_unsteady_bc_types(Path(u_path))

    hydro_section = _fmt_b_hydrograph_section(bcs)

    content = f"""\
HEC-RAS 7.0 April 2026
       1       1       0       0
       0       0
       F
       1
Initial Conditions Flow Information
       1        Initial Profile
 3.1E+38      13
Flow and Seasonal Roughness Flag (plan)
       F       F
       2       0       0
       3       0    .001
 3.1E+38
       1       1       0       0
       0       0
       F
Project Title, Plan Title and Plan ShortID
{proj_title}
{plan_title}
{plan_short:<64s}
Job Control Information
  Computation Interval  = {comp_interval}
  Warmup Interval       =  0
  Instantaneous Profile = {detail_interval}
  Hydrograph Interval   = {hydro_interval}
  Theta Simulation      =        1
  Theta Warmup          =        1
  Friction Slope Method =        2
  Maximum Iterations    =       20
  Max Iter WOImprovement=        0
  Number Warmup Steps   =       20
  Abort DZ Tolerance    =      100
  DZ Tolerance          =      .02
  DZSA Tolerance        =      .02
  DQ Tolerance          =  3.1E+38
  Weir Flow Stability   =        2
  Spillway Stability    =        1
  Write Restart File    =        F       F
  Echo Input TS         =        F
  Echo Parameters       =        F
  Echo Output TS        =        F
  DSS Message Level     =        4
  Write HDF5 File       =        T
  Write DSS File        =        T
{dss_name}
Computational Time Window
  Start Date/Time       = {start_str} 0000
  End Date/Time         = {end_str} 2300
Initial Conditions (use restart file?)
       F
Log File Information
       F       0       0
Computation Level Output
       F-3.4E+38 3.4E+38       F       F       F       F       F       F       F       F       F   {map_interval}
Mixed Flow - Acceleration term reduction based on Froude number
       F      10       1       1
Number of Gate Groups and Internal Boundaries with Gates
       0       0       0       0
Breach Data
       0
{hydro_section}\
Internal Observed Stage/Flow Boundaries
       0
Ground Water Interflows
       0
Old River Diversions
       F
Lateral Inflows, Ungaged Lateral Inflows, Outlet TS, and Observed DSS
       0       0       0       0
Stage and Flow Boundary and Ungaged Areas
       0       0
Time Slicing Parameters
       F
HYDROGRAPH LOCATIONS
 0
Rules (number of rule sets, number of lookbacks, number of tables)
       0       0       0
Extra Commands
       0
"""
    # HEC-RAS .b files use CRLF line terminators; the Fortran fixed-format
    # reader expects them even on Linux.
    with open(b_path, "w", newline="\r\n") as f:
        f.write(content)
    print(f"Generated boundary file: {b_path.name}")


def update_b01_time(b01_path, start, end):
    if not Path(b01_path).exists():
        return

    start_str = start.strftime("%d%b%Y")
    end_str = end.strftime("%d%b%Y")

    with open(b01_path, "r") as f:
        lines = f.readlines()

    out = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "Computational Time Window":
            out.append(lines[i])
            out.append(f"  Start Date/Time       = {start_str} 0000\n")
            out.append(f"  End Date/Time         = {end_str} 2300\n")
            i += 3
        else:
            out.append(lines[i])
            i += 1

    with open(b01_path, "w", newline="\r\n") as f:
        f.writelines(out)

    print("Dates updated in boundary (.b) file")


# ========================================
# DATA FETCH HELPERS
# ========================================
def _shift_to_model_tz(df, offset_hours):
    """Shift a fetched (UTC) series onto the model's clock (LST).

    USGS, NOAA (fetched as ``gmt``) and the forecast clients all return
    UTC timestamps; the model window is Local Standard Time, so add the
    offset to align them.  A no-op when the offset is 0 (UTC base) or
    the frame is empty.
    """
    if df is None or getattr(df, "empty", True) or not offset_hours:
        return df
    df = df.copy()
    df["datetime"] = (
        pd.to_datetime(df["datetime"]) + pd.Timedelta(hours=offset_hours)
    )
    return df


def _fetch_series(spec, sim_start, end, usgs, noaa, label,
                  offset_hours=0):
    source = str(spec.get("source", "none")).lower()
    station = str(spec.get("station", "")).strip()
    if source in ("usgs", "noaa") and not station:
        print(
            f"WARNING: {label}: source is '{source}' but no station ID "
            f"was provided - skipping fetch, leaving this BC at the "
            f"model's original values."
        )
        return None
    # Fetch a day wider on each side so the offset shift can't leave the
    # window edges unfilled, then trim happens downstream on alignment.
    _fs = pd.to_datetime(sim_start) - pd.Timedelta(days=1)
    _fe = pd.to_datetime(end) + pd.Timedelta(days=1)
    if source == "usgs":
        df = usgs.fetch(
            site=station,
            parameter_cd=str(spec.get("parameter", "00060")),
            startDT=_fs,
            endDT=_fe,
            label=label,
        )
        return _shift_to_model_tz(df, offset_hours)
    if source == "noaa":
        # Always fetch UTC ("gmt") and apply the model's single LST
        # offset, so NOAA aligns identically to USGS/forecast.
        df = noaa.fetch_water_level(
            station=station,
            startDT=_fs,
            endDT=_fe,
            datum=spec.get("datum", "NAVD"),
            units=spec.get("units", "english"),
            time_zone="gmt",
            label=label,
        )
        return _shift_to_model_tz(df, offset_hours)
    return None


def re_safe(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(name))[:60]


# ========================================
# ENTRY POINT 1: RUN (engine only)
# ========================================
def run():
    """Fetch BCs → patch model → run engine → extract & save raw results."""
    _progress(2, "Loading configuration")
    settings_path = os.environ.get("SETTINGS_PATH", "/app/settings.yml")
    config = load_config(settings_path)

    start = pd.to_datetime(config.raw["simulation"]["start"])
    end = pd.to_datetime(config.raw["simulation"]["end"])
    # Engine runs over the same window the user requested - no implicit
    # warmup.  The ``sim_start`` alias is retained because many downstream
    # helpers (BC fetch / patch / HDF writers) take it as their reference
    # instant; keeping the name avoids a sweeping rename for no benefit.
    sim_start = start
    print(f"Simulation window: {start} -> {end}")

    hecras_cfg = config.hecras
    project_name = hecras_cfg["project_name"]
    base_name = project_name.rsplit(".", 1)[0]
    plan_suffix = hecras_cfg.get("plan_suffix", "p01")
    geom_suffix = hecras_cfg.get("geom_suffix", "g01")
    unsteady_suffix = hecras_cfg.get("unsteady_suffix", "u01")
    exec_suffix = "x" + plan_suffix[1:]
    flow_area_name = hecras_cfg.get("flow_area_name", "")

    model_dir = Path(hecras_cfg["model_dir"])

    # ── Model unit system - single source of truth for every source→model
    # conversion below.  Prefer the value the app detected and wrote into
    # settings; fall back to reading the .prj directly so older settings
    # files / schedule templates still convert correctly.  Unknown → SI
    # (the .prj always carries the token, and SI matches the historical
    # USGS/NOAA conversion path).
    _unit_system = hecras_cfg.get("unit_system")
    if not _unit_system:
        try:
            from model_scanner import _parse_prj_unit_system
            _unit_system = _parse_prj_unit_system(model_dir / project_name)
        except Exception:
            _unit_system = None
    model_is_si = str(_unit_system).upper() != "ENGLISH"
    print(
        f"Model unit system: {_unit_system or 'unknown'} - converting "
        f"boundary data to {'SI (m, m³/s)' if model_is_si else 'English (ft, cfs)'}"
    )
    output_dir = Path(hecras_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # ── STEP 1: writable workspace ──
    _progress(5, "Creating writable workspace")
    workspace = Path(tempfile.mkdtemp(prefix="hecras_run_"))
    work_model = workspace / "model"
    shutil.copytree(model_dir, work_model)
    print(f"Workspace created: {workspace}")

    ras_text_exts = (
        "*.prj", "*.p[0-9]*", "*.g[0-9]*", "*.u[0-9]*",
        "*.b[0-9]*", "*.x[0-9]*", "*.rasmap", "*.ic.*",
    )
    text_files = []
    for pat in ras_text_exts:
        text_files.extend(glob.glob(str(work_model / pat)))
    if text_files:
        subprocess.run(["dos2unix", "--quiet", *text_files], check=False)
        print(f"dos2unix: converted {len(text_files)} model text files")

    # ── STEP 2: fetch BC data ──
    usgs = USGSClient()
    noaa = NOAAClient()

    # "Active" BCs are anything other than fallback ("none").
    # USGS / NOAA fetch real data;  "constant" synthesizes a flat
    # time series at the requested value (model native units).
    bc_specs = [
        bc for bc in config.boundary_conditions
        if str(bc.get("source", "none")).lower() in
        ("usgs", "noaa", "constant", "forecast")
    ]
    if not bc_specs:
        print("No boundary conditions configured with a data source - "
              "using model defaults")

    # Single model time base: fetched data (UTC) is shifted to the
    # model's Local Standard Time so it aligns with the sim window.
    _tz_off = config.time_offset_hours
    if _tz_off:
        print(f"Model time base: Local Standard Time = UTC{_tz_off:+d}h "
              f"(all fetched data shifted to match the sim window).")

    injections = []
    step = 0
    for bc in bc_specs:
        step += 1
        label = bc.get("name", bc.get("hdf_key", f"bc{step}"))
        _src = str(bc.get("source", "")).lower()
        _progress(
            5 + int(10 * step / max(len(bc_specs), 1)),
            f"Building {_src} series for {label}",
        )

        if _src == "constant":
            # Synthesize a flat time series at the BC's interval.
            # The actual interval gets honoured downstream when the
            # series is resampled to the .u01's Interval= line, so
            # two anchor points (start + end) are sufficient here.
            try:
                value = float(bc.get("constant_value", 0.0))
            except Exception:
                value = 0.0
            df = pd.DataFrame({
                "datetime": [pd.Timestamp(sim_start),
                             pd.Timestamp(end)],
                "value": [value, value],
            })
            print(
                f"{label}: constant {value} "
                f"({bc.get('constant_unit_label') or 'model native'}) "
                f"applied for the full window."
            )
            usgs.save(df, raw_dir / f"bc_{re_safe(label)}.csv")
            injections.append({
                "hdf_key": bc["hdf_key"],
                "df": df,
                "unit_factor": 1.0,  # already in model native units
                "label": label,
            })
            continue

        if _src == "forecast":
            # NWM streamflow / NWM+rating / STOFS TWL (v3.0.0).
            # Dispatcher returns a clean (datetime, value) frame
            # already in the units the BC type expects:
            #   nwm_q              → m³/s  (flow BC)
            #   nwm_q_*_rating     → ft    (stage BC, via rating)
            #   stofs_twl          → m     (stage BC, total water level)
            try:
                from forecast_client import fetch_forecast_series
                df = fetch_forecast_series(bc, sim_start, end)
                df = _shift_to_model_tz(df, _tz_off)
            except Exception as e:
                print(
                    f"WARNING: {label}: forecast fetch failed ({e}) - "
                    f"leaving this BC at the model's original values."
                )
                df = None
            # Per-product unit factor, driven by the model's unit system.
            # Forecast clients return: NWM Q in m³/s, STOFS TWL in m, and
            # rating-curve stage in ft.  Convert each to the model's native
            # units (SI → leave m/m³/s as-is, feet→m; English → m³/s→cfs,
            # m→ft, leave ft as-is).
            _fprod = str(bc.get("forecast_product", "")).lower()
            if _fprod == "nwm_q":                       # m³/s
                _uf = 1.0 if model_is_si else CMS_TO_CFS
            elif _fprod == "stofs_twl":                 # m
                _uf = 1.0 if model_is_si else M_TO_FT
            else:                                       # rating-curve stage in ft
                _uf = FT_TO_M if model_is_si else 1.0
            if df is not None and not df.empty:
                print(
                    f"{label}: {len(df)} forecast records "
                    f"({_fprod}) {df['datetime'].min()} -> "
                    f"{df['datetime'].max()}"
                )
                usgs.save(df, raw_dir / f"bc_{re_safe(label)}.csv")
            injections.append({
                "hdf_key": bc["hdf_key"],
                "df": df,
                "unit_factor": _uf,
                "label": label,
            })
            continue

        try:
            df = _fetch_series(
                bc, sim_start, end, usgs, noaa, label, _tz_off
            )
        except Exception as e:
            print(
                f"WARNING: {label}: data fetch failed ({e}) - leaving "
                f"this BC at the model's original values and continuing."
            )
            df = None
        if df is not None and not df.empty:
            print(
                f"{label}: {len(df)} records "
                f"{df['datetime'].min()} -> {df['datetime'].max()}"
            )
            usgs.save(df, raw_dir / f"bc_{re_safe(label)}.csv")
        injections.append({
            "hdf_key": bc["hdf_key"],
            "df": df,
            "unit_factor": _unit_factor(
                bc.get("bc_type", "flow"),
                str(bc.get("source", "")).lower(),
                bc.get("units", "english"),
                model_is_si,
            ),
            "label": label,
        })

    # ── Build BC provenance for summary.txt ──────────────────────────
    # One row per *configured* BC (not just successful fetches) so the
    # user can tell at a glance whether the engine ran on fresh data or
    # fell back to the model defaults for any given boundary.
    bc_provenance: list[dict] = []
    _inj_by_key = {i["hdf_key"]: i for i in injections}
    for bc in config.boundary_conditions:
        name = bc.get("name") or bc.get("hdf_key", "?").rsplit("/", 1)[-1]
        source = str(bc.get("source", "none")).lower()
        station = str(bc.get("station", "")).strip()
        if source == "forecast":
            _fprod = str(bc.get("forecast_product", "?"))
            _inj = _inj_by_key.get(bc.get("hdf_key", ""))
            _df = _inj.get("df") if _inj else None
            if _df is None or _df.empty:
                bc_provenance.append({
                    "name": name, "status": "fallback",
                    "source": "forecast", "station": _fprod,
                    "detail": "forecast fetch returned no data",
                })
            else:
                bc_provenance.append({
                    "name": name, "status": "forecast",
                    "source": "forecast", "station": _fprod,
                    "n_points": int(len(_df)),
                })
            continue
        if source == "constant":
            _cv = bc.get("constant_value", "?")
            _cu = bc.get("constant_unit_label") or "model native"
            bc_provenance.append({
                "name": name, "status": "constant",
                "source": "constant",
                "detail": f"{_cv} {_cu}",
            })
            continue
        if source not in ("usgs", "noaa"):
            bc_provenance.append({
                "name": name, "status": "fallback",
                "detail": f"source={source}",
            })
            continue
        if not station:
            bc_provenance.append({
                "name": name, "status": "fallback",
                "source": source, "detail": "no station ID",
            })
            continue
        inj = _inj_by_key.get(bc.get("hdf_key", ""))
        df = inj.get("df") if inj else None
        if df is None or df.empty:
            bc_provenance.append({
                "name": name, "status": "fallback",
                "source": source, "station": station,
                "detail": "fetch returned no data",
            })
        else:
            bc_provenance.append({
                "name": name, "status": "fetched",
                "source": source, "station": station,
                "n_points": int(len(df)),
            })

    # ── STEP 3: patch simulation window + inject BCs ──
    _progress(16, "Patching simulation window into model files")
    u01_path = work_model / f"{base_name}.{unsteady_suffix}"
    if not u01_path.exists():
        candidates = list(work_model.rglob(f"{base_name}.{unsteady_suffix}"))
        if candidates:
            u01_path = candidates[0]
            print(f"Unsteady file found in subdirectory: {u01_path.relative_to(work_model)}")

    # "Leave unchanged" BCs on a re-timed window: replay the native
    # series (time-shifted) instead of letting the engine look up
    # absolute 20XX timestamps that no longer overlap the run window.
    # Must run BEFORE update_simulation_time overwrites the native
    # 'Simulation Date=' line we read the original window from.
    replay_unchanged_bcs(
        boundary_conditions=config.boundary_conditions,
        u01_path=u01_path,
        p01_path=work_model / f"{base_name}.{plan_suffix}",
        plan_hdf=work_model / f"{base_name}.{plan_suffix}.hdf",
        sim_start=sim_start,
        sim_end=end,
        injections=injections,
        bc_provenance=bc_provenance,
    )

    update_simulation_time(u01_path, sim_start, end)

    # ── STEP 3a: write fetched BCs as fresh DSS records ──
    # The HEC-RAS Linux engine reads BC time series from the DSS file
    # referenced by ``DSS File=`` / ``DSS Path=`` inside each
    # ``Boundary Location=`` block of the ``.u01``.  Patching the plan
    # HDF (done below) alone is ignored: the engine re-reads from DSS
    # at runtime and overwrites the HDF input snapshot.  Without this
    # step every simulation produces byte-identical hydraulics because
    # the engine keeps consuming the model-bundled calibration DSS.
    _progress(17, "Writing fetched BCs to fresh DSS file")
    from dss_writer import (  # noqa: WPS433 - local import keeps tests light
        collect_interval_map,
        update_unsteady_dss_paths,
        write_boundary_dss,
    )
    bc_intervals = collect_interval_map(u01_path)
    if bc_intervals:
        print(f"BC intervals found in .u01: {bc_intervals}")
    name_to_dss_path = write_boundary_dss(
        workspace_model_dir=work_model,
        injections=injections,
        sim_start=sim_start,
        sim_end=end,
        bc_intervals=bc_intervals,
        u01_path=u01_path,
    )
    if name_to_dss_path:
        update_unsteady_dss_paths(
            u01_path=u01_path,
            bc_name_to_dss_path=name_to_dss_path,
            dss_filename="boundary.dss",
        )
    else:
        print(
            "DSS: no records written - engine will fall back to whatever "
            "the model files reference (likely the original calibration "
            "data).  Check the BC fetch logs above."
        )

    p01_path = work_model / f"{base_name}.{plan_suffix}"
    update_plan_time(p01_path, sim_start, end)

    b_suffix = f"b{plan_suffix[1:]}"
    b01_path = work_model / f"{base_name}.{b_suffix}"
    if not b01_path.exists():
        b_donors = sorted(work_model.glob(f"{base_name}.b[0-9][0-9]"))
        if b_donors:
            shutil.copy2(b_donors[0], b01_path)
            print(f"Copied {b_donors[0].name} → {b01_path.name}")
        else:
            _generate_boundary_file(
                b01_path, p01_path, sim_start, end,
                u_path=u01_path, model_dir=work_model,
                base_name=base_name,
            )
    update_b01_time(b01_path, sim_start, end)

    plan_hdf = work_model / f"{base_name}.{plan_suffix}.hdf"
    run_geom = hecras_cfg.get("run_geom_preprocess", False)

    if not plan_hdf.exists():
        geom_hdf = work_model / f"{base_name}.{geom_suffix}.hdf"
        if not geom_hdf.exists():
            raise FileNotFoundError(
                f"Neither plan HDF ({plan_hdf.name}) nor geometry HDF "
                f"({geom_hdf.name}) found in model directory."
            )
        _progress(19, "Creating plan HDF from geometry (first run)")
        _seed_plan_hdf(geom_hdf, plan_hdf, sim_start, end)
        run_geom = True
        print(f"Seeded plan HDF from {geom_hdf.name}")

    if plan_hdf.exists():
        update_plan_hdf_time(plan_hdf, sim_start, end)
        update_plan_hdf_boundary_data(plan_hdf, injections, sim_start)

    # ── STEP 3b: rain on mesh - constant precipitation ──
    precip_cfg = config.precipitation
    _pmode = str(precip_cfg.get("mode", "constant")).lower()
    if precip_cfg.get("enabled") and _pmode == "unchanged":
        # "Leave unchanged" - keep the model's own precipitation.  A
        # constant rate still needs its plan-HDF Timestamp/Values
        # expansion regenerated: the arrays baked into the plan cover
        # the window the model was last computed for, so a re-timed run
        # would otherwise see no rain.  Non-constant (Gridded/Point)
        # precip is passed through untouched.
        from model_scanner import parse_u01_precipitation
        _det = parse_u01_precipitation(u01_path)
        if (
            _det.get("enabled")
            and str(_det.get("mode") or "").lower() == "constant"
            and _det.get("constant_value") is not None
        ):
            _pv = float(_det["constant_value"])
            _pu = str(_det.get("constant_units") or "mm/hr")
            _progress(18, f"Re-timing model's own rain ({_pv:g} {_pu})")
            update_hdf_precipitation(
                plan_hdf, _pv, _pu, plan_style=True,
                sim_start=sim_start, sim_end=end,
            )
            bc_provenance.append({
                "name": "Rain on Mesh", "status": "constant",
                "source": "model", "station": "",
                "detail": (
                    f"model's own {_pv:g} {_pu} re-timed to the "
                    f"run window"
                ),
            })
        elif _det.get("enabled"):
            print(
                "Rain on mesh: model uses "
                f"{_det.get('mode') or 'unknown'}-mode precipitation - "
                "left untouched (its data must cover the simulation "
                "window or the engine will see no rain)"
            )
            bc_provenance.append({
                "name": "Rain on Mesh", "status": "constant",
                "source": "model", "station": "",
                "detail": (
                    f"model's own {_det.get('mode') or 'unknown'} "
                    f"precipitation left untouched"
                ),
            })
        else:
            print(
                "Rain on mesh: 'Leave unchanged' selected but the model "
                "has precipitation disabled - no rain applied"
            )
    elif precip_cfg.get("enabled") and _pmode == "gridded":
        # Spatially-varying rain from a gridded source (AORC hindcast /
        # HRRR forecast).  Fetch, resample onto a model-CRS grid, and
        # write the real raster into the plan HDF.  A fetch failure must
        # not break the run - fall back to no rain on mesh.
        _src = str(precip_cfg.get("source", "aorc")).lower()
        _src_label = (
            "DSS file" if _src == "dss" else _src.upper()
        )
        _progress(18, f"Fetching gridded rain on mesh ({_src_label})")
        try:
            from precip_gridded import fetch_gridded
            with h5py.File(plan_hdf, "r") as _pf:
                _wkt = ""
                if "Projection" in _pf.attrs:
                    _raw = _pf.attrs["Projection"]
                    _wkt = (
                        _raw.decode() if isinstance(_raw, bytes)
                        else str(_raw)
                    )
                _fa = _pf.get("Geometry/2D Flow Areas")
                _areas = [
                    k for k in _fa.keys()
                    if isinstance(_fa[k], h5py.Group)
                ] if _fa is not None else []
                _C = np.concatenate([
                    np.asarray(_fa[a]["Cells Center Coordinate"])
                    for a in _areas
                ])
            if not _wkt:
                # Fallback: the terrain VRT carries the model CRS.
                for _vrt in work_model.rglob("*.vrt"):
                    _txt = _vrt.read_text(errors="ignore")
                    if "<SRS" in _txt:
                        _wkt = _txt.split("<SRS", 1)[1].split(
                            ">", 1)[1].split("</SRS>")[0]
                        break
            _bbox = (
                float(_C[:, 0].min()), float(_C[:, 1].min()),
                float(_C[:, 0].max()), float(_C[:, 1].max()),
            )
            if _src == "dss":
                # User-supplied DSS is staged to a writable app dir and read
                # by absolute path (RAS never consumes it - we only read it
                # here to build the plan-HDF grid).  Fall back to the old
                # work_model location for settings written by older UIs.
                _dss_file = precip_cfg.get("dss_path") or str(
                    work_model / str(precip_cfg.get("dss_filename", ""))
                )
                _gp = fetch_gridded(
                    "dss", _wkt, _bbox, sim_start, end,
                    dss_file=str(_dss_file),
                    grid_pattern=str(precip_cfg.get("grid_path", "")),
                    interp=str(precip_cfg.get("interp", "nearest")).lower(),
                )
            else:
                # HRRR decodes GRIB messages one forecast-hour at a time
                # (slow) - stream "reading hour i/n" to the progress bar so
                # the user can tell it's working, not hung.
                def _hrrr_progress(_i, _n):
                    _progress(
                        18,
                        f"Fetching gridded rain on mesh ({_src_label}) "
                        f"- reading hour {_i}/{_n}",
                    )
                _gp = fetch_gridded(
                    _src, _wkt, _bbox, sim_start, end,
                    progress=_hrrr_progress if _src == "hrrr" else None,
                )
            update_hdf_gridded_precipitation(
                plan_hdf, grid_values=_gp.values, grid_left=_gp.left,
                grid_top=_gp.top, cellsize=_gp.cellsize,
                timestamps=_gp.timestamps, units=_gp.units,
            )
            bc_provenance.append({
                "name": "Rain on Mesh", "status": "gridded",
                "source": _src, "station": "",
                "detail": (
                    f"{_gp.source} gridded precip · "
                    f"{_gp.values.shape[1]}×{_gp.values.shape[2]} grid · "
                    f"peak {float(_gp.values.max()):.1f} {_gp.units}/hr"
                ),
            })
        except Exception as _ge:
            print(
                f"WARNING: gridded precip ({_src}) failed - {_ge}; "
                f"running without rain on mesh"
            )
            bc_provenance.append({
                "name": "Rain on Mesh", "status": "gridded-failed",
                "source": _src, "station": "",
                "detail": f"{_src.upper()} fetch failed: {_ge}",
            })
    elif precip_cfg.get("enabled"):
        _pv = float(precip_cfg.get("constant_value", 0.0))
        _pu = str(precip_cfg.get("constant_units", "mm/hr"))
        _progress(18, f"Enabling rain on mesh ({_pv:g} {_pu})")
        update_u01_precipitation(u01_path, _pv, _pu)
        update_hdf_precipitation(
            Path(f"{u01_path}.hdf"), _pv, _pu, plan_style=False,
        )
        update_hdf_precipitation(
            plan_hdf, _pv, _pu, plan_style=True,
            sim_start=sim_start, sim_end=end,
        )
        bc_provenance.append({
            "name": "Rain on Mesh", "status": "constant",
            "source": "constant", "station": "",
            "detail": f"{_pv:g} {_pu} uniform over all 2D flow areas",
        })
    else:
        # Toggle OFF - the master switch is off, so this run must have no
        # rain on mesh even if the imported model ships with it enabled.
        # Strip any native precipitation out of the plan HDF the engine
        # reads (otherwise a ready model floods the whole domain).
        from model_scanner import parse_u01_precipitation
        _det = parse_u01_precipitation(u01_path)
        if _det.get("enabled"):
            _progress(18, "Rain on mesh OFF - disabling model's native rain")
            disable_precipitation(
                u01_path, plan_hdf, Path(f"{u01_path}.hdf")
            )
            bc_provenance.append({
                "name": "Rain on Mesh", "status": "off",
                "source": "disabled", "station": "",
                "detail": "model's native rain on mesh disabled for this run",
            })

    # Ensure the execution file exists (.xNN).  The .x file is geometry
    # preprocessor output and MUST match the geometry the plan uses.
    # Models often ship .x files only for some plans/geometries, so a
    # donor copy from a different plan can be the WRONG geometry (e.g.
    # plan p01 uses g02 but p62 uses g01).  Feeding RasUnsteady a
    # mismatched .x file corrupts the boundary-file read.  When the
    # plan's own .x file is absent, force the geometry preprocessor to
    # regenerate it from the geometry embedded in the plan HDF.
    exec_file = work_model / f"{base_name}.{exec_suffix}"
    if not exec_file.exists():
        donors = sorted(work_model.glob(f"{base_name}.x[0-9][0-9]"))
        if donors:
            shutil.copy2(donors[0], exec_file)
            print(f"Copied {donors[0].name} → {exec_file.name} (seed)")
        else:
            print("No execution file found - geometry preprocessor will create it")
        if not run_geom:
            run_geom = True
            print(
                f"Forcing geometry preprocessor: no native {exec_file.name} "
                f"for this plan's geometry ({geom_suffix})"
            )

    # ── STEP 4: run HEC-RAS engine ──
    _progress(
        20,
        "Running HEC-RAS unsteady engine - live progress below",
    )
    _nt = hecras_cfg.get("num_threads")
    try:
        _nt = int(_nt) if _nt is not None else None
    except (TypeError, ValueError):
        _nt = None
    results_hdf = run_hecras_linux(
        project_dir=work_model,
        plan_hdf=plan_hdf,
        plan_suffix=plan_suffix,
        exec_suffix=exec_suffix,
        run_geom_preprocess=run_geom,
        timeout_s=hecras_cfg.get("unsteady_timeout_seconds") or 7200,
        sim_start=sim_start.to_pydatetime(),
        sim_end=end.to_pydatetime(),
        num_threads=_nt,
    )

    # ── STEP 5: extract results ──
    _progress(91, "Extracting results from HDF")
    with h5py.File(results_hdf, "r") as f:
        wse_key = (
            f"Results/Unsteady/Output/Output Blocks/Base Output/"
            f"Unsteady Time Series/2D Flow Areas/{flow_area_name}/"
            f"Water Surface"
        )
        coords_key = (
            f"Geometry/2D Flow Areas/{flow_area_name}/"
            f"Cells Center Coordinate"
        )
        time_key = (
            "Results/Unsteady/Output/Output Blocks/Base Output/"
            "Unsteady Time Series/Time Date Stamp"
        )
        wse = f[wse_key][:]
        coords = f[coords_key][:]
        time_stamps = f[time_key][:]
        proj_wkt = ""
        if "Projection" in f.attrs:
            raw = f.attrs["Projection"]
            proj_wkt = raw.decode() if isinstance(raw, bytes) else str(raw)

        # Per-cell velocity magnitude, averaged from the cell's faces.
        vel = None
        try:
            geom_root = f"Geometry/2D Flow Areas/{flow_area_name}"
            fv_key = (
                f"Results/Unsteady/Output/Output Blocks/Base Output/"
                f"Unsteady Time Series/2D Flow Areas/{flow_area_name}/"
                f"Face Velocity"
            )
            if fv_key in f and f"{geom_root}/Cells Face and Orientation Info" in f:
                face_vel = np.abs(f[fv_key][:])  # (n_time, n_faces)
                cf_info = f[f"{geom_root}/Cells Face and Orientation Info"][:]
                cf_vals = f[f"{geom_root}/Cells Face and Orientation Values"][:]
                n_cells = cf_info.shape[0]
                vel = np.zeros((face_vel.shape[0], n_cells), dtype=np.float32)
                for c in range(n_cells):
                    s, n = int(cf_info[c, 0]), int(cf_info[c, 1])
                    if n > 0:
                        fids = cf_vals[s:s + n, 0]
                        vel[:, c] = face_vel[:, fids].mean(axis=1)
                print(f"Extracted per-cell velocity {vel.shape}")
        except Exception as e:
            print(f"Velocity extraction skipped: {e}")
            vel = None

        # Per-cell minimum terrain elevation (for water-depth maps).
        min_elev = None
        try:
            me_key = (
                f"Geometry/2D Flow Areas/{flow_area_name}/"
                f"Cells Minimum Elevation"
            )
            if me_key in f:
                min_elev = f[me_key][:].astype(np.float32)  # (n_cells,)
                print(f"Extracted per-cell min elevation {min_elev.shape}")
        except Exception as e:
            print(f"Min-elevation extraction skipped: {e}")
            min_elev = None

        # Cell polygons (facepoint rings) for filled inundation maps.
        cell_fp = None
        fp_xy = None
        try:
            fp_idx_key = (
                f"Geometry/2D Flow Areas/{flow_area_name}/"
                f"Cells FacePoint Indexes"
            )
            fp_xy_key = (
                f"Geometry/2D Flow Areas/{flow_area_name}/"
                f"FacePoints Coordinate"
            )
            if fp_idx_key in f and fp_xy_key in f:
                cell_fp = f[fp_idx_key][:].astype(np.int32)
                fp_xy = f[fp_xy_key][:].astype(np.float64)
                print(
                    f"Extracted cell polygons {cell_fp.shape} / "
                    f"facepoints {fp_xy.shape}"
                )
        except Exception as e:
            print(f"Cell-polygon extraction skipped: {e}")
            cell_fp = None
            fp_xy = None

    # HEC-RAS stores WSE / velocity / elevation in the model's *native*
    # computational units (SI → metres & m/s; English → feet & ft/s).
    # We keep them native and label every output from the detected unit
    # system - no blanket metres→feet conversion.  (The old code used the
    # horizontal CRS projection unit to decide a vertical-value
    # conversion, which conflated two independent unit systems.)
    print(
        f"Results kept in model-native units: "
        f"{'metres / m·s⁻¹ (SI)' if model_is_si else 'feet / ft·s⁻¹ (English)'}"
    )

    model_time = pd.to_datetime(
        [
            t.decode("utf-8") if isinstance(t, bytes) else t
            for t in time_stamps
        ],
        format="%d%b%Y %H:%M:%S",
    )

    # Defensive: drop any output rows the engine may emit before the
    # requested start (rare; most plans write the first row at start).
    analysis_mask = model_time >= start
    if not analysis_mask.all():
        model_time = model_time[analysis_mask]
        wse = wse[analysis_mask]
        if vel is not None:
            vel = vel[analysis_mask]
        print(
            f"Dropped {(~analysis_mask).sum()} pre-start row(s); kept "
            f"{analysis_mask.sum()} timesteps."
        )

    # Save raw extracted data for later validation
    _npz_payload = dict(
        wse=wse,
        coords=coords,
        model_time=model_time.values.astype("int64"),
        proj_wkt=np.array([proj_wkt]),
        # Self-describing unit system so any consumer (Tab 5/6/7,
        # re-opened folders, cloud-loaded runs) labels results in the
        # model's native units without re-scanning the .prj.
        unit_system=np.array([_unit_system or ("SI" if model_is_si else "English")]),
        # Self-describing CLOCK, same idea as unit_system above
        # (v4.8.0).  ``model_time`` is the model's own wall clock and
        # carries no time zone of its own - HEC-RAS never records one.
        # Storing both offsets makes the run folder self-describing:
        #   model_utc_offset - what model_time means (UTC + this)
        #   site_lst_offset  - Local Standard Time at the model site,
        #                      for display only; never shifts data.
        # Without these, no downstream consumer (Results, Validation,
        # the alert agent, the live dashboard, a re-opened folder) can
        # convert or even honestly label a timestamp.
        model_utc_offset=np.array([int(_tz_off or 0)]),
        site_lst_offset=np.array([int(config.site_lst_offset_hours)]),
    )
    if vel is not None:
        _npz_payload["vel"] = vel
    if min_elev is not None:
        _npz_payload["min_elev"] = min_elev
    if cell_fp is not None and fp_xy is not None:
        _npz_payload["cell_fp"] = cell_fp
        _npz_payload["fp_xy"] = fp_xy
    np.savez_compressed(output_dir / "wse_extract.npz", **_npz_payload)

    # ── run_meta.json - model identity for re-opening (v2.7.2+) ─────
    # When the user later opens this folder via Tab 1 → "Open Previous
    # Results", the UI reads run_meta.json to display the originating
    # model + plan/geometry/unsteady file names and, if the model_dir
    # still exists on disk, can fully restore the project for editing.
    try:
        import json as _json
        _meta = {
            "schema_version": 2,
            "extracted_from": str(results_hdf),
            "extracted_at": datetime.utcnow().isoformat() + "Z",
            "source": "hecinbox",
            "project_name": project_name,
            "model_dir": str(model_dir),
            "plan_hdf_name": Path(results_hdf).name,
            "plan_suffix": plan_suffix,
            "geom_suffix": geom_suffix,
            "unsteady_suffix": unsteady_suffix,
            "flow_area_name": flow_area_name,
            "unit_system": _unit_system or ("SI" if model_is_si else "English"),
            # schema 2 (v4.8.0): the run's clock, so window_start /
            # window_end below are no longer ambiguous wall-clock
            # strings.  model_utc_offset_hours says what they mean;
            # site_lst_offset_hours is the display clock.
            "model_utc_offset_hours": int(_tz_off or 0),
            "site_lst_offset_hours": int(config.site_lst_offset_hours),
            "n_timesteps": int(wse.shape[0]),
            "n_cells": int(wse.shape[1]),
            "window_start": str(model_time[0]),
            "window_end": str(model_time[-1]),
        }
        (output_dir / "run_meta.json").write_text(
            _json.dumps(_meta, indent=2)
        )
    except Exception as _e:
        print(f"WARNING: could not write run_meta.json - {_e}")

    # ── User-friendly artifacts (browseable without opening Streamlit) ──
    # Drop a plain-text summary, peak-WSE & peak-depth maps as PNG,
    # and a CSV time series at the deepest cell, right next to the raw
    # wse_extract.npz so the run folder is self-describing.
    try:
        _progress(94, "Saving run summary + peak-map PNGs + CSV")
        _save_run_artifacts(
            output_dir=output_dir,
            wse=wse,
            coords=coords,
            model_time=model_time,
            min_elev=min_elev,
            vel=vel,
            cell_fp=cell_fp,
            fp_xy=fp_xy,
            bc_provenance=bc_provenance,
            model_is_si=model_is_si,
            model_dir=model_dir,
            proj_wkt=proj_wkt,
        )
    except Exception as _e:
        # Artifact generation must never fail the pipeline.
        print(f"WARNING: could not save per-run artifacts - {_e}")

    _progress(96, "Saving results HDF")
    shutil.copy2(results_hdf, output_dir / results_hdf.name)
    print(f"Results HDF copied to {output_dir / results_hdf.name}")

    keep = os.environ.get("KEEP_WORKSPACE", "0") == "1"
    if keep:
        print(f"Workspace preserved at {workspace}")
    else:
        shutil.rmtree(workspace, ignore_errors=True)
        print("Workspace cleaned up")

    # ── Optional: upload the finished run folder to cloud storage ──
    cloud_cfg = config.raw.get("cloud", {}) or {}
    dest_uri = (cloud_cfg.get("upload_results_to") or "").strip()
    if dest_uri:
        try:
            from cloud_storage import upload_dir
            _progress(97, "Uploading results to cloud storage")
            n_up = upload_dir(output_dir, dest_uri)
            print(f"Uploaded {n_up} result file(s) to {dest_uri}")
        except Exception as e:
            # A failed upload must not fail the whole run - the results
            # are still on local disk for the user to retrieve.
            print(f"WARNING: cloud upload failed - {e}")

    # ── Real-time decision-support agent - only when the run was
    #     launched with **Enable alert agent** turned on in the Run
    #     tab (scheduled-mode opt-in).  Single-shot runs never page
    #     anyone; a hand-fired test run shouldn't trigger a real-time
    #     alert chain by accident. ──
    agent_cfg = config.raw.get("agent", {}) or {}
    if agent_cfg.get("enabled"):
        try:
            from agent import evaluate_after_run
            _progress(98, "Evaluating decision-support rules")
            run_meta = {
                "start": str(config.raw.get("simulation", {}).get("start", "")),
                "end": str(config.raw.get("simulation", {}).get("end", "")),
                "output_dir": str(output_dir),
            }
            events = evaluate_after_run(output_dir, run_meta)
            if events:
                n_ok = sum(1 for e in events if e.get("email_ok"))
                print(
                    f"Agent: {len(events)} alert(s) fired this run "
                    f"({n_ok} email(s) delivered)."
                )
        except Exception as e:
            # Agent failures must never fail a flood-forecast run.
            print(f"WARNING: agent evaluation skipped - {e}")
    else:
        print("Agent: disabled for this run (no alerts will be sent).")

    _progress(100, "Simulation complete")
    print("Simulation complete.")


# ========================================
# ENTRY POINT 2: VALIDATE (post-processing)
# ========================================
def resolve_validation_cells(
    validation, coords, wse, proj_wkt: str = "", n_neighbors: int = 10,
    min_elev=None,
):
    if validation.get("cell_index") is not None:
        idx = int(validation["cell_index"])
        print(f"Validation: using explicit cell_index={idx}")
        dist = np.zeros(coords.shape[0])
        return np.array([idx]), dist

    if validation.get("x") is not None and validation.get("y") is not None:
        x_gage, y_gage = float(validation["x"]), float(validation["y"])
        print(f"Validation: using projected x={x_gage}, y={y_gage}")
    else:
        if proj_wkt:
            target_crs = CRS.from_wkt(proj_wkt)
            print(f"Validation: using model CRS from HDF - {target_crs.name}")
        else:
            target_crs = CRS.from_epsg(32616)
            print("Validation: no CRS in model data, falling back to EPSG:32616 (UTM 16N)")
        transformer = Transformer.from_crs(
            "EPSG:4326", target_crs, always_xy=True
        )
        x_gage, y_gage = transformer.transform(
            validation["lon"], validation["lat"]
        )
        print(f"Validation: lat/lon -> projected x={x_gage:.1f}, y={y_gage:.1f}")

    dist = np.sqrt(
        (coords[:, 0] - x_gage) ** 2 + (coords[:, 1] - y_gage) ** 2
    )

    # Prefer depth-based wet detection (WSE − terrain > 0) over variance,
    # which fails for near-steady rivers.
    if min_elev is not None:
        wet_mask = np.nanmax(wse - min_elev, axis=0) > 0.01
    else:
        wet_mask = np.nanstd(wse, axis=0) > 0.01
    if not wet_mask.any():
        raise RuntimeError(
            "No wet cells found in WSE output - check HEC-RAS run."
        )

    wet_indices = np.where(wet_mask)[0]
    wet_sorted = wet_indices[np.argsort(dist[wet_indices])]
    nearest_cells = wet_sorted[:n_neighbors]

    print(
        "Nearest wet cells:", nearest_cells,
        "distances (m):", np.round(dist[nearest_cells], 1),
    )
    return nearest_cells, dist


def validate():
    """Fetch observed gage data, compare with saved results, produce plots."""
    _progress(2, "Loading configuration")
    settings_path = os.environ.get("SETTINGS_PATH", "/app/settings.yml")
    config = load_config(settings_path)

    start = pd.to_datetime(config.raw["simulation"]["start"])
    end = pd.to_datetime(config.raw["simulation"]["end"])
    output_dir = Path(config.hecras["output_dir"])

    # ── Load saved model results ──
    _progress(10, "Loading model results")
    npz_path = output_dir / "wse_extract.npz"
    if not npz_path.exists():
        raise FileNotFoundError(
            f"No model results found at {npz_path}. "
            "Run the simulation first (Tab 4)."
        )

    data = np.load(npz_path, allow_pickle=True)
    wse = data["wse"]
    coords = data["coords"]
    model_time = pd.to_datetime(data["model_time"])
    proj_wkt = str(data["proj_wkt"][0]) if "proj_wkt" in data else ""

    # Unit system the model results were saved in (native, un-converted).
    # Used to (a) convert observed gage data into the same units before
    # computing metrics, and (b) label the validation plot.
    from units import is_si, unit_labels
    _unit_system = (
        str(data["unit_system"][0]) if "unit_system" in data.files else None
    )
    model_is_si = is_si(_unit_system)

    # ── Fetch validation gage data ──
    _progress(20, "Fetching validation gage data")
    usgs = USGSClient()
    noaa = NOAAClient()
    validation = config.validation

    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Shift observed data onto the model's clock (LST) too, so it lines
    # up with model_time when computing metrics.
    validation_df = _fetch_series(
        validation, start, end, usgs, noaa, "validation",
        config.time_offset_hours,
    )
    if validation_df is None or validation_df.empty:
        raise RuntimeError(
            "Could not fetch validation data - check station ID and date range."
        )

    # Convert observed gage data into the model's native units so the
    # model-vs-observed metrics and plot compare like-for-like.  Observed
    # stage arrives in feet (USGS 00065) or feet/metres (NOAA, per the
    # validation 'units' field); the same factor used for BC injection
    # maps it onto the model's unit system.
    _obs_factor = _unit_factor(
        "stage",
        str(validation.get("source", "")).lower(),
        validation.get("units", "english"),
        model_is_si,
    )
    if _obs_factor != 1.0:
        validation_df = validation_df.copy()
        validation_df["value"] = (
            pd.to_numeric(validation_df["value"], errors="coerce")
            * _obs_factor
        )
        print(
            f"Observed validation data scaled ×{_obs_factor:g} into model "
            f"units ({'metres' if model_is_si else 'feet'})."
        )
    usgs.save(validation_df, raw_dir / "validation.csv")

    # ── Resolve validation cells ──
    _progress(50, "Resolving validation cells")
    _min_elev = data["min_elev"] if "min_elev" in data.files else None
    nearest_cells, dist = resolve_validation_cells(
        validation, coords, wse, proj_wkt=proj_wkt, min_elev=_min_elev,
    )

    dist_subset = dist[nearest_cells]
    weights = 1 / (dist_subset + 1e-6)
    weights = weights / np.sum(weights)
    model_vals = np.sum(wse[:, nearest_cells] * weights, axis=1)
    model_ts = pd.Series(model_vals, index=model_time, name="model")

    # ── Align observed data + metrics ──
    _progress(70, "Aligning observed data and computing metrics")
    obs_ts = (
        pd.Series(
            validation_df["value"].values,
            index=pd.to_datetime(validation_df["datetime"]),
            name="observed",
        )
        .sort_index()
    )
    obs_on_model = (
        obs_ts.reindex(obs_ts.index.union(model_ts.index))
        .interpolate(method="time")
        .reindex(model_ts.index)
    )

    combined = pd.concat([model_ts, obs_on_model], axis=1).dropna()
    if combined.empty:
        raise RuntimeError(
            "No overlapping timestamps between model and observed.\n"
            f"  Model:    {model_ts.index.min()} -> {model_ts.index.max()}\n"
            f"  Observed: {obs_ts.index.min()} -> {obs_ts.index.max()}"
        )

    # Compute metrics on the RAW paired series so the reported bias /
    # RMSE / NSE faithfully reflect the model's true error (including
    # any vertical datum offset).  The prior code subtracted ``iloc[0]``
    # from BOTH series, which silently zeroed out the bias and was
    # extremely sensitive to noise at the first overlapping timestamp.
    _progress(85, "Computing validation metrics")
    metrics = Metrics.compute(
        combined["model"].values, combined["observed"].values
    )
    print(metrics.as_text())

    # v3.1.0 - moving-block bootstrap confidence intervals (TEEHR-style)
    # so reported skill carries an honest uncertainty band rather than a
    # bare point estimate.
    try:
        from evaluation import bootstrap_cis
        metric_cis = bootstrap_cis(
            combined["model"].values, combined["observed"].values
        )
        print("95% bootstrap CIs:")
        for _k, (_lo, _hi) in metric_cis.items():
            print(f"  {_k:10s} [{_lo:.4f}, {_hi:.4f}]")
    except Exception as _e:  # pragma: no cover - defensive
        print(f"Bootstrap CIs skipped: {_e}")
        metric_cis = {}

    # Apply a datum shift to the model series for PLOTTING ONLY, so the
    # two curves overlay on a common vertical reference.  The shift is
    # the constant ``mean(model) − mean(observed)`` over all paired
    # samples - robust to noise at any individual timestep, model-
    # agnostic (no calibration assumption), and it does NOT influence
    # the metrics computed above.  The print line documents the offset
    # so the user can still see the raw bias the model carries.
    _datum_shift = float(
        combined["model"].mean() - combined["observed"].mean()
    )
    combined["model"] = combined["model"] - _datum_shift
    print(
        f"Plot datum shift applied to model: {_datum_shift:+.3f} "
        f"(model − observed mean over paired samples; metrics above "
        f"are computed on the un-shifted series)."
    )

    # ── Save outputs ──
    _progress(95, "Saving plots and metrics")
    from timebase import TimeBase
    _vtb = TimeBase(config.time_offset_hours, config.site_lst_offset_hours)
    writer = ResultsWriter(output_dir, start, end, timebase=_vtb)
    _stage_label = f"Stage ({unit_labels(model_is_si)['length']})"
    writer.save_all(combined, metrics, show=False, ylabel=_stage_label)
    writer.save_metrics_json(metrics, cis=metric_cis)

    _progress(100, "Validation complete")
    print("Validation complete.")


# ========================================
# CLI DISPATCH
# ========================================
def main():
    """Legacy single-shot: run + validate in sequence."""
    run()
    validate()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "run":
        run()
    elif cmd == "validate":
        validate()
    else:
        main()
