# grower-web-map Local Instructions

## Purpose

Generate a self-contained interactive Leaflet HTML map for one grower, showing all field polygon boundaries across all farms.

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly asks for a broader skill change.

## Read nearby docs first

Read `SKILL.md` for routing, then `GUIDE.md` for the usage walkthrough. Read `../AGENTS.md` for data-pipeline runtime conventions.

## Runtime contract

- `DATA_PIPELINE_DATA_ROOT` must be exported and point to an absolute writable path.
- The script lives in the runtime copy: `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src/scripts/reporting/generate_grower_map.py`
- Output goes to: `${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers/<grower-slug>/derived/dashboards/grower_web_map.html`
- No additional Python dependencies required beyond what the pipeline venv already provides (geopandas, shapely).

## Command runbook

Generate map for a single grower:

```bash
export DATA_PIPELINE_DATA_ROOT=~/my-farm-advisor-runtime
cd "${DATA_PIPELINE_DATA_ROOT}/data-pipeline/src"
"${DATA_PIPELINE_DATA_ROOT}/data-pipeline/.venv/bin/python" \
  scripts/reporting/generate_grower_map.py \
  --grower-slug <grower-slug>
```

Re-seed the runtime after editing the checkout source:

```bash
rsync -r --no-times --checksum \
  ~/.config/opencode/skills/my-farm-advisor/data-pipeline/src/ \
  ~/my-farm-advisor-runtime/data-pipeline/src/
```

## Local-delta-only reminder

This AGENTS.md only records instructions that differ from the parent data-pipeline AGENTS.md.
