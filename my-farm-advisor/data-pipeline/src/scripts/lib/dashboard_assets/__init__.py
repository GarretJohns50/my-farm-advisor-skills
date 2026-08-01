"""Dashboard HTML assets: Plotly.js vendoring, CSS, and JavaScript controls.

All assets are inlined into the final HTML. No external dependencies at runtime.
"""

from __future__ import annotations

from pathlib import Path

import requests

_PLOTLY_VERSION = "2.27.0"
_PLOTLY_CDN = f"https://cdn.plot.ly/plotly-{_PLOTLY_VERSION}.min.js"

# Colorblind-safe categorical palette
FIELD_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


def _get_plotly_bundle_path(shared_dir: Path) -> Path:
    return shared_dir / "dashboard_assets" / f"plotly-{_PLOTLY_VERSION}.min.js"


def ensure_plotly_bundle(shared_dir: Path) -> str:
    """Download and cache Plotly.js, return the minified JS string."""
    bundle_path = _get_plotly_bundle_path(shared_dir)
    if bundle_path.exists():
        return bundle_path.read_text(encoding="utf-8")

    try:
        resp = requests.get(_PLOTLY_CDN, timeout=60)
        if resp.status_code == 200:
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            bundle_path.write_text(resp.text, encoding="utf-8")
            return resp.text
    except Exception:
        pass

    # Fallback: try to use any plotly installed in the venv
    try:
        import plotly
        plotly_pkg = Path(plotly.__file__).parent
        local_min = plotly_pkg / "package_data" / "plotly.min.js"
        if local_min.exists():
            return local_min.read_text(encoding="utf-8")
    except Exception:
        pass

    raise RuntimeError(
        f"Could not download Plotly {_PLOTLY_VERSION} and no local copy found. "
        "Please check network connectivity or pre-download the bundle."
    )


def _css_template() -> str:
    return """\
:root { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
body { margin: 0; padding: 0; background: #f4f5f7; color: #333; }
.dashboard-header {
    background: #fff;
    border-bottom: 1px solid #ddd;
    padding: 1rem 1.5rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 1rem;
}
.dashboard-header h1 { margin: 0; font-size: 1.3rem; color: #1a472a; }
.dashboard-header .subtitle { margin: 0; font-size: 0.85rem; color: #666; }
.controls-bar {
    background: #fff;
    border-bottom: 1px solid #ddd;
    padding: 0.75rem 1.5rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
}
.dropdown-wrapper { position: relative; display: inline-block; }
.dropdown-toggle {
    background: #fff;
    border: 1px solid #ccc;
    border-radius: 4px;
    padding: 0.4rem 0.8rem;
    cursor: pointer;
    font-size: 0.9rem;
}
.dropdown-menu {
    position: absolute;
    top: 100%;
    left: 0;
    background: #fff;
    border: 1px solid #ccc;
    border-radius: 4px;
    margin-top: 2px;
    min-width: 160px;
    z-index: 100;
    display: none;
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
}
.dropdown-menu.open { display: block; }
.dropdown-menu label {
    display: block;
    padding: 0.4rem 0.8rem;
    cursor: pointer;
    font-size: 0.9rem;
    white-space: nowrap;
}
.dropdown-menu label:hover { background: #f0f0f0; }
.dropdown-menu input { margin-right: 0.4rem; }
.btn {
    background: #1a472a;
    color: #fff;
    border: none;
    border-radius: 4px;
    padding: 0.4rem 0.9rem;
    cursor: pointer;
    font-size: 0.9rem;
}
.btn:hover { background: #2e7d32; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; padding: 1rem; }
@media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
.chart-card {
    background: #fff;
    border-radius: 6px;
    border: 1px solid #ddd;
    padding: 1rem;
}
.chart-card h3 { margin: 0 0 0.5rem; font-size: 1rem; color: #444; }
.chart-container { width: 100%; height: 350px; }
.map-section {
    background: #fff;
    border-radius: 6px;
    border: 1px solid #ddd;
    margin: 1rem;
    padding: 1rem;
}
.map-section h3 { margin: 0 0 0.5rem; font-size: 1rem; color: #444; }
#map-container { width: 100%; height: 500px; }
#map-container .scatterlayer .textpoint text {
    text-shadow: -1px -1px 0 #fff, 1px -1px 0 #fff, -1px 1px 0 #fff, 1px 1px 0 #fff;
}
.map-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    padding: 1rem;
}
@media (max-width: 900px) { .map-row { grid-template-columns: 1fr; } }
.map-row .map-section { margin: 0; }
#spread-map-container { width: 100%; height: 500px; }
#spread-map-container .scatterlayer .textpoint text {
    text-shadow: -1px -1px 0 #fff, 1px -1px 0 #fff, -1px 1px 0 #fff, 1px 1px 0 #fff;
}
.spread-map-info {
    font-size: 0.85rem;
    color: #666;
    margin-top: 0.5rem;
}
.legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.5rem;
    font-size: 0.8rem;
}
.legend-item { display: flex; align-items: center; gap: 0.25rem; }
.legend-swatch { width: 12px; height: 12px; border-radius: 2px; display: inline-block; }
"""


