"""Authenticated Streamable HTTP MCP interface to a shared Weather Desk workspace."""

from __future__ import annotations

import hmac
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from weather_desk.cartography import PROJECTIONS
from weather_desk.data import DATA, MODEL_FIELDS
from weather_desk.workspace import SQLiteWorkspaceRepository, WorkspaceService


def _service() -> WorkspaceService:
    path = Path(os.environ.get("WEATHER_DESK_DB", "/app/data/workspace.sqlite3"))
    return WorkspaceService(SQLiteWorkspaceRepository(path))


mcp = FastMCP(
    "Weather Desk",
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "weather-desk.galerne-routing.com",
            "127.0.0.1:8765",
            "localhost:8765",
        ],
        allowed_origins=[
            "https://weather-desk.galerne-routing.com",
            "http://127.0.0.1:8765",
            "http://localhost:8765",
        ],
    ),
)
_PROPOSALS: dict[str, tuple[str, int, list[dict], float]] = {}
_PROPOSAL_LOCK = threading.Lock()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_workspace(workspace_id: str = "main") -> dict:
    """Read the current workspace configuration and revision."""
    return _service().read(workspace_id).to_dict()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def get_catalogue() -> dict:
    """List supported satellite products, forecast models and weather fields."""
    products = DATA.satellite_catalogue()
    return {
        "satellite_products": [
            {
                "id": product.product_id,
                "title": product.title,
                "valid_times": list(product.times),
            }
            for product in products
        ],
        "models": [
            {
                "id": "ifs",
                "title": "ECMWF IFS",
                "latest_run": DATA.latest_run(),
                "available_steps_hours": list(range(0, 73, 3)),
            }
        ],
        "fields": [
            {"id": key, "title": value.title, "units": value.units}
            for key, value in MODEL_FIELDS.items()
        ],
        "layouts": ["auto", "1", "2-horizontal", "2-vertical", "4", "6"],
        "projections": list(PROJECTIONS),
    }


@mcp.resource("weather-desk://catalogue")
def catalogue_resource() -> str:
    """Catalogue of products, models and fields available to workspace commands."""
    import json

    return json.dumps(get_catalogue(), ensure_ascii=False)


@mcp.resource("weather-desk://workspace/{workspace_id}")
def workspace_resource(workspace_id: str) -> str:
    """Versioned workspace state for a named workspace."""
    import json

    return json.dumps(get_workspace(workspace_id), ensure_ascii=False)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def preview_workspace_commands(
    commands: list[dict], expected_revision: int, workspace_id: str = "main"
) -> dict:
    """Validate commands without writing; show this proposal to the user first."""
    state = _service().preview(
        commands, expected_revision=expected_revision, workspace_id=workspace_id
    )
    proposal_id = str(uuid4())
    now = time.monotonic()
    with _PROPOSAL_LOCK:
        for old_id, proposal in list(_PROPOSALS.items()):
            if now - proposal[3] > 300:
                del _PROPOSALS[old_id]
        if len(_PROPOSALS) >= 256:
            raise RuntimeError(
                "Trop de propositions en attente; en réutiliser ou attendre leur expiration."
            )
        _PROPOSALS[proposal_id] = (workspace_id, expected_revision, commands, now)
    return {
        "status": "proposal_only",
        "proposal_id": proposal_id,
        "commands": commands,
        "expected_revision": expected_revision,
        "proposed_state": state.to_dict(),
        "confirmation_required": True,
    }


@mcp.tool(
    description="Apply only after the user has explicitly confirmed the exact preview.",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    ),
)
def apply_workspace_commands(proposal_id: str, confirmed: bool) -> dict:
    """Apply the exact preview, only after explicit user confirmation, at its saved revision."""
    if confirmed is not True:
        raise ValueError("La proposition doit être explicitement confirmée par l'utilisateur.")
    with _PROPOSAL_LOCK:
        proposal = _PROPOSALS.pop(proposal_id, None)
    if proposal is None or time.monotonic() - proposal[3] > 300:
        raise ValueError("Proposition inconnue, déjà consommée ou expirée; en créer une nouvelle.")
    workspace_id, expected_revision, commands, _created = proposal
    state = _service().apply(
        commands, expected_revision=expected_revision, workspace_id=workspace_id
    )
    return state.to_dict()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def export_workspace_manifest(workspace_id: str = "main") -> dict:
    """Export a portable JSON snapshot of workspace layout and panel selections."""
    return {
        "format": "weather-desk-workspace-v1",
        "workspace": get_workspace(workspace_id),
    }


@mcp.prompt()
def control_weather_desk(request: str) -> str:
    """Turn a natural-language request into a safe, reviewable workspace proposal."""
    return (
        "Help configure Weather Desk. Read get_workspace and get_catalogue first. "
        "Only use supported identifiers. Call preview_workspace_commands and show the "
        "result to the user. Never call apply_workspace_commands until the user explicitly "
        "confirms the exact proposal; then pass its proposal_id and confirmed=true.\n\n"
        f"User request: {request}"
    )


class BearerTokenMiddleware:
    """Require a dedicated shared bearer token on every MCP HTTP request."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        auth = request.headers.get("authorization", "")
        supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
        if len(self.token) < 32:
            response = JSONResponse({"error": "MCP token is not configured"}, status_code=503)
            await response(scope, receive, send)
            return
        if not hmac.compare_digest(supplied, self.token):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(token: str | None = None) -> Callable:
    token = token or os.environ.get("WEATHER_DESK_MCP_TOKEN", "")
    app = mcp.streamable_http_app()
    return BearerTokenMiddleware(app, token)


app = create_app()
