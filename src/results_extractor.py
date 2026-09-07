"""Standalone HEC-RAS results extractor.

Reads a computed plan HDF (``*.pXX.hdf``) from any HEC-RAS 2D unsteady
model - whether produced by HECinBOX or by a manual HEC-RAS run - and
writes ``wse_extract.npz`` (plus a basic ``summary.txt`` and
``run_meta.json``) into a target folder.  This lets the Streamlit UI
open *any* HEC-RAS result folder, not only HECinBOX-generated runs.

The extraction logic mirrors the inline block in ``main.py`` STEP 5,
factored out so the UI can call it on demand for raw HEC-RAS folders.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import h5py
import numpy as np
import pandas as pd

from model_scanner import scan_model


# ── HDF key templates ──────────────────────────────────────────────────
_WSE_KEY = (
    "Results/Unsteady/Output/Output Blocks/Base Output/"
    "Unsteady Time Series/2D Flow Areas/{area}/Water Surface"
)
_FACE_VEL_KEY = (
    "Results/Unsteady/Output/Output Blocks/Base Output/"
    "Unsteady Time Series/2D Flow Areas/{area}/Face Velocity"
)
_COORDS_KEY = "Geometry/2D Flow Areas/{area}/Cells Center Coordinate"
_MIN_ELEV_KEY = "Geometry/2D Flow Areas/{area}/Cells Minimum Elevation"
_TIME_KEY = (
    "Results/Unsteady/Output/Output Blocks/Base Output/"
    "Unsteady Time Series/Time Date Stamp"
)
_GEOM_ROOT = "Geometry/2D Flow Areas/{area}"


def _find_active_plan_hdf(model_dir: Path, scan: dict) -> Path | None:
    """Pick the plan HDF to extract.

    Prefer the active plan named in the .prj.  Fall back to the newest
    ``*.pXX.hdf`` that contains an actual results dataset.
    """
    base = Path(scan.get("project_name", "")).stem
    plan_suffix = scan.get("plan_suffix")
    if base and plan_suffix:
        candidate = model_dir / f"{base}.{plan_suffix}.hdf"
        if candidate.exists() and _has_results(candidate):
            return candidate

    # Fall back: any .pXX.hdf with results, newest first.
    candidates = sorted(
        model_dir.glob("*.p[0-9][0-9].hdf"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for c in candidates:
        if _has_results(c):
            return c
    return None


def _has_results(plan_hdf: Path) -> bool:
    """Return True if the plan HDF has an Unsteady Output Block."""
    try:
        with h5py.File(plan_hdf, "r") as f:
            return (
                "Results/Unsteady/Output/Output Blocks/Base Output/"
                "Unsteady Time Series" in f
            )
    except Exception:
        return False


def _detect_flow_area(plan_hdf: Path, preferred: str | None = None) -> str:
    """Return a 2D flow area name with WSE results in the plan HDF."""
    with h5py.File(plan_hdf, "r") as f:
        ts_root = (
            "Results/Unsteady/Output/Output Blocks/Base Output/"
            "Unsteady Time Series/2D Flow Areas"
        )
        if ts_root not in f:
            raise ValueError(f"No 2D flow area results in {plan_hdf.name}")
        areas = list(f[ts_root].keys())
        if preferred and preferred in areas:
            return preferred
        if not areas:
            raise ValueError(f"No 2D flow areas in {plan_hdf.name}")
        return areas[0]


def extract_plan_hdf_to_folder(
    plan_hdf: Path,
    output_dir: Path,
    flow_area_name: str | None = None,
    model_dir: Path | None = None,
    project_name: str | None = None,
    model_utc_offset_hours: int | None = None,
    site_lst_offset_hours: int | None = None,
) -> dict:
    """Extract a HEC-RAS plan HDF into HECinBOX's standard folder format.

    Writes:
      - ``wse_extract.npz`` (raw arrays Tab 5 reads)
      - ``summary.txt`` (plain-text summary)
      - ``run_meta.json`` (model identity for re-opening)

    Returns a metadata dict.
    """
    plan_hdf = Path(plan_hdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    area = flow_area_name or _detect_flow_area(plan_hdf)
    with h5py.File(plan_hdf, "r") as f:
        wse = f[_WSE_KEY.format(area=area)][:]
        coords = f[_COORDS_KEY.format(area=area)][:]
        time_stamps = f[_TIME_KEY][:]
        proj_wkt = ""
        if "Projection" in f.attrs:
            raw = f.attrs["Projection"]
            proj_wkt = raw.decode() if isinstance(raw, bytes) else str(raw)

        # Velocity (optional)
        vel = None
        fv_key = _FACE_VEL_KEY.format(area=area)
        geom_root = _GEOM_ROOT.format(area=area)
        try:
            cfi_key = f"{geom_root}/Cells Face and Orientation Info"
            cfv_key = f"{geom_root}/Cells Face and Orientation Values"
            if fv_key in f and cfi_key in f and cfv_key in f:
                face_vel = np.abs(f[fv_key][:])
                cf_info = f[cfi_key][:]
                cf_vals = f[cfv_key][:]
                n_cells = cf_info.shape[0]
                vel = np.zeros(
                    (face_vel.shape[0], n_cells), dtype=np.float32
                )
                for c in range(n_cells):
                    s, n = int(cf_info[c, 0]), int(cf_info[c, 1])
                    if n > 0:
                        fids = cf_vals[s:s + n, 0]
                        vel[:, c] = face_vel[:, fids].mean(axis=1)
        except Exception:
            vel = None

        # Min terrain elevation (optional)
        min_elev = None
        try:
            me_key = _MIN_ELEV_KEY.format(area=area)
            if me_key in f:
                min_elev = f[me_key][:].astype(np.float32)
        except Exception:
            min_elev = None

        # Cell polygons (optional, for filled inundation maps)
        cell_fp = None
        fp_xy = None
        try:
            fp_idx_key = f"{geom_root}/Cells FacePoint Indexes"
            fp_xy_key = f"{geom_root}/FacePoints Coordinate"
            if fp_idx_key in f and fp_xy_key in f:
                cell_fp = f[fp_idx_key][:].astype(np.int32)
                fp_xy = f[fp_xy_key][:].astype(np.float64)
        except Exception:
            cell_fp = None
            fp_xy = None

    # Unit conversion: HEC-RAS outputs in the model CRS units.  Tab 5
    # assumes feet.  Convert from metres if the projection says metric.
    model_units_ft = (
        "foot" in proj_wkt.lower() or "feet" in proj_wkt.lower()
    )
    if not model_units_ft:
        wse = wse * 3.28084
        if vel is not None:
            vel = vel * 3.28084
        if min_elev is not None:
            min_elev = min_elev * 3.28084

    model_time = pd.to_datetime(
        [
            t.decode("utf-8") if isinstance(t, bytes) else t
            for t in time_stamps
        ],
        format="%d%b%Y %H:%M:%S",
    )

    # ── Save wse_extract.npz ────────────────────────────────────────
    payload = dict(
        wse=wse.astype(np.float32),
        coords=coords,
        model_time=model_time.values.astype("int64"),
        proj_wkt=np.array([proj_wkt]),
    )
    # Self-describing clock (v4.8.0), same idea as unit_system.  For a
    # raw HEC-RAS folder the model's clock is genuinely UNKNOWN - the
    # user opened someone else's results and never declared one - so
    # these keys are written only when a caller supplies them.  A
    # consumer that finds them absent must say "unknown", never assume.
    if model_utc_offset_hours is not None:
        payload["model_utc_offset"] = np.array([int(model_utc_offset_hours)])
    if site_lst_offset_hours is not None:
        payload["site_lst_offset"] = np.array([int(site_lst_offset_hours)])
    if vel is not None:
        payload["vel"] = vel
    if min_elev is not None:
        payload["min_elev"] = min_elev
    if cell_fp is not None and fp_xy is not None:
        payload["cell_fp"] = cell_fp
        payload["fp_xy"] = fp_xy
    np.savez_compressed(output_dir / "wse_extract.npz", **payload)

    # ── Save run_meta.json (model identity) ─────────────────────────
    meta = {
        "schema_version": 2,
        "extracted_from": str(plan_hdf),
        "extracted_at": datetime.utcnow().isoformat() + "Z",
        "source": "external_hecras",
        "project_name": project_name,
        "model_dir": str(model_dir) if model_dir else None,
        "plan_hdf_name": plan_hdf.name,
        "flow_area_name": area,
        # None = the clock these window strings are on is unknown.
        "model_utc_offset_hours": (
            None if model_utc_offset_hours is None
            else int(model_utc_offset_hours)
        ),
        "site_lst_offset_hours": (
            None if site_lst_offset_hours is None
            else int(site_lst_offset_hours)
        ),
        "n_timesteps": int(wse.shape[0]),
        "n_cells": int(wse.shape[1]),
        "window_start": str(model_time[0]),
        "window_end": str(model_time[-1]),
    }
    (output_dir / "run_meta.json").write_text(
        json.dumps(meta, indent=2)
    )

    # ── Save a minimal summary.txt ──────────────────────────────────
    peak_wse = np.nanmax(wse, axis=0)
    lines = [
        "HECinBOX external-results summary",
        "=" * 40,
        f"Extracted from : {plan_hdf.name}",
        f"Source model   : {project_name or '(unknown)'}",
        f"Flow area      : {area}",
        f"Timesteps      : {wse.shape[0]:,}",
        f"Mesh cells     : {wse.shape[1]:,}",
        f"Window start   : {model_time[0]}",
        f"Window end     : {model_time[-1]}",
        "",
        f"Min WSE  (ft)  : {float(np.nanmin(peak_wse)):>10.3f}",
        f"Max WSE  (ft)  : {float(np.nanmax(peak_wse)):>10.3f}",
        f"Mean WSE (ft)  : {float(np.nanmean(peak_wse)):>10.3f}",
    ]
    (output_dir / "summary.txt").write_text("\n".join(lines) + "\n")

    return meta


def extract_model_dir_to_folder(
    model_dir: Path, output_dir: Path
) -> dict:
    """Convenience: scan a HEC-RAS model dir + extract its active plan."""
    model_dir = Path(model_dir)
    scan = scan_model(model_dir)
    if "project_name" not in scan:
        raise ValueError(
            f"No HEC-RAS project found in {model_dir} "
            f"(no .prj with 'Proj Title=')"
        )
    plan_hdf = _find_active_plan_hdf(model_dir, scan)
    if plan_hdf is None:
        raise ValueError(
            f"No computed plan HDF (.pXX.hdf) found in {model_dir}. "
            "Run HEC-RAS first to produce results."
        )
    return extract_plan_hdf_to_folder(
        plan_hdf=plan_hdf,
        output_dir=output_dir,
        flow_area_name=scan.get("flow_area_name"),
        model_dir=model_dir,
        project_name=scan.get("project_name"),
    )


def detect_folder_kind(folder: Path) -> dict:
    """Classify what's inside a folder.

    Returns a dict with keys:
      - kind: "hecinbox" | "hecras_results" | "hecras_model" | "empty"
      - npz: Path to wse_extract.npz, if present
      - plan_hdf: Path to a results-bearing plan HDF, if present
      - model_dir: Path to the HEC-RAS model dir, if discoverable
      - meta: dict from run_meta.json, if present
    """
    folder = Path(folder)
    out: dict = {
        "kind": "empty",
        "npz": None,
        "plan_hdf": None,
        "model_dir": None,
        "meta": None,
    }

    # 1) HECinBOX-style output folder
    npz = folder / "wse_extract.npz"
    if npz.exists():
        out["kind"] = "hecinbox"
        out["npz"] = npz
        meta_p = folder / "run_meta.json"
        if meta_p.exists():
            try:
                out["meta"] = json.loads(meta_p.read_text())
                md = (out["meta"] or {}).get("model_dir")
                if md and Path(md).exists():
                    out["model_dir"] = Path(md)
            except Exception:
                pass
        return out

    # 2) Raw HEC-RAS model folder (has a HEC-RAS .prj)
    try:
        scan = scan_model(folder)
    except Exception:
        scan = {}
    if "project_name" in scan:
        plan_hdf = _find_active_plan_hdf(folder, scan)
        if plan_hdf is not None:
            out["kind"] = "hecras_results"
            out["plan_hdf"] = plan_hdf
            out["model_dir"] = folder
        else:
            out["kind"] = "hecras_model"
            out["model_dir"] = folder
        out["meta"] = {
            "project_name": scan.get("project_name"),
            "plan_suffix": scan.get("plan_suffix"),
            "geom_suffix": scan.get("geom_suffix"),
            "unsteady_suffix": scan.get("unsteady_suffix"),
            "flow_area_name": scan.get("flow_area_name"),
        }
    return out
