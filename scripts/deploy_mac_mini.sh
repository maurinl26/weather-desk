#!/bin/sh
set -eu

REMOTE="${WEATHER_DESK_SSH_TARGET:-macmini}"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
APP_DIR='weather-desk'

ssh "$REMOTE" "mkdir -p ~/$APP_DIR"
rsync -az --delete --exclude='.git/' --exclude='.venv/' --exclude='__pycache__/' \
  --exclude='.pytest_cache/' --exclude='.ruff_cache/' \
  --exclude='data/cache/' --exclude='artifacts/' \
  "$ROOT/" "$REMOTE:~/$APP_DIR/"

ssh "$REMOTE" 'sh -s' <<'REMOTE_SCRIPT'
set -eu

PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
export PATH
export HOMEBREW_NO_AUTO_UPDATE=1
export HOMEBREW_NO_INSTALL_CLEANUP=1
APP="$HOME/weather-desk"
CONFIG="$HOME/.config/weather-desk"
CF="$HOME/.cloudflared"
LAUNCH="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs"
HOSTNAME='weather-desk.galerne-routing.com'

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo 'OrbStack must be running and its Docker engine available.' >&2
  exit 1
fi
if [ "$(docker context show)" != 'orbstack' ]; then
  echo 'Select the OrbStack Docker context before deploying.' >&2
  exit 1
fi
if ! command -v cloudflared >/dev/null 2>&1; then
  echo 'cloudflared is required on the Mac mini.' >&2
  exit 1
fi

mkdir -p "$CONFIG" "$CF" "$LAUNCH" "$LOGS"
chmod 700 "$CONFIG"
if [ ! -f "$CONFIG/users.json" ]; then
  PASSWORD="$(openssl rand -base64 36 | tr -d '\n')"
  printf '{"maurin.loic.ac@gmail.com":"%s"}\n' "$PASSWORD" > "$CONFIG/users.json"
  printf 'username=maurin.loic.ac@gmail.com\npassword=%s\n' "$PASSWORD" > "$CONFIG/access.txt"
fi
if [ ! -f "$CONFIG/cookie-secret" ]; then
  openssl rand -hex 32 > "$CONFIG/cookie-secret"
fi
if [ ! -f "$CONFIG/mcp-token" ]; then
  openssl rand -hex 32 > "$CONFIG/mcp-token"
fi
if [ ! -f "$CONFIG/llm.env" ]; then
  : > "$CONFIG/llm.env"
fi
chmod 600 "$CONFIG/users.json" "$CONFIG/access.txt" "$CONFIG/cookie-secret" \
  "$CONFIG/mcp-token" "$CONFIG/llm.env"
USERS_JSON="$(cat "$CONFIG/users.json")"
COOKIE_SECRET="$(cat "$CONFIG/cookie-secret")"
MCP_TOKEN="$(cat "$CONFIG/mcp-token")"
{
  printf 'PANEL_BASIC_AUTH_JSON=%s\n' "$USERS_JSON"
  printf 'PANEL_COOKIE_SECRET=%s\n' "$COOKIE_SECRET"
  printf 'WEATHER_DESK_MCP_TOKEN=%s\n' "$MCP_TOKEN"
} > "$CONFIG/compose.env"
chmod 600 "$CONFIG/compose.env"
unset USERS_JSON COOKIE_SECRET MCP_TOKEN

TUNNEL_ID="$(cloudflared tunnel list --output json | /usr/bin/python3 -c 'import json,sys; print(next((t["id"] for t in json.load(sys.stdin) if t["name"] == "weather-desk"), ""))')"
if [ -z "$TUNNEL_ID" ]; then
  cloudflared tunnel create weather-desk
  TUNNEL_ID="$(cloudflared tunnel list --output json | /usr/bin/python3 -c 'import json,sys; print(next((t["id"] for t in json.load(sys.stdin) if t["name"] == "weather-desk"), ""))')"
fi
if [ -z "$TUNNEL_ID" ]; then
  echo 'Could not find or create the weather-desk Cloudflare tunnel.' >&2
  exit 1
fi

sed "s/REPLACE_WITH_TUNNEL_ID/$TUNNEL_ID/g" "$APP/deploy/mac-mini/weather-desk-tunnel.yml" \
  | sed "s#REPLACE_ME#$USER#g" > "$CF/weather-desk.yml"
chmod 600 "$CF/weather-desk.yml" "$CF/$TUNNEL_ID.json"

sed "s#REPLACE_ME#$USER#g" "$APP/deploy/mac-mini/pro.galerne.weather-desk-tunnel.plist" \
  > "$LAUNCH/pro.galerne.weather-desk-tunnel.plist"
chmod 644 "$LAUNCH/pro.galerne.weather-desk-tunnel.plist"

docker compose --env-file "$CONFIG/compose.env" \
  --env-file "$CONFIG/llm.env" \
  -f "$APP/deploy/mac-mini/compose.yml" up -d --build
GUI_UID="$(id -u)"
LABEL=pro.galerne.weather-desk-tunnel
if launchctl print "gui/$GUI_UID/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$GUI_UID/$LABEL"
else
  launchctl bootstrap "gui/$GUI_UID" "$LAUNCH/$LABEL.plist"
fi

printf 'Weather Desk container deployed on OrbStack. Tunnel: %s\n' "$TUNNEL_ID"
printf 'Access credentials are stored in %s/access.txt (mode 600).\n' "$CONFIG"
printf 'Cloudflare DNS CNAME needed: %s -> %s.cfargotunnel.com (proxied)\n' "$HOSTNAME" "$TUNNEL_ID"
cat "$CONFIG/access.txt"
REMOTE_SCRIPT
