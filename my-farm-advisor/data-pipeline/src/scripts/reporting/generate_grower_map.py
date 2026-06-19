#!/usr/bin/env python3
"""Generate self-contained Leaflet HTML web map for a single grower.

Usage:
    python scripts/reporting/generate_grower_map.py --grower-slug <slug>

Output:
    growers/<slug>/derived/dashboards/grower_web_map.html
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd

_LOCAL_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(_LOCAL_LIB))

from runtime_paths import resolve_runtime_paths  # noqa: E402


FIELD_COLORS = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
    "#dcbeff", "#9A6324", "#fffac8", "#800000", "#aaffc3",
    "#808000", "#ffd8b1", "#000075", "#a9a9a9", "#e6beff",
]


def _load_grower_features(grower_dir: Path, grower_slug: str) -> list[dict]:
    """Load and annotate all field features across all farms for a grower."""
    farms_dir = grower_dir / "farms"
    if not farms_dir.is_dir():
        print(f"ERROR: no farms directory at {farms_dir}")
        sys.exit(1)

    all_features: list[dict] = []
    farm_dirs = sorted(f for f in farms_dir.iterdir() if f.is_dir())

    if not farm_dirs:
        print(f"ERROR: no farm subdirectories found under {farms_dir}")
        sys.exit(1)

    for farm_dir in farm_dirs:
        farm_json_path = farm_dir / "farm.json"
        boundary_path = farm_dir / "boundary" / "field_boundaries.geojson"

        if not farm_json_path.exists():
            print(f"  skipping {farm_dir.name}: no farm.json")
            continue
        if not boundary_path.exists():
            print(f"  skipping {farm_dir.name}: no field_boundaries.geojson")
            continue

        farm_meta = json.loads(farm_json_path.read_text())
        farm_display = farm_meta.get("display_name", farm_dir.name)
        farm_slug = farm_meta.get("farm_slug", farm_dir.name)

        gdf = gpd.read_file(str(boundary_path))
        if gdf.empty:
            continue

        gdf["grower_slug"] = grower_slug
        gdf["farm_display"] = farm_display
        gdf["farm_slug"] = farm_slug

        geojson_str = gdf.to_json()
        fc = json.loads(geojson_str)
        features = fc.get("features", [])
        all_features.extend(features)
        print(f"  {farm_dir.name}: {len(features)} fields loaded")

    if not all_features:
        print(f"ERROR: no field features found for grower '{grower_slug}'")
        sys.exit(1)

    return all_features


def _compute_center(features: list[dict]) -> tuple[float, float, float, float, float, float]:
    """Compute overall bounding box and center from feature geometries."""
    min_x, min_y, max_x, max_y = float("inf"), float("inf"), float("-inf"), float("-inf")
    for feat in features:
        coords = _extract_coords(feat["geometry"])
        for lng, lat in coords:
            if lng < min_x:
                min_x = lng
            if lng > max_x:
                max_x = lng
            if lat < min_y:
                min_y = lat
            if lat > max_y:
                max_y = lat
    center_lat = (min_y + max_y) / 2
    center_lng = (min_x + max_x) / 2
    return center_lat, center_lng, min_y, min_x, max_y, max_x


def _extract_coords(geom: dict) -> list[tuple[float, float]]:
    """Recursively extract all coordinate pairs from a GeoJSON geometry."""
    coords: list[tuple[float, float]] = []
    gtype = geom["type"]
    if gtype == "Polygon":
        for ring in geom["coordinates"]:
            coords.extend((c[0], c[1]) for c in ring)
    elif gtype == "MultiPolygon":
        for poly in geom["coordinates"]:
            for ring in poly:
                coords.extend((c[0], c[1]) for c in ring)
    return coords


def _build_html(
    grower_slug: str,
    grower_display: str,
    features: list[dict],
    center_lat: float,
    center_lng: float,
    min_y: float,
    min_x: float,
    max_y: float,
    max_x: float,
) -> str:
    """Generate the self-contained Leaflet map HTML."""
    geojson_str = json.dumps(features)

    field_items_html = ""
    for i, feat in enumerate(features):
        props = feat.get("properties", {})
        field_id = props.get("field_id", f"field-{i}")
        farm_disp = props.get("farm_display", "")
        label = f"{farm_disp} — {field_id}" if farm_disp else field_id
        field_items_html += (
            f'<div class="field-item" data-index="{i}"'
            f' onclick="zoomToField({i})">'
            f'<span class="field-color" style="background:{FIELD_COLORS[i % len(FIELD_COLORS)]}">'
            f"</span>{label}</div>\n"
        )

    colors_json = json.dumps(FIELD_COLORS)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{grower_display} — Field Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
#container {{ display: flex; height: 100vh; }}
#sidebar {{
  width: 300px; background: #f8f9fa; border-right: 1px solid #ddd;
  display: flex; flex-direction: column; overflow: hidden;
}}
#sidebar-header {{
  padding: 16px; background: #2E7D32; color: #fff;
}}
#sidebar-header h1 {{ font-size: 1.1em; font-weight: 600; }}
#sidebar-header p {{ font-size: 0.85em; opacity: 0.9; margin-top: 2px; }}
#field-list {{ flex: 1; overflow-y: auto; padding: 8px; }}
.field-item {{
  display: flex; align-items: center; gap: 10px; padding: 8px 10px;
  cursor: pointer; border-radius: 6px; font-size: 0.85em;
  transition: background 0.15s;
}}
.field-item:hover {{ background: #e9ecef; }}
.field-color {{
  flex-shrink: 0; width: 14px; height: 14px; border-radius: 3px;
}}
#map {{ flex: 1; }}
.leaflet-popup-content {{ font-size: 0.9em; line-height: 1.5; }}
</style>
</head>
<body>
<div id="container">
  <div id="sidebar">
    <div id="sidebar-header">
      <h1>{grower_display}</h1>
      <p>{len(features)} field{"s" if len(features) != 1 else ""}</p>
    </div>
    <div id="field-list">
      {field_items_html}
    </div>
  </div>
  <div id="map"></div>
</div>
<script>
var map = L.map('map', {{ zoomControl: true }});

var satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
  attribution: '&copy; Esri, USGS, NOAA',
  maxZoom: 19
}});

var osm = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19
}});

satellite.addTo(map);

var baseMaps = {{
  "Satellite": satellite,
  "Street Map": osm
}};
L.control.layers(baseMaps).addTo(map);

var fieldsGeoJSON = {geojson_str};
var colors = {colors_json};
var fieldLayers = [];

L.geoJSON(fieldsGeoJSON, {{
  style: function(feature) {{
    var idx = feature.properties._index || 0;
    return {{
      color: colors[idx % colors.length],
      weight: 2,
      fillOpacity: 0.35
    }};
  }},
  onEachFeature: function(feature, layer) {{
    var p = feature.properties;
    var idx = fieldLayers.length;
    feature.properties._index = idx;
    fieldLayers.push(layer);

    var html = '<b>' + p.field_id + '</b><br>' +
      'Grower: ' + p.grower_slug + '<br>' +
      'Farm: ' + p.farm_display + '<br>' +
      'Area: ' + Number(p.area_acres).toFixed(1) + ' ac<br>' +
      'County: ' + (p.county_name || '--');
    layer.bindPopup(html);
  }}
}}).addTo(map);

map.fitBounds([[{min_y}, {min_x}], [{max_y}, {max_x}]]);

function zoomToField(idx) {{
  var layer = fieldLayers[idx];
  if (layer) {{
    map.fitBounds(layer.getBounds(), {{ padding: [30, 30] }});
    layer.openPopup();
  }}
}}
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a self-contained Leaflet web map for a grower"
    )
    parser.add_argument(
        "--grower-slug", required=True,
        help="Grower slug (e.g., ia-grower)"
    )
    args = parser.parse_args()
    grower_slug = args.grower_slug

    paths = resolve_runtime_paths()
    data_root = paths.runtime_base
    grower_dir = data_root / "growers" / grower_slug

    if not grower_dir.is_dir():
        print(f"ERROR: grower directory not found: {grower_dir}")
        sys.exit(1)

    grower_json_path = grower_dir / "grower.json"
    if not grower_json_path.exists():
        print(f"ERROR: grower.json not found at {grower_json_path}")
        sys.exit(1)

    grower_meta = json.loads(grower_json_path.read_text())
    grower_display = grower_meta.get("display_name", grower_slug)
    print(f"Loading fields for grower: {grower_display} ({grower_slug})")

    features = _load_grower_features(grower_dir, grower_slug)
    center_lat, center_lng, min_y, min_x, max_y, max_x = _compute_center(features)

    output_dir = grower_dir / "derived" / "dashboards"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "grower_web_map.html"

    html = _build_html(
        grower_slug, grower_display, features,
        center_lat, center_lng, min_y, min_x, max_y, max_x,
    )
    output_path.write_text(html)
    print(f"Wrote {output_path} ({len(html)} bytes, {len(features)} fields)")


if __name__ == "__main__":
    main()
