"""Real-time decision-support agent for HECinBOX.

The agent watches each completed simulation and sends **email alerts**
when user-defined conditions transition from false → true.  Built for
the unattended-server workflow: configure once, deploy, the daemon's
pipeline keeps firing alerts forever without any human in the loop.

Rules live in ``/app/.agent_state.json``; history of past firings lives
in ``/app/.agent_history.json``.  Nothing sensitive is ever stored -
SMTP credentials come from environment variables at ``docker run``::

    -e SMTP_HOST=smtp.gmail.com
    -e SMTP_PORT=587
    -e SMTP_USER=alerts@example.com
    -e SMTP_PASS="<app password>"
    -e SMTP_FROM=alerts@example.com
    -e SMTP_TO=engineer@example.com   # default; rules may override

Each rule has the shape::

    {
      "id":          str,            # uuid-ish
      "name":        str,            # human label, used in the email subject
      "type":        "cell" | "domain_peak" | "wetted_area",
      "enabled":     bool,
      "params": {
          # type == "cell"
          "cell_id":   int,
          "variable":  "depth" | "wse" | "velocity",
          "operator":  ">" | ">=" | "<" | "<=",
          "threshold": float,
          # type == "domain_peak"  (same shape minus cell_id)
          # type == "wetted_area"
          "min_wet_cells": int,
      },
      "recipients":  str,            # comma-separated; empty = SMTP_TO
      "fire_mode":   "transition" | "every_cycle",
      "last_state":  "below" | "above",
      "last_fired_at": ISO-8601 | None,
      "fire_count":  int,
    }

Firing semantics depend on ``fire_mode``:

* ``"transition"`` (default) - fire once on each below → above
  transition.  Re-arms when the condition goes back below.  Built
  for decision-makers who should not be paged repeatedly during a
  single sustained flood event.
* ``"every_cycle"`` - fire every scheduled iteration where the
  condition is true.  Heartbeat-style; useful when you *want*
  confirmation each cycle that the event is still active.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import uuid
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import numpy as np


# ── On-disk state ────────────────────────────────────────────────────
def _persist_dir() -> Path:
    """Directory for state that must survive container recreation.

    Prefers the mounted ``/host_out`` volume (the user's HEC-RAS-Outputs
    folder) so SMTP credentials, alert rules and history are NOT wiped
    every time the container is rebuilt or recreated - the single most
    common surprise before v3.1.2.  Falls back to the ephemeral ``/app``
    when no volume is mounted (e.g. a bare ``docker run`` with no -v).
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
    """Resolve a state-file path, migrating a pre-3.1.2 ``/app`` copy.

    Honours an explicit ``env_key`` override; otherwise points at the
    persistent dir.  If the persistent file doesn't exist yet but a
    legacy ephemeral copy does, the legacy contents are migrated once so
    upgrades don't lose existing rules/history.
    """
    p = Path(os.environ.get(env_key, str(_persist_dir() / name)))
    legacy_p = Path(legacy)
    if p != legacy_p and not p.exists() and legacy_p.exists():
        try:
            p.write_bytes(legacy_p.read_bytes())
        except OSError:
            pass
    return p


STATE_FILE = _state_file(
    "AGENT_STATE_FILE", "agent_state.json", "/app/.agent_state.json"
)
HISTORY_FILE = _state_file(
    "AGENT_HISTORY_FILE", "agent_history.json", "/app/.agent_history.json"
)
SMTP_CONFIG_FILE = _state_file(
    "AGENT_SMTP_CONFIG_FILE", "agent_smtp.json", "/app/.agent_smtp.json"
)
HISTORY_LIMIT = 500


# ── SMTP configuration persistence (UI → /app/.agent_smtp.json) ──────
_SMTP_KEYS = ("host", "port", "user", "password", "from", "to")


