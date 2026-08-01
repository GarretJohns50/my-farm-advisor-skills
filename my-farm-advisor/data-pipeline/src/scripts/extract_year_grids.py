#!/usr/bin/env python3
"""Extract year-specific soil grid data from a zip file and create enriched GeoJSONs.

Usage:
    python extract_year_grids.py \
        --zip /path/to/Field\ 1\ grid\ data.zip \
        --base-grid /path/to/sampling_grid.geojson \
        --output-dir /path/to/grids/
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd


def _read_soil_excel(path: str | Path) -> pd.DataFrame:
    """Read soil test Excel with header in row 2."""
    df = pd.read_excel(path, sheet_name=0, header=1)
    if "samp_id" in df.columns:
        df = df.rename(columns={"samp_id": "point_id"})
    df["point_id"] = pd.to_numeric(df["point_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["point_id"])
    df["point_id"] = df["point_id"].astype(int)
    return df


def extract_year_grids(
    zip_path: str | Path,
    base_grid_path: str | Path,
    output_dir: str | Path,
) -> list[Path]:
    """Extract zip, join each year's Excel to base grid, save year-specific GeoJSONs."""
    zip_path = Path(zip_path)
    base_grid_path = Path(base_grid_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not zip_path.exists():
        raise FileNotFoundError(f"Zip file not found: {zip_path}")
    if not base_grid_path.exists():
        raise FileNotFoundError(f"Base grid not found: {base_grid_path}")

    # Extract zip to temp location
    extract_dir = output_dir / "_temp_extract"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(extract_dir)

    # Load base grid
    base_gdf = gpd.read_file(str(base_grid_path))

    # Find Excel files
    excel_files = sorted(extract_dir.glob("Field_1_*.xlsx"))
    if not excel_files:
        raise ValueError(f"No Field_1_*.xlsx files found in {extract_dir}")

    created: list[Path] = []
    for excel_path in excel_files:
        # Extract year from filename (e.g., Field_1_2021.xlsx -> 2021)
        year_str = excel_path.stem.split("_")[-1]
        try:
            year = int(year_str)
        except ValueError:
            print(f"Skipping {excel_path.name} — cannot parse year")
            continue

        soil_df = _read_soil_excel(excel_path)

        # Drop any existing soil columns from base to avoid duplication
        soil_cols = [c for c in soil_df.columns if c != "point_id"]
        merged = base_gdf.copy()
        for c in soil_cols:
            if c in merged.columns:
                merged = merged.drop(columns=[c])

        merged["point_id"] = merged["point_id"].astype(int)
        merged = merged.merge(soil_df, on="point_id", how="left")

        # Add year column
        merged["year"] = year

        out_path = output_dir / f"sampling_grid_{year}.geojson"
        merged.to_file(str(out_path), driver="GeoJSON")
        created.append(out_path)
        print(f"Created: {out_path} ({len(merged)} points, {len(soil_cols)} soil properties)")

    # Clean up temp extract dir
    import shutil
    shutil.rmtree(extract_dir, ignore_errors=True)

    return created


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="extract_year_grids.py",
        description="Extract year-specific soil grids from a zip file.",
    )
    parser.add_argument("--zip", type=str, required=True, help="Path to zip file with year Excel files")
    parser.add_argument("--base-grid", type=str, required=True, help="Path to base sampling_grid.geojson")
    parser.add_argument("--output-dir", type=str, required=True, help="Output directory for year GeoJSONs")
    args = parser.parse_args()

    try:
        out_paths = extract_year_grids(args.zip, args.base_grid, args.output_dir)
        print(f"\nDone. Created {len(out_paths)} year-specific grid files.")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
