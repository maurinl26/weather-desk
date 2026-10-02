import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from PIL import Image

from weather_desk.analysis import checksum, drawings_geojson, to_lonlat, to_mercator
from weather_desk.ui import WeatherDesk, image_url, normalize_image
from weather_desk.workspace import SQLiteWorkspaceRepository, WorkspaceService


def sample_image():
    buffer = BytesIO()
    Image.new("RGB", (12, 8), "blue").save(buffer, format="JPEG")
    return buffer.getvalue()


def test_projection_preserves_geojson_longitude_latitude_order():
    assert to_lonlat(*to_mercator(2.5, 46.5)) == [2.5, 46.5]


@pytest.mark.parametrize("point", [(float("nan"), 0), (0, float("inf")), (1e20, 0)])
def test_invalid_coordinates_rejected(point):
    with pytest.raises(ValueError):
        to_lonlat(*point)


def test_polygon_closed_and_incomplete_gesture_omitted():
    points = [to_mercator(*p) for p in [(0, 45), (1, 45), (1, 46)]]
    result = drawings_geojson(
        {
            "area": {
                "geometry": "Polygon",
                "data": {"xs": [[p[0] for p in points], [0]], "ys": [[p[1] for p in points], [0]]},
            },
        },
        "2026-09-30T12:00:00Z",
    )
    assert len(result["features"]) == 1
    feature = result["features"][0]
    assert feature["geometry"]["coordinates"] == [[[0, 45], [1, 45], [1, 46], [0, 45]]]
    assert feature["properties"]["valid_time"] == "2026-09-30T12:00:00Z"


def test_session_state_survives_edit_and_is_not_shared():
    desk = WeatherDesk()
    other = WeatherDesk()
    points = [to_mercator(-2, 45), to_mercator(-1, 48)]
    source = desk.layers["cold_front"]["source"]
    source.data = {"xs": [[p[0] for p in points]], "ys": [[p[1] for p in points]]}
    desk.fields["headline"].value = "Un front aborde la côte."
    assert "Un front aborde la côte." in desk.preview.object
    assert len(desk.snapshot()[1]["features"]) == 1
    assert other.snapshot()[1]["features"] == []
    assert desk.analysis_id != other.analysis_id
    desk.fields["valid_time"].value = "2026-10-01T00:00:00Z"
    assert desk.snapshot()[1]["features"][0]["properties"]["valid_time"] == "2026-10-01T00:00:00Z"
    source.data = {"xs": [], "ys": []}
    assert desk.snapshot()[1]["features"] == []


def test_bundle_contains_images_provenance_and_verifiable_checksums():
    desk = WeatherDesk()
    satellite = desk.sources[0]
    satellite.upload.filename = "../../outside.jpg"
    satellite.upload.value = sample_image()
    satellite.origin.value = "Satellite test"
    satellite.valid.value = "2026-09-30T12:00:00Z"
    desk.fields["analysis"].value = "Analyse de test"
    with ZipFile(desk.download("zip")) as archive:
        assert set(archive.namelist()) == {
            "bulletin.md",
            "annotations.geojson",
            "manifest.json",
            "images/satellite.png",
        }
        manifest = json.loads(archive.read("manifest.json"))
        for name, metadata in manifest["files"].items():
            assert checksum(archive.read(name)) == metadata["sha256"]
        src = manifest["sources"][0]
        assert src["source"] == "Satellite test"
        assert src["original_filename"] == "outside.jpg"
        assert src["original_sha256"] == checksum(sample_image())
        assert src["sha256"] == checksum(archive.read(src["uri"]))
        assert "Analyse de test" in archive.read("bulletin.md").decode()
        assert json.loads(archive.read("annotations.geojson"))["type"] == "FeatureCollection"


def test_invalid_image_disables_exports_and_clearing_recovers():
    desk = WeatherDesk()
    desk.sources[0].upload.value = b"not an image"
    assert all(button.disabled for button in desk.downloads)
    with pytest.raises(ValueError):
        desk.download("zip")
    desk.sources[0].upload.value = None
    assert not any(button.disabled for button in desk.downloads)


def test_image_normalization_and_size_limit():
    normalized = normalize_image(sample_image())
    assert Image.open(BytesIO(normalized)).format == "PNG"
    with pytest.raises(ValueError, match="volumineuse"):
        normalize_image(b"0" * (15 * 1024 * 1024 + 1))


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.com/a"]
)
def test_reject_non_image_url_schemes_and_credentials(url):
    with pytest.raises(ValueError):
        image_url(url)


def test_remote_url_is_browser_only_and_not_falsely_checksummed():
    desk = WeatherDesk()
    source = desk.sources[0]
    source.url.value = 'https://example.invalid/image.png?a=" onerror="alert(1)'
    assert source.error is None  # No server-side DNS lookup or fetch.
    assert "&quot;" in source.preview.objects[0].object
    assert source.metadata()["sha256"] is None
    assert source.metadata()["kind"] == "remote_url"
    assert source.metadata()["georeferenced"] is False


def test_bokeh_document_builds():
    from bokeh.core.validation import check_integrity
    from bokeh.document import Document

    desk = WeatherDesk()
    desk.layout.value = "6"
    assert len(desk.map_grid.objects) == 6
    assert desk.map_grid.ncols == 3
    assert all(plot.x_range is desk.map.x_range for plot in desk.maps)
    assert all(plot.y_range is desk.map.y_range for plot in desk.maps)
    document = Document()
    desk.view.server_doc(document)
    issues = check_integrity(list(document.models))
    assert not issues.error


def test_shared_workspace_layout_updates_another_browser_session(tmp_path):
    service = WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    screen = WeatherDesk(service)
    remote = WeatherDesk(service)
    screen.layout.value = "6"
    assert len(screen.workspace_state.panels) == 6
    remote.sync_workspace()
    assert remote.workspace_state.revision == screen.workspace_state.revision
    assert len(remote.map_grid.objects) == 6
    assert remote.map_grid.ncols == 3


def test_workspace_commands_update_shared_camera_fields_and_valid_time(tmp_path):
    from weather_desk.workspace import Camera

    service = WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    screen = WeatherDesk(service)
    remote = WeatherDesk(service)
    state = service.read()
    service.apply(
        [
            {"op": "set_reference_time", "value": "2026-10-02T12:00:00Z"},
            {"op": "set_camera", "camera": {"longitude": -8, "latitude": 48, "zoom": 5.5}},
            {
                "op": "configure_panel",
                "panel_id": state.panels[0].panel_id,
                "changes": {"fields": ["wind10m"]},
            },
        ],
        expected_revision=state.revision,
    )

    remote.sync_workspace()

    assert remote.fields["valid_time"].value == "2026-10-02T12:00:00Z"
    assert remote.live.model_fields.value == ["wind10m"]
    assert remote.workspace_state.camera == Camera(-8, 48, 5.5)
    assert remote.map.x_range.start > screen.map.x_range.start
