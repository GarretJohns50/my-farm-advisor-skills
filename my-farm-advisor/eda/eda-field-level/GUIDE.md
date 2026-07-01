---
name: eda-field-level
description: Field-level exploratory data analysis comparing boundaries, CDL/cropland, and weather across fields within a farm and across growers.
version: 1.0.0
author: Boreal Bytes
tags: [field-level, boundaries, CDL, weather, comparison, EDA]
---

# Workflow: eda-field-level

## Description

This workflow generates static Python visualizations and comparison tables for field-level EDA. It operates on three data layers:

- **Boundaries** — field polygons, area (acres), and circularity (pivot detection)
- **CDL / Cropland** — NASS Cropland Data Layer crop history per field, 2021–2025
- **Weather** — NASA POWER daily weather per field centroid, 2021–2025

## When to Use This Workflow

- You need field-level comparison visualizations (not farm-level aggregates)
- You want to understand intra-farm variability in weather, crop rotation, or field geometry
- You need cross-grower comparisons at the field level
- You want static PNG/CSV outputs suitable for embedding in reports

## Prerequisites

```bash
pip install pandas geopandas matplotlib seaborn numpy scipy
```

## Quick Start

```bash
# Per-farm analysis
python scripts/eda/eda_field_level.py \
  --grower-slug central-il-grower \
  --farm-slug central-il-grower-illinois

# With cross-grower comparison
python scripts/eda/eda_field_level.py \
  --grower-slug central-il-grower \
  --farm-slug central-il-grower-illinois \
  --cross-grower
```

## Output Structure

Per farm:
```
 growers/<grower>/farms/<farm>/derived/eda_field_level/
├── A1_boundary_size_distribution.png
├── A2_boundary_shape_scatter.png
├── A3_boundary_correlation.csv
├── B1_cdl_dominant_heatmap.png
├── B2_cdl_diversity_index.png
├── B3_cdl_rotation_matrix.csv
├── C1_weather_temporal_trends.png
├── C2_weather_interfield_spread.png
├── C3_weather_cv_analysis.csv
└── M1_field_boundary_map.png
```

Cross-grower (when `--cross-grower`):
```
growers/<grower>/farms/<farm>/derived/eda_field_level/
├── X1_cross_boundary_sizes.png
├── X2_cross_cdl_mix.png
└── X3_cross_weather_cv.png
```

## Storytelling Guide

### Category A: Field Boundaries

**A1 — Size Distribution**
Story: "How heterogeneous are our field sizes?" A right-skewed box plot with outliers says the farm has a few very large fields that may need different equipment or management.

**A2 — Shape Scatter**
Story: "Do larger fields tend to be center-pivot circles?" A positive correlation between area and circularity suggests the farm expanded by adding round pivot-irrigated blocks.

**A3 — Correlation CSV**
Story: "How strong is the size-shape link?" Pearson r + p-value quantifies the relationship for reporting.

**M1 — Geospatial Boundary Map**
Story: "Where are our fields and how large is each one?" A choropleth map with field polygons colored by area and labeled by field ID. Shows spatial clustering (e.g., large fields grouped together) and gaps between parcels. This is the only true geospatial output — everything else is statistical.

### Category B: CDL / Cropland

**B1 — Dominant Crop Heatmap**
Story: "What is our rotation strategy?" Horizontal bands (same crop every year) = monoculture; diagonal stripes = strict corn-soy rotation; checkerboards = mixed strategy.

**B2 — Diversity Index**
Story: "Which fields are monoculture vs. mixed-use?" High Shannon diversity = cover crops, grass strips, or inconsistent planting. Low diversity = pure corn or pure soy.

**B3 — Rotation Matrix**
Story: "How faithful is our rotation?" P(corn→soy) near 1.0 = strict rotation. P(corn→corn) > 0.3 = continuous corn on some fields.

### Category C: Weather

**C1 — Temporal Trends**
Story: "Are our fields warming or drying over time?" Parallel GDD curves that rise together = uniform climate exposure. Diverging curves = some fields are in microclimates.

**C2 — Inter-Field Spread**
Story: "How patchy is our farm's weather?" A wide box/violin spread means management zones should differ. Narrow spread means uniform practices work everywhere.

**C3 — CV Analysis**
Story: "Is our farm's climate getting more or less uniform?" Rising CV across years = increasing microclimate fragmentation. Falling CV = convergence.

### Cross-Grower (X)

**X1 — Boundary Sizes**
Story: "How does our field-size profile compare to peers?" Different medians = different equipment scales or land-tenure histories.

**X2 — CDL Mix**
Story: "Do regions differ in crop preference?" A grower with 90% corn may be continuous-corn dominant; one with 50/50 may rotate strictly.

**X3 — Weather CV**
Story: "Which region has the most uniform field-level weather?" Low CV = flat, uniform terrain. High CV = variable topography or mixed irrigation.

## Resources

- [NASS CDL](https://www.nass.usda.gov/Research_and_Science/Cropland/SARS1a.php)
- [NASA POWER](https://power.larc.nasa.gov/)
- [Shannon Diversity Index](https://en.wikipedia.org/wiki/Diversity_index)
- [Coefficient of Variation](https://en.wikipedia.org/wiki/Coefficient_of_variation)
