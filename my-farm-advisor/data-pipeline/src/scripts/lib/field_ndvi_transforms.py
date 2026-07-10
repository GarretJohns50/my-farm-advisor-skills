"""Extract NDVI time-series from per-scene TIFFs using manifest JSONs.

Prioritizes Sentinel-2 scenes. Landsat fills gaps only when no Sentinel
scene exists within +/-5 days of the target date. Filters by cloud cover
threshold (default 20%).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image


_CLOUD_THRESHOLD = 20.0
_SENTINEL_PRIORITY_DAYS = 5


def _parse_manifest(manifest_path: Path) -> list[dict]:
    """Parse a manifest JSON and return flat list of scene dicts."""
    if not manifest_path.exists():
        return []
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    scenes = []
    for year_rec in data.get("years", []):
        for sc in year_rec.get("scenes", []):
            if sc.get("status") != "complete":
                continue
            ndvi_path = sc.get("ndvi_tif")
            if not ndvi_path:
                continue
            scenes.append({
                "date": sc["scene_date"],
                "cloud_cover": float(sc.get("cloud_cover", 100.0)),
                "ndvi_path": ndvi_path,
                "source": data.get("dataset_name", "unknown"),
                "scene_id": sc.get("scene_id", ""),
            })
    return scenes


def _read_ndvi_mean(ndvi_path: Path, runtime_base: Path) -> float | None:
    """Read a single-band NDVI TIFF and return the mean pixel value."""
    # Paths in manifest are relative to runtime base
    full_path = runtime_base / ndvi_path
    if not full_path.exists():
        return None
    try:
        img = Image.open(full_path)
        arr = np.array(img)
        # Handle nodata (common values: -9999, 0, or NaN)
        if arr.dtype.kind in {"i", "u"}:
            # Integer type — assume 0 or negative are nodata
            mask = (arr > 0) & (arr < 10000)  # reasonable NDVI scaled integer range
            if mask.any():
                return float(arr[mask].mean())
            return None
        else:
            # Float — look for NaN and extreme values
            mask = np.isfinite(arr) & (arr > -1.0) & (arr < 1.0)
            if mask.any():
                return float(arr[mask].mean())
            return None
    except Exception:
        return None


def compute_field_ndvi_series(
    field_dir: Path,
    runtime_base: Path,
    cloud_threshold: float = _CLOUD_THRESHOLD,
) -> list[dict]:
    """Build a merged NDVI time-series for a single field.

    Parameters
    ----------
    field_dir
        Absolute path to the field directory (contains satellite/).
    runtime_base
        Absolute path to the data-pipeline runtime root (for resolving
        relative paths in manifest JSON).
    cloud_threshold
        Maximum cloud cover % to include a scene.

    Returns
    -------
    List of dicts with keys: date, doy, mean_ndvi, cloud_cover, source,
    scene_id, year. Sorted by date. Sentinel prioritized; Landsat used
    only for gaps.
    """
    satellite_dir = field_dir / "satellite"
    sentinel_manifest = satellite_dir / "sentinel" / "manifest.json"
    landsat_manifest = satellite_dir / "landsat" / "manifest.json"

    sentinel_scenes = _parse_manifest(sentinel_manifest)
    landsat_scenes = _parse_manifest(landsat_manifest)

    # Filter by cloud cover
    sentinel_scenes = [s for s in sentinel_scenes if s["cloud_cover"] <= cloud_threshold]
    landsat_scenes = [s for s in landsat_scenes if s["cloud_cover"] <= cloud_threshold]

    # Build date-index for Sentinel
    sentinel_by_date: dict[str, dict] = {}
    for s in sentinel_scenes:
        sentinel_by_date[s["date"]] = s

    # Landsat gap-fill: only add Landsat scenes when no Sentinel within +/-5 days
    merged: dict[str, dict] = dict(sentinel_by_date)
    for ls in landsat_scenes:
        ls_date = datetime.strptime(ls["date"], "%Y-%m-%d")
        has_nearby_sentinel = False
        for delta in range(-_SENTINEL_PRIORITY_DAYS, _SENTINEL_PRIORITY_DAYS + 1):
            check_date = (ls_date + timedelta(days=delta)).strftime("%Y-%m-%d")
            if check_date in sentinel_by_date:
                has_nearby_sentinel = True
                break
        if not has_nearby_sentinel:
            merged[ls["date"]] = ls

    # Read NDVI values and build output records
    results = []
    for date_str in sorted(merged.keys()):
        sc = merged[date_str]
        ndvi_path = Path(sc["ndvi_path"])
        mean_ndvi = _read_ndvi_mean(ndvi_path, runtime_base)
        if mean_ndvi is None:
            continue
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        results.append({
            "date": date_str,
            "doy": int(dt.timetuple().tm_yday),
            "mean_ndvi": round(mean_ndvi, 4),
            "cloud_cover": round(sc["cloud_cover"], 2),
            "source": sc["source"],
            "scene_id": sc["scene_id"],
            "year": dt.year,
        })

    return results


def get_crop_history(field_dir: Path) -> list[dict]:
    """Read crop history from ndvi_card_summary.json or ndvi_year_crop_join.csv.

    Returns list of dicts with year, crop_name, scene_count, peak_ndvi.
    """
    summary_path = field_dir / "derived" / "summaries" / "ndvi_yearly_summary.json"
    if summary_path.exists():
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        return [
            {
                "year": y["year"],
                "crop_name": y.get("crop_name", "Unknown"),
                "scene_count": y.get("scene_count", 0),
                "peak_ndvi": None,  # filled later from card summary
            }
            for y in data.get("years", [])
        ]

    csv_path = field_dir / "derived" / "tables" / "ndvi_year_crop_join.csv"
    if csv_path.exists():
        import pandas as pd
        df = pd.read_csv(csv_path)
        return [
            {
                "year": int(row["year"]),
                "crop_name": row.get("crop_name", "Unknown"),
                "scene_count": int(row.get("scene_count", 0)),
                "peak_ndvi": None,
            }
            for _, row in df.iterrows()
        ]

    return []
