"""Write fetched USGS / NOAA data as fresh DSS records for HEC-RAS.

Why this module exists
----------------------
The HEC-RAS Linux unsteady engine reads boundary-condition time series
from a **DSS file** that the ``.u01`` text file references via two
companion lines inside each ``Boundary Location=`` block::

    DSS File=BoundaryTimeSeries/BraysBayou.dss
    DSS Path=/BRAYS/UPSTREAM/FLOW//15Minute/USGS/

Prior HECinBOX releases patched only the **plan HDF**'s
``Event Conditions/Unsteady/Boundary Conditions/...`` datasets.  Those
HDF rows turn out to be the engine's *cached input snapshot*, not the
authoritative source - the engine re-reads from DSS at runtime and
silently overwrites our HDF edit.  Result: every simulation, regardless
of the requested year, produced byte-identical hydraulics because the
engine kept consuming the model-bundled calibration DSS records.

This module fixes that by:

1. Wiping any pre-existing ``.dss`` (and side-car ``.dsc`` / ``.dsk``)
   inside the workspace model directory so the engine cannot fall back
   to stale calibration data.
2. Writing a single fresh ``boundary.dss`` at the model root with one
   regular time-series record per fetched BC, resampled to that BC's
   ``Interval=`` setting from the ``.u01``.
3. Returning a mapping ``{bc_short_name: dss_pathname}`` so the caller
   can rewrite the ``.u01``'s ``DSS Path=`` / ``DSS File=`` lines.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

# Imported lazily inside ``write_boundary_dss`` so that simply *parsing*
# this file (e.g. for unit-test collection) doesn't crash if the native
# ``hecdss`` wheel isn't installed in the active environment.


# ── Interval normalization ───────────────────────────────────────────
# Map RAS ``Interval=`` text → (minutes, DSS E-part as RAS expects it).
_INTERVAL_MAP: dict[str, tuple[int, str]] = {
    "1MIN":   (1,    "1Minute"),
    "2MIN":   (2,    "2Minute"),
    "5MIN":   (5,    "5Minute"),
    "6MIN":   (6,    "6Minute"),
    "10MIN":  (10,   "10Minute"),
    "15MIN":  (15,   "15Minute"),
    "20MIN":  (20,   "20Minute"),
    "30MIN":  (30,   "30Minute"),
    "1HOUR":  (60,   "1Hour"),
    "2HOUR":  (120,  "2Hour"),
    "3HOUR":  (180,  "3Hour"),
    "6HOUR":  (360,  "6Hour"),
    "12HOUR": (720,  "12Hour"),
    "1DAY":   (1440, "1Day"),
}


def parse_interval(raw: str) -> tuple[int, str]:
    """Normalize a RAS ``Interval=`` value to ``(minutes, E-part)``.

    Falls back to 15-minute on unknown / empty input so we always
    produce a writable regular time series.
    """
    key = (raw or "").strip().upper().replace(" ", "")
    return _INTERVAL_MAP.get(key, (15, "15Minute"))


def bc_short_name(hdf_key: str) -> str:
    """Extract the BC-line short name from a full plan-HDF key.

    Examples
    --------
    ``Event Conditions/Unsteady/Boundary Conditions/Flow Hydrographs/2D: AreaName BCLine: DS-Stage``
        → ``"DS-Stage"``
    ``Event Conditions/Unsteady/Boundary Conditions/Flow Hydrographs/SomeBC``
        → ``"SomeBC"``
    """
    tail = (hdf_key or "").rsplit("/", 1)[-1].strip()
    if " BCLine: " in tail:
        return tail.split(" BCLine: ", 1)[1].strip()
    return tail


def _safe_path_part(name: str) -> str:
    """Strip a BC name to alphanumeric uppercase for use as a DSS A/B/C part."""
    out = "".join(c if c.isalnum() else "" for c in (name or "")).upper()[:32]
    return out or "BC"


def _type_part(bc_type: str) -> str:
    return {"flow": "FLOW", "stage": "STAGE"}.get(
        (bc_type or "").lower(), "VALUE",
    )


def _units_for(bc_type: str) -> str:
    return {"flow": "CFS", "stage": "FT"}.get((bc_type or "").lower(), "")


def build_pathname(bc_name: str, bc_type: str, e_part: str) -> str:
    """Canonical HECinBOX DSS pathname (D-part deliberately blank).

    HEC-RAS resolves a blank D-part by matching the record with the
    simulation window, which removes any need to keep the ``.u01``
    DSS-path D-part in sync with the run dates.
    """
    return (
        f"/HECINBOX/{_safe_path_part(bc_name)}/{_type_part(bc_type)}/"
        f"/{e_part}/HECINBOX/"
    )


def _resample(
    df: pd.DataFrame,
    factor: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
    interval_minutes: int,
) -> tuple[list[datetime], list[float]]:
    """Clean fetched data and resample to a regular grid.

    Strategy:
      * coerce ``value`` to numeric, drop NaNs, sort by datetime,
      * apply the unit conversion ``factor`` (e.g. m → ft),
      * build a fixed ``interval_minutes`` index covering ``[start, end]``,
      * time-interpolate onto the union index, snap back to the regular
        grid, then ``ffill`` / ``bfill`` the edges so the engine never
        sees a NaN at the BC.
    """
    s = pd.Series(
        pd.to_numeric(df["value"], errors="coerce").values * float(factor),
        index=pd.to_datetime(df["datetime"]).dt.tz_localize(None),
    ).dropna().sort_index()
    if s.empty:
        return [], []

    freq = f"{int(interval_minutes)}min"
    idx = pd.date_range(
        pd.Timestamp(start).floor(freq),
        pd.Timestamp(end).ceil(freq),
        freq=freq,
    )
    out = s.reindex(s.index.union(idx)).interpolate("time").reindex(idx)
    out = out.ffill().bfill()
    if out.isna().any():
        # Single-value or all-NaN series after resample - bail rather
        # than write garbage that would crash the engine.
        return [], []

    return (
        [t.to_pydatetime() for t in out.index],
        [float(v) for v in out.values],
    )


def write_boundary_dss(
    workspace_model_dir: Path,
    injections: list[dict],
    sim_start: pd.Timestamp,
    sim_end: pd.Timestamp,
    bc_intervals: dict[str, str] | None = None,
    u01_path: Path | None = None,
) -> dict[str, str]:
    """Write a fresh ``boundary.dss`` and return BC-name → DSS path.

    Parameters
    ----------
    workspace_model_dir
        Per-run writable copy of the HEC-RAS model.  Pre-existing
        ``.dss`` files anywhere under here are deleted first.
    injections
        The list ``main.run()`` already builds - each entry has
        ``hdf_key`` (full plan-HDF dataset path), ``df`` (the fetched
        time series), ``unit_factor`` (multiplier to engine units),
        ``label`` (human label), and optionally ``bc_type``
        (``"flow"`` / ``"stage"``).
    sim_start, sim_end
        Simulation window - used as the regular-grid bounds.
    bc_intervals
        Optional ``{bc_name_lower: "15MIN"}`` mapping returned by
        :func:`collect_interval_map`.  If absent for a BC we default
        to 15-minute, which is fine for any RAS BC table.
    u01_path
        Optional path to the model's unsteady text file.  When given,
        any DSS file it still references via ``DSS File=`` lines is
        **preserved** during the stale-DSS wipe.  This protects
        "Leave unchanged" BCs (and failed-fetch fallbacks), whose
        blocks keep pointing at the model's original DSS - deleting
        it would leave the engine with no boundary data at all.

    Returns
    -------
    dict[str, str]
        ``{bc_short_name_lower: dss_pathname}`` for every record we
        wrote successfully.  Empty if no BC had usable fetched data.
    """
    workspace_model_dir = Path(workspace_model_dir)
    bc_intervals = bc_intervals or {}

    # Nothing to write → touch nothing.  The engine must keep reading
    # whatever the model files already reference (e.g. an all-"Leave
    # unchanged" run replaying its native calibration DSS).
    if not injections:
        return {}

    # 1. Wipe pre-existing DSS state so the engine can't read stale
    #    calibration data - EXCEPT files the ``.u01`` still references.
    #    Those belong to BC blocks we are not re-pointing ("Leave
    #    unchanged" or failed fetches) and must survive.  Side-car
    #    catalogue files (``.dsc`` / ``.dsk``) of a preserved ``.dss``
    #    are preserved with it.
    preserve: set[Path] = (
        collect_referenced_dss_files(u01_path) if u01_path else set()
    )
    for pat in ("*.dss", "*.dsc", "*.dsk"):
        for p in workspace_model_dir.rglob(pat):
            try:
                resolved = p.resolve()
            except OSError:
                resolved = p
            if (
                resolved in preserve
                or resolved.with_suffix(".dss") in preserve
            ):
                rel = p.relative_to(workspace_model_dir)
                print(f"DSS: preserved {rel} (still referenced by .u01)")
                continue
            try:
                p.unlink()
                rel = p.relative_to(workspace_model_dir)
                print(f"DSS: removed stale {rel}")
            except OSError as exc:
                print(f"DSS: could not remove {p}: {exc}")

    # 2. Open a fresh DSS file at the model root.
    out_dss = workspace_model_dir / "boundary.dss"
    name_to_path: dict[str, str] = {}

    try:
        from hecdss import HecDss, RegularTimeSeries  # noqa: WPS433
    except ImportError as exc:
        print(
            "DSS: 'hecdss' import failed - BC data CANNOT be propagated "
            f"to the engine. Install hecdss in the container. ({exc})"
        )
        return {}

    dss = HecDss(str(out_dss))
    try:
        for inj in injections:
            name = bc_short_name(inj.get("hdf_key", ""))
            df = inj.get("df")
            label = inj.get("label", name)
            if df is None or getattr(df, "empty", True):
                print(f"DSS: skip {label} (no fetched data)")
                continue

            interval_raw = bc_intervals.get(name.lower(), "15MIN")
            minutes, e_part = parse_interval(interval_raw)

            times, values = _resample(
                df,
                inj.get("unit_factor", 1.0),
                sim_start,
                sim_end,
                minutes,
            )
            if not values:
                print(f"DSS: skip {label} (resample produced no points)")
                continue

            # Infer BC type from the HDF key if not stated explicitly.
            bc_type = (inj.get("bc_type") or "").lower()
            if not bc_type:
                bc_type = (
                    "stage" if "Stage" in inj.get("hdf_key", "")
                    else "flow"
                )

            ts = RegularTimeSeries()
            ts.id = build_pathname(name, bc_type, e_part)
            ts.times = times
            ts.values = values
            ts.units = _units_for(bc_type)
            ts.data_type = "INST-VAL"

            status = dss.put(ts)
            if status == 0:
                name_to_path[name.lower()] = ts.id
                print(
                    f"DSS write OK: {label} -> {ts.id}  "
                    f"({len(values)} pts @ {minutes}min, "
                    f"{times[0]} → {times[-1]}, "
                    f"min={min(values):.3f} max={max(values):.3f})"
                )
            else:
                print(f"DSS write FAILED for {label}: status={status}")
    finally:
        dss.close()

    print(
        f"DSS file written: {out_dss.name} "
        f"({len(name_to_path)} record(s))"
    )
    return name_to_path


# ── .u01 text-file patching ──────────────────────────────────────────
def collect_referenced_dss_files(u01_path: Path) -> set[Path]:
    """Resolved paths of every DSS file the ``.u01`` references.

    Any ``DSS File=`` line anywhere in the unsteady file counts.  The
    value is resolved relative to the ``.u01``'s own directory, with
    Windows-style separators normalised (models ship lines like
    ``DSS File=.\\BoundaryTimeSeries\\BraysBayou.dss``).
    """
    u01_path = Path(u01_path)
    refs: set[Path] = set()
    if not u01_path.exists():
        return refs
    with open(u01_path, "r", errors="replace") as fh:
        for line in fh:
            if not line.startswith("DSS File="):
                continue
            raw = line.split("=", 1)[1].strip().replace("\\", "/")
            if not raw or raw.lower() in ("dss", "null"):
                continue
            try:
                refs.add((u01_path.parent / raw).resolve())
            except OSError:
                continue
    return refs


def _extract_bc_name_from_boundary_line(line: str) -> str:
    """Pull the BC short name out of a ``Boundary Location=`` row.

    HEC-RAS layout (commas matter - empty fields are placeholders)::

        Boundary Location=  ,  ,  ,  ,  ,  , <2D Area>, <BC Line>, ...

    For 2D BCs the BC-line name is the 8th comma-separated field
    (index 7).  We walk that region first, then fall back to scanning
    the whole row for the first alpha-containing token so 1D BCs (which
    encode the name as river/reach/RS) still produce *something*
    matchable.
    """
    if "=" not in line:
        return ""
    fields = line.split("=", 1)[1].rstrip("\n").split(",")
    for f in fields[7:] + fields[:7]:
        s = f.strip()
        if s and any(c.isalpha() for c in s):
            return s
    return ""


def collect_interval_map(u01_path: Path) -> dict[str, str]:
    """Scan ``.u01`` for the ``Interval=`` line inside each BC block.

    Returns ``{bc_name_lower: interval_str}`` so :func:`write_boundary_dss`
    can resample each fetched series to the BC's expected cadence
    before writing.
    """
    out: dict[str, str] = {}
    u01_path = Path(u01_path)
    if not u01_path.exists():
        return out

    with open(u01_path, "r", errors="replace") as fh:
        lines = fh.readlines()

    current = ""
    for line in lines:
        if line.startswith("Boundary Location="):
            current = _extract_bc_name_from_boundary_line(line)
        elif current and line.startswith("Interval="):
            out[current.lower()] = line.split("=", 1)[1].strip()
            current = ""  # capture only the first Interval per block

    return out


def update_unsteady_dss_paths(
    u01_path: Path,
    bc_name_to_dss_path: dict[str, str],
    dss_filename: str = "boundary.dss",
) -> int:
    """Re-point ``DSS Path=`` / ``DSS File=`` inside each BC block.

    For every ``Boundary Location=`` block whose BC name appears in
    ``bc_name_to_dss_path`` (case-insensitive on the short name):

    * the next ``DSS Path=`` line is rewritten to our fresh pathname;
    * the next ``DSS File=`` line is rewritten to ``dss_filename``
      (relative to the ``.u01``'s directory, which is the model root).

    BC blocks we have no data for are left untouched so the engine
    keeps whatever they originally referenced.

    Returns the number of BC blocks that had at least one line patched.
    """
    u01_path = Path(u01_path)
    if not u01_path.exists() or not bc_name_to_dss_path:
        return 0

    with open(u01_path, "r", errors="replace") as fh:
        lines = fh.readlines()

    out: list[str] = []
    current_bc_name = ""
    have_match = False
    blocks_patched = 0
    matched_in_current = False

    for line in lines:
        if line.startswith("Boundary Location="):
            current_bc_name = _extract_bc_name_from_boundary_line(line)
            have_match = (
                current_bc_name.lower() in bc_name_to_dss_path
            )
            matched_in_current = False
            out.append(line)
            continue

        if have_match and line.startswith("DSS Path="):
            new_path = bc_name_to_dss_path[current_bc_name.lower()]
            out.append(f"DSS Path={new_path}\n")
            if not matched_in_current:
                blocks_patched += 1
                matched_in_current = True
            continue

        if have_match and line.startswith("DSS File="):
            out.append(f"DSS File={dss_filename}\n")
            if not matched_in_current:
                blocks_patched += 1
                matched_in_current = True
            continue

        out.append(line)

    with open(u01_path, "w") as fh:
        fh.writelines(out)

    print(
        f"u01 patched: {blocks_patched} BC block(s) re-pointed to "
        f"{dss_filename}"
    )
    return blocks_patched
