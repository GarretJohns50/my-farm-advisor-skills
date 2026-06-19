---
name: grower-web-map
description: Generate self-contained interactive Leaflet web maps for individual growers using pipeline field-boundary GeoJSON.
version: 1.0.0
author: My Farm Advisor / Data Pipeline
tags: [web-map, leaflet, grower, visualization, geospatial, dashboard]
---

# grower-web-map

Generate a lightweight interactive HTML field map for a single grower.

## When to use

- After the data pipeline has created a grower with fields
- You want a browser-ready field map showing all farms under one grower

## How to use

```bash
export DATA_PIPELINE_DATA_ROOT=~/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_map.py \
  --grower-slug ia-grower
```

Output: `growers/<slug>/derived/dashboards/grower_web_map.html`

## Start here

Read `GUIDE.md` for the full walkthrough. Read `AGENTS.md` for agent instructions.
