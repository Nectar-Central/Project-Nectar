"""
61 — Full test for ONE hand capture file (zw_lg / duo / boxfights / free)
Usage:
  python -m server.lib.photon_engine.examples.61_full_test_one_file logs/photon-capture-123.json
  python -m server.lib.photon_engine.examples.61_full_test_one_file logs/test_data.json --sid sess_test
  python -m server.lib.photon_engine.examples.61_full_test_one_file logs/engine.jsonl  # auto-detects engine log format

Tests: rooms isolation, zone mm:ss, teams (duo), playtime, players (skin/game_id), kills, invariants
Each line is commented for learning — see inline # comments.
"""
import sys, json, time, pathlib  # sys: path hack, json: parse file, time: benchmark, pathlib unused but kept
from pathlib import Path  # Path: file handling
# Allow running as `python -m ...` from project root — add parent 4 levels to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from server.lib.photon_engine import Engine  # Engine: holds SessionView per session_id, feed hex → mutates RoomView

def load_packets(path: Path):
    """Load packets from either collect JSON {meta, packets:[{rawHex}]} or JSONL (engine.jsonl)."""
    text = path.read_text(encoding="utf-8", errors="ignore").strip()  # read whole file, ignore bad utf8, strip whitespace
    if not text:  # empty file
        return [], {}
    # Try JSON {meta, packets:[{rawHex}]} — the collect download format (Ctrl+Shift+S)
    try:
        data = json.loads(text)  # parse JSON
        if isinstance(data, dict) and "packets" in data:  # collect file shape
            return data["packets"], data.get("meta", {})  # return packets list + meta
        if isinstance(data, list):  # rare: file is just a list of packets
            return data, {}
    except Exception:  # not JSON object, fall through to JSONL
        pass
    # Try JSONL (engine.jsonl or test_data.jsonl with one JSON per line) — engine structured logs, not feedable
    packets = []  # accumulator
    for line in text.splitlines():  # split by newline
        line=line.strip()  # trim
        if not line:  # skip empty lines
            continue
        try:
            obj=json.loads(line)  # parse one line
            # engine.jsonl lines are events, not rawHex — skip for engine feed
            if "rawHex" in obj:  # feedable collect packet
                packets.append(obj)
            elif "packets" in obj:  # line itself is a collect object
                packets.extend(obj["packets"])
            else:
                # engine event — not feedable, keep for zone/kill stats check
                packets.append({"_engine_event": obj})  # tag to distinguish later
        except Exception:  # bad line, ignore
            continue
    # If still empty and file is JSONL with rawHex per line, try again (fallback)
    if not packets:
        try:
            for line in text.splitlines():
                if "rawHex" in line:
                    packets.append(json.loads(line))
        except: pass
    return packets, {}  # return packets + empty meta for JSONL

