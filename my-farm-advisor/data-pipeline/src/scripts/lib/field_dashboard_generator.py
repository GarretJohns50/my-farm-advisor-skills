"""Single-field NDVI dashboard generator.

Produces a self-contained HTML dashboard for one field, including:
- Field boundary map with satellite basemap
- Temperature range chart (°F, from T2M/T2M_MAX/T2M_MIN)
- Weather charts (GDD, rainfall, cumulative)
- NDVI time-series chart (Sentinel-2 only)
- Embedded composite PNG thumbnails (peak-95, cumulative)
- Crop history table
- Unified year filter (affects both Temperature and NDVI charts)
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
from lib.dashboard_assets import FIELD_COLORS, ensure_plotly_bundle
from lib.dashboard_assets import _css_template
from lib.field_dashboard_assets import _field_css_template
from lib.field_ndvi_transforms import compute_field_ndvi_series, get_crop_history
from lib.paths import (
    field_weather_path,
)
from lib.runtime_paths import resolve_runtime_paths
from lib.weather_transforms import compute_weather_transforms, parse_daily_weather

# Corn growth stage GDD thresholds (base 10°C)
CORN_GROWTH_STAGES: list[dict] = [
    {"stage": "VE", "name": "Emergence", "gdd": 125, "color": "#2ca02c"},
    {"stage": "V6", "name": "6-Leaf", "gdd": 575, "color": "#2ca02c"},
    {"stage": "VT", "name": "Tasseling", "gdd": 1150, "color": "#ff7f0e"},
    {"stage": "R1", "name": "Silking", "gdd": 1250, "color": "#d62728"},
    {"stage": "R3", "name": "Milk", "gdd": 1925, "color": "#9467bd"},
    {"stage": "R6", "name": "Physiological Maturity", "gdd": 2700, "color": "#7f7f7f"},
]


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


def _build_temp_chart_data(field_weather: list[dict]) -> list[dict]:
    """Build temperature range area chart traces in Fahrenheit.

    Returns traces for each year: min baseline, max fill, avg line.
    """
    if not field_weather:
        return []

    traces = []
    years = sorted({d["year"] for d in field_weather})

    for i, year in enumerate(years):
        year_rec = next((r for r in field_weather if r["year"] == year), None)
        if not year_rec:
            continue
        daily = year_rec["daily"]
        if not daily:
            continue

        color = FIELD_COLORS[i % len(FIELD_COLORS)]
        doys = [d["dayOfYear"] for d in daily]
        tmin = [d["t2m_min"] * 9.0 / 5.0 + 32.0 for d in daily]
        tmax = [d["t2m_max"] * 9.0 / 5.0 + 32.0 for d in daily]
        tavg = [d["t2m_avg"] * 9.0 / 5.0 + 32.0 for d in daily]

        # Bottom baseline (invisible)
        traces.append({
            "type": "scatter",
            "mode": "lines",
            "x": doys,
            "y": tmin,
            "line": {"width": 0},
            "fill": "none",
            "showlegend": False,
            "hoverinfo": "skip",
            "year": year,
        })
        # Top fill (the range)
        traces.append({
            "type": "scatter",
            "mode": "lines",
            "x": doys,
            "y": tmax,
            "line": {"width": 0},
            "fill": "tonexty",
            "fillcolor": color + "33",
            "name": f"{year} Range",
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Range: %{{customdata[0]:.1f}}–%{{y:.1f}}°F<extra></extra>",
            "customdata": [[round(t, 1)] for t in tmin],
            "showlegend": True,
            "year": year,
        })
        # Average line
        traces.append({
            "type": "scatter",
            "mode": "lines",
            "x": doys,
            "y": tavg,
            "line": {"color": color, "width": 2},
            "name": f"{year} Avg",
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Avg: %{{y:.1f}}°F<extra></extra>",
            "showlegend": True,
            "year": year,
        })

    return traces


def _build_heat_stress_chart_data(field_weather: list[dict]) -> list[dict]:
    """Build heat stress bar chart data (degrees above 86°F)."""
    if not field_weather:
        return []
    traces = []
    color = FIELD_COLORS[0]
    for year_rec in field_weather:
        year = year_rec["year"]
        daily = year_rec.get("daily", [])
        if not daily:
            continue
        doys = [d["dayOfYear"] for d in daily]
        stress = [d.get("heatStress", 0.0) for d in daily]
        traces.append({
            "type": "bar",
            "x": doys,
            "y": stress,
            "name": str(year),
            "marker": {"color": color + "88"},
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Stress: %{{y:.1f}}°F above 86°F<extra></extra>",
            "year": year,
        })
    return traces


def _build_moisture_deficit_chart_data(field_weather: list[dict]) -> list[dict]:
    """Build cumulative moisture deficit line chart data."""
    if not field_weather:
        return []
    traces = []
    for i, year_rec in enumerate(field_weather):
        year = year_rec["year"]
        daily = year_rec.get("daily", [])
        if not daily:
            continue
        color = FIELD_COLORS[i % len(FIELD_COLORS)]
        doys = [d["dayOfYear"] for d in daily]
        deficit = [d.get("cumulativeDeficit", 0.0) for d in daily]
        traces.append({
            "type": "scatter",
            "mode": "lines",
            "x": doys,
            "y": deficit,
            "name": str(year),
            "line": {"color": color, "width": 2},
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Deficit: %{{y:.2f}} in<extra></extra>",
            "year": year,
        })
    return traces


def _build_solar_chart_data(field_weather: list[dict]) -> list[dict]:
    """Build solar radiation line chart data."""
    if not field_weather:
        return []
    traces = []
    for i, year_rec in enumerate(field_weather):
        year = year_rec["year"]
        daily = year_rec.get("daily", [])
        if not daily:
            continue
        color = FIELD_COLORS[i % len(FIELD_COLORS)]
        doys = [d["dayOfYear"] for d in daily]
        solar = [d.get("solarRadiation", 0.0) for d in daily]
        traces.append({
            "type": "scatter",
            "mode": "lines",
            "x": doys,
            "y": solar,
            "name": str(year),
            "line": {"color": color, "width": 1.5},
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Solar: %{{y:.2f}} kWh/m²<extra></extra>",
            "year": year,
        })
    return traces


def _build_solar_layout() -> dict:
    """Build solar chart layout with low-radiation threshold line."""
    return {
        "title": {"text": "Solar Radiation", "font": {"size": 12}},
        "xaxis": {"title": "Day of year"},
        "yaxis": {"title": "kWh/m²/day"},
        "shapes": [{
            "type": "line",
            "x0": 0,
            "x1": 1,
            "xref": "paper",
            "y0": 2.6,
            "y1": 2.6,
            "line": {"color": "#999", "width": 1.5, "dash": "dash"},
        }],
        "annotations": [{
            "x": 1.0,
            "xref": "paper",
            "y": 2.6,
            "text": "Low threshold: 2.6",
            "showarrow": False,
            "font": {"size": 9, "color": "#666"},
            "xanchor": "right",
            "yanchor": "bottom",
        }],
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }


def _build_ndvi_chart_data(ndvi_series: list[dict]) -> list[dict]:
    """Build Plotly traces for NDVI time-series."""
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

        sizes = [10 if c <= 10 else 7 for c in clouds]

        traces.append({
            "type": "scatter",
            "mode": "lines+markers",
            "x": doys,
            "y": ndvis,
            "name": str(year),
            "line": {"color": color, "width": 2},
            "marker": {
                "symbol": "circle",
                "size": sizes,
                "color": color,
                "line": {"width": 1, "color": "white"},
            },
            "hovertemplate": (
                "<b>%{text}</b><br>"
                "Day: %{x}<br>"
                "NDVI: %{y:.4f}<br>"
                "Cloud: %{customdata}%<extra></extra>"
            ),
            "text": dates,
            "customdata": clouds,
            "year": year,
        })

    return traces


def _add_stage_lines_to_layout(layout: dict, stage_medians: list[dict]) -> dict:
    """Add corn growth stage vertical lines and annotations to a Plotly layout."""
    if "shapes" not in layout:
        layout["shapes"] = []
    if "annotations" not in layout:
        layout["annotations"] = []
    for stage in stage_medians:
        median_doy = stage["median_doy"]
        layout["shapes"].append({
            "type": "line",
            "x0": median_doy,
            "x1": median_doy,
            "y0": 0,
            "y1": 1,
            "yref": "paper",
            "line": {"color": stage["color"], "width": 1.5, "dash": "dot"},
        })
        layout["annotations"].append({
            "x": median_doy,
            "y": 1.02,
            "yref": "paper",
            "text": f"{stage['stage']}<br>{stage['name']}",
            "showarrow": False,
            "font": {"size": 8, "color": stage["color"]},
            "bgcolor": "rgba(255,255,255,0.8)",
            "borderpad": 2,
            "align": "center",
        })
    return layout


def _build_combined_ndvi_gdd_chart_data(
    ndvi_series: list[dict], field_weather: list[dict], stage_medians: list[dict]
) -> tuple[list[dict], dict]:
    """Build combined NDVI + Cumulative GDD dual-axis chart data.

    Returns (traces, layout).
    """
    traces: list[dict] = []
    years = sorted({d["year"] for d in ndvi_series})

    # Build cumulative GDD lookup by year -> list of (doy, cum_gdd)
    gdd_by_year: dict[int, list[tuple[int, float]]] = {}
    for year_rec in field_weather:
        yr = year_rec["year"]
        gdd_by_year[yr] = [
            (d["dayOfYear"], d["cumulativeGdd"]) for d in year_rec.get("daily", [])
        ]

    for i, year in enumerate(years):
        color = FIELD_COLORS[i % len(FIELD_COLORS)]

        # NDVI trace (left axis)
        year_ndvi = [d for d in ndvi_series if d["year"] == year]
        if year_ndvi:
            doys = [d["doy"] for d in year_ndvi]
            ndvis = [d["mean_ndvi"] for d in year_ndvi]
            clouds = [d["cloud_cover"] for d in year_ndvi]
            dates = [d["date"] for d in year_ndvi]
            sizes = [10 if c <= 10 else 7 for c in clouds]
            traces.append({
                "type": "scatter",
                "mode": "lines+markers",
                "x": doys,
                "y": ndvis,
                "name": f"{year} NDVI",
                "line": {"color": color, "width": 2},
                "marker": {
                    "symbol": "circle",
                    "size": sizes,
                    "color": color,
                    "line": {"width": 1, "color": "white"},
                },
                "hovertemplate": (
                    "<b>%{text}</b><br>"
                    "Day: %{x}<br>"
                    "NDVI: %{y:.4f}<br>"
                    "Cloud: %{customdata}%<extra></extra>"
                ),
                "text": dates,
                "customdata": clouds,
                "year": year,
            })

        # Cumulative GDD trace (right axis)
        gdd_points = gdd_by_year.get(year, [])
        if gdd_points:
            gdd_doys = [p[0] for p in gdd_points]
            gdd_vals = [p[1] for p in gdd_points]
            traces.append({
                "type": "scatter",
                "mode": "lines",
                "x": gdd_doys,
                "y": gdd_vals,
                "name": f"{year} GDD",
                "line": {"color": color, "width": 1.5, "dash": "solid"},
                "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Cum GDD: %{{y:.2f}}<extra></extra>",
                "yaxis": "y2",
                "year": year,
            })

    # Max GDD for range
    max_gdd = 0
    for pts in gdd_by_year.values():
        if pts:
            max_gdd = max(max_gdd, max(p[1] for p in pts))
    max_gdd = max(max_gdd, 3000)  # At least 3000 to fit stage lines

    layout = {
        "title": {"text": "NDVI vs Cumulative GDD (with Corn Growth Stages)", "font": {"size": 12}},
        "xaxis": {"title": "Day of year"},
        "yaxis": {
            "title": "NDVI",
            "side": "left",
            "range": [0, 1],
        },
        "yaxis2": {
            "title": "Cumulative GDD",
            "side": "right",
            "overlaying": "y",
            "range": [0, max_gdd],
        },
        "margin": {"l": 50, "r": 60, "t": 60, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }
    # Add corn growth stage lines
    layout = _add_stage_lines_to_layout(layout, stage_medians)
    return traces, layout


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


def _compute_stage_median_doys(field_weather: list[dict]) -> list[dict]:
    """For each corn growth stage, compute the median DOY across all years.

    Returns a list of stage dicts augmented with 'median_doy'.
    """
    stage_medians = []
    for stage in CORN_GROWTH_STAGES:
        threshold = stage["gdd"]
        doys: list[int] = []
        for year_rec in field_weather:
            daily = year_rec.get("daily", [])
            for d in daily:
                if d.get("cumulativeGdd", 0) >= threshold:
                    doys.append(d["dayOfYear"])
                    break
        if doys:
            doys.sort()
            median_doy = doys[len(doys) // 2]
            stage_medians.append({
                **stage,
                "median_doy": median_doy,
            })
    return stage_medians


def _build_cum_gdd_layout_with_stages(stage_medians: list[dict]) -> dict:
    """Build Cumulative GDD chart layout with corn growth stage reference lines.

    Stage lines are placed at the median DOY when cumulative GDD first reaches
    each stage threshold, so they align with the x-axis (Day of year).
    """
    lo = {
        "title": {"text": "Cumulative GDD (with Corn Growth Stages)", "font": {"size": 12}},
        "xaxis": {"title": "Day of year"},
        "yaxis": {"title": "GDD"},
        "margin": {"l": 50, "r": 20, "t": 60, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }
    return _add_stage_lines_to_layout(lo, stage_medians)


def generate_field_dashboard(
    farm_dir_path: Path,
    field_id: str,
    output_path: Path | None = None,
    no_basemap: bool = False,
    force_basemap: bool = False,
) -> Path:
    """Generate a self-contained NDVI dashboard for a single field."""
    farm_dir_path = farm_dir_path.resolve()

    parts = farm_dir_path.parts
    try:
        growers_idx = parts.index("growers")
        grower_slug = parts[growers_idx + 1]
        farm_slug = parts[growers_idx + 3]
    except (ValueError, IndexError):
        grower_slug = farm_dir_path.parent.parent.name
        farm_slug = farm_dir_path.name

    boundary_path = farm_dir_path / "boundary" / "field_boundaries.geojson"
    gdf = _load_field_boundary(boundary_path, field_id)
    if gdf is None:
        raise FileNotFoundError(f"Field {field_id} not found in {boundary_path}")

    weather_csv = field_weather_path(grower_slug, farm_slug, field_id)
    weather_df = parse_daily_weather(weather_csv)
    weather_transforms = compute_weather_transforms(weather_df)

    runtime_paths = resolve_runtime_paths()
    runtime_base = runtime_paths.runtime_base
    field_dir = farm_dir_path / "fields" / field_id
    ndvi_series = compute_field_ndvi_series(field_dir, runtime_base)

    crop_history = get_crop_history(field_dir)
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

    composites = []
    features_dir = field_dir / "derived" / "features"
    for png_name, caption in [
        ("ndvi_corn_peak_95.png", "Corn Peak-95 NDVI (DOY 180-240)"),
        ("ndvi_current_season_cumulative.png", "Cumulative Season NDVI (Sentinel)"),
    ]:
        b64 = _read_composite_png_base64(features_dir / png_name)
        if b64:
            composites.append({"src": b64, "caption": caption})

    shared_dir = runtime_paths.runtime_base / "shared"
    cache_dir = shared_dir / "dashboard_assets" / "basemaps"
    basemap_b64, mercator_extent = fetch_basemap(
        gdf, cache_dir=cache_dir, no_basemap=no_basemap, force_refresh=force_basemap
    )

    map_data, map_layout = _build_field_map_data(gdf, basemap_b64, mercator_extent, weather_transforms)
    gdd_data, rainfall_data, cum_gdd_data, cum_rain_data = _build_weather_charts_single_field(weather_transforms)
    temp_data = _build_temp_chart_data(weather_transforms)
    stage_medians = _compute_stage_median_doys(weather_transforms)
    combined_data, combined_layout = _build_combined_ndvi_gdd_chart_data(ndvi_series, weather_transforms, stage_medians)
    heat_data = _build_heat_stress_chart_data(weather_transforms)
    deficit_data = _build_moisture_deficit_chart_data(weather_transforms)
    solar_data = _build_solar_chart_data(weather_transforms)

    gdd_layout = _default_chart_layout("Daily Growing Degree Days", "GDD")
    rainfall_layout = _default_chart_layout("Daily Rainfall (inches)", "inches")
    cum_gdd_layout = _build_cum_gdd_layout_with_stages(stage_medians)
    cum_rain_layout = _default_chart_layout("Cumulative Rainfall", "inches")
    heat_layout = _default_chart_layout("Heat Stress", "°F above 86°F")
    deficit_layout = _default_chart_layout("Cumulative Moisture Deficit", "inches")
    solar_layout = _build_solar_layout()
    temp_layout = {
        "title": {"text": "Daily Temperature Range (°F)", "font": {"size": 12}},
        "xaxis": {"title": "Day of year"},
        "yaxis": {"title": "Temperature (°F)"},
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }

    plotly_bundle = ensure_plotly_bundle(shared_dir)

    area_str = ""
    if "area_acres" in gdf.columns:
        area_str = f"{gdf.iloc[0].get('area_acres', 'N/A')} ac"
    crop_str = ""
    if crop_history:
        crop_str = f" | {crop_history[-1].get('crop_name', '')}"
    title = f"Field {field_id} — NDVI Dashboard"
    subtitle = f"{area_str}{crop_str} | {len(weather_transforms)} years weather | {len(ndvi_series)} clear-sky scenes"

    years = sorted({d["year"] for d in weather_transforms})

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
        temp_layout=temp_layout,
        temp_data=temp_data,
        combined_layout=combined_layout,
        combined_data=combined_data,
        heat_layout=heat_layout,
        heat_data=heat_data,
        deficit_layout=deficit_layout,
        deficit_data=deficit_data,
        solar_layout=solar_layout,
        solar_data=solar_data,
        composites=composites,
        crop_history=crop_history,
        years=years,
        stage_medians=stage_medians,
    )

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
    temp_layout: dict,
    temp_data: list[dict],
    combined_layout: dict,
    combined_data: list[dict],
    heat_layout: dict,
    heat_data: list[dict],
    deficit_layout: dict,
    deficit_data: list[dict],
    solar_layout: dict,
    solar_data: list[dict],
    composites: list[dict],
    crop_history: list[dict],
    years: list[int],
    stage_medians: list[dict],
) -> str:
    """Assemble the self-contained HTML for a single-field dashboard."""
    import json

    year_buttons = "\n".join(
        f'<button class="year-btn active" data-year="{y}" onclick="toggleYear(\'{y}\')">{y}</button>'
        for y in years
    )

    composite_html = ""
    if composites:
        items = "\n".join(
            f'<div class="composite-item"><img src="{c["src"]}" alt="{c["caption"]}" loading="lazy"><div class="caption">{c["caption"]}</div></div>'
            for c in composites
        )
        composite_html = f'<div class="composite-gallery">{items}</div>'

    stage_legend_html = ""
    if stage_medians:
        items = []
        for i, s in enumerate(stage_medians):
            items.append(
                f'<span class="stage-item"><span class="stage-swatch" style="background:{s["color"]}"></span>{s["stage"]} (doy {s["median_doy"]})</span>'
            )
        dividers = '<span class="stage-divider">→</span>'
        stage_legend_html = (
            '<div class="stage-legend-bar">'
            '<span class="stage-label">Corn Stages:</span>'
            + dividers.join(items)
            + '</div>'
        )

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
    temp_layout_json = json.dumps(temp_layout, default=str)
    temp_data_json = json.dumps(temp_data, default=str)
    combined_layout_json = json.dumps(combined_layout, default=str)
    combined_data_json = json.dumps(combined_data, default=str)
    heat_layout_json = json.dumps(heat_layout, default=str)
    heat_data_json = json.dumps(heat_data, default=str)
    deficit_layout_json = json.dumps(deficit_layout, default=str)
    deficit_data_json = json.dumps(deficit_data, default=str)
    solar_layout_json = json.dumps(solar_layout, default=str)
    solar_data_json = json.dumps(solar_data, default=str)

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
    <span>Year Filter (Temp + NDVI):</span>
    <div class="year-filter-bar">
        {year_buttons}
        <button class="year-btn global" onclick="setAllYears(true)">All Years</button>
        <button class="year-btn global" onclick="setAllYears(false)">None</button>
    </div>
    <span class="legend-inline"><span class="legend-symbol">&#9679;</span> Sentinel-2</span>
</div>
<div class="map-section">
    <h3>Field Boundary</h3>
    <div id="map-container"></div>
</div>
<div class="temp-section">
    <h3>Daily Temperature Range (°F)</h3>
    <div id="temp-chart" class="temp-chart-container"></div>
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
        {stage_legend_html}
    </div>
    <div class="chart-card">
        <h3>Cumulative Rainfall (inches)</h3>
        <div id="cumulative-rainfall-chart" class="chart-container"></div>
    </div>
</div>
<div class="grid grid-3">
    <div class="chart-card">
        <h3>Heat Stress</h3>
        <div id="heat-chart" class="chart-container"></div>
    </div>
    <div class="chart-card">
        <h3>Moisture Deficit</h3>
        <div id="deficit-chart" class="chart-container"></div>
    </div>
    <div class="chart-card">
        <h3>Solar Radiation</h3>
        <div id="solar-chart" class="chart-container"></div>
        <p class="chart-note">&lt; 2.6 kWh/m² considered low</p>
    </div>
</div>
<div class="combined-section">
    <h3>NDVI vs Cumulative GDD (with Corn Growth Stages)</h3>
    <div id="combined-chart" class="combined-chart-container"></div>
    {composite_html}
</div>
{crop_table_html}
<script>
// Unified year filtering
var activeYears = [{', '.join(str(y) for y in years)}];

function toggleYear(year) {{
    var btn = document.querySelector('.year-btn[data-year="' + year + '"]');
    var idx = activeYears.indexOf(parseInt(year));
    if (idx >= 0) {{
        activeYears.splice(idx, 1);
        if (btn) btn.classList.remove('active');
    }} else {{
        activeYears.push(parseInt(year));
        if (btn) btn.classList.add('active');
    }}
    updateAllCharts();
}}

function setAllYears(active) {{
    activeYears = active ? [{', '.join(str(y) for y in years)}] : [];
    document.querySelectorAll('.year-btn[data-year]').forEach(function(btn) {{
        if (active) btn.classList.add('active');
        else btn.classList.remove('active');
    }});
    updateAllCharts();
}}

function updateChartVisibility(chartId, years) {{
    var gd = document.getElementById(chartId);
    if (!gd || !gd.data) return;
    var visible = [];
    for (var i = 0; i < gd.data.length; i++) {{
        var d = gd.data[i];
        var show = years.indexOf(d.year) >= 0;
        visible.push(show ? true : 'legendonly');
    }}
    Plotly.restyle(gd, {{visible: visible}});
}}

function updateAllCharts() {{
    updateChartVisibility('temp-chart', activeYears);
    updateChartVisibility('combined-chart', activeYears);
    updateChartVisibility('heat-chart', activeYears);
    updateChartVisibility('deficit-chart', activeYears);
    updateChartVisibility('solar-chart', activeYears);
}}

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

var tempLayout = {temp_layout_json};
var tempData = {temp_data_json};
Plotly.newPlot('temp-chart', tempData, tempLayout, {{responsive: true}});

var combinedLayout = {combined_layout_json};
var combinedData = {combined_data_json};
Plotly.newPlot('combined-chart', combinedData, combinedLayout, {{responsive: true}});

var heatLayout = {heat_layout_json};
var heatData = {heat_data_json};
Plotly.newPlot('heat-chart', heatData, heatLayout, {{responsive: true}});

var deficitLayout = {deficit_layout_json};
var deficitData = {deficit_data_json};
Plotly.newPlot('deficit-chart', deficitData, deficitLayout, {{responsive: true}});

var solarLayout = {solar_layout_json};
var solarData = {solar_data_json};
Plotly.newPlot('solar-chart', solarData, solarLayout, {{responsive: true}});
</script>
</body>
</html>
"""
