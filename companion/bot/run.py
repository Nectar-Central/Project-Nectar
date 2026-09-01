"""Run bot + API together"""
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(dotenv_path="vercel-api/.env")
    load_dotenv(dotenv_path="../vercel-api/.env")
    load_dotenv(dotenv_path="photon-sniffer/bot/.env")
except: pass
import asyncio, uvicorn, threading
import database as db

async def start_api():
    import api as api_mod
    config = uvicorn.Config(api_mod.app, host="127.0.0.1", port=8766, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

def run_api_thread():
    asyncio.run(start_api())

if __name__ == "__main__":
    # init db then start both
    asyncio.run(db.init_db())
    # API in thread
    t = threading.Thread(target=run_api_thread, daemon=True)
    t.start()
    # Bot in main thread (blocking)
    import bot as bot_mod
    import discord
    # bot_mod.bot.run will block
    bot_mod.bot.run(os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN"))
