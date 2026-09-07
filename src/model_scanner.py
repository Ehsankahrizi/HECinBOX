"""Auto-detect HEC-RAS model structure from project files.

Scans a model directory and returns a dict of discovered settings so the
user doesn't have to fill them in manually.  Works with any 2D unsteady
HEC-RAS model - the project file, active plan, geometry/unsteady
references, and every boundary condition are discovered from the files
themselves.
"""
from __future__ import annotations

from pathlib import Path

import h5py


_GEOM_HDF_META_KEYS = frozenset({
    "Attributes", "Cell Info", "Cell Points",
    "Polygon Info", "Polygon Parts", "Polygon Points",
})

# Boundary-condition group → semantic type.  Only Flow/Stage hydrographs
# carry an external time series we can replace with USGS/NOAA data.
# Everything else (normal depth, rating curve, precipitation, …) is a
# fixed/computed boundary the pipeline must leave untouched.
_BC_GROUP_TYPES = {
    "Flow Hydrographs": "flow",
    "Stage Hydrographs": "stage",
}

_BC_ROOT = "Event Conditions/Unsteady/Boundary Conditions"


# ── Per-BC detail block parsed from the .u01 text file ────────────────
# For each `Boundary Location=` line in the unsteady text file, capture
# the BC type, interval, and a one-line summary of the bundled data so
# the UI (Tab 1) can preview what each boundary contains before the
# user configures Tab 3.

# Keywords inside a Boundary Location block that identify the BC type.
# Order matters - first match wins.  Each value is (type_label, kind):
#   kind: "series" (Flow / Stage with inline values + Interval)
#         "structure" (gate / lateral / internal - no time series to
#                      preview at the BC level)
#         "computed" (Normal Depth - slope-based, no time series)
#         "rating"  (Rating Curve)
_BC_TYPE_KEYWORDS: list[tuple[str, tuple[str, str]]] = [
    ("Flow Hydrograph=",   ("Flow Hydrograph",   "series")),
    ("Stage Hydrograph=",  ("Stage Hydrograph",  "series")),
    ("Friction Slope=",    ("Normal Depth",      "computed")),
    ("Rating Curve=",      ("Rating Curve",      "rating")),
    ("Gate Name=",         ("Gate",              "structure")),
    ("Lateral Inflow=",    ("Lateral Inflow",    "series")),
]


def _summarise_series_values(
    block_lines: list[str], n_values: int
) -> tuple[float | None, float | None, float | None, bool]:
    """Return (min, max, mean, is_constant) of the inline value block.

    HEC-RAS writes value rows after the `Flow Hydrograph=N` /
    `Stage Hydrograph=N` declaration, 10 per line, in fixed-width
    columns.  We collect up to ``n_values`` numbers from the lines
    immediately after the declaration line.
    """
    nums: list[float] = []
    for line in block_lines:
        # Stop as soon as we hit the next directive (no leading space).
        if line and not line[0].isspace():
            break
        s = line.rstrip()
        # Fixed-width 8-char columns; tolerate variable spacing too.
        i = 0
        while i + 8 <= len(s):
            tok = s[i:i + 8].strip()
            i += 8
            if not tok:
                continue
            try:
                nums.append(float(tok))
            except ValueError:
                # Fall back to whitespace-split on this line.
                nums = []
                for chunk in s.split():
                    try:
                        nums.append(float(chunk))
                    except ValueError:
                        pass
                break
        if len(nums) >= n_values:
            break

    if not nums:
        return None, None, None, False
    nums = nums[:n_values]
    lo, hi = min(nums), max(nums)
    mean = sum(nums) / len(nums)
    is_constant = (hi - lo) < 1e-6
    return lo, hi, mean, is_constant


