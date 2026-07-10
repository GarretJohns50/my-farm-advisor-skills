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
from lib.doy_baseline import ensure_doy_temperature_baseline
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


def _build_rainfall_anomaly_chart_data(field_weather: list[dict]) -> tuple[list[dict], dict]:
    """Build monthly rainfall anomaly bar chart (Mar–Oct).

    For each month, computes the 5-year average monthly rainfall, then
    shows each year's deviation from that average. Surplus (green) and
    deficit (red) bars are grouped by month.
    """
    if not field_weather:
        return [], {}

    months = list(range(3, 11))  # Mar–Oct
    month_names = {3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
                   7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct"}

    # monthly_totals[year][month] = total inches
    monthly_totals: dict[int, dict[int, float]] = {}
    for year_rec in field_weather:
        year = year_rec["year"]
        daily = year_rec.get("daily", [])
        monthly_totals[year] = {}
        for month in months:
            month_days = [d for d in daily if int(d["date"].split("-")[1]) == month]
            total = sum(d.get("dailyRainfallIn", 0.0) for d in month_days)
            monthly_totals[year][month] = round(total, 2)

    # 5-year average per month
    month_avg: dict[int, float] = {}
    for month in months:
        totals = [monthly_totals[yr][month] for yr in monthly_totals]
        month_avg[month] = sum(totals) / len(totals)

    traces = []
    for i, year_rec in enumerate(field_weather):
        year = year_rec["year"]
        xs = []
        ys = []
        colors = []
        customdata = []
        for month in months:
            actual = monthly_totals[year][month]
            avg = month_avg[month]
            anomaly = actual - avg
            xs.append(month)
            ys.append(round(anomaly, 2))
            colors.append("#2ca02c" if anomaly >= 0 else "#d62728")
            customdata.append([actual, round(avg, 2)])
        traces.append({
            "type": "bar",
            "x": xs,
            "y": ys,
            "name": str(year),
            "marker": {"color": colors},
            "hovertemplate": (
                f"<b>{year}</b><br>"
                "%{text}<br>"
                "Anomaly: %{y:.2f} in<br>"
                "Actual: %{customdata[0]:.2f} in | Avg: %{customdata[1]:.2f} in"
                "<extra></extra>"
            ),
            "text": [month_names[m] for m in xs],
            "customdata": customdata,
            "year": year,
        })

    layout = {
        "title": {"text": "Monthly Rainfall Anomaly (Mar–Oct)", "font": {"size": 12}},
        "xaxis": {
            "title": "Month",
            "tickmode": "array",
            "tickvals": months,
            "ticktext": [month_names[m] for m in months],
        },
        "yaxis": {"title": "Rainfall anomaly (inches)"},
        "barmode": "group",
        "shapes": [{
            "type": "line",
            "x0": 0,
            "x1": 1,
            "xref": "paper",
            "y0": 0,
            "y1": 0,
            "line": {"color": "#000", "width": 2},
        }],
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }
    return traces, layout


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
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Solar: %{{y:.2f}} MJ/m²<extra></extra>",
            "year": year,
        })
    return traces


def _build_wind_chart_data(field_weather: list[dict]) -> list[dict]:
    """Build wind speed line chart data (mph, from WS10M)."""
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
        wind = [d.get("windSpeedMph", 0.0) for d in daily]
        traces.append({
            "type": "scatter",
            "mode": "lines",
            "x": doys,
            "y": wind,
            "name": str(year),
            "line": {"color": color, "width": 1.5},
            "hovertemplate": f"<b>{year}</b><br>Day: %{{x}}<br>Wind: %{{y:.1f}} mph<extra></extra>",
            "year": year,
        })
    return traces


