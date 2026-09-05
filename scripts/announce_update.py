"""One-shot update broadcaster for CI (GitHub Action).

Connects as the bot, DMs every registered player (bngc_users) the version +
patch notes + reinstall link, prints a summary, disconnects. Run via:
  VERSION=2.14.3 NOTES="..." USERSCRIPT_URL=... \
  DISCORD_TOKEN=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
  python scripts/announce_update.py

Env (all required): DISCORD_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
VERSION, NOTES. Optional: USERSCRIPT_URL (defaults to the raw main file).
Exit code 0 even when some DMs fail (summary still printed); exit 2 on
missing env or total lookup failure.
"""
import asyncio
import os
import sys

import discord
from supabase import create_client

USERSCRIPT_URL = (
    os.environ.get("USERSCRIPT_URL")
    or "https://raw.githubusercontent.com/Nectar-Central/Project-Nectar/main/companion/buildnow-companion.user.js"
)
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
PATCH_CHANNEL_ID = 1512793466852212846


class Announcer(discord.Client):
    def __init__(self, version: str, notes: str):
        super().__init__(intents=discord.Intents.default())
        self.version = version
        self.notes = notes[:1000]
        self.sent = 0
        self.failed = 0

    async def on_ready(self):
        try:
            sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
            res = sb.table("bngc_users").select("discord_id").limit(10000).execute()
            targets = [str(r.get("discord_id")) for r in (res.data or []) if r.get("discord_id")]
        except Exception as exc:
            print(f"user lookup failed: {exc}")
            await self.close()
            sys.exit(2)
        print(f"announcing v{self.version} to {len(targets)} players")
        if DRY_RUN:
            print(f"dry run: would DM {len(targets)} players, sending nothing")
            await self.close()
            return
        embed = discord.Embed(title=f"🚀 Nectar update {self.version} is out",
                              description=self.notes, color=0x7C3AED)
        embed.add_field(name="Reinstall",
                        value=f"[Click here to reinstall]({USERSCRIPT_URL})", inline=False)
        embed.set_footer(text="Open the link in Tampermonkey → Reinstall, then reload the game page.")
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Reinstall userscript", url=USERSCRIPT_URL))
        for did in targets:
            try:
                user = await self.fetch_user(int(did))
                await user.send(embed=embed, view=view)
                self.sent += 1
            except discord.Forbidden:
                self.failed += 1  # DMs closed — skip silently
            except Exception as exc:
                print(f"DM failed for {did}: {exc}")
                self.failed += 1
            await asyncio.sleep(0.6)  # stay under DM rate limits
        print(f"done: {self.sent} sent, {self.failed} failed")
        try:
            channel = await self.fetch_channel(PATCH_CHANNEL_ID)
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="Reinstall userscript", url=USERSCRIPT_URL))
            await channel.send(embed=embed, view=view)
            print("posted to patch channel")
        except Exception as exc:
            print(f"patch channel post failed: {exc}")
        await self.close()


def main() -> int:
    missing = [k for k in ("DISCORD_TOKEN", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "VERSION", "NOTES")
               if not os.environ.get(k)]
    if missing:
        print(f"missing env: {', '.join(missing)}")
        return 2
    client = Announcer(os.environ["VERSION"].strip(), os.environ["NOTES"])
    try:
        client.run(os.environ["DISCORD_TOKEN"])
    except Exception as exc:
        print(f"bot run failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
