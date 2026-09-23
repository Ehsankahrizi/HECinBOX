"""Detached HEC-RAS pipeline job runner.

The pipeline is spawned as a fully independent OS process (its own session,
new process group) so it survives:

  - browser tab focus loss
  - laptop sleep / screen off
  - Streamlit websocket drops or full page reloads

State is persisted on disk inside the run's output_dir:

  run.pid     - PID of the pipeline process
  run.status  - JSON {state, pid, started_at, ended_at, returncode}
  run.log     - combined stdout/stderr stream (unbuffered)

The Streamlit UI polls these files via an `@st.fragment(run_every=...)`
loop. Process handles for jobs started during the current Streamlit
process are kept in a module-level dict so we can read returncodes via
``proc.poll()``; if the Streamlit container itself restarts, the running
pipeline continues independently and is tracked by PID only (best-effort
``done/failed`` inference from log tail).
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

_PROGRESS_RE = re.compile(r"^PROGRESS\|(\d+)\|(.*)$")

# Process handles for jobs started during this Streamlit process.
_active: dict[str, subprocess.Popen] = {}


def _status_path(d: Path) -> Path:
    return d / "run.status"


def _pid_path(d: Path) -> Path:
    return d / "run.pid"


def _log_path(d: Path) -> Path:
    return d / "run.log"


def start_job(
    output_dir: Path,
    settings_path: Path,
    python_exe: str,
    main_cwd: str = "/app/src",
) -> int:
    """Spawn the pipeline as a detached process. Returns the PID."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_f = open(_log_path(output_dir), "wb", buffering=0)
    env = {
        **os.environ,
        "SETTINGS_PATH": str(settings_path),
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [python_exe, "-m", "main", "run"],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        cwd=main_cwd,
        env=env,
        start_new_session=True,  # detach from parent's process group
        close_fds=True,
    )
    _active[str(output_dir)] = proc
    _pid_path(output_dir).write_text(str(proc.pid))
    _status_path(output_dir).write_text(json.dumps({
        "state": "running",
        "pid": proc.pid,
        "started_at": datetime.utcnow().isoformat(),
    }))
    return proc.pid


