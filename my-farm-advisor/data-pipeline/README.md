# Data Pipeline Runtime Setup

This subskill ships the scripts that build the data-pipeline reports and
posters. Each runtime host creates its own virtualenv inside the data tree on
first run; the scripts auto-bootstrap that environment before continuing.

## Quick start

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/ingest/bootstrap_farm_from_county.py \
  --state-fips 17 \
  --county-name DeKalb \
  --count 5 \
  --seed 77 \
  --grower-slug il-dekalb-grower \
  --farm-slug dekalb-demo-farm \
  --farm-name "DeKalb Demo Farm" \
  --run-pipeline \
  --force
```

For a first run that also initializes shared data and seeds fields for a grower in a state, use the installer directly from the checkout:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh \
  --prepare-shared-data \
  --seed-grower-slug acme-grower \
  --seed-state Illinois \
  --seed-field-count 12 \
  --seed-farm-name "Acme Illinois Farm"
```

That command installs the runtime source and venv, builds shared geoadmin L0/L1/L2 payloads, shared NASA POWER county weather, GDD, annual corn RM, annual soybean MG, five-year FIPS-average corn RM and soybean MG datasets, and last-five-year CONUS CDL rasters. It then selects a top-crop county in the requested state, samples the requested number of OSM fields, and runs the full farm pipeline so derived tables, field weather, soil outputs, CDL history, satellite/NDVI products, reports, cards, posters, and HTML/Markdown farm reports are generated automatically.

If the runtime is already installed, run the equivalent from the runtime source copy:

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/farm_dashboard.py create \
  --prepare-shared-data \
  --grower-slug acme-grower \
  --state Illinois \
  --field-count 12 \
  --farm-name "Acme Illinois Farm"
```

`DATA_PIPELINE_DATA_ROOT` is required. Set it to an absolute writable path outside the skill checkout before running the installer or any pipeline entrypoint. There is no implicit fallback to a platform workspace path or to a checkout-local `data/` directory.

The installer creates and refreshes the runtime tree under:

- runtime base: `${DATA_PIPELINE_DATA_ROOT}/data-pipeline`
- runtime source copy: `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src`
- default runtime venv: `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv`

Generated outputs, manifests, reports, logs, and downloaded payloads belong under the runtime base, for example `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers` and `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared`. The committed checkout remains the source for installer scripts and baseline `src/` files, but runtime execution happens from the copied source.

Farm weather now uses NASA POWER's public S3 Zarr stores by default at actual field centroids. The default farm weather controls are `--weather-backend zarr`, `--weather-start-year 2021`, `--weather-end-year 2025`, and `--weather-time-standard lst`. The output path and CSV schema stay compatible with existing reports:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/derived/tables/<farm>_weather_2021_2025.csv
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower>/farms/<farm>/fields/<field>/weather/daily_weather.csv
```

Run or override those defaults from the runtime source copy:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/run_farm_pipeline.py \
  --grower-slug il-dekalb-grower \
  --farm-slug dekalb-demo-farm \
  --farm-name "DeKalb Demo Farm" \
  --weather-backend zarr \
  --weather-start-year 2021 \
  --weather-end-year 2025 \
  --weather-time-standard lst
```

Use `--weather-backend api` only when explicitly debugging the legacy NASA POWER point API path for small field sets.

Shared county weather for maturity-by-FIPS uses NASA POWER's public S3 Zarr stores by default instead of issuing one `power.larc.nasa.gov` point API request per county grid cell. This avoids API rate-limit failures for L2 geoadmin scopes while preserving the existing output path and schema:

```text
${DATA_PIPELINE_DATA_ROOT}/data-pipeline/shared/weather/nasa-power/<year>/daily_weather_by_fips.parquet
```

For the full shared lower48 baseline, initialize the runtime with multi-year county weather, GDD, corn RM, soybean MG, corn/soybean five-year FIPS averages, and CDL raster outputs. The default shared maturity range is 2021-2025 to match the farm weather and CDL helper defaults; CDL initialization fetches the last five available CONUS rasters by default:

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd my-farm-advisor/data-pipeline
./scripts/install.sh --prepare-shared-data
```

