"""Extract NDVI time-series from Sentinel-2 per-scene TIFFs using manifest JSON.

Uses Sentinel-2 data exclusively. No Landsat fallback. Filters by cloud
cover threshold (default 20%).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from lib.ndvi_quality import compute_cloud_masked_ndvi, flag_temporal_anomalies

_CLOUD_THRESHOLD = 20.0


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
                "raw_tiffs": sc.get("raw_tiffs", {}),
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
    """Build an NDVI time-series from Sentinel-2 for a single field.

    Parameters
    ----------
    field_dir
        Absolute path to the field directory (contains satellite/sentinel/).    runtime_base
        Absolute path to the data-pipeline runtime root (for resolving
        relative paths in manifest JSON).
    cloud_threshold
        Maximum cloud cover % to include a scene.

    Returns
    -------
    List of dicts with keys: date, doy, mean_ndvi, cloud_cover, source,
    scene_id, year. Sorted by date. Sentinel-2 only.
    """
    satellite_dir = field_dir / "satellite"
    sentinel_manifest = satellite_dir / "sentinel" / "manifest.json"

    sentinel_scenes = _parse_manifest(sentinel_manifest)

    # Filter by cloud cover
    sentinel_scenes = [s for s in sentinel_scenes if s["cloud_cover"] <= cloud_threshold]

    # Build date-index
    sentinel_by_date: dict[str, dict] = {}
    for s in sentinel_scenes:
        sentinel_by_date[s["date"]] = s

    # Read NDVI values with SCL cloud masking and build output records
    results = []
    for date_str in sorted(sentinel_by_date.keys()):
        sc = sentinel_by_date[date_str]
        ndvi_path = runtime_base / sc["ndvi_path"]

        # Try SCL-based cloud masking first
        raw_tiffs = sc.get("raw_tiffs", {})
        scl_rel = raw_tiffs.get("scl")
        masked = False
        clear_fraction = 1.0

        if scl_rel:
            scl_path = runtime_base / scl_rel
            mean_ndvi, clear_fraction = compute_cloud_masked_ndvi(ndvi_path, scl_path)
            if mean_ndvi is None:
                masked = True
        else:
            # Fallback: read raw NDVI without SCL masking
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
            "clear_fraction": round(clear_fraction, 3),
            "masked": masked,
            "temporal_flag": False,
            "expected_ndvi": round(mean_ndvi, 4),
        })

    # Layer 3: temporal consistency check per year
    years = sorted({r["year"] for r in results})
    for yr in years:
        year_records = [r for r in results if r["year"] == yr]
        year_records = flag_temporal_anomalies(year_records)
        # Update in place
        for rec in year_records:
            orig = next(r for r in results if r["date"] == rec["date"])
            orig["temporal_flag"] = rec["temporal_flag"]
            orig["expected_ndvi"] = rec["expected_ndvi"]

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