def is_alive(pid: int) -> bool:
    """OS-level liveness check (signal 0 = existence probe)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def _parse_log(output_dir: Path) -> tuple[int, str, str]:
    """Return (progress_pct, last_message, last-80-lines-of-log)."""
    lp = _log_path(output_dir)
    if not lp.exists():
        return 0, "Starting…", ""
    text = lp.read_text(errors="ignore")
    last_pct = 0
    last_msg = ""
    for line in text.splitlines():
        m = _PROGRESS_RE.match(line.strip())
        if m:
            last_pct = max(0, min(100, int(m.group(1))))
            last_msg = m.group(2)
    tail = "\n".join(text.splitlines()[-80:])
    return last_pct, last_msg, tail


# Known engine / pipeline failures -> a plain-language next step.  Matched
# against the reason line, first hit wins.
_FAILURE_HINTS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"forrtl.*file .*\.b\d\d\s*$", re.I),
     "HEC-RAS could not read the boundary file (.b). This model folder "
     "did not include one, so HECinBOX generated it, and the engine "
     "rejected it. Open the model in HEC-RAS "
     "on Windows, compute this plan once (that writes the .b file), and "
     "upload the folder again."),
    (re.compile(r"forrtl.*file .*\.x\d\d\s*$", re.I),
     "HEC-RAS could not read the execution file (.x). Compute this plan "
     "once in HEC-RAS on Windows and upload the folder again."),
    (re.compile(r"forrtl", re.I),
     "The HEC-RAS engine stopped while reading the model files. The "
     "file named above is the one it could not read."),
    (re.compile(r"HDF_ERROR|HDF5-DIAG|OSError: Unable to", re.I),
     "The plan HDF could not be read or written. Make sure the plan "
     "HDF in the model folder is complete (not a partial upload) and "
     "matches the geometry."),
    (re.compile(r"FileNotFoundError", re.I),
     "A file the model needs is missing from the model folder. Check "
     "the file named above was uploaded."),
    (re.compile(r"exited with code|killed|MemoryError", re.I),
     "The HEC-RAS engine stopped before finishing. The log excerpt "
     "below shows its last messages."),
)

# Lines that are progress noise, never the cause of a failure.
_NOISE = (
    "PROGRESS", "SIMTIME=", "ABSDATE=", "ABSTIME=", "ITER2D=",
    "FONT=", "LABEL=",
)


def failure_summary(output_dir: Path) -> dict[str, str]:
    """Why a finished run failed, pulled from its ``run.log``.

    Returns ``{"reason", "hint", "excerpt"}``.  ``reason`` is the one
    line that names the cause (a Fortran ``forrtl`` error beats a Python
    exception, which just says the engine exited); ``hint`` is a
    plain-language next step when the cause is a known one; ``excerpt``
    is the tail of the log from the first error onwards.
    """
    lp = _log_path(output_dir)
    try:
        lines = lp.read_text(errors="ignore").splitlines()
    except OSError:
        return {"reason": "", "hint": "", "excerpt": ""}
    lines = [
        ln.rstrip() for ln in lines
        if ln.strip() and not any(n in ln for n in _NOISE)
    ]

    def _clean(ln: str) -> str:
        # Engine lines are echoed as "  [RasUnsteady] <text>".
        return re.sub(r"^\s*\[[^\]]+\]\s*", "", ln).strip()

    reason = ""
    start = None
    # The engine's own error beats the Python exception that only says
    # the engine exited.
    for marker in ("forrtl:", "HDF_ERROR"):
        for i, ln in enumerate(lines):
            if marker in ln:
                reason, start = _clean(ln), i
                break
        if reason:
            break
    if not reason:
        for i in range(len(lines) - 1, -1, -1):
            if re.match(r"^\w*(Error|Exception)\b.*:", lines[i].strip()):
                reason = lines[i].strip()
                start = i
                break
        for i, ln in enumerate(lines):
            if ln.startswith("Traceback"):
                start = i
                break

    hint = ""
    for pat, text in _FAILURE_HINTS:
        if reason and pat.search(reason):
            hint = text
            break
    tail = lines[start:] if start is not None else lines[-40:]
    return {"reason": reason, "hint": hint, "excerpt": "\n".join(tail[:60])}


def read_status(output_dir: Path) -> dict[str, Any]:
    """Return a fresh status dict including parsed progress + log tail."""
    sp = _status_path(output_dir)
    if not sp.exists():
        return {"state": "absent"}
    try:
        status = json.loads(sp.read_text())
    except Exception:
        return {"state": "absent"}

    # If still flagged running, check the actual process.
    if status.get("state") == "running":
        proc = _active.get(str(output_dir))
        if proc is not None:
            rc = proc.poll()
            if rc is not None:
                status["state"] = "done" if rc == 0 else "failed"
                status["returncode"] = rc
                status["ended_at"] = datetime.utcnow().isoformat()
                sp.write_text(json.dumps(status))
                _active.pop(str(output_dir), None)
        else:
            # No live handle (e.g. Streamlit restarted); use PID liveness.
            pid = status.get("pid")
            if pid and not is_alive(pid):
                _, _, tail = _parse_log(output_dir)
                status["state"] = (
                    "done" if "PROGRESS|100" in tail
                    or "Results harvested" in tail
                    else "ended"
                )
                status["ended_at"] = datetime.utcnow().isoformat()
                sp.write_text(json.dumps(status))

    progress, message, tail = _parse_log(output_dir)
    status["progress"] = progress
    status["message"] = message
    status["log_tail"] = tail
    return status


def stop_job(output_dir: Path) -> bool:
    """SIGTERM the process group. Returns True if a signal was sent."""
    pp = _pid_path(output_dir)
    if not pp.exists():
        return False
    try:
        pid = int(pp.read_text().strip())
    except ValueError:
        return False
    if not is_alive(pid):
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    sp = _status_path(output_dir)
    status: dict[str, Any] = {}
    if sp.exists():
        try:
            status = json.loads(sp.read_text())
        except Exception:
            status = {}
    status["state"] = "stopped"
    status["ended_at"] = datetime.utcnow().isoformat()
    sp.write_text(json.dumps(status))
    _active.pop(str(output_dir), None)
    return True
