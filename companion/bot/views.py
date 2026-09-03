"""Discord UI components — selects and buttons for session -> match drill-down."""

import discord
import database as db
import formatters as fmt

# ---------------------------------------------------------------------------
# Helpers to build embeds (shared between commands and views)
# ---------------------------------------------------------------------------

def sessions_overview_embed(sessions: list[dict]) -> discord.Embed:
    e = discord.Embed(title="Your Sessions", description="Pick a session to see its matches.", color=0x7C3AED)
    if not sessions:
        e.description = "No sessions yet. Play a match first."
        return e
    for i, s in enumerate(sessions[:10]):
        sid = s["session_id"]
        gms = s.get("game_modes") or []
        gm_label = ", ".join(fmt.format_game_mode(g) for g in gms[:2]) or "Unknown"
        # Prefer last_played over created_at
        ts = fmt.fmt_date(s.get("last_played") or s.get("created_at"))
        stats = f"K:{s.get('kills',0)} D:{s.get('deaths',0)} W:{s.get('wins',0)} L:{s.get('defeats',0)}"
        if s.get("match_count"):
            stats += f" · {s['match_count']} match{'es' if s['match_count']!=1 else ''}"
        # No raw IDs — show human session label
        e.add_field(name=f"{i+1}. {ts} — {gm_label}", value=stats, inline=False)
    e.set_footer(text="Use the menu below to pick a session")
    return e

def matches_in_session_embed(session_id: str, events: list[dict], session_meta: dict | None = None) -> discord.Embed:
    # Group events by match/room if event_data has room_id/code, else treat whole session as one match
    # For now we treat the session as one match and list summary
    e = discord.Embed(title="Matches in Session", color=0x06B6D4)
    if not events:
        e.description = "No matches found in this session."
        return e
    # Try to group by room_id + match_version (private zw_lg multi-match)
    groups: dict[str, list[dict]] = {}
    for ev in events:
        gd = ev.get("event_data") or {}
        rid = gd.get("room_id") or gd.get("room") or gd.get("code") or gd.get("short_code") or session_id
        rid = str(rid)
        mv = gd.get("match_version")
        if mv is not None:
            try:
                key = f"{rid}_{int(mv)}"
            except Exception:
                key = f"{rid}_{mv}"
        else:
            key = rid
        groups.setdefault(key, []).append(ev)
    # If no real grouping (all same), show single match summary
    if len(groups) <= 1:
        gms = list({(ev.get("event_data") or {}).get("gm") for ev in events if (ev.get("event_data") or {}).get("gm")})
        gm_label = ", ".join(fmt.format_game_mode(g) for g in gms[:2]) or "Unknown"
        kills = sum(1 for ev in events if ev.get("event_type") == "killfeed" and (ev.get("event_data") or {}).get("kill_type") == "killed")
        knocks = sum(1 for ev in events if ev.get("event_type") == "killfeed" and (ev.get("event_data") or {}).get("kill_type") == "knocked")
        wins = sum(1 for ev in events if ev.get("event_type") == "win")
        e.description = f"**{gm_label}** · {kills} eliminations · {knocks} knocks" + (" · Victory" if wins else "")
        e.add_field(name="Events", value=f"{len(events)} total", inline=True)
        if session_meta:
            e.set_footer(text=f"{fmt.fmt_date(session_meta.get('created_at'))} · {len(events)} events")
        return e
    # Multiple matches in session — list each (with last played per match)
    for idx, (rid, evs) in enumerate(list(groups.items())[:10], 1):
        gms = list({(ev.get("event_data") or {}).get("gm") for ev in evs if (ev.get("event_data") or {}).get("gm")})
        gm_label = ", ".join(fmt.format_game_mode(g) for g in gms[:1]) or "Match"
        kills = sum(1 for ev in evs if ev.get("event_type") == "killfeed" and (ev.get("event_data") or {}).get("kill_type") == "killed")
        # last played for this match = most recent event in group
        last_ts = None
        try:
            # evs are not ordered here; find max created_at
            last_ev = max(evs, key=lambda x: x.get("created_at") or "")
            last_ts = fmt.fmt_date(last_ev.get("created_at"))
        except: pass
        suffix = f" · {last_ts}" if last_ts else ""
        e.add_field(name=f"Match {idx} — {gm_label}", value=f"{kills} eliminations · {len(evs)} events{suffix}", inline=False)
    e.set_footer(text=f"{len(groups)} matches in this session — pick one below")
    return e