def load_smtp_config() -> dict:
    """Return the persisted SMTP config (or an empty stub).

    Disk config takes precedence over env vars - typed values win.
    For any key the user hasn't filled in, we fall back to the
    corresponding environment variable so an existing env-based
    deployment keeps working without re-entering anything.
    """
    cfg: dict = {}
    if SMTP_CONFIG_FILE.exists():
        try:
            data = json.loads(SMTP_CONFIG_FILE.read_text())
            if isinstance(data, dict):
                cfg = {k: data.get(k, "") for k in _SMTP_KEYS}
        except (json.JSONDecodeError, OSError):
            cfg = {}
    # Environment-variable fallback (per-key, so partial configs work).
    env_fallback = {
        "host": os.environ.get("SMTP_HOST", ""),
        "port": os.environ.get("SMTP_PORT", "") or "587",
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASS", ""),
        "from": os.environ.get("SMTP_FROM", ""),
        "to": os.environ.get("SMTP_TO", ""),
    }
    out = {}
    for k in _SMTP_KEYS:
        v = cfg.get(k, "")
        out[k] = (str(v) if v not in ("", None) else env_fallback.get(k, ""))
    return out


def save_smtp_config(cfg: dict) -> None:
    """Persist SMTP config to disk (inside the container).

    Caller is expected to pass a dict whose keys are a subset of
    ``_SMTP_KEYS``; anything else is ignored.  Empty strings are kept
    so the disk file represents an explicit user choice (and stops
    falling back to the env var for that key).
    """
    clean = {k: str(cfg.get(k, "")).strip() for k in _SMTP_KEYS}
    # Gmail (and others) display app passwords as four space-separated
    # groups - "abcd efgh ijkl mnop".  Users paste them verbatim and the
    # interior spaces make SMTP AUTH fail with "535 Username and Password
    # not accepted".  Strip ALL whitespace from the password so a pasted
    # app password just works.
    clean["password"] = re.sub(r"\s+", "", clean.get("password", ""))
    try:
        SMTP_CONFIG_FILE.write_text(json.dumps(clean, indent=2))
    except OSError:
        pass


def clear_smtp_config() -> None:
    """Delete the persisted SMTP config (env vars take over again)."""
    try:
        SMTP_CONFIG_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def load_state() -> dict:
    """Return the persisted rule set (or a fresh empty one)."""
    if not STATE_FILE.exists():
        return {"rules": []}
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"rules": []}
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        return {"rules": []}
    return data


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except OSError:
        pass


def append_history(entry: dict) -> None:
    history: list = []
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text())
            if isinstance(data, list):
                history = data
        except (json.JSONDecodeError, OSError):
            pass
    history.append(entry)
    history = history[-HISTORY_LIMIT:]
    try:
        HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))
    except OSError:
        pass


def load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def clear_history() -> None:
    """Wipe the alert-firing history (user-initiated from the Agent tab).

    Rules and SMTP config are untouched - only the record of past
    firings is removed.
    """
    try:
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
    except OSError:
        # Fall back to truncating to an empty list if unlink is blocked.
        try:
            HISTORY_FILE.write_text("[]")
        except OSError:
            pass


# ── Rule CRUD helpers (used by the Streamlit UI) ─────────────────────
def new_rule_id() -> str:
    return uuid.uuid4().hex[:12]


def upsert_rule(rule: dict) -> None:
    """Add a rule, or update the existing one with the same id."""
    state = load_state()
    rules = state["rules"]
    rid = rule.get("id") or new_rule_id()
    rule["id"] = rid
    rule.setdefault("enabled", True)
    rule.setdefault("fire_mode", "transition")
    rule.setdefault("last_state", "below")
    rule.setdefault("last_fired_at", None)
    rule.setdefault("fire_count", 0)
    rule.setdefault("recipients", "")
    for i, r in enumerate(rules):
        if r.get("id") == rid:
            rules[i] = rule
            break
    else:
        rules.append(rule)
    save_state(state)


def delete_rule(rule_id: str) -> None:
    state = load_state()
    state["rules"] = [r for r in state["rules"] if r.get("id") != rule_id]
    save_state(state)


def set_enabled(rule_id: str, enabled: bool) -> None:
    state = load_state()
    for r in state["rules"]:
        if r.get("id") == rule_id:
            r["enabled"] = bool(enabled)
            break
    save_state(state)


# ── SMTP delivery ────────────────────────────────────────────────────
def smtp_configured() -> bool:
    """True if a usable SMTP config exists (disk file OR env vars)."""
    cfg = load_smtp_config()
    return all(
        str(cfg.get(k, "")).strip()
        for k in ("host", "user", "password", "from")
    )


