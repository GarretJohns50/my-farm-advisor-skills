"""CSS and asset extensions for the single-field NDVI dashboard.

Builds on dashboard_assets with additional styles for NDVI chart,
composite image gallery, and crop history table.
"""

from __future__ import annotations


def _field_css_template() -> str:
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
/* NDVI specific */
.ndvi-section {
    background: #fff;
    border-radius: 6px;
    border: 1px solid #ddd;
    margin: 1rem;
    padding: 1rem;
    grid-column: 1 / -1;
}
.ndvi-section h3 { margin: 0 0 0.5rem; font-size: 1.1rem; color: #444; }
.ndvi-chart-container { width: 100%; height: 400px; }
.composite-gallery {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 1rem;
    margin-top: 1rem;
}
.composite-item {
    background: #fafafa;
    border: 1px solid #eee;
    border-radius: 4px;
    padding: 0.5rem;
    text-align: center;
}
.composite-item img {
    max-width: 100%;
    height: auto;
    border-radius: 3px;
}
.composite-item .caption {
    font-size: 0.8rem;
    color: #666;
    margin-top: 0.3rem;
}
/* Crop history table */
.crop-section {
    background: #fff;
    border-radius: 6px;
    border: 1px solid #ddd;
    margin: 1rem;
    padding: 1rem;
}
.crop-section h3 { margin: 0 0 0.5rem; font-size: 1rem; color: #444; }
.crop-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
}
.crop-table th {
    background: #f0f0f0;
    text-align: left;
    padding: 0.5rem;
    border-bottom: 2px solid #ddd;
}
.crop-table td {
    padding: 0.5rem;
    border-bottom: 1px solid #eee;
}
.crop-table tr:hover { background: #fafafa; }
.legend-inline {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.8rem;
    color: #666;
    margin-left: 1rem;
}
.legend-symbol { font-size: 1rem; }
"""


def _field_js_template() -> str:
    return """\
function toggleVisibility(chartId, years) {
    var gd = document.getElementById(chartId);
    if (!gd || !gd.data) return;
    var visible = [];
    for (var i = 0; i < gd.data.length; i++) {
        var d = gd.data[i];
        visible.push(years.indexOf(String(d.year)) >= 0 ? true : 'legendonly');
    }
    Plotly.restyle(gd, {visible: visible});
}
function resetYears(chartId) {
    var gd = document.getElementById(chartId);
    if (!gd || !gd.data) return;
    var visible = [];
    for (var i = 0; i < gd.data.length; i++) visible.push(true);
    Plotly.restyle(gd, {visible: visible});
}
"""