def parse_u01_precipitation(unsteady_path: Path) -> dict:
    """Detect the model's own Meteorological precipitation settings.

    Parsed from the ``.uXX`` text file's Met block, e.g.::

        Precipitation Mode=Enable
        Met BC=Precipitation|Mode=Constant
        Met BC=Precipitation|Constant Value=30
        Met BC=Precipitation|Constant Units=mm/hr

    Returns ``{"enabled", "mode", "constant_value", "constant_units"}``
    (mode is ``Constant`` / ``Point`` / ``Gridded`` or None when the
    model never had a Met precipitation block).
    """
    out: dict = {
        "enabled": False, "mode": None,
        "constant_value": None, "constant_units": None,
    }
    try:
        with open(unsteady_path, "r", errors="ignore") as f:
            for ln in f:
                if ln.startswith("Precipitation Mode="):
                    out["enabled"] = (
                        ln.split("=", 1)[1].strip().lower() == "enable"
                    )
                elif ln.startswith("Met BC=Precipitation|Mode="):
                    out["mode"] = ln.split("Mode=", 1)[1].strip() or None
                elif ln.startswith("Met BC=Precipitation|Constant Value="):
                    try:
                        out["constant_value"] = float(
                            ln.split("=", 2)[-1].strip()
                        )
                    except ValueError:
                        pass
                elif ln.startswith("Met BC=Precipitation|Constant Units="):
                    out["constant_units"] = (
                        ln.split("Units=", 1)[1].strip() or None
                    )
    except OSError:
        pass
    return out