def smtp_status() -> dict:
    """Summary for the UI - no secrets, no password value included."""
    cfg = load_smtp_config()
    return {
        "configured": smtp_configured(),
        "host": (cfg.get("host") or "").strip() or None,
        "port": int((cfg.get("port") or "587").strip() or "587"),
        "user": (cfg.get("user") or "").strip() or None,
        "from": (cfg.get("from") or "").strip() or None,
        "to": (cfg.get("to") or "").strip() or None,
        "source": (
            "UI (disk)" if SMTP_CONFIG_FILE.exists()
            else ("environment" if os.environ.get("SMTP_HOST") else "none")
        ),
    }


def send_email(
    subject: str,
    body: str,
    *,
    to: str | None = None,
) -> tuple[bool, str]:
    """Send a plain-text email via SMTP.

    ``to`` may be a comma-separated list; if omitted, falls back to
    the persisted ``to`` field (and finally to ``SMTP_TO`` env var).
    Returns ``(ok, info)``.
    """
    cfg = load_smtp_config()
    host = (cfg.get("host") or "").strip()
    user = (cfg.get("user") or "").strip()
    # Strip any whitespace (incl. interior, e.g. Gmail app-password
    # groups) so an env-var or pre-3.1.1 saved config still authenticates.
    password = re.sub(r"\s+", "", cfg.get("password") or "")
    sender = (cfg.get("from") or "").strip()
    default_to = (cfg.get("to") or "").strip()
    if not (host and user and password and sender):
        return False, (
            "SMTP not configured - fill in host / user / password / "
            "from in **Tab 7 · Agent → Email channel → Configure SMTP**, "
            "or pass SMTP_* env vars at `docker run` time."
        )
    try:
        port = int((cfg.get("port") or "587").strip() or "587")
    except ValueError:
        port = 587

    recipients = (to or default_to).strip()
    if not recipients:
        return False, "No recipient - set a default 'To' or override per-rule."

    recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]
    if not recipient_list:
        return False, "Recipient list empty after parsing."

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipient_list)
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(user, password)
                s.send_message(msg)
    except Exception as exc:
        return False, f"SMTP send failed: {exc}"
    return True, f"Email delivered to {len(recipient_list)} recipient(s)."


# ── Rule evaluation ─────────────────────────────────────────────────
def _apply(value: float, threshold: float, operator: str) -> bool:
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    return False


def _peak_arrays(npz: Any) -> dict:
    """Return per-cell peak arrays for depth / WSE / velocity (where available)."""
    wse = np.asarray(npz["wse"], dtype=np.float32)
    out: dict = {"wse_peak": np.nanmax(wse, axis=0)}
    if "min_elev" in npz.files:
        depth = np.clip(wse - npz["min_elev"], 0.0, None)
        out["depth_peak"] = np.nanmax(depth, axis=0)
    if "vel" in npz.files:
        vel = np.asarray(npz["vel"], dtype=np.float32)
        out["vel_peak"] = np.nanmax(vel, axis=0)
    return out


_VAR_KEY = {
    "depth": "depth_peak",
    "wse": "wse_peak",
    "velocity": "vel_peak",
}
def _var_label(var: str, model_is_si: bool) -> str:
    """Label like ``depth (m)`` / ``velocity (ft/s)`` for the model's
    unit system.  HEC-RAS results are already in the model's native
    units (SI metres / English feet); the agent only labels them, it
    never converts, so the threshold the user typed is compared in the
    same units the plots show."""
    length = "m" if model_is_si else "ft"
    vel = "m/s" if model_is_si else "ft/s"
    return {
        "depth": f"depth ({length})",
        "wse": f"WSE ({length})",
        "velocity": f"velocity ({vel})",
    }.get(var, var)


