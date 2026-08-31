import os
import random

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


@tree.command(name="view_dev_team", description="Shows the main developers for Nectar")
async def view_dev_team(interaction: discord.Interaction):
    embed = discord.Embed(
        description="<:people:1543709237987909682> Here are the main devs for nectar\n\n"
                    "**<@1295478685876617299>** as the senior dev\n"
                    "**<@1267528889383784513>** as the founder\n"
                    "**<@1143465561548927037>** as the web dev\n"
                    "**<@1526807976801865870>** as the bot dev\n"
                    "**<@1484202460577796178>** as our manager",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="coinflip", description="Flip a coin to make a decision")
async def coinflip(interaction: discord.Interaction):
    result = random.choice(["Heads", "Tails"])
    embed = discord.Embed(
        title="Coin Flip",
        description=f"<:165460coin:1543709235278516374> The coin landed on **{result}**!",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)


client.run(TOKEN)