That install flag runs the equivalent of:

```bash
python scripts/run_maturity_years_by_fips.py \
  --start-year 2021 \
  --end-year 2025 \
  --coverage lower48 \
  --weather-backend zarr \
  --weather-time-standard lst
python scripts/ingest/download_cdl.py \
  --raster-only \
  --cdl-scope conus \
  --cdl-latest-year 2025 \
  --cdl-window-years 5
```

`--prepare-shared-maturity` remains available for weather/GDD/corn/soy maturity only, but it does not prepare CDL rasters.

The maturity runner writes annual files like `shared/corn_maturity/tables/rm_by_fips_2025.parquet` and final five-year average files like `shared/corn_maturity/tables/rm_by_fips_2021_2025_average.parquet` and `shared/soybean_maturity/tables/mg_by_fips_2021_2025_average.parquet`.

For a single annual refresh, run:

```bash
python scripts/run_maturity_by_fips.py \
  --year 2025 \
  --coverage lower48 \
  --weather-backend zarr \
  --weather-time-standard lst
```

Use `--weather-backend api` only when explicitly debugging the legacy NASA POWER point API path for county weather.

To persist the default data root for future login sessions, write the user environment file and still export the variable in the current shell before running commands:

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/environment.d"
cat > "${XDG_CONFIG_HOME:-$HOME/.config}/environment.d/60-my-farm-advisor.conf" <<'EOF'
DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
EOF
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
```

The `environment.d` file applies to future sessions only. It does not update an already-running shell.

## Running inside OpenClaw CLI

When invoking the pipeline from the control UI or `openclaw-cli`, you can still
activate the environment explicitly, but the entrypoints will install and re-exec
themselves if the runtime venv is missing.

```bash
bash -lc 'export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime && \
  cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src" && \
  "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
    scripts/run_farm_pipeline.py --grower-slug ... --farm-slug ...'
```

This ensures every pipeline step (including geopandas/rasterio operations) uses
the shared environment that lives alongside the replicated scripts.

## Weather Dashboard

The pipeline can generate an interactive, self-contained weather dashboard as an
optional final step. The dashboard is a single HTML file with zero runtime
external dependencies (no CDN, no API calls). Plotly.js is vendored/inline at
build time; satellite basemap tiles are fetched at generation time and
base64-encoded.

### Pipeline integration

Add `--dashboard` to `run_farm_pipeline.py`:

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/run_farm_pipeline.py \
  --grower-slug il-dekalb-grower \
  --farm-slug dekalb-demo-farm \
  --farm-name "DeKalb Demo Farm" \
  --dashboard \
  --no-basemap
```

Flags:
- `--dashboard` — append weather dashboard generation as the final pipeline step
- `--no-basemap` — skip satellite imagery, use a neutral background
- `--force-basemap` — ignore tile cache and re-download basemap tiles

### Standalone CLI

Generate a dashboard outside the full pipeline:

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/dashboard_cli.py dashboard generate \
  --farm-dir "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/central-ia-grower/farms/central-ia-grower-iowa" \
  --no-basemap
