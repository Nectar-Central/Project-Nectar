"""
scoring.py — the one scoring brain for comp and ranked.

Every surface (comp dashboard, /ranked tourney mirror, /ranked/rooms admin,
userscript overlays, bot embeds) must read the same standings from the same
event rows, so the match engine + aggregation live here as pure functions:

  compute_match_points     one sealed match -> per-player rows
                           (static table: {kill, win, placement})
  dynamic_rows             the ranked dynamic economy as a strategy around
                           the same engine: comp math for placements/wins,
                           injected per-kill upset awards
  group_standings_by_team  fold a player board into team rows
  aggregate_standings      sum per-match rows into a series board
  compute_tourney_pr       performance rating over an aggregated board

Strategies share the engine by construction: comp's static table is the
base strategy, ranked's dynamic economy feeds the same engine a zeroed
kill column and then injects per-kill awards. Golden cross-tests pin that
identical event sets produce structurally identical standings under both
strategies, and that rescoring a sealed match reproduces its frozen table
exactly.

Pure module: no I/O, no Supabase, no server-state imports.
"""

DEFAULT_TABLE = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}


def compute_match_points(events: list[dict], table: dict | None = None,
                         winner_override: str | None = None,
                         placements: dict | None = None,
                         excluded: set | list | None = None,
                         awards: dict | None = None) -> list[dict]:
    """Points for ONE sealed match. Pure, unit-tested.

    events: event rows {etype, discord_id, actor_name, alive}.
    Kills count per mapped discord_id only (twins stay null upstream).
    excluded: discord IDs removed mid-match (left) — ignored in kills,
    placement, winner and output rows.
    awards: manual point awards {did: pts} (dashboard +pts) folded per row.
    Placement: manual map first ({did: place}), else reverse alv-death order
    (last alive first). Winner: override, else manual P1, else last alive,
    else None (kills-only scoring). Anyone mapped on any event row
    (knock/leave/alive-True included) boards even at zero kills/placement,
    so manual subs via the add-player endpoints show at 0 pts.
    Row totals floored at 0.
    Returns [{discord_id, name, kills, placement, won, points}] sorted desc.
    """
    table = table or dict(DEFAULT_TABLE)
    try:
        place_pts = list(table.get("placement") or [])
        kill_pts = int(table.get("kill", 1))
        win_pts = int(table.get("win", 3))
    except Exception:
        place_pts, kill_pts, win_pts = list(DEFAULT_TABLE["placement"]), 1, 3
    kills: dict[str, int] = {}
    names: dict[str, str] = {}
    seen: set[str] = set()  # every mapped did in any event row (knock/leave/
    # alive-True included): participants with zero kills still board at 0 pts
    # so the dashboard can show/add them without inventing stats.
    death_order: list[str] = []  # first out -> last out
    seen_dead: set[str] = set()
    try:
        xset = {str(x) for x in (excluded or []) if str(x or "").strip()}
    except Exception:
        xset = set()
    for e in (events or []):
        try:
            et = (e or {}).get("etype")
            did = (e or {}).get("discord_id")
            if did and str(did) in xset:
                continue  # removed mid-match: ignored everywhere
            if did:
                seen.add(str(did))
            if et == "kill" and did:
                kills[did] = kills.get(did, 0) + 1
                if (e or {}).get("actor_name") and did not in names:
                    names[did] = str(e["actor_name"])
            elif et == "alv" and did and (e or {}).get("alive") is False:
                if did not in seen_dead:
                    seen_dead.add(did)
                    death_order.append(did)
                if (e or {}).get("actor_name") and did not in names:
                    names[did] = str(e["actor_name"])
            elif did and (e or {}).get("actor_name") and did not in names:
                names[did] = str(e["actor_name"])
        except Exception:
            continue
    order = list(reversed(death_order))  # last alive first
    try:
        order = [d for d in order if d not in xset]
    except Exception:
        pass
    winner = None
    try:
        if winner_override and str(winner_override) not in xset:
            # host-set winner always counts (even with no kill/alv rows —
            # manual matches start empty). Falls back to alv order below.
            winner = str(winner_override)
        elif order:
            winner = order[0]
    except Exception:
        winner = None
    placement_of = {did: i + 1 for i, did in enumerate(order)}
    # Manual placement override (dashboard): exact places beat alv order;
    # winner falls back to manual P1 when no override is set.
    try:
        man = {str(k): int(v) for k, v in (placements or {}).items()
               if str(k or "").strip() and str(k) not in xset}
    except Exception:
        man = {}
    if man:
        try:
            known = set(list(kills) + order + list(man))
            placement_of = {did: man[did] for did in sorted(man, key=lambda d: man[d]) if did in known}
            if not winner:
                first = sorted(man.items(), key=lambda kv: kv[1])
                if first and first[0][0] in known:
                    winner = first[0][0]
        except Exception:
            pass
    # Tied P1 shares the win: every manual P1 holder gets the full WIN
    # bonus (no split). Without manual placements only the winner bonuses.
    try:
        p1_tied = {did for did, p in (man or {}).items() if int(p) == 1}
    except Exception:
        p1_tied = set()
    rows = []
    try:
        award_map = {str(k): int(v) for k, v in (awards or {}).items()
                     if str(k or "").strip() and str(k) not in xset}
    except Exception:
        award_map = {}
    universe = set(list(kills) + order + list(man))
    universe |= seen  # participants (knock/leave/alive rows) board at 0
    if winner and winner not in xset:
        universe.add(winner)
    universe |= set(award_map)
    for did in sorted(universe):
        k = kills.get(did, 0)
        p = placement_of.get(did)
        pts = k * kill_pts
        if (p1_tied and did in p1_tied) or (not p1_tied and winner and did == winner):
            pts += win_pts
        if p and p <= len(place_pts):
            pts += place_pts[p - 1]
        pts += award_map.get(did, 0)
        pts = max(0, pts)  # totals never go negative
        rows.append({"discord_id": did, "name": names.get(did) or did[:6],
                     "kills": k, "placement": p,
                     "won": bool((p1_tied and did in p1_tied) or (not p1_tied and winner and did == winner)),
                     "points": pts})
    rows.sort(key=lambda r: (r["points"], r["kills"]), reverse=True)
    return rows


