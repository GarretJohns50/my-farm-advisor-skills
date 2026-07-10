"""Weather data transformations for the dashboard.

Parse per-field daily weather CSVs and compute:
- Last frost date (latest day before July 1 with T2M_MIN <= 0.0°C)
- Growing degree days: max((T2M_MAX + T2M_MIN)/2 - 10.0, 0)
- Rainfall in inches: PRECTOTCORR * 0.0393701
- Cumulative totals starting from last frost date
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


def parse_daily_weather(csv_path: Path) -> pd.DataFrame | None:
    """Read daily_weather.csv, return DataFrame or None if empty/unreadable."""
    if not csv_path.exists():
        return None
    try:
        df = pd.read_csv(csv_path, parse_dates=["date"])
    except Exception:
        return None
    if df.empty or len(df.columns) <= 1:
        return None
    # Ensure required columns exist
    required = {"date", "T2M_MAX", "T2M_MIN", "PRECTOTCORR"}
    if not required.issubset(df.columns):
        return None
    return df


def compute_last_frost_date(df: pd.DataFrame, year: int) -> tuple[str, int]:
    """Find the latest day before July 1 where T2M_MIN <= 0.0°C.

    Returns (iso_date_str, day_of_year). Defaults to Jan 1 if no frost found.
    """
    year_df = df[df["date"].dt.year == year].copy()
    if year_df.empty:
        default = datetime(year, 1, 1)
        return default.strftime("%Y-%m-%d"), 1

    # Filter to before July 1
    before_july = year_df[year_df["date"] < pd.Timestamp(year=year, month=7, day=1)]
    if before_july.empty:
        default = datetime(year, 1, 1)
        return default.strftime("%Y-%m-%d"), 1

    frosts = before_july[before_july["T2M_MIN"] <= 0.0]
    if frosts.empty:
        default = datetime(year, 1, 1)
        return default.strftime("%Y-%m-%d"), 1

    last_frost_row = frosts.loc[frosts["date"].idxmax()]
    last_frost_date = last_frost_row["date"]
    return last_frost_date.strftime("%Y-%m-%d"), int(last_frost_date.dayofyear)


def compute_weather_transforms(df: pd.DataFrame) -> list[dict]:
    """Compute GDD, rainfall, and cumulative values per field-year.

    Returns a list of weather-by-field-year dicts with embedded daily records.
    """
    if df is None or df.empty:
        return []

    results = []
    years = sorted(df["date"].dt.year.unique())

    for year in years:
        year_df = df[df["date"].dt.year == year].copy()
        if year_df.empty:
            continue

        last_frost_iso, last_frost_doy = compute_last_frost_date(df, year)
        last_frost_ts = pd.Timestamp(last_frost_iso)

        # Filter to days at and after last frost
        growing = year_df[year_df["date"] >= last_frost_ts].copy()
        if growing.empty:
            continue

        growing = growing.sort_values("date").reset_index(drop=True)

        # Daily calculations
        growing["dailyGdd"] = ((growing["T2M_MAX"] + growing["T2M_MIN"]) / 2.0 - 10.0).clip(lower=0.0)
        growing["dailyRainfallIn"] = growing["PRECTOTCORR"] * 0.0393701

        # Cumulative
        growing["cumulativeGdd"] = growing["dailyGdd"].cumsum()
        growing["cumulativeRainfallIn"] = growing["dailyRainfallIn"].cumsum()

        daily_records = []
        for _, row in growing.iterrows():
            daily_records.append({
                "date": row["date"].strftime("%Y-%m-%d"),
                "dayOfYear": int(row["date"].dayofyear),
                "dailyGdd": round(float(row["dailyGdd"]), 2),
                "cumulativeGdd": round(float(row["cumulativeGdd"]), 2),
                "dailyRainfallIn": round(float(row["dailyRainfallIn"]), 2),
                "cumulativeRainfallIn": round(float(row["cumulativeRainfallIn"]), 2),
            })

        results.append({
            "year": int(year),
            "lastFrostDate": last_frost_iso,
            "lastFrostDoy": last_frost_doy,
            "daily": daily_records,
        })

    return results
