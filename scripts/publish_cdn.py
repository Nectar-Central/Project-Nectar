"""Publish userscripts to the Supabase Storage CDN bucket.

Uploads the OBFUSCATED builds (run `npm run obfs` first):
  companion/buildnow-companion.obfs.user.js
      -> cdn/nectar-companion.latest.user.js
  companion/buildnow-companion.lite.obfs.user.js
      -> cdn/nectar-companion.lite.latest.user.js
  companion/buildnow-companion.competitive.obfs.user.js
      -> cdn/nectar-companion.competitive.latest.user.js

All three are published. Each is a SEPARATE product line with its own stable
name, so an install of one is never overwritten by another — the full script's
@updateURL points at the full object, and the competitive build re-enables its
own @updateURL/@downloadURL against the competitive object (the lite build
blanks them on purpose, so lite is never silently replaced by full).

The full path matches the @updateURL/@downloadURL header in the userscript,
so Tampermonkey auto-update picks it up.

Env (required): SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
Run: npm run obfs; if ($?) { python scripts/publish_cdn.py }
"""
import os
import sys
import time

import requests

FILES = {
    "companion/buildnow-companion.obfs.user.js": "nectar-companion.latest.user.js",
    "companion/buildnow-companion.lite.obfs.user.js": "nectar-companion.lite.latest.user.js",
    "companion/buildnow-companion.competitive.obfs.user.js": "nectar-companion.competitive.latest.user.js",
}
BUCKET = "cdn"


def main() -> int:
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        print("missing env: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
        return 2
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ok = True
    for local, remote in FILES.items():
        path = os.path.join(root, *local.split("/"))
        if not os.path.exists(path):
            print(f"SKIP {local}: file not found")
            ok = False
            continue
        with open(path, "rb") as f:
            data = f.read()
        url = f"{base}/storage/v1/object/{BUCKET}/{remote}"
        try:
            r = requests.post(
                url,
                data=data,
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/javascript; charset=utf-8",
                    "x-upsert": "true",
                },
                timeout=60,
            )
        except Exception as exc:
            print(f"FAIL {remote}: request error: {exc}")
            ok = False
            continue
        if r.status_code not in (200, 201):
            print(f"FAIL {remote}: HTTP {r.status_code}: {r.text[:200]}")
            ok = False
            continue
        # verify: download back and compare bytes (retry — the public edge
        # cache can serve a stale copy for a few seconds right after upsert)
        match = False
        for attempt in range(3):
            try:
                v = requests.get(f"{base}/storage/v1/object/public/{BUCKET}/{remote}", timeout=60)
                match = v.content == data
            except Exception as exc:
                print(f"WARN {remote}: verify attempt {attempt + 1} failed: {exc}")
            if match:
                break
            time.sleep(10)
        print(f"OK {remote}: {len(data)} bytes, verify={'match' if match else 'MISMATCH'}")
        if not match:
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
