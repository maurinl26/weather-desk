import numpy as np
import pytest

from weather_desk.analysis import to_lonlat, to_mercator
from weather_desk.cartography import (
    PROJECTIONS,
    domain_bounds,
    land_lines,
    project_xy,
    reproject_lines,
    transformer,
    warp_rgba,
)
from weather_desk.data import IMAGE_SIZE
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


def test_satellite_raster_reprojection_keeps_pixels_and_masks_outside_domain():
    image = np.full((IMAGE_SIZE[1], IMAGE_SIZE[0]), 0xFF123456, dtype=np.uint32)

    mercator, mercator_bounds = warp_rgba(image, "mercator")
    lambert, lambert_bounds = warp_rgba(image, "lambert")

    assert mercator_bounds[2] > 0 and mercator_bounds[3] > 0
    assert np.all(mercator == image)
    assert lambert_bounds != mercator_bounds
    assert np.count_nonzero(lambert) < lambert.size
    assert np.count_nonzero(lambert) > 0


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
