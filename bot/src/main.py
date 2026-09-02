import json
import os
import asyncio
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID") or "1543724907651932282"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN or DISCORD_BOT_TOKEN is missing")


intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

WARNINGS_FILE = Path(__file__).resolve().parent / "warnings.json"


def load_warnings():
    """Load saved warnings, or start fresh if the file doesn't exist."""
    try:
        with open(WARNINGS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_warnings():
    with open(WARNINGS_FILE, "w") as f:
        json.dump(warnings, f, indent=4)


warnings = load_warnings()


@bot.event
    async def on_ready():
      try:
          if GUILD_ID and GUILD_ID.isdigit():
              await tree.sync(guild=discord.Object(id=int(GUILD_ID)))
              print(f"Synced commands to guild {GUILD_ID}")
          else:
              await tree.sync()
              print("Synced global commands")
      except discord.HTTPException as exc:
          print(f"Command sync failed: {exc}")
      print(f"Logged in as {bot.user}")
      print("Nectar Central is online!")


    @tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
      print(f"Command error in {getattr(interaction.command, 'name', 'unknown')}: {error}")
      message = "Something went wrong while running that command. Please try again."
      if isinstance(error, app_commands.MissingPermissions):
          message = "You do not have permission to use that command."
      elif isinstance(error, app_commands.CommandOnCooldown):
          message = "That command is on cooldown. Please try again shortly."
      if interaction.response.is_done():
          await interaction.followup.send(message, ephemeral=True)
      else:
          await interaction.response.send_message(message, ephemeral=True)


    def save_registered_player(discord_id: int, username: str, display_name: str, game_handle: str, avatar_url: str):
      if not SUPABASE_URL or not SUPABASE_KEY:
          raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing")
      payload = {
          "id": str(discord_id),
          "username": username,
          "display_name": display_name,
          "game_handle": game_handle,
          "avatar_url": avatar_url,
          "updated_at": datetime.now(timezone.utc).isoformat(),
      }
      request = Request(
          f"{SUPABASE_URL}/rest/v1/players?on_conflict=id",
          data=json.dumps(payload).encode("utf-8"),
          headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
          method="POST",
      )
      try:
          with urlopen(request, timeout=10) as response:
              if response.status not in (200, 201, 204):
                  raise RuntimeError(f"Supabase returned HTTP {response.status}")
      except HTTPError as exc:
          details = exc.read().decode("utf-8", errors="replace")[:300]
          raise RuntimeError(f"Supabase returned HTTP {exc.code}: {details}") from exc
      except URLError as exc:
          raise RuntimeError(f"Supabase request failed: {exc.reason}") from exc


    @tree.command(name="register", description="Create or update your Nectar player profile")
@app_commands.describe(game_handle="Your in-game handle")
async def register(interaction: discord.Interaction, game_handle: app_commands.Range[str, 1, 32]):
    clean_handle = str(game_handle).strip()
    if not clean_handle:
        return await interaction.response.send_message("Please provide a valid game handle.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    user = interaction.user
    display_name = getattr(user, "global_name", None) or user.display_name
    try:
        await asyncio.to_thread(save_registered_player, user.id, user.name, display_name, clean_handle, user.display_avatar.url)
    except Exception as exc:
        print(f"Registration failed for {user.id}: {exc}")
        return await interaction.followup.send("I could not save your profile yet. Please try again in a moment.", ephemeral=True)
    await interaction.followup.send(f"Registered {display_name} with game handle {clean_handle}. Your profile is live.", ephemeral=True)


@tree.command(name="ping", description="Check if the bot is online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")


@tree.command(
    name="view_dev_team",
    description="Shows the main developers for Nectar"
)
async def view_dev_team(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Nectar Development Team",
        description=(
            "<:people:1543709237987909682> "
            "Here are the main developers for Nectar.\n\n"
            "**<@1295478685876617299>** — Senior Developer\n"
            "**<@1267528889383784513>** — Founder\n"
            "**<@1143465561548927037>** — Web Developer\n"
            "**<@1526807976801865870>** — Bot Developer\n"
            "**<@1484202460577796178>** — Manager"
        ),
        color=discord.Color.gold()
    )

    await interaction.response.send_message(embed=embed)


@tree.command(name="coinflip", description="Flip a coin")
async def coinflip(interaction: discord.Interaction):
    result = random.choice(["Heads", "Tails"])

    embed = discord.Embed(
        title="Coin Flip",
        description=(
            f"<:165460coin:1543709235278516374> "
            f"The coin landed on **{result}!**"
        ),
        color=discord.Color.gold()
    )

    await interaction.response.send_message(embed=embed)


@tree.command(
    name="serverinfo",
    description="Shows information about the current server"
)
async def serverinfo(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "❌ This command can only be used inside a server.",
            ephemeral=True
        )

    guild = interaction.guild

    embed = discord.Embed(
        title=f"Server Info: {guild.name}",
        color=discord.Color.gold()
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name="Server ID", value=str(guild.id), inline=True)
    embed.add_field(
        name="Owner",
        value=f"<@{guild.owner_id}>",
        inline=True
    )
    embed.add_field(
        name="Members",
        value=guild.member_count,
        inline=True
    )
    embed.add_field(
        name="Created",
        value=f"<t:{int(guild.created_at.timestamp())}:F>",
        inline=False
    )

    await interaction.response.send_message(embed=embed)


@tree.command(name="avatar", description="Show a user's avatar")
@app_commands.describe(user="The user whose avatar you want to see")
async def avatar(
    interaction: discord.Interaction,
    user: discord.Member | None = None
):
    member = user or interaction.user

    embed = discord.Embed(
        title=f"{member.display_name}'s Avatar",
        color=discord.Color.gold()
    )
    embed.set_image(url=member.display_avatar.url)

    await interaction.response.send_message(embed=embed)


@tree.command(name="warn", description="Warn a member")
@app_commands.describe(
    user="The member you want to warn",
    reason="Why you are warning them"
)
@app_commands.default_permissions(administrator=True)
async def warn(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: app_commands.Range[str, 1, 500]
):
    if interaction.guild is None:
        return await interaction.response.send_message(
            "❌ This command can only be used inside a server.",
            ephemeral=True
        )


    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "❌ You need Administrator permissions to use this command.",
            ephemeral=True
        )

    if user == interaction.user:
        return await interaction.response.send_message(
            "❌ You can't warn yourself.",
            ephemeral=True
        )

    if user.bot:
        return await interaction.response.send_message(
            "❌ You can't warn bots.",
            ephemeral=True
        )

    if user == interaction.guild.owner:
        return await interaction.response.send_message(
            "❌ You can't warn the server owner.",
            ephemeral=True
        )

    if interaction.user != interaction.guild.owner and user.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "❌ You can only warn members below your highest role.",
            ephemeral=True
        )

    bot_member = interaction.guild.me
    if bot_member and user.top_role >= bot_member.top_role:
        return await interaction.response.send_message(
            "❌ My role must be above the member I am warning.",
            ephemeral=True
        )

    guild_id = str(interaction.guild.id)
    user_id = str(user.id)

    warnings.setdefault(guild_id, {})
    warnings[guild_id][user_id] = warnings[guild_id].get(user_id, 0) + 1

    warning_count = warnings[guild_id][user_id]
    save_warnings()

    if warning_count >= 3:
        try:
            await user.send(
                f"🔨 You have been banned from **{interaction.guild.name}** "
                "because you received 3 warnings."
            )
        except discord.Forbidden:
            pass

        try:
            await user.ban(reason="Reached 3 warnings.")

        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I couldn't ban this member. "
                "Make sure I have the **Ban Members** permission "
                "and that my role is above their role.",
                ephemeral=True
            )

        embed = discord.Embed(
            title="🔨 Member Banned",
            description=(
                f"{user.mention} has been banned after receiving "
                "**3 warnings**."
            ),
            color=discord.Color.red()
        )
        embed.add_field(name="Latest Warning", value=reason, inline=False)
        embed.add_field(
            name="Banned By",
            value=interaction.user.mention,
            inline=False
        )

        return await interaction.response.send_message(embed=embed)

    embed = discord.Embed(
        title="⚠️ Member Warned",
        color=discord.Color.gold()
    )
    embed.add_field(
        name="Member",
        value=f"{user.mention} (`{user.id}`)",
        inline=False
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(
        name="Warnings",
        value=f"**{warning_count}/3**",
        inline=False
    )
    embed.add_field(
        name="Warned By",
        value=interaction.user.mention,
        inline=False
    )

    await interaction.response.send_message(embed=embed)


    try:
        dm = discord.Embed(
            title="⚠️ You have been warned",
            description=(
                f"You received a warning in **{interaction.guild.name}**."
            ),
            color=discord.Color.gold()
        )
        dm.add_field(name="Reason", value=reason, inline=False)
        dm.add_field(
            name="Warnings",
            value=f"**{warning_count}/3**",
            inline=False
        )
        dm.add_field(
            name="Warned By",
            value=interaction.user.display_name,
            inline=False
        )

        await user.send(embed=dm)

    except discord.Forbidden:
        pass


bot.run(TOKEN)
