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
    reasoning_effort = os.environ.get("WEATHER_DESK_LLM_REASONING_EFFORT", "").strip()
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
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You configure a weather analysis workspace. Return exactly one JSON "
                    'object shaped like {"commands":[{"op":"configure_panel",'
                    '"panel_id":"PANEL_ID_FROM_WORKSPACE",'
                    '"changes":{"fields":["wind10m"]}}]}. The outer commands '
                    "array is mandatory, must not be omitted, and is the only top-level key. "
                    "Each item in it MUST use the exact workspace protocol: "
                    '{"op":"configure_panel", '
                    '"panel_id":"<an existing panel id>", '
                    '"changes":{"fields":["wind10m"]}}. The command key is '
                    "always named op, never command or action. To set a panel's fields, "
                    "use configure_panel with changes.fields as an array of field IDs. "
                    'Other supported forms are {"op":"add_panel"}, '
                    '{"op":"add_panel","copy_from":"<existing panel id>"}, '
                    '{"op":"remove_panel","panel_id":"<existing panel id>"}, '
                    '{"op":"configure_panel","panel_id":"<existing panel id>",'
                    '"changes":{"satellite_product":"<catalogue id>"}}, '
                    '{"op":"configure_panel","panel_id":"<existing panel id>",'
                    '"changes":{"model_id":"ifs","fields":["msl"]}}, '
                    '{"op":"set_layout","layout":"4"}, '
                    '{"op":"set_reference_time","value":"<ISO-8601 time>"}, '
                    'and {"op":"set_camera","camera":{"longitude":0,'
                    '"latitude":50,"zoom":4}}. Use only panel IDs and catalogue '
                    "identifiers present in the supplied workspace and catalogue. Never "
                    "invent a field, model, product, timestamp, or panel ID. Never include "
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
    }
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        json=body,
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
