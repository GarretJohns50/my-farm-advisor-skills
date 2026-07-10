"""Smoke tests for the single-field NDVI dashboard generator.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import sys
scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from lib.field_dashboard_generator import generate_field_dashboard


def _runtime_base() -> Path:
    env = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if env:
        return Path(env) / "data-pipeline"
    home = Path.home()
    candidates = list(home.rglob("my-farm-advisor-runtime/data-pipeline"))
    if candidates:
        return candidates[0]
    pytest.skip("No runtime data-pipeline root found.")


def _ne_farm_dir() -> Path:
    base = _runtime_base()
    farm = base / "growers" / "central-ne-grower" / "farms" / "central-ne-grower-nebraska"
    if not farm.exists():
        pytest.skip("NE fixture not found.")
    return farm


def test_generate_field_dashboard(tmp_path: Path) -> None:
    farm_dir = _ne_farm_dir()
    output = tmp_path / "test_field_dashboard.html"
    result = generate_field_dashboard(
        farm_dir_path=farm_dir,
        field_id="osm-554305501",
        output_path=output,
        no_basemap=True,
    )
    assert result.exists()
    assert result.stat().st_size > 500_000
    text = result.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in text
    assert "NDVI Dashboard" in text
    assert "NDVI Time Series" in text


def test_composite_images_embedded(tmp_path: Path) -> None:
    farm_dir = _ne_farm_dir()
    output = tmp_path / "test_field_composites.html"
    generate_field_dashboard(
        farm_dir_path=farm_dir,
        field_id="osm-554305501",
        output_path=output,
        no_basemap=True,
    )
    text = output.read_text(encoding="utf-8")
    assert "data:image/png;base64" in text


def test_crop_history_table(tmp_path: Path) -> None:
    farm_dir = _ne_farm_dir()
    output = tmp_path / "test_field_crop.html"
    generate_field_dashboard(
        farm_dir_path=farm_dir,
        field_id="osm-554305501",
        output_path=output,
        no_basemap=True,
    )
    text = output.read_text(encoding="utf-8")
    assert "Crop History" in text
    assert "<table" in text


def test_ndvi_data_present(tmp_path: Path) -> None:
    farm_dir = _ne_farm_dir()
    output = tmp_path / "test_field_ndvi.html"
    generate_field_dashboard(
        farm_dir_path=farm_dir,
        field_id="osm-554305501",
        output_path=output,
        no_basemap=True,
    )
    text = output.read_text(encoding="utf-8")
    assert "ndvi-chart" in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