def recent_matches_embed(matches: list[dict]) -> discord.Embed:
    """Flat list of recent individual matches (room + version) across sessions.

    Each item: {session_short, room_short, ts, gm, kills, deaths, win, defeat}.
    """
    e = discord.Embed(title="Recent Matches", color=0x7C3AED)
    if not matches:
        e.description = "No matches yet. Play a match first."
        return e
    for i, m in enumerate(matches[:10], 1):
        gm_label = fmt.format_game_mode(m.get("gm")) if m.get("gm") else "Match"
        ts = fmt.fmt_date(m.get("ts"))
        result = "Victory" if m.get("win") else ("Defeat" if m.get("defeat") else "—")
        stats = f"K:{m.get('kills',0)} D:{m.get('deaths',0)} · {result}"
        e.add_field(name=f"{i}. {ts} — {gm_label}", value=f"{stats} · `{m.get('room_short','?')}`", inline=False)
    e.set_footer(text="Use /replay <session> or /killfeed <session> with the session id from /sessions")
    return e


def match_detail_embed(session_id: str, events: list[dict], room_id: str | None = None) -> discord.Embed:
    # Filter to room if provided (supports composite room_id_version)
    if room_id:
        def _ev_composite(ev):
            gd = ev.get("event_data") or {}
            r = str(gd.get("room_id") or gd.get("code") or gd.get("short_code") or session_id)
            mv = gd.get("match_version")
            if mv is not None:
                try: r = f"{r}_{int(mv)}"
                except Exception: r = f"{r}_{mv}"
            return r
        filtered = [ev for ev in events if _ev_composite(ev) == room_id]
        if filtered:
            events = filtered
        else:
            # legacy fallback where room_id is plain without version
            events = [ev for ev in events if str((ev.get("event_data") or {}).get("room_id") or session_id) == room_id or str((ev.get("event_data") or {}).get("code") or "") == room_id or str((ev.get("event_data") or {}).get("short_code") or "") == room_id]
    gms = list({(ev.get("event_data") or {}).get("gm") for ev in events if (ev.get("event_data") or {}).get("gm")})
    gm_label = ", ".join(fmt.format_game_mode(g) for g in gms[:2]) or "Unknown"
    kills = [ev for ev in events if ev.get("event_type") == "killfeed" and (ev.get("event_data") or {}).get("kill_type") == "killed"]
    knocks = [ev for ev in events if ev.get("event_type") == "killfeed" and (ev.get("event_data") or {}).get("kill_type") == "knocked"]
    wins = any(ev.get("event_type") == "win" for ev in events)
    defeats = any(ev.get("event_type") == "defeat" for ev in events)
    zone_changes = sum(1 for ev in events if ev.get("event_type") == "zone_change")
    title = "Match Details" if not room_id else "Match Details"
    e = discord.Embed(title=title, color=0x7C3AED)
    e.add_field(name="Mode", value=gm_label, inline=True)
    e.add_field(name="Eliminations", value=str(len(kills)), inline=True)
    e.add_field(name="Knocks", value=str(len(knocks)), inline=True)
    if zone_changes:
        e.add_field(name="Zone changes", value=str(zone_changes), inline=True)
    result = "Victory" if wins else ("Defeat" if defeats else "—")
    e.add_field(name="Result", value=result, inline=True)
    e.add_field(name="Events", value=f"{len(events)} total", inline=True)
    # Time range
    if events:
        start = fmt.fmt_date(events[0].get("created_at"))
        e.set_footer(text=f"{start} · {len(events)} events")
    return e