```

The CLI supports runtime directory discovery: explicit `--farm-dir` → `--growers-dir` →
`DATA_PIPELINE_DATA_ROOT` env var → auto-scan under `~`. When multiple farms exist,
the CLI errors clearly and lists candidates so you can select one explicitly.

### Output

Dashboards are written to `growers/<grower>/farms/<farm>/derived/dashboards/<farm>_dashboard.html`.

### Dashboard features

- **Field boundary map** — interactive Plotly scattermap with fill and labels
- **Last frost marker** — per-field-year last frost date tooltip
- **Daily GDD chart** — growing degree days (base 10°C) from last frost
- **Daily rainfall chart** — rainfall in inches from last frost
- **Cumulative GDD** — running total of heat units
- **Cumulative rainfall** — running total of precipitation
- **Field + Year dropdown filters** — toggle visibility without re-rendering
- **Reset button** — restore all traces
- **Colorblind-safe palette** — 10 distinguishable colors per field

## Single-Field NDVI Dashboard

For deep analysis of an individual field, generate a focused dashboard that
combines weather data with per-scene NDVI time series from Sentinel-2
(prioritized) and Landsat (gap-filled). The dashboard embeds derived
composite images (peak-95 NDVI, cumulative season NDVI) and a crop history
table.

### Pipeline integration

Add `--field-dashboard <field-id>` as the final step:

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/run_farm_pipeline.py \
  --grower-slug central-ne-grower \
  --farm-slug central-ne-grower-nebraska \
  --farm-name "Central NE Grower Nebraska" \
  --field-dashboard osm-554305501 \
  --no-basemap
```

### Standalone CLI

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/field_dashboard_cli.py field-dashboard generate \
  --grower-slug central-ne-grower \
  --farm-slug central-ne-grower-nebraska \
  --field-id osm-554305501 \
  --no-basemap
```

### Output

`fields/<field-id>/derived/dashboards/<field-id>_dashboard.html`

### Dashboard features

- **Field boundary map** — single polygon with satellite basemap
- **Weather charts (4 panels)** — GDD, rainfall, cumulative GDD, cumulative rainfall
- **NDVI time-series** — one trace per year, Sentinel circles vs Landsat triangles, cloud-cover sized markers
- **Year filter buttons** — toggle individual years on the NDVI chart
- **Embedded composite images** — corn peak-95 NDVI map, cumulative season NDVI (base64 PNGs)
- **Crop history table** — year, crop, scene count, peak NDVI
- **Colorblind-safe** — years differentiated by color, satellites by marker shape

## Combined Multi-Field Dashboard

Generate a single dashboard that compares two or more fields side by side.
The combined dashboard overlays every field boundary on one satellite map,
shows per-field weather and NDVI panels, builds report cards, computes
fertilizer/lime recommendations from soil samples, and renders spread-rate
maps. A **data-computed Analysis Summary panel** grades each field (overall,
soil, pH, vigor, weather), reports year-over-year NDVI trends and heat-stress
days, lists the top non-optimal soil variables, and summarizes recommended
actions.

### Standalone CLI

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/field_dashboard_cli.py field-dashboard generate \
  --grower-slug central-ne-grower \
  --farm-slug central-ne-grower-nebraska \
  --field-ids osm-554305501,osm-554305653
```

Use `--field-id <field-id>` instead of `--field-ids` to build a single-field
dashboard; use a comma-separated list of two or more field IDs for the
combined dashboard.

### Output

`growers/<grower>/farms/<farm>/derived/dashboards/combined_fields_dashboard.html`

### Combined dashboard features

- **Overlay map** — all selected field boundaries on one satellite basemap
- **Per-field weather + NDVI panels** — one row of charts per field
- **Report cards** — per-field grading from soil and weather data
- **Fertilizer / lime recommendations** — derived from soil sample sufficiency ranges
- **Spread-rate maps** — recommended application rates overlaid on each field
- **Analysis Summary panel** — computed grades, NDVI trends, top variables, actions

## Field Grid Generation

Soil-sample and NDVI analysis maps are built on a ~90 m analysis grid per
field. These helper scripts generate and enrich that grid; the dashboard CLI
calls them automatically, so running them directly is optional.

```bash
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
# 1. Build the analysis grid for a field
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/generate_field_grid.py \
  --grower-slug central-ne-grower \
  --farm-slug central-ne-grower-nebraska \
  --field-id osm-554305501

# 2. Attach soil sample values (CEC, pH, P, K, Zn, S, NO3, ...) to each grid cell
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/enrich_grid_with_soil.py \
  --grower-slug central-ne-grower \
  --farm-slug central-ne-grower-nebraska \
  --field-id osm-554305501

# 3. Pull annual NDVI summaries onto the grid
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/extract_year_grids.py \
  --grower-slug central-ne-grower \
  --farm-slug central-ne-grower-nebraska \
  --field-id osm-554305501
```

