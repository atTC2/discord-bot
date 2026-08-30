"""
Discord Bot - Entry Point
--------------------------
This is the central bot script: it creates the bot, loads feature extensions
(cogs), and starts it up. It doesn't contain any feature logic itself.

To add another feature module (e.g. a voting_cog.py), drop the file in this
same folder and add its module name to EXTENSIONS below.
"""

import os
import asyncio
import logging

import discord
from discord.ext import commands

from help_command import PrettyHelpCommand

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
COMMAND_PREFIX = "!"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot")

intents = discord.Intents.default()
intents.message_content = True   # needed to read !command text
intents.voice_states = True      # needed so cogs can see who's in which voice channel

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
bot.help_command = PrettyHelpCommand()

# Extensions (cogs) to load on startup.
# Add new modules here as you build them, e.g.:
#   EXTENSIONS = ["music_cog", "voting_cog"]
EXTENSIONS = [
    "music_cog",
]


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    logger.info(f"Loaded extensions: {', '.join(EXTENSIONS)}")
    logger.info("Bot is ready. Waiting for commands...")


async def main():
    if not DISCORD_BOT_TOKEN:
        raise SystemExit(
            "ERROR: DISCORD_BOT_TOKEN is not set.\n"
            "Set it as an environment variable, or create a .env file with:\n"
            "    DISCORD_BOT_TOKEN=your_token_here\n"
            "See README.md for full setup instructions."
        )

    async with bot:
        for extension in EXTENSIONS:
            await bot.load_extension(extension)
        await bot.start(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
