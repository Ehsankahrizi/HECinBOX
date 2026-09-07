# HECinBOX

Automated 2D unsteady HEC-RAS flood simulation pipeline with a Streamlit web interface. Fetches real-time USGS/NOAA boundary conditions, injects them into your HEC-RAS model, runs the Linux compute engine, extracts results, and validates against observed gages - optionally on a continuous real-time schedule.

## Quick Start

### Mac / Linux

```bash
mkdir -p ~/HEC-RAS-Outputs

docker run -d --platform linux/amd64 -p 8501:8501 \
  --cpus=4 \
  -v ~/:/host:ro \
  -v ~/HEC-RAS-Outputs:/host_out \
  ehsankahrizi1991/hecinbox:v4.0.0
```

> **Tip:** `--cpus=4` gives the container 4 CPU cores. Increase it (e.g. `--cpus=8`) for faster simulations. The Run tab shows available cores and lets you choose how many threads HEC-RAS uses.

### Windows (PowerShell)

```powershell
mkdir "$HOME\HEC-RAS-Outputs"

docker run -d --platform linux/amd64 -p 8501:8501 `
  --cpus=4 `
  -v $HOME:/host:ro `
  -v $HOME\HEC-RAS-Outputs:/host_out `
  ehsankahrizi1991/hecinbox:v4.0.0
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

## Features

- **Model Scanner** - auto-detects HEC-RAS project files, plan/geometry suffixes, 2D flow areas, and boundary conditions from your `.prj` and `.hdf` files
- **Real-time Agent** - define rules (cell threshold, domain peak, wetted-area) that watch every completed simulation and send email alerts the moment a condition transitions - built for unattended-server forecasting
- **Cloud Storage (S3)** - load a model straight from an Amazon S3 (or S3-compatible) bucket and upload finished runs back to the cloud - ideal for running on an AWS/GC server
- **Real-Time Data Fetch** - pulls discharge (USGS) and tide/stage (NOAA) observations and injects them as boundary conditions
- **HEC-RAS Linux Engine** - runs RasGeomPreprocess + RasUnsteady headless with live progress tracking
- **Parallel Computation** - select number of CPU threads in the Run tab; the app detects available cores automatically
- **Detached Runs** - simulations survive browser tab close, screen sleep, or websocket drops
- **Interactive Inundation Map** - RAS-Mapper-style filled-cell mesh with hover-per-cell, peak/timestep slider, multiple basemaps
- **Flood Propagation Animation** - timelapse of flood spreading across the domain with Play/Pause, speed control, and downloadable HTML
- **Variable Selector** - WSE, Velocity, Water Depth, Terrain (DEM) with RAS-Mapper color ramps
- **Validation** - compares model output against observed gage data with NSE, RMSE, MAE, bias, and 1:1 scatter plots
- **Auto-Scheduling** - continuous real-time forecasting on a configurable interval
- **Interactive Plotly Charts** - all time series are zoomable, hoverable, and downloadable as CSV, HTML, or PNG

## Using the Web UI

1. Choose a **model source** (Tab 1) - *This machine* (a mounted folder) or *Cloud storage (S3)* - then select your HEC-RAS model
2. Configure simulation dates, boundary conditions, and USGS/NOAA station IDs (Tabs 2-3)
3. Click **Run Simulation** (Tab 4) - watch live engine progress
4. Explore results: inundation map, flood animation, time series (Tab 5)
5. Validate against observed gages (Tab 6)

## Cloud Storage (S3)

HECinBOX can load a model from - and save results to - Amazon S3 or any **S3-compatible** service (Cloudflare R2, MinIO, Wasabi, Backblaze B2, …). In Tab 1, pick **Cloud storage (S3)**, paste the S3 URI of your model folder, and set a results S3 URI; the finished run is uploaded there automatically.

Credentials are never entered in the web UI - they come from the container's environment:

```bash
docker run -d --platform linux/amd64 -p 8501:8501 \
  --cpus=4 \
  -e AWS_ACCESS_KEY_ID=your_key \
  -e AWS_SECRET_ACCESS_KEY=your_secret \
  -e AWS_DEFAULT_REGION=us-east-1 \
  ehsankahrizi1991/hecinbox:v4.0.0
```

| Env var | Description |
|---------|-------------|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Cloud credentials. **Omit entirely** when running on an AWS server with an attached IAM role - access is granted automatically. |
| `AWS_DEFAULT_REGION` | Bucket region, e.g. `us-east-1`. |
| `S3_ENDPOINT_URL` | Optional - custom endpoint for an S3-compatible service (e.g. `https://<accountid>.r2.cloudflarestorage.com`). |

When deployed on an EC2/ECS instance, attach an IAM role with S3 read/write permissions and pass **no credentials at all**.

## Agent - Real-time Email Alerts

Tab 7 lets you define rules that watch each completed simulation and email a decision-maker the moment a condition transitions from below → above its threshold. SMTP credentials come from env vars at `docker run` time - nothing sensitive is typed into the web UI:

```bash
docker run -d --platform linux/amd64 -p 8501:8501 \
  --cpus=4 \
  -e SMTP_HOST=smtp.gmail.com \
  -e SMTP_PORT=587 \
  -e SMTP_USER=alerts@example.com \
  -e SMTP_PASS=<app_password> \
  -e SMTP_FROM=alerts@example.com \
  -e SMTP_TO=engineer@example.com \
  -v ~/:/host:ro -v ~/HEC-RAS-Outputs:/host_out \
  ehsankahrizi1991/hecinbox:v4.0.0
```

| Env var | Description |
|---------|-------------|
| `SMTP_HOST` / `SMTP_PORT` | Mail server (`587` for STARTTLS, `465` for SSL). |
| `SMTP_USER` / `SMTP_PASS` | Login credentials. For Gmail, use an **app password**, not your regular password. |
| `SMTP_FROM` | From address that appears on outgoing alerts. |
| `SMTP_TO` | Default recipient; individual rules can override with a comma-separated list. |

In Tab 7 click *Send test email* to validate the setup before going live.

---

## Changelog

### v4.8.0 - Model time base is now an explicit choice

- **Fixed: buttons carrying a help tooltip rendered as bare text.** Streamlit wraps any button that has a `?` tooltip in an extra tooltip element, which meant the app's button styling (written as a direct-child rule) never reached it, while the help-icon rule stripped its background, border and shadow. **Reset**, **Run Simulation**, **Stop schedule** and **Update schedule with current settings** all looked like plain labels instead of buttons. They now match every other button; the `?` icons themselves are unchanged.

- **The model's time zone must now be chosen by hand before a run can start.** Previously HECinBOX guessed it from the model's longitude (Local Standard Time) and pre-filled that guess, which was silently wrong for any model whose simulation window was kept in UTC. Nothing failed when the guess was wrong: the run completed, the maps looked normal, and the only symptom was a validation hydrograph shifted sideways by a whole number of hours. The setting now starts empty, the **Run** button stays disabled until you pick, and the detected local-standard-time value is offered as one of the options rather than applied for you.
- **Three options:** local standard time at the model site (with the detected offset shown), UTC, or a custom offset you type in.
- **A full explanation lives in the field's ? tooltip,** in plain language: why HEC-RAS cannot tell us the time zone (it stores dates as a bare clock reading, with no time-zone field anywhere in the project), why the data services can (USGS, NOAA CO-OPS, NWM and STOFS all publish in UTC), and a worked two-row table showing exactly what to pick depending on whether the model's boundary data was left on the gage's local clock or converted to UTC before it went into HEC-RAS. It also explains how to detect a wrong choice after the fact from the Tab 6 validation plot.
- If an auto-schedule is already armed, the time base you chose when you armed it is restored on reload instead of being asked again.
- **Results, validation and alerts now read in local standard time, with UTC alongside.** Times render as `2022-10-14 18:00 LST(UTC-6)  ·  2022-10-15 00:00 UTC` (the UTC date is repeated only when it falls on a different day). This covers the Results tab summary and timestep slider, the cell time-series plot, the validation time-series plot, and the alert email. The alert email in particular used to mix two clocks in one message: "Fired at" was UTC while the simulation window below it was on the model's own clock, and only the first was labelled. Both are now on the same footing.
- **The Timestep slider names its clock.** With **Peak** off, the slider reads `Timestep - LST(UTC-6)` and the exact selected step is spelled out underneath on both clocks, so scrubbing through a 2D or 3D map never leaves you looking at an unlabelled time.
- **The live dashboard, the flood-propagation video and the Tab 2 window preview follow the same rule.** The live cumulative plot and its axis are on local standard time, each video frame is labelled with its clock, and Tab 2 now says which clock the model's own detected window is on.
- **Exported files carry the UTC offset.** The cell time-series CSV and the model-vs-observed CSV now use ISO-8601 with an explicit offset (`2022-10-14T18:00:00-06:00`) instead of a bare wall-clock string, so an exported file can never be misread the way a HEC-RAS window can.
- **A run computed before v4.8.0 is never reinterpreted.** Those folders recorded no clock, so their times are shown exactly as the model stored them, with a note saying the clock was not recorded. Guessing there would repeat the very bug this release removes.
- **Every run folder now records its own clock.** `wse_extract.npz` and `run_meta.json` (schema 2) carry `model_utc_offset_hours` (what the stored timestamps mean) alongside the existing `unit_system`, plus `site_lst_offset_hours` (Local Standard Time at the model site, for display). Until now a saved run described its units but not its clock, so nothing downstream could convert or even honestly label a timestamp. Folders opened from a raw HEC-RAS project record the clock as `null`, meaning genuinely unknown, rather than guessing.

