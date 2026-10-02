"""OpenAI-compatible, server-side proposal generation for the prompt UI."""

from __future__ import annotations

import json
import os

import requests

from weather_desk.data import DATA, MODEL_FIELDS
from weather_desk.workspace import WorkspaceService


def propose_commands(request: str, service: WorkspaceService, workspace_id: str = "main") -> dict:
    base_url = os.environ.get("WEATHER_DESK_LLM_BASE_URL", "").rstrip("/")
    model = os.environ.get("WEATHER_DESK_LLM_MODEL", "")
    api_key = os.environ.get("WEATHER_DESK_LLM_API_KEY", "")
    if not base_url or not model:
        raise RuntimeError("Le fournisseur LLM serveur n'est pas configuré.")
    if not request.strip() or len(request) > 2000:
        raise ValueError("Le prompt doit contenir entre 1 et 2 000 caractères.")
    current = service.read(workspace_id)
    satellite_products = DATA.satellite_catalogue()
    catalogue = {
        "layouts": ["auto", "1", "2-horizontal", "2-vertical", "4", "6"],
        "models": [
            {"id": "ifs", "latest_run": DATA.latest_run(), "steps_hours": list(range(0, 73, 3))}
        ],
        "satellite_products": [
            {"id": product.product_id, "valid_times": list(product.times)}
            for product in satellite_products
        ],
        "fields": list(MODEL_FIELDS),
    }
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        json={
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You configure a weather analysis workspace. Return only JSON with "
                        "a commands array. Supported commands are add_panel(copy_from?), "
                        "remove_panel(panel_id), configure_panel(panel_id, changes), "
                        "set_layout(layout), set_reference_time(value), and "
                        "set_camera(camera). Use only catalogue identifiers. Never include "
                        "expected_revision or claim a change has been applied."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request": request,
                            "workspace": current.to_dict(),
                            "catalogue": catalogue,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        },
        timeout=(5, 45),
    )
    response.raise_for_status()
    try:
        content = response.json()["choices"][0]["message"]["content"]
        payload = json.loads(content)
        commands = payload["commands"]
        if not isinstance(commands, list) or not commands or len(commands) > 12:
            raise ValueError
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Le fournisseur a renvoyé une proposition illisible.") from exc
    proposed = service.preview(
        commands, expected_revision=current.revision, workspace_id=workspace_id
    )
    return {
        "commands": commands,
        "expected_revision": current.revision,
        "workspace_id": workspace_id,
        "proposed_state": proposed.to_dict(),
    }
