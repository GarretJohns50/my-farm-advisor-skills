#!/usr/bin/env python3
"""Standalone CLI to generate a field sampling grid.

Usage:
    python generate_field_grid.py \
        --boundary /path/to/field_boundary.geojson \
        --zone-acres 2.5 \
        --target-count 57 \
        --output /path/to/sampling_grid.geojson
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

from lib.field_grid_generator import generate_field_grid, save_field_grid


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="generate_field_grid.py",
        description="Generate a regular sampling grid inside a field boundary.",
    )
    parser.add_argument(
        "--boundary", type=str, required=True,
        help="Path to field boundary GeoJSON",
    )
    parser.add_argument(
        "--zone-acres", type=float, default=2.5,
        help="Target zone size in acres (default: 2.5)",
    )
    parser.add_argument(
        "--target-count", type=int, default=57,
        help="Exact number of grid points to generate (default: 57)",
    )
    parser.add_argument(
        "--output", type=str, required=True,
        help="Output GeoJSON path for the grid",
    )

    args = parser.parse_args()

    boundary = Path(args.boundary)
    if not boundary.exists():
        print(f"ERROR: Boundary file not found: {boundary}")
        sys.exit(1)

    try:
        grid_gdf = generate_field_grid(
            boundary_path=boundary,
            zone_acres=args.zone_acres,
            target_count=args.target_count,
        )
        out = save_field_grid(grid_gdf, args.output)
        print(f"Saved {len(grid_gdf)} grid points to: {out}")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