def evaluate_rule(
    rule: dict, peaks: dict, model_is_si: bool = True
) -> tuple[bool, str]:
    """Evaluate a single rule against peak arrays.

    Returns ``(condition_now, summary)``.  ``summary`` is a short
    human-readable explanation suitable for the email body.
    """
    rtype = rule.get("type")
    p = rule.get("params", {}) or {}
    op = p.get("operator", ">")

    if rtype == "cell":
        cell_id = int(p.get("cell_id", -1))
        var = p.get("variable", "depth")
        thr = float(p.get("threshold", 0.0))
        arr = peaks.get(_VAR_KEY.get(var, ""))
        label = _var_label(var, model_is_si)
        if arr is None or cell_id < 0 or cell_id >= len(arr):
            return False, f"Cell {cell_id}: no {label} data available."
        value = float(arr[cell_id])
        if not np.isfinite(value):
            return False, f"Cell {cell_id}: non-finite {label}."
        triggered = _apply(value, thr, op)
        return triggered, (
            f"Cell {cell_id} peak {label} = {value:.3f}  "
            f"(threshold {op} {thr:.3f}) → "
            f"{'TRIGGERED' if triggered else 'below threshold'}."
        )

    if rtype == "domain_peak":
        var = p.get("variable", "depth")
        thr = float(p.get("threshold", 0.0))
        arr = peaks.get(_VAR_KEY.get(var, ""))
        label = _var_label(var, model_is_si)
        if arr is None:
            return False, f"No {label} data in this run."
        worst = float(np.nanmax(arr))
        if not np.isfinite(worst):
            return False, f"No finite {label}."
        worst_cell = int(np.nanargmax(arr))
        triggered = _apply(worst, thr, op)
        return triggered, (
            f"Domain peak {label} = {worst:.3f} at cell {worst_cell}  "
            f"(threshold {op} {thr:.3f}) → "
            f"{'TRIGGERED' if triggered else 'below threshold'}."
        )

    if rtype == "wetted_area":
        min_wet = int(p.get("min_wet_cells", 0))
        depth = peaks.get("depth_peak")
        if depth is None:
            return False, (
                "Wetted-area rule needs depth data; this model has no "
                "min_elev field - re-run the simulation to populate it."
            )
        n_wet = int(np.sum(np.isfinite(depth) & (depth > 0.05)))
        triggered = n_wet >= min_wet
        return triggered, (
            f"Wetted cells = {n_wet}  (threshold ≥ {min_wet}) → "
            f"{'TRIGGERED' if triggered else 'below threshold'}."
        )

    return False, f"Unknown rule type: {rtype!r}"


def describe_rule(rule: dict) -> str:
    """Short one-line description of a rule for the UI."""
    rtype = rule.get("type")
    p = rule.get("params", {}) or {}
    if rtype == "cell":
        return (
            f"Cell {p.get('cell_id', '?')} · peak "
            f"{p.get('variable', '?')} "
            f"{p.get('operator', '?')} {p.get('threshold', '?')}"
        )
    if rtype == "domain_peak":
        return (
            f"Domain peak {p.get('variable', '?')} "
            f"{p.get('operator', '?')} {p.get('threshold', '?')}"
        )
    if rtype == "wetted_area":
        return f"Wetted cells ≥ {p.get('min_wet_cells', '?')}"
    return rtype or "?"


def _format_email(
    rule: dict, summary: str, run_meta: dict, tb=None
) -> tuple[str, str]:
    from timebase import TimeBase
    tb = tb or TimeBase()
    name = rule.get("name", "Unnamed rule")
    subject = f"[HECinBOX] Alert: {name}"
    # Every time in this email is rendered on the same two clocks
    # (v4.8.0).  Before, "Fired at" was UTC while the window below it
    # was on the model's own clock, with only the first one labelled -
    # two different clocks side by side in one message.
    start = tb.stamp(run_meta["start"]) if run_meta.get("start") else "-"
    end = tb.stamp(run_meta["end"]) if run_meta.get("end") else "-"
    out_dir = run_meta.get("output_dir") or "-"
    # The firing instant is a real-world event, so it is converted from
    # UTC rather than from the model clock.
    _fired = TimeBase(0, tb.site_lst_offset).stamp(datetime.utcnow())
    body = (
        "HECinBOX real-time decision-support alert\n"
        "============================================\n\n"
        f"Rule name      : {name}\n"
        f"Rule type      : {rule.get('type')}\n"
        f"Definition     : {describe_rule(rule)}\n"
        f"Fired at       : {_fired}\n\n"
        f"Trigger detail : {summary}\n\n"
        "Simulation window\n"
        f"  start        : {start}\n"
        f"  end          : {end}\n"
        f"  output_dir   : {out_dir}\n\n"
        "--\n"
        "This is an automated alert from the HECinBOX agent.\n"
        "Manage rules in Tab 7 · Agent of the web UI.\n"
    )
    return subject, body


