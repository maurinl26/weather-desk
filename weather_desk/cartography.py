"""Projection transforms and bundled low-resolution land outlines for the live maps."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
from pyproj import CRS, Transformer
from shapely.geometry import box, shape

from weather_desk.analysis import EARTH_RADIUS, to_mercator
from weather_desk.data import BBOX, DOMAIN, IMAGE_SIZE

PROJECTIONS = {
    "mercator": ("Mercator", "EPSG:3857"),
    "lambert": (
        "Lambert Europe",
        "+proj=lcc +lat_1=35 +lat_2=65 +lat_0=52 +lon_0=10 +datum=WGS84 +units=m +no_defs",
    ),
    "stereopolar": (
        "Stéréopolaire nord",
        "+proj=stere +lat_0=90 +lat_ts=70 +lon_0=0 +datum=WGS84 +units=m +no_defs",
    ),
}
SOURCE_CRS = "EPSG:3857"
LAND_FILE = Path(__file__).with_name("assets") / "ne_110m_land.geojson"


@lru_cache(maxsize=6)
def transformer(projection: str, inverse: bool = False):
    if projection not in PROJECTIONS:
        raise ValueError("Projection cartographique inconnue.")
    target = PROJECTIONS[projection][1]
    source, destination = (target, SOURCE_CRS) if inverse else (SOURCE_CRS, target)
    return Transformer.from_crs(
        CRS.from_user_input(source), CRS.from_user_input(destination), always_xy=True
    ).transform


@lru_cache(maxsize=3)
def domain_bounds(projection: str) -> tuple[float, float, float, float]:
    west, south, east, north = DOMAIN
    edge = np.linspace(0, 1, 181)
    longitude = np.concatenate(
        [
            west + (east - west) * edge,
            np.full_like(edge, east),
            east - (east - west) * edge,
            np.full_like(edge, west),
        ]
    )
    latitude = np.concatenate(
        [
            np.full_like(edge, south),
            south + (north - south) * edge,
            np.full_like(edge, north),
            north - (north - south) * edge,
        ]
    )
    x, y = transformer(projection)(*_lonlat_to_mercator(longitude, latitude))
    valid = np.isfinite(x) & np.isfinite(y)
    return (
        float(np.min(x[valid])),
        float(np.min(y[valid])),
        float(np.max(x[valid])),
        float(np.max(y[valid])),
    )


def _lonlat_to_mercator(longitude, latitude):
    longitude = np.asarray(longitude, dtype=float)
    latitude = np.clip(np.asarray(latitude, dtype=float), -85.05112878, 85.05112878)
    return (
        EARTH_RADIUS * np.deg2rad(longitude),
        EARTH_RADIUS * np.log(np.tan(np.pi / 4 + np.deg2rad(latitude) / 2)),
    )


def project_xy(x, y, projection: str):
    return transformer(projection)(x, y)


def reproject_lines(
    xs: list[list[float]], ys: list[list[float]], projection: str
) -> tuple[list, list]:
    projected_x, projected_y = [], []
    convert = transformer(projection)
    for line_x, line_y in zip(xs, ys, strict=True):
        x, y = convert(np.asarray(line_x), np.asarray(line_y))
        valid = np.isfinite(x) & np.isfinite(y)
        if valid.sum() >= 2:
            projected_x.append(np.asarray(x)[valid].tolist())
            projected_y.append(np.asarray(y)[valid].tolist())
    return projected_x, projected_y


@lru_cache(maxsize=1)
def land_lonlat() -> tuple[tuple[tuple[float, ...], tuple[float, ...]], ...]:
    document = json.loads(LAND_FILE.read_text(encoding="utf-8"))
    lines = []
    for feature in document["features"]:
        geometry = feature["geometry"]
        polygons = (
            [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        )
        for polygon in polygons:
            for ring in polygon[:1]:
                segment_x, segment_y = [], []
                for longitude, latitude, *_ in ring:
                    if latitude < 20:
                        if len(segment_x) > 1:
                            lines.append((tuple(segment_x), tuple(segment_y)))
                        segment_x, segment_y = [], []
                        continue
                    if segment_x and abs(longitude - segment_x[-1]) > 180:
                        if len(segment_x) > 1:
                            lines.append((tuple(segment_x), tuple(segment_y)))
                        segment_x, segment_y = [], []
                    segment_x.append(longitude)
                    segment_y.append(latitude)
                if len(segment_x) > 1:
                    lines.append((tuple(segment_x), tuple(segment_y)))
    return tuple(lines)


@lru_cache(maxsize=3)
def land_lines(projection: str) -> tuple[list[list[float]], list[list[float]]]:
    convert = transformer(projection)
    xs, ys = [], []
    for longitude, latitude in land_lonlat():
        x, y = convert(*_lonlat_to_mercator(longitude, latitude))
        valid = np.isfinite(x) & np.isfinite(y)
        if valid.sum() >= 2:
            xs.append(np.asarray(x)[valid].tolist())
            ys.append(np.asarray(y)[valid].tolist())
    return xs, ys


@lru_cache(maxsize=3)
def land_polygons(projection: str) -> tuple[list[list[float]], list[list[float]]]:
    """Clip land to the displayed region, then project its exterior rings."""
    document = json.loads(LAND_FILE.read_text(encoding="utf-8"))
    convert = transformer(projection)
    west, south, east, north = DOMAIN
    region = box(west, south, east, north)
    xs, ys = [], []
    for feature in document["features"]:
        # Projection transforms straight lon/lat clip edges into curves. Add
        # vertices before projection to avoid long artificial chords.
        clipped = shape(feature["geometry"]).intersection(region).segmentize(0.25)
        polygons = (
            [clipped]
            if clipped.geom_type == "Polygon"
            else [
                geometry
                for geometry in getattr(clipped, "geoms", ())
                if geometry.geom_type == "Polygon"
            ]
        )
        for polygon in polygons:
            ring = polygon.exterior.coords
            longitude = np.asarray([point[0] for point in ring])
            latitude = np.asarray([point[1] for point in ring])
            x, y = convert(*_lonlat_to_mercator(longitude, latitude))
            valid = np.isfinite(x) & np.isfinite(y)
            if valid.sum() >= 3:
                xs.append(np.asarray(x)[valid].tolist())
                ys.append(np.asarray(y)[valid].tolist())
    return xs, ys


@lru_cache(maxsize=3)
def _warp_plan(projection: str):
    xmin, ymin, xmax, ymax = domain_bounds(projection)
    width, height = IMAGE_SIZE
    grid_x, grid_y = np.meshgrid(np.linspace(xmin, xmax, width), np.linspace(ymin, ymax, height))
    source_x, source_y = transformer(projection, inverse=True)(grid_x, grid_y)
    x0, y0 = to_mercator(DOMAIN[0], DOMAIN[1])
    x1, y1 = to_mercator(DOMAIN[2], DOMAIN[3])
    source_col = (source_x - x0) / (x1 - x0) * (width - 1)
    source_row = (source_y - y0) / (y1 - y0) * (height - 1)
    valid = (
        np.isfinite(source_x)
        & np.isfinite(source_y)
        & (source_col >= 0)
        & (source_col < width)
        & (source_row >= 0)
        & (source_row < height)
    )
    src_col = np.clip(np.nan_to_num(np.rint(source_col)), 0, width - 1).astype(np.uint16)
    src_row = np.clip(np.nan_to_num(np.rint(source_row)), 0, height - 1).astype(np.uint16)
    return src_row, src_col, valid, (xmin, ymin, xmax - xmin, ymax - ymin)


def warp_rgba(
    image: np.ndarray, projection: str
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """Reproject a WMS raster onto a regular grid suitable for Bokeh image_rgba."""
    src_row, src_col, valid, bounds = _warp_plan(projection)
    result = image[src_row, src_col].copy()
    result[~valid] = 0
    return result, bounds


def warp_rgba_rasterio(
    image: np.ndarray, projection: str
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """Prototype Rasterio reprojection for comparison with the production NumPy warp."""
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject

    height, width = image.shape
    west, south, east, north = BBOX
    source = np.moveaxis(np.flipud(image).view(np.uint8).reshape(height, width, 4), -1, 0).copy()
    xmin, ymin, xmax, ymax = domain_bounds(projection)
    destination = np.zeros((4, height, width), dtype=np.uint8)
    reproject(
        source=source,
        destination=destination,
        src_transform=from_bounds(west, south, east, north, width, height),
        src_crs=SOURCE_CRS,
        src_alpha=4,
        dst_transform=from_bounds(xmin, ymin, xmax, ymax, width, height),
        dst_crs=PROJECTIONS[projection][1],
        dst_alpha=4,
        dst_nodata=0,
        resampling=Resampling.bilinear,
        num_threads=1,
    )
    rgba = np.ascontiguousarray(np.moveaxis(np.flip(destination, axis=1), 0, -1))
    return rgba.view(np.uint32).reshape(height, width), (xmin, ymin, xmax - xmin, ymax - ymin)
