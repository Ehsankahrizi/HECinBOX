"""HECinBOX - Streamlit web UI (6-tab layout).

Tabs
----
1. Model Folder        - browse & select the HEC-RAS project directory
2. Simulation Window   - date range, real-time toggle
3. Boundary Conditions - per-BC data-source configuration
4. Run                 - launch the HEC-RAS engine, progress bar
5. Results             - explore simulation outputs, WSE extraction
6. Validation          - fetch observed gage data, compare, plots & metrics
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import gc
import json
import os
import re
import subprocess
import sys
import time
import uuid
import warnings
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

# Expected, harmless numpy warnings: a cell/timestep with no valid data
# yields an all-NaN reduction (handled downstream) - silence the noise.
warnings.filterwarnings("ignore", message="All-NaN slice encountered")
warnings.filterwarnings("ignore", message="Mean of empty slice")

import html as _html
import streamlit as st
import streamlit.components.v1 as _st_components
import yaml

from model_scanner import scan_model
from units import is_si, unit_labels


def _unit_labels_for_run(run_dir) -> dict:
    """Unit labels for a saved run folder, read from its own metadata.

    Looks at ``wse_extract.npz`` (``unit_system`` array) first, then
    ``run_meta.json``; falls back to SI when neither records it.  Keeps
    re-opened / detached / cloud-loaded runs self-describing rather than
    assuming the currently-scanned model's units.
    """
    from pathlib import Path as _P
    run_dir = _P(run_dir)
    us = None
    try:
        import numpy as _np
        _npz = run_dir / "wse_extract.npz"
        if _npz.exists():
            _d = _np.load(_npz, allow_pickle=True)
            if "unit_system" in _d.files:
                us = str(_d["unit_system"][0])
    except Exception:
        us = None
    if not us:
        try:
            _meta = json.loads((run_dir / "run_meta.json").read_text())
            us = _meta.get("unit_system")
        except Exception:
            us = None
    return unit_labels(is_si(us))


def _window_class() -> "str | None":
    """Classify the Tab 2 simulation window for source-compatibility hints.

    Drives the Tab 3 warnings that keep data sources consistent with the
    time window:

    * ``'past'``   - the window ends before now, so forward forecasts
      (NWM / STOFS / HRRR) cannot produce data for it.
    * ``'future'`` - real-time mode is on, or the window reaches now /
      ahead, so observed hindcasts (USGS / NOAA / AORC) cannot cover the
      forward part.
    * ``None``     - undetermined (no window selected yet).
    """
    import pandas as pd
    if st.session_state.get("rt"):
        # Real-time mode is bidirectional: a Hindcast window looks back
        # (past), a Forecast window looks ahead (future).
        _dir = str(st.session_state.get("rt_direction", ""))
        return "future" if _dir.startswith("Forecast") else "past"
    end = st.session_state.get("_win_end")
    if end is None:
        return None
    try:
        end_ts = pd.Timestamp(end)
        if end_ts.hour == 0 and end_ts.minute == 0:
            end_ts += pd.Timedelta(hours=23, minutes=59)  # date → end of day
        return "past" if end_ts < pd.Timestamp.now() else "future"
    except Exception:
        return None


def _window_source_warnings(bc_cfg, precip_cfg, wc) -> list:
    """Temporal source/window mismatches for the Tab 4 run guard.

    Mirrors the inline Tab 3 warnings but aggregated, so a stale dropdown
    choice can't silently slip into a launched run.
    """
    msgs: list = []
    if wc == "past":
        for b in (bc_cfg or []):
            if str(b.get("source", "")).lower() == "forecast":
                msgs.append(
                    f"BC “{b.get('name', '?')}” uses a Forecast source, but "
                    f"the window is historical (it can't forecast the past)."
                )
        if str((precip_cfg or {}).get("source", "")).lower() == "hrrr":
            msgs.append(
                "Rain on Mesh uses HRRR (forecast), but the window is "
                "historical - use AORC or a DSS upload."
            )
    elif wc == "future":
        for b in (bc_cfg or []):
            if str(b.get("source", "")).lower() in ("usgs", "noaa"):
                msgs.append(
                    f"BC “{b.get('name', '?')}” uses observed "
                    f"{str(b.get('source', '')).upper()}, which has no data "
                    f"for the forecast part of the window."
                )
        if str((precip_cfg or {}).get("source", "")).lower() == "aorc":
            msgs.append(
                "Rain on Mesh uses AORC (hindcast, ~10-day lag), but the "
                "window reaches now/the future - use HRRR."
            )
    return msgs


APP_VERSION = "4.8.8"
RELEASE_DATE = "September 10, 2026"

# Reusable field help (shown as the widget's ? tooltip).
_USGS_STATION_HELP = (
    "The USGS site number of the gage that drives this boundary "
    "(usually 8 digits, e.g. 08075000). Find it on "
    "waterdata.usgs.gov: search the map or your river name, open the "
    "site, and copy the number after 'USGS' in the page title."
)
_USGS_PARAM_HELP = (
    "The USGS parameter (statistic) to pull. Common codes:\n\n"
    "- **00060** - Discharge / streamflow (cubic feet per second) - "
    "use for a **flow** boundary.\n"
    "- **00065** - Gage height / stage (feet) - use for a **stage** "
    "boundary.\n"
    "- **00045** - Precipitation (inches).\n"
    "- **00010** - Water temperature (deg C).\n\n"
    "Pick 00060 for inflow hydrographs and 00065 for water-surface "
    "boundaries. The full list is at help.waterdata.usgs.gov "
    "(Parameter codes)."
)
_NOAA_STATION_HELP = (
    "The NOAA CO-OPS station ID for the tide/water-level gage (7 "
    "digits, e.g. 8770777 for Manchester, TX). Find it at "
    "tidesandcurrents.noaa.gov: locate the station on the map and "
    "copy the ID shown next to its name."
)

# The single most consequential setting in the app, and the one users
# are least equipped to guess - hence the long, example-led tooltip.
# A wrong offset shifts every fetched series by whole hours, and the
# run still completes and still looks plausible (v4.8.0).
_TIME_BASE_HELP = """
**Why HECinBOX has to ask you this**

A HEC-RAS model stores its simulation dates as plain clock readings,
for example `15OCT2022 00:00`. It never records which time zone that
clock belongs to. There is no field for it anywhere in the project
files, so the model itself cannot tell us.

The data services are the opposite. USGS, NOAA CO-OPS, NWM and STOFS
all deliver their records in **UTC**, always.

To put fetched data on your model's clock, HECinBOX has to know what
that clock is. You are the only one who knows, because it was decided
when the model's boundary data was first prepared.

**How to choose**

Ask one question: when the boundary data was put into the model, was
it left on the local clock at the site, or converted to UTC first?

| When the model was built, its boundary data was... | Pick this | A Houston model's `00:00` then means |
| --- | --- | --- |
| left on the **local clock** at the gage, i.e. copied as it appeared on the agency website | Local standard time (UTC-6 for Houston) | midnight **in Houston** |
| **converted to UTC** before it went into HEC-RAS | UTC (offset 0) | midnight **UTC**, which is 18:00 the previous day in Houston |

If you built the model yourself and never thought about time zones,
you almost certainly copied gage data as-is, which is the first row.
If the model came from a national or coastal modeling group, UTC is
common, which is the second row.

**Why it matters**

Getting this wrong does not cause an error. The simulation runs, the
maps look normal, and nothing warns you. What happens instead is that
every fetched series lands a fixed number of hours off, so the
simulated flood peak and tide are shifted sideways by exactly that
many hours.

**If you are not sure**

Pick your best guess and run once, then open **Tab 6 Validation**. If
the simulated and observed curves have the same shape but one is
shifted sideways by a whole number of hours, that number is your
offset error. Correct it here and re-run.

**One more thing**

This is **standard** time only, with no daylight saving. HEC-RAS keeps
one continuous clock, so a model window never jumps an hour in spring
or autumn.
"""


def _version_tuple(s: str) -> tuple:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(s))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


@st.cache_data(ttl=3600, show_spinner=False)
def _latest_hub_version(_current: str):
    """Highest published ``vX.Y.Z`` tag on Docker Hub, or ``None``.

    Powers the "update available" banner.  Cached for an hour and fully
    fail-silent - offline / rate-limited / API change all just return
    ``None`` (no banner).  ``_current`` is only a cache key.
    """
    import requests
    try:
        r = requests.get(
            "https://hub.docker.com/v2/repositories/"
            "ehsankahrizi1991/hecinbox/tags",
            params={"page_size": 100}, timeout=6,
        )
        r.raise_for_status()
        vers = [
            tuple(int(x) for x in m.groups())
            for t in r.json().get("results", [])
            if (m := re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)",
                                  str(t.get("name", ""))))
        ]
        return max(vers) if vers else None
    except Exception:
        return None

ABOUT_MD = f"""
## HECinBOX

**Developer - Ehsan Kahrizi**

[Coastal Hydrology Lab](https://sites.ua.edu/hmoftakhari/), University of Alabama

ekahrizi@crimson.ua.edu

`v{APP_VERSION}` · {RELEASE_DATE}

---

Have an idea, found a bug, or want to request a feature? Reach out at **ekahrizi@crimson.ua.edu**.

For how to use the app, see the **User Manual** tab.
"""


MANUAL_MD = f"""
**HECinBOX** is an automated, agent-based flood-warning framework built on **2D unsteady HEC-RAS** simulations. From this web interface it fetches discharge and stage data from USGS and NOAA, injects them as boundary conditions, applies rain on the mesh, runs the HEC-RAS Linux compute engine, then extracts, visualizes, and validates the results, with no manual file editing.

`v{APP_VERSION}` · {RELEASE_DATE}

This manual walks through the app tab by tab and tells you exactly what to enter at each step. If you follow it top to bottom, you can run a full simulation without prior knowledge of the internals.

---

## What HECinBOX does and does NOT do

HECinBOX **runs** your model. It does **not build or calibrate** it. Your model must already be set up, meshed, and calibrated in HEC-RAS before you load it here.

**Requirements and limitations**

- **2D unsteady models only.** 1D models and steady-flow models are **not supported**.
- The model must already be **built, meshed, and calibrated** in HEC-RAS.
- Live data fetching is **US-only**: USGS and NOAA gages, and the gridded sources (AORC, HRRR, NWM, STOFS) cover **CONUS** (AORC also covers AK and PR). Outside the US, use *Constant*, *Leave unchanged*, or a *DSS upload*.
- **AORC** (observed rainfall) lags real time by about **10 days** and goes back to **1979**. It is for past events only.
- **HRRR** (forecast rainfall) only covers roughly the **next 18 to 48 hours**.
- Forecast sources only reach so far ahead: NWM short range about 18 hours, NWM medium range about 10 days, STOFS about 4 days. The real reach is a bit shorter, because it counts from the last published forecast cycle and not from this minute. Tab 2 works that out and sizes the run for you.

---

## Quick start (the whole workflow in 6 steps)

1. **Tab 1 - Model Folder:** pick the model folder and the output folder.
2. **Tab 2 - Simulation Window:** confirm the time zone, then set the start/end dates (or turn on Real-time mode).
3. **Tab 3 - Boundary Conditions:** for each boundary, choose a data source and enter its gage ID / value. Optionally turn on Rain on Mesh.
4. **Tab 4 - Run:** click **Run simulation** and watch the progress bar.
5. **Tab 5 - Results:** view the depth / water-surface / velocity maps, time series, and animation.
6. **Tab 6 - Validation (optional):** compare the model against a real gage to check accuracy.

---

## Tab-by-tab guide (detailed)

### 1 · Model Folder
Choose the HEC-RAS project to run.
- **This machine:** browse to the folder that contains your `.prj` file (and its `.gNN`, `.pNN`, `.uNN` files). Click into the folder and press **Use this folder**.
- **Cloud (S3):** paste the S3 URI of a model folder if your deployment is cloud-connected.
- **Output folder:** set where results are written. Each run creates a timestamped subfolder there.
- After selecting, the app auto-detects the plan, geometry, unit system, and every boundary condition and shows them for confirmation. *Reset* clears the selection.

### 2 · Simulation Window
Set the time period to simulate. **Read the time zone first.**
- **Model time base (required, no default):** you must pick this yourself before you can run. A HEC-RAS model stores its dates as a bare clock reading and never records the time zone, while USGS, NOAA, NWM and STOFS all publish in UTC, so only you know which clock the model was built on. Pick **Local standard time** (detected from the model location, no daylight saving, e.g. UTC-6 for Texas) if the model's boundary data was left on the gage's local clock, or **UTC** if it was converted to UTC before it went into HEC-RAS. Every date you enter here and every fetched series then share that one clock. Getting it wrong does not raise an error: the run completes and only the validation hydrograph looks shifted sideways by whole hours.
- **Manual dates (Real-time OFF):** type a **Start date** and **End date** in the model's local standard time. The greyed text shows the model's own built-in window as a reference.
- **Real-time mode (ON):** the app pulls the most recent data automatically. Choose a direction:
  - **Hindcast:** the past N days. You pick N. Use it with USGS / NOAA observations.
  - **Forecast:** from now forward. You do **not** pick the length. The app works it out and tells you what it chose. See *How long is a forecast run?* just below.

**How long is a forecast run?**

Every forecast source only reaches so far ahead. NWM medium range goes about 10 days. STOFS goes about 4. The app asks each source how far its newest data really reaches, then uses the **shortest** answer. If one boundary runs dry after 4 days, the run is 4 days.

That is on purpose. With a longer window, the shorter source would run out partway and the engine would just hold its last value to the end. Nothing would look wrong. But the results after that point would not be real.

The green message above the dates names the boundary that set the length, and lists how far each one reaches. Those numbers shrink as a forecast cycle gets older, and jump back up when the next one is published, so the length can differ from one day to the next.
- **Auto-run scheduling (optional):** re-run on a fixed interval for operational monitoring. The run keeps going even if you close the browser.

### 3 · Boundary Conditions
This is where you connect each model boundary to real data.
- **Boundary locations map:** at the top, a map shows your model and its boundaries. You can pan it and zoom it.
  - The **blue shaded area** is the 2D domain. That is what your model covers.
  - A **numbered red dot** marks each boundary. The numbers match the boundary blocks below, so with two or more upstream rivers you can tell which is which.
  - When you assign a source to a boundary, a **blue dot with the same number** appears where that source is, with a line joining the two. Hover either dot to see the name and how far apart they are. If the source sits right on the boundary, which an NWM reach normally does, the blue dot shows up inside the red one.
  - **Basemap** switches the background between Topographic, Satellite, Streets and Light gray.
  - **Suggest sources within (km)** shows you what data is available nearby. See just below.

**Finding a gage without leaving the app**

Type a distance into **Suggest sources within (km)**, above the map. The app draws a dashed ring at that distance around every boundary, and marks each active USGS gage and NOAA tide station inside the rings. A table under the map lists them: the ID, the name, what the gage measures, and how far it is.

Nothing gets filled in for you. Read an ID off the table and type it into the field you want. That is the point of it: you can see what is out there without going to the USGS or NOAA websites.

Two things to check before you use one:

- **Is it on the same river?** A gage can be close in a straight line and still sit on a different creek. The blue shading helps here. A gage outside the shading is outside your model.
- **Does it measure what you need?** The table tells you. A gage that only records gage height cannot drive a flow boundary.
- For **each boundary** (numbered to match the map), pick a **Data source**:
  - **Leave unchanged:** keep the model's built-in values for that boundary. Only selectable while your **Tab 2 window matches the model's native window** - the built-in data carries its original dates and stores no gauge IDs, so it cannot supply real data for a re-timed window. Change the Tab 2 dates and the entry shows as *(unavailable - needs native window)* until you set them back. The same rule applies to the **Rain on Mesh** mode when the model's own precipitation is gridded/point (a native *constant* rate stays selectable - it is date-free and re-timed automatically).
  - **USGS:** drive it from a USGS river gage. Enter:
    - **USGS station ID:** the site number of the gage (usually 8 digits, e.g. 08075000). Find it at waterdata.usgs.gov by searching your river or the map.
    - **Parameter code:** what to pull. **00060** = discharge / streamflow (use for a **flow** boundary); **00065** = gage height / stage (use for a **stage** boundary). The app pre-fills the right one for the detected boundary type. Hover the field's ? icon for more codes.
  - **NOAA:** drive a coastal stage / tide boundary from a NOAA tide gage. Enter the **NOAA station ID** (7 digits, e.g. 8770777), the **Datum**, and **Units**.
  - **Constant:** hold the boundary at a single value for the whole run. Enter the value **in the model's own units and datum** (the app does not convert it).
  - **Forecast (NWM v.3):** drive a flow (or rating-curve stage) boundary from the National Water Model forecast. Enter the reach **COMID** (and pick a horizon).
  - **Forecast (STOFS):** drive a coastal stage boundary from the STOFS total-water-level forecast. Enter two things:
    - the **NOAA tide station** the forecast should be read at, and
    - the **Vertical datum** your model's terrain uses.

    STOFS reports water level above mean sea level (MSL). Most US models are built on NAVD88 instead, so NAVD88 is the default here, and the app shifts the series onto it using the station's own published datums. The exact shift is printed next to the field. At Manchester, TX it is about 0.31 m, close to a foot, so it is not a detail you can skip. Pick MSL only if your model really is on MSL.
- **Match the source to your window.** Observations (USGS / NOAA / AORC) are for past windows; forecast products are for future windows. See the matching table below. A mismatch shows an inline warning.
- **Rain on Mesh (optional):** apply precipitation to every 2D cell. Turn on **Enable rain on mesh**, then choose: *Constant* (one rate), *Gridded - AORC* (observed, past events), *Gridded - HRRR* (forecast), or *Gridded - DSS file* (upload your own gridded DSS).

### 4 · Run
- Click **Run simulation** to start. The **progress bar** and **pipeline log** show each stage: fetching data, injecting boundaries, running the engine, extracting results.
- Runs are **detached**: closing the browser tab does not stop them. Use **Stop** to cancel.
- When it finishes, the app jumps you toward the results.

### 5 · Results
Explore the finished run.
- **Map style:** *Filled cells* (per-cell, GPU, with an optional 3D view), *Smooth* (continuous surface), or *Points* (fast).
- **Variable:** Water Depth, Water Surface Elevation, Velocity, or Terrain.
- **Depth classes** (Smooth style, Water Depth): switch the continuous colour ramp to discrete hazard bands, the way official flood maps present depth. Available for runs computed from v4.7.4 on.
- **Shaded relief** (Smooth style, Water Depth): drape the water over the hillshaded terrain, RAS-Mapper style, so the ground relief reads through the flood. Available for runs computed from v4.7.4 on.
- **Peak** shows the maximum over the whole run; turn it off and drag the **Timestep** slider to scrub through time.
- **Basemap** includes Light, Streets, Dark, Terrain, and Satellite.
- Below the map: a **time series at any cell** and a **flood-propagation animation** you can download as MP4 or GIF.

### 6 · Validation
Check the model against reality.
- Pick a **USGS or NOAA gage** near a modeled location, enter its **station ID** and **parameter code**, and the app compares the modeled stage/flow against the observed record.
- It reports goodness-of-fit metrics (NSE, RMSE, KGE) and an overlay plot. Observed data is automatically shifted to the model's local standard time so the two line up.

### 7 · Agent
- Configure **threshold alerts** (for example, email when stage at a location exceeds a value) for operational monitoring. Used together with auto-run scheduling in Tab 2.

### 8 · Live
- A **read-only dashboard** of the latest scheduled run, shareable with a view-only link (add `?live=1` to the URL).

---

## Which Tab 3 source for which Tab 2 window

A window is **Historical** (ends in the past) or **Forecast** (reaches now or the future). Pick sources that match:

| Tab 2 setting (how you configure the window) | Window type | Tab 3 - Valid Boundary-Condition sources | Tab 3 - Valid Rain-on-Mesh modes | Avoid (app warns) |
|---|---|---|---|---|
| Real-time **OFF**, dates **end in the past** | Historical | Leave unchanged, Constant, USGS, NOAA | Leave unchanged, Constant, AORC (hindcast), DSS upload | Forecast (NWM v.3), Forecast (STOFS), HRRR |
| Real-time **ON** + **Hindcast** | Historical | Leave unchanged, Constant, USGS, NOAA | Leave unchanged, Constant, AORC (hindcast), DSS upload | Forecast (NWM v.3), Forecast (STOFS), HRRR |
| Real-time **ON** + **Forecast** | Forecast | Leave unchanged, Constant, Forecast (NWM v.3), Forecast (STOFS) | Leave unchanged, Constant, HRRR (forecast), DSS upload | USGS, NOAA, AORC |
| Real-time **OFF**, window **reaches now/future** | Forecast | Leave unchanged, Constant, Forecast (NWM v.3), Forecast (STOFS) | Leave unchanged, Constant, HRRR (forecast), DSS upload | USGS, NOAA, AORC |

*Constant* and *DSS upload* are safe for any window (DSS only if its record timestamps overlap the window). *Leave unchanged* is offered **only while the Tab 2 window matches the model's native window**. Anything in the **Avoid** column stays selectable, but the app shows an inline warning and asks you to confirm before a single-shot run.

---

## Field reference (what to enter)

- **USGS station ID:** the gage site number, usually 8 digits (e.g. 08075000). Source: waterdata.usgs.gov.
- **USGS parameter code:** **00060** = discharge / streamflow (cfs), **00065** = gage height / stage (ft), **00045** = precipitation, **00010** = water temperature. Use 00060 for flow boundaries and 00065 for stage boundaries.
- **NOAA station ID:** the CO-OPS tide-gage ID, 7 digits (e.g. 8770777). Source: tidesandcurrents.noaa.gov.
- **NWM COMID (feature_id):** the NHDPlus number of the river reach your boundary sits on. Two ways to find it:
    - Open **water.noaa.gov/map** in a browser, zoom in, and click the blue river line at your boundary. The reach ID it shows is the number you need.
    - Or paste this into a browser, with your own longitude and latitude in the brackets: `https://api.water.usgs.gov/nldi/linked-data/comid/position?coords=POINT(-95.4245 29.6969)&f=json` . In the answer, the number after `comid` is the one to enter.

    Easiest of all: set a radius in **Suggest sources within (km)** on the Tab 3 map, which shows the gages near each boundary directly.
- **Datum (NOAA):** the vertical reference the tide data is reported against (NAVD, MLLW, MSL, STND). Match your model's datum.
- **Vertical datum (STOFS):** the datum your model's terrain is on, NAVD88 or MSL. STOFS always publishes on MSL, and the app converts the series to what you pick here.
- **Model time base:** the clock your model window is on, as a UTC offset (Tab 2). Required, with no default. All dates and all fetched data use it.

---

## Troubleshooting and tips

- **Run looks stuck at "Fetching gridded rain on mesh (HRRR)".** HRRR decodes one forecast hour at a time and is slow; the progress bar shows *reading hour X/Y*. It is working, not hung. For a **past** event use **AORC**, which is much faster.
- **A source warning appears in Tab 3.** Your data source does not match the Tab 2 window; see the table above.
- **Data seems shifted in time.** Almost always the **Model time base** in Tab 2. Open Tab 6 Validation: if simulated and observed have the same shape but one is shifted sideways by a whole number of hours, that number is your offset error. Switch between local standard time and UTC and re-run.
- **DSS upload finds no grids / no rain.** The file must hold **gridded** precipitation records whose **times overlap** your Tab 2 window, over your model's footprint.
- **"Leave unchanged" shows as *(unavailable - needs native window)*.** Your Tab 2 window differs from the model's native window, so the model's built-in data (which carries its original dates and no gauge IDs) cannot represent the run. Either assign a real source (USGS / NOAA / Constant / Forecast) per boundary, or set the Tab 2 dates back to the native window shown above the date pickers. Selecting the unavailable entry just bounces back with a warning.
- **All boundaries "Leave unchanged".** Requires the Tab 2 window to match the model's native dates; the model then runs on its own built-in data untouched.
- **No boundaries detected.** The model may not be a 2D unsteady model, or the geometry files are missing from the folder.
"""

HOST_ROOT = Path(os.environ.get("HOST_ROOT", "/host"))
OUTPUTS_ROOT = Path(os.environ.get("OUTPUTS_ROOT", "/host_out"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/app/data/outputs"))
SETTINGS_PATH = Path(os.environ.get("SETTINGS_PATH", "/app/settings.yml"))
ASSETS_DIR = Path(os.environ.get("ASSETS_DIR", "/app/assets"))

# ── Demo mode (locked-down hosted demo, e.g. on ECS) ─────────────────
# When DEMO_MODE is on, the UI only lets visitors run the curated sample
# models under DEMO_MODELS_URI (no local browse, no arbitrary S3 URI, no
# uploads, no scheduling), and writes results to DEMO_OUTPUT_URI.  The
# real enforcement is the task's least-privilege IAM role; this just
# keeps the public demo bounded and tidy.
DEMO_MODE = os.environ.get("DEMO_MODE", "").strip().lower() in (
    "1", "true", "yes", "on"
)
DEMO_MODELS_URI = os.environ.get("DEMO_MODELS_URI", "").strip()
DEMO_OUTPUT_URI = os.environ.get("DEMO_OUTPUT_URI", "").strip()

_logo_path = ASSETS_DIR / "logo.png"


@st.cache_data(show_spinner=False)
def _logo_data_uri(path_str: str) -> str:
    """Return a base64 data-URI for the logo so we can serve the full
    intrinsic resolution from inline HTML.

    ``st.image(path, width=200)`` sometimes downsamples server-side,
    which produces a soft 200 px raster that looks blurry on Retina
    displays (effective 400 px).  Embedding the full PNG as a data URI
    inside an ``<img>`` tag lets the browser do the downscale itself
    using all available source pixels - crisp on any pixel density.
    """
    try:
        p = Path(path_str)
        if not p.exists():
            return ""
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except OSError:
        return ""


def _render_logo(path: Path, css_width: str = "300px",
                 align: str = "right",
                 pull_top_px: int = 32,
                 pull_right_px: int = 18) -> None:
    """Render the logo via inline HTML at full source resolution.

    ``pull_top_px`` / ``pull_right_px`` are negative-margin offsets
    that pull the logo up & toward the right edge so it sits tight to
    the header corner instead of being inset by Streamlit's default
    column padding.
    """
    uri = _logo_data_uri(str(path))
    if not uri:
        return
    if align == "right":
        side_margin = (
            f"margin-left:auto;margin-right:-{pull_right_px}px;"
        )
    else:
        side_margin = (
            f"margin-left:-{pull_right_px}px;margin-right:auto;"
        )
    st.markdown(
        f'<img src="{uri}" '
        f'style="width:{css_width};max-width:100%;height:auto;'
        f'display:block;{side_margin}'
        f'margin-top:-{pull_top_px}px;margin-bottom:0;'
        f'image-rendering:auto;" '
        f'alt="HECinBOX logo" />',
        unsafe_allow_html=True,
    )
_favicon_path = ASSETS_DIR / "logo_256.png"

st.set_page_config(
    page_title="HECinBOX",
    page_icon=str(_favicon_path) if _favicon_path.exists() else None,
    layout="wide",
    initial_sidebar_state="collapsed",  # no sidebar content, stay hidden
    menu_items={
        "About": ABOUT_MD,
        "Get Help": None,
        "Report a bug": None,
    },
)

from job_runner import (
    start_job as _start_job,
    read_status as _read_job_status,
    stop_job as _stop_job,
)

_ACTIVE_JOB_FILE = Path("/app/.active_job")

# ── On-disk state shared with the auto_scheduler daemon ──────────────
# These paths MUST resolve identically in app.py and auto_scheduler.py
# (same container, same logic) or the UI and daemon would read/write
# different files.  Prefer the persistent /host_out volume so the
# schedule + run history survive container recreation (v3.1.2).
def _persist_dir() -> Path:
    base = Path("/host_out")
    if base.is_dir():
        d = base / ".hecinbox"
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            pass
    return Path("/app")


def _state_file(env_key: str, name: str, legacy: str) -> Path:
    p = Path(os.environ.get(env_key, str(_persist_dir() / name)))
    legacy_p = Path(legacy)
    if p != legacy_p and not p.exists() and legacy_p.exists():
        try:
            p.write_bytes(legacy_p.read_bytes())
        except OSError:
            pass
    return p


SCHEDULE_FILE = _state_file(
    "AUTOSCHEDULE_FILE", "autoschedule.json", "/app/.autoschedule.json"
)
HISTORY_FILE = _state_file(
    "AUTOSCHEDULE_HISTORY",
    "autoschedule.history.json",
    "/app/.autoschedule.history.json",
)
LOG_FILE = Path(
    os.environ.get("AUTOSCHEDULE_LOG", "/app/.autoschedule.log")
)
USER_PREFS_FILE = Path(
    os.environ.get(
        "USER_PREFS_FILE",
        # Default to the writable host-output volume so prefs (model
        # path, BC dropdowns, station IDs, schedule cadence, …) survive
        # container restarts.  Falls back to /app for older deployments
        # that don't mount /host_out.
        "/host_out/.hecinbox_user_prefs.json"
        if Path("/host_out").exists()
        else "/app/.user_prefs.json"
    )
)


# ── Per-session isolation (multi-user demo) ─────────────────────────
# Streamlit runs every browser session in ONE process but keeps each
# session's ``st.session_state`` separate.  The only things that leak
# between concurrent users are the SHARED on-disk files: the settings
# YAML, the active-job pointer, and the saved-prefs JSON.  In DEMO_MODE
# (a public, multi-user deployment with the scheduler disabled) we give
# each session its own copy of those three, so two people can test at
# the same time without clobbering each other.  Outside demo mode the
# original global files are used unchanged (single user + the scheduler
# daemon that coordinates through them).
def _session_id() -> str:
    """Stable per-browser-session id (survives reruns within a session)."""
    sid = st.session_state.get("_sid")
    if not sid:
        sid = uuid.uuid4().hex[:12]
        st.session_state["_sid"] = sid
    return sid


def _session_dir() -> Path:
    d = _persist_dir() / "sessions" / _session_id()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _settings_path() -> Path:
    """Settings YAML the run/validation pipeline reads (env SETTINGS_PATH)."""
    return (_session_dir() / "settings.yml") if DEMO_MODE else SETTINGS_PATH


def _active_job_file() -> Path:
    """Active-job pointer (per session in the demo, global otherwise)."""
    return (_session_dir() / ".active_job") if DEMO_MODE else _ACTIVE_JOB_FILE


def _user_prefs_path() -> Path:
    """Saved-prefs JSON (per session in the demo, global otherwise)."""
    return (_session_dir() / "user_prefs.json") if DEMO_MODE else USER_PREFS_FILE


# ── Demo run queue (cap concurrent simulations on shared resources) ──
# The public demo runs in one small container, so we let at most
# ``DEMO_MAX_CONCURRENT`` heavy 2D simulations run at once and queue the
# rest.  State lives in one flock-guarded JSON file shared by all
# sessions.  Only active in DEMO_MODE; single-user/scheduled deployments
# launch immediately as before.
_DEMO_MAX_CONCURRENT = max(
    1, int(os.environ.get("DEMO_MAX_CONCURRENT", "2") or 2)
)


def _demo_runq_file() -> Path:
    return _persist_dir() / ".demo_runq.json"


@contextlib.contextmanager
def _demo_runq_locked():
    """Yield the queue dict under an exclusive file lock; save on exit."""
    p = _demo_runq_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read()
        try:
            d = json.loads(raw) if raw.strip() else {}
        except Exception:
            d = {}
        d.setdefault("running", {})   # sid -> {output_dir, ts}
        d.setdefault("queue", [])     # ordered list of waiting sids
        d.setdefault("seen", {})      # sid -> last heartbeat ts
        _demo_runq_prune(d)
        yield d
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(d))
        fh.flush()
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def _demo_runq_prune(d: dict) -> None:
    """Free slots for finished runs and drop queued sessions that left."""
    now = time.time()
    for sid, info in list(d["running"].items()):
        od = info.get("output_dir")
        alive = False
        if od:
            try:
                alive = _read_job_status(Path(od)).get("state") == "running"
            except Exception:
                alive = False
        # 20 s grace so a just-reserved slot isn't freed before its
        # status file appears.
        if not alive and (now - info.get("ts", 0)) > 20:
            d["running"].pop(sid, None)
    d["queue"] = [
        s for s in d["queue"]
        if (now - d["seen"].get(s, now)) < 45  # left the tab -> drop
    ]
    for s in list(d["seen"]):
        if s not in d["queue"] and s not in d["running"]:
            d["seen"].pop(s, None)


def _demo_slot_request(sid: str, output_dir) -> tuple[str, int, int]:
    """Try to take a run slot. Returns (decision, position, running_count).

    decision is 'run' (go ahead and launch) or 'queued' (wait).
    """
    with _demo_runq_locked() as d:
        d["seen"][sid] = time.time()
        if sid in d["running"]:
            return "run", 0, len(d["running"])
        free = len(d["running"]) < _DEMO_MAX_CONCURRENT
        first = (not d["queue"]) or d["queue"][0] == sid
        if free and first:
            d["queue"] = [s for s in d["queue"] if s != sid]
            d["running"][sid] = {
                "output_dir": str(output_dir), "ts": time.time(),
            }
            d["seen"].pop(sid, None)
            return "run", 0, len(d["running"])
        if sid not in d["queue"]:
            d["queue"].append(sid)
        return "queued", d["queue"].index(sid) + 1, len(d["running"])


def _demo_slot_release(sid: str) -> None:
    """Give up this session's slot / queue place (e.g. launch failed)."""
    with _demo_runq_locked() as d:
        d["running"].pop(sid, None)
        d["queue"] = [s for s in d["queue"] if s != sid]
        d["seen"].pop(sid, None)


def _launch_run(output_dir: Path, settings_path: Path) -> bool:
    """Start the detached pipeline and mark it the session's active job."""
    try:
        _start_job(
            output_dir=output_dir,
            settings_path=settings_path,
            python_exe=sys.executable,
            main_cwd="/app/src",
        )
    except Exception as e:
        st.error(f"Failed to start pipeline: {e}")
        if DEMO_MODE:
            _demo_slot_release(_session_id())
        return False
    st.session_state["active_job_dir"] = str(output_dir)
    _persist_active_job(str(output_dir))
    return True

# Which session_state keys are saved across browser sessions / restarts.
_PREF_KEY_WHITELIST = frozenset({
    "model_source",
    "browse_path", "out_path", "results_browse_path",
    "cloud_model_uri", "cloud_output_uri", "cloud_results_uri",
    "cloud_model_dir", "cloud_model_src_uri",
    "wu", "rt", "rt_days", "sched", "sched_n", "sched_unit",
    "adv_threads", "adv_p", "adv_g", "adv_u", "adv_fa", "adv_geom",
    "enable_alert_agent", "enable_live_dashboard",
    "res_var", "res_map_mode", "res_map_base", "res_map_scope",
})
# Anything matching one of these prefixes is also persisted - covers
# the dynamic widget keys (BC sources, per-project date inputs, …).
_PREF_KEY_PREFIXES = ("bc_", "sd_", "ed_", "precip_")


def _load_user_prefs() -> None:
    """Restore persisted preferences into session_state on app startup.

    Called *before* widgets render so each widget's ``key`` picks up
    the saved value as its default.
    """
    # If the user just clicked Reset, skip the entire restore.  This
    # tombstone flag is set inside _reset_app() right before st.rerun()
    # and survives one render - long enough for the page to come up in
    # a truly clean state before save-on-render writes a fresh prefs.
    if st.session_state.get("_reset_tombstone"):
        return
    if not _user_prefs_path().exists():
        return
    try:
        data = json.loads(_user_prefs_path().read_text())
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    for k, v in data.items():
        if k in st.session_state:
            continue
        if k in _PREF_KEY_WHITELIST or k.startswith(_PREF_KEY_PREFIXES):
            try:
                st.session_state[k] = v
            except Exception:
                pass


def _save_user_prefs() -> None:
    """Persist whitelisted preferences to disk after each script run.

    Merges over the existing file rather than overwriting it.  This is
    deliberate: Streamlit garbage-collects a widget's ``session_state``
    key on any rerun where the widget isn't drawn (e.g. the
    conditionally-rendered ``enable_alert_agent`` toggle, which only
    appears while auto-scheduling is on).  A plain overwrite would then
    drop that key from disk and the toggle would silently revert to its
    default on the next load.  Merging keeps the last known value for any
    whitelisted key that is momentarily absent from session_state
    (v3.1.1).
    """
    # Start from what's already on disk so transiently-absent keys survive.
    out: dict = {}
    if _user_prefs_path().exists():
        try:
            data = json.loads(_user_prefs_path().read_text())
            if isinstance(data, dict):
                out = data
        except (json.JSONDecodeError, OSError):
            out = {}
    try:
        items = list(st.session_state.items())
    except Exception:
        return
    for k, v in items:
        if not (k in _PREF_KEY_WHITELIST or k.startswith(_PREF_KEY_PREFIXES)):
            continue
        try:
            json.dumps(v)  # only persist JSON-serialisable values
            out[k] = v
        except (TypeError, ValueError):
            continue
    try:
        _user_prefs_path().write_text(json.dumps(out, indent=2, default=str))
    except OSError:
        pass


# ── Auto-scheduler daemon - disk-backed schedule state ────────────────
def _read_schedule_state() -> dict | None:
    """Return the daemon's current schedule state, or None."""
    if not SCHEDULE_FILE.exists():
        return None
    try:
        data = json.loads(SCHEDULE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write_schedule_state(d: dict) -> None:
    try:
        SCHEDULE_FILE.write_text(json.dumps(d, indent=2, default=str))
    except OSError:
        pass


def _autoschedule_active() -> bool:
    """True if the daemon is currently armed."""
    s = _read_schedule_state()
    return bool(s and s.get("enabled"))


def _autoschedule_activate(
    *,
    interval_minutes: int,
    realtime_days: int,
    settings_template: dict,
    output_parent: Path,
    base_name: str,
    cloud_output_uri: str | None = None,
    forecast: bool = False,
    window_hours: int | None = None,
) -> None:
    """Arm the auto-scheduler daemon for this model.

    The first run fires immediately (``next_run_at = now``); subsequent
    runs are spaced by ``interval_minutes``.  The daemon polls the
    schedule file every ~15 s and survives any browser / Streamlit
    restart.
    """
    now = datetime.utcnow()
    now_iso = now.isoformat()
    state = _read_schedule_state() or {}
    # Stable parent folder for every iteration of this scheduled
    # session - created on first arm, kept on subsequent
    # "Update schedule" clicks so the loop's outputs stay grouped.
    schedule_root = state.get("schedule_root") or (
        f"{base_name}_schedule_{now:%Y%m%d_%H%M%S}"
    )
    state.update({
        "enabled": True,
        "interval_minutes": int(interval_minutes),
        "realtime_days": int(realtime_days),
        # Direction + span so the daemon rebuilds a forward window for a
        # forecast schedule and a backward one for a hindcast schedule.
        # Fall back to realtime_days * 24 for older callers.
        "forecast": bool(forecast),
        "window_hours": int(window_hours) if window_hours
        else int(realtime_days) * 24,
        "next_run_at": now_iso,             # fire as soon as the daemon polls
        "settings_template": settings_template,
        "output_parent": str(output_parent),
        "base_name": base_name,
        "schedule_root": schedule_root,
        "cloud_output_uri": (cloud_output_uri or "").strip() or None,
        "started_at": state.get("started_at") or now_iso,
        "state": "idle",
    })
    _write_schedule_state(state)


def _fmt_duration(seconds: int) -> str:
    """Human-readable duration: '5 m 23 s', '1 h 12 m', '2 d 3 h'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m} m {s:02d} s" if s else f"{m} m"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h} h {m:02d} m" if m else f"{h} h"
    d, rem = divmod(seconds, 86400)
    h = rem // 3600
    return f"{d} d {h} h" if h else f"{d} d"


def _run_display_metadata(output_dir: Path) -> dict:
    """Compute display info for a finished run shown in Tab 5.

    Returns a dict with optional keys:
      * ``started_at`` / ``ended_at`` (ISO strings from run.status)
      * ``duration_seconds``         (end − start)
      * ``run_index`` / ``run_total`` (position in autoschedule history)
    """
    info: dict = {}
    # Wall-clock start + end from the job runner's status file.
    status_p = output_dir / "run.status"
    if status_p.exists():
        try:
            s = json.loads(status_p.read_text())
            if isinstance(s, dict):
                if s.get("started_at"):
                    info["started_at"] = str(s["started_at"])
                if s.get("ended_at"):
                    info["ended_at"] = str(s["ended_at"])
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback ``ended_at`` = mtime of wse_extract.npz (it's written
    # exactly at the end of the pipeline, before cleanup).
    if "ended_at" not in info:
        npz_p = output_dir / "wse_extract.npz"
        if npz_p.exists():
            try:
                info["ended_at"] = datetime.utcfromtimestamp(
                    npz_p.stat().st_mtime
                ).isoformat()
            except OSError:
                pass
    if info.get("started_at") and info.get("ended_at"):
        try:
            _s = info["started_at"].replace("Z", "")
            _e = info["ended_at"].replace("Z", "")
            t0 = datetime.fromisoformat(_s)
            t1 = datetime.fromisoformat(_e)
            info["duration_seconds"] = int((t1 - t0).total_seconds())
        except ValueError:
            pass

    # Position in the autoschedule history (only counts successful runs
    # so the index matches the "N completed" counter shown elsewhere).
    if HISTORY_FILE.exists():
        try:
            hist = json.loads(HISTORY_FILE.read_text())
            if isinstance(hist, list):
                done_runs = [
                    e for e in hist
                    if isinstance(e, dict) and e.get("state") == "done"
                ]
                target = str(output_dir)
                for idx, e in enumerate(done_runs):
                    if str(e.get("output_dir", "")) == target:
                        info["run_index"] = idx + 1
                        info["run_total"] = len(done_runs)
                        break
        except (json.JSONDecodeError, OSError):
            pass
    return info


def _latest_successful_run() -> Path | None:
    """Find the most recent **successful** daemon run on disk.

    Walks the auto-scheduler history file from newest to oldest, and
    returns the first entry whose output folder still exists and
    contains ``wse_extract.npz`` (so Tab 5 can load it without error).

    This lets the Results tab auto-refresh every loop iteration
    *independently* of Streamlit's session state - a reloaded browser
    tab, a fresh container, or even a server restart all converge to
    the same "latest finished run" view as long as the on-disk history
    survives.
    """
    if not HISTORY_FILE.exists():
        return None
    try:
        hist = json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(hist, list):
        return None
    for entry in reversed(hist):
        if not isinstance(entry, dict) or entry.get("state") != "done":
            continue
        p = entry.get("output_dir")
        if not p:
            continue
        try:
            run_dir = Path(p)
            if run_dir.exists() and (run_dir / "wse_extract.npz").exists():
                return run_dir
        except OSError:
            continue
    return None


def _sync_results_to_latest_daemon_run() -> None:
    """Auto-update Tab 5's selection to the latest successful daemon run.

    Runs on every Streamlit render.  Behaviour:

    * **Schedule armed** - always promote the latest finished run so
      Tab 5 refreshes every loop iteration, even after a page reload.
    * **Schedule idle, no selection** - same (so a fresh browser tab
      lands on the most recent finished run instead of empty state).
    * **Schedule idle, user has manually opened a previous run** - do
      nothing, so "Open Previous Results" picks are respected.
    """
    sched = _read_schedule_state()
    sched_armed = bool(sched and sched.get("enabled"))
    has_selection = bool(st.session_state.get("view_results"))
    if not sched_armed and has_selection:
        return
    latest = _latest_successful_run()
    if latest is None:
        return
    if st.session_state.get("last_output_dir") != str(latest):
        st.session_state["last_output_dir"] = str(latest)
        st.session_state["view_results"] = True


def _autoschedule_disable() -> None:
    """Tell the daemon to stop after the current run."""
    s = _read_schedule_state()
    if not s:
        return
    s["enabled"] = False
    s["state"] = "disabled"
    s["disabled_at"] = datetime.utcnow().isoformat()
    _write_schedule_state(s)


def _persist_active_job(d: str | None) -> None:
    """Write or clear the on-disk active-job marker."""
    if d:
        _active_job_file().write_text(d)
    elif _active_job_file().exists():
        _active_job_file().unlink(missing_ok=True)


def _active_job_dir() -> str | None:
    """Return output_dir of an in-flight job, or None.

    The on-disk ``/app/.active_job`` file is the **source of truth** -
    we read it on every call rather than trusting a session_state
    cache.  The daemon writes a *new* path here at the start of every
    scheduled iteration, so a cached value from the previous run would
    cause the Tab 4 progress UI to disappear after the first loop.

    We also never delete ``.active_job`` from this function: the
    daemon (and the Stop-run button) own that file's lifecycle.
    """
    if not _active_job_file().exists():
        st.session_state.pop("active_job_dir", None)
        return None
    try:
        d = _active_job_file().read_text().strip()
    except OSError:
        return None
    if not d:
        return None
    try:
        s = _read_job_status(Path(d))
    except Exception:
        return None
    if s.get("state") != "running":
        # File exists but the run already ended (status not yet caught
        # up by the daemon) - treat as "no active job" but leave the
        # file alone so the daemon's own cleanup is the only writer.
        st.session_state.pop("active_job_dir", None)
        return None
    st.session_state["active_job_dir"] = d
    st.session_state.setdefault("last_output_dir", d)
    return d


def _busy_banner_if_active() -> bool:
    """Show a 'simulation running' banner. Returns True if a job is active."""
    if _active_job_dir():
        st.warning(
            "**Simulation in progress** - controls in this tab are "
            "inactive. Switch to **Tab 4 · Run** to watch progress or stop "
            "the run. Use the **Reset** button at the top right to abort.",
        )
        return True
    return False


class _BusyExit(Exception):
    """Sentinel to short-circuit a tab body while a simulation is running."""


@st.fragment(run_every="1s")
def _render_active_job(output_dir_str: str) -> None:
    """Poll the detached job's status and update the UI every second.

    The HEC-RAS engine emits a ``PROGRESS|pct|msg`` marker roughly
    every second (run_hecras rate-limits to 1 Hz); polling at the same
    cadence keeps the progress bar and pipeline log advancing smoothly
    instead of jumping in 3-second chunks.
    """
    d = Path(output_dir_str)
    s = _read_job_status(d)
    state = s.get("state", "running")
    pct = int(s.get("progress", 0))
    msg = s.get("message", "")
    st.progress(
        max(0, min(100, pct)),
        text=(f"{pct}% - {msg}" if msg else f"{pct}%"),
    )

    # ── Full pipeline log (lightly filtered) ─────────────────────────
    # A scheduled HEC-RAS run is a *pipeline* - config load, USGS /
    # NOAA fetch, model copy, BC injection, engine compute, WSE
    # extraction, optional cloud upload.
    #
    # The status reader (`_parse_log`) only returns the last 80 RAW
    # lines of run.log, and once the engine is past startup those 80
    # lines are *all* per-timestep spam.  Filtering then leaves
    # nothing visible.  Read the **full** run.log here so the
    # non-engine pipeline messages (a few dozen per run) survive for
    # the whole simulation.
    #
    # Drop only the high-volume / low-value noise:
    #   - our own ``PROGRESS|…`` markers (already in the progress bar)
    #   - per-timestep engine markers (``SIMTIME``, ``ABSDATE``,
    #     ``ABSTIME``, ``ITER2D``, internal ``PROGRESS=``)
    #   - verbose third-party HTTP / debug chatter
    _ENGINE_SPAM = (
        "SIMTIME=", "ABSDATE=", "ABSTIME=", "ITER2D=", "PROGRESS=",
    )
    _full_log_text = ""
    try:
        _log_path_file = Path(output_dir_str) / "run.log"
        if _log_path_file.exists():
            # Cap the read so very long simulations don't allocate
            # hundreds of MB into the Streamlit process - 2 MiB of
            # tail is plenty for a few hundred filtered lines.
            _sz = _log_path_file.stat().st_size
            _cap = 2 * 1024 * 1024
            with open(_log_path_file, "rb") as _fh:
                if _sz > _cap:
                    _fh.seek(_sz - _cap)
                _full_log_text = _fh.read().decode(
                    "utf-8", errors="ignore"
                )
    except OSError:
        _full_log_text = ""
    if not _full_log_text:
        # Fall back to whatever the status reader has if the log file
        # isn't on disk yet (e.g. very first second of a run).
        _full_log_text = s.get("log_tail", "") or ""

    _pipeline_lines: list[str] = []
    for _ln in _full_log_text.splitlines():
        _stripped = _ln.strip()
        if not _stripped:
            continue
        if _stripped.startswith("PROGRESS|"):
            continue
        if _stripped.startswith((
            "DEBUG:", "INFO:urllib", "WARNING:urllib",
            "INFO:botocore", "DEBUG:botocore",
        )):
            continue
        if any(tag in _stripped for tag in _ENGINE_SPAM):
            continue
        _pipeline_lines.append(_ln)
    _pipeline_log = (
        "\n".join(_pipeline_lines[-300:])
        if _pipeline_lines else "Pipeline starting…"
    )
    # Fixed-height, auto-scrolling iframe - keeps the page layout
    # stable as the log grows, and pins the view to the most recent
    # line on every render.
    _log_iframe_height = 380
    _log_html_doc = f"""<!doctype html>
<html><head><meta charset='utf-8'><style>
  html, body {{ margin:0; padding:0; height:100%; }}
  pre.log {{
    background: rgba(13, 27, 42, 0.85);
    color: #e6edf3;
    font-family: 'SF Mono','JetBrains Mono',Menlo,Consolas,monospace;
    font-size: 0.82rem;
    line-height: 1.45;
    padding: 0.7rem 0.9rem;
    margin: 0;
    white-space: pre;
    box-sizing: border-box;
    height: 100vh;
    width: 100%;
    overflow-y: auto;
    border: 1px solid rgba(0,180,216,0.25);
    border-radius: 6px;
  }}
</style></head><body>
<pre class='log' id='log'>{_html.escape(_pipeline_log)}</pre>
<script>
  const el = document.getElementById('log');
  if (el) {{ el.scrollTop = el.scrollHeight; }}
</script>
</body></html>"""
    with st.expander("Live pipeline log", expanded=True):
        _st_components.html(
            _log_html_doc, height=_log_iframe_height, scrolling=False
        )
    if state == "running":
        if st.button("Stop run", type="secondary", key="stop_run_btn"):
            _stop_job(d)
            st.session_state.pop("active_job_dir", None)
            _persist_active_job(None)
            st.rerun(scope="app")
        st.caption(
            "The simulation is detached from this browser - closing the "
            "tab, sleeping the screen, or losing the websocket will **not** "
            "stop it. Come back any time to check progress."
        )
        return

    # Job ended - promote state and trigger a full rerun.
    _succeeded = (
        state == "done" and int(s.get("returncode", 1)) == 0
    )
    if _succeeded:
        st.session_state["run_complete"] = True
        st.session_state["view_results"] = True
        # Advance the Results tab to this just-finished run - without
        # this, Tab 5 would keep showing the *first* completed run
        # forever even though new loops keep producing fresh results.
        st.session_state["last_output_dir"] = output_dir_str
        st.session_state["last_run_summary"] = {
            "output_dir": output_dir_str,
            "start": st.session_state.get("last_start_str", ""),
            "end": st.session_state.get("last_end_str", ""),
            "completed_at": datetime.utcnow().isoformat(),
        }
        run_history = st.session_state.setdefault("run_history", [])
        run_history.append(st.session_state["last_run_summary"])
    elif state in ("failed", "stopped", "ended"):
        st.session_state["run_failed_state"] = state
        # Record the failure so the user can see it; the auto-schedule
        # keeps running so transient errors do not break the real-time
        # loop on an unattended server.
        fails = st.session_state.setdefault("failed_runs", [])
        fails.append({
            "state": state,
            "output_dir": output_dir_str,
            "at": datetime.utcnow().isoformat(),
            "message": s.get("message", ""),
        })

    # Auto-scheduling is owned by the standalone auto_scheduler
    # daemon now - no session-state re-arm needed here.

    st.session_state.pop("active_job_dir", None)
    _persist_active_job(None)
    st.rerun(scope="app")


def _reset_app() -> None:
    """Clear all run/result/schedule state and return to Tab 1.

    Also disables the auto-scheduler daemon so a months-long real-time
    loop can be stopped with a single click.
    """
    _busy = st.session_state.get("active_job_dir")
    if _busy:
        try:
            _stop_job(Path(_busy))
        except Exception:
            pass
    _persist_active_job(None)
    # Stop the auto-scheduler daemon if it is armed.
    try:
        _autoschedule_disable()
    except Exception:
        pass
    # Wipe the schedule-state file entirely so a stale `settings_template`
    # can't re-hydrate widgets after the rerun.
    try:
        if SCHEDULE_FILE.exists():
            SCHEDULE_FILE.unlink()
    except OSError:
        pass
    # Same for the run-history file - otherwise Tab 5 / Live snap back
    # to the previous "latest run" on the very next render.
    try:
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
    except OSError:
        pass
    # Wipe the on-disk preference snapshot.  This must happen *before*
    # clearing session_state so a delete failure (read-only mount, etc.)
    # can't silently leave the file behind.
    _prefs_removed = False
    try:
        if _user_prefs_path().exists():
            _user_prefs_path().unlink()
            _prefs_removed = True
    except OSError:
        pass
    # Clear session state last.
    for _k in list(st.session_state.keys()):
        del st.session_state[_k]
    # Set a tombstone flag so the *next* run skips _load_user_prefs and
    # _hydrate_bc_widgets even if any subsystem managed to recreate the
    # files between unlink and rerun.
    st.session_state["_reset_tombstone"] = True
    # User-visible confirmation that the Reset actually fired.
    st.toast(
        "App reset - model, schedule, history & prefs cleared"
        + ("" if _prefs_removed else " (prefs file already gone)"),
    )
    st.rerun()


# ── Professional light palette (refined slate + deep ocean blue) ─────
# v2.5.5 polished the palette around slate-200/500/800 (Tailwind) plus
# sky-700 for primary accents.  Subtle shadows, tighter radii, and an
# Inter typography stack give the UI a more refined, software-product
# feel.
_THEME_LIGHT_VARS = (
    # HECinBOX brand palette - white page like the logo, deep navy
    # text echoing the "HEC" wordmark, water-blue primary echoing the
    # "inBOX" wordmark and the running water in the logo art.
    "--bg0:#eef0f5;"              # cool grey page bg (user's RGB 238,240,245)
    "--bg1:#ffffff;"              # card surface - keep crisp white for cards
    "--bg2:#e1e6ee;"              # deeper accent wash - harmonised w/ bg0
    "--text:#0f1e3a;"             # deep navy (matches logo wordmark)
    "--text-muted:#475569;"       # slate-600
    "--primary:#3c78af;"          # rgb(60,120,175) - muted water blue
    "--primary-hover:#306496;"    # ~15% darker for hover state
    "--primary-soft:rgba(60,120,175,0.10);"
    "--border:#dbeafe;"           # sky-100
    "--border-strong:#93c5fd;"    # sky-300
    "--card:rgba(255,255,255,0.99);"
    "--input:#ffffff;"
    "--grid:rgba(15,30,58,0.06);"
    "--shadow-sm:0 1px 2px rgba(15,30,58,0.05);"
    "--shadow:0 1px 3px rgba(15,30,58,0.07),0 1px 2px rgba(15,30,58,0.04);"
    "--shadow-md:0 4px 14px rgba(15,30,58,0.10);"
    "--radius-sm:6px;--radius-md:8px;--radius-lg:12px;"
)

_THEME_RULES = """
/* ── Modern typography stack ─────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

/* ── Oversized HECinBOX title (h1.hb-title) ──────────────────────
   Uses CSS `clamp()` so the title scales with viewport width but
   never gets absurdly large on a 4K display or tiny on a phone.
   Min: 2.0rem (mobile)  ·  Pref: 6vw (fluid)  ·  Max: 5.5rem (desktop). */
.hb-title {
    font-family: 'Inter', sans-serif !important;
    font-weight: 800 !important;
    letter-spacing: -0.025em !important;
    line-height: 1.0 !important;
    margin: 0.1rem 0 0.25rem !important;
    font-size: clamp(2rem, 6vw, 5.5rem) !important;
    background: linear-gradient(135deg, #0f1e3a 0%, #3c78af 100%);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
}

/* ── Responsive layout ───────────────────────────────────────────
   Streamlit's default layout works well on 1200px+ screens; tighten
   spacing on narrower viewports so phones / tablets / small laptops
   don't get awkward overflow.  We do NOT touch the underlying column
   structure - Streamlit's flex children already wrap when squeezed -
   but we soften paddings, shrink the logo, and trim the page margin. */
@media (max-width: 900px) {
    .stApp .block-container {
        padding: 1rem 1rem 2rem !important;
        max-width: 100% !important;
    }
    .hb-title { font-size: clamp(1.75rem, 8vw, 3rem) !important; }
    /* Pull the logo down so it doesn't collide with the title above. */
    img[alt="HECinBOX"], div[data-testid="stImage"] img {
        max-width: 180px !important;
    }
}
@media (max-width: 600px) {
    .stApp .block-container {
        padding: 0.5rem 0.6rem 1.5rem !important;
    }
    .hb-title { font-size: clamp(1.5rem, 9vw, 2.4rem) !important; }
    img[alt="HECinBOX"], div[data-testid="stImage"] img {
        max-width: 130px !important;
    }
    /* Stack BC widget rows on phones instead of fixed-width columns. */
    div[data-testid="stHorizontalBlock"] {
        flex-wrap: wrap !important;
    }
    div[data-testid="column"] {
        min-width: 0 !important;
        flex: 1 1 100% !important;
    }
}

/* Make the page itself never exceed the viewport horizontally so
   long file paths / DSS pathnames don't create a horizontal
   scrollbar on narrow screens. */
.stApp { overflow-x: hidden !important; }
.stApp .block-container { overflow-x: visible !important; }


/* Material Symbols / Icons - Streamlit uses these for expander
   chevrons, alert icons, etc.  Importing explicitly guarantees the
   ligatures render as glyphs, not literal text like `arrow_drop_down`
   or `configure`, regardless of the host's network policy. */
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined&display=swap');
@import url('https://fonts.googleapis.com/icon?family=Material+Icons');

/*
 * Apply Inter to the page surface - but NOT via a wildcard `*`
 * selector, which would clobber Streamlit's Material Symbols icon
 * spans (expander chevrons, alert icons, etc.) and cause ligature
 * names like `arrow_right` / `arrow_drop_down` to render as literal
 * text stacked on top of each other.  We target text-bearing
 * containers explicitly and leave icon spans alone.
 */
.stApp, body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont,
                 'Segoe UI', Roboto, Helvetica, Arial, sans-serif !important;
}
/*
 * Apply Inter ONLY to known text-content elements.  Bare `span` / `div`
 * / `button` selectors would still hit Streamlit's Material Symbols
 * icon spans (which ARE `<span>` elements) and break the chevron icons
 * back into literal `arrow_drop_down` text.  We exclude any element
 * whose computed font-family looks like Material Symbols by using a
 * `:not()` guard on common icon class names.
 */
.stApp p, .stApp label, .stApp a, .stApp li, .stApp td, .stApp th,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
.stApp input, .stApp textarea, .stApp select {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont,
                 'Segoe UI', Roboto, Helvetica, Arial, sans-serif !important;
}
/* Force Material Symbols on any element whose class hints at it.
   `!important` AFTER the broad rule wins.  Streamlit's emotion CSS
   often uses generated class names - these substring matches cover
   the official Material classes regardless of version. */
.stApp [class*="material-symbols"],
.stApp [class*="MaterialSymbols"],
.stApp [class*="material-icons"],
.stApp .material-icons,
.stApp .material-icons-outlined,
.stApp .material-icons-rounded {
    font-family: 'Material Symbols Rounded',
                 'Material Symbols Outlined',
                 'Material Icons' !important;
    font-weight: normal !important;
    font-style: normal !important;
    text-transform: none !important;
    letter-spacing: normal !important;
    word-wrap: normal !important;
    white-space: nowrap !important;
    direction: ltr !important;
    -webkit-font-feature-settings: 'liga' !important;
    -webkit-font-smoothing: antialiased !important;
    font-feature-settings: 'liga' !important;
}
/* Help (?) tooltip icons: force the outline (no-fill) Material Symbols
   variant and a fully transparent background - no shading box, no
   border, no shadow - so every help icon is a clean outlined glyph. */
[data-testid="stTooltipHoverTarget"],
[data-testid="stTooltipHoverTarget"] *,
[data-testid="stTooltipIcon"],
[data-testid="stTooltipIcon"] *,
[data-testid="stTooltipHoverTarget"]:hover,
[data-testid="stTooltipIcon"]:hover {
    font-variation-settings: 'FILL' 0, 'wght' 400 !important;
    color: var(--text-muted) !important;
    background: transparent !important;
    background-color: transparent !important;
    box-shadow: none !important;
    border: none !important;
    border-radius: 0 !important;
}
/* Tooltip body: wide enough for the worked examples and the small
   comparison table in the model-time-base help (v4.8.0), and capped in
   height so a long tooltip scrolls instead of running off-screen. */
[data-testid="stTooltipContent"] {
    max-width: 620px !important;
    max-height: 70vh !important;
    overflow-y: auto !important;
}
[data-testid="stTooltipContent"] table {
    width: 100% !important;
    border-collapse: collapse !important;
    font-size: 0.82em !important;
    margin: 0.4em 0 !important;
}
[data-testid="stTooltipContent"] th,
[data-testid="stTooltipContent"] td {
    border: 1px solid var(--border, #cbd3e1) !important;
    padding: 4px 7px !important;
    text-align: left !important;
    vertical-align: top !important;
}
code, pre, .stCode, [data-testid="stCodeBlock"],
.stApp code, .stApp pre {
    font-family: 'JetBrains Mono', 'SF Mono', Menlo, Consolas,
                 monospace !important;
}

/* ── Page surface ─────────────────────────────────────────────── */
.stApp {
    background: var(--bg0) !important;
    color: var(--text) !important;
}
.stApp, [data-testid="stMarkdownContainer"],
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li,
label, [data-testid="stWidgetLabel"] p {
    color: var(--text) !important;
}
[data-testid="stHeader"] { background: transparent !important; }

/* ── Headings - tighter tracking, refined weights ─────────────── */
h1, h2, h3, h4, h5, h6 {
    color: var(--text) !important;
    font-weight: 600 !important;
    letter-spacing: -0.01em !important;
}
h1 { font-weight: 700 !important; letter-spacing: -0.02em !important; }
h5, h6 { color: var(--text-muted) !important; }

/* ── Tabs - clean underline indicator, muted inactive ─────────── */
div[data-baseweb="tab-list"] {
    gap: 0.15rem !important;
    border-bottom: 1px solid var(--border) !important;
    padding-bottom: 0 !important;
}
button[data-baseweb="tab"] {
    font-size: 1.05rem !important;
    font-weight: 500 !important;
    letter-spacing: 0 !important;
    padding: 0.7rem 1.15rem !important;
    color: var(--text-muted) !important;
    border-bottom: 2px solid transparent !important;
    background: transparent !important;
    transition: color 0.15s ease, border-color 0.15s ease,
                background 0.15s ease !important;
}
button[data-baseweb="tab"] > div > p {
    font-size: 1.05rem !important;
    font-weight: 500 !important;
    color: inherit !important;
}
button[data-baseweb="tab"]:hover {
    color: var(--primary) !important;
    background: var(--primary-soft) !important;
    border-radius: var(--radius-sm) var(--radius-sm) 0 0 !important;
}
button[data-baseweb="tab"][aria-selected="true"] {
    color: var(--primary) !important;
    border-bottom-color: var(--primary) !important;
    font-weight: 600 !important;
}
button[data-baseweb="tab"][aria-selected="true"] > div > p {
    font-weight: 600 !important;
}

/* ── Buttons - subtle shadow, polished hover ─────────────────── */
/* Descendant (not child) selectors on purpose: Streamlit wraps any
   button that carries help= in stTooltipIcon > stTooltipHoverTarget,
   so `.stButton > button` misses it and the help-icon rule above
   strips its background/border/shadow - the button then renders as
   bare text.  Hit both shapes, and outrank that rule (v4.8.0). */
.stButton button,
.stDownloadButton button {
    background: var(--bg1) !important;
    color: var(--text) !important;
    border-radius: var(--radius-md) !important;
    border: 1px solid var(--border) !important;
    font-weight: 500 !important;
    box-shadow: var(--shadow-sm) !important;
    transition: all 0.15s ease !important;
}
.stButton button:hover,
.stDownloadButton button:hover {
    background: var(--bg2) !important;
    border-color: var(--primary) !important;
    color: var(--primary) !important;
    box-shadow: var(--shadow) !important;
}
.stButton button[kind="primary"] {
    background: var(--primary) !important;
    color: #ffffff !important;
    border-color: var(--primary) !important;
    box-shadow: 0 1px 3px rgba(3,105,161,0.28) !important;
}
/* The label sits in an inner markdown <p>/<div> that carries its own
   (dark) colour, overriding the button's white - force the text white
   in every state so primary buttons read white like the brand. */
.stButton button[kind="primary"] p,
.stButton button[kind="primary"] div,
.stButton button[kind="primary"] span,
.stButton button[kind="primary"] [data-testid="stMarkdownContainer"],
.stButton button[kind="primary"]:hover p,
.stButton button[kind="primary"]:hover div,
.stButton button[kind="primary"]:hover span {
    color: #ffffff !important;
}
.stButton button[kind="primary"]:hover {
    background: var(--primary-hover) !important;
    border-color: var(--primary-hover) !important;
    box-shadow: 0 4px 10px rgba(3,105,161,0.32) !important;
    transform: translateY(-1px) !important;
}

/* ── Cards / bordered blocks - subtle shadow + lift on hover ─── */
div[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: var(--radius-lg) !important;
    background: var(--card) !important;
    border: 1px solid var(--border) !important;
    box-shadow: var(--shadow) !important;
    padding: 0.75rem 1.1rem !important;
    transition: box-shadow 0.2s ease !important;
}

/* ── Inputs - focus ring, modern styling ─────────────────────── */
div[data-baseweb="select"] > div,
div[data-baseweb="input"] > div,
.stNumberInput input, .stTextInput input,
.stDateInput input, .stTextArea textarea {
    background: var(--input) !important;
    color: var(--text) !important;
    border-radius: var(--radius-md) !important;
    border: 1px solid var(--border) !important;
    box-shadow: var(--shadow-sm) !important;
    transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
}
.stTextInput input:focus, .stNumberInput input:focus,
.stDateInput input:focus, .stTextArea textarea:focus,
div[data-baseweb="input"]:focus-within > div,
div[data-baseweb="select"]:focus-within > div {
    border-color: var(--primary) !important;
    box-shadow: 0 0 0 3px var(--primary-soft) !important;
    outline: none !important;
}
.stNumberInput button, .stNumberInput button > svg,
.stDateInput button, .stDateInput button > svg {
    background: var(--input) !important;
    color: var(--text-muted) !important;
    fill: var(--text-muted) !important;
}
[role="listbox"] {
    background: var(--bg1) !important;
    border: 1px solid var(--border) !important;
    box-shadow: var(--shadow-md) !important;
    border-radius: var(--radius-md) !important;
}
[role="option"], [role="option"] * { color: var(--text) !important; }
[role="option"]:hover { background: var(--bg2) !important; }

/* ── Radio / toggle / checkbox labels ─────────────────────────── */
[data-baseweb="radio"] label, [data-baseweb="checkbox"] label,
[data-testid="stRadio"] label, [data-testid="stCheckbox"] label,
[data-testid="stToggle"] label, [data-testid="stToggle"] p {
    color: var(--text) !important;
}

/* ── Code blocks - refined mono, subtle border ────────────────── */
code {
    background: var(--bg2) !important;
    color: var(--primary) !important;
    border-radius: var(--radius-sm) !important;
    padding: 0.1rem 0.35rem !important;
    font-size: 0.88em !important;
}
pre, [data-testid="stCodeBlock"] {
    background: var(--bg1) !important;
    color: var(--text) !important;
    border-radius: var(--radius-md) !important;
    border: 1px solid var(--border) !important;
    box-shadow: var(--shadow-sm) !important;
}

/* ── Expanders - flat, hoverable ──────────────────────────────── */
details {
    background: var(--bg1) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius-md) !important;
    box-shadow: var(--shadow-sm) !important;
    transition: box-shadow 0.15s ease !important;
}
details:hover { box-shadow: var(--shadow) !important; }
details summary { padding: 0.5rem 0.75rem !important; }
details summary, details summary p { color: var(--text) !important; }

/* ── Dividers - subtle ─────────────────────────────────────────── */
hr {
    border: none !important;
    border-top: 1px solid var(--border) !important;
    margin: 1.4rem 0 !important;
}

/* ── Alerts / banners - refined left border ───────────────────── */
[data-testid="stAlert"] {
    border-radius: var(--radius-md) !important;
    border: 1px solid var(--border) !important;
    border-left-width: 4px !important;
    box-shadow: var(--shadow-sm) !important;
}

/* ── Metric - refined card look ───────────────────────────────── */
[data-testid="stMetric"] {
    background: var(--bg1) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius-md) !important;
    padding: 0.65rem 0.85rem !important;
    box-shadow: var(--shadow-sm) !important;
}
[data-testid="stMetricLabel"] {
    color: var(--text-muted) !important;
    font-weight: 500 !important;
    font-size: 0.8rem !important;
    letter-spacing: 0.01em !important;
}
[data-testid="stMetricValue"] {
    color: var(--text) !important;
    font-weight: 600 !important;
}

/* ── Progress bar ───────────────────────────────────────────────
   Intentionally NOT styled here.  Streamlit already paints the fill
   with the theme's `primaryColor` (#3c78af, see .streamlit/config.toml).
   The previous hand-rolled overrides guessed at BaseWeb's internal DOM
   (`[role="progressbar"] > div`, `div[style*="width"]`) with !important
   and mis-targeted the track/fill on Streamlit >=1.35, which made the
   bar render empty.  Removed in v3.1.1 - let the theme handle it. */

/* ── Plotly modebar - light backing ────────────────────────────── */
.modebar { background: rgba(255,255,255,0.92) !important; }
.modebar-btn path { fill: #475569 !important; }
"""


def _theme_css() -> str:
    """Build the light-theme <style> block (single palette, no toggle)."""
    return f"<style>:root{{{_THEME_LIGHT_VARS}}}{_THEME_RULES}</style>"


# Restore any persisted preferences *before* the widgets render so each
# widget's `key` picks up the saved value as its default.
_load_user_prefs()


def _hydrate_bc_widgets_from_active_schedule() -> None:  # noqa: D401
    # Skip immediately after a Reset so cleared widgets don't get
    # repopulated from a stale schedule template.
    if st.session_state.get("_reset_tombstone"):
        return
    return _hydrate_bc_widgets_from_active_schedule_impl()


def _hydrate_bc_widgets_from_active_schedule_impl() -> None:
    """Pre-populate Tab 3 BC widget state from the running schedule.

    When the auto-scheduler is armed, its `settings_template` already
    holds the boundary-condition configuration the daemon is actually
    using each cycle.  This is the *authoritative* source of truth -
    the user's session-state may have drifted (or been wiped on page
    reload before USER_PREFS_FILE got a chance to write).  We mirror
    the template's BC list into the widget keys so Tab 3 dropdowns
    reflect what the schedule is really doing, and so the Tab 4
    warning banner doesn't fire incorrectly.
    """
    sched = _read_schedule_state()
    if not sched or not sched.get("enabled"):
        return
    tmpl = sched.get("settings_template") or {}
    bcs = tmpl.get("boundary_conditions") or []
    for i, bc in enumerate(bcs):
        src = str(bc.get("source", "none")).lower()
        if src == "forecast":
            # v3.0.3 - forecast is now two dropdown entries; pick the
            # right one from the stored product.
            src_label = (
                "Forecast (STOFS)"
                if "stofs" in str(bc.get("forecast_product", ""))
                else "Forecast (NWM v.3)"
            )
        else:
            src_label = {
                "usgs": "USGS",
                "noaa": "NOAA",
                "constant": "Constant",
            }.get(src, "Leave unchanged")
        st.session_state[f"bc_src_{i}"] = src_label
        # USGS / NOAA secondary fields.
        if src in ("usgs", "noaa"):
            station = str(bc.get("station", ""))
            st.session_state[f"bc_st_{i}"] = station
        if src == "usgs":
            st.session_state[f"bc_pc_{i}"] = str(
                bc.get("parameter", "00060")
            )
            st.session_state[f"bc_ts_{i}"] = int(
                bc.get("timestep_minutes", 15)
            )
        elif src == "noaa":
            st.session_state[f"bc_dt_{i}"] = str(
                bc.get("datum", "NAVD")
            )
            st.session_state[f"bc_un_{i}"] = str(
                bc.get("units", "english")
            )
            st.session_state[f"bc_tsn_{i}"] = int(
                bc.get("timestep_minutes", 60)
            )
        elif src == "constant":
            try:
                st.session_state[f"bc_cv_{i}"] = float(
                    bc.get("constant_value", 0.0)
                )
            except (TypeError, ValueError):
                st.session_state[f"bc_cv_{i}"] = 0.0
            st.session_state[f"bc_cu_{i}"] = str(
                bc.get("constant_unit_label", "")
            )
        elif src == "forecast":
            # v3.0.3 - restore Forecast UI state on page reload so the
            # daemon-template-driven displays match disk truth.  The
            # STOFS vs NWM choice is reconstructed from the product in
            # the dropdown hydrate above; here we only restore the
            # secondary fields.
            _fprod = str(bc.get("forecast_product", ""))
            if "stofs" in _fprod:
                st.session_state[f"bc_st_{i}"] = str(
                    bc.get("stofs_station", "")
                )
                st.session_state[f"bc_fc_dom_{i}"] = str(
                    bc.get("stofs_domain", "atlantic")
                )
            elif "usgs_rating" in _fprod:
                st.session_state[f"bc_fc_path_{i}"] = (
                    "Path A - USGS empirical rating "
                    "(recommended where a USGS gauge exists)"
                )
                st.session_state[f"bc_st_{i}"] = str(
                    bc.get("rating_site_no", "")
                )
            elif "hand_rating" in _fprod:
                st.session_state[f"bc_fc_path_{i}"] = (
                    "Path B - HAND synthetic rating "
                    "(universal fallback for ungauged reaches)"
                )
                st.session_state[f"bc_fc_hand_{i}"] = str(
                    bc.get("hand_reach_id") or bc.get("comid", "")
                )
            if bc.get("comid"):
                st.session_state[f"bc_fc_comid_{i}"] = str(bc["comid"])
            st.session_state[f"bc_fc_h_{i}"] = str(
                bc.get("forecast_horizon", "medium_range")
            )
            try:
                st.session_state[f"bc_fc_m_{i}"] = int(
                    bc.get("forecast_member", 1)
                )
            except (TypeError, ValueError):
                st.session_state[f"bc_fc_m_{i}"] = 1

    # Rain-on-mesh widgets (v3.2.0) - mirror the template's
    # precipitation block the same way the BC rows are mirrored above.
    precip = tmpl.get("precipitation") or {}
    st.session_state["precip_on"] = bool(precip.get("enabled"))
    if precip.get("enabled"):
        # The schedule already committed to rain on mesh - pre-clear the
        # "add rain?" confirm gate so re-arming doesn't block on it.
        st.session_state["_precip_confirm_add"] = True
        _pm = str(precip.get("mode", "constant")).lower()
        if _pm == "gridded":
            _src = str(precip.get("source", "aorc")).lower()
            st.session_state["precip_mode"] = {
                "aorc": "Gridded - AORC (hindcast)",
                "hrrr": "Gridded - HRRR (forecast)",
                "dss": "Gridded - DSS file (upload)",
            }.get(_src, "Gridded - AORC (hindcast)")
        elif _pm == "constant":
            st.session_state["precip_mode"] = "Constant"
        else:
            st.session_state["precip_mode"] = "Leave unchanged"
        if _pm == "constant":
            try:
                st.session_state["precip_value"] = float(
                    precip.get("constant_value", 10.0)
                )
            except (TypeError, ValueError):
                st.session_state["precip_value"] = 10.0
            st.session_state["precip_units"] = str(
                precip.get("constant_units", "mm/hr")
            )


_hydrate_bc_widgets_from_active_schedule()

# Keep the Results tab pinned to the auto-scheduler's most recent
# successful run, independent of session state.  Without this Tab 5
# would stay empty after a page reload even though completed runs
# exist on disk.
_sync_results_to_latest_daemon_run()


@st.fragment(run_every="2s")
def _daemon_refresh_beacon() -> None:
    """Force a full app rerun when the daemon's on-disk state changes.

    The auto_scheduler daemon runs in a separate process and writes three
    files this beacon watches:

      * ``.active_job``               - set when a run starts, cleared at end
      * ``autoschedule.history.json`` - appended on each completion
      * ``autoschedule.json``         - schedule state / next_run_at

    When any of them changes we trigger ``st.rerun(scope="app")`` so the
    Results tab (Tab 5) and Live tab (Tab 8) refresh automatically at the
    end of every scheduled cycle - no manual browser reload.

    This lives at the **top level** of the script on purpose.  A
    ``run_every`` fragment's app-scoped rerun is reliably delivered from
    the top-level render tree but was being swallowed when the same logic
    sat nested inside ``with tab_run:`` (the cause of the stale-Tab-5 bug
    fixed in v3.1.2).  The beacon renders nothing visible.
    """
    _ajob_now = _active_job_file().exists()
    try:
        _hist_mtime = (
            HISTORY_FILE.stat().st_mtime if HISTORY_FILE.exists() else 0.0
        )
    except OSError:
        _hist_mtime = 0.0
    try:
        _sched_mtime = (
            SCHEDULE_FILE.stat().st_mtime if SCHEDULE_FILE.exists() else 0.0
        )
    except OSError:
        _sched_mtime = 0.0

    _ajob_was = st.session_state.get("_daemon_run_active")
    _hist_was = st.session_state.get("_hist_mtime")
    _sched_was = st.session_state.get("_sched_mtime")

    # Record the current baseline before deciding whether to rerun.
    st.session_state["_daemon_run_active"] = _ajob_now
    st.session_state["_hist_mtime"] = _hist_mtime
    st.session_state["_sched_mtime"] = _sched_mtime

    # First observation in this session - just record, don't rerun.
    if _ajob_was is None or _hist_was is None or _sched_was is None:
        return

    if (
        _ajob_now != _ajob_was
        or _hist_mtime != float(_hist_was)
        or _sched_mtime != float(_sched_was)
    ):
        st.rerun(scope="app")


_daemon_refresh_beacon()


# ── Detect ?live=1 early so we can skip the header / Reset button ──
# Viewers landing via a shared `?live=1` URL must not see the Reset
# button, which would let them stop the schedule.  We detect the flag
# here (before any UI renders) and conditionally render the header.
try:
    _qp_early = st.query_params
    _LIVE_MODE = (
        str(_qp_early.get("live", "")).lower() in ("1", "true", "yes")
    )
except Exception:
    _LIVE_MODE = False

# ── Header - title, Reset, logo (single light theme, no sidebar) ──
# In live mode the entire header is hidden so a view-only viewer can't
# trigger Reset / see version / etc.  The full app's header still
# renders normally in non-live mode.
if not _LIVE_MODE:
    # Two-column header: oversized title on the left, logo on the right.
    # Reset moved to Tab 1 (right of the Select Model heading) so the
    # header has more breathing room.
    _hcol_text, _hcol_logo = st.columns([6.0, 2.2])
    with _hcol_text:
        st.markdown(
            "<h1 class='hb-title'>HECinBOX</h1>",
            unsafe_allow_html=True,
        )
        st.caption(
            "An automated agent-based flood warning framework using 2D "
            f"unsteady HEC-RAS simulations · v{APP_VERSION}"
        )
    with _hcol_logo:
        if _logo_path.exists():
            _render_logo(
                _logo_path,
                css_width="300px",
                align="right",
                pull_top_px=38,
                pull_right_px=20,
            )

    # ── "Update available" banner ────────────────────────────────────
    # Check Docker Hub for a newer published image and nudge local users
    # to pull it.  Hidden in the hosted demo (visitors can't update it)
    # and fully fail-silent when offline.
    if not DEMO_MODE:
        _latest = _latest_hub_version(APP_VERSION)
        if _latest and _latest > _version_tuple(APP_VERSION):
            _lv = ".".join(map(str, _latest))
            st.info(
                f"**Update available - v{_lv}** "
                f"(you're on v{APP_VERSION}). Pull the new image: "
                f"`docker pull ehsankahrizi1991/hecinbox:v{_lv}`"
            )

# Inject the light theme.
st.markdown(_theme_css(), unsafe_allow_html=True)


# ── Helpers ────────────────────────────────────────────────────────────
_SKIP_EXTENSIONS = frozenset({
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz",
    ".pkg", ".dmg", ".iso", ".img",
})


def _list_subdirs(path: Path) -> list[str]:
    names: list[str] = []
    # Use os.scandir for speed - avoids stat() on every entry
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                # Skip archive files that macOS may report as dirs
                _ext = os.path.splitext(entry.name)[1].lower()
                if _ext in _SKIP_EXTENSIONS:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        return []
    return sorted(names)


def _find_prj(path: Path) -> list[Path]:
    try:
        return sorted(path.glob("*.prj"))
    except (PermissionError, OSError):
        return []


def _find_result_runs(root: Path) -> list[Path]:
    """Find output folders containing wse_extract.npz, up to 2 levels deep.

    Returns folders sorted newest-first by modification time.
    """
    found: list[Path] = []
    try:
        for d1 in root.iterdir():
            if d1.name.startswith(".") or not d1.is_dir():
                continue
            if (d1 / "wse_extract.npz").exists():
                found.append(d1)
                continue
            try:
                for d2 in d1.iterdir():
                    if (
                        not d2.name.startswith(".")
                        and d2.is_dir()
                        and (d2 / "wse_extract.npz").exists()
                    ):
                        found.append(d2)
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        return []
    try:
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        found.sort(reverse=True)
    return found


def _fmt_interval(minutes: int) -> str:
    """Human-readable schedule interval, e.g. '30 min' or '6 h 30 min'."""
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h" if m == 0 else f"{h} h {m} min"


def _parse_model_date(value, fallback: date) -> date:
    try:
        return datetime.strptime(str(value).split()[0], "%d%b%Y").date()
    except (ValueError, AttributeError, IndexError, TypeError):
        return fallback


def _run_subprocess(cmd_args: list[str], progress_widget, log_widget, status_widget):
    """Run a subprocess, parse PROGRESS markers, stream logs."""
    lines: list[str] = []
    prog_re = re.compile(r"^PROGRESS\|(\d+)\|(.*)$")

    proc = subprocess.Popen(
        cmd_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd="/app/src",
        env={**os.environ, "SETTINGS_PATH": str(_settings_path())},
    )

    for line in proc.stdout:
        m = prog_re.match(line.strip())
        if m:
            pct = max(0, min(100, int(m.group(1))))
            progress_widget.progress(pct, text=f"{pct}% - {m.group(2)}")
            continue
        lines.append(line)
        log_widget.code("".join(lines[-80:]), language="text")

    proc.wait()
    return proc.returncode, lines


@st.cache_data(show_spinner="Building cell mesh…", max_entries=2)
def _cell_geojson(npz_path: str, _mtime: float):
    """Build a lon/lat GeoJSON FeatureCollection of 2D mesh cell polygons.

    Returns (geojson_dict, n_cells) or None if the npz lacks polygon data
    or the model projection (WKT) is unavailable.
    """
    import numpy as _np
    d = _np.load(npz_path, allow_pickle=True)
    if "cell_fp" not in d.files or "fp_xy" not in d.files:
        return None
    wkt = str(d["proj_wkt"][0]) if "proj_wkt" in d.files else ""
    if not wkt:
        return None
    from pyproj import Transformer
    cf = d["cell_fp"]
    fp = d["fp_xy"]
    tf = Transformer.from_crs(wkt, "EPSG:4326", always_xy=True)
    lon, lat = tf.transform(fp[:, 0], fp[:, 1])
    feats = []
    for ci in range(cf.shape[0]):
        idx = cf[ci]
        idx = idx[idx >= 0]
        if len(idx) < 3:
            continue
        ring = [[float(lon[k]), float(lat[k])] for k in idx]
        ring.append(ring[0])
        feats.append({
            "type": "Feature",
            "id": int(ci),
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {},
        })
    return {"type": "FeatureCollection", "features": feats}, cf.shape[0]


def _norm_plotly_color(c):
    """Coerce a Plotly colour into a matplotlib-acceptable value.

    Plotly stops may be ``"#rrggbb"``, a named colour, or a
    ``"rgb(r,g,b)"`` / ``"rgba(r,g,b,a)"`` string (0-255 channels) -
    matplotlib rejects the latter, so convert those to 0-1 tuples.
    Anything else passes through unchanged.
    """
    if isinstance(c, str) and c.strip().lower().startswith("rgb"):
        nums = re.findall(r"[\d.]+", c)
        if len(nums) >= 3:
            r, g, b = (float(nums[0]) / 255.0, float(nums[1]) / 255.0,
                       float(nums[2]) / 255.0)
            a = float(nums[3]) if len(nums) >= 4 else 1.0
            return (r, g, b, a)
    return c


def _mpl_cmap_from_scale(scale):
    """Matplotlib colormap matching a Plotly colorscale used in the app.

    ``scale`` is either a Plotly named scale (e.g. ``"Turbo"``/``"earth"``)
    or a list of ``[position, color]`` stops (the custom WSE/Depth ramps;
    colours may be hex, named, or ``rgb(...)`` strings).  Building from
    the same stops keeps the pydeck colours identical to the Plotly maps.
    """
    import matplotlib as mpl
    import matplotlib.colors as mcolors
    if isinstance(scale, (list, tuple)):
        stops = [
            (float(p), _norm_plotly_color(c)) for p, c in scale
        ]
        return mcolors.LinearSegmentedColormap.from_list("_hb", stops)
    name = {
        "Turbo": "turbo", "Viridis": "viridis", "Blues": "Blues",
        "earth": "gist_earth", "Cividis": "cividis",
    }.get(str(scale), str(scale))
    # matplotlib.cm.get_cmap was removed in 3.9+ - use the colormap registry.
    try:
        return mpl.colormaps[name]
    except (KeyError, ValueError):
        return mpl.colormaps["viridis"]


def _legend_html(cmap, vlo, vhi, short, unit):
    """Vertical colour legend overlaid INSIDE the map, right edge.

    pydeck has no built-in colorbar, so this draws one styled to match
    the Plotly maps' inset colorbar (white card, right side).  It must be
    emitted immediately *after* the ``st.pydeck_chart`` call: a 0-height
    anchor with an absolutely-positioned child floats the card up over
    the 680-px-tall chart above it.
    """
    import matplotlib.colors as mcolors
    stops = ", ".join(mcolors.to_hex(cmap(i / 10.0)) for i in range(11))
    vmid = (vlo + vhi) / 2.0
    st.markdown(
        "<div style='position:relative;height:0;z-index:10;'>"
        "<div style='position:absolute;right:22px;bottom:170px;"
        "background:rgba(255,255,255,0.92);border:1px solid #0d1b2a;"
        "border-radius:4px;padding:8px 10px;font-size:11px;"
        "color:#0d1b2a;line-height:1.25;'>"
        f"<b>{short}<br/>({unit})</b>"
        "<div style='display:flex;gap:6px;margin-top:6px;'>"
        "<div style='width:14px;height:300px;border:1px solid #0d1b2a;"
        f"background:linear-gradient(to top,{stops});'></div>"
        "<div style='display:flex;flex-direction:column;"
        "justify-content:space-between;height:300px;'>"
        f"<span>{vhi:.2f}</span><span>{vmid:.2f}</span>"
        f"<span>{vlo:.2f}</span></div>"
        "</div></div></div>",
        unsafe_allow_html=True,
    )


def _legend_html_classes(colors, labels, short, unit):
    """Discrete class legend overlaid INSIDE the map, right edge.

    Same floating white card as ``_legend_html`` but with one swatch
    per depth class instead of a continuous gradient (for the "Depth
    classes" display of the smooth map).  Deepest class at the top,
    matching the vertical colorbar convention.
    """
    rows = "".join(
        "<div style='display:flex;align-items:center;gap:6px;'>"
        "<div style='width:14px;height:14px;flex:none;"
        f"border:1px solid #0d1b2a;background:{c};'></div>"
        f"<span>{_html.escape(str(l))}</span></div>"
        for c, l in zip(reversed(list(colors)), reversed(list(labels)))
    )
    st.markdown(
        "<div style='position:relative;height:0;z-index:10;'>"
        "<div style='position:absolute;right:22px;bottom:170px;"
        "background:rgba(255,255,255,0.92);border:1px solid #0d1b2a;"
        "border-radius:4px;padding:8px 10px;font-size:11px;"
        "color:#0d1b2a;line-height:1.5;'>"
        f"<b>{short}<br/>({unit})</b>"
        "<div style='margin-top:6px;display:flex;"
        f"flex-direction:column;gap:3px;'>{rows}</div>"
        "</div></div>",
        unsafe_allow_html=True,
    )


# ── Map image export (Save PNG) ───────────────────────────────────────
# Target print resolution of the exported inundation map.
_EXPORT_DPI = 400

# Inundation-map panel shapes: label -> (chart height px, container
# max-width px or None for the full content width).  "Wide" is the
# original 680-px full-width panel, so the default view is unchanged.
# The bordered container adds ~34 px of padding+border, so a max-width of
# height + 34 makes the chart itself square.
_MAP_PANELS = {
    "Wide": (680, None),
    "Tall": (900, None),
    "Square": (820, 854),
}


def _gl_capture_patch() -> None:
    """Make the map's WebGL canvases readable back as pixels.

    deck.gl already keeps its drawing buffer, but the MapLibre basemap
    underneath it does not - captured on its own it reads back empty, so
    an exported map would show the mesh floating on white.  A context's
    ``preserveDrawingBuffer`` flag is fixed when the context is created
    and cannot be changed afterwards, so this override has to be in place
    *before* any chart is built: it is emitted once at the top of the app,
    while the results map only ever appears on a later rerun.
    """
    import streamlit.components.v1 as _components
    _components.html(
        """
        <script>
        (function () {
          var W = window.parent || window;
          if (W.__hbGLPatched) { return; }
          var proto = W.HTMLCanvasElement && W.HTMLCanvasElement.prototype;
          if (!proto) { return; }
          var orig = proto.getContext;
          proto.getContext = function (type, attrs) {
            if (type === 'webgl' || type === 'webgl2' ||
                type === 'experimental-webgl') {
              attrs = Object.assign({}, attrs || {},
                                    {preserveDrawingBuffer: true});
            }
            return orig.call(this, type, attrs);
          };
          W.__hbGLPatched = true;
        })();
        </script>
        """,
        height=0,
    )


def _map_panel_box(map_w: int | None):
    """Bordered panel that holds the inundation map.

    Streamlit's deck.gl chart measures its width once, when it mounts, and
    never re-measures - capping the panel with CSS alone would squeeze a
    full-width render into a narrow box and distort the map.  Wrapping the
    narrowed panels in columns changes the element tree, which remounts the
    chart so it picks the new width up.
    """
    if map_w:
        _, _mid, _ = st.columns([1, 8, 1])
        return _mid.container(border=True, key="res_map_box")
    return st.container(border=True, key="res_map_box")


def _legend_spec(cmap, vlo, vhi, short, unit) -> dict:
    """Colorbar description for the PNG export (mirrors _legend_html)."""
    import matplotlib.colors as mcolors
    return {
        "kind": "gradient",
        "title": str(short),
        "unit": str(unit),
        "stops": [mcolors.to_hex(cmap(i / 10.0)) for i in range(11)],
        "labels": [
            f"{vhi:.2f}", f"{(vlo + vhi) / 2.0:.2f}", f"{vlo:.2f}",
        ],
    }


def _legend_spec_classes(colors, labels, short, unit) -> dict:
    """Class legend description for the PNG export (deepest class first)."""
    return {
        "kind": "classes",
        "title": str(short),
        "unit": str(unit),
        "colors": list(reversed([str(c) for c in colors])),
        "labels": list(reversed([str(le) for le in labels])),
    }


def _map_save_png(filename: str, legend: dict | None = None,
                  dpi: int = _EXPORT_DPI) -> None:
    """"Save PNG" button for the inundation map, exactly as displayed.

    Whatever the user has zoomed, panned, tilted or rotated to on screen is
    what gets written out - the export reads the live map back rather than
    re-rendering it server-side.

    Every map style draws its basemap and its data into GPU canvases (the
    pydeck map adds the deck.gl mesh over the MapLibre basemap; the Plotly
    maps draw their points / raster straight into the map canvas), so one
    composite covers them all.  The colour legend floats over the map as
    HTML rather than living in a canvas, so it is repainted into the bitmap
    here, at export resolution.  The PNG is tagged ``dpi`` in its header and
    so drops into a paper or slide at that print resolution.
    """
    import json as _jse
    import streamlit.components.v1 as _components
    # The config is inlined into a <script>; escaping "</" keeps a stray
    # tag in a label (class names, units) from closing it early.
    _cfg = _jse.dumps({
        "file": filename, "dpi": int(dpi), "legend": legend,
    }).replace("</", "<\\/")
    _components.html(
        """
        <style>
          .hb-row { display:flex; align-items:center; gap:10px;
                    font-family:"Source Sans Pro", sans-serif; }
          .hb-btn { border:1px solid rgba(49,51,63,0.2); border-radius:8px;
                    background:#fff; color:#31333f; cursor:pointer;
                    font-size:14px; padding:6px 14px; line-height:1.6; }
          .hb-btn:hover { border-color:#3c78af; color:#3c78af; }
          .hb-btn:disabled { opacity:0.55; cursor:default; }
          .hb-msg { font-size:12px; color:#5b6570; }
        </style>
        <div class="hb-row">
          <button class="hb-btn" id="hbSave">Save PNG (__DPI__ dpi)</button>
          <span class="hb-msg" id="hbMsg"></span>
        </div>
        <script>
        (function () {
          var CFG = __CFG__;
          var W = window.parent, D = W.document;
          var btn = document.getElementById('hbSave');
          var out = document.getElementById('hbMsg');
          function msg(t) { out.textContent = t; }

          function crc32(bytes) {
            var crc = 0xFFFFFFFF;
            for (var i = 0; i < bytes.length; i++) {
              crc ^= bytes[i];
              for (var k = 0; k < 8; k++) {
                crc = (crc >>> 1) ^ (0xEDB88320 & (-(crc & 1)));
              }
            }
            return (crc ^ 0xFFFFFFFF) >>> 0;
          }

          // Stamp the physical resolution into the PNG (pHYs chunk, right
          // after IHDR) so the file reports the requested dpi.
          function tagDpi(png, dpi) {
            var ppm = Math.round(dpi / 0.0254);
            var chunk = new Uint8Array(21);
            var dv = new DataView(chunk.buffer);
            dv.setUint32(0, 9);
            chunk.set([0x70, 0x48, 0x59, 0x73], 4);   // 'pHYs'
            dv.setUint32(8, ppm);
            dv.setUint32(12, ppm);
            chunk[16] = 1;                            // unit: metre
            dv.setUint32(17, crc32(chunk.subarray(4, 17)));
            var at = 33;                              // 8 sig + 25 IHDR
            var o = new Uint8Array(png.length + chunk.length);
            o.set(png.subarray(0, at), 0);
            o.set(chunk, at);
            o.set(png.subarray(at), at + chunk.length);
            return o;
          }

          function save(blob, name) {
            // Mint the blob URL in the app's own window, not this
            // sandboxed frame's: the download link lives in the app
            // document, and a URL is only valid in the realm that made it.
            var PU = (D.defaultView || W).URL;
            var url = PU.createObjectURL(blob);
            var a = D.createElement('a');
            a.href = url; a.download = name;
            D.body.appendChild(a); a.click(); a.remove();
            setTimeout(function () { PU.revokeObjectURL(url); }, 5000);
          }

          function box() { return D.querySelector('.st-key-res_map_box'); }

          function roundRect(c, x, y, w, h, r) {
            c.beginPath();
            c.moveTo(x + r, y);
            c.arcTo(x + w, y, x + w, y + h, r);
            c.arcTo(x + w, y + h, x, y + h, r);
            c.arcTo(x, y + h, x, y, r);
            c.arcTo(x, y, x + w, y, r);
            c.closePath();
          }

          // Repaint the on-screen legend card into the export bitmap: it
          // is HTML floating over the map, so it is not in either canvas.
          function drawLegend(c, W_, H_, s, lg) {
            if (!lg) { return; }
            var f = function (n) { return n * s; };
            var fam = ' "Source Sans Pro", Arial, sans-serif';
            var pad = f(9), gap = f(6), sw = f(14);
            var bodyH = lg.kind === 'gradient' ? f(300)
                                               : lg.labels.length * f(17);
            c.font = 'bold ' + f(11) + 'px' + fam;
            var head = [lg.title, '(' + lg.unit + ')'];
            var textW = 0, i;
            for (i = 0; i < head.length; i++) {
              textW = Math.max(textW, c.measureText(head[i]).width);
            }
            c.font = f(11) + 'px' + fam;
            var labW = 0;
            for (i = 0; i < lg.labels.length; i++) {
              labW = Math.max(labW, c.measureText(lg.labels[i]).width);
            }
            var bodyW = sw + gap + labW;
            var cardW = Math.max(textW, bodyW) + 2 * pad;
            var headH = 2 * f(14);
            var cardH = headH + f(6) + bodyH + 2 * pad;
            var x = W_ - f(22) - cardW;
            var y = Math.max(f(8), H_ - f(170) - cardH);

            c.fillStyle = 'rgba(255,255,255,0.92)';
            c.strokeStyle = '#0d1b2a';
            c.lineWidth = Math.max(1, s * 0.8);
            roundRect(c, x, y, cardW, cardH, f(4));
            c.fill(); c.stroke();

            c.fillStyle = '#0d1b2a';
            c.textBaseline = 'top';
            c.font = 'bold ' + f(11) + 'px' + fam;
            for (i = 0; i < head.length; i++) {
              c.fillText(head[i], x + pad, y + pad + i * f(14));
            }

            var bx = x + pad, by = y + pad + headH + f(6);
            c.font = f(11) + 'px' + fam;
            if (lg.kind === 'gradient') {
              var g = c.createLinearGradient(0, by + bodyH, 0, by);
              for (i = 0; i < lg.stops.length; i++) {
                g.addColorStop(i / (lg.stops.length - 1), lg.stops[i]);
              }
              c.fillStyle = g;
              c.fillRect(bx, by, sw, bodyH);
              c.strokeRect(bx, by, sw, bodyH);
              c.fillStyle = '#0d1b2a';
              var anchors = ['top', 'middle', 'bottom'];
              for (i = 0; i < lg.labels.length; i++) {
                c.textBaseline = anchors[i] || 'middle';
                c.fillText(lg.labels[i], bx + sw + gap,
                           by + (bodyH * i) / (lg.labels.length - 1));
              }
            } else {
              var rowH = f(17);
              for (i = 0; i < lg.colors.length; i++) {
                var ry = by + i * rowH;
                c.fillStyle = lg.colors[i];
                c.fillRect(bx, ry, sw, f(14));
                c.strokeRect(bx, ry, sw, f(14));
                c.fillStyle = '#0d1b2a';
                c.textBaseline = 'top';
                c.fillText(lg.labels[i], bx + sw + gap, ry + f(1));
              }
            }
          }

          // Basemap credit lives in the DOM, not the canvas - carry it
          // into the file so the imagery stays attributed.
          function drawCredit(c, W_, H_, s) {
            var b = box();
            var el = b && b.querySelector(
              '.mapboxgl-ctrl-attrib-inner, .maplibregl-ctrl-attrib-inner');
            var t = el ? (el.innerText || '').trim() : '';
            if (!t) { return; }
            c.font = (9 * s) + 'px "Source Sans Pro", Arial, sans-serif';
            var w = c.measureText(t).width + 8 * s;
            var h = 13 * s;
            c.fillStyle = 'rgba(255,255,255,0.72)';
            c.fillRect(W_ - w, H_ - h, w, h);
            c.fillStyle = '#3d4750';
            c.textBaseline = 'middle';
            c.fillText(t, W_ - w + 4 * s, H_ - h / 2);
          }

          function saveMap(tries) {
            var b = box();
            // The basemap canvas carries the tiles (and, on the Plotly
            // maps, the data itself); the deck.gl overlay, when there is
            // one, carries the mesh.  Anything else in the panel (Plotly's
            // spare regl canvases) must stay out of the composite.
            var base = b && b.querySelector(
              'canvas.mapboxgl-canvas, canvas.maplibregl-canvas');
            var deck = b && b.querySelector('#deckgl-overlay');
            var top = deck || base;
            // A map that has not been laid out yet still has a canvas, at
            // the library's placeholder size (400x300 / 300x150).  Saving
            // that would hand back an empty picture, so wait for a canvas
            // that actually fills the panel.
            var seen = top ? top.getBoundingClientRect().width : 0;
            var panel = b ? b.getBoundingClientRect().width : 0;
            if (!top || !top.width || seen < 100 || seen < panel * 0.5) {
              if ((tries || 0) < 4) {
                setTimeout(function () { saveMap((tries || 0) + 1); }, 600);
                return;
              }
              msg('Map is still drawing - scroll it into view, then save '
                  + 'again.');
              return;
            }
            var w = top.width, h = top.height;
            var cv = D.createElement('canvas');
            cv.width = w; cv.height = h;
            var c = cv.getContext('2d');
            c.imageSmoothingQuality = 'high';
            c.fillStyle = '#ffffff';
            c.fillRect(0, 0, w, h);
            var baseOk = true;
            if (base) {
              var gl = base.getContext('webgl2') || base.getContext('webgl');
              baseOk = !gl || gl.getContextAttributes().preserveDrawingBuffer;
              c.drawImage(base, 0, 0, w, h);
            }
            if (deck) { c.drawImage(deck, 0, 0, w, h); }
            var s = w / Math.max(1, top.getBoundingClientRect().width);
            drawLegend(c, w, h, s, CFG.legend);
            drawCredit(c, w, h, s);
            cv.toBlob(function (blob) {
              blob.arrayBuffer().then(function (buf) {
                var png = tagDpi(new Uint8Array(buf), CFG.dpi);
                save(new Blob([png], {type: 'image/png'}),
                     CFG.file + '.png');
                msg(baseOk
                  ? 'Saved ' + w + ' x ' + h + ' px at ' + CFG.dpi + ' dpi.'
                  : 'Saved, but the basemap came out blank - reload the '
                    + 'page, reopen the results, then save again.');
              });
            }, 'image/png');
          }

          btn.addEventListener('click', function () {
            btn.disabled = true;
            msg('Rendering…');
            try {
              saveMap(0);
            } catch (e) {
              msg('Could not save: ' + e.message);
            }
            setTimeout(function () { btn.disabled = false; }, 3000);
          });
        })();
        </script>
        """.replace("__CFG__", _cfg).replace("__DPI__", str(int(dpi))),
        height=46,
    )


# ── Source-location lookups for the Tab 3 locator map ────────────────
# Each returns (lon, lat) or None.  Cached for a day - a gauge does not
# move, and the map re-renders on every widget interaction.
_ESRI_TILES = {
    "Topographic": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Topo_Map/MapServer/tile/{z}/{y}/{x}"
    ),
    "Satellite": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}"
    ),
    "Streets": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Street_Map/MapServer/tile/{z}/{y}/{x}"
    ),
    "Light gray": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
    ),
}
# Public glyph (font) endpoint.  A raster-only style has no fonts of
# its own, and MapLibre silently drops every text label when it cannot
# load glyphs - which is what turns the numbered markers into blank
# dots.  Any family used in `textfont` must exist in this font stack.
_MAP_GLYPHS = "https://fonts.openmaptiles.org/{fontstack}/{range}.pbf"
_MAP_LABEL_FONT = "Open Sans Regular"


def _esri_style(basemap: str) -> dict:
    """MapLibre style JSON for one tokenless Esri raster basemap."""
    tiles = _ESRI_TILES.get(basemap) or _ESRI_TILES["Topographic"]
    return {
        "version": 8,
        "glyphs": _MAP_GLYPHS,
        "sources": {
            "esri": {
                "type": "raster",
                "tiles": [tiles],
                "tileSize": 256,
                "attribution": "Esri",
            }
        },
        "layers": [{"id": "esri", "type": "raster", "source": "esri"}],
    }


@st.cache_data(ttl=86400, show_spinner=False)
def _stofs_datum_offset(station_id: str, target: str):
    """Metres added to a STOFS series to put it on ``target``.

    Cached for a day - published tidal datums change only when NOAA
    re-computes the epoch.  ``None`` means the station has no such
    datum and the series is left on MSL.
    """
    if not str(station_id or "").strip():
        return None
    from forecast_client import noaa_datum_offset_m
    return noaa_datum_offset_m(station_id, target)


@st.cache_data(ttl=86400, show_spinner=False)
def _usgs_site_latlon_cached(site_no: str):
    """(lon, lat, name) for a USGS site number, via the NWIS site service.

    Raises on a failed lookup so the cache never memoises a transient
    network error; :func:`_usgs_site_latlon` is the catching wrapper.
    """
    import requests
    site_no = str(site_no).strip()
    r = requests.get(
        "https://waterservices.usgs.gov/nwis/site/",
        params={"format": "rdb", "sites": site_no},
        timeout=12,
    )
    r.raise_for_status()
    rows = [ln for ln in r.text.splitlines() if ln and not ln.startswith("#")]
    if len(rows) < 3:
        raise ValueError(f"USGS site {site_no} not found")
    rec = dict(zip(rows[0].split("\t"), rows[2].split("\t")))
    return (
        float(rec["dec_long_va"]), float(rec["dec_lat_va"]),
        rec.get("station_nm", site_no).strip(),
    )


@st.cache_data(ttl=86400, show_spinner=False)
def _noaa_station_latlon_cached(station_id: str):
    """(lon, lat, name) for a NOAA CO-OPS tide station."""
    import requests
    station_id = str(station_id).strip()
    r = requests.get(
        "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/"
        f"stations/{station_id}.json",
        timeout=12,
    )
    r.raise_for_status()
    rec = r.json()["stations"][0]
    name = f"{rec.get('name', station_id)}, {rec.get('state', '')}"
    return (float(rec["lng"]), float(rec["lat"]), name.strip(", "))


@st.cache_data(ttl=86400, show_spinner=False)
def _nwm_reach_latlon_cached(comid: str):
    """(lon, lat, name) for an NHDPlus COMID, via the NOAA NWPS reach API."""
    import requests
    r = requests.get(
        f"https://api.water.noaa.gov/nwps/v1/reaches/{str(comid).strip()}",
        timeout=12,
    )
    r.raise_for_status()
    j = r.json()
    return (
        float(j["longitude"]), float(j["latitude"]),
        j.get("name") or f"reach {comid}",
    )


def _usgs_site_latlon(site_no: str):
    """Uncached catcher - a failed lookup must not stick for a day."""
    if not str(site_no or "").strip():
        return None
    try:
        return _usgs_site_latlon_cached(site_no)
    except Exception:
        return None


def _noaa_station_latlon(station_id: str):
    """Uncached catcher - see :func:`_usgs_site_latlon`."""
    if not str(station_id or "").strip():
        return None
    try:
        return _noaa_station_latlon_cached(station_id)
    except Exception:
        return None


def _nwm_reach_latlon(comid: str):
    """Uncached catcher - see :func:`_usgs_site_latlon`."""
    if not str(comid or "").strip().isdigit():
        return None
    try:
        return _nwm_reach_latlon_cached(comid)
    except Exception:
        return None



@st.cache_data(ttl=86400, show_spinner=False)
def _nldi_reach_geometry(comid: str):
    """The NHDPlus flowline for one COMID as a [[lon, lat], …] polyline.

    NLDI serves the reach's real geometry, which is what lets the map
    put the marker on the part of the reach nearest the boundary rather
    than on the reach's midpoint.
    """
    import requests
    r = requests.get(
        "https://api.water.usgs.gov/nldi/linked-data/comid/"
        f"{str(comid).strip()}",
        params={"f": "json"},
        timeout=12,
    )
    r.raise_for_status()
    geom = r.json()["features"][0]["geometry"]
    if geom["type"] == "LineString":
        parts = [geom["coordinates"]]
    else:  # MultiLineString
        parts = geom["coordinates"]
    pts = [(float(x), float(y)) for part in parts for x, y in part]
    if len(pts) < 2:
        raise ValueError(f"COMID {comid} has no usable geometry")
    return pts


def _nearest_point_on_line(pts, lon0, lat0):
    """Point on the polyline ``pts`` closest to (lon0, lat0).

    Works in a local equirectangular frame (longitude squeezed by
    cos(lat)), which is accurate well past the length of any single
    NHDPlus reach and avoids pulling in a projection library.
    """
    import math
    k = math.cos(math.radians(lat0)) or 1e-6

    def _xy(lon, lat):
        return ((lon - lon0) * k, lat - lat0)

    best = None
    for (alon, alat), (blon, blat) in zip(pts, pts[1:]):
        ax, ay = _xy(alon, alat)
        bx, by = _xy(blon, blat)
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(
            0.0, min(1.0, -(ax * dx + ay * dy) / seg)
        )
        px, py = ax + t * dx, ay + t * dy
        d2 = px * px + py * py
        if best is None or d2 < best[0]:
            best = (
                d2,
                (alon + t * (blon - alon), alat + t * (blat - alat)),
            )
    return best[1]


def _nwm_reach_point(comid: str, bc_lon, bc_lat):
    """(lon, lat, name, exact) for the reach a forecast BC is drawn from.

    ``exact`` is True when the point is the spot on the reach nearest
    the boundary (NLDI geometry), False when only the NWPS
    representative point - roughly the reach midpoint - was available.
    """
    if not str(comid or "").strip().isdigit():
        return None
    named = _nwm_reach_latlon(comid)
    name = named[2] if named else f"reach {comid}"
    if bc_lon is not None and bc_lat is not None:
        try:
            pts = _nldi_reach_geometry(comid)
            lon, lat = _nearest_point_on_line(pts, bc_lon, bc_lat)
            return (lon, lat, name, True)
        except Exception:
            pass
    if named is None:
        return None
    return (named[0], named[1], name, False)


def _bc_source_points(bc_lines: list[dict]) -> dict:
    """Map BC row index → the location of the source it is assigned to.

    Reads the *live* Tab 3 widget state (``bc_src_<i>`` and the
    per-source ID fields), so the marker follows the dropdown as soon
    as the user changes it.  Returns
    ``{i: {"lon", "lat", "label", "kind"}}`` for every boundary whose
    source has a resolvable location; boundaries left on Constant /
    Leave unchanged simply do not appear.
    """
    out: dict = {}
    for i, _bc in enumerate(bc_lines):
        src = str(st.session_state.get(f"bc_src_{i}", "") or "")
        hit = None
        kind = ""
        if src == "USGS":
            hit = _usgs_site_latlon(st.session_state.get(f"bc_st_{i}", ""))
            kind = "USGS gauge"
        elif src == "NOAA":
            hit = _noaa_station_latlon(st.session_state.get(f"bc_st_{i}", ""))
            kind = "NOAA station"
        elif src == "Forecast (STOFS)":
            hit = _noaa_station_latlon(st.session_state.get(f"bc_st_{i}", ""))
            kind = "STOFS station"
        elif src == "Forecast (NWM v.3)":
            # Only a *stage* BC routes NWM Q through a rating curve, and
            # only Path A has a gauge - show that gauge, since it is
            # what sets the stage the engine sees.  A flow BC has no
            # rating gauge at all, so never read `bc_st_<i>` for one:
            # that key is shared with the USGS/NOAA sources and can
            # still hold a stale station from the previously selected
            # source, which would pin the marker to the wrong place.
            if str(_bc.get("bc_type", "")).lower() == "stage":
                _site = str(
                    st.session_state.get(f"bc_st_{i}", "") or ""
                ).strip()
                if _site:
                    hit = _usgs_site_latlon(_site)
                    kind = "USGS rating gauge"
            if hit is None:
                _reach = _nwm_reach_point(
                    st.session_state.get(f"bc_fc_comid_{i}", ""),
                    _bc.get("lon"), _bc.get("lat"),
                )
                if _reach:
                    # Marker goes on the point of the reach nearest the
                    # boundary.  Only when NLDI has no geometry does it
                    # fall back to the NWPS representative point, which
                    # is roughly the reach midpoint and can sit a few km
                    # away on a long reach - labelled so, and exempt
                    # from the "too far" note below.
                    hit = _reach[:3]
                    kind = (
                        "NWM reach" if _reach[3] else "NWM reach midpoint"
                    )
        if hit:
            out[i] = {
                "lon": hit[0], "lat": hit[1],
                "label": f"{hit[2]}", "kind": kind,
            }
    return out


# ── "What sources exist near my boundaries?" (Tab 3 map layer) ───────
# Purely advisory: it draws what is out there and lists the IDs so they
# can be typed into the fields below.  Nothing is applied automatically
# - picking a source stays the user's call.
USGS_PARAM_LABELS = {"00060": "discharge", "00065": "gage height"}


@st.cache_data(ttl=3600, show_spinner=False)
def _usgs_sites_in_box_cached(lon: float, lat: float, radius_km: float):
    """Active USGS stream gauges near a point, tagged by parameter.

    Queried per parameter code so each hit can say whether it carries
    discharge, stage, or both - a stage-only gauge cannot drive a flow
    boundary, and the map should say so rather than just show a dot.

    Raises on a failed query rather than returning what it managed to
    collect.  The NWIS site service throws intermittent 503s, and a
    half-answer cached for an hour reads exactly like "there is nothing
    here" - which is how a gauge 1.2 km from a boundary went missing
    when the radius was widened.
    """
    import math
    import time
    import requests
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 1e-6))
    bbox = (
        f"{lon - dlon:.6f},{lat - dlat:.6f},"
        f"{lon + dlon:.6f},{lat + dlat:.6f}"
    )
    found: dict = {}
    for pcode in USGS_PARAM_LABELS:
        last = None
        for attempt in range(3):
            try:
                r = requests.get(
                    "https://waterservices.usgs.gov/nwis/site/",
                    params={
                        "format": "rdb", "bBox": bbox, "siteType": "ST",
                        "parameterCd": pcode, "hasDataTypeCd": "iv",
                        "siteStatus": "active",
                    },
                    timeout=20,
                )
            except Exception as e:
                last = str(e)
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 200:
                last = None
                break
            # 404 is NWIS's "no sites match", not a failure.
            if r.status_code == 404:
                last = None
                r = None
                break
            last = f"HTTP {r.status_code}"
            time.sleep(1.5 * (attempt + 1))
        if last is not None:
            raise RuntimeError(
                f"USGS site service: {last} for parameter {pcode}"
            )
        if r is None:
            continue
        rows = [
            ln for ln in r.text.splitlines()
            if ln and not ln.startswith("#")
        ]
        if len(rows) < 3:
            continue
        head = rows[0].split("\t")
        for ln in rows[2:]:
            rec = dict(zip(head, ln.split("\t")))
            try:
                sid = rec["site_no"].strip()
                slon = float(rec["dec_long_va"])
                slat = float(rec["dec_lat_va"])
            except Exception:
                continue
            hit = found.setdefault(sid, {
                "id": sid, "kind": "USGS",
                "name": rec.get("station_nm", sid).strip(),
                "lon": slon, "lat": slat, "params": [],
            })
            hit["params"].append(USGS_PARAM_LABELS[pcode])
    return list(found.values())


@st.cache_data(ttl=86400, show_spinner=False)
def _noaa_station_list():
    """Every NOAA CO-OPS water-level station: id, name, position, SHEF."""
    import requests
    r = requests.get(
        "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/"
        "stations.json",
        params={"type": "waterlevels"},
        timeout=40,
    )
    r.raise_for_status()
    out = []
    for st_ in r.json().get("stations", []):
        try:
            out.append({
                "id": str(st_["id"]), "kind": "NOAA",
                "name": (
                    f"{st_.get('name', '')}, {st_.get('state', '')}"
                ).strip(", "),
                "lon": float(st_["lng"]), "lat": float(st_["lat"]),
                "shef": str(st_.get("shefcode") or "").upper(),
            })
        except Exception:
            continue
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def _stofs_station_handles(domain: str = "atlantic"):
    """SHEF handles STOFS-3D actually writes, for the newest cycle.

    The station file's SHEF sibling is ~300 KB, so this is a cheap way
    to tell a plain tide gauge from one STOFS also outputs at - not
    every CO-OPS station is a STOFS point.
    """
    import re
    from datetime import date, timedelta as _td
    import requests
    stem = "stofs_3d_atl" if domain == "atlantic" else "stofs_3d_pac"
    prefix = "STOFS-3D-Atl" if domain == "atlantic" else "STOFS-3D-Pac"
    for back in range(0, 4):
        d = date.today() - _td(days=back)
        url = (
            f"https://noaa-nos-stofs3d-pds.s3.amazonaws.com/{prefix}/"
            f"{stem}.{d:%Y%m%d}/{stem}.t12z.points.cwl.shef"
        )
        try:
            r = requests.get(url, timeout=30)
            if r.status_code != 200:
                continue
            return set(re.findall(r"^\.E\s+(\w+)\s", r.text, re.M))
        except Exception:
            continue
    return set()


def _nearby_sources(bc_lines: list[dict], radius_km: float):
    """Candidate sources within ``radius_km`` of each boundary.

    Returns ``(candidates, problems)``: ``{bc_index: [candidate, …]}``
    plus a list of boundaries whose lookup failed.  Each candidate
    carries its id, kind, name, position and distance, and a NOAA
    station that STOFS also writes is marked - the one thing the
    station list itself cannot tell you.

    A failed lookup is reported, never rendered as an empty result: an
    upstream service having a bad minute must not look like "no gauges
    near this boundary".
    """
    try:
        noaa = _noaa_station_list()
    except Exception:
        noaa = []
    try:
        handles = (
            _stofs_station_handles("atlantic")
            | _stofs_station_handles("pacific")
        )
    except Exception:
        handles = set()
    out: dict = {}
    problems: list = []
    for i, bc in enumerate(bc_lines):
        lon, lat = bc.get("lon"), bc.get("lat")
        if lon is None or lat is None:
            continue
        hits = []
        try:
            sites = _usgs_sites_in_box_cached(lon, lat, radius_km)
        except Exception as e:
            problems.append((i, str(e)))
            sites = []
        for site in sites:
            d = _haversine_km(lon, lat, site["lon"], site["lat"])
            if d <= radius_km:
                hits.append(dict(site, dist=d))
        for st_ in noaa:
            d = _haversine_km(lon, lat, st_["lon"], st_["lat"])
            if d <= radius_km:
                hits.append(dict(
                    st_, dist=d,
                    stofs=bool(st_.get("shef") and st_["shef"] in handles),
                ))
        if hits:
            out[i] = sorted(hits, key=lambda h: h["dist"])
    return out, problems


def _dashed_circle(lon0: float, lat0: float, radius_km: float,
                   dashes: int = 54, duty: float = 0.55):
    """Lon/lat arrays tracing a dashed circle of ``radius_km``.

    Plotly's mapbox line traces carry no dash property, so the gaps are
    made by breaking one trace into arcs with ``None`` between them.
    Positions use a local equirectangular approximation, which is well
    inside a pixel at the radii this control allows.
    """
    import math
    if not radius_km or radius_km <= 0:
        return [], []
    k_lat = radius_km / 111.32
    k_lon = radius_km / (111.32 * max(math.cos(math.radians(lat0)), 1e-6))
    lons: list = []
    lats: list = []
    step = 2 * math.pi / dashes
    for d in range(dashes):
        a0 = d * step
        for j in range(7):
            a = a0 + step * duty * (j / 6.0)
            lons.append(lon0 + k_lon * math.sin(a))
            lats.append(lat0 + k_lat * math.cos(a))
        lons.append(None)
        lats.append(None)
    return lons, lats


def _bc_location_map(bc_lines, geom, sources=None, basemap="Topographic",
                     candidates=None, radius_km=0.0):
    """Locator map: domain outline, numbered BC markers, and sources.

    Each red numbered dot is a boundary (the number matches its block
    below).  When a boundary has a data source with a known location -
    a USGS gauge, a NOAA tide station, an NWM reach - a blue marker is
    drawn for it and a dashed line ties it to the boundary it feeds, so
    you can see at a glance whether the gauge you picked is actually
    near that inflow.  Returns a Plotly figure, or ``None`` when no BC
    has coordinates (older/text-only scans) so the caller can skip it.

    The basemap is an Esri raster service, which needs no token.  (The
    CARTO styles Plotly ships as built-ins now serve "API KEY
    REQUIREMENTS" placeholder tiles.)
    """
    import math
    import plotly.graph_objects as _go
    outline = (geom or {}).get("outline") or []
    dots = [
        (i, b) for i, b in enumerate(bc_lines)
        if b.get("lon") is not None and b.get("lat") is not None
    ]
    if not dots:
        return None

    fig = _go.Figure()
    all_lon: list = []
    all_lat: list = []
    for _r, ring in enumerate(outline):
        lons = [p[0] for p in ring] + [ring[0][0]]
        lats = [p[1] for p in ring] + [ring[0][1]]
        fig.add_trace(_go.Scattermapbox(
            lon=lons, lat=lats, mode="lines",
            line=dict(color="#3c78af", width=2),
            # Wash the mesh footprint in its own blue so the modelled
            # area reads at a glance - a boundary or a candidate gauge
            # outside the shading is outside the model.
            fill="toself",
            fillcolor="rgba(60, 120, 175, 0.16)",
            name="2D domain",
            hoverinfo="skip",
            # One legend entry no matter how many rings the mesh has.
            showlegend=(_r == 0),
        ))
        all_lon += lons
        all_lat += lats

    # --- search radius, so the number in the box has a shape ----------
    if radius_km and radius_km > 0:
        _rlon: list = []
        _rlat: list = []
        for _i, _b in dots:
            _cl, _ca = _dashed_circle(_b["lon"], _b["lat"], radius_km)
            _rlon += _cl
            _rlat += _ca
        if _rlon:
            fig.add_trace(_go.Scattermapbox(
                lon=_rlon, lat=_rlat, mode="lines",
                line=dict(color="#c2610a", width=1.6),
                name=f"{radius_km:g} km search radius",
                hoverinfo="skip", showlegend=True,
            ))
            all_lon += [v for v in _rlon if v is not None]
            all_lat += [v for v in _rlat if v is not None]

    # --- nearby-source suggestions (advisory layer) -------------------
    # Drawn first so it sits under the boundaries and their assigned
    # sources: these are candidates to read off, not selections.
    _cand = []
    for _i, _lst in (candidates or {}).items():
        for _c in _lst:
            _cand.append((_i, _c))
    if _cand:
        fig.add_trace(_go.Scattermapbox(
            lon=[c["lon"] for _, c in _cand],
            lat=[c["lat"] for _, c in _cand],
            mode="markers",
            marker=dict(size=11, color="#f5a623"),
            name="Nearby source",
            hovertext=[
                f"{c['kind']} {c['id']} · {c['name']}"
                + (" · " + " + ".join(c["params"]) if c.get("params") else "")
                + (" · also a STOFS point" if c.get("stofs") else "")
                + f" · {c['dist']:.2f} km from boundary {i + 1}"
                for i, c in _cand
            ],
            hoverinfo="text", showlegend=True,
        ))
        all_lon += [c["lon"] for _, c in _cand]
        all_lat += [c["lat"] for _, c in _cand]

    # --- source markers + BC→source connectors ------------------------
    sources = sources or {}
    _src_rows = [
        (i, b, sources[i]) for i, b in dots if i in sources
    ]
    for i, b, sp in _src_rows:
        fig.add_trace(_go.Scattermapbox(
            lon=[b["lon"], sp["lon"]], lat=[b["lat"], sp["lat"]],
            mode="lines",
            line=dict(color="#1565c0", width=1),
            hoverinfo="skip", showlegend=False,
        ))
    if _src_rows:
        all_lon += [sp["lon"] for _, _, sp in _src_rows]
        all_lat += [sp["lat"] for _, _, sp in _src_rows]

    _dlon = [b["lon"] for _, b in dots]
    _dlat = [b["lat"] for _, b in dots]
    _labels = [str(i + 1) for i, _ in dots]
    _hover = [
        f"{i + 1}. "
        f"{b['name'].replace('2D: ', '').replace(' BCLine:', ' -')}"
        f" · {b.get('bc_type', '?')}"
        for i, b in dots
    ]
    fig.add_trace(_go.Scattermapbox(
        lon=_dlon, lat=_dlat, mode="markers+text",
        marker=dict(size=22, color="#e53935"),
        text=_labels,
        textfont=dict(color="white", size=11, family=_MAP_LABEL_FONT),
        textposition="middle center",
        name="Boundary",
        hovertext=_hover, hoverinfo="text", showlegend=True,
    ))
    all_lon += _dlon
    all_lat += _dlat

    # Source markers go on TOP of the boundary markers, smaller and
    # ringed in white.  A source can legitimately sit within metres of
    # its boundary - an NWM reach point does - and underneath the
    # larger boundary dot it would be invisible; drawn this way it
    # reads as a blue centre inside the red ring.
    if _src_rows:
        _slon = [sp["lon"] for _, _, sp in _src_rows]
        _slat = [sp["lat"] for _, _, sp in _src_rows]
        _shover = []
        for i, b, sp in _src_rows:
            _km = _haversine_km(b["lon"], b["lat"], sp["lon"], sp["lat"])
            _sep = (
                f"{_km * 1000:.0f} m" if _km < 1 else f"{_km:.2f} km"
            )
            _shover.append(
                f"{i + 1}. {sp['kind']}: {sp['label']} · {_sep} from the "
                f"boundary"
            )
        fig.add_trace(_go.Scattermapbox(
            lon=_slon, lat=_slat, mode="markers",
            marker=dict(size=16, color="white"),
            hoverinfo="skip", showlegend=False,
        ))
        fig.add_trace(_go.Scattermapbox(
            lon=_slon, lat=_slat, mode="markers+text",
            marker=dict(size=13, color="#1565c0"),
            text=[str(i + 1) for i, _, _ in _src_rows],
            textfont=dict(color="white", size=9, family=_MAP_LABEL_FONT),
            textposition="middle center",
            name="Assigned source",
            hovertext=_shover, hoverinfo="text", showlegend=True,
        ))

    lon_min, lon_max = min(all_lon), max(all_lon)
    lat_min, lat_max = min(all_lat), max(all_lat)
    center = dict(
        lon=(lon_min + lon_max) / 2.0, lat=(lat_min + lat_max) / 2.0,
    )
    span = max(lon_max - lon_min, lat_max - lat_min, 1e-3)
    zoom = max(3.0, min(15.0, math.log2(360.0 / (span * 1.5)) - 0.3))
    fig.update_layout(
        mapbox=dict(
            style=_esri_style(basemap),
            center=center,
            zoom=zoom,
        ),
        height=320, margin=dict(l=0, r=0, t=0, b=0),
        showlegend=True,
        legend=dict(
            x=0.008, y=0.985,
            xanchor="left", yanchor="top",
            bgcolor="rgba(255,255,255,0.88)",
            bordercolor="#c8ccd4", borderwidth=1,
            font=dict(size=10),
            # A legend on a two-item map is a key, not a control -
            # clicking an entry must not hide the boundaries.
            itemclick=False, itemdoubleclick=False,
        ),
    )
    return fig


def _haversine_km(lon1, lat1, lon2, lat2) -> float:
    """Great-circle distance in km between two lon/lat pairs."""
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


# Tokenless basemap styles for the pydeck (GPU) map.  Streamlit's
# DeckGlJsonChart requires ``map_style`` to be a URL string (a style
# dict crashes its frontend), so the raster basemaps (Satellite /
# Terrain) are MapLibre style JSONs served by Streamlit itself from
# ``src/static/`` (needs ``server.enableStaticServing``).  Their raster
# tile sources give full-resolution imagery at every zoom, no token.
# NOTE: the /app/static/ paths are origin-relative - they break only if
# the app is deployed under a non-root ``server.baseUrlPath``.
_PDK_STYLES = {
    "Light": "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    "Dark": "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
    "Streets": "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
    "Terrain": "/app/static/terrain-style.json",
    "Satellite": "/app/static/satellite-style.json",
}


@st.cache_data(show_spinner="Preparing 3-D surface…", max_entries=6)
def _smooth_terrain_assets(
    npz_path: str, _mtime: float,
    texture_png: str, _tex_mtime: float,
    corners: tuple,
) -> tuple | None:
    """Build the deck.gl TerrainLayer inputs for the Smooth 3-D view.

    Encodes the physical **peak** surface - terrain, replaced by the
    peak water-surface elevation where wet - into a 16-bit-in-RGB
    terrain PNG (elevation relative to the mesh-lowest terrain, in
    0.1-model-unit steps: ``elev = R*25.6 + G*0.1``), and composites
    the smooth texture PNG over a neutral ground tint.  Both files are
    written under ``src/static/`` (served by Streamlit) and returned as
    ``(elevation_url, texture_url, (west, south, east, north))``.
    Returns ``None`` when the surface cannot be built.
    """
    import hashlib
    import numpy as _np
    from pathlib import Path as _Path
    from PIL import Image

    d = _np.load(npz_path, allow_pickle=True)
    if "min_elev" not in d.files or "proj_wkt" not in d.files:
        return None
    wse = _np.asarray(d["wse"], dtype=_np.float32)
    min_el = _np.asarray(d["min_elev"], dtype=_np.float32)
    coords = d["coords"]
    try:
        from pyproj import Transformer
        tf = Transformer.from_crs(
            str(d["proj_wkt"][0]), "EPSG:4326", always_xy=True
        )
        lon, lat = tf.transform(coords[:, 0], coords[:, 1])
        lon, lat = _np.asarray(lon), _np.asarray(lat)
    except Exception:
        return None

    peak = _np.nanmax(wse, axis=0)
    ok = (
        _np.isfinite(min_el) & _np.isfinite(lon) & _np.isfinite(lat)
    )
    if ok.sum() < 3:
        return None

    # Texture bbox (the smooth PNG's mapbox corner coordinates)
    _lons = [c[0] for c in corners]
    _lats = [c[1] for c in corners]
    west, east = min(_lons), max(_lons)
    south, north = min(_lats), max(_lats)

    # TIN of terrain and peak WSE evaluated on the texture's grid
    import math as _math
    from matplotlib.tri import Triangulation, LinearTriInterpolator
    try:
        tri = Triangulation(lon[ok], lat[ok])
        terr_i = LinearTriInterpolator(tri, min_el[ok])
        wse_i = LinearTriInterpolator(tri, _np.where(
            _np.isfinite(peak[ok]), peak[ok], min_el[ok]
        ))
    except Exception:
        return None
    W = 1024
    _my = lambda la: _math.log(_math.tan(
        _math.pi / 4.0 + _math.radians(la) / 2.0
    ))
    H = int(min(2048, max(64, round(
        W * (_my(north) - _my(south))
        / max(_math.radians(east - west), 1e-9)
    ))))
    gx, gy = _np.meshgrid(
        _np.linspace(west, east, W),
        _np.linspace(north, south, H),   # row 0 = north (image top)
    )
    tz = terr_i(gx, gy)
    wz = wse_i(gx, gy)
    tz = _np.ma.filled(tz, _np.nan)
    wz = _np.ma.filled(wz, _np.nan)
    z = _np.where(wz - tz > 0.01, wz, tz)

    # Fill outside-hull pixels by NaN dilation (no scipy in the image)
    for _ in range(80):
        m = _np.isnan(z)
        if not m.any():
            break
        p = _np.pad(z, 1, constant_values=_np.nan)
        with _np.errstate(all="ignore"):
            import warnings as _warn
            with _warn.catch_warnings():
                _warn.simplefilter("ignore")
                nb = _np.nanmean(_np.stack([
                    p[1:-1, :-2], p[1:-1, 2:],
                    p[:-2, 1:-1], p[2:, 1:-1],
                ]), axis=0)
        z[m] = nb[m]
    datum = float(_np.nanmin(min_el[ok]))
    z = _np.nan_to_num(z, nan=datum)

    # Encode relative elevation in 0.1-unit steps across R (hi) + G (lo)
    v = _np.clip((z - datum) / 0.1, 0.0, 65535.0).astype(_np.uint16)
    rgb = _np.zeros((H, W, 3), dtype=_np.uint8)
    rgb[..., 0] = (v >> 8).astype(_np.uint8)
    rgb[..., 1] = (v & 255).astype(_np.uint8)

    static_dir = _Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    tag = hashlib.md5(
        f"{npz_path}|{_mtime}|{texture_png}|{_tex_mtime}".encode()
    ).hexdigest()[:10]
    elev_name = f"s3d_elev_{tag}.png"
    tex_name = f"s3d_tex_{tag}.png"
    Image.fromarray(rgb).save(static_dir / elev_name)
    # Keep the smooth raster's transparency: outside the mesh footprint
    # the texture is transparent, so deck.gl's TerrainLayer shows the
    # basemap through the flat apron instead of a solid grey table.
    # Resample to the elevation grid so texel/vertex alignment is exact.
    tex = Image.open(texture_png).convert("RGBA").resize(
        (W, H), Image.BILINEAR
    )
    tex.save(static_dir / tex_name)
    # Prune stale generated files (keep the most recent dozen)
    _old = sorted(
        static_dir.glob("s3d_*.png"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for _p in _old[12:]:
        try:
            _p.unlink()
        except OSError:
            pass
    return (
        f"/app/static/{elev_name}", f"/app/static/{tex_name}",
        (west, south, east, north),
    )


# XYZ tile URLs for the GIF basemap (match the inundation-map options).
_GIF_BASEMAPS = {
    "Light": "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    "Streets": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "Dark": "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
    "Terrain": "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
    "Satellite": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}"
    ),
}


def _fetch_basemap_mosaic(west, south, east, north, provider_url):
    """Fetch & stitch XYZ map tiles covering a lon/lat bbox.

    Returns ``(rgb_array, (left, right, bottom, top))`` in EPSG:3857
    metres, or ``None`` on any failure.
    """
    import io
    import math
    import numpy as _np
    import requests
    from PIL import Image

    R = 20037508.342789244

    def _deg2tile(lon, lat, z):
        n = 2 ** z
        xt = (lon + 180.0) / 360.0 * n
        yt = (1.0 - math.asinh(
            math.tan(math.radians(lat))) / math.pi) / 2.0 * n
        return xt, yt

    # Pick the highest zoom that keeps the mosaic <= 64 tiles (8×8 →
    # ~2048 px): a sharper basemap for the MP4 so it reads closer to the
    # crisp interactive map (was 16 tiles → soft on wide domains).
    zoom = 10
    for z in range(19, 3, -1):
        x0, y0 = _deg2tile(west, north, z)
        x1, y1 = _deg2tile(east, south, z)
        if (int(x1) - int(x0) + 1) * (int(y1) - int(y0) + 1) <= 64:
            zoom = z
            break

    z = zoom
    n = 2 ** z
    x0, y0 = _deg2tile(west, north, z)
    x1, y1 = _deg2tile(east, south, z)
    xi0, xi1 = max(0, int(x0)), min(n - 1, int(x1))
    yi0, yi1 = max(0, int(y0)), min(n - 1, int(y1))

    nx, ny = xi1 - xi0 + 1, yi1 - yi0 + 1
    mosaic = Image.new("RGB", (nx * 256, ny * 256), "#e8edf1")
    headers = {"User-Agent": "HECinBOX/2.2 flood-gif"}
    for xi in range(xi0, xi1 + 1):
        for yi in range(yi0, yi1 + 1):
            url = provider_url.format(z=z, x=xi, y=yi)
            r = requests.get(url, headers=headers, timeout=12)
            r.raise_for_status()
            tile = Image.open(io.BytesIO(r.content)).convert("RGB")
            mosaic.paste(tile, ((xi - xi0) * 256, (yi - yi0) * 256))

    tile_w = 2 * R / n
    left = -R + xi0 * tile_w
    right = -R + (xi1 + 1) * tile_w
    top = R - yi0 * tile_w
    bottom = R - (yi1 + 1) * tile_w
    return _np.asarray(mosaic), (left, right, bottom, top)


@st.cache_data(show_spinner="Rendering flood animation…", max_entries=3)
def _build_flood_gif(
    npz_path: str, _mtime: float, variable: str,
    n_frames: int, fps: int,
    map_style: str = "Filled cells",
    basemap: str = "Light",
    scope: str = "Whole domain",
    fmt: str = "mp4",
) -> bytes | None:
    """Render a flood-propagation animation that mirrors the inundation map.

    Layout: a single axes fills the whole figure, so the map uses every
    pixel. The title and colourbar are placed **inside** the map area
    (with semi-transparent backings for legibility on satellite tiles),
    maximising the animation footprint.

    Frames are rendered via Matplotlib's Agg canvas.  ``fmt="mp4"`` (the
    default) encodes them with ffmpeg into a **full-colour, seekable**
    H.264 video - far higher quality than a 256-colour GIF, and it plays
    in an HTML5 player with native play/pause/scrub.  ``fmt="gif"`` keeps
    the legacy looping GIF (255-colour, for slides).  Returns ``None`` if
    there is nothing to animate.
    """
    import tempfile
    import numpy as _np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as _plt
    from matplotlib.collections import PolyCollection
    from PIL import Image
    import pandas as _pd

    d = _np.load(npz_path, allow_pickle=True)
    wse = _np.asarray(d["wse"], dtype=_np.float32)
    coords = d["coords"]
    mt = _pd.to_datetime(d["model_time"])
    # Frame labels carry the clock too (v4.8.0), so a video shared with
    # a stakeholder is not a sequence of unlabelled wall-clock strings.
    from timebase import TimeBase as _GTimeBase
    _gtb = _GTimeBase.from_npz(d)
    if not _gtb.known:
        _gtb = _GTimeBase.from_dir(Path(npz_path).parent)
    mt_lbl = _gtb.to_lst(mt)
    _clock_tag = f"  ({_gtb.lst_label})" if _gtb.known else ""
    has_depth = "min_elev" in d.files
    from units import is_si, unit_labels
    _u = unit_labels(is_si(
        str(d["unit_system"][0]) if "unit_system" in d.files else None
    ))

    # Variable array + colour ramp (depth uses the shared natural water
    # ramp so the GIF matches the static maps).
    from raster_render import water_cmap
    if variable == "Velocity" and "vel" in d.files:
        arr = _np.asarray(d["vel"], dtype=_np.float32)
        label, cmap = f"Velocity ({_u['velocity']})", "turbo"
    elif variable == "Water Depth" and has_depth:
        arr = _np.clip(wse - d["min_elev"], 0.0, None)
        label, cmap = f"Depth ({_u['length']})", water_cmap()
    else:
        arr = wse
        label, cmap = f"WSE ({_u['length']})", "viridis"

    # Wet-cell mask (depth-based - robust for steady rivers)
    if variable == "Water Depth":
        wet = _np.nanmax(arr, axis=0) > 0.05
    elif variable == "Velocity":
        wet = _np.nanmax(arr, axis=0) > 0.01
    elif has_depth:
        wet = _np.nanmax(wse - d["min_elev"], axis=0) > 0.01
    else:
        wet = _np.nanstd(wse, axis=0) > 0.01
    finite = _np.isfinite(_np.nanmax(arr, axis=0))
    wet = wet & finite

    # Cell scope - honour the inundation-map toggle
    show = wet if scope == "Wetted only" else finite
    ids = _np.nonzero(show)[0]
    if ids.size == 0:
        return None

    # Reproject model CRS → Web Mercator (EPSG:3857) + lon/lat
    wkt = str(d["proj_wkt"][0]) if "proj_wkt" in d.files else ""
    merc_ok = False
    tf = None
    if wkt:
        try:
            from pyproj import Transformer
            tf = Transformer.from_crs(wkt, "EPSG:3857", always_xy=True)
            cx, cy = tf.transform(coords[:, 0], coords[:, 1])
            cx, cy = _np.asarray(cx), _np.asarray(cy)
            tf_ll = Transformer.from_crs(
                wkt, "EPSG:4326", always_xy=True
            )
            lon, lat = tf_ll.transform(coords[:, 0], coords[:, 1])
            lon, lat = _np.asarray(lon), _np.asarray(lat)
            merc_ok = True
        except Exception:
            merc_ok = False
    if not merc_ok:
        cx = coords[:, 0].astype(float)
        cy = coords[:, 1].astype(float)

    n_steps = arr.shape[0]
    n_frames = int(max(2, min(n_frames, n_steps)))
    fidx = _np.linspace(0, n_steps - 1, n_frames).astype(int)
    sub = arr[fidx][:, ids]
    vmin = float(_np.nanmin(sub))
    vmax = float(_np.nanmax(sub))
    # When animating the Smooth style, use the SAME colour range the
    # static smooth map shows (the robust 1-99 % range recorded in
    # smooth_maps.json) so the GIF and the map above read identically -
    # the raw frame min/max is dominated by outlier cells.
    if map_style.startswith("Smooth"):
        try:
            import json as _json0
            from pathlib import Path as _Path0
            _sv = _json0.loads(
                (_Path0(npz_path).parent / "smooth_maps.json").read_text()
            ).get("variables", {}).get(variable)
            if _sv:
                vmin = float(_sv["vmin"])
                vmax = float(_sv["vmax"])
        except Exception:
            pass
    if vmax <= vmin:
        vmax = vmin + 1e-6

    # Bounding box (with a small margin) of the shown cells
    xmin, xmax = float(cx[ids].min()), float(cx[ids].max())
    ymin, ymax = float(cy[ids].min()), float(cy[ids].max())
    padx = (xmax - xmin) * 0.04 + 1.0
    pady = (ymax - ymin) * 0.04 + 1.0
    xmin, xmax = xmin - padx, xmax + padx
    ymin, ymax = ymin - pady, ymax + pady

    aspect = (xmax - xmin) / max(1e-6, ymax - ymin)
    # Match the figure aspect to the data aspect so the equal-aspect map
    # fills the frame edge-to-edge - no white margins around the video.
    if aspect >= 1.0:
        fw, fh = 12.0, 12.0 / aspect
    else:
        fw, fh = 12.0 * aspect, 12.0
    _plt.rcParams.update({"font.size": 11})
    fig = _plt.figure(figsize=(fw, fh), dpi=120)
    fig.patch.set_facecolor("#eef3f7")
    # Single axes filling the *entire* figure - zero whitespace
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.set_facecolor("#eef3f7")

    # Basemap tiles behind the mesh
    if merc_ok:
        try:
            west = float(_np.nanmin(lon[ids]))
            east = float(_np.nanmax(lon[ids]))
            south = float(_np.nanmin(lat[ids]))
            north = float(_np.nanmax(lat[ids]))
            mosaic = _fetch_basemap_mosaic(
                west, south, east, north,
                _GIF_BASEMAPS.get(basemap, _GIF_BASEMAPS["Light"]),
            )
            if mosaic is not None:
                _img, _ext = mosaic
                ax.imshow(_img, extent=_ext, origin="upper", zorder=0)
        except Exception:
            pass

    # ── True sub-grid smooth animation ────────────────────────────────
    # For "Smooth (peak)" style, render each frame on the terrain pixel
    # grid: depth(t) = WSE(t, containing cell) − terrain per pixel, and
    # WSE/velocity via a per-frame TIN - the animated twin of the static
    # smooth map.  Falls back to filled cells when the model terrain is
    # unavailable (e.g. the model folder moved since the run).
    artist = None
    _frame_field = None
    if map_style.startswith("Smooth") and merc_ok:
        try:
            import json as _json
            from pathlib import Path as _Path
            from raster_render import prepare_subgrid
            _meta = _json.loads(
                (_Path(npz_path).parent / "run_meta.json").read_text()
            )
            _mdl = _meta.get("model_dir", "")
            _prep = (
                prepare_subgrid(_mdl, d["cell_fp"], d["fp_xy"],
                                target_px=1600)
                if _mdl and _Path(_mdl).exists() else None
            )
            if _prep is not None:
                _dem_g, _cidx_g, (_g0, _gpx, _g3, _gpy), _ = _prep
                # 3857-aligned pixel grid over the view bbox, sampled
                # back into the model-CRS DEM grid (nearest pixel).
                _gW = 1300
                _gH = max(220, int(round(_gW / max(0.3, aspect))))
                _gx = _np.linspace(xmin, xmax, _gW)
                _gy = _np.linspace(ymax, ymin, _gH)   # top → bottom
                _tf_inv = Transformer.from_crs(
                    "EPSG:3857", wkt, always_xy=True
                )
                _XX, _YY = _np.meshgrid(_gx, _gy)
                _Xm, _Ym = _tf_inv.transform(_XX.ravel(), _YY.ravel())
                _Xm = _np.asarray(_Xm)
                _Ym = _np.asarray(_Ym)
                _col = ((_Xm - _g0) / _gpx).astype(_np.int64)
                _row = ((_Ym - _g3) / _gpy).astype(_np.int64)
                _hh, _ww = _dem_g.shape
                _inb = (
                    (_col >= 0) & (_col < _ww)
                    & (_row >= 0) & (_row < _hh)
                )
                _dem_s = _np.full(_Xm.shape, _np.nan, _np.float32)
                _cidx_s = _np.full(_Xm.shape, -1, _np.int64)
                _dem_s[_inb] = _dem_g[_row[_inb], _col[_inb]]
                _cidx_s[_inb] = _cidx_g[_row[_inb], _col[_inb]]
                _foot = (_cidx_s >= 0) & _np.isfinite(_dem_s)
                _cidx_c = _np.where(_foot, _cidx_s, 0)

                _is_depth = variable == "Water Depth"
                _itp_tri = None
                if not _is_depth:
                    import matplotlib.tri as _mtri
                    _ctrs = coords[:, :2].astype(float)
                    _cgood = _np.isfinite(_ctrs).all(axis=1)
                    _itp_tri = _mtri.Triangulation(
                        _ctrs[_cgood, 0], _ctrs[_cgood, 1]
                    )
                    _tri_ids = _np.nonzero(_cgood)[0]

                def _frame_field(t):
                    # Sub-grid wet mask drives both the depth field and
                    # the "Wetted only" scope for WSE/velocity.
                    _d_t = wse[t][_cidx_c] - _dem_s
                    _d_t[~_foot] = _np.nan
                    _wet_t = _d_t > 0.01
                    if _is_depth:
                        fld = _np.where(_wet_t, _d_t, _np.nan)
                    else:
                        vals_t = arr[t][_tri_ids].astype(float)
                        _vok = _np.isfinite(vals_t)
                        fld = _np.full(_Xm.shape, _np.nan, _np.float32)
                        if _vok.sum() >= 3:
                            import matplotlib.tri as _mtri
                            itp = _mtri.LinearTriInterpolator(
                                _itp_tri,
                                _np.where(_vok, vals_t, 0.0),
                            )
                            zz = itp(_Xm, _Ym).filled(_np.nan)
                            fld = zz.astype(_np.float32)
                        # flat per-cell fallback outside the hull
                        _gap = _foot & ~_np.isfinite(fld)
                        if _gap.any():
                            fld[_gap] = arr[t][_cidx_c[_gap]]
                        fld[~_foot] = _np.nan
                        if scope == "Wetted only":
                            fld[~_wet_t] = _np.nan
                    return _np.ma.masked_invalid(
                        fld.reshape(_gH, _gW)
                    )

                # Depth uses the shared natural water ramp; dry pixels
                # stay fully transparent over the basemap.
                if _is_depth:
                    cmap_s = water_cmap()
                    _vmin_f, _vmax_f = 0.0, vmax
                else:
                    cmap_s = (
                        _plt.get_cmap(cmap) if isinstance(cmap, str)
                        else cmap
                    )
                    _vmin_f, _vmax_f = vmin, vmax
                cmap_s.set_bad(alpha=0.0)
                im = ax.imshow(
                    _frame_field(int(fidx[0])),
                    extent=(xmin, xmax, ymin, ymax),
                    origin="upper", zorder=2, alpha=0.9,
                    cmap=cmap_s, vmin=_vmin_f, vmax=_vmax_f,
                    interpolation="bilinear",
                )
                artist = ("imshow", im, None)
        except Exception:
            artist = None
            _frame_field = None

    # Mesh - filled polygons or points (also the fallback when the
    # sub-grid smooth animation is unavailable).
    use_filled = (
        artist is None
        and map_style in ("Filled cells", "Smooth (peak)") and merc_ok
        and "cell_fp" in d.files and "fp_xy" in d.files
    )
    if use_filled:
        try:
            cf = d["cell_fp"]
            fp = d["fp_xy"]
            fpx, fpy = tf.transform(fp[:, 0], fp[:, 1])
            fpx, fpy = _np.asarray(fpx), _np.asarray(fpy)
            polys, vids = [], []
            for ci in ids:
                idx = cf[ci]
                idx = idx[idx >= 0]
                if len(idx) < 3:
                    continue
                polys.append(_np.column_stack([fpx[idx], fpy[idx]]))
                vids.append(ci)
            if polys:
                pc = PolyCollection(
                    polys, cmap=cmap, edgecolors="none",
                    alpha=0.88, zorder=2,
                )
                pc.set_clim(vmin, vmax)
                pc.set_array(arr[fidx[0], _np.asarray(vids)])
                ax.add_collection(pc)
                artist = ("poly", pc, _np.asarray(vids))
        except Exception:
            artist = None
    if artist is None:
        sc = ax.scatter(
            cx[ids], cy[ids], s=8, c=arr[fidx[0], ids],
            cmap=cmap, vmin=vmin, vmax=vmax, linewidths=0,
            alpha=0.9, zorder=2,
        )
        artist = ("scatter", sc, ids)
    _kind, _art, _art_ids = artist

    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_aspect("equal")
    for spine in ax.spines.values():
        spine.set_visible(False)

    # ── In-axes title (top-centre, semi-transparent backing) ─────────
    title_text = ax.text(
        0.5, 0.965, "", transform=ax.transAxes,
        ha="center", va="top",
        fontsize=15, fontweight="bold", color="#0d1b2a",
        bbox=dict(
            facecolor="white", alpha=0.88, edgecolor="#0d1b2a",
            boxstyle="round,pad=0.45", linewidth=1.0,
        ),
        zorder=10,
    )

    # ── In-axes colourbar - tall, clean card matching the main map ───
    # Auto-place on whichever side of the view has the least flooded
    # footprint, so the card covers basemap rather than results.  Ticks
    # sit on the side facing INTO the map so labels never clip the edge.
    _xn = (cx[ids] - xmin) / max(1e-9, (xmax - xmin))
    _on_right = float((_xn > 0.86).mean()) <= float((_xn < 0.14).mean())
    if _on_right:
        _card_xy = (0.902, 0.16)
        _cax_box = [0.940, 0.225, 0.016, 0.54]
        _tick_side = "left"
    else:
        _card_xy = (0.020, 0.16)
        _cax_box = [0.052, 0.225, 0.016, 0.54]
        _tick_side = "right"
    from matplotlib.patches import FancyBboxPatch
    ax.add_patch(FancyBboxPatch(
        _card_xy, 0.078, 0.68, transform=ax.transAxes,
        boxstyle="round,pad=0.004,rounding_size=0.012",
        facecolor="white", alpha=0.85, edgecolor="#cdd6e3",
        linewidth=1.0, zorder=9,
    ))
    cax = fig.add_axes(_cax_box)
    cbar = fig.colorbar(_art, cax=cax)
    from matplotlib.ticker import MaxNLocator
    cbar.locator = MaxNLocator(6)
    cbar.update_ticks()
    cax.yaxis.set_ticks_position(_tick_side)
    cbar.ax.tick_params(
        labelsize=9, colors="#0d1b2a", pad=2, length=3, width=0.6,
    )
    # Two-line title above the bar, e.g. "Depth" / "(m)".
    cax.set_title(
        label.replace(" (", "\n("), fontsize=9.5, fontweight="bold",
        color="#0d1b2a", pad=6,
    )
    cbar.outline.set_edgecolor("#cdd6e3")
    cbar.outline.set_linewidth(0.8)

    # ── Render frames to full-colour RGB arrays ──────────────────────
    frames_rgb: list = []
    for k in range(n_frames):
        if _kind == "imshow":
            _art.set_data(_frame_field(int(fidx[k])))
        else:
            _art.set_array(arr[fidx[k], _art_ids])
        title_text.set_text(
            f"{variable}   ·   {mt_lbl[fidx[k]]:%Y-%m-%d %H:%M}{_clock_tag}"
        )
        fig.canvas.draw()
        buf = _np.asarray(fig.canvas.buffer_rgba())
        frames_rgb.append(buf[..., :3].copy())   # drop alpha
    _plt.close(fig)

    if fmt == "mp4":
        # ── Encode a full-colour, seekable H.264 MP4 via ffmpeg ───────
        # libx264 + yuv420p needs even dimensions, so crop to even.
        import imageio.v2 as _imageio
        H, W = frames_rgb[0].shape[:2]
        H, W = H - (H % 2), W - (W % 2)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as _tf:
            _tmp_mp4 = _tf.name
        try:
            # imageio's libx264 default output is already yuv420p (the
            # browser-compatible pixel format), so we don't re-specify it.
            writer = _imageio.get_writer(
                _tmp_mp4, fps=max(1, int(fps)), codec="libx264",
                quality=9, macro_block_size=None,
            )
            for _f in frames_rgb:
                writer.append_data(_f[:H, :W])
            writer.close()
            with open(_tmp_mp4, "rb") as _fh:
                data = _fh.read()
        finally:
            try:
                os.unlink(_tmp_mp4)
            except OSError:
                pass
        return data

    # ── Legacy GIF path (255-colour, looping) for slide downloads ────
    frames_pil = [Image.fromarray(f) for f in frames_rgb]
    # One SHARED 255-colour palette for every frame (taken from a
    # mid-animation frame, when the flood is present).  Per-frame
    # adaptive palettes make the colour mapping shift between frames -
    # the colorbar visibly "breathes" while real map changes get lost.
    _pal_src = frames_pil[len(frames_pil) // 2].quantize(
        colors=255, method=Image.MEDIANCUT
    )
    frames_pil = [
        f.quantize(palette=_pal_src, dither=Image.Dither.NONE)
        for f in frames_pil
    ]
    with tempfile.NamedTemporaryFile(
        suffix=".gif", delete=False
    ) as _tf:
        _tmp_gif = _tf.name
    try:
        duration_ms = int(round(1000 / max(1, int(fps))))
        frames_pil[0].save(
            _tmp_gif,
            save_all=True,
            append_images=frames_pil[1:],
            duration=duration_ms,
            loop=0,             # 0 = loop forever
            optimize=False,
            disposal=2,
        )
        with open(_tmp_gif, "rb") as _fh:
            data = _fh.read()
    finally:
        try:
            os.unlink(_tmp_gif)
        except OSError:
            pass
    return data


def _polish_fig(fig, ytitle, xtitle="Time", title=None, square=False):
    """Apply the shared publication-style look to a Plotly figure.

    Bold axis titles + tick labels, four-sided box, gridlines on both
    axes.  ``square=True`` forces a 600x600 equal-aspect canvas (1:1 plot).
    """
    _bold_title = dict(size=15, color="#1f2933", weight="bold")
    _bold_tick = dict(size=12, color="#1f2933", weight="bold")
    _axis_kw = dict(
        showline=True, linewidth=1.4, linecolor="#33415c",
        mirror=True, ticks="outside",
        showgrid=True, gridcolor="#e3e8ee", gridwidth=0.7,
        zeroline=False,
        title_font=_bold_title, tickfont=_bold_tick,
    )
    fig.update_xaxes(title_text=xtitle, **_axis_kw)
    fig.update_yaxes(title_text=ytitle, **_axis_kw)
    layout = dict(
        template="plotly_white",
        hovermode="x unified" if not square else "closest",
        font=dict(size=13, color="#1f2933"),
        # Force a white paper + plot background and dark text so the
        # figure stays readable in dark app mode - Plotly would
        # otherwise pick up Streamlit's dark theme via the runtime
        # template, washing out axis labels and titles.
        paper_bgcolor="white",
        plot_bgcolor="white",
        legend=dict(
            orientation="h", y=1.02, yanchor="bottom",
            x=1, xanchor="right", bgcolor="rgba(255,255,255,0.6)",
            font=dict(weight="bold", color="#1f2933"),
        ),
    )
    if square:
        layout.update(width=600, height=600,
                       margin=dict(l=70, r=30, t=60, b=60))
        fig.update_yaxes(scaleanchor="x", scaleratio=1)
    else:
        layout.update(height=460, margin=dict(l=70, r=30, t=60, b=60))
    if title is not None:
        layout["title"] = dict(
            text=f"<b>{title}</b>", x=0.5, xanchor="center",
            font=dict(size=16, color="#1f2933"),
        )
    fig.update_layout(**layout)
    return fig


@st.fragment(run_every="1s")
def _auto_schedule_watch() -> None:
    """Live countdown for the auto-schedule - updates every second.

    Reads its state from the **on-disk schedule file** owned by the
    ``auto_scheduler`` daemon, so the countdown is correct even after a
    browser refresh, a websocket drop, or a Streamlit restart.  The
    daemon - not the UI - actually fires the next run, so the loop
    keeps going for as long as the container runs.
    """
    sched = _read_schedule_state()

    # NOTE: change-detection + full-app rerun moved to the top-level
    # ``_daemon_refresh_beacon`` fragment (v3.1.2).  A ``run_every``
    # fragment's ``st.rerun(scope="app")`` only reliably propagates to a
    # full rerun when the fragment lives at the top level - nested here
    # inside ``with tab_run:`` it was swallowed, which is why Tab 5 / Tab 8
    # used to need a manual browser refresh after each cycle.  This
    # fragment now only renders the live countdown.

    # Nothing visible to render if the schedule is idle / disabled.
    if not sched or not sched.get("enabled"):
        return

    iso = sched.get("next_run_at") or datetime.utcnow().isoformat()
    try:
        nxt = datetime.fromisoformat(iso.replace("Z", ""))
    except ValueError:
        nxt = datetime.utcnow()
    sched_min = int(sched.get("interval_minutes", 60))
    state = str(sched.get("state", "idle"))

    now = datetime.utcnow()
    rem = nxt - now
    total = max(0, int(rem.total_seconds()))
    hh, rest = divmod(total, 3600)
    mm, ss = divmod(rest, 60)
    timer = (
        f"{hh:02d}:{mm:02d}:{ss:02d}" if hh
        else f"{mm:02d}:{ss:02d}"
    )
    if state == "running" or total == 0:
        timer = "running"

    n_done = int(sched.get("runs_completed", 0))
    n_fail = int(sched.get("runs_failed", 0))
    fail_chip = (
        f"&nbsp;·&nbsp;<span style='color:#ff8a80;font-weight:600;'>"
        f"{n_fail} failed</span>"
        if n_fail else ""
    )

    st.markdown(
        f"""
<div style="
    text-align:center;
    padding:1.6rem 1rem 1.3rem;
    margin:0.7rem 0 1.1rem;
    border-radius:14px;
    background: linear-gradient(135deg,
        rgba(0,180,216,0.12) 0%,
        rgba(0,180,216,0.04) 100%);
    border: 1.5px solid rgba(0,180,216,0.55);
    box-shadow: 0 0 24px rgba(0,180,216,0.10);">
  <div style="
      font-size:0.95rem;
      font-weight:700;
      letter-spacing:0.08em;
      text-transform:uppercase;
      color:#00B4D8;
      margin-bottom:0.5rem;">
    Auto-schedule active &nbsp;·&nbsp; next run in
  </div>
  <div style="
      font-size:5.2rem;
      font-weight:800;
      font-variant-numeric: tabular-nums;
      line-height:1;
      margin:0.2rem 0 0.7rem;
      color: var(--text, #e6edf3);
      font-family: 'SF Mono','JetBrains Mono',Menlo,Consolas,monospace;
      letter-spacing:0.05em;
      text-shadow: 0 2px 12px rgba(0,180,216,0.18);">
    {timer}
  </div>
  <div style="
      font-size:0.95rem;
      color: var(--text, #e6edf3);
      opacity:0.88;">
    Re-running every <b>{_fmt_interval(sched_min)}</b>
    &nbsp;·&nbsp;
    Next at <b>{nxt:%Y-%m-%d %H:%M:%S} UTC</b>
    &nbsp;·&nbsp;
    <b>{n_done}</b> completed{fail_chip}
  </div>
  <div style="
      font-size:0.82rem;
      color: var(--text, #e6edf3);
      opacity:0.65;
      margin-top:0.7rem;">
    Managed by the persistent <b>auto_scheduler</b> daemon - keeps
    running even with the browser closed. Use the <b>Stop schedule</b>
    button or Reset (top-right) to stop.
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )


# Fixed forecast horizons (in hours) per product/horizon key.  Used to
# auto-size the Forecast simulation window in Tab 2 - for true
# forecasts the length is dictated by the product, NOT chosen freely
# by the user.  `analysis_assim` is a nowcast (no forward horizon), so
# it maps to None and falls back to a user-chosen look-back.
FORECAST_HORIZON_HOURS = {
    "analysis_assim": None,     # nowcast / current-state estimate
    "short_range": 18,          # 18 hours
    "medium_range": 240,        # 10 days
    "long_range": 720,          # 30 days
    "stofs_2d_global": 180,     # ~7.5 days
    "stofs_3d_atlantic": 96,    # ~4 days
    "stofs_3d_pacific": 96,     # ~4 days
}


@st.cache_data(ttl=600, show_spinner=False)
def _coverage_end_cached(product: str, horizon: str, member: int,
                         domain: str):
    """Live coverage end (UTC) for one forecast source, cached 10 min.

    Cycles publish a few times a day, so a ten-minute cache keeps the
    Simulation-Window tab responsive without ever showing a stale
    reach for long.
    """
    from forecast_client import forecast_coverage_end
    return forecast_coverage_end({
        "forecast_product": product,
        "forecast_horizon": horizon,
        "forecast_member": member,
        "stofs_domain": domain,
    })


def _forecast_window_from_snapshot():
    """Forward reach of every forecast boundary, and the binding one.

    Returns ``(rows, hours)``.  Each row is
    ``(bc_name, horizon_key, hours_from_now, is_live)`` - ``is_live``
    marks a reach read from the newest published cycle rather than
    from the nominal horizon table.  ``hours`` is the **smallest** of
    them: the window every configured boundary can actually supply
    real data for.  ``None`` when no forward-forecast BC is set.

    Sizing to the smallest is deliberate.  Sizing to the largest (what
    this did before v4.8.3) let a 10-day NWM boundary stretch a window
    that a 4-day STOFS boundary could only fill for its first days -
    the engine then held the tide at a frozen value for the rest, which
    looks like a result but is not one.

    The live reach also accounts for cycle age: medium range is a
    240-hour product, but a cycle issued 10 hours ago only reaches 230
    hours past now.
    """
    now = datetime.utcnow()
    rows = []
    for b in (st.session_state.get("_bc_config_snapshot") or []):
        if str(b.get("source", "")).lower() != "forecast":
            continue
        hkey = str(b.get("forecast_horizon", "")).lower()
        nominal = FORECAST_HORIZON_HOURS.get(hkey)
        if not nominal:
            # analysis_assim and friends: a nowcast has no forward
            # reach, so it cannot bind the window.
            continue
        hours, live = nominal, False
        try:
            end = _coverage_end_cached(
                str(b.get("forecast_product", "")),
                hkey,
                int(b.get("forecast_member", 1) or 1),
                str(b.get("stofs_domain", "atlantic")),
            )
            if end is not None:
                real = (end - now).total_seconds() / 3600.0
                if real > 0:
                    hours, live = real, True
        except Exception:
            pass
        rows.append((b.get("name", "?"), hkey, hours, live))
    hours = min((h for _, _, h, _ in rows), default=None)
    return rows, hours


def _explain_horizon(horizon: str) -> None:
    """Render a context-sensitive info box for the selected NWM horizon.

    Each NWM forecast product covers a different time window, retention
    policy, and ensemble structure.  Picking the wrong one is the single
    most common mistake when wiring up a Forecast boundary, so we
    surface a plain-language explanation right under the selectbox.
    """
    h = str(horizon).lower()
    if h == "analysis_assim":
        st.info(
            "**`analysis_assim` - Analysis & Assimilation (nowcast).**  \n"
            "NWM's *current-state estimate*, updated **hourly** with a "
            "3-hour look-back per cycle. Not a forecast - it's the "
            "model's best guess of conditions *right now*.  \n\n"
            "• **Use it for:** running a simulation through the most "
            "recent observed period (last few days).  \n"
            "• **Don't use it for:** a window in the future (no forecast "
            "data) or a window older than ~30 days (the operational "
            "bucket purges old cycles)."
        )
    elif h == "short_range":
        st.info(
            "**`short_range` - Short-range forecast (18 hours).**  \n"
            "Deterministic forecast issued **hourly**, covering the "
            "next **18 hours** at hourly resolution. Highest temporal "
            "fidelity of any NWM product.  \n\n"
            "• **Use it for:** intra-day operational forecasting, "
            "flash-flood scenarios, near-term alerting.  \n"
            "• Ensemble member must be **`1`** (short-range is "
            "deterministic - no ensemble)."
        )
    elif h == "medium_range":
        st.info(
            "**`medium_range` - Medium-range forecast (10 days).**  \n"
            "Issued **four times per day** (00z, 06z, 12z, 18z) at "
            "**3-hourly** resolution out to **10 days**. A **7-member "
            "ensemble** - member 1 is deterministic, 2-7 are perturbed.  \n\n"
            "• **Use it for:** the typical real-time forecasting use "
            "case - week-ahead flood awareness, decision-support cycles.  \n"
            "• To run an ensemble HEC-RAS forecast, re-run with "
            "**Ensemble member = 1, 2, 3, …, 7** in turn.  \n"
            "• *Recommended default for most use cases.*"
        )
    elif h == "long_range":
        st.info(
            "**`long_range` - Long-range forecast (30 days).**  \n"
            "Issued **four times per day** at **6-hourly** resolution "
            "out to **30 days**. A **4-member ensemble** (members 1-4). "
            "Much coarser temporal resolution than medium-range.  \n\n"
            "• **Use it for:** monthly water-supply outlooks, "
            "reservoir-rule decisions, sustained-event scenario "
            "planning.  \n"
            "• **Don't use it for:** sub-daily flood timing - the "
            "6-hour interval will under-resolve flash events."
        )
    elif h in ("stofs_3d_atlantic", "stofs_3d_pacific"):
        _basin = "Pacific" if h.endswith("pacific") else "Atlantic"
        st.info(
            f"**STOFS-3D {_basin} - Total Water Level (~4-day "
            "forecast).**  \n"
            "Issued **once per day**, STOFS-3D provides a fixed "
            "**~4-day (96-hour)** coastal Total-Water-Level forecast "
            "(tide + surge + steric + wave setup) at the model mesh "
            "node nearest your tide station.  \n\n"
            "• **The forecast length is fixed by the product** - you "
            "don't choose a number of days. The Simulation-Window tab "
            "auto-sizes the window to ~4 days from *now*.  \n"
            "• **Use it for:** coastal / tidal downstream **stage** "
            "boundaries during the next few days."
        )
    elif h == "stofs_2d_global":
        st.info(
            "**STOFS-2D Global - Total Water Level (~7.5-day "
            "forecast).**  \n"
            "Issued **four times per day**, the 2-D global product "
            "provides a fixed **~7.5-day (180-hour)** Total-Water-"
            "Level forecast. Coarser than STOFS-3D but longer "
            "horizon.  \n\n"
            "• **The forecast length is fixed by the product** - the "
            "Simulation-Window tab auto-sizes the window to ~7.5 days."
        )


TAB_NAMES = [
    "1 · Model Folder",
    "2 · Simulation Window",
    "3 · Boundary Conditions",
    "4 · Run",
    "5 · Results",
    "6 · Validation",
    "7 · Agent",
    "8 · Live",
    "9 · User Manual",
]

# Live-mode flag is already detected earlier (before the header) so
# the Reset button doesn't render for view-only viewers.  Keeping
# this stub for backwards-readability - `_LIVE_MODE` is already set.

def _render_live_panels() -> None:
    """The actual Live-tab content, refreshed in-place every 15 s."""
    _sched_now = _read_schedule_state() or {}
    _root = _sched_now.get("schedule_root", "-")
    _completed = (
        _sched_now.get("runs_completed")
        or _sched_now.get("completed_count")
        or 0
    )
    if not _completed and HISTORY_FILE.exists():
        try:
            _hh = json.loads(HISTORY_FILE.read_text())
            _completed = sum(
                1 for e in (_hh or [])
                if isinstance(e, dict)
                and e.get("state") == "done"
                and _root in (e.get("output_dir") or "")
            )
        except Exception:
            pass
    _next_at = (
        _sched_now.get("next_run_at")
        or _sched_now.get("next_run_iso")
        or ""
    )
    _h1, _h2, _h3 = st.columns(3)
    _h1.metric("Schedule", str(_root)[-32:])
    _h2.metric("Iterations completed", int(_completed or 0))
    _h3.metric("Next run", str(_next_at)[:16] if _next_at else "-")

    _latest = _latest_successful_run()
    if _latest is None or not (_latest / "wse_extract.npz").exists():
        st.warning("Waiting for the first iteration to complete…")
        return
    try:
        import numpy as _lnp
        import pandas as _lpd
        import plotly.graph_objects as _lgo
        _ld = _lnp.load(_latest / "wse_extract.npz", allow_pickle=True)
        from units import is_si as _is_si, unit_labels as _unit_labels
        _lu = _unit_labels(_is_si(
            str(_ld["unit_system"][0]) if "unit_system" in _ld.files else None
        ))
        _has_depth = "min_elev" in _ld.files
        if not _has_depth:
            st.warning(
                "The latest run does not contain terrain elevation - "
                "depth view requires `min_elev`. Showing WSE instead."
            )
            _ldepth = _lnp.asarray(_ld["wse"], dtype="float32")
            _ldepth_label = f"Water Surface ({_lu['length']})"
        else:
            _ldepth = _lnp.clip(
                _lnp.asarray(_ld["wse"], dtype="float32")
                - _lnp.asarray(_ld["min_elev"], dtype="float32"),
                0.0, None,
            )
            _ldepth_label = f"Water Depth ({_lu['length']})"
        _lcoords = _ld["coords"]
        _ltime = _lpd.to_datetime(_ld["model_time"])
        # Live dashboard renders on the same clock as everywhere else
        # (v4.8.0); stakeholders reading this panel want local time.
        from timebase import TimeBase as _LTimeBase
        _ltb = _LTimeBase.from_npz(_ld)
        if not _ltb.known:
            _ltb = _LTimeBase.from_dir(_latest)
        _lpeak = _lnp.nanmax(_ldepth, axis=0)
        _lwet = _lpeak > 0.01

        try:
            _all_hist = (
                json.loads(HISTORY_FILE.read_text())
                if HISTORY_FILE.exists() else []
            )
        except Exception:
            _all_hist = []
        _hits = [
            e for e in _all_hist
            if isinstance(e, dict)
            and e.get("state") == "done"
            and _root in (e.get("output_dir") or "")
        ]
        _hits.sort(
            key=lambda r: _lpd.to_datetime(r.get("start", ""))
            if r.get("start") else _lpd.Timestamp.min
        )

        # Focus cell = wettest cell of the latest iteration.  Hoisted
        # out of the `if _hits:` block so the depth map below can
        # reference it even when no cumulative trace exists yet.
        _focus_cell = (
            int(_lnp.nanargmax(_lpeak))
            if _lnp.isfinite(_lpeak).any() else 0
        )

        if _hits:
            st.markdown(
                f"##### Live cumulative depth - {len(_hits)} "
                f"iteration{'s' if len(_hits) != 1 else ''}"
            )
            # Build the cumulative trace as ONE Scatter, but insert a
            # `None` value at the boundary between iterations so Plotly
            # doesn't draw a connector line across the gap.  Concatenating
            # raw lists used to produce a long horizontal line spanning
            # the whole x-axis when adjacent iters had non-overlapping
            # windows.
            _cum_x: list = []
            _cum_y: list = []
            for _hi, _h in enumerate(_hits):
                try:
                    _hd = _lnp.load(
                        Path(_h["output_dir"]) / "wse_extract.npz",
                        allow_pickle=True,
                    )
                except Exception:
                    continue
                if "min_elev" in _hd.files:
                    _hdepth = _lnp.clip(
                        _hd["wse"] - _hd["min_elev"], 0.0, None
                    )
                else:
                    _hdepth = _hd["wse"]
                _hci = min(_focus_cell, _hdepth.shape[1] - 1)
                _times = list(
                    _ltb.to_lst(_lpd.to_datetime(_hd["model_time"]))
                )
                _vals = _hdepth[:, _hci].tolist()
                if _cum_x:
                    # Inject a single gap point so the line breaks
                    # between iterations.
                    _cum_x.append(None)
                    _cum_y.append(None)
                _cum_x.extend(_times)
                _cum_y.extend(_vals)

            if _cum_x:
                _cfig = _lgo.Figure()
                _cfig.add_trace(_lgo.Scatter(
                    x=_cum_x, y=_cum_y, mode="lines", name="Depth",
                    line=dict(width=1.6, color="#3c78af"),
                    connectgaps=False,
                ))
                _polish_fig(
                    _cfig, ytitle=_ldepth_label,
                    xtitle=_ltb.axis_label(),
                    title=f"Live cumulative - Cell {_focus_cell}",
                )
                st.plotly_chart(
                    _cfig, width="stretch",
                    config={"displaylogo": False, "displayModeBar": False},
                )

        st.markdown(
            f"##### Latest iteration - depth map  ({_latest.name})"
        )
        # Always render the depth map as an interactive Plotly scatter
        # so we can overlay a red marker on the cell the cumulative
        # plot is tracking.  The previously-preferred peak_depth_map.png
        # (matplotlib) didn't let us annotate a cell in-browser.
        _scat = _lgo.Figure()
        _scat.add_trace(_lgo.Scattergl(
            x=_lcoords[_lwet, 0],
            y=_lcoords[_lwet, 1],
            mode="markers",
            marker=dict(
                size=4, color=_lpeak[_lwet],
                colorscale="Blues", showscale=True,
                colorbar=dict(title=f"Depth ({_lu['length']})"),
            ),
            hovertemplate=(
                f"Cell:%{{text}}<br>Depth:%{{marker.color:.2f}} {_lu['length']}"
                "<extra></extra>"
            ),
            text=[str(i) for i in _lnp.where(_lwet)[0]],
            name="Peak depth",
            showlegend=False,
        ))
        # Red ring at the focus cell (the one the cumulative plot
        # tracks) so viewers know exactly which location the line
        # chart corresponds to.
        if _focus_cell is not None and _focus_cell < _lcoords.shape[0]:
            _fx = float(_lcoords[_focus_cell, 0])
            _fy = float(_lcoords[_focus_cell, 1])
            _scat.add_trace(_lgo.Scatter(
                x=[_fx], y=[_fy],
                mode="markers",
                marker=dict(
                    size=18, color="rgba(0,0,0,0)",
                    line=dict(color="#e53935", width=3),
                    symbol="circle",
                ),
                name=f"Cell {_focus_cell} (live plot)",
                hovertemplate=(
                    f"<b>Cell {_focus_cell}</b><br>"
                    "The cumulative plot above tracks this cell"
                    "<extra></extra>"
                ),
                showlegend=True,
            ))
        _scat.update_layout(
            height=520,
            title=(
                f"Peak depth - wet cells "
                f"(Cell {_focus_cell} tracked above)"
            ),
            xaxis_title="X (model CRS)",
            yaxis_title="Y (model CRS)",
            yaxis=dict(scaleanchor="x", scaleratio=1),
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02,
                xanchor="right", x=1,
            ),
        )
        _polish_fig(_scat, ytitle="Y (model CRS)")
        st.plotly_chart(
            _scat, width="stretch",
            config={"displaylogo": False, "displayModeBar": False},
        )

        st.caption(
            f"Window: {_ltime[0]} → {_ltime[-1]} · "
            "Auto-refreshes every 15 s"
        )
    except Exception as _live_err:
        st.error(f"Could not load latest iteration: {_live_err}")


    if not _LIVE_MODE:
        _tab_nav(7)


# ── Live-mode short-circuit ────────────────────────────────────────
# When ?live=1 is present in the URL, render ONLY the Live dashboard
# (no tabs, no controls, no other content) and halt the rest of the
# Streamlit page rendering via st.stop().  This is the cleanest way
# to deliver a real read-only view: viewers can't see anything they
# could click to break, because the rest of the app isn't rendered.
if _LIVE_MODE:
    st.markdown(
        """
        <style>
          header[data-testid="stHeader"] { display: none !important; }
          #MainMenu { visibility: hidden !important; }
          footer { visibility: hidden !important; }
          [data-testid="stToolbar"] { display: none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("### HECinBOX · Live Dashboard")
    st.caption(
        "View-only mode - controls are hidden. Auto-updates "
        "when a new iteration completes."
    )
    _sched_now_live = _read_schedule_state() or {}
    if not _sched_now_live.get("enabled"):
        st.info(
            "**Auto-schedule isn't running.** The owner needs to "
            "start a schedule for this dashboard to populate."
        )
    else:
        @st.fragment(run_every="3s")
        def _live_dashboard_only_fragment() -> None:
            try:
                _mtime = (
                    HISTORY_FILE.stat().st_mtime
                    if HISTORY_FILE.exists() else 0.0
                )
            except OSError:
                _mtime = 0.0
            _ls = st.session_state.get(
                "_live_only_last_mtime", -1.0
            )
            if _mtime != _ls:
                st.session_state["_live_only_last_mtime"] = _mtime
            _render_live_panels()
        _live_dashboard_only_fragment()
    st.stop()



def _tab_nav(current_idx: int) -> None:
    """Render Previous / Next navigation hints at the bottom of a tab."""
    parts: list[str] = []
    if current_idx > 0:
        parts.append(f"**← Previous:** {TAB_NAMES[current_idx - 1]}")
    if current_idx < len(TAB_NAMES) - 1:
        parts.append(f"**Next:** {TAB_NAMES[current_idx + 1]} **→**")
    if parts:
        st.markdown("---")
        st.caption(" · ".join(parts))


# Arm the results map for PNG export before any chart can be created.
_gl_capture_patch()

# ── Tabs ───────────────────────────────────────────────────────────────
(
    tab_model, tab_window, tab_bc, tab_run,
    tab_results, tab_val, tab_agent, tab_live, tab_manual
) = st.tabs(TAB_NAMES)

MODEL_DIR: Path | None = None
scan: dict | None = None

# ── TAB 1: Model folder + output location ─────────────────────────────
with tab_model:
    if _busy_banner_if_active():
        _tab_nav(0)
    else:
        # Heading row: "Select Model" on the left, Reset on the right.
        # Placing Reset here (instead of in the page header) keeps it
        # contextually close to the model selection it actually clears,
        # and frees the global header for a larger title + logo.
        _smc1, _smc2 = st.columns([5, 1.2])
        with _smc1:
            st.subheader("Select Model")
        with _smc2:
            if st.button(
                "Reset",
                key="reset_tab1",
                help=(
                    "Stop the auto-schedule, clear the loaded model, "
                    "results, and saved preferences."
                ),
                width="stretch",
            ):
                _reset_app()

        # ── Model source: local folder vs. cloud (S3) ──────────────────
        if DEMO_MODE:
            # Hosted demo - only the curated sample models, cloud-only.
            st.info(
                "**Demo mode** - try HECinBOX on one of the bundled "
                "sample models below. Loading your own model is disabled "
                "in this hosted demo. To run **your own** HEC-RAS model, "
                "download HECinBOX and run it on your machine."
            )
            _model_source = "Cloud storage (S3)"
            _cloud_mode = True
        else:
            _model_source = st.radio(
                "Where is your HEC-RAS model?",
                ["This machine", "Cloud storage (S3)"],
                horizontal=True,
                key="model_source",
                help=(
                    "This machine - browse a folder mounted into the "
                    "container. Cloud storage - load the model directly "
                    "from an Amazon S3 (or S3-compatible) bucket, ideal "
                    "when HECinBOX runs on an AWS / Google Cloud server."
                ),
            )
            _cloud_mode = _model_source == "Cloud storage (S3)"

        if not DEMO_MODE:
            st.warning(
                "The model you select must have been **successfully run at "
                "least once** in the HEC-RAS desktop application (Windows). "
                "The pipeline requires the plan HDF file (`.pXX.hdf`) and "
                "execution file (`.xXX`) generated during the first "
                "computation."
            )

        if not _cloud_mode:
            # ── LOCAL: folder browser ──────────────────────────────────
            if "browse_path" not in st.session_state:
                st.session_state.browse_path = str(HOST_ROOT)

            current = Path(st.session_state.browse_path)

            col_path, col_up = st.columns([5, 1])
            with col_path:
                st.markdown(f"**Current:** `{current}`")
            with col_up:
                if current != HOST_ROOT:
                    if st.button(":arrow_up: Up", width="stretch"):
                        st.session_state.browse_path = str(current.parent)
                        st.rerun()

            prj_files = _find_prj(current)
            if prj_files:
                st.success(
                    "HEC-RAS model folder detected - see "
                    "**Detected Model** below."
                )
                MODEL_DIR = current
            else:
                subdirs = _list_subdirs(current)
                if subdirs:
                    cols_per_row = 4
                    for i in range(0, len(subdirs), cols_per_row):
                        row_dirs = subdirs[i : i + cols_per_row]
                        cols = st.columns(cols_per_row)
                        for j, dirname in enumerate(row_dirs):
                            with cols[j]:
                                if st.button(
                                    f":open_file_folder: {dirname}",
                                    key=f"dir_{i+j}",
                                    width="stretch",
                                ):
                                    st.session_state.browse_path = str(
                                        current / dirname
                                    )
                                    st.rerun()
                else:
                    st.warning(
                        "No subdirectories or `.prj` files found here."
                    )
        else:
            # ── CLOUD: load the model from an S3 bucket ────────────────
            _uri = ""
            _connect_clicked = False
            if DEMO_MODE:
                # Curated sample-model picker (no free-text S3 URI).
                st.markdown("##### Sample model")
                _samples = []
                try:
                    from cloud_storage import list_s3_model_prefixes
                    _samples = list_s3_model_prefixes(DEMO_MODELS_URI)
                except Exception as _e:  # noqa: BLE001
                    st.error(f"Couldn't list the demo models: {_e}")
                if _samples:
                    _labels = [s["name"] for s in _samples]
                    _sm_col, _sm_btn = st.columns([4, 1])
                    with _sm_col:
                        _sel = st.selectbox(
                            "Sample model", _labels,
                            key="demo_model_pick",
                            label_visibility="collapsed",
                        )
                    with _sm_btn:
                        _connect_clicked = st.button(
                            "Load model", width="stretch",
                            type="primary",
                        )
                    _uri = _samples[_labels.index(_sel)]["uri"]
                else:
                    st.warning(
                        "No sample models are available in the demo "
                        "bucket right now."
                    )
            else:
                st.markdown("##### Cloud model location")
                st.caption(
                    "Enter the **S3 URI of the folder** that holds your "
                    "HEC-RAS project files (`.prj`, `.pXX`, `.gXX`, "
                    "`.uXX`, `.pXX.hdf`, `.xXX`, …). HECinBOX downloads "
                    "the model into the container, then runs it just like "
                    "a local model. Credentials come from the container's "
                    "environment / IAM role - see the *Cloud setup* note "
                    "below."
                )
                _cm_uri_col, _cm_btn_col = st.columns([4, 1])
                with _cm_uri_col:
                    _cloud_model_uri = st.text_input(
                        "Model S3 URI",
                        placeholder="s3://my-bucket/models/kalamazoo/",
                        key="cloud_model_uri",
                        label_visibility="collapsed",
                    )
                with _cm_btn_col:
                    _connect_clicked = st.button(
                        "Connect & Scan",
                        width="stretch",
                        type="primary",
                    )
                _uri = (_cloud_model_uri or "").strip()

            if _connect_clicked:
                import hashlib as _hl
                import shutil as _sh
                _uri = (_uri or "").strip()
                if not _uri:
                    st.error("Select or enter a model first.")
                else:
                    _stage = (
                        Path("/app/data/cloud_models")
                        / _hl.md5(_uri.encode()).hexdigest()
                    )
                    try:
                        from cloud_storage import download_prefix
                        if _stage.exists():
                            _sh.rmtree(_stage, ignore_errors=True)
                        with st.spinner(
                            f"Downloading model from {_uri} …"
                        ):
                            _n = download_prefix(_uri, _stage)
                        st.session_state["cloud_model_dir"] = str(_stage)
                        st.session_state["cloud_model_src_uri"] = _uri
                        for _k in list(st.session_state.keys()):
                            if _k.startswith("scan_"):
                                del st.session_state[_k]
                        st.success(
                            f"Downloaded {_n} file(s) from the bucket."
                        )
                        st.rerun()
                    except Exception as _e:
                        st.error(f"Cloud download failed - {_e}")

            _cmd = st.session_state.get("cloud_model_dir")
            if _cmd and Path(_cmd).exists() and _find_prj(Path(_cmd)):
                MODEL_DIR = Path(_cmd)
                st.success(
                    "HEC-RAS model downloaded from "
                    f"`{st.session_state.get('cloud_model_src_uri', '')}` "
                    "- see **Detected Model** below."
                )
            elif _cmd and Path(_cmd).exists():
                st.error(
                    "The downloaded folder has no `.prj` file. Make "
                    "sure the S3 URI points **directly at the model "
                    "folder** that contains the HEC-RAS project files."
                )

            if not DEMO_MODE:
                with st.expander("Cloud setup - credentials & endpoints"):
                    st.markdown(
                        "HECinBOX never asks for keys in this UI. It "
                        "uses the **container's environment**:\n\n"
                        "- On an **AWS server**, an attached **IAM role** "
                        "grants S3 access automatically - nothing to "
                        "configure.\n"
                        "- Otherwise pass keys at `docker run` time:\n"
                        "  `-e AWS_ACCESS_KEY_ID=…  "
                        "-e AWS_SECRET_ACCESS_KEY=…  "
                        "-e AWS_DEFAULT_REGION=us-east-1`\n"
                        "- For an **S3-compatible** service (Cloudflare "
                        "R2, MinIO, Wasabi, Backblaze B2, …) also pass "
                        "`-e S3_ENDPOINT_URL=https://…`\n"
                        "- For a **public bucket** (e.g. a shared sample "
                        "model), **no keys are needed** - run with no AWS "
                        "credentials and HECinBOX reads it anonymously. To "
                        "force this even when keys exist, pass "
                        "`-e S3_ANONYMOUS=1`."
                    )

        if MODEL_DIR is not None:
            cache_key = f"scan_{MODEL_DIR}"
            if cache_key not in st.session_state:
                st.session_state[cache_key] = scan_model(MODEL_DIR)
            scan = st.session_state[cache_key]
            # Tab 1 short-circuits while a run is active, which used to
            # leave `scan` as None for the whole script run: Tab 2 then
            # forgot which model was loaded and fell back to a default
            # window. Remember the last good scan so the other tabs can
            # still name the model and its dates during a run.
            st.session_state["_last_scan"] = scan
            st.session_state["_last_model_dir"] = str(MODEL_DIR)

            for err in scan.get("errors", []):
                st.warning(err)

            if "project_name" not in scan:
                st.error(f"Could not parse model in `{MODEL_DIR}`.")
                scan = None
            else:
                st.markdown("##### Detected Model")
                mc1, mc2 = st.columns(2)
                with mc1:
                    st.markdown(
                        f"**Project:** `{scan.get('project_name', '-')}`"
                    )
                    st.markdown(f"**Plan:** `{scan.get('plan_suffix', '-')}`")
                    st.markdown(
                        f"**Geometry:** `{scan.get('geom_suffix', '-')}`"
                    )
                with mc2:
                    st.markdown(
                        f"**Unsteady:** `{scan.get('unsteady_suffix', '-')}`"
                    )
                    st.markdown(
                        f"**Exec file:** `{scan.get('exec_suffix', '-')}`"
                    )
                    if scan.get("original_start"):
                        st.markdown(
                            f"**Original window:** "
                            f"`{scan.get('original_start')}` → "
                            f"`{scan.get('original_end')}`"
                        )
                    _unit = scan.get("unit_system")
                    _unit_label = {
                        "SI": "SI (metric - m, m³/s)",
                        "English": "English (US customary - ft, cfs)",
                    }.get(_unit, "-")
                    st.markdown(f"**Unit system:** `{_unit_label}`")

                # ── Per-BC detail block (v2.8.1+) ─────────────────
                # Parsed from the model's .u01 file so the user can see
                # the boundary types, intervals, and bundled values
                # *before* configuring Tab 3.  Station IDs / parameter
                # codes are NOT stored in HEC-RAS files - those are
                # HECinBOX-side fetch settings.
                _bcd = scan.get("bc_details") or []
                if _bcd:
                    st.markdown("##### Boundary Conditions in this model")
                    st.caption(
                        "Parsed from the `.u01` text file. *Source / "
                        "station ID / parameter code* are not stored "
                        "in HEC-RAS files - set those in **Tab 3** to "
                        "fetch real-time data, or pick **Constant** to "
                        "override with a fixed value."
                    )
                    _hdr_cols = st.columns([2.2, 1.6, 1.0, 3.2])
                    _hdr_cols[0].markdown("**Boundary**")
                    _hdr_cols[1].markdown("**Type**")
                    _hdr_cols[2].markdown("**Interval**")
                    _hdr_cols[3].markdown("**Bundled data**")

                    for _b in _bcd:
                        _r = st.columns([2.2, 1.6, 1.0, 3.2])
                        _nm = _b.get("name") or "(unnamed)"
                        _area = _b.get("area")
                        _r[0].markdown(
                            f"`{_nm}`"
                            + (f"  ·  *{_area}*" if _area else "")
                        )
                        _r[1].markdown(f"`{_b.get('type', '-')}`")
                        _r[2].markdown(
                            f"`{_b.get('interval') or '-'}`"
                        )
                        # Bundled-data summary.
                        if _b.get("uses_dss"):
                            _dssp = _b.get("dss_path") or "(no path)"
                            _summary = f"DSS-driven · `{_dssp}`"
                        elif _b.get("kind") == "computed":
                            _slope = _b.get("friction_slope")
                            _summary = (
                                f"Normal depth · slope = "
                                f"`{_slope:.5f}`"
                                if _slope is not None
                                else "Normal depth"
                            )
                        elif _b.get("kind") == "structure":
                            _summary = "Internal structure (gate)"
                        elif _b.get("kind") == "rating":
                            _summary = (
                                f"Rating curve · "
                                f"{_b.get('n_values') or '?'} points"
                            )
                        elif _b.get("kind") == "series":
                            _n = _b.get("n_values") or 0
                            _lo = _b.get("value_min")
                            _hi = _b.get("value_max")
                            _mean = _b.get("value_mean")
                            if _b.get("is_constant") and _lo is not None:
                                _summary = (
                                    f"**Constant** `{_lo:.3f}` · "
                                    f"{_n:,} pts"
                                )
                            elif _lo is not None and _hi is not None:
                                _summary = (
                                    f"Varying `{_lo:.1f}` - `{_hi:.1f}` "
                                    f"(mean `{_mean:.1f}`) · {_n:,} pts"
                                )
                            else:
                                _summary = f"{_n:,} inline values"
                        else:
                            _summary = "-"
                        _r[3].markdown(_summary)

        if MODEL_DIR is not None:
            if st.button("Change Model"):
                if _cloud_mode:
                    st.session_state.pop("cloud_model_dir", None)
                    st.session_state.pop("cloud_model_src_uri", None)
                else:
                    st.session_state.browse_path = str(HOST_ROOT)
                for key in list(st.session_state.keys()):
                    if key.startswith("scan_"):
                        del st.session_state[key]
                st.rerun()

        st.divider()
        st.subheader("Output Location")

        if not _cloud_mode:
            # ── LOCAL: output folder browser ───────────────────────────
            if "out_path" not in st.session_state:
                st.session_state.out_path = str(OUTPUTS_ROOT)

            out_current = Path(st.session_state.out_path)

            oc_path, oc_up = st.columns([5, 1])
            with oc_path:
                st.markdown(f"**Saving to:** `{out_current}`")
            with oc_up:
                if out_current != OUTPUTS_ROOT:
                    if st.button(
                        ":arrow_up: Up", key="out_up",
                        width="stretch",
                    ):
                        st.session_state.out_path = str(out_current.parent)
                        st.rerun()

            out_subdirs = _list_subdirs(out_current)
            if out_subdirs:
                o_cols = st.columns(4)
                for idx, dname in enumerate(out_subdirs):
                    with o_cols[idx % 4]:
                        if st.button(
                            f":file_folder: {dname}",
                            key=f"out_dir_{idx}",
                            width="stretch",
                        ):
                            st.session_state.out_path = str(
                                out_current / dname
                            )
                            st.rerun()

            OUTPUT_PARENT = out_current
            if scan is not None:
                _base_name = scan["project_name"].rsplit(".", 1)[0]
                st.caption(
                    f"A run folder named `{_base_name}_<start>_<end>` "
                    f"will be created inside `{OUTPUT_PARENT}`."
                )
        elif DEMO_MODE:
            # Demo - results go to a fixed demo-outputs prefix; the
            # visitor doesn't (and can't) choose where to write.
            st.session_state["cloud_output_uri"] = DEMO_OUTPUT_URI
            _cloud_out_uri = DEMO_OUTPUT_URI
            if DEMO_OUTPUT_URI:
                st.caption(
                    "Results are saved to the demo's output location "
                    "automatically."
                )
            else:
                st.warning(
                    "Demo output location isn't configured "
                    "(`DEMO_OUTPUT_URI`)."
                )
            OUTPUT_PARENT = OUTPUT_DIR
            if scan is not None:
                _base_name = scan["project_name"].rsplit(".", 1)[0]
            try:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        else:
            # ── CLOUD: upload the finished run to an S3 bucket ──────────
            st.caption(
                "Enter the **S3 URI of the folder** where the finished "
                "run should be uploaded. When the simulation completes, "
                "a sub-folder named `<project>_<start>_<end>` is created "
                "there with the inundation results, GIF, results HDF and "
                "summary. The upload runs inside the detached pipeline, "
                "so it survives a closed browser."
            )
            _cloud_out_uri = st.text_input(
                "Results S3 URI",
                placeholder="s3://my-bucket/hecinbox-results/",
                key="cloud_output_uri",
            )
            # The engine must write to a real filesystem first; the
            # pipeline uploads the finished folder to S3 afterwards.
            OUTPUT_PARENT = OUTPUT_DIR
            try:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            if scan is not None:
                _base_name = scan["project_name"].rsplit(".", 1)[0]
                _co = (_cloud_out_uri or "").strip()
                if _co:
                    st.caption(
                        f"Finished run → "
                        f"`{_co.rstrip('/')}/{_base_name}_<start>_<end>/`"
                    )
                else:
                    st.info(
                        "Enter a results S3 URI above so the finished "
                        "run can be uploaded to your cloud bucket."
                    )

        # ── Open a previous run's results ──────────────────────────────
        st.divider()
        st.subheader("Open Previous Results")

        if not _cloud_mode:
            st.caption(
                "Browse to **any folder** that contains finished results - "
                "either a HECinBOX run (`wse_extract.npz` present) **or** a "
                "raw HEC-RAS model folder with a computed plan "
                "(`.prj` + `.pXX.hdf`). HECinBOX will identify the model "
                "and load its inundation map, GIF, time series, and "
                "summary in **Tab 5 · Results**."
            )

            if "results_browse_path" not in st.session_state:
                st.session_state.results_browse_path = str(OUTPUTS_ROOT)
            _rbp = Path(st.session_state.results_browse_path)

            # Quick-jump roots
            _qr1, _qr2, _qr3 = st.columns([1.2, 1.2, 3.6])
            with _qr1:
                if st.button(
                    ":file_folder: Outputs", key="rb_root_out",
                    width="stretch",
                    help=f"Jump to {OUTPUTS_ROOT}",
                ):
                    st.session_state.results_browse_path = str(OUTPUTS_ROOT)
                    st.rerun()
            with _qr2:
                if st.button(
                    ":file_folder: Home", key="rb_root_host",
                    width="stretch",
                    help=f"Jump to {HOST_ROOT}",
                ):
                    st.session_state.results_browse_path = str(HOST_ROOT)
                    st.rerun()

            _rb_path_col, _rb_up_col = st.columns([5, 1])
            with _rb_path_col:
                st.markdown(f"**Current:** `{_rbp}`")
            with _rb_up_col:
                if st.button(
                    ":arrow_up: Up", key="rb_up", width="stretch"
                ):
                    st.session_state.results_browse_path = str(_rbp.parent)
                    st.rerun()

            _rb_subdirs = _list_subdirs(_rbp)
            if _rb_subdirs:
                _rb_cols = st.columns(4)
                for _i, _dn in enumerate(_rb_subdirs):
                    with _rb_cols[_i % 4]:
                        if st.button(
                            f":open_file_folder: {_dn}",
                            key=f"rb_dir_{_i}",
                            width="stretch",
                        ):
                            st.session_state.results_browse_path = str(
                                _rbp / _dn
                            )
                            st.rerun()

            # ── Smart folder-kind detection (v2.7.2+) ────────────────
            # Three cases:
            #   1) HECinBOX folder      → has wse_extract.npz
            #   2) Raw HEC-RAS results  → has .prj + computed .pXX.hdf
            #   3) Neither              → keep browsing
            try:
                from results_extractor import (
                    detect_folder_kind,
                    extract_model_dir_to_folder,
                )
                _fkind = detect_folder_kind(_rbp)
            except Exception as _e:
                _fkind = {"kind": "empty"}
                st.warning(f"Could not classify folder: {_e}")

            _kind = _fkind.get("kind", "empty")

            if _kind == "hecinbox":
                _m = _fkind.get("meta") or {}
                _proj = _m.get("project_name") or "-"
                _ws = _m.get("window_start") or ""
                _we = _m.get("window_end") or ""
                st.success(
                    f"**HECinBOX run** in `{_rbp.name}` - "
                    f"model: `{_proj}`"
                    + (f"  ·  window: `{_ws}` → `{_we}`" if _ws else "")
                )
                if st.button(
                    "Open Results →", key="rb_open", type="primary",
                ):
                    st.session_state["last_output_dir"] = str(_rbp)
                    st.session_state["view_results"] = True
                    # If the originating model still exists, restore it
                    # so Tab 1's Detected Model panel and the other tabs
                    # populate as if the user re-selected it.
                    _md = _fkind.get("model_dir")
                    if _md and Path(_md).exists():
                        st.session_state.browse_path = str(_md)
                        for _k in list(st.session_state.keys()):
                            if _k.startswith("scan_"):
                                del st.session_state[_k]
                    st.rerun()

            elif _kind == "hecras_results":
                _m = _fkind.get("meta") or {}
                _proj = _m.get("project_name") or "-"
                _plan = _m.get("plan_suffix") or "-"
                _geom = _m.get("geom_suffix") or "-"
                _uns = _m.get("unsteady_suffix") or "-"
                _plan_hdf = _fkind.get("plan_hdf")
                st.info(
                    f"**Raw HEC-RAS folder** - model: `{_proj}`  ·  "
                    f"plan: `{_plan}`  ·  geom: `{_geom}`  ·  "
                    f"unsteady: `{_uns}`"
                )
                # Write extract under OUTPUTS_ROOT (writable) since the
                # /host mount is read-only.  Folder name combines the
                # model folder name + plan HDF name for uniqueness.
                _ext_out = (
                    OUTPUTS_ROOT
                    / "external_extracts"
                    / f"{_rbp.name}__{Path(_plan_hdf).stem}"
                    if _plan_hdf else None
                )
                if _plan_hdf:
                    st.caption(
                        f"Will extract from `{Path(_plan_hdf).name}` "
                        f"into `{_ext_out}` (writable outputs root, since "
                        "the model folder is read-only in the container)."
                    )
                if st.button(
                    "Extract & Open Results →",
                    key="rb_open_raw",
                    type="primary",
                ):
                    try:
                        with st.spinner(
                            f"Extracting `{Path(_plan_hdf).name}` … "
                            "this may take a moment for large meshes."
                        ):
                            extract_model_dir_to_folder(_rbp, _ext_out)
                        st.session_state["last_output_dir"] = str(_ext_out)
                        st.session_state["view_results"] = True
                        st.session_state.browse_path = str(_rbp)
                        # Drop cached scans so Tab 1 re-scans the
                        # newly-selected HEC-RAS model.
                        for _k in list(st.session_state.keys()):
                            if _k.startswith("scan_"):
                                del st.session_state[_k]
                        st.success(
                            f"Extracted results to `{_ext_out.name}` - "
                            "opening Tab 5."
                        )
                        st.rerun()
                    except Exception as _e:
                        st.error(f"Could not extract results - {_e}")

            elif _kind == "hecras_model":
                _m = _fkind.get("meta") or {}
                _proj = _m.get("project_name") or "-"
                st.warning(
                    f"This is a HEC-RAS model folder (`{_proj}`) "
                    "but no computed plan HDF (`.pXX.hdf`) was found. "
                    "Run HEC-RAS first to produce results."
                )

            else:
                st.caption(
                    "This folder has no `wse_extract.npz` and no "
                    "HEC-RAS project (`.prj`). Navigate into a completed "
                    "run folder or a HEC-RAS model folder to enable "
                    "**Open Results**."
                )
        else:
            # ── CLOUD: open a finished run from an S3 folder ────────────
            st.caption(
                "Enter the **S3 URI of a finished run folder** (one "
                "that contains a `wse_extract.npz`). HECinBOX "
                "downloads the extracted results and opens its "
                "inundation map, GIF, time series, and summary in "
                "**Tab 5 · Results**."
            )
            _cr_uri = st.text_input(
                "Run folder S3 URI",
                placeholder="s3://my-bucket/hecinbox-results/run_.../",
                key="cloud_results_uri",
            )
            if st.button(
                "Open from cloud →", key="cloud_open_btn", type="primary",
            ):
                import hashlib as _hl2
                _u = (_cr_uri or "").strip()
                if not _u:
                    st.error("Enter a run-folder S3 URI first.")
                else:
                    try:
                        from cloud_storage import download_file
                        _npz_uri = _u.rstrip("/") + "/wse_extract.npz"
                        _dest_dir = (
                            Path("/app/data/cloud_results")
                            / _hl2.md5(_u.encode()).hexdigest()
                        )
                        with st.spinner(
                            f"Downloading results from {_u} …"
                        ):
                            download_file(
                                _npz_uri, _dest_dir / "wse_extract.npz"
                            )
                        st.session_state["last_output_dir"] = str(_dest_dir)
                        st.session_state["view_results"] = True
                        st.success(
                            "Results downloaded - open **Tab 5 · "
                            "Results** to explore them."
                        )
                        st.rerun()
                    except Exception as _e:
                        st.error(f"Could not open cloud results - {_e}")

        if st.session_state.get("view_results"):
            _cur = st.session_state.get("last_output_dir", "")
            if _cur:
                st.info(
                    f"Currently loaded: **{Path(_cur).name}** - open "
                    "**Tab 5 · Results** to explore it."
                )

        _tab_nav(0)

# While a run is in progress Tab 1 renders only its banner, so nothing
# above set MODEL_DIR / scan. Fall back to the remembered scan rather
# than treating the model as unselected: the run is using that model,
# and every other tab needs to describe it correctly.
if scan is None and _active_job_dir():
    scan = st.session_state.get("_last_scan")
    if scan is not None and MODEL_DIR is None:
        _remembered = st.session_state.get("_last_model_dir")
        if _remembered:
            MODEL_DIR = Path(_remembered)

_model_ready = scan is not None


# Tab 2 widget keys that get disabled while a run is in flight.
_TAB2_LOCKABLE = (
    "tz_mode", "model_lst_offset_custom", "rt", "rt_direction",
    "rt_days", "sched", "sched_n", "sched_unit",
)


def _carry_locked_widgets(busy: bool) -> None:
    """Carry Tab 2's values across the enable/disable flip.

    Streamlit folds ``disabled`` into a widget's identity, so flipping it
    orphans the value recorded against the old identity and the widget
    quietly falls back to its default. That is not cosmetic: the run
    direction reverted from Forecast to Hindcast the moment a run
    started, and the chosen model time base reverted to "not chosen",
    which is what raised "choose the model time base first" during a run
    that had one.

    So mirror each value into a plain key while the widgets are live,
    and seed it back before they are rebuilt in the locked state.
    Session state may be seeded before a widget is created; it is only
    assignment *after* creation that Streamlit refuses.
    """
    for k in _TAB2_LOCKABLE:
        mirror = f"_keep_{k}"
        if busy:
            if st.session_state.get(k) is None and mirror in st.session_state:
                st.session_state[k] = st.session_state[mirror]
        elif st.session_state.get(k) is not None:
            st.session_state[mirror] = st.session_state[k]


def _preflight_rows(bc_cfg, precip_cfg, win_start, win_end, tz_off):
    """What each input covers, against the window about to be run.

    Built for the Run tab so the span every source can actually supply
    is visible *before* a run rather than inferred from the log after
    it. Coverage ends come from the same live lookups Tab 2 sizes the
    window with, converted onto the model's clock.

    Returns ``(rows, shortfalls)``; a shortfall is an input that stops
    before the window does.
    """
    rows, short = [], []
    off = timedelta(hours=int(tz_off or 0))

    def _add(what, source, covers_to, detail=""):
        gap = ""
        if covers_to is not None and win_end is not None:
            missing = (win_end - covers_to).total_seconds() / 3600.0
            if missing > 0.5:
                gap = f"stops {missing:.0f} h early"
                short.append((what, source, missing, detail))
        rows.append({
            "Input": what,
            "Source": source,
            "Covers until": (
                covers_to.strftime("%d %b %H:%M") if covers_to
                else "whole window"
            ),
            "Gap": gap or "-",
        })

    for b in (bc_cfg or []):
        name = str(b.get("name", "?")).split("BCLine:")[-1].strip()
        src = str(b.get("source", "none")).lower()
        if src == "none":
            continue
        if src == "forecast":
            prod = str(b.get("forecast_product", ""))
            if prod == "stofs_twl":
                label = f"STOFS · station {b.get('stofs_station', '?')}"
            else:
                label = (
                    f"NWM {b.get('forecast_horizon', '?')} · "
                    f"COMID {b.get('comid', '?')}"
                )
            end = None
            try:
                e = _coverage_end_cached(
                    prod, str(b.get("forecast_horizon", "")),
                    int(b.get("forecast_member", 1) or 1),
                    str(b.get("stofs_domain", "atlantic")),
                )
                end = (e + off) if e else None
            except Exception:
                end = None
            _add(name, label, end)
        elif src == "usgs":
            _add(name, f"USGS · {b.get('station', '?')}", None)
        elif src == "noaa":
            _add(name, f"NOAA · {b.get('station', '?')}", None)
        elif src == "constant":
            _add(name, "Constant value", None)
        else:
            _add(name, src.title(), None)

    if (precip_cfg or {}).get("enabled"):
        mode = str(precip_cfg.get("source", "constant")).lower()
        if mode == "hrrr":
            end, detail = None, ""
            try:
                from precip_gridded import hrrr_coverage_end
                cov = _hrrr_coverage_cached()
                if cov:
                    end = cov[1] + off
                    detail = (
                        f"the {cov[0]:%H}z cycle forecasts {cov[2]} h "
                        f"ahead"
                    )
            except Exception:
                end = None
            _add("Rain on mesh", "HRRR forecast", end, detail)
        elif mode == "aorc":
            _add("Rain on mesh", "AORC observed", None)
        elif mode == "dss":
            _add("Rain on mesh", "DSS upload", None)
        else:
            _add("Rain on mesh", "Constant rate", None)
    return rows, short


@st.cache_data(ttl=600, show_spinner=False)
def _hrrr_coverage_cached():
    """Newest HRRR cycle and how far it reaches, cached 10 minutes."""
    from precip_gridded import hrrr_coverage_end
    return hrrr_coverage_end()


# ── TAB 2: Simulation window ──────────────────────────────────────────
with tab_window:
    # Locked, not hidden: Tab 4 still reads the window from here while a
    # run is going, so the controls stay on screen showing what the run
    # is using - they just cannot be edited, the same as Tabs 1 and 5.
    _win_busy = bool(_active_job_dir())
    _carry_locked_widgets(_win_busy)
    if _win_busy:
        st.warning(
            "**Simulation in progress** - the window is locked and "
            "shows what the current run is using. Switch to "
            "**Tab 4 · Run** to watch progress or stop the run. Use "
            "**Reset** at the top right to abort.",
        )

    if not _model_ready:
        st.info("Select a model in the **Model Folder** tab first.")

    # ── Model time base - the single clock for the whole window and
    # every fetched source (USGS/NOAA/forecast all return UTC and are
    # shifted onto it).
    #
    # v4.8.0: this is now an EXPLICIT, REQUIRED choice with no
    # pre-selection.  Up to v4.7.8 the longitude-derived Local Standard
    # Time offset was silently pre-filled, and a model whose window was
    # actually kept in UTC would run with every fetched series shifted
    # by whole hours - the run completes, the maps look fine, and the
    # only symptom is a sideways-shifted hydrograph in validation.  The
    # scanner's guess is still offered, but the user has to pick it.
    _tz_off = None
    _tz_detected = int((scan or {}).get("lst_offset_hours", 0) or 0)
    if _model_ready:
        _tz_lst_label = (
            f"Local standard time at the model site "
            f"(UTC{_tz_detected:+d}, detected from the model location)"
        )
        _TZ_UTC = "UTC - my model window is already in UTC (offset 0)"
        _TZ_OTHER = "Another offset - let me enter it myself"

        # An armed schedule already carries a time base the user chose
        # explicitly.  Restore it instead of making them pick again on
        # every page reload - re-picking is where they could silently
        # pick *differently* and change the running schedule's clock.
        if "tz_mode" not in st.session_state:
            _sched_tz = None
            try:
                _s = _read_schedule_state() or {}
                if _s.get("enabled"):
                    _sched_tz = (
                        (_s.get("settings_template") or {})
                        .get("time", {})
                        .get("lst_offset_hours")
                    )
            except Exception:
                _sched_tz = None
            if _sched_tz is not None:
                _sched_tz = int(_sched_tz)
                if _sched_tz == 0:
                    st.session_state["tz_mode"] = _TZ_UTC
                elif _sched_tz == _tz_detected:
                    st.session_state["tz_mode"] = _tz_lst_label
                else:
                    st.session_state["tz_mode"] = _TZ_OTHER
                    st.session_state["model_lst_offset_custom"] = _sched_tz

        _tz_choice = st.radio(
            "Model time base (required)",
            [_tz_lst_label, _TZ_UTC, _TZ_OTHER],
            index=None,
            key="tz_mode",
            help=_TIME_BASE_HELP,
            disabled=_win_busy,
        )
        if _tz_choice == _tz_lst_label:
            _tz_off = _tz_detected
        elif _tz_choice == _TZ_UTC:
            _tz_off = 0
        elif _tz_choice == _TZ_OTHER:
            _twcol, _ = st.columns([1, 1])
            with _twcol:
                _tz_off = int(st.number_input(
                    "UTC offset (hours)",
                    min_value=-12, max_value=14,
                    value=int(st.session_state.get(
                        "model_lst_offset_custom", _tz_detected
                    )),
                    step=1, key="model_lst_offset_custom",
                    disabled=_win_busy,
                ))

        if _tz_off is None:
            st.warning(
                "**Choose the model's time base before you continue.** "
                "A HEC-RAS model stores its dates as a bare clock "
                "reading with no time zone, while USGS, NOAA and the "
                "forecast services all publish in UTC. Only you know "
                "which clock your model was built on. Hover the **?** "
                "above for how to decide - picking the wrong one "
                "shifts every fetched series by whole hours without "
                "any error."
            )
        else:
            _tz_txt = f"UTC{_tz_off:+d}" if _tz_off else "UTC"
            st.caption(
                f"🕒 The simulation window below is read as **{_tz_txt}**, "
                f"and every observed and forecast series is shifted onto "
                f"that same clock. Standard time only, no daylight "
                f"saving."
            )
        # Resolved value for Tab 3, Tab 4 and the settings writer.
        st.session_state["model_lst_offset"] = _tz_off
        st.divider()

    # Effective offset for the window arithmetic and previews below.
    # While the choice is still unmade the previews fall back to UTC so
    # the rest of the tab keeps rendering; the run itself is blocked in
    # Tab 4 until a real choice exists.
    _tz_eff = 0 if _tz_off is None else int(_tz_off)

    realtime = st.toggle(
        "Real-time mode (pull the most recent data automatically)",
        key="rt",
        disabled=_win_busy,
    )

    if realtime:
        # v3.0.2 - choose time direction.  Hindcast = backward-looking
        # window (USGS/NOAA observations, NWM analysis_assim).
        # Forecast = forward-looking window (NWM short/medium/long
        # range, STOFS forecast).  The two cases need opposite
        # window arithmetic, so we expose them as an explicit choice
        # rather than hiding the distinction.
        _direction = st.radio(
            "Time direction",
            [
                "Hindcast - pull the past N days "
                "(USGS / NOAA observations, NWM analysis_assim)",
                "Forecast - pull the next N days from now "
                "(NWM short / medium / long range, STOFS forecast)",
            ],
            key="rt_direction",
            horizontal=False,
            help=(
                "Hindcast = look backward in time. Forecast = look "
                "forward in time. Pick the direction that matches "
                "the boundary-condition sources you configured in "
                "Tab 3 - using a Forecast source with a Hindcast "
                "window (or vice-versa) will fetch no data."
            ),
            disabled=_win_busy,
        )
        _is_forecast = _direction.startswith("Forecast")
        # Anchor "now" in the model's Local Standard Time so the
        # real-time window matches the LST-shifted fetched data.
        _now = (
            datetime.utcnow() + timedelta(hours=_tz_eff)
        ).replace(second=0, microsecond=0)

        # v3.0.3 - for a true forecast the window length is dictated
        # by the chosen product horizon, NOT by a free "N days" dial
        # (short=18h, medium=10d, long=30d, STOFS-3D≈4d).  So when
        # forecast BCs are configured we AUTO-SIZE the window from the
        # longest selected horizon and hide the N-days input.  The
        # manual input only reappears for hindcast, or for a forecast
        # built solely on `analysis_assim` (a nowcast with no forward
        # horizon), or before any forecast BC has been configured.
        _auto_window = False
        _fc_rows, _fc_hours = ([], None)
        if _is_forecast:
            _fc_rows, _fc_hours = _forecast_window_from_snapshot()
            _auto_window = _fc_hours is not None

        if _auto_window:
            _fc_hours = max(1.0, float(_fc_hours))
            sim_start_dt = _now
            sim_end_dt = _now + timedelta(hours=_fc_hours)
            sim_end_dt = sim_end_dt.replace(minute=0, second=0,
                                            microsecond=0)
            sim_start = sim_start_dt.date()
            sim_end = sim_end_dt.date()
            _days = _fc_hours / 24.0
            _span_txt = (
                f"{_fc_hours:.0f} h"
                if _fc_hours < 48
                else f"{_days:.1f} days"
            )
            _bind = min(_fc_rows, key=lambda r: r[2]) if _fc_rows else None
            st.success(
                "**Forecast window auto-sized to the span every "
                f"boundary can actually cover:** {_span_txt}  \n"
                f"{sim_start_dt:%Y-%m-%d %H:%M} → "
                f"{sim_end_dt:%Y-%m-%d %H:%M} "
                f"{('UTC%+d' % _tz_eff) if _tz_eff else 'UTC'}"
                + (f"  \nLimited by **{_bind[0]}** (`{_bind[1]}`)."
                   if _bind and len(_fc_rows) > 1 else "")
            )
            if len(_fc_rows) > 1:
                _lines = "\n".join(
                    f"- **{n}** → `{k}` reaches "
                    f"{h:.0f} h / {h / 24.0:.1f} d"
                    + ("" if live else " *(nominal - live reach "
                                       "unavailable)*")
                    + ("  ⟵ sets the window" if h == min(
                        r[2] for r in _fc_rows) else "")
                    for (n, k, h, live) in _fc_rows
                )
                st.caption(
                    "The window is the **overlap** of every boundary's "
                    "forecast, so no boundary runs out of data mid-run. "
                    "A longer-reaching boundary is simply cut short "
                    "here:\n" + _lines
                )
            _stale = [r for r in _fc_rows if not r[3]]
            if _stale:
                st.caption(
                    "Reaches marked *nominal* come from the product's "
                    "advertised horizon because the live cycle listing "
                    "could not be read. Those ignore cycle age, so the "
                    "true reach may be several hours shorter."
                )
            st.caption(
                "Reaches are measured from the newest published cycle, "
                "so they shrink as a cycle ages and jump back up when "
                "the next one lands. To change the forecast length, "
                "pick a different horizon / product on the **Boundary "
                "Conditions** tab."
            )
        else:
            # Manual N-days input: hindcast, nowcast-only forecast, or
            # forecast before any BC is wired up.
            _default_n = 10 if _is_forecast else 7
            realtime_days = st.number_input(
                (
                    "Pull the **next** N days from now"
                    if _is_forecast
                    else "Pull the most recent N days"
                ),
                min_value=1,
                max_value=30 if _is_forecast else 90,
                value=_default_n,
                key="rt_days",
                help=(
                    "No fixed-horizon forecast BC is configured yet "
                    "(e.g. only `analysis_assim`, which is a nowcast). "
                    "Once you set a short/medium/long-range or STOFS "
                    "boundary in Tab 3, this window auto-sizes itself."
                    if _is_forecast else
                    "How far back to look. USGS / NOAA observations "
                    "are available indefinitely; NWM `analysis_assim` "
                    "only covers the most recent ~30 days."
                ),
                disabled=_win_busy,
            )
            if _is_forecast:
                sim_start_dt = _now
                sim_end_dt = _now + timedelta(days=int(realtime_days))
            else:
                sim_start_dt = _now - timedelta(days=int(realtime_days))
                sim_end_dt = _now
            sim_start = sim_start_dt.date()
            sim_end = sim_end_dt.date()
            st.info(
                f"**Window ({'Forecast' if _is_forecast else 'Hindcast'}):"
                f"**  {sim_start_dt:%Y-%m-%d %H:%M} → "
                f"{sim_end_dt:%Y-%m-%d %H:%M} "
                f"{('UTC%+d' % _tz_eff) if _tz_eff else 'UTC'}"
            )

        # ── Canonical real-time window for the auto-scheduler ────────
        # Defined for *every* branch above (auto-sized forecast, manual
        # forecast, hindcast).  Two reasons:
        #   1. The auto-sized forecast branch never assigns
        #      ``realtime_days`` - without this, arming a schedule from
        #      that branch raised ``NameError: name 'realtime_days' is
        #      not defined`` (Tab 4 -> _autoschedule_activate).
        #   2. The daemon needs the span *and direction* so a forecast
        #      schedule re-forecasts forward each run instead of
        #      silently running a backward hindcast.
        _rt_window_hours = max(
            1,
            int(round((sim_end_dt - sim_start_dt).total_seconds() / 3600.0)),
        )
        realtime_days = max(1, int(round(_rt_window_hours / 24.0)))

        # Friendly consistency check: warn if the user's BC source
        # choices don't match the time-direction choice.  Avoids the
        # silent "no data fetched" mode where the user wonders why
        # the run fell back to model defaults.
        try:
            _bcs = (
                st.session_state.get("_bc_config_snapshot") or []
            )
            _has_forecast_bc = any(
                str(b.get("source", "")).lower() == "forecast"
                for b in _bcs
            )
            _has_obs_bc = any(
                str(b.get("source", "")).lower() in ("usgs", "noaa")
                for b in _bcs
            )
            if _is_forecast and _has_obs_bc and not _has_forecast_bc:
                st.warning(
                    "You picked **Forecast** direction but all "
                    "active BCs use **observation** sources (USGS / "
                    "NOAA). Observations don't exist for future "
                    "windows - switch to **Hindcast** or change the "
                    "BC sources to **Forecast (NWM v.3)** / "
                    "**Forecast (STOFS)** in Tab 3."
                )
            elif not _is_forecast and _has_forecast_bc:
                st.warning(
                    "You picked **Hindcast** direction but at "
                    "least one BC uses a **Forecast (NWM v.3)** / "
                    "**Forecast (STOFS)** source. The operational "
                    "NWM/STOFS buckets only "
                    "retain ~30 days of cycles, so an older Hindcast "
                    "window will return no data. Switch direction "
                    "to **Forecast** or change the BC source."
                )
        except Exception:
            pass

        schedule = st.toggle(
            "Enable auto-run scheduling",
            key="sched",
            disabled=_win_busy,
        )
        if schedule:
            _iv1, _iv2 = st.columns([2, 1])
            with _iv1:
                _interval_n = st.number_input(
                    "Re-run interval",
                    min_value=1,
                    max_value=999,
                    value=6,
                    key="sched_n",
                    disabled=_win_busy,
                )
            with _iv2:
                _interval_unit = st.selectbox(
                    "Unit",
                    ["Minutes", "Hours"],
                    index=1,
                    key="sched_unit",
                    disabled=_win_busy,
                )
            interval_minutes = int(_interval_n) * (
                1 if _interval_unit == "Minutes" else 60
            )
            st.caption(
                f"The pipeline will re-run automatically every "
                f"**{_fmt_interval(interval_minutes)}** while this "
                f"page stays open."
            )
        else:
            interval_minutes = None
    else:
        sim_start_dt = sim_end_dt = None
        # Key the date widgets on the model, and remember which model
        # that was. Without the fallback the key flipped to "none" the
        # moment `scan` went missing, which silently swapped in a second
        # pair of date inputs: anything typed into them was thrown away
        # when the real pair came back.
        _tag = (scan or {}).get("project_name") or st.session_state.get(
            "_last_window_tag", "none"
        )
        if scan and scan.get("project_name"):
            st.session_state["_last_window_tag"] = scan["project_name"]
        # Default to the model's own window. There is no sensible
        # date to invent when no model is loaded, so fall back to the
        # last week rather than to one particular model's dates - the
        # old default was Brays Bayou's window and showed up under
        # every other model.
        _today = datetime.utcnow().date()
        _def_start = _parse_model_date(
            (scan or {}).get("original_start"), _today - timedelta(days=7)
        )
        _def_end = _parse_model_date(
            (scan or {}).get("original_end"), _today
        )
        if scan and scan.get("original_start"):
            st.caption(
                f"Defaults from the model's detected window: "
                f"`{scan.get('original_start')}` → "
                f"`{scan.get('original_end')}`"
                + (f"  (on the model's own clock, "
                   f"{('UTC%+d' % _tz_off) if _tz_off else 'UTC'})"
                   if _tz_off is not None else "")
            )
        c1, c2 = st.columns(2)
        with c1:
            sim_start = st.date_input(
                "Start date", value=_def_start, key=f"sd_{_tag}",
                disabled=_win_busy,
            )
        with c2:
            sim_end = st.date_input(
                "End date", value=_def_end, key=f"ed_{_tag}",
                disabled=_win_busy,
            )
        schedule = False
        interval_minutes = None

    # Stash the window so Tab 3/4 source-compatibility checks can read
    # it regardless of which tab is rendering (tabs share one script run).
    st.session_state["_win_start"] = sim_start
    st.session_state["_win_end"] = sim_end

    _tab_nav(1)


# ── TAB 3: Boundary conditions ────────────────────────────────────────
bc_config: list[dict] = []
precip_config: dict = {"enabled": False}
with tab_bc:
    if _active_job_dir():
        st.info(
            "A run is currently in progress - your changes here "
            "will apply to the **next** scheduled run after you "
            "click **Update schedule with current settings** "
            "in **Tab 4 · Run**.",
        )

    if not _model_ready:
        st.info("Select a model in the **Model Folder** tab first.")
    bc_lines = scan.get("bc_lines", []) if scan else []
    if _model_ready and not bc_lines:
        st.warning("No boundary conditions found in the model.")
    if _model_ready and scan.get("needs_plan_hdf"):
        st.info(
            "This model has not been computed before - the plan HDF will "
            "be created automatically and geometry preprocessing will run "
            "before the first simulation."
        )

    # Locator map - domain outline with a numbered red dot per boundary,
    # plus a blue dot for whatever source that boundary is assigned to.
    # The numbers match the BC blocks below, so with multiple upstream
    # inflows you can tell which gauge goes with which boundary - and
    # see how far the gauge actually sits from the inflow it drives.
    if _model_ready and bc_lines:
        _src_pts = _bc_source_points(bc_lines)
        _mc1, _mc2, _mc3 = st.columns([2, 1, 1])
        with _mc2:
            _bc_base = st.selectbox(
                "Basemap",
                list(_ESRI_TILES),
                key="bc_map_base",
                help=(
                    "Esri tiles - no API key, so they keep working "
                    "where the default CARTO basemap serves a "
                    "placeholder tile."
                ),
            )
        with _mc3:
            _sug_r = st.number_input(
                "Suggest sources within (km)",
                min_value=0.0, max_value=50.0, value=0.0, step=1.0,
                key="bc_suggest_km",
                help=(
                    "Set a radius to draw every USGS gauge and NOAA "
                    "tide station near your boundaries, with their IDs "
                    "listed below the map. Nothing is applied - it is "
                    "there so you can see what exists without leaving "
                    "the app, then type the ID into the field you want."
                ),
            )
        _cands, _cand_problems = {}, []
        if _sug_r and _sug_r > 0:
            with st.spinner(f"Looking for sources within {_sug_r:g} km…"):
                try:
                    _cands, _cand_problems = _nearby_sources(
                        bc_lines, float(_sug_r)
                    )
                except Exception as _e:
                    _cand_problems = [(None, str(_e))]
        _bc_fig = _bc_location_map(
            bc_lines, scan.get("bc_geometry"),
            sources=_src_pts, basemap=_bc_base,
            candidates=_cands, radius_km=float(_sug_r or 0.0),
        )
        if _bc_fig is not None:
            with _mc1:
                st.caption(
                    "**Boundary locations** - each numbered red dot is "
                    "a boundary below; a blue dot of the same number is "
                    "the source you assigned to it, joined by a line. "
                    "A source sitting right on its boundary (an NWM "
                    "reach usually does) shows as the blue dot inside "
                    "the red one. Hover either for its name and "
                    "separation."
                )
            st.plotly_chart(
                _bc_fig, use_container_width=True,
                config={
                    "displaylogo": False,
                    # The mode bar was hidden and scroll zoom left off,
                    # which left no way at all to zoom this map.
                    "scrollZoom": True,
                    "modeBarButtonsToAdd": [
                        "zoomInMapbox", "zoomOutMapbox", "resetViewMapbox",
                    ],
                    "modeBarButtonsToRemove": [
                        "select2d", "lasso2d", "toggleHover",
                    ],
                },
            )
            _far = [
                (i, sp) for i, sp in _src_pts.items()
                if bc_lines[i].get("lon") is not None
                and "midpoint" not in sp["kind"]
                and _haversine_km(
                    bc_lines[i]["lon"], bc_lines[i]["lat"],
                    sp["lon"], sp["lat"],
                ) > 15.0
            ]
            if _cands:
                _rows = []
                for _i, _lst in sorted(_cands.items()):
                    _bcn = str(bc_lines[_i].get("name", "")).split(
                        "BCLine:")[-1].strip()
                    for _c in _lst:
                        _rows.append({
                            "Boundary": f"{_i + 1}. {_bcn}",
                            "Source": _c["kind"],
                            "ID": _c["id"],
                            "Name": _c["name"],
                            "Measures": (
                                " + ".join(_c["params"])
                                if _c.get("params")
                                else ("water level"
                                      + (" (STOFS point)"
                                         if _c.get("stofs") else ""))
                            ),
                            "km": round(_c["dist"], 2),
                        })
                st.caption(
                    f"**{len(_rows)} source(s) within {_sug_r:g} km** "
                    "- orange dots on the map. Copy an ID into the "
                    "matching field below; nothing here is applied for "
                    "you. Check that a gauge is on the same watercourse "
                    "as the boundary: proximity alone does not make it "
                    "the right source."
                )
                import pandas as _spd
                st.dataframe(
                    _spd.DataFrame(_rows), width="stretch",
                    hide_index=True,
                )
            elif _sug_r and _sug_r > 0 and not _cand_problems:
                st.caption(
                    f"No USGS gauge or NOAA station within "
                    f"{_sug_r:g} km of any boundary. Try a larger "
                    "radius."
                )
            if _cand_problems:
                _who = ", ".join(
                    (f"boundary {i + 1}" if i is not None else "the search")
                    for i, _ in _cand_problems
                )
                st.warning(
                    f"**The gauge search could not complete for "
                    f"{_who}.** "
                    + _cand_problems[0][1]
                    + ". Any list above is therefore incomplete - "
                    "re-enter the radius to try again rather than "
                    "reading it as 'nothing nearby'."
                )
            if _far:
                st.caption(
                    "Note: "
                    + "; ".join(
                        f"**{i + 1}** is {_haversine_km(bc_lines[i]['lon'], bc_lines[i]['lat'], sp['lon'], sp['lat']):.0f} km "
                        f"from its {sp['kind']}"
                        for i, sp in _far
                    )
                    + ". Check that the source really represents that "
                    "boundary."
                )

    # The model time zone lives in Tab 2 (it governs the sim window);
    # all fetched USGS/NOAA/forecast data is shifted to it.
    if _model_ready and bc_lines:
        _tz_now = st.session_state.get("model_lst_offset")
        if _tz_now is None:
            st.warning(
                "⏱️ The **model time base** has not been chosen yet. "
                "Set it in **Tab 2 · Simulation Window** before you "
                "run, otherwise every series fetched below lands on "
                "the wrong clock."
            )
        else:
            _tz_now = int(_tz_now)
            _tz_txt = f"UTC{_tz_now:+d}" if _tz_now else "UTC"
            st.caption(
                f"⏱️ All observed & forecast data is fetched on the "
                f"model's clock (**{_tz_txt}**), set in "
                f"**Tab 2 · Simulation Window**."
            )

    # "Leave unchanged" is only offered while the Tab 2 window matches
    # the model's native one.  The model's bundled data carries absolute
    # native-year timestamps and stores no gauge IDs, so on a re-timed
    # window it cannot represent (or re-fetch) real data - the user must
    # assign a real source per boundary instead (v4.7.8).
    _retimed = False
    if scan and scan.get("original_start"):
        if st.session_state.get("rt"):
            # A real-time window is anchored to *now*; it can never
            # match an uploaded model's historical window.
            _retimed = True
        else:
            _ns = _parse_model_date(scan.get("original_start"), None)
            _ne = _parse_model_date(scan.get("original_end"), None)
            _ws = st.session_state.get("_win_start")
            _we = st.session_state.get("_win_end")
            if _ns is not None and _ws is not None:
                _retimed = (_ws != _ns) or (
                    _ne is not None and _we is not None and _we != _ne
                )
    # Streamlit can't gray out a single selectbox option, so the
    # disabled state is emulated: the entry stays visible with an
    # "(unavailable)" marker, and a pre-render sanitizer bounces any
    # attempt to select it back to a window-appropriate source.
    _UNCHANGED = "Leave unchanged"
    _UNCHANGED_OFF = "Leave unchanged (unavailable - needs native window)"
    if _retimed and any(b.get("injectable") for b in bc_lines):
        st.info(
            "**\"Leave unchanged\" is disabled: your Tab 2 window "
            "differs from the model's native window** "
            f"(`{scan.get('original_start')}` → "
            f"`{scan.get('original_end')}`). The model's built-in data "
            "carries its original dates and stores no gauge IDs, so it "
            "cannot supply real data for the new window. Pick a data "
            "source for each boundary below - or set the Tab 2 window "
            "back to the native dates to re-enable the option."
        )

    for i, bc in enumerate(bc_lines):
        nice = bc["name"].replace("2D: ", "").replace(" BCLine:", " -")
        _num = f"{i + 1}. "
        if not bc["injectable"]:
            st.markdown(
                f"**{_num}{nice}**  ·  _{bc['bc_type']}_ - fixed "
                f"boundary, not data-driven (left unchanged)."
            )
            st.divider()
            continue

        st.markdown(
            f"**{_num}{nice}**  ·  detected type: `{bc['bc_type']}`"
        )
        _src_options = [
            _UNCHANGED, "USGS", "NOAA",
            "Constant",
            "Forecast (NWM v.3)", "Forecast (STOFS)",
        ]
        _bc_key = f"bc_src_{i}"
        _bounced = False
        if _retimed:
            _src_options[0] = _UNCHANGED_OFF
            # The disabled entry must never end up selected.  Two ways
            # it could: the user just clicked it (bounce it back with a
            # warning below), or a previously saved / schedule-hydrated
            # plain "Leave unchanged" survived into this session (remap
            # silently - the banner above already explains).  Either
            # would otherwise crash the selectbox (value not in options).
            if st.session_state.get(_bc_key) == _UNCHANGED_OFF:
                _bounced = True
            # None too: on a fresh session the widget would otherwise
            # default to index 0 - the disabled entry itself.
            if st.session_state.get(_bc_key) in (
                None, _UNCHANGED, _UNCHANGED_OFF,
            ):
                st.session_state[_bc_key] = (
                    "Forecast (NWM v.3)"
                    if _window_class() == "future" else "USGS"
                )
        elif st.session_state.get(_bc_key) == _UNCHANGED_OFF:
            # Window set back to native - restore the real option.
            st.session_state[_bc_key] = _UNCHANGED
        src = st.selectbox(
            "Data source",
            _src_options,
            key=_bc_key,
        )
        if _bounced:
            st.warning(
                "**\"Leave unchanged\" can't be selected for this "
                "window.** The model's built-in data only exists for "
                f"`{scan.get('original_start')}` → "
                f"`{scan.get('original_end')}`. Pick a real source, or "
                "set the Tab 2 dates back to the native window."
            )
        # Keep the source consistent with the Tab 2 window (warn, don't block).
        _wc = _window_class()
        if _wc == "past" and src in ("Forecast (NWM v.3)", "Forecast (STOFS)"):
            st.warning(
                "Your **Tab 2 window is historical** (it ends in the "
                "past), but a **Forecast** source only produces data from "
                "*now* forward - this BC will likely come back empty. Use "
                "**USGS**/**NOAA** observed for a past event, or set a "
                "real-time/future window in Tab 2."
            )
        elif _wc == "future" and src in ("USGS", "NOAA"):
            st.warning(
                "Your **window reaches now/the future**, but "
                f"**{src}** is *observed* data and stops at the present - "
                "the forecast part of the window has no observations. Use a "
                "**Forecast** source for a forward-looking run."
            )
        entry = {
            "hdf_key": bc["hdf_key"],
            "name": bc["name"],
            "bc_type": bc["bc_type"],
            "source": "none",
        }
        if src == "Constant":
            # The constant is written verbatim into DSS as a steady time
            # series at the BC's own interval, so the value MUST be in
            # the model's native units / datum (whatever the .u01 file
            # uses).  HECinBOX does NOT perform any datum conversion.
            _cu = unit_labels(is_si((scan or {}).get("unit_system")))
            _default_unit = (
                _cu["flow"] if bc["bc_type"] == "flow" else _cu["length"]
            )
            st.info(
                "**Constant boundary - units are the model's own.**\n\n"
                "Enter the value **exactly as it appears in the model's "
                "`.u01` file** - HECinBOX does **not** convert datums or "
                "units. The number is written into DSS unchanged.\n\n"
                "- For a **stage** boundary, the model's vertical datum "
                "controls the meaning of the number - NGVD29 ft, NAVD88 "
                "ft, ft above MHHW, or metres. *Example: the Kalamazoo "
                "downstream stage in `.u01` is `628.14` (ft NGVD29) - "
                "you would enter `628.14` here.* If you want to think "
                "in MHHW for a coastal model, do that conversion "
                "yourself and enter the MHHW number - because that is "
                "what your model expects.\n"
                "- For a **flow** boundary, the value is cfs (English "
                "units) or m³/s (SI units), matching the model's "
                "configuration.\n"
                "- The constant is applied for the **entire simulation "
                "window** at the BC's existing `Interval=` from `.u01`."
            )
            c1, c2 = st.columns(2)
            with c1:
                entry["constant_value"] = st.number_input(
                    f"Constant value ({_default_unit}, model native)",
                    value=0.0,
                    step=0.01,
                    format="%.4f",
                    key=f"bc_cv_{i}",
                )
            with c2:
                entry["constant_unit_label"] = st.text_input(
                    "Unit / datum label (informational only)",
                    value=(
                        (f"{_cu['length']} NGVD29")
                        if bc["bc_type"] == "stage"
                        else _cu["flow"]
                    ),
                    key=f"bc_cu_{i}",
                    help=(
                        "Free-text label for your records. Not used by "
                        "the engine - useful when you re-open the run "
                        "later and want to remember what datum/unit you "
                        "used."
                    ),
                )
            entry["source"] = "constant"
        elif src in ("Forecast (NWM v.3)", "Forecast (STOFS)"):
            # v3.0.3 - forecast sources split into two explicit
            # dropdown entries so users aren't confused about which
            # product family they're wiring up:
            #   • Forecast (NWM v.3) - National Water Model streamflow
            #     (flow BCs directly; stage BCs via a rating curve).
            #   • Forecast (STOFS)   - STOFS-3D coastal Total Water
            #     Level (stage / tidal BCs only).
            # Both share source="forecast" downstream; the specific
            # product is carried in forecast_product, and the fixed
            # product horizon is carried in forecast_horizon so Tab 2
            # can auto-size the forecast simulation window.
            entry["source"] = "forecast"
            _is_stofs = src == "Forecast (STOFS)"

            if _is_stofs:
                # ---- STOFS-3D Total Water Level (coastal) ---------
                if bc["bc_type"] == "flow":
                    st.warning(
                        "**STOFS produces water level, not "
                        "streamflow.** It can't drive a *flow* "
                        "boundary. For a flow boundary, use "
                        "**Forecast (NWM v.3)** instead."
                    )
                    entry["source"] = "none"
                else:
                    st.info(
                        "**Forecast source - STOFS-3D Total Water "
                        "Level.**\n\n"
                        "STOFS combines astronomical tide + storm "
                        "surge + steric effects + wave setup into a "
                        "single signal - exactly what a coastal HEC-"
                        "RAS downstream **stage** BC expects. NWM "
                        "stops at the coastline; STOFS is its coastal "
                        "counterpart.\n\n"
                        "- **Atlantic** covers the U.S. East Coast + "
                        "Gulf of Mexico (Houston / Galveston / Brays "
                        "Bayou).\n"
                        "- **Pacific** covers the U.S. West Coast.\n\n"
                        "Output is in metres above mean sea level "
                        "(MSL) - automatically converted to feet for "
                        "English-unit models.\n\n"
                        "**The forecast length is fixed by the product "
                        "(STOFS-3D ≈ 4 days)** - you do not pick a "
                        "number of days; the Simulation-Window tab "
                        "sizes the forecast window automatically."
                    )
                    _c1, _c2 = st.columns(2)
                    with _c1:
                        entry["stofs_station"] = st.text_input(
                            "NOAA tide station ID",
                            key=f"bc_st_{i}",
                            help=(
                                "Same 7-digit NOAA CO-OPS station "
                                "ID you would use for direct tide-"
                                "gauge fetch - e.g. 8770777 for "
                                "Manchester, TX. STOFS interpolates "
                                "TWL at the station's mesh node."
                            ),
                        )
                    with _c2:
                        entry["stofs_domain"] = st.selectbox(
                            "STOFS domain",
                            ["atlantic", "pacific"],
                            key=f"bc_fc_dom_{i}",
                        )
                    _c3s, _c4s = st.columns(2)
                    with _c3s:
                        entry["stofs_datum"] = st.selectbox(
                            "Vertical datum",
                            ["NAVD88", "MSL"],
                            key=f"bc_fc_dat_{i}",
                            help=(
                                "STOFS publishes water level in metres "
                                "above MSL. Pick the datum your model's "
                                "terrain uses. NAVD88 applies the "
                                "station's published MSL→NAVD88 shift; "
                                "MSL writes the raw series."
                            ),
                        )
                    with _c4s:
                        _off = _stofs_datum_offset(
                            st.session_state.get(f"bc_st_{i}", ""),
                            entry["stofs_datum"],
                        )
                        st.write("")
                        if entry["stofs_datum"] == "MSL":
                            st.caption("No shift - series stays on MSL.")
                        elif _off is None:
                            st.caption(
                                "No published NAVD88 for this station - "
                                "the series will stay on MSL."
                            )
                        else:
                            st.caption(
                                f"Shift applied: **{_off:+.3f} m** "
                                f"(MSL → NAVD88)."
                            )
                    entry["forecast_product"] = "stofs_twl"
                    # Carry a horizon key so Tab 2 can auto-size the
                    # forecast window (STOFS-3D ≈ 4-day horizon).
                    entry["forecast_horizon"] = (
                        "stofs_3d_pacific"
                        if entry["stofs_domain"] == "pacific"
                        else "stofs_3d_atlantic"
                    )
                    _explain_horizon(entry["forecast_horizon"])

            elif bc["bc_type"] == "flow":
                # ---- NWM v.3 streamflow - FLOW boundary -----------
                st.info(
                    "**Forecast source - National Water Model "
                    "(NWM v3.0).**  Drives this boundary from NWM "
                    "streamflow (CHRTOUT) instead of a real-time "
                    "gauge.  **The forecast length is fixed by the "
                    "chosen horizon** (short = 18 h, medium = 10 d, "
                    "long = 30 d) - the Simulation-Window tab sizes "
                    "the forecast window automatically."
                )
                _nu = unit_labels(is_si((scan or {}).get("unit_system")))
                st.markdown(
                    "**Product:** _NWM streamflow_ (CHRTOUT) at the "
                    "matching NHDPlus reach. Output in m³/s, "
                    f"automatically converted to {_nu['flow']} for the model."
                )
                _c1, _c2, _c3 = st.columns(3)
                with _c1:
                    entry["comid"] = st.text_input(
                        "NHDPlus COMID (feature_id)",
                        key=f"bc_fc_comid_{i}",
                        help=(
                            "The NHDPlus number of the reach this "
                            "boundary sits on. To find it: click the "
                            "river at your boundary on water.noaa.gov/"
                            "map and read its reach ID, or set a "
                            "radius in 'Suggest sources within (km)' "
                            "above the map. (The USGS NLDI service "
                            "also returns it for a point: "
                            "api.water.usgs.gov/nldi/linked-data/"
                            "comid/position?coords=POINT(lon lat)"
                            "&f=json)"
                        ),
                    )
                with _c2:
                    entry["forecast_horizon"] = st.selectbox(
                        "Forecast horizon",
                        [
                            "analysis_assim",  # current state
                            "short_range",     # 18 h
                            "medium_range",    # 10 d
                            "long_range",      # 30 d
                        ],
                        index=2,
                        key=f"bc_fc_h_{i}",
                    )
                with _c3:
                    entry["forecast_member"] = st.number_input(
                        "Ensemble member",
                        min_value=1, max_value=7, value=1,
                        key=f"bc_fc_m_{i}",
                        help=(
                            "Medium-range has 7 members (1-7); "
                            "long-range has 4 (1-4). Member 1 is "
                            "the deterministic forecast."
                        ),
                    )
                _explain_horizon(entry["forecast_horizon"])
                entry["forecast_product"] = "nwm_q"

            else:
                # ---- NWM v.3 streamflow - STAGE boundary ----------
                # NWM Q must be converted via a rating curve.
                st.info(
                    "**Forecast source - National Water Model "
                    "(NWM v3.0).**  **The forecast length is fixed "
                    "by the chosen horizon** (short = 18 h, medium = "
                    "10 d, long = 30 d) - the Simulation-Window tab "
                    "sizes the forecast window automatically."
                )
                st.markdown(
                    "**NWM does not produce stage directly** - it "
                    "produces streamflow Q. To drive a stage BC "
                    "from NWM we must convert Q → stage through a "
                    "rating curve. Choose which curve to use:"
                )
                _path = st.radio(
                    "Rating-curve source",
                    [
                        "Path A - USGS empirical rating "
                        "(recommended where a USGS gauge exists)",
                        "Path B - HAND synthetic rating "
                        "(universal fallback for ungauged reaches)",
                    ],
                    key=f"bc_fc_path_{i}",
                    horizontal=False,
                )

                if True:
                    if _path.startswith("Path A"):
                        st.info(
                            "**Path A - USGS empirical rating "
                            "curve.**\n\n"
                            "Pulls the **published USGS stage-"
                            "discharge rating** for a co-located "
                            "gauge from `waterdata.usgs.gov`. The "
                            "rating is **empirically calibrated** "
                            "against field measurements at that "
                            "exact location, so this is the most "
                            "defensible choice when a USGS gauge "
                            "sits at (or very near) your boundary.\n\n"
                            "**How it works:**\n"
                            "1. Fetch NWM Q forecast for the "
                            "boundary's NHDPlus reach (COMID).\n"
                            "2. Fetch the USGS rating-curve table "
                            "for the gauge.\n"
                            "3. Interpolate stage(Q) for each NWM "
                            "timestep.\n"
                            "4. Write the resulting stage time "
                            "series to DSS (in feet).\n\n"
                            "**Caveats:**\n"
                            "- Discharge values outside the rating "
                            "table are *clamped* (flagged in the "
                            "run log).\n"
                            "- The rating curve must exist for the "
                            "gauge - some seasonal gauges have only "
                            "stage records.\n"
                            "- The rating represents the gauge's "
                            "channel cross-section; if your "
                            "boundary is far from the gauge the "
                            "stage may not transfer faithfully."
                        )
                        _c1, _c2 = st.columns(2)
                        with _c1:
                            entry["comid"] = st.text_input(
                                "NHDPlus COMID (for NWM Q)",
                                key=f"bc_fc_comid_{i}",
                            )
                        with _c2:
                            entry["rating_site_no"] = st.text_input(
                                "USGS gauge for rating curve",
                                key=f"bc_st_{i}",
                                help=(
                                    "8-digit USGS site number, e.g. "
                                    "08075000. Must have a "
                                    "published rating curve."
                                ),
                            )
                        entry["forecast_product"] = "nwm_q_usgs_rating"

                    else:
                        st.info(
                            "**Path B - HAND synthetic rating "
                            "curve.**\n\n"
                            "Pulls a **synthetic stage-discharge "
                            "rating** derived from Height-Above-"
                            "Nearest-Drainage (HAND) terrain "
                            "analysis, published by NOAA's National "
                            "Water Prediction Service (NWPS) for "
                            "every NHDPlus reach. Use this **where "
                            "no USGS rating exists** (ungauged "
                            "reaches, headwaters, tributaries).\n\n"
                            "**How it works:**\n"
                            "1. Fetch NWM Q forecast for the "
                            "boundary's NHDPlus reach (COMID).\n"
                            "2. Fetch the HAND synthetic rating "
                            "from the NWPS API for the same reach "
                            "(or a user-supplied reach).\n"
                            "3. Interpolate stage(Q) for each "
                            "timestep.\n"
                            "4. Write stage to DSS (in feet).\n\n"
                            "**Caveats:**\n"
                            "- HAND ratings are **modelled**, not "
                            "calibrated against measurements. "
                            "Accuracy depends on the DEM resolution "
                            "and the Manning's-n assumed by NOAA.\n"
                            "- Coverage is universal across NHDPlus "
                            "reaches, but accuracy is best on "
                            "well-defined channels and degrades on "
                            "low-gradient or braided reaches.\n"
                            "- Prefer Path A whenever a co-located "
                            "USGS gauge is available."
                        )
                        _c1, _c2 = st.columns(2)
                        with _c1:
                            entry["comid"] = st.text_input(
                                "NHDPlus COMID (for NWM Q)",
                                key=f"bc_fc_comid_{i}",
                            )
                        with _c2:
                            entry["hand_reach_id"] = st.text_input(
                                "HAND reach ID (default = COMID)",
                                key=f"bc_fc_hand_{i}",
                                help=(
                                    "Usually the same as the COMID. "
                                    "Override only if NOAA publishes "
                                    "the HAND rating under a "
                                    "different reach identifier."
                                ),
                            )
                        entry["forecast_product"] = "nwm_q_hand_rating"

                # Common to both Path A and Path B (NWM stage)
                _c1, _c2 = st.columns(2)
                with _c1:
                    entry["forecast_horizon"] = st.selectbox(
                        "Forecast horizon",
                        [
                            "analysis_assim", "short_range",
                            "medium_range", "long_range",
                        ],
                        index=2,
                        key=f"bc_fc_h_{i}",
                    )
                with _c2:
                    entry["forecast_member"] = st.number_input(
                        "Ensemble member",
                        min_value=1, max_value=7, value=1,
                        key=f"bc_fc_m_{i}",
                    )
                _explain_horizon(entry["forecast_horizon"])
        elif src == "USGS":
            default_param = "00060" if bc["bc_type"] == "flow" else "00065"
            c1, c2 = st.columns(2)
            with c1:
                entry["station"] = st.text_input(
                    "USGS station ID", key=f"bc_st_{i}",
                    help=_USGS_STATION_HELP,
                )
                entry["parameter"] = st.text_input(
                    "Parameter code", value=default_param,
                    key=f"bc_pc_{i}", help=_USGS_PARAM_HELP,
                )
            with c2:
                entry["timestep_minutes"] = st.number_input(
                    "Timestep (min)", value=15, min_value=1, key=f"bc_ts_{i}"
                )
            entry["source"] = "usgs"
        elif src == "NOAA":
            c1, c2 = st.columns(2)
            with c1:
                entry["station"] = st.text_input(
                    "NOAA station ID", key=f"bc_st_{i}",
                    help=_NOAA_STATION_HELP,
                )
                entry["datum"] = st.selectbox(
                    "Datum",
                    ["NAVD", "MLLW", "MSL", "STND"],
                    key=f"bc_dt_{i}",
                )
                entry["units"] = st.selectbox(
                    "Units", ["english", "metric"], key=f"bc_un_{i}"
                )
            with c2:
                entry["timestep_minutes"] = st.number_input(
                    "Timestep (min)",
                    value=60,
                    min_value=1,
                    key=f"bc_tsn_{i}",
                )
                st.caption(
                    "Fetched in UTC and shifted to the model's Local "
                    "Standard Time (set above), so no per-station time "
                    "zone is needed."
                )
            # Pipeline forces gmt then applies the single model LST
            # offset - no per-BC time-zone choice.
            entry["time_zone"] = "gmt"
            entry["source"] = "noaa"

        bc_config.append(entry)
        st.divider()

    # Snapshot for Tab 2's hindcast-vs-forecast consistency check.
    st.session_state["_bc_config_snapshot"] = list(bc_config)

    # ── Rain on Mesh - mesh-wide precipitation forcing (v3.2.0) ──────
    # Precipitation is NOT a perimeter BC line: in HEC-RAS it lives in
    # the Unsteady Flow file's Meteorological Data block and applies to
    # every cell of every 2D flow area.  Hence its own section here
    # rather than another row in the BC list above.
    if _model_ready:
        st.subheader("Rain on Mesh")
        # What the *native* model declares - drives the confirm gate
        # below.  A model that never used rain on mesh shouldn't silently
        # gain it from a stale toggle, so we ask before exposing the
        # controls.
        _native_precip = (scan or {}).get("precipitation") or {}
        _native_precip_on = bool(_native_precip.get("enabled"))

        precip_on = st.toggle(
            "Enable rain on mesh (precipitation)",
            key="precip_on",
            help=(
                "Applies rainfall directly onto the 2D mesh - every "
                "cell of every 2D flow area receives it, in addition "
                "to the boundary-condition inflows above."
            ),
        )
        # Re-ask the confirmation every time the toggle is switched back
        # on for a model that doesn't natively use rain.
        if not precip_on:
            st.session_state.pop("_precip_confirm_add", None)

        _precip_confirmed = bool(st.session_state.get("_precip_confirm_add"))
        _precip_needs_confirm = (
            precip_on and not _native_precip_on and not _precip_confirmed
        )

        if _precip_needs_confirm:
            # The native model has precipitation disabled - make the user
            # explicitly opt in before showing the mode / value controls.
            st.warning(
                "**This model does not natively use rain on mesh.**\n\n"
                "The selected HEC-RAS model has precipitation **disabled** "
                "in its Meteorological Data. Adding rain on mesh here will "
                "introduce a forcing the original model was not built "
                "with.\n\n"
                "**Are you sure you want to add rain on mesh to this "
                "run?**"
            )

            def _precip_accept() -> None:
                # Runs before widgets re-instantiate, so it may set the
                # mode/value widget keys safely.  Default to Constant -
                # "Leave unchanged" is meaningless for a model with no
                # precipitation of its own.
                st.session_state["_precip_confirm_add"] = True
                st.session_state["precip_mode"] = "Constant"

            def _precip_decline() -> None:
                # Flip the toggle back off and drop the confirmation.
                st.session_state["precip_on"] = False
                st.session_state.pop("_precip_confirm_add", None)

            _yc1, _yc2, _ = st.columns([1, 1, 3])
            _yc1.button(
                "Yes, add rain",
                key="precip_confirm_yes",
                type="primary",
                width="stretch",
                on_click=_precip_accept,
            )
            _yc2.button(
                "No",
                key="precip_confirm_no",
                width="stretch",
                on_click=_precip_decline,
            )
            # precip_config stays {"enabled": False} until they accept.
        elif precip_on:
            _precip_opts = [
                _UNCHANGED, "Constant",
                "Gridded - AORC (hindcast)",
                "Gridded - HRRR (forecast)",
                "Gridded - DSS file (upload)",
            ]
            # Same guard as the BC dropdowns (v4.7.8): on a re-timed
            # window the model's own precip cannot represent the run.
            # Exception: a native *constant* rate is date-free and the
            # pipeline re-times it automatically, so "unchanged" stays
            # valid for it on any window.
            _precip_unchanged_off = _retimed and (
                str(_native_precip.get("mode") or "").lower()
                != "constant"
            )
            _p_bounced = False
            if _precip_unchanged_off:
                _precip_opts[0] = _UNCHANGED_OFF
                if st.session_state.get("precip_mode") == _UNCHANGED_OFF:
                    _p_bounced = True
                # None too: a fresh session would otherwise default to
                # index 0 - the disabled entry itself.
                if st.session_state.get("precip_mode") in (
                    None, _UNCHANGED, _UNCHANGED_OFF,
                ):
                    st.session_state["precip_mode"] = (
                        "Gridded - HRRR (forecast)"
                        if _window_class() == "future"
                        else "Gridded - AORC (hindcast)"
                    )
            elif st.session_state.get("precip_mode") == _UNCHANGED_OFF:
                st.session_state["precip_mode"] = _UNCHANGED
            precip_mode = st.selectbox(
                "Mode",
                _precip_opts,
                key="precip_mode",
                help=(
                    "Leave unchanged - keep the model's own precip "
                    "(a constant rate is re-timed to your window; "
                    "gridded/point precip needs the native window, so "
                    "the option is disabled on a re-timed run). "
                    "Constant - one uniform rate over the mesh. "
                    "Gridded AORC - real spatially-varying observed "
                    "rainfall (~800 m hourly) for a past event. "
                    "Gridded HRRR - 3 km forecast rainfall for an "
                    "upcoming event. "
                    "Gridded DSS - your own gridded rainfall from a "
                    "HEC-DSS file, like HEC-RAS's Source: DSS."
                ),
            )
            if _p_bounced:
                st.warning(
                    "**\"Leave unchanged\" can't be selected for this "
                    "window.** The model's own gridded/point rainfall "
                    "only exists for the native window "
                    f"(`{scan.get('original_start')}` → "
                    f"`{scan.get('original_end')}`). Pick a rain "
                    "source, or set the Tab 2 dates back to the "
                    "native window."
                )
            # Keep the rain mode consistent with the Tab 2 window.
            _wc_p = _window_class()
            if _wc_p == "past" and "HRRR" in precip_mode:
                st.warning(
                    "Your **Tab 2 window is historical**, but **HRRR** is "
                    "a forecast (only ~next 48 h). For a past storm use "
                    "**Gridded - AORC (hindcast)** or **DSS file (upload)**."
                )
            elif _wc_p == "future" and "AORC" in precip_mode:
                st.warning(
                    "Your **window reaches now/the future**, but **AORC** "
                    "is an observed hindcast (~10-day lag) and won't cover a "
                    "forecast period. Use **Gridded - HRRR (forecast)**."
                )
            if "DSS" in precip_mode:
                st.info(
                    "**Gridded - DSS file.** Upload a HEC-DSS file that "
                    "already holds gridded precipitation records (e.g. "
                    "gage-adjusted radar / MRMS, AORC, or grids from a "
                    "prior study) - exactly like HEC-RAS *Meteorological "
                    "Data → Precipitation → Gridded → Source: DSS*. The "
                    "grids are read, reprojected onto your model's grid, "
                    "and written into the plan as true gridded "
                    "precipitation.\n\n"
                    "- Records whose time falls inside your **Tab 2 "
                    "window** are used; make sure they overlap.\n"
                    "- The grid's own projection (e.g. SHG/Albers) is "
                    "honoured - no need to pre-reproject."
                )
                _dss_up = st.file_uploader(
                    "DSS file", type=["dss"], key="precip_dss_up",
                )
                # Stage to a WRITABLE app dir - the model folder is often
                # mounted read-only, and the DSS only needs to be read once
                # (at run time, to build the plan-HDF grid), so it never has
                # to live inside the model directory.
                _stage_dir = Path("/app/data/precip_uploads")
                try:
                    _stage_dir.mkdir(parents=True, exist_ok=True)
                except OSError:
                    import tempfile
                    _stage_dir = Path(tempfile.gettempdir())
                _safe_name = Path(_dss_up.name).name if _dss_up else ""
                _dss_staged = _stage_dir / (_safe_name or "precip_input.dss")
                if _dss_up is not None:
                    _dss_staged.write_bytes(_dss_up.getbuffer())

                _grid_opts = []
                if _dss_up is not None and _dss_staged.exists():
                    try:
                        from precip_gridded import list_dss_grid_paths
                        _grid_opts = list_dss_grid_paths(str(_dss_staged))
                    except Exception as _e:  # noqa: BLE001
                        st.error(
                            f"Couldn't read grid records from this DSS: "
                            f"{_e}"
                        )

                if _grid_opts:
                    _labels = [g["label"] for g in _grid_opts]
                    _sel = st.selectbox(
                        "Path", _labels, key="precip_dss_path",
                        help=(
                            "The gridded DSS pathname to apply (C-part is "
                            "the variable, usually PRECIP). The date/time "
                            "part is matched across your simulation "
                            "window automatically."
                        ),
                    )
                    _chosen = _grid_opts[_labels.index(_sel)]
                    _interp = st.selectbox(
                        "Interpolation Method", ["Nearest", "Bilinear"],
                        key="precip_dss_interp",
                        help=(
                            "How each model-grid cell samples the DSS "
                            "grid. Nearest - take the DSS cell that "
                            "covers it (preserves original values). "
                            "Bilinear - smooth blend of the 4 nearest "
                            "DSS cells (softer field)."
                        ),
                    )
                    precip_config = {
                        "enabled": True,
                        "mode": "gridded",
                        "source": "dss",
                        "dss_path": str(_dss_staged),
                        "grid_path": _chosen["pattern"],
                        "interp": _interp.lower(),
                    }
                elif _dss_up is not None:
                    st.warning(
                        "No gridded records found in this DSS file. "
                        "Pick a file exported as **gridded** "
                        "precipitation (grid records, not time series)."
                    )
                else:
                    st.caption(
                        "Upload a DSS file to choose its gridded path and "
                        "interpolation method."
                    )
            elif precip_mode.startswith("Gridded"):
                _is_aorc = "AORC" in precip_mode
                _gsrc = "aorc" if _is_aorc else "hrrr"
                if _is_aorc:
                    st.info(
                        "**AORC - observed gridded rainfall (hindcast).** "
                        "Fetches NOAA's ~800 m hourly Analysis of Record "
                        "(the precipitation behind the National Water "
                        "Model) for your **simulation window** and "
                        "applies it as real, spatially-varying rain on "
                        "the mesh.\n\n"
                        "- Set the **dates in Tab 2** to a *past* storm "
                        "(AORC lags real time by ~10 days, back to 1979).\n"
                        "- US (CONUS/AK/PR) coverage only."
                    )
                else:
                    st.info(
                        "**HRRR - forecast gridded rainfall.** Fetches "
                        "NOAA's 3 km High-Resolution Rapid Refresh "
                        "forecast and applies it as spatially-varying "
                        "rain on the mesh - for predicting an upcoming "
                        "flood.\n\n"
                        "- Set the **dates in Tab 2** to a window within "
                        "**~18 h of now** (the forecast horizon).\n"
                        "- US (CONUS) coverage only; fetched live at run "
                        "time from the latest model cycle."
                    )
                st.caption(
                    "Fetched at run time over your model's footprint, "
                    "resampled onto a model-CRS grid, and written into "
                    "the plan as true gridded precipitation. A fetch "
                    "failure falls back to no rain (the run still "
                    "completes)."
                )
                precip_config = {
                    "enabled": True,
                    "mode": "gridded",
                    "source": _gsrc,
                }
            elif precip_mode == "Constant":
                st.info(
                    "**Mesh-wide forcing - not tied to a BC line.**\n\n"
                    "The rate below is applied **uniformly to every cell "
                    "of all 2D flow areas** for the **entire simulation "
                    "window** (HEC-RAS *Meteorological Data → "
                    "Precipitation*, Constant mode).\n\n"
                    "- Works in both SI and English-unit models - "
                    "HEC-RAS converts the rate internally, so pick "
                    "whichever unit you think in.\n"
                    "- Unless your geometry includes an "
                    "**infiltration layer**, *all* rain becomes runoff "
                    "- expect conservative (high) flood extents."
                )
                _pc1, _pc2 = st.columns(2)
                with _pc1:
                    precip_value = st.number_input(
                        "Rain rate",
                        value=10.0,
                        min_value=0.0,
                        step=1.0,
                        format="%.2f",
                        key="precip_value",
                    )
                with _pc2:
                    precip_units = st.selectbox(
                        "Units", ["mm/hr", "in/hr"], key="precip_units",
                    )
                precip_config = {
                    "enabled": True,
                    "mode": "constant",
                    "constant_value": float(precip_value),
                    "constant_units": str(precip_units),
                }
            else:
                # Leave unchanged - surface what the model itself
                # defines so the user knows what "unchanged" means.
                _mp = (scan or {}).get("precipitation") or {}
                _mp_mode = str(_mp.get("mode") or "").lower()
                if (
                    _mp.get("enabled")
                    and _mp_mode == "constant"
                    and _mp.get("constant_value") is not None
                ):
                    st.success(
                        f"The model defines **constant rain at "
                        f"{_mp['constant_value']:g} "
                        f"{_mp.get('constant_units') or 'mm/hr'}** - it "
                        f"will be applied over your simulation window."
                    )
                elif _mp.get("enabled"):
                    st.warning(
                        f"The model uses "
                        f"**{_mp.get('mode') or 'unknown'}-mode "
                        f"precipitation**, which HECinBOX passes through "
                        f"untouched. Its data covers the window the "
                        f"model was built for - make sure your "
                        f"simulation window matches, or the engine may "
                        f"see no rain."
                    )
                else:
                    st.warning(
                        "This model has **no precipitation "
                        "configured** - *Leave unchanged* means **no "
                        "rain will fall**. Switch Mode to **Constant** "
                        "to force rain onto the mesh."
                    )
                precip_config = {
                    "enabled": True,
                    "mode": "unchanged",
                }
        st.divider()

    _tab_nav(2)


# ── TAB 4: Run ────────────────────────────────────────────────────────
with tab_run:
    _adir = _active_job_dir()
    st.subheader(
        "Run Simulation - in progress"
        if _adir else "Run Simulation"
    )

    # Demo run queue: if this session parked a run because all slots were
    # busy, show its place and launch it automatically when one frees.
    if DEMO_MODE and st.session_state.get("_demo_pending"):
        @st.fragment(run_every="3s")
        def _demo_queue_watch():
            pend = st.session_state.get("_demo_pending")
            if not pend:
                return
            _sid = _session_id()
            _dec, _pos, _running = _demo_slot_request(
                _sid, pend["output_target"]
            )
            if _dec == "run":
                st.session_state.pop("_demo_pending", None)
                _launch_run(
                    Path(pend["output_target"]),
                    Path(pend["settings_path"]),
                )
                st.rerun(scope="app")
            else:
                st.warning(
                    f"**You are in the demo run queue - position "
                    f"{_pos}.**  \n"
                    f"To keep the shared demo responsive on limited "
                    f"resources, only **{_DEMO_MAX_CONCURRENT} simulations "
                    f"run at the same time** ({_running} running now). "
                    f"Your run starts automatically when a slot frees up "
                    f"- please keep this tab open."
                )
                if st.button("Cancel and leave the queue",
                             key="demo_q_cancel"):
                    _demo_slot_release(_sid)
                    st.session_state.pop("_demo_pending", None)
                    st.rerun(scope="app")
        _demo_queue_watch()

    def _detect_cores() -> int:
        try:
            return max(1, len(os.sched_getaffinity(0)))
        except (AttributeError, OSError):
            return max(1, os.cpu_count() or 4)
    _cpu_total = _detect_cores()
    _thread_choices = [1, 2, 4, 8, 16, 32]
    _thread_choices = [t for t in _thread_choices if t <= _cpu_total]
    if _cpu_total not in _thread_choices:
        _thread_choices.append(_cpu_total)
    _thread_choices = sorted(set(_thread_choices))
    _default_threads = min(8, _cpu_total)

    if not _model_ready:
        st.info("Select a model in the **Model Folder** tab first.")
        flow_area_name = ""
        run_geom = False
        plan_suffix = "p01"
        geom_suffix = "g01"
        unsteady_suffix = "u01"
        num_threads_setting = _default_threads
    else:
        with st.expander("Advanced / Auto-detected", expanded=False):
            a1, a2 = st.columns(2)
            with a1:
                flow_area_name = st.text_input(
                    "2D Flow Area name",
                    value=scan.get("flow_area_name", ""),
                    key="adv_fa",
                )
                _geom_default = bool(scan.get("needs_plan_hdf"))
                run_geom = st.checkbox(
                    "Run geometry preprocessor",
                    value=_geom_default,
                    key="adv_geom",
                )
            with a2:
                plan_suffix = st.text_input(
                    "Plan suffix",
                    value=scan.get("plan_suffix", "p01"),
                    key="adv_p",
                )
                geom_suffix = st.text_input(
                    "Geom suffix",
                    value=scan.get("geom_suffix", "g01"),
                    key="adv_g",
                )
                unsteady_suffix = st.text_input(
                    "Unsteady suffix",
                    value=scan.get("unsteady_suffix", "u01"),
                    key="adv_u",
                )
        st.divider()
        # Tight Cores + Threads pair on the left; spacer; Agent toggle
        # pinned to the right so the three groups don't drift apart.
        _tc1, _tc2, _tc_pad, _tc3 = st.columns([0.9, 1.3, 2.3, 1.5])
        with _tc1:
            st.markdown(
                f"**Available cores:** `{_cpu_total}`"
            )
        with _tc2:
            # If a persisted thread count is no longer valid (e.g.
            # the container was restarted with fewer --cpus), drop
            # the stale value so the widget falls back to the
            # default rather than erroring out.
            if (
                "adv_threads" in st.session_state
                and st.session_state["adv_threads"] not in _thread_choices
            ):
                del st.session_state["adv_threads"]
            num_threads_setting = st.selectbox(
                "Threads",
                options=_thread_choices,
                index=_thread_choices.index(_default_threads)
                if _default_threads in _thread_choices else 0,
                key="adv_threads",
                help="Sets OMP_NUM_THREADS / MKL_NUM_THREADS for the "
                     "HEC-RAS Linux engine. More threads = faster "
                     "single-run, but each thread roughly doubles "
                     "working memory.",
            )
        with _tc3:
            # The alert agent (Tab 7) only makes sense in real-time /
            # scheduled mode - a one-off run doesn't have anything to
            # alert about beyond itself.  Show the toggle only when
            # 'Enable auto-run scheduling' is on in Tab 2.
            _sched_on_t2 = bool(st.session_state.get("sched", False))
            if _sched_on_t2:
                st.toggle(
                    "Enable alert agent",
                    key="enable_alert_agent",
                    help=(
                        "When on, every scheduled run is evaluated "
                        "against the rules defined in **Tab 7 · Agent** "
                        "and an email is sent for any condition that "
                        "transitions below → above its threshold. "
                        "Off → the agent stays silent even if rules "
                        "are saved."
                    ),
                )
                st.toggle(
                    "Enable Live dashboard",
                    key="enable_live_dashboard",
                    value=True,
                    help=(
                        "When on, **Tab 8 · Live** auto-updates the "
                        "cumulative plot and depth map each time a new "
                        "cycle finishes (detected via the auto-scheduler "
                        "history file's mtime - no fixed polling "
                        "interval). Off → Tab 8 stays frozen on the "
                        "last view until you refresh manually."
                    ),
                )
            else:
                st.caption(
                    "*Enable alert agent* and *Live dashboard* "
                    "appear here when **Auto-run scheduling** is on "
                    "in Tab 2."
                )

    def _build_run_settings(
        start_str: str, end_str: str, output_target: Path,
        cloud_dest: str | None = None,
    ) -> dict:
        settings = {
            "simulation": {
                "start": start_str,
                "end": end_str,
            },
            "time": {
                # The model's clock, as an offset from UTC; the pipeline
                # shifts all fetched (UTC) data by this so it aligns with
                # the sim window.  Chosen explicitly in Tab 2 (v4.8.0) -
                # the run is blocked until it is, so the ``or 0`` here is
                # only a belt-and-braces fallback.
                "lst_offset_hours": int(
                    st.session_state.get("model_lst_offset") or 0
                ),
                # Display-only companion (v4.8.0): Local Standard Time
                # at the model site, from the scanner's longitude.  It
                # never shifts data - it travels into the run's
                # artifacts so results and alerts can later be rendered
                # in local standard time with UTC alongside, whichever
                # clock the model itself was built on.
                "site_lst_offset_hours": int(
                    (scan or {}).get("lst_offset_hours", 0) or 0
                ),
            },
            "boundary_conditions": bc_config,
            "precipitation": precip_config,
            "validation": {
                "source": "none",
            },
            "hecras": {
                "model_dir": str(MODEL_DIR),
                "output_dir": str(output_target),
                "project_name": scan["project_name"],
                "plan_suffix": plan_suffix,
                "geom_suffix": geom_suffix,
                "unsteady_suffix": unsteady_suffix,
                "flow_area_name": flow_area_name,
                "unit_system": scan.get("unit_system"),
                "run_geom_preprocess": run_geom,
                "unsteady_timeout_seconds": None,
                "num_threads": int(num_threads_setting),
            },
        }
        # Cloud mode - the pipeline uploads the finished run folder
        # to this S3 prefix once the simulation completes.
        if cloud_dest:
            settings["cloud"] = {"upload_results_to": cloud_dest}
        # Alert agent - enabled only when the Tab 4 toggle is on AND
        # we're in scheduled mode (the toggle is hidden otherwise).
        _agent_on = bool(
            st.session_state.get("enable_alert_agent", False)
            and st.session_state.get("sched", False)
        )
        settings["agent"] = {"enabled": _agent_on}
        return settings

    # ── Auto-scheduler - live countdown from the daemon's file ──
    # The watcher fragment runs unconditionally (every 1 s) so it can
    # detect external state changes - even when the schedule is idle
    # and nothing visible is being rendered - and force a full app
    # rerun the moment the daemon completes a run.  That's what keeps
    # Tab 5 fresh without a manual browser refresh.
    _auto_schedule_watch()
    _sched_state = _read_schedule_state()
    _sched_active = bool(_sched_state and _sched_state.get("enabled"))
    if _sched_active:
        if st.button(
            "Stop schedule", key="stop_schedule_btn",
            width="stretch",
            help="Tell the auto-scheduler daemon to stop after "
                 "the current run completes. The current run "
                 "(if any) keeps going - use **Stop run** to "
                 "kill it immediately.",
        ):
            _autoschedule_disable()
            st.success("Auto-schedule disabled.")
            st.rerun()

    # ── Persistent banner for the last completed run ──
    _lrs = st.session_state.get("last_run_summary")
    if _lrs:
        _n_runs = len(st.session_state.get("run_history", []))
        st.success(
            f"Last run complete ({_lrs['start']} → {_lrs['end']}). "
            f"Results are in **Tab 5 · Results**"
            + (f" - {_n_runs} runs total." if _n_runs > 1 else ".")
        )

    # ── Schedule history (when daemon is active or recently was) ──
    if _sched_state:
        _ndone = int(_sched_state.get("runs_completed", 0))
        _nfail = int(_sched_state.get("runs_failed", 0))
        _lstate = _sched_state.get("last_state", "")
        if _ndone or _nfail:
            if _nfail:
                st.warning(
                    f"Schedule so far - **{_ndone}** completed, "
                    f"**{_nfail}** failed. Failures do not stop "
                    f"the loop; the daemon re-attempts at every "
                    f"interval until you click **Stop schedule** "
                    f"or **Reset**. Last run: `{_lstate}`."
                )
            else:
                st.info(
                    f"Schedule so far - **{_ndone}** completed. "
                    f"Last run: `{_lstate}`."
                )

    # ── BC-source sanity check ──────────────────────────────────────
    # A boundary is "active" only when the user picked a real data
    # source for it (USGS / NOAA / Constant / Forecast).  If every
    # boundary is left as "Leave unchanged", HEC-RAS just reuses the
    # data already inside the model, so the run gives the same result
    # for any date range.  That's a valid native replay - we show a
    # calm note about it below, not an error.
    def _bc_is_active(b: dict) -> bool:
        _src = str(b.get("source", "none")).lower()
        return bool(
            (_src in ("usgs", "noaa") and str(b.get("station", "")).strip())
            or _src == "constant"
            or (_src == "forecast" and str(b.get("forecast_product", "")).strip())
        )

    _bc_active = [b for b in bc_config if _bc_is_active(b)]
    # When the daemon is armed, the *authoritative* BC config is the
    # schedule's settings_template - the session-state widgets behind
    # ``bc_config`` can be momentarily empty (e.g. the toggle-purge /
    # not-yet-hydrated case), which used to make this banner fire even
    # though every scheduled cycle was injecting real-time data.  Trust
    # the template when it has an active BC (v3.1.1).
    _sched_bc_active = False
    _sched_precip_active = False
    if _sched_active:
        _tmpl = (
            (_read_schedule_state() or {}).get("settings_template", {}) or {}
        )
        _tmpl_bcs = _tmpl.get("boundary_conditions", []) or []
        _sched_bc_active = any(_bc_is_active(b) for b in _tmpl_bcs)
        _sched_precip = _tmpl.get("precipitation") or {}
        _sched_precip_active = bool(
            _sched_precip.get("enabled")
            and str(_sched_precip.get("mode", "constant")).lower()
            in ("constant", "gridded")
        )
    # Constant or Gridded rain counts as a real forcing source: with it
    # the run is NOT a byte-identical replay of the calibration event,
    # even if every BC line rides the model-bundled DSS data.  "Leave
    # unchanged" rain deliberately does NOT count - it replays whatever
    # the model already had.
    _precip_active = bool(
        (
            precip_config.get("enabled")
            and str(precip_config.get("mode", "")).lower()
            in ("constant", "gridded")
        )
        or _sched_precip_active
    )
    if bc_config and not _bc_active and not _sched_bc_active \
            and not _precip_active:
        # All boundaries are "Leave unchanged" and there's no constant
        # rain - a valid native replay, so this is a calm heads-up (blue
        # info), not a red error.  Plain wording so it's easy to follow.
        st.info(
            "**All boundaries are set to “Leave unchanged”.**  \n"
            "The model will run with its **own built-in data** - the same "
            "data that came inside the ready model. This is fine if you "
            "just want to run the model as it is.  \n\n"
            "Good to know: that built-in data is tied to **fixed dates**, "
            "so you will get the **same results no matter which dates you "
            "pick** in Tab 2.  \n\n"
            "Want live, real-time data instead? Go to **Tab 3** and set "
            "the **Source** to **USGS, NOAA, or Constant** for at least "
            "one boundary."
        )
    elif bc_config and not _bc_active and not _sched_bc_active \
            and _precip_active:
        st.info(
            "**Only rain on mesh is added as new input.**  \n"
            "The upstream and downstream boundaries will use the model's "
            "**own built-in data**, and the rain is applied on top. This "
            "is fine for rain studies. If you also want live inflow data, "
            "set a **Source** in **Tab 3**."
        )

    # ── Pre-flight: what the run will actually cover ──────────────────
    # The window and every source's reach, side by side, before the run
    # rather than in the log afterwards. Rain is the usual shortfall:
    # HRRR reaches 18 h from an ordinary cycle and 48 h from 00/06/12/18z,
    # so it routinely covers only part of a multi-day forecast window.
    if _model_ready and (bc_config or precip_config.get("enabled")):
        if realtime and sim_start_dt is not None:
            _pf_start, _pf_end = sim_start_dt, sim_end_dt
        elif sim_start is not None and sim_end is not None:
            _pf_start = datetime.combine(sim_start, dtime(0, 0))
            _pf_end = datetime.combine(sim_end, dtime(23, 0))
        else:
            _pf_start = _pf_end = None

        if _pf_start is not None:
            _pf_rows, _pf_short = _preflight_rows(
                bc_config, precip_config, _pf_start, _pf_end,
                st.session_state.get("model_lst_offset"),
            )
            _pf_hours = (_pf_end - _pf_start).total_seconds() / 3600.0
            _tzl = (
                f"UTC{int(st.session_state.get('model_lst_offset') or 0):+d}"
                if st.session_state.get("model_lst_offset") else "UTC"
            )
            st.markdown("##### Before you run")
            st.markdown(
                f"**Simulation window:** {_pf_start:%d %b %Y %H:%M} → "
                f"{_pf_end:%d %b %Y %H:%M} {_tzl}  ·  "
                f"**{_pf_hours:.0f} hours** ({_pf_hours / 24:.1f} days)"
            )
            if _pf_rows:
                import pandas as _ppd
                st.dataframe(
                    _ppd.DataFrame(_pf_rows), width="stretch",
                    hide_index=True,
                )
            for _what, _srcname, _missing, _detail in _pf_short:
                st.warning(
                    f"**{_what} ({_srcname}) covers only the first "
                    f"{_pf_hours - _missing:.0f} of {_pf_hours:.0f} "
                    f"hours.**"
                    + (f" That is because {_detail}." if _detail else "")
                    + (
                        " The rest of the run has no rain on the mesh."
                        if _what == "Rain on mesh"
                        else " After that the engine holds its last "
                             "value, which is not a real forecast."
                    )
                    + " Shorten the window in **Tab 2** if that matters "
                      "for what you are simulating."
                )

    # ── Temporal consistency guard ────────────────────────────────────
    # Aggregate any source/window mismatches (forecast for a past window,
    # observed/AORC for a future one).  Single-shot runs are soft-blocked
    # behind an explicit override; scheduled/real-time runs are intentional
    # and never blocked.
    _win_warnings = _window_source_warnings(
        bc_config, precip_config, _window_class()
    )
    _win_override = False
    if _win_warnings and not (schedule and interval_minutes):
        st.warning(
            "**Your data sources don't match the Tab 2 time window:**\n\n"
            + "\n".join(f"- {m}" for m in _win_warnings)
        )
        _win_override = st.checkbox(
            "I understand - run anyway", key="win_mismatch_override"
        )

    # ── Time-base guard (v4.8.0) ──────────────────────────────────────
    # Hard block, not a soft override: an unset model clock silently
    # shifts every fetched series by whole hours and the run still
    # completes looking plausible, so there is nothing to "understand
    # and run anyway".
    _tz_missing = _model_ready and (
        st.session_state.get("model_lst_offset") is None
    )
    if _tz_missing:
        st.error(
            "**Choose the model time base first (Tab 2 · Simulation "
            "Window).** HEC-RAS stores your simulation dates without a "
            "time zone, while USGS, NOAA and the forecast services all "
            "publish in UTC, so HECinBOX cannot line them up until you "
            "say which clock the model was built on."
        )

    run_clicked = st.button(
        "Run Simulation" if not _sched_active
        else "Update schedule with current settings",
        type="primary",
        width="stretch",
        disabled=(not _model_ready) or _tz_missing,
        help=(
            "Set the model time base in Tab 2 to enable this."
            if _tz_missing else
            "Re-arms the auto-scheduler daemon with the current "
            "Tab 2 / Tab 3 / Tab 4 settings - the next run uses "
            "the new template."
            if _sched_active else None
        ),
    )

    if _model_ready and run_clicked:
        # ── Cloud-mode validation ─────────────────────────────────
        _is_cloud = (
            st.session_state.get("model_source")
            == "Cloud storage (S3)"
        )
        _cloud_uri = ""
        if _is_cloud:
            _cloud_uri = st.session_state.get(
                "cloud_output_uri", ""
            ).strip()
            if not _cloud_uri:
                st.error(
                    "Cloud mode - enter a **results S3 URI** under "
                    "*Output Location* in **Tab 1 · Model Folder** "
                    "before running."
                )
                st.stop()

        # ── Workflow guards ───────────────────────────────────
        # Catch the two ways a user can accidentally launch a
        # second, overlapping job:
        # (a) the daemon is armed but the user toggled off
        #     'Auto-run scheduling' in Tab 2 - clicking the
        #     button would silently start a single-shot run
        #     while the daemon keeps firing; and
        # (b) a single-shot run is already in flight and the
        #     user clicks Run again.
        if _sched_active and not (schedule and interval_minutes):
            st.error(
                "Auto-schedule is currently armed. Either turn"
                " **Auto-run scheduling** back on in **Tab 2** "
                "(then click this button to update the template),"
                " or click **Stop schedule** above to"
                " stop the loop first."
            )
            st.stop()
        if _adir and not (schedule and interval_minutes):
            st.error(
                "A run is already in progress. Wait for it to"
                " finish, or use **Stop run** below to kill"
                " it before starting a new single-shot."
            )
            st.stop()
        # (c) data sources don't match the Tab 2 window and the user
        #     hasn't explicitly overridden - block the single-shot run.
        if _win_warnings and not (schedule and interval_minutes) \
                and not _win_override:
            st.error(
                "Your data sources don't match the simulation window. "
                "Fix the sources in **Tab 3** (or the window in **Tab 2**), "
                "or tick **I understand - run anyway** above to proceed."
            )
            st.stop()

        if schedule and interval_minutes:
            # ── AUTO-SCHEDULE: hand off to the persistent daemon ──
            # Build a settings *template*. The daemon overwrites
            # ``simulation.start``, ``simulation.end`` and
            # ``hecras.output_dir`` (plus the per-run cloud sub-URI)
            # on every run, so the realtime window stays current.
            template = _build_run_settings(
                start_str="__TEMPLATE__",
                end_str="__TEMPLATE__",
                output_target=Path("__TEMPLATE__"),
                cloud_dest=None,
            )
            # Window span + direction so the daemon rebuilds the right
            # window each run: forecast -> now .. now+span, hindcast ->
            # now-span .. now.  Derived from the Tab 2 window so it is
            # correct for the auto-sized forecast branch too.
            _sched_window_hours = max(
                1,
                int(round((sim_end_dt - sim_start_dt).total_seconds() / 3600.0)),
            )
            _autoschedule_activate(
                interval_minutes=int(interval_minutes),
                realtime_days=int(realtime_days),
                forecast=bool(_is_forecast),
                window_hours=int(_sched_window_hours),
                settings_template=template,
                output_parent=OUTPUT_PARENT,
                base_name=_base_name,
                cloud_output_uri=_cloud_uri if _is_cloud else None,
            )
            st.success(
                f"Auto-schedule armed - re-running every "
                f"**{_fmt_interval(int(interval_minutes))}** "
                f"forever. The daemon keeps the loop alive even "
                f"if you close this tab; use **Stop schedule** "
                f"or **Reset** to stop."
            )
            st.rerun()
        else:
            # ── SINGLE-SHOT: existing launch path ─────────────────
            if realtime:
                start_str = sim_start_dt.strftime("%Y-%m-%d %H:%M")
                end_str = sim_end_dt.strftime("%Y-%m-%d %H:%M")
            else:
                start_str = f"{sim_start} 00:00"
                end_str = f"{sim_end} 23:00"

            run_tag = (
                f"{sim_start.strftime('%Y%m%d')}_"
                f"{sim_end.strftime('%Y%m%d')}"
            )
            # In the multi-user demo, tag the run folder with the session
            # id so two people running the same model + window at once
            # write to separate folders.
            if DEMO_MODE:
                run_tag = f"{run_tag}_{_session_id()}"
            output_target = OUTPUT_PARENT / f"{_base_name}_{run_tag}"

            _cloud_dest = None
            if _is_cloud:
                _cloud_dest = (
                    _cloud_uri.rstrip("/") + "/"
                    + output_target.name + "/"
                )

            try:
                output_target.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                st.error(
                    f"Cannot create output folder `{output_target}`: {e}"
                )
                st.stop()

            st.session_state["last_output_dir"] = str(output_target)
            st.session_state["last_start_str"] = start_str
            st.session_state["last_end_str"] = end_str

            settings = _build_run_settings(
                start_str, end_str, output_target, _cloud_dest
            )
            _run_settings_path = _settings_path()
            _run_settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_run_settings_path, "w") as f:
                yaml.dump(
                    settings, f, default_flow_style=False, sort_keys=False
                )

            # Demo: cap concurrent runs. If all slots are busy, park this
            # run and let the queue watcher launch it when one frees up.
            if DEMO_MODE:
                _decision, _pos, _running = _demo_slot_request(
                    _session_id(), output_target
                )
                if _decision == "queued":
                    st.session_state["_demo_pending"] = {
                        "output_target": str(output_target),
                        "settings_path": str(_run_settings_path),
                    }
                    st.rerun()

            if _launch_run(output_target, _run_settings_path):
                st.rerun()
            else:
                st.stop()


    # ── Active-run progress (single-shot or daemon-launched) ──
    # Always rendered AFTER the schedule controls, threads, and
    # buttons above - so the user never loses sight of the timer
    # / Stop-schedule / Update buttons while a run is in flight.
    if _adir:
        st.divider()
        st.markdown("##### Current run progress")
        _render_active_job(_adir)

    _tab_nav(3)


# ── TAB 5: Results ───────────────────────────────────────────────────
def _render_results_tab_body() -> None:
    st.subheader("Simulation Results")

    last_output = st.session_state.get("last_output_dir")
    _view_results = st.session_state.get("view_results", False)
    # Daemon/live mode: while a schedule is armed, ALWAYS track the latest
    # finished run so each fragment tick advances to the newest iteration
    # (session_state.last_output_dir is only refreshed on a full app rerun,
    # which doesn't happen on fragment ticks - so we resolve from disk
    # here).  When the schedule is idle we honour a manually-opened run,
    # falling back to the latest on disk if nothing is selected (v3.1.3).
    _sched_armed_live = bool((_read_schedule_state() or {}).get("enabled"))
    if _sched_armed_live or not _view_results:
        _auto_latest = _latest_successful_run()
        if _auto_latest is not None:
            last_output = str(_auto_latest)
            _view_results = True

    if not _view_results or not last_output or not Path(last_output).exists():
        st.info(
            "**No results loaded.**\n\n"
            "- **Run a new simulation** in **Tab 4 · Run**, or\n"
            "- **Open a previous run** from the *Open Previous Results* "
            "section at the bottom of **Tab 1 · Model Folder**.\n\n"
            "Results stay hidden here until you load a run - this keeps "
            "the rest of the app fast while you browse."
        )
    else:
        import numpy as _np

        output_path = Path(last_output)

        # ── Show originating model identity (v2.7.2+) ──
        # run_meta.json is written by main.py for HECinBOX runs and by
        # results_extractor for opened external HEC-RAS folders.
        _rm_path = output_path / "run_meta.json"
        if _rm_path.exists():
            try:
                import json as _rj
                _rmeta = _rj.loads(_rm_path.read_text())
                _src = (_rmeta.get("source") or "").lower()
                _badge = (
                    "HECinBOX" if _src == "hecinbox"
                    else "External HEC-RAS"
                )
                _proj = _rmeta.get("project_name") or "-"
                _flow_area = _rmeta.get("flow_area_name") or "-"
                _plan_hdf = _rmeta.get("plan_hdf_name") or "-"
                _md = _rmeta.get("model_dir") or "-"
                st.caption(
                    f"{_badge}  ·  **{_proj}**  ·  "
                    f"plan: `{_plan_hdf}`  ·  area: `{_flow_area}`  ·  "
                    f"model: `{_md}`"
                )
            except Exception:
                pass

        _rc1, _rc2 = st.columns([4, 1])
        with _rc1:
            st.markdown(f"**Output folder:** `{output_path}`")
        with _rc2:
            if st.button("Refresh", key="refresh_results",
                         width="stretch"):
                st.rerun()

        # ── Simulation summary from wse_extract.npz ──
        npz_path = output_path / "wse_extract.npz"
        _wse_data = None
        if npz_path.exists():
          try:
            _wse_data = _np.load(npz_path, allow_pickle=True)
            from units import is_si as _is_si, unit_labels as _unit_labels
            _ru = _unit_labels(_is_si(
                str(_wse_data["unit_system"][0])
                if "unit_system" in _wse_data.files else None
            ))
            _wse = _np.asarray(_wse_data["wse"], dtype=_np.float32)
            _coords = _wse_data["coords"]
            _vel = (
                _np.asarray(_wse_data["vel"], dtype=_np.float32)
                if "vel" in _wse_data.files else None
            )
            import pandas as _rpd
            import plotly.graph_objects as _go

            _model_time = _rpd.to_datetime(_wse_data["model_time"])
            # The run records what clock those timestamps are on
            # (v4.8.0), so every time shown below is rendered in the
            # site's local standard time with UTC alongside.  Runs
            # computed before v4.8.0 carry no clock and are rendered
            # raw, marked as such, rather than silently reinterpreted.
            from timebase import TimeBase as _TimeBase
            _tb = _TimeBase.from_npz(_wse_data)
            if not _tb.known:
                _tb = _TimeBase.from_dir(output_path)
            _lst_time = _tb.to_lst(_model_time)

            # ── Variable selector ──
            _has_depth = "min_elev" in _wse_data.files
            _var_opts = ["Water Surface Elevation"]
            if _vel is not None:
                _var_opts.append("Velocity")
            if _has_depth:
                _var_opts.append("Water Depth")
                _var_opts.append("Terrain (DEM)")
            _var = st.selectbox(
                "Variable", _var_opts, key="res_var"
            )
            # RAS-Mapper-style colour ramps per variable.
            _RAMP_WSE = [
                [0.0, "#1a9850"], [0.5, "#ffffbf"], [1.0, "#d73027"],
            ]
            # Natural water ramp - keep in sync with
            # raster_render.WATER_PLOTLY so depth looks the same in
            # every map style and the GIF.
            _RAMP_DEPTH = [
                [0.0, "#d8f6ff"], [0.33, "#7fd8d8"],
                [0.66, "#2b8cbe"], [1.0, "#084081"],
            ]
            if _var == "Velocity":
                _arr, _unit, _short, _color, _scale = (
                    _vel, _ru["velocity"], "Velocity", "#d2691e", "Turbo"
                )
            elif _var == "Water Depth":
                _arr = _np.clip(
                    _wse - _wse_data["min_elev"], 0.0, None
                )
                _unit, _short, _color, _scale = (
                    _ru["length"], "Depth", "#2c7fb8", _RAMP_DEPTH
                )
            elif _var == "Terrain (DEM)":
                _arr = _np.asarray(
                    _wse_data["min_elev"], dtype="float32"
                )[None, :]
                _unit, _short, _color, _scale = (
                    _ru["length"], "Terrain", "#8c6d3f", "earth"
                )
            else:
                _arr, _unit, _short, _color, _scale = (
                    _wse, _ru["length"], "WSE", "#1f77b4", _RAMP_WSE
                )

            _peak_per_cell = _np.nanmax(_arr, axis=0)
            # Wet-cell detection: prefer depth-based (WSE − terrain > 0)
            # over variance-based, which fails for near-steady rivers.
            if _has_depth:
                _depth_arr = _wse - _wse_data["min_elev"]
                _max_depth = _np.nanmax(_depth_arr, axis=0)
                _wet_mask = _max_depth > 0.01  # >0.01 ft ever wet
            else:
                _wet_mask = _np.nanstd(_wse, axis=0) > 0.01
            _n_wet = int(_wet_mask.sum())
            _peak_wet = _np.where(_wet_mask, _peak_per_cell, _np.nan)
            if _np.isfinite(_peak_wet).any():
                _max_cell = int(_np.nanargmax(_peak_wet))
                _min_cell = int(_np.nanargmin(_peak_wet))
                _max_val = float(_peak_wet[_max_cell])
                _min_val = float(_peak_wet[_min_cell])
                _mean_val = float(_np.nanmean(_peak_wet))
            else:
                _max_cell = int(_np.nanargmax(_peak_per_cell))
                _min_cell = int(_np.nanargmin(_peak_per_cell))
                _max_val = float(_peak_per_cell[_max_cell])
                _min_val = float(_peak_per_cell[_min_cell])
                _mean_val = float(_np.nanmean(_peak_per_cell))
            _peak_cell = _max_cell

            # ── Styled summary box ──
            st.markdown(
                """
    <style>
    div[data-testid="stMetricValue"] {
    font-size: 1.25rem !important;
    line-height: 1.25 !important;
    }
    div[data-testid="stMetricLabel"] {
    font-size: 0.78rem !important;
    color: #5b6770 !important;
    }
    /* Bold box framing the inundation map (keyed container).  The
       st-key class may land on the border wrapper itself or on an
       inner block depending on the Streamlit version - cover both. */
    .st-key-res_map_box[data-testid="stVerticalBlockBorderWrapper"],
    .st-key-res_map_box div[data-testid="stVerticalBlockBorderWrapper"],
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.st-key-res_map_box) {
    border: 2px solid #0d1b2a !important;
    border-radius: 8px !important;
    overflow: hidden;
    }
    </style>
                """,
                unsafe_allow_html=True,
            )
            with st.container(border=True):
                st.markdown("##### Simulation Summary")
                # ── Run identity & wall-clock duration ──
                _meta = _run_display_metadata(output_path)
                r1, r2, r3 = st.columns([1, 1, 2])
                if _meta.get("run_index"):
                    r1.metric(
                        "Run in schedule",
                        f"#{_meta['run_index']} / {_meta['run_total']}",
                        help=(
                            "This run's position in the auto-scheduler's "
                            "history of successful runs. Tab 5 always "
                            "shows the most recently completed run "
                            "while the schedule is armed."
                        ),
                    )
                else:
                    r1.metric(
                        "Run in schedule",
                        "single-shot",
                        help="This run was not launched by the daemon.",
                    )
                if "duration_seconds" in _meta:
                    r2.metric(
                        "Compute duration",
                        _fmt_duration(_meta["duration_seconds"]),
                        help=(
                            "Wall-clock time between job start and "
                            "wse_extract.npz being written - covers the "
                            "whole pipeline (USGS/NOAA fetch, model "
                            "patch, HEC-RAS engine, results extraction "
                            "and any cloud upload)."
                        ),
                    )
                else:
                    r2.metric("Compute duration", "-")
                r3.metric(
                    "Run folder",
                    output_path.name,
                    help=str(output_path),
                )

                s1, s2, s3, s4 = st.columns(4)
                s1.metric("Timesteps", f"{_arr.shape[0]:,}")
                s2.metric("Mesh cells", f"{_arr.shape[1]:,}")
                s3.metric(
                    "Start", _lst_time[0].strftime("%Y-%m-%d %H:%M"),
                    help=_tb.stamp(_model_time[0]),
                )
                s4.metric(
                    "End", _lst_time[-1].strftime("%Y-%m-%d %H:%M"),
                    help=_tb.stamp(_model_time[-1]),
                )
                st.caption(
                    f"🕒 Times shown in {_tb.lst_label}."
                    + ("" if _tb.known else
                       "  This run predates v4.8.0, so its clock was "
                       "never recorded - times are shown exactly as the "
                       "model stored them.")
                )
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Wet cells", f"{_n_wet:,}")
                m2.metric(
                    f"Min {_short} ({_unit})",
                    f"{_min_val:.2f}  (cell {_min_cell})",
                )
                m3.metric(
                    f"Max {_short} ({_unit})",
                    f"{_max_val:.2f}  (cell {_max_cell})",
                )
                m4.metric(
                    f"Mean {_short} ({_unit})",
                    f"{_mean_val:.2f}",
                )

            st.divider()

            # ── Interactive inundation map ──
            st.markdown(f"##### {_short} Inundation Map")
            _wkt = (
                str(_wse_data["proj_wkt"][0])
                if "proj_wkt" in _wse_data.files else ""
            )
            _lon = _lat = None
            if _wkt:
                try:
                    from pyproj import Transformer
                    _tf = Transformer.from_crs(
                        _wkt, "EPSG:4326", always_xy=True
                    )
                    _lon, _lat = _tf.transform(
                        _coords[:, 0], _coords[:, 1]
                    )
                    _lon = _np.asarray(_lon)
                    _lat = _np.asarray(_lat)
                except Exception as _e:
                    st.caption(
                        f"Map unavailable - CRS reproject failed: {_e}"
                    )
            else:
                st.caption(
                    "Map unavailable - model projection (WKT) not "
                    "stored in this result."
                )

            if _lon is not None:
                _has_poly = (
                    "cell_fp" in _wse_data.files
                    and "fp_xy" in _wse_data.files
                )
                # Smooth (RAS-Mapper-style) peak rasters, if the run
                # produced them (needs the model's terrain DEM at run
                # time).  One raster per variable; offered only for the
                # variable currently selected above.
                _smooth_json = npz_path.parent / "smooth_maps.json"
                _smooth_meta = None
                _smooth_var = None
                if _smooth_json.exists():
                    try:
                        import json as _jsm
                        _smooth_meta = _jsm.loads(_smooth_json.read_text())
                        _smooth_var = (
                            _smooth_meta.get("variables", {}).get(_var)
                        )
                    except Exception:
                        _smooth_meta = None
                _has_smooth = (
                    _smooth_var is not None
                    and (npz_path.parent / _smooth_var["file"]).exists()
                )

                _c1, _c2, _c3, _c4 = st.columns([2, 2, 1.2, 1.4])
                with _c1:
                    _mode_opts = (
                        ["Filled cells", "Points (fast)"]
                        if _has_poly else ["Points (fast)"]
                    )
                    if _has_smooth:
                        _mode_opts = ["Smooth (peak)"] + _mode_opts
                    _map_mode = st.radio(
                        "Map style", _mode_opts,
                        horizontal=True, key="res_map_mode",
                        help=(
                            "Smooth (peak) samples the result onto the "
                            "terrain DEM for a RAS-Mapper-style map of the "
                            "selected variable's peak. Filled cells / "
                            "Points colour each mesh cell and support "
                            "per-timestep playback."
                        ),
                    )
                _is_smooth = _map_mode == "Smooth (peak)"
                with _c2:
                    # ("builtin", style) or ("raster", tile-url)
                    _base_opts = {
                        "Light": ("builtin", "carto-positron"),
                        "Streets": ("builtin", "open-street-map"),
                        "Dark": ("builtin", "carto-darkmatter"),
                        "Terrain": (
                            "raster",
                            "https://a.tile.opentopomap.org/"
                            "{z}/{x}/{y}.png",
                        ),
                        "Satellite": (
                            "raster",
                            "https://server.arcgisonline.com/ArcGIS/rest/"
                            "services/World_Imagery/MapServer/"
                            "tile/{z}/{y}/{x}",
                        ),
                    }
                    _base = st.selectbox(
                        "Basemap", list(_base_opts), key="res_map_base"
                    )
                with _c3:
                    st.write("")
                    _show_peak = st.toggle(
                        "Peak", value=True, key="res_map_peak",
                        help="Peak over the whole simulation vs. a "
                             "single timestep.",
                    )
                with _c4:
                    _panel = st.selectbox(
                        "Panel", list(_MAP_PANELS), key="res_map_panel",
                        help="Shape of the map panel. **Wide** is the full "
                             "content width. **Tall** keeps that width but "
                             "raises the panel. **Square** narrows it to an "
                             "equal-sided map - the shape most journals "
                             "want for a figure.",
                    )
                _map_h, _map_w = _MAP_PANELS[_panel]
                if _map_w:
                    # Pin the narrowed panel to an exact pixel width (the
                    # column around it only gives a percentage), so the map
                    # comes out square rather than screen-dependent.  The
                    # frame lives on the border wrapper, which may be the
                    # keyed element or its parent - cap both so it keeps
                    # hugging the map.
                    st.markdown(
                        "<style>"
                        "div.st-key-res_map_box,"
                        'div[data-testid="stVerticalBlockBorderWrapper"]'
                        ":has(.st-key-res_map_box){"
                        # width:100% is load-bearing - the chart inside
                        # sizes to its container, so as a flex child with
                        # only a max-width the panel collapses to nothing.
                        "width:100%;"
                        f"max-width:{_map_w}px;margin-left:auto;"
                        "margin-right:auto;}</style>",
                        unsafe_allow_html=True,
                    )

                if _show_peak:
                    _mvals = _np.nanmax(_arr, axis=0)
                    _mtag = "peak"
                elif _arr.shape[0] < 2:
                    # Static variables (Terrain) have a single frame -
                    # a one-option slider crashes the frontend
                    # (RangeError: min == max).
                    _ti = 0
                    _mvals = _arr[0]
                    _mtag = "static"
                else:
                    # The label carries the clock (v4.8.0) - the slider
                    # value alone is a bare wall-clock reading, which is
                    # exactly the ambiguity this release removes.
                    _ti = st.select_slider(
                        _tb.axis_label("Timestep"),
                        options=list(range(_arr.shape[0])),
                        value=int(
                            _np.nanargmax(_np.nanmax(_arr, axis=1))
                        ),
                        format_func=lambda i: _lst_time[i].strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                        key="res_map_t",
                        help=(
                            "Times are shown in the model site's local "
                            "standard time, with no daylight saving. "
                            "The UTC equivalent of the selected step is "
                            "shown just below the slider."
                            if _tb.known else
                            "This run was computed before v4.8.0, so it "
                            "never recorded which time zone its "
                            "timestamps are on. They are shown exactly "
                            "as the model stored them."
                        ),
                    )
                    _mvals = _arr[_ti]
                    _mtag = _model_time[_ti].strftime("%Y%m%d_%H%M")
                    st.caption(f"🕒 {_tb.stamp(_model_time[_ti])}")

                _scope = st.radio(
                    "Cells", ["Whole domain", "Wetted only"],
                    horizontal=True, key="res_map_scope",
                    help="Whole domain shows every mesh cell (full extent "
                         "+ boundaries). Wetted only hides dry cells so the "
                         "basemap shows through.",
                )
                _finite = _np.isfinite(_mvals)
                if _scope == "Wetted only":
                    if _var == "Water Depth":
                        _wet = _mvals > 0.05
                    elif _var == "Velocity":
                        _wet = _mvals > 0.01
                    elif _has_depth:
                        _depth_scope = _wse - _wse_data["min_elev"]
                        _wet = _np.nanmax(_depth_scope, axis=0) > 0.01
                    else:
                        _wet = _np.nanstd(_wse, axis=0) > 0.01
                    _wet = _wet & _finite
                else:
                    _wet = _finite
                _ids = _np.nonzero(_wet)[0]

                _ref = _lon[_wet] if _wet.any() else _lon
                _refy = _lat[_wet] if _wet.any() else _lat
                # Centre on the bbox centre (not the mean) so off-centre
                # geometries don't shift the viewport off the data.
                _lon_min = float(_np.nanmin(_ref))
                _lon_max = float(_np.nanmax(_ref))
                _lat_min = float(_np.nanmin(_refy))
                _lat_max = float(_np.nanmax(_refy))
                _center = dict(
                    lon=(_lon_min + _lon_max) / 2.0,
                    lat=(_lat_min + _lat_max) / 2.0,
                )

                # Auto-fit Mapbox zoom to the data extent so the flood
                # fills the viewport instead of being a small dot in a
                # zoomed-out city map.  Empirical formula: at zoom z,
                # one tile (256 px) spans 360 / 2**z degrees of
                # longitude (Mercator) - solve for z given the bbox.
                import math as _math
                _lon_span = max(
                    abs(_lon_max - _lon_min) * 1.25, 1e-4
                )
                _lat_span = max(
                    abs(_lat_max - _lat_min) * 1.25, 1e-4
                )
                _z_lon = _math.log2(360.0 * 1200.0 / (256.0 * _lon_span))
                _z_lat = _math.log2(170.0 * 620.0 / (256.0 * _lat_span))
                _zoom = max(3.0, min(18.0, min(_z_lon, _z_lat) - 0.35))

                # Inset colorbar - overlay on the map's right edge so
                # the basemap uses the full container width.
                _CBAR = dict(
                    title=dict(
                        text=f"<b>{_short}<br>({_unit})</b>",
                        font=dict(color="#0d1b2a", size=11),
                        side="top",
                    ),
                    x=0.985, y=0.5,
                    xanchor="right", yanchor="middle",
                    len=0.55, thickness=14,
                    bgcolor="rgba(255,255,255,0.88)",
                    bordercolor="#0d1b2a",
                    borderwidth=0.6,
                    outlinewidth=0,
                    tickfont=dict(color="#0d1b2a", size=11),
                )

                _mfig = None
                _extra_layers = []
                _pydeck_rendered = False
                # Legend to paint into the PNG export.  The Plotly maps
                # carry their colorbar in an SVG overlay, which is not part
                # of the map canvas the export reads back, so it is redrawn
                # from this spec - same card the pydeck maps show.
                _png_legend = None
                if _is_smooth:
                    # RAS-Mapper-style: drop the pre-rendered raster for
                    # the selected variable as a Mapbox image layer; carry
                    # the colorbar on an invisible marker trace (image
                    # layers have none).
                    import base64 as _b64m
                    # Honour the Cells scope: the renderer writes a
                    # wet-masked variant for WSE/velocity ("file_wet");
                    # depth is wet-by-definition and terrain has no wet
                    # variant (falls back to the full footprint).
                    _fname_s = _smooth_var["file"]
                    if _scope == "Wetted only":
                        _fname_s = _smooth_var.get("file_wet") or _fname_s
                        if not (npz_path.parent / _fname_s).exists():
                            _fname_s = _smooth_var["file"]
                    _png = npz_path.parent / _fname_s
                    _vmin_s = float(_smooth_var.get("vmin", 0.0))
                    _vmax_s = float(_smooth_var.get("vmax", 1.0))
                    if _vmax_s - _vmin_s < 1e-6:
                        _vmax_s = _vmin_s + 1.0
                    _u_s = _smooth_var.get("units", _unit)
                    _scale_s = _smooth_var.get("plotly", "Blues")

                    # Depth classes - discrete hazard bands (official
                    # flood-map style) as an alternative to the
                    # continuous ramp.  Offered only when this run's
                    # renderer wrote the banded raster (older runs
                    # have the continuous PNG only).
                    _cls_meta = (
                        _smooth_var.get("classes")
                        if _var == "Water Depth" else None
                    )
                    _cls_png = (
                        npz_path.parent
                        / str(_smooth_var.get("file_classes", ""))
                        if _cls_meta else None
                    )
                    _has_cls = bool(
                        _cls_meta and _cls_png and _cls_png.exists()
                    )
                    _shade_name = (
                        _smooth_var.get("file_shaded")
                        if _var == "Water Depth" else None
                    )
                    _has_shade = bool(
                        _shade_name
                        and (npz_path.parent / str(_shade_name)).exists()
                    )
                    _use_classes = False
                    _use_shade = False
                    if _has_cls or _has_shade:
                        _tgc1, _tgc2 = st.columns(2)
                        if _has_cls:
                            with _tgc1:
                                _use_classes = st.toggle(
                                    "Depth classes",
                                    key="res_smooth_classes",
                                    help="Show water depth in discrete "
                                         "hazard bands, the way official "
                                         "flood maps present it: each "
                                         "band reads as a practical risk "
                                         "level (around the second bound "
                                         "most vehicles stall). Off = "
                                         "continuous colour ramp.",
                                )
                        if _has_shade:
                            with _tgc2:
                                _use_shade = st.toggle(
                                    "Shaded relief",
                                    key="res_smooth_shade",
                                    help="Drape the water over the "
                                         "hillshaded terrain (RAS-Mapper "
                                         "style): dry ground shows the "
                                         "shaded relief and the terrain "
                                         "texture reads through the "
                                         "flood, instead of a flat "
                                         "colour sheet on the basemap.",
                                )
                    if _use_classes:
                        _png = _cls_png
                    if _use_shade:
                        _sh_file = (
                            _smooth_var.get("file_classes_shaded")
                            if _use_classes
                            else _smooth_var.get("file_shaded")
                        )
                        _sh_png = npz_path.parent / str(_sh_file or "")
                        if _sh_file and _sh_png.exists():
                            _png = _sh_png

                    # ── Smooth 3-D: the smooth raster draped over the
                    # physical peak surface as a GPU terrain mesh ─────
                    _s3d = st.toggle(
                        "3D view", key="res_pdk_3d",
                        help="Drape the smooth map over the physical "
                             "surface - terrain plus water-surface "
                             "elevation at the peak - as a GPU terrain "
                             "mesh. Right-drag (or Ctrl+drag) to tilt "
                             "and rotate.",
                    ) if _has_depth else False
                    if _s3d and not _show_peak:
                        st.info(
                            "The smooth 3-D surface uses the **peak** "
                            "state - turn **Peak** on to view it in "
                            "3-D. Showing the 2-D map for this "
                            "timestep."
                        )
                        _s3d = False
                    if _s3d:
                        _cs1, _cs2 = st.columns(2)
                        with _cs1:
                            _exag_s = st.slider(
                                "Vertical exaggeration",
                                min_value=1, max_value=10, value=5,
                                key="res_pdk_exag2",
                                help="Stretches the z-axis uniformly "
                                     "(terrain and water alike).",
                            )
                        with _cs2:
                            _bear_s = st.slider(
                                "View direction (°)",
                                min_value=0, max_value=350, value=0,
                                step=10, key="res_pdk_bearing",
                                help="Rotate the camera around the "
                                     "scene - or right-drag "
                                     "(Ctrl+drag) sideways to spin "
                                     "it freely.",
                            )
                        _s3d_assets = _smooth_terrain_assets(
                            str(npz_path), npz_path.stat().st_mtime,
                            str(_png), _png.stat().st_mtime,
                            tuple(
                                tuple(c)
                                for c in _smooth_meta["coordinates"]
                            ),
                        )
                        if _s3d_assets is None:
                            st.warning(
                                "Could not build the 3-D surface for "
                                "this run - showing the 2-D map."
                            )
                            _s3d = False
                        else:
                            import pydeck as _pdk
                            _elev_url, _tex_url, _bnds = _s3d_assets
                            # pydeck opens image-suffixed strings as
                            # LOCAL files unless they are full http(s)
                            # URLs - build the absolute URL from the
                            # request's Host header so it works on any
                            # deployment (localhost, ECS ALB, …).
                            try:
                                _hh = st.context.headers
                                _origin = (
                                    _hh.get("X-Forwarded-Proto", "http")
                                    + "://" + _hh.get("Host", "")
                                ) if _hh.get("Host") else ""
                            except Exception:
                                _origin = ""
                            _elev_url = _origin + _elev_url
                            _tex_url = _origin + _tex_url
                            _sfac = (
                                1.0 if _ru["length"] == "m" else 0.3048
                            ) * float(_exag_s)
                            _deck_s = _pdk.Deck(
                                layers=[_pdk.Layer(
                                    "TerrainLayer",
                                    elevation_data=_elev_url,
                                    texture=_tex_url,
                                    elevation_decoder={
                                        "rScaler": 25.6 * _sfac,
                                        "gScaler": 0.1 * _sfac,
                                        "bScaler": 0.0,
                                        "offset": 0.0,
                                    },
                                    bounds=list(_bnds),
                                    mesh_max_error=3.5,
                                )],
                                initial_view_state=_pdk.ViewState(
                                    longitude=_center["lon"],
                                    latitude=_center["lat"],
                                    zoom=_zoom, pitch=45,
                                    bearing=float(_bear_s),
                                ),
                                map_style=_PDK_STYLES.get(
                                    _base, _PDK_STYLES["Light"]
                                ),
                                height=_map_h,
                            )
                            with _map_panel_box(_map_w):
                                # st.pydeck_chart carries its own height
                                # (default 500) and ignores the Deck's -
                                # pass it here or the panel never grows.
                                st.pydeck_chart(
                                    _deck_s, width="stretch",
                                    height=_map_h,
                                )
                                if _use_classes:
                                    _legend_html_classes(
                                        _cls_meta["colors"],
                                        _cls_meta["labels"],
                                        _short, _u_s,
                                    )
                                else:
                                    _legend_html(
                                        _mpl_cmap_from_scale(_scale_s),
                                        _vmin_s, _vmax_s, _short, _u_s,
                                    )
                            st.caption(
                                "GPU terrain mesh (pydeck TerrainLayer)"
                                " - the smooth peak map draped over the"
                                " physical surface · scroll to zoom · "
                                "drag to pan · right-drag (or "
                                "Ctrl+drag) to tilt and rotate."
                            )
                            _map_save_png(
                                f"{_short.lower().replace(' ', '_')}"
                                f"_smooth3d_{_mtag}",
                                _legend_spec_classes(
                                    _cls_meta["colors"],
                                    _cls_meta["labels"], _short, _u_s,
                                ) if _use_classes else _legend_spec(
                                    _mpl_cmap_from_scale(_scale_s),
                                    _vmin_s, _vmax_s, _short, _u_s,
                                ),
                            )
                            _pydeck_rendered = True
                    if not _pydeck_rendered:
                        _png_legend = (
                            _legend_spec_classes(
                                _cls_meta["colors"], _cls_meta["labels"],
                                _short, _u_s,
                            ) if _use_classes else _legend_spec(
                                _mpl_cmap_from_scale(_scale_s),
                                _vmin_s, _vmax_s, _short, _u_s,
                            )
                        )
                        _data_uri = (
                            "data:image/png;base64,"
                            + _b64m.b64encode(_png.read_bytes()).decode()
                        )
                        _cbar_s = dict(_CBAR)
                        _cbar_s["title"] = dict(
                            text=f"<b>{_short}<br>({_u_s})</b>",
                            font=dict(color="#0d1b2a", size=11),
                            side="top",
                        )
                        if _use_classes:
                            # Stepped colorbar: one flat colour per
                            # class, tick label centred on each band.
                            _n_cls = len(_cls_meta["colors"])
                            _steps = []
                            for _ci, _cc in enumerate(
                                _cls_meta["colors"]
                            ):
                                _steps.append([_ci / _n_cls, _cc])
                                _steps.append([(_ci + 1) / _n_cls, _cc])
                            _cbar_s["tickvals"] = [
                                _ci + 0.5 for _ci in range(_n_cls)
                            ]
                            _cbar_s["ticktext"] = _cls_meta["labels"]
                            _marker_s = dict(
                                size=0.1, color=[0.0],
                                cmin=0.0, cmax=float(_n_cls),
                                colorscale=_steps, showscale=True,
                                colorbar=_cbar_s,
                            )
                        else:
                            _marker_s = dict(
                                size=0.1, color=[_vmin_s],
                                cmin=_vmin_s, cmax=_vmax_s,
                                colorscale=_scale_s, showscale=True,
                                colorbar=_cbar_s,
                            )
                        _mfig = _go.Figure(_go.Scattermapbox(
                            lon=[_center["lon"]], lat=[_center["lat"]],
                            mode="markers",
                            marker=_marker_s,
                            hoverinfo="skip", showlegend=False,
                        ))
                        _extra_layers = [dict(
                            sourcetype="image",
                            source=_data_uri,
                            coordinates=_smooth_meta["coordinates"],
                            # The shaded composite carries its own
                            # hillshaded terrain - draw it solid;
                            # water-only overlays stay slightly
                            # translucent over the basemap.
                            opacity=1.0 if _use_shade else 0.9,
                        )]
                elif _map_mode == "Filled cells":
                    _built = _cell_geojson(
                        str(npz_path), npz_path.stat().st_mtime
                    )
                    if _built is None:
                        st.warning(
                            "Filled mode needs polygon geometry - re-run "
                            "the simulation to populate it. Showing points."
                        )
                    else:
                        # GPU-rendered mesh via pydeck (deck.gl): smooth
                        # pan/zoom for big meshes, optional 3D extrusion.
                        _gj, _ = _built
                        import pydeck as _pdk
                        _cmap_pd = _mpl_cmap_from_scale(_scale)
                        _vlo = (
                            float(_np.nanmin(_mvals[_ids]))
                            if _ids.size else 0.0
                        )
                        _vhi = (
                            float(_np.nanmax(_mvals[_ids]))
                            if _ids.size else 1.0
                        )
                        if _vhi - _vlo < 1e-9:
                            _vhi = _vlo + 1.0
                        _c3d1, _c3d2, _c3d3, _c3d4 = st.columns(
                            [1.0, 1.6, 1.2, 1.2]
                        )
                        with _c3d1:
                            _extrude = st.toggle(
                                "3D view",
                                key="res_pdk_3d",
                                help="Extrude the mesh in 3D - "
                                     "right-drag (or Ctrl+drag) to tilt "
                                     "and rotate.",
                            )
                        _BASIS_POND = "Ponding (depth above flat ground)"
                        _BASIS_PHYS = "Physical (WSE over terrain)"
                        with _c3d2:
                            _basis = st.radio(
                                "3D height basis",
                                [_BASIS_POND, _BASIS_PHYS],
                                key="res_pdk_basis",
                                help="**Ponding** flattens the terrain "
                                     "onto the map and raises water by "
                                     "its depth - deeper ponds stand "
                                     "taller, rising water grows upward. "
                                     "**Physical** extrudes gray terrain "
                                     "plus water columns topping out at "
                                     "the true water-surface elevation; "
                                     "honest, but the valley fall dwarfs "
                                     "the water at basin scale.",
                            ) if (_extrude and _has_depth) else _BASIS_PHYS
                        _phys = _basis == _BASIS_PHYS
                        with _c3d3:
                            _exag = st.slider(
                                "Vertical exaggeration",
                                min_value=1, max_value=10, value=5,
                                key="res_pdk_exag2",
                                help="Stretches the z-axis uniformly "
                                     "(terrain and water alike).",
                            ) if _extrude else 0
                        with _c3d4:
                            _bearing = st.slider(
                                "View direction (°)",
                                min_value=0, max_value=350, value=0,
                                step=10, key="res_pdk_bearing",
                                help="Rotate the camera around the scene "
                                     "to see it from any side - or "
                                     "right-drag (Ctrl+drag) the map "
                                     "sideways to spin it freely.",
                            ) if _extrude else 0

                        # Physical heights: WSE above a common datum
                        # (mesh-lowest terrain), so column tops trace the
                        # real water surface instead of depth towers.
                        # deck.gl elevations are metres - convert if the
                        # model runs in feet.
                        _z2m = 1.0 if _ru["length"] == "m" else 0.3048
                        _min_el = (
                            _np.asarray(
                                _wse_data["min_elev"], dtype=_np.float32
                            )
                            if _has_depth else None
                        )
                        _wse_now = (
                            _np.nanmax(_wse, axis=0) if _show_peak
                            else _wse[_ti]
                        )
                        if _min_el is not None:
                            _datum = float(_np.nanmin(_min_el))
                        elif _np.isfinite(_wse_now).any():
                            _datum = float(_np.nanmin(_wse_now))
                        else:
                            _datum = 0.0
                        _depth_now = (
                            _wse_now - _min_el
                            if _min_el is not None else None
                        )

                        def _cell_height(_cid: int) -> float:
                            """Extrusion height in metres.

                            Ponding basis: depth above a flattened
                            ground (the map plane).  Physical basis:
                            WSE above the mesh-lowest terrain, so tops
                            trace the real water surface.  Terrain
                            variable always shows the DEM itself.
                            """
                            if (_var == "Terrain (DEM)"
                                    and _min_el is not None):
                                _h = _min_el[_cid] - _datum
                            elif not _phys and _depth_now is not None:
                                _h = _depth_now[_cid]
                            else:
                                _h = _wse_now[_cid] - _datum
                                if (not _np.isfinite(_h)
                                        and _min_el is not None):
                                    _h = _min_el[_cid] - _datum
                            if not _np.isfinite(_h):
                                _h = 0.0
                            return max(float(_h), 0.0) * _z2m

                        # In physical 3D, cells that are dry at the
                        # displayed time belong to the terrain base, not
                        # the water body - keep them out of the coloured
                        # layer so the two don't z-fight at identical
                        # heights.  Ponding has no terrain layer: dry
                        # cells stay as flat coloured polygons.
                        _dry_to_base = (
                            _extrude and _phys
                            and _depth_now is not None
                            and _var != "Terrain (DEM)"
                        )
                        _idset = {int(i) for i in _ids.tolist()}
                        _rows = []
                        for _f in _gj["features"]:
                            _cid = int(_f["id"])
                            if _cid not in _idset:
                                continue
                            _v = float(_mvals[_cid])
                            if not _np.isfinite(_v):
                                continue
                            if (_dry_to_base
                                    and not (_depth_now[_cid] > 0.01)):
                                continue
                            _rr, _gg, _bb, _ = _cmap_pd(
                                (_v - _vlo) / (_vhi - _vlo)
                            )
                            _rows.append({
                                "polygon": _f["geometry"]["coordinates"][0],
                                # opaque in 3D so the water body stands
                                # out of the terrain trench
                                "color": [int(_rr * 255), int(_gg * 255),
                                          int(_bb * 255),
                                          240 if _extrude else 210],
                                "elev": _cell_height(_cid),
                                "cell": _cid, "val": round(_v, 2),
                            })
                        _water_layer = _pdk.Layer(
                            "PolygonLayer", data=_rows,
                            get_polygon="polygon",
                            get_fill_color="color",
                            get_line_color=[70, 70, 70, 60],
                            line_width_min_pixels=0.3,
                            get_elevation="elev",
                            elevation_scale=(
                                float(_exag) if _extrude else 0.0
                            ),
                            extruded=bool(_extrude),
                            stroked=True, filled=True, pickable=True,
                        )
                        _terr_layer = None
                        if _extrude and _phys and _min_el is not None:
                            # Gray terrain base over the whole mesh so
                            # the water visibly sits in the valley.
                            _terr_rows = [{
                                "polygon":
                                    _f["geometry"]["coordinates"][0],
                                "elev": max(
                                    float(_min_el[int(_f["id"])])
                                    - _datum, 0.0,
                                ) * _z2m,
                            } for _f in _gj["features"]
                                if _np.isfinite(_min_el[int(_f["id"])])]
                            _terr_layer = _pdk.Layer(
                                "PolygonLayer", data=_terr_rows,
                                get_polygon="polygon",
                                # light warm gray - visually quiet so
                                # the water pops
                                get_fill_color=[198, 192, 182, 255],
                                get_elevation="elev",
                                elevation_scale=float(_exag),
                                extruded=True,
                                stroked=False, filled=True,
                                pickable=False,
                            )
                        if _terr_layer is None:
                            _layers_pdk = [_water_layer]
                        else:
                            _layers_pdk = [_terr_layer, _water_layer]
                        _style_pdk = _PDK_STYLES.get(
                            _base, _PDK_STYLES["Light"]
                        )
                        _deck = _pdk.Deck(
                            layers=_layers_pdk,
                            initial_view_state=_pdk.ViewState(
                                longitude=_center["lon"],
                                latitude=_center["lat"],
                                zoom=_zoom,
                                pitch=45 if _extrude else 0,
                                bearing=float(_bearing),
                            ),
                            map_style=_style_pdk,
                            height=_map_h,
                            tooltip={
                                "html": f"<b>Cell {{cell}}</b><br/>"
                                        f"{_short}: {{val}} {_unit}",
                            },
                        )
                        with _map_panel_box(_map_w):
                            # st.pydeck_chart carries its own height
                            # (default 500) and ignores the Deck's - pass
                            # it here or the panel never grows.
                            st.pydeck_chart(
                                _deck, width="stretch", height=_map_h,
                            )
                            _legend_html(
                                _cmap_pd, _vlo, _vhi, _short, _unit
                            )
                        st.caption(
                            "GPU-rendered mesh (pydeck) · scroll to zoom · "
                            "drag to pan · right-drag (or Ctrl+drag) to "
                            "tilt and rotate. "
                            + (
                                (
                                    "3D heights are physical: gray "
                                    "terrain + water columns topping out "
                                    "at the water-surface elevation, "
                                    "stretched by the exaggeration "
                                    "factor. "
                                    if _phys else
                                    "3D heights show ponding: terrain is "
                                    "flattened onto the map and water "
                                    "rises by its depth × exaggeration. "
                                ) if _extrude else ""
                            )
                        )
                        _map_save_png(
                            f"{_short.lower().replace(' ', '_')}"
                            f"_map_{_mtag}"
                            + ("_3d" if _extrude else ""),
                            _legend_spec(
                                _cmap_pd, _vlo, _vhi, _short, _unit
                            ),
                        )
                        _pydeck_rendered = True

                if not _pydeck_rendered:
                    if _mfig is None:
                        _mfig = _go.Figure(_go.Scattermapbox(
                            lon=_lon[_wet], lat=_lat[_wet], mode="markers",
                            marker=dict(
                                size=6, color=_mvals[_wet],
                                colorscale=_scale, showscale=True,
                                colorbar=_CBAR,
                            ),
                            customdata=_ids,
                            hovertemplate=(
                                "Cell %{customdata}<br>"
                                f"{_short}: " "%{marker.color:.2f} "
                                f"{_unit}<extra></extra>"
                            ),
                        ))
                        # Plotly auto-ranges the colorbar over the plotted
                        # values - mirror that range so the exported legend
                        # reads the same as the one on screen.
                        _pv = _mvals[_wet]
                        _pv = _pv[_np.isfinite(_pv)]
                        _png_legend = _legend_spec(
                            _mpl_cmap_from_scale(_scale),
                            float(_np.nanmin(_pv)) if _pv.size else 0.0,
                            float(_np.nanmax(_pv)) if _pv.size else 1.0,
                            _short, _unit,
                        )

                    _bkind, _bsrc = _base_opts[_base]
                    _layers = []
                    if _bkind == "raster":
                        _layers.append(dict(
                            sourcetype="raster",
                            source=[_bsrc],
                            below="traces",
                            opacity=1.0,
                        ))
                    # Depth raster (smooth mode) sits on the basemap.
                    _layers.extend(_extra_layers)
                    _mbox = dict(
                        style="white-bg" if _bkind == "raster" else _bsrc,
                        center=_center, zoom=_zoom,
                    )
                    if _layers:
                        _mbox["layers"] = _layers
                    _mfig.update_layout(
                        mapbox=_mbox,
                        autosize=True,
                        height=_map_h, margin=dict(l=0, r=0, t=0, b=0),
                    )
                    with _map_panel_box(_map_w):
                        st.plotly_chart(
                            _mfig, width="stretch",
                            config={
                                "displaylogo": False,
                                "scrollZoom": True,
                                "modeBarButtonsToAdd": [
                                    "zoomInMapbox", "zoomOutMapbox",
                                    "resetViewMapbox",
                                ],
                                "toImageButtonOptions": {
                                    "format": "png",
                                    "filename": (
                                        f"{_short.lower()}_map_{_mtag}"
                                    ),
                                    "scale": 3,
                                },
                            },
                        )
                    st.caption(
                        "Scroll to zoom · drag to pan · double-click to "
                        "reset. Switch **Map style → Filled cells** for the "
                        "GPU mesh view."
                    )
                    _map_save_png(
                        f"{_short.lower().replace(' ', '_')}_map_{_mtag}",
                        _png_legend,
                    )
                    # Lazy HTML export - only serialise on demand (the
                    # 50k-cell figure is expensive to convert every rerun).
                    if st.button(
                        "Prepare map download (HTML)", key="prep_map_dl"
                    ):
                        st.session_state["_map_html_ready"] = True
                    if st.session_state.get("_map_html_ready"):
                        st.download_button(
                            "Download map (HTML)",
                            data=_mfig.to_html(include_plotlyjs="cdn"),
                            file_name=f"{_short.lower()}_map.html",
                            mime="text/html",
                            key="map_dl_btn",
                        )

            st.divider()

            # ── Flood propagation animation (GIF) ──
            st.markdown(f"##### {_short} Propagation Animation")
            if _var == "Terrain (DEM)":
                st.info(
                    "Terrain is static - switch the **Variable** selector "
                    "to WSE, Velocity, or Water Depth to animate flood "
                    "propagation."
                )
            elif _arr.shape[0] < 2:
                st.caption("Animation needs at least two timesteps.")
            else:
                # Mirror the inundation-map settings chosen above
                _gif_style = st.session_state.get(
                    "res_map_mode", "Filled cells"
                )
                _gif_base = st.session_state.get(
                    "res_map_base", "Light"
                )
                _gif_scope = st.session_state.get(
                    "res_map_scope", "Whole domain"
                )
                st.caption(
                    "Generate a full-colour **video** of the flood event - "
                    "it uses the same **map style**, **basemap** and "
                    "**cell scope** you picked for the inundation map "
                    "above. It plays inline with **play/pause** and a "
                    "**scrub bar** so you can stop at any time, and "
                    "downloads as MP4 (or GIF for slides)."
                )
                st.caption(
                    f"Current settings → style: **{_gif_style}** · "
                    f"basemap: **{_gif_base}** · cells: **{_gif_scope}**"
                )
                _gc1, _gc2, _gc3 = st.columns([2, 1, 1])
                with _gc1:
                    _gif_frames = st.slider(
                        "Frames",
                        min_value=20, max_value=200, value=80, step=10,
                        key="res_gif_frames",
                        help="Evenly-spaced timesteps sampled for the "
                             "video. More frames = smoother but larger.",
                    )
                with _gc2:
                    _gif_speed = st.selectbox(
                        "Speed",
                        options=[("Fast", 12), ("Medium", 8), ("Slow", 4)],
                        index=0, format_func=lambda x: x[0],
                        key="res_gif_speed",
                    )
                with _gc3:
                    st.write("")
                    if st.button(
                        "Generate video",
                        key="res_gen_gif",
                        width="stretch",
                        type="primary",
                    ):
                        # Freeze the settings used for this animation so
                        # later dropdown changes don't trigger a re-render.
                        st.session_state["_gif_params"] = (
                            _var, int(_gif_frames),
                            int(_gif_speed[1]),
                            _gif_style, _gif_base, _gif_scope,
                        )
                        st.session_state.pop("_gif_gif_bytes", None)

                _gp = st.session_state.get("_gif_params")
                # Older sessions may hold an 8-tuple (dropped 3-D video) -
                # discard anything that isn't the current 6-tuple.
                if _gp and len(_gp) != 6:
                    _gp = None
                    st.session_state.pop("_gif_params", None)
                if not _gp:
                    st.caption(
                        "Set the frames and speed above, then click "
                        "**Generate video**."
                    )
                else:
                    try:
                        _vid_bytes = _build_flood_gif(
                            str(npz_path), npz_path.stat().st_mtime,
                            *_gp, "mp4",
                        )
                    except Exception as _vid_err:
                        _vid_bytes = None
                        st.error(f"Video rendering failed: {_vid_err}")
                    if _vid_bytes is None:
                        st.info("No wet cells to animate.")
                    else:
                        st.video(_vid_bytes, format="video/mp4")
                        st.caption(
                            f"{_gp[0]} flood propagation - {_gp[1]} frames "
                            f"· {_gp[3]} · {_gp[4]} basemap · {_gp[5]} · "
                            f"{len(_vid_bytes) / 1e6:.1f} MB · use the "
                            "player's play/pause and scrub bar."
                        )
                        _dlc1, _dlc2 = st.columns(2)
                        with _dlc1:
                            st.download_button(
                                "Download MP4",
                                data=_vid_bytes,
                                file_name=(
                                    f"{_short.lower()}_flood_animation.mp4"
                                ),
                                mime="video/mp4",
                                key="vid_dl_btn",
                                width="stretch",
                            )
                        with _dlc2:
                            # Build the GIF lazily only if asked (it's the
                            # slower, lower-quality format - for slides).
                            if st.button(
                                "Prepare GIF (for slides)",
                                key="res_gen_gif2",
                                width="stretch",
                            ):
                                st.session_state["_gif_gif_bytes"] = (
                                    _build_flood_gif(
                                        str(npz_path),
                                        npz_path.stat().st_mtime,
                                        *_gp, "gif",
                                    )
                                )
                            _gif_b = st.session_state.get("_gif_gif_bytes")
                            if _gif_b:
                                st.download_button(
                                    "Download GIF",
                                    data=_gif_b,
                                    file_name=(
                                        f"{_short.lower()}"
                                        "_flood_animation.gif"
                                    ),
                                    mime="image/gif",
                                    key="gif_dl_btn",
                                    width="stretch",
                                )
                        gc.collect()
            st.divider()

            # ── Time series at a selected cell ──
            st.markdown(f"##### {_short} Time Series at a Cell")
            st.caption(
                "Pick a cell to inspect its time series. Hover for values, "
                "drag to zoom, use the camera icon to save a PNG. Use "
                "**Tab 6 · Validation** to compare with an observed gage."
            )

            _cell_idx = st.number_input(
                "Cell index",
                value=_peak_cell,
                min_value=0,
                max_value=int(_arr.shape[1] - 1),
                key="res_cell",
            )

            _plot_cfg = {
                "displaylogo": False,
                "toImageButtonOptions": {
                    "format": "png",
                    "filename": f"{_short.lower()}_cell_{_cell_idx}",
                    "scale": 3,
                },
            }

            _cell_series = _arr[:, _cell_idx]
            _pfig = _go.Figure()
            _pfig.add_trace(_go.Scatter(
                x=_lst_time,
                y=_cell_series,
                mode="lines",
                line=dict(width=1.8, color=_color),
                name=f"{_short} ({_unit})",
                hovertemplate=(
                    "%{x|%Y-%m-%d %H:%M}<br>"
                    f"{_short}: " "%{y:.3f} " f"{_unit}<extra></extra>"
                ),
            ))
            _pfig.update_layout(showlegend=True)
            _polish_fig(
                _pfig,
                ytitle=f"{_short} ({_unit})",
                xtitle=_tb.axis_label(),
                title=(
                    f"{_var} - Cell {_cell_idx} "
                    f"({_coords[_cell_idx, 0]:.0f}, "
                    f"{_coords[_cell_idx, 1]:.0f})"
                ),
            )
            st.plotly_chart(
                _pfig, width="stretch", config=_plot_cfg
            )

            # ── Downloads: data CSV + interactive HTML ──
            # Exports carry the offset explicitly (ISO-8601).  A naive
            # local timestamp in a CSV recreates exactly the ambiguity
            # this release exists to remove.
            _cell_df = _rpd.DataFrame({
                "datetime": [_tb.iso(_t) for _t in _model_time],
                f"{_short.lower()}_{_unit.replace('/', 'p')}": _cell_series,
            })
            _dc1, _dc2 = st.columns(2)
            _dc1.download_button(
                "Download data (CSV)",
                data=_cell_df.to_csv(index=False),
                file_name=f"{_short.lower()}_cell_{_cell_idx}.csv",
                mime="text/csv",
                width="stretch",
            )
            _dc2.download_button(
                "Download interactive plot (HTML)",
                data=_pfig.to_html(include_plotlyjs="cdn"),
                file_name=f"{_short.lower()}_cell_{_cell_idx}.html",
                mime="text/html",
                width="stretch",
            )

            st.divider()

            # ── Live cumulative plot for the active auto-schedule ──
            # Read the on-disk auto-scheduler history file and collect
            # every "done" iteration that belongs to the current
            # schedule_root.  This produces one continuous time series
            # that grows after every completed loop iteration - visible
            # *during* the schedule, not only after all runs finish.
            # Works across page reloads because it's disk-backed.
            try:
                _hist_runs: list[dict] = []
                if HISTORY_FILE.exists():
                    _all_hist = json.loads(HISTORY_FILE.read_text())
                    if isinstance(_all_hist, list):
                        # If a schedule is active, restrict to its
                        # iterations (same `schedule_root`).  Otherwise
                        # fall back to whatever schedule the currently
                        # opened run belongs to.
                        _sched_now = _read_schedule_state() or {}
                        _root_filter = _sched_now.get("schedule_root")
                        if not _root_filter:
                            # Infer from the open run's parent folder.
                            try:
                                _root_filter = (
                                    Path(last_output).parent.name
                                    if last_output else None
                                )
                            except Exception:
                                _root_filter = None
                        for _e in _all_hist:
                            if (
                                not isinstance(_e, dict)
                                or _e.get("state") != "done"
                            ):
                                continue
                            _od = _e.get("output_dir") or ""
                            if (
                                _root_filter
                                and _root_filter not in _od
                            ):
                                continue
                            _p = Path(_od) / "wse_extract.npz"
                            if not _p.exists():
                                continue
                            _hist_runs.append({
                                "output_dir": _od,
                                "start": _e.get("start", ""),
                                "end": _e.get("end", ""),
                                "npz": _p,
                            })

                if len(_hist_runs) >= 2:
                    # Sort by simulation-window start so the cumulative
                    # trace flows left-to-right in real chronological
                    # order, regardless of which iteration finished
                    # first on disk.
                    _hist_runs.sort(
                        key=lambda r: _rpd.to_datetime(r["start"])
                        if r["start"] else _rpd.Timestamp.min
                    )

                    st.markdown(
                        f"##### Live cumulative {_short} - all "
                        f"completed iterations of this schedule"
                    )
                    st.caption(
                        f"Auto-updating from the auto-scheduler "
                        f"history file.  {len(_hist_runs)} iteration"
                        f"{'s' if len(_hist_runs) != 1 else ''} so "
                        "far · grows after every completed loop."
                    )

                    _cum_fig = _go.Figure()
                    _cum_x: list = []
                    _cum_y: list = []
                    for _ix, _hr in enumerate(_hist_runs):
                        try:
                            _hd = _np.load(
                                _hr["npz"], allow_pickle=True
                            )
                        except Exception:
                            continue
                        if _var == "Velocity":
                            if "vel" not in _hd.files:
                                continue
                            _harr = _hd["vel"]
                        elif _var == "Water Depth":
                            if "min_elev" not in _hd.files:
                                continue
                            _harr = _np.clip(
                                _hd["wse"] - _hd["min_elev"],
                                0.0, None,
                            )
                        else:
                            _harr = _hd["wse"]
                        _htime = _rpd.to_datetime(_hd["model_time"])
                        _hci = min(_cell_idx, _harr.shape[1] - 1)
                        if _cum_x:
                            # Break the line between iterations - otherwise
                            # overlapping windows make the x jump backwards
                            # and Plotly draws a flat connector across the
                            # whole axis.
                            _cum_x.append(None)
                            _cum_y.append(None)
                        _cum_x.extend(_htime.tolist())
                        _cum_y.extend(_harr[:, _hci].tolist())

                    if _cum_x:
                        _cum_fig.add_trace(_go.Scatter(
                            x=_cum_x,
                            y=_cum_y,
                            mode="lines",
                            name=f"{_short} (cumulative)",
                            line=dict(width=1.5),
                            connectgaps=False,
                            hovertemplate=(
                                "%{x|%Y-%m-%d %H:%M}<br>"
                                f"{_short}: " "%{y:.3f} "
                                f"{_unit}<extra></extra>"
                            ),
                        ))
                        _polish_fig(
                            _cum_fig,
                            ytitle=f"{_short} ({_unit})",
                            title=(
                                f"Live cumulative {_short} - Cell "
                                f"{_cell_idx} · "
                                f"{len(_hist_runs)} iterations"
                            ),
                        )
                        st.plotly_chart(
                            _cum_fig, width="stretch",
                            config=_plot_cfg,
                        )
                        st.divider()
            except Exception as _cum_err:
                # Plot is a nice-to-have - never let it break Tab 5.
                st.caption(
                    f"(Live cumulative plot unavailable: {_cum_err})"
                )

            # ── Cumulative run history ────────────────────────────
            # Designed for the auto-scheduler use case (consecutive runs
            # every N hours).  When runs are ad-hoc historical events
            # from very different periods (e.g. Oct 2022 + Oct 2025), the
            # plot becomes a pair of spike clusters with multi-year empty
            # space between them - useless.  Heuristic: if any gap
            # between adjacent run windows exceeds 24 h, treat as ad-hoc
            # and hide the plot by default behind a toggle.
            _run_history = st.session_state.get("run_history", [])
            if len(_run_history) > 1:
                # Detect ad-hoc vs scheduled by checking inter-run gaps.
                _runs_sorted = sorted(
                    _run_history,
                    key=lambda r: _rpd.to_datetime(r.get("start", "")),
                )
                _is_contiguous = True
                for _a, _b in zip(_runs_sorted, _runs_sorted[1:]):
                    try:
                        _gap = (
                            _rpd.to_datetime(_b["start"])
                            - _rpd.to_datetime(_a.get("end", _a["start"]))
                        ).total_seconds()
                    except Exception:
                        _gap = 0
                    if _gap > 24 * 3600:
                        _is_contiguous = False
                        break

                # Show the plot by default for contiguous (scheduler)
                # runs; hide it behind a toggle for ad-hoc historical
                # comparisons where it tends to be uninformative.
                _show_history = True
                if not _is_contiguous:
                    st.caption(
                        "The runs in this session are from "
                        "non-contiguous time periods (ad-hoc historical "
                        "events rather than a continuous schedule). "
                        "The cumulative overlay would show large empty "
                        "gaps between runs - hidden by default."
                    )
                    _show_history = st.toggle(
                        "Show run-history overlay anyway",
                        value=False,
                        key=f"show_runhist_{_var}",
                    )

                if _show_history:
                    st.markdown(f"##### Run History - {_short} (all runs)")
                    _hist_fig = _go.Figure()
                    # Reference window = the most recent run's actual span.
                    # Only overlay runs whose window OVERLAPS it, so a stale
                    # one-off (e.g. a 2022 run) isn't stretched across the
                    # same absolute-time axis as a 2026 real-time schedule.
                    _ref_span = None
                    for _rh in reversed(_run_history):
                        _p = Path(_rh["output_dir"]) / "wse_extract.npz"
                        if not _p.exists():
                            continue
                        try:
                            _t = _rpd.to_datetime(
                                _np.load(_p, allow_pickle=True)["model_time"]
                            )
                            _ref_span = (_t.min(), _t.max())
                            break
                        except Exception:
                            continue
                    _n_plotted = 0
                    _n_skipped = 0
                    for _ri, _rh in enumerate(_run_history):
                        _rh_path = (
                            Path(_rh["output_dir"]) / "wse_extract.npz"
                        )
                        if not _rh_path.exists():
                            continue
                        _rh_data = _np.load(_rh_path, allow_pickle=True)
                        if _var == "Velocity":
                            if "vel" not in _rh_data.files:
                                continue
                            _rh_arr = _rh_data["vel"]
                        elif _var == "Water Depth":
                            if "min_elev" not in _rh_data.files:
                                continue
                            _rh_arr = _np.clip(
                                _rh_data["wse"] - _rh_data["min_elev"],
                                0.0, None,
                            )
                        else:
                            _rh_arr = _rh_data["wse"]
                        _rh_time = _rpd.to_datetime(_rh_data["model_time"])
                        # Skip runs whose window is disjoint from the latest.
                        if _ref_span is not None and (
                            _rh_time.max() < _ref_span[0]
                            or _rh_time.min() > _ref_span[1]
                        ):
                            _n_skipped += 1
                            continue
                        _ci = min(_cell_idx, _rh_arr.shape[1] - 1)
                        _hist_fig.add_trace(_go.Scatter(
                            x=_rh_time,
                            y=_rh_arr[:, _ci],
                            mode="lines",
                            # Label from the ACTUAL plotted window, not the
                            # stored 'start' (which can be the model's native
                            # date and mismatch a re-timed run).
                            name=(
                                f"Run {_ri + 1} "
                                f"({_rh_time.min():%Y-%m-%d})"
                            ),
                            hovertemplate=(
                                "%{x|%Y-%m-%d %H:%M}<br>"
                                f"{_short}: " "%{y:.3f} "
                                f"{_unit}<extra></extra>"
                            ),
                        ))
                        _n_plotted += 1
                    if _n_skipped:
                        st.caption(
                            f"{_n_skipped} run(s) with a different time "
                            "window are hidden (they don't overlap the "
                            "latest run)."
                        )
                    _polish_fig(
                        _hist_fig,
                        ytitle=f"{_short} ({_unit})",
                        title=f"Cumulative {_short} - All Runs",
                    )
                    st.plotly_chart(
                        _hist_fig, width="stretch",
                        config={"displaylogo": False},
                    )
                    st.divider()

            gc.collect()
          except Exception as _res_err:
            st.error(
                f"**Error loading results:** {_res_err}\n\n"
                "The output file may be incomplete or corrupted. "
                "Try re-running the simulation."
            )
            _wse_data = None

        # ── HDF and raw files ──
        all_files = sorted(output_path.glob("*"))
        hdfs = [f for f in all_files if f.suffix == ".hdf"]
        raw_dir = output_path / "raw"
        raw_csvs = sorted(raw_dir.glob("*.csv")) if raw_dir.is_dir() else []

        if hdfs:
            st.markdown("##### HDF Output Files")
            for h in hdfs:
                st.caption(f"`{h.name}` - {h.stat().st_size / 1e6:.1f} MB")

        if raw_csvs:
            import pandas as _raw_pd

            with st.expander("Raw boundary condition data (CSV)"):
                for c in raw_csvs:
                    st.markdown(f"**{c.name}**")
                    st.dataframe(
                        _raw_pd.read_csv(c), width="stretch"
                    )

        if not _wse_data and not hdfs:
            st.info(
                "Simulation output folder exists but no extractable data "
                "yet. The simulation may still be running."
            )

    _tab_nav(4)


# ── TAB 6: Validation ────────────────────────────────────────────────


with tab_results:
    # Auto-refresh the Results tab in place while a schedule is armed,
    # mirroring the Live tab: a run_every fragment re-renders the body
    # (which resolves the latest finished run) so new iterations appear
    # without a manual browser refresh.  When no schedule is running we
    # render once, preserving full interactivity for browsing past runs
    # (v3.1.3).
    _sched_armed_for_results = bool(
        (_read_schedule_state() or {}).get("enabled")
    )
    if _sched_armed_for_results:
        @st.fragment(run_every="8s")
        def _results_live_fragment() -> None:
            _render_results_tab_body()
        _results_live_fragment()
    else:
        _render_results_tab_body()
with tab_val:
    if _busy_banner_if_active():
        _tab_nav(5)
    else:
        st.subheader("Validation Settings")

        has_results = st.session_state.get("run_complete", False)
        last_output = st.session_state.get("last_output_dir")

        if not has_results or not last_output:
            st.info(
                "Run a simulation in **Tab 4 · Run** first. "
                "Validation compares model output with observed gage data."
            )

        # ── Gage configuration ──
        st.markdown("##### Validation Gage")
        val_source = st.selectbox(
            "Data source", ["USGS", "NOAA"], key="val_src"
        )
        validation_station = st.text_input(
            "Validation station ID", value="", key="val_st",
            help=(_USGS_STATION_HELP if val_source == "USGS"
                  else _NOAA_STATION_HELP),
        )
        validation_param = st.text_input(
            "Parameter code (USGS)", value="00065", key="val_pc",
            help=_USGS_PARAM_HELP,
        )

        st.divider()

        # ── Location ──
        st.markdown("##### Validation Location")
        val_method = st.radio(
            "Specify location by",
            ["Lat / Lon", "Cell index"],
            horizontal=True,
            key="val_m",
        )
        if val_method == "Lat / Lon":
            vc1, vc2 = st.columns(2)
            with vc1:
                val_lat = st.number_input(
                    "Latitude", value=0.0, format="%.6f", key="val_lat"
                )
            with vc2:
                val_lon = st.number_input(
                    "Longitude", value=0.0, format="%.6f", key="val_lon"
                )
            val_cell = None
        else:
            val_cell = st.number_input(
                "Cell index", value=0, min_value=0, key="val_cell"
            )
            val_lat = val_lon = None

        st.divider()

        validate_clicked = st.button(
            "Run Validation",
            type="primary",
            width="stretch",
            disabled=not (has_results and last_output),
        )

        if validate_clicked and last_output and not validation_station.strip():
            # Guard the common mistake up front - a friendly message beats
            # a backend traceback ("no station ID was provided").
            st.error(
                "Enter a **validation station ID** first - the USGS site "
                "number (e.g. `08075000`) or NOAA station ID for the gage "
                "you want to compare against. Validation also needs a "
                "**past** date range with observed data (a forecast window "
                "has none yet)."
            )
        elif validate_clicked and last_output:
            validation_cfg: dict = {
                "source": val_source.lower(),
                "station": validation_station,
                "parameter": validation_param,
            }
            if val_cell is not None:
                validation_cfg["cell_index"] = int(val_cell)
            else:
                validation_cfg["lat"] = val_lat
                validation_cfg["lon"] = val_lon

            with open(_settings_path(), "r") as f:
                settings = yaml.safe_load(f)
            settings["validation"] = validation_cfg
            settings["hecras"]["output_dir"] = last_output
            with open(_settings_path(), "w") as f:
                yaml.dump(
                    settings, f, default_flow_style=False, sort_keys=False
                )

            progress = st.progress(0, text="Starting validation…")
            log_area = st.container().empty()
            status = st.status("Running validation...", expanded=True)

            rc, lines = _run_subprocess(
                [sys.executable, "-m", "main", "validate"],
                progress,
                log_area,
                status,
            )

            if rc == 0:
                progress.progress(100, text="100% - Validation complete")
                status.update(
                    label="Validation complete!",
                    state="complete",
                    expanded=False,
                )
                st.session_state["validation_complete"] = True
                st.success("Validation finished successfully.")
            else:
                progress.progress(100, text="Failed")
                status.update(
                    label="Validation failed", state="error", expanded=True
                )
                st.error(f"Validation exited with code {rc}")
                st.code("".join(lines), language="text")

        # ── Show validation results inline ──
        if last_output and st.session_state.get("validation_complete"):
            _val_out = Path(last_output)
            _val_plots = sorted(_val_out.glob("*.png"))
            _val_txts = [
                f for f in sorted(_val_out.glob("*.txt"))
                if f.name != "settings.txt"
            ]
            _val_csvs = sorted(_val_out.glob("*.csv"))

            _val_jsons = sorted(_val_out.glob("metrics_ci_*.json"))

            if _val_plots or _val_txts or _val_csvs or _val_jsons:
                st.divider()
                st.subheader("Validation Results")

                # ── Metrics table with 95% bootstrap CIs (v3.1.0) ──
                if _val_jsons:
                    import json as _vjson
                    try:
                        _mj = _vjson.loads(_val_jsons[-1].read_text())
                    except Exception:
                        _mj = None
                    if _mj:
                        _pt = _mj.get("point", {})
                        _ci = _mj.get("ci95", {})
                        # metric key in point dict  ->  display label
                        _rows_spec = [
                            ("nse", "NSE", "NSE"),
                            ("kge", "KGE", "KGE"),
                            ("rmse", "RMSE", "RMSE"),
                            ("mae", "MAE", "MAE"),
                            ("bias", "Bias", "Bias"),
                            ("pearson_r", "Pearson r", "Pearson r"),
                        ]
                        _table = []
                        for _pkey, _cikey, _label in _rows_spec:
                            _val = _pt.get(_pkey)
                            _lohi = _ci.get(_cikey)
                            _ci_txt = "-"
                            if _lohi and all(
                                v is not None and v == v for v in _lohi
                            ):
                                _ci_txt = f"[{_lohi[0]:.3f}, {_lohi[1]:.3f}]"
                            _table.append({
                                "Metric": _label,
                                "Value": (
                                    f"{_val:.3f}" if isinstance(_val, (int, float))
                                    and _val == _val else "-"
                                ),
                                "95% CI (bootstrap)": _ci_txt,
                            })
                        st.markdown(
                            f"**Goodness-of-fit** · n = {_mj.get('n', '?')} "
                            "paired samples"
                        )
                        st.dataframe(
                            _table, width="stretch", hide_index=True,
                        )
                        # KGE decomposition + educational popup.
                        _kr = _pt.get("kge_r")
                        _ka = _pt.get("kge_alpha")
                        _kb = _pt.get("kge_beta")
                        if all(
                            isinstance(x, (int, float)) and x == x
                            for x in (_kr, _ka, _kb)
                        ):
                            st.caption(
                                f"KGE decomposition →  r = {_kr:.3f}  ·  "
                                f"α (variability) = {_ka:.3f}  ·  "
                                f"β (bias ratio) = {_kb:.3f}"
                            )
                        with st.expander(
                            "How to read these metrics (KGE & "
                            "confidence intervals)"
                        ):
                            st.info(
                                "**KGE - Kling-Gupta Efficiency.**  A "
                                "modern hydrologic skill score that "
                                "splits model performance into three "
                                "independent parts:\n\n"
                                "- **r** - correlation (does the model "
                                "get the *timing / shape* right?)\n"
                                "- **α** - variability ratio "
                                "`std(model)/std(obs)` (does it get the "
                                "*amplitude* right? 1 = perfect)\n"
                                "- **β** - bias ratio "
                                "`mean(model)/mean(obs)` (1 = no bias)\n\n"
                                "`KGE = 1 − √((r−1)² + (α−1)² + (β−1)²)`, "
                                "so **1 is perfect**. KGE complements NSE "
                                "(which can be dominated by high flows).\n\n"
                                "For a **stage** series on an arbitrary "
                                "vertical datum (e.g. ~628 ft NGVD29) the "
                                "mean is large, so **β ≈ 1 always** and KGE "
                                "is driven by r and α - read it with that "
                                "in mind.\n\n"
                                "**95% CI (bootstrap).**  Each metric is a "
                                "single estimate from a finite, "
                                "autocorrelated record. The interval comes "
                                "from a **moving-block bootstrap** "
                                "(resampling contiguous blocks to respect "
                                "serial correlation) - it tells you how "
                                "much the score would wobble on a "
                                "different but equivalent sample. Narrow = "
                                "robust; wide = treat the point value with "
                                "caution."
                            )

                # ── Raw metrics text (kept for completeness) ──
                if _val_txts:
                    with st.expander("Raw metrics (text)"):
                        for t in _val_txts:
                            st.markdown(
                                f"**{t.stem.replace('_', ' ').title()}**"
                            )
                            st.code(t.read_text(), language="text")

                # ── Interactive plots from CSV data ──
                if _val_csvs:
                    import pandas as _vpd
                    import plotly.graph_objects as _vgo

                    _vu = _unit_labels_for_run(_val_out)
                    # Both series in this CSV are on the model's clock
                    # (validate() shifts the observations onto it), so
                    # one conversion renders the pair (v4.8.0).
                    from timebase import TimeBase as _VTimeBase
                    _vtb = _VTimeBase.from_dir(_val_out)
                    for c in _val_csvs:
                        df_val = _vpd.read_csv(c, parse_dates=True, index_col=0)
                        if "model" in df_val.columns and "observed" in df_val.columns:
                            _vx = _vtb.to_lst(df_val.index)
                            # Time series comparison
                            _ts_fig = _vgo.Figure()
                            _ts_fig.add_trace(_vgo.Scatter(
                                x=_vx, y=df_val["model"],
                                mode="lines", name="Model",
                                line=dict(color="#1f77b4", width=1.8),
                                hovertemplate=f"%{{x|%Y-%m-%d %H:%M}}<br>Model: %{{y:.2f}} {_vu['length']}<extra></extra>",
                            ))
                            _ts_fig.add_trace(_vgo.Scatter(
                                x=_vx, y=df_val["observed"],
                                mode="lines", name="Observed",
                                line=dict(color="#ff7f0e", width=1.8),
                                hovertemplate=f"%{{x|%Y-%m-%d %H:%M}}<br>Obs: %{{y:.2f}} {_vu['length']}<extra></extra>",
                            ))
                            _polish_fig(
                                _ts_fig,
                                ytitle=f"Stage ({_vu['length']})",
                                xtitle=_vtb.axis_label(),
                                title="Model vs Observed",
                            )
                            st.plotly_chart(
                                _ts_fig, width="stretch",
                                config={"displaylogo": False},
                            )

                            # 1:1 scatter
                            _lo = min(df_val["observed"].min(), df_val["model"].min())
                            _hi = max(df_val["observed"].max(), df_val["model"].max())
                            _pad = 0.05 * (_hi - _lo) if _hi > _lo else 0.5
                            _oo_fig = _vgo.Figure()
                            _oo_fig.add_trace(_vgo.Scatter(
                                x=df_val["observed"], y=df_val["model"],
                                mode="markers", name="Paired samples",
                                marker=dict(size=6, color="#1f77b4", opacity=0.6),
                                hovertemplate="Obs: %{x:.2f}<br>Model: %{y:.2f}<extra></extra>",
                            ))
                            _oo_fig.add_trace(_vgo.Scatter(
                                x=[_lo - _pad, _hi + _pad],
                                y=[_lo - _pad, _hi + _pad],
                                mode="lines", name="1:1 line",
                                line=dict(color="black", dash="dash", width=1),
                            ))
                            _polish_fig(
                                _oo_fig,
                                ytitle="Model",
                                xtitle="Observed",
                                title="1:1 Plot",
                                square=True,
                            )
                            _oo_fig.update_xaxes(
                                range=[_lo - _pad, _hi + _pad]
                            )
                            _oo_fig.update_yaxes(
                                range=[_lo - _pad, _hi + _pad]
                            )
                            st.plotly_chart(
                                _oo_fig, width="content",
                                config={"displaylogo": False},
                            )

                        with st.expander(f"Raw data: {c.name}"):
                            st.dataframe(df_val, width="stretch")
                            st.download_button(
                                f"Download {c.name}",
                                data=df_val.to_csv(),
                                file_name=c.name,
                                mime="text/csv",
                                key=f"dl_{c.name}",
                            )

                # ── Fallback: show saved PNGs if no CSV available ──
                elif _val_plots:
                    for p in _val_plots:
                        st.image(
                            str(p), caption=p.name, width="stretch"
                        )

        # ── Ensemble verification - CRPS (v3.1.0) ──────────────────
        st.divider()
        with st.expander("Ensemble verification (CRPS)"):
            st.info(
                "**CRPS - Continuous Ranked Probability Score.**  When "
                "you drive a boundary from an **NWM ensemble** "
                "(medium-range has 7 members, long-range 4) you can run "
                "HEC-RAS once per member and score the *whole ensemble* "
                "- not just member 1.\n\n"
                "CRPS rewards a forecast that is both **accurate** (the "
                "members bracket the truth) and **sharp** (not "
                "needlessly wide). It is reported in the model's own "
                "units (ft/cfs for English models, m/m³·s⁻¹ for SI) and "
                "**collapses exactly to MAE when "
                "the ensemble has a single member**, so it is directly "
                "comparable to the deterministic error. **Lower is "
                "better.**\n\n"
                "**How to use:** run the same model/window once for each "
                "ensemble member (set *Ensemble member = 1, 2, …* in the "
                "Forecast boundary), run validation each time, then "
                "select the resulting `model_vs_observed_*.csv` exports "
                "below (one per member)."
            )

            # Auto-detect candidate member CSVs near the current run.
            _member_csvs: list = []
            try:
                if last_output:
                    _root = Path(last_output).parent
                    _member_csvs = sorted(
                        {
                            p for p in _root.rglob("model_vs_observed*.csv")
                        },
                        key=lambda p: str(p),
                    )
            except Exception:
                _member_csvs = []

            _picked_paths: list = []
            if _member_csvs:
                _labels = {
                    str(p.relative_to(Path(last_output).parent)): p
                    for p in _member_csvs
                }
                _sel = st.multiselect(
                    "Select ≥2 member exports (auto-detected near this run)",
                    options=list(_labels.keys()),
                    key="crps_pick",
                )
                _picked_paths = [_labels[s] for s in _sel]
            else:
                st.caption(
                    "No `model_vs_observed_*.csv` files auto-detected - "
                    "upload them below instead."
                )

            _uploads = st.file_uploader(
                "…or upload member CSVs",
                type=["csv"],
                accept_multiple_files=True,
                key="crps_upload",
            )

            if st.button("Compute CRPS", key="crps_go"):
                import pandas as _cpd
                import evaluation as _ev
                _frames = []
                for _p in _picked_paths:
                    try:
                        _frames.append(
                            _cpd.read_csv(_p, parse_dates=True, index_col=0)
                        )
                    except Exception:
                        pass
                for _u in (_uploads or []):
                    try:
                        _frames.append(
                            _cpd.read_csv(_u, parse_dates=True, index_col=0)
                        )
                    except Exception:
                        pass

                if len(_frames) < 2:
                    st.warning(
                        "CRPS needs **at least 2** ensemble-member "
                        "exports. Select or upload more."
                    )
                else:
                    _res = _ev.crps_from_member_frames(_frames)
                    if _res["n_members"] < 2 or _res["n_times"] == 0:
                        st.error(
                            "Could not align the selected files into an "
                            "ensemble (need a shared `model`/`observed` "
                            "schema and overlapping timestamps)."
                        )
                    else:
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Mean CRPS", f"{_res['mean_crps']:.3f}")
                        c2.metric("Members", _res["n_members"])
                        c3.metric("Timesteps", _res["n_times"])
                        st.caption(
                            "CRPS is in the model's native units (stage in "
                            "ft or m, flow in cfs or m³/s). Compare against the "
                            "single-member MAE above - a well-calibrated "
                            "ensemble should score **lower** than its "
                            "best individual member."
                        )

        _tab_nav(5)


# ── TAB 7: Agent - real-time decision-support rules + email alerts ──
import agent as _agent  # noqa: E402  (kept at module level after tabs)

with tab_agent:
    st.subheader("Agent - Real-time Decision Support")
    st.caption(
        "Define **rules** that watch each completed simulation and send "
        "an email alert when a condition transitions from below → above "
        "its threshold. Designed for an unattended server running a "
        "real-time auto-schedule: configure once, deploy, the agent "
        "keeps an eye on every loop iteration and pages the right "
        "person the moment something fires."
    )

    # ── SMTP status + in-UI configuration ────────────────────────────
    _smtp = _agent.smtp_status()
    _smtp_cfg = _agent.load_smtp_config()
    with st.container(border=True):
        st.markdown("##### Email channel - SMTP")
        if _smtp["configured"]:
            st.success(
                f"SMTP configured ({_smtp['source']}) · "
                f"host=`{_smtp['host']}:{_smtp['port']}` · "
                f"from=`{_smtp['from']}`"
                + (
                    f" · default to=`{_smtp['to']}`"
                    if _smtp["to"] else " · (no default recipient)"
                )
            )
        else:
            st.warning(
                "SMTP is not configured yet - fill in the form "
                "below (or pass `SMTP_*` env vars at `docker run` "
                "time) so the agent can send email alerts."
            )

        with st.expander(
            "Configure SMTP", expanded=not _smtp["configured"]
        ):
            if DEMO_MODE:
                st.info(
                    "In this shared demo, email is **preconfigured by the "
                    "operator** - alerts are sent from a fixed sender. You "
                    "cannot change the sender here; just set a destination "
                    "in the **Send test email** box below (or on an alert "
                    "rule) to receive mail."
                )
            st.caption(
                "Values are saved on the persistent **HEC-RAS-Outputs** "
                "volume (`.hecinbox/agent_smtp.json`), so they now "
                "survive container rebuilds.  Keep the **password** safe "
                "- for Gmail, use an **app password** (Google Account → "
                "Security → 2-Step Verification → App passwords) and "
                "**not** your normal account password.  Spaces in the "
                "app password are stripped automatically.  Disk values "
                "override any `SMTP_*` env vars."
            )
            _f1, _f2 = st.columns(2)
            with _f1:
                _cf_host = st.text_input(
                    "Host",
                    value=_smtp_cfg.get("host", ""),
                    placeholder="smtp.gmail.com",
                    key="agent_smtp_host",
                )
                _cf_user = st.text_input(
                    "Username",
                    value=_smtp_cfg.get("user", ""),
                    placeholder="alerts@example.com",
                    key="agent_smtp_user",
                )
                _cf_from = st.text_input(
                    "From address",
                    value=_smtp_cfg.get("from", ""),
                    placeholder="alerts@example.com",
                    key="agent_smtp_from",
                )
            with _f2:
                _cf_port = st.text_input(
                    "Port (587 = STARTTLS, 465 = SSL)",
                    value=str(_smtp_cfg.get("port", "587")),
                    key="agent_smtp_port",
                )
                _cf_pass = st.text_input(
                    "Password / app password",
                    # Never pre-fill (and never reveal) the sender
                    # password in the shared multi-user demo.
                    value="" if DEMO_MODE else _smtp_cfg.get("password", ""),
                    type="password",
                    key="agent_smtp_pass",
                    disabled=DEMO_MODE,
                )
                _cf_to = st.text_input(
                    "Default 'To' (comma-separated)",
                    value=_smtp_cfg.get("to", ""),
                    placeholder="engineer@example.com",
                    key="agent_smtp_to",
                )

            # ── Inline validation - catch the most common Gmail mistakes
            #    *before* a rule silently fails at 2 a.m. (v3.1.4). ──
            _v_host = (_cf_host or "").strip().lower()
            _v_user = (_cf_user or "").strip()
            _v_from = (_cf_from or "").strip()
            _is_gmail = "gmail.com" in _v_host or "googlemail" in _v_host
            _smtp_warns: list[str] = []
            if _is_gmail and _v_user and "@" not in _v_user:
                _smtp_warns.append(
                    "**Username** must be your **full Gmail address** "
                    "(e.g. `you@gmail.com`) - Gmail SMTP rejects a bare "
                    "account name like `ehsan2`."
                )
            if (
                _is_gmail and _v_user and _v_from
                and "@" in _v_user and _v_user.lower() != _v_from.lower()
            ):
                _smtp_warns.append(
                    "For Gmail the **From address** must match the "
                    "**Username** (the authenticated account), or the "
                    "send is rejected."
                )
            if not (_cf_to or "").strip():
                _smtp_warns.append(
                    "**Default 'To'** is empty.  A rule with a blank "
                    "recipient has nowhere to send - set a default here "
                    "(then **Save**) or fill *Recipient(s)* on the rule."
                )
            if _smtp_warns:
                st.warning("\n\n".join("" + w for w in _smtp_warns))

            _sb1, _sb2 = st.columns([1, 1])
            with _sb1:
                if st.button(
                    "Save SMTP config",
                    key="agent_smtp_save",
                    type="primary",
                    width="stretch",
                    disabled=DEMO_MODE,
                ):
                    _agent.save_smtp_config({
                        "host": _cf_host,
                        "port": _cf_port,
                        "user": _cf_user,
                        "password": _cf_pass,
                        "from": _cf_from,
                        "to": _cf_to,
                    })
                    st.success(
                        "SMTP config saved. Use **Send test email** "
                        "below to verify."
                    )
                    st.rerun()
            with _sb2:
                if st.button(
                    "Clear stored credentials",
                    key="agent_smtp_clear",
                    width="stretch",
                    disabled=DEMO_MODE,
                ):
                    _agent.clear_smtp_config()
                    st.success(
                        "Stored credentials cleared. The agent will "
                        "fall back to `SMTP_*` env vars if any are "
                        "set."
                    )
                    st.rerun()

        _t1, _t2 = st.columns([3, 1])
        with _t1:
            _test_to = st.text_input(
                "Send test email to (override)",
                placeholder=(
                    "enter your email address to receive a test alert"
                    if DEMO_MODE
                    else "leave blank to use the default 'To'"
                ),
                key="agent_test_to",
                label_visibility="collapsed",
            )
        with _t2:
            if st.button(
                "Send test email", key="agent_test_send",
                width="stretch",
                disabled=not _smtp["configured"],
            ):
                if DEMO_MODE and not _test_to.strip():
                    # No default recipient in the demo - the visitor must
                    # supply their own address.
                    st.error(
                        "Enter your email address above so the test "
                        "alert has somewhere to go."
                    )
                else:
                    _ok, _info = _agent.send_email(
                        "[HECinBOX] Test alert",
                        "This is a test email from the HECinBOX agent.\n"
                        "If you received it, SMTP is wired up correctly.\n",
                        to=_test_to.strip() or None,
                    )
                    if _ok:
                        st.success(_info)
                    else:
                        st.error(_info)

    # ── Existing rules ────────────────────────────────────────────────
    st.markdown("##### Active rules")
    _state = _agent.load_state()
    _rules = _state.get("rules") or []

    if not _rules:
        st.info(
            "No rules yet. Use the **Create new rule** form below to "
            "add one. Typical first rule: *cell wetted* on a critical "
            "cross-section, or *domain peak depth* exceeding a flood "
            "stage."
        )
    else:
        for _r in _rules:
            _rid = _r.get("id")
            with st.container(border=True):
                _hc1, _hc2, _hc3, _hc4 = st.columns([4, 2, 1, 1])
                with _hc1:
                    _en_emoji = "" if _r.get("enabled", True) else ""
                    st.markdown(
                        f"{_en_emoji} **{_r.get('name', '(unnamed)')}**  \n"
                        f"<span style='opacity:0.75;font-size:0.88rem;'>"
                        f"{_agent.describe_rule(_r)}</span>",
                        unsafe_allow_html=True,
                    )
                with _hc2:
                    _fired = _r.get("last_fired_at")
                    _state_label = _r.get("last_state", "below")
                    _mode_label = _r.get("fire_mode", "transition")
                    _mode_short = (
                        "every cycle" if _mode_label == "every_cycle"
                        else "transition"
                    )
                    st.markdown(
                        f"<span style='opacity:0.75;font-size:0.88rem;'>"
                        f"Fires: <b>{_r.get('fire_count', 0)}</b><br>"
                        f"Last: {_fired[:19] + 'Z' if _fired else '-'}<br>"
                        f"State: <code>{_state_label}</code> · "
                        f"Mode: <code>{_mode_short}</code></span>",
                        unsafe_allow_html=True,
                    )
                    _mode_idx = 1 if _mode_label == "every_cycle" else 0
                    _new_mode = st.selectbox(
                        "Fire mode",
                        options=["transition", "every_cycle"],
                        index=_mode_idx,
                        format_func=lambda m: (
                            "Once per transition" if m == "transition"
                            else "Every cycle while true"
                        ),
                        key=f"agent_mode_{_rid}",
                        label_visibility="collapsed",
                    )
                    if _new_mode != _mode_label:
                        _r2 = dict(_r)
                        _r2["fire_mode"] = _new_mode
                        _agent.upsert_rule(_r2)
                        st.rerun()
                with _hc3:
                    _new_en = st.toggle(
                        "On", value=bool(_r.get("enabled", True)),
                        key=f"agent_en_{_rid}",
                        label_visibility="collapsed",
                    )
                    if _new_en != bool(_r.get("enabled", True)):
                        _agent.set_enabled(_rid, _new_en)
                        st.rerun()
                with _hc4:
                    if st.button(
                        "Delete", key=f"agent_del_{_rid}",
                        help="Delete this rule",
                    ):
                        _agent.delete_rule(_rid)
                        st.rerun()

    # ── Create-new-rule form ─────────────────────────────────────────
    st.markdown("##### Create new rule")
    with st.container(border=True):
        _rname = st.text_input(
            "Rule name",
            placeholder="e.g. 'Cell 1234 wetted' or 'Domain peak > 5'",
            key="agent_new_name",
        )
        _rtype = st.selectbox(
            "Rule type",
            options=[
                ("cell", "Cell threshold - watch one specific cell"),
                ("domain_peak", "Domain peak - watch any cell"),
                ("wetted_area", "Wetted area - total wet cells"),
            ],
            format_func=lambda x: x[1],
            key="agent_new_type",
        )
        _rtype_id = _rtype[0]

        _au = unit_labels(is_si((scan or {}).get("unit_system")))
        _params: dict = {}
        if _rtype_id in ("cell", "domain_peak"):
            _vc, _oc, _tc = st.columns([1.4, 1, 1.4])
            with _vc:
                _var = st.selectbox(
                    "Variable",
                    options=["depth", "wse", "velocity"],
                    format_func=lambda v: {
                        "depth": f"Depth ({_au['length']})",
                        "wse": f"WSE ({_au['length']})",
                        "velocity": f"Velocity ({_au['velocity']})",
                    }[v],
                    key=f"agent_new_var_{_rtype_id}",
                )
            with _oc:
                _op = st.selectbox(
                    "Operator",
                    options=[">", ">=", "<", "<="],
                    key=f"agent_new_op_{_rtype_id}",
                )
            with _tc:
                _thr = st.number_input(
                    "Threshold",
                    value=1.0, step=0.1, format="%.3f",
                    key=f"agent_new_thr_{_rtype_id}",
                )
            _params = {
                "variable": _var, "operator": _op, "threshold": float(_thr)
            }
            if _rtype_id == "cell":
                _cid = st.number_input(
                    "Cell ID  (hover the inundation map in Tab 5 to find it)",
                    min_value=0, value=0, step=1,
                    key="agent_new_cell_id",
                )
                _params["cell_id"] = int(_cid)
                st.caption(
                    "Tip: a 'cell wetted' rule = variable **depth**, "
                    f"operator **>**, threshold **0.05** {_au['length']}."
                )
        elif _rtype_id == "wetted_area":
            _mw = st.number_input(
                "Trigger when wet-cell count ≥",
                min_value=1, value=100, step=10,
                key="agent_new_minwet",
            )
            _params = {"min_wet_cells": int(_mw)}

        _recipients = st.text_input(
            "Recipient(s) - override SMTP_TO (comma-separated)",
            placeholder="leave blank to use the SMTP_TO env var",
            key="agent_new_recipients",
        )

        _fire_mode_opt = st.radio(
            "Fire mode",
            options=["transition", "every_cycle"],
            format_func=lambda m: {
                "transition": (
                    "Once per below → above transition "
                    "(re-arms when condition drops below)"
                ),
                "every_cycle": (
                    "Every scheduled cycle while condition is true "
                    "(heartbeat - emails every iteration)"
                ),
            }[m],
            index=0,
            key="agent_new_fire_mode",
            horizontal=False,
        )

        if st.button(
            "Save rule", type="primary",
            key="agent_save_rule",
            disabled=not _rname.strip(),
        ):
            _agent.upsert_rule({
                "id": _agent.new_rule_id(),
                "name": _rname.strip(),
                "type": _rtype_id,
                "enabled": True,
                "params": _params,
                "recipients": _recipients.strip(),
                "fire_mode": _fire_mode_opt,
            })
            st.success(f"Rule **{_rname.strip()}** saved.")
            st.rerun()

    # ── Recent alert history ─────────────────────────────────────────
    _hist = _agent.load_history()[-50:]
    _ah_head, _ah_btn = st.columns([6, 1])
    with _ah_head:
        st.markdown("##### Recent alerts")
    with _ah_btn:
        if _hist and st.button(
            "Clear", key="clear_alerts", width="stretch",
            help="Remove all recorded alert history. Your rules and "
                 "email settings are kept.",
        ):
            _agent.clear_history()
            st.rerun()
    if not _hist:
        st.caption(
            "No alerts have fired yet. The agent will record every "
            "below → above transition here, along with whether the "
            "email was delivered."
        )
    else:
        import pandas as _pd_agent
        _rows = []
        for _e in reversed(_hist):
            if "error" in _e:
                _rows.append({
                    "When (UTC)": (_e.get("at") or "")[:19],
                    "Rule": "-",
                    "Email": "-",
                    "Detail": _e.get("error", ""),
                })
            else:
                # On a delivery failure the trigger summary is useless -
                # the actionable text is *why* the send failed, so prefer
                # email_info there.  Keep both so the operator can see the
                # threshold value AND the SMTP error (v3.1.1).
                _ok = _e.get("email_ok")
                _summary = _e.get("summary") or ""
                _info = _e.get("email_info") or ""
                if _ok:
                    _detail = _summary or _info
                else:
                    _detail = " - ".join(p for p in (_info, _summary) if p)
                _rows.append({
                    "When (UTC)": (_e.get("at") or "")[:19],
                    "Rule": _e.get("rule_name", "(unnamed)"),
                    "Email": "sent" if _ok else "failed",
                    "Detail": _detail,
                })
        st.dataframe(
            _pd_agent.DataFrame(_rows),
            hide_index=True, width="stretch",
        )

    _tab_nav(6)


# ── TAB 8: Live (read-only dashboard for shared URLs) ────────────────



with tab_live:
    st.subheader("Live Dashboard")

    _sched_now = _read_schedule_state() or {}
    _sched_is_on = bool(_sched_now.get("enabled"))

    # ── Share button + caption ────────────────────────────────────
    _share_col1, _share_col2 = st.columns([3, 1])
    with _share_col1:
        if _LIVE_MODE:
            st.success(
                "**View-only mode** - controls are hidden. Anyone "
                "with this URL sees the latest results in real time."
            )
        else:
            st.caption(
                "This tab is a read-only dashboard intended for "
                "sharing with people who should *see* the live "
                "schedule but not change it. Add `?live=1` to the URL "
                "to enter view-only mode (other tabs hidden)."
            )
    with _share_col2:
        if not _LIVE_MODE:
            # Streamlit's markdown sanitiser strips `onclick=` event
            # handlers for security, so we render an iframe via
            # components.v1.html which is *not* sanitised.  The iframe
            # has full access to clipboard + reads its parent's URL.
            import streamlit.components.v1 as _components
            _components.html(
                """
<button
  onclick="const u = window.parent.location.origin + window.parent.location.pathname + '?live=1'; navigator.clipboard.writeText(u); this.textContent='Copied - paste & share'; setTimeout(()=>this.textContent='Copy view-only URL', 1800);"
  style="width:100%;padding:9px 12px;border-radius:8px;border:1px solid #306496;background:#3c78af;color:white;cursor:pointer;font-weight:600;font-family:system-ui,-apple-system,sans-serif;font-size:14px;">
  Copy view-only URL
</button>
                """,
                height=50,
            )

    if not _sched_is_on:
        st.info(
            "**Auto-schedule isn't running.**\n\n"
            "Start a schedule in **Tab 4 · Run** to enable this view. "
            "Once it's running, this dashboard will show the live "
            "cumulative water-depth plot and the most recent "
            "iteration's depth inundation map, auto-refreshing every "
            "15 seconds."
        )
    else:
        # Respect the per-user "Enable Live dashboard" toggle on
        # Tab 4 · Run.  Default is True so the dashboard works out
        # of the box.
        _live_on = st.session_state.get(
            "enable_live_dashboard", True
        )

        if _live_on:
            # Poll the history-file mtime every ~3 s inside a
            # fragment.  When the daemon finishes a new iteration
            # the history file is rewritten and mtime jumps; we
            # detect that and re-render.  Between cycles the
            # fragment still re-runs (Streamlit's design), but
            # NPZ loads are fast since the OS page cache is hot,
            # and there's no full page rerun -> the user stays
            # on Tab 8.
            @st.fragment(run_every="3s")
            def _live_dashboard_fragment() -> None:
                # The fragment body re-runs every 3 s, but we only
                # actually re-render the panels when the auto-
                # scheduler history file's mtime has *changed* -
                # i.e. when a new iteration has finished.  Between
                # cycles we still need to emit content (Streamlit
                # fragments replace their output on each rerun and
                # would otherwise clear the panels), so we always
                # call `_render_live_panels()`; the loads are cheap
                # (OS page cache stays hot) and the user perceives
                # no flicker because the rendered content is
                # identical until a new iteration arrives.
                try:
                    _mtime = (
                        HISTORY_FILE.stat().st_mtime
                        if HISTORY_FILE.exists() else 0.0
                    )
                except OSError:
                    _mtime = 0.0
                _last_seen = st.session_state.get(
                    "_live_panels_last_mtime", -1.0
                )
                _is_new_cycle = _mtime != _last_seen
                if _is_new_cycle:
                    st.session_state["_live_panels_last_mtime"] = _mtime
                _render_live_panels()
                if _is_new_cycle and _last_seen != -1.0:
                    # Subtle toast so the viewer knows a fresh
                    # iteration just landed.
                    st.toast(
                        "New iteration loaded into the Live "
                        "dashboard",
                    )

            _live_dashboard_fragment()
        else:
            st.caption(
                "Live dashboard auto-refresh is **off** "
                "(toggle in Tab 4 · Run). Reload the page manually "
                "to see new cycles."
            )
            _render_live_panels()

    # Stop processing rest of tab here (kept for live-mode CSS injection
    # block which runs after the with-tab_live: scope).


with tab_manual:
    st.subheader("User Manual")
    st.markdown(MANUAL_MD)


# ── Live-mode CSS (hide tabs 1-7 + header when ?live=1) ──────────────
# Streamlit renders tab content for all tabs; we leverage that and just
# hide the tab BUTTONS (except #8) and the inactive tab PANELS via CSS.
# The Live tab itself is the 8th and last `tabpanel`, so :nth-of-type(8)
# is the one we want to keep visible.
if _LIVE_MODE:
    st.markdown(
        """
        <style>
        /* Hide page header (logo, version chip, About button) */
        header[data-testid="stHeader"] { display: none !important; }
        /* Hide the entire top header section / logo */
        div[data-testid="stHeadingWithActionElements"]:first-of-type,
        div[data-testid="stHorizontalBlock"]:first-of-type {
            display: none !important;
        }
        /* Hide all tab buttons except Tab 8 · Live (now that a 9th
           User-Manual tab follows it, target the 8th, not :last-child) */
        button[role="tab"]:not(:nth-of-type(8)) {
            display: none !important;
        }
        /* Hide non-Live tab panels */
        div[role="tabpanel"]:not(:nth-of-type(8)) {
            display: none !important;
        }
        /* Hide Streamlit's hamburger menu + footer */
        #MainMenu { visibility: hidden !important; }
        footer { visibility: hidden !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# Persist user preferences at the end of every render so that
# threads, dates, BCs, theme, cloud URIs, etc. survive a closed tab,
# a Streamlit restart, or a container restart.
_save_user_prefs()
