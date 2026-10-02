#!/bin/sh
set -eu

ROOT="$HOME/weather-desk"
CONFIG="$HOME/.config/weather-desk"
PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
export PATH
UV="$(command -v uv)"

export PANEL_BASIC_AUTH="$CONFIG/users.json"
export PANEL_COOKIE_SECRET="$(cat "$CONFIG/cookie-secret")"

cd "$ROOT"
exec "$UV" run --locked --no-dev --project "$ROOT" panel serve app.py \
  --address 127.0.0.1 \
  --port 5006 \
  --allow-websocket-origin weather-desk.galerne-routing.com \
  --use-xheaders \
  --liveness
