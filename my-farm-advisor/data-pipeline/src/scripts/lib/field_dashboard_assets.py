"""CSS and asset extensions for the single-field NDVI dashboard.

Builds on dashboard_assets with additional styles for NDVI chart,
composite image gallery, crop history table, temperature chart,
and unified year filter bar.
"""

from __future__ import annotations


def _field_css_template() -> str:
    return """\
/* Year filter */
.year-filter-bar {
    display: flex;
    gap: 0.4rem;
    align-items: center;
    flex-wrap: wrap;
}
.year-btn {
    background: #fff;
    border: 1px solid #ccc;
    border-radius: 4px;
    padding: 0.3rem 0.6rem;
    cursor: pointer;
    font-size: 0.85rem;
    transition: all 0.2s;
}
.year-btn.active {
    background: #1a472a;
    color: #fff;
    border-color: #1a472a;
}
.year-btn.global {
    font-weight: bold;
    border-color: #666;
}
.year-btn:hover:not(.active) {
    background: #f0f0f0;
}
/* Temperature section */
.temp-section {
    background: #fff;
    border-radius: 6px;
    border: 1px solid #ddd;
    margin: 1rem;
    padding: 1rem;
}
.temp-section h3 { margin: 0 0 0.5rem; font-size: 1rem; color: #444; }
.temp-chart-container { width: 100%; height: 300px; }
/* NDVI section */
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
/* Composite gallery */
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
/* Corn growth stage legend */
.stage-legend-bar {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
    margin-top: 0.5rem;
    padding-top: 0.5rem;
    border-top: 1px solid #eee;
    font-size: 0.75rem;
    color: #555;
}
.stage-label {
    font-weight: bold;
    margin-right: 0.3rem;
}
.stage-item {
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    white-space: nowrap;
}
.stage-swatch {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
}
.stage-divider {
    color: #aaa;
    font-size: 0.7rem;
}
"""
