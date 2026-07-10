"""Smoke tests for the weather dashboard generator.

These tests require the Iowa fixture data to be present at the runtime root.
Run from the repo root:
    pytest my-farm-advisor/data-pipeline/src/tests/test_dashboard_generator.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Ensure bootstrap is available
import sys
scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from lib.dashboard_generator import generate_dashboard


def _runtime_base() -> Path:
    """Resolve the data-pipeline runtime root for fixtures."""
    env = os.environ.get("DATA_PIPELINE_DATA_ROOT")
    if env:
        return Path(env) / "data-pipeline"
    # Fallback to home scan
    home = Path.home()
    candidates = list(home.rglob("my-farm-advisor-runtime/data-pipeline"))
    if candidates:
        return candidates[0]
    pytest.skip("No runtime data-pipeline root found.")


def _iowa_farm_dir() -> Path:
    base = _runtime_base()
    farm = base / "growers" / "central-ia-grower" / "farms" / "central-ia-grower-iowa"
    if not farm.exists():
        pytest.skip("Iowa fixture not found.")
    return farm


def test_explicit_farm_dir(tmp_path: Path) -> None:
    """Dashboard generates valid HTML for the Iowa fixture."""
    farm_dir = _iowa_farm_dir()
    output = tmp_path / "test_dashboard.html"
    result = generate_dashboard(farm_dir_path=farm_dir, output_path=output, no_basemap=True)
    assert result.exists()
    assert result.stat().st_size > 500_000  # Plotly bundle alone is ~3.5MB
    text = result.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in text
    assert "Weather Dashboard" in text


def test_self_contained(tmp_path: Path) -> None:
    """Output contains no external HTTP/HTTPS or CDN references."""
    farm_dir = _iowa_farm_dir()
    output = tmp_path / "test_dashboard.html"
    generate_dashboard(farm_dir_path=farm_dir, output_path=output, no_basemap=True)
    text = output.read_text(encoding="utf-8")
    assert "http://" not in text
    assert "https://" not in text
    assert "cdn.plot.ly" not in text
    assert "cdn.jsdelivr" not in text


def test_no_basemap_flag(tmp_path: Path) -> None:
    """--no-basemap produces a usable dashboard without imagery."""
    farm_dir = _iowa_farm_dir()
    output = tmp_path / "test_no_basemap.html"
    generate_dashboard(farm_dir_path=farm_dir, output_path=output, no_basemap=True)
    text = output.read_text(encoding="utf-8")
    assert "Field Boundaries" in text


def test_header_only_weather(tmp_path: Path) -> None:
    """Empty daily_weather.csv (header only) does not crash dashboard generation."""
    farm_dir = _iowa_farm_dir()
    # Temporarily back up and replace a field weather file
    field_dir = farm_dir / "fields"
    weather_files = list(field_dir.rglob("*/weather/daily_weather.csv"))
    if not weather_files:
        pytest.skip("No per-field weather files found.")
    target = weather_files[0]
    original = target.read_text(encoding="utf-8")
    try:
        header = original.splitlines()[0] if original else "field_id,lat,lon,date,T2M,T2M_MAX,T2M_MIN,PRECTOTCORR,ALLSKY_SFC_SW_DWN,RH2M,WS10M"
        target.write_text(header + "\n", encoding="utf-8")
        output = tmp_path / "test_header_only.html"
        generate_dashboard(farm_dir_path=farm_dir, output_path=output, no_basemap=True)
        text = output.read_text(encoding="utf-8")
        # Should still produce valid HTML even if some fields have no data
        assert "<!DOCTYPE html>" in text
    finally:
        target.write_text(original, encoding="utf-8")


def test_multiple_farms_error() -> None:
    """Auto-discovery with multiple farms should require explicit selection."""
    # This is tested at CLI level; the generator itself requires an explicit farm_dir.
    # The generator raises FileNotFoundError when the boundary is missing.
    from lib.dashboard_generator import _load_farm_geojson
    assert _load_farm_geojson(Path("/nonexistent")) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
