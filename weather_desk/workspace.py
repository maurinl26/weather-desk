"""Versioned, UI-independent state for the shared Weather Desk workspace."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 2
MAX_PANELS = 6
_SLUG = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")
_LAYOUTS = {"auto", "1", "2-horizontal", "2-vertical", "4", "6"}
_FIELDS = {"msl", "gh500", "t2m", "tp", "wind10m"}
_PROJECTIONS = {"mercator", "lambert", "stereopolar"}


class WorkspaceError(ValueError):
    """Invalid workspace state or unsupported workspace command."""


class WorkspaceConflict(RuntimeError):
    """The workspace changed since the caller last read it."""


@dataclass(frozen=True)
class Camera:
    longitude: float = 5.0
    latitude: float = 50.0
    zoom: float = 4.0

    def validate(self) -> None:
        values = (self.longitude, self.latitude, self.zoom)
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in values):
            raise WorkspaceError("La caméra doit contenir des nombres.")
        if not all(math.isfinite(value) for value in values):
            raise WorkspaceError("La caméra doit contenir des valeurs finies.")
        if not -180 <= self.longitude <= 180 or not -85.05112878 <= self.latitude <= 85.05112878:
            raise WorkspaceError("Le centre de la caméra est hors des limites géographiques.")
        if not 0 <= self.zoom <= 24:
            raise WorkspaceError("Le zoom doit être compris entre 0 et 24.")


@dataclass(frozen=True)
class PanelConfig:
    panel_id: str
    satellite_product: str | None = "msg_fes:wv062"
    model_id: str | None = "ifs"
    fields: tuple[str, ...] = ("msl", "gh500")

    @classmethod
    def create(cls, **values: Any) -> PanelConfig:
        return cls(panel_id=str(uuid4()), **values)

    def validate(self) -> None:
        if not isinstance(self.panel_id, str) or not _SLUG.fullmatch(self.panel_id):
            raise WorkspaceError("Identifiant de panneau invalide.")
        if not isinstance(self.fields, tuple):
            raise WorkspaceError("Les champs doivent être une liste immuable d'identifiants.")
        for value in (self.satellite_product, self.model_id, *self.fields):
            if value is not None and (not isinstance(value, str) or not _SLUG.fullmatch(value)):
                raise WorkspaceError("Identifiant de source ou de champ invalide.")
        if set(self.fields) - _FIELDS:
            raise WorkspaceError("Un champ météo n'est pas disponible dans le catalogue.")
        if len(set(self.fields)) != len(self.fields):
            raise WorkspaceError("Un champ ne peut apparaître qu'une fois dans un panneau.")
        if self.satellite_product is None and self.model_id is None:
            raise WorkspaceError("Un panneau doit contenir une source satellite ou modèle.")


@dataclass(frozen=True)
class WorkspaceState:
    workspace_id: str = "main"
    name: str = "Analyse météo"
    revision: int = 0
    layout: str = "1"
    reference_time: str = ""
    camera: Camera = Camera()
    camera_sync: bool = True
    projection: str = "mercator"
    panels: tuple[PanelConfig, ...] = ()
    annotations: dict[str, Any] | None = None
    editorial: dict[str, str] | None = None
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def default(cls, workspace_id: str = "main") -> WorkspaceState:
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return cls(
            workspace_id=workspace_id,
            reference_time=now,
            panels=(PanelConfig.create(),),
            annotations={"type": "FeatureCollection", "features": []},
            editorial={
                "zone": "France / façade Atlantique",
                "confidence": "moyenne",
                "headline": "",
                "analysis": "",
                "impacts": "",
                "limitations": "",
            },
        )

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise WorkspaceError(
                f"Version de workspace non prise en charge : {self.schema_version}."
            )
        if not isinstance(self.workspace_id, str) or not _SLUG.fullmatch(self.workspace_id):
            raise WorkspaceError("Identifiant de workspace invalide.")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 160:
            raise WorkspaceError("Le nom de l'analyse doit contenir de 1 à 160 caractères.")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 0
        ):
            raise WorkspaceError("Révision de workspace invalide.")
        if not isinstance(self.layout, str) or self.layout not in _LAYOUTS:
            raise WorkspaceError("Disposition de workspace inconnue.")
        if not self.panels or len(self.panels) > MAX_PANELS:
            raise WorkspaceError("Un workspace doit contenir entre 1 et 6 panneaux.")
        if len({panel.panel_id for panel in self.panels}) != len(self.panels):
            raise WorkspaceError("Les identifiants des panneaux doivent être uniques.")
        for panel in self.panels:
            panel.validate()
        if not isinstance(self.reference_time, str) or not self.reference_time:
            raise WorkspaceError("L'échéance de référence est obligatoire.")
        try:
            parsed = datetime.fromisoformat(self.reference_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WorkspaceError("L'échéance doit être une date ISO-8601.") from exc
        if parsed.tzinfo is None:
            raise WorkspaceError("L'échéance doit inclure son fuseau horaire.")
        self.camera.validate()
        if not isinstance(self.projection, str) or self.projection not in _PROJECTIONS:
            raise WorkspaceError("Projection cartographique inconnue.")
        if not isinstance(self.camera_sync, bool):
            raise WorkspaceError("camera_sync doit être booléen.")
        if self.annotations is not None and (
            not isinstance(self.annotations, dict)
            or self.annotations.get("type") != "FeatureCollection"
            or not isinstance(self.annotations.get("features"), list)
        ):
            raise WorkspaceError("Les annotations doivent être une FeatureCollection GeoJSON.")
        if (
            self.editorial is None
            or set(self.editorial)
            != {"zone", "confidence", "headline", "analysis", "impacts", "limitations"}
            or not all(isinstance(value, str) for value in self.editorial.values())
        ):
            raise WorkspaceError("Le brouillon éditorial du workspace est invalide.")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = asdict(self)
        payload["panels"] = [
            {**asdict(panel), "fields": list(panel.fields)} for panel in self.panels
        ]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WorkspaceState:
        try:
            data = dict(payload)
            version = data.get("schema_version", 1)
            if version == 1:
                data["schema_version"] = SCHEMA_VERSION
                data["editorial"] = WorkspaceState.default().editorial
                data["name"] = "Analyse météo"
            elif version != SCHEMA_VERSION:
                raise WorkspaceError(f"Migration requise pour la version {version}.")
            data["camera"] = Camera(**data.get("camera", {}))
            data["panels"] = tuple(
                PanelConfig(**{**panel, "fields": tuple(panel.get("fields", ()))})
                for panel in data.get("panels", ())
            )
            result = cls(**data)
        except WorkspaceError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError("État de workspace mal formé.") from exc
        result.validate()
        return result


class SQLiteWorkspaceRepository:
    """Small transactional repository; state JSON is canonical and versioned."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def load(self, workspace_id: str = "main") -> WorkspaceState:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state_json, revision FROM workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if row is None:
                state = WorkspaceState.default(workspace_id)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO workspaces VALUES (?, ?, ?, ?)",
                    (workspace_id, state.revision, _json(state.to_dict()), _now()),
                )
                connection.execute("COMMIT")
                return self.load(workspace_id)
            state = WorkspaceState.from_dict(json.loads(row["state_json"]))
            if state.revision != row["revision"]:
                raise WorkspaceError("Révision SQLite et document de workspace incohérents.")
            return state

    def list(self) -> list[WorkspaceState]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT state_json, revision FROM workspaces ORDER BY updated_at DESC"
            ).fetchall()
        states = [WorkspaceState.from_dict(json.loads(row["state_json"])) for row in rows]
        for state, row in zip(states, rows, strict=True):
            if state.revision != row["revision"]:
                raise WorkspaceError("Révision SQLite et document de workspace incohérents.")
        return states

    def create(self, state: WorkspaceState) -> WorkspaceState:
        state.validate()
        if state.revision != 0:
            raise WorkspaceError("Une nouvelle analyse doit commencer à la révision zéro.")
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    "INSERT INTO workspaces VALUES (?, ?, ?, ?)",
                    (state.workspace_id, state.revision, _json(state.to_dict()), _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkspaceConflict("Une analyse porte déjà cet identifiant.") from exc
        return state

    def save(self, state: WorkspaceState, expected_revision: int) -> WorkspaceState:
        state.validate()
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or state.revision != expected_revision
        ):
            raise WorkspaceError("La révision de l'état ne correspond pas à la révision attendue.")
        next_state = replace(state, revision=expected_revision + 1)
        encoded = _json(next_state.to_dict())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM workspaces WHERE workspace_id = ?", (state.workspace_id,)
            ).fetchone()
            current = row["revision"] if row else None
            if current != expected_revision:
                connection.execute("ROLLBACK")
                raise WorkspaceConflict(
                    f"Workspace modifié depuis la révision {expected_revision} "
                    f"(révision actuelle : {current})."
                )
            if row:
                connection.execute(
                    "UPDATE workspaces SET revision=?, state_json=?, updated_at=? "
                    "WHERE workspace_id=?",
                    (next_state.revision, encoded, _now(), state.workspace_id),
                )
            else:
                connection.execute(
                    "INSERT INTO workspaces VALUES (?, ?, ?, ?)",
                    (state.workspace_id, next_state.revision, encoded, _now()),
                )
            connection.execute("COMMIT")
        return next_state


