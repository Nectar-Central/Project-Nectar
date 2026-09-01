import json
import os
import random

import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from the .env file")


intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

WARNINGS_FILE = "warnings.json"


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
    await tree.sync()
    print(f"Logged in as {bot.user}")
    print("Nectar Central is online!")


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

    embed.add_field(name="Server ID", value=guild.id, inline=True)
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
    reason: str
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
