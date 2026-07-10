"""Single-field NDVI dashboard generator.

Produces a self-contained HTML dashboard for one field, including:
- Field boundary map with satellite basemap
- Weather charts (GDD, rainfall, cumulative)
- NDVI time-series chart (Sentinel-prioritized, Landsat gapfill)
- Embedded composite PNG thumbnails (peak-95, cumulative)
- Crop history table
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
from PIL import Image

from lib.basemap_fetcher import fetch_basemap
from lib.dashboard_assets import FIELD_COLORS, build_html_body, ensure_plotly_bundle
from lib.dashboard_assets import _css_template, _js_controls_template
from lib.field_dashboard_assets import _field_css_template, _field_js_template
from lib.field_ndvi_transforms import compute_field_ndvi_series, get_crop_history
from lib.paths import (
    DATA_ROOT,
    field_weather_path,
)
from lib.runtime_paths import resolve_runtime_paths
from lib.weather_transforms import compute_weather_transforms, parse_daily_weather


def _load_field_boundary(farm_boundary_path: Path, field_id: str) -> gpd.GeoDataFrame | None:
    """Extract a single field polygon from the farm boundaries GeoJSON."""
    if not farm_boundary_path.exists():
        return None
    try:
        gdf = gpd.read_file(str(farm_boundary_path))
        if gdf.empty or "field_id" not in gdf.columns:
            return None
        field_gdf = gdf[gdf["field_id"] == field_id].copy()
        if field_gdf.empty:
            return None
        return field_gdf
    except Exception:
        return None


def _read_composite_png_base64(png_path: Path) -> str | None:
    """Read a PNG and return base64 data URI."""
    if not png_path.exists():
        return None
    try:
        with open(png_path, "rb") as f:
            data = f.read()
        return f"data:image/png;base64,{base64.b64encode(data).decode()}"
    except Exception:
        return None


def _build_field_map_data(
    gdf: gpd.GeoDataFrame,
    basemap_b64: str | None,
    mercator_extent: tuple[float, float, float, float] | None,
    field_weather: list[dict],
) -> tuple[list[dict], dict]:
    """Build Plotly map data and layout for a single field."""
    from shapely.geometry import MultiPolygon, Polygon

    use_mercator = basemap_b64 is not None and mercator_extent is not None

    if use_mercator:
        if gdf.crs is None or gdf.crs.to_epsg() != 3857:
            gdf = gdf.to_crs(epsg=3857)
    else:
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)

    map_data: list[dict] = []
    color = FIELD_COLORS[0]

    for _, row in gdf.iterrows():
        geom = row.geometry
        rings = []
        if isinstance(geom, Polygon):
            rings.append(list(geom.exterior.coords))
        elif isinstance(geom, MultiPolygon):
            for poly in geom.geoms:
                rings.append(list(poly.exterior.coords))

        for ring in rings:
            xs = [c[0] for c in ring]
            ys = [c[1] for c in ring]
            if xs and xs[0] != xs[-1]:
                xs.append(xs[0])
                ys.append(ys[0])

            lf = field_weather[0].get("lastFrostDate", "") if field_weather else ""
            lf_str = f" | Last frost: {lf}" if lf else ""

            map_data.append({
                "type": "scatter",
                "mode": "lines",
                "x": xs,
                "y": ys,
                "fill": "toself",
                "fillcolor": color + "33",
                "line": {"color": color, "width": 2},
                "name": f"Field {row.get('field_id', '')}{lf_str}",
                "hovertemplate": f"<b>Field {row.get('field_id', '')}</b><br>" +
                    (f"Area: {row.get('area_acres', 'N/A')} ac<br>" if "area_acres" in gdf.columns else "") +
                    (f"Crop: {row.get('crop_name', 'N/A')}<br>" if "crop_name" in gdf.columns else "") +
                    (f"Last frost: {lf}<extra></extra>" if lf else "<extra></extra>"),
            })

    # Centroid label
    try:
        gdf_pts = gdf.copy()
        gdf_pts["geometry"] = gdf_pts.geometry.centroid
        for _, row in gdf_pts.iterrows():
            map_data.append({
                "type": "scatter",
                "mode": "text",
                "x": [row.geometry.x],
                "y": [row.geometry.y],
                "text": [str(row.get("field_id", ""))],
                "textposition": "middle center",
                "textfont": {"size": 12, "color": "#000", "family": "Arial Black"},
                "showlegend": False,
                "hoverinfo": "skip",
            })
    except Exception:
        pass

    layout = {
        "title": {"text": "Field Boundary", "font": {"size": 14}},
        "xaxis": {"showgrid": False, "zeroline": False, "showticklabels": False},
        "yaxis": {"showgrid": False, "zeroline": False, "showticklabels": False, "scaleanchor": "x", "scaleratio": 1},
        "margin": {"l": 0, "r": 0, "t": 40, "b": 0},
        "showlegend": False,
        "hovermode": "closest",
    }

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
        bounds = gdf.total_bounds
        layout["xaxis"]["range"] = [bounds[0], bounds[2]]
        layout["yaxis"]["range"] = [bounds[1], bounds[3]]

    return map_data, layout


def _build_weather_charts_single_field(field_weather: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Build chart data for a single field's weather (one trace per year)."""
    color = FIELD_COLORS[0]
    gdd_data: list[dict] = []
    rainfall_data: list[dict] = []
    cum_gdd_data: list[dict] = []
    cum_rain_data: list[dict] = []

    for year_rec in field_weather:
        year = year_rec["year"]
        daily = year_rec["daily"]
        if not daily:
            continue
        doys = [d["dayOfYear"] for d in daily]
        gdds = [d["dailyGdd"] for d in daily]
        rains = [d["dailyRainfallIn"] for d in daily]
        cgdds = [d["cumulativeGdd"] for d in daily]
        crains = [d["cumulativeRainfallIn"] for d in daily]

        label = f"{year}"
        gdd_data.append({
            "type": "scatter", "mode": "lines",
            "x": doys, "y": gdds, "name": label,
            "line": {"color": color, "width": 1.5},
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>GDD: %{{y:.2f}}<extra></extra>",
            "year": year,
        })
        rainfall_data.append({
            "type": "bar",
            "x": doys, "y": rains, "name": label,
            "marker": {"color": color + "88"},
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Rain: %{{y:.2f}} in<extra></extra>",
            "year": year,
        })
        cum_gdd_data.append({
            "type": "scatter", "mode": "lines",
            "x": doys, "y": cgdds, "name": label,
            "line": {"color": color, "width": 2},
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Cum GDD: %{{y:.2f}}<extra></extra>",
            "year": year,
        })
        cum_rain_data.append({
            "type": "scatter", "mode": "lines",
            "x": doys, "y": crains, "name": label,
            "line": {"color": color, "width": 2, "dash": "dot"},
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Cum Rain: %{{y:.2f}} in<extra></extra>",
            "year": year,
        })

    return gdd_data, rainfall_data, cum_gdd_data, cum_rain_data


