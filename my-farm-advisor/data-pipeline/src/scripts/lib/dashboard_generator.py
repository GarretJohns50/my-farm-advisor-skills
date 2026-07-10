"""Dashboard generator: orchestrate data → self-contained HTML.

Reads a farm directory, loads field boundaries and weather,
computes transforms, fetches basemap, builds Plotly figures,
and assembles a single self-contained HTML file.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd

from lib.basemap_fetcher import fetch_basemap
from lib.dashboard_assets import FIELD_COLORS, build_html_body, ensure_plotly_bundle
from lib.paths import (
    DATA_ROOT,
    farm_dashboards_dir,
    farm_dir,
    field_weather_path,
)
from lib.runtime_paths import resolve_runtime_paths
from lib.weather_transforms import compute_weather_transforms, parse_daily_weather


def _load_farm_geojson(farm_dir: Path) -> gpd.GeoDataFrame | None:
    """Load farm boundary GeoJSON (field_boundaries.geojson)."""
    boundary_path = farm_dir / "boundary" / "field_boundaries.geojson"
    if not boundary_path.exists():
        return None
    try:
        gdf = gpd.read_file(str(boundary_path))
        if gdf.empty:
            return None
        return gdf
    except Exception:
        return None


def _get_field_weather_csvs(gdf: gpd.GeoDataFrame, grower_slug: str, farm_slug: str) -> dict[str, pd.DataFrame | None]:
    """Map field_id -> daily weather DataFrame for each field."""
    result: dict[str, pd.DataFrame | None] = {}
    if "field_id" not in gdf.columns:
        return result
    for fid in gdf["field_id"].unique():
        path = field_weather_path(grower_slug, farm_slug, str(fid))
        result[str(fid)] = parse_daily_weather(path)
    return result


def _flatten_multi_coords(geom) -> list[list[tuple[float, float]]]:
    """Extract polygon rings from any shapely geometry."""
    try:
        from shapely.geometry import MultiPolygon, Polygon
    except Exception:
        return []

    if geom is None:
        return []
    if isinstance(geom, Polygon):
        return [list(geom.exterior.coords)]
    if isinstance(geom, MultiPolygon):
        rings = []
        for poly in geom.geoms:
            rings.append(list(poly.exterior.coords))
        return rings
    # Try to iterate if it has geoms
    if hasattr(geom, "geoms"):
        rings = []
        for g in geom.geoms:
            rings.extend(_flatten_multi_coords(g))
        return rings
    return []


def _build_map_data(
    gdf: gpd.GeoDataFrame,
    basemap_b64: str | None,
    mercator_extent: tuple[float, float, float, float] | None,
    field_weather: dict[str, list[dict]],
) -> tuple[list[dict], dict]:
    """Build Plotly map data and layout.

    Returns (data, layout).
    """
    use_mercator = basemap_b64 is not None and mercator_extent is not None

    if use_mercator:
        # Convert to Web Mercator so traces and basemap share the same CRS
        if gdf.crs is None or gdf.crs.to_epsg() != 3857:
            gdf = gdf.to_crs(epsg=3857)
    else:
        # No basemap — keep lat/lon for natural axis labels
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)

    field_ids = sorted(gdf["field_id"].unique()) if "field_id" in gdf.columns else []
    color_map = {fid: FIELD_COLORS[i % len(FIELD_COLORS)] for i, fid in enumerate(field_ids)}

    map_data: list[dict] = []

    # Boundary traces
    for _, row in gdf.iterrows():
        fid = str(row.get("field_id", ""))
        color = color_map.get(fid, "#999")
        rings = _flatten_multi_coords(row.geometry)
        for ring in rings:
            xs = [c[0] for c in ring]
            ys = [c[1] for c in ring]
            # Close the ring
            if xs and xs[0] != xs[-1]:
                xs.append(xs[0])
                ys.append(ys[0])

            years = field_weather.get(fid, [])
            last_frost_str = ""
            if years:
                lf = years[0].get("lastFrostDate", "")
                if lf:
                    last_frost_str = f" | Last frost: {lf}"

            map_data.append({
                "type": "scatter",
                "mode": "lines",
                "x": xs,
                "y": ys,
                "fill": "toself",
                "fillcolor": color + "33",  # 20% alpha
                "line": {"color": color, "width": 2},
                "name": f"Field {fid}{last_frost_str}",
                "hovertemplate": f"<b>Field {fid}</b><br>" +
                    (f"Area: {row.get('area_acres', 'N/A')} ac<br>" if "area_acres" in gdf.columns else "") +
                    (f"Crop: {row.get('crop_name', 'N/A')}<br>" if "crop_name" in gdf.columns else "") +
                    (f"Last frost: {last_frost_str}<extra></extra>" if last_frost_str else "<extra></extra>"),
                "fieldId": fid,
            })

    # Add centroid labels
    try:
        gdf_pts = gdf.copy()
        # Compute centroid in the same CRS as the map
        gdf_pts["geometry"] = gdf_pts.geometry.centroid
        for _, row in gdf_pts.iterrows():
            fid = str(row.get("field_id", ""))
            color = color_map.get(fid, "#999")
            map_data.append({
                "type": "scatter",
                "mode": "text",
                "x": [row.geometry.x],
                "y": [row.geometry.y],
                "text": [fid],
                "textposition": "middle center",
                "textfont": {"size": 10, "color": "#000", "family": "Arial Black"},
                "showlegend": False,
                "hoverinfo": "skip",
                "fieldId": fid,
            })
    except Exception:
        pass

    layout = {
        "title": {"text": "Field Boundaries", "font": {"size": 14}},
        "xaxis": {"showgrid": False, "zeroline": False, "showticklabels": False},
        "yaxis": {"showgrid": False, "zeroline": False, "showticklabels": False, "scaleanchor": "x", "scaleratio": 1},
        "margin": {"l": 0, "r": 0, "t": 40, "b": 0},
        "showlegend": True,
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.8)"},
        "hovermode": "closest",
    }

    # Add basemap image to layout if available
    if use_mercator and mercator_extent:
        layout["images"] = [{
            "source": basemap_b64,
            "xref": "x",
            "yref": "y",
            "x": mercator_extent[0],
            "y": mercator_extent[3],
            "sizex": mercator_extent[2] - mercator_extent[0],
            "sizey": mercator_extent[3] - mercator_extent[1],
            "sizing": "stretch",
            "opacity": 1,
            "layer": "below",
        }]
        layout["xaxis"]["range"] = [mercator_extent[0], mercator_extent[2]]
        layout["yaxis"]["range"] = [mercator_extent[1], mercator_extent[3]]
    else:
        # Use lat/lon range
        bounds = gdf.total_bounds
        layout["xaxis"]["range"] = [bounds[0], bounds[2]]
        layout["yaxis"]["range"] = [bounds[1], bounds[3]]

    return map_data, layout


def _build_weather_charts(field_weather: dict[str, list[dict]]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Build chart data for GDD and rainfall.

    Returns (gdd_data, rainfall_data, cumulative_gdd_data, cumulative_rainfall_data).
    """
    field_ids = sorted(field_weather.keys())
    color_map = {fid: FIELD_COLORS[i % len(FIELD_COLORS)] for i, fid in enumerate(field_ids)}

    gdd_data: list[dict] = []
    rainfall_data: list[dict] = []
    cum_gdd_data: list[dict] = []
    cum_rain_data: list[dict] = []

    for fid in field_ids:
        years = field_weather[fid]
        color = color_map[fid]
        for year_rec in years:
            year = year_rec["year"]
            daily = year_rec["daily"]
            if not daily:
                continue

            doys = [d["dayOfYear"] for d in daily]
            gdds = [d["dailyGdd"] for d in daily]
            rains = [d["dailyRainfallIn"] for d in daily]
            cgdds = [d["cumulativeGdd"] for d in daily]
            crains = [d["cumulativeRainfallIn"] for d in daily]

            label = f"Field {fid} – {year}"

            gdd_data.append({
                "type": "scatter",
                "mode": "lines",
                "x": doys,
                "y": gdds,
                "name": label,
                "line": {"color": color, "width": 1.5},
                "hovertemplate": f"<b>Field {fid}</b><br>Day: %{{x}}<br>GDD: %{{y:.2f}}<extra></extra>",
                "fieldId": fid,
                "year": year,
            })

            rainfall_data.append({
                "type": "bar",
                "x": doys,
                "y": rains,
                "name": label,
                "marker": {"color": color + "88"},  # 50% alpha
                "hovertemplate": f"<b>Field {fid}</b><br>Day: %{{x}}<br>Rain: %{{y:.2f}} in<extra></extra>",
                "fieldId": fid,
                "year": year,
            })

            cum_gdd_data.append({
                "type": "scatter",
                "mode": "lines",
                "x": doys,
                "y": cgdds,
                "name": label,
                "line": {"color": color, "width": 2},
                "hovertemplate": f"<b>Field {fid}</b><br>Day: %{{x}}<br>Cum GDD: %{{y:.2f}}<extra></extra>",
                "fieldId": fid,
                "year": year,
            })

            cum_rain_data.append({
                "type": "scatter",
                "mode": "lines",
                "x": doys,
                "y": crains,
                "name": label,
                "line": {"color": color, "width": 2, "dash": "dot"},
                "hovertemplate": f"<b>Field {fid}</b><br>Day: %{{x}}<br>Cum Rain: %{{y:.2f}} in<extra></extra>",
                "fieldId": fid,
                "year": year,
            })

    return gdd_data, rainfall_data, cum_gdd_data, cum_rain_data


