import numpy as np
import pytest

from weather_desk.analysis import to_lonlat, to_mercator
from weather_desk.cartography import (
    PROJECTIONS,
    domain_bounds,
    land_lines,
    land_polygons,
    project_xy,
    reproject_lines,
    transformer,
    warp_rgba,
)
from weather_desk.data import BBOX, IMAGE_SIZE
from weather_desk.ui import WeatherDesk
from weather_desk.workspace import (
    SQLiteWorkspaceRepository,
    WorkspaceError,
    WorkspaceService,
)


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_projection_round_trip_and_domain_bounds(projection):
    x, y = project_xy(*to_mercator(2.0, 48.0), projection)
    longitude, latitude = to_lonlat(*transformer(projection, inverse=True)(x, y))
    x0, y0, x1, y1 = domain_bounds(projection)

    assert longitude == pytest.approx(2.0)
    assert latitude == pytest.approx(48.0)
    assert x0 < x < x1
    assert y0 < y < y1


def test_land_outlines_are_available_in_each_projection():
    for projection in PROJECTIONS:
        xs, ys = land_lines(projection)
        assert len(xs) > 50
        assert len(xs) == len(ys)
        assert all(len(x) == len(y) and len(x) > 1 for x, y in zip(xs, ys, strict=True))


def test_land_fills_clip_large_continent_geometry_to_the_display_region():
    for projection in PROJECTIONS:
        xs, ys = land_polygons(projection)
        assert len(xs) > 10
        assert len(xs) == len(ys)
        assert all(
            np.isfinite(x).all() and np.isfinite(y).all() for x, y in zip(xs, ys, strict=True)
        )
        assert (
            max(float(np.hypot(np.diff(x), np.diff(y)).max()) for x, y in zip(xs, ys, strict=True))
            < 200_000
        )
        has_north_african_coast = False
        inverse = transformer(projection, inverse=True)
        for x, y in zip(xs, ys, strict=True):
            mercator_x, mercator_y = inverse(x, y)
            coordinates = [
                to_lonlat(float(point_x), float(point_y))
                for point_x, point_y in zip(mercator_x, mercator_y, strict=True)
            ]
            longitude, latitude = np.asarray(coordinates).T
            if np.any((np.abs(latitude - 25) < 1e-6) & (longitude > -20) & (longitude < 35)):
                has_north_african_coast = True
                break
        assert has_north_african_coast


def test_satellite_raster_reprojection_keeps_pixels_and_masks_outside_domain():
    image = np.full((IMAGE_SIZE[1], IMAGE_SIZE[0]), 0xFF123456, dtype=np.uint32)

    mercator, mercator_bounds = warp_rgba(image, "mercator")
    lambert, lambert_bounds = warp_rgba(image, "lambert")

    assert mercator_bounds[2] > 0 and mercator_bounds[3] > 0
    assert np.all(mercator == image)
    assert lambert_bounds != mercator_bounds
    assert np.count_nonzero(lambert) < lambert.size
    assert np.count_nonzero(lambert) > 0


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_rasterio_prototype_preserves_rgba_orientation_and_masks_domain(projection):
    from weather_desk.cartography import warp_rgba_rasterio

    image = np.full((IMAGE_SIZE[1], IMAGE_SIZE[0]), 0xFFFF0000, dtype=np.uint32)
    output, bounds = warp_rgba_rasterio(image, projection)
    assert output.shape == image.shape
    assert np.isfinite(bounds).all()
    valid = output != 0
    assert valid.any()
    assert np.all(output[valid] == 0xFFFF0000)
    if projection == "mercator":
        assert np.all(valid)
    else:
        assert np.count_nonzero(valid) < valid.size


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_rasterio_prototype_keeps_geographic_pixel_alignment(projection):
    from weather_desk.cartography import warp_rgba_rasterio

    height, width = IMAGE_SIZE[1], IMAGE_SIZE[0]
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[:, :, 0] = np.arange(width, dtype=np.uint16)[None, :] % 251
    rgba[:, :, 1] = np.arange(height, dtype=np.uint16)[:, None] % 251
    rgba[:, :, 2] = 73
    rgba[:, :, 3] = 255
    image = np.ascontiguousarray(rgba).view(np.uint32).reshape(height, width)

    output, (xmin, ymin, span_x, span_y) = warp_rgba_rasterio(image, projection)
    destination_x, destination_y = project_xy(*to_mercator(0, 50), projection)
    column = round((destination_x - xmin) / span_x * width)
    row = round((destination_y - ymin) / span_y * height)
    actual = output.view(np.uint8).reshape(height, width, 4)[row, column]
    source_x, source_y = to_mercator(0, 50)
    source_column = round((source_x - BBOX[0]) / (BBOX[2] - BBOX[0]) * width)
    source_row = round((source_y - BBOX[1]) / (BBOX[3] - BBOX[1]) * height)

    assert int(actual[0]) == pytest.approx(source_column % 251, abs=4)
    assert int(actual[1]) == pytest.approx(source_row % 251, abs=4)
    assert int(actual[2]) == 73
    assert int(actual[3]) == 255


def test_model_lines_are_reprojected_without_losing_levels():
    xs, ys = reproject_lines([[0, 100_000, 200_000]], [[6_000_000] * 3], "lambert")

    assert len(xs) == len(ys) == 1
    assert all(np.isfinite(xs[0]))
    assert all(np.isfinite(ys[0]))


def test_projection_selection_updates_workspace_and_preserves_geojson_drawings(tmp_path):
    service = WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    desk = WeatherDesk(service)
    source = desk.layers["cold_front"]["source"]
    source.data = {"xs": [[0, 100_000]], "ys": [[6_000_000, 6_100_000]]}
    before = desk.snapshot()[1]["features"][0]["geometry"]["coordinates"]

    desk.live.projection_control.value = "lambert"
    desk.sync_workspace()
    after = desk.snapshot()[1]["features"][0]["geometry"]["coordinates"]

    assert desk.workspace_state.projection == "lambert"
    assert np.asarray(before) == pytest.approx(np.asarray(after))
    assert desk.snapshot()[0]["map_view"]["projection"] == "lambert"


def test_invalid_projection_command_is_rejected(tmp_path):
    service = WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    with pytest.raises(WorkspaceError, match="Projection"):
        service.apply(
            [{"op": "set_projection", "projection": "globe"}],
            expected_revision=0,
        )


def test_legacy_workspace_defaults_to_mercator():
    from weather_desk.workspace import WorkspaceState

    payload = WorkspaceState.default().to_dict()
    del payload["projection"]

    assert WorkspaceState.from_dict(payload).projection == "mercator"


def test_projection_command_is_exposed_by_mcp_catalogue(monkeypatch):
    from weather_desk.mcp_server import get_catalogue

    monkeypatch.setattr("weather_desk.mcp_server.DATA.satellite_catalogue", lambda: [])
    monkeypatch.setattr("weather_desk.mcp_server.DATA.latest_run", lambda: "2026-10-02T00:00:00Z")

    assert get_catalogue()["projections"] == list(PROJECTIONS)