- Grid + soil enrichment logic: `scripts/lib/field_grid_generator.py`
- Sufficiency rating and recommendation logic: `scripts/lib/sufficiency_ranges.py`
- Soil sample inputs are `.xlsx` workbooks read with `pandas.read_excel` (requires `openpyxl`, see [Requirements](#requirements-dependencies))

## Assignment 3: Yearly Grower Dashboard

A single-field agronomic dashboard that aligns NDVI, precipitation, temperature, and cumulative GDD on a shared time axis for the most recent year.

### Inputs

- `growers/<grower>/farms/<farm>/fields/<field>/weather/daily_weather.csv` — NASA POWER daily weather (2021–2025)
- `growers/<grower>/farms/<farm>/fields/<field>/satellite/sentinel/manifest.json` — Sentinel-2 scene inventory
- `growers/<grower>/farms/<farm>/boundary/field_boundaries.geojson` — field polygon

### Weather metrics

- Growing Degree Days (GDD): `max((T2M_MAX + T2M_MIN)/2 - 10°C, 0)`, cumulative from last frost
- Heat stress: degrees above 86°F
- Last frost: latest pre-July 1 day with T2M_MIN ≤ 0°C
- Rainfall, solar radiation, wind speed (mph from WS10M)

### Generated outputs

- Interactive HTML: `fields/<field-id>/derived/dashboards/<field-id>_dashboard.html`
- Multi-panel PNG (2025-only): `fields/<field-id>/derived/dashboards/<field-id>_multi_panel_2025.png`
  - Generated at runtime by `scripts/lib/multi_panel_dashboard.py` (not committed to repo per asset policy)

### Rerun the workflow

```bash
export DATA_PIPELINE_DATA_ROOT=/absolute/path/to/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/field_dashboard_cli.py field-dashboard generate \
  --grower-slug central-ne-grower \
  --farm-slug central-ne-grower-nebraska \
  --field-id osm-554305501
```

### Known limitations

- NDVI cloud masking requires Sentinel-2 SCL band; scenes without SCL fall back to metadata cloud cover only
- Temporal anomaly detection needs ≥4 clear-sky scenes per year
- Cool-period detection needs a pre-computed DOY temperature baseline (auto-generated from county parquet on first run)
- Growth stages are corn-specific; soybean stages available via `crop` parameter in `detect_critical_events()`

## Requirements / Dependencies

Python dependencies are pinned in [`requirements.txt`](requirements.txt) and
installed into the runtime venv on first install (`./scripts/install.sh`).
Key groups:

- **Core data stack** — `pandas`, `numpy`, `scipy`, `scikit-learn`, `tqdm`
- **Geospatial** — `geopandas`, `fiona`, `pyproj`, `shapely`, `rasterio`, `rasterstats`
- **Storage / interchange** — `pyarrow`, `xarray`, `zarr`, `fsspec`
- **Plotting / reporting** — `matplotlib`, `seaborn`, `contextily`, `plotly`, `pillow`
- **General utilities** — `requests`, `beautifulsoup4`, `python-dotenv`
- **Excel soil inputs** — `openpyxl` (required to read `.xlsx` soil sample
  workbooks and `sufficiency ranges` via `pandas.read_excel` in
  `scripts/lib/sufficiency_ranges.py`)

Install into an existing runtime venv after adding new requirements:

```bash
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" -m pip install -r requirements.txt
```

Large raw datasets (raster tiles, downloaded payloads, generated dashboards,
and derived tables) are **not** committed to this repository. They live in the
runtime tree under `DATA_PIPELINE_DATA_ROOT` per the [Runtime
contract](AGENTS.md#runtime-contract) and must stay out of Git.