def _default_chart_layout(title: str) -> dict:
    return {
        "title": {"text": title, "font": {"size": 12}},
        "xaxis": {"title": "Day of year"},
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }


def generate_dashboard(
    farm_dir_path: Path,
    output_path: Path | None = None,
    no_basemap: bool = False,
    force_basemap: bool = False,
) -> Path:
    """Generate a self-contained weather dashboard for the farm.

    Parameters
    ----------
    farm_dir_path
        Absolute path to the farm directory (must contain boundary/field_boundaries.geojson).
    output_path
        Where to write the HTML file. Defaults to farm_dashboards_dir / <slug>_dashboard.html.
    no_basemap
        Skip satellite basemap entirely.
    force_basemap
        Ignore tile cache and re-download.

    Returns
    -------
    Path to the generated HTML file.
    """
    farm_dir_path = farm_dir_path.resolve()

    # Derive grower/farm slugs from path
    # Path pattern: .../growers/<grower>/farms/<farm>
    parts = farm_dir_path.parts
    try:
        growers_idx = parts.index("growers")
        grower_slug = parts[growers_idx + 1]
        farm_slug = parts[growers_idx + 3]
    except (ValueError, IndexError):
        grower_slug = farm_dir_path.parent.parent.name
        farm_slug = farm_dir_path.name

    # Load boundaries
    gdf = _load_farm_geojson(farm_dir_path)
    if gdf is None:
        raise FileNotFoundError(f"No field_boundaries.geojson found in {farm_dir_path / 'boundary'}")

    # Load weather per field
    field_weather_raw = _get_field_weather_csvs(gdf, grower_slug, farm_slug)

    # Compute transforms
    field_weather: dict[str, list[dict]] = {}
    for fid, df in field_weather_raw.items():
        transforms = compute_weather_transforms(df)
        field_weather[fid] = transforms

    # Fetch basemap
    runtime_paths = resolve_runtime_paths()
    shared_dir = runtime_paths.runtime_base / "shared"
    cache_dir = shared_dir / "dashboard_assets" / "basemaps"
    basemap_b64, mercator_extent = fetch_basemap(
        gdf, cache_dir=cache_dir, no_basemap=no_basemap, force_refresh=force_basemap
    )

    # Build map
    map_data, map_layout = _build_map_data(gdf, basemap_b64, mercator_extent, field_weather)

    # Build charts
    gdd_data, rainfall_data, cum_gdd_data, cum_rain_data = _build_weather_charts(field_weather)

    # Default layouts
    gdd_layout = _default_chart_layout("Daily GDD")
    rainfall_layout = _default_chart_layout("Daily Rainfall")
    cum_gdd_layout = _default_chart_layout("Cumulative GDD")
    cum_rain_layout = _default_chart_layout("Cumulative Rainfall")

    # Ensure Plotly bundle
    plotly_bundle = ensure_plotly_bundle(shared_dir)

    # Collect options for dropdowns
    field_options = sorted(field_weather.keys())
    all_years: set[int] = set()
    for recs in field_weather.values():
        for rec in recs:
            all_years.add(rec["year"])
    year_options = sorted(all_years)

    # Build title
    farm_name = farm_slug.replace("-", " ").replace("_", " ").title()
    title = f"{farm_name} — Weather Dashboard"
    subtitle = f"{len(field_options)} fields | {len(year_options)} years | Last frost + GDD + Rainfall"

    # Assemble HTML
    html = build_html_body(
        plotly_bundle=plotly_bundle,
        title=title,
        subtitle=subtitle,
        field_options=field_options,
        year_options=year_options,
        map_layout=map_layout,
        map_data=map_data,
        gdd_layout=gdd_layout,
        gdd_data=gdd_data,
        rainfall_layout=rainfall_layout,
        rainfall_data=rainfall_data,
        cumulative_gdd_layout=cum_gdd_layout,
        cumulative_gdd_data=cum_gdd_data,
        cumulative_rainfall_layout=cum_rain_layout,
        cumulative_rainfall_data=cum_rain_data,
    )

    # Determine output path
    if output_path is None:
        dashboards_dir = farm_dashboards_dir(grower_slug, farm_slug)
        dashboards_dir.mkdir(parents=True, exist_ok=True)
        safe_slug = farm_slug.replace("-", "_")
        output_path = dashboards_dir / f"{safe_slug}_dashboard.html"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path
