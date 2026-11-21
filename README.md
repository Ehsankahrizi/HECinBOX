# HECinBOX

**Turn an existing 2D HEC-RAS model into an automated flood forecasting and warning system.**

[![Version](https://img.shields.io/badge/version-v4.9.1-2ea44f)](DOCKERHUB_README.md#changelog)
[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-ehsankahrizi1991%2Fhecinbox-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/ehsankahrizi1991/hecinbox)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![DOI (v4.9.1)](https://img.shields.io/badge/DOI%20(v4.9.1)-10.5281%2Fzenodo.24407375-1682D4)](https://doi.org/10.5281/zenodo.24407375)
[![DOI (all versions)](https://img.shields.io/badge/DOI%20(all%20versions)-10.5281%2Fzenodo.22647562-1682D4)](https://doi.org/10.5281/zenodo.22647562)

You bring a 2D unsteady HEC-RAS model. HECinBOX does the rest:

1. downloads the latest gauge data and forecasts,
2. puts them into your model,
3. runs HEC-RAS (no Windows needed),
4. makes flood maps and checks the results against gauges,
5. emails you when a flood threshold is crossed.

No manual file editing. Everything runs in one Docker container.

**Try it online:** <https://demo.hecinbox.com> (click *Start the demo* and wait about 3 minutes)

**Project site:** <https://hecinbox.com>

---

## Quick start

**You need:**

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- a 2D unsteady HEC-RAS model that has been run once in HEC-RAS on Windows

**Run:**

```bash
docker run --rm -p 8501:8501 \
  -v /path/to/your/models:/host \
  -v /path/to/your/outputs:/host_out \
  ehsankahrizi1991/hecinbox:latest
```

**Open:** <http://localhost:8501>

To stay on this exact version, use `ehsankahrizi1991/hecinbox:v4.9.1`
instead of `latest`.

More detail (Windows, macOS and Linux setup, cloud storage, alerts, and the
full changelog) is in [DOCKERHUB_README.md](DOCKERHUB_README.md).

## What it does

| Step | What HECinBOX does |
| --- | --- |
| Read the model | Finds the plan, geometry, flow file, simulation dates and every boundary |
| Get data | USGS flow and stage, NOAA water levels, National Water Model and STOFS forecasts, and AORC or HRRR rainfall |
| Replay old forecasts | Reruns the model with National Water Model forecasts issued on any past date since 2025 |
| Correct forecasts | Optional bias correction of National Water Model flow (monthly, quantile mapping, or a manual factor) |
| Update the model | Writes the new data into the model so HEC-RAS cannot reuse old data by mistake |
| Run | Runs HEC-RAS on Linux and shows live progress |
| Show results | Depth, water level and velocity maps (2D and 3D), animations, and time series |
| Check accuracy | Compares the model with a gauge (NSE, KGE, RMSE and more) and saves 400 dpi figures |
| Warn | Checks your threshold rules after each run and sends email alerts |
| Run on a schedule | Runs by itself on a timer, with a read-only dashboard |

## Before you start

- **2D unsteady models only.** 1D and steady-flow models do not work.
- **The model must be ready.** It must be built, meshed and run once in
  HEC-RAS, so that the plan HDF file exists.
- **Live data is US only.** Gauges are USGS and NOAA; gridded data covers the
  contiguous US. Outside the US, use constant values, keep the model's own
  data, or upload a DSS file.
- **Data delays.** AORC rainfall is about 10 days behind real time. HRRR
  forecasts cover about the next 18 to 48 hours.

## Check your model's time zone

HEC-RAS does not store a time zone, but all US data services use UTC. So
HECinBOX asks you once which clock your model uses (UTC or local time) and
will not run until you answer. A wrong answer gives no error; it just shifts
all the data by a few hours.

## HEC-RAS engine

The HEC-RAS Linux engine belongs to the U.S. Army Corps of Engineers and is
not included in this repository. The Docker image includes it, so the
container works out of the box. See [NOTICE](NOTICE).

## How to cite

For a paper, cite the exact version you used:

> Kahrizi, E., & Moftakhari, H. (2026). *HECinBOX: an automated agent-based
> flood warning framework using 2D unsteady HEC-RAS simulation* (v4.9.1)
> [Software]. Zenodo. https://doi.org/10.5281/zenodo.24407375

To refer to the software in general (always points to the newest version),
use https://doi.org/10.5281/zenodo.22647562.

A paper describing HECinBOX is in preparation.

## Contact

Ehsan Kahrizi, <ekahrizi@crimson.ua.edu>  
[Coastal Hydrology Lab](https://sites.ua.edu/hmoftakhari/), University of Alabama

## License

MIT, see [LICENSE](LICENSE). This covers the HECinBOX code only, not the
HEC-RAS engine.
