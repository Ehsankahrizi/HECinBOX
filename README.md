# HECinBOX

**An automated, agent-based flood early-warning framework built on 2D unsteady HEC-RAS simulations.**

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-ehsankahrizi1991%2Fhecinbox-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/ehsankahrizi1991/hecinbox)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22647563.svg)](https://doi.org/10.5281/zenodo.22647563)

HECinBOX turns an existing, calibrated 2D unsteady HEC-RAS model into a
continuously operating flood-warning system. It retrieves discharge and stage
data from USGS and NOAA, applies rainfall directly to the computational mesh,
writes the data into the model through the DSS-7 pathway the solver actually
reads, runs the headless HEC-RAS Linux engine, extracts and maps the results,
validates them against observed gauges, and emails alerts when user-defined
thresholds are crossed. No manual file editing, and no Windows desktop.

**Live demo:** <https://demo.hecinbox.com> · **Project site:** <https://hecinbox.com>

---

## Quick start

You need Docker Desktop and a 2D unsteady HEC-RAS model that has already been
computed once in the HEC-RAS desktop application.

```bash
docker run --rm -p 8501:8501 \
  -v /path/to/your/models:/host \
  -v /path/to/your/outputs:/host_out \
  ehsankahrizi1991/hecinbox:latest
```

Then open <http://localhost:8501>.

Full run instructions for macOS, Linux and Windows, the complete feature tour,
the cloud (S3) setup and the alert-agent configuration are in
[DOCKERHUB_README.md](DOCKERHUB_README.md).

## What it does

| Stage | What happens |
| --- | --- |
| Model scanning | Discovers the project file, active plan, geometry, unsteady file, simulation window and every boundary-condition line |
| Data ingestion | USGS discharge/stage, NOAA CO-OPS water level, NWM and STOFS forecasts, AORC and HRRR gridded precipitation |
| BC orchestration | Writes fresh DSS-7 records and re-points the unsteady file at them, so the solver cannot silently reuse calibration-period data |
| Execution | Runs RasGeomPreprocess / RasUnsteady headless, with live progress |
| Results | Water-surface elevation, depth, velocity and terrain as 2D and 3D maps, animations, and per-cell time series |
| Validation | Pairs the model against an observed gauge and reports NSE, KGE, RMSE, MAE, bias and Pearson r with bootstrap confidence intervals |
| Warning agent | Evaluates threshold rules after each run, keeps state across cycles, and sends email alerts |
| Dissemination | A scheduler daemon for unattended operation and a read-only live dashboard |

## Requirements and limitations

- **2D unsteady models only.** 1D and steady-flow models are not supported.
- The model must already be **built, meshed and calibrated** in HEC-RAS, and
  computed at least once so the plan HDF exists.
- Live data retrieval is **US-only**: USGS and NOAA gauges, and the gridded
  products cover CONUS (AORC also covers AK and PR). Elsewhere, use constant
  values, *leave unchanged*, or a DSS upload.
- AORC lags real time by roughly 10 days; HRRR covers roughly the next 18-48 h.

## A note on time zones

HEC-RAS stores simulation dates as a bare clock reading and records no time
zone, while USGS, NOAA, NWM and STOFS all publish in UTC. HECinBOX therefore
asks you once, explicitly, which clock your model was built on, and refuses to
run until you answer. Results, validation plots and alerts are then shown in
the model site's local standard time with UTC alongside. Getting this wrong
does not raise an error, it just shifts every fetched series by whole hours, so
the question is asked rather than guessed.

## What is not in this repository

The **HEC-RAS Linux compute engine** is distributed by the U.S. Army Corps of
Engineers under its own terms and is not redistributed here. The published
Docker image bundles it so the container works out of the box. To build the
image yourself, obtain the engine from USACE and place its binaries and shared
libraries in `hecras_engine/`.

Sample models, simulation outputs and manuscript material are also excluded;
this repository is the source code.

## Repository layout

```
src/                  Python application
  app.py              Streamlit UI (9 tabs)
  main.py             pipeline: run() and validate()
  model_scanner.py    HEC-RAS project discovery
  usgs_client.py      observed discharge / stage
  noaa_client.py      CO-OPS water level
  forecast_client.py  NWM, STOFS, USGS rating curves
  precip_gridded.py   AORC and HRRR rain on mesh
  dss_writer.py       DSS-7 boundary records
  run_hecras.py       headless engine driver
  job_runner.py       detached job process + status files
  results_extractor.py, raster_render.py, visualization.py
  evaluation.py       KGE, bootstrap CIs, CRPS
  timebase.py         the model clock and how times are displayed
  agent.py            stateful warning agent
  auto_scheduler.py   scheduling daemon
  cloud_storage.py    S3 / S3-compatible storage
config/               default settings and docker-compose
.streamlit/           Streamlit runtime configuration
assets/               branding and workflow icons
Dockerfile            builds the published image
```

## Citing HECinBOX

If you use HECinBOX in published work, please cite the software release and the
accompanying paper (in preparation):

**Software release (this version, v4.8.0):**

> Kahrizi, E., & Moftakhari, H. (2026). *HECinBOX: an automated agent-based
> flood warning framework using 2D unsteady HEC-RAS simulation* (v4.8.0)
> [Software]. Zenodo. https://doi.org/10.5281/zenodo.22647563

To cite whichever version is current rather than this one, use the concept DOI
<https://doi.org/10.5281/zenodo.22647562>, which always resolves to the latest
release.

## Contact

Ehsan Kahrizi — <ekahrizi@crimson.ua.edu>
[Coastal Hydrology Lab](https://sites.ua.edu/hmoftakhari/), Department of Civil,
Construction and Environmental Engineering, University of Alabama.

## License

MIT, see [LICENSE](LICENSE). The licence covers the HECinBOX source code
only, not the HEC-RAS engine, which is covered by [NOTICE](NOTICE).
