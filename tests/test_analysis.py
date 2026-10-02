import json
from concurrent.futures import Future
from io import BytesIO
from types import SimpleNamespace
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


def test_session_state_survives_edit_and_time_changes_gate_exports(monkeypatch):
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

    def hold_request(channel, work, apply):
        desk.live.pending[channel] = (Future(), apply)

    monkeypatch.setattr(desk.live, "submit", hold_request)
    desk.fields["valid_time"].value = "2026-10-01T00:00:00Z"
    assert desk.live.reference_time == "2026-10-01T00:00:00Z"
    with pytest.raises(ValueError, match="chargement du modèle"):
        desk.snapshot()
    source.data = {"xs": [], "ys": []}
    assert desk.layers["cold_front"]["source"].data == {"xs": [], "ys": []}
    assert "model" in desk.live.pending


def test_editorial_and_annotations_autosave_and_restore_across_sessions(tmp_path):
    database = tmp_path / "workspace.sqlite3"
    service = WorkspaceService(SQLiteWorkspaceRepository(database))
    desk = WeatherDesk(service)
    points = [to_mercator(-2, 45), to_mercator(-1, 48)]
    desk.layers["cold_front"]["source"].data = {
        "xs": [[point[0] for point in points]],
        "ys": [[point[1] for point in points]],
    }
    desk.fields["headline"].value = "Front froid sur le golfe de Gascogne."
    desk.fields["analysis"].value = "Renforcement du vent de secteur ouest."

    reopened = WeatherDesk(WorkspaceService(SQLiteWorkspaceRepository(database)))

    assert reopened.fields["headline"].value == "Front froid sur le golfe de Gascogne."
    assert reopened.fields["analysis"].value == "Renforcement du vent de secteur ouest."
    assert len(reopened.snapshot()[1]["features"]) == 1
    coordinates = reopened.snapshot()[1]["features"][0]["geometry"]["coordinates"]
    assert coordinates == [[-2.0, 45.0], [-1.0, 48.0]]


def test_named_analysis_can_be_duplicated_and_reopened_in_the_ui(tmp_path):
    service = WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    desk = WeatherDesk(service)
    desk.analysis_name.value = "Route des Açores"
    desk._rename_analysis(None)
    desk.fields["headline"].value = "Dépression en approche."
    desk.analysis_name.value = "Route bis"
    desk._duplicate_analysis(None)
    duplicate_id = desk.workspace_state.workspace_id

    desk._open_analysis(type("Selection", (), {"new": "main"})())

    assert desk.fields["headline"].value == "Dépression en approche."
    desk._open_analysis(type("Selection", (), {"new": duplicate_id})())
    assert desk.analysis_name.value == "Route bis — copie"
    assert desk.fields["headline"].value == "Dépression en approche."


def test_ui_freezes_and_downloads_an_immutable_edition(tmp_path):
    service = WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    desk = WeatherDesk(service)
    desk.fields["headline"].value = "Bulletin initial."

    desk._freeze_edition(None)
    edition_id = desk.edition_select.value
    desk.fields["headline"].value = "Brouillon modifié après livraison."

    with ZipFile(desk._download_edition()) as archive:
        frozen_manifest = json.loads(archive.read("manifest.json"))
        bulletin = archive.read("bulletin.md").decode()
    assert frozen_manifest["analysis"]["headline"] == "Bulletin initial."
    assert "Bulletin initial." in bulletin
    assert "Brouillon modifié" not in bulletin
    assert service.read_edition(edition_id).manifest["edition"]["immutable"] is True


def test_concurrent_edit_is_detected_without_overwriting_local_text(tmp_path):
    database = tmp_path / "workspace.sqlite3"
    first = WeatherDesk(WorkspaceService(SQLiteWorkspaceRepository(database)))
    second = WeatherDesk(WorkspaceService(SQLiteWorkspaceRepository(database)))
    second.fields["headline"].value = "Texte de la seconde session."

    first.fields["headline"].value = "Texte local à conserver."
    first.sync_workspace()

    assert first.fields["headline"].value == "Texte local à conserver."
    assert first._has_local_conflict
    assert first.reload_conflict_button.visible
    first._reload_workspace_conflict(None)
    assert first.fields["headline"].value == "Texte de la seconde session."
    assert not first._has_local_conflict


def test_model_field_selection_is_saved_and_restored_with_the_workspace(tmp_path):
    database = tmp_path / "workspace.sqlite3"
    service = WorkspaceService(SQLiteWorkspaceRepository(database))
    desk = WeatherDesk(service)

    desk.live.model_fields.value = ["gh500"]
    reopened = WeatherDesk(WorkspaceService(SQLiteWorkspaceRepository(database)))

    assert reopened.live.model_fields.value == ["gh500"]
    assert reopened.workspace_state.panels[0].fields == ("gh500",)


def test_manual_ifs_run_and_step_are_persisted_and_restored(monkeypatch, tmp_path):
    service = WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    desk = WeatherDesk(service)
    live = desk.live
    monkeypatch.setattr(live, "load_model", lambda: None)
    reference_time = service.read().reference_time
    live._setting_controls = True
    live.run.options = ["2026-10-02T12:00:00Z", "2026-10-02T06:00:00Z"]
    live.run.value = "2026-10-02T12:00:00Z"
    live.run.disabled = False
    live.step.value = 6
    live._setting_controls = False

    desk._panel_model_selection_changed(SimpleNamespace(obj=live.step, new=6))

    saved = service.read()
    assert saved.reference_time == reference_time
    assert saved.panels[0].model_run == "2026-10-02T12:00:00Z"
    assert saved.panels[0].step_hours == 6

    reopened = WeatherDesk(
        WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    )
    reopened_live = reopened.live
    monkeypatch.setattr(reopened_live, "load_model", lambda: None)
    reopened_live._run_loaded("2026-10-02T18:00:00Z")
    assert reopened_live.run.value == "2026-10-02T12:00:00Z"
    assert reopened_live.step.value == 6


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


def test_workspace_commands_update_shared_camera_fields_and_valid_time(monkeypatch, tmp_path):
    from weather_desk.workspace import Camera

    service = WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    screen = WeatherDesk(service)
    remote = WeatherDesk(service)
    monkeypatch.setattr(remote.live, "submit", lambda channel, work, apply: None)
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