def _build_solar_layout(stage_medians: list[dict]) -> dict:
    """Build solar chart layout with low-radiation threshold line and R-stage markers."""
    layout = {
        "title": {"text": "Solar Radiation", "font": {"size": 12}},
        "xaxis": {"title": "Day of year", "range": [60, 305]},
        "yaxis": {"title": "MJ/m²/day"},
        "shapes": [{
            "type": "line",
            "x0": 0,
            "x1": 1,
            "xref": "paper",
            "y0": 18,
            "y1": 18,
            "line": {"color": "#999", "width": 1.5, "dash": "dash"},
        }],
        "annotations": [{
            "x": 1.0,
            "xref": "paper",
            "y": 18,
            "text": "Low threshold: 18",
            "showarrow": False,
            "font": {"size": 9, "color": "#666"},
            "xanchor": "right",
            "yanchor": "bottom",
        }],
        "margin": {"l": 50, "r": 20, "t": 40, "b": 55},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }
    # Add R-stage vertical lines (no V stages)
    for stage in stage_medians:
        if not stage["stage"].startswith("R"):
            continue
        layout["shapes"].append({
            "type": "line",
            "x0": stage["median_doy"],
            "x1": stage["median_doy"],
            "y0": 0,
            "y1": 1,
            "yref": "paper",
            "line": {"color": stage["color"], "width": 1.5, "dash": "dot"},
        })
        layout["annotations"].append({
            "x": stage["median_doy"],
            "y": -0.12,
            "yref": "paper",
            "text": f"{stage['stage']} — {stage['name']}",
            "showarrow": False,
            "font": {"size": 8, "color": stage["color"]},
            "bgcolor": "rgba(255,255,255,0.8)",
            "borderpad": 2,
            "align": "center",
        })
    return layout


def _build_wind_layout() -> dict:
    """Build wind chart layout with 30 mph threshold line."""
    return {
        "title": {"text": "Wind Speed", "font": {"size": 12}},
        "xaxis": {"title": "Day of year", "range": [60, 305]},
        "yaxis": {"title": "Wind speed (mph)"},
        "shapes": [{
            "type": "line",
            "x0": 0,
            "x1": 1,
            "xref": "paper",
            "y0": 30,
            "y1": 30,
            "line": {"color": "#ff7f0e", "width": 1.5, "dash": "dash"},
        }],
        "annotations": [{
            "x": 1.0,
            "xref": "paper",
            "y": 30,
            "text": "R-stage threshold: 30 mph",
            "showarrow": False,
            "font": {"size": 9, "color": "#ff7f0e"},
            "xanchor": "right",
            "yanchor": "bottom",
        }],
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }


