"""Independent auto-scheduler daemon for HECinBOX.

Why this exists
---------------
The Streamlit web UI cannot, by itself, keep a real-time forecasting
loop alive for days or months - its session state lives in the browser
and dies the moment the tab closes, the websocket drops, or the
Streamlit server restarts.  For a true unattended server workflow we
need a *separate long-lived process* that owns the schedule.  That is
this daemon.

How it works
------------
The Streamlit UI writes a tiny JSON file (``/app/.autoschedule.json``)
when the user enables auto-scheduling.  The daemon polls that file
forever, and whenever ``now >= next_run_at`` and ``enabled`` is True
it:

  1. Recomputes the simulation window so realtime runs stay current,
  2. Writes a fresh ``settings.yml`` into a per-run output folder,
  3. Launches the HEC-RAS pipeline via :mod:`job_runner` (so the
     existing Streamlit UI sees and can stop the run),
  4. Waits for it to finish (with a configurable max wait),
  5. Records the outcome in ``/app/.autoschedule.history.json``,
  6. Sets ``next_run_at = now + interval_minutes`` and loops.

Failures, crashes, transient network errors - none of them stop the
loop.  Only an explicit ``enabled: false`` (or container shutdown)
does.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# Make the rest of the application importable so the daemon can
# delegate run-launching to the existing job_runner.
sys.path.insert(0, "/app/src")
from job_runner import read_status, start_job  # noqa: E402


def _persist_dir() -> Path:
    """Directory for schedule state that must survive container recreation.

    Mirrors :func:`agent._persist_dir` - prefers the mounted ``/host_out``
    volume so the schedule (and its run history) is not lost on every
    rebuild, falling back to the ephemeral ``/app`` when no volume is
    mounted.  Both the daemon and the Streamlit UI compute this the same
    way so they always read/write the same files (v3.1.2).
    """
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
# The daemon log stays ephemeral (/app) - it's debug noise, not state.
LOG_FILE = Path(
    os.environ.get("AUTOSCHEDULE_LOG", "/app/.autoschedule.log")
)
ACTIVE_JOB_FILE = Path("/app/.active_job")
POLL_SECONDS = int(os.environ.get("AUTOSCHEDULE_POLL", "15"))
DEFAULT_MAX_RUN_SECONDS = int(
    os.environ.get("AUTOSCHEDULE_MAX_RUN_SECONDS", str(6 * 3600))
)


# ── utilities ────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    line = f"[{datetime.utcnow().isoformat()}Z] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr, flush=True)


def _read_schedule() -> dict | None:
    if not SCHEDULE_FILE.exists():
        return None
    try:
        return json.loads(SCHEDULE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_schedule(d: dict) -> None:
    try:
        SCHEDULE_FILE.write_text(json.dumps(d, indent=2, default=str))
    except OSError as exc:
        _log(f"Failed to write schedule: {exc}")


def _append_history(entry: dict) -> None:
    history: list = []
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text())
            if isinstance(data, list):
                history = data
        except (json.JSONDecodeError, OSError):
            pass
    history.append(entry)
    history = history[-500:]  # cap so the file does not grow forever
    try:
        HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))
    except OSError as exc:
        _log(f"Failed to write history: {exc}")


def _now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _parse_iso(s: str | None) -> datetime:
    if not s:
        return _now()
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return _now()
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ── one cycle, split in two halves so the schedule's "state=running"
# write happens *after* the subprocess + ``.active_job`` are in place. ──
def _start_one(sched: dict) -> tuple[str | None, Path]:
    """Set up + launch a HEC-RAS run.

    Computes the output directory, writes ``settings.yml``, spawns the
    pipeline subprocess (via :func:`job_runner.start_job` - which
    creates ``run.status`` with state=running) and writes
    ``/app/.active_job``.  Returns ``(None, output_dir)`` on success or
    ``("crashed", output_dir)`` on failure.

    Returning before the schedule is updated lets the Streamlit UI see
    a fully-consistent post-start state the very first time it polls -
    no "running text with no log window" gap.
    """
    template = dict(sched.get("settings_template") or {})
    rt_days = int(sched.get("realtime_days", 7))
    # Direction + span (v4.7.6).  A forecast schedule looks forward
    # (now .. now+span); a hindcast schedule looks backward
    # (now-span .. now).  Older schedule files carry neither key, so
    # default to a backward window of rt_days for backward compatibility.
    is_forecast = bool(sched.get("forecast", False))
    window_hours = int(sched.get("window_hours", rt_days * 24))
    now = _now()
    if is_forecast:
        start_dt = now
        end_dt = now + timedelta(hours=window_hours)
    else:
        start_dt = now - timedelta(hours=window_hours)
        end_dt = now
    start_str = start_dt.strftime("%Y-%m-%d %H:%M")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M")

    template.setdefault("simulation", {})
    template["simulation"]["start"] = start_str
    template["simulation"]["end"] = end_str

    output_parent = Path(
        sched.get("output_parent", "/app/data/outputs")
    )
    base = sched.get("base_name", "run")
    tag = f"{start_dt:%Y%m%d}_{end_dt:%Y%m%d}_{now:%H%M%S}"

    # ── Nested layout for scheduled sessions ────────────────────────
    # When the schedule was armed with v2.6.4+, ``schedule_root`` is
    # set - every iteration goes inside ``<output_parent>/<schedule_root>/``
    # as ``iter_<NNN>_<timestamp>`` so dozens of loops don't flood the
    # top of /host_out.  Pre-v2.6.4 schedules (no root) keep the old
    # flat layout for backward compatibility.
    n_completed = int(sched.get("runs_completed", 0))
    n_failed = int(sched.get("runs_failed", 0))
    iter_num = n_completed + n_failed + 1
    schedule_root = (sched.get("schedule_root") or "").strip()
    if schedule_root:
        parent = output_parent / schedule_root
        output_dir = parent / f"iter_{iter_num:03d}_{now:%Y%m%d_%H%M%S}"
    else:
        output_dir = output_parent / f"{base}_{tag}"
    output_dir.mkdir(parents=True, exist_ok=True)

    template.setdefault("hecras", {})
    template["hecras"]["output_dir"] = str(output_dir)

    cloud_uri = (sched.get("cloud_output_uri") or "").strip()
    if cloud_uri:
        # Cloud uploads mirror the same nesting so the bucket layout
        # matches the local filesystem.
        if schedule_root:
            cloud_subpath = f"{schedule_root}/{output_dir.name}"
        else:
            cloud_subpath = output_dir.name
        template["cloud"] = {
            "upload_results_to": (
                cloud_uri.rstrip("/") + "/" + cloud_subpath + "/"
            )
        }

    settings_path = output_dir / "settings.yml"
    with open(settings_path, "w") as f:
        yaml.dump(template, f, default_flow_style=False, sort_keys=False)
    _log(
        f"Run #{n_completed + n_failed + 1} launching → {output_dir} "
        f"(window {start_str} → {end_str})"
    )

    try:
        pid = start_job(
            output_dir=output_dir,
            settings_path=settings_path,
            python_exe=sys.executable,
            main_cwd="/app/src",
        )
    except Exception:
        _log(f"start_job failed:\n{traceback.format_exc()}")
        return "crashed", output_dir

    _log(f"PID {pid} started; .active_job written")
    try:
        ACTIVE_JOB_FILE.write_text(str(output_dir))
    except OSError:
        pass
    return None, output_dir


def _wait_one(output_dir: Path, max_wait: int) -> str:
    """Poll status.json until the run finishes.

    Returns the final state ∈ {"done", "failed", "timeout"}.
    """
    waited = 0
    while waited < max_wait:
        time.sleep(8)
        waited += 8
        try:
            s = read_status(output_dir)
        except Exception:
            continue
        if s.get("state") != "running":
            rc = int(s.get("returncode", 1))
            state = "done" if rc == 0 else "failed"
            _log(f"Run finished - state={state} rc={rc}")
            return state
    _log(
        f"Run exceeded max_run_seconds={max_wait}s - moving on; the "
        f"HEC-RAS process may still be alive."
    )
    return "timeout"


# ── main loop ────────────────────────────────────────────────────────
def main() -> int:
    _log("Auto-scheduler daemon started.")
    while True:
        try:
            sched = _read_schedule()
            if not sched or not sched.get("enabled"):
                time.sleep(POLL_SECONDS)
                continue

            try:
                next_at = _parse_iso(sched.get("next_run_at"))
            except Exception:
                next_at = _now()

            if _now() < next_at:
                time.sleep(POLL_SECONDS)
                continue

            interval = max(1, int(sched.get("interval_minutes", 60)))
            max_wait = int(
                sched.get("max_run_seconds", DEFAULT_MAX_RUN_SECONDS)
            )

            # ── Phase 1: spawn the subprocess + write .active_job ──
            # We do this BEFORE flipping the schedule to state="running"
            # so by the time Streamlit's watcher detects any change,
            # ``.active_job`` and ``run.status`` are already in place
            # and the UI can render the progress + log immediately.
            try:
                crashed, output_dir = _start_one(sched)
            except Exception:
                tb = traceback.format_exc()
                _log(f"_start_one crashed:\n{tb}")
                crashed, output_dir = "crashed", Path("")

            # ── Phase 2: update schedule state ──
            sched["state"] = "running"
            sched["last_started_at"] = _now().isoformat()
            sched["last_output_dir"] = str(output_dir)
            _write_schedule(sched)

            # ── Phase 3: wait for the run to finish (or crash) ──
            if crashed == "crashed":
                state = "crashed"
                _append_history({
                    "at": _now().isoformat(),
                    "state": "crashed",
                    "output_dir": str(output_dir),
                })
            else:
                try:
                    state = _wait_one(output_dir, max_wait)
                except Exception:
                    tb = traceback.format_exc()
                    _log(f"_wait_one crashed:\n{tb}")
                    state = "crashed"

            _append_history({
                "at": _now().isoformat(),
                "state": state,
                "output_dir": str(output_dir),
            })

            n_done = int(sched.get("runs_completed", 0)) + (
                1 if state == "done" else 0
            )
            n_failed = int(sched.get("runs_failed", 0)) + (
                0 if state == "done" else 1
            )

            # Clear ``.active_job`` only AFTER history and the run
            # counters are written - by the time Streamlit's watcher
            # sees the file disappear, every downstream piece of state
            # is already in place and Tab 5 refreshes without manual
            # browser action.
            try:
                ACTIVE_JOB_FILE.unlink(missing_ok=True)
            except (FileNotFoundError, OSError):
                pass

            # Re-read so any edits the user made mid-run survive.  The
            # daemon owns the run counters / state; the UI owns the
            # config (interval, settings_template, agent toggle, …).  We
            # apply the daemon-owned fields onto the freshly-read schedule
            # instead of writing back the stale in-memory ``sched`` - that
            # earlier clobbered every "Update schedule" the user clicked
            # while a run was in flight (v3.1.1).
            latest = _read_schedule() or {}
            if not latest.get("enabled"):
                _log("Schedule disabled mid-run - exiting loop.")
                continue

            # Honour an interval the user may have changed during the run.
            interval = max(1, int(latest.get("interval_minutes", interval)))

            latest["runs_completed"] = n_done
            latest["runs_failed"] = n_failed
            latest["last_state"] = state
            latest["last_output_dir"] = str(output_dir)
            latest["enabled"] = True
            latest["state"] = "idle"
            latest["last_completed_at"] = _now().isoformat()
            latest["next_run_at"] = (
                _now() + timedelta(minutes=interval)
            ).isoformat()
            _write_schedule(latest)
            # Keep the in-memory copy in sync for the next loop iteration.
            sched = latest
            _log(f"Next run scheduled at {latest['next_run_at']}.")

        except Exception:
            _log(f"Scheduler loop error (continuing):\n{traceback.format_exc()}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