def dynamic_rows(events: list, table: dict, winner: str | None,
                 placements: dict | None = None,
                 kill_awards: list[dict] | None = None,
                 excluded: set[str] | list[str] | None = None,
                 point_awards: dict | None = None,
                 default_win: int = 50) -> list[dict]:
    """Ranked dynamic economy strategy: the shared engine, then kill awards.

    The engine runs with kill=0 (per-kill value is upset-based, not a flat
    constant), the table's win/placement purse intact, then the frozen
    per-kill awards (kill_awards: [{killer, victim, nth, points}]) are
    injected per player and rows re-sorted. default_win backs tables that
    somehow lack a win entry (legacy rows); the live dynamic tables always
    carry one.
    """
    base = {"kill": 0, "win": int(table.get("win", default_win)),
            "placement": list(table.get("placement") or [])}
    rows = compute_match_points(events, base, winner, placements or None,
                                excluded, point_awards)
    bonus: dict[str, int] = {}
    try:
        for award in (kill_awards or []):
            did = str((award or {}).get("killer") or "").strip()
            if did:
                bonus[did] = bonus.get(did, 0) + int((award or {}).get("points") or 0)
    except Exception:
        pass
    for row in rows:
        try:
            did = str(row.get("discord_id"))
            row["points"] = int(row.get("points") or 0) + bonus.get(did, 0)
        except Exception:
            continue
    rows.sort(key=lambda r: (int(r.get("points") or 0), int(r.get("kills") or 0)), reverse=True)
    return rows


def _team_display_label(label) -> str | None:
    """A `team_label` worth showing to a human, or None.

    The bot stores a duo's label as raw mention syntax (`<@a> / <@b>`, or
    `<@a> <@b>` from /tourney-add-player) because that is what renders in a
    Discord thread. Anywhere else it is just a column of discord ids: the
    dashboard's Player column showed them instead of names. A mention-shaped
    label is therefore not a label, and the member display names are used
    instead. A real human label ("Champs") is still honoured. Never raises.
    """
    try:
        s = str(label or "").strip()
        if not s or "<@" in s:
            return None
        return s
    except Exception:
        return None


