#!/bin/bash
set -euo pipefail
umask 077
ROOT=/home/shortstories/workspace/dsh
export DISPLAY=:99
export GEMINI_WEB_STATE="$ROOT/data/integrations/gemini-web"
export GEMINI_WEB_TOKEN_FILE="$GEMINI_WEB_STATE/proxy.token"
mkdir -p "$GEMINI_WEB_STATE"
Xvfb :99 -screen 0 1360x960x24 -nolisten tcp -ac &
display_pid=$!
for attempt in {1..30}; do
  if [[ -S /tmp/.X11-unix/X99 ]]; then break; fi
  sleep 0.1
done
x11vnc -display :99 -localhost -rfbport 15901 -nopw -forever -shared -noxdamage > "$GEMINI_WEB_STATE/vnc.log" 2>&1 &
vnc_pid=$!
websockify --web /usr/share/novnc 127.0.0.1:16080 127.0.0.1:15901 > "$GEMINI_WEB_STATE/websockify.log" 2>&1 &
websocket_pid=$!
cleanup() { kill "$display_pid" "$vnc_pid" "$websocket_pid" 2>/dev/null || true; }
trap cleanup EXIT
cd "$ROOT/plugins/gemini-web-proxy"
"$ROOT/runtime/gemini-web-proxy-env/bin/python" -m uvicorn server:app --host 127.0.0.1 --port 18089 --no-access-log &
proxy_pid=$!
trap 'kill "$proxy_pid" 2>/dev/null || true; cleanup' TERM INT
wait "$proxy_pid"