def _js_controls_template() -> str:
    return """\
function toggleDropdown(id) {
    document.querySelectorAll('.dropdown-menu').forEach(function(el) {
        if (el.id !== id) el.classList.remove('open');
    });
    document.getElementById(id).classList.toggle('open');
}
document.addEventListener('click', function(e) {
    if (!e.target.closest('.dropdown-wrapper')) {
        document.querySelectorAll('.dropdown-menu').forEach(function(el) {
            el.classList.remove('open');
        });
    }
});
function updateVisibility() {
    var selectedFields = [];
    document.querySelectorAll('input[data-field]:checked').forEach(function(cb) {
        selectedFields.push(cb.getAttribute('data-field'));
    });
    var selectedYears = [];
    document.querySelectorAll('input[data-year]:checked').forEach(function(cb) {
        selectedYears.push(cb.getAttribute('data-year'));
    });
    // Update charts via Plotly restyle
    var chartIds = ['gdd-chart','rainfall-chart','cumulative-gdd-chart','cumulative-rainfall-chart'];
    chartIds.forEach(function(id) {
        var gd = document.getElementById(id);
        if (!gd || !gd.data) return;
        var traces = [];
        var visible = [];
        for (var i = 0; i < gd.data.length; i++) {
            var d = gd.data[i];
            var show = selectedFields.indexOf(d.fieldId) >= 0 && selectedYears.indexOf(String(d.year)) >= 0;
            visible.push(show ? true : 'legendonly');
        }
        Plotly.restyle(gd, {visible: visible});
    });
}
function resetAll() {
    document.querySelectorAll('input[data-field]').forEach(function(cb) { cb.checked = true; });
    document.querySelectorAll('input[data-year]').forEach(function(cb) { cb.checked = true; });
    updateVisibility();
}
function syncMapToCharts() {
    var selectedFields = [];
    document.querySelectorAll('input[data-field]:checked').forEach(function(cb) {
        selectedFields.push(cb.getAttribute('data-field'));
    });
    if (window.farmMap) {
        window.farmMap.data.forEach(function(trace) {
            if (trace.type === 'scatter' && trace.mode === 'lines') {
                var show = selectedFields.indexOf(trace.fieldId) >= 0;
                Plotly.restyle(window.farmMap, {visible: show ? true : 'legendonly'}, [trace.index]);
            }
        });
    }
}
"""


def build_html_body(
    plotly_bundle: str,
    title: str,
    subtitle: str,
    field_options: list[str],
    year_options: list[int],
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
) -> str:
    """Assemble the self-contained HTML dashboard body."""

    field_dropdown_items = "\n".join(
        f'<label><input type="checkbox" data-field="{f}" checked onchange="updateVisibility();syncMapToCharts();"> Field {f}</label>'
        for f in field_options
    )
    year_dropdown_items = "\n".join(
        f'<label><input type="checkbox" data-year="{y}" checked onchange="updateVisibility();syncMapToCharts();"> {y}</label>'
        for y in year_options
    )

    import json

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

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{_css_template()}</style>
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
    <div class="dropdown-wrapper">
        <button class="dropdown-toggle" onclick="toggleDropdown('field-dropdown')">Fields</button>
        <div class="dropdown-menu" id="field-dropdown">
            {field_dropdown_items}
        </div>
    </div>
    <div class="dropdown-wrapper">
        <button class="dropdown-toggle" onclick="toggleDropdown('year-dropdown')">Years</button>
        <div class="dropdown-menu" id="year-dropdown">
            {year_dropdown_items}
        </div>
    </div>
    <button class="btn" onclick="resetAll()">Reset</button>
</div>
<div class="map-section">
    <h3>Field Boundaries</h3>
    <div id="map-container"></div>
    <div class="legend" id="map-legend"></div>
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
<script>
{_js_controls_template()}

var mapLayout = {map_layout_json};
var mapData = {map_data_json};
Plotly.newPlot('map-container', mapData, mapLayout, {{responsive: true}});
window.farmMap = document.getElementById('map-container');

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
</script>
</body>
</html>
"""
