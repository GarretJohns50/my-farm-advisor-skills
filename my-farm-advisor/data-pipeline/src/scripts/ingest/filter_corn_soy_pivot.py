#!/usr/bin/env python3
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
"""Filter farm fields to top corn/soy center-pivot fields.

Reads CDL tables (2021-2025), keeps fields where Corn+Soybeans dominate >75% of
years. Computes polygon circularity to detect center-pivot irrigation shapes
(circularity >= 0.75). Ranks by combined score and selects top N.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from runtime_paths import resolve_runtime_paths  # noqa: E402

_RUNTIME_PATHS = resolve_runtime_paths()
_REPO = _RUNTIME_PATHS.runtime_base
_SCRIPTS = _RUNTIME_PATHS.runtime_scripts
_LIB = _RUNTIME_PATHS.runtime_scripts / "lib"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_LIB))

from naming import field_slug_from_id  # noqa: E402
from paths import (  # noqa: E402
    farm_boundary_path,
    farm_cdl_year_table_path,
    farm_manifest_dir,
)

_DEFAULT_GROWER = os.environ.get("AG_GROWER_SLUG", "default-grower")
_DEFAULT_FARM = os.environ.get("AG_FARM_SLUG", "default-farm")

_CORN_SOY_CROPS = {"Corn", "Soybeans"}
_MIN_CORN_SOY_RATIO = 0.75
_MIN_CIRCULARITY = 0.75


def _dominant_crop_per_year(cdl_df: pd.DataFrame, field_id: str) -> dict[int, str]:
    """Return {year: dominant_crop_name} for a field."""
    field_cdl = cdl_df[cdl_df["field_id"] == field_id]
    if field_cdl.empty:
        return {}
    # Group by year, get crop with max pct per year
    result: dict[int, str] = {}
    for year, group in field_cdl.groupby("year"):
        top = group.loc[group["pct"].idxmax()]
        result[int(year)] = str(top["crop_name"])
    return result


def _corn_soy_score(dominant_by_year: dict[int, str]) -> float:
    """Fraction of years where dominant crop is Corn or Soybeans."""
    if not dominant_by_year:
        return 0.0
    corn_soy_years = sum(1 for crop in dominant_by_year.values() if crop in _CORN_SOY_CROPS)
    return corn_soy_years / len(dominant_by_year)


def _circularity(geom, crs) -> float:
    """Compute (4 * pi * area) / perimeter^2 for a polygon.

    Both area and perimeter are computed in projected units (EPSG:5070)
    so the ratio is unitless and comparable across fields.
    """
    if geom is None or geom.is_empty:
        return 0.0
    # Reproject to equal-area for both area and perimeter
    temp = gpd.GeoDataFrame([{"geometry": geom}], crs=crs).to_crs("EPSG:5070")
    area = temp.geometry.area.iloc[0]
    perimeter = temp.geometry.length.iloc[0]
    if perimeter == 0:
        return 0.0
    return (4.0 * 3.14159265359 * area) / (perimeter ** 2)


def _load_all_cdl(grower_slug: str, farm_slug: str) -> pd.DataFrame:
    """Load and concatenate all annual CDL tables for the farm."""
    frames: list[pd.DataFrame] = []
    for year in range(2021, 2026):
        path = farm_cdl_year_table_path(grower_slug, farm_slug, year)
        if path.exists():
            df = pd.read_csv(path)
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["field_id", "year", "crop_name", "pct"])
    return pd.concat(frames, ignore_index=True)


def _write_inventory(path: Path, field_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["field_id", "field_slug"])
        for fid in field_ids:
            writer.writerow([fid, field_slug_from_id(fid)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grower-slug", default=_DEFAULT_GROWER)
    parser.add_argument("--farm-slug", default=_DEFAULT_FARM)
    parser.add_argument("--top-n", type=int, default=10, help="Number of fields to keep")
    parser.add_argument(
        "--min-corn-soy",
        type=float,
        default=_MIN_CORN_SOY_RATIO,
        help="Min fraction of years dominated by corn or soybeans",
    )
    parser.add_argument(
        "--min-circularity",
        type=float,
        default=_MIN_CIRCULARITY,
        help="Min circularity score (0-1, 1=perfect circle)",
    )
    parser.add_argument(
        "--run-pipeline",
        action="store_true",
        help="Re-run farm pipeline after filtering",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Corn/Soy Center-Pivot Field Filter")
    print("=" * 60)
    print(f"Grower: {args.grower_slug} | Farm: {args.farm_slug}")
    print(f"Filters: corn/soy >= {args.min_corn_soy:.0%}, circularity >= {args.min_circularity:.2f}")

    # Load boundary
    boundary_path = farm_boundary_path(args.grower_slug, args.farm_slug)
    if not boundary_path.exists():
        print(f"Boundary not found: {boundary_path}")
        sys.exit(1)

    gdf = gpd.read_file(boundary_path)
    print(f"Input: {len(gdf)} fields")

    # Load all CDL data
    cdl_df = _load_all_cdl(args.grower_slug, args.farm_slug)
    if cdl_df.empty:
        print("No CDL data found. Exiting.")
        sys.exit(1)

    # Score each field
    gdf_crs = gdf.crs
    scores: list[dict] = []
    for idx, row in gdf.iterrows():
        field_id = str(row["field_id"])
        dominant = _dominant_crop_per_year(cdl_df, field_id)
        crop_score = _corn_soy_score(dominant)
        circ = _circularity(row.geometry, gdf_crs)
        area_ac = float(row.get("area_acres", 0) or 0)

        scores.append({
            "field_id": field_id,
            "crop_score": crop_score,
            "circularity": circ,
            "area_acres": area_ac,
            "dominant_by_year": dominant,
            "rank_score": crop_score * circ * max(area_ac, 1.0),
        })

    # Sort by rank score descending
    scores.sort(key=lambda x: x["rank_score"], reverse=True)

    # Show all scores
    print("\nAll fields ranked:")
    print(f"{'Rank':>4} {'Field ID':<18} {'CropScore':>9} {'Circularity':>11} {'Area(ac)':>8} {'Score':>8} {'Keep':>4}")
    for i, s in enumerate(scores, 1):
        keep = "YES" if s["crop_score"] >= args.min_corn_soy and s["circularity"] >= args.min_circularity else "no"
        print(f"{i:>4} {s['field_id']:<18} {s['crop_score']:>8.1%} {s['circularity']:>10.3f} {s['area_acres']:>7.1f} {s['rank_score']:>7.1f} {keep:>4}")

    # Filter and select top N
    qualified = [
        s for s in scores
        if s["crop_score"] >= args.min_corn_soy and s["circularity"] >= args.min_circularity
    ]

    selected = qualified[:args.top_n]
    selected_ids = [s["field_id"] for s in selected]

    print(f"\nQualified: {len(qualified)} fields meet both thresholds")
    print(f"Selected:  {len(selected)} fields (top {args.top_n})")

    if len(selected) < args.top_n:
        print(f"WARNING: Only {len(selected)} of {args.top_n} requested fields qualified")

    # Filter GeoDataFrame to selected fields only
    filtered_gdf = gdf[gdf["field_id"].isin(selected_ids)].copy()
    filtered_gdf = filtered_gdf.sort_values("field_id").reset_index(drop=True)

    # Write new boundary
    filtered_gdf.to_file(boundary_path, driver="GeoJSON")
    print(f"\n✓ Rewrote boundary → {boundary_path} ({len(filtered_gdf)} fields)")

    # Write new inventory
    inventory_path = farm_manifest_dir(args.grower_slug, args.farm_slug) / "field-inventory.csv"
    _write_inventory(inventory_path, selected_ids)
    print(f"✓ Rewrote inventory → {inventory_path}")

    # Summary JSON
    summary = {
        "grower_slug": args.grower_slug,
        "farm_slug": args.farm_slug,
        "input_count": len(gdf),
        "qualified_count": len(qualified),
        "selected_count": len(selected),
        "selected_fields": selected_ids,
        "filters": {
            "min_corn_soy": args.min_corn_soy,
            "min_circularity": args.min_circularity,
            "top_n": args.top_n,
        },
    }
    print(json.dumps(summary, indent=2))

    if args.run_pipeline:
        print("\nRe-running farm pipeline for filtered fields...")
        import subprocess
        cmd = [
            sys.executable,
            str(_SCRIPTS / "run_farm_pipeline.py"),
            "--boundaries", str(boundary_path),
            "--grower-slug", args.grower_slug,
            "--farm-slug", args.farm_slug,
            "--farm-name", os.environ.get("AG_FARM_NAME", args.farm_slug.replace("-", " ").title()),
            "--inventory-csv", str(inventory_path),
            "--force",
        ]
        subprocess.run(cmd, cwd=str(_REPO), check=True)


if __name__ == "__main__":
    main()
