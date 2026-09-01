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
    print(f"Logged in as {client.user}")
    print("Nectar bot is online and working")


@tree.command(name="ping", description="check if the bot is online and working")
async def ping(interaction: discord.Interaction):
    embed = discord.Embed(
        title="I am working yall, wsg how yall doing?",
        description=f"<:165460ping:1543709237987909682> The bot is online and working",
        color=discord.Color.dark_blue()
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="view_dev_team", description="shows the developers for Nectar labs")
async def view_dev_team(interaction: discord.Interaction):
    embed = discord.Embed(
        description="<:people:1543709237987909682> Here are the main devs for nectar\n\n"
                    "**<@1295478685876617299>** as our senior dev and co-founder\n"
                    "**<@1267528889383784513>** as our founder\n"
                    "**<@1143465561548927037>** as our web dev\n"
                    "**<@1526807976801865870>** as our lead bot dev\n"
                    "**<@1484202460577796178>** as our manager\n"
                    "**<@1433549145251123234>** as our backend dev and our darling web designer \n"
                    "**<@1479807273474986076>** as our second bot dev\n",
        color=discord.Color.dark_blue()
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="coinflip", description="Flip a coin to make a decision")
async def coinflip(interaction: discord.Interaction):
    result = random.choice(["heads", "tails"])
    embed = discord.Embed(
        title="Coin Flip",
        description=f"<:165460coin:1543709235278516374> The coin landed on **{result}**w! to the winner",
        color=discord.Color.dark_blue()
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="serverinfo", description="Displays information about the current server")
async def serverinfo(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used inside a server!", ephemeral=True)
        return

    guild = interaction.guild
    
    embed = discord.Embed(
        title=f" Server Info: {guild.name}",
        color=discord.Color.dark_blue()
    )
    
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
        
    embed.add_field(name="Server ID", value=guild.id, inline=True)
    embed.add_field(name="Founder", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Members", value=f" {guild.member_count}", inline=True)
    embed.add_field(name="Created On", value=f"<t:{int(guild.created_at.timestamp())}:F>", inline=False)
    
    await interaction.response.send_message(embed=embed)


@tree.command(name="avatar", description="Displays a user's avatar")
@app_commands.describe(user="The user whose avatar you want to see (leave blank for yourself)")
async def avatar(interaction: discord.Interaction, user: discord.Member = None):
    target_user = user or interaction.user
    
    embed = discord.Embed(
        title=f" {target_user.display_name}'s Avatar",
        color=discord.Color.dark_blue()
    )
    embed.set_image(url=target_user.display_avatar.url)
    
    await interaction.response.send_message(embed=embed)


client.run(TOKEN)