import pytest

from weather_desk.workspace import (
    Camera,
    SQLiteWorkspaceRepository,
    WorkspaceConflict,
    WorkspaceError,
    WorkspaceService,
    WorkspaceState,
)


def service_at(path):
    return WorkspaceService(SQLiteWorkspaceRepository(path))


def test_workspace_round_trips_and_survives_repository_restart(tmp_path):
    service = service_at(tmp_path / "workspace.sqlite3")
    initial = service.read()
    configured = service.apply(
        [
            {"op": "set_layout", "layout": "2-horizontal"},
            {"op": "set_reference_time", "value": "2026-10-02T12:00:00Z"},
            {
                "op": "set_camera",
                "camera": {"longitude": -8, "latitude": 48, "zoom": 5.5},
            },
        ],
        expected_revision=initial.revision,
    )
    reopened = service_at(tmp_path / "workspace.sqlite3").read()
    assert reopened == configured
    assert reopened.revision == 1
    assert reopened.layout == "2-horizontal"
    assert len(reopened.panels) == 2
    assert reopened.camera == Camera(-8, 48, 5.5)


def test_batch_is_atomic_when_a_later_command_is_invalid(tmp_path):
    service = service_at(tmp_path / "workspace.sqlite3")
    initial = service.read()
    with pytest.raises(WorkspaceError, match="champ|Champ"):
        service.apply(
            [
                {"op": "add_panel"},
                {
                    "op": "configure_panel",
                    "panel_id": initial.panels[0].panel_id,
                    "changes": {"fields": ["malicious field"]},
                },
            ],
            expected_revision=initial.revision,
        )
    assert service.read() == initial


def test_add_remove_and_configure_panels_preserve_independent_settings(tmp_path):
    service = service_at(tmp_path / "workspace.sqlite3")
    initial = service.read()
    state = service.apply([{"op": "add_panel"}], expected_revision=initial.revision)
    first, second = state.panels
    state = service.apply(
        [
            {
                "op": "configure_panel",
                "panel_id": second.panel_id,
                "changes": {"satellite_product": None, "fields": ["gh500"]},
            }
        ],
        expected_revision=state.revision,
    )
    assert state.panels[0] == first
    assert state.panels[1].satellite_product is None
    assert state.panels[1].fields == ("gh500",)
    state = service.apply(
        [{"op": "remove_panel", "panel_id": second.panel_id}],
        expected_revision=state.revision,
    )
    assert state.panels == (first,)


def test_workspace_rejects_more_than_six_panels_and_keeps_state(tmp_path):
    service = service_at(tmp_path / "workspace.sqlite3")
    state = service.read()
    state = service.apply(
        [{"op": "add_panel"}] * 5,
        expected_revision=state.revision,
    )
    assert len(state.panels) == 6
    with pytest.raises(WorkspaceError, match="six"):
        service.apply([{"op": "add_panel"}], expected_revision=state.revision)
    assert service.read() == state


def test_stale_writer_gets_revision_conflict(tmp_path):
    path = tmp_path / "workspace.sqlite3"
    first_client, second_client = service_at(path), service_at(path)
    first_state, stale_state = first_client.read(), second_client.read()
    first_client.apply(
        [{"op": "set_layout", "layout": "4"}], expected_revision=first_state.revision
    )
    with pytest.raises(WorkspaceConflict, match="révision actuelle"):
        second_client.apply(
            [{"op": "set_layout", "layout": "6"}], expected_revision=stale_state.revision
        )
    assert first_client.read().layout == "4"


def test_preview_validates_without_persisting_and_requires_current_revision(tmp_path):
    service = service_at(tmp_path / "workspace.sqlite3")
    initial = service.read()
    commands = [{"op": "set_layout", "layout": "4"}]

    proposal = service.preview(commands, expected_revision=initial.revision)

    assert proposal.layout == "4"
    assert len(proposal.panels) == 4
    assert service.read().to_dict() == initial.to_dict()
    service.apply(commands, expected_revision=initial.revision)
    with pytest.raises(WorkspaceConflict):
        service.preview(commands, expected_revision=initial.revision)


@pytest.mark.parametrize(
    "camera",
    [
        {"longitude": float("nan"), "latitude": 50, "zoom": 4},
        {"longitude": 0, "latitude": 90, "zoom": 4},
        {"longitude": 0, "latitude": 50, "zoom": 25},
        {"longitude": "0", "latitude": 50, "zoom": 4},
    ],
)
def test_invalid_camera_is_rejected_without_writing(tmp_path, camera):
    service = service_at(tmp_path / "workspace.sqlite3")
    state = service.read()
    with pytest.raises(WorkspaceError):
        service.apply([{"op": "set_camera", "camera": camera}], expected_revision=state.revision)
    assert service.read() == state


def test_invalid_time_and_schema_are_rejected(tmp_path):
    service = service_at(tmp_path / "workspace.sqlite3")
    state = service.read()
    with pytest.raises(WorkspaceError, match="ISO-8601"):
        service.apply(
            [{"op": "set_reference_time", "value": "not-a-date"}],
            expected_revision=state.revision,
        )
    document = state.to_dict()
    document["schema_version"] = 3
    with pytest.raises(WorkspaceError, match="Migration"):
        WorkspaceState.from_dict(document)
    assert service.read() == state


def test_schema_one_workspace_migrates_with_empty_editorial_draft():
    state = WorkspaceState.default()
    document = state.to_dict()
    document["schema_version"] = 1
    document.pop("editorial")

    migrated = WorkspaceState.from_dict(document)

    assert migrated.schema_version == 2
    assert migrated.editorial["headline"] == ""
    assert migrated.annotations["features"] == []


def test_analysis_can_be_named_and_duplicated_without_sharing_future_edits(tmp_path):
    service = service_at(tmp_path / "workspace.sqlite3")
    original = service.read()
    renamed = service.apply(
        [{"op": "rename", "name": "Route Atlantique"}],
        expected_revision=original.revision,
    )
    duplicated = service.duplicate(renamed.workspace_id, "Route Atlantique — copie")

    assert {state.workspace_id for state in service.list()} == {
        renamed.workspace_id,
        duplicated.workspace_id,
    }
    assert duplicated.workspace_id != renamed.workspace_id
    assert duplicated.revision == 0
    assert (
        service.apply(
            [{"op": "rename", "name": "Copie modifiée"}],
            expected_revision=duplicated.revision,
            workspace_id=duplicated.workspace_id,
        ).name
        == "Copie modifiée"
    )
    assert service.read(renamed.workspace_id).name == "Route Atlantique"


def test_workspace_starts_with_the_existing_live_layers(tmp_path):
    state = service_at(tmp_path / "workspace.sqlite3").read()
    assert state.panels[0].satellite_product == "msg_fes:wv062"
    assert state.panels[0].model_id == "ifs"
    assert state.panels[0].fields == ("msl", "gh500")
