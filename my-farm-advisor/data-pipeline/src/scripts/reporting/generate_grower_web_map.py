#!/usr/bin/env python3
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false
"""Generate a self-contained interactive HTML web map for a grower.

Aggregates all farms for the grower, displays field polygons on a Leaflet map
with OSM and Esri satellite basemap options, and provides a sidebar with a
searchable, toggleable field list and zoom-to controls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

import geopandas as gpd

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from runtime_paths import resolve_runtime_paths  # noqa: E402

_RUNTIME_PATHS = resolve_runtime_paths()
_REPO = _RUNTIME_PATHS.runtime_base
_SCRIPTS = _RUNTIME_PATHS.runtime_scripts
_LIB = _RUNTIME_PATHS.runtime_scripts / "lib"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_LIB))

from paths import (  # noqa: E402
    farm_boundary_path,
    grower_dir,
    grower_map_path,
)

_DEFAULT_GROWER = os.environ.get("AG_GROWER_SLUG", "default-grower")

# Farm colors for polygon styling (cycled per farm)
_FARM_COLORS = [
    "#2E7D32",  # green
    "#1565C0",  # blue
    "#E65100",  # orange
    "#6A1B9A",  # purple
    "#C62828",  # red
    "#00695C",  # teal
    "#F9A825",  # yellow
    "#5D4037",  # brown
]


def _humanize_farm_name(farm_slug: str) -> str:
    """Convert a farm slug like 'central-ia-grower-iowa' to a readable name."""
    return farm_slug.replace("-", " ").replace("_", " ").title()


def _discover_farms(grower_slug: str) -> list[tuple[str, str, gpd.GeoDataFrame]]:
    """Return list of (farm_slug, farm_name, fields_gdf) for all farms of a grower."""
    farms_dir = grower_dir(grower_slug) / "farms"
    if not farms_dir.exists():
        return []

    results: list[tuple[str, str, gpd.GeoDataFrame]] = []
    for farm_path in sorted(farms_dir.iterdir()):
        if not farm_path.is_dir():
            continue
        farm_slug = farm_path.name
        farm_name = _humanize_farm_name(farm_slug)
        boundary_path = farm_boundary_path(grower_slug, farm_slug)
        if not boundary_path.exists():
            print(f"  warn: no boundary for farm {farm_slug}, skipping")
            continue
        try:
            gdf = gpd.read_file(boundary_path)
            if gdf.empty:
                print(f"  warn: empty boundary for farm {farm_slug}, skipping")
                continue
            # Enrich each feature with grower/farm metadata
            gdf["grower_slug"] = grower_slug
            gdf["farm_slug"] = farm_slug
            gdf["farm_name"] = farm_name
            results.append((farm_slug, farm_name, gdf))
            print(f"  loaded {len(gdf)} field(s) from farm {farm_slug}")
        except Exception as exc:
            print(f"  warn: failed to read boundary for farm {farm_slug}: {exc}")
            continue

    return results


def _build_geojson(farms: list[tuple[str, str, gpd.GeoDataFrame]]) -> dict:
    """Merge all farm GeoDataFrames into a single GeoJSON FeatureCollection."""
    all_features: list[dict] = []
    for farm_slug, farm_name, gdf in farms:
        geojson = json.loads(gdf.to_json())
        for feat in geojson.get("features", []):
            # Ensure properties we need are present
            props = feat.setdefault("properties", {})
            props.setdefault("farm_slug", farm_slug)
            props.setdefault("farm_name", farm_name)
            props.setdefault("field_id", props.get("field_id", "unknown"))
            props.setdefault("crop_name", props.get("crop_name", "—"))
            props.setdefault("area_acres", props.get("area_acres", 0))
            props.setdefault("county_name", props.get("county_name", "—"))
            props.setdefault("state_fips", props.get("state_fips", ""))
            all_features.append(feat)

    return {"type": "FeatureCollection", "features": all_features}


def _generate_html(grower_slug: str, geojson_data: dict) -> str:
    """Build the self-contained HTML string with Leaflet map and sidebar."""
    geojson_json = json.dumps(geojson_data, separators=(",", ":"))
    n_fields = len(geojson_data["features"])
    n_farms = len({f["properties"]["farm_slug"] for f in geojson_data["features"]})

    # Compute map center and bounds from all geometries
    all_coords: list[list[float]] = []
    for feat in geojson_data["features"]:
        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            for ring in geom["coordinates"]:
                all_coords.extend(ring)
        elif geom["type"] == "MultiPolygon":
            for poly in geom["coordinates"]:
                for ring in poly:
                    all_coords.extend(ring)

    if all_coords:
        lons = [c[0] for c in all_coords]
        lats = [c[1] for c in all_coords]
        center_lat = sum(lats) / len(lats)
        center_lon = sum(lons) / len(lons)
    else:
        center_lat, center_lon = 40.0, -93.0

    # Build a field registry for the sidebar JavaScript
    field_registry: list[dict] = []
    for i, feat in enumerate(geojson_data["features"]):
        props = feat["properties"]
        # Compute centroid for zoom-to
        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            coords = geom["coordinates"][0]
        elif geom["type"] == "MultiPolygon":
            coords = geom["coordinates"][0][0]
        else:
            coords = [[center_lon, center_lat]]
        cx = sum(c[0] for c in coords) / len(coords)
        cy = sum(c[1] for c in coords) / len(coords)
        area = float(props.get("area_acres", 0) or 0)
        field_registry.append(
            {
                "idx": i,
                "field_id": str(props.get("field_id", f"field-{i}")),
                "farm_slug": str(props.get("farm_slug", "")),
                "farm_name": str(props.get("farm_name", "")),
                "crop": str(props.get("crop_name", "—")),
                "area_acres": round(area, 1),
                "county": str(props.get("county_name", "—")),
                "state_fips": str(props.get("state_fips", "")),
                "centroid_lon": cx,
                "centroid_lat": cy,
            }
        )

    field_registry_json = json.dumps(field_registry, separators=(",", ":"))
    farm_slugs = sorted({f["farm_slug"] for f in field_registry})
    farm_colors_json = json.dumps(
        {slug: _FARM_COLORS[i % len(_FARM_COLORS)] for i, slug in enumerate(farm_slugs)},
        separators=(",", ":"),
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{grower_slug.replace("-", " ").title()} — Field Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
        crossorigin=""/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
          integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
          crossorigin=""></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; height: 100%; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
    #container {{ display: flex; height: 100vh; width: 100vw; }}

    /* Sidebar */
    #sidebar {{
      width: 360px;
      min-width: 300px;
      background: #f8fafc;
      border-right: 1px solid #e2e8f0;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}
    #sidebar-header {{
      padding: 1.25rem 1rem 0.75rem;
      background: linear-gradient(135deg, #1e3a5f, #2563eb);
      color: white;
    }}
    #sidebar-header h1 {{ margin: 0; font-size: 1.15rem; font-weight: 600; }}
    #sidebar-header p {{ margin: 0.25rem 0 0; font-size: 0.8rem; opacity: 0.85; }}

    #controls {{
      padding: 0.75rem 1rem;
      border-bottom: 1px solid #e2e8f0;
      background: white;
    }}
    #search {{
      width: 100%;
      padding: 0.5rem 0.75rem;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      font-size: 0.9rem;
      margin-bottom: 0.5rem;
    }}
    #search:focus {{ outline: none; border-color: #2563eb; }}
    .bulk-btns {{
      display: flex;
      gap: 0.4rem;
      flex-wrap: wrap;
    }}
    .bulk-btns button {{
      padding: 0.35rem 0.6rem;
      border: 1px solid #cbd5e1;
      border-radius: 4px;
      background: white;
      font-size: 0.78rem;
      cursor: pointer;
      color: #334155;
    }}
    .bulk-btns button:hover {{ background: #f1f5f9; }}
    .bulk-btns button.active {{ background: #dbeafe; border-color: #2563eb; color: #1e40af; }}

    #field-list {{
      flex: 1;
      overflow-y: auto;
      padding: 0 0.75rem 0.75rem;
    }}
    .farm-group {{
      margin-top: 0.75rem;
    }}
    .farm-group-header {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 0.25rem;
      font-size: 0.85rem;
      font-weight: 600;
      color: #1e3a5f;
      border-bottom: 2px solid #bfdbfe;
      margin-bottom: 0.25rem;
      cursor: pointer;
      user-select: none;
    }}
    .farm-group-header .color-dot {{
      width: 12px;
      height: 12px;
      border-radius: 50%;
      flex-shrink: 0;
    }}
    .farm-group-header .farm-count {{
      margin-left: auto;
      font-size: 0.75rem;
      color: #64748b;
      font-weight: 400;
    }}

    .field-row {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.4rem 0.25rem;
      border-radius: 4px;
      font-size: 0.82rem;
      color: #334155;
    }}
    .field-row:hover {{ background: #f1f5f9; }}
    .field-row.hidden-row {{ opacity: 0.4; }}
    .field-row input[type="checkbox"] {{ cursor: pointer; }}
    .field-row .field-info {{
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
    }}
    .field-row .field-id {{
      font-weight: 600;
      color: #1e293b;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .field-row .field-meta {{
      font-size: 0.75rem;
      color: #64748b;
    }}
    .field-row .zoom-btn {{
      padding: 0.2rem 0.4rem;
      border: none;
      background: #dbeafe;
      color: #1e40af;
      border-radius: 4px;
      font-size: 0.75rem;
      cursor: pointer;
      flex-shrink: 0;
    }}
    .field-row .zoom-btn:hover {{ background: #bfdbfe; }}

    /* Map */
    #map {{
      flex: 1;
      min-width: 0;
      height: 100%;
    }}

    /* Popup */
    .field-popup h3 {{ margin: 0 0 0.4rem; font-size: 1rem; color: #1e3a5f; }}
    .field-popup table {{ font-size: 0.85rem; border-collapse: collapse; width: 100%; }}
    .field-popup td {{ padding: 0.2rem 0; }}
    .field-popup td:first-child {{ color: #64748b; padding-right: 0.75rem; white-space: nowrap; }}
    .field-popup td:last-child {{ font-weight: 500; }}

    /* Responsive */
    @media (max-width: 768px) {{
      #container {{ flex-direction: column; }}
      #sidebar {{ width: 100%; height: 45vh; min-width: auto; border-right: none; border-bottom: 1px solid #e2e8f0; }}
      #map {{ height: 55vh; }}
    }}
  </style>
</head>
<body>
  <div id="container">
    <div id="sidebar">
      <div id="sidebar-header">
        <h1>{grower_slug.replace("-", " ").title()}</h1>
        <p>{n_fields} fields across {n_farms} farm{'s' if n_farms != 1 else ''}</p>
      </div>
      <div id="controls">
        <input type="text" id="search" placeholder="Search fields, farms, crops...">
        <div class="bulk-btns">
          <button onclick="selectAll(true)">Select all</button>
          <button onclick="selectAll(false)">Deselect all</button>
          <button onclick="resetFilter()" id="reset-btn" style="display:none">Clear filter</button>
        </div>
      </div>
      <div id="field-list"></div>
    </div>
    <div id="map"></div>
  </div>

  <script>
    // Embedded data
    var fieldData = {geojson_json};
    var fieldRegistry = {field_registry_json};
    var farmColors = {farm_colors_json};

    // Initialize map
    var map = L.map('map').setView([{center_lat}, {center_lon}], 12);

    // Basemap layers
    var osmLayer = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '&copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19
    }});
    var satelliteLayer = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
      attribution: 'Tiles &copy; Esri',
      maxZoom: 19
    }});

    osmLayer.addTo(map);

    // Layer control
    L.control.layers({{
      "OpenStreetMap": osmLayer,
      "Satellite (Esri)": satelliteLayer
    }}, null, {{ position: 'topright', collapsed: true }}).addTo(map);

    // Polygon layers keyed by feature index
    var layers = {{}};
    var popups = {{}};

    function getFarmColor(farmSlug) {{
      return farmColors[farmSlug] || '#757575';
    }}

    function formatArea(ac) {{
      if (ac >= 100) return ac.toFixed(0) + ' ac';
      if (ac >= 10) return ac.toFixed(1) + ' ac';
      return ac.toFixed(2) + ' ac';
    }}

    function buildPopupContent(props) {{
      var stateName = props.state_fips ? ' (FIPS ' + props.state_fips + ')' : '';
      return '<div class="field-popup">' +
        '<h3>' + (props.field_id || 'Field') + '</h3>' +
        '<table>' +
        '<tr><td>Grower</td><td>' + (props.grower_slug || '—') + '</td></tr>' +
        '<tr><td>Farm</td><td>' + (props.farm_name || props.farm_slug || '—') + '</td></tr>' +
        '<tr><td>Field ID</td><td>' + (props.field_id || '—') + '</td></tr>' +
        '<tr><td>Area</td><td>' + formatArea(parseFloat(props.area_acres || 0)) + '</td></tr>' +
        '<tr><td>Crop</td><td>' + (props.crop_name || '—') + '</td></tr>' +
        '<tr><td>County</td><td>' + (props.county_name || '—') + stateName + '</td></tr>' +
        '<tr><td>Source</td><td>' + (props.source || '—') + '</td></tr>' +
        '</table></div>';
    }}

    // Render field polygons
    fieldData.features.forEach(function(feat, idx) {{
      var color = getFarmColor(feat.properties.farm_slug);
      var layer = L.geoJSON(feat, {{
        style: {{
          color: color,
          weight: 2.5,
          fillColor: color,
          fillOpacity: 0.25,
          opacity: 0.9
        }},
        onEachFeature: function(feature, layer) {{
          var popup = L.popup({{ maxWidth: 280 }}).setContent(buildPopupContent(feature.properties));
          layer.bindPopup(popup);
          layer.on('mouseover', function() {{
            layer.setStyle({{ weight: 4, fillOpacity: 0.45 }});
          }});
          layer.on('mouseout', function() {{
            layer.setStyle({{ weight: 2.5, fillOpacity: 0.25 }});
          }});
        }}
      }}).addTo(map);
      layers[idx] = layer;
    }});

    // Fit to all fields
    if (fieldData.features.length > 0) {{
      var group = new L.featureGroup(Object.values(layers));
      map.fitBounds(group.getBounds().pad(0.1));
    }}

    // Sidebar: group fields by farm
    var farmsInSidebar = {{}};
    fieldRegistry.forEach(function(reg) {{
      if (!farmsInSidebar[reg.farm_slug]) {{
        farmsInSidebar[reg.farm_slug] = {{ name: reg.farm_name, color: getFarmColor(reg.farm_slug), fields: [] }};
      }}
      farmsInSidebar[reg.farm_slug].fields.push(reg);
    }});

    var listContainer = document.getElementById('field-list');
    var allCheckboxes = [];
    var searchTerm = '';

    function renderSidebar() {{
      listContainer.innerHTML = '';
      allCheckboxes = [];
      Object.keys(farmsInSidebar).sort().forEach(function(farmSlug) {{
        var farm = farmsInSidebar[farmSlug];
        var groupDiv = document.createElement('div');
        groupDiv.className = 'farm-group';

        var header = document.createElement('div');
        header.className = 'farm-group-header';
        header.innerHTML = '<span class="color-dot" style="background:' + farm.color + '"></span>' +
          farm.name +
          '<span class="farm-count">' + farm.fields.length + ' field' + (farm.fields.length !== 1 ? 's' : '') + '</span>';
        groupDiv.appendChild(header);

        farm.fields.forEach(function(reg) {{
          var matches = !searchTerm ||
            reg.field_id.toLowerCase().includes(searchTerm) ||
            reg.farm_name.toLowerCase().includes(searchTerm) ||
            reg.crop.toLowerCase().includes(searchTerm) ||
            reg.county.toLowerCase().includes(searchTerm);

          var row = document.createElement('div');
          row.className = 'field-row' + (matches ? '' : ' hidden-row');
          row.dataset.idx = reg.idx;

          var cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.checked = true;
          cb.dataset.idx = reg.idx;
          cb.addEventListener('change', function() {{
            toggleField(reg.idx, cb.checked);
          }});
          allCheckboxes.push(cb);

          var info = document.createElement('div');
          info.className = 'field-info';
          info.innerHTML = '<span class="field-id">' + reg.field_id + '</span>' +
            '<span class="field-meta">' + reg.crop + ' &middot; ' + formatArea(reg.area_acres) + ' &middot; ' + reg.county + '</span>';

          var zoomBtn = document.createElement('button');
          zoomBtn.className = 'zoom-btn';
          zoomBtn.textContent = '🎯';
          zoomBtn.title = 'Zoom to field';
          zoomBtn.addEventListener('click', function(e) {{
            e.stopPropagation();
            zoomToField(reg.idx);
          }});

          row.appendChild(cb);
          row.appendChild(info);
          row.appendChild(zoomBtn);
          groupDiv.appendChild(row);

          // Click row to zoom
          row.addEventListener('click', function(e) {{
            if (e.target !== cb && e.target !== zoomBtn) {{
              zoomToField(reg.idx);
            }}
          }});
        }});

        listContainer.appendChild(groupDiv);
      }});
    }}

    function toggleField(idx, visible) {{
      var layer = layers[idx];
      if (!layer) return;
      if (visible) {{
        if (!map.hasLayer(layer)) map.addLayer(layer);
      }} else {{
        if (map.hasLayer(layer)) map.removeLayer(layer);
      }}
    }}

    function zoomToField(idx) {{
      var layer = layers[idx];
      var reg = fieldRegistry.find(function(r) {{ return r.idx === idx; }});
      if (!layer || !reg) return;
      map.setView([reg.centroid_lat, reg.centroid_lon], 15);
      layer.openPopup();
    }}

    function selectAll(state) {{
      allCheckboxes.forEach(function(cb) {{
        cb.checked = state;
        toggleField(parseInt(cb.dataset.idx), state);
      }});
    }}

    function resetFilter() {{
      document.getElementById('search').value = '';
      searchTerm = '';
      renderSidebar();
      selectAll(true);
      document.getElementById('reset-btn').style.display = 'none';
    }}

    // Search listener
    document.getElementById('search').addEventListener('input', function(e) {{
      var val = e.target.value.toLowerCase().trim();
      searchTerm = val;
      document.getElementById('reset-btn').style.display = val ? 'inline-block' : 'none';
      renderSidebar();
      // Keep visibility in sync with checkboxes after re-render
      allCheckboxes.forEach(function(cb) {{
        toggleField(parseInt(cb.dataset.idx), cb.checked);
      }});
    }});

    renderSidebar();
  </script>
</body>
</html>"""
    return html


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--grower-slug",
        default=_DEFAULT_GROWER,
        help="Grower slug (default: AG_GROWER_SLUG env or 'default-grower')",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Override output HTML file path",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="Open the generated map in the default browser",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Grower Web Map — interactive Leaflet field map")
    print("=" * 60)
    print(f"Grower: {args.grower_slug}")

    farms = _discover_farms(args.grower_slug)
    if not farms:
        print("No farms found for grower. Exiting.")
        sys.exit(1)

    total_fields = sum(len(gdf) for _, _, gdf in farms)
    print(f"Total: {total_fields} field(s) across {len(farms)} farm(s)")

    geojson_data = _build_geojson(farms)
    html = _generate_html(args.grower_slug, geojson_data)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = grower_map_path(args.grower_slug)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    size_kb = output_path.stat().st_size / 1024
    print(f"✓ Map saved → {output_path}")
    print(f"  Size: {size_kb:.1f} KB ({len(geojson_data['features'])} fields)")

    if args.open_browser:
        import webbrowser
        webbrowser.open(f"file://{output_path.resolve()}")


if __name__ == "__main__":
    main()
