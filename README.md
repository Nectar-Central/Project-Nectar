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

The tray hosts everything in one process — the decryptor *and* the presence
bridge — so presence needs no second terminal:

```powershell
dist\BuildNowTray.exe                 # packaged build
# or from source:
pip install -r companion/tray/requirements-tray.txt
python companion/tray/tray_app.py
```

The bare bridge (no tray, no decryptor) is the dev path:

```bash
pip install pypresence
python companion/rich_presence.py
```

---

## Userscript Install

1. Install [Violentmonkey](https://violentmonkey.github.io/) or [Tampermonkey](https://www.tampermonkey.net/)
2. Import `companion/buildnow-companion.user.js` (Tampermonkey → Dashboard → Create → paste → Save)
3. Open [BuildNow GG on CrazyGames](https://www.crazygames.com/game/buildnow-gg) — a **Link Discord** modal appears once

> Paste the token from `/register` in Discord, or click **Guest** to play without tracking. The banner shows **BNC active** when capture is on.

### Sessions expire every 2 days

The server keeps a session for `SESSION_TTL_DAYS = 2` and then answers `Session expired - please re-link`. The userscript notices that on any API reply (capture included), drops the dead session and quietly re-links with the saved login token — the same refresh the boot path does, so most players never see anything. When the saved token no longer works either, a **Session expired** modal takes over the screen: run `/refresh-token` in the Nectar Discord server, paste the token it DMs you and hit **Save & reload** (or **Reload page** to re-link from the start, **Not now** to play on without tracking). `onSessionExpired()` is the single entry point, so one endpoint noticing the expiry is enough for every other caller to recover.

### When Tampermonkey can't run (CrazyGames iframe)

The game runs inside an iframe that can block userscripts.

1. Right-click the game → Inspect → in the frame selector pick `buildnow-gg.html`
2. Paste the contents of `companion/buildnow-companion.user.js` into that frame's console, Enter

---

## Discord Rich Presence

Keep the Discord desktop app running while the bridge is active.

```bash
pip install pypresence
python companion/rich_presence.py
```

The bridge formats what the userscript posts (F4 → Presence → *Show what I'm
playing on Discord*) and drives Discord through
[`pypresence`](https://qwertyquerty.github.io/pypresence/): it owns the pipe and
the handshake, this side only maps facts onto `Presence.update(...)` — including
the clickable-URL fields (`details_url`/`state_url`/`large_url`) and the two
buttons, *Join Nectar* always and *Join my party* for a private party pID. It
binds 127.0.0.1:8765 and walks forward to 8766-8769 when the decryptor already
owns that port; the userscript auto-detects which one answered, so the tray is
the only thing a user has to launch. A different Discord application goes in
`--client-id YOUR_APPLICATION_ID`.

What the card shows: the app name renders as **Playing Buildnow.gg with Nectar**,
and the resting state is **In Lobby** with the Join Nectar button — the tray sends
that the moment it starts, before the game is even open. Line one is only *where
you are* (**In Match** / **In Lobby**, plus the session name when there is one)
and line two carries the mode, the occupancy and the party — a party is a thing
you are in, not a place you are at, so a party in the lobby reads "In Lobby" over
"in a party" rather than repeating itself. With nothing picked yet line two reads
**Idle** rather than announcing the Free Mode the client boots on (df is the
client's default, not a mode anybody chose; a real match keeps its own mode). Any
party that carries a shareable code gets a **Join my party** button next to Join
Nectar — the code comes from the room the player is standing in, or from the
session's own party code for a party formed in the lobby. The userscript pushes
every 15s (scene, mode, party, occupancy, never rank/points), and going quiet for
45s returns the card to the resting state rather than leaving a stale match on it.
F4 → Presence is just the switch, the status line and the tray button. The same
panel has a **Nerds** tab repainting twice a second with live FPS, socket/packet
rates, **latency** (round trips timed on the game socket itself — Photon answers
a request with a response carrying the same op code, so the reading is pure RTT —
plus the stats poll's own round trip, jitter and a 30-sample history), the Photon
cluster it landed on, and GPU and draw-call stats. One caveat the panel states out
loud: the socket can only be timed on what the client sends it, which in real
captures is a handful of ops per minute (the ~10/s traffic is a one-way
RaiseEvent that nothing answers), so the ping is exact but arrives in bursts —
the age badge beside it and the API round trip are the parts that keep moving.
An always-on **FPS chip** sits in the top-right corner of the game: frames per
second repainting four times a second plus the session's worst and best second,
click-through (it never eats a mouse input) and stepping aside only for the
full-screen panels, never for the drawer. `__BNC__.fpsChip` reports where it
stands (`mounted` / `display` / `hiddenBy` / `rect`).

The legacy Node bridge still works as a fallback:

```bash
npm run discord
node companion/discord-presence.js --client-id YOUR_DISCORD_APPLICATION_ID
```

The decryptor exposes `http://127.0.0.1:8765/status`, the bridge
`http://127.0.0.1:8765/health` and `/presence`.

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
