"""Comp tourney final results — the embeds posted when a tourney is closed.

`/tourney-stop` ends a comp tourney, and until now the final standings only
existed in an ephemeral command reply. This module builds the archive post
for the results channel: a header line plus the aggregate board, 10 players
per embed so any roster size fits inside Discord's 4096-char description
limit. Pure builders only (no Discord, no network) so the layout is testable.
"""

import asyncio
import os

# Channel every finished comp tourney's final standings are posted to.
RESULTS_CHANNEL_ID = "1512793471440916564"

GOLD = 0xFFD700
MEDALS = ("🥇", "🥈", "🥉")
PER_EMBED = 10
DESC_LIMIT = 4096
TITLE_LIMIT = 256


def results_channel_id() -> str:
    """Results channel id, read at call time; blank env falls back to default."""
    try:
        return (str(os.getenv("COMP_RESULTS_CHANNEL_ID", "") or "").strip()
                or RESULTS_CHANNEL_ID)
    except Exception:
        return RESULTS_CHANNEL_ID


def mention(discord_id) -> str:
    """<@id> mention, or "?" when there is no id. Pure."""
    try:
        did = str(discord_id or "").strip()
        return f"<@{did}>" if did else "?"
    except Exception:
        return "?"


def board_lines(board, limit: int = 0) -> list:
    """Render aggregate standings lines (medals, points, kills, wins, avg).

    Numbering is global over the whole board, so chunking the result never
    restarts the placements. limit=0 renders all rows. Never raises.
    """
    lines: list = []
    try:
        rows = list(board or [])
        if limit:
            rows = rows[:int(limit)]
        for i, r in enumerate(rows, 1):
            r = r or {}
            name = str(r.get("name") or "?")
            did = r.get("discord_id")
            who = f"{mention(did)} **{name}**" if did else f"**{name}**"
            try:
                pts = r.get("points", 0)
            except Exception:
                pts = 0
            try:
                kills = r.get("kills", 0)
                wins = r.get("wins", 0)
            except Exception:
                kills = wins = 0
            avg = r.get("avg_placement")
            tail = f" ({kills}K, {wins}W" + (f", {avg} avg" if avg is not None else "") + ")"
            prefix = MEDALS[i - 1] if i <= 3 else f"{i}."
            lines.append(f"{prefix} {who} — {pts} pts{tail}")
    except Exception:
        return lines
    return lines


def _footer(tourney: dict, players: int, page: int, pages: int) -> str:
    sealed = (tourney or {}).get("sealed", 0)
    series = (tourney or {}).get("series_len") or "∞"
    return f"{players} players · {sealed}/{series} matches · page {page}/{pages}"


def results_embeds(tourney: dict, board, per_embed: int = PER_EMBED) -> list:
    """Final-standings embed specs for one finished tourney.

    Returns [] when there is nothing to show (no sealed matches), so the
    results channel never fills with empty posts. Each spec is
    {"title", "description", "color", "footer"} — ready for
    discord.Embed(**spec). Never raises.
    """
    try:
        t = tourney or {}
        lines = board_lines(board)
        if not lines:
            return []
        try:
            step = max(1, int(per_embed or PER_EMBED))
        except Exception:
            step = PER_EMBED
        chunks = [lines[i:i + step] for i in range(0, len(lines), step)]
        name = str(t.get("name") or "Comp tourney")
        tid = t.get("id")
        out = []
        for i, chunk in enumerate(chunks, 1):
            head = (f"🏆 **{name}** — final standings"
                    if i == 1 else f"🏆 **{name}** — final standings (cont.)")
            desc = "\n".join([head, ""] + chunk)
            if len(desc) > DESC_LIMIT:
                desc = desc[:DESC_LIMIT - 1].rstrip() + "…"
            title = f"🏆 {name} — #{tid}" if tid else f"🏆 {name}"
            out.append({
                "title": str(title)[:TITLE_LIMIT],
                "description": desc,
                "color": GOLD,
                "footer": _footer(t, len(lines), i, len(chunks)),
            })
        return out
    except Exception:
        return []


async def post_results(bot, tourney_id, fetch, *, channel_id: str | None = None,
                       per_embed: int = PER_EMBED, sleep=asyncio.sleep) -> tuple:
    """Post one finished tourney's final standings to the results channel.

    `fetch(tourney_id)` must return the /api/comp/tourney payload (or None).
    Returns (embeds_posted, note). Never raises: a failed archive post must
    never stop a tourney from closing, so every failure is a note.
    """
    try:
        cid = str(channel_id or results_channel_id() or "").strip()
        if not cid.isdigit():
            return 0, "results channel id is not a channel id"
        data = await fetch(tourney_id)
        if not data or data.get("error"):
            return 0, (f"results unavailable "
                       f"({(data or {}).get('error') or 'server unreachable'})")
        tourney = data.get("tourney") or {}
        specs = results_embeds(tourney, data.get("standings") or [],
                              per_embed=per_embed)
        if not specs:
            return 0, "no sealed matches — nothing to post"
        channel = None
        try:
            channel = bot.get_channel(int(cid))
            if channel is None:
                channel = await bot.fetch_channel(int(cid))
        except Exception as e:
            print(f"[comp] results channel fetch failed: {e}")
        if channel is None:
            return 0, f"results channel {cid} not found"
        sent = 0
        for i, spec in enumerate(specs):
            try:
                embed = _to_embed(spec)
                await channel.send(embed=embed)
                sent += 1
            except Exception as e:
                print(f"[comp] results embed {i + 1}/{len(specs)} failed: {e}")
                return sent, f"stopped after {sent} embed(s): {e}"
            if i + 1 < len(specs):
                # Stay well under the channel rate limit on big rosters.
                await sleep(2)
        print(f"[comp] posted final results for tourney #{tourney_id} "
              f"({sent} embed(s)) to {cid}")
        return sent, f"{sent} embed(s)"
    except Exception as e:
        print(f"[comp] results post failed: {e}")
        return 0, "results post failed"


def _to_embed(spec: dict):
    """Spec dict -> discord.Embed (footer is set via set_footer, not a kwarg).
    Imported lazily so this module stays importable (and testable) without
    Discord."""
    import discord
    spec = dict(spec or {})
    footer = spec.pop("footer", None)
    embed = discord.Embed(**spec)
    if footer:
        try:
            embed.set_footer(text=str(footer))
        except Exception:
            pass
    return embed
