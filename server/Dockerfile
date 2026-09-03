# BNGC API + Discord bot — single container, single replica.
# Engine state lives in process memory (like local dev); Upstash is only a
# safety net. Keep exactly 1 replica/worker — more workers split brain state.
# Build from the REPO ROOT:  docker build -f server/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Deps first (better layer caching) — API + bot together
COPY server/requirements.txt ./server-requirements.txt
COPY companion/bot/requirements.txt ./bot-requirements.txt
RUN pip install --no-cache-dir -r ./server-requirements.txt -r ./bot-requirements.txt

# App code (.dockerignore keeps .env / caches / logs out of the image)
COPY server/ ./server/
COPY companion/bot/ ./companion/bot/
COPY server/start.sh ./server/start.sh
RUN chmod +x ./server/start.sh

EXPOSE 8000

# start.sh launches API (+ bot when DISCORD_TOKEN is set), honoring $PORT
CMD ["./server/start.sh"]
