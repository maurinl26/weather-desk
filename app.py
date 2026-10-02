"""Panel entrypoint; see README.md for the local launch command."""

from pathlib import Path

import panel as pn

from weather_desk.ui import WeatherDesk
from weather_desk.workspace import SQLiteWorkspaceRepository, WorkspaceService

pn.extension()

# Every Panel session uses the same persisted workspace so the local screen and
# authenticated remote clients can stay in sync.
workspace = WorkspaceService(
    SQLiteWorkspaceRepository(Path(__file__).resolve().parent / "data" / "workspace.sqlite3")
)
desk = WeatherDesk(workspace)
desk.view.servable()
pn.state.onload(desk.start)