def evaluate_after_run(
    output_dir: str | Path,
    run_meta: dict | None = None,
) -> list[dict]:
    """Evaluate every enabled rule against a just-finished run.

    Reads ``wse_extract.npz`` from ``output_dir``, applies each rule,
    fires one email per rule that transitioned ``below → above``, and
    persists the new ``last_state`` per rule.  Errors are caught and
    recorded in the history rather than raised - flood forecasting
    must not crash because the mail server is down.

    Returns the list of fire events (one per email sent).
    """
    output_dir = Path(output_dir)
    npz_path = output_dir / "wse_extract.npz"
    if not npz_path.exists():
        return []

    state = load_state()
    rules = state.get("rules") or []
    if not rules:
        return []

    run_meta = dict(run_meta or {})
    run_meta.setdefault("output_dir", str(output_dir))

    try:
        with np.load(npz_path, allow_pickle=True) as npz:
            peaks = _peak_arrays(npz)
            # The run writes its unit system into the npz (main.py), so
            # alerts label depth/velocity in the model's native units
            # (m / m/s for SI, ft / ft/s for English) instead of always
            # feet.  Values and thresholds are untouched - only labels.
            _unit_system = (
                str(npz["unit_system"][0])
                if "unit_system" in npz.files else None
            )
            # The run also records its clock (v4.8.0), so alert times
            # are rendered in the site's local standard time with UTC
            # alongside instead of a bare model-clock string.
            from timebase import TimeBase
            _tb = TimeBase.from_npz(npz)
        model_is_si = str(_unit_system).upper() != "ENGLISH"
        if not _tb.known:
            # Pre-v4.8.0 folder: fall back to the run_meta, then accept
            # "unknown" rather than inventing an offset.
            _tb = TimeBase.from_run_meta(run_meta)
    except Exception as exc:
        append_history({
            "at": datetime.utcnow().isoformat(),
            "error": f"Could not load {npz_path}: {exc}",
        })
        return []

    events: list[dict] = []
    changed = False
    for rule in rules:
        if not rule.get("enabled", True):
            continue

        try:
            cond_now, summary = evaluate_rule(rule, peaks, model_is_si)
        except Exception as exc:
            append_history({
                "at": datetime.utcnow().isoformat(),
                "rule_id": rule.get("id"),
                "rule_name": rule.get("name"),
                "error": f"Evaluation error: {exc}",
            })
            continue

        was = rule.get("last_state", "below")
        changed = True

        # Two firing modes:
        #   * "transition"  - fire once on below → above (default).
        #   * "every_cycle" - fire every iteration while condition holds.
        mode = (rule.get("fire_mode") or "transition").strip()
        if mode == "every_cycle":
            should_fire = bool(cond_now)
        else:
            should_fire = (was == "below" and cond_now)

        # Track the physical condition by default.  In transition mode we
        # only *commit* the below→above arming once the alert is actually
        # delivered - otherwise a transient SMTP failure would flip the
        # state to "above" and the alert would never re-fire, silently
        # losing the event (v3.1.1).
        new_state = "above" if cond_now else "below"

        if should_fire:
            subject, body = _format_email(rule, summary, run_meta, _tb)
            recipients = (rule.get("recipients") or "").strip() or None
            ok, info = send_email(subject, body, to=recipients)
            event = {
                "at": datetime.utcnow().isoformat(),
                "rule_id": rule.get("id"),
                "rule_name": rule.get("name"),
                "summary": summary,
                "email_ok": ok,
                "email_info": info,
                "output_dir": str(output_dir),
            }
            events.append(event)
            append_history(event)
            rule["last_fired_at"] = event["at"]
            rule["fire_count"] = int(rule.get("fire_count", 0)) + 1
            if mode != "every_cycle" and cond_now and not ok:
                # Delivery failed - stay "below" so the next scheduled
                # cycle re-attempts the alert for this same event.
                new_state = "below"

        rule["last_state"] = new_state

    if changed:
        save_state(state)
    return events