def _build_ndvi_chart_data(ndvi_series: list[dict]) -> list[dict]:
    """Build Plotly traces for NDVI time-series.

    One trace per year, markers differentiate Sentinel vs Landsat.
    """
    if not ndvi_series:
        return []

    years = sorted({d["year"] for d in ndvi_series})
    traces = []

    for i, year in enumerate(years):
        color = FIELD_COLORS[i % len(FIELD_COLORS)]
        year_data = [d for d in ndvi_series if d["year"] == year]

        doys = [d["doy"] for d in year_data]
        ndvis = [d["mean_ndvi"] for d in year_data]
        sources = [d["source"] for d in year_data]
        clouds = [d["cloud_cover"] for d in year_data]
        dates = [d["date"] for d in year_data]

        # Marker symbols: circle for sentinel, triangle-up for landsat
        symbols = ["circle" if s == "sentinel" else "triangle-up" for s in sources]
        sizes = [10 if c <= 10 else 7 for c in clouds]

        traces.append({
            "type": "scatter",
            "mode": "lines+markers",
            "x": doys,
            "y": ndvis,
            "name": str(year),
            "line": {"color": color, "width": 2},
            "marker": {
                "symbol": symbols,
                "size": sizes,
                "color": color,
                "line": {"width": 1, "color": "white"},
            },
            "hovertemplate": (
                "<b>%{text}</b><br>" +
                "Day: %{x}<br>" +
                "NDVI: %{y:.4f}<br>" +
                "Cloud: %{customdata}%<br>" +
                "Source: %{meta}<extra></extra>"
            ),
            "text": dates,
            "customdata": clouds,
            "meta": sources,
            "year": year,
        })

    return traces


