import asyncio
import json
from types import SimpleNamespace

import httpx
import panel as pn

from weather_desk.assistant import propose_commands
from weather_desk.mcp_server import (
    BearerTokenMiddleware,
    apply_workspace_commands,
    preview_workspace_commands,
    resolve_reference_time,
)
from weather_desk.ui import WeatherDesk
from weather_desk.workspace import SQLiteWorkspaceRepository, WorkspaceService


def test_prompt_leads_a_collapsible_grouped_sidebar():
    desk = WeatherDesk()

    assert isinstance(desk.prompt, pn.widgets.TextInput)
    assert desk.view.sidebar[0] is desk.prompt_bar
    assert desk.view.collapsed_sidebar is False
    assert desk.view.sidebar_width == 340
    assert isinstance(desk.view.sidebar[1], pn.Accordion)
    assert desk.prompt_result.styles["color"] == "#f5f7fa"
    assert desk.prompt_details.styles["color"] == "#f5f7fa"


def test_bulletin_editor_uses_a_map_panel_height():
    desk = WeatherDesk()
    # Le bulletin vit dans un drawer latéral droit (FloatPanel), pas dans la
    # colonne principale : la carte garde toute la hauteur visible.
    assert not any(isinstance(card, pn.Card) and card.title == "Bulletin" for card in desk.view.main)
    assert desk.bulletin_panel.position == "right-top"
    assert not desk.bulletin_panel.visible
    # Le formulaire contient tous les champs du bulletin.
    names = {w.label for w in desk.bulletin_panel[0] if hasattr(w, "label")}
    assert {"Zone", "Confiance", "Message principal"} <= names
    # La carte reste l'élément dominant : le panneau d'état est flottant,
    # le statut n'est plus en tête de colonne principale.
    assert not any(el is desk.live.time_status for el in desk.view.main[0])
    assert desk.status_panel.position == "right-bottom"


def test_ui_reference_time_persists_through_the_shared_workspace_service(monkeypatch, tmp_path):
    service = WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    desk = WeatherDesk(service)
    channels = []
    monkeypatch.setattr(
        desk.live,
        "submit",
        lambda channel, work, apply: channels.append(channel),
    )

    desk.fields["valid_time"].value = "2026-10-02T17:40:00+00:00"

    assert desk.live.reference_time == "2026-10-02T17:40:00Z"
    assert service.read().reference_time == "2026-10-02T17:40:00Z"
    assert channels == ["model"]


def test_mcp_time_resolution_matches_the_ui_policy(monkeypatch):
    monkeypatch.setattr("weather_desk.mcp_server.DATA.latest_run", lambda: "2026-10-02T18:00:00Z")
    resolved = resolve_reference_time("2026-10-02T17:40:00Z")

    assert resolved["valid_time"] == "2026-10-02T18:00:00Z"
    assert resolved["run"] == "2026-10-02T18:00:00Z"
    assert resolved["step_hours"] == 0
    assert resolved["offset_minutes"] == 20


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"commands": [{"op": "set_layout", "layout": "4"}]})
                    }
                }
            ]
        }


def test_prompt_returns_preview_and_does_not_apply(monkeypatch, tmp_path):
    service = WorkspaceService(SQLiteWorkspaceRepository(tmp_path / "workspace.sqlite3"))
    monkeypatch.setenv("WEATHER_DESK_LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("WEATHER_DESK_LLM_MODEL", "weather-model")
    monkeypatch.setenv("WEATHER_DESK_LLM_REASONING_EFFORT", "none")
    request_body = {}
    monkeypatch.setattr(
        "weather_desk.assistant.DATA.satellite_catalogue",
        lambda: [SimpleNamespace(product_id="msg_fes:wv062", times=("2026-10-02T12:00:00Z",))],
    )
    monkeypatch.setattr("weather_desk.assistant.DATA.latest_run", lambda: "2026-10-02T00:00:00Z")

    def fake_post(*args, **kwargs):
        request_body.update(kwargs["json"])
        return FakeResponse()

    monkeypatch.setattr("weather_desk.assistant.requests.post", fake_post)

    proposal = propose_commands("Affiche quatre panneaux", service)

    assert proposal["expected_revision"] == 0
    assert proposal["proposed_state"]["layout"] == "4"
    assert service.read().revision == 0
    assert request_body["reasoning_effort"] == "none"
    assert '"op":"set_layout"' in request_body["messages"][0]["content"]
    assert '"op":"set_projection"' in request_body["messages"][0]["content"]
    assert request_body["messages"][1]["content"].find('"projections"') >= 0


def test_mcp_only_applies_the_exact_confirmed_preview_once(monkeypatch, tmp_path):
    database = tmp_path / "workspace.sqlite3"
    monkeypatch.setenv("WEATHER_DESK_DB", str(database))
    service = WorkspaceService(SQLiteWorkspaceRepository(database))
    initial = service.read()
    proposal = preview_workspace_commands(
        [{"op": "set_layout", "layout": "4"}], expected_revision=initial.revision
    )

    try:
        apply_workspace_commands(proposal["proposal_id"], False)
    except ValueError as exc:
        assert "confirmée" in str(exc)
    else:
        raise AssertionError("A confirmation was required")
    assert service.read() == initial

    updated = apply_workspace_commands(proposal["proposal_id"], True)

    assert updated["layout"] == "4"
    assert updated["revision"] == 1
    try:
        apply_workspace_commands(proposal["proposal_id"], True)
    except ValueError as exc:
        assert "inconnue" in str(exc)
    else:
        raise AssertionError("A proposal can only be applied once")


def test_mcp_bearer_middleware_rejects_missing_and_accepts_valid_token():
    async def endpoint(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BearerTokenMiddleware(endpoint, "a" * 32)

    async def check_auth():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            unauthorized = await client.get("/mcp")
            authorized = await client.get("/mcp", headers={"Authorization": f"Bearer {'a' * 32}"})
            return unauthorized.status_code, authorized.status_code

    unauthorized_status, authorized_status = asyncio.run(check_auth())

    assert unauthorized_status == 401
    assert authorized_status == 204
