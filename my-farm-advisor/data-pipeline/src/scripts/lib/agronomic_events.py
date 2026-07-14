"""Reusable critical agronomic event detection engine.

Detects heavy rain, heat waves, cool periods, NDVI dips/surges, low solar,
and high wind events for any crop, any year, with configurable thresholds.
"""

from __future__ import annotations

import json
from pathlib import Path


# Crop growth stage GDD thresholds (base 10°C)
CROP_GROWTH_STAGES: dict[str, list[dict]] = {
    "corn": [
        {"stage": "VE", "name": "Emergence", "gdd": 125, "color": "#2ca02c"},
        {"stage": "V6", "name": "6-Leaf", "gdd": 575, "color": "#2ca02c"},
        {"stage": "VT", "name": "Tasseling", "gdd": 1150, "color": "#ff7f0e"},
        {"stage": "R1", "name": "Silking", "gdd": 1250, "color": "#d62728"},
        {"stage": "R3", "name": "Milk", "gdd": 1925, "color": "#9467bd"},
        {"stage": "R6", "name": "Physiological Maturity", "gdd": 2700, "color": "#7f7f7f"},
    ],
    "soybean": [
        {"stage": "VE", "name": "Emergence", "gdd": 100, "color": "#2ca02c"},
        {"stage": "V3", "name": "3-Leaf", "gdd": 250, "color": "#2ca02c"},
        {"stage": "R1", "name": "Beginning Bloom", "gdd": 800, "color": "#d62728"},
        {"stage": "R3", "name": "Beginning Pod", "gdd": 1200, "color": "#9467bd"},
        {"stage": "R6", "name": "Full Seed", "gdd": 2000, "color": "#7f7f7f"},
    ],
}


DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "heavy_rain": {"daily_min_in": 1.0, "mild": 1.5, "moderate": 2.5},
    "heat_wave": {"consecutive_days": 3, "temp_f": 86.0, "mild": 5, "moderate": 7},
    "cool_period": {"consecutive_days": 3, "deficit_f": 10.0, "mild": 15, "moderate": 20},
    "ndvi_dip": {"delta": 0.10, "mild": 0.15, "moderate": 0.25},
    "ndvi_surge": {"delta": 0.15, "mild": 0.20, "moderate": 0.30},
    "low_solar": {"max_mj": 18.0, "mild": 3, "moderate": 6},
    "high_wind": {"min_mph": 30.0, "mild": 35, "moderate": 40},
}


def _severity(value: float, mild: float, moderate: float) -> str:
    """Return severity string from value and thresholds."""
    if value >= moderate:
        return "severe"
    if value >= mild:
        return "moderate"
    return "mild"


