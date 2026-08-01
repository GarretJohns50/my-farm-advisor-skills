#!/usr/bin/env python3
"""Standalone CLI for the field NDVI dashboard.

Usage:
    python field_dashboard_cli.py field-dashboard generate \
        --grower-slug central-ne-grower \
        --farm-slug central-ne-grower-nebraska \
        --field-id osm-554305501 \
        [--no-basemap]

    # Combined multi-field dashboard with a Field selector
    python field_dashboard_cli.py field-dashboard generate \
        --grower-slug central-ne-grower \
        --farm-slug central-ne-grower-nebraska \
        --field-ids osm-554305501,osm-554305653 \
        [--no-basemap]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

from lib.field_dashboard_generator import generate_field_dashboard, generate_multi_field_dashboard
from lib.paths import farm_dir


def _cmd_generate(args) -> None:
    farm_path = farm_dir(args.grower_slug, args.farm_slug)
    if not farm_path.exists():
        print(f"ERROR: Farm directory not found: {farm_path}")
        sys.exit(1)

    boundary = farm_path / "boundary" / "field_boundaries.geojson"
    if not boundary.exists():
        print(f"ERROR: No field_boundaries.geojson in {farm_path / 'boundary'}")
        sys.exit(1)

    output = Path(args.output) if args.output else None

    try:
        if args.field_ids:
            field_ids = [fid.strip() for fid in args.field_ids.split(",") if fid.strip()]
            if not field_ids:
                print("ERROR: --field-ids must be a non-empty comma-separated list")
                sys.exit(1)
            result = generate_multi_field_dashboard(
                farm_dir_path=farm_path,
                field_ids=field_ids,
                output_path=output,
                no_basemap=args.no_basemap,
                force_basemap=args.force_basemap,
            )
            print(f"Combined field dashboard written to: {result}")
            return
        if not args.field_id:
            print("ERROR: either --field-id or --field-ids is required")
            sys.exit(1)
        result = generate_field_dashboard(
            farm_dir_path=farm_path,
            field_id=args.field_id,
            output_path=output,
            no_basemap=args.no_basemap,
            force_basemap=args.force_basemap,
        )
        print(f"Field dashboard written to: {result}")
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="field_dashboard_cli.py",
        description="Generate a single-field NDVI dashboard.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fd = sub.add_parser("field-dashboard", help="Field dashboard operations")
    fd_sub = fd.add_subparsers(dest="fd_cmd", required=True)

    gen = fd_sub.add_parser("generate", help="Generate a field dashboard")
    gen.add_argument("--grower-slug", type=str, required=True, help="Grower slug")
    gen.add_argument("--farm-slug", type=str, required=True, help="Farm slug")
    gen.add_argument("--field-id", type=str, default=None, help="Field ID for a single-field dashboard")
    gen.add_argument("--field-ids", type=str, default=None, help="Comma-separated field IDs for a combined multi-field dashboard")
    gen.add_argument("--output", type=str, default=None, help="Output HTML file path")
    gen.add_argument("--no-basemap", action="store_true", help="Skip satellite basemap")
    gen.add_argument("--force-basemap", action="store_true", help="Re-download basemap tiles")
    gen.set_defaults(func=_cmd_generate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