def parse_bc_details_from_text(unsteady_path: Path) -> list[dict]:
    """Per-BC summary parsed from the `.uXX` text file.

    Returns a list of dicts, one per `Boundary Location=` block, with:
      - name:        BC label (last non-empty field of Boundary Location)
      - area:        2D flow area (field 6 in 2D BC lines), if any
      - type:        Flow Hydrograph / Stage Hydrograph / Normal Depth /
                     Rating Curve / Gate / Lateral Inflow / (other)
      - kind:        series | structure | computed | rating | other
      - interval:    Interval= value if present (e.g. "5MIN", "1HOUR")
      - uses_dss:    True if `Use DSS=True` is set in the block
      - dss_path:    DSS Path= value, if any
      - n_values:    declared time-series length (Flow / Stage)
      - value_min / value_max / value_mean: summary of inline values
      - is_constant: True if min == max (within tolerance)
      - friction_slope: from `Friction Slope=...` for Normal Depth BCs
      - fixed_start: from `Fixed Start Date/Time=` if set
    """
    try:
        with open(unsteady_path, "r", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("Boundary Location="):
            i += 1
            continue

        # Parse Boundary Location row.  2D BCs put the BC name in the
        # last non-empty field; the 6th comma-field holds the 2D area.
        bl_fields = [
            f.strip()
            for f in lines[i].split("=", 1)[1].rstrip().split(",")
        ]
        bc_name = next(
            (f for f in reversed(bl_fields) if f), ""
        )
        area = bl_fields[5] if len(bl_fields) > 5 else ""

        block: dict = {
            "name": bc_name,
            "area": area,
            "type": "Other",
            "kind": "other",
            "interval": None,
            "uses_dss": False,
            "dss_path": None,
            "n_values": None,
            "value_min": None,
            "value_max": None,
            "value_mean": None,
            "is_constant": False,
            "friction_slope": None,
            "fixed_start": None,
        }

        # Walk forward until the next `Boundary Location=` (block end).
        j = i + 1
        series_decl_line = None
        while j < len(lines) and not lines[j].startswith(
            "Boundary Location="
        ):
            ln = lines[j].rstrip()
            for kw, (tlabel, tkind) in _BC_TYPE_KEYWORDS:
                if ln.startswith(kw) and block["type"] == "Other":
                    block["type"] = tlabel
                    block["kind"] = tkind
                    if tkind == "series":
                        try:
                            n = int(ln.split("=", 1)[1].strip())
                            block["n_values"] = n
                            series_decl_line = j
                        except (ValueError, IndexError):
                            pass
                    elif tkind == "computed" and kw == "Friction Slope=":
                        try:
                            slope_raw = ln.split("=", 1)[1].split(",")[0]
                            block["friction_slope"] = float(
                                slope_raw.strip()
                            )
                        except (ValueError, IndexError):
                            pass
                    elif tkind == "rating":
                        try:
                            block["n_values"] = int(
                                ln.split("=", 1)[1].strip()
                            )
                        except (ValueError, IndexError):
                            pass
            if ln.startswith("Interval=") and block["interval"] is None:
                block["interval"] = ln.split("=", 1)[1].strip()
            elif ln.startswith("Use DSS=") and not block["uses_dss"]:
                block["uses_dss"] = (
                    ln.split("=", 1)[1].strip().lower() == "true"
                )
            elif ln.startswith("DSS Path=") and not block["dss_path"]:
                v = ln.split("=", 1)[1].strip()
                if v:
                    block["dss_path"] = v
            elif ln.startswith("Fixed Start Date/Time=") \
                    and not block["fixed_start"]:
                v = ln.split("=", 1)[1].strip()
                if v and v != ",":
                    block["fixed_start"] = v
            j += 1

        # For DSS-driven BCs, the authoritative interval lives in the
        # DSS pathname's E-part (the 5th `/`-separated segment of
        # `/A/B/C/D/E/F/`).  The `.u01`'s `Interval=` line is often
        # stale leftover text that doesn't match the actual DSS data.
        if block["uses_dss"] and block["dss_path"]:
            parts = [p for p in block["dss_path"].split("/") if p != ""]
            if len(parts) >= 5:
                e_part = parts[4].strip()
                if e_part:
                    block["dss_interval"] = e_part
                    block["interval"] = e_part

        # Summarise inline values if this is a Flow / Stage series.
        if (
            block["kind"] == "series"
            and block["n_values"]
            and series_decl_line is not None
            and not block["uses_dss"]
        ):
            data_lines = lines[series_decl_line + 1:j]
            lo, hi, mean, is_const = _summarise_series_values(
                data_lines, block["n_values"]
            )
            block["value_min"] = lo
            block["value_max"] = hi
            block["value_mean"] = mean
            block["is_constant"] = is_const

        blocks.append(block)
        i = j

    return blocks

# Mapping from unsteady-text-file keywords to BC group / type.
_TEXT_BC_KEYWORDS: dict[str, tuple[str, bool]] = {
    "Flow Hydrograph=": ("Flow Hydrographs", True),
    "Stage Hydrograph=": ("Stage Hydrographs", True),
    "Friction Slope=": ("Normal Depths", False),
}


def _find_hecras_prj(model_dir: Path) -> Path | None:
    """Return the HEC-RAS project file (not a GIS .prj projection file).

    A HEC-RAS .prj begins with ``Proj Title=``; an ESRI/GIS projection
    file begins with ``PROJCS[`` / ``GEOGCS[``.
    """
    prj_files = sorted(model_dir.glob("*.prj"))
    for prj in prj_files:
        try:
            with open(prj, "r", errors="replace") as fh:
                head = fh.read(256).lstrip()
        except OSError:
            continue
        if head.startswith("Proj Title="):
            return prj
    return prj_files[0] if prj_files else None


def _parse_prj_current_plan(prj_path: Path) -> str | None:
    try:
        with open(prj_path, "r", errors="replace") as fh:
            for line in fh:
                if line.startswith("Current Plan="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def _parse_prj_unit_system(prj_path: Path) -> str | None:
    """Return the model's unit system from the HEC-RAS .prj file.

    The .prj file carries a bare token line - ``SI Units`` or
    ``English Units`` (US customary). Returns ``"SI"``, ``"English"``,
    or ``None`` if neither line is present.
    """
    try:
        with open(prj_path, "r", errors="replace") as fh:
            for line in fh:
                tok = line.strip()
                if tok == "SI Units":
                    return "SI"
                if tok == "English Units":
                    return "English"
    except OSError:
        return None
    return None


def _extract_suffix(raw_ref: str) -> str:
    """Extract the file suffix from a plan reference that may be a relative path.

    ``"g01"`` → ``"g01"``
    ``.\\Hydrology\\project.u01`` → ``"u01"``
    """
    name = raw_ref.replace("\\", "/").rsplit("/", 1)[-1]
    parts = name.rsplit(".", 1)
    return parts[-1] if len(parts) == 2 else raw_ref


def _parse_plan_refs(
    plan_path: Path,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (geom_suffix, unsteady_suffix, sim_start, sim_end)."""
    geom = unsteady = sim_start = sim_end = None
    try:
        with open(plan_path, "r", errors="replace") as fh:
            for line in fh:
                if line.startswith("Geom File=") and geom is None:
                    geom = _extract_suffix(line.split("=", 1)[1].strip())
                elif line.startswith("Flow File=") and unsteady is None:
                    unsteady = _extract_suffix(line.split("=", 1)[1].strip())
                elif line.startswith("Simulation Date=") and sim_start is None:
                    parts = line.split("=", 1)[1].strip().split(",")
                    if len(parts) >= 4:
                        sim_start = f"{parts[0]} {parts[1]}"
                        sim_end = f"{parts[2]} {parts[3]}"
    except OSError:
        pass
    return geom, unsteady, sim_start, sim_end


def _plan_window_from_hdf(plan_hdf: Path):
    """Read ``(start, end)`` from a plan HDF's *Plan Information* attrs.

    Fallback for when the plan TEXT file has no ``Simulation Date=`` line
    (some models store the window only in the HDF after the first
    compute).  Returns strings normalised to the ``'15OCT2022 0000'``
    text format, or ``(None, None)``.
    """
    if not plan_hdf.exists():
        return None, None
    try:
        import h5py
        from datetime import datetime as _dt

        def _norm(grp, attr):
            v = grp.attrs.get(attr)
            if v is None:
                return None
            s = (v.decode() if isinstance(v, bytes) else str(v)).strip()
            try:  # '15Oct2022 00:00:00' -> '15OCT2022 0000'
                return _dt.strptime(s, "%d%b%Y %H:%M:%S").strftime(
                    "%d%b%Y %H%M"
                ).upper()
            except ValueError:
                return s or None

        with h5py.File(plan_hdf, "r") as f:
            g = f.get("Plan Data/Plan Information")
            if g is None:
                return None, None
            return (_norm(g, "Simulation Start Time"),
                    _norm(g, "Simulation End Time"))
    except Exception:
        return None, None


def _parse_bc_lines_from_text(
    unsteady_path: Path,
    geom_hdf: Path | None,
) -> list[dict]:
    """Parse boundary conditions from the unsteady text file (.uXX).

    Used as a fallback when no plan HDF exists (fresh/uncomputed model).
    Cross-references the geometry HDF for authoritative BC line names and
    their 2D flow area assignments.
    """
    # Build authoritative BC name → flow area map from geometry HDF.
    geom_bc_names: list[tuple[str, str]] = []
    if geom_hdf and geom_hdf.exists():
        try:
            with h5py.File(geom_hdf, "r") as f:
                if "Geometry/Boundary Condition Lines/Attributes" in f:
                    attrs = f["Geometry/Boundary Condition Lines/Attributes"][:]
                    for row in attrs:
                        name = row["Name"].decode().strip()
                        area = row["SA-2D"].decode().strip()
                        geom_bc_names.append((name, area))
        except Exception:
            pass

    # Parse BC types from the unsteady text file.  The file may contain
    # truncated names (fixed-width format) or duplicate entries for the
    # same BC line, so we map text-file names to their type.
    text_bc_types: dict[str, tuple[str, bool]] = {}
    try:
        with open(unsteady_path, "r", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Boundary Location="):
            fields = [f.strip() for f in line.split("=", 1)[1].split(",")]
            bc_name = ""
            for f in reversed(fields):
                if f:
                    bc_name = f
                    break

            group = None
            injectable = False
            j = i + 1
            while j < len(lines) and not lines[j].startswith(
                "Boundary Location="
            ):
                for kw, (grp, inj) in _TEXT_BC_KEYWORDS.items():
                    if lines[j].startswith(kw):
                        group = grp
                        injectable = inj
                        break
                if group is not None:
                    break
                j += 1

            if bc_name:
                text_bc_types[bc_name] = (group or "Other", injectable)
        i += 1

    # Match geometry BC names to their types discovered in the text file.
    # Use startswith matching to handle fixed-width truncation.
    bcs: list[dict] = []
    if geom_bc_names:
        for bc_name, area in geom_bc_names:
            group, injectable = "Other", False
            for txt_name, (grp, inj) in text_bc_types.items():
                if bc_name.startswith(txt_name) or txt_name.startswith(bc_name):
                    group, injectable = grp, inj
                    break

            bc_type = _BC_GROUP_TYPES.get(group)
            ds_name = f"2D: {area} BCLine: {bc_name}" if area else bc_name
            bcs.append({
                "hdf_key": f"{_BC_ROOT}/{group}/{ds_name}",
                "group": group,
                "name": ds_name,
                "bc_type": bc_type or group,
                "injectable": injectable,
                "shape": None,
            })
    else:
        # No geometry HDF - fall back to raw text-file names.
        for bc_name, (group, injectable) in text_bc_types.items():
            bc_type = _BC_GROUP_TYPES.get(group)
            bcs.append({
                "hdf_key": f"{_BC_ROOT}/{group}/{bc_name}",
                "group": group,
                "name": bc_name,
                "bc_type": bc_type or group,
                "injectable": injectable,
                "shape": None,
            })

    return bcs


def _scan_boundary_conditions(plan_hdf: Path) -> list[dict]:
    """Enumerate and classify every boundary condition in the plan HDF."""
    bcs: list[dict] = []
    with h5py.File(plan_hdf, "r") as f:
        if _BC_ROOT not in f:
            return bcs
        root = f[_BC_ROOT]
        for group_name in root.keys():
            grp = root[group_name]
            if not isinstance(grp, h5py.Group):
                continue
            bc_type = _BC_GROUP_TYPES.get(group_name)
            injectable = bc_type is not None
            for ds_name in grp.keys():
                ds = grp[ds_name]
                if not isinstance(ds, h5py.Dataset):
                    continue
                bcs.append({
                    "hdf_key": f"{_BC_ROOT}/{group_name}/{ds_name}",
                    "group": group_name,
                    "name": ds_name,
                    "bc_type": bc_type or group_name,
                    "injectable": injectable,
                    "shape": tuple(ds.shape),
                })
    return bcs


def _extract_bc_geometry(hdf_path: Path) -> dict:
    """BC-line centroids + the 2D domain outline, in lon/lat.

    Reads the geometry HDF's Boundary Condition Lines (``Polyline Info``
    + ``Polyline Points``) and the 2D Flow Areas perimeter polygon,
    reprojecting from the model CRS (root ``Projection`` WKT) to
    EPSG:4326.  Returns ``{"outline": [[[lon, lat], …], …],
    "bc_points": {bc_name: [lon, lat]}}``.  Best-effort - any missing
    piece comes back empty so the Tab 3 location map degrades quietly.
    """
    out: dict = {"outline": [], "bc_points": {}}
    try:
        with h5py.File(hdf_path, "r") as f:
            proj = f.attrs.get("Projection")
            if not proj:
                return out
            wkt = proj.decode() if isinstance(proj, bytes) else str(proj)
            from pyproj import Transformer
            tf = Transformer.from_crs(wkt, "EPSG:4326", always_xy=True)

            bc = "Geometry/Boundary Condition Lines"
            if (bc + "/Attributes" in f and bc + "/Polyline Info" in f
                    and bc + "/Polyline Points" in f):
                attrs = f[bc + "/Attributes"][:]
                pinfo = f[bc + "/Polyline Info"][:]
                ppts = f[bc + "/Polyline Points"][:]
                for i, row in enumerate(attrs):
                    s, c = int(pinfo[i][0]), int(pinfo[i][1])
                    seg = ppts[s:s + c]
                    if len(seg) == 0:
                        continue
                    lon, lat = tf.transform(
                        float(seg[:, 0].mean()), float(seg[:, 1].mean())
                    )
                    name = row["Name"].decode().strip()
                    out["bc_points"][name] = [float(lon), float(lat)]

            fa = "Geometry/2D Flow Areas"
            if fa + "/Polygon Info" in f and fa + "/Polygon Points" in f:
                pinfo = f[fa + "/Polygon Info"][:]
                ppts = f[fa + "/Polygon Points"][:]
                for r in pinfo:
                    s, c = int(r[0]), int(r[1])
                    ring = ppts[s:s + c]
                    if len(ring) < 3:
                        continue
                    lon, lat = tf.transform(ring[:, 0], ring[:, 1])
                    out["outline"].append(
                        [[float(a), float(b)] for a, b in zip(lon, lat)]
                    )
    except Exception:
        pass
    return out


def _attach_bc_coords(bc_lines: list[dict], bc_points: dict) -> None:
    """Tag each BC entry with ``lon``/``lat`` from the geometry centroids.

    BC entry names are like ``2D: <area> BCLine: <name>`` while the
    geometry ``Attributes`` name is the bare ``<name>`` - match on the
    ``BCLine:`` suffix, then fall back to substring containment.
    """
    if not bc_points:
        return
    for bc in bc_lines:
        nm = str(bc.get("name", ""))
        short = nm.split("BCLine:")[-1].strip() if "BCLine:" in nm else nm
        coord = bc_points.get(short)
        if coord is None:
            for k, v in bc_points.items():
                if k and (k in nm or nm.endswith(k)):
                    coord = v
                    break
        if coord:
            bc["lon"], bc["lat"] = coord[0], coord[1]


def scan_model(model_dir: str | Path) -> dict:
    """Return auto-detected settings from a HEC-RAS model directory."""
    model_dir = Path(model_dir)
    info: dict = {"model_dir": str(model_dir), "errors": []}

    # --- Project file (skip GIS projection .prj files) ---
    prj = _find_hecras_prj(model_dir)
    if prj is None:
        info["errors"].append("No .prj file found")
        return info
    info["project_name"] = prj.name
    base = prj.stem

    # --- Unit system (SI vs English/US customary) ---
    unit_system = _parse_prj_unit_system(prj)
    if unit_system:
        info["unit_system"] = unit_system

    # --- Active plan (declared in the .prj) ---
    plan_suffix = _parse_prj_current_plan(prj)
    if not plan_suffix:
        matches = sorted(model_dir.glob(f"{base}.p[0-9][0-9]"))
        if matches:
            plan_suffix = matches[0].suffix.lstrip(".")
    if plan_suffix:
        info["plan_suffix"] = plan_suffix

    # --- Geometry / unsteady referenced by the active plan ---
    geom_suffix = unsteady_suffix = None
    if plan_suffix:
        plan_file = model_dir / f"{base}.{plan_suffix}"
        if plan_file.exists():
            geom_suffix, unsteady_suffix, sim_s, sim_e = _parse_plan_refs(
                plan_file
            )
            if sim_s:
                info["original_start"] = sim_s
            if sim_e:
                info["original_end"] = sim_e
        # Fallback: read the window from the plan HDF when the text plan
        # had no 'Simulation Date=' line - otherwise non-Brays models fall
        # back to a hardcoded UI default and show the wrong year.
        if not info.get("original_start") or not info.get("original_end"):
            _hs, _he = _plan_window_from_hdf(
                model_dir / f"{base}.{plan_suffix}.hdf"
            )
            if _hs and not info.get("original_start"):
                info["original_start"] = _hs
            if _he and not info.get("original_end"):
                info["original_end"] = _he

    if not geom_suffix:
        m = sorted(model_dir.glob(f"{base}.g[0-9][0-9]"))
        geom_suffix = m[0].suffix.lstrip(".") if m else None
    if not unsteady_suffix:
        m = sorted(model_dir.glob(f"{base}.u[0-9][0-9]"))
        unsteady_suffix = m[0].suffix.lstrip(".") if m else None

    if geom_suffix:
        info["geom_suffix"] = geom_suffix
    if unsteady_suffix:
        info["unsteady_suffix"] = unsteady_suffix
    if plan_suffix and plan_suffix.startswith("p"):
        info["exec_suffix"] = "x" + plan_suffix[1:]

    # --- 2D flow area names from the geometry HDF ---
    if geom_suffix:
        geom_hdf = model_dir / f"{base}.{geom_suffix}.hdf"
        if geom_hdf.exists():
            try:
                with h5py.File(geom_hdf, "r") as f:
                    if "Geometry/2D Flow Areas" in f:
                        g = f["Geometry/2D Flow Areas"]
                        areas = [
                            k for k in g.keys()
                            if k not in _GEOM_HDF_META_KEYS
                        ]
                        info["flow_areas"] = areas
                        if areas:
                            info["flow_area_name"] = areas[0]
            except Exception as e:
                info["errors"].append(f"Could not read {geom_hdf.name}: {e}")

    # --- Boundary conditions from the plan HDF ---
    plan_hdf_exists = False
    if plan_suffix:
        plan_hdf = model_dir / f"{base}.{plan_suffix}.hdf"
        if plan_hdf.exists():
            plan_hdf_exists = True
            try:
                info["bc_lines"] = _scan_boundary_conditions(plan_hdf)
            except Exception as e:
                info["errors"].append(
                    f"Could not read BCs from {plan_hdf.name}: {e}"
                )

    # Fallback: parse BCs from the unsteady text file when the plan HDF
    # does not exist or contains no BCs (fresh / uncomputed model).
    if not info.get("bc_lines") and unsteady_suffix:
        unsteady_path = model_dir / f"{base}.{unsteady_suffix}"
        geom_hdf_path = (
            (model_dir / f"{base}.{geom_suffix}.hdf")
            if geom_suffix else None
        )
        if unsteady_path.exists():
            try:
                info["bc_lines"] = _parse_bc_lines_from_text(
                    unsteady_path, geom_hdf_path
                )
                info["bc_source"] = "text"
            except Exception as e:
                info["errors"].append(
                    f"Could not parse BCs from {unsteady_path.name}: {e}"
                )

    if not plan_hdf_exists:
        info["needs_plan_hdf"] = True

    # --- BC-line locations + domain outline (for the Tab 3 map) ---
    _geom_src = None
    if geom_suffix:
        _g = model_dir / f"{base}.{geom_suffix}.hdf"
        if _g.exists():
            _geom_src = _g
    if _geom_src is None and plan_suffix:
        _p = model_dir / f"{base}.{plan_suffix}.hdf"
        if _p.exists():
            _geom_src = _p
    if _geom_src is not None:
        _geo = _extract_bc_geometry(_geom_src)
        info["bc_geometry"] = _geo
        if info.get("bc_lines"):
            _attach_bc_coords(info["bc_lines"], _geo.get("bc_points", {}))

        # Local Standard Time offset (hours from UTC), from the model's
        # mean longitude → nearest standard meridian.  "Standard" = no
        # daylight saving, matching how HEC-RAS models keep a continuous
        # clock.  Drives the pipeline's single time base so fetched
        # USGS/NOAA/forecast data aligns with the model window.
        _lons = [
            p[0] for ring in _geo.get("outline", []) for p in ring
        ] or [c[0] for c in _geo.get("bc_points", {}).values()]
        if _lons:
            info["lst_offset_hours"] = int(
                round((sum(_lons) / len(_lons)) / 15.0)
            )

    # ── Per-BC detail block from the unsteady text file ────────────
    # Adds: type, interval, inline-vs-DSS, value min/max/mean,
    # friction slope for Normal Depth, etc.  Used by Tab 1 to give
    # the user a preview of what each boundary contains before they
    # configure Tab 3.
    if unsteady_suffix:
        unsteady_path = model_dir / f"{base}.{unsteady_suffix}"
        if not unsteady_path.exists():
            # Some models tuck the .uXX into a Hydrology/ subfolder.
            sub = list(model_dir.rglob(f"{base}.{unsteady_suffix}"))
            if sub:
                unsteady_path = sub[0]
        if unsteady_path.exists():
            try:
                info["bc_details"] = parse_bc_details_from_text(
                    unsteady_path
                )
            except Exception as e:
                info["errors"].append(
                    f"Could not parse BC details from "
                    f"{unsteady_path.name}: {e}"
                )
            try:
                info["precipitation"] = parse_u01_precipitation(
                    unsteady_path
                )
            except Exception as e:
                info["errors"].append(
                    f"Could not parse precipitation from "
                    f"{unsteady_path.name}: {e}"
                )

    return info