def detect_critical_events(
    field_weather: list[dict],
    ndvi_series: list[dict],
    target_year: int,
    stage_medians: list[dict],
    baseline_path: Path | None = None,
    thresholds: dict[str, dict[str, float]] | None = None,
    crop: str = "corn",
) -> list[dict]:
    """Detect critical agronomic events for a specific year.

    Parameters
    ----------
    field_weather : list[dict]
        List of weather records by year from weather_transforms.
    ndvi_series : list[dict]
        List of NDVI records from compute_field_ndvi_series.
    target_year : int
        Year to analyze (e.g., 2025).
    stage_medians : list[dict]
        Growth stage median DOYs from _compute_stage_median_doys.
    baseline_path : Path | None
        Path to DOY temperature baseline JSON (for cool periods).
    thresholds : dict | None
        Override default thresholds. Partial overrides allowed.
    crop : str
        Crop type ("corn", "soybean", etc.).

    Returns
    -------
    list[dict]
        Event records with type, severity, chart_target, description, etc.
    """
    events: list[dict] = []

    # Merge user thresholds with defaults
    thresh = {}
    for key, defaults in DEFAULT_THRESHOLDS.items():
        if thresholds and key in thresholds:
            merged = {**defaults, **thresholds[key]}
            thresh[key] = merged
        else:
            thresh[key] = dict(defaults)

    # Locate target year data
    weather_year = next((r for r in field_weather if r["year"] == target_year), None)
    if weather_year is None:
        return events

    daily = weather_year.get("daily", [])
    if not daily:
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
        daily_all = r.get("daily", [])
        monthly_totals[yr] = {}
        for month in range(3, 11):
            month_days = [d for d in daily_all if int(d["date"].split("-")[1]) == month]
            monthly_totals[yr][month] = sum(d.get("dailyRainfallIn", 0.0) for d in month_days)
    month_avg: dict[int, float] = {}
    for month in range(3, 11):
        totals = [monthly_totals[yr][month] for yr in monthly_totals]
        month_avg[month] = sum(totals) / len(totals) if totals else 0.0

    # R-stage DOY window
    r_stages = [s for s in stage_medians if s["stage"].startswith("R")]
    r1_doy = next((s["median_doy"] for s in r_stages if s["stage"] == "R1"), None)
    r6_doy = next((s["median_doy"] for s in r_stages if s["stage"] == "R6"), None)

    # 1. Heavy rain events
    t = thresh["heavy_rain"]
    for d in daily:
        rain = d.get("dailyRainfallIn", 0.0)
        if rain > t["daily_min_in"]:
            month = int(d["date"].split("-")[1])
            avg = month_avg.get(month, 0.0)
            ratio = round(rain / avg, 1) if avg > 0 else 0.0
            sev = _severity(rain, t["mild"], t["moderate"])
            events.append({
                "type": "heavy_rain",
                "display_name": "Heavy Rain",
                "year": target_year,
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

    # 2. Heat wave events
    t = thresh["heat_wave"]
    tmax_f = [(d["dayOfYear"], d["date"], d.get("t2m_max", 0.0) * 9.0 / 5.0 + 32.0) for d in daily]
    streak = 0
    streak_start = 0
    for i, (doy, date, temp_f) in enumerate(tmax_f):
        if temp_f > t["temp_f"]:
            if streak == 0:
                streak_start = i
            streak += 1
        else:
            if streak >= int(t["consecutive_days"]):
                start = tmax_f[streak_start]
                end = tmax_f[i - 1]
                max_temp = max(tmax_f[j][2] for j in range(streak_start, i))
                sev = _severity(streak, t["mild"], t["moderate"])
                events.append({
                    "type": "heat_wave",
                    "display_name": "Heat Wave",
                    "year": target_year,
                    "start_doy": start[0],
                    "end_doy": end[0],
                    "start_date": start[1],
                    "end_date": end[1],
                    "value": streak,
                    "unit": "days",
                    "severity": sev,
                    "chart_target": "temp",
                    "description": f"{streak} consecutive days >{int(t['temp_f'])}°F (DOY {start[0]}–{end[0]})",
                    "agronomic_note": f"Peak {max_temp:.1f}°F. Heat stress during pollination can reduce kernel set.",
                    "baseline_context": {},
                })
            streak = 0
    if streak >= int(t["consecutive_days"]):
        start = tmax_f[streak_start]
        end = tmax_f[-1]
        max_temp = max(tmax_f[j][2] for j in range(streak_start, len(tmax_f)))
        sev = _severity(streak, t["mild"], t["moderate"])
        events.append({
            "type": "heat_wave",
            "display_name": "Heat Wave",
            "year": target_year,
            "start_doy": start[0],
            "end_doy": end[0],
            "start_date": start[1],
            "end_date": end[1],
            "value": streak,
            "unit": "days",
            "severity": sev,
            "chart_target": "temp",
            "description": f"{streak} consecutive days >{int(t['temp_f'])}°F (DOY {start[0]}–{end[0]})",
            "agronomic_note": f"Peak {max_temp:.1f}°F. Heat stress during pollination can reduce kernel set.",
            "baseline_context": {},
        })

    # 3. Cool period events
    t = thresh["cool_period"]
    if baseline:
        tavg_f = [(d["dayOfYear"], d["date"], d.get("t2m_avg", 0.0) * 9.0 / 5.0 + 32.0) for d in daily]
        streak = 0
        streak_start = 0
        for i, (doy, date, temp_f) in enumerate(tavg_f):
            doy_stats = baseline.get(doy, {})
            base_t = doy_stats.get("mean_t2m", 0.0) * 9.0 / 5.0 + 32.0
            deficit = base_t - temp_f
            if deficit > t["deficit_f"]:
                if streak == 0:
                    streak_start = i
                streak += 1
            else:
                if streak >= int(t["consecutive_days"]):
                    start = tavg_f[streak_start]
                    end = tavg_f[i - 1]
                    max_deficit = max(base_t - tavg_f[j][2] for j in range(streak_start, i))
                    sev = _severity(max_deficit, t["mild"], t["moderate"])
                    events.append({
                        "type": "cool_period",
                        "display_name": "Cool Period",
                        "year": target_year,
                        "start_doy": start[0],
                        "end_doy": end[0],
                        "start_date": start[1],
                        "end_date": end[1],
                        "value": round(max_deficit, 1),
                        "unit": "°F below normal",
                        "severity": sev,
                        "chart_target": "temp",
                        "description": f"{streak} days >{int(t['deficit_f'])}°F below normal (DOY {start[0]}–{end[0]})",
                        "agronomic_note": f"Max deficit {max_deficit:.1f}°F below 5-year DOY average. Cool, cloudy weather slows GDD accumulation.",
                        "baseline_context": {},
                    })
                streak = 0
        if streak >= int(t["consecutive_days"]):
            start = tavg_f[streak_start]
            end = tavg_f[-1]
            max_deficit = max(base_t - tavg_f[j][2] for j in range(streak_start, len(tavg_f)))
            sev = _severity(max_deficit, t["mild"], t["moderate"])
            events.append({
                "type": "cool_period",
                "display_name": "Cool Period",
                "year": target_year,
                "start_doy": start[0],
                "end_doy": end[0],
                "start_date": start[1],
                "end_date": end[1],
                "value": round(max_deficit, 1),
                "unit": "°F below normal",
                "severity": sev,
                "chart_target": "temp",
                "description": f"{streak} days >{int(t['deficit_f'])}°F below normal (DOY {start[0]}–{end[0]})",
                "agronomic_note": f"Max deficit {max_deficit:.1f}°F below 5-year DOY average. Cool, cloudy weather slows GDD accumulation.",
                "baseline_context": {},
            })

    # 4. NDVI dip events
    t = thresh["ndvi_dip"]
    ndvi_year = [d for d in ndvi_series if d["year"] == target_year]
    ndvi_year.sort(key=lambda x: x["doy"])
    for i in range(1, len(ndvi_year)):
        prev = ndvi_year[i - 1]
        curr = ndvi_year[i]
        drop = prev["mean_ndvi"] - curr["mean_ndvi"]
        if drop > t["delta"]:
            sev = _severity(drop, t["mild"], t["moderate"])
            events.append({
                "type": "ndvi_dip",
                "display_name": "NDVI Dip",
                "year": target_year,
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

    # 5. NDVI surge events
    t = thresh["ndvi_surge"]
    for i in range(1, len(ndvi_year)):
        prev = ndvi_year[i - 1]
        curr = ndvi_year[i]
        rise = curr["mean_ndvi"] - prev["mean_ndvi"]
        if rise > t["delta"]:
            sev = _severity(rise, t["mild"], t["moderate"])
            events.append({
                "type": "ndvi_surge",
                "display_name": "NDVI Surge",
                "year": target_year,
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

    # 6. Low solar events during R-stages
    t = thresh["low_solar"]
    if r1_doy and r6_doy:
        for d in daily:
            doy = d["dayOfYear"]
            if r1_doy <= doy <= r6_doy:
                solar = d.get("solarRadiation", 0.0)
                if solar < t["max_mj"]:
                    sev = _severity(t["max_mj"] - solar, t["mild"], t["moderate"])
                    events.append({
                        "type": "low_solar",
                        "display_name": "Low Solar",
                        "year": target_year,
                        "start_doy": doy,
                        "end_doy": doy,
                        "start_date": d["date"],
                        "end_date": d["date"],
                        "value": round(solar, 1),
                        "unit": "MJ/m²",
                        "severity": sev,
                        "chart_target": "solar",
                        "description": f"{solar:.1f} MJ/m² on DOY {doy} during {crop} R-stages",
                        "agronomic_note": "Low solar radiation during grain-fill reduces photosynthesis and can lower yield potential.",
                        "baseline_context": {},
                    })

    # 7. High wind events during R-stages
    t = thresh["high_wind"]
    if r1_doy and r6_doy:
        for d in daily:
            doy = d["dayOfYear"]
            if r1_doy <= doy <= r6_doy:
                wind = d.get("windSpeedMph", 0.0)
                if wind > t["min_mph"]:
                    sev = _severity(wind, t["mild"], t["moderate"])
                    events.append({
                        "type": "high_wind",
                        "display_name": "High Wind",
                        "year": target_year,
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
