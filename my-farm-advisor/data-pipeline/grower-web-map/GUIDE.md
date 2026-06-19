# grower-web-map Guide

## Overview

`generate_grower_map.py` reads a grower's field boundary GeoJSON from the data-pipeline runtime and produces a single self-contained HTML file with an interactive Leaflet map.

## Output

```
growers/<grower-slug>/derived/dashboards/grower_web_map.html
```

Open the HTML in any modern browser. No server or internet required after first load (the map tiles are cached by the browser).

## Map features

- **Basemap**: OpenStreetMap default, with Satellite and Terrain layer options
- **Field polygons**: Each field rendered with a distinct color
- **Click popup**: Shows grower slug, farm name, field ID, area (acres), and county
- **Sidebar list**: Click any field name to zoom directly to it
- **Auto-fit**: Map zooms to show all fields on load

## Usage

```bash
export DATA_PIPELINE_DATA_ROOT=~/my-farm-advisor-runtime

# Run from the runtime source copy
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src/scripts/reporting/generate_grower_map.py" \
  --grower-slug ia-grower
```

## Dependencies

The script uses only packages already in the pipeline venv:
- `geopandas` — read GeoJSON boundaries
- `shapely` — geometry handling

Leaflet.js is loaded from CDN at runtime in the browser (no Python dependency).

## File structure

```
data-pipeline/grower-web-map/
├── SKILL.md
├── AGENTS.md
├── GUIDE.md
└── src/
    └── generate_grower_map.py
```