class WorkspaceService:
    """Validates and applies workspace commands atomically."""

    def __init__(self, repository: SQLiteWorkspaceRepository):
        self.repository = repository

    def read(self, workspace_id: str = "main") -> WorkspaceState:
        return self.repository.load(workspace_id)

    def list(self) -> list[WorkspaceState]:
        return self.repository.list()

    def duplicate(self, workspace_id: str, name: str) -> WorkspaceState:
        original = self.read(workspace_id)
        duplicate = replace(
            original,
            workspace_id=str(uuid4()),
            name=name.strip(),
            revision=0,
        )
        return self.repository.create(duplicate)

    def preview(
        self,
        commands: list[dict[str, Any]],
        *,
        expected_revision: int,
        workspace_id: str = "main",
    ) -> WorkspaceState:
        """Validate a proposal against the current revision without persisting it."""
        if not commands:
            raise WorkspaceError("Une mutation doit contenir au moins une commande.")
        state = self.read(workspace_id)
        if state.revision != expected_revision:
            raise WorkspaceConflict(
                f"Workspace modifié depuis la révision {expected_revision} "
                f"(révision actuelle : {state.revision})."
            )
        for command in commands:
            state = _apply_command(state, command)
        return state

    def apply(
        self,
        commands: list[dict[str, Any]],
        *,
        expected_revision: int,
        workspace_id: str = "main",
    ) -> WorkspaceState:
        if not commands:
            raise WorkspaceError("Une mutation doit contenir au moins une commande.")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise WorkspaceError("Révision attendue invalide.")
        state = self.preview(
            commands, expected_revision=expected_revision, workspace_id=workspace_id
        )
        return self.repository.save(state, expected_revision)


