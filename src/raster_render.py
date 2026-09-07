"""Smooth (sub-grid) inundation raster - RAS-Mapper-style depth map.

The interactive Tab-5 map colours each *computational cell* with one flat
value, so the mesh structure shows through and the result looks blocky.
RAS Mapper instead samples the result back onto the fine **terrain
raster**: for every terrain pixel it computes ``depth = WSE(cell) -
terrain_elevation(pixel)``.  Because the terrain varies continuously
inside a cell, the inundation edge follows the micro-topography and the
map looks smooth and professional.

This module reproduces that, server-side, with no GDAL/rasterio
dependency:

* the terrain GeoTIFF is read with **Pillow** (pixels) + the model's
  ``.vrt`` / GeoTIFF tags (georeferencing),
* cell polygons (already in the model's projected CRS) are burned onto
  the terrain grid with ``PIL.ImageDraw`` to get a per-pixel cell index,
* ``peak_wse[cell_index] - terrain`` gives the peak-depth raster,
* it is coloured to a transparent RGBA PNG and the four corner
  coordinates are reprojected to lon/lat so the app can drop it onto the
  Plotly/Mapbox map as an image overlay.

The DEM and the mesh share the model's projected CRS, so no
reprojection is needed between them - only the final image corners are
converted to WGS-84 for the web overlay.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np


# Pillow refuses very large images by default (decompression-bomb guard);
# DEMs are legitimately large, so lift the cap for this module's reads.
def _open_dem(path: Path):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    return Image.open(path)


def _parse_vrt(vrt_path: Path):
    """Return ``(geotransform, (W, H), tif_path)`` from a GDAL ``.vrt``.

    ``geotransform`` is the 6-tuple ``(x0, dx, 0, y0, 0, dy)`` (north-up
    rasters have ``dy < 0``).  Returns ``None`` on any parse failure.
    """
    try:
        txt = vrt_path.read_text(errors="ignore")
        gt_m = re.search(r"<GeoTransform>(.*?)</GeoTransform>", txt, re.S)
        gt = [float(x) for x in gt_m.group(1).replace(",", " ").split()]
        ds = re.search(r'rasterXSize="(\d+)"\s+rasterYSize="(\d+)"', txt)
        size = (int(ds.group(1)), int(ds.group(2)))
        src = re.search(r"<SourceFilename[^>]*>(.*?)</SourceFilename>", txt)
        tif = (vrt_path.parent / src.group(1).strip()) if src else None
        if tif is None or not tif.exists():
            return None
        return gt, size, tif
    except Exception:
        return None


def _gt_from_tif(tif_path: Path):
    """Geotransform from a GeoTIFF's tags (ModelPixelScale/Tiepoint)."""
    try:
        img = _open_dem(tif_path)
        tags = img.tag_v2
        scale = tags.get(33550)        # (sx, sy, sz)
        tie = tags.get(33922)          # (i, j, k, x, y, z)
        if not scale or not tie:
            return None
        sx, sy = float(scale[0]), float(scale[1])
        i, j, x, y = float(tie[0]), float(tie[1]), float(tie[3]), float(tie[4])
        gt = [x - i * sx, sx, 0.0, y + j * sy, 0.0, -sy]
        return gt, (img.width, img.height)
    except Exception:
        return None


def _find_terrain(model_dir: Path):
    """Locate the terrain DEM under a model folder.

    Returns ``(tif_path, geotransform, (W, H))`` or ``None``.  Prefers a
    ``.vrt`` (carries georeferencing explicitly); otherwise falls back to
    a GeoTIFF read via its tags, favouring files under ``Terrain``/``DEM``
    folders and the largest one.
    """
    model_dir = Path(model_dir)
    for vrt in sorted(model_dir.rglob("*.vrt")):
        parsed = _parse_vrt(vrt)
        if parsed:
            gt, size, tif = parsed
            return tif, gt, size

    tifs: list[Path] = []
    for pat in ("*.tif", "*.tiff", "*.TIF"):
        tifs.extend(model_dir.rglob(pat))

    def _score(p: Path):
        s = str(p).lower()
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        return (("terrain" in s or "dem" in s), sz)

    for tif in sorted(set(tifs), key=_score, reverse=True):
        parsed = _gt_from_tif(tif)
        if parsed:
            gt, size = parsed
            return tif, gt, size
    return None