### v4.7.6 - Agent alert units, and a scheduling crash fix

- **Agent alerts now label depth and velocity in the model's units.** For an SI model the triggered-alert history (and email) showed `depth (ft)` / `velocity (ft/s)` even though the plots, the rule builder, and the compared values were all in metres. The values and thresholds were always correct and in native units, only the label was wrong; it now reads `depth (m)` / `velocity (m/s)` for SI models and `ft` / `ft/s` for English models. Unit system is read from the run's own `wse_extract.npz`.

- Enabling **Auto-run scheduling** (Tab 2) while the forecast window was **auto-sized** from a fixed-horizon boundary (NWM short/medium/long range or STOFS) crashed with `NameError: name 'realtime_days' is not defined` the moment you clicked Run. The `realtime_days` dial is hidden in that mode, so the value it fed to the scheduler never existed. The window value is now always defined, from the actual Tab 2 window, so arming a schedule works in every mode.
- The scheduler is now **direction-aware**. A forecast schedule re-forecasts *forward* each run (now to now+horizon); a hindcast schedule looks *backward* (now-N to now) as before. Previously the daemon always built a backward window, so a scheduled forecast would have silently run the wrong dates. Existing schedules keep working unchanged.

### v4.7.4 - Results map: depth classes, shaded relief, readable shallow depths

- The Smooth (peak) water-depth map has a new **Depth classes** toggle: instead of the continuous colour ramp, depth is shown in five discrete bands (SI models: below 0.15, 0.15 to 0.5, 0.5 to 1, 1 to 2, above 2 m; English models use 0.5 / 1.5 / 3 / 6.5 ft), the way official flood-hazard maps present it. Each band reads as a practical risk level; the continuous ramp stays the default.
- New **Shaded relief** toggle: drapes the water over the hillshaded terrain, RAS-Mapper style. Dry ground shows the shaded relief and the terrain texture reads through the flood, instead of a flat colour sheet floating on the basemap. Combines with Depth classes.
- The continuous depth ramp now uses a power-law colour scale (gamma 0.5). Flood depth is heavily skewed - the channel is metres deep while the floodplain sits in the bottom of the range - so the old linear ramp painted the whole floodplain one flat pale blue. Shallow depths are now clearly differentiated, and the colorbar gradient is warped identically so it stays truthful.
- All of it works in both the 2D map and the 3D draped view, with matching colorbars / legends.
- The new rasters are written at run time, so the toggles appear for runs computed from this version on; older run folders keep showing the previous continuous map.

### v4.7.1 - Demo run queue (concurrency cap)

- The demo now caps how many simulations run at once (default **2**, set by `DEMO_MAX_CONCURRENT`). Additional users are placed in a first-come queue and their run starts automatically when a slot frees up.
- Queued users see a clear note explaining that, due to limited shared demo resources, only N runs execute at a time and their run will begin shortly (with a Cancel option).
- Queue state is a single lock-guarded file shared across sessions; only active in demo mode.

### v4.7.0 - Concurrent-safe multi-user demo

- The hosted demo now isolates each browser session, so two or more people can test at the same time without clobbering each other. Each session gets its own settings file, active-job pointer, saved-prefs file, and output folder (keyed by a per-session id).
- The run pipeline was already isolated (each run copies the model to a unique temp workspace and writes to its own output folder); this closes the remaining shared-file gaps on the app side.
- Non-demo (single-user / scheduled) deployments are unchanged and still use the shared global state files.
- Note: this is correctness/isolation. Running many heavy 2D simulations at once still shares the container's CPU and memory, so concurrent runs can be slower on a small task.

### v4.6.4 - Copy polish, header, field help, expanded manual

- Removed all em/en dashes across the app's text.
- Header now reads "An automated agent-based flood warning framework using 2D unsteady HEC-RAS simulations".
- Added help (?) tooltips explaining USGS parameter codes (00060 = streamflow, 00065 = stage, etc.) and where to find USGS/NOAA station IDs, on the boundary-condition and validation inputs.
- Rewrote the Tab 9 User Manual into a detailed, step-by-step guide: quick start, per-tab instructions on exactly what to enter, a field reference, and troubleshooting.

### v4.6.3 - Help icons: transparent background

- Help (?) tooltip icons now have a fully transparent background - no shading box, border, or shadow - so they render as a clean outlined glyph everywhere.

### v4.6.2 - Tab 2 time-zone UI polish

- Added a divider below the **Model time zone** control so it's visually separated from **Real-time mode**.
- Fixed the crowded/overlapping layout: the offset input now sits on its own row with its explanation as a full-width caption below it.
- Normalized all help (?) tooltip icons to the outline (no-fill) Material Symbols style so they're consistent across the app.

### v4.6.1 - Model time-zone control moved to Tab 2

- The **"Model time zone (UTC offset)"** control now lives in **Tab 2 · Simulation Window** (where the time settings belong) instead of Tab 3; Tab 3 shows a short read-only note. The real-time window is now anchored in the model's local standard time (not UTC) so it matches the shifted data.

### v4.6.0 - Single, consistent Local Standard Time base for all data

- **All fetched data now aligns to one model time zone (Local Standard Time).** USGS, NOAA, and the forecast clients all return UTC internally; previously they were injected/validated against the model window as-is, so a model whose window was in local time was misaligned by the UTC offset (≈5-6 h) - worst for USGS, which had no time-zone option at all.
- HECinBOX now **auto-detects the model's Local Standard Time offset from its longitude** (no daylight saving) and shifts every fetched series to it, so tide/flow timing lines up with the flood event. A **"Model time zone (UTC offset)"** control in Tab 3 shows the detected value and lets you override it (set 0 to work in UTC).
- The per-boundary NOAA "Time zone" dropdown is removed - the single model-level setting governs all sources.
- **Action required:** enter your Tab 2 simulation window in the model's **local standard time**. If your model window was previously in UTC, set the time-zone offset to **0**. Existing scheduled runs default to 0 (UTC), preserving old behavior until re-saved.

### v4.5.9 - Boundary-location map in Tab 3 (multi-upstream gauge assignment)

- **Tab 3 now shows a locator map** of the 2-D domain outline with a **numbered red dot for each boundary condition**, matching the numbered BC blocks below it. When a model has more than one upstream inflow, this lets you see *where* each boundary sits before assigning it a USGS/NOAA gauge - so you don't wire the wrong gauge to the wrong river.
- The model scanner now extracts each BC line's centroid and the domain perimeter from the geometry HDF (reprojected to lon/lat). Per-BC gauge assignment already worked; this makes it safe to use with multiple boundaries.

### v4.5.8 - Removed the 3-D flood video option

- **The 3-D perspective option for the propagation video has been removed.** The video now always renders in 2-D (map view), which is faster, keeps the satellite/terrain basemap, and avoids the added image weight. The interactive 3-D maps (Filled cells and Smooth) are unaffected - 3-D exploration lives there.

### v4.5.7 - Physical 3-D flood views, orbiting 3-D animation, true satellite basemap on the GPU map

- **The 3-D inundation view is now physical.** The old "height ∝ value" towers are gone. Two height bases: **Ponding** (terrain flattened onto the map, water rising by its depth) and **Physical** (gray terrain at ground elevation with water columns topping out at the true water-surface elevation - the water visibly fills the river channel and rises/spreads as you scrub the timestep). Vertical exaggeration is 1-10×, and a **View direction** control (plus right-drag/Ctrl+drag) rotates the scene to view it from any side.
- **Smooth style renders in 3-D too.** The smooth peak map is draped over the physical surface (terrain + peak water-surface elevation) as a GPU terrain mesh - a continuous RAS-Mapper-style 3-D surface, no cell columns.
- **3-D flood animation.** The propagation video can now render in 3-D perspective: coloured water surfaces over gray terrain while the camera orbits the scene a full 360°. MP4 and GIF, same frames/speed controls.
- **Real satellite imagery on the GPU (Filled cells) map.** Satellite/Terrain basemaps are now true tile services in the pydeck view (crisp at every zoom) instead of silently falling back to the Light vector map.
- Fix: turning **Peak** off while viewing Terrain (a single-frame variable) crashed the timestep slider (`RangeError: min (0) is equal/bigger than max (0)`).

### v4.4.0 - Gridded rain on mesh: AORC (hindcast) + HRRR (forecast)

- **Real spatially-varying rainfall on the 2D mesh.** Beyond the existing *Constant* rate, Rain on Mesh now has two **Gridded** modes that fetch true gridded precipitation, resample it onto a model-CRS grid, and write it into the plan as HEC-RAS *Gridded* precipitation (the engine-faithful raster format, not a single uniform cell):
  - **AORC (hindcast)** - NOAA's ~800 m hourly Analysis of Record (the precipitation behind the National Water Model), for reconstructing a *past* storm. Anonymous-S3 Zarr, US coverage, back to 1979 (~10-day lag).
  - **HRRR (forecast)** - NOAA's 3 km High-Resolution Rapid Refresh forecast, fetched live from the latest model cycle, for predicting an *upcoming* flood (forecast horizon ~18 h). Reads only the precip field from the official GRIB2 via byte-range.
