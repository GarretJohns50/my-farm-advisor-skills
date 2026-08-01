#!/usr/bin/env python3
"""Enrich a field sampling grid GeoJSON with soil test data from an Excel file.

Usage:
    python enrich_grid_with_soil.py \
        --grid /path/to/sampling_grid.geojson \
        --soil /path/to/Field 1.xlsx \
        --output /path/to/sampling_grid.geojson
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd


def _read_soil_excel(path: str | Path) -> pd.DataFrame:
    """Read the soil test Excel, using the second row as the actual header."""
    path = Path(path)
    df = pd.read_excel(path, sheet_name=0, header=1)
    # Rename samp_id to point_id for join
    if "samp_id" in df.columns:
        df = df.rename(columns={"samp_id": "point_id"})
    df["point_id"] = pd.to_numeric(df["point_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["point_id"])
    df["point_id"] = df["point_id"].astype(int)
    return df


def enrich_grid(
    grid_path: str | Path,
    soil_path: str | Path,
    output_path: str | Path | None = None,
) -> Path:
    """Merge soil test data into a grid GeoJSON and overwrite (or save to output)."""
    grid_path = Path(grid_path)
    soil_path = Path(soil_path)

    if not grid_path.exists():
        raise FileNotFoundError(f"Grid not found: {grid_path}")
    if not soil_path.exists():
        raise FileNotFoundError(f"Soil Excel not found: {soil_path}")

    grid_gdf = gpd.read_file(str(grid_path))
    soil_df = _read_soil_excel(soil_path)

    # Drop any existing soil columns to avoid duplication on re-runs
    soil_cols = [c for c in soil_df.columns if c != "point_id"]
    for c in soil_cols:
        if c in grid_gdf.columns:
            grid_gdf = grid_gdf.drop(columns=[c])

    # Merge on point_id
    grid_gdf["point_id"] = grid_gdf["point_id"].astype(int)
    merged = grid_gdf.merge(soil_df, on="point_id", how="left")

    if output_path is None:
        output_path = grid_path
    else:
        output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_file(str(output_path), driver="GeoJSON")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="enrich_grid_with_soil.py",
        description="Enrich a sampling grid GeoJSON with soil test Excel data.",
    )
    parser.add_argument("--grid", type=str, required=True, help="Input grid GeoJSON path")
    parser.add_argument("--soil", type=str, required=True, help="Soil test Excel path")
    parser.add_argument("--output", type=str, default=None, help="Output path (default: overwrite --grid)")
    args = parser.parse_args()

    try:
        out = enrich_grid(args.grid, args.soil, args.output)
        print(f"Enriched grid written to: {out}")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