def main():
    import argparse  # argparse: CLI args
    ap=argparse.ArgumentParser()  # create parser
    ap.add_argument("file", nargs="?", default="C:\\Users\\assko\\Desktop\\websockets-capture\\server\\logs\\photon-capture-1788230698873.json")  # positional file, default
    ap.add_argument("--sid", default="sess_full_test")  # --sid session id for Engine
    args=ap.parse_args()  # parse CLI
    path=Path(args.file)  # use CLI arg, not hardcoded — respects --sid file
    if not path.exists():  # file missing
        print(f"File not found: {path}")  # tell user
        print("Create one via userscript Ctrl+Shift+C → play → Ctrl+Shift+S")  # how to create
        sys.exit(1)  # exit with error

    packets, meta = load_packets(path)  # load packets + meta from file
    # Filter to feedable packets (must have rawHex hex string len>=6)
    feedable=[p for p in packets if isinstance(p.get("rawHex"), str) and len(p["rawHex"])>=6]  # only hex feedable
    engine_events=[p for p in packets if "_engine_event" in p]  # engine log lines for analysis

    print(f"Loaded {len(packets)} records, {len(feedable)} feedable, {len(engine_events)} engine-events, meta={meta}")  # summary
    if not feedable:  # no hex to feed — file is logs/engine.jsonl output, not input
        print("No feedable rawHex packets found.")  # explain
        if engine_events:  # it's engine log
            print("This looks like logs/engine.jsonl (output, not input). Use a collect file: logs/photon-capture-*.json")  # hint
            # Still analyze engine events for zone/teams
            kills=[e["_engine_event"] for e in engine_events if e["_engine_event"].get("event")=="killfeed"]  # extract kills
            print(f"Engine log kills: {len(kills)}")  # count
            for k in kills[:5]:  # show first 5
                print(f"  {k.get('killer')} -> {k.get('victim')}")  # killer -> victim
        sys.exit(0)  # exit, nothing to feed

    engine=Engine()  # create fresh Engine (holds many SessionViews, like pygame)
    sid=args.sid  # session id for this test (one per hand file)
    t0=time.time()  # benchmark start
    for rec in feedable:  # feed in order — order matters for room switching
        engine.feed(sid, rec["rawHex"])  # decrypt → EnginePacket → mutate RoomView → log
    elapsed=time.time()-t0  # benchmark end
    view=engine.get_session(sid)  # get SessionView for this sid (holds rooms dict)
    print(f"\nFed {len(feedable)} packets in {elapsed:.3f}s ({len(feedable)/elapsed:.0f}/s)")  # throughput

    # Summary — Session → Rooms → scoped data
    print(f"\n=== Session {sid} ===")  # header
    print(f"me: {view.me.to_dict() if view.me else None}")  # self player (actor -1, is_self True, skin/pickaxe)
    print(f"current_room: {view.current_room_id}")  # current RoomView id (RoomName UUID or lobby)
    print(f"rooms: {list(view.rooms.keys())}")  # all rooms in this session (isolation test)
    for rid, rv in view.rooms.items():  # iterate each RoomView
        # Room header: code, gm, state, curScn
        print(f"\n-- Room {rid[:12]} code={rv.code} gm={rv.game_mode.value if rv.game_mode else '?'} state={rv.state.value} curScn={rv.cur_scn} --")
        print(f"  players {rv.players_count}/{rv.max_players} ({len(rv.players)} in dict)")  # count (seeded or incremented) + dict len
        for p in list(rv.players.values())[:5]:  # show first 5 players
            # username skin gid alive ready — skin/gid now filled for others too (engine _other_props)
            print(f"    {p.username} skin={p.skin} gid={str(p.game_id)[:8] if p.game_id else None} alive={p.is_alive} ready={p.is_ready}")
        if len(rv.players)>5:  # more than 5
            print(f"    ... +{len(rv.players)-5} more")
        # Zone mm:ss countdown: set_time(300) → remaining_mmss() 05:00 → is_at(4,30) for your 4:30/3:00 checks
        print(f"  zone time={rv.zone.time} phase={rv.zone.phase} remaining={rv.zone.remaining_mmss()} initial={rv.zone.initial_time}")
        if rv.zone.time:  # if zone was set
            print(f"    is_at 4:30? {rv.zone.is_at(4,30)} is_between 5:00-4:00? {rv.zone.is_between((5,0),(4,0))}")  # your time checks
        print(f"  party is_party={rv.party.is_party} code={rv.party.code}")  # party for duos
        print(f"  teams: { {tid: t.to_dict() for tid,t in rv.teams.items()} if rv.teams else '{}' }")  # duo teams (WWZW/BF2V2)
        print(f"  kills {len(rv.recent_kills)} knocks {len(rv.recent_knocks)}")  # recent killfeed per room
        for k in rv.recent_kills[:5]:  # show kills
            print(f"    {k.instigator} -> {k.instigated} ({k.event_type})")  # incl leaves (left)
        if len(rv.recent_kills)>5:
            print(f"    ... +{len(rv.recent_kills)-5}")

    print(f"\n=== Playtime (session) ===")  # playtime global + per GM
    print(view.playtime.to_dict())  # total_seconds, per_gm, lobby_seconds, last_state
    print(f"  total {view.playtime.total_seconds}s = lobby {view.playtime.lobby_seconds}s + per_gm {view.playtime.per_gm}")  # breakdown

    print(f"\n=== to_dict (current room proxy + rooms) ===")  # bot reads this via GET /api/session/{id}/state
    d=view.to_dict()  # SessionView.to_dict() → {rooms, current_room_id, players (current), zone, playtime}
    print(f"  state={d['state']} gm={d['game_mode']} cur_scn={d['cur_scn']}")  # note snake_case cur_scn from to_dict
    print(f"  players_count={d['players_count']} zone={d['zone']}")

    # Invariants — catch dupes / count bugs early
    print(f"\n=== Invariants ===")
    ok=True  # flag
    for rid, rv in view.rooms.items():  # per room
        if rv.players_count <0:  # count should never be negative
            print(f"FAIL {rid} players_count negative")
            ok=False
        usernames=[p.username for p in rv.players.values()]  # check dupes
        if len(usernames)!=len(set(usernames)):  # deduped by engine, should not happen
            print(f"FAIL {rid} duplicate usernames {usernames}")
            ok=False
        else:
            print(f"OK {rid} no duplicates ({len(usernames)} players)")
        if rv.max_players and len(rv.players) > rv.max_players + 5:  # allow late join buffer
            print(f"WARN {rid} players {len(rv.players)} > max {rv.max_players}")
    if ok:
        print("All invariants OK")

    # Processor dry-run hint — what DB would get
    print(f"\n=== Next ===")
    print(f"Processor dry-run: await process_batch('{sid}', [p.to_dict() for p in packets], view)")  # writes bngc_game_events + bngc_users
    print(f"Bot: GET /api/session/{sid}/state → rooms, zone.remaining_mmss, teams, playtime")  # bot poll

if __name__=="__main__":
    main()  # run when called as `python -m ...`