def _detect_2025_events(
    field_weather: list[dict],
    ndvi_series: list[dict],
    stage_medians: list[dict],
    baseline_path: Path | None,
) -> list[dict]:
    """Detect critical agronomic events for year 2025 against 5-year baselines.

    Returns a list of event dicts with severity, chart target, and agronomic
    context for rendering in the dashboard summary panel and chart annotations.
    """
    events: list[dict] = []

    # Locate 2025 data
    weather_2025 = next((r for r in field_weather if r["year"] == 2025), None)
    if weather_2025 is None:
        return events

    daily_2025 = weather_2025.get("daily", [])
    if not daily_2025:
        return events

    # Load DOY baseline for cool-period detection
    baseline: dict[int, dict[str, float]] = {}
    if baseline_path and baseline_path.exists():
        try:
            raw = json.loads(baseline_path.read_text(encoding="utf-8"))
            baseline = {int(k): v for k, v in raw.get("doy_stats", {}).items()}
        except Exception:
            pass

    # Build 5-year averages for rainfall context
    monthly_totals: dict[int, dict[int, float]] = {}
    for r in field_weather:
        yr = r["year"]
        daily = r.get("daily", [])
        monthly_totals[yr] = {}
        for month in range(3, 11):
            month_days = [d for d in daily if int(d["date"].split("-")[1]) == month]
            monthly_totals[yr][month] = sum(d.get("dailyRainfallIn", 0.0) for d in month_days)
    month_avg: dict[int, float] = {}
    for month in range(3, 11):
        totals = [monthly_totals[yr][month] for yr in monthly_totals]
        month_avg[month] = sum(totals) / len(totals) if totals else 0.0

    # R-stage DOY window
    r_stages = [s for s in stage_medians if s["stage"].startswith("R")]
    r1_doy = next((s["median_doy"] for s in r_stages if s["stage"] == "R1"), None)
    r6_doy = next((s["median_doy"] for s in r_stages if s["stage"] == "R6"), None)

    # Helper: severity from thresholds
    def _sev(value: float, mild: float, moderate: float) -> str:
        if value >= moderate:
            return "severe"
        if value >= mild:
            return "moderate"
        return "mild"

    # 1. Heavy rain events (>1.0 in/day)
    for d in daily_2025:
        rain = d.get("dailyRainfallIn", 0.0)
        if rain > 1.0:
            month = int(d["date"].split("-")[1])
            avg = month_avg.get(month, 0.0)
            ratio = round(rain / avg, 1) if avg > 0 else 0.0
            sev = _sev(rain, 1.5, 2.5)
            events.append({
                "type": "heavy_rain",
                "display_name": "Heavy Rain",
                "year": 2025,
                "start_doy": d["dayOfYear"],
                "end_doy": d["dayOfYear"],
                "start_date": d["date"],
                "end_date": d["date"],
                "value": round(rain, 2),
                "unit": "in",
                "severity": sev,
                "chart_target": "rainfall",
                "description": f"{rain:.1f} in rainfall on DOY {d['dayOfYear']}",
                "agronomic_note": f"{ratio:.1f}× above 5-year {['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][month]} average. May cause nitrogen leaching or saturated soils.",
                "baseline_context": {"month_avg": round(avg, 2), "ratio": ratio},
            })

    # 2. Heat wave events (3+ consecutive days max temp >86°F)
    tmax_f = [(d["dayOfYear"], d["date"], d.get("t2m_max", 0.0) * 9.0 / 5.0 + 32.0) for d in daily_2025]
    streak = 0
    streak_start = 0
    for i, (doy, date, temp_f) in enumerate(tmax_f):
        if temp_f > 86.0:
            if streak == 0:
                streak_start = i
            streak += 1
        else:
            if streak >= 3:
                start = tmax_f[streak_start]
                end = tmax_f[i - 1]
                max_temp = max(tmax_f[j][2] for j in range(streak_start, i))
                sev = _sev(streak, 5, 7)
                events.append({
                    "type": "heat_wave",
                    "display_name": "Heat Wave",
                    "year": 2025,
                    "start_doy": start[0],
                    "end_doy": end[0],
                    "start_date": start[1],
                    "end_date": end[1],
                    "value": streak,
                    "unit": "days",
                    "severity": sev,
                    "chart_target": "temp",
                    "description": f"{streak} consecutive days >86°F (DOY {start[0]}–{end[0]})",
                    "agronomic_note": f"Peak {max_temp:.1f}°F. Heat stress during pollination can reduce kernel set.",
                    "baseline_context": {},
                })
            streak = 0
    if streak >= 3:
        start = tmax_f[streak_start]
        end = tmax_f[-1]
        max_temp = max(tmax_f[j][2] for j in range(streak_start, len(tmax_f)))
        sev = _sev(streak, 5, 7)
        events.append({
            "type": "heat_wave",
            "display_name": "Heat Wave",
            "year": 2025,
            "start_doy": start[0],
            "end_doy": end[0],
            "start_date": start[1],
            "end_date": end[1],
            "value": streak,
            "unit": "days",
            "severity": sev,
            "chart_target": "temp",
            "description": f"{streak} consecutive days >86°F (DOY {start[0]}–{end[0]})",
            "agronomic_note": f"Peak {max_temp:.1f}°F. Heat stress during pollination can reduce kernel set.",
            "baseline_context": {},
        })

    # 3. Cool period events (3+ consecutive days avg temp >10°F below DOY baseline)
    if baseline:
        tavg_f = [(d["dayOfYear"], d["date"], d.get("t2m_avg", 0.0) * 9.0 / 5.0 + 32.0) for d in daily_2025]
        streak = 0
        streak_start = 0
        for i, (doy, date, temp_f) in enumerate(tavg_f):
            doy_stats = baseline.get(doy, {})
            base_t = doy_stats.get("mean_t2m", 0.0) * 9.0 / 5.0 + 32.0
            deficit = base_t - temp_f
            if deficit > 10.0:
                if streak == 0:
                    streak_start = i
                streak += 1
            else:
                if streak >= 3:
                    start = tavg_f[streak_start]
                    end = tavg_f[i - 1]
                    max_deficit = max(base_t - tavg_f[j][2] for j in range(streak_start, i))
                    sev = _sev(max_deficit, 15, 20)
                    events.append({
                        "type": "cool_period",
                        "display_name": "Cool Period",
                        "year": 2025,
                        "start_doy": start[0],
                        "end_doy": end[0],
                        "start_date": start[1],
                        "end_date": end[1],
                        "value": round(max_deficit, 1),
                        "unit": "°F below normal",
                        "severity": sev,
                        "chart_target": "temp",
                        "description": f"{streak} days >10°F below normal (DOY {start[0]}–{end[0]})",
                        "agronomic_note": f"Max deficit {max_deficit:.1f}°F below 5-year DOY average. Cool, cloudy weather slows GDD accumulation.",
                        "baseline_context": {},
                    })
                streak = 0
        if streak >= 3:
            start = tavg_f[streak_start]
            end = tavg_f[-1]
            max_deficit = max(base_t - tavg_f[j][2] for j in range(streak_start, len(tavg_f)))
            sev = _sev(max_deficit, 15, 20)
            events.append({
                "type": "cool_period",
                "display_name": "Cool Period",
                "year": 2025,
                "start_doy": start[0],
                "end_doy": end[0],
                "start_date": start[1],
                "end_date": end[1],
                "value": round(max_deficit, 1),
                "unit": "°F below normal",
                "severity": sev,
                "chart_target": "temp",
                "description": f"{streak} days >10°F below normal (DOY {start[0]}–{end[0]})",
                "agronomic_note": f"Max deficit {max_deficit:.1f}°F below 5-year DOY average. Cool, cloudy weather slows GDD accumulation.",
                "baseline_context": {},
            })

    # 4. NDVI dip events (>0.10 drop between consecutive scenes)
    ndvi_2025 = [d for d in ndvi_series if d["year"] == 2025]
    ndvi_2025.sort(key=lambda x: x["doy"])
    for i in range(1, len(ndvi_2025)):
        prev = ndvi_2025[i - 1]
        curr = ndvi_2025[i]
        drop = prev["mean_ndvi"] - curr["mean_ndvi"]
        if drop > 0.10:
            sev = _sev(drop, 0.15, 0.25)
            events.append({
                "type": "ndvi_dip",
                "display_name": "NDVI Dip",
                "year": 2025,
                "start_doy": curr["doy"],
                "end_doy": curr["doy"],
                "start_date": curr["date"],
                "end_date": curr["date"],
                "value": round(drop, 3),
                "unit": "NDVI",
                "severity": sev,
                "chart_target": "combined",
                "description": f"NDVI dropped {drop:.3f} from {prev['mean_ndvi']:.3f} to {curr['mean_ndvi']:.3f} (DOY {curr['doy']})",
                "agronomic_note": "Sharp NDVI decline may indicate drought stress, disease, hail damage, or end-of-season senescence.",
                "baseline_context": {},
            })

    # 5. NDVI surge events (>0.15 increase between consecutive scenes)
    for i in range(1, len(ndvi_2025)):
        prev = ndvi_2025[i - 1]
        curr = ndvi_2025[i]
        rise = curr["mean_ndvi"] - prev["mean_ndvi"]
        if rise > 0.15:
            sev = _sev(rise, 0.20, 0.30)
            events.append({
                "type": "ndvi_surge",
                "display_name": "NDVI Surge",
                "year": 2025,
                "start_doy": curr["doy"],
                "end_doy": curr["doy"],
                "start_date": curr["date"],
                "end_date": curr["date"],
                "value": round(rise, 3),
                "unit": "NDVI",
                "severity": sev,
                "chart_target": "combined",
                "description": f"NDVI surged {rise:.3f} from {prev['mean_ndvi']:.3f} to {curr['mean_ndvi']:.3f} (DOY {curr['doy']})",
                "agronomic_note": "Rapid NDVI increase indicates rapid canopy development, often following rain after dry conditions.",
                "baseline_context": {},
            })

    # 6. Low solar events during R-stages (<18 MJ/m²)
    if r1_doy and r6_doy:
        for d in daily_2025:
            doy = d["dayOfYear"]
            if r1_doy <= doy <= r6_doy:
                solar = d.get("solarRadiation", 0.0)
                if solar < 18.0:
                    sev = _sev(18.0 - solar, 3, 6)
                    events.append({
                        "type": "low_solar",
                        "display_name": "Low Solar",
                        "year": 2025,
                        "start_doy": doy,
                        "end_doy": doy,
                        "start_date": d["date"],
                        "end_date": d["date"],
                        "value": round(solar, 1),
                        "unit": "MJ/m²",
                        "severity": sev,
                        "chart_target": "solar",
                        "description": f"{solar:.1f} MJ/m² on DOY {doy} during {stage_medians[0].get('crop', 'corn')} R-stages",
                        "agronomic_note": "Low solar radiation during grain-fill reduces photosynthesis and can lower yield potential.",
                        "baseline_context": {},
                    })

    # 7. High wind events during R-stages (>30 mph)
    if r1_doy and r6_doy:
        for d in daily_2025:
            doy = d["dayOfYear"]
            if r1_doy <= doy <= r6_doy:
                wind = d.get("windSpeedMph", 0.0)
                if wind > 30.0:
                    sev = _sev(wind, 35, 40)
                    events.append({
                        "type": "high_wind",
                        "display_name": "High Wind",
                        "year": 2025,
                        "start_doy": doy,
                        "end_doy": doy,
                        "start_date": d["date"],
                        "end_date": d["date"],
                        "value": round(wind, 1),
                        "unit": "mph",
                        "severity": sev,
                        "chart_target": "wind",
                        "description": f"{wind:.1f} mph wind on DOY {doy} during R-stages",
                        "agronomic_note": "Strong winds during reproductive stages can cause root lodging, greensnap, or silk desiccation.",
                        "baseline_context": {},
                    })

    # Deduplicate: merge same-type events within 3 DOY
    events.sort(key=lambda e: (e["type"], e["start_doy"]))
    deduped: list[dict] = []
    for e in events:
        if deduped and e["type"] == deduped[-1]["type"] and e["start_doy"] - deduped[-1]["end_doy"] <= 3:
            # merge
            prev = deduped[-1]
            prev["end_doy"] = e["end_doy"]
            prev["end_date"] = e["end_date"]
            if e["value"] > prev["value"]:
                prev["value"] = e["value"]
                prev["severity"] = e["severity"]
                prev["description"] = e["description"]
            prev["description"] = f"{prev['type']} events DOY {prev['start_doy']}–{prev['end_doy']}"
        else:
            deduped.append(e)

    return deduped


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
        "xaxis": {"title": "Day of year", "range": [60, 305]},
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


