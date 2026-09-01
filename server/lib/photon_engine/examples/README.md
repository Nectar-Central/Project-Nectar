# Photon Engine — 50 Examples

Run: `python -m server.lib.photon_engine.examples.01_basic_parse`

## Core (01-20)
| # | File | What |
|---|------|------|
|01|01_basic_parse.py|hex → EnginePacket|
|02|02_is_essential.py|fast 3-byte filter|
|03|03_human_sentence.py|human readable logs|
|04|04_engine_single_session.py|single SessionView feed|
|05|05_engine_multi_session.py|one Engine many sessions|
|06|06_session_view_me.py|view.me lifecycle|
|07|07_session_view_players.py|players dict|
|08|08_session_view_zone.py|zone time/phase|
|09|09_session_view_room_party.py|room/party|
|10|10_killfeed_detection.py|is_kill detection|
|11|11_death_vs_kill.py|self kill vs death|
|12|12_alive_eliminated.py|alv transitions|
|13|13_join_leave_dedupe.py|dedupe by username|
|14|14_state_transition.py|lobby→match|
|15|15_game_mode_inference.py|GameMode|
|16|16_zone_phase_change.py|zone phase 0→1|
|17|17_replay_from_collect_file.py|replay collect file|
|18|18_batch_from_capture_records.py|batch like /api/capture|
|19|19_win_defeat_detection.py|win/defeat check|
|20|20_full_integration.py|JSON/YAML + Engine|

## Collect → Engine (21-50)
Hotkeys: `Ctrl+Shift+C` toggle, `S` save, `L` load, `R` replay. Badge `#bn-collect-badge`.

| # | File | What |
|---|------|------|
|21|21_collect_toggle_overlay.py|toggle/badge|
|22|22_collect_download_format.py|JSON format|
|23|23_collect_load_file.py|load via L|
|24|24_collect_replay_basic.py|basic replay|
|25|25_collect_replay_with_engine.py|inspect view per feed|
|26|26_collect_replay_filter_essential.py|filter essential|
|27|27_collect_replay_killfeed_analysis.py|killfeed list|
|28|28_collect_replay_player_recovery.py|late join via killfeed|
|29|29_collect_replay_zone_timeline.py|zone timeline|
|30|30_collect_replay_state_machine.py|LOBBY→MATCH|
|31|31_collect_replay_win_detection.py|win check|
|32|32_collect_replay_error_injection.py|truncated hex|
|33|33_collect_split_by_session.py|multi-session file|
|34|34_collect_merge_two_files.py|merge files|
|35|35_collect_convert_to_yaml_logs.py|JSON→YAML logs|
|36|36_collect_analyze_gamemodes.py|game modes in file|
|37|37_collect_extract_skins.py|skid/pid|
|38|38_collect_validate_packets.py|validate|
|39|39_collect_benchmark_feed_speed.py|benchmark 1000 feeds|
|40|40_collect_replay_with_delay.py|delay via t|
|41|41_collect_live_vs_replay_comparison.py|determinism|
|42|42_collect_dedup_test.py|2x replay dedup|
|43|43_collect_partial_replay.py|first N packets|
|44|44_collect_export_for_bot.py|to_dict for bot|
|45|45_collect_processor_dry_run.py|what DB would get|
|46|46_collect_statistics.py|Counter by type|
|47|47_collect_visualize_timeline.py|sorted by t|
|48|48_collect_find_bugs.py|assert invariants|
|49|49_collect_diff_two_sessions.py|isolated sessions|
|50|50_full_collect_to_production.py|collect→engine→processor→bot|

Collect files: `logs/photon-capture-*.json` `{meta, packets:[{rawHex,seq,dir,url,t,label}]}` → `engine.feed(sid, rec["rawHex"])` in order. Processor syncs `view.me` → `bngc_users`; bot `GET /api/session/{id}/state`.

## Rooms (51-55) — NEW
| # | File | What |
|---|------|------|
|51|51_room_scoped_session.py|Session → rooms isolation|
|52|52_room_players_isolation.py|players + skin per room|
|53|53_room_kills_scoped.py|killfeed per room|
|54|54_room_query_via_bot.py|GET /state rooms dict|
|55|55_room_leave_scoped.py|leave per room|

All events: `CreateRoom/JoinRoom` → new `RoomView(room_id=RoomName)`; `zone`/`players`/`kills` scoped to `view.current_room` (`view.rooms[room_id]`). Top-level `view.players` etc are back-compat proxies to current room.

## Zone / Teams / Playtime (56-60) — NEW
| # | File | What |
|---|------|------|
|56|56_zone_mmss_timer.py|Zone mm:ss countdown, is_at, is_between|
|57|57_zone_time_tracking.py|Phase 5:00→0:00 tracking|
|58|58_teams_duo.py|Duo teams via Party code|
|59|59_playtime_per_gm.py|Playtime per GM + total|
|60|60_playtime_global.py|Lobby vs match global|

Zone: `room.zone.set_time(300,"0")` → `remaining_mmss()` `04:30`, `is_at(4,30)`, `is_between((5,0),(4,0))`. Teams: `room.teams[party_code].members` for WWZW/BF2V2. Playtime: `session.playtime.{total_seconds, per_gm, lobby_seconds}` auto-ticked on each feed.

