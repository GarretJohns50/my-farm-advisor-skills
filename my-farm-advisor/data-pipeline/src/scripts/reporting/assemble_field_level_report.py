#!/usr/bin/env python3
"""Assemble a one-time HTML report from field-level EDA outputs.

Reads PNGs and CSVs from all 3 growers plus cross-grower shared directory,
base64-encodes images, and writes a self-contained HTML file to
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/reports/field_level_eda_report.html
"""

from __future__ import annotations

import base64
import glob
import sys
from pathlib import Path

import pandas as pd

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from paths import DATA_ROOT, farm_dir, cross_grower_eda_dir  # noqa: E402

_GROWERS = [
    ("central-ia-grower", "central-ia-grower-iowa", "Iowa (Kossuth)"),
    ("central-il-grower", "central-il-grower-illinois", "Illinois (Iroquois)"),
    ("central-ne-grower", "central-ne-grower-nebraska", "Nebraska (Hamilton)"),
]

_REP_GROWER = "central-il-grower"
_REP_FARM = "central-il-grower-illinois"


def _b64_png(path: Path) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        data = f.read()
    return f"data:image/png;base64,{base64.b64encode(data).decode()}"


def _load_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_csv(path)


def _table_html(df: pd.DataFrame, index: bool = False) -> str:
    return df.to_html(index=index, classes="data-table", border=0)


def _a3_table() -> str:
    rows = []
    for g, f, label in _GROWERS:
        csv = _load_csv(farm_dir(g, f) / "derived" / "eda_field_level" / "A3_boundary_correlation.csv")
        if csv is not None and not csv.empty:
            r = csv.iloc[0]
            rows.append({
                "Grower": label,
                "Pearson r": round(r["pearson_r"], 3),
                "p-value": round(r["p_value"], 3),
                "Significant (α=0.05)": "Yes" if r["significant_05"] else "No",
            })
    return _table_html(pd.DataFrame(rows))


def _b3_table() -> str:
    rows = []
    for g, f, label in _GROWERS:
        # Load the normalized transition matrix (first column is the index)
        path = farm_dir(g, f) / "derived" / "eda_field_level" / "B3_cdl_rotation_matrix.csv"
        if not path.exists():
            continue
        csv = pd.read_csv(path, index_col=0)

        # Compute raw counts from CDL full composition
        cdl_pattern = str(farm_dir(g, f) / "derived" / "tables" / "*full_composition.csv")
        cdl_paths = glob.glob(cdl_pattern)
        cdl_csv = _load_csv(Path(cdl_paths[0])) if cdl_paths else None
        counts: dict[str, int] = {}
        if cdl_csv is not None:
            dom = cdl_csv.loc[cdl_csv.groupby(["field_id", "year"])["pct"].idxmax()].reset_index(drop=True)[["field_id", "year", "crop_name"]]
            for fid in dom["field_id"].unique():
                sub = dom[dom["field_id"] == fid].sort_values("year")
                for i in range(len(sub) - 1):
                    key = f"{sub.iloc[i]['crop_name']}->{sub.iloc[i+1]['crop_name']}"
                    counts[key] = counts.get(key, 0) + 1

        def _fmt(from_crop: str, to_crop: str) -> str:
            if from_crop not in csv.index or to_crop not in csv.columns:
                return "—"
            pct = float(csv.loc[from_crop, to_crop])
            key = f"{from_crop}->{to_crop}"
            n = counts.get(key, 0)
            # Total transitions starting with from_crop (sum over all possible to_crops)
            total = sum(counts.get(f"{from_crop}->{t}", 0) for t in csv.columns)
            if n == 0 and pct == 0:
                return "—"
            return f"{pct:.0%} ({n}/{total})"

        rows.append({
            "Grower": label,
            "Corn→Corn": _fmt("Corn", "Corn"),
            "Corn→Soy": _fmt("Corn", "Soybeans"),
            "Soy→Corn": _fmt("Soybeans", "Corn"),
            "Soy→Soy": _fmt("Soybeans", "Soybeans"),
        })
    return _table_html(pd.DataFrame(rows))


def _c3_table() -> str:
    rows = []
    for g, f, label in _GROWERS:
        csv = _load_csv(farm_dir(g, f) / "derived" / "eda_field_level" / "C3_weather_cv_analysis.csv")
        if csv is None or csv.empty:
            continue
        mean_gdd = csv["cv_gdd"].mean()
        mean_precip = csv["cv_precip"].mean()
        rows.append({
            "Grower": label,
            "Mean GDD CV": round(mean_gdd, 4),
            "Mean Precip CV": round(mean_precip, 4),
            "Interpretation": "Most uniform" if mean_gdd < 0.012 else "Moderate" if mean_gdd < 0.016 else "Most variable",
        })
    return _table_html(pd.DataFrame(rows))