- Set the simulation window in Tab 2, pick the gridded mode in Tab 3, and run - the rainfall is fetched over your model's footprint automatically. A fetch failure falls back to no rain so the run still completes.
- No GDAL required - AORC uses the existing Zarr/xarray stack; HRRR uses bundled-binary GRIB wheels.

### v4.3.5 - GIF colour range matches the map + smarter legend placement

- **The animation's colour range now matches the static smooth map.** The GIF previously scaled colours to the raw frame min/max (dominated by a few outlier cells - e.g. velocity stretched to 1.6 m/s while the map showed 0-0.45), washing most of the field into one hue. Smooth-style GIFs now read the same robust 1-99 % range the static map uses, so the two are directly comparable.
- **The GIF legend no longer covers the flood.** The colorbar card is placed automatically on whichever side of the view has the least flooded footprint, and it is more compact (smaller bar and fonts).

### v4.3.4 - GIF polish: stable colours, readable legend, natural water ramp

- **Fix: the GIF's colour mapping no longer "breathes" between frames.** Frames were each quantised to their own 256-colour palette, so the colour scale visibly shifted frame-to-frame (most noticeable in the colorbar) while real map changes were masked. All frames now share one palette - the colorbar is rock-stable and the actual per-pixel flood transitions show clearly.
- **Fix: the GIF legend is now fully inside the frame.** A white inset card on the right with the title above the bar and tick numbers on its left - nothing clipped at the edge anymore.
- **Natural water-depth ramp.** Depth is now coloured very-shallow cyan → teal → deep navy (`#d8f6ff → #7fd8d8 → #2b8cbe → #084081`) consistently across the static smooth map, mesh views, and the animation.

### v4.3.3 - True smooth flood-propagation animation

- **The GIF animation now renders genuinely smooth frames when the map style is Smooth.** Each frame is computed on the terrain pixel grid - depth(t) = WSE(t) − terrain per pixel, and WSE/velocity via per-frame TIN interpolation - instead of falling back to filled cells. The animation is now the moving twin of the static smooth map and matches **all** the settings above it (variable, map style, basemap, and cells scope).
- **Per-frame wetting makes propagation clear.** With *Wetted only*, dry pixels are transparent each frame, so you watch the flood spread and recede across the terrain over time.
- **Nicer water ramp.** The depth animation uses the same truncated-Blues ramp as the static smooth map - shallow water is clearly light blue, dry ground fully transparent.
- Falls back to filled-cell frames only if the model's terrain folder is no longer available at its recorded path.

### v4.3.2 - Crisp smooth maps (TIN interpolation, no more fog)

- **Smooth WSE/Velocity maps are now rendered by linear (TIN) interpolation between cell centres** - the same approach RAS Mapper uses - instead of blurring per-cell patches. The result is crisp: sharp channel lines and clean continuous gradients at any zoom, with none of the foggy softness of v4.3.1. Pixels outside the triangulation keep the flat per-cell value as fallback.

### v4.3.1 - Sharper smooth maps, wetted-only scope, in-map legend, boxed map

- **Smooth maps are sharper and truly smooth.** The raster resolution was raised (~2× finer pixels), and the per-cell painted variables (WSE, Velocity) are now blurred with a cell-size-aware kernel so values grade continuously between neighbouring cells instead of showing flat blocky patches. Depth keeps its exact sub-grid edge; Terrain benefits from the resolution bump.
- **"Wetted only" now works in Smooth style.** WSE and Velocity get a wet-masked raster variant (same colour range as the full-domain one, so the two scopes are directly comparable). Depth is wet-by-definition; Terrain always shows the full footprint.
- **Consistent in-map legend.** The Filled-cells (GPU) view now draws its colour legend as a white inset card on the right edge *inside* the map - matching the Plotly maps' inset colorbar position.
- **The map is framed in a bold box** in all three styles.

### v4.3.0 - GPU mesh rendering (pydeck) + 3D view

- **The "Filled cells" map is now GPU-rendered with pydeck (deck.gl).** Panning and zooming a large 2D mesh is far smoother than the previous SVG-based rendering, and the cells draw faster. Hover still shows the per-cell value.
- **New 3D view.** A *3D view (height ∝ value)* toggle extrudes each mesh cell by its value - right-drag (or Ctrl+drag) to tilt the camera. Great for visualising depth or WSE as a surface.
- Basemaps for the GPU mesh use tokenless Carto vector styles (Light / Dark / Streets); the **Satellite** basemap remains available in the **Smooth** and **Points** map styles.

### v4.2.2 - Fix: smooth WSE/velocity maps now cover the full domain

