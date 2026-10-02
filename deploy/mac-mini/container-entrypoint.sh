#!/bin/sh
set -eu

RUNTIME_CONFIG=/tmp/weather-desk-runtime

if [ -z "${PANEL_BASIC_AUTH_JSON:-}" ] || [ -z "${PANEL_COOKIE_SECRET:-}" ]; then
  echo 'Weather Desk authentication files are missing.' >&2
  exit 1
fi

umask 077
mkdir -p "$RUNTIME_CONFIG"
printf '%s\n' "$PANEL_BASIC_AUTH_JSON" > "$RUNTIME_CONFIG/users.json"
unset PANEL_BASIC_AUTH_JSON
export PANEL_BASIC_AUTH="$RUNTIME_CONFIG/users.json"

exec "$@"
