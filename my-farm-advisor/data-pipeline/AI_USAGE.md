# AI Tool Usage

This project used AI-assisted development. All AI-generated output was
reviewed and verified before being accepted — nothing was merged blind.

## Debugging Python errors

- Diagnosed and fixed a field-switch bug in
  `src/scripts/lib/field_dashboard_generator.py` where selecting a field left
  the previous field's weather and NDVI data visible.
- Used `node --check` on the JavaScript extracted from the generated HTML to
  catch syntax errors before they reached the browser.
- Wrote a stub-browser smoke test (`node /tmp/opencode/smoke.js`) that loads
  the generated dashboard HTML and asserts on expected elements and computed
  Analysis Summary content — 94/94 checks passing.

## Improving visualizations

- Refined the NDVI time series to differentiate years by color and
  Sentinel-2 vs Landsat by marker shape (colorblind-safe).
- Sized NDVI markers by cloud cover so lower-quality scenes are visually
  distinguishable.
- Added spread-rate maps overlaying recommended fertilizer/lime application
  rates on each field.

## Explaining geospatial workflows

- Documented the field analysis grid pipeline: grid generation
  (`scripts/generate_field_grid.py`), soil-sample enrichment
  (`scripts/enrich_grid_with_soil.py`), and annual NDVI extraction
  (`scripts/extract_year_grids.py`).
- Explained basemap buffer sizing and soil sufficiency rating logic in
  `src/scripts/lib/sufficiency_ranges.py`.

## Generating alternative analytical ideas

- Compared year-over-year NDVI trends to flag declining vs volatile vigor
  (Field 1 steady decline; Field 2 larger swings with a 2024 dip).
- Proposed heat-stress-day counts and top non-optimal soil variables
  (P, S, K, Zn) as the most informative dashboard metrics.
- Derived fertilizer/lime action suggestions from soil sample sufficiency
  ranges.

## Improving dashboard layout structure

- Moved from single-field pages to a combined multi-field dashboard with an
  overlay map, per-field weather/NDVI panels, report cards, and a computed
  Analysis Summary panel.
- Grouped related charts into logical sections and added a summary panel that
  grades each field (overall, soil, pH, vigor, weather).

## Verification

Every AI-assisted change was independently checked: `scripts/validate.sh`
(44/44 PASS), byte-identical regeneration of the single-field dashboard
(sha256 match), and the 94-check smoke test over the rendered HTML.