def _default_chart_layout(title: str, y_title: str | None = None) -> dict:
    lo = {
        "title": {"text": title, "font": {"size": 12}},
        "xaxis": {"title": "Day of year"},
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }
    if y_title:
        lo["yaxis"] = {"title": y_title}
    return lo


def generate_field_dashboard(
    farm_dir_path: Path,
    field_id: str,
    output_path: Path | None = None,
    no_basemap: bool = False,
    force_basemap: bool = False,
) -> Path:
    """Generate a self-contained NDVI dashboard for a single field.

    Parameters
    ----------
    farm_dir_path
        Absolute path to the farm directory.
    field_id
        Field identifier (must exist in farm boundary GeoJSON).
    output_path
        Where to write the HTML. Defaults to field_dashboards_dir.
    no_basemap
        Skip satellite basemap.
    force_basemap
        Ignore tile cache and re-download.

    Returns
    -------
    Path to the generated HTML file.
    """
    farm_dir_path = farm_dir_path.resolve()

    # Derive slugs from path
    parts = farm_dir_path.parts
    try:
        growers_idx = parts.index("growers")
        grower_slug = parts[growers_idx + 1]
        farm_slug = parts[growers_idx + 3]
    except (ValueError, IndexError):
        grower_slug = farm_dir_path.parent.parent.name
        farm_slug = farm_dir_path.name

    # Load field boundary
    boundary_path = farm_dir_path / "boundary" / "field_boundaries.geojson"
    gdf = _load_field_boundary(boundary_path, field_id)
    if gdf is None:
        raise FileNotFoundError(f"Field {field_id} not found in {boundary_path}")

    # Load weather
    weather_csv = field_weather_path(grower_slug, farm_slug, field_id)
    weather_df = parse_daily_weather(weather_csv)
    weather_transforms = compute_weather_transforms(weather_df)

    # Load NDVI series
    runtime_paths = resolve_runtime_paths()
    runtime_base = runtime_paths.runtime_base
    field_dir = farm_dir_path / "fields" / field_id
    ndvi_series = compute_field_ndvi_series(field_dir, runtime_base)

    # Load crop history
    crop_history = get_crop_history(field_dir)
    # Augment with peak NDVI from card summary if available
    card_path = field_dir / "derived" / "summaries" / "ndvi_card_summary.json"
    if card_path.exists():
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
            corn_peak = card.get("cards", {}).get("corn_peak_95", {}).get("mean_ndvi")
            for rec in crop_history:
                if rec.get("crop_name", "").lower() == "corn" and corn_peak:
                    rec["peak_ndvi"] = round(corn_peak, 3)
        except Exception:
            pass

    # Load composite PNGs as base64
    composites = []
    features_dir = field_dir / "derived" / "features"
    for png_name, caption in [
        ("ndvi_corn_peak_95.png", "Corn Peak-95 NDVI (DOY 180-240)"),
        ("ndvi_current_season_cumulative.png", "Cumulative Season NDVI (Sentinel)"),
    ]:
        b64 = _read_composite_png_base64(features_dir / png_name)
        if b64:
            composites.append({"src": b64, "caption": caption})

    # Fetch basemap
    shared_dir = runtime_paths.runtime_base / "shared"
    cache_dir = shared_dir / "dashboard_assets" / "basemaps"
    basemap_b64, mercator_extent = fetch_basemap(
        gdf, cache_dir=cache_dir, no_basemap=no_basemap, force_refresh=force_basemap
    )

    # Build charts
    map_data, map_layout = _build_field_map_data(gdf, basemap_b64, mercator_extent, weather_transforms)
    gdd_data, rainfall_data, cum_gdd_data, cum_rain_data = _build_weather_charts_single_field(weather_transforms)
    ndvi_data = _build_ndvi_chart_data(ndvi_series)

    # Chart layouts
    gdd_layout = _default_chart_layout("Daily Growing Degree Days", "GDD")
    rainfall_layout = _default_chart_layout("Daily Rainfall (inches)", "inches")
    cum_gdd_layout = _default_chart_layout("Cumulative GDD", "GDD")
    cum_rain_layout = _default_chart_layout("Cumulative Rainfall", "inches")
    ndvi_layout = {
        "title": {"text": "NDVI Time Series (Sentinel + Landsat)", "font": {"size": 12}},
        "xaxis": {"title": "Day of year"},
        "yaxis": {"title": "Mean NDVI", "range": [0, 1]},
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }

    # Ensure Plotly bundle
    plotly_bundle = ensure_plotly_bundle(shared_dir)

    # Title and subtitle
    area_str = ""
    if "area_acres" in gdf.columns:
        area_str = f"{gdf.iloc[0].get('area_acres', 'N/A')} ac"
    crop_str = ""
    if crop_history:
        crop_str = f" | {crop_history[-1].get('crop_name', '')}"
    title = f"Field {field_id} — NDVI Dashboard"
    subtitle = f"{area_str}{crop_str} | {len(weather_transforms)} years weather | {len(ndvi_series)} clear-sky scenes"

    # Build HTML body
    # We need to extend the base HTML with NDVI-specific sections
    html = _build_field_html_body(
        plotly_bundle=plotly_bundle,
        title=title,
        subtitle=subtitle,
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
        ndvi_layout=ndvi_layout,
        ndvi_data=ndvi_data,
        composites=composites,
        crop_history=crop_history,
        years=sorted({d["year"] for d in weather_transforms}),
    )

    # Determine output path
    if output_path is None:
        dashboards_dir = field_dir / "derived" / "dashboards"
        dashboards_dir.mkdir(parents=True, exist_ok=True)
        safe_id = field_id.replace("-", "_")
        output_path = dashboards_dir / f"{safe_id}_dashboard.html"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _build_field_html_body(
    plotly_bundle: str,
    title: str,
    subtitle: str,
    map_layout: dict,
    map_data: list[dict],
    gdd_layout: dict,
    gdd_data: list[dict],
    rainfall_layout: dict,
    rainfall_data: list[dict],
    cumulative_gdd_layout: dict,
    cumulative_gdd_data: list[dict],
    cumulative_rainfall_layout: dict,
    cumulative_rainfall_data: list[dict],
    ndvi_layout: dict,
    ndvi_data: list[dict],
    composites: list[dict],
    crop_history: list[dict],
    years: list[int],
) -> str:
    """Assemble the self-contained HTML for a single-field dashboard."""
    import json

    year_buttons = " ".join(
        f'<button class="btn" onclick="toggleVisibility(\'ndvi-chart\', [{y}])">{y}</button>'
        for y in years
    )

    # Composite gallery HTML
    composite_html = ""
    if composites:
        items = "\n".join(
            f'<div class="composite-item"><img src="{c["src"]}" alt="{c["caption"]}" loading="lazy"><div class="caption">{c["caption"]}</div></div>'
            for c in composites
        )
        composite_html = f'<div class="composite-gallery">{items}</div>'

    # Crop table HTML
    crop_table_html = ""
    if crop_history:
        rows = "\n".join(
            f'<tr><td>{r["year"]}</td><td>{r.get("crop_name", "")}</td><td>{r.get("scene_count", "")}</td><td>{r.get("peak_ndvi", "")}</td></tr>'
            for r in crop_history
        )
        crop_table_html = f"""\
<div class="crop-section">
<h3>Crop History</h3>
<table class="crop-table">
<thead><tr><th>Year</th><th>Crop</th><th>Scenes</th><th>Peak NDVI</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</div>"""

    map_layout_json = json.dumps(map_layout, default=str)
    map_data_json = json.dumps(map_data, default=str)
    gdd_layout_json = json.dumps(gdd_layout, default=str)
    gdd_data_json = json.dumps(gdd_data, default=str)
    rainfall_layout_json = json.dumps(rainfall_layout, default=str)
    rainfall_data_json = json.dumps(rainfall_data, default=str)
    cum_gdd_layout_json = json.dumps(cumulative_gdd_layout, default=str)
    cum_gdd_data_json = json.dumps(cumulative_gdd_data, default=str)
    cum_rain_layout_json = json.dumps(cumulative_rainfall_layout, default=str)
    cum_rain_data_json = json.dumps(cumulative_rainfall_data, default=str)
    ndvi_layout_json = json.dumps(ndvi_layout, default=str)
    ndvi_data_json = json.dumps(ndvi_data, default=str)

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{_css_template()}{_field_css_template()}</style>
<script>{plotly_bundle}</script>
</head>
<body>
<div class="dashboard-header">
    <div>
        <h1>{title}</h1>
        <p class="subtitle">{subtitle}</p>
    </div>
