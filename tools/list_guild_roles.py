"""List a guild's roles (name + id) and draft RANKED_ROLE_* env lines.

Usage (bot must already be a member of the server):
    python tools/list_guild_roles.py <guild_id>
    python tools/list_guild_roles.py <guild_id> --match unranked,bronze,silver

Token: DISCORD_TOKEN (also accepts DISCORD_BOT_TOKEN / BOT_TOKEN).
Read-only: logs in, fetches roles, prints, exits. Never modifies anything.
"""
import os
import sys

TIERS = ("Unranked", "Bronze", "Silver", "Gold", "Platinum",
         "Diamond", "Emerald", "Champion", "Master", "Grandmaster")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    try:
        guild_id = int(sys.argv[1])
    except Exception:
        print("guild_id must be numeric (Server Settings > Widget > Server ID, "
              "or right-click the server icon > Copy Server ID).")
        return 2
    token = (os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
             or os.getenv("BOT_TOKEN") or "MTU0MzcwODkyNDEwNzI5MjcyMg.GYi_8X.B7zIh0yB7mN_U5HEJwD3MdZpK6_ZICtXpxYgGc")
    if not token:
        print("Set DISCORD_TOKEN first (same token the bot runs with).")
        return 2
    try:
        import discord
    except Exception:
        print("discord.py is not installed in this Python "
              "(pip install -U discord.py).")
        return 2

    matches = []
    if len(sys.argv) > 2 and sys.argv[2] == "--match":
        try:
            matches = [m.strip().lower() for m in sys.argv[3].split(",") if m.strip()]
        except Exception:
            matches = []

    import asyncio

    async def run():
        intents = discord.Intents.none()
        intents.guilds = True
        client = discord.Client(intents=intents)
        try:
            await client.login(token)
        except Exception as e:
            print(f"Login failed: {e}")
            return 2
        try:
            guild = client.get_guild(guild_id)
            if guild is None:
                try:
                    guild = await client.fetch_guild(guild_id)
                except Exception as e:
                    print(f"Guild {guild_id} unreachable (bot not a member?): {e}")
                    return 2
            roles = sorted(guild.roles, key=lambda r: (r.position, r.id))
            print(f"{guild.name} ({guild.id}) — {len(roles)} roles, "
                  f"bot top role: {guild.me.top_role if guild.me else '?'}")
            print(f"{'pos':>4}  {'id':<20} members  name")
            for r in roles:
                try:
                    members = len(r.members or [])
                except Exception:
                    members = "?"
                print(f"{r.position:>4}  {r.id:<20} {members!s:<8} {r.name}")
            if matches or True:
                # draft env lines for tier-name role matches
                by_name = {r.name.strip().lower(): r for r in roles}
                print("\n# RANKED_ROLE_* draft (verify names, then export):")
                for tier in TIERS:
                    if matches and tier.lower() not in matches:
                        continue
                    hit = by_name.get(tier.lower())
                    if hit:
                        print(f"RANKED_ROLE_{tier.upper()}={hit.id}")
                    else:
                        print(f"# RANKED_ROLE_{tier.upper()}=  # no role named '{tier}'")
        finally:
            try:
                await client.close()
            except Exception:
                pass
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
