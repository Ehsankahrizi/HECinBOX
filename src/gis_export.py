"""Write a finished run out as standard GIS layers.

HECinBOX kept its results in ``wse_extract.npz`` and its own maps, so
nothing a run produced could be opened in QGIS or ArcGIS, overlaid on
other data, or archived alongside a paper.  This module turns a run
into layers any GIS reads:

* ``mesh_peak.geojson``  - the 2D cells as real polygons, each carrying
  its peak depth, peak water surface, peak velocity and bed elevation.
  This is the faithful export: no interpolation, the model's own
  geometry.
* ``peak_depth.asc`` / ``peak_wse.asc`` / ``terrain.asc`` - the same
  peaks on a regular grid, for the classic flood-map overlay, with a
  companion ``.prj`` so a GIS places them correctly.
* ``boundaries.geojson`` - the 2D domain outline and the boundary
  condition lines.

Everything is written with numpy, matplotlib and pyproj, all of which
the image already carries.  Proper Cloud-Optimized GeoTIFF would mean
adding rasterio, which in turn needs GDAL and a system library the
slim base image does not ship, so the rasters use ESRI ASCII Grid:
plain text, no dependencies, and read by QGIS, ArcGIS, GDAL and
HEC-RAS alike.

Vector layers are written in EPSG:4326 because that is what web maps
and GeoJSON consumers expect.  Rasters stay in the model's own
projected CRS, because resampling a flood depth grid into degrees
would distort the cell sizes the depths were computed on.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np

NODATA = -9999.0
# Long side of the exported rasters, in pixels.  Big enough to keep a
# city-scale floodplain legible, small enough that the ASCII grids stay
# a few megabytes rather than tens.
RASTER_LONG_SIDE = 1200


def _peaks(d):
    """Peak water surface, depth, velocity and bed elevation per cell."""
    wse = np.asarray(d["wse"], dtype=np.float32)
    bed = np.asarray(d["min_elev"], dtype=np.float32)
    with np.errstate(all="ignore"):
        peak_wse = np.nanmax(wse, axis=0)
        depth = np.where(
            np.isfinite(peak_wse) & np.isfinite(bed),
            np.maximum(peak_wse - bed, 0.0),
            np.nan,
        )
    if "vel" in d.files:
        with np.errstate(all="ignore"):
            peak_vel = np.nanmax(
                np.asarray(d["vel"], dtype=np.float32), axis=0
            )
    else:
        peak_vel = np.full(bed.shape, np.nan, dtype=np.float32)
    return peak_wse, depth, peak_vel, bed


def _cell_polygons(d):
    """Yield (cell_index, [(x, y), …]) for every usable mesh cell.

    ``cell_fp`` holds up to eight face-point indices per cell, padded
    with -1.  A handful of entries carry only two points; those are not
    polygons and are skipped rather than exported as slivers.
    """
    fp = np.asarray(d["cell_fp"])
    xy = np.asarray(d["fp_xy"], dtype=np.float64)
    for i, row in enumerate(fp):
        idx = row[row >= 0]
        if idx.size < 3:
            continue
        yield i, xy[idx]


def _transformer(d):
    """Model CRS → EPSG:4326, or None when the CRS is unreadable."""
    try:
        from pyproj import Transformer
        wkt = str(np.asarray(d["proj_wkt"]).ravel()[0])
        if not wkt.strip():
            return None
        return Transformer.from_crs(wkt, "EPSG:4326", always_xy=True)
    except Exception:
        return None


def _write_mesh_geojson(path, d, log=print):
    """Cell polygons in EPSG:4326 with their peak values attached."""
    peak_wse, depth, peak_vel, bed = _peaks(d)
    tf = _transformer(d)
    if tf is None:
        log("GIS export: no usable projection - skipping the mesh layer.")
        return None

    def _num(v):
        return None if not np.isfinite(v) else round(float(v), 3)

    n = 0
    with open(path, "w") as fh:
        fh.write('{"type":"FeatureCollection",'
                 '"crs":{"type":"name","properties":'
                 '{"name":"urn:ogc:def:crs:OGC:1.3:CRS84"}},'
                 '"features":[')
        first = True
        for i, poly in _cell_polygons(d):
            lon, lat = tf.transform(poly[:, 0], poly[:, 1])
            ring = [
                [round(float(a), 7), round(float(b), 7)]
                for a, b in zip(lon, lat)
            ]
            ring.append(ring[0])           # GeoJSON rings must close
            feat = {
                "type": "Feature",
                "properties": {
                    "cell": int(i),
                    "depth_max": _num(depth[i]),
                    "wse_max": _num(peak_wse[i]),
                    "vel_max": _num(peak_vel[i]),
                    "bed_elev": _num(bed[i]),
                },
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
            fh.write(("" if first else ",") + json.dumps(feat))
            first = False
            n += 1
        fh.write("]}")
    log(f"GIS export: {path.name} - {n} cell polygons.")
    return path


def _grid(d, log=print):
    """Peak WSE and bed elevation on a regular grid in the model CRS.

    Returns ``(depth, wse, terrain, transform)`` where transform is
    ``(xllcorner, yllcorner, cellsize, ncols, nrows)``.  Uses the same
    triangulated interpolation the Smooth results view already relies
    on, so the exported raster and the on-screen surface agree.
    """
    from matplotlib.tri import LinearTriInterpolator, Triangulation

    coords = np.asarray(d["coords"], dtype=np.float64)
    peak_wse, _depth, _vel, bed = _peaks(d)
    ok = (
        np.isfinite(coords[:, 0]) & np.isfinite(coords[:, 1])
        & np.isfinite(bed)
    )
    if ok.sum() < 3:
        log("GIS export: too few valid cells to build a raster.")
        return None

    x, y = coords[ok, 0], coords[ok, 1]
    bed_ok = bed[ok]
    wse_ok = np.where(np.isfinite(peak_wse[ok]), peak_wse[ok], bed_ok)
    try:
        tri = Triangulation(x, y)
        f_bed = LinearTriInterpolator(tri, bed_ok)
        f_wse = LinearTriInterpolator(tri, wse_ok)
    except Exception as e:
        log(f"GIS export: could not triangulate the mesh ({e}).")
        return None

    x0, x1 = float(x.min()), float(x.max())
    y0, y1 = float(y.min()), float(y.max())
    span = max(x1 - x0, y1 - y0)
    if span <= 0:
        return None
    cell = span / float(RASTER_LONG_SIDE)
    ncols = max(2, int(np.ceil((x1 - x0) / cell)))
    nrows = max(2, int(np.ceil((y1 - y0) / cell)))
    # Cell centres, north row first, the way an ASCII grid is written.
    gx = x0 + (np.arange(ncols) + 0.5) * cell
    gy = y1 - (np.arange(nrows) + 0.5) * cell
    mx, my = np.meshgrid(gx, gy)

    terr = np.ma.filled(f_bed(mx, my), np.nan)
    wse = np.ma.filled(f_wse(mx, my), np.nan)
    with np.errstate(all="ignore"):
        depth = np.where(
            np.isfinite(wse) & np.isfinite(terr), wse - terr, np.nan
        )
        # Outside the wet area the interpolated surface sits on the bed;
        # a hair of numerical noise there is not flooding.
        depth = np.where(depth > 0.01, depth, np.nan)
    return depth, wse, terr, (x0, y0, cell, ncols, nrows)


def _write_asc(path, arr, transform, wkt, log=print):
    """One ESRI ASCII Grid plus the .prj that georeferences it."""
    x0, y0, cell, ncols, nrows = transform
    grid = np.where(np.isfinite(arr), arr, NODATA)
    with open(path, "w") as fh:
        fh.write(
            f"ncols {ncols}\nnrows {nrows}\n"
            f"xllcorner {x0:.6f}\nyllcorner {y0:.6f}\n"
            f"cellsize {cell:.6f}\nNODATA_value {NODATA:.0f}\n"
        )
        np.savetxt(fh, grid, fmt="%.3f", delimiter=" ")
    if wkt:
        path.with_suffix(".prj").write_text(wkt)
    log(f"GIS export: {path.name} - {ncols}x{nrows} @ {cell:.2f} units.")
    return path


def _write_boundaries(path, bc_geometry, log=print):
    """Domain outline and boundary lines, already in lon/lat."""
    geom = bc_geometry or {}
    feats = []
    for ring in (geom.get("outline") or []):
        if len(ring) < 3:
            continue
        closed = [[round(p[0], 7), round(p[1], 7)] for p in ring]
        closed.append(closed[0])
        feats.append({
            "type": "Feature",
            "properties": {"layer": "2D domain"},
            "geometry": {"type": "Polygon", "coordinates": [closed]},
        })
    for name, pt in (geom.get("bc_points") or {}).items():
        feats.append({
            "type": "Feature",
            "properties": {"layer": "boundary condition", "name": name},
            "geometry": {
                "type": "Point",
                "coordinates": [round(pt[0], 7), round(pt[1], 7)],
            },
        })
    if not feats:
        return None
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {
            "name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": feats,
    }))
    log(f"GIS export: {path.name} - {len(feats)} feature(s).")
    return path


def export_run(npz_path, out_dir, bc_geometry=None, log=print):
    """Write every GIS layer for one run. Returns the files created."""
    npz_path, out_dir = Path(npz_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    d = np.load(npz_path, allow_pickle=True)

    wkt = ""
    try:
        wkt = str(np.asarray(d["proj_wkt"]).ravel()[0])
    except Exception:
        pass
    units = "unknown"
    try:
        units = str(np.asarray(d["unit_system"]).ravel()[0])
    except Exception:
        pass

    made = []
    mesh = _write_mesh_geojson(out_dir / "mesh_peak.geojson", d, log)
    if mesh:
        made.append(mesh)

    g = _grid(d, log)
    if g:
        depth, wse, terr, transform = g
        for name, arr in (
            ("peak_depth", depth), ("peak_wse", wse), ("terrain", terr),
        ):
            made.append(
                _write_asc(out_dir / f"{name}.asc", arr, transform, wkt, log)
            )
            if wkt:
                made.append(out_dir / f"{name}.prj")

    bnd = _write_boundaries(out_dir / "boundaries.geojson", bc_geometry, log)
    if bnd:
        made.append(bnd)

    length = "feet" if str(units).lower().startswith("eng") else "metres"
    readme = out_dir / "README.txt"
    readme.write_text(
        "HECinBOX GIS export\n"
        "===================\n\n"
        f"Run            : {npz_path.parent.name}\n"
        f"Model units    : {units} (depths and elevations in {length};\n"
        f"                 velocity in {length}/s)\n\n"
        "mesh_peak.geojson  2D mesh cells as polygons, EPSG:4326.\n"
        "                   Attributes per cell: depth_max, wse_max,\n"
        "                   vel_max, bed_elev. No interpolation - this\n"
        "                   is the model's own geometry and values.\n"
        "peak_depth.asc     Peak depth on a regular grid, in the model's\n"
        "peak_wse.asc       own projected CRS (see the .prj files).\n"
        "terrain.asc        Dry ground is NODATA in peak_depth.\n"
        "boundaries.geojson 2D domain outline and boundary locations.\n\n"
        "The rasters are interpolated from cell values onto a regular\n"
        "grid, so the polygons are the authoritative layer where the two\n"
        "disagree. Open the .asc files in QGIS or ArcGIS directly; the\n"
        ".prj beside each one carries the projection.\n"
    )
    made.append(readme)
    return [p for p in made if p and Path(p).exists()]


def export_zip(npz_path, out_dir, bc_geometry=None, log=print):
    """Write the layers and bundle them into one downloadable zip."""
    out_dir = Path(out_dir)
    gis_dir = out_dir / "gis"
    files = export_run(npz_path, gis_dir, bc_geometry, log)
    if not files:
        return None
    zip_path = out_dir / f"{Path(out_dir).name}_gis.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=Path(f).name)
    log(
        f"GIS export: {zip_path.name} "
        f"({zip_path.stat().st_size / 1e6:.1f} MB, {len(files)} files)."
    )
    return zip_path
