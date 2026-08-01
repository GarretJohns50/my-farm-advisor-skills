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
import math
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
from lib.agronomic_events import detect_critical_events, CROP_GROWTH_STAGES
from lib.doy_baseline import ensure_doy_temperature_baseline
from lib.sufficiency_ranges import (
    DISPLAY_NUTRIENTS,
    get_nutrient_ratings,
    load_sufficiency_ranges,
    SUFFICIENCY_COLORS,
)
from lib.weather_transforms import compute_weather_transforms, parse_daily_weather

# Default to corn stages; can be overridden per crop
CORN_GROWTH_STAGES: list[dict] = CROP_GROWTH_STAGES["corn"]


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
    grid_gdf: gpd.GeoDataFrame | None = None,
) -> tuple[list[dict], dict, list[dict], dict]:
    """Build Plotly map data and layout for a single field (soil map + spread map)."""
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

    # --- Multi-year sampling grid overlay ---
    year_grids: dict[int, gpd.GeoDataFrame] = grid_gdf if isinstance(grid_gdf, dict) else {}

    # Soil columns and metadata
    soil_cols = [
        "pH", "BpH", "OM", "P", "K", "S", "ZN",
        "CA", "MG", "NA", "CEC", "H_SAT", "K_SAT",
        "CA_SAT", "MG_SAT", "NA_SAT", "NO3",
    ]
    nutrient_index_map = {
        "pH": 2, "BpH": 3, "OM": 4, "P": 5, "K": 6, "S": 7,
        "ZN": 8, "CA": 9, "MG": 10, "NA": 11, "CEC": 12,
        "H_SAT": 13, "K_SAT": 14, "CA_SAT": 15, "MG_SAT": 16,
        "NA_SAT": 17, "NO3": 18,
    }
    nutrient_display_names = {
        "pH": "pH", "BpH": "Buffer pH", "OM": "Organic Matter (%)",
        "P": "Phosphorus (P)", "K": "Potassium (K)", "S": "Sulfur (S)",
        "ZN": "Zinc (Zn)", "CA": "Calcium (Ca)", "MG": "Magnesium (Mg)",
        "NA": "Sodium (Na)", "CEC": "CEC",
        "H_SAT": "H Saturation (%)", "K_SAT": "K Saturation (%)",
        "CA_SAT": "Ca Saturation (%)", "MG_SAT": "Mg Saturation (%)",
        "NA_SAT": "Na Saturation (%)", "NO3": "Nitrate (NO₃)",
    }
    # 3-stop color scale: low=red, mid=yellow, high=green
    COLOR_LOW = [0xd6, 0x27, 0x28]   # #d62728
    COLOR_MID = [0xff, 0xeb, 0x3b]   # #ffeb3b
    COLOR_HIGH = [0x2c, 0xa0, 0x2c]  # #2ca02c

    def _lerp_color(t: float) -> str:
        """Linearly interpolate between low→mid→high based on t in [0,1]."""
        if t <= 0.5:
            s = t * 2
            r = int(COLOR_LOW[0] + s * (COLOR_MID[0] - COLOR_LOW[0]))
            g = int(COLOR_LOW[1] + s * (COLOR_MID[1] - COLOR_LOW[1]))
            b = int(COLOR_LOW[2] + s * (COLOR_MID[2] - COLOR_LOW[2]))
        else:
            s = (t - 0.5) * 2
            r = int(COLOR_MID[0] + s * (COLOR_HIGH[0] - COLOR_MID[0]))
            g = int(COLOR_MID[1] + s * (COLOR_HIGH[1] - COLOR_MID[1]))
            b = int(COLOR_MID[2] + s * (COLOR_HIGH[2] - COLOR_MID[2]))
        return f"#{r:02x}{g:02x}{b:02x}"

    # Gather all year data for shared color scales and JS comparison table
    all_soil_data: dict[int, list[list[str]]] = {}  # year -> [point1_values, ...]
    all_point_coords: dict[int, tuple[list[float], list[float]]] = {}  # year -> (xs, ys)
    all_point_meta: dict[int, tuple[list[str], list[str], list[str]]] = {}  # year -> (nums, lats, lons)
    _zone_acres_by_point: dict[str, float] | None = None

    for year, gdf_year in sorted(year_grids.items()):
        try:
            if use_mercator:
                if gdf_year.crs is None or gdf_year.crs.to_epsg() != 3857:
                    grid_plot = gdf_year.to_crs(epsg=3857)
                else:
                    grid_plot = gdf_year.copy()
            else:
                if gdf_year.crs is not None and gdf_year.crs.to_epsg() != 4326:
                    grid_plot = gdf_year.to_crs(epsg=4326)
                else:
                    grid_plot = gdf_year.copy()

            xs = grid_plot.geometry.x.tolist()
            ys = grid_plot.geometry.y.tolist()
            nums = grid_plot["zone_num"].astype(str).tolist()
            lats = grid_plot["lat"].round(6).astype(str).tolist()
            lons = grid_plot["lon"].round(6).astype(str).tolist()
            zone_acres = grid_plot["zone_acres"].astype(float).tolist()

            soil_data: list[list[str]] = []
            for i in range(len(grid_plot)):
                row = grid_plot.iloc[i]
                soil_data.append([str(row.get(c, "")) for c in soil_cols])

            all_soil_data[year] = soil_data
            all_point_coords[year] = (xs, ys)
            all_point_meta[year] = (nums, lats, lons)
            _zone_acres_by_point = dict(zip(nums, zone_acres))
        except Exception:
            continue

    # Compute shared color scales across ALL years
    nutrient_colors_by_year: dict[int, dict[str, list[str]]] = {}
    for col_name, idx in nutrient_index_map.items():
        # Collect all values across all years
        all_vals = []
        for year, soil_data in all_soil_data.items():
            for row in soil_data:
                try:
                    all_vals.append(float(row[idx - 2]))
                except (ValueError, TypeError):
                    all_vals.append(float("nan"))
        valid = [v for v in all_vals if not math.isnan(v)]
        vmin = min(valid) if valid else 0.0
        vmax = max(valid) if valid else 1.0
        vrange = vmax - vmin if vmax > vmin else 1.0

        # Generate colors per year using shared scale
        for year, soil_data in all_soil_data.items():
            if year not in nutrient_colors_by_year:
                nutrient_colors_by_year[year] = {}
            colors = []
            for row in soil_data:
                try:
                    v = float(row[idx - 2])
                except (ValueError, TypeError):
                    colors.append("#999999")
                    continue
                if math.isnan(v):
                    colors.append("#999999")
                else:
                    t = (v - vmin) / vrange
                    colors.append(_lerp_color(t))
            nutrient_colors_by_year[year][col_name] = colors

    # Build JS comparison data: {point_num: {year: {nutrient: value}}}
    js_soil_data: dict[str, dict[str, dict[str, str]]] = {}
    for year, soil_data in all_soil_data.items():
        nums = all_point_meta[year][0]
        for i, num in enumerate(nums):
            if num not in js_soil_data:
                js_soil_data[num] = {}
            js_soil_data[num][str(year)] = {}
            for col_name, idx in nutrient_index_map.items():
                js_soil_data[num][str(year)][col_name] = soil_data[i][idx - 2]

    # Determine default map year from available grid years for initial visibility
    default_map_year = max(all_soil_data.keys()) if all_soil_data else 2025

    # Create one trace per year
    default_nutrient = "pH"
    for year in sorted(all_soil_data.keys()):
        soil_data = all_soil_data[year]
        xs, ys = all_point_coords[year]
        nums, lats, lons = all_point_meta[year]

        customdata = []
        for i in range(len(lons)):
            cd = [lons[i], lats[i]] + soil_data[i] + [nums[i]]
            customdata.append(cd)

        has_soil = any(v not in ("", "None", "nan") for v in soil_data[0][:1])

        # Default hover shows selected nutrient (pH by default) + lat/lon
        # Use customdata[19] for point number since text now shows nutrient value
        hovertemplate = (
            "<b>Sample Point %{customdata[19]}</b><br>"
            "Year: " + str(year) + "<br>"
            "pH: %{customdata[2]} | OM: %{customdata[4]}%<br>"
            "P: %{customdata[5]} | K: %{customdata[6]}<br>"
            "Lat: %{customdata[1]} | Lon: %{customdata[0]}<extra></extra>"
        )
        if not has_soil:
            hovertemplate = "<b>Sample Point %{customdata[19]}</b><br><i>Soil data: not loaded</i><extra></extra>"

        default_colors = nutrient_colors_by_year.get(year, {}).get(default_nutrient, ["#ffeb3b"] * len(xs))
        # Initial text labels show default nutrient value (pH) instead of point number
        default_idx = nutrient_index_map.get(default_nutrient, 2)
        default_text = [str(cd[default_idx]) for cd in customdata]

        # Marker trace (no text) — keeps click/hover semantics
        map_data.append({
            "type": "scatter",
            "mode": "markers",
            "x": xs,
            "y": ys,
            "customdata": customdata,
            "marker": {
                "size": 20,
                "color": default_colors,
                "line": {"color": "#333", "width": 1},
            },
            "name": f"Sample Points {year}",
            "hovertemplate": hovertemplate,
            "showlegend": False,
            "year": year,
            "role": "markers",
            "visible": True if year == default_map_year else "legendonly",
        })
        # Label trace (text only) — drawn above markers so glyphs are never
        # clipped by the marker outline (which previously looked like stray
        # "(" / "'" / ")" fragments around digits).
        map_data.append({
            "type": "scatter",
            "mode": "text",
            "x": xs,
            "y": ys,
            "text": default_text,
            "textposition": "middle center",
            "textfont": {"size": 13, "color": "#000", "family": "Arial Black"},
            "hoverinfo": "skip",
            "showlegend": False,
            "year": year,
            "role": "labels",
            "visible": True if year == default_map_year else "legendonly",
        })

    _nutrient_colors_by_year = nutrient_colors_by_year
    _nutrient_index_map = nutrient_index_map
    _nutrient_display_names = nutrient_display_names
    _js_soil_data = js_soil_data
    _available_years = sorted(all_soil_data.keys())

    layout = {
        "title": {"text": "Soil Sample Data", "font": {"size": 14}},
        "xaxis": {"showgrid": False, "zeroline": False, "showticklabels": False, "autorange": False},
        "yaxis": {"showgrid": False, "zeroline": False, "showticklabels": False, "scaleanchor": "x", "scaleratio": 1, "autorange": False},
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

    # Add nutrient metadata to layout (if available)
    layout["_nutrient_colors_by_year"] = _nutrient_colors_by_year
    layout["_nutrient_index_map"] = _nutrient_index_map
    layout["_nutrient_display_names"] = _nutrient_display_names
    layout["_js_soil_data"] = _js_soil_data
    layout["_available_years"] = _available_years
    layout["_zone_acres_by_point"] = _zone_acres_by_point or {}

    # --- Build spread map data (placeholder for fertilizer rate display) ---
    spread_map_data = []
    for trace in map_data:
        t = dict(trace)  # shallow copy
        if t.get("role") == "markers":
            # Gray placeholder markers instead of nutrient-colored
            t["marker"] = dict(t["marker"])
            t["marker"]["color"] = ["#999999"] * len(t.get("x", []))
        elif t.get("role") == "labels":
            # "TBD" placeholder labels instead of nutrient values
            t["text"] = ["TBD"] * len(t.get("x", []))
        spread_map_data.append(t)

    spread_layout = dict(layout)
    spread_layout["title"] = {"text": "Fertilizer Spread Rate (lbs/ac)", "font": {"size": 14}}
    # Remove nutrient-specific metadata from spread layout
    for key in ["_nutrient_colors_by_year", "_nutrient_index_map",
                "_nutrient_display_names", "_js_soil_data", "_available_years",
                "_zone_acres_by_point"]:
        spread_layout.pop(key, None)

    return map_data, layout, spread_map_data, spread_layout


def build_weather_charts(field_weather: list[dict]) -> tuple[list[dict], list[dict]]:
    """Build chart data for a single field's weather (one trace per year)."""
    color = FIELD_COLORS[0]
    cum_gdd_data: list[dict] = []
    cum_rain_data: list[dict] = []

    for year_rec in field_weather:
        year = year_rec["year"]
        daily = year_rec["daily"]
        if not daily:
            continue
        doys = [d["dayOfYear"] for d in daily]
        cgdds = [d["cumulativeGdd"] for d in daily]
        crains = [d["cumulativeRainfallIn"] for d in daily]

        label = f"{year}"
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

    return cum_gdd_data, cum_rain_data


def build_temperature_chart(field_weather: list[dict]) -> list[dict]:
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


def build_rainfall_anomaly_chart(field_weather: list[dict]) -> tuple[list[dict], dict]:
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


def build_solar_chart(field_weather: list[dict]) -> list[dict]:
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


def build_ndvi_chart(ndvi_series: list[dict]) -> list[dict]:
    """Build Plotly traces for NDVI time-series. Filters masked scenes."""
    if not ndvi_series:
        return []

    # Filter out cloud-masked scenes (>25% pixels rejected)
    clear_series = [d for d in ndvi_series if not d.get("masked", False)]

    years = sorted({d["year"] for d in clear_series})
    traces = []

    for i, year in enumerate(years):
        color = FIELD_COLORS[i % len(FIELD_COLORS)]
        year_data = [d for d in clear_series if d["year"] == year]
        if not year_data:
            continue

        doys = [d["doy"] for d in year_data]
        ndvis = [d["mean_ndvi"] for d in year_data]
        clouds = [d["cloud_cover"] for d in year_data]
        dates = [d["date"] for d in year_data]
        temporal_flags = [d.get("temporal_flag", False) for d in year_data]
        clear_fracs = [d.get("clear_fraction", 1.0) for d in year_data]

        # Size by cloud cover, smaller if temporal anomaly
        sizes = [8 if tf else (10 if c <= 10 else 7) for tf, c in zip(temporal_flags, clouds)]
        # Symbols: hollow diamond for temporal anomalies, circle for normal
        symbols = ["diamond-open" if tf else "circle" for tf in temporal_flags]

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
                "<b>%{text}</b><br>"
                "Day: %{x}<br>"
                "NDVI: %{y:.4f}<br>"
                "Cloud: %{customdata[0]}%<br>"
                "Clear: %{customdata[1]:.1%}<extra></extra>"
            ),
            "text": dates,
            "customdata": [[c, cf] for c, cf in zip(clouds, clear_fracs)],
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


