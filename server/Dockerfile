# BNGC Python API — single-replica image.
# Engine state lives in process memory (like local dev); Upstash is only a
# safety net. Keep exactly 1 replica/worker — more workers split brain state.
# Build from the REPO ROOT:  docker build -f server/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Deps first (better layer caching)
COPY server/requirements.txt ./server-requirements.txt
RUN pip install --no-cache-dir -r ./server-requirements.txt

# App code (.dockerignore keeps .env / caches / logs out of the image)
COPY server/ ./server/

EXPOSE 8000

# Single worker: engine sessions are in-process memory.
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
