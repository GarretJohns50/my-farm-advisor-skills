"""Basemap tile acquisition for the weather dashboard.

Downloads Esri World Imagery tiles, stitches them with Pillow,
crops to farm extent, and returns a base64-encoded PNG.
"""

from __future__ import annotations

import base64
import hashlib
import io
import math
from pathlib import Path

import geopandas as gpd
import requests
from PIL import Image


def _lon_to_mercator_x(lon: float) -> float:
    return math.radians(lon) * 6378137.0


def _lat_to_mercator_y(lat: float) -> float:
    return math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)) * 6378137.0


def _mercator_x_to_lon(x: float) -> float:
    return math.degrees(x / 6378137.0)


def _mercator_y_to_lat(y: float) -> float:
    return math.degrees(2.0 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2.0)


def _get_tile_url(z: int, x: int, y: int) -> str:
    return f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"


def _compute_extent(gdf: gpd.GeoDataFrame, buffer_ratio: float = 0.15) -> tuple[float, float, float, float]:
    """Compute Mercator extent with buffer. Returns (min_x, min_y, max_x, max_y)."""
    bounds = gdf.total_bounds  # (min_lon, min_lat, max_lon, max_lat)
    min_x = _lon_to_mercator_x(bounds[0])
    min_y = _lat_to_mercator_y(bounds[1])
    max_x = _lon_to_mercator_x(bounds[2])
    max_y = _lat_to_mercator_y(bounds[3])

    width = max_x - min_x
    height = max_y - min_y
    buf_x = width * buffer_ratio
    buf_y = height * buffer_ratio

    return (min_x - buf_x, min_y - buf_y, max_x + buf_x, max_y + buf_y)


def _choose_zoom(extent: tuple[float, float, float, float], target_width_px: int = 1500) -> int:
    """Choose zoom level so the stitched image is approximately target_width_px wide."""
    min_x, min_y, max_x, max_y = extent
    width_m = max_x - min_x
    # Tile size is 256px. At zoom z, 1 tile covers earth_circumference / 2^z meters
    earth_circumference = 2 * math.pi * 6378137.0
    for z in range(20, 0, -1):
        tile_m = earth_circumference / (2 ** z)
        n_tiles_x = math.ceil(width_m / tile_m)
        px_width = n_tiles_x * 256
        if px_width <= target_width_px * 1.5 and n_tiles_x <= 20:
            return z
    return 10


def _download_tile(z: int, x: int, y: int, timeout: int = 15) -> Image.Image | None:
    url = _get_tile_url(z, x, y)
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception:
        pass
    return None


def _compute_tiles(z: int, extent: tuple[float, float, float, float]) -> tuple[list[tuple[int, int]], int, int]:
    """Compute tile indices covering the extent. Returns (list of (x,y), offset_x, offset_y)."""
    min_x, min_y, max_x, max_y = extent
    n = 2 ** z
    tile_size = (2 * math.pi * 6378137.0) / n

    def _meters_to_tile(mx: float, my: float) -> tuple[int, int]:
        tx = int((mx + math.pi * 6378137.0) / tile_size)
        ty = int((math.pi * 6378137.0 - my) / tile_size)
        return tx, ty

    min_tx, max_ty = _meters_to_tile(min_x, min_y)
    max_tx, min_ty = _meters_to_tile(max_x, max_y)

    tiles = []
    for ty in range(min_ty, max_ty + 1):
        for tx in range(min_tx, max_tx + 1):
            tiles.append((tx, ty))

    return tiles, min_tx, min_ty


def fetch_basemap(
    gdf: gpd.GeoDataFrame,
    cache_dir: Path,
    no_basemap: bool = False,
    force_refresh: bool = False,
) -> tuple[str | None, tuple[float, float, float, float]]:
    """Download and stitch basemap tiles.

    Returns (base64_png_string_or_None, mercator_extent).
    """
    extent = _compute_extent(gdf)

    if no_basemap:
        return None, extent

    # Cache key
    extent_hash = hashlib.md5(
        f"{extent[0]:.2f}_{extent[1]:.2f}_{extent[2]:.2f}_{extent[3]:.2f}".encode()
    ).hexdigest()[:12]
    cache_path = cache_dir / f"basemap_{extent_hash}.png"

    if not force_refresh and cache_path.exists():
        with open(cache_path, "rb") as f:
            data = f.read()
        return f"data:image/png;base64,{base64.b64encode(data).decode()}", extent

    z = _choose_zoom(extent)
    tiles, min_tx, min_ty = _compute_tiles(z, extent)

    if len(tiles) > 100:
        # Too many tiles — skip basemap
        return None, extent

    # Download tiles
    tile_images: dict[tuple[int, int], Image.Image] = {}
    for tx, ty in tiles:
        img = _download_tile(z, tx, ty)
        if img:
            tile_images[(tx, ty)] = img

    if not tile_images:
        return None, extent

    # Stitch
    tile_size = 256
    n_tx = max(t[0] for t in tile_images) - min_tx + 1
    n_ty = max(t[1] for t in tile_images) - min_ty + 1
    stitched = Image.new("RGB", (n_tx * tile_size, n_ty * tile_size))

    for (tx, ty), img in tile_images.items():
        px = (tx - min_tx) * tile_size
        py = (ty - min_ty) * tile_size
        stitched.paste(img, (px, py))

    # Crop to exact extent
    earth_circumference = 2 * math.pi * 6378137.0
    tile_earth_m = earth_circumference / (2 ** z)
    total_width_px = n_tx * tile_size
    total_height_px = n_ty * tile_size

    # Pixel coordinates of extent within stitched image
    # Mercator y increases northward; image y increases southward
    x_west = min_tx * tile_earth_m - math.pi * 6378137.0
    y_north = math.pi * 6378137.0 - min_ty * tile_earth_m

    left = int((extent[0] - x_west) / tile_earth_m * tile_size)
    top = int((y_north - extent[3]) / tile_earth_m * tile_size)
    right = int((extent[2] - x_west) / tile_earth_m * tile_size)
    bottom = int((y_north - extent[1]) / tile_earth_m * tile_size)

    # Clamp
    left = max(0, min(left, total_width_px))
    top = max(0, min(top, total_height_px))
    right = max(0, min(right, total_width_px))
    bottom = max(0, min(bottom, total_height_px))

    if right > left and bottom > top:
        cropped = stitched.crop((left, top, right, bottom))
    else:
        return None, extent

    # Save to cache
    cache_dir.mkdir(parents=True, exist_ok=True)
    cropped.save(cache_path, "PNG")

    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}", extent
