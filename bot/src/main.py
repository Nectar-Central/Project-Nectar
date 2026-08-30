import os

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

intents = discord.Intents.default()

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")
    print("🚀 Nectar Central bot is online!")


@tree.command(name="ping", description="Check if the bot is online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")


client.run(TOKEN)