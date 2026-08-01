"""Generate a regular sampling grid inside an irregular field polygon.

Key features:
- Projects to UTM for metric calculations
- Orients grid to the field's minimum rotated rectangle (longest axis)
- Adaptive grid dimensions to approximate target count
- Subsamples from overshoots by removing edge points
- Numbers points left-to-right, top-to-bottom in the oriented frame
- Projects back to WGS84 for dashboard consumption
"""

from __future__ import annotations

import math
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.affinity import rotate
from shapely.geometry import Point, Polygon


def _utm_epsg_from_lon(lon: float) -> int:
    """Return UTM EPSG code from a longitude value."""
    zone = int((lon + 180) / 6) + 1
    return 32600 + zone  # northern hemisphere default


def _compute_rotated_bbox(polygon: Polygon) -> tuple[Polygon, float]:
    """Return the minimum rotated rectangle and its rotation angle (degrees)."""
    try:
        mrr = polygon.minimum_rotated_rectangle
    except AttributeError:
        mrr = polygon.convex_hull.minimum_rotated_rectangle

    coords = list(mrr.exterior.coords)[:-1]
    if len(coords) < 4:
        return mrr, 0.0

    max_len = 0.0
    best_angle = 0.0
    for i in range(len(coords)):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % len(coords)]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length > max_len:
            max_len = length
            best_angle = math.degrees(math.atan2(dy, dx))

    angle = best_angle % 180
    if angle > 90:
        angle -= 180
    return mrr, angle


def _generate_grid_points(
    polygon: Polygon,
    spacing_m: float,
    angle_deg: float,
) -> list[Point]:
    """Generate candidate grid points, return only those inside polygon."""
    rotated_poly = rotate(polygon, -angle_deg, origin=(0, 0), use_radians=False)
    minx, miny, maxx, maxy = rotated_poly.bounds

    xs = np.arange(minx + spacing_m / 2, maxx, spacing_m)
    ys = np.arange(maxy - spacing_m / 2, miny, -spacing_m)

    interior_rotated: list[tuple[float, float]] = []
    for y in ys:
        for x in xs:
            pt = Point(x, y)
            if rotated_poly.contains(pt) or rotated_poly.touches(pt):
                interior_rotated.append((x, y))

    # Rotate back to original orientation
    angle_rad = math.radians(angle_deg)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    interior_original: list[Point] = []
    for x, y in interior_rotated:
        xr = x * cos_a - y * sin_a
        yr = x * sin_a + y * cos_a
        interior_original.append(Point(xr, yr))

    return interior_original


def _subsample_to_target(
    points: list[Point],
    target_count: int,
    polygon: Polygon,
) -> list[Point]:
    """If we have more points than target, remove edge points systematically."""
    if len(points) <= target_count:
        return points

    # Compute polygon centroid
    centroid = polygon.centroid
    cx, cy = centroid.x, centroid.y

    # Sort by distance from centroid (ascending) — keep closest, remove furthest
    points_with_dist = [(pt, (pt.x - cx) ** 2 + (pt.y - cy) ** 2) for pt in points]
    points_with_dist.sort(key=lambda t: t[1])

    kept = [pt for pt, _ in points_with_dist[:target_count]]
    return kept


