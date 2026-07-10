"""Pre-computed DOY temperature baseline generator.

Computes per-day-of-year (DOY) temperature normals from county-level
NASA POWER daily weather parquet files (2021–2025) and caches them
as JSON for use by cool-period detection in the field dashboard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


FIPS_RENAME_MAP = {
    "COUNTYFP": "fips",
    "countyfp": "fips",
    "GEOID": "fips",
    "geoid": "fips",
    "FIPS": "fips",
}


def _resolve_fips_col(df: pd.DataFrame) -> str | None:
    """Return the column name that holds the FIPS code."""
    for candidate in FIPS_RENAME_MAP:
        if candidate in df.columns:
            return candidate
    return None


def ensure_doy_temperature_baseline(
    fips_code: str,
    shared_dir: Path,
    years: list[int] | None = None,
) -> Path:
    """Generate or return cached DOY temperature baseline for a county.

    Reads county-level daily weather parquet files from
    shared/weather/nasa-power/<year>/daily_weather_by_fips.parquet,
    computes mean T2M, T2M_MAX, and T2M_MIN per DOY across all years,
    and saves JSON to shared/weather/doy_baseline/<fips>_t2m_baseline.json.

    Parameters
    ----------
    fips_code : str
        5-digit county FIPS (e.g. '19169').
    shared_dir : Path
        Runtime shared directory (e.g. .../data-pipeline/shared).
    years : list[int] | None
        Years to include. Defaults to [2021, 2022, 2023, 2024, 2025].

    Returns
    -------
    Path
        Path to the generated or cached baseline JSON file.
    """
    if years is None:
        years = [2021, 2022, 2023, 2024, 2025]

    baseline_dir = shared_dir / "weather" / "doy_baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = baseline_dir / f"{fips_code}_t2m_baseline.json"

    if baseline_path.exists():
        return baseline_path

    doy_sums: dict[int, dict[str, float]] = {}
    doy_counts: dict[int, int] = {}

    for year in years:
        parquet_path = shared_dir / "weather" / "nasa-power" / str(year) / "daily_weather_by_fips.parquet"
        if not parquet_path.exists():
            continue
        try:
            df = pd.read_parquet(parquet_path)
        except Exception:
            continue

        fips_col = _resolve_fips_col(df)
        if fips_col is None:
            continue

        county_df = df[df[fips_col].astype(str).str.zfill(5) == fips_code.zfill(5)].copy()
        if county_df.empty:
            continue

        # Ensure date column exists
        date_col = None
        for c in ["date", "DATE", "Date"]:
            if c in county_df.columns:
                date_col = c
                break
        if date_col is None:
            continue
        county_df[date_col] = pd.to_datetime(county_df[date_col])
        county_df["doy"] = county_df[date_col].dt.dayofyear

        for _, row in county_df.iterrows():
            doy = int(row["doy"])
            if doy not in doy_sums:
                doy_sums[doy] = {"t2m_sum": 0.0, "t2m_max_sum": 0.0, "t2m_min_sum": 0.0}
                doy_counts[doy] = 0
            if "T2M" in county_df.columns:
                doy_sums[doy]["t2m_sum"] += float(row["T2M"])
            if "T2M_MAX" in county_df.columns:
                doy_sums[doy]["t2m_max_sum"] += float(row["T2M_MAX"])
            if "T2M_MIN" in county_df.columns:
                doy_sums[doy]["t2m_min_sum"] += float(row["T2M_MIN"])
            doy_counts[doy] += 1

    doy_stats = {}
    for doy in sorted(doy_sums.keys()):
        cnt = doy_counts[doy]
        if cnt == 0:
            continue
        s = doy_sums[doy]
        doy_stats[str(doy)] = {
            "mean_t2m": round(s["t2m_sum"] / cnt, 2),
            "mean_t2m_max": round(s["t2m_max_sum"] / cnt, 2),
            "mean_t2m_min": round(s["t2m_min_sum"] / cnt, 2),
        }

    baseline = {
        "fips": fips_code,
        "years": years,
        "doy_stats": doy_stats,
    }

    baseline_path.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    return baseline_path