def _default_chart_layout(title: str, y_title: str | None = None, x_range: tuple[int, int] | None = None) -> dict:
    lo = {
        "title": {"text": title, "font": {"size": 12}},
        "xaxis": {"title": "Day of year"},
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }
    if y_title:
        lo["yaxis"] = {"title": y_title}
    if x_range:
        lo["xaxis"]["range"] = list(x_range)
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
        "xaxis": {"title": "Day of year", "range": [60, 305]},
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
    # Always embed satellite basemap for field dashboard map
    basemap_b64, mercator_extent = fetch_basemap(
        gdf, cache_dir=cache_dir, no_basemap=False, force_refresh=force_basemap
    )

    map_data, map_layout = _build_field_map_data(gdf, basemap_b64, mercator_extent, weather_transforms)
    gdd_data, rainfall_data, cum_gdd_data, cum_rain_data = _build_weather_charts_single_field(weather_transforms)
    temp_data = _build_temp_chart_data(weather_transforms)
    stage_medians = _compute_stage_median_doys(weather_transforms)
    combined_data, combined_layout = _build_combined_ndvi_gdd_chart_data(ndvi_series, weather_transforms, stage_medians)
    heat_data = _build_heat_stress_chart_data(weather_transforms)
    anomaly_data, anomaly_layout = _build_rainfall_anomaly_chart_data(weather_transforms)
    solar_data = _build_solar_chart_data(weather_transforms)
    wind_data = _build_wind_chart_data(weather_transforms)

    # Ensure DOY temperature baseline for cool-period detection
    fips_code = ""
    if "fips" in gdf.columns:
        fips_code = str(gdf.iloc[0].get("fips", "")).zfill(5)
    elif "COUNTYFP" in gdf.columns:
        fips_code = str(gdf.iloc[0].get("COUNTYFP", "")).zfill(5)
    baseline_path = None
    if fips_code and len(fips_code) == 5:
        baseline_path = ensure_doy_temperature_baseline(fips_code, shared_dir)

    events = _detect_2025_events(weather_transforms, ndvi_series, stage_medians, baseline_path)

    gdd_layout = _default_chart_layout("Daily Growing Degree Days", "GDD", x_range=(60, 305))
    rainfall_layout = _default_chart_layout("Daily Rainfall (inches)", "inches", x_range=(60, 305))
    cum_gdd_layout = _build_cum_gdd_layout_with_stages(stage_medians)
    cum_rain_layout = _default_chart_layout("Cumulative Rainfall", "inches", x_range=(60, 305))
    heat_layout = _default_chart_layout("Heat Stress", "°F above 86°F", x_range=(60, 305))
    solar_layout = _build_solar_layout(stage_medians)
    temp_layout = {
        "title": {"text": "Daily Temperature Range (°F)", "font": {"size": 12}},
        "xaxis": {"title": "Day of year", "range": [60, 305]},
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
        anomaly_layout=anomaly_layout,
        anomaly_data=anomaly_data,
        solar_layout=solar_layout,
        solar_data=solar_data,
        wind_layout=_build_wind_layout(),
        wind_data=wind_data,
        composites=composites,
        crop_history=crop_history,
        years=years,
        stage_medians=stage_medians,
        events=events,
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
    anomaly_layout: dict,
    anomaly_data: list[dict],
    solar_layout: dict,
    solar_data: list[dict],
    wind_layout: dict,
    wind_data: list[dict],
    composites: list[dict],
    crop_history: list[dict],
    years: list[int],
    stage_medians: list[dict],
    events: list[dict],
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

    # Build 2025 critical events summary HTML
    events_2025 = [e for e in events if e["year"] == 2025]
    event_type_icons = {
        "heavy_rain": "&#127783;",
        "heat_wave": "&#127777;",
        "cool_period": "&#10052;",
        "ndvi_dip": "&#128315;",
        "ndvi_surge": "&#128314;",
        "low_solar": "&#9728;",
        "high_wind": "&#128168;",
    }
    event_type_labels = {
        "heavy_rain": "Rain",
        "heat_wave": "Heat",
        "cool_period": "Cool",
        "ndvi_dip": "NDVI Dip",
        "ndvi_surge": "NDVI Surge",
        "low_solar": "Solar",
        "high_wind": "Wind",
    }
    # Count events by type for chips
    type_counts: dict[str, int] = {}
    for e in events_2025:
        type_counts[e["type"]] = type_counts.get(e["type"], 0) + 1
    event_chips = []
    # "All" chip — active by default
    event_chips.append(
        f'<button class="event-chip active" data-filter="all" onclick="filterEvents(\'all\')">All ({len(events_2025)})</button>'
    )
    for etype, count in sorted(type_counts.items()):
        sev = "moderate" if any(e["severity"] == "moderate" for e in events_2025 if e["type"] == etype) else "mild"
        if any(e["severity"] == "severe" for e in events_2025 if e["type"] == etype):
            sev = "severe"
        event_chips.append(
            f'<button class="event-chip {sev}" data-filter="{etype}" onclick="filterEvents(\'{etype}\')">{event_type_icons.get(etype, "")} {count} {event_type_labels.get(etype, etype)}</button>'
        )
    from datetime import datetime

    def _fmt_date(iso: str) -> str:
        """Format ISO date as 'Mon D' (e.g., 'Jun 1')."""
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d")

    events_summary_html = ""
    events_grid_html = ""
    if events_2025:
        chips = "\n".join(event_chips)
        cards = []
        for e in events_2025:
            sev = e.get("severity", "mild")
            start_fmt = _fmt_date(e["start_date"])
            end_fmt = _fmt_date(e["end_date"])
            date_range = start_fmt if e["start_date"] == e["end_date"] else f"{start_fmt} – {end_fmt}"
            cards.append(
                f'<div class="event-card {sev}" data-event-type="{e["type"]}" onclick="toggleEventDetail(this)">'
                f'<div class="event-header">'
                f'<span class="event-title">{event_type_icons.get(e["type"], "")} {e["display_name"]}'
                f'<span class="severity-badge {sev}">{sev}</span></span>'
                f'<span class="event-date">{date_range}</span></div>'
                f'<div class="event-value">{e["value"]} {e["unit"]}</div>'
                f'<div class="event-context">{e["description"]}</div>'
                f'<div class="event-detail">'
                f'<div>{e.get("agronomic_note", "")}</div>'
                f'</div></div>'
            )
        events_grid_html = "\n".join(cards)
        events_summary_html = f"""\
<div class="events-panel">
    <div class="events-summary">
        <div class="events-chips">
            <strong>2025 Critical Events:</strong>
            {chips}
        </div>
        <button class="expand-btn" id="events-expand-btn" onclick="toggleEventsPanel()">&#9660; Expand</button>
    </div>
    <div class="events-grid collapsed" id="events-grid">
        {events_grid_html}
    </div>
</div>"""

    # Severe event annotations for charts (max 3 per chart)
    chart_annotations: dict[str, list[dict]] = {}
    for e in events_2025:
        if e["severity"] != "severe":
            continue
        target = e.get("chart_target", "")
        if target not in chart_annotations:
            chart_annotations[target] = []
        if len(chart_annotations[target]) >= 3:
            continue
        chart_annotations[target].append({
            "x": e["start_doy"],
            "y": 1.0,
            "xref": "x",
            "yref": "paper",
            "text": f"{event_type_icons.get(e['type'], '')} {e['display_name']}",
            "showarrow": True,
            "arrowhead": 2,
            "arrowsize": 1,
            "arrowwidth": 1,
            "ax": 0,
            "ay": -30,
            "font": {"size": 8, "color": "#333"},
            "bgcolor": "rgba(255,255,255,0.85)",
            "borderpad": 2,
        })

    def _inject_annotations(layout: dict, target: str) -> dict:
        if target in chart_annotations:
            if "annotations" not in layout:
                layout["annotations"] = []
            layout["annotations"].extend(chart_annotations[target])
        return layout

    # Inject severe event annotations into chart layouts
    temp_layout = _inject_annotations(temp_layout, "temp")
    rainfall_layout = _inject_annotations(rainfall_layout, "rainfall")
    solar_layout = _inject_annotations(solar_layout, "solar")
    combined_layout = _inject_annotations(combined_layout, "combined")
    # Wind chart doesn't get event annotations (events are flagged, not annotated on chart)

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
    anomaly_layout_json = json.dumps(anomaly_layout, default=str)
    anomaly_data_json = json.dumps(anomaly_data, default=str)
    solar_layout_json = json.dumps(solar_layout, default=str)
    solar_data_json = json.dumps(solar_data, default=str)
    wind_layout_json = json.dumps(wind_layout, default=str)
    wind_data_json = json.dumps(wind_data, default=str)

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
{events_summary_html}
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
<div class="grid grid-4">
    <div class="chart-card">
        <h3>Heat Stress</h3>
        <div id="heat-chart" class="chart-container"></div>
    </div>
    <div class="chart-card">
        <h3>Rainfall Anomaly</h3>
        <div id="anomaly-chart" class="chart-container"></div>
    </div>
    <div class="chart-card">
        <h3>Solar Radiation</h3>
        <div id="solar-chart" class="chart-container"></div>
        <p class="chart-note">&lt; 18 MJ/m² considered low</p>
    </div>
    <div class="chart-card">
        <h3>Wind Speed</h3>
        <div id="wind-chart" class="chart-container"></div>
        <p class="chart-note">&gt; 30 mph threshold during R-stages</p>
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
    updateChartVisibility('gdd-chart', activeYears);
    updateChartVisibility('rainfall-chart', activeYears);
    updateChartVisibility('cumulative-gdd-chart', activeYears);
    updateChartVisibility('cumulative-rainfall-chart', activeYears);
    updateChartVisibility('combined-chart', activeYears);
    updateChartVisibility('heat-chart', activeYears);
    updateChartVisibility('anomaly-chart', activeYears);
    updateChartVisibility('solar-chart', activeYears);
    updateChartVisibility('wind-chart', activeYears);
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

var anomalyLayout = {anomaly_layout_json};
var anomalyData = {anomaly_data_json};
Plotly.newPlot('anomaly-chart', anomalyData, anomalyLayout, {{responsive: true}});

var solarLayout = {solar_layout_json};
var solarData = {solar_data_json};
Plotly.newPlot('solar-chart', solarData, solarLayout, {{responsive: true}});

var windLayout = {wind_layout_json};
var windData = {wind_data_json};
Plotly.newPlot('wind-chart', windData, windLayout, {{responsive: true}});

function toggleEventsPanel() {{
    var grid = document.getElementById('events-grid');
    var btn = document.getElementById('events-expand-btn');
    if (!grid || !btn) return;
    if (grid.classList.contains('collapsed')) {{
        grid.classList.remove('collapsed');
        btn.innerHTML = '&#9650; Collapse';
    }} else {{
        grid.classList.add('collapsed');
        btn.innerHTML = '&#9660; Expand';
    }}
}}

function filterEvents(type) {{
    document.querySelectorAll('.event-card').forEach(function(card) {{
        card.style.display = (type === 'all' || card.dataset.eventType === type) ? 'block' : 'none';
    }});
    document.querySelectorAll('.event-chip').forEach(function(chip) {{
        chip.classList.toggle('active', chip.dataset.filter === type);
    }});
}}

function toggleEventDetail(card) {{
    var detail = card.querySelector('.event-detail');
    if (!detail) return;
    detail.classList.toggle('visible');
}}
</script>
</body>
</html>
"""
