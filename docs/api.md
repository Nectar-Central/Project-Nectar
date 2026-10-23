# Nectar Companion -Public Website API

Base URL: `https://project-nectar.onrender.com`

All endpoints below are `GET`, return JSON, need no auth, and allow
cross-origin browser calls -embed them directly in the website.

```js
const API = "https://project-nectar.onrender.com";

async function getUser(gameUsername) {
  const r = await fetch(`${API}/api/user?game_username=${encodeURIComponent(gameUsername)}`);
  if (!r.ok) return null; // 404 = never played / not linked
  return r.json();
}
```

---

## GET /api/search -find players

| Param | Required | Notes |
|---|---|---|
| `q` (or `query`) | yes | min 2 chars; matches discord username, game username, game id |
| `limit` | no | default 10, max 25 |

```
GET /api/search?q=cis6led
```

```json
{
  "query": "cis6led",
  "count": 1,
  "results": [
    {
      "discord_id": "129547...",
      "discord_username": "cis6led",
      "game_username": "dc: cis6led",
      "game_id": "CB6F...",
      "skin": "cct_123",
      "kills": 21, "deaths": 29, "wins": 3, "defeats": 16,
      "kd": 0.72, "win_rate": 0.158,
      "playtime_global": 5420
    }
  ]
}
```

Errors: `400` missing/short `q` · `500` server not configured.

---

## GET /api/stats -full stats + live session

| Param | Required | Notes |
|---|---|---|
| `session_id` (or `sid`) | one of these | live session id from the userscript |
| `discord_id` (or `did`) | one of these | Discord user id |
| POST JSON body | alternative | `{ "session_id": ..., "discord_id": ... }` |

```
GET /api/stats?discord_id=129547...
```

```json
{
  "discord_id": "129547...",
  "discord_username": "cis6led",
  "game_username": "dc: cis6led",
  "game_id": "CB6F...",
  "skin": "cct_123",
  "pickaxe": "Pickaxe_42",
  "kills": 21, "deaths": 29, "wins": 3, "defeats": 16,
  "playtime_global": 5420,
  "playtime_per_gm": { "zw_lg": 3200, "lobby": 900 },
  "kd": 0.72, "win_rate": 0.158,
  "is_locked": false,
  "lobby_music_url": "music/track.mp3",
  "lobby_music_volume": 0.5,
  "session": {
    "game_mode": "zw_lg",
    "cur_scn": "ZoneWars_LateGame_12Players",
    "state": "match",
    "players_count": 8,
    "max_players": 12,
    "zone": { "time": 45, "phase": "3" },
    "room_id": "7e03f28f...",
    "short_code": null
  }
}
```

`session` is `null` when the player has no live engine session (offline or
no packets fed yet) -user stats are still returned. Raw `game_mode` values:
`DF`, `zw`, `zw_lg`, `wwzw`, `WW`, `BF`, `BF2V2`, `VA`, `tdm_bvr`, `R_1V1`.
Map them to display names client-side.

Errors: `400` no identifier · `500` server not configured.

---

## GET /api/user -public player profile

| Param | Required | Notes |
|---|---|---|
| `discord_id` | one of these | exact match |
| `game_username` | one of these | exact match |
| `game_id` | one of these | exact match |

```
GET /api/user?game_username=dc:%20cis6led
```

```json
{
  "discord_id": "129547...",
  "discord_username": "cis6led",
  "game_username": "dc: cis6led",
  "game_id": "CB6F...",
  "skin": "cct_123",
  "pickaxe": "Pickaxe_42",
  "kills": 21, "deaths": 29, "wins": 3, "defeats": 16,
  "kd": 0.72, "win_rate": 0.158,
  "playtime_global": 5420,
  "playtime_per_gm": { "zw_lg": 3200 }
}
```

Only public stat fields are returned -no tokens, secrets, lock IPs, or
session data. Errors: `400` no identifier · `404` user not found.

---

## Notes for website embeds

- Users with no matches yet return zeros -show an empty state ("play a
  match to appear here") instead of a broken card.
- `game_username` values may carry a `dc: ` prefix -display as-is.
- Full per-match history lives behind the Discord bot (`/sessions`,
  `/recentmatches`, `/replay`, `/killfeed`); the endpoints above are the
  website-safe surface.