# Natural water ramp for depth maps - very shallow cyan → deep navy.
# Used everywhere depth is coloured (static smooth map, mesh views and
# the flood-propagation GIF) so the product looks consistent.
WATER_HEX = ["#d8f6ff", "#7fd8d8", "#2b8cbe", "#084081"]
WATER_PLOTLY = [
    [0.0, "#d8f6ff"], [0.33, "#7fd8d8"],
    [0.66, "#2b8cbe"], [1.0, "#084081"],
]


def water_cmap():
    """Matplotlib colormap of the natural water-depth ramp."""
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list(
        "hecinbox_water", WATER_HEX, N=256
    )


def prepare_subgrid(model_dir, cell_fp, fp_xy, target_px: int = 2600):
    """Load the model terrain and burn the mesh onto its pixel grid.

    The shared first stage of every sub-grid rendering (static smooth
    maps and the per-timestep flood animation): the DEM is downsampled so
    its long side ≈ ``target_px``, and each mesh cell's polygon is burned
    onto that grid to give a per-pixel containing-cell index.

    Returns ``(dem, cellidx, (gt0, px, gt3, py), burned)`` - the DEM
    array (NaN where NoData), the int32 cell-index grid (−1 outside the
    mesh), the downsampled geotransform, and the number of cells burned -
    or ``None`` when the model has no usable terrain raster.
    """
    found = _find_terrain(Path(model_dir))
    if not found:
        return None
    tif, gt, _size = found
    gt0, gt1, _, gt3, _, gt5 = gt

    dem_full = np.asarray(_open_dem(tif), dtype=np.float32)
    f = max(1, int(round(max(dem_full.shape) / float(target_px))))
    dem = np.array(dem_full[::f, ::f], dtype=np.float32)
    px, py = gt1 * f, gt5 * f
    H, W = dem.shape
    dem[dem <= -9990] = np.nan           # GDAL NoData sentinel

    from PIL import Image, ImageDraw
    cell_fp = np.asarray(cell_fp)
    fp_xy = np.asarray(fp_xy, dtype=np.float64)
    idx_img = Image.new("I", (W, H), 0)
    drw = ImageDraw.Draw(idx_img)
    burned = 0
    for cid in range(cell_fp.shape[0]):
        idx = cell_fp[cid]
        idx = idx[idx >= 0]
        if len(idx) < 3:
            continue
        xy = fp_xy[idx]
        col = (xy[:, 0] - gt0) / px
        row = (xy[:, 1] - gt3) / py
        drw.polygon(list(zip(col.tolist(), row.tolist())), fill=cid + 1)
        burned += 1
    cellidx = np.asarray(idx_img, dtype=np.int32) - 1
    return dem, cellidx, (gt0, px, gt3, py), burned


