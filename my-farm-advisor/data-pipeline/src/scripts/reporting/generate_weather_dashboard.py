#!/usr/bin/env python3
"""Pipeline step: generate the weather dashboard.

Called by run_farm_pipeline.py when --dashboard is enabled.
Reads environment variables set by the pipeline.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

from lib.dashboard_generator import generate_dashboard
from lib.paths import farm_dir


def main() -> None:
    grower = os.environ.get("AG_GROWER_SLUG", "")
    farm = os.environ.get("AG_FARM_SLUG", "")
    if not grower or not farm:
        print("ERROR: AG_GROWER_SLUG and AG_FARM_SLUG must be set.")
        sys.exit(1)

    farm_path = farm_dir(grower, farm)
    if not farm_path.exists():
        print(f"ERROR: Farm directory not found: {farm_path}")
        sys.exit(1)

    no_basemap = os.environ.get("AG_NO_BASEMAP", "0") == "1"
    force_basemap = os.environ.get("AG_FORCE_BASEMAP", "0") == "1"

    output = generate_dashboard(
        farm_dir_path=farm_path,
        no_basemap=no_basemap,
        force_basemap=force_basemap,
    )
    print(f"Dashboard generated: {output}")


if __name__ == "__main__":
    main()
