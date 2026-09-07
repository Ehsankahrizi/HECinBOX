from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import requests


class NOAAClient:
    BASE_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    # ----------------------------------------
    # Fetch NOAA water level using explicit time window
    # ----------------------------------------
    def fetch_water_level(
        self,
        station: str,
        startDT: datetime,   # ← NEW (from YAML)
        endDT: datetime,     # ← NEW (from YAML)
        datum: str = "NAVD",
        units: str = "english",
        time_zone: str = "gmt",
        label: str = "noaa_water_level",
    ) -> pd.DataFrame:

        # NOAA CO-OPS limits water_level (6-min) requests to 31 days.
        # Split the window into <=31-day chunks and concatenate.
        start = pd.to_datetime(startDT)
        end = pd.to_datetime(endDT)

        chunks = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + pd.Timedelta(days=30), end)
            chunk_df = self._fetch_chunk(
                station, chunk_start, chunk_end, datum, units, time_zone
            )
            if not chunk_df.empty:
                chunks.append(chunk_df)
            chunk_start = chunk_end + pd.Timedelta(days=1)

        if not chunks:
            return pd.DataFrame()

        df = pd.concat(chunks, ignore_index=True)

        df["station_id"] = station
        df["label"] = label
        df["datum"] = datum
        df["units"] = units
        df["time_zone"] = time_zone

        df = (
            df.dropna(subset=["datetime", "value"])
              .drop_duplicates(subset=["datetime"])
              .sort_values("datetime")
              .reset_index(drop=True)
        )

        return df

    def _fetch_chunk(
        self,
        station: str,
        start: datetime,
        end: datetime,
        datum: str,
        units: str,
        time_zone: str,
    ) -> pd.DataFrame:
        params = {
            "product": "water_level",
            "station": station,
            "begin_date": pd.to_datetime(start).strftime("%Y%m%d"),
            "end_date": pd.to_datetime(end).strftime("%Y%m%d"),
            "datum": datum,
            "units": units,
            "time_zone": time_zone,
            "format": "json",
            "application": "HEC_RAS_Project",
        }

        response = requests.get(self.BASE_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()

        if "error" in payload:
            raise RuntimeError(f"NOAA API error: {payload['error']}")

        df = pd.DataFrame(payload.get("data", []))
        if df.empty:
            return df

        df = df.rename(columns={"t": "datetime", "v": "value"})
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce").dt.tz_localize(None)
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        return df

    # ----------------------------------------
    # Save DataFrame to CSV
    # ----------------------------------------
    @staticmethod
    def save(df: pd.DataFrame, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)