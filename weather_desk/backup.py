"""Create a consistent SQLite backup of workspaces and immutable editions."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from weather_desk.workspace import SQLiteWorkspaceRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="SQLite backup file to create")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("WEATHER_DESK_DB", "/app/data/workspace.sqlite3")),
        help="active SQLite database (defaults to WEATHER_DESK_DB)",
    )
    args = parser.parse_args()
    path = SQLiteWorkspaceRepository(args.database).backup_to(args.destination)
    print(path)


if __name__ == "__main__":
    main()
