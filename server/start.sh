#!/bin/bash
# Container entrypoint: API always, bot when DISCORD_TOKEN is set.
# PORT is injected by Render (default 10000); local/docker-compose default 8000.
# SERVER_URL defaults to the local API so the bot talks to this container.
set -u

PORT="${PORT:-8000}"
export SERVER_URL="${SERVER_URL:-http://127.0.0.1:${PORT}}"

echo "[start] API on 0.0.0.0:${PORT}, SERVER_URL=${SERVER_URL}"

uvicorn server.main:app --host 0.0.0.0 --port "${PORT}" --workers 1 &
if [ -n "${DISCORD_TOKEN:-}" ] || [ -n "${DISCORD_BOT_TOKEN:-}" ] || [ -n "${BOT_TOKEN:-}" ]; then
  echo "[start] DISCORD_TOKEN present — starting bot"
  python companion/bot/bot.py &
  # Exit when either process dies (container restarts, both come back in sync)
  wait -n
  echo "[start] a process exited — shutting down"
else
  echo "[start] no DISCORD_TOKEN — API only"
  wait
fi
