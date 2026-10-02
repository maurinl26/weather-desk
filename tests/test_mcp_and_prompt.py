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
)
from weather_desk.ui import WeatherDesk
from weather_desk.workspace import SQLiteWorkspaceRepository, WorkspaceService


def test_prompt_uses_a_full_width_fixed_single_line_dock():
    desk = WeatherDesk()

    assert isinstance(desk.prompt, pn.widgets.TextInput)
    assert desk.view.header[0] is desk.prompt_bar
    assert desk.view.main[-1].height == 100
    assert all(widget is not desk.prompt for widget in desk.view.sidebar)
    assert desk.prompt_bar.styles["position"] == "fixed"
    assert desk.prompt_bar.styles["width"] == "100vw"


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
