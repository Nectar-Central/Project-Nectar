"""
Vercel serverless entry point for FastAPI.

Vercel's Python runtime expects a file in `api/` that exposes
the ASGI app as `app`. We re-export the FastAPI app from server/main.py.
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so `server.*` imports work on Vercel
# Vercel runs from repo root; locally `api/index.py` is one level deep.
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.main import app  # noqa: E402  (import after sys.path fix)

# Vercel looks for `app` or `handler` -export both for compatibility
handler = app