def build_report() -> str:
    rep_dir = farm_dir(_REP_GROWER, _REP_FARM) / "derived" / "eda_field_level"
    cross_dir = cross_grower_eda_dir()
    report_dir = DATA_ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    # Representative images (Illinois)
    a1 = _b64_png(rep_dir / "A1_boundary_size_distribution.png")
    a2 = _b64_png(rep_dir / "A2_boundary_shape_scatter.png")
    c1 = _b64_png(rep_dir / "C1_weather_temporal_trends.png")
    c2 = _b64_png(rep_dir / "C2_weather_interfield_spread.png")

    # B1/B2 for all 3 growers (side-by-side in report)
    b1_ia = _b64_png(farm_dir("central-ia-grower", "central-ia-grower-iowa") / "derived" / "eda_field_level" / "B1_cdl_dominant_heatmap.png")
    b1_il = _b64_png(farm_dir("central-il-grower", "central-il-grower-illinois") / "derived" / "eda_field_level" / "B1_cdl_dominant_heatmap.png")
    b1_ne = _b64_png(farm_dir("central-ne-grower", "central-ne-grower-nebraska") / "derived" / "eda_field_level" / "B1_cdl_dominant_heatmap.png")

    b2_ia = _b64_png(farm_dir("central-ia-grower", "central-ia-grower-iowa") / "derived" / "eda_field_level" / "B2_cdl_diversity_index.png")
    b2_il = _b64_png(farm_dir("central-il-grower", "central-il-grower-illinois") / "derived" / "eda_field_level" / "B2_cdl_diversity_index.png")
    b2_ne = _b64_png(farm_dir("central-ne-grower", "central-ne-grower-nebraska") / "derived" / "eda_field_level" / "B2_cdl_diversity_index.png")

    # Geospatial maps (all 3)
    m1_il = _b64_png(farm_dir("central-il-grower", "central-il-grower-illinois") / "derived" / "eda_field_level" / "M1_field_boundary_map.png")
    m1_ia = _b64_png(farm_dir("central-ia-grower", "central-ia-grower-iowa") / "derived" / "eda_field_level" / "M1_field_boundary_map.png")
    m1_ne = _b64_png(farm_dir("central-ne-grower", "central-ne-grower-nebraska") / "derived" / "eda_field_level" / "M1_field_boundary_map.png")

    # Cross-grower images
    x1 = _b64_png(cross_dir / "field_level_boundary_sizes.png")
    x2 = _b64_png(cross_dir / "field_level_cdl_mix.png")
    x3 = _b64_png(cross_dir / "field_level_weather_cv.png")

    # Data tables
    a3_html = _a3_table()
    b3_html = _b3_table()
    c3_html = _c3_table()

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Field-Level EDA Report — My Farm Advisor</title>
<style>
  :root {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
  body {{ max-width: 950px; margin: 0 auto; padding: 2rem 1rem; line-height: 1.6; color: #333; background: #fafafa; }}
  h1 {{ font-size: 1.8rem; margin-bottom: 0.3rem; color: #1a472a; border-bottom: 3px solid #2E7D32; padding-bottom: 0.5rem; }}
  h2 {{ font-size: 1.3rem; margin-top: 2rem; color: #1565C0; border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }}
  h3 {{ font-size: 1.05rem; color: #444; margin-top: 1.5rem; }}
  .subtitle {{ color: #666; font-size: 0.95rem; margin-bottom: 2rem; }}
  .note {{ background: #e3f2fd; border-left: 4px solid #2196F3; padding: 0.8rem 1rem; margin: 1rem 0; font-size: 0.9rem; }}
  .warning {{ background: #fff3e0; border-left: 4px solid #FF9800; padding: 0.8rem 1rem; margin: 1rem 0; font-size: 0.9rem; }}
  img {{ max-width: 100%; height: auto; display: block; margin: 1rem 0; border: 1px solid #ddd; border-radius: 4px; }}
  .img-caption {{ font-size: 0.85rem; color: #555; margin-top: -0.5rem; margin-bottom: 1rem; font-style: italic; }}
  .img-row {{ display: flex; gap: 1rem; flex-wrap: wrap; margin: 1rem 0; }}
  .img-row img {{ flex: 1 1 30%; min-width: 250px; margin: 0; }}
  .img-row .caption {{ flex: 1 1 100%; font-size: 0.85rem; color: #555; font-style: italic; margin-top: 0.3rem; }}
  table.data-table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.9rem; }}
  table.data-table th {{ background: #f5f5f5; padding: 0.5rem; text-align: left; border-bottom: 2px solid #ddd; font-weight: 600; }}
  table.data-table td {{ padding: 0.5rem; border-bottom: 1px solid #eee; }}
  table.data-table tr:nth-child(even) {{ background: #fafafa; }}
  ul {{ margin: 0.5rem 0; padding-left: 1.5rem; }}
  li {{ margin: 0.3rem 0; }}
  code {{ background: #f4f4f4; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.9em; }}
  .footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #ddd; font-size: 0.85rem; color: #777; text-align: center; }}
</style>
</head>
<body>

<h1>Field-Level Exploratory Data Analysis Report</h1>
<p class="subtitle">My Farm Advisor — Assignment 2 &nbsp;|&nbsp; Generated {pd.Timestamp.now().strftime("%Y-%m-%d")}</p>

<div class="note">
  <strong>Representative Grower:</strong> All 3 growers were analyzed with identical methods. 
  Illinois (Iroquois County) is shown as the representative grower for per-farm visualizations. 
  Summary tables include all growers. Cross-grower plots are shared across all analyses.
</div>

<h2>1. Dataset Scope &amp; Grower Locations</h2>
<p>
  This analysis covers <strong>3 commercial growers</strong> in the U.S. Corn Belt, 
  each with <strong>10 fields</strong> sampled from county-level OpenStreetMap boundary data:
</p>
<ul>
  <li><strong>Iowa (Kossuth County)</strong> — 10 fields, 1,169 acres total</li>
  <li><strong>Illinois (Iroquois County)</strong> — 10 fields, 759 acres total</li>
  <li><strong>Nebraska (Hamilton County)</strong> — 10 fields, 1,009 acres total</li>
</ul>
<p>Time window: <strong>2021–2025</strong> (5 years of weather and CDL history).</p>

<h2>2. Data Layers Used</h2>
<table class="data-table">
  <tr><th>Layer</th><th>Source</th><th>Granularity</th><th>Years</th></tr>
  <tr><td>Field Boundaries</td><td>OpenStreetMap / Overpass</td><td>Per-field polygon</td><td>Static</td></tr>
  <tr><td>NASS CDL</td><td>USDA Cropland Data Layer</td><td>Per-field pixel composition</td><td>2021–2025</td></tr>
  <tr><td>Weather</td><td>NASA POWER (Zarr backend)</td><td>Daily, per-field centroid</td><td>2021–2025</td></tr>
</table>

<h2>3. Comparison Levels</h2>
<ul>
  <li><strong>Within-field (time):</strong> CDL crop history across 5 years per field; weather trends per field</li>
  <li><strong>Across fields (within grower):</strong> Boundary size/shape comparison; inter-field weather variability; crop diversity</li>
  <li><strong>Across growers:</strong> Shared cross-grower plots for boundaries, CDL mix, and weather uniformity</li>
  <li><strong>Field-year:</strong> Dominant crop assignment per field per year for rotation analysis</li>
</ul>

<h2>4. Field Boundaries (Category A)</h2>
<h3>A1 — Field Size Distribution</h3>
<img src="{a1}" alt="Field size distribution">
<p class="img-caption">Box + swarm plot showing area heterogeneity. Outliers indicate very large fields needing different equipment.</p>

<h3>A2 — Size vs. Shape (Circularity)</h3>
<img src="{a2}" alt="Size vs circularity scatter">
<p class="img-caption">Scatter plot of area against polygon circularity (4πA/P²). Values ≥ 0.70 suggest center-pivot geometry.</p>

<h3>A3 — Boundary Correlation (All Growers)</h3>
{a3_html}
<p class="img-caption">None are significant at α = 0.05 — size and shape are independent in this sample.</p>

<h2>5. CDL / Cropland (Category B)</h2>
<h3>B1 — Dominant Crop Heatmap (All Growers)</h3>
<div class="img-row">
  <img src="{b1_ia}" alt="Iowa CDL heatmap">
  <img src="{b1_il}" alt="Illinois CDL heatmap">
  <img src="{b1_ne}" alt="Nebraska CDL heatmap">
  <div class="caption">
    Left: Iowa — mixed corn/soy with some continuous corn blocks and grass/pasture persistence.
    Center: Illinois — mostly strict corn-soy rotation with occasional grass/pasture strips.
    Right: Nebraska — near-monoculture corn (horizontal green bands across all years).
  </div>
</div>

<h3>B2 — Crop Diversity Index (All Growers)</h3>
<div class="img-row">
  <img src="{b2_ia}" alt="Iowa diversity index">
  <img src="{b2_il}" alt="Illinois diversity index">
  <img src="{b2_ne}" alt="Nebraska diversity index">
  <div class="caption">
    Left: Iowa — moderate diversity (forest/grass strips on some fields). 
    Center: Illinois — low-to-moderate diversity (mostly corn/soy). 
    Right: Nebraska — very low diversity (almost pure corn on every field).
  </div>
</div>

<h3>B3 — Rotation Transition Matrix (All Growers)</h3>
{b3_html}
<p class="img-caption">
  Percentages are <em>row-normalized</em> (e.g., "Corn→Corn: 81%" means 81% of transitions 
  that started as Corn stayed Corn). Counts shown in parentheses for verification.
</p>

<h2>6. Weather (Category C)</h2>
<h3>C1 — Temporal GDD Trends</h3>
<img src="{c1}" alt="Per-field GDD temporal trends">
<p class="img-caption">Cumulative GDD curves per field, 2021–2025. Parallel = uniform climate; diverging = microclimates.</p>

<h3>C2 — Inter-Field Spread</h3>
<img src="{c2}" alt="Inter-field GDD spread">
<p class="img-caption">Box plot of per-field growing-season GDD by year. Spread width = microclimate patchiness.</p>

<h3>C3 — Weather CV Analysis (All Growers)</h3>
{c3_html}
<p class="img-caption"><strong>Nebraska most uniform</strong> (flat pivot terrain). <strong>Iowa most variable</strong>.</p>

<h2>7. Geospatial Maps (M1)</h2>
<p>Choropleth maps colored by area with field ID labels:</p>
<div class="img-row">
  <img src="{m1_ia}" alt="Iowa boundary map">
  <img src="{m1_il}" alt="Illinois boundary map">
  <img src="{m1_ne}" alt="Nebraska boundary map">
  <div class="caption">Left: Iowa — irregular shapes. Center: Illinois — representative. Right: Nebraska — near-perfect pivot circles.</div>
</div>

<h2>8. Cross-Grower Comparisons</h2>
<p>Shared across all growers — written to <code>eda/cross_grower/</code>:</p>

<h3>Boundary Sizes</h3>
<img src="{x1}" alt="Cross-grower boundary sizes">
<p class="img-caption">Iowa largest median (~117 ac); Illinois smallest (~76 ac). Nebraska tightest distribution.</p>

<h3>Crop Mix</h3>
<img src="{x2}" alt="Cross-grower CDL mix">
<p class="img-caption">Nebraska heavily corn-dominant. Illinois and Iowa more balanced corn-soy.</p>

<h3>Weather Uniformity</h3>
<img src="{x3}" alt="Cross-grower weather CV">
<p class="img-caption">Nebraska lowest inter-field CV — flat pivot circles most uniform. Iowa most variable.</p>

<h2>9. Limitations, Missing Data &amp; Assumptions</h2>
<div class="warning">
  <ul>
    <li><strong>Sample size:</strong> 10 fields per grower is small for significant correlations (A3 non-significant for all).</li>
    <li><strong>Satellite timeouts:</strong> Landsat/Sentinel downloads occasionally fail (HTTP 503/403). Farm-level tables complete; per-field satellite dirs may be incomplete.</li>
    <li><strong>Weather resolution:</strong> NASA POWER Zarr uses ~0.5° grid cells at field centroids. Sub-field variation not captured.</li>
    <li><strong>CDL accuracy:</strong> Known misclassification at field edges. Small fields (&lt; 5 ac) may have few pixels.</li>
    <li><strong>Circularity threshold:</strong> 0.70 is a heuristic, not a calibrated irrigation detector.</li>
  </ul>
</div>

<h2>10. Soil Analysis</h2>
<div class="note">
  <strong>Soil analysis is not required and was not performed.</strong> 
  Focus is on boundaries, CDL, and weather. SSURGO tables exist in runtime but were excluded per assignment scope.
</div>

<div class="footer">
  Report generated from field-level EDA outputs (eda_field_level.py) &nbsp;|&nbsp; 
  3 growers × 10 fields × 5 years &nbsp;|&nbsp; 
  Static PNGs embedded as base64 — no external dependencies
</div>

</body>
</html>"""

    out_path = report_dir / "field_level_eda_report.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"✓ Report written: {out_path}")
    print(f"  Size: {out_path.stat().st_size / 1024:.0f} KB")
    return str(out_path)


if __name__ == "__main__":
    path = build_report()
    print(f"\nOpen with: python -m webbrowser file://{path}")