- **Fix: the Smooth map for Water Surface Elevation and Velocity no longer collapses to the channel.** It previously masked those variables to the sub-grid *wet* pixels (correct for depth, wrong for WSE/velocity), so they showed only a thin line along the channel. They now paint over the full mesh footprint, matching the Filled-cells view. Depth still uses the sub-grid wet boundary (it's an inundation product).

### v4.2.1 - Hillshaded terrain map

- **The Terrain map now looks like RAS Mapper.** Selecting **Variable → Terrain (DEM)** with the **Smooth (peak)** map style renders a hillshaded, full-resolution DEM with a green→yellow→red hypsometric ramp - the same 3-D relief look as HEC-RAS RAS Mapper - instead of one flat colour per mesh cell. Generated at run time from the model's terrain DEM (no extra dependencies).

### v4.2.0 - Rain-toggle fix + per-variable smooth maps + better animation

- **Fix (important): turning the Rain on Mesh toggle off now guarantees no rain.** Previously, a model that shipped with precipitation enabled in its Meteorological Data kept applying that rain even when the toggle was off (because HECinBOX left the model's met settings untouched) - flooding the whole domain and skewing validation. The toggle is now the master switch: **off → precipitation is stripped from the run**, regardless of what the model came with. To *keep* a model's own rain, enable the toggle and choose **Leave unchanged**.
- **Smooth map now follows the Variable selector.** The "Smooth (peak)" map style renders the *selected* variable - Water Depth (true sub-grid `WSE − terrain`), Water Surface Elevation, or Velocity - instead of only depth. All variables share the same sub-grid wet boundary, so the inundation edge stays smooth.
- **Better flood-propagation animation.** The GIF now renders as filled mesh cells (not scattered points) for both the *Filled cells* and *Smooth* map styles, and honours the selected variable - a much cleaner animation for slides and reports.

### v4.1.0 - Smooth (RAS-Mapper-style) inundation map

- **New "Smooth (peak depth)" map style in the Results tab.** Alongside the per-cell *Filled cells* / *Points* views, the interactive map can now render a smooth inundation surface that looks like HEC-RAS RAS Mapper. Instead of colouring each computational cell one flat value (which shows the blocky mesh), it samples the result back onto the model's fine **terrain DEM** - depth at every terrain pixel is `peak WSE (of the containing cell) − terrain elevation` - so the inundation edge follows the real topography (streets, channel banks).
- **Generated automatically at run time, no extra dependencies.** When a run's model includes a terrain DEM, the pipeline produces a georeferenced `smooth_depth.png` (+ corner coordinates) and the Results map offers it as an overlay. The terrain GeoTIFF is read with Pillow and the cell→pixel mapping is burned with the model's own projection - no GDAL/rasterio required. Runs without a terrain DEM simply fall back to the cell views.
- The smooth view shows **peak water depth** over the whole simulation; the *Filled cells* / *Points* styles remain for every variable (WSE, velocity, depth) and per-timestep playback.

### v4.0.0 - Rain on Mesh (mesh-wide precipitation forcing)

- **New Tab 3 section: Rain on Mesh.** Precipitation is no longer something you can only bake in beforehand - HECinBOX can now drive a 2D model with rainfall applied directly onto the mesh, in addition to (or instead of) the boundary-condition inflows. Unlike a boundary condition, rain is a *mesh-wide* meteorological forcing: every cell of every 2D flow area receives it, matching the HEC-RAS *Unsteady Flow → Meteorological Data → Precipitation* concept.
- **Two modes, mirroring the BC rows:**
  - **Constant** - apply a single uniform rate (mm/hr or in/hr) over the whole mesh for the entire simulation window. Works in both SI and English-unit models.
  - **Leave unchanged** - keep the model's own precipitation. A model-defined constant rate is **automatically re-timed** to your simulation window, so a re-dated run still sees the rain it should (the arrays baked into a ready model only cover the window it was last computed for). Gridded/point precipitation is passed through untouched.
- **"Are you sure?" confirmation gate.** If a model has precipitation **disabled** in its native Meteorological Data and you switch the toggle on, HECinBOX warns that the model wasn't built with rain on mesh and asks you to confirm before exposing the rate controls - so a stale toggle can't silently add a forcing the model never had.
- **Engine-faithful injection.** The pipeline writes the full plan-HDF precipitation structure the Linux compute engine requires (hourly `Timestamp`/`Values` arrays plus the per-cell/per-face mesh mapping), bit-for-bit matching what the Windows HEC-RAS GUI produces - verified end-to-end against the engine, not just the file format. The run's `summary.txt` provenance block records the rain source and rate.

### v3.1.5 - Unit-system detection + correct unit conversion for every BC source

- **Tab 1 now detects and displays the model's unit system.** The model scanner reads the `SI Units` / `English Units` token from the HEC-RAS `.prj` file and the **Detected Model** panel shows a new **Unit system** row - `SI (metric - m, m³/s)` or `English (US customary - ft, cfs)` - so you know a freshly opened project's units at a glance, before configuring anything.
- **Boundary-condition unit conversion is now driven by the detected unit system (bug fix).** Previously the two injection paths made *opposite*, hard-coded assumptions: the USGS/NOAA path always converted to SI (`cfs→cms`, `ft→m`), while the NWM/STOFS forecast path defaulted to English (`m³/s→cfs`, `m→ft`). On a model whose units didn't match the assumption, injected boundary values were off by the conversion factor (~35× for flow, ~3.28× for stage). All source→model conversions - USGS discharge/stage, NOAA tide (english/metric), NWM streamflow, STOFS TWL, and rating-curve stage - now convert into the model's *actual* native units. The detected `unit_system` is written into the run settings and consumed by the pipeline (`main.py`); for older settings files it falls back to re-reading the `.prj`.
- **All outputs now report in the model's native units (bug fix).** Results were previously force-converted to feet and every label/column hard-coded to `ft` / `ft/s`, regardless of the model - so an SI model produced feet output. Worse, the conversion was keyed off the horizontal **map-projection** unit (`proj_wkt`), which is independent of the vertical computational units. HEC-RAS already stores WSE/depth/velocity in the model's native units, so the pipeline no longer converts them; instead the run folder is made **self-describing** (`unit_system` saved into `wse_extract.npz` and `run_meta.json`) and every consumer labels from it: `summary.txt`, peak-map PNGs, the peak-cell CSV (`wse_m`/`wse_ft`, …), the Tab 5 inundation map / flood GIF / scheduler monitor, the Tab 6 validation plots, and the Tab 7 alert-rule hints. Validation also converts **observed** gage data into the model's units so NSE/RMSE/KGE compare like-for-like. New shared module `src/units.py`.

### v3.1.4 - Pre-flight validation of alert-agent rules

- Agent rules are now validated when saved, surfacing misconfiguration immediately instead of letting a rule silently fail mid-run on an unattended server (e.g. at 2 a.m.).

### v3.1.3 - Robust output-folder selection

- Results/Validation tabs fall back to the latest run on disk when no run is explicitly selected, so reopening the app after a detached or scheduled run still finds the most recent results.

### v3.1.2 - Persistent schedule & run history across container recreation

- **Schedule and run history now survive container recreation.** Daemon state, schedule config, and run history are read/written through a single consistent path (persisted volume) instead of the ephemeral `/app`, which was a common surprise when a container was recreated.
- Daemon-refresh beacon fragment reworked so the UI reflects daemon-driven cycles reliably (renders nothing visible).

### v3.1.1 - Scheduler & alert-agent robustness fixes

- Schedule template keeps a BC entry when it has an active boundary condition (no longer dropped on reload).
- Alert agent no longer loses an event when a rule transitions while a run is in flight; alert failures now report both the threshold value **and** the underlying SMTP error.
- Removed a legacy CSS rule that could render the progress bar empty - the theme handles it.

### v3.1.0 - TEEHR-inspired evaluation metrics (Validation overhaul)

- New `src/evaluation.py`: rigorous, standardized verification on top of the basic goodness-of-fit metrics, reimplementing **TEEHR**'s *methods* as a lightweight NumPy-only layer (no Spark/Iceberg).
  - **Kling-Gupta Efficiency (KGE)** with its `(r, alpha, beta)` decomposition - the modern hydrologic skill metric.
  - **Moving-block bootstrap 95% confidence intervals** on the metrics table.
  - **Ensemble verification via CRPS** for probabilistic/forecast sources.

### v3.0.3 - Explicit forecast source dropdowns + UI-state restore

- Forecast sources split into **two explicit dropdown entries** instead of one ambiguous selector; the picker chooses the right one per BC type.
- For a true forecast, the window length is dictated by the chosen horizon.
- Forecast UI state is restored on page reload so daemon-driven cycles never drift from the displayed configuration.

### v3.0.2 - Hindcast vs Forecast time direction in Real-time mode

- **Tab 2 - Real-time mode now exposes a Hindcast / Forecast direction.** Previously the only behaviour was *Pull the most recent N days* → a **backward-looking** window, which is correct for USGS / NOAA observations but **wrong** for NWM / STOFS forecast sources where the window should be **forward-looking** (now → now + N).
  - **Hindcast** (default for observation sources): window = `[now − N days, now]`. N defaults to 7, max 90.
  - **Forecast** (for NWM / STOFS sources): window = `[now, now + N days]`. N defaults to 10, max 30 (matches NWM long-range horizon).
  - Each option's *help* text suggests matching N to the chosen NWM horizon (short=1d, medium=10d, long=30d).
- **Consistency warning.** If the Tab 2 direction doesn't match the Tab 3 BC sources (e.g., Forecast direction with USGS-only BCs, or Hindcast direction with a Forecast BC), a yellow warning explains the mismatch before the user clicks Run - no more silent fall-back to model defaults.

### v3.0.1 - Forecast horizon explanations + progress-bar fix

- **Per-horizon explanation popups in Tab 3.** Selecting a NWM forecast horizon now reveals a context-sensitive info box explaining what that product *is* (issue cadence, temporal resolution, ensemble structure, retention) and *when to use it*. Covers `analysis_assim` (nowcast), `short_range` (18 h), `medium_range` (10 d, recommended default), `long_range` (30 d). Reduces the most common new-user mistake - picking the wrong horizon for the simulation window.
- **Run progress-bar blue fill restored.** The CSS at line 1096 still targeted Streamlit's pre-1.30 nesting (`.stProgress > div > div > div > div`), which no longer matches Streamlit 1.35's `[data-testid="stProgress"] > [role="progressbar"]` DOM. Bar appeared as a grey track with no blue fill during runs. New CSS targets both the modern `data-testid` + `role="progressbar"` selectors and the legacy chain, so the blue fill renders regardless of Streamlit version.

### v3.0.0 - NWM / STOFS forecast sources (major release)

The boundary-condition system now supports **four forecast products** under a single new *Forecast (NWM/STOFS)* source on Tab 3, in addition to the existing USGS / NOAA / Constant sources:

- **NWM streamflow** (`CHRTOUT`) at any NHDPlus reach (COMID) - drives upstream / inland *flow* boundaries directly. Horizons: `analysis_assim`, `short_range` (18 h), `medium_range` (10 d), `long_range` (30 d). Ensemble member configurable.
- **NWM Q + USGS rating curve → stage** - *Path A*, for inland *stage* boundaries co-located with a USGS gauge. Pulls the empirically calibrated stage-discharge rating from `waterdata.usgs.gov` and converts each NWM-forecast Q to stage. Most defensible where a USGS rating exists.
- **NWM Q + HAND synthetic rating → stage** - *Path B*, universal fallback for ungauged inland stage boundaries. Pulls the Height-Above-Nearest-Drainage synthetic rating from NOAA's NWPS API. Modelled, not measured - use only where Path A is unavailable.
- **STOFS-3D Total Water Level** (Atlantic / Pacific) - drives coastal / tidal *stage* boundaries with NOAA's coastal forecast: astronomical tide + storm surge + steric effects + wave setup combined. The right product for downstream BCs on coastal HEC-RAS models (e.g. Brays Bayou).

**UI:** Tab 3 now exposes a context-aware Forecast workflow - Flow BCs get NWM Q only; Stage BCs first pick *Inland* vs. *Coastal*, then inland picks *Path A* vs. *Path B* with full pop-up explanations of each rating-curve provenance and its caveats (out-of-range clamping, calibration gauge transferability, HAND modelling assumptions).

**New module:** `src/forecast_client.py` (~430 LOC) - `NWMClient` (S3 NetCDF reads via `s3fs` + `xarray`), `STOFSClient` (same), `USGSRatingCurve` (text-table parse), `HANDRatingCurve` (NWPS REST), and a `fetch_forecast_series` dispatcher that the main pipeline calls exactly like the existing USGS / NOAA paths.

**Pipeline:** `main.py` recognises `source: forecast` as a fourth BC source, applies the right unit factor per product (NWM m³/s → cfs, STOFS m → ft, rating-curve outputs already in ft), and annotates `summary.txt` with a `FORECAST  nwm_q  (n pts)` provenance line. The schedule template re-hydrates Forecast UI fields on page reload exactly like USGS/NOAA/Constant, so daemon-driven cycles never drift from the displayed configuration.

**Validation untouched:** Tab 6 continues to validate against observed USGS / NOAA gauge data. The framework deliberately avoids validating model-vs-model (forecast-vs-forecast) because that would be circular.

**Dependencies added:** `s3fs`, `xarray`, `h5netcdf`, `netCDF4`.

### v2.10.9 - Header polish + responsive layout

- **Oversized HECinBOX title.** Replaced the default `st.title` with a custom `<h1 class="hb-title">` styled at `clamp(2rem, 6vw, 5.5rem)` - fluid sizing that fills the previously-empty space on wide screens (up to 5.5 rem on desktops), shrinks gracefully on narrow screens (down to 2 rem on phones). Gradient fill from navy → water blue. Inter @ 800 weight, tight letter-spacing, no extra top margin.
- **⟲ Reset relocated.** Moved out of the page header into Tab 1, right of the **Select Model** subheader. Keeps the global header clean (now just title + logo) and puts Reset contextually closer to the model selection it actually clears.
- **Responsive layout.** Added `@media (max-width: 900px)` and `@media (max-width: 600px)` rules: tighter page padding, smaller logo, BC widget rows stack vertically instead of using fixed-width columns on phones. `.stApp { overflow-x: hidden }` prevents horizontal scroll from long file paths / DSS pathnames on narrow screens.

### v2.10.8 - Reset button actually resets everything

- Two related problems v2.10.7 didn't fix:
  1. On a fresh container start, Tab 1 showed the previously-selected model folder (e.g. `/host/Desktop/ReadyModels/run/BaselineModel`) instead of `/host`.
  2. Clicking ⟲ Reset didn't visibly clear that path.
- Both stemmed from v2.10.5's "prefs file lives on the persistent volume" change: the file survives Reset and gets re-loaded immediately on the next render, restoring the old browse path.
- v2.10.8 fixes ⟲ Reset to be truly aggressive:
  - Wipes `USER_PREFS_FILE`, `SCHEDULE_FILE`, and `HISTORY_FILE` from disk.
  - Sets a one-shot `_reset_tombstone` flag that makes `_load_user_prefs()` and `_hydrate_bc_widgets_from_active_schedule()` skip restoration on the next render even if the files somehow reappear.
  - Shows a toast ("🧹 App reset - model, schedule, history & prefs cleared") so you can confirm it ran.

### v2.10.7 - Hide Reset button (and header) in `?live=1` view

- The Reset button (top-right) was still visible on the `?live=1` shared URL, meaning any viewer could stop the schedule and clear results - defeating the purpose of "read-only sharing." The entire page header (title, version chip, Reset button, logo) now disappears when `?live=1` is set, leaving only the Live Dashboard heading and panels.
- Live-mode detection moved up to run *before* the header renders, so the header block is skipped entirely (not just hidden via CSS).

### v2.10.6 - Live tab polish: gap-aware cumulative + focus-cell marker on map

- **Cumulative plot no longer draws a connector line between iterations.** Each iteration's time series is still part of one trace, but a single `None` value is inserted between adjacent iterations and `connectgaps=False` is set on the Scatter, so Plotly breaks the line at the boundary. The long horizontal line that previously spanned the entire x-axis (joining the last point of iter_001 to the first of iter_NN) is gone.
- **Red focus-cell marker on the depth map.** The "Latest iteration - depth map" panel now always renders as an interactive Plotly scatter (replacing the static `peak_depth_map.png` we used to display), with a hollow red ring drawn at the cell the cumulative plot tracks. Title now shows `(🔴 Cell N tracked above)` so viewers know exactly which location the time series corresponds to.

### v2.10.5 - BC dropdowns now persist correctly across cycles + container restarts

- **Tab 3 BC dropdowns no longer reset to "Leave unchanged"** after a cycle finishes or a page reload. Two complementary fixes:
  1. **Schedule template is now the source of truth.** On every page load, if an auto-schedule is active, HECinBOX reads the boundary-condition list from the daemon's `settings_template` (the same one each cycle actually uses) and pre-populates Tab 3's widget state - Source, station ID, parameter code, NOAA datum / timezone, Constant value + unit label. The UI now always matches what the daemon is doing.
  2. **User-prefs file moved to the persistent volume.** Default location is now `/host_out/.hecinbox_user_prefs.json` (the writable host mount) instead of `/app/.user_prefs.json` (lost on container restart). Falls back to the old path for deployments that don't mount `/host_out`.

### v2.10.4 - Fix blank `/?live=1` page

- v2.10.0-v2.10.3 tried to render the Live tab as a normal Streamlit tab and hide tabs 1-7 via a `:nth-of-type(8)` CSS selector. Streamlit's DOM doesn't lay out tab panels as direct siblings of one nominal parent, so the selector didn't match and the live URL came up blank.
- The live URL now uses a proper short-circuit: when `?live=1` is detected, the app renders the live header + panels directly and calls `st.stop()` - bypassing `st.tabs()` entirely. No CSS hacks needed, and there's literally no other content in the DOM for the viewer to interact with.

### v2.10.3 - Live dashboard: event-driven refresh + toggle

- **New "📡 Enable Live dashboard" toggle** in Tab 4 · Run, right under "🔔 Enable alert agent". Off → Tab 8 stays frozen on the last view. Default is on.
- **Refresh is now event-driven**, not timer-driven. The Live tab polls the auto-scheduler history file's mtime every 3 s; when a new iteration finishes (mtime jumps) the panels re-render and a small toast appears: *"🔄 New iteration loaded into the Live dashboard."* Between cycles the rendered content is preserved so the chart and map don't flicker.

### v2.10.2 - Tab 8 refresh no longer jumps back to Tab 1

- The 15-s auto-refresh on Tab 8 used `st_autorefresh` (or a meta-refresh fallback), which triggers a **full Streamlit rerun**. Streamlit's `st.tabs` doesn't preserve the active tab across reruns, so every 15 s the page snapped back to Tab 1 - frustrating, and made the Live view almost unusable.
- Replaced with `@st.fragment(run_every=15)`, which re-runs **only the Live panel** in place. The user stays on Tab 8, plot and map refresh smoothly, no tab navigation lost.

### v2.10.1 - Tab 8 fixes: working Copy-URL button + correct iteration count

- **Copy view-only URL button** now works. Previously the button HTML rendered as literal text because Streamlit's markdown sanitizer strips `onclick=` handlers. Switched to `streamlit.components.v1.html()` (iframe-based), which is not sanitised. The button now properly reads `window.parent.location` and writes `<host>/?live=1` to the clipboard with a "✓ Copied" confirmation.
- **Iterations completed** now reads the daemon's actual field `runs_completed` (was reading a non-existent `completed_count`, always showed 0). If the field hasn't been written yet, falls back to counting `state="done"` entries in the on-disk history file. Same fallback for `next_run_iso`.

### v2.10.0 - New **Tab 8 · Live** read-only dashboard

- A dedicated **Live** tab that auto-refreshes every 15 s while an auto-schedule is running. Shows two panels:
  1. **Live cumulative depth plot** at the wettest cell, stitching every completed iteration of the active schedule.
  2. **Latest-iteration depth map** (the `peak_depth_map.png` produced by the run, with an in-browser scatter fallback if the PNG is missing).
- A **"Copy view-only URL"** button on the tab adds `?live=1` to the page URL and copies it to clipboard. Anyone who opens that link sees only Tab 8 - the other tabs, the page header, the Run button, and Streamlit's hamburger menu are hidden via CSS. Soft read-only: editing the URL gets the full app back, so use only for trusted recipients; for a public URL add auth at the reverse proxy.
- When the schedule isn't running, the tab shows a placeholder explaining how to start one.

### v2.9.5 - Live cumulative plot for the active auto-schedule

- Tab 5 now shows a **live cumulative time-series plot** below the single-run plot, stitching together *every completed iteration of the currently active schedule* into one continuous trace. It grows every time the daemon finishes another loop, so you can watch the forecast build up in real time.
- Backed by the on-disk auto-scheduler history file, so the plot survives page reloads, fresh browser tabs, and container restarts - not just the current session.
- Restricted to iterations of the **same schedule_root** (e.g. `BraysBayou_schedule_20260524_092313/iter_001…iter_NNN`) so multiple unrelated schedules don't bleed into each other.
- Only renders when ≥ 2 done iterations exist - single-run output stays untouched.

### v2.9.4 - Apply softer primary colour to Streamlit's own theme

- v2.9.2 only changed the CSS variable, but Streamlit's `config.toml` still pinned `primaryColor = "#1d4ed8"` (the old dark blue), so built-in widgets (`↻ Update schedule with current settings` button, sliders, toggle accents, etc.) ignored our CSS and stayed dark. Updated `.streamlit/config.toml` to `primaryColor = "#3c78af"` so the entire app - both custom-styled and stock-Streamlit elements - now uses the softer water blue.

### v2.9.3 - Hide useless Run-History plot for ad-hoc runs

- The "Run History - All Runs" overlay at the bottom of Tab 5 was designed for the auto-scheduler use case (consecutive runs every N hours). When the user instead runs **ad-hoc historical events from different periods** (e.g. an Oct 2022 storm and an Oct 2025 storm), the plot showed two tiny spike clusters separated by years of empty x-axis - useless.
- HECinBOX now detects this: if any gap between adjacent run windows exceeds **24 h**, the plot is hidden by default and a toggle "*Show run-history overlay anyway*" appears, with a short caption explaining why. For contiguous (scheduler) runs the overlay still shows automatically as before.

### v2.9.2 - Softer primary button colour

- Primary button / tab-highlight / focus-ring colour changed from `#1d4ed8` (blue-700, very dark) to **`#3c78af`** = RGB(60, 120, 175) - a softer, more eye-friendly water blue. Hover variant is `#306496` (~15% darker), soft tint is `rgba(60,120,175,0.10)`.
- Applies to the **Run Simulation** button, the active tab underline, focus rings on inputs, the "Open Results →" button, and every `type="primary"` Streamlit button across the app.

### v2.9.1 - Real fix for expander chevron overlap

- The v2.9.0 fix didn't catch all cases because Streamlit's icon containers are themselves `<span>` elements - so the `.stApp span` rule was still clobbering them. This release drops `span`, `div`, and `button` from the broad font override (they inherit Inter from `body` anyway) and explicitly imports the Material Symbols / Material Icons web fonts so the chevron ligatures render as glyphs regardless of network policy. Also added the full Material Symbols ligature property block (`font-feature-settings: 'liga'`, `text-transform: none`, etc.) so partial CSS resets can't break ligature resolution.

### v2.9.0 - Expander / button icon overlap fix

- **Fixed overlapping icon-name text on expander buttons** (`Advanced / Auto-detected`, `Raw boundary condition data (CSV)`, `Configure SMTP`, *Live pipeline log*, …). The cause was a CSS wildcard rule (`.stApp *`) with `!important` that forced Inter onto every element - including the Material Symbols spans Streamlit uses for expander chevrons. With the icon font replaced by Inter, the ligature names (`arrow_right`, `arrow_drop_down`) rendered as literal text, and both open/closed states stacked on top of each other.
- The Inter font is now applied to text-bearing containers (`p`, `span`, `div`, `label`, `button`, headings, form inputs) explicitly, and Material Symbols / Icons is restored on any element whose class hints at icon usage (`[class*="material-symbols"]`, `.material-icons`, etc.).

### v2.8.4 - Live progress *during* engine initialization

- **Bar now moves through the long pre-simulation phase**, not just timestepping. Before this release, the engine band only updated once `SIMTIME=` lines appeared, which is *after* HEC-RAS finishes reading the mesh, building topology, and solving initial conditions - that pre-phase can take 1-5 minutes for a 50k-cell mesh, during which the bar looked frozen.
- **Init-phase signposts** - `LABEL=` lines emitted by RasUnsteady are now parsed and mapped to phase-aware sub-bands inside 20→23%: `Reading model files → Loading 2D mesh → Initializing 2D mesh topology → Computing initial conditions (slow for large meshes) → Running warm-up timesteps → first SIMTIME hits → real timestepping`.
- **Heartbeat timer** - even when HEC-RAS is silent for minutes during the initial-conditions solve, the elapsed counter under the bar refreshes every 5 s so you can see the run is alive.
- **Phase-aware status text** - the line under the bar now names the current phase (`Computing initial conditions (steady-state - slow for large meshes)`) instead of a generic engine label.
- Implementation: replaced the blocking `for line in proc.stdout` loop with a reader thread + `queue.Queue` polled every 2 s, so we can fire heartbeats independently of HEC-RAS output cadence.

### v2.8.3 - Live engine progress (no more frozen 55% bar)

- **Progress bar now moves continuously during the HEC-RAS run.** Previously the bar shot to 55% in seconds and then sat there for minutes/hours while the engine was actually doing 90% of the work. The engine's own `SIMTIME=<hours>` markers (HEC-RAS 7.0+) are now parsed in real time and mapped to a wide 20→90 band, so each completed timestep visibly nudges the bar.
- **Status line shows simulated date + elapsed wall-clock time.** Example: `HEC-RAS engine - 04 Apr 2025 17:30  (35% of window · elapsed 4m21s)`.
- **Rebalanced step weights:** fetch + DSS write + patching compress to 0-20%; the engine takes 20-90%; extraction / artifact saving runs 91-99%.
- Old `ABSDATE` / `ABSTIME` parser kept as a fallback for older HEC-RAS builds.

### v2.8.2 - Correct interval for DSS-driven boundaries

- For DSS-driven BCs the Detected Model panel now reads the interval from the **DSS pathname's E-part** (the 5th `/`-segment of `/A/B/C/D/E/F/`), which is the authoritative value. The `.u01`'s `Interval=` line is often stale leftover text. *Example: Brays Bayou UpstreamBC's DSS path ends in `…/15Minute/USGS/` and now correctly displays `Interval: 15Minute`.*

### v2.8.1 - Boundary-condition preview on Tab 1

- **Detected Model panel** now shows a per-BC table with **type** (Flow Hydrograph / Stage Hydrograph / Normal Depth / Rating Curve / Gate / Lateral Inflow), **interval**, and a one-line summary of the bundled data - `Constant 628.140 · 1,152 pts`, `Varying 3840.0 - 4550.0 (mean 4121.5) · 1,152 pts`, `Normal depth · slope = 0.00030`, `DSS-driven · /A/B/C/.../1Hour/F/`, etc. Lets the user see the model's boundaries at a glance before touching Tab 3.
- New `parse_bc_details_from_text()` in `model_scanner.py` extracts type / interval / inline-vs-DSS / value statistics from every `Boundary Location=` block in the `.u01`.
- The caption clarifies that *source / station ID / parameter code* live in Tab 3 (HECinBOX-side fetch settings) - the model files only hold the bundled data.

### v2.8.0 - Constant boundary-condition source

- **New `Constant` option in the Tab 3 BC source dropdown.** Many models pin a downstream stage or upstream inflow to a single value rather than a live gage (e.g. Kalamazoo's `downstream_elevation = 628.14 ft NGVD29`). Picking *Constant* synthesizes a flat time series at that value for the full simulation window and writes it into DSS like any other fetched BC.
- **Model-native units, no datum conversion.** When the user picks *Constant*, a prominent in-tab info box explains that the value is written verbatim - for a stage BC, that means whatever vertical datum the `.u01` uses (NGVD29 / NAVD88 / ft above MHHW / metres); for a flow BC, cfs or m³/s. The user enters the number exactly as it appears in the model file. An optional *Unit / datum label* free-text field records what the user intended, purely for their own records when the run is re-opened later.
- **`summary.txt` provenance** now emits `CONSTANT  628.14 ft NGVD29` rows alongside the existing `FRESH` / `FALLBACK` lines.
- **Tab 4 warning** no longer fires when every active BC is a constant - a constant counts as "configured" because the engine will run on the user's chosen value, not a stale calibration default.

### v2.7.3 - Open any HEC-RAS result folder, with model auto-identification

- **"Open Previous Results" now works on raw HEC-RAS folders too.** Previously the feature required a `wse_extract.npz` (HECinBOX-only) and never identified the originating model. Now the browser classifies the selected folder into three kinds:
  - *HECinBOX run* - `wse_extract.npz` present → opens immediately, restores the source `model_dir` so Tab 1's *Detected Model* panel auto-populates.
  - *Raw HEC-RAS folder* - `.prj` + computed `.pXX.hdf` present → HECinBOX extracts `wse_extract.npz` on the fly into `/host_out/external_extracts/<model>__<plan>/` (writable side, because the `/host` mount is read-only inside the container), then opens it like any HECinBOX run.
  - *HEC-RAS model folder without results* → tells the user to compute a plan first.
- **`run_meta.json`** is now written into every output folder (model name, plan / geometry / unsteady suffix, flow area, plan HDF, window). Tab 5 reads it to show a one-line *project · plan · area · model* header above the Output folder path.
- **New `src/results_extractor.py`** factors the result-HDF → `wse_extract.npz` logic out of the run pipeline so the UI can call it on demand for external HEC-RAS folders.

### v2.7.1 - BC-not-configured warning + summary.txt provenance

- **Red warning banner on Tab 4** when no boundary condition has `Source = USGS` / `NOAA` + a Station ID. Previously the engine would silently fall back to the model-bundled calibration data and produce identical results for every date range - easy to miss until a multi-year comparison exposed it. Now the user is told *before* clicking Run.
- **`summary.txt` now ends with a `Boundary conditions` block** listing every configured BC and whether the engine ran on freshly fetched data (`FRESH USGS 08075000 (528 pts)`) or fell back to the model defaults (with the reason: `source=none`, `no station ID`, or `fetch returned no data`). A quick way to spot fall-back without re-reading the run log.

### v2.7.0 - DSS-based boundary-condition injection (major fix)

- **The engine now actually consumes the fetched USGS / NOAA data.** Prior releases patched only the plan HDF's `Boundary Conditions` dataset - the HEC-RAS Linux engine treats that as its cached *input snapshot* and silently re-reads from the DSS file referenced by `DSS File=` / `DSS Path=` inside each `Boundary Location=` block of the `.u01`. Result: every simulation produced byte-identical output (same Max WSE, Mean WSE, Max depth, Max velocity) regardless of which year was requested.
- **New `src/dss_writer.py`** writes a fresh `boundary.dss` at the model root with one regular time-series record per fetched BC (resampled to the BC's existing `Interval=`) and re-points the `.u01` lines to it. All pre-existing `.dss` / `.dsc` / `.dsk` files in the workspace are wiped first so the engine cannot fall back to stale calibration data.
- **New dependency: `hecdss`** (HEC's cross-platform Python bindings for DSS-7).
- Verbose logging at every step - fetched range / min / max / count, BCs found in `.u01`, DSS records written, `.u01` blocks patched - so any remaining mismatch is visible in the run log.

### v2.6.11 - Warmup option removed

- The **Warmup days** input has been removed from Tab 2 entirely. HEC-RAS now runs exactly over the start → end window the user selects. v2.6.10 fixed the BC anchor and dropped the warmup default to 0; this release completes the cleanup by deleting the option so it can no longer be set by accident.

### v2.6.10 - Warmup-induced validation drift fixed

- **Default `warmup_days` now 0** (was 3). For calibrated models with a good stored IC, any non-zero warmup forces the engine to spin up from a cold state and arrives at the analysis start in a different (worse) condition - visible as a 1-day peak shift and inflated tails in the validation plot. The Tab 2 tooltip now explains when to set it >0.
- **Fixed hardcoded `"2400"` in BC `Start Date` HDF attribute** - was `sim_start.strftime("%d%b%Y 2400")` (RAS shorthand for end-of-day = next-day 00:00), now `"%d%b%Y %H%M"` matching the `End Date` format so the BC anchor and the engine clock agree exactly.
- **Validation metrics now computed on raw paired series**, with the datum shift applied *only* to the plotted model curve. Previously both series were de-biased to `iloc[0]=0`, which masked real model bias and was highly sensitive to noise at the first overlapping timestamp.

### v2.6.9 - Cleaner About dialog

- Removed the version-by-version changelog from the **About** menu - it now shows a clear description of what HECinBOX does, its scope (2D unsteady models only, model must be pre-calibrated), and a contact line for feedback and bug reports.

### v2.6.8 - Per-rule Alert Fire Mode

- Alert rules now have a **Fire mode**: *Once per below→above transition* (default, anti-spam) or *Every cycle while condition is true* (heartbeat). Switchable per-rule inline - no need to recreate existing rules.

### v2.6.7 - Artifact Maps Fit the Watershed

- Saved PNG maps now auto-size to the data's aspect ratio (no more wasted whitespace next to a tall colour-bar). Slim proportional colour-bar hugs the map. Mesh polygons cached & reused across all three maps for speed.

### v2.6.6 - Filled-Cell Artifact Maps + Velocity

- PNG artifact maps (`peak_wse_map.png`, `peak_depth_map.png`) now render as filled mesh cells via PolyCollection - same look as Tab 5 - instead of point scatter. New `peak_velocity_map.png` joins the bundle.

### v2.6.5 - Self-Describing Run Folders

- Every completed run now also writes `summary.txt`, `peak_wse_map.png`, `peak_depth_map.png` (when depth available), and a `peak_cell_<id>_timeseries.csv` directly into the output folder - so users can browse results in Finder / Explorer without launching Streamlit.

### v2.6.4 - Nested Per-Schedule Output Folder

- Auto-scheduler now writes every iteration of a session into a single parent folder `<base>_schedule_<UTC-timestamp>/iter_<NNN>_<UTC-timestamp>/` instead of flooding `/host_out` with dozens of siblings. Cloud uploads mirror the structure. Single-shot runs keep the flat layout unchanged.

### v2.6.1 - Crisp Hi-DPI Logo

- Header logo is now embedded as a base64 data URI inside an inline `<img>` tag with explicit CSS width, so the browser gets the full 1254×1254 source to downscale - crisp on Retina displays instead of a pre-downsampled 210 px raster.

### v2.6.0 - Rebranded to HECinBOX · Logo-Matched Palette

- Product renamed: **AutoHEC-RAS → HECinBOX**. New palette built around the brand - white page background like the logo, deep navy text (`#0f1e3a`, the "HEC" wordmark colour), water-blue primary (`#1d4ed8`, the "inBOX" / flowing-water colour). Docker image renamed to `ehsankahrizi1991/hecinbox` (previous `autohecras` tags remain on Docker Hub but receive no further updates).

### v2.5.6 - Warmer Stone Palette

- Shifted from cool slate to warm stone (soft beige undertone). Background is slightly darker / more substantial. Primary accent deepened from sky-700 to sky-800. Shadows warm-tinted.

### v2.5.5 - Professional Appearance Polish

- Refined typography (Inter + JetBrains Mono), slate/sky palette, underline-indicator tabs, subtle card shadows, primary-button hover lift, modern input focus rings. Whole UI feels more "engineering tool" than "demo dashboard."

### v2.5.4 - Light-Only, Sidebar Removed, Reset Back in Header

- Dark + System theme options dropped - HECinBOX is now light-only. The sidebar from v2.5.3 is removed; **⟲ Reset** is back in the header next to the logo. `.streamlit/config.toml` switched to `base = "light"` so Streamlit's runtime widgets render natively without CSS contortions.

### v2.5.3 - Theme & Reset in Collapsible Sidebar

- **🎨 Appearance** picker and **⟲ Reset everything** moved into a Streamlit sidebar that slides out from the left. Starts collapsed, click the chevron to open. Header is now just title + logo.

### v2.5.2 - Theme Popover · Light-Mode Contrast · Readable Plot Text

- **🎨 popover** replaces the header theme dropdown - same options (System / Light / Dark), tiny footprint.
- **Light mode now legible** - added CSS overrides for every Streamlit widget (buttons, inputs, selects, code blocks, etc.) so they switch to the light palette instead of staying dark.
- **Plot text always readable** - Plotly figures force `paper_bgcolor = plot_bgcolor = "white"` with explicit dark text, so axis labels and titles stay crisp on the dark app background.

### v2.5.1 - Tighter Threads-Row Layout

- Available cores + Threads dropdown are now tucked together on the left; the *Enable alert agent* toggle pins to the right edge instead of floating in the middle.

### v2.5.0 - Alert Agent Opt-In (Scheduled-Only)

- **🔔 Enable alert agent** toggle on the Run tab's *Available cores / Threads* row, visible only when *Auto-run scheduling* is on. Single-shot runs no longer send alerts by accident; scheduled runs only send alerts when this toggle is explicitly enabled.
- The setting flows into the run's `settings.yml → agent.enabled` so the auto-scheduler daemon respects the choice for every loop iteration.

### v2.4.6 - Smooth Progress + Second-Loop Progress UI Restored

- **1 Hz polling** for the active-run fragment so the progress bar and log advance once per second instead of jumping every three.
- **`_active_job_dir()` is now disk-first** - fixes a stale-session-state bug where the second loop iteration showed *running* with no progress bar / log, because the UI was caching the previous run's path *and* deleting the daemon's new `.active_job` on every poll.

### v2.4.5 - Run-Start Polish: No Gap + Fixed-Height Log

- **No more "running text with no log" gap at run start** - the daemon spawns the subprocess and writes `.active_job` *before* flipping `state=running` in the schedule file, so Streamlit sees a consistent post-start state on the next 1-second poll. The progress bar + log appear at the same moment the schedule shows *running*.
- **Fixed-height, auto-scrolling pipeline log** - the *Live pipeline log* expander is now a 380 px scrollable iframe. The page no longer grows taller as lines arrive; JS keeps the view pinned to the latest entry, and you can scroll up freely for history.

### v2.4.4 - Run Identity + Duration in Results Summary

- **Top row of the Results Summary** now shows the run's position in the auto-scheduler's history (e.g. `#5 / 12`), the wall-clock compute duration, and the run-folder name on hover for the full path. Single-shot runs are labeled accordingly.

### v2.4.3 - Pipeline Log Reads Full run.log

- **Reads the whole `run.log`** (capped at 2 MiB tail) instead of the 80-line tail provided by the status reader - so the non-engine pipeline messages survive even after the engine has been running for hours.

### v2.4.2 - Whole-Pipeline Live Log

- **Engine per-timestep spam filtered out** - `SIMTIME / ABSDATE / ABSTIME / ITER2D` lines repeat 4× per timestep and used to flood out the early setup messages (config load, USGS fetch, model patch). The progress bar already tracks engine progress, so they're now suppressed.
- **Log buffer bumped 120 → 300 lines** so every stage from start to finish stays visible for the whole run.

### v2.4.1 - In-UI SMTP Configuration

- **Configure SMTP from the web UI** - Tab 7 now has a *🔧 Configure SMTP* form (host, port, user, password, from, default to). Saved to `/app/.agent_smtp.json` so it survives container restarts. No more `docker run` env-var dance required.
- **Env-var fallback preserved** - disk config wins when present; otherwise the existing `SMTP_*` env vars take effect.
- **Clear-credentials button** wipes the on-disk SMTP config in one click.

### v2.4.0 - Agent Tab: Real-time Decision Support

- **New Tab 7 · Agent** - define rules that watch every completed run and email a decision-maker the instant a condition flips. Built for unattended-server real-time flood forecasting.
- **Three rule types** out of the box:
  - **Cell threshold** - a specific mesh cell's peak depth / WSE / velocity vs. operator + threshold (e.g. cell 1234 depth `>` 0.05 ft = "wetted").
  - **Domain peak** - the worst cell anywhere in the model crosses a threshold.
  - **Wetted area** - total wet-cell count crosses a threshold (early-warning for spreading).
- **Once-per-transition firing** - re-arms when the condition goes back below, so the same flood event doesn't spam you every loop iteration.
- **Email via SMTP** - works with any provider; credentials come from env vars (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`, `SMTP_TO`). Built-in *Send test email* button.
- **Persistent rule set + history** - `/app/.agent_state.json` and `/app/.agent_history.json` survive restarts.

### v2.3.10 - Tab 5 Auto-Refresh (No Browser Refresh Needed)

- **Watcher fragment polls history + schedule + active-job mtimes** every second, so Tab 5 picks up every finished loop iteration without a manual refresh.
- **Always-on watcher** - runs even when the schedule is idle, so external state changes are detected on any tab.
- **Daemon write-order fix** - `.active_job` is cleared only after history and schedule are flushed, eliminating the race where Streamlit reran on an empty history file.

### v2.3.9 - Workflow Audit & Stability Fixes

- **Edit settings during runs** - Tabs 2 & 3 are no longer locked while a job is in flight. Tweak Sim Window / Boundary Conditions mid-loop, then click *↻ Update schedule with current settings* to push the new template to the daemon.
- **Overlap guard** - clicking Run while a single-shot is already running now errors clearly instead of launching a parallel job.
- **Schedule-toggle guard** - if the daemon is armed but *Auto-run scheduling* is off in Tab 2, the Update button explains how to re-arm or stop, rather than starting a single-shot alongside the loop.
- **Colour-bar title** moved to top-with-line-break so it stays readable on narrow viewports.

### v2.3.8 - Unified Run Tab Stays Visible

- **Schedule controls always on screen** - timer, 🛑 Stop schedule, schedule history and Run / Update button stay visible even while a run is executing.
- **Progress + log appear below as a new section** - the live progress bar and pipeline log are an additive panel at the bottom, not a replacement view, so you never lose access to the scheduler controls mid-run.

### v2.3.7 - Inundation Map Fills the Canvas

- **Auto-fit zoom** - Mapbox zoom is computed from the data bbox so the flood mesh always fills the viewport instead of being a tiny dot in a city-wide view.
- **Inset colour-bar** - the WSE / Depth / Velocity colour-bar now overlays the map's right edge so the basemap uses the full container width.
- **Bigger default canvas** - 680 px tall for a more presentation-grade view.

### v2.3.6 - Full-Pipeline Live Log

- **Live pipeline log shows every stage** - config load, USGS / NOAA fetches, model copy, BC injection, engine compute, WSE extraction, cloud upload. Only redundant `PROGRESS|` markers and third-party HTTP debug noise are suppressed. The progress bar already steps through the same stages 2 % → 100 % - now the log matches.

### v2.3.5 - Disk-Backed Results Auto-Refresh

- **Tab 5 always shows the latest finished run** - reads the auto-scheduler's history file directly, so the Results tab updates every loop iteration regardless of session state. Reload the browser, open the page for the first time hours into a loop, restart the container - the most recent successful run is always there.

### v2.3.4 - Unified Run Tab · Engine-Only Live Log

- **Schedule timer + run progress on one screen** - the countdown and Stop-schedule button stay at the top; the live progress bar, engine log and Stop-run button render right below during a scheduled run.
- **Engine-only live log** - the Live engine log expander shows only the HEC-RAS engine stream and any error / warning lines, no more Python startup chatter or USGS fetch noise.

### v2.3.3 - Live Progress During Scheduled Runs · Auto-refreshing Results

- **Progress UI for scheduled runs** - Tab 4 now switches into the live progress view (progress bar, engine log tail, ⏹ Stop run button) whenever the auto-scheduler launches a run, and switches back to the countdown when it finishes.
- **Results tab auto-refreshes every loop** - each completed scheduled run is promoted to Tab 5 automatically. Open it once and it stays current as long as the loop runs.

### v2.3.2 - Server-Grade Auto-Schedule + Persistent Settings

- **Independent scheduler daemon** - auto-scheduling now runs as a separate process inside the container, not in the Streamlit session. Close the tab, refresh, restart Streamlit - the schedule keeps running. Built for unattended servers running for months.
- **Failed runs no longer stop the loop** - the daemon re-arms for the next interval no matter what. Per-run history is recorded to `/app/.autoschedule.log` and `/app/.autoschedule.history.json`. Configurable per-run timeout (`AUTOSCHEDULE_MAX_RUN_SECONDS`, default 6 h).
- **🛑 Stop schedule** - explicit button to stop the loop without nuking the rest of your session.
- **Settings persist across sessions** - threads, dates, BC config, theme, model source, cloud URIs and folder paths are saved to `/app/.user_prefs.json` and restored on the next render. Reopen the page, restart the container - your settings are still there.

### v2.3.1 - Bigger GIF, Resilient Auto-Schedule, Big Timer

- **Edge-to-edge flood GIF** - title and colour-bar are now placed *inside* the map (semi-transparent backings), so the animation fills the whole canvas. The GIF is written with PIL `loop=0` so it animates correctly in browsers, PowerPoint, Slack, etc. - not just in macOS Preview.
- **Resilient auto-schedule** - a failed/stopped/aborted run no longer breaks the real-time loop. The schedule re-arms automatically for the next interval, and a warning banner shows how many runs failed since the last reset.
- **Big centred countdown timer** - the auto-schedule clock is now a large, second-by-second ticker showing time to next run, interval, completed and failed counts at a glance - perfect for an unattended server display.

### v2.3.0 - Cloud Storage (S3)

- **Load models from the cloud** - new *Model source* selector in Tab 1: browse a mounted folder, or paste an S3 URI to load a HEC-RAS model straight from a bucket.
- **Save results to the cloud** - set a results S3 URI and the finished run folder is uploaded automatically when the simulation completes. The upload runs inside the detached pipeline, so it survives a closed browser.
- **Open previous results from the cloud** - point *Open Previous Results* at an S3 run folder to view its map, GIF, and summary.
- **S3-compatible** - works with Amazon S3, Cloudflare R2, MinIO, Wasabi, Backblaze B2 and more via an optional endpoint URL.
- **Built for cloud servers** - credentials come from the environment or the server's IAM role; nothing sensitive is typed into the web UI.

### v2.2.0 - Results Browser, GIF Animation, Themes & Stability

- **Open previous results** - browse to any finished run under `/host` or `/host_out` from the bottom of Tab 1 and open its map, GIF, time series, and summary without re-running.
- **Flood-event GIF** - the animation is now a looping GIF that mirrors your inundation-map settings (basemap, filled cells vs points, whole-domain vs wetted). Plays in the browser, downloads as one file.
- **Light / Dark / System theme switch** - new appearance selector in the header; System follows your OS.
- **Flexible auto-scheduling** - re-run interval now supports minutes or hours (every 10 min, 30 min, 6 h, …).
- **Fixed wet-cells detection** - uses water depth (WSE − terrain) instead of WSE variance, correct for steady-state and large-river models.
- **Faster, more stable UI** - the Results tab does no heavy work until a run is loaded, so browsing and tab switching stay fast even with 50k+ cell models; float32 arrays and graceful error handling prevent crashes.

### v2.1.0 - Detached Runs & Parallel Threads

- **Detached job runner** - simulations run as independent OS processes; closing the browser, sleeping the screen, or losing the websocket will not stop the run. Reopen the tab any time to see progress.
- **Parallel computation** - CPU thread selector in the Run tab (OMP/MKL). The app auto-detects available cores via `--cpus`.
- **Results tab stays active** - browse previous results while a new simulation is running.
- **Session recovery** - active job state persisted to disk; reopening the browser reconnects to the running simulation.
- **Tabs locked during runs** - other tabs show a busy banner to prevent conflicting changes.

### v2.0.0 - Flood Animation & Summary

- **Flood propagation animation** - timelapse showing flood spreading across the domain. Play/Pause, frame decimation, speed control, downloadable interactive HTML.
- **Improved simulation summary** - compact layout with Min/Max over wetted cells and cell index.

### v1.5.0 - Interactive Inundation Map

- RAS-Mapper-style filled-cell map with mesh polygons and cell boundaries
- Variable selector: WSE, Velocity, Water Depth, Terrain (DEM) with color ramps
- Peak over simulation or step through timesteps with a slider
- Whole domain vs. wetted-only toggle
- Multiple basemaps: Light, Streets, Dark, Terrain (OpenTopoMap), Satellite (ESRI)
- Live engine progress bar, auto-scheduling, interactive Plotly charts

### v1.4.0 - Initial Release

- 6-tab Streamlit UI, USGS/NOAA boundary-condition fetch, HEC-RAS Linux headless engine, automated validation against observed gages, model scanner.

---

## Docker Options

| Flag | Description |
|------|-------------|
| `--cpus=N` | Number of CPU cores for the container (e.g. `4`, `8`). Shown in the Run tab. |
| `-p 8501:8501` | Map Streamlit port to localhost |
| `-v ~/:/host:ro` | Mount your home directory read-only for model file browsing |
| `-v ~/HEC-RAS-Outputs:/host_out` | Writable output folder for results |

## Requirements

- Docker Desktop installed
- A HEC-RAS 2D unsteady model (`.prj`, `.p01`, `.g01`, `.u01`, `.hdf` files)
- No GPU required - HEC-RAS runs on CPU only
- Outbound internet for USGS/NOAA API calls and map tile basemaps
- Apple Silicon Macs (M1/M2/M3/M4) run via emulation (slower but works) - the `--platform` flag handles this automatically

## Stopping the Container

```bash
docker ps
docker stop <CONTAINER_ID>
```

## Developer

**Ehsan Kahrizi** - [Coastal Hydrology Lab](https://sites.ua.edu/hmoftakhari/), University of Alabama - ekahrizi@crimson.ua.edu
