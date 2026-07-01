#!/usr/bin/env python3
# pyright: reportCallIssue=false
"""
05_eda_overview.py - Combined data overview

Creates an overview of all downloaded data and saves summary statistics.

Input:  All downloaded data (fields, soil, weather, CDL)
Output: growers/default-grower/farms/default-farm/derived/summaries/iowa_field_summary.csv under the runtime root, summary stats
"""

import argparse
import os
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from lib.paths import (  # noqa: E402
    farm_boundary_path,
    farm_cdl_year_table_path,
    farm_soil_sample_path,
    farm_summaries_dir,
    farm_weather_path,
)

_DEFAULT_GROWER = os.environ.get("AG_GROWER_SLUG", "default-grower")
_DEFAULT_FARM = os.environ.get("AG_FARM_SLUG", "default-farm")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grower-slug", default=_DEFAULT_GROWER)
    parser.add_argument("--farm-slug", default=_DEFAULT_FARM)
    return parser.parse_args()


def main(args: argparse.Namespace | None = None) -> pd.DataFrame:
    if args is None:
        args = parse_args()

    print("=" * 60)
    print("Step 5: Data Overview")
    print("=" * 60)
    print(f"Grower: {args.grower_slug} | Farm: {args.farm_slug}")

    summaries_dir = farm_summaries_dir(args.grower_slug, args.farm_slug)
    summaries_dir.mkdir(parents=True, exist_ok=True)

    # Load all data
    fields = gpd.read_file(farm_boundary_path(args.grower_slug, args.farm_slug))
    soil_path = farm_soil_sample_path(args.grower_slug, args.farm_slug)
    soil = pd.read_csv(soil_path) if soil_path.exists() else pd.DataFrame()
    weather = pd.read_csv(
        farm_weather_path(args.grower_slug, args.farm_slug),
        parse_dates=["date"],
    )
    cdl_2023 = pd.read_csv(farm_cdl_year_table_path(args.grower_slug, args.farm_slug, 2023))
    cdl_2024 = pd.read_csv(farm_cdl_year_table_path(args.grower_slug, args.farm_slug, 2024))

    print("\n=== Data Summary ===")
    print(f"Fields: {len(fields)} fields")
    print(f"  Total area: {fields['area_acres'].sum():.1f} acres")
    print(f"  Crops (OSM): {fields['crop_name'].value_counts().to_dict()}")

    if not soil.empty:
        print(f"\nSoil: {len(soil)} records for {soil['field_id'].nunique()} fields")
        if "muname" in soil.columns:
            print(f"  Dominant soil types: {soil.groupby('field_id').first()['muname'].to_dict()}")
    else:
        print("\nSoil: no soil data available")

    print(f"\nWeather: {len(weather)} daily records")
    print(f"  Date range: {weather['date'].min().date()} to {weather['date'].max().date()}")
    print(f"  Mean temp: {weather['T2M'].mean():.1f}C")
    print(f"  Total precip: {weather['PRECTOTCORR'].sum():.1f} mm")

    print(f"\nCDL 2023: {len(cdl_2023)} fields")
    print(f"  Crop types: {cdl_2023['crop_name'].value_counts().to_dict()}")

    # Create merged summary
    if not soil.empty and "comppct_r" in soil.columns:
        dominant_soil = (
            soil.sort_values(["field_id", "comppct_r"], ascending=[True, False])
            .groupby("field_id")
            .first()
            .reset_index()
        )
    else:
        dominant_soil = pd.DataFrame(columns=["field_id"])

    summary = fields.copy()
    if not dominant_soil.empty and {"field_id", "muname", "compname", "om_r", "ph1to1h2o_r", "cec7_r", "drainagecl"}.issubset(dominant_soil.columns):
        summary = summary.merge(
            dominant_soil[
                ["field_id", "muname", "compname", "om_r", "ph1to1h2o_r", "cec7_r", "drainagecl"]
            ],
            on="field_id",
            how="left",
        )

    if {"field_id", "crop_name", "pct"}.issubset(cdl_2023.columns):
        cdl23_top = (
            cdl_2023.sort_values(["field_id", "pct"], ascending=[True, False])
            .groupby("field_id")
            .first()
            .reset_index()
        )
        summary = summary.merge(
            cdl23_top[["field_id", "crop_name", "pct"]].rename(
                columns={"crop_name": "cdl_2023", "pct": "cdl_2023_pct"}
            ),
            on="field_id",
            how="left",
        )

    if {"field_id", "crop_name", "pct"}.issubset(cdl_2024.columns):
        cdl24_top = (
            cdl_2024.sort_values(["field_id", "pct"], ascending=[True, False])
            .groupby("field_id")
            .first()
            .reset_index()
        )
        summary = summary.merge(
            cdl24_top[["field_id", "crop_name", "pct"]].rename(
                columns={"crop_name": "cdl_2024", "pct": "cdl_2024_pct"}
            ),
            on="field_id",
            how="left",
        )

    output_path = summaries_dir / f"{args.farm_slug.replace('-', '_')}_field_summary.csv"
    summary.to_csv(output_path, index=False)
    print(f"\n✓ Saved: {output_path}")

    return summary


if __name__ == "__main__":
    main(parse_args())
