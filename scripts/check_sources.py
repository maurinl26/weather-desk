"""Explicit live check (network); the pytest suite itself is offline."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather_desk.data import DATA  # noqa: E402

started = time.monotonic()
times = DATA.satellite_catalogue()
satellite = DATA.satellite(times[-1])
print("Satellite", satellite.valid_time, len(satellite.png), flush=True)
run = DATA.latest_run()
print("IFS run", run, flush=True)
model = DATA.model(run, 0)
print("IFS", len(model.msl["xs"]), "isobares,", len(model.gh["xs"]), "isohypses", flush=True)
first = time.monotonic() - started
started = time.monotonic()
DATA.satellite(times[-1])
DATA.model(run, 0)
print(
    json.dumps(
        {"first_seconds": round(first, 3), "warm_seconds": round(time.monotonic() - started, 3)}
    )
)
