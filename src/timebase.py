"""One clock, rendered one way, everywhere (v4.8.0).

A HEC-RAS model stores its timestamps as a bare wall-clock reading and
records no time zone anywhere in the project files, so a raw timestamp
out of a results HDF means nothing on its own.  Since v4.8.0 every run
folder records what its clock is (``model_utc_offset``) plus the Local
Standard Time of the model site (``site_lst_offset``), in both
``wse_extract.npz`` and ``run_meta.json`` - the same self-describing
trick already used for ``unit_system``.

This module turns those two numbers into the single rendering used by
the Results tab, the Validation tab, the alert agent and the live
dashboard::

    2022-10-14 18:00 LST(UTC-6)  ·  2022-10-15 00:00 UTC

Times are shown in the site's **Local Standard Time** (what a local
emergency manager reads) with UTC alongside (what every data service
publishes).  The UTC date is repeated only when it differs from the
local one, so a midnight rollover can never be misread.

The one rule this module never breaks: when a run folder does not
record its clock - a raw HEC-RAS project someone opened, or a folder
computed before v4.8.0 - it says so instead of guessing.  Silently
assuming a clock is exactly the failure this whole mechanism exists to
prevent.
"""
from __future__ import annotations

# No daylight saving anywhere in here: HEC-RAS keeps one continuous
# clock, so a model window never jumps an hour in spring or autumn.
_UNKNOWN_NOTE = "clock not recorded"


def _fmt_offset(h: int) -> str:
    """``-6`` -> ``'UTC-6'``, ``0`` -> ``'UTC'``."""
    return f"UTC{h:+d}" if h else "UTC"


class TimeBase:
    """The clock a run's timestamps are on, plus how to display them.

    ``model_utc_offset`` is the authoritative fact: model wall clock =
    UTC + this.  ``site_lst_offset`` is presentation only and never
    shifts stored data.  ``model_utc_offset is None`` means the run
    folder did not record a clock.
    """

    __slots__ = ("model_utc_offset", "site_lst_offset")

    def __init__(
        self,
        model_utc_offset: int | None = None,
        site_lst_offset: int | None = None,
    ) -> None:
        self.model_utc_offset = (
            None if model_utc_offset is None else int(model_utc_offset)
        )
        # A run that knows its own clock but not the site's falls back
        # to the model clock, which reproduces pre-v4.8.0 rendering.
        if site_lst_offset is None:
            self.site_lst_offset = self.model_utc_offset
        else:
            self.site_lst_offset = int(site_lst_offset)

    # ── construction ────────────────────────────────────────────────
    @classmethod
    def from_npz(cls, npz) -> "TimeBase":
        """Read the clock out of an open ``wse_extract.npz``."""
        def _get(key):
            try:
                if key in npz.files:
                    return int(npz[key][0])
            except Exception:
                pass
            return None
        return cls(_get("model_utc_offset"), _get("site_lst_offset"))

    @classmethod
    def from_run_meta(cls, meta: dict | None) -> "TimeBase":
        meta = meta or {}
        def _get(key):
            v = meta.get(key)
            try:
                return None if v is None else int(v)
            except (TypeError, ValueError):
                return None
        return cls(
            _get("model_utc_offset_hours"), _get("site_lst_offset_hours")
        )

    @classmethod
    def from_dir(cls, output_dir) -> "TimeBase":
        """Best-effort read from a run folder (npz first, then meta)."""
        from pathlib import Path
        d = Path(output_dir)
        try:
            import numpy as np
            p = d / "wse_extract.npz"
            if p.exists():
                with np.load(p, allow_pickle=True) as z:
                    tb = cls.from_npz(z)
                if tb.known:
                    return tb
        except Exception:
            pass
        try:
            import json
            p = d / "run_meta.json"
            if p.exists():
                return cls.from_run_meta(json.loads(p.read_text()))
        except Exception:
            pass
        return cls(None, None)

    # ── properties ──────────────────────────────────────────────────
    @property
    def known(self) -> bool:
        """False when the run folder never recorded its clock."""
        return self.model_utc_offset is not None

    @property
    def lst_label(self) -> str:
        """``'LST(UTC-6)'``, or a plain marker when the clock is unknown."""
        if not self.known:
            return _UNKNOWN_NOTE
        return f"LST({_fmt_offset(self.site_lst_offset)})"

    # ── conversion (model wall clock -> …) ──────────────────────────
    def to_utc(self, t):
        """Model-clock timestamp(s) -> UTC. Unchanged if clock unknown."""
        if not self.known:
            return t
        return self._shift(t, -self.model_utc_offset)

    def to_lst(self, t):
        """Model-clock timestamp(s) -> site Local Standard Time."""
        if not self.known:
            return t
        return self._shift(t, self.site_lst_offset - self.model_utc_offset)

    @staticmethod
    def _shift(t, hours: int):
        if not hours:
            return t
        import pandas as pd
        return pd.to_datetime(t) + pd.Timedelta(hours=hours)

    # ── rendering ───────────────────────────────────────────────────
    def stamp(self, t, fmt: str = "%Y-%m-%d %H:%M") -> str:
        """One timestamp, local standard time with UTC alongside.

        The UTC date is repeated only when it lands on a different day
        than the local one.
        """
        import pandas as pd
        try:
            ts = pd.to_datetime(t)
        except Exception:
            return str(t)
        if not self.known:
            return f"{ts:{fmt}} ({_UNKNOWN_NOTE})"
        lst, utc = self.to_lst(ts), self.to_utc(ts)
        utc_txt = (
            f"{utc:%H:%M} UTC" if utc.date() == lst.date()
            else f"{utc:{fmt}} UTC"
        )
        return f"{lst:{fmt}} {self.lst_label}  ·  {utc_txt}"

    def axis_label(self, base: str = "Time") -> str:
        """Axis title carrying the clock, e.g. ``'Time - LST(UTC-6)'``.

        Tick labels themselves stay in local standard time; repeating
        both clocks on every tick would be unreadable.
        """
        if not self.known:
            return f"{base} ({_UNKNOWN_NOTE})"
        return f"{base} - {self.lst_label}"

    def iso(self, t) -> str:
        """ISO-8601 **with offset** - the only safe form for a file.

        Naive local time in an exported CSV recreates exactly the
        ambiguity HEC-RAS itself suffers from, so exports carry the
        offset explicitly.
        """
        import pandas as pd
        try:
            ts = pd.to_datetime(t)
        except Exception:
            return str(t)
        if not self.known:
            return f"{ts:%Y-%m-%dT%H:%M:%S}"
        off = self.site_lst_offset
        sign = "+" if off >= 0 else "-"
        return (
            f"{self.to_lst(ts):%Y-%m-%dT%H:%M:%S}"
            f"{sign}{abs(off):02d}:00"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TimeBase(model_utc_offset={self.model_utc_offset}, "
            f"site_lst_offset={self.site_lst_offset})"
        )