def generate_field_grid(
    boundary_path: str | Path,
    zone_acres: float = 2.5,
    target_count: int = 57,
) -> gpd.GeoDataFrame:
    """Generate a numbered sampling grid inside a field boundary.

    Parameters
    ----------
    boundary_path : str | Path
        Path to GeoJSON (or any geopandas-readable) boundary file.
    zone_acres : float
        Target zone size in acres. Default 2.5.
    target_count : int
        Target number of grid points. The algorithm tries to get close,
        then subsamples from overshoots by removing edge points.

    Returns
    -------
    gpd.GeoDataFrame
        Columns: geometry, point_id, zone_num, lon, lat, easting, northing,
        zone_acres, zone_m2. CRS is WGS84 (EPSG:4326).
    """
    boundary_path = Path(boundary_path)
    gdf = gpd.read_file(str(boundary_path))
    if gdf.empty:
        raise ValueError(f"Boundary file is empty: {boundary_path}")

    # Ensure WGS84
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    polygon = gdf.iloc[0].geometry
    if hasattr(polygon, "geoms"):
        polygon = max(polygon.geoms, key=lambda p: p.area)

    # Determine UTM zone from polygon centroid
    centroid_lon = polygon.centroid.x
    utm_epsg = _utm_epsg_from_lon(centroid_lon)

    gdf_utm = gdf.to_crs(epsg=utm_epsg)
    polygon_utm = gdf_utm.iloc[0].geometry
    if hasattr(polygon_utm, "geoms"):
        polygon_utm = max(polygon_utm.geoms, key=lambda p: p.area)

    # Compute orientation from minimum rotated rectangle
    _, angle_deg = _compute_rotated_bbox(polygon_utm)

    # Initial spacing based on zone_acres
    m2_per_acre = 4046.8564224
    target_area_m2 = zone_acres * m2_per_acre
    spacing_m = math.sqrt(target_area_m2)

    # Adaptive iteration: try a few spacings around the target
    best_points: list[Point] = []
    best_count_diff = float("inf")
    best_spacing = spacing_m

    for adj in [1.0, 1.05, 1.1, 0.95, 0.9, 1.15, 0.85]:
        test_spacing = spacing_m * adj
        points = _generate_grid_points(polygon_utm, test_spacing, angle_deg)
        count = len(points)
        diff = abs(count - target_count)
        if diff < best_count_diff:
            best_count_diff = diff
            best_points = points
            best_spacing = test_spacing
        if count == target_count:
            break

    # Subsample if we overshoot
    if len(best_points) > target_count:
        best_points = _subsample_to_target(best_points, target_count, polygon_utm)

    if len(best_points) != target_count:
        # If still not exact (e.g., undershoot), try one more spacing
        # with a denser grid and subsample
        denser_spacing = best_spacing * 0.8
        points = _generate_grid_points(polygon_utm, denser_spacing, angle_deg)
        if len(points) >= target_count:
            best_points = _subsample_to_target(points, target_count, polygon_utm)
        else:
            raise RuntimeError(
                f"Could not generate at least {target_count} grid points; "
                f"got {len(points)} with spacing {denser_spacing:.1f}m. "
                "Try adjusting zone_acres or target_count."
            )

    # Build GeoDataFrame in UTM with numbering left-to-right, top-to-bottom.
    # We sort purely by geographic coordinates for intuitive north-up map
    # reading order: descending latitude (north→south), then ascending
    # longitude (west→east).
    geo_points = [(pt.y, pt.x, pt) for pt in best_points]
    geo_points.sort(key=lambda t: (-t[0], t[1]))

    records = []
    for i, (_, _, pt) in enumerate(geo_points, start=1):
        records.append({
            "geometry": pt,
            "point_id": i,
            "zone_num": i,
            "easting": pt.x,
            "northing": pt.y,
            "zone_acres": zone_acres,
            "zone_m2": target_area_m2,
        })

    grid_gdf = gpd.GeoDataFrame(records, crs=f"EPSG:{utm_epsg}")

    # Project back to WGS84
    grid_gdf = grid_gdf.to_crs(epsg=4326)
    grid_gdf["lon"] = grid_gdf.geometry.x
    grid_gdf["lat"] = grid_gdf.geometry.y

    # Reorder columns for clarity
    grid_gdf = grid_gdf[["geometry", "point_id", "zone_num", "lon", "lat",
                         "easting", "northing", "zone_acres", "zone_m2"]]

    return grid_gdf


def save_field_grid(
    grid_gdf: gpd.GeoDataFrame,
    output_path: str | Path,
) -> Path:
    """Save grid GeoDataFrame to GeoJSON."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    grid_gdf.to_file(str(output_path), driver="GeoJSON")
    return output_path
