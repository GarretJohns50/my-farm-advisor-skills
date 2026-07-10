#!/usr/bin/env python3
"""Standalone CLI for the Grower Field Weather Dashboard.

Usage:
    python dashboard_cli.py dashboard generate --farm-dir <path> [--output <path>] [--no-basemap]
    python dashboard_cli.py dashboard generate --growers-dir <path> [--output <path>]
    python dashboard_cli.py dashboard generate                     [--output <path>]

Runtime discovery (in order of precedence):
1. --farm-dir (explicit single farm)
2. --growers-dir (explicit growers directory; errors if >1 farm)
3. DATA_PIPELINE_DATA_ROOT environment variable
4. Auto-scan ~ for my-farm-advisor-runtime/data-pipeline
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

from lib.dashboard_generator import generate_dashboard
from lib.paths import DATA_ROOT
from lib.runtime_paths import resolve_runtime_paths


def _auto_discover_runtime() -> Path:
    """Find the data-pipeline runtime root under ~."""
    home = Path.home()
    candidates = list(home.rglob("my-farm-advisor-runtime/data-pipeline"))
    if not candidates:
        raise RuntimeError(
            "Could not auto-discover my-farm-advisor-runtime/data-pipeline under home directory."
        )
    if len(candidates) > 1:
        raise RuntimeError(
            f"Multiple runtime roots found: {candidates}. Please specify one explicitly."
        )
    return candidates[0]


def _resolve_runtime_base(args) -> Path:
    """Resolve the runtime base directory."""
    if args.farm_dir:
        return Path(args.farm_dir).resolve().parent.parent.parent.parent.parent
    if args.growers_dir:
        return Path(args.growers_dir).resolve().parent
    env_root = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return _auto_discover_runtime()


def _find_farm_dirs(growers_dir: Path) -> list[Path]:
    """Find all farm directories under growers_dir."""
    farms: list[Path] = []
    for grower in growers_dir.iterdir():
        if grower.is_dir():
            farms_dir = grower / "farms"
            if farms_dir.exists():
                for farm in farms_dir.iterdir():
                    if farm.is_dir() and (farm / "boundary" / "field_boundaries.geojson").exists():
                        farms.append(farm)
    return farms


def _cmd_generate(args) -> None:
    farm_dir_path: Path | None = None

    if args.farm_dir:
        farm_dir_path = Path(args.farm_dir).resolve()
        if not farm_dir_path.exists():
            print(f"ERROR: Farm directory does not exist: {farm_dir_path}")
            sys.exit(1)
        boundary = farm_dir_path / "boundary" / "field_boundaries.geojson"
        if not boundary.exists():
            print(f"ERROR: No field_boundaries.geojson in {farm_dir_path / 'boundary'}")
            sys.exit(1)
    else:
        # Need to discover growers dir then find farms
        growers_dir: Path | None = None
        if args.growers_dir:
            growers_dir = Path(args.growers_dir).resolve()
        else:
            runtime_base = _resolve_runtime_base(args)
            growers_dir = runtime_base / "growers"

        if not growers_dir.exists():
            print(f"ERROR: Growers directory not found: {growers_dir}")
            sys.exit(1)

        farms = _find_farm_dirs(growers_dir)
        if not farms:
            print(f"ERROR: No valid farms found under {growers_dir}")
            sys.exit(1)
        if len(farms) > 1:
            print(f"ERROR: Multiple farms found. Please select one explicitly with --farm-dir:")
            for f in farms:
                print(f"  {f}")
            sys.exit(1)
        farm_dir_path = farms[0]

    output_path = Path(args.output) if args.output else None

    try:
        result = generate_dashboard(
            farm_dir_path=farm_dir_path,
            output_path=output_path,
            no_basemap=args.no_basemap,
            force_basemap=args.force_basemap,
        )
        print(f"Dashboard written to: {result}")
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dashboard_cli.py",
        description="Generate self-contained weather dashboards for farm fields.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dash = sub.add_parser("dashboard", help="Dashboard operations")
    dash_sub = dash.add_subparsers(dest="dash_cmd", required=True)

    gen = dash_sub.add_parser("generate", help="Generate a weather dashboard")
    gen.add_argument("--farm-dir", type=str, default=None,
                     help="Explicit path to a single farm directory")
    gen.add_argument("--growers-dir", type=str, default=None,
                     help="Path to growers directory (errors if >1 farm)")
    gen.add_argument("--output", type=str, default=None,
                     help="Output HTML file path")
    gen.add_argument("--no-basemap", action="store_true",
                     help="Skip satellite basemap, use neutral background")
    gen.add_argument("--force-basemap", action="store_true",
                     help="Ignore tile cache and re-download")
    gen.set_defaults(func=_cmd_generate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
