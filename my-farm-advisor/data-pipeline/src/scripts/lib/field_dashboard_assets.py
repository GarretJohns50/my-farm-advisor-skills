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
/* Combined NDVI + GDD chart */
.combined-section {
    background: #fff;
    border-radius: 6px;
    border: 1px solid #ddd;
    margin: 1rem;
    padding: 1rem;
    grid-column: 1 / -1;
}
.combined-section h3 { margin: 0 0 0.5rem; font-size: 1.1rem; color: #444; }
.combined-chart-container { width: 100%; height: 400px; }
/* 3-column grid for weather panels */
.grid-3 {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 1rem;
    padding: 1rem;
}
@media (max-width: 900px) { .grid-3 { grid-template-columns: 1fr; } }
/* Chart note annotation */
.chart-note {
    font-size: 0.75rem;
    color: #666;
    margin-top: 0.3rem;
    font-style: italic;
}
/* 4-column grid for weather panels */
.grid-4 {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr 1fr;
    gap: 1rem;
    padding: 1rem;
}
@media (max-width: 1100px) { .grid-4 { grid-template-columns: 1fr 1fr; } }
@media (max-width: 600px) { .grid-4 { grid-template-columns: 1fr; } }
/* 2025 Critical Events panel */
.events-panel {
    background: #fff;
    border: 1px solid #ddd;
    border-radius: 6px;
    margin: 1rem;
    padding: 0.75rem 1rem;
}
.events-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    user-select: none;
}
.events-chips {
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
    align-items: center;
}
.event-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    background: #f5f5f5;
    border: 1px solid #e0e0e0;
    border-radius: 12px;
    padding: 0.25rem 0.6rem;
    font-size: 0.8rem;
    color: #444;
    cursor: pointer;
    transition: all 0.2s;
    user-select: none;
}
.event-chip:hover:not(.active) { background: #e8e8e8; }
.event-chip.active { background: #1a472a; color: #fff; border-color: #1a472a; }
.event-chip.severe { background: #ffebee; border-color: #ef5350; color: #c62828; }
.event-chip.severe.active { background: #c62828; border-color: #c62828; color: #fff; }
.event-chip.moderate { background: #fff3e0; border-color: #ffa726; color: #ef6c00; }
.event-chip.moderate.active { background: #ef6c00; border-color: #ef6c00; color: #fff; }
.event-chip.mild { background: #e8f5e9; border-color: #2ca02c; color: #2e7d32; }
.event-chip.mild.active { background: #2e7d32; border-color: #2e7d32; color: #fff; }
.expand-btn {
    background: none;
    border: none;
    font-size: 0.85rem;
    color: #666;
    cursor: pointer;
    padding: 0.2rem 0.5rem;
}
.events-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 0.75rem;
    margin-top: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px solid #eee;
}
.events-grid.collapsed { display: none; }
.event-card {
    background: #fafafa;
    border: 1px solid #eee;
    border-radius: 4px;
    padding: 0.6rem 0.75rem;
    cursor: pointer;
    transition: background 0.15s;
    border-left: 4px solid #bdbdbd;
}
.event-card:hover { background: #f0f0f0; }
.event-card.severe { border-left-color: #d62728; }
.event-card.moderate { border-left-color: #ff7f0e; }
.event-card.mild { border-left-color: #2ca02c; }
.event-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.85rem;
}
.event-title { font-weight: 600; color: #333; }
.event-date { font-size: 0.75rem; color: #888; }
.event-value {
    font-size: 0.9rem;
    font-weight: 600;
    color: #444;
    margin-top: 0.2rem;
}
.event-context {
    font-size: 0.75rem;
    color: #777;
    margin-top: 0.15rem;
}
.event-detail {
    display: none;
    margin-top: 0.5rem;
    padding-top: 0.5rem;
    border-top: 1px dashed #ddd;
    font-size: 0.8rem;
    color: #555;
    line-height: 1.4;
}
.event-detail.visible { display: block; }
.agro-note {
    font-style: italic;
    color: #666;
    margin-top: 0.3rem;
}
.severity-badge {
    display: inline-block;
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    margin-left: 0.3rem;
}
.severity-badge.severe { background: #ffebee; color: #c62828; }
.severity-badge.moderate { background: #fff3e0; color: #ef6c00; }
.severity-badge.mild { background: #e8f5e9; color: #2e7d32; }
"""