def build_combined_chart(
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

    # Filter out cloud-masked scenes for NDVI traces
    clear_series = [d for d in ndvi_series if not d.get("masked", False)]

    for i, year in enumerate(years):
        color = FIELD_COLORS[i % len(FIELD_COLORS)]

        # NDVI trace (left axis) — clear scenes only
        year_ndvi = [d for d in clear_series if d["year"] == year]
        if year_ndvi:
            doys = [d["doy"] for d in year_ndvi]
            ndvis = [d["mean_ndvi"] for d in year_ndvi]
            clouds = [d["cloud_cover"] for d in year_ndvi]
            dates = [d["date"] for d in year_ndvi]
            temporal_flags = [d.get("temporal_flag", False) for d in year_ndvi]
            clear_fracs = [d.get("clear_fraction", 1.0) for d in year_ndvi]

            sizes = [8 if tf else (10 if c <= 10 else 7) for tf, c in zip(temporal_flags, clouds)]
            symbols = ["diamond-open" if tf else "circle" for tf in temporal_flags]

            traces.append({
                "type": "scatter",
                "mode": "lines+markers",
                "x": doys,
                "y": ndvis,
                "name": f"{year} NDVI",
                "line": {"color": color, "width": 2},
                "marker": {
                    "symbol": symbols,
                    "size": sizes,
                    "color": color,
                    "line": {"width": 1, "color": "white"},
                },
                "hovertemplate": (
                    "<b>%{text}</b><br>"
                    "Day: %{x}<br>"
                    "NDVI: %{y:.4f}<br>"
                    "Cloud: %{customdata[0]}%<br>"
                    "Clear: %{customdata[1]:.1%}<extra></extra>"
                ),
                "text": dates,
                "customdata": [[c, cf] for c, cf in zip(clouds, clear_fracs)],
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


def compute_year_event_counts(field_weather: list[dict], detected_events: list[dict]) -> dict[int, dict]:
    """Count critical weather events per year.

    Uses detect_critical_events output for the year that has it,
    and falls back to raw weather counting for other years.

    Returns {year: {total: int, heat: int, rain: int, ndvi_dip: int,
                     ndvi_surge: int, dominant_type: str}}
    """
    # Group detected events by year (only one year typically)
    detected_by_year: dict[int, list[dict]] = {}
    for e in detected_events:
        y = e["year"]
        if y not in detected_by_year:
            detected_by_year[y] = []
        detected_by_year[y].append(e)

    counts: dict[int, dict] = {}
    for year_rec in field_weather:
        year = year_rec["year"]

        # If we have detected events for this year, use those
        if year in detected_by_year:
            events_list = detected_by_year[year]
            total = len(events_list)
            heat = sum(1 for e in events_list if e["type"] == "heat_wave")
            rain = sum(1 for e in events_list if e["type"] == "heavy_rain")
            ndvi_dip = sum(1 for e in events_list if e["type"] == "ndvi_dip")
            ndvi_surge = sum(1 for e in events_list if e["type"] == "ndvi_surge")
            wind = sum(1 for e in events_list if e["type"] == "high_wind")
            type_map = {"heat": heat, "rain": rain, "ndvi_dip": ndvi_dip, "ndvi_surge": ndvi_surge, "wind": wind}
            dominant = max(type_map, key=type_map.get) if max(type_map.values()) > 0 else "none"
            counts[year] = {
                "total": total, "heat": heat, "rain": rain,
                "ndvi_dip": ndvi_dip, "ndvi_surge": ndvi_surge,
                "dominant_type": dominant,
            }
        else:
            # Fallback: count extreme days from raw weather
            daily = year_rec.get("daily", [])
            heat = 0
            rain = 0
            wind = 0
            for d in daily:
                if d.get("heatStress", 0) > 0:
                    heat += 1
                if d.get("dailyRainfallIn", 0) > 1.0:
                    rain += 1
                if d.get("windSpeedMph", 0) > 30.0:
                    wind += 1
            total = heat + rain + wind
            type_map = {"heat": heat, "rain": rain, "wind": wind}
            dominant = max(type_map, key=type_map.get) if max(type_map.values()) > 0 else "none"
            counts[year] = {
                "total": total, "heat": heat, "rain": rain,
                "ndvi_dip": 0, "ndvi_surge": 0,
                "dominant_type": dominant,
            }
    return counts


def compute_year_ndvi_stats(ndvi_series: list[dict]) -> dict[int, dict]:
    """Compute per-year NDVI mean and compare to multi-year average.

    Returns {year: {diff_pct: float, year_mean: float, overall_mean: float}}
    """
    from collections import defaultdict
    year_ndvis: dict[int, list[float]] = defaultdict(list)
    for rec in ndvi_series:
        if rec.get("masked", False):
            continue
        year_ndvis[rec["year"]].append(rec["mean_ndvi"])

    year_means: dict[int, float] = {}
    all_values: list[float] = []
    for year, vals in year_ndvis.items():
        if vals:
            m = sum(vals) / len(vals)
            year_means[year] = m
            all_values.extend(vals)

    overall_mean = sum(all_values) / len(all_values) if all_values else 0.0

    stats: dict[int, dict] = {}
    year_count = len(year_means)
    for year, mean_val in year_means.items():
        diff_pct = ((mean_val - overall_mean) / overall_mean) * 100 if overall_mean else 0.0
        stats[year] = {
            "diff_pct": round(diff_pct, 2),
            "year_mean": round(mean_val, 4),
            "overall_mean": round(overall_mean, 4),
            "scene_count": len(year_ndvis[year]),
            "year_count": year_count,
        }
    return stats


def _compute_field_payload(
    farm_dir_path: Path,
    field_id: str,
    no_basemap: bool = False,
    force_basemap: bool = False,
) -> dict:
    """Compute every per-field value needed to render a dashboard.

    Returns a dict of the exact kwargs consumed by ``_build_field_html_body``
    (plus a few extras: ``field_id``, ``basemap_b64``, ``mercator_extent``).
    Shared by the single-field and combined multi-field builders.
    """
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

    # Auto-detect multi-year sampling grids
    year_grids: dict[int, gpd.GeoDataFrame] = {}
    grids_dir = field_dir / "derived" / "grids"
    if grids_dir.exists():
        for grid_file in sorted(grids_dir.glob("sampling_grid_*.geojson")):
            try:
                year = int(grid_file.stem.split("_")[-1])
                year_grids[year] = gpd.read_file(str(grid_file))
            except (ValueError, Exception):
                continue
        # Fallback: legacy single grid without year suffix
        if not year_grids:
            legacy_grid = grids_dir / "sampling_grid.geojson"
            if legacy_grid.exists():
                try:
                    year_grids[2025] = gpd.read_file(str(legacy_grid))
                except Exception:
                    pass

    map_data, map_layout, spread_map_data, spread_map_layout = _build_field_map_data(
        gdf, basemap_b64, mercator_extent, weather_transforms, grid_gdf=year_grids
    )
    cum_gdd_data, cum_rain_data = build_weather_charts(weather_transforms)
    temp_data = build_temperature_chart(weather_transforms)
    stage_medians = _compute_stage_median_doys(weather_transforms)
    combined_data, combined_layout = build_combined_chart(ndvi_series, weather_transforms, stage_medians)
    anomaly_data, anomaly_layout = build_rainfall_anomaly_chart(weather_transforms)
    solar_data = build_solar_chart(weather_transforms)

    # Ensure DOY temperature baseline for cool-period detection
    fips_code = ""
    if "fips" in gdf.columns:
        fips_code = str(gdf.iloc[0].get("fips", "")).zfill(5)
    elif "COUNTYFP" in gdf.columns:
        fips_code = str(gdf.iloc[0].get("COUNTYFP", "")).zfill(5)
    baseline_path = None
    if fips_code and len(fips_code) == 5:
        baseline_path = ensure_doy_temperature_baseline(fips_code, shared_dir)

    events = detect_critical_events(
        field_weather=weather_transforms,
        ndvi_series=ndvi_series,
        target_year=max(d["year"] for d in weather_transforms),
        stage_medians=stage_medians,
        baseline_path=baseline_path,
    )

    year_event_counts = compute_year_event_counts(weather_transforms, events)
    year_ndvi_stats = compute_year_ndvi_stats(ndvi_series)

    cum_gdd_layout = _build_cum_gdd_layout_with_stages(stage_medians)
    cum_rain_layout = _default_chart_layout("Cumulative Rainfall", "inches", x_range=(60, 305))
    solar_layout = _build_solar_layout(stage_medians)
    temp_layout = {
        "title": {"text": "Daily Temperature Range (°F)", "font": {"size": 12}},
        "xaxis": {"title": "Day of year", "range": [60, 305]},
        "yaxis": {"title": "Temperature (°F)"},
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
        "hovermode": "closest",
        "legend": {"x": 0, "y": 1, "bgcolor": "rgba(255,255,255,0.7)", "font": {"size": 9}},
    }

    # Inject severe event annotations into chart layouts
    chart_annotations = _build_chart_annotations(events)
    temp_layout = _inject_chart_annotations(temp_layout, "temp", chart_annotations)
    solar_layout = _inject_chart_annotations(solar_layout, "solar", chart_annotations)
    combined_layout = _inject_chart_annotations(combined_layout, "combined", chart_annotations)

    plotly_bundle = ensure_plotly_bundle(shared_dir)

    area_str = ""
    if "area_acres" in gdf.columns:
        area_str = f"{gdf.iloc[0].get('area_acres', 'N/A')} ac"
    crop_str = ""
    if crop_history:
        crop_str = f" | {crop_history[-1].get('crop_name', '')}"
    # NDVI quality summary
    total_scenes = len(ndvi_series)
    clear_scenes = len([d for d in ndvi_series if not d.get("masked", False)])
    masked_scenes = total_scenes - clear_scenes
    temporal_flags = len([d for d in ndvi_series if d.get("temporal_flag", False)])

    title = f"Field {field_id} — NDVI Dashboard"
    grid_str = ""
    if year_grids:
        total_points = sum(len(gdf) for gdf in year_grids.values())
        grid_str = f" | {len(year_grids)} years grid data ({total_points} points)"
    subtitle = (
        f"{area_str}{crop_str} | {len(weather_transforms)} years weather | "
        f"{clear_scenes} clear scenes"
        f"{' (' + str(masked_scenes) + ' cloud-masked)' if masked_scenes > 0 else ''}"
        f"{' | ' + str(temporal_flags) + ' temporal anomaly' if temporal_flags > 0 else ''}"
        f"{grid_str}"
    )

    years = sorted({d["year"] for d in weather_transforms})

    # Extract nutrient metadata from map_layout for JS injection
    nutrient_colors_by_year = map_layout.get("_nutrient_colors_by_year", {})
    nutrient_index_map = map_layout.get("_nutrient_index_map", {})
    nutrient_display_names = map_layout.get("_nutrient_display_names", {})
    js_soil_data = map_layout.get("_js_soil_data", {})
    available_grid_years = map_layout.get("_available_years", [])
    zone_acres_by_point = map_layout.get("_zone_acres_by_point", {})

    # Load sufficiency ranges (from committed repo root)
    sufficiency_path = Path(__file__).resolve().parent.parent.parent.parent.parent.parent / "sufficiancy ranges.xlsx"
    sufficiency_ranges = {}
    if sufficiency_path.exists():
        try:
            sufficiency_ranges = load_sufficiency_ranges(sufficiency_path)
        except Exception:
            pass

    # Build per-point sufficiency ratings for JS (one entry per point per year)
    js_sufficiency_ratings: dict[str, dict[str, list[dict]]] = {}
    if sufficiency_ranges and js_soil_data:
        for point_id, year_data in js_soil_data.items():
            js_sufficiency_ratings[point_id] = {}
            for year, values in year_data.items():
                ratings = get_nutrient_ratings(sufficiency_ranges, values)
                js_sufficiency_ratings[point_id][year] = ratings

    # Build per-year field-wide average ratings for cards (top 5)
    js_year_average_ratings: dict[str, list[dict]] = {}
    if sufficiency_ranges and js_soil_data:
        for year in available_grid_years:
            year_values: dict[str, list[float]] = {nutrient: [] for nutrient in DISPLAY_NUTRIENTS}
            for point_id, year_data in js_soil_data.items():
                if str(year) not in year_data:
                    continue
                values = year_data[str(year)]
                for nutrient in DISPLAY_NUTRIENTS:
                    if nutrient in values:
                        try:
                            v = float(values[nutrient])
                            year_values[nutrient].append(v)
                        except (ValueError, TypeError):
                            pass
            # Compute averages
            avg_values: dict[str, str] = {}
            for nutrient, vals in year_values.items():
                if vals:
                    avg_values[nutrient] = str(round(sum(vals) / len(vals), 2))
            # Rate the averages
            if avg_values:
                ratings = get_nutrient_ratings(sufficiency_ranges, avg_values)
                js_year_average_ratings[str(year)] = ratings

    # Build per-year averages for ALL nutrients (table display, not just rated ones)
    js_year_all_averages: dict[str, dict[str, str]] = {}
    if js_soil_data:
        for year in available_grid_years:
            all_year_values: dict[str, list[float]] = {nutrient: [] for nutrient in nutrient_index_map.keys()}
            for point_id, year_data in js_soil_data.items():
                if str(year) not in year_data:
                    continue
                values = year_data[str(year)]
                for nutrient in nutrient_index_map.keys():
                    if nutrient in values:
                        try:
                            v = float(values[nutrient])
                            all_year_values[nutrient].append(v)
                        except (ValueError, TypeError):
                            pass
            # Compute averages for all nutrients
            year_avgs: dict[str, str] = {}
            for nutrient, vals in all_year_values.items():
                if vals:
                    year_avgs[nutrient] = str(round(sum(vals) / len(vals), 2))
            if year_avgs:
                js_year_all_averages[str(year)] = year_avgs

    # Build optimal range strings for each nutrient (for card display)
    js_sufficiency_ranges: dict[str, str] = {}
    if sufficiency_ranges:
        for nutrient, ranges in sufficiency_ranges.items():
            if "Optimal" in ranges:
                opt_min, opt_max = ranges["Optimal"]
                if opt_min is not None and opt_max is not None:
                    js_sufficiency_ranges[nutrient] = f"{opt_min}–{opt_max}"
                elif opt_min is not None:
                    js_sufficiency_ranges[nutrient] = f">{opt_min}"
                elif opt_max is not None:
                    js_sufficiency_ranges[nutrient] = f"<{opt_max}"

    # Raw optimal_low values for fertilizer rate calculation (source: sufficiency ranges)
    js_nutrient_optimal_low: dict[str, float] = {}
    if sufficiency_ranges:
        for nutrient, ranges in sufficiency_ranges.items():
            if "Optimal" in ranges:
                opt_min, opt_max = ranges["Optimal"]
                if opt_min is not None:
                    js_nutrient_optimal_low[nutrient] = float(opt_min)

    # Build crop dropdown options from available crops in CROP_GROWTH_STAGES
    # (currently corn + soybean; more can be added in agronomic_events.py)
    from lib.agronomic_events import CROP_GROWTH_STAGES
    available_crops = list(CROP_GROWTH_STAGES.keys())
    default_crop = "corn"
    if crop_history:
        most_recent = crop_history[-1].get("crop_name", "").lower()
        if most_recent in available_crops:
            default_crop = most_recent
    crop_options = _build_crop_options(available_crops, default_crop)

    # Default yield goals by crop (bu/acre)
    DEFAULT_YIELD_GOALS = {"corn": 200, "soybean": 60}
    default_yield = DEFAULT_YIELD_GOALS.get(default_crop, 200)

    return {
        "field_id": field_id,
        "basemap_b64": basemap_b64,
        "mercator_extent": mercator_extent,
        "plotly_bundle": plotly_bundle,
        "title": title,
        "subtitle": subtitle,
        "map_layout": map_layout,
        "map_data": map_data,
        "spread_map_layout": spread_map_layout,
        "spread_map_data": spread_map_data,
        "zone_acres_by_point": zone_acres_by_point,
        "nutrient_colors_by_year": nutrient_colors_by_year,
        "nutrient_index_map": nutrient_index_map,
        "nutrient_display_names": nutrient_display_names,
        "js_soil_data": js_soil_data,
        "js_sufficiency_ratings": js_sufficiency_ratings,
        "js_year_average_ratings": js_year_average_ratings,
        "js_sufficiency_ranges": js_sufficiency_ranges,
        "js_nutrient_optimal_low": js_nutrient_optimal_low,
        "js_year_all_averages": js_year_all_averages,
        "available_grid_years": available_grid_years,
        "cumulative_gdd_layout": cum_gdd_layout,
        "cumulative_gdd_data": cum_gdd_data,
        "cumulative_rainfall_layout": cum_rain_layout,
        "cumulative_rainfall_data": cum_rain_data,
        "temp_layout": temp_layout,
        "temp_data": temp_data,
        "combined_layout": combined_layout,
        "combined_data": combined_data,
        "anomaly_layout": anomaly_layout,
        "anomaly_data": anomaly_data,
        "solar_layout": solar_layout,
        "solar_data": solar_data,
        "composites": composites,
        "crop_history": crop_history,
        "years": years,
        "stage_medians": stage_medians,
        "events": events,
        "year_event_counts": year_event_counts,
        "year_ndvi_stats": year_ndvi_stats,
        "crop_options": crop_options,
        "default_crop": default_crop,
        "available_crops": available_crops,
        "default_yield": default_yield,
        "grid_points": None,
    }


def generate_field_dashboard(
    farm_dir_path: Path,
    field_id: str,
    output_path: Path | None = None,
    no_basemap: bool = False,
    force_basemap: bool = False,
) -> Path:
    """Generate a self-contained NDVI dashboard for a single field."""
    payload = _compute_field_payload(
        farm_dir_path, field_id, no_basemap=no_basemap, force_basemap=force_basemap
    )
    build_kwargs = {k: v for k, v in payload.items() if k not in ("field_id", "basemap_b64", "mercator_extent")}
    html = _build_field_html_body(**build_kwargs)

    field_dir = farm_dir_path.resolve() / "fields" / field_id
    if output_path is None:
        dashboards_dir = field_dir / "derived" / "dashboards"
        dashboards_dir.mkdir(parents=True, exist_ok=True)
        safe_id = field_id.replace("-", "_")
        output_path = dashboards_dir / f"{safe_id}_dashboard.html"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def generate_multi_field_dashboard(
    farm_dir_path: Path,
    field_ids: list[str],
    output_path: Path | None = None,
    no_basemap: bool = False,
    force_basemap: bool = False,
) -> Path:
    """Generate a combined self-contained dashboard with a Field selector.

    Builds one payload per field, embeds them all under ``FIELDS`` and adds a
    ``loadField(fid)`` JS switcher so toggling re-plots the current field's
    charts into the shared DOM containers (no page reload, no CSS-hiding).
    """
    farm_dir_path = farm_dir_path.resolve()
    payloads = [
        _compute_field_payload(
            farm_dir_path, fid, no_basemap=no_basemap, force_basemap=force_basemap
        )
        for fid in field_ids
    ]
    html = _build_multi_field_html_body(payloads)

    if output_path is None:
        output_path = farm_dir_path / "derived" / "dashboards" / "combined_fields_dashboard.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


_MULTI_FIELD_JS = """\
// ============ Combined multi-field dashboard ============
// One data blob per field; loadField(fid) re-points the shared globals
// and re-plots into the same DOM containers (no page reload).
var FIELDS = @@FIELDS_DATA@@;
var currentField = @@DEFAULT_FIELD_ID@@;

// ---- Shared, field-independent data (identical across fields in this farm) ----
var sufficiencyRanges = @@SUFFICIENCY_RANGES@@;
var availableCrops = @@AVAILABLE_CROPS@@;
var nutrientOptimalLow = @@NUTRIENT_OPTIMAL_LOW@@;

var fertilizersByNutrient = {
    "P": ["MAP (11-52-0)", "DAP (18-46-0)", "Triple Super Phosphate (0-46-0)"],
    "K": ["Potash (0-0-60)", "Sulfate of Potash (0-0-50)"],
    "NO3": ["Urea (46-0-0)", "UAN (28-0-0)"],
    "S": ["Ammonium Sulfate (21-0-0)", "Elemental Sulfur (90% S)"],
    "ZN": ["Zinc Sulfate", "Zinc Oxide"],
    "pH": ["Ag Lime", "Pelletized Lime"],
    "BpH": ["Ag Lime", "Pelletized Lime"],
    "N": ["Urea (46-0-0)", "UAN-32 (32-0-0)"],
};

// Fertilizer rate formula source: Fertilizer_Product_sheet.xlsx (confirmed by user)
// Step 1a: maintenance_lb_P2O5_per_acre = yield_goal * 0.3 (P2O5 per bu corn)
// Step 1b: buildup_lb_P2O5_per_acre = (target_ppm - current_ppm) * 18
// Step 1c: total_P2O5_lb_per_acre = maintenance + (buildup / years_to_build)
// Step 2: product_rate_per_acre = total_P2O5_lb_per_acre / productAnalysis["P"][selected_product]
// Step 3: recommended_rate_per_year = product_rate_per_acre (already per-year)
const productAnalysis = {
  "P": {
    "MAP (11-52-0)": 0.52,
    "DAP (18-46-0)": 0.46,
    "Triple Super Phosphate (0-46-0)": 0.46
  },
  "N": {
    "Urea (46-0-0)": 0.46,
    "UAN-32 (32-0-0)": 0.32
  }
};
const ppmToNutrientFactor = {
  "P": 18
};

// Card color mapping
var cardColors = {
    "Very Low": "#d62728",
    "Low": "#ff7f0e",
    "Optimal": "#2ca02c",
    "High": "#bcbd22",
    "Very High": "#9467bd"
};

var reportCardColors = {
    "A": "#2ca02c", "B": "#bcbd22", "C": "#ff7f0e", "D": "#d62728", "F": "#8b0000",
};

// ---- Per-field state, populated by loadField(fid) ----
var activeYears = [];
var mapYear = null;
var mapLayout = null;
var mapData = null;
var spreadMapLayout = null;
var spreadMapData = null;
var nutrientColorsByYear = {};
var nutrientIndexMap = {};
var nutrientDisplayNames = {};
var allSoilData = {};
var sufficiencyRatings = {};
var yearAverageRatings = {};
var yearAllAverages = {};
var availableGridYears = [];
var zoneAcresByPoint = {};
var yearEventCounts = {};
var yearNdviStats = {};
var cgLayout = null, cgData = null;
var crLayout = null, crData = null;
var tempLayout = null, tempData = null;
var combinedLayout = null, combinedData = null;
var anomalyLayout = null, anomalyData = null;
var solarLayout = null, solarData = null;
var selectedNutrient = 'P';
var selectedCrop = 'corn';
var yieldGoal = 200;
var selectedFertilizer = null;
var buildYears = "1";
var spreadMapTotal = 0;
var lastClickedPoint = null;
var nutrientNameByIdx = {};
// Guards against stale async Plotly renders: incremented on every loadField.
// Any async continuation (newPlot promise) must match the latest token before
// mutating the DOM, otherwise a slow in-flight render from a previous field
// would clobber the freshly-switched field's charts.
var renderToken = 0;

// Unified year filtering
function toggleYear(year) {
    var btn = document.querySelector('.year-btn[data-year="' + year + '"]');
    var idx = activeYears.indexOf(parseInt(year));
    if (idx >= 0) {
        // Removing year
        activeYears.splice(idx, 1);
        if (btn) btn.classList.remove('active');
        // If removed the current mapYear, fall back to highest remaining
        if (mapYear === parseInt(year)) {
            mapYear = activeYears.length > 0 ? Math.max.apply(null, activeYears) : null;
        }
    } else {
        // Adding year — becomes the new mapYear
        activeYears.push(parseInt(year));
        if (btn) btn.classList.add('active');
        mapYear = parseInt(year);
    }
    activeYears.sort(function(a, b) { return a - b; });
    updateAllCharts();
    updateSpreadMapRates();
    updateCardsVisibility();
    if (lastClickedPoint) {
        rebuildSoilTable(lastClickedPoint, false);
    } else {
        rebuildSoilTable(null, true);
    }
}

function setAllYears(active) {
    if (active) {
        activeYears = FIELDS[currentField].years.slice();
        mapYear = FIELDS[currentField].defaultMapYear;
        document.querySelectorAll('.year-btn[data-year]').forEach(function(btn) {
            btn.classList.add('active');
        });
    } else {
        activeYears = [];
        mapYear = null;
        document.querySelectorAll('.year-btn[data-year]').forEach(function(btn) {
            btn.classList.remove('active');
        });
    }
    updateAllCharts();
    updateSpreadMapRates();
    updateCardsVisibility();
    if (lastClickedPoint) {
        rebuildSoilTable(lastClickedPoint, false);
    } else {
        rebuildSoilTable(null, true);
    }
}

function updateChartVisibility(chartId, years) {
    var gd = document.getElementById(chartId);
    if (!gd || !gd.data) return;
    var visible = [];
    for (var i = 0; i < gd.data.length; i++) {
        var d = gd.data[i];
        // Traces without a 'year' property (field boundary, centroid label) stay visible always
        if (d.year === undefined) {
            visible.push(true);
        } else {
            var show = years.indexOf(d.year) >= 0;
            visible.push(show ? true : 'legendonly');
        }
    }
    Plotly.restyle(gd, {visible: visible});
}

function updateAllCharts() {
    updateChartVisibility('temp-chart', activeYears);
    updateChartVisibility('cumulative-gdd-chart', activeYears);
    updateChartVisibility('cumulative-rainfall-chart', activeYears);
    updateChartVisibility('combined-chart', activeYears);
    updateChartVisibility('anomaly-chart', activeYears);
    updateChartVisibility('solar-chart', activeYears);
    // Map: show only the current mapYear trace; hide all others
    var mapGd = document.getElementById('map-container');
    if (mapGd && mapGd.data) {
        var mapVisible = [];
        for (var i = 0; i < mapGd.data.length; i++) {
            var d = mapGd.data[i];
            if (d.year === undefined) {
                mapVisible.push(true);
            } else {
                mapVisible.push((mapYear !== null && d.year === mapYear) ? true : 'legendonly');
            }
        }
        Plotly.restyle(mapGd, {visible: mapVisible});
    }
    // Spread map: same year-based visibility
    var spreadGd = document.getElementById('spread-map-container');
    if (spreadGd && spreadGd.data) {
        var spreadVisible = [];
        for (var i = 0; i < spreadGd.data.length; i++) {
            var d = spreadGd.data[i];
            if (d.year === undefined) {
                spreadVisible.push(true);
            } else {
                spreadVisible.push((mapYear !== null && d.year === mapYear) ? true : 'legendonly');
            }
        }
        Plotly.restyle(spreadGd, {visible: spreadVisible});
    }
}

function buildYearButtons() {
    var f = FIELDS[currentField];
    var html = '';
    for (var i = 0; i < f.years.length; i++) {
        html += '<button class="year-btn active" data-year="' + f.years[i] + '" onclick="toggleYear(\\'' + f.years[i] + '\\')">' + f.years[i] + '</button>';
    }
    html += '<button class="year-btn global" onclick="setAllYears(true)">All Years</button>';
    html += '<button class="year-btn global" onclick="setAllYears(false)">None</button>';
    document.getElementById('year-filter-bar').innerHTML = html;
}

// Re-attach the single stored basemap to both map layouts (dedup: one copy per field)
function attachBasemap() {
    var f = FIELDS[currentField];
    if (!f.basemapB64 || !f.mercatorExtent) {
        mapLayout.images = [];
        spreadMapLayout.images = [];
        return;
    }
    var ext = f.mercatorExtent;
    var img = [{
        source: f.basemapB64,
        xref: 'x', yref: 'y',
        x: ext[0], y: ext[3],
        sizex: ext[2] - ext[0], sizey: ext[3] - ext[1],
        sizing: 'stretch', opacity: 1, layer: 'below'
    }];
    mapLayout.images = img;
    spreadMapLayout.images = img;
}

function renderCharts() {
    attachBasemap();
    // Purge every container before re-plotting. Plotly.newPlot's internal
    // draw (_doPlot) is asynchronous and chains on gd._promises; without
    // purging, a slow in-flight render from a previous field can resolve
    // after a quick field switch and overwrite the freshly-drawn charts.
    var containers = ['map-container', 'spread-map-container', 'cumulative-gdd-chart',
        'cumulative-rainfall-chart', 'temp-chart', 'combined-chart',
        'anomaly-chart', 'solar-chart'];
    for (var ci = 0; ci < containers.length; ci++) {
        var el = document.getElementById(containers[ci]);
        if (el && el._fullData) Plotly.purge(el);
    }
    return Promise.all([
        Plotly.newPlot('map-container', mapData, mapLayout, {responsive: true}),
        Plotly.newPlot('spread-map-container', spreadMapData, spreadMapLayout, {responsive: true}),
        Plotly.newPlot('cumulative-gdd-chart', cgData, cgLayout, {responsive: true}),
        Plotly.newPlot('cumulative-rainfall-chart', crData, crLayout, {responsive: true}),
        Plotly.newPlot('temp-chart', tempData, tempLayout, {responsive: true}),
        Plotly.newPlot('combined-chart', combinedData, combinedLayout, {responsive: true}),
        Plotly.newPlot('anomaly-chart', anomalyData, anomalyLayout, {responsive: true}),
        Plotly.newPlot('solar-chart', solarData, solarLayout, {responsive: true})
    ]);
}

function loadField(fid) {
    var token = ++renderToken;
    currentField = fid;
    var f = FIELDS[fid];

    document.getElementById('dashboard-title').textContent = f.title;
    document.getElementById('dashboard-subtitle').textContent = f.subtitle;
    document.getElementById('field-select').value = fid;

    // Re-point all per-field globals
    mapLayout = f.mapLayout;
    mapData = f.mapData;
    spreadMapLayout = f.spreadMapLayout;
    spreadMapData = f.spreadMapData;
    zoneAcresByPoint = f.zoneAcresByPoint;
    nutrientColorsByYear = f.nutrientColorsByYear;
    nutrientIndexMap = f.nutrientIndexMap;
    nutrientDisplayNames = f.nutrientDisplayNames;
    allSoilData = f.allSoilData;
    sufficiencyRatings = f.sufficiencyRatings;
    yearAverageRatings = f.yearAverageRatings;
    yearAllAverages = f.yearAllAverages;
    availableGridYears = f.availableGridYears;
    yearEventCounts = f.yearEventCounts;
    yearNdviStats = f.yearNdviStats;
    cgLayout = f.cgLayout; cgData = f.cgData;
    crLayout = f.crLayout; crData = f.crData;
    tempLayout = f.tempLayout; tempData = f.tempData;
    combinedLayout = f.combinedLayout; combinedData = f.combinedData;
    anomalyLayout = f.anomalyLayout; anomalyData = f.anomalyData;
    solarLayout = f.solarLayout; solarData = f.solarData;

    // Reset UI state
    selectedNutrient = 'P';
    selectedCrop = f.defaultCrop;
    yieldGoal = f.defaultYield;
    selectedFertilizer = null;
    buildYears = "1";
    spreadMapTotal = 0;
    lastClickedPoint = null;
    activeYears = f.years.slice();
    mapYear = f.defaultMapYear;

    nutrientNameByIdx = {};
    for (var name in nutrientIndexMap) {
        nutrientNameByIdx[nutrientIndexMap[name]] = name;
    }

    // Rebuild dynamic controls / slots
    buildYearButtons();
    document.getElementById('nutrient-select').innerHTML = f.nutrientOptionsHtml;
    document.getElementById('crop-select').innerHTML = f.cropOptionsHtml;
    document.getElementById('yield-goal').value = f.defaultYield;
    document.getElementById('build-years').innerHTML = f.buildYearOptionsHtml;
    document.getElementById('composites-slot').innerHTML = f.compositeHtml;
    document.getElementById('crop-table-slot').innerHTML = f.cropTableHtml;
    document.getElementById('stage-legend-slot').innerHTML = f.stageLegendHtml;

    // Plot all charts, then apply the default nutrient (P) view only after
    // the new field's plots have actually finished drawing. The token guard
    // discards stale continuations if the user switches fields again while a
    // previous (slow, 12 MB) render is still in flight.
    return renderCharts().then(function() {
        if (token !== renderToken) return;
        attachMapClickHandler();
        // Apply the default nutrient (P) view: recolors the left map, populates
        // the fertilizer dropdown, and computes the spread-map rates/total.
        updateNutrientDisplay();
        if (soilDetailPanel) soilDetailPanel.classList.add('visible');
        updateCardsVisibility();
        rebuildSoilTable(null, true);
        buildAnalysisSummary();
    }).catch(function(err) {
        // A rejected newPlot shouldn't strand the UI; still refresh the
        // non-Plotly controls and the soil table.
        if (token !== renderToken) return;
        attachMapClickHandler();
        updateNutrientDisplay();
        if (soilDetailPanel) soilDetailPanel.classList.add('visible');
        updateCardsVisibility();
        rebuildSoilTable(null, true);
        buildAnalysisSummary();
    });
}

function buildCards(mapYear) {
    var topGrid = document.getElementById('top-cards-grid');
    var bottomGrid = document.getElementById('bottom-cards-grid');
    if (!topGrid || !bottomGrid) return;

    var ratings = yearAverageRatings[String(mapYear)];
    if (!ratings || ratings.length === 0) {
        topGrid.innerHTML = '';
        bottomGrid.innerHTML = '';
        return;
    }

    var top5 = ratings.slice(0, 5);
    var bottom7 = ratings.slice(5);

    function makeCard(r) {
        var color = cardColors[r.category] || '#999';
        var displayName = nutrientDisplayNames[r.nutrient] || r.nutrient;
        var rangeText = sufficiencyRanges[r.nutrient] || '';
        var rangeHtml = rangeText ? '<div class="card-range">Optimal range: ' + rangeText + '</div>' : '';
        return '<div class="sufficiency-card" style="border-left-color:' + color + '">' +
            '<div class="card-nutrient">' + displayName + '</div>' +
            '<div class="card-value">' + r.value + '</div>' +
            '<div class="card-category" style="color:' + color + '">' + r.category + '</div>' +
            rangeHtml +
            '</div>';
    }

    topGrid.innerHTML = top5.map(makeCard).join('');
    bottomGrid.innerHTML = bottom7.map(makeCard).join('');
}

function updateCardsVisibility() {
    if (mapYear === null || activeYears.length === 0) {
        if (topCardsPanel) topCardsPanel.classList.remove('visible');
        if (bottomCardsPanel) bottomCardsPanel.classList.remove('visible');
    } else {
        if (topCardsPanel) topCardsPanel.classList.add('visible');
        if (bottomCardsPanel) bottomCardsPanel.classList.add('visible');
        buildCards(mapYear);
    }
    var rcYear = mapYear !== null ? mapYear : (activeYears.length > 0 ? Math.max(...activeYears) : 0);
    if (rcYear > 0) buildReportCard(rcYear);
}

function analysisNumber(v) {
    var n = parseFloat(v);
    return isNaN(n) ? null : n;
}

function fieldSummaryMetrics(fid, yearStr) {
    var f = FIELDS[fid];
    if (!f) return null;
    var avgs = (f.yearAllAverages || {})[yearStr] || {};
    var ratings = (f.yearAverageRatings || {})[yearStr] || [];
    var ndviKeys = Object.keys(f.yearNdviStats || {}).map(Number).sort(function(a, b) { return a - b; });
    var latestNdvi = null;
    if (ndviKeys.length) {
        var st = f.yearNdviStats[ndviKeys[ndviKeys.length - 1]];
        latestNdvi = (st && st.diff_pct != null) ? st.diff_pct : null;
    }
    var nonOptimal = ratings.filter(function(r) { return r.nutrient !== 'pH' && r.category !== 'Optimal'; }).length;
    return {
        fid: fid,
        title: f.title || fid,
        p: analysisNumber(avgs['P']),
        k: analysisNumber(avgs['K']),
        zn: analysisNumber(avgs['ZN']),
        s: analysisNumber(avgs['S']),
        no3: analysisNumber(avgs['NO3']),
        cec: analysisNumber(avgs['CEC']),
        ph: analysisNumber(avgs['pH']),
        nonOptimal: nonOptimal,
        ndvi: latestNdvi
    };
}

function buildAnalysisSummary() {
    var body = document.getElementById('analysis-summary-body');
    if (!body) return;
    var rcYear = mapYear !== null ? mapYear : (activeYears.length > 0 ? Math.max(...activeYears) : 0);
    if (rcYear <= 0) { body.innerHTML = '<p class="analysis-empty">No data to summarize.</p>'; return; }
    var yearStr = String(rcYear);

    // Grades, reusing the same logic as the report card
    var soilG = computeSoilGrade(rcYear);
    var phG = computePhGrade(rcYear);
    var vigG = computeVigorGrade(rcYear);
    var weaG = computeWeatherGrade(rcYear);
    var categories = [
        {name: 'Soil Fertility', points: soilG.points, letter: soilG.letter},
        {name: 'pH Balance', points: phG.points, letter: phG.letter},
        {name: 'Crop Vigor', points: vigG.points, letter: vigG.letter},
        {name: 'Weather Stress', points: weaG.points, letter: weaG.letter}
    ];
    var overall = computeOverallGrade(categories);

    // Trend: NDVI first year -> latest year
    var ndviKeys = Object.keys(yearNdviStats || {}).map(Number).sort(function(a, b) { return a - b; });
    var trendText = 'Insufficient NDVI history for a trend.';
    var firstDiff = null, lastDiff = null;
    if (ndviKeys.length >= 2) {
        firstDiff = yearNdviStats[ndviKeys[0]].diff_pct;
        lastDiff = yearNdviStats[ndviKeys[ndviKeys.length - 1]].diff_pct;
        var delta = lastDiff - firstDiff;
        var dir = 'stable';
        if (delta > 2) dir = 'improving';
        else if (delta < -2) dir = 'declining';
        trendText = 'Crop vigor is ' + dir + ' across ' + ndviKeys.length + ' years of NDVI (' +
            (firstDiff >= 0 ? '+' : '') + firstDiff.toFixed(1) + '% in ' + ndviKeys[0] +
            ' to ' + (lastDiff >= 0 ? '+' : '') + lastDiff.toFixed(1) + '% in ' + ndviKeys[ndviKeys.length - 1] +
            ' vs the field\u2019s long-term average).';
    }

    // Heat stress trend
    var evKeys = Object.keys(yearEventCounts || {}).map(Number).sort(function(a, b) { return a - b; });
    var heatText = '';
    if (evKeys.length >= 2) {
        var older = yearEventCounts[evKeys[evKeys.length - 2]] || {};
        var latest = yearEventCounts[evKeys[evKeys.length - 1]] || {};
        var olderHeat = older.heat || 0, latestHeat = latest.heat || 0;
        heatText = 'Heat-stress days went from ' + olderHeat + ' (' + evKeys[evKeys.length - 2] +
            ') to ' + latestHeat + ' (' + evKeys[evKeys.length - 1] + '), making weather the dominant year-to-year factor.';
    }

    // Most important variables (non-optimal nutrients, sorted by impact)
    var ratings = (yearAverageRatings || {})[yearStr] || [];
    var nonOptimal = ratings.filter(function(r) { return r.nutrient !== 'pH' && r.category !== 'Optimal'; });
    nonOptimal.sort(function(a, b) {
        var sa = (b.score || 0) - (a.score || 0);
        return sa !== 0 ? sa : ((b.distance || 0) - (a.distance || 0));
    });
    var top3 = nonOptimal.slice(0, 3);
    var varText = 'All measured nutrients are in the optimal range.';
    if (top3.length) {
        varText = top3.map(function(r) {
            var name = nutrientDisplayNames[r.nutrient] || r.nutrient;
            return name + ' (' + r.category.toLowerCase() + ', ' + r.value + ')';
        }).join('; ') + '.';
    }

    // Recommended actions from key thresholds
    var actions = [];
    var avgs = (yearAllAverages || {})[yearStr] || {};
    var pH = analysisNumber(avgs['pH']);
    var hSat = analysisNumber(avgs['H_SAT']);
    var P = analysisNumber(avgs['P']);
    var S = analysisNumber(avgs['S']);
    var K = analysisNumber(avgs['K']);
    var ZN = analysisNumber(avgs['ZN']);
    if (pH !== null && pH < 6.0) actions.push('Lime to raise pH toward the 6.0\u20136.8 target.');
    if (hSat !== null && hSat > 10) actions.push('H-saturation is elevated (' + hSat + '%) \u2014 confirm lime need before planting.');
    if (P !== null && P < 18) actions.push('Apply P (MAP/DAP) \u2014 field P is below the target.');
    if (S !== null && S < 7) actions.push('Include sulfur \u2014 field S is low.');
    if (K !== null && K > 250) actions.push('Skip K fertilizer \u2014 K is already high.');
    else if (K !== null && K < 151) actions.push('Consider K fertilizer \u2014 K is below optimal.');
    if (ZN !== null && ZN < 0.9) actions.push('Monitor zinc \u2014 ZN is below the sufficiency band.');
    if (!actions.length) actions.push('No urgent nutrient corrections detected \u2014 maintain current program.');
    var actionText = actions.join(' ');

    // Cross-field comparison
    var otherIds = Object.keys(FIELDS).filter(function(fid) { return fid !== currentField; });
    var rowsHtml = '';
    var myM = fieldSummaryMetrics(currentField, yearStr);
    if (myM) {
        var allM = [myM].concat(otherIds.map(function(fid) { return fieldSummaryMetrics(fid, yearStr); }).filter(Boolean));
        rowsHtml = allM.map(function(m) {
            var cur = m.fid === currentField ? ' class="analysis-current"' : '';
            var nd = m.ndvi == null ? '\u2014' : ((m.ndvi >= 0 ? '+' : '') + m.ndvi.toFixed(1) + '%');
            return '<tr' + cur + '><td>' + m.title + '</td>' +
                '<td>' + (m.cec == null ? '\u2014' : m.cec.toFixed(0)) + '</td>' +
                '<td>' + (m.ph == null ? '\u2014' : m.ph.toFixed(1)) + '</td>' +
                '<td>' + (m.p == null ? '\u2014' : m.p.toFixed(0)) + '</td>' +
                '<td>' + (m.k == null ? '\u2014' : m.k.toFixed(0)) + '</td>' +
                '<td>' + (m.zn == null ? '\u2014' : m.zn.toFixed(1)) + '</td>' +
                '<td>' + (m.s == null ? '\u2014' : m.s.toFixed(1)) + '</td>' +
                '<td>' + (m.no3 == null ? '\u2014' : m.no3.toFixed(1)) + '</td>' +
                '<td>' + m.nonOptimal + '</td>' +
                '<td>' + nd + '</td></tr>';
        }).join('');
    }

    var html = '';
    html += '<div class="analysis-grades">Overall <b style="color:' + overall.color + '">' + overall.letter + '</b> \u2014 ' +
        'Soil ' + soilG.letter + ' / pH ' + phG.letter + ' / Vigor ' + vigG.letter + ' / Weather ' + weaG.letter + '</div>';
    html += '<p><b>Patterns / trends:</b> ' + trendText + ' ' + heatText + '</p>';
    html += '<p><b>Most important variables:</b> ' + varText + '</p>';
    html += '<p><b>Actions the data suggests:</b> ' + actionText + '</p>';
    if (rowsHtml) {
        html += '<p class="analysis-compare-label"><b>Across fields (' + yearStr + '):</b></p>' +
            '<table class="analysis-table"><thead><tr>' +
            '<th>Field</th><th>CEC</th><th>pH</th><th>P</th><th>K</th><th>Zn</th><th>S</th><th>NO3</th><th># off-optimal</th><th>NDVI</th>' +
            '</tr></thead><tbody>' + rowsHtml + '</tbody></table>';
    }
    body.innerHTML = html;
}

function repopulateFertilizerDropdown() {
    var fertSelect = document.getElementById('fertilizer-select');
    if (!fertSelect) return;

    // Clear existing options
    fertSelect.innerHTML = '';

    var products = fertilizersByNutrient[selectedNutrient];
    if (!products || products.length === 0) {
        // No fertilizers for this nutrient (pH, OM, CEC, saturation %, etc.)
        var opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '\u2014';  // em dash
        fertSelect.appendChild(opt);
        selectedFertilizer = null;
        return;
    }

    products.forEach(function(prod) {
        var opt = document.createElement('option');
        opt.value = prod;
        opt.textContent = prod;
        fertSelect.appendChild(opt);
    });
    selectedFertilizer = products[0];
}

function updateNutrientDisplay() {
    var select = document.getElementById('nutrient-select');
    if (!select) return;
    selectedNutrient = select.value;
    // Nitrogen (N) computes fertilizer rates from NO3/OM; the left soil map shows NO3
    var mapNutrient = selectedNutrient === 'N' ? 'NO3' : selectedNutrient;
    // "Years to Build" only applies to soil-test buildup nutrients; hide it for N
    var byWrap = document.getElementById('build-years-wrap');
    if (byWrap) byWrap.style.display = selectedNutrient === 'N' ? 'none' : '';
    repopulateFertilizerDropdown();
    updateSpreadMapInfo();
    updateSpreadMapRates();
    var idx = nutrientIndexMap[mapNutrient];
    var gd = document.getElementById('map-container');
    if (!gd || !gd.data) return;

    // Build a lookup from year -> customdata (only marker traces carry it)
    var customdataByYear = {};
    for (var i = 0; i < gd.data.length; i++) {
        var t = gd.data[i];
        if (t.year === undefined) continue;
        if (t.role === 'markers' && t.customdata) {
            customdataByYear[t.year] = t.customdata;
        }
    }

    // Update colors, text labels, and hover for ALL year traces
    for (var i = 0; i < gd.data.length; i++) {
        var trace = gd.data[i];
        if (trace.year === undefined) continue;
        var year = trace.year;
        var colors = nutrientColorsByYear[year][mapNutrient];
        var name = nutrientDisplayNames[mapNutrient] || mapNutrient;
        if (trace.role === 'markers') {
            if (colors) {
                Plotly.restyle(gd, {'marker.color': [colors]}, [i]);
            }
            var newHover = '<b>Sample Point %{customdata[19]}</b><br>' +
                name + ': %{customdata[' + idx + ']}<br>' +
                'Year: ' + year + '<br>' +
                'Lat: %{customdata[1]} | Lon: %{customdata[0]}<extra></extra>';
            Plotly.restyle(gd, {'hovertemplate': newHover}, [i]);
        } else if (trace.role === 'labels') {
            var cd = customdataByYear[year];
            if (cd) {
                var newText = cd.map(function(row) { return row[idx]; });
                Plotly.restyle(gd, {'text': [newText]}, [i]);
            }
        }
    }

    // If a point is already selected, rebuild the table with the new nutrient at the top
    if (lastClickedPoint) {
        rebuildSoilTable(lastClickedPoint, false);
    } else {
        rebuildSoilTable(null, true);
    }
}

function updateCropDisplay() {
    var select = document.getElementById('crop-select');
    if (!select) return;
    selectedCrop = select.value;

    // No-op for now: only corn sufficiency ranges are fully defined.
    // When additional crops are added, this will:
    // 1. Reload sufficiencyRanges for selectedCrop
    // 2. Recompute nutrientColorsByYear using crop-specific ranges
    // 3. Call updateNutrientDisplay() to refresh map colors
    // 4. Call buildCards(mapYear) to refresh deficiency cards

    updateCardsVisibility();
    if (lastClickedPoint) {
        rebuildSoilTable(lastClickedPoint, false);
    } else {
        rebuildSoilTable(null, true);
    }
}

function updateYieldGoal() {
    var input = document.getElementById('yield-goal');
    if (!input) return;
    var val = parseInt(input.value, 10);
    if (isNaN(val) || val < 0) {
        val = 0;
    } else if (val > 400) {
        val = 400;
    }
    yieldGoal = val;

    updateSpreadMapInfo();
    updateSpreadMapRates();

    updateCardsVisibility();
    if (lastClickedPoint) {
        rebuildSoilTable(lastClickedPoint, false);
    } else {
        rebuildSoilTable(null, true);
    }
}

function updateFertilizerDisplay() {
    var select = document.getElementById('fertilizer-select');
    if (!select) return;
    selectedFertilizer = select.value;
    updateSpreadMapInfo();
    updateSpreadMapRates();
}

function updateBuildYears() {
    var select = document.getElementById('build-years');
    if (!select) return;
    buildYears = select.value;
    updateSpreadMapRates();
}

function updateSpreadMapInfo() {
    var info = document.getElementById('spread-map-info');
    if (!info) return;
    var fert = selectedFertilizer || '\u2014';
    var mode = (selectedNutrient === 'pH' || selectedNutrient === 'N')
        ? ''
        : (buildYears === 'no_build' ? ', Crop Removal Only' : '');
    var totalText = spreadMapTotal > 0
        ? ' \u2014 ' + String(Math.round(spreadMapTotal / 5) * 5).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',') + ' lbs total'
        : '';
    info.textContent = yieldGoal + ' bu/acre goal, ' + fert + mode + totalText;
}

function rateToColor(t) {
    var r, g, b;
    if (t <= 0.5) {
        var p = t * 2;
        r = 220 + Math.round(p * 20);
        g = 40 + Math.round(p * 180);
        b = 40 + Math.round(p * 10);
    } else {
        var p = (t - 0.5) * 2;
        r = 240 - Math.round(p * 200);
        g = 220 - Math.round(p * 40);
        b = 50 + Math.round(p * 10);
    }
    return 'rgb(' + r + ',' + g + ',' + b + ')';
}

function updateSpreadMapRates() {
    var gd = document.getElementById('spread-map-container');
    if (!gd || !gd.data) return;

    // If no fertilizer selected, reset markers to gray, labels to dash
    if (!selectedFertilizer) {
        spreadMapTotal = 0;
        updateSpreadMapInfo();
        for (var i = 0; i < gd.data.length; i++) {
            var trace = gd.data[i];
            if (trace.year === undefined) continue;
            var n = trace.x ? trace.x.length : 0;
            if (trace.role === 'markers') {
                Plotly.restyle(gd, {'marker.color': [new Array(n).fill('#999999')]}, [i]);
            } else if (trace.role === 'labels') {
                Plotly.restyle(gd, {'text': [new Array(n).fill('\u2014')]}, [i]);
            }
        }
        return;
    }

    // Lime rate: one-time soil correction (Lime_rec.xlsx, 100% CCE, 6-inch incorporation).
    // If current pH >= 6.5 no lime is needed (rate 0); otherwise:
    //   rate = 0.6 * (CEC/10) * (7.5 - current_BpH) * (6.5/current_pH) * 2000
    // Not divided by years_to_build (lime is a one-time correction, unlike P maintenance).
    if (selectedNutrient === 'pH') {
        var yearTotals = {};
        for (var i = 0; i < gd.data.length; i++) {
            var trace = gd.data[i];
            if (trace.year === undefined) continue;

            var cd = trace.customdata;
            if (!cd && trace.role === 'labels') {
                for (var j = 0; j < gd.data.length; j++) {
                    var mt = gd.data[j];
                    if (mt.role === 'markers' && mt.year === trace.year && mt.customdata) {
                        cd = mt.customdata;
                        break;
                    }
                }
            }
            if (!cd) continue;

            // Pass 1: compute all rates
            var rates = cd.map(function(row) {
                var ph = parseFloat(row[2]);
                var bph = parseFloat(row[3]);
                var cec = parseFloat(row[12]);
                if (isNaN(ph) || isNaN(bph) || isNaN(cec)) return null;
                if (ph >= 6.5) return 0;
                var lime = 0.6 * (cec / 10) * (7.5 - bph) * (6.5 / ph) * 2000;
                return Math.max(0, lime);
            });

            // Compute acreage-weighted field total
            var accumulatedLbs = 0;
            for (var k = 0; k < rates.length; k++) {
                if (rates[k] !== null && rates[k] > 0) {
                    var ptId = cd[k][19];
                    var acres = zoneAcresByPoint[ptId] || 0;
                    accumulatedLbs += rates[k] * acres;
                }
            }
            yearTotals[trace.year] = accumulatedLbs;

            // Find min/max non-zero rates for color scale
            var nonZero = rates.filter(function(r) { return r !== null && r > 0; });
            var minRate = nonZero.length > 0 ? Math.min.apply(null, nonZero) : 0;
            var maxRate = nonZero.length > 0 ? Math.max.apply(null, nonZero) : 0;
            var range = maxRate - minRate;

            // Pass 2: build colors and label texts
            var colors = [];
            var texts = [];
            for (var k = 0; k < rates.length; k++) {
                var r = rates[k];
                if (r === null) {
                    colors.push('#999999');
                    texts.push('\u2014');
                } else if (r === 0) {
                    colors.push('#999999');
                    texts.push('0');
                } else {
                    var t = range > 0 ? (r - minRate) / range : 0.5;
                    colors.push(rateToColor(t));
                    texts.push(String(Math.round(r / 5) * 5));
                }
            }

            if (trace.role === 'markers') {
                Plotly.restyle(gd, {'marker.color': [colors]}, [i]);
            } else if (trace.role === 'labels') {
                Plotly.restyle(gd, {'text': [texts]}, [i]);
            }
        }
        spreadMapTotal = yearTotals[mapYear] !== undefined ? yearTotals[mapYear] : 0;
        updateSpreadMapInfo();
        return;
    }

    // Nitrogen rate: annual yield-driven recommendation.
    // total_N = max(0, (1.2 * yield_goal) - (current_NO3 * 2) - (current_OM * 10))
    // rate = total_N / productAnalysis["N"][selected_product]
    // Not affected by years_to_build (N is not a soil-test buildup like P).
    if (selectedNutrient === 'N') {
        var prodRate = productAnalysis['N']?.[selectedFertilizer];
        if (!prodRate) {
            spreadMapTotal = 0;
            updateSpreadMapInfo();
            return;
        }
        var yearTotals = {};
        for (var i = 0; i < gd.data.length; i++) {
            var trace = gd.data[i];
            if (trace.year === undefined) continue;

            var cd = trace.customdata;
            if (!cd && trace.role === 'labels') {
                for (var j = 0; j < gd.data.length; j++) {
                    var mt = gd.data[j];
                    if (mt.role === 'markers' && mt.year === trace.year && mt.customdata) {
                        cd = mt.customdata;
                        break;
                    }
                }
            }
            if (!cd) continue;

            // Pass 1: compute all rates (NO3 idx 18, OM idx 4)
            var rates = cd.map(function(row) {
                var no3 = parseFloat(row[18]);
                var om = parseFloat(row[4]);
                if (isNaN(no3) || isNaN(om)) return null;
                var totalN = (1.2 * yieldGoal) - (no3 * 2) - (om * 10);
                if (totalN <= 0) return 0;
                return totalN / prodRate;
            });

            // Compute acreage-weighted field total
            var accumulatedLbs = 0;
            for (var k = 0; k < rates.length; k++) {
                if (rates[k] !== null && rates[k] > 0) {
                    var ptId = cd[k][19];
                    var acres = zoneAcresByPoint[ptId] || 0;
                    accumulatedLbs += rates[k] * acres;
                }
            }
            yearTotals[trace.year] = accumulatedLbs;

            // Find min/max non-zero rates for color scale
            var nonZero = rates.filter(function(r) { return r !== null && r > 0; });
            var minRate = nonZero.length > 0 ? Math.min.apply(null, nonZero) : 0;
            var maxRate = nonZero.length > 0 ? Math.max.apply(null, nonZero) : 0;
            var range = maxRate - minRate;

            // Pass 2: build colors and label texts
            var colors = [];
            var texts = [];
            for (var k = 0; k < rates.length; k++) {
                var r = rates[k];
                if (r === null) {
                    colors.push('#999999');
                    texts.push('\u2014');
                } else if (r === 0) {
                    colors.push('#999999');
                    texts.push('0');
                } else {
                    var t = range > 0 ? (r - minRate) / range : 0.5;
                    colors.push(rateToColor(t));
                    texts.push(String(Math.round(r / 5) * 5));
                }
            }

            if (trace.role === 'markers') {
                Plotly.restyle(gd, {'marker.color': [colors]}, [i]);
            } else if (trace.role === 'labels') {
                Plotly.restyle(gd, {'text': [texts]}, [i]);
            }
        }
        spreadMapTotal = yearTotals[mapYear] !== undefined ? yearTotals[mapYear] : 0;
        updateSpreadMapInfo();
        return;
    }

    // Non-P nutrient without a rate formula: reset markers to gray, labels to dash
    if (selectedNutrient !== 'P') {
        spreadMapTotal = 0;
        updateSpreadMapInfo();
        for (var i = 0; i < gd.data.length; i++) {
            var trace = gd.data[i];
            if (trace.year === undefined) continue;
            var n = trace.x ? trace.x.length : 0;
            if (trace.role === 'markers') {
                Plotly.restyle(gd, {'marker.color': [new Array(n).fill('#999999')]}, [i]);
            } else if (trace.role === 'labels') {
                Plotly.restyle(gd, {'text': [new Array(n).fill('\u2014')]}, [i]);
            }
        }
        return;
    }

    var idx = nutrientIndexMap[selectedNutrient];
    if (idx === undefined) return;

    var factor = ppmToNutrientFactor[selectedNutrient];
    var prodRate = productAnalysis[selectedNutrient]?.[selectedFertilizer];
    if (!factor || !prodRate) return;

    var targetPpm = nutrientOptimalLow[selectedNutrient];
    if (targetPpm === undefined) return;

    // Per-year totals keyed by year; spreadMapTotal reflects the visible mapYear
    var yearTotals = {};
    for (var i = 0; i < gd.data.length; i++) {
        var trace = gd.data[i];
        if (trace.year === undefined) continue;

        var cd = trace.customdata;
        if (!cd && trace.role === 'labels') {
            for (var j = 0; j < gd.data.length; j++) {
                var mt = gd.data[j];
                if (mt.role === 'markers' && mt.year === trace.year && mt.customdata) {
                    cd = mt.customdata;
                    break;
                }
            }
        }
        if (!cd) continue;

        // Pass 1: compute all rates
        var rates = cd.map(function(row) {
            var currentPpm = parseFloat(row[idx]);
            if (isNaN(currentPpm)) return null;
            var maintenance = yieldGoal * 0.3;
            var buildup;
            if (buildYears === 'no_build') {
                buildup = 0;
            } else {
                var years = parseInt(buildYears, 10) || 1;
                buildup = Math.max(0, (targetPpm - currentPpm) * factor) / years;
            }
            var totalLbs = maintenance + buildup;
            if (totalLbs <= 0) return 0;
            return totalLbs / prodRate;
        });

        // Compute acreage-weighted field total from visible year
        var accumulatedLbs = 0;
        for (var k = 0; k < rates.length; k++) {
            if (rates[k] !== null && rates[k] > 0) {
                var ptId = cd[k][19];
                var acres = zoneAcresByPoint[ptId] || 0;
                accumulatedLbs += rates[k] * acres;
            }
        }
        yearTotals[trace.year] = accumulatedLbs;

        // Find min/max non-zero rates for color scale
        var nonZero = rates.filter(function(r) { return r !== null && r > 0; });
        var minRate = nonZero.length > 0 ? Math.min.apply(null, nonZero) : 0;
        var maxRate = nonZero.length > 0 ? Math.max.apply(null, nonZero) : 0;
        var range = maxRate - minRate;

        // Pass 2: build colors and label texts
        var colors = [];
        var texts = [];
        for (var k = 0; k < rates.length; k++) {
            var r = rates[k];
            if (r === null || r === 0) {
                colors.push('#999999');
                texts.push('\u2014');
            } else {
                var t = range > 0 ? (r - minRate) / range : 0.5;
                colors.push(rateToColor(t));
                texts.push(String(Math.round(r / 5) * 5));
            }
        }

        if (trace.role === 'markers') {
            Plotly.restyle(gd, {'marker.color': [colors]}, [i]);
        } else if (trace.role === 'labels') {
            Plotly.restyle(gd, {'text': [texts]}, [i]);
        }
    }
    spreadMapTotal = yearTotals[mapYear] !== undefined ? yearTotals[mapYear] : 0;
    updateSpreadMapInfo();
}

// Grid point click handler — year comparison table
var soilDetailPanel = document.getElementById('soil-detail-panel');
var soilPointId = document.getElementById('soil-point-id');
var soilMapYear = document.getElementById('soil-map-year');
var soilTableBody = document.getElementById('soil-table-body');
var lastClickedPoint = null;

var soilLabels = [
    ["pH", 2], ["Buffer pH", 3], ["Organic Matter (%)", 4],
    ["Phosphorus (P)", 5], ["Potassium (K)", 6], ["Sulfur (S)", 7],
    ["Zinc (Zn)", 8], ["Calcium (Ca)", 9], ["Magnesium (Mg)", 10],
    ["Sodium (Na)", 11], ["CEC", 12], ["H Saturation (%)", 13],
    ["K Saturation (%)", 14], ["Ca Saturation (%)", 15],
    ["Mg Saturation (%)", 16], ["Na Saturation (%)", 17],
    ["Nitrate (NO₃)", 18],
];

// Reverse mapping: numeric index → nutrient name for data lookup (rebuilt per field)
var nutrientNameByIdx = {};

function classifyTrend(vals) {
    var nums = [];
    for (var i = 0; i < vals.length; i++) {
        var n = parseFloat(vals[i]);
        if (isNaN(n)) return null;
        nums.push(n);
    }
    var hasIncrease = false;
    var hasDecrease = false;
    for (var i = 1; i < nums.length; i++) {
        if (nums[i] > nums[i - 1]) hasIncrease = true;
        if (nums[i] < nums[i - 1]) hasDecrease = true;
    }
    if (hasIncrease && !hasDecrease) return 'up';
    if (hasDecrease && !hasIncrease) return 'down';
    return 'mixed';
}

function trendCellHtml(trend) {
    if (trend === 'up') {
        return '<td class="change-up"><span class="change-cell"><span class="arrow">▲</span></span></td>';
    }
    if (trend === 'down') {
        return '<td class="change-down"><span class="change-cell"><span class="arrow">▼</span></span></td>';
    }
    return '<td>\u2014</td>';
}

function rebuildSoilTable(num, isAverage) {
    if (!soilTableBody) return;

    // Update panel title
    if (soilPointId) {
        soilPointId.textContent = isAverage ? 'Field Average' : 'Point ' + num;
    }
    var prefixEl = document.getElementById('map-year-prefix');
    if (prefixEl) {
        prefixEl.textContent = activeYears.length > 1 ? 'Map Years' : 'Map Year';
    }
    if (soilMapYear) {
        if (activeYears.length === 0) {
            soilMapYear.textContent = '—';
        } else {
            soilMapYear.textContent = activeYears.join('+');
        }
    }

    // Build comparison table header from activeYears (only selected years)
    var headerRow = document.getElementById('soil-table-header');
    var showChange = activeYears.length === 2;
    var showTrend = activeYears.length >= 3;
    if (headerRow) {
        var headerHtml = '<th>Property</th>';
        for (var y = 0; y < activeYears.length; y++) {
            headerHtml += '<th>' + activeYears[y] + '</th>';
        }
        if (showTrend) {
            headerHtml += '<th>Trend</th>';
        }
        if (showChange) {
            headerHtml += '<th>Change</th>';
        }
        headerRow.innerHTML = headerHtml;
    }

    // Build comparison table rows
    // Nitrogen (N) highlights the driving NO3 row
    var highlightNutrient = selectedNutrient === 'N' ? 'NO3' : selectedNutrient;
    var highlightIdx = nutrientIndexMap[highlightNutrient];
    var rows = [];
    var orderedLabels = [];

    // Put selected nutrient first, then rest in fixed order
    for (var i = 0; i < soilLabels.length; i++) {
        if (soilLabels[i][1] === highlightIdx) {
            orderedLabels.unshift(soilLabels[i]);
        } else {
            orderedLabels.push(soilLabels[i]);
        }
    }

    // Determine data source: individual point vs field-wide averages
    var dataSource = isAverage ? null : (num ? allSoilData[num] : null);
    var allAvgs = isAverage ? yearAllAverages : null;

    for (var i = 0; i < orderedLabels.length; i++) {
        var label = orderedLabels[i][0];
        var idx = orderedLabels[i][1];
        var nutrientName = nutrientNameByIdx[idx];
        var cls = (idx === highlightIdx) ? 'highlight' : '';
        var rowHtml = '<tr class="' + cls + '"><td>' + label + '</td>';
        var yearVals = [];
        for (var y = 0; y < activeYears.length; y++) {
            var year = activeYears[y];
            var val = '—';
            if (isAverage && allAvgs && allAvgs[String(year)]) {
                val = allAvgs[String(year)][nutrientName];
            } else if (dataSource && dataSource[year] && nutrientName) {
                val = dataSource[year][nutrientName];
            }
            yearVals.push(val);
        }
        var rowTrend = showTrend ? classifyTrend(yearVals) : null;
        for (var y = 0; y < activeYears.length; y++) {
            var colCls = (activeYears[y] === mapYear) ? 'map-year-col' : '';
            rowHtml += '<td class="' + colCls + '">' + (yearVals[y] || '—') + '</td>';
        }
        if (showTrend) {
            rowHtml += trendCellHtml(rowTrend);
        }
        // Add change column when exactly 2 years are shown
        if (showChange && yearVals.length === 2) {
            var olderVal = parseFloat(yearVals[0]);
            var newerVal = parseFloat(yearVals[1]);
            if (!isNaN(olderVal) && !isNaN(newerVal)) {
                var diff = newerVal - olderVal;
                var diffStr = diff.toFixed(2);
                if (diff > 0) {
                    rowHtml += '<td class="change-up"><span class="change-cell"><span class="arrow">▲</span><span class="value">+' + diffStr + '</span></span></td>';
                } else if (diff < 0) {
                    rowHtml += '<td class="change-down"><span class="change-cell"><span class="arrow">▼</span><span class="value">' + diffStr + '</span></span></td>';
                } else {
                    rowHtml += '<td>—</td>';
                }
            } else {
                rowHtml += '<td>—</td>';
            }
        }
        rowHtml += '</tr>';
        rows.push(rowHtml);
    }
    soilTableBody.innerHTML = rows.join('');
}

// Plotly.purge() (used by renderCharts to cancel stale in-flight draws)
// removes the element's event emitter, so the plotly_click binding must be
// re-attached after every render. All logic lives in handleMapClick.
function handleMapClick(data) {
    if (!data.points || data.points.length === 0) return;
    var pt = data.points[0];
    if (!pt.customdata) return;
    // customdata layout: [lon, lat, ...17 soil values..., point_num]  (index 19)
    var num = (pt.customdata && pt.customdata[19] != null) ? pt.customdata[19] : (pt.text || '');

    // Toggle: clicking the same point deselects
    if (lastClickedPoint === num) {
        lastClickedPoint = null;
        rebuildSoilTable(null, true);
        return;
    }

    lastClickedPoint = num;

    // Show panel
    if (soilDetailPanel) soilDetailPanel.classList.add('visible');

    rebuildSoilTable(num, false);
}

function attachMapClickHandler() {
    var gd = document.getElementById('map-container');
    if (gd && typeof gd.on === 'function') gd.on('plotly_click', handleMapClick);
}

var topCardsPanel = document.getElementById('top-cards-panel');
var bottomCardsPanel = document.getElementById('bottom-cards-panel');

function computeSoilGrade(year) {
    var ratings = yearAverageRatings[String(year)];
    if (!ratings) return {letter: '—', points: 0, blurb: 'No data', color: '#999', extreme: 0};
    var nonOptimal = ratings.filter(function(r) { return r.nutrient !== 'pH' && r.category !== 'Optimal'; });
    var count = nonOptimal.length;
    var letter, points;
    if (count <= 1) { letter = 'A'; points = 4; }
    else if (count <= 3) { letter = 'B'; points = 3; }
    else if (count <= 5) { letter = 'C'; points = 2; }
    else if (count <= 7) { letter = 'D'; points = 1; }
    else { letter = 'F'; points = 0; }
    nonOptimal.sort(function(a, b) { return (b.score || 0) - (a.score || 0); });
    var extremes = nonOptimal.slice(0, 2).map(function(r) {
        return r.nutrient + ' (' + r.category.toLowerCase() + ')';
    }).join(' and ');
    var extremeText = extremes ? ', including ' + extremes : '';
    var blurb = count + ' of 11 nutrients are outside optimal range' + extremeText + '.';
    return {letter: letter, points: points, blurb: blurb, color: reportCardColors[letter], extreme: count};
}

function computePhGrade(year) {
    var avgs = yearAllAverages[String(year)];
    if (!avgs || !avgs['pH']) return {letter: '—', points: 0, blurb: 'No data', color: '#999', extreme: 0};
    var ph = parseFloat(avgs['pH']);
    var diff, absDiff, letter, points;
    if (ph >= 6.0 && ph <= 6.8) { letter = 'A'; points = 4; diff = 0; }
    else if (ph >= 5.7 && ph < 6.0) { letter = 'B'; points = 3; diff = 6.0 - ph; }
    else if (ph > 6.8 && ph <= 7.1) { letter = 'B'; points = 3; diff = ph - 6.8; }
    else if (ph >= 5.4 && ph < 5.7) { letter = 'C'; points = 2; diff = 6.0 - ph; }
    else if (ph > 7.1 && ph <= 7.4) { letter = 'C'; points = 2; diff = ph - 6.8; }
    else if (ph >= 5.0 && ph < 5.4) { letter = 'D'; points = 1; diff = 6.0 - ph; }
    else if (ph > 7.4 && ph <= 7.8) { letter = 'D'; points = 1; diff = ph - 6.8; }
    else { letter = 'F'; points = 0; diff = Math.abs(ph - 6.4); }
    absDiff = Math.round(diff * 10) / 10;
    var blurb;
    if (letter === 'A') {
        blurb = 'Current pH is ' + ph + ', within the optimal 6.0–6.8 range for corn.';
    } else {
        var dir = ph < 6.0 ? 'below' : 'above';
        blurb = 'Current pH is ' + ph + ', ' + absDiff + ' points ' + dir + ' the optimal 6.0–6.8 range for corn.';
    }
    return {letter: letter, points: points, blurb: blurb, color: reportCardColors[letter], extreme: absDiff};
}

function computeWeatherGrade(year) {
    var ec = yearEventCounts[year];
    if (!ec) return {letter: '—', points: 0, blurb: 'No data', color: '#999', extreme: 0};
    var total = ec.total;
    var letter, points;
    if (total <= 3) { letter = 'A'; points = 4; }
    else if (total <= 7) { letter = 'B'; points = 3; }
    else if (total <= 12) { letter = 'C'; points = 2; }
    else if (total <= 18) { letter = 'D'; points = 1; }
    else { letter = 'F'; points = 0; }
    var dom = ec.dominant_type;
    var domLabel = {heat: 'heat stress', rain: 'heavy rain', wind: 'high wind', none: ''}[dom] || '';
    var blurb = total + ' critical weather events this year' + (domLabel ? ', mostly ' + domLabel : '') + '.';
    return {letter: letter, points: points, blurb: blurb, color: reportCardColors[letter], extreme: total};
}

function computeVigorGrade(year) {
    var stats = yearNdviStats[year];
    if (!stats) return {letter: '—', points: 0, blurb: 'No data', color: '#999', extreme: 0};
    var diff = stats.diff_pct;
    var letter, points;
    if (diff >= 0) { letter = 'A'; points = 4; }
    else if (diff >= -5) { letter = 'B'; points = 3; }
    else if (diff >= -10) { letter = 'C'; points = 2; }
    else if (diff >= -15) { letter = 'D'; points = 1; }
    else { letter = 'F'; points = 0; }
    var absDiff = Math.abs(diff).toFixed(1);
    var dir = diff >= 0 ? 'above' : 'below';
    var blurb = 'Current NDVI is ' + absDiff + '% ' + dir + ' the ' + stats.year_count + '-year average for this time of year.';
    return {letter: letter, points: points, blurb: blurb, color: reportCardColors[letter], extreme: Math.abs(diff)};
}

function computeOverallGrade(categories) {
    var sum = 0;
    var worst = null;
    var worstPoints = 5;
    for (var i = 0; i < categories.length; i++) {
        sum += categories[i].points;
        if (categories[i].points < worstPoints) { worstPoints = categories[i].points; worst = categories[i]; }
    }
    var avg = sum / categories.length;
    var letter;
    if (avg >= 3.5) { letter = 'A'; }
    else if (avg >= 2.5) { letter = 'B'; }
    else if (avg >= 1.5) { letter = 'C'; }
    else if (avg >= 0.5) { letter = 'D'; }
    else { letter = 'F'; }
    var worstName = worst ? worst.name : '';
    var blurb = 'Overall grade driven primarily by ' + worstName + ' this season.';
    return {letter: letter, blurb: blurb, color: reportCardColors[letter]};
}

function makeReportCard(name, letter, blurb, color) {
    return '<div class="report-card" style="border-left-color:' + color + '">' +
        '<div class="report-card-name">' + name + '</div>' +
        '<div class="report-card-grade" style="color:' + color + '">' + letter + '</div>' +
        '<div class="report-card-blurb">' + blurb + '</div></div>';
}

function makeOverallCard(letter, blurb, color) {
    return '<div class="report-card overall" style="border-left-color:' + color + '">' +
        '<div class="report-card-name">Overall</div>' +
        '<div class="report-card-grade" style="color:' + color + '">' + letter + '</div>' +
        '<div class="report-card-blurb">' + blurb + '</div></div>';
}

function buildReportCard(mapYear) {
    var grid = document.getElementById('report-card-grid');
    if (!grid) return;
    var soil = computeSoilGrade(mapYear);
    soil.name = 'Soil Fertility';
    var ph = computePhGrade(mapYear);
    ph.name = 'pH Balance';
    var weather = computeWeatherGrade(mapYear);
    weather.name = 'Weather Stress';
    var vigor = computeVigorGrade(mapYear);
    vigor.name = 'Crop Vigor';
    var categories = [soil, ph, weather, vigor];
    var overall = computeOverallGrade(categories);
    var cardsHtml = '';
    cardsHtml += makeOverallCard(overall.letter, overall.blurb, overall.color);
    cardsHtml += makeReportCard(ph.name, ph.letter, ph.blurb, ph.color);
    cardsHtml += makeReportCard(soil.name, soil.letter, soil.blurb, soil.color);
    cardsHtml += makeReportCard(vigor.name, vigor.letter, vigor.blurb, vigor.color);
    cardsHtml += makeReportCard(weather.name, weather.letter, weather.blurb, weather.color);
    grid.innerHTML = cardsHtml;
}

// Initialize with the default field
loadField(currentField);
"""


def _build_multi_field_html_body(payloads: list[dict]) -> str:
    """Assemble a self-contained HTML combining multiple fields.

    Each payload becomes one entry in the ``FIELDS`` JS object; basemap images
    are stripped from both map layouts and stored once per field, then
    re-attached by ``loadField`` so only one b64 copy is embedded per field.
    """
    import json

    first = payloads[0]
    plotly_bundle = first["plotly_bundle"]

    fields: dict[str, dict] = {}
    for p in payloads:
        fid = p["field_id"]
        map_layout = dict(p["map_layout"])
        spread_map_layout = dict(p["spread_map_layout"])
        # Basemap dedup: strip images (and the metadata sub-blobs which are
        # embedded at top level already) from both layouts.
        map_layout.pop("images", None)
        spread_map_layout.pop("images", None)
        for key in ["_nutrient_colors_by_year", "_nutrient_index_map",
                    "_nutrient_display_names", "_js_soil_data", "_available_years",
                    "_zone_acres_by_point"]:
            map_layout.pop(key, None)
        fields[fid] = {
            "title": p["title"],
            "subtitle": p["subtitle"],
            "years": p["years"],
            "defaultMapYear": max(p["available_grid_years"]) if p["available_grid_years"] else 2025,
            "defaultCrop": p["default_crop"],
            "defaultYield": p["default_yield"],
            "basemapB64": p["basemap_b64"],
            "mercatorExtent": list(p["mercator_extent"]) if p["mercator_extent"] else None,
            "mapLayout": map_layout,
            "mapData": p["map_data"],
            "spreadMapLayout": spread_map_layout,
            "spreadMapData": p["spread_map_data"],
            "zoneAcresByPoint": p["zone_acres_by_point"],
            "nutrientColorsByYear": p["nutrient_colors_by_year"],
            "nutrientIndexMap": p["nutrient_index_map"],
            "nutrientDisplayNames": p["nutrient_display_names"],
            "allSoilData": p["js_soil_data"],
            "sufficiencyRatings": p["js_sufficiency_ratings"],
            "yearAverageRatings": p["js_year_average_ratings"],
            "yearAllAverages": p["js_year_all_averages"],
            "availableGridYears": p["available_grid_years"],
            "yearEventCounts": p["year_event_counts"],
            "yearNdviStats": p["year_ndvi_stats"],
            "cgLayout": p["cumulative_gdd_layout"],
            "cgData": p["cumulative_gdd_data"],
            "crLayout": p["cumulative_rainfall_layout"],
            "crData": p["cumulative_rainfall_data"],
            "tempLayout": p["temp_layout"],
            "tempData": p["temp_data"],
            "combinedLayout": p["combined_layout"],
            "combinedData": p["combined_data"],
            "anomalyLayout": p["anomaly_layout"],
            "anomalyData": p["anomaly_data"],
            "solarLayout": p["solar_layout"],
            "solarData": p["solar_data"],
            "compositeHtml": _build_composite_html(p["composites"]),
            "cropTableHtml": _build_crop_table_html(p["crop_history"]),
            "stageLegendHtml": _build_stage_legend_html(p["stage_medians"]),
            "nutrientOptionsHtml": _build_nutrient_options(p["nutrient_display_names"], default_nutrient="P") + '\n<option value="N">Nitrogen (N)</option>',
            "cropOptionsHtml": p["crop_options"],
            "buildYearOptionsHtml": _build_build_year_options(),
        }

    fields_data_json = json.dumps(fields, default=str)
    sufficiency_ranges_json = json.dumps(first["js_sufficiency_ranges"], default=str)
    available_crops_json = json.dumps(first["available_crops"], default=str)
    nutrient_optimal_low_json = json.dumps(first["js_nutrient_optimal_low"], default=str)

    js_body = (
        _MULTI_FIELD_JS
        .replace("@@FIELDS_DATA@@", fields_data_json)
        .replace("@@SUFFICIENCY_RANGES@@", sufficiency_ranges_json)
        .replace("@@AVAILABLE_CROPS@@", available_crops_json)
        .replace("@@NUTRIENT_OPTIMAL_LOW@@", nutrient_optimal_low_json)
        .replace("@@DEFAULT_FIELD_ID@@", json.dumps(first["field_id"]))
    )

    field_options = "\n".join(
        f'<option value="{p["field_id"]}"{" selected" if p is first else ""}>{p["title"]}</option>'
        for p in payloads
    )

    report_card_html = """<div class="report-card-panel">
    <h4>Field Report Card</h4>
    <div id="report-card-grid" class="report-card-grid"></div>
</div>"""

    analysis_summary_html = """<div class="analysis-summary-panel">
    <h4>Analysis Summary</h4>
    <div id="analysis-summary-body" class="analysis-summary-body"></div>
</div>"""

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Combined Fields Dashboard</title>
<style>{_css_template()}{_field_css_template()}
.field-selector-bar {{ display: flex; align-items: center; gap: 10px; background: #eef3fa; border: 1px solid #c9d6e5; border-radius: 8px; padding: 10px 14px; margin-bottom: 12px; flex-wrap: wrap; }}
.field-selector-bar label {{ font-weight: 600; color: #1f3a5f; }}
.field-selector-bar select {{ padding: 6px 10px; font-size: 14px; border: 1px solid #b8c7d9; border-radius: 6px; background: #fff; min-width: 280px; }}
.analysis-summary-panel {{ background: #f4f8fc; border: 1px solid #c9d6e5; border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; }}
.analysis-summary-panel h4 {{ margin: 0 0 8px; color: #1f3a5f; }}
.analysis-summary-body {{ font-size: 14px; color: #23405f; line-height: 1.5; }}
.analysis-summary-body p {{ margin: 6px 0; }}
.analysis-grades {{ margin-bottom: 6px; font-weight: 600; color: #1f3a5f; }}
.analysis-empty {{ font-style: italic; color: #6b7c92; }}
.analysis-compare-label {{ margin-top: 8px; }}
.analysis-table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 4px; }}
.analysis-table th, .analysis-table td {{ border: 1px solid #c9d6e5; padding: 4px 8px; text-align: center; }}
.analysis-table th {{ background: #e3ecf6; color: #1f3a5f; }}
.analysis-table td:first-child {{ text-align: left; }}
.analysis-table tr.analysis-current {{ background: #dce9f7; font-weight: 600; }}
</style>
<script>{plotly_bundle}</script>
</head>
<body>
<div class="dashboard-header">
    <div>
        <h1 id="dashboard-title">{first["title"]}</h1>
        <p class="subtitle" id="dashboard-subtitle">{first["subtitle"]}</p>
    </div>
</div>
<div class="field-selector-bar">
    <label for="field-select">Field:</label>
    <select id="field-select" onchange="loadField(this.value)">
        {field_options}
    </select>
</div>
<div class="controls-bar">
    <span>Year Filter (Temp + NDVI):</span>
    <div class="year-filter-bar" id="year-filter-bar"></div>
    <span class="legend-inline"><span class="legend-symbol">&#9679;</span> Sentinel-2</span>
</div>
{report_card_html}
{analysis_summary_html}
<div class="nutrient-selector-bar">
    <label for="nutrient-select">Nutrient:</label>
    <select id="nutrient-select" onchange="updateNutrientDisplay()"></select>
    <span id="nutrient-legend" class="nutrient-legend">
        <span class="legend-swatch" style="background:#d62728"></span> Low
        <span class="legend-swatch" style="background:#ffeb3b"></span> Mid
        <span class="legend-swatch" style="background:#2ca02c"></span> High
    </span>
    <label for="crop-select">Crop:</label>
    <select id="crop-select" onchange="updateCropDisplay()"></select>
    <label for="yield-goal">Yield:</label>
    <div class="yield-input-wrapper">
        <input type="number" id="yield-goal" value="{first["default_yield"]}" min="0" max="400" step="1" oninput="updateYieldGoal()">
        <span class="yield-unit">bu/acre</span>
    </div>
    <label for="fertilizer-select">Fertilizer:</label>
    <select id="fertilizer-select" onchange="updateFertilizerDisplay()"></select>
    <span id="build-years-wrap">
        <label for="build-years">Years to Build to Min PPM:</label>
        <select id="build-years" onchange="updateBuildYears()"></select>
    </span>
</div>
<div class="map-row">
    <div class="map-section">
        <div id="map-container"></div>
    </div>
    <div class="map-section">
        <div id="spread-map-container"></div>
        <div id="spread-map-info" class="spread-map-info"></div>
    </div>
</div>
<!-- Top 5 sufficiency cards -->
<div id="top-cards-panel" class="top-cards-panel">
    <h4>Top 5 Critical Soil Values</h4>
    <div id="top-cards-grid" class="cards-grid top-5">
        <!-- Top 5 cards built by JS -->
    </div>
</div>
<!-- Soil detail panel (year comparison table for clicked point) -->
<div id="soil-detail-panel" class="soil-detail-panel">
    <h4>Soil Test Results — <span id="soil-point-id">Field Average</span> (<span id="map-year-prefix">Map Year</span>: <span id="soil-map-year">—</span>)</h4>
    <div class="soil-table-wrapper">
        <table class="soil-table">
            <thead>
                <tr id="soil-table-header">
                    <th>Property</th>
                    <!-- Year columns built dynamically by JS from activeYears -->
                </tr>
            </thead>
            <tbody id="soil-table-body"></tbody>
        </table>
    </div>
</div>
<!-- Remaining sufficiency cards -->
<div id="bottom-cards-panel" class="bottom-cards-panel">
    <h4>Additional Nutrient Levels</h4>
    <div id="bottom-cards-grid" class="cards-grid bottom-7">
        <!-- Remaining cards built by JS -->
    </div>
</div>
<div class="temp-section">
    <h3>Daily Temperature Range (°F)</h3>
    <div id="temp-chart" class="temp-chart-container"></div>
</div>
<div class="grid">
    <div class="chart-card">
        <h3>Cumulative GDD</h3>
        <div id="cumulative-gdd-chart" class="chart-container"></div>
        <div id="stage-legend-slot"></div>
    </div>
    <div class="chart-card">
        <h3>Cumulative Rainfall (inches)</h3>
        <div id="cumulative-rainfall-chart" class="chart-container"></div>
    </div>
</div>
<div class="grid">
    <div class="chart-card">
        <h3>Rainfall Anomaly</h3>
        <div id="anomaly-chart" class="chart-container"></div>
    </div>
    <div class="chart-card">
        <h3>Solar Radiation</h3>
        <div id="solar-chart" class="chart-container"></div>
        <p class="chart-note">&lt; 18 MJ/m² considered low</p>
    </div>
</div>
<div class="combined-section">
    <h3>NDVI vs Cumulative GDD (with Corn Growth Stages)</h3>
    <div id="combined-chart" class="combined-chart-container"></div>
    <p class="chart-legend-note"><span class="symbol">&#9671;</span> Open diamond = temporal anomaly (deviates >0.15 from seasonal trend)</p>
    <div id="composites-slot"></div>
</div>
<div id="crop-table-slot"></div>
<script>
{js_body}
</script>
</body>
</html>
"""


def _build_year_buttons(years: list[int]) -> str:
    return "\n".join(
        f'<button class="year-btn active" data-year="{y}" onclick="toggleYear(\'{y}\')">{y}</button>'
        for y in years
    )


def _build_composite_html(composites: list[dict]) -> str:
    if not composites:
        return ""
    items = "\n".join(
        f'<div class="composite-item"><img src="{c["src"]}" alt="{c["caption"]}" loading="lazy"><div class="caption">{c["caption"]}</div></div>'
        for c in composites
    )
    return f'<div class="composite-gallery">{items}</div>'


def _build_stage_legend_html(stage_medians: list[dict]) -> str:
    if not stage_medians:
        return ""
    items = []
    for s in stage_medians:
        items.append(
            f'<span class="stage-item"><span class="stage-swatch" style="background:{s["color"]}"></span>{s["stage"]} (doy {s["median_doy"]})</span>'
        )
    dividers = '<span class="stage-divider">→</span>'
    return (
        '<div class="stage-legend-bar">'
        '<span class="stage-label">Corn Stages:</span>'
        + dividers.join(items)
        + '</div>'
    )


def _build_crop_table_html(crop_history: list[dict]) -> str:
    if crop_history:
        rows = "\n".join(
            f'<tr><td>{r["year"]}</td><td>{r.get("crop_name", "")}</td><td>{r.get("scene_count", "")}</td><td>{r.get("peak_ndvi", "")}</td></tr>'
            for r in crop_history
        )
        return f"""\
<div class="crop-section">
<h3>Crop History</h3>
<table class="crop-table">
<thead><tr><th>Year</th><th>Crop</th><th>Scenes</th><th>Peak NDVI</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</div>"""
    return """<div class="crop-section">
<h3>Crop History</h3>
<p class="crop-placeholder">Crop history data not yet available for this field</p>
</div>"""


def _build_chart_annotations(events: list[dict]) -> dict[str, list[dict]]:
    event_type_icons = {
        "heavy_rain": "&#127783;",
        "heat_wave": "&#127777;",
        "cool_period": "&#10052;",
        "ndvi_dip": "&#128315;",
        "ndvi_surge": "&#128314;",
        "low_solar": "&#9728;",
        "high_wind": "&#128168;",
    }
    events_2025 = [e for e in events if e["year"] == 2025]
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
    return chart_annotations


def _inject_chart_annotations(layout: dict, target: str, chart_annotations: dict[str, list[dict]]) -> dict:
    if target in chart_annotations:
        if "annotations" not in layout:
            layout["annotations"] = []
        layout["annotations"].extend(chart_annotations[target])
    return layout


def _build_nutrient_options(nutrient_display_names: dict[str, str], default_nutrient: str = "pH") -> str:
    return "\n".join(
        f'<option value="{k}"{" selected" if k == default_nutrient else ""}>{v}</option>'
        for k, v in nutrient_display_names.items()
    ) if nutrient_display_names else ""


def _build_build_year_options() -> str:
    parts = ['<option value="no_build">Crop Removal Only (No Build)</option>']
    for y in [1, 2, 3, 4, 5]:
        sel = ' selected' if y == 1 else ''
        parts.append(
            f'<option value="{y}"{sel}>{y} year{"s" if y != 1 else ""}</option>'
        )
    return "\n".join(parts)


def _build_crop_options(available_crops: list[str], default_crop: str) -> str:
    return "\n".join(
        f'<option value="{c}"{" selected" if c == default_crop else ""}>{c.title()}</option>'
        for c in available_crops
    )


def _build_field_html_body(
    plotly_bundle: str,
    title: str,
    subtitle: str,
    map_layout: dict,
    map_data: list[dict],
    spread_map_layout: dict,
    spread_map_data: list[dict],
    zone_acres_by_point: dict[str, float],
    nutrient_colors_by_year: dict[int, dict[str, list[str]]],
    nutrient_index_map: dict[str, int],
    nutrient_display_names: dict[str, str],
    js_soil_data: dict[str, dict[str, dict[str, str]]],
    js_sufficiency_ratings: dict[str, dict[str, list[dict]]],
    js_year_average_ratings: dict[str, list[dict]],
    js_sufficiency_ranges: dict[str, str],
    js_nutrient_optimal_low: dict[str, float],
    js_year_all_averages: dict[str, dict[str, str]],
    available_grid_years: list[int],
    cumulative_gdd_layout: dict,
    cumulative_gdd_data: list[dict],
    cumulative_rainfall_layout: dict,
    cumulative_rainfall_data: list[dict],
    temp_layout: dict,
    temp_data: list[dict],
    combined_layout: dict,
    combined_data: list[dict],
    anomaly_layout: dict,
    anomaly_data: list[dict],
    solar_layout: dict,
    solar_data: list[dict],
    composites: list[dict],
    crop_history: list[dict],
    years: list[int],
    stage_medians: list[dict],
    events: list[dict],
    year_event_counts: dict[int, dict],
    year_ndvi_stats: dict[int, dict],
    crop_options: str,
    default_crop: str,
    available_crops: list[str],
    default_yield: int,
    grid_points: gpd.GeoDataFrame | None = None,
) -> str:
    """Assemble the self-contained HTML for a single-field dashboard."""
    import json

    year_buttons = _build_year_buttons(years)
    composite_html = _build_composite_html(composites)
    stage_legend_html = _build_stage_legend_html(stage_medians)
    crop_table_html = _build_crop_table_html(crop_history)

    report_card_html = """<div class="report-card-panel">
    <h4>Field Report Card</h4>
    <div id="report-card-grid" class="report-card-grid"></div>
</div>"""

    # Severe event annotations for charts (max 3 per chart); layouts were
    # pre-annotated by the payload builder, so no injection is needed here.

    map_layout_json = json.dumps(map_layout, default=str)
    map_data_json = json.dumps(map_data, default=str)
    spread_map_layout_json = json.dumps(spread_map_layout, default=str)
    spread_map_data_json = json.dumps(spread_map_data, default=str)
    cum_gdd_layout_json = json.dumps(cumulative_gdd_layout, default=str)
    cum_gdd_data_json = json.dumps(cumulative_gdd_data, default=str)
    cum_rain_layout_json = json.dumps(cumulative_rainfall_layout, default=str)
    cum_rain_data_json = json.dumps(cumulative_rainfall_data, default=str)
    temp_layout_json = json.dumps(temp_layout, default=str)
    temp_data_json = json.dumps(temp_data, default=str)
    combined_layout_json = json.dumps(combined_layout, default=str)
    combined_data_json = json.dumps(combined_data, default=str)
    anomaly_layout_json = json.dumps(anomaly_layout, default=str)
    anomaly_data_json = json.dumps(anomaly_data, default=str)
    solar_layout_json = json.dumps(solar_layout, default=str)
    solar_data_json = json.dumps(solar_data, default=str)

    # Nutrient data for JS
    nutrient_colors_by_year_json = json.dumps(nutrient_colors_by_year, default=str)
    nutrient_index_map_json = json.dumps(nutrient_index_map, default=str)
    nutrient_display_names_json = json.dumps(nutrient_display_names, default=str)
    js_soil_data_json = json.dumps(js_soil_data, default=str)
    js_sufficiency_ratings_json = json.dumps(js_sufficiency_ratings, default=str)
    js_year_average_ratings_json = json.dumps(js_year_average_ratings, default=str)
    js_sufficiency_ranges_json = json.dumps(js_sufficiency_ranges, default=str)
    js_nutrient_optimal_low_json = json.dumps(js_nutrient_optimal_low, default=str)
    js_year_all_averages_json = json.dumps(js_year_all_averages, default=str)
    available_grid_years_json = json.dumps(available_grid_years, default=str)
    zone_acres_by_point_json = json.dumps(zone_acres_by_point, default=str)
    year_event_counts_json = json.dumps(year_event_counts, default=str)
    year_ndvi_stats_json = json.dumps(year_ndvi_stats, default=str)

    # Build nutrient dropdown options (default = pH)
    nutrient_options = _build_nutrient_options(nutrient_display_names)

    # Build map year dropdown options (default = most recent year)
    default_map_year = max(available_grid_years) if available_grid_years else 2025
    map_year_options = "\n".join(
        f'<option value="{y}"{" selected" if y == default_map_year else ""}>{y}</option>'
        for y in sorted(available_grid_years)
    ) if available_grid_years else ""

    # Build years to build to min PPM options (default = 1)
    build_year_options = _build_build_year_options()

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
{report_card_html}
<div class="nutrient-selector-bar">
    <label for="nutrient-select">Nutrient:</label>
    <select id="nutrient-select" onchange="updateNutrientDisplay()">
        {nutrient_options}
    </select>
    <span id="nutrient-legend" class="nutrient-legend">
        <span class="legend-swatch" style="background:#d62728"></span> Low
        <span class="legend-swatch" style="background:#ffeb3b"></span> Mid
        <span class="legend-swatch" style="background:#2ca02c"></span> High
    </span>
    <label for="crop-select">Crop:</label>
    <select id="crop-select" onchange="updateCropDisplay()">
        {crop_options}
    </select>
    <label for="yield-goal">Yield:</label>
    <div class="yield-input-wrapper">
        <input type="number" id="yield-goal" value="{default_yield}" min="0" max="400" step="1" oninput="updateYieldGoal()">
        <span class="yield-unit">bu/acre</span>
    </div>
    <label for="fertilizer-select">Fertilizer:</label>
    <select id="fertilizer-select" onchange="updateFertilizerDisplay()">
        <!-- Options populated dynamically by updateNutrientDisplay() -->
    </select>
    <label for="build-years">Years to Build to Min PPM:</label>
    <select id="build-years" onchange="updateBuildYears()">
        {build_year_options}
    </select>
</div>
<div class="map-row">
    <div class="map-section">
        <div id="map-container"></div>
    </div>
    <div class="map-section">
        <div id="spread-map-container"></div>
        <div id="spread-map-info" class="spread-map-info"></div>
    </div>
</div>
<!-- Top 5 sufficiency cards -->
<div id="top-cards-panel" class="top-cards-panel">
    <h4>Top 5 Critical Soil Values</h4>
    <div id="top-cards-grid" class="cards-grid top-5">
        <!-- Top 5 cards built by JS -->
    </div>
</div>
<!-- Soil detail panel (year comparison table for clicked point) -->
<div id="soil-detail-panel" class="soil-detail-panel">
    <h4>Soil Test Results — <span id="soil-point-id">Field Average</span> (<span id="map-year-prefix">Map Year</span>: <span id="soil-map-year">—</span>)</h4>
    <div class="soil-table-wrapper">
        <table class="soil-table">
            <thead>
                <tr id="soil-table-header">
                    <th>Property</th>
                    <!-- Year columns built dynamically by JS from activeYears -->
                </tr>
            </thead>
            <tbody id="soil-table-body"></tbody>
        </table>
    </div>
</div>
<!-- Remaining sufficiency cards -->
<div id="bottom-cards-panel" class="bottom-cards-panel">
    <h4>Additional Nutrient Levels</h4>
    <div id="bottom-cards-grid" class="cards-grid bottom-7">
        <!-- Remaining cards built by JS -->
    </div>
</div>
<div class="temp-section">
    <h3>Daily Temperature Range (°F)</h3>
    <div id="temp-chart" class="temp-chart-container"></div>
</div>
<div class="grid">
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
<div class="grid">
    <div class="chart-card">
        <h3>Rainfall Anomaly</h3>
        <div id="anomaly-chart" class="chart-container"></div>
    </div>
    <div class="chart-card">
        <h3>Solar Radiation</h3>
        <div id="solar-chart" class="chart-container"></div>
        <p class="chart-note">&lt; 18 MJ/m² considered low</p>
    </div>
</div>
<div class="combined-section">
    <h3>NDVI vs Cumulative GDD (with Corn Growth Stages)</h3>
    <div id="combined-chart" class="combined-chart-container"></div>
    <p class="chart-legend-note"><span class="symbol">&#9671;</span> Open diamond = temporal anomaly (deviates >0.15 from seasonal trend)</p>
    {composite_html}
</div>
{crop_table_html}
<script>
// Unified year filtering
var activeYears = [{', '.join(str(y) for y in years)}];
var mapYear = {default_map_year};

function toggleYear(year) {{
    var btn = document.querySelector('.year-btn[data-year="' + year + '"]');
    var idx = activeYears.indexOf(parseInt(year));
    if (idx >= 0) {{
        // Removing year
        activeYears.splice(idx, 1);
        if (btn) btn.classList.remove('active');
        // If removed the current mapYear, fall back to highest remaining
        if (mapYear === parseInt(year)) {{
            mapYear = activeYears.length > 0 ? Math.max.apply(null, activeYears) : null;
        }}
    }} else {{
        // Adding year — becomes the new mapYear
        activeYears.push(parseInt(year));
        if (btn) btn.classList.add('active');
        mapYear = parseInt(year);
    }}
    activeYears.sort(function(a, b) {{ return a - b; }});
    updateAllCharts();
    updateCardsVisibility();
    if (lastClickedPoint) {{
        rebuildSoilTable(lastClickedPoint, false);
    }} else {{
        rebuildSoilTable(null, true);
    }}
}}

function setAllYears(active) {{
    if (active) {{
        activeYears = [{', '.join(str(y) for y in years)}];
        mapYear = {default_map_year};
        document.querySelectorAll('.year-btn[data-year]').forEach(function(btn) {{
            btn.classList.add('active');
        }});
    }} else {{
        activeYears = [];
        mapYear = null;
        document.querySelectorAll('.year-btn[data-year]').forEach(function(btn) {{
            btn.classList.remove('active');
        }});
    }}
    updateAllCharts();
    updateCardsVisibility();
    if (lastClickedPoint) {{
        rebuildSoilTable(lastClickedPoint, false);
    }} else {{
        rebuildSoilTable(null, true);
    }}
}}

function updateChartVisibility(chartId, years) {{
    var gd = document.getElementById(chartId);
    if (!gd || !gd.data) return;
    var visible = [];
    for (var i = 0; i < gd.data.length; i++) {{
        var d = gd.data[i];
        // Traces without a 'year' property (field boundary, centroid label) stay visible always
        if (d.year === undefined) {{
            visible.push(true);
        }} else {{
            var show = years.indexOf(d.year) >= 0;
            visible.push(show ? true : 'legendonly');
        }}
    }}
    Plotly.restyle(gd, {{visible: visible}});
}}

function updateAllCharts() {{
    updateChartVisibility('temp-chart', activeYears);
    updateChartVisibility('cumulative-gdd-chart', activeYears);
    updateChartVisibility('cumulative-rainfall-chart', activeYears);
    updateChartVisibility('combined-chart', activeYears);
    updateChartVisibility('anomaly-chart', activeYears);
    updateChartVisibility('solar-chart', activeYears);
    // Map: show only the current mapYear trace; hide all others
    var mapGd = document.getElementById('map-container');
    if (mapGd && mapGd.data) {{
        var mapVisible = [];
        for (var i = 0; i < mapGd.data.length; i++) {{
            var d = mapGd.data[i];
            if (d.year === undefined) {{
                mapVisible.push(true);
            }} else {{
                mapVisible.push((mapYear !== null && d.year === mapYear) ? true : 'legendonly');
            }}
        }}
        Plotly.restyle(mapGd, {{visible: mapVisible}});
    }}
    // Spread map: same year-based visibility
    var spreadGd = document.getElementById('spread-map-container');
    if (spreadGd && spreadGd.data) {{
        var spreadVisible = [];
        for (var i = 0; i < spreadGd.data.length; i++) {{
            var d = spreadGd.data[i];
            if (d.year === undefined) {{
                spreadVisible.push(true);
            }} else {{
                spreadVisible.push((mapYear !== null && d.year === mapYear) ? true : 'legendonly');
            }}
        }}
        Plotly.restyle(spreadGd, {{visible: spreadVisible}});
    }}
}}

var mapLayout = {map_layout_json};
var mapData = {map_data_json};
Plotly.newPlot('map-container', mapData, mapLayout, {{responsive: true}});

var spreadMapLayout = {spread_map_layout_json};
var spreadMapData = {spread_map_data_json};
Plotly.newPlot('spread-map-container', spreadMapData, spreadMapLayout, {{responsive: true}});

// Multi-year nutrient selector data
var nutrientColorsByYear = {nutrient_colors_by_year_json};
var nutrientIndexMap = {nutrient_index_map_json};
var nutrientDisplayNames = {nutrient_display_names_json};
var allSoilData = {js_soil_data_json};
var sufficiencyRatings = {js_sufficiency_ratings_json};
var yearAverageRatings = {js_year_average_ratings_json};
var sufficiencyRanges = {js_sufficiency_ranges_json};
var yearAllAverages = {js_year_all_averages_json};
var availableGridYears = {available_grid_years_json};
var zoneAcresByPoint = {zone_acres_by_point_json};
var spreadMapTotal = 0;
var selectedNutrient = 'pH';
var selectedCrop = '{default_crop}';
var availableCrops = {json.dumps(available_crops)};
var yieldGoal = {default_yield};
var yearEventCounts = {year_event_counts_json};
var yearNdviStats = {year_ndvi_stats_json};

var fertilizersByNutrient = {{
    "P": ["MAP (11-52-0)", "DAP (18-46-0)", "Triple Super Phosphate (0-46-0)"],
    "K": ["Potash (0-0-60)", "Sulfate of Potash (0-0-50)"],
    "NO3": ["Urea (46-0-0)", "UAN (28-0-0)"],
    "S": ["Ammonium Sulfate (21-0-0)", "Elemental Sulfur (90% S)"],
    "ZN": ["Zinc Sulfate", "Zinc Oxide"],
    "pH": ["Ag Lime", "Pelletized Lime"],
    "BpH": ["Ag Lime", "Pelletized Lime"],
}};
var selectedFertilizer = null;
var buildYears = "1";

// Fertilizer rate formula source: Fertilizer_Product_sheet.xlsx (confirmed by user)
// Step 1a: maintenance_lb_P2O5_per_acre = yield_goal * 0.3 (P2O5 per bu corn)
// Step 1b: buildup_lb_P2O5_per_acre = (target_ppm - current_ppm) * 18
// Step 1c: total_P2O5_lb_per_acre = maintenance + (buildup / years_to_build)
// Step 2: product_rate_per_acre = total_P2O5_lb_per_acre / productAnalysis["P"][selected_product]
// Step 3: recommended_rate_per_year = product_rate_per_acre (already per-year)
const productAnalysis = {{
  "P": {{
    "MAP (11-52-0)": 0.52,
    "DAP (18-46-0)": 0.46,
    "Triple Super Phosphate (0-46-0)": 0.46
  }}
}};
const ppmToNutrientFactor = {{
  "P": 18
}};
var nutrientOptimalLow = {js_nutrient_optimal_low_json};

// Initialize fertilizer dropdown based on default nutrient (pH)
repopulateFertilizerDropdown();
updateSpreadMapInfo();
updateSpreadMapRates();

// Card color mapping
var cardColors = {{
    "Very Low": "#d62728",
    "Low": "#ff7f0e",
    "Optimal": "#2ca02c",
    "High": "#bcbd22",
    "Very High": "#9467bd"
}};

function buildCards(mapYear) {{
    var topGrid = document.getElementById('top-cards-grid');
    var bottomGrid = document.getElementById('bottom-cards-grid');
    if (!topGrid || !bottomGrid) return;

    var ratings = yearAverageRatings[String(mapYear)];
    if (!ratings || ratings.length === 0) {{
        topGrid.innerHTML = '';
        bottomGrid.innerHTML = '';
        return;
    }}

    var top5 = ratings.slice(0, 5);
    var bottom7 = ratings.slice(5);

    function makeCard(r) {{
        var color = cardColors[r.category] || '#999';
        var displayName = nutrientDisplayNames[r.nutrient] || r.nutrient;
        var rangeText = sufficiencyRanges[r.nutrient] || '';
        var rangeHtml = rangeText ? '<div class="card-range">Optimal range: ' + rangeText + '</div>' : '';
        return '<div class="sufficiency-card" style="border-left-color:' + color + '">' +
            '<div class="card-nutrient">' + displayName + '</div>' +
            '<div class="card-value">' + r.value + '</div>' +
            '<div class="card-category" style="color:' + color + '">' + r.category + '</div>' +
            rangeHtml +
            '</div>';
    }}

    topGrid.innerHTML = top5.map(makeCard).join('');
    bottomGrid.innerHTML = bottom7.map(makeCard).join('');
}}

function updateCardsVisibility() {{
    if (mapYear === null || activeYears.length === 0) {{
        if (topCardsPanel) topCardsPanel.classList.remove('visible');
        if (bottomCardsPanel) bottomCardsPanel.classList.remove('visible');
    }} else {{
        if (topCardsPanel) topCardsPanel.classList.add('visible');
        if (bottomCardsPanel) bottomCardsPanel.classList.add('visible');
        buildCards(mapYear);
    }}
    var rcYear = mapYear !== null ? mapYear : (activeYears.length > 0 ? Math.max(...activeYears) : 0);
    if (rcYear > 0) buildReportCard(rcYear);
}}

function repopulateFertilizerDropdown() {{
    var fertSelect = document.getElementById('fertilizer-select');
    if (!fertSelect) return;

    // Clear existing options
    fertSelect.innerHTML = '';

    var products = fertilizersByNutrient[selectedNutrient];
    if (!products || products.length === 0) {{
        // No fertilizers for this nutrient (pH, OM, CEC, saturation %, etc.)
        var opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '\u2014';  // em dash
        fertSelect.appendChild(opt);
        selectedFertilizer = null;
        return;
    }}

    products.forEach(function(prod) {{
        var opt = document.createElement('option');
        opt.value = prod;
        opt.textContent = prod;
        fertSelect.appendChild(opt);
    }});
    selectedFertilizer = products[0];
}}

function updateNutrientDisplay() {{
    var select = document.getElementById('nutrient-select');
    if (!select) return;
    selectedNutrient = select.value;
    repopulateFertilizerDropdown();
    updateSpreadMapInfo();
    updateSpreadMapRates();
    var idx = nutrientIndexMap[selectedNutrient];
    var gd = document.getElementById('map-container');
    if (!gd || !gd.data) return;

    // Build a lookup from year -> customdata (only marker traces carry it)
    var customdataByYear = {{}};
    for (var i = 0; i < gd.data.length; i++) {{
        var t = gd.data[i];
        if (t.year === undefined) continue;
        if (t.role === 'markers' && t.customdata) {{
            customdataByYear[t.year] = t.customdata;
        }}
    }}

    // Update colors, text labels, and hover for ALL year traces
    for (var i = 0; i < gd.data.length; i++) {{
        var trace = gd.data[i];
        if (trace.year === undefined) continue;
        var year = trace.year;
        var colors = nutrientColorsByYear[year][selectedNutrient];
        var name = nutrientDisplayNames[selectedNutrient] || selectedNutrient;
        if (trace.role === 'markers') {{
            if (colors) {{
                Plotly.restyle(gd, {{'marker.color': [colors]}}, [i]);
            }}
            var newHover = '<b>Sample Point %{{customdata[19]}}</b><br>' +
                name + ': %{{customdata[' + idx + ']}}<br>' +
                'Year: ' + year + '<br>' +
                'Lat: %{{customdata[1]}} | Lon: %{{customdata[0]}}<extra></extra>';
            Plotly.restyle(gd, {{'hovertemplate': newHover}}, [i]);
        }} else if (trace.role === 'labels') {{
            var cd = customdataByYear[year];
            if (cd) {{
                var newText = cd.map(function(row) {{ return row[idx]; }});
                Plotly.restyle(gd, {{'text': [newText]}}, [i]);
            }}
        }}
    }}

    // If a point is already selected, rebuild the table with the new nutrient at the top
    if (lastClickedPoint) {{
        rebuildSoilTable(lastClickedPoint, false);
    }} else {{
        rebuildSoilTable(null, true);
    }}
}}

function updateCropDisplay() {{
    var select = document.getElementById('crop-select');
    if (!select) return;
    selectedCrop = select.value;

    // No-op for now: only corn sufficiency ranges are fully defined.
    // When additional crops are added, this will:
    // 1. Reload sufficiencyRanges for selectedCrop
    // 2. Recompute nutrientColorsByYear using crop-specific ranges
    // 3. Call updateNutrientDisplay() to refresh map colors
    // 4. Call buildCards(mapYear) to refresh deficiency cards

    updateCardsVisibility();
    if (lastClickedPoint) {{
        rebuildSoilTable(lastClickedPoint, false);
    }} else {{
        rebuildSoilTable(null, true);
    }}
}}

function updateYieldGoal() {{
    var input = document.getElementById('yield-goal');
    if (!input) return;
    var val = parseInt(input.value, 10);
    if (isNaN(val) || val < 0) {{
        val = 0;
    }} else if (val > 400) {{
        val = 400;
    }}
    yieldGoal = val;

    updateSpreadMapInfo();
    updateSpreadMapRates();

    updateCardsVisibility();
    if (lastClickedPoint) {{
        rebuildSoilTable(lastClickedPoint, false);
    }} else {{
        rebuildSoilTable(null, true);
    }}
}}

function updateFertilizerDisplay() {{
    var select = document.getElementById('fertilizer-select');
    if (!select) return;
    selectedFertilizer = select.value;
    updateSpreadMapInfo();
    updateSpreadMapRates();
}}

function updateBuildYears() {{
    var select = document.getElementById('build-years');
    if (!select) return;
    buildYears = select.value;
    updateSpreadMapRates();
}}

function updateSpreadMapInfo() {{
    var info = document.getElementById('spread-map-info');
    if (!info) return;
    var fert = selectedFertilizer || '\u2014';
    var mode = buildYears === 'no_build' ? ', Crop Removal Only' : '';
    var totalText = spreadMapTotal > 0
        ? ' \u2014 ' + String(Math.round(spreadMapTotal / 5) * 5).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',') + ' lbs total'
        : '';
    info.textContent = yieldGoal + ' bu/acre goal, ' + fert + mode + totalText;
}}

function rateToColor(t) {{
    var r, g, b;
    if (t <= 0.5) {{
        var p = t * 2;
        r = 220 + Math.round(p * 20);
        g = 40 + Math.round(p * 180);
        b = 40 + Math.round(p * 10);
    }} else {{
        var p = (t - 0.5) * 2;
        r = 240 - Math.round(p * 200);
        g = 220 - Math.round(p * 40);
        b = 50 + Math.round(p * 10);
    }}
    return 'rgb(' + r + ',' + g + ',' + b + ')';
}}

function updateSpreadMapRates() {{
    var gd = document.getElementById('spread-map-container');
    if (!gd || !gd.data) return;

    // If not P or no fertilizer selected, reset markers to gray, labels to dash
    if (selectedNutrient !== 'P' || !selectedFertilizer) {{
        spreadMapTotal = 0;
        updateSpreadMapInfo();
        for (var i = 0; i < gd.data.length; i++) {{
            var trace = gd.data[i];
            if (trace.year === undefined) continue;
            var n = trace.x ? trace.x.length : 0;
            if (trace.role === 'markers') {{
                Plotly.restyle(gd, {{'marker.color': [new Array(n).fill('#999999')]}}, [i]);
            }} else if (trace.role === 'labels') {{
                Plotly.restyle(gd, {{'text': [new Array(n).fill('\u2014')]}}, [i]);
            }}
        }}
        return;
    }}

    var idx = nutrientIndexMap[selectedNutrient];
    if (idx === undefined) return;

    var factor = ppmToNutrientFactor[selectedNutrient];
    var prodRate = productAnalysis[selectedNutrient]?.[selectedFertilizer];
    if (!factor || !prodRate) return;

    var targetPpm = nutrientOptimalLow[selectedNutrient];
    if (targetPpm === undefined) return;

    for (var i = 0; i < gd.data.length; i++) {{
        var trace = gd.data[i];
        if (trace.year === undefined) continue;

        var cd = trace.customdata;
        if (!cd && trace.role === 'labels') {{
            for (var j = 0; j < gd.data.length; j++) {{
                var mt = gd.data[j];
                if (mt.role === 'markers' && mt.year === trace.year && mt.customdata) {{
                    cd = mt.customdata;
                    break;
                }}
            }}
        }}
        if (!cd) continue;

        // Pass 1: compute all rates
        var rates = cd.map(function(row) {{
            var currentPpm = parseFloat(row[idx]);
            if (isNaN(currentPpm)) return null;
            var maintenance = yieldGoal * 0.3;
            var buildup;
            if (buildYears === 'no_build') {{
                buildup = 0;
            }} else {{
                var years = parseInt(buildYears, 10) || 1;
                buildup = Math.max(0, (targetPpm - currentPpm) * factor) / years;
            }}
            var totalLbs = maintenance + buildup;
            if (totalLbs <= 0) return 0;
            return totalLbs / prodRate;
        }});

        // Compute acreage-weighted field total from visible year
        var accumulatedLbs = 0;
        for (var k = 0; k < rates.length; k++) {{
            if (rates[k] !== null && rates[k] > 0) {{
                var ptId = cd[k][19];
                var acres = zoneAcresByPoint[ptId] || 0;
                accumulatedLbs += rates[k] * acres;
            }}
        }}
        spreadMapTotal = accumulatedLbs;

        // Find min/max non-zero rates for color scale
        var nonZero = rates.filter(function(r) {{ return r !== null && r > 0; }});
        var minRate = nonZero.length > 0 ? Math.min.apply(null, nonZero) : 0;
        var maxRate = nonZero.length > 0 ? Math.max.apply(null, nonZero) : 0;
        var range = maxRate - minRate;

        // Pass 2: build colors and label texts
        var colors = [];
        var texts = [];
        for (var k = 0; k < rates.length; k++) {{
            var r = rates[k];
            if (r === null || r === 0) {{
                colors.push('#999999');
                texts.push('\u2014');
            }} else {{
                var t = range > 0 ? (r - minRate) / range : 0.5;
                colors.push(rateToColor(t));
                texts.push(String(Math.round(r / 5) * 5));
            }}
        }}

        if (trace.role === 'markers') {{
            Plotly.restyle(gd, {{'marker.color': [colors]}}, [i]);
        }} else if (trace.role === 'labels') {{
            Plotly.restyle(gd, {{'text': [texts]}}, [i]);
        }}
    }}
    updateSpreadMapInfo();
}}

// Grid point click handler — year comparison table
var soilDetailPanel = document.getElementById('soil-detail-panel');
var soilPointId = document.getElementById('soil-point-id');
var soilMapYear = document.getElementById('soil-map-year');
var soilTableBody = document.getElementById('soil-table-body');
var lastClickedPoint = null;

var soilLabels = [
    ["pH", 2], ["Buffer pH", 3], ["Organic Matter (%)", 4],
    ["Phosphorus (P)", 5], ["Potassium (K)", 6], ["Sulfur (S)", 7],
    ["Zinc (Zn)", 8], ["Calcium (Ca)", 9], ["Magnesium (Mg)", 10],
    ["Sodium (Na)", 11], ["CEC", 12], ["H Saturation (%)", 13],
    ["K Saturation (%)", 14], ["Ca Saturation (%)", 15],
    ["Mg Saturation (%)", 16], ["Na Saturation (%)", 17],
    ["Nitrate (NO₃)", 18],
];

// Reverse mapping: numeric index → nutrient name for data lookup
var nutrientNameByIdx = {{}};
for (var name in nutrientIndexMap) {{
    nutrientNameByIdx[nutrientIndexMap[name]] = name;
}}

function classifyTrend(vals) {{
    var nums = [];
    for (var i = 0; i < vals.length; i++) {{
        var n = parseFloat(vals[i]);
        if (isNaN(n)) return null;
        nums.push(n);
    }}
    var hasIncrease = false;
    var hasDecrease = false;
    for (var i = 1; i < nums.length; i++) {{
        if (nums[i] > nums[i - 1]) hasIncrease = true;
        if (nums[i] < nums[i - 1]) hasDecrease = true;
    }}
    if (hasIncrease && !hasDecrease) return 'up';
    if (hasDecrease && !hasIncrease) return 'down';
    return 'mixed';
}}

function trendCellHtml(trend) {{
    if (trend === 'up') {{
        return '<td class="change-up"><span class="change-cell"><span class="arrow">▲</span></span></td>';
    }}
    if (trend === 'down') {{
        return '<td class="change-down"><span class="change-cell"><span class="arrow">▼</span></span></td>';
    }}
    return '<td>\u2014</td>';
}}

function rebuildSoilTable(num, isAverage) {{
    if (!soilTableBody) return;

    // Update panel title
    if (soilPointId) {{
        soilPointId.textContent = isAverage ? 'Field Average' : 'Point ' + num;
    }}
    var prefixEl = document.getElementById('map-year-prefix');
    if (prefixEl) {{
        prefixEl.textContent = activeYears.length > 1 ? 'Map Years' : 'Map Year';
    }}
    if (soilMapYear) {{
        if (activeYears.length === 0) {{
            soilMapYear.textContent = '—';
        }} else {{
            soilMapYear.textContent = activeYears.join('+');
        }}
    }}

    // Build comparison table header from activeYears (only selected years)
    var headerRow = document.getElementById('soil-table-header');
    var showChange = activeYears.length === 2;
    var showTrend = activeYears.length >= 3;
    if (headerRow) {{
        var headerHtml = '<th>Property</th>';
        for (var y = 0; y < activeYears.length; y++) {{
            headerHtml += '<th>' + activeYears[y] + '</th>';
        }}
        if (showTrend) {{
            headerHtml += '<th>Trend</th>';
        }}
        if (showChange) {{
            headerHtml += '<th>Change</th>';
        }}
        headerRow.innerHTML = headerHtml;
    }}

    // Build comparison table rows
    var highlightIdx = nutrientIndexMap[selectedNutrient];
    var rows = [];
    var orderedLabels = [];

    // Put selected nutrient first, then rest in fixed order
    for (var i = 0; i < soilLabels.length; i++) {{
        if (soilLabels[i][1] === highlightIdx) {{
            orderedLabels.unshift(soilLabels[i]);
        }} else {{
            orderedLabels.push(soilLabels[i]);
        }}
    }}

    // Determine data source: individual point vs field-wide averages
    var dataSource = isAverage ? null : (num ? allSoilData[num] : null);
    var allAvgs = isAverage ? yearAllAverages : null;

    for (var i = 0; i < orderedLabels.length; i++) {{
        var label = orderedLabels[i][0];
        var idx = orderedLabels[i][1];
        var nutrientName = nutrientNameByIdx[idx];
        var cls = (idx === highlightIdx) ? 'highlight' : '';
        var rowHtml = '<tr class="' + cls + '"><td>' + label + '</td>';
        var yearVals = [];
        for (var y = 0; y < activeYears.length; y++) {{
            var year = activeYears[y];
            var val = '—';
            if (isAverage && allAvgs && allAvgs[String(year)]) {{
                val = allAvgs[String(year)][nutrientName];
            }} else if (dataSource && dataSource[year] && nutrientName) {{
                val = dataSource[year][nutrientName];
            }}
            yearVals.push(val);
        }}
        var rowTrend = showTrend ? classifyTrend(yearVals) : null;
        for (var y = 0; y < activeYears.length; y++) {{
            var colCls = (activeYears[y] === mapYear) ? 'map-year-col' : '';
            rowHtml += '<td class="' + colCls + '">' + (yearVals[y] || '—') + '</td>';
        }}
        if (showTrend) {{
            rowHtml += trendCellHtml(rowTrend);
        }}
        // Add change column when exactly 2 years are shown
        if (showChange && yearVals.length === 2) {{
            var olderVal = parseFloat(yearVals[0]);
            var newerVal = parseFloat(yearVals[1]);
            if (!isNaN(olderVal) && !isNaN(newerVal)) {{
                var diff = newerVal - olderVal;
                var diffStr = diff.toFixed(2);
                if (diff > 0) {{
                    rowHtml += '<td class="change-up"><span class="change-cell"><span class="arrow">▲</span><span class="value">+' + diffStr + '</span></span></td>';
                }} else if (diff < 0) {{
                    rowHtml += '<td class="change-down"><span class="change-cell"><span class="arrow">▼</span><span class="value">' + diffStr + '</span></span></td>';
                }} else {{
                    rowHtml += '<td>—</td>';
                }}
            }} else {{
                rowHtml += '<td>—</td>';
            }}
        }}
        rowHtml += '</tr>';
        rows.push(rowHtml);
    }}
    soilTableBody.innerHTML = rows.join('');
}}

document.getElementById('map-container').on('plotly_click', function(data) {{
    if (!data.points || data.points.length === 0) return;
    var pt = data.points[0];
    if (!pt.customdata) return;
    // customdata layout: [lon, lat, ...17 soil values..., point_num]  (index 19)
    var num = (pt.customdata && pt.customdata[19] != null) ? pt.customdata[19] : (pt.text || '');

    // Toggle: clicking the same point deselects
    if (lastClickedPoint === num) {{
        lastClickedPoint = null;
        rebuildSoilTable(null, true);
        return;
    }}

    lastClickedPoint = num;

    // Show panel
    if (soilDetailPanel) soilDetailPanel.classList.add('visible');

    rebuildSoilTable(num, false);
}});

var topCardsPanel = document.getElementById('top-cards-panel');
var bottomCardsPanel = document.getElementById('bottom-cards-panel');

var reportCardColors = {{
    "A": "#2ca02c", "B": "#bcbd22", "C": "#ff7f0e", "D": "#d62728", "F": "#8b0000",
}};

function computeSoilGrade(year) {{
    var ratings = yearAverageRatings[String(year)];
    if (!ratings) return {{letter: '—', points: 0, blurb: 'No data', color: '#999', extreme: 0}};
    var nonOptimal = ratings.filter(function(r) {{ return r.nutrient !== 'pH' && r.category !== 'Optimal'; }});
    var count = nonOptimal.length;
    var letter, points;
    if (count <= 1) {{ letter = 'A'; points = 4; }}
    else if (count <= 3) {{ letter = 'B'; points = 3; }}
    else if (count <= 5) {{ letter = 'C'; points = 2; }}
    else if (count <= 7) {{ letter = 'D'; points = 1; }}
    else {{ letter = 'F'; points = 0; }}
    nonOptimal.sort(function(a, b) {{ return (b.score || 0) - (a.score || 0); }});
    var extremes = nonOptimal.slice(0, 2).map(function(r) {{
        return r.nutrient + ' (' + r.category.toLowerCase() + ')';
    }}).join(' and ');
    var extremeText = extremes ? ', including ' + extremes : '';
    var blurb = count + ' of 11 nutrients are outside optimal range' + extremeText + '.';
    return {{letter: letter, points: points, blurb: blurb, color: reportCardColors[letter], extreme: count}};
}}

function computePhGrade(year) {{
    var avgs = yearAllAverages[String(year)];
    if (!avgs || !avgs['pH']) return {{letter: '—', points: 0, blurb: 'No data', color: '#999', extreme: 0}};
    var ph = parseFloat(avgs['pH']);
    var diff, absDiff, letter, points;
    if (ph >= 6.0 && ph <= 6.8) {{ letter = 'A'; points = 4; diff = 0; }}
    else if (ph >= 5.7 && ph < 6.0) {{ letter = 'B'; points = 3; diff = 6.0 - ph; }}
    else if (ph > 6.8 && ph <= 7.1) {{ letter = 'B'; points = 3; diff = ph - 6.8; }}
    else if (ph >= 5.4 && ph < 5.7) {{ letter = 'C'; points = 2; diff = 6.0 - ph; }}
    else if (ph > 7.1 && ph <= 7.4) {{ letter = 'C'; points = 2; diff = ph - 6.8; }}
    else if (ph >= 5.0 && ph < 5.4) {{ letter = 'D'; points = 1; diff = 6.0 - ph; }}
    else if (ph > 7.4 && ph <= 7.8) {{ letter = 'D'; points = 1; diff = ph - 6.8; }}
    else {{ letter = 'F'; points = 0; diff = Math.abs(ph - 6.4); }}
    absDiff = Math.round(diff * 10) / 10;
    var blurb;
    if (letter === 'A') {{
        blurb = 'Current pH is ' + ph + ', within the optimal 6.0–6.8 range for corn.';
    }} else {{
        var dir = ph < 6.0 ? 'below' : 'above';
        blurb = 'Current pH is ' + ph + ', ' + absDiff + ' points ' + dir + ' the optimal 6.0–6.8 range for corn.';
    }}
    return {{letter: letter, points: points, blurb: blurb, color: reportCardColors[letter], extreme: absDiff}};
}}

function computeWeatherGrade(year) {{
    var ec = yearEventCounts[year];
    if (!ec) return {{letter: '—', points: 0, blurb: 'No data', color: '#999', extreme: 0}};
    var total = ec.total;
    var letter, points;
    if (total <= 3) {{ letter = 'A'; points = 4; }}
    else if (total <= 7) {{ letter = 'B'; points = 3; }}
    else if (total <= 12) {{ letter = 'C'; points = 2; }}
    else if (total <= 18) {{ letter = 'D'; points = 1; }}
    else {{ letter = 'F'; points = 0; }}
    var dom = ec.dominant_type;
    var domLabel = {{heat: 'heat stress', rain: 'heavy rain', wind: 'high wind', none: ''}}[dom] || '';
    var blurb = total + ' critical weather events this year' + (domLabel ? ', mostly ' + domLabel : '') + '.';
    return {{letter: letter, points: points, blurb: blurb, color: reportCardColors[letter], extreme: total}};
}}

function computeVigorGrade(year) {{
    var stats = yearNdviStats[year];
    if (!stats) return {{letter: '—', points: 0, blurb: 'No data', color: '#999', extreme: 0}};
    var diff = stats.diff_pct;
    var letter, points;
    if (diff >= 0) {{ letter = 'A'; points = 4; }}
    else if (diff >= -5) {{ letter = 'B'; points = 3; }}
    else if (diff >= -10) {{ letter = 'C'; points = 2; }}
    else if (diff >= -15) {{ letter = 'D'; points = 1; }}
    else {{ letter = 'F'; points = 0; }}
    var absDiff = Math.abs(diff).toFixed(1);
    var dir = diff >= 0 ? 'above' : 'below';
    var blurb = 'Current NDVI is ' + absDiff + '% ' + dir + ' the ' + stats.year_count + '-year average for this time of year.';
    return {{letter: letter, points: points, blurb: blurb, color: reportCardColors[letter], extreme: Math.abs(diff)}};
}}

function computeOverallGrade(categories) {{
    var sum = 0;
    var worst = null;
    var worstPoints = 5;
    for (var i = 0; i < categories.length; i++) {{
        sum += categories[i].points;
        if (categories[i].points < worstPoints) {{ worstPoints = categories[i].points; worst = categories[i]; }}
    }}
    var avg = sum / categories.length;
    var letter;
    if (avg >= 3.5) {{ letter = 'A'; }}
    else if (avg >= 2.5) {{ letter = 'B'; }}
    else if (avg >= 1.5) {{ letter = 'C'; }}
    else if (avg >= 0.5) {{ letter = 'D'; }}
    else {{ letter = 'F'; }}
    var worstName = worst ? worst.name : '';
    var blurb = 'Overall grade driven primarily by ' + worstName + ' this season.';
    return {{letter: letter, blurb: blurb, color: reportCardColors[letter]}};
}}

function makeReportCard(name, letter, blurb, color) {{
    return '<div class="report-card" style="border-left-color:' + color + '">' +
        '<div class="report-card-name">' + name + '</div>' +
        '<div class="report-card-grade" style="color:' + color + '">' + letter + '</div>' +
        '<div class="report-card-blurb">' + blurb + '</div></div>';
}}

function makeOverallCard(letter, blurb, color) {{
    return '<div class="report-card overall" style="border-left-color:' + color + '">' +
        '<div class="report-card-name">Overall</div>' +
        '<div class="report-card-grade" style="color:' + color + '">' + letter + '</div>' +
        '<div class="report-card-blurb">' + blurb + '</div></div>';
}}

function buildReportCard(mapYear) {{
    var grid = document.getElementById('report-card-grid');
    if (!grid) return;
    var soil = computeSoilGrade(mapYear);
    soil.name = 'Soil Fertility';
    var ph = computePhGrade(mapYear);
    ph.name = 'pH Balance';
    var weather = computeWeatherGrade(mapYear);
    weather.name = 'Weather Stress';
    var vigor = computeVigorGrade(mapYear);
    vigor.name = 'Crop Vigor';
    var categories = [soil, ph, weather, vigor];
    var overall = computeOverallGrade(categories);
    var cardsHtml = '';
    cardsHtml += makeOverallCard(overall.letter, overall.blurb, overall.color);
    cardsHtml += makeReportCard(ph.name, ph.letter, ph.blurb, ph.color);
    cardsHtml += makeReportCard(soil.name, soil.letter, soil.blurb, soil.color);
    cardsHtml += makeReportCard(vigor.name, vigor.letter, vigor.blurb, vigor.color);
    cardsHtml += makeReportCard(weather.name, weather.letter, weather.blurb, weather.color);
    grid.innerHTML = cardsHtml;
}}

// Show default comparison table (field-wide averages) and cards on page load
if (soilDetailPanel) soilDetailPanel.classList.add('visible');
lastClickedPoint = null;
updateCardsVisibility();
rebuildSoilTable(null, true);

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

var anomalyLayout = {anomaly_layout_json};
var anomalyData = {anomaly_data_json};
Plotly.newPlot('anomaly-chart', anomalyData, anomalyLayout, {{responsive: true}});

var solarLayout = {solar_layout_json};
var solarData = {solar_data_json};
Plotly.newPlot('solar-chart', solarData, solarLayout, {{responsive: true}});


</script>
</body>
</html>
"""
