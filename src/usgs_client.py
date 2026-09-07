from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests


class USGSClient:
    BASE_URL = "https://waterservices.usgs.gov/nwis/iv/"

    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    # ----------------------------------------
    # Fetch USGS data using explicit time window
    # ----------------------------------------
    def fetch(
        self,
        site: str,
        parameter_cd: str,
        startDT: datetime,   # ← NEW (from YAML)
        endDT: datetime,     # ← NEW (from YAML)
        label: str = "usgs_data",
    ) -> pd.DataFrame:

        # ----------------------------------------
        # Convert datetime → string format required by USGS API
        # ----------------------------------------
        start_str = pd.to_datetime(startDT).strftime("%Y-%m-%d")
        end_str = pd.to_datetime(endDT).strftime("%Y-%m-%d")

        # ----------------------------------------
        # API request parameters
        # ----------------------------------------
        params = {
            "format": "json",
            "sites": site,
            "parameterCd": parameter_cd,
            "siteStatus": "all",
            "startDT": start_str,   # ← YAML-controlled start
            "endDT": end_str,       # ← YAML-controlled end
            "agencyCd": "USGS",
        }

        # ----------------------------------------
        # Call USGS API
        # ----------------------------------------
        response = requests.get(self.BASE_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()

        # ----------------------------------------
        # Parse JSON → rows
        # ----------------------------------------
        rows = []
        for ts in payload.get("value", {}).get("timeSeries", []):
            site_name = ts.get("sourceInfo", {}).get("siteName", "")
            var_info = ts.get("variable", {})

            var_name = var_info.get("variableName", "")
            var_code = ""
            if var_info.get("variableCode"):
                var_code = var_info["variableCode"][0].get("value", "")

            for block in ts.get("values", []):
                for item in block.get("value", []):
                    rows.append(
                        {
                            "datetime": item.get("dateTime"),
                            "value": item.get("value"),
                            "qualifiers": ",".join(item.get("qualifiers", [])),
                            "site_id": site,
                            "site_name": site_name,
                            "parameter_code": var_code,
                            "parameter_name": var_name,
                            "label": label,
                        }
                    )

        # ----------------------------------------
        # Convert to DataFrame
        # ----------------------------------------
        df = pd.DataFrame(rows)

        if df.empty:
            return df

        # ----------------------------------------
        # Clean data
        # ----------------------------------------
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True).dt.tz_convert(None)
        df["value"] = pd.to_numeric(df["value"], errors="coerce")

        df = (
            df.dropna(subset=["datetime", "value"])
              .sort_values("datetime")
              .reset_index(drop=True)
        )

        return df

    # ----------------------------------------
    # Save DataFrame to CSV
    # ----------------------------------------
    @staticmethod
    def save(df: pd.DataFrame, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)