def render_smooth_maps(
    *,
    output_dir,
    model_dir,
    peak_wse,
    cell_fp,
    fp_xy,
    proj_wkt: str,
    peak_vel=None,
    coords=None,
    model_is_si: bool = True,
    target_px: int = 2600,
    lo: float = 0.22,
    log=print,
) -> dict | None:
    """Render RAS-Mapper-style smooth peak rasters, one per variable.

    Produces a transparent PNG for each available variable -
    ``smooth_depth.png`` (sub-grid: ``peak WSE − terrain`` per pixel),
    ``smooth_wse.png`` / ``smooth_velocity.png`` (per-cell value painted
    over the whole mesh footprint, blurred so cell-to-cell transitions
    grade continuously) plus ``*_wet.png`` variants masked to the
    sub-grid wet boundary (for the "Wetted only" scope), and
    ``smooth_terrain.png`` (hillshaded DEM) - with ``smooth_maps.json``
    describing the overlay corner lon/lat and the per-variable colour
    range / cmap.

    Returns the metadata dict, or ``None`` if it could not run (no
    terrain, no projection, missing polygons …).  Never raises.
    """
    try:
        if not proj_wkt:
            log("Smooth raster: model has no projection (proj_wkt) - skipped")
            return None
        if cell_fp is None or fp_xy is None:
            log("Smooth raster: no mesh polygons - skipped")
            return None

        prepared = prepare_subgrid(
            model_dir, cell_fp, fp_xy, target_px=target_px
        )
        if prepared is None:
            log("Smooth raster: no terrain DEM found in model - skipped")
            return None
        dem, cellidx, (gt0, px, gt3, py), burned = prepared
        H, W = dem.shape

        peak_wse = np.asarray(peak_wse, dtype=np.float32)
        cell_fp = np.asarray(cell_fp)
        fp_xy = np.asarray(fp_xy, dtype=np.float64)

        covered = cellidx >= 0
        if not covered.any():
            log("Smooth raster: mesh did not overlap the DEM grid - skipped")
            return None
        ci = cellidx[covered]
        from PIL import Image

        # depth = peak WSE of the containing cell − terrain elevation.
        depth = np.full((H, W), np.nan, np.float32)
        depth[covered] = peak_wse[ci] - dem[covered]
        depth[~np.isfinite(depth)] = np.nan
        depth[depth < 0.01] = np.nan
        wet = np.isfinite(depth)        # sub-grid wet boundary
        if not wet.any():
            log("Smooth raster: no wet pixels at peak - skipped")
            return None

        import matplotlib as mpl

        def _paint(per_cell):
            """Per-cell value → pixel grid, over the whole mesh footprint.

            WSE and velocity exist across the entire wetted cell area, not
            just the sub-grid-wet pixels - painting them over all covered
            pixels makes the smooth map match the filled-cell (whole-
            domain) extent instead of collapsing to the channel.
            """
            out = np.full((H, W), np.nan, np.float32)
            out[covered] = np.asarray(per_cell, np.float32)[ci]
            return out

        # Pixel-centre coordinate grid (model CRS) for interpolation.
        _pxx = gt0 + (np.arange(W) + 0.5) * px
        _pyy = gt3 + (np.arange(H) + 0.5) * py

        # Cell centres for the TIN: passed-in result coords if available,
        # else each cell's facepoint centroid.
        if coords is not None and len(coords) == cell_fp.shape[0]:
            _centers = np.asarray(coords, np.float64)[:, :2]
        else:
            _centers = np.full((cell_fp.shape[0], 2), np.nan)
            for cid in range(cell_fp.shape[0]):
                idx = cell_fp[cid]
                idx = idx[idx >= 0]
                if len(idx) >= 3:
                    _centers[cid] = fp_xy[idx].mean(axis=0)

        def _interp_paint(per_cell):
            """Per-cell value → linearly interpolated (TIN) pixel surface.

            This is how RAS Mapper renders cell results smoothly: a
            Delaunay triangulation of the cell centres evaluated at every
            terrain pixel - crisp continuous gradients, no blocky cell
            patches and no blur fog.  Pixels outside the convex hull (or
            on failure) keep the flat per-cell value as fallback.
            """
            vals = np.asarray(per_cell, np.float32)
            out = np.full((H, W), np.nan, np.float32)
            out[covered] = vals[ci]                  # nearest-cell base
            try:
                import matplotlib.tri as mtri
                good = (
                    np.isfinite(vals)
                    & np.isfinite(_centers).all(axis=1)
                )
                if good.sum() >= 3:
                    tri = mtri.Triangulation(
                        _centers[good, 0], _centers[good, 1]
                    )
                    itp = mtri.LinearTriInterpolator(tri, vals[good])
                    xx, yy = np.meshgrid(_pxx, _pyy)
                    z = itp(xx, yy).filled(np.nan).astype(np.float32)
                    m = covered & np.isfinite(z)
                    out[m] = z[m]
            except Exception:
                pass                                  # keep flat fallback
            return out

        def _range(field):
            m = np.isfinite(field)
            vmin = float(np.nanpercentile(field[m], 1))
            vmax = float(np.nanpercentile(field[m], 99))
            if vmax - vmin < 1e-6:
                vmax = vmin + 0.1
            return vmin, vmax

        def _save(field, fname, cmap_name, lift, vmin, vmax, gamma=1.0):
            """Colour *field* and write the PNG; returns the RGBA array.

            ``gamma`` < 1 spends more of the colour ramp on the low end
            (power-law normalisation).  Flood depth is heavily skewed -
            the channel is metres deep while the floodplain, the part
            people care about, sits in the bottom few percent of the
            range - so a linear ramp paints the whole floodplain one
            flat pale blue.  gamma 0.5 keeps the shallow band readable.
            """
            m = np.isfinite(field)
            frac = np.clip((np.nan_to_num(field) - vmin) / (vmax - vmin),
                           0.0, 1.0)
            if gamma != 1.0:
                frac = frac ** gamma
            ramped = (lift + (1.0 - lift) * frac) if lift else frac
            _cm = (
                mpl.colormaps[cmap_name] if isinstance(cmap_name, str)
                else cmap_name
            )
            rgba = (_cm(ramped) * 255).astype(np.uint8)
            rgba[~m] = (0, 0, 0, 0)
            Image.fromarray(rgba, "RGBA").save(Path(output_dir) / fname)
            return rgba

        u = "m" if model_is_si else "ft"
        v = "m/s" if model_is_si else "ft/s"
        variables: dict = {}

        # Depth - true sub-grid, coloured with the natural water ramp
        # (very-shallow cyan is already clearly visible, so no lift
        # needed).  Wet-by-definition: both scopes use the same file.
        _, dvmax = _range(depth)
        # gamma < 1: flood depth is heavily right-skewed (deep channel,
        # shallow floodplain), so a linear ramp leaves the floodplain
        # one flat pale blue.  The colorbar stays truthful because the
        # SAME warp is baked into the plotly stops written below - the
        # app draws linear tick positions against the warped gradient.
        _dgamma = 0.5
        depth_rgba = _save(
            depth, "smooth_depth.png", water_cmap(),
            0.0, 0.0, dvmax, gamma=_dgamma,
        )
        _wcm_g = water_cmap()
        import matplotlib.colors as _mc
        depth_plotly = [
            [round(float(q), 4), _mc.to_hex(_wcm_g(float(q) ** _dgamma))]
            for q in np.linspace(0.0, 1.0, 13)
        ]
        variables["Water Depth"] = {
            "file": "smooth_depth.png", "file_wet": "smooth_depth.png",
            "vmin": 0.0, "vmax": round(dvmax, 3),
            "units": u, "cmap": "water", "plotly": depth_plotly,
            "gamma": _dgamma,
        }

        depth_cls_rgba = None
        # Depth classes - the same depth banded into discrete hazard
        # categories, the way official flood-hazard maps present it
        # (each band reads as a practical consequence: passable on
        # foot, vehicles stall, adults swept away, ground floor
        # submerged).  Written alongside the continuous ramp so the
        # app can offer it as a display toggle; colours are the water
        # ramp sampled at five even steps so both styles match.
        try:
            import matplotlib.colors as mcolors
            cls_bounds = (
                [0.15, 0.5, 1.0, 2.0] if model_is_si   # m
                else [0.5, 1.5, 3.0, 6.5]              # ft
            )
            _wcm = water_cmap()
            cls_hex = [
                mcolors.to_hex(_wcm(f))
                for f in (0.0, 0.25, 0.5, 0.75, 1.0)
            ]
            lut = np.array(
                [
                    [int(h[1:3], 16), int(h[3:5], 16),
                     int(h[5:7], 16), 255]
                    for h in cls_hex
                ],
                np.uint8,
            )
            band = np.digitize(np.where(wet, depth, 0.0), cls_bounds)
            rgba_c = lut[band]
            rgba_c[~wet] = (0, 0, 0, 0)
            Image.fromarray(rgba_c, "RGBA").save(
                Path(output_dir) / "smooth_depth_classes.png"
            )
            depth_cls_rgba = rgba_c
            _lb = [f"{b:g}" for b in cls_bounds]
            variables["Water Depth"]["file_classes"] = (
                "smooth_depth_classes.png"
            )
            variables["Water Depth"]["classes"] = {
                "bounds": cls_bounds,
                "colors": cls_hex,
                "labels": [
                    f"< {_lb[0]}",
                    f"{_lb[0]} to {_lb[1]}",
                    f"{_lb[1]} to {_lb[2]}",
                    f"{_lb[2]} to {_lb[3]}",
                    f"> {_lb[3]}",
                ],
            }
        except Exception as _ce:
            log(f"Depth classes: skipped - {type(_ce).__name__}: {_ce}")

        # WSE / velocity - painted per cell over the whole footprint,
        # blurred for continuous gradients; a wet-masked variant serves
        # the "Wetted only" scope.  Both share one colour range so the
        # two scopes are directly comparable.
        def _painted_pair(per_cell, base, cmap_name, plotly_name, unit):
            field = _interp_paint(per_cell)
            vmin, vmax = _range(field)
            _save(field, f"{base}.png", cmap_name, 0.0, vmin, vmax)
            _save(np.where(wet, field, np.nan), f"{base}_wet.png",
                  cmap_name, 0.0, vmin, vmax)
            return {
                "file": f"{base}.png", "file_wet": f"{base}_wet.png",
                "vmin": round(vmin, 3), "vmax": round(vmax, 3),
                "units": unit, "cmap": cmap_name, "plotly": plotly_name,
            }

        variables["Water Surface Elevation"] = _painted_pair(
            peak_wse, "smooth_wse", "viridis", "Viridis", u)
        if peak_vel is not None:
            variables["Velocity"] = _painted_pair(
                peak_vel, "smooth_velocity", "turbo", "Turbo", v)

        # Terrain - hillshaded, full-resolution DEM with a green→red
        # hypsometric ramp (the "nice" RAS-Mapper look).  Masked to the
        # mesh footprint so it matches the modelled domain.
        try:
            import matplotlib.pyplot as plt
            from matplotlib.colors import LightSource, Normalize
            terr = np.where(covered & np.isfinite(dem), dem, np.nan)
            tm = np.isfinite(terr)
            if tm.any():
                tvmin = float(np.nanpercentile(terr, 1))
                tvmax = float(np.nanpercentile(terr, 99))
                if tvmax - tvmin < 1e-6:
                    tvmax = tvmin + 1.0
                z = np.where(tm, terr, np.nanmean(terr))
                ls = LightSource(azdeg=315, altdeg=45)
                rgba = ls.shade(
                    z, cmap=plt.get_cmap("RdYlGn_r"),
                    norm=Normalize(tvmin, tvmax),
                    blend_mode="soft", vert_exag=2.0,
                    dx=abs(px), dy=abs(py),
                )
                rgba = (rgba * 255).astype(np.uint8)
                rgba[~tm] = (0, 0, 0, 0)
                Image.fromarray(rgba, "RGBA").save(
                    Path(output_dir) / "smooth_terrain.png"
                )
                variables["Terrain (DEM)"] = {
                    "file": "smooth_terrain.png",
                    "vmin": round(tvmin, 2), "vmax": round(tvmax, 2),
                    "units": u, "cmap": "RdYlGn_r",
                    # Green (low) → yellow → red (high), for the colorbar.
                    "plotly": [
                        [0.0, "rgb(0,104,55)"],
                        [0.25, "rgb(166,217,106)"],
                        [0.5, "rgb(255,255,191)"],
                        [0.75, "rgb(253,174,97)"],
                        [1.0, "rgb(165,0,38)"],
                    ],
                }

                # Water over shaded relief - the RAS-Mapper composite:
                # dry pixels show the hillshaded terrain, wet pixels
                # take the depth colour *modulated by the hillshade
                # intensity*, so the terrain relief reads through the
                # flood instead of a flat colour sheet.  Modulation
                # (not transparency) keeps the depth hues true to the
                # colorbar.  One composite per depth style (continuous
                # + classes); the app offers them as a toggle.
                try:
                    shade = ls.hillshade(
                        z, vert_exag=2.0, dx=abs(px), dy=abs(py)
                    ).astype(np.float32)[..., None]
                    smod = 0.55 + 0.45 * shade

                    def _composite(water_rgba, fname):
                        base = rgba.copy()
                        wm = water_rgba[..., 3] > 0
                        wcol = np.clip(
                            water_rgba[..., :3].astype(np.float32)
                            * smod, 0, 255,
                        ).astype(np.uint8)
                        base[wm, :3] = wcol[wm]
                        base[wm, 3] = 255
                        Image.fromarray(base, "RGBA").save(
                            Path(output_dir) / fname
                        )

                    _composite(depth_rgba, "smooth_depth_shaded.png")
                    variables["Water Depth"]["file_shaded"] = (
                        "smooth_depth_shaded.png"
                    )
                    if depth_cls_rgba is not None:
                        _composite(
                            depth_cls_rgba,
                            "smooth_depth_classes_shaded.png",
                        )
                        variables["Water Depth"]["file_classes_shaded"] = (
                            "smooth_depth_classes_shaded.png"
                        )
                except Exception as _se:
                    log(
                        f"Shaded-relief composite: skipped - "
                        f"{type(_se).__name__}: {_se}"
                    )
        except Exception as _te:
            log(f"Smooth terrain: skipped - {type(_te).__name__}: {_te}")

        # Four corner lon/lat (projected model CRS → WGS-84), in the
        # order Plotly's image layer expects: TL, TR, BR, BL.
        from pyproj import CRS, Transformer
        tr = Transformer.from_crs(
            CRS.from_wkt(proj_wkt), CRS.from_epsg(4326), always_xy=True
        )
        xL, xR = gt0, gt0 + W * px
        yT, yB = gt3, gt3 + H * py
        corners = [(xL, yT), (xR, yT), (xR, yB), (xL, yB)]
        coordinates = [list(tr.transform(x, y)) for x, y in corners]

        meta = {"coordinates": coordinates, "variables": variables}
        (Path(output_dir) / "smooth_maps.json").write_text(
            json.dumps(meta, indent=2)
        )
        log(
            f"Smooth rasters: {burned} cells → {int(wet.sum())} wet px · "
            f"{', '.join(variables)}"
        )
        return meta
    except Exception as e:  # never break a run over a picture
        log(f"Smooth raster: failed - {type(e).__name__}: {e}")
        return None