def killfeed_lines_for_events(events: list[dict], room_id: str | None = None) -> list[str]:
    if room_id:
        def _ev_key(ev):
            gd = ev.get("event_data") or {}
            r = str(gd.get("room_id") or gd.get("code") or gd.get("short_code") or "")
            mv = gd.get("match_version")
            if mv is not None:
                try: r = f"{r}_{int(mv)}"
                except Exception: r = f"{r}_{mv}"
            return r
        # composite match: try exact composite first, else legacy plain room_id fallback
        has_composite = any(_ev_key(ev) == room_id for ev in events)
        if has_composite:
            events = [ev for ev in events if _ev_key(ev) == room_id]
        else:
            events = [ev for ev in events if str((ev.get("event_data") or {}).get("room_id") or "") == room_id or str((ev.get("event_data") or {}).get("code") or "") == room_id or str((ev.get("event_data") or {}).get("short_code") or "") == room_id or not (ev.get("event_data") or {}).get("room_id")]
    # Only killfeed events, sorted already by created_at
    lines = []
    for ev in events:
        if ev.get("event_type") != "killfeed":
            continue
        gd = ev.get("event_data") or {}
        ts = fmt.fmt_time(ev.get("created_at"))
        kf_type = gd.get("kill_type", "killed")
        ig = gd.get("instigator", "?")
        ed = gd.get("instigated")
        if kf_type == "killed" and ed:
            lines.append(f"`{ts}` {ig} eliminated {ed}")
        elif kf_type == "knocked" and ed:
            lines.append(f"`{ts}` {ig} knocked {ed}")
        elif kf_type == "left":
            lines.append(f"`{ts}` {ig} left")
        elif kf_type == "joined":
            lines.append(f"`{ts}` {ig} joined")
    return lines

def replay_lines_for_events(events: list[dict], room_id: str | None = None) -> list[str]:
    if room_id:
        def _ev_key(ev):
            gd = ev.get("event_data") or {}
            r = str(gd.get("room_id") or gd.get("code") or gd.get("short_code") or "")
            mv = gd.get("match_version")
            if mv is not None:
                try: r = f"{r}_{int(mv)}"
                except Exception: r = f"{r}_{mv}"
            return r
        has_composite = any(_ev_key(ev) == room_id for ev in events)
        if has_composite:
            events = [ev for ev in events if _ev_key(ev) == room_id]
        else:
            filtered = []
            for ev in events:
                gd = ev.get("event_data") or {}
                rid = str(gd.get("room_id") or gd.get("code") or gd.get("short_code") or "")
                if rid and rid != room_id:
                    continue
                filtered.append(ev)
            events = filtered
    lines = []
    for ev in events:
        et = ev.get("event_type", "?")
        gd = ev.get("event_data") or {}
        ts = fmt.fmt_time(ev.get("created_at"))
        if et == "killfeed":
            kf_type = gd.get("kill_type", "killed")
            ig = gd.get("instigator", "?")
            ed = gd.get("instigated", "?")
            if kf_type == "killed":
                lines.append(f"`{ts}` {ig} eliminated {ed}")
            elif kf_type == "knocked":
                lines.append(f"`{ts}` {ig} knocked {ed}")
            elif kf_type == "left":
                lines.append(f"`{ts}` {ig} left")
            elif kf_type == "joined":
                lines.append(f"`{ts}` {ig} joined")
        elif et == "zone_change":
            phase = gd.get("phase", "?")
            t = gd.get("time")
            time_str = f" ({t}s)" if t is not None else ""
            lines.append(f"`{ts}` Zone Phase {phase}{time_str}")
        elif et == "win":
            gm = fmt.format_game_mode(gd.get("gm", ""))
            lines.append(f"`{ts}` **Victory** — {gm}")
        elif et == "defeat":
            gm = fmt.format_game_mode(gd.get("gm", ""))
            lines.append(f"`{ts}` Defeat — {gm}")
        elif et == "kill":
            victim = gd.get("victim", "?")
            lines.append(f"`{ts}` You eliminated {victim}")
        elif et == "death":
            killer = gd.get("killer", "?")
            lines.append(f"`{ts}` You were eliminated by {killer}")
        elif et == "join":
            p = gd.get("player") or gd.get("player_name") or "?"
            lines.append(f"`{ts}` {p} joined")
        elif et == "leave":
            p = gd.get("player") or gd.get("player_name") or "?"
            lines.append(f"`{ts}` {p} left")
    return lines

