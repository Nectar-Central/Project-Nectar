"""
connected_to_api_and_userscript.py -- Full flow: userscript -> FastAPI -> Engine -> your script

This is the "does the job" example you asked for:
  userscript (thin sender, any browser) --POST /api/capture--> FastAPI (server/main.py)
  FastAPI feeds hex to Engine (server/lib/photon_engine, in-process, per session_id)
  Your script (this file) polls GET /api/session/{id} and gets live SessionView
                OR reads the POST response's `packets` + `session` directly.

You don't need to understand F3/GpBinary -- just read Player.username, skin, etc.

Run:
  1. Start Python backend:  uvicorn server.main:app --host 127.0.0.1 --port 3000 --reload
  2. Install userscript 1.0.50+ in Tampermonkey, set login token via /register
  3. Play a game -- userscript will POST rawHex to /api/capture
  4. Run this script:       python -m server.examples.connected_to_api_and_userscript --session sess_mth --api http://127.0.0.1:3000

It will print live state as packets arrive, like discord.py's on_ready.

Prereqs:
  pip install requests
"""

import sys
import time
import argparse
from pathlib import Path

# Allow running as `python server/examples/connected_to_api_and_userscript.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import requests

# For local testing without a live game, you can also feed a capture file directly
# to the Engine without HTTP -- see full_session.py for that. This example shows
# the *connected* path (userscript -> API -> your script).
from server.lib.photon_engine import Engine
from server.lib.photon_engine.enums import GameMode

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_API = "http://127.0.0.1:3000"
DEFAULT_SESSION = "sess_mth"  # replace with your real sess_... from GM storage or POST response


def poll_session_state(api_base: str, session_id: str, interval: float = 1.0):
    """
    Simple poller -- GET /api/session/{id} every second.

    This endpoint is provided by server/routes/session_state.py (new).
    It just does `engine.get_session(session_id).to_dict()` -- no DB, no calc.
    If you prefer, you can read the POST /api/capture response directly instead of polling:
        resp = requests.post(f"{api_base}/api/capture", json={...})
        print(resp.json()["packets"][0]["kill"], resp.json()["session"]["players_count"])
    Polling is better for an overlay that wants live state even when no packets are flowing
    (zone ticks, ready changes).
    """

    url = f"{api_base.rstrip('/')}/api/session/{session_id}/state"

    print(f"Polling {url} every {interval}s -- Ctrl+C to stop")
    print("Waiting for userscript to POST packets -> Engine -> this poll will show live data\n")

    try:
        while True:
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code == 404:
                    print(f"[{time.strftime('%H:%M:%S')}] session {session_id} not found yet -- play a game to create it")
                elif resp.ok:
                    data = resp.json()
                    # data is SessionView.to_dict() -- real names, no indices
                    me = data.get("me") or {}
                    players = data.get("players") or {}
                    print(
                        f"[{time.strftime('%H:%M:%S')}] "
                        f"me={me.get('username')} skin={me.get('skin')} "
                        f"gm={data.get('game_mode')} curScn={data.get('cur_scn')} "
                        f"players={data.get('players_count')}/{data.get('max_players')} "
                        f"zone={data.get('zone', {}).get('time')}s "
                        f"names={[p['username'] for p in players.values()]}"
                    )

                    # Your own win logic (you own it, engine doesn't):
                    # if data["players_count"] == 1 and me.get("is_alive"):
                    #     print("WIN candidate!")

                else:
                    print(f"GET {url} -> {resp.status_code} {resp.text[:200]}")

            except Exception as exc:
                print(f"poll error: {exc}")

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\nStopped.")


def demo_local_engine_without_http():
    """
    Same logic, but without HTTP -- directly feed hex to Engine.
    Useful for testing with a .jsonl capture file, no server needed.
    This is what POST /api/capture does internally.

    Packet JSON that userscript sends (what POST /api/capture receives):
        {
            "op": 227, "opName": "CreateRoom",
            "playerProps": {"255": "true jew", "pfid": "CB6F...", "skid": "cct_..."}
        }
    Engine turns that into:
        Player(actor_nr=2, username="true jew", game_id="CB6F...", skin="cct_...")
    """

    from server.lib.photon_api import Photon

    engine = Engine()
    sid = "local_demo_sess"

    # Simulate what POST /api/capture does for each record:
    fake_records = [
        # In real life, these hex strings come from records[].rawHex in the POST body
        "f302e301...",  # CreateRoom with playerProps 255="true jew" (first join -- sets me)
    ]

    print("Local engine demo (no HTTP):")
    for hx in fake_records:
        # Engine does: parse -> Player/Room/Zone dataclasses, per session_id, no collision
        pkt = engine.feed(sid, hx)

        # pkt is AbstractPacket with real names:
        # pkt.packet == PacketType.CREATE_ROOM, pkt.player.username == "true jew"
        print(f"  fed {hx[:16]}... -> packet={pkt.packet.value if hasattr(pkt.packet,'value') else pkt.packet} player={pkt.player.username if pkt.player else None}")

        # Live view
        view = engine.get_session(sid)
        if view and view.me:
            print(f"    me: {view.me.username} skin={view.me.skin} game_id={view.me.game_id}")
            print(f"    players: {view.players_count}/{view.max_players} {[p.username for p in view.players.values()]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll live session state fed by userscript -> FastAPI -> Engine")
    parser.add_argument("--session", default=DEFAULT_SESSION, help="authSessionId (sess_...) from GM storage or validate-token")
    parser.add_argument("--api", default=DEFAULT_API, help="FastAPI base URL")
    parser.add_argument("--local-demo", action="store_true", help="Run local engine demo without HTTP")
    args = parser.parse_args()

    if args.local_demo:
        demo_local_engine_without_http()
    else:
        poll_session_state(args.api, args.session)