</div>
<div class="controls-bar">
    <span>NDVI Years:</span>
    {year_buttons}
    <button class="btn" onclick="resetYears('ndvi-chart')">All Years</button>
    <span class="legend-inline"><span class="legend-symbol">&#9679;</span> Sentinel <span class="legend-symbol">&#9650;</span> Landsat</span>
</div>
<div class="map-section">
    <h3>Field Boundary</h3>
    <div id="map-container"></div>
</div>
<div class="grid">
    <div class="chart-card">
        <h3>Daily Growing Degree Days</h3>
        <div id="gdd-chart" class="chart-container"></div>
    </div>
    <div class="chart-card">
        <h3>Daily Rainfall (inches)</h3>
        <div id="rainfall-chart" class="chart-container"></div>
    </div>
    <div class="chart-card">
        <h3>Cumulative GDD</h3>
        <div id="cumulative-gdd-chart" class="chart-container"></div>
    </div>
    <div class="chart-card">
        <h3>Cumulative Rainfall (inches)</h3>
        <div id="cumulative-rainfall-chart" class="chart-container"></div>
    </div>
</div>
<div class="ndvi-section">
    <h3>NDVI Time Series</h3>
    <div id="ndvi-chart" class="ndvi-chart-container"></div>
    {composite_html}
</div>
{crop_table_html}
<script>
{_js_controls_template()}
{_field_js_template()}

var mapLayout = {map_layout_json};
var mapData = {map_data_json};
Plotly.newPlot('map-container', mapData, mapLayout, {{responsive: true}});

var gddLayout = {gdd_layout_json};
var gddData = {gdd_data_json};
Plotly.newPlot('gdd-chart', gddData, gddLayout, {{responsive: true}});

var rainfallLayout = {rainfall_layout_json};
var rainfallData = {rainfall_data_json};
Plotly.newPlot('rainfall-chart', rainfallData, rainfallLayout, {{responsive: true}});

var cgLayout = {cum_gdd_layout_json};
var cgData = {cum_gdd_data_json};
Plotly.newPlot('cumulative-gdd-chart', cgData, cgLayout, {{responsive: true}});

var crLayout = {cum_rain_layout_json};
var crData = {cum_rain_data_json};
Plotly.newPlot('cumulative-rainfall-chart', crData, crLayout, {{responsive: true}});

var ndviLayout = {ndvi_layout_json};
var ndviData = {ndvi_data_json};
Plotly.newPlot('ndvi-chart', ndviData, ndviLayout, {{responsive: true}});
</script>
</body>
</html>
"""
