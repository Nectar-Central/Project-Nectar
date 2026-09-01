# BuildNow Companion

Photon WebSocket capture for BuildNow GG → live stats on **https://api-compbng.vercel.app**

Captures killfeed, player names, room ID/code, party status, alive/dead state,
and player count from Photon WebSocket traffic in Unity WebGL games.

## What it captures

| Field | Source |
|-------|--------|
| **Killfeed** | evCode 101 — kills, knocks, leaves |
| **Player names** | PlayerProperties (key 255) |
| **Room ID** | JoinRoom/CreateRoom response (GUID) |
| **Party code** | `pID` game property (e.g. `4GHL7`) |
| **In party** | `Party` boolean game property |
| **Alive/dead** | `alv` player property |
| **Player count** | Tracked from Join/Leave events |
| **Game mode** | `gm` property (DF, zw_lg, BF2V2) |
| **Scene** | `curScn` property (MainLobby, MainWorld, etc.) |

---

## Quick Start

### Prerequisites

- Node.js 18+
- Chrome or Edge + Violentmonkey / Tampermonkey
- Discord app (optional, for Rich Presence)

### Install

```bash
git clone https://github.com/<org>/websockets-capture.git
cd websockets-capture
npm install
```

### Run (Development — remote API)

The userscript points at `https://api-compbng.vercel.app` by default.
For local dev override: `GMSet('bnc_api_base','http://127.0.0.1:8000')` in console or `?api=local`.

```bash
npm run decryptor   # optional local decryptor on 127.0.0.1:8765
```

### Run (Rich Presence only)

```bash
node companion/decryptor.js
# In another terminal:
set DISCORD_CLIENT_ID=your_app_id
node companion/discord-presence.js
```

---

## Userscript Install

1. Install [Violentmonkey](https://violentmonkey.github.io/) or [Tampermonkey](https://www.tampermonkey.net/)
2. Import `companion/buildnow-companion.user.js` (Tampermonkey → Dashboard → Create → paste → Save)
3. Open [BuildNow GG on CrazyGames](https://www.crazygames.com/game/buildnow-gg) — a **Link Discord** modal appears once

> Paste the token from `/register` in Discord, or click **Guest** to play without tracking. The banner shows **BNC active** when capture is on.

### When Tampermonkey can't run (CrazyGames iframe)

The game runs inside an iframe that can block userscripts.

1. Right-click the game → Inspect → in the frame selector pick `buildnow-gg.html`
2. Paste the contents of `companion/buildnow-companion.user.js` into that frame's console, Enter

---

## Discord Rich Presence

Keep Discord running while the bridge is active.

```bash
npm run discord
```

To use a different Discord application:

```bash
node companion/discord-presence.js --client-id YOUR_DISCORD_APPLICATION_ID
```

The decryptor exposes `http://127.0.0.1:8765/status` and `/presence`.

---

## Build from Source

### Requirements

- Node.js 18+ — https://nodejs.org
- Python 3.11+ (for tray exe only)

### Build tray exe

```powershell
pip install -r companion/tray/requirements-tray.txt
companion\tray\build-exe.bat
# → dist/BuildNowTray.exe
```

---

## CLI Usage

```bash
# Offline decode of a capture
node companion/decryptor.js companion/captures/foo.jsonl
node companion/decryptor.js --json companion/captures/foo.jsonl > decoded.json

# Or use legacy decode wrapper
node decode.js captures/<file>.jsonl --killfeed --rooms --players
```

---

## Browser Console API

```js
// Global (rebranded, legacy alias still works)
window.__BUILDNOW_COMPANION__.sessionId
window.__BNC__.queueLen
window.__BUILDNOW_COMPANION__.flush()
window.__BUILDNOW_COMPANION__.clearToken()
window.__BUILDNOW_COMPANION__.toggleCollect() // Ctrl+Shift+C
window.__BUILDNOW_COMPANION__.saveCollect()   // Ctrl+Shift+S

// Legacy alias
window.__PHOTON_SNIFFER__
```

---

## Project Structure

```
websockets-capture/
├── companion/               # BuildNow Companion kit
│   ├── buildnow-companion.user.js  # Tampermonkey userscript (modal fixed)
│   ├── decryptor.js         # Node live server + offline decoder
│   ├── discord-presence.js  # Discord Rich Presence bridge
│   ├── tray/                # System tray app (Python)
│   └── bot/                 # Discord bot (Python)
├── server/                  # Python FastAPI — deployed to api-compbng.vercel.app
├── api/                     # Vercel entry (api/index.py → server.main:app)
├── docs/                    # Specs, plans, signal maps
├── scripts/                 # Utility scripts
└── package.json
```

---

## Tooling Reference

| Tool | Purpose |
|------|---------|
| `companion/buildnow-companion.user.js` | Tampermonkey userscript — hooks Photon, filters essentials, POSTs to api-compbng.vercel.app |
| `companion/decryptor.js` | Local decryptor + offline decoder (ANSI, zero-dep) |
| `companion/discord-presence.js` | Discord Rich Presence bridge (polls 127.0.0.1:8765/presence) |
| `tools/decoder.js` | Browser-safe wire decoder (UMD) — canonical parser |
| `tools/detector.js` | Match state machine — structured events |
| `tools/sender.js` | Ingest sender — batching, auth, backoff |

---

## Server API

Live at `https://api-compbng.vercel.app`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` / `/status` | GET | Health check |
| `/api/validate-token` / `/api/auth/verify` | POST | `{token}` → `{session_id, initial_token}` |
| `/api/capture` | POST | `{sessionId, records:[{seq,t,dir,url,rawHex}]}` + auth headers → `{packets, session, new_req_token}` |
| `/api/session/{id}/state` | GET | Live session view |

Override locally: `GMSet('bnc_api_base','http://127.0.0.1:8000')` or add `?api=local` to URL.

---

## Tests

```bash
npm test
```

## Docs

| File | Purpose |
|------|---------|
| `docs/spec.md` | Event engine spec |
| `docs/plan.md` | Phase plan |
| `docs/todo.md` | Task list |
| `docs/api.md` | Token-authed ingest spec |
| `docs/websocket-packets.md` | Photon framing spec |
