# Grower Web Map

Interactive, self-contained HTML map for a grower showing all farm fields.

## What it does

- Discovers every farm under a grower
- Reads each farm's `field_boundaries.geojson`
- Renders a single HTML file with a Leaflet map
- Sidebar shows a searchable, checkbox-toggleable field list grouped by farm
- Basemap toggle: OpenStreetMap ↔ Esri World Imagery (satellite)
- Click a field polygon or sidebar row to see metadata popup
- Zoom-to buttons focus the map on individual fields

## Run

From the runtime copy (after install/refresh):

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_web_map.py \
  --grower-slug central-ia-grower
```

Options:
- `--grower-slug` — required grower identifier
- `--output` — override output path
- `--open` — open the HTML in the default browser after generation

## Output

`${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/maps/grower_web_map.html`

The file is self-contained: all GeoJSON data is embedded, no external data files needed at runtime. Only Leaflet.js and tile layers are loaded from CDN.