def paged_embed(title: str, lines: list[str], color: int, footer: str, page: int = 0, per_page: int = 12) -> discord.Embed:
    total_pages = max(1, (len(lines) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = lines[page * per_page:(page + 1) * per_page]
    e = discord.Embed(title=title, color=color, description="\n".join(chunk) if chunk else "No events.")
    if total_pages > 1:
        e.set_footer(text=f"{footer} · Page {page+1}/{total_pages}")
    else:
        e.set_footer(text=footer)
    return e

# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class SessionSelectView(discord.ui.View):
    def __init__(self, sessions: list[dict], discord_id: str, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.discord_id = discord_id
        self.sessions = sessions
        # Build select options — label must be <=100 chars
        options = []
        for s in sessions[:25]:
            gms = s.get("game_modes") or []
            gm_label = ", ".join(fmt.format_game_mode(g) for g in gms[:2]) or "Unknown"
            ts = fmt.fmt_date(s.get("created_at"))
            label = f"{ts} — {gm_label}"[:100]
            # value is full session_id
            options.append(discord.SelectOption(label=label, value=s["session_id"], description=f"K:{s.get('kills',0)} D:{s.get('deaths',0)}"[:100]))
        select = discord.ui.Select(placeholder="Choose a session", options=options, min_values=1, max_values=1)
        select.callback = self.on_select  # type: ignore
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        sid = interaction.data["values"][0]  # type: ignore
        await interaction.response.defer()
        events = await db.get_match_events(sid, self.discord_id)
        # Find session meta
        meta = next((s for s in self.sessions if s["session_id"] == sid), None)
        embed = matches_in_session_embed(sid, events, meta)
        # Next step: show matches in this session
        # If single match, go straight to detail; else show match picker
        groups: dict[str, list[dict]] = {}
        for ev in events:
            gd = ev.get("event_data") or {}
            rid = gd.get("room_id") or gd.get("code") or gd.get("short_code") or sid
            rid = str(rid)
            mv = gd.get("match_version")
            if mv is not None:
                try: rid = f"{rid}_{int(mv)}"
                except Exception: rid = f"{rid}_{mv}"
            groups.setdefault(rid, []).append(ev)
        if len(groups) <= 1:
            detail = match_detail_embed(sid, events)
            view = MatchActionsView(sid, self.discord_id, events, room_id=None)
            await interaction.edit_original_response(embed=detail, view=view)
        else:
            view = MatchSelectView(sid, self.discord_id, events, list(groups.keys()))
            await interaction.edit_original_response(embed=embed, view=view)

class MatchSelectView(discord.ui.View):
    def __init__(self, session_id: str, discord_id: str, events: list[dict], room_ids: list[str], timeout: float = 120):
        super().__init__(timeout=timeout)
        self.session_id = session_id
        self.discord_id = discord_id
        self.events = events
        # Build options per room (room_ids are composite keys like room_id or room_id_version)
        options = []
        for idx, rid in enumerate(room_ids[:25], 1):
            def _ev_key(ev):
                gd = ev.get("event_data") or {}
                r = str(gd.get("room_id") or gd.get("code") or gd.get("short_code") or session_id)
                mv = gd.get("match_version")
                if mv is not None:
                    try: r = f"{r}_{int(mv)}"
                    except Exception: r = f"{r}_{mv}"
                return r
            evs = [ev for ev in events if _ev_key(ev) == rid]
            # Fallback for legacy events without match_version where rid is plain room_id
            if not evs and "_" not in rid:
                evs = [ev for ev in events if str((ev.get("event_data") or {}).get("room_id") or session_id) == rid or str((ev.get("event_data") or {}).get("code") or "") == rid or str((ev.get("event_data") or {}).get("short_code") or "") == rid]
            gms = list({(ev.get("event_data") or {}).get("gm") for ev in evs if (ev.get("event_data") or {}).get("gm")})
            gm_label = fmt.format_game_mode(gms[0]) if gms else f"Match {idx}"
            kills = sum(1 for ev in evs if ev.get("event_type") == "killfeed" and (ev.get("event_data") or {}).get("kill_type") == "killed")
            label = f"Match {idx} — {gm_label}"[:100]
            options.append(discord.SelectOption(label=label, value=rid, description=f"{kills} eliminations"[:100]))
        select = discord.ui.Select(placeholder="Choose a match", options=options, min_values=1, max_values=1)
        select.callback = self.on_select  # type: ignore
        self.add_item(select)
        # Back button
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self.on_back  # type: ignore
        self.add_item(back)

    async def on_select(self, interaction: discord.Interaction):
        rid = interaction.data["values"][0]  # type: ignore
        await interaction.response.defer()
        # If room_id == session_id, treat as no room filter
        room_filter = None if rid == self.session_id else rid
        detail = match_detail_embed(self.session_id, self.events, room_filter)
        view = MatchActionsView(self.session_id, self.discord_id, self.events, room_filter)
        await interaction.edit_original_response(embed=detail, view=view)

    async def on_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # Go back to sessions overview — need to refetch sessions
        sessions = await db.get_recent_matches(self.discord_id, limit=10)
        embed = sessions_overview_embed(sessions)
        view = SessionSelectView(sessions, self.discord_id)
        await interaction.edit_original_response(embed=embed, view=view)

class MatchActionsView(discord.ui.View):
    def __init__(self, session_id: str, discord_id: str, events: list[dict], room_id: str | None, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.session_id = session_id
        self.discord_id = discord_id
        self.events = events
        self.room_id = room_id
        self.page = 0
        self.mode: str | None = None  # "killfeed" or "replay"

    @discord.ui.button(label="Killfeed", style=discord.ButtonStyle.danger, emoji="💀")
    async def killfeed_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.mode = "killfeed"
        self.page = 0
        lines = killfeed_lines_for_events(self.events, self.room_id)
        if not lines:
            await interaction.edit_original_response(embed=discord.Embed(title="Killfeed", description="No eliminations in this match.", color=0xDC2626), view=self)
            return
        embed = paged_embed("Killfeed", lines, 0xDC2626, f"{len(lines)} events", page=self.page)
        # Update pagination buttons state
        self._update_pagination(len(lines))
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Replay", style=discord.ButtonStyle.primary, emoji="▶️")
    async def replay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.mode = "replay"
        self.page = 0
        lines = replay_lines_for_events(self.events, self.room_id)
        if not lines:
            await interaction.edit_original_response(embed=discord.Embed(title="Replay", description="No events to replay.", color=0x7C3AED), view=self)
            return
        embed = paged_embed("Replay", lines, 0x7C3AED, f"{len(lines)} events", page=self.page)
        self._update_pagination(len(lines))
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Players", style=discord.ButtonStyle.secondary, emoji="👥")
    async def players_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        # Build players embed from events — scoped to current room_id so matches don't merge
        seen: dict[str, str] = {}
        # Filter events to current room if room_id set (composite key supported)
        src_events = self.events
        if self.room_id:
            def _ev_key(ev):
                gd = ev.get("event_data") or {}
                r = str(gd.get("room_id") or gd.get("code") or gd.get("short_code") or "")
                mv = gd.get("match_version")
                if mv is not None:
                    try: r = f"{r}_{int(mv)}"
                    except Exception: r = f"{r}_{mv}"
                return r
            has_composite = any(_ev_key(ev) == self.room_id for ev in src_events)
            if has_composite:
                src_events = [ev for ev in src_events if _ev_key(ev) == self.room_id]
            else:
                src_events = [ev for ev in src_events if str((ev.get("event_data") or {}).get("room_id") or "") == self.room_id or str((ev.get("event_data") or {}).get("code") or "") == self.room_id or str((ev.get("event_data") or {}).get("short_code") or "") == self.room_id or not (ev.get("event_data") or {}).get("room_id")]
        for ev in src_events:
            gd = ev.get("event_data") or {}
            # killfeed instigator/instigated + join/leave player
            for k in ("instigator", "instigated", "victim", "killer", "player", "player_name"):
                name = gd.get(k)
                if name and name not in seen:
                    seen[name] = name
        if not seen:
            await interaction.edit_original_response(embed=discord.Embed(title="Players", description="No players found for this match.", color=0x7C3AED), view=self)
            return
        lines = [f"{i+1}. {name}" for i, name in enumerate(list(seen.keys())[:20])]
        e = discord.Embed(title="Players", description="\n".join(lines), color=0x7C3AED)
        e.set_footer(text=f"{len(seen)} players")
        await interaction.edit_original_response(embed=e, view=self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        # Go back to match detail
        detail = match_detail_embed(self.session_id, self.events, self.room_id)
        self.mode = None
        self.page = 0
        await interaction.edit_original_response(embed=detail, view=self)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.mode is None:
            return await interaction.response.defer()
        await interaction.response.defer()
        self.page = max(0, self.page - 1)
        lines = killfeed_lines_for_events(self.events, self.room_id) if self.mode == "killfeed" else replay_lines_for_events(self.events, self.room_id)
        color = 0xDC2626 if self.mode == "killfeed" else 0x7C3AED
        title = "Killfeed" if self.mode == "killfeed" else "Replay"
        embed = paged_embed(title, lines, color, f"{len(lines)} events", page=self.page)
        self._update_pagination(len(lines))
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.mode is None:
            return await interaction.response.defer()
        await interaction.response.defer()
        lines = killfeed_lines_for_events(self.events, self.room_id) if self.mode == "killfeed" else replay_lines_for_events(self.events, self.room_id)
        total_pages = max(1, (len(lines) + 12 - 1) // 12)
        self.page = min(total_pages - 1, self.page + 1)
        color = 0xDC2626 if self.mode == "killfeed" else 0x7C3AED
        title = "Killfeed" if self.mode == "killfeed" else "Replay"
        embed = paged_embed(title, lines, color, f"{len(lines)} events", page=self.page)
        self._update_pagination(len(lines))
        await interaction.edit_original_response(embed=embed, view=self)

    def _update_pagination(self, total_lines: int):
        total_pages = max(1, (total_lines + 12 - 1) // 12)
        # Find prev/next buttons (row 1)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label in ("◀ Prev", "Next ▶"):
                child.disabled = total_pages <= 1 or (child.label == "◀ Prev" and self.page == 0) or (child.label == "Next ▶" and self.page >= total_pages - 1)


class SimplePagedView(discord.ui.View):
    """Prev/Next pagination for standalone /killfeed and /replay."""

    def __init__(self, title: str, lines: list[str], color: int, footer: str, per_page: int = 12, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.title = title
        self.lines = lines
        self.color = color
        self.footer = footer
        self.per_page = per_page
        self.page = 0
        self._refresh_buttons()

    def current_embed(self) -> discord.Embed:
        return paged_embed(self.title, self.lines, self.color, self.footer, page=self.page, per_page=self.per_page)

    def _refresh_buttons(self):
        total_pages = max(1, (len(self.lines) + self.per_page - 1) // self.per_page)
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.label == "◀ Prev":
                    child.disabled = self.page == 0 or total_pages <= 1
                elif child.label == "Next ▶":
                    child.disabled = self.page >= total_pages - 1 or total_pages <= 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.page = max(0, self.page - 1)
        self._refresh_buttons()
        await interaction.edit_original_response(embed=self.current_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def nxt(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        total_pages = max(1, (len(self.lines) + self.per_page - 1) // self.per_page)
        self.page = min(total_pages - 1, self.page + 1)
        self._refresh_buttons()
        await interaction.edit_original_response(embed=self.current_embed(), view=self)