def group_standings_by_team(board: list[dict], regs: list[dict] | None,
                            names: dict | None = None) -> list[dict]:
    """Fold a per-player series board into team rows. Pure, unit-tested.

    Teams come from accepted regs ({team_label, member_ids}); each team acts
    as one player sharing summed points/kills/wins. Display name is the
    label when set, else @a & @b over member display names. Unregistered
    players (played but never registered) stand alone under their own name.
    Rows carry member_ids so the dashboard can still resolve individuals.
    """
    try:
        regs = regs or []
        names = names or {}
        team_of: dict[str, int] = {}
        teams: list[dict] = []
        for r in regs:
            try:
                members = [str(x) for x in ((r or {}).get("member_ids") or []) if x]
            except Exception:
                members = []
            if not members:
                continue
            label = _team_display_label((r or {}).get("team_label"))
            teams.append({"label": label, "member_ids": members,
                          "kills": 0, "wins": 0, "points": 0, "matches": 0,
                          "avg_w": 0.0, "avg_n": 0})
            idx = len(teams) - 1
            for m in members:
                team_of.setdefault(m, idx)
        solos: dict[str, dict] = {}
        for row in (board or []):
            try:
                did = str((row or {}).get("discord_id") or "")
                if not did:
                    continue
                k = int((row or {}).get("kills") or 0)
                w = int((row or {}).get("wins") or 0)
                pts = int((row or {}).get("points") or 0)
                mt = int((row or {}).get("matches") or 0)
                nm = (row or {}).get("name") or names.get(did) or did[:6]
                try:
                    av = (row or {}).get("avg_placement")
                    av = float(av) if av is not None else None
                except Exception:
                    av = None
                if did in team_of:
                    t = teams[team_of[did]]
                    t["kills"] += k
                    t["wins"] += w
                    t["points"] += pts
                    t["matches"] = max(t["matches"], mt)
                    if av is not None and mt > 0:
                        t["avg_w"] += av * mt
                        t["avg_n"] += mt
                else:
                    s = solos.setdefault(did, {"member_ids": [did], "kills": 0,
                                               "wins": 0, "points": 0, "matches": 0,
                                               "name": nm, "avg_w": 0.0, "avg_n": 0})
                    s["kills"] += k
                    s["wins"] += w
                    s["points"] += pts
                    s["matches"] = max(s["matches"], mt)
                    if av is not None and mt > 0:
                        s["avg_w"] += av * mt
                        s["avg_n"] += mt
            except Exception:
                continue

        def _avg_of(t) -> float | None:
            try:
                n = int(t.get("avg_n") or 0)
                return round(float(t.get("avg_w") or 0.0) / n, 1) if n > 0 else None
            except Exception:
                return None

        out = []
        for t in teams:
            try:
                disp = [str(names.get(m) or m[:6]) for m in t["member_ids"]]
            except Exception:
                disp = [str(m)[:6] for m in t["member_ids"]]
            name = t["label"] or ("@" + " & @".join(disp) if disp else "?")
            out.append({"team": True, "team_label": t["label"],
                        "name": name, "member_ids": t["member_ids"],
                        "kills": t["kills"], "wins": t["wins"],
                        "points": t["points"], "matches": t["matches"],
                        "avg_placement": _avg_of(t)})
        for did, s in solos.items():
            out.append({"team": False, "discord_id": did, "name": s["name"],
                        "member_ids": s["member_ids"], "kills": s["kills"],
                        "wins": s["wins"], "points": s["points"],
                        "matches": s["matches"], "avg_placement": _avg_of(s)})
        out.sort(key=lambda r: (r["points"], r["kills"]), reverse=True)
        return out
    except Exception:
        return list(board or [])


def aggregate_standings(per_match: list[list[dict]]) -> list[dict]:
    """Sum match rows into a series board. Pure, unit-tested.

    Adds avg_placement (mean of per-match placements, 1 decimal; None when
    the player has no placed match, e.g. kills-only scoring).
    """
    tot: dict[str, dict] = {}
    try:
        for rows in (per_match or []):
            for r in (rows or []):
                did = str((r or {}).get("discord_id") or "")
                if not did:
                    continue
                d = tot.setdefault(did, {"discord_id": did, "name": (r or {}).get("name") or did[:6],
                                         "kills": 0, "wins": 0, "points": 0, "matches": 0,
                                         "placements": []})
                d["kills"] += int((r or {}).get("kills") or 0)
                d["wins"] += 1 if (r or {}).get("won") else 0
                d["points"] += int((r or {}).get("points") or 0)
                d["matches"] += 1
                try:
                    p = (r or {}).get("placement")
                    if p is not None:
                        d["placements"].append(int(p))
                except Exception:
                    pass
    except Exception:
        pass
    out = []
    for d in tot.values():
        try:
            pl = d.pop("placements", [])
            d["avg_placement"] = round(sum(pl) / len(pl), 1) if pl else None
        except Exception:
            d["avg_placement"] = None
        out.append(d)
    out.sort(key=lambda r: (r["points"], r["kills"]), reverse=True)
    return out


def compute_tourney_pr(board: list[dict], per_match: list[list[dict]] | None = None) -> list[dict]:
    """Tourney PR per player. Pure, unit-tested.

    Formula: round(2*(kills + 3*wins + max(0, players-avgPlacement+1))/sqrt(players), 1)
    players = avg lobby size across sealed matches (mean of per-match roster
    sizes). avg_placement None (no placed match) -> placement term 0.
    sqrt normalization (not /players): beating more players pays more, so a
    dominant win in a big lobby outranks a quiet win in a small one.
    Returns [{discord_id, name, pr, kills, wins, matches, avg_placement,
    players}] sorted by pr desc. Empty when no matches/players.
    """
    try:
        sizes = [len(r or []) for r in (per_match or []) if r]
        players = round(sum(sizes) / len(sizes), 2) if sizes else 0
    except Exception:
        players = 0
    out = []
    try:
        if not players:
            return []
        for r in (board or []):
            try:
                did = str((r or {}).get("discord_id") or "")
                if not did:
                    continue
                kills = int((r or {}).get("kills") or 0)
                wins = int((r or {}).get("wins") or 0)
                avg = (r or {}).get("avg_placement")
                try:
                    avg_f = float(avg) if avg is not None else None
                except Exception:
                    avg_f = None
                place_term = max(0.0, players - avg_f + 1) if avg_f is not None else 0.0
                pr = round(2 * (kills + 3 * wins + place_term) / (players ** 0.5), 1)
                out.append({"discord_id": did, "name": (r or {}).get("name") or did[:6],
                            "pr": pr, "kills": kills, "wins": wins,
                            "matches": int((r or {}).get("matches") or 0),
                            "avg_placement": avg_f, "players": players})
            except Exception:
                continue
    except Exception:
        pass
    out.sort(key=lambda r: (r.get("pr") or 0), reverse=True)
    return out
