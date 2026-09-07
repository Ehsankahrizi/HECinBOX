from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import yaml


@dataclass
class AppConfig:
    raw: Dict[str, Any]

    @property
    def boundary_conditions(self) -> List[Dict[str, Any]]:
        """User-configured boundary conditions to inject (one per BC line)."""
        return self.raw.get("boundary_conditions", [])

    @property
    def precipitation(self) -> Dict[str, Any]:
        """Rain-on-mesh (constant precipitation) forcing, if enabled."""
        return self.raw.get("precipitation", {}) or {}

    @property
    def validation(self) -> Dict[str, Any]:
        return self.raw.get("validation", {})

    @property
    def hecras(self) -> Dict[str, Any]:
        return self.raw["hecras"]

    @property
    def time_offset_hours(self) -> int:
        """Model Local-Standard-Time offset from UTC (hours).

        Fetched USGS/NOAA/forecast timestamps (all UTC) are shifted by
        this to land on the model's clock.  Defaults to 0 (UTC) when the
        run predates the time-base setting.
        """
        try:
            return int((self.raw.get("time") or {}).get(
                "lst_offset_hours", 0
            ) or 0)
        except Exception:
            return 0

    @property
    def site_lst_offset_hours(self) -> int:
        """Local Standard Time offset at the model site (hours from UTC).

        This is a *display* value only - it never shifts any data.  It
        exists so the run's artifacts can later render times in the
        site's local standard time with UTC alongside, regardless of
        which clock the model itself was built on
        (:attr:`time_offset_hours`).

        Derived from the model's longitude by the scanner.  Falls back
        to the model's own clock when absent, which keeps pre-v4.8.0
        runs rendering exactly as before.
        """
        t = self.raw.get("time") or {}
        try:
            v = t.get("site_lst_offset_hours")
            return int(self.time_offset_hours if v is None else v)
        except Exception:
            return self.time_offset_hours


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return AppConfig(raw=data)
