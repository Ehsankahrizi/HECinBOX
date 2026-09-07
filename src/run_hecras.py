"""Run the HEC-RAS Linux headless compute engine via subprocess.

The USACE Linux engine ships three binaries - RasGeomPreprocess, RasUnsteady,
and RasSteady.  Unlike the Windows COM API, the Linux binaries are invoked
directly on a *temporary* copy of the plan HDF:

    1.  cp  Project.p01.hdf  →  Project.p01.tmp.hdf
    2.  RasGeomPreprocess  Project.p01.tmp.hdf          (optional)
    3.  RasUnsteady        Project.p01.tmp.hdf  b01
    4.  mv  Project.p01.tmp.hdf  →  Project.p01.hdf    (harvest results)

The second argument to RasUnsteady is the boundary-condition file suffix that
the plan references (plan p01 → boundary b01, plan p02 → boundary b02, …).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import h5py


ENGINE_DIR = Path(os.environ.get("HECRAS_ENGINE_DIR", "/app/hecras_engine"))

# RasUnsteady (HEC-RAS 7.0+) emits `SIMTIME=<hours>` once per inner
# timestep, where the value is hours since the simulation start.  It can
# be negative during the warm-up phase (typically a few hours before
# sim_start).  Older versions also emitted ABSDATE / ABSTIME - kept as a
# fallback in case a future HEC-RAS build switches back.
_SIMTIME_RE = re.compile(r"SIMTIME=\s*(-?[0-9]+(?:\.[0-9]+)?)")
_ABSDATE_RE = re.compile(r"ABSDATE=\s*([0-9]{1,2}[A-Za-z]{3}[0-9]{4})")
_ABSTIME_RE = re.compile(r"ABSTIME=\s*([0-9]{1,2}:[0-9]{2}:[0-9]{2})")

# `LABEL=` markers that RasUnsteady prints during the long initialisation
# phase, *before* the first `SIMTIME=` line.  Mapping each known label
# to a sub-band % inside the engine's init range so the bar visibly
# moves through "reading data → mesh init → initial conditions" instead
# of sitting at the start of the engine band for minutes.  The fraction
# is relative to the engine band; absolute % is interpolated between
# pct_lo and the start of SIMTIME progress (pct_lo + ~10).
_LABEL_PHASES: list[tuple[str, float, str]] = [
    # (substring to match, fraction of init sub-band, human-readable msg)
    ("Reading Data",          0.10, "Reading model files"),
    ("Reading Boundary",      0.15, "Reading boundary conditions"),
    ("Reading 2D Area",       0.25, "Loading 2D mesh"),
    ("initializing 2D Area",  0.40, "Initializing 2D mesh topology"),
    ("Initializing",          0.20, "Initializing solver"),
    ("initial conditions",    0.70, "Computing initial conditions "
                                    "(steady-state - slow for large meshes)"),
    ("warm up",               0.90, "Running warm-up timesteps"),
]
_LABEL_RE = re.compile(r"LABEL=\s*(.+?)\s*$")


def _emit_progress(pct: int, msg: str) -> None:
    """Emit a PROGRESS marker the Streamlit UI parses (same as main._progress)."""
    print(f"PROGRESS|{pct}|{msg}", flush=True)


def _engine_env(num_threads: int | None = None) -> dict[str, str]:
    env = os.environ.copy()
    libs = f"{ENGINE_DIR}/libs:{ENGINE_DIR}/libs/mkl:{ENGINE_DIR}/libs/rhel_8"
    env["LD_LIBRARY_PATH"] = libs + ":" + env.get("LD_LIBRARY_PATH", "")
    if num_threads is not None and num_threads > 0:
        env["OMP_NUM_THREADS"] = str(num_threads)
        env["MKL_NUM_THREADS"] = str(num_threads)
        env["OMP_DYNAMIC"] = "FALSE"
        env["MKL_DYNAMIC"] = "FALSE"
    return env


def _run_binary(
    exe_name: str,
    args: list[str],
    cwd: Path,
    timeout_s: int,
    label: str,
    sim_start: datetime | None = None,
    sim_end: datetime | None = None,
    pct_lo: int = 55,
    pct_hi: int = 84,
    num_threads: int | None = None,
) -> None:
    exe = ENGINE_DIR / exe_name
    if not exe.exists():
        raise FileNotFoundError(f"HEC-RAS binary not found: {exe}")

    cmd = [str(exe), *args]
    print(f"[{label}] {' '.join(cmd)}", flush=True)
    print(f"[{label}] cwd = {cwd}", flush=True)
    if num_threads:
        print(f"[{label}] OMP/MKL threads = {num_threads}", flush=True)

    total_span = None
    total_hours = None
    if sim_start is not None and sim_end is not None:
        total_span = (sim_end - sim_start).total_seconds()
        if total_span <= 0:
            total_span = None
        else:
            total_hours = total_span / 3600.0

    t0 = time.time()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=_engine_env(num_threads=num_threads),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    tail: list[str] = []
    cur_date: str | None = None
    last_pct = -1
    last_emit = 0.0

    # Engine progress is split into two bands inside [pct_lo, pct_hi]:
    #   init_lo .. sim_lo  (LABEL=-driven, before first SIMTIME=)
    #   sim_lo  .. pct_hi  (SIMTIME=-driven, real simulation timestepping)
    # The init band is the *first ~14%* of the engine range, leaving the
    # bulk for actual timestepping.
    init_lo = pct_lo
    sim_lo = pct_lo + int((pct_hi - pct_lo) * 0.14)
    sim_started = False
    current_phase_msg = "Engine starting"

    # Reader thread: blocking I/O on proc.stdout, push every line into a
    # queue.  The main loop polls the queue with a short timeout so we
    # can fire heartbeat updates (elapsed timer) even when HEC-RAS goes
    # silent for minutes during initial-conditions solving.
    import queue
    import threading

    line_q: "queue.Queue[str | None]" = queue.Queue()

    def _reader() -> None:
        try:
            for raw_line in proc.stdout:
                line_q.put(raw_line.rstrip("\n"))
        finally:
            line_q.put(None)  # sentinel: stream closed

    reader_t = threading.Thread(target=_reader, daemon=True)
    reader_t.start()

    def _do_emit(pct: int, msg: str, force: bool = False) -> None:
        """Throttled progress emit (≥1s gap unless forced)."""
        nonlocal last_pct, last_emit
        now = time.time()
        if not force and pct == last_pct and (now - last_emit) < 1.0:
            return
        elapsed_s = now - t0
        elapsed_str = (
            f"elapsed {int(elapsed_s // 60):d}m"
            f"{int(elapsed_s % 60):02d}s"
        )
        _emit_progress(pct, f"{msg} · {elapsed_str}")
        last_pct = pct
        last_emit = now

    def _phase_pct_for(label_text: str) -> tuple[int, str] | None:
        """Map a `LABEL=` line to an init-band % and message."""
        low = label_text.lower()
        for substr, frac, human in _LABEL_PHASES:
            if substr.lower() in low:
                pct = int(init_lo + frac * (sim_lo - init_lo))
                return pct, human
        return None

    try:
        while True:
            try:
                # 2-second poll lets the heartbeat fire even when the
                # engine produces no output (typical during the long
                # initial-conditions solve for large meshes).
                line = line_q.get(timeout=2.0)
            except queue.Empty:
                # Heartbeat: re-emit the latest progress with a fresh
                # elapsed timer so the user can see the run is alive.
                now = time.time()
                if (now - last_emit) >= 5.0 and last_pct >= 0:
                    _do_emit(last_pct, current_phase_msg, force=True)
                if proc.poll() is not None:
                    # Process finished - drain whatever's left in the
                    # queue, then exit.
                    while True:
                        try:
                            line = line_q.get_nowait()
                        except queue.Empty:
                            break
                        if line is None:
                            break
                    break
                continue

            if line is None:
                break  # reader signalled EOF

            # Stream the line live so the UI log updates in real time.
            print(f"  [{label}] {line}", flush=True)
            tail.append(line)
            if len(tail) > 200:
                tail.pop(0)

            if total_span is None:
                continue

            # ── Preferred: SIMTIME=<hours since sim_start> ──────────
            m = _SIMTIME_RE.search(line)
            if m and total_hours:
                try:
                    sim_hours = float(m.group(1))
                except ValueError:
                    sim_hours = None
                if sim_hours is not None:
                    sim_started = True
                    # Warmup steps emit negative SIMTIME - clamp to 0
                    # so the bar starts at sim_lo, not below it.
                    frac = max(0.0, min(1.0, sim_hours / total_hours))
                    pct = int(sim_lo + frac * (pct_hi - sim_lo))
                    cur_dt = sim_start + timedelta(
                        hours=max(0.0, sim_hours)
                    )
                    current_phase_msg = (
                        f"HEC-RAS engine - {cur_dt:%d %b %Y %H:%M}  "
                        f"({frac * 100:.0f}% of window)"
                    )
                    _do_emit(pct, current_phase_msg)
                    continue

            # ── LABEL=-driven init progress (before SIMTIME starts) ─
            if not sim_started:
                m = _LABEL_RE.search(line)
                if m:
                    mapped = _phase_pct_for(m.group(1))
                    if mapped:
                        pct_init, human = mapped
                        current_phase_msg = (
                            f"HEC-RAS engine - {human}"
                        )
                        _do_emit(pct_init, current_phase_msg)
                        continue

            # ── Fallback: ABSDATE + ABSTIME (older HEC-RAS builds) ──
            m = _ABSDATE_RE.search(line)
            if m:
                cur_date = m.group(1)
            m = _ABSTIME_RE.search(line)
            if m and cur_date:
                try:
                    cur_dt = datetime.strptime(
                        f"{cur_date} {m.group(1)}", "%d%b%Y %H:%M:%S"
                    )
                except ValueError:
                    continue
                sim_started = True
                frac = (cur_dt - sim_start).total_seconds() / total_span
                frac = max(0.0, min(1.0, frac))
                pct = int(sim_lo + frac * (pct_hi - sim_lo))
                current_phase_msg = (
                    f"HEC-RAS engine - simulating "
                    f"{cur_dt:%d %b %Y %H:%M} ({frac * 100:.0f}% of window)"
                )
                _do_emit(pct, current_phase_msg)

        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(
            f"{exe_name} exceeded timeout of {timeout_s}s and was killed."
        )

    elapsed = time.time() - t0

    if proc.returncode != 0:
        raise RuntimeError(
            f"{exe_name} exited with code {proc.returncode} "
            f"after {elapsed:.0f}s.\n" + "\n".join(tail[-40:])
        )
    print(f"[{label}] completed in {elapsed:.1f}s (exit 0)", flush=True)


def run_hecras_linux(
    project_dir: Path,
    plan_hdf: Path,
    plan_suffix: str = "p01",
    exec_suffix: str = "x01",
    run_geom_preprocess: bool = False,
    timeout_s: int = 7200,
    sim_start: datetime | None = None,
    sim_end: datetime | None = None,
    num_threads: int | None = None,
) -> Path:
    """Execute the HEC-RAS Linux compute engine on *project_dir*.

    Parameters
    ----------
    project_dir:
        Directory containing the full HEC-RAS project (must be writable).
    plan_hdf:
        Path to the plan HDF, e.g. ``project_dir / "BraysBayou.p01.hdf"``.
    plan_suffix:
        Plan file suffix (``"p01"``, ``"p02"``, …).
    exec_suffix:
        Execution file suffix (``"x01"``, ``"x02"``, …).  The engine reads
        ``<project>.<exec_suffix>`` for array sizes, job control, etc.
    run_geom_preprocess:
        If True, run ``RasGeomPreprocess`` before the solver.
    timeout_s:
        Hard wall-clock timeout (seconds) for each subprocess call.

    Returns
    -------
    Path to the results HDF (same as *plan_hdf*, now containing output).
    """
    if not plan_hdf.exists():
        raise FileNotFoundError(f"Plan HDF not found: {plan_hdf}")

    # RasUnsteady refuses to run if the HDF already contains a Results group
    # (from a prior Windows or Linux run).  Strip it before copying.
    with h5py.File(plan_hdf, "a") as f:
        if "Results" in f:
            del f["Results"]
            print(f"Stripped stale Results group from {plan_hdf.name}")

    tmp_hdf = plan_hdf.with_name(
        plan_hdf.name.replace(f".{plan_suffix}.hdf", f".{plan_suffix}.tmp.hdf")
    )
    shutil.copy2(plan_hdf, tmp_hdf)
    print(f"Created tmp HDF: {tmp_hdf.name}")

    try:
        if run_geom_preprocess:
            _run_binary(
                "RasGeomPreprocess",
                [tmp_hdf.name, exec_suffix],
                cwd=project_dir,
                timeout_s=timeout_s,
                label="GeomPreprocess",
                num_threads=num_threads,
            )

        _run_binary(
            "RasUnsteady",
            [tmp_hdf.name, exec_suffix],
            cwd=project_dir,
            timeout_s=timeout_s,
            label="RasUnsteady",
            sim_start=sim_start,
            sim_end=sim_end,
            # Engine band: cover most of the bar's range so the user
            # sees continuous motion during the long-running simulation
            # instead of a long stall at one value (the engine is what
            # actually takes minutes / hours; the steps around it are
            # seconds).  Pre-engine setup ends at ~20, post-engine
            # extraction/save resumes from 90.
            pct_lo=20,
            pct_hi=90,
            num_threads=num_threads,
        )

        shutil.move(str(tmp_hdf), str(plan_hdf))
        print(f"Results harvested into {plan_hdf.name}")

    except Exception:
        if tmp_hdf.exists():
            print(f"Preserving {tmp_hdf.name} for postmortem inspection")
        raise

    return plan_hdf