def _apply_command(state: WorkspaceState, command: dict[str, Any]) -> WorkspaceState:
    if not isinstance(command, dict) or not isinstance(command.get("op"), str):
        raise WorkspaceError("Commande de workspace mal formée.")
    op = command["op"]
    command_keys = {
        "add_panel": {"op", "copy_from"},
        "remove_panel": {"op", "panel_id"},
        "configure_panel": {"op", "panel_id", "changes"},
        "set_layout": {"op", "layout"},
        "set_reference_time": {"op", "value"},
        "set_camera": {"op", "camera"},
        "set_camera_sync": {"op", "enabled"},
        "set_projection": {"op", "projection"},
        "set_annotations": {"op", "value"},
        "set_editorial": {"op", "value"},
        "rename": {"op", "name"},
    }
    if op not in command_keys or set(command) - command_keys[op]:
        raise WorkspaceError(f"Arguments de commande invalides : {op}.")
    panels = list(state.panels)
    if op == "add_panel":
        if len(panels) >= MAX_PANELS:
            raise WorkspaceError("Maximum de six panneaux atteint.")
        source = next(
            (item for item in panels if item.panel_id == command.get("copy_from")),
            panels[-1],
        )
        panels.append(replace(source, panel_id=str(uuid4())))
        layout = "auto"
        result = replace(state, panels=tuple(panels), layout=layout)
    elif op == "remove_panel":
        panels = [panel for panel in panels if panel.panel_id != command.get("panel_id")]
        if len(panels) == len(state.panels):
            raise WorkspaceError("Panneau inconnu.")
        if not panels:
            raise WorkspaceError("Un workspace doit conserver au moins un panneau.")
        result = replace(state, panels=tuple(panels), layout="auto")
    elif op == "configure_panel":
        panel_id = command.get("panel_id")
        changes = command.get("changes")
        if not isinstance(changes, dict) or set(changes) - {
            "satellite_product",
            "model_id",
            "fields",
        }:
            raise WorkspaceError("Configuration de panneau invalide.")
        changes = dict(changes)
        if "fields" in changes:
            if not isinstance(changes["fields"], list) or not all(
                isinstance(value, str) for value in changes["fields"]
            ):
                raise WorkspaceError("La liste des champs est invalide.")
            changes["fields"] = tuple(changes["fields"])
        matched = False
        configured = []
        for panel in panels:
            if panel.panel_id == panel_id:
                matched = True
                configured.append(replace(panel, **changes))
            else:
                configured.append(panel)
        if not matched:
            raise WorkspaceError("Panneau inconnu.")
        result = replace(state, panels=tuple(configured))
    elif op == "set_layout":
        layout = command.get("layout")
        if not isinstance(layout, str) or layout not in _LAYOUTS:
            raise WorkspaceError("Disposition de workspace inconnue.")
        count = {"1": 1, "2-horizontal": 2, "2-vertical": 2, "4": 4, "6": 6}.get(
            layout, len(panels)
        )
        panels = panels[:count]
        while len(panels) < count:
            panels.append(replace(panels[-1], panel_id=str(uuid4())))
        result = replace(state, layout=layout, panels=tuple(panels))
    elif op == "set_reference_time":
        result = replace(state, reference_time=command.get("value"))
    elif op == "set_camera":
        camera = command.get("camera")
        if not isinstance(camera, dict) or set(camera) != {"longitude", "latitude", "zoom"}:
            raise WorkspaceError("Configuration de caméra invalide.")
        result = replace(state, camera=Camera(**camera))
    elif op == "set_camera_sync":
        result = replace(state, camera_sync=command.get("enabled"))
    elif op == "set_projection":
        result = replace(state, projection=command.get("projection"))
    elif op == "set_annotations":
        result = replace(state, annotations=command.get("value"))
    elif op == "set_editorial":
        result = replace(state, editorial=command.get("value"))
    elif op == "rename":
        result = replace(state, name=command.get("name"))
    else:
        raise WorkspaceError(f"Commande inconnue : {op}.")
    result.validate()
    return result


def _json(value: dict[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
