"""NDVI quality control: SCL-based cloud masking and temporal anomaly detection.

Layer 1: Sentinel-2 SCL (Scene Classification Layer) pixel-level masking
Layer 2: Temporal consistency check against seasonal trend
"""

from __future__ import annotations

import numpy as np
from pathlib import Path
from PIL import Image


# Sentinel-2 SCL valid codes (clear pixels)
_SCL_VALID = {4, 5, 6}  # vegetation, bare soil, water
# SCL mask codes (clouds, shadows, cirrus)
_SCL_MASK = {3, 7, 8, 9, 10}
# Minimum clear fraction to keep a scene (75% = reject >25% masked)
_MIN_CLEAR_FRACTION = 0.75


def _read_tif_band(tif_path: Path) -> np.ndarray | None:
    """Read a single-band GeoTIFF as numpy array."""
    if not tif_path.exists():
        return None
    try:
        img = Image.open(tif_path)
        arr = np.array(img)
        return arr
    except Exception:
        return None


def compute_cloud_masked_ndvi(
    ndvi_tif: Path,
    scl_tif: Path,
) -> tuple[float | None, float]:
    """Compute cloud-masked NDVI mean using Sentinel-2 SCL band.

    Parameters
    ----------
    ndvi_tif : Path
        Path to single-band NDVI GeoTIFF.
    scl_tif : Path
        Path to single-band SCL (Scene Classification Layer) GeoTIFF.

    Returns
    -------
    tuple[float | None, float]
        (clear_mean_ndvi, clear_fraction). If clear_fraction < _MIN_CLEAR_FRACTION,
        clear_mean_ndvi is None (scene should be rejected).
    """
    ndvi_arr = _read_tif_band(ndvi_tif)
    scl_arr = _read_tif_band(scl_tif)

    if ndvi_arr is None or scl_arr is None:
        return None, 0.0

    if ndvi_arr.shape != scl_arr.shape:
        # Shape mismatch — cannot mask pixel-by-pixel
        return None, 0.0

    # Build valid pixel mask from SCL
    valid_mask = np.isin(scl_arr, list(_SCL_VALID))

    # Also mask invalid NDVI values
    if ndvi_arr.dtype.kind in {"i", "u"}:
        ndvi_valid = (ndvi_arr > 0) & (ndvi_arr < 10000)
    else:
        ndvi_valid = np.isfinite(ndvi_arr) & (ndvi_arr > -1.0) & (ndvi_arr < 1.0)

    combined_mask = valid_mask & ndvi_valid
    clear_count = int(np.count_nonzero(combined_mask))
    total_count = int(ndvi_arr.size)
    clear_fraction = clear_count / total_count if total_count > 0 else 0.0

    if clear_fraction < _MIN_CLEAR_FRACTION:
        return None, clear_fraction

    clear_mean = float(ndvi_arr[combined_mask].mean())
    return clear_mean, clear_fraction


def flag_temporal_anomalies(
    series: list[dict],
    threshold: float = 0.15,
) -> list[dict]:
    """Flag NDVI scenes that deviate >threshold from the seasonal trend line.

    Fits a 2nd-degree polynomial (quadratic) on DOY vs NDVI for the year,
    then marks each scene where |observed - expected| > threshold.

    Parameters
    ----------
    series : list[dict]
        List of NDVI records with keys "doy" and "mean_ndvi".
    threshold : float
        Absolute deviation threshold (default 0.15).

    Returns
    -------
    list[dict]
        Same records augmented with "temporal_flag" (bool) and
        "expected_ndvi" (float) keys.
    """
    if len(series) < 4:
        # Not enough points for a meaningful trend
        for rec in series:
            rec["temporal_flag"] = False
            rec["expected_ndvi"] = rec["mean_ndvi"]
        return series

    doys = np.array([r["doy"] for r in series], dtype=float)
    ndvis = np.array([r["mean_ndvi"] for r in series], dtype=float)

    # Fit quadratic: NDVI = a*DOY^2 + b*DOY + c
    coeffs = np.polyfit(doys, ndvis, 2)
    poly = np.poly1d(coeffs)

    for rec in series:
        expected = float(poly(rec["doy"]))
        rec["expected_ndvi"] = round(expected, 4)
        deviation = abs(rec["mean_ndvi"] - expected)
        rec["temporal_flag"] = deviation > threshold

    return series
