"""Parse sufficiency ranges Excel and provide rating functions.

Usage:
    from lib.sufficiency_ranges import load_sufficiency_ranges, rate_nutrient_value

    ranges = load_sufficiency_ranges("path/to/sufficiancy ranges.xlsx")
    category, color = rate_nutrient_value(ranges, "pH", 6.3)
    # category = "Optimal", color = "#2ca02c"
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


# Colorblind-safe palette for sufficiency categories
SUFFICIENCY_COLORS = {
    "Very Low": "#d62728",   # red
    "Low": "#ff7f0e",        # orange
    "Optimal": "#2ca02c",    # green
    "High": "#bcbd22",       # yellow-green
    "Very High": "#9467bd",  # purple
}

# Priority scores for sorting (higher = more urgent)
SUFFICIENCY_SCORES = {
    "Very Low": 4,
    "Low": 3,
    "Very High": 2,
    "High": 1,
    "Optimal": 0,
}

# Default top-5 order when all are optimal
DEFAULT_TOP5_ORDER = ["pH", "P", "K", "S", "ZN"]

# Nutrients to display (those with defined ranges)
DISPLAY_NUTRIENTS = [
    "pH", "OM", "P", "K", "S", "ZN", "MG",
    "K_SAT", "CA_SAT", "MG_SAT", "NA_SAT", "NO3",
]


def _parse_range_str(s: str) -> tuple[float | None, float | None]:
    """Parse a range string like '<5.5', '5.6-6.1', '>7.7', '<.5'.

    Returns (min, max) where None means unbounded.
    """
    if pd.isna(s) or s == "NaN" or str(s).strip() == "":
        return (None, None)
    s = str(s).strip().replace(" ", "")
    # Handle <X format
    if s.startswith("<"):
        val = s[1:]
        try:
            return (None, float(val))
        except ValueError:
            return (None, None)
    # Handle >X format
    if s.startswith(">"):
        val = s[1:]
        try:
            return (float(val), None)
        except ValueError:
            return (None, None)
    # Handle X-Y format
    if "-" in s:
        parts = s.split("-", 1)
        try:
            return (float(parts[0]), float(parts[1]))
        except ValueError:
            return (None, None)
    # Fallback: try as single number
    try:
        return (float(s), float(s))
    except ValueError:
        return (None, None)


def load_sufficiency_ranges(path: str | Path) -> dict[str, dict[str, tuple[float | None, float | None]]]:
    """Load sufficiency ranges from Excel.

    Returns: {nutrient_name: {category: (min, max)}}
    """
    path = Path(path)
    df = pd.read_excel(path, sheet_name=0, header=0)
    ranges: dict[str, dict[str, tuple[float | None, float | None]]] = {}
    for col in df.columns:
        if col == "Ranges":
            continue
        ranges[col] = {}
        for _, row in df.iterrows():
            category = row["Ranges"]
            val = row[col]
            ranges[col][category] = _parse_range_str(val)
    return ranges


def rate_nutrient_value(
    ranges: dict[str, dict[str, tuple[float | None, float | None]]],
    nutrient: str,
    value: float,
) -> tuple[str, str, float, float | None]:
    """Rate a nutrient value against sufficiency ranges.

    Returns: (category, color, score, distance_from_optimal)
    - category: "Very Low", "Low", "Optimal", "High", "Very High"
    - color: hex color for the category
    - score: priority score (higher = more urgent)
    - distance_from_optimal: absolute deviation from optimal midpoint (for tie-breaking)
    """
    if nutrient not in ranges:
        return ("—", "#999999", 0, None)

    nutrient_ranges = ranges[nutrient]

    # Find the matching category
    category = "—"
    for cat in ["Very Low", "Low", "Optimal", "High", "Very High"]:
        if cat not in nutrient_ranges:
            continue
        min_val, max_val = nutrient_ranges[cat]
        if min_val is None and max_val is not None:
            if value < max_val:
                category = cat
                break
        elif max_val is None and min_val is not None:
            if value > min_val:
                category = cat
                break
        elif min_val is not None and max_val is not None:
            if min_val <= value <= max_val:
                category = cat
                break

    if category == "—":
        # Fallback: check boundaries
        # If less than Very Low max, it's Very Low
        if "Very Low" in nutrient_ranges:
            vl_min, vl_max = nutrient_ranges["Very Low"]
            if vl_max is not None and value < vl_max:
                category = "Very Low"
        # If greater than Very High min, it's Very High
        if category == "—" and "Very High" in nutrient_ranges:
            vh_min, vh_max = nutrient_ranges["Very High"]
            if vh_min is not None and value > vh_min:
                category = "Very High"

    color = SUFFICIENCY_COLORS.get(category, "#999999")
    score = SUFFICIENCY_SCORES.get(category, 0)

    # Compute distance from optimal midpoint for tie-breaking
    distance = None
    if "Optimal" in nutrient_ranges and category != "—":
        opt_min, opt_max = nutrient_ranges["Optimal"]
        if opt_min is not None and opt_max is not None:
            midpoint = (opt_min + opt_max) / 2
            distance = abs(value - midpoint)

    return (category, color, score, distance)


def get_nutrient_ratings(
    ranges: dict,
    point_data: dict[str, str],
) -> list[dict]:
    """Get ratings for all display nutrients for a single point.

    Returns sorted list: highest priority first.
    """
    ratings = []
    for nutrient in DISPLAY_NUTRIENTS:
        if nutrient not in point_data:
            continue
        val_str = point_data[nutrient]
        if val_str in ("", "None", "nan", "NaN"):
            continue
        try:
            val = float(val_str)
        except (ValueError, TypeError):
            continue
        category, color, score, distance = rate_nutrient_value(ranges, nutrient, val)
        ratings.append({
            "nutrient": nutrient,
            "value": val_str,
            "category": category,
            "color": color,
            "score": score,
            "distance": distance,
        })

    # Sort by score descending, then by distance descending
    def sort_key(r):
        if r["score"] == 0:
            # All optimal: use default order
            try:
                return (-1, DEFAULT_TOP5_ORDER.index(r["nutrient"]))
            except ValueError:
                return (-1, 999)
        return (r["score"], r["distance"] or 0)

    ratings.sort(key=sort_key, reverse=True)
    return ratings
