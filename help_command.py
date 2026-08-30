"""
Pretty Help Command
--------------------
A drop-in replacement for discord.py's default !help command that renders one
formatted section per loaded cog, as an embed.

Cogs don't need to do anything special to appear here -- ordinary discord.py
docstrings are all that's read. See COG_GUIDE.md for the full convention
(short version: give your Cog subclass a docstring, give each @commands.command
a docstring, and optionally set a `COG_EMOJI = "🗳️"` class attribute).

Wire this up once in your base bot script:

    from help_command import PrettyHelpCommand
    bot.help_command = PrettyHelpCommand()

Nothing else is required -- every cog you load_extension() afterwards will
show up in !help automatically.
"""

import discord
from discord.ext import commands

DEFAULT_COG_EMOJI = "🔧"
EMBED_COLOR = discord.Color.blurple()


class PrettyHelpCommand(commands.HelpCommand):
    """Renders !help as an embed, grouped by cog, using cog/command docstrings."""

    def __init__(self):
        super().__init__(command_attrs={
            "help": "Shows this help message, or details on one command or category.",
        })

    async def send_bot_help(self, mapping):
        prefix = self.context.clean_prefix
        embed = discord.Embed(
            title="📖 Bot Commands",
            description=f"Use `{prefix}help <command>` for details on a specific command.",
            color=EMBED_COLOR,
        )

        for cog, cog_commands in mapping.items():
            filtered = await self.filter_commands(cog_commands, sort=True)
            if not filtered:
                continue

            name = cog.qualified_name if cog else "Other"
            emoji = getattr(cog, "COG_EMOJI", DEFAULT_COG_EMOJI) if cog else DEFAULT_COG_EMOJI
            description = (cog.description or "").strip() if cog else ""

            command_lines = [
                f"`{prefix}{command.name}` — {command.short_doc or 'No description provided.'}"
                for command in filtered
            ]

            field_value = "\n".join(command_lines)
            if description:
                field_value = f"{description}\n{field_value}"

            embed.add_field(name=f"{emoji} {name}", value=field_value, inline=False)

        await self.get_destination().send(embed=embed)

    async def send_cog_help(self, cog):
        prefix = self.context.clean_prefix
        emoji = getattr(cog, "COG_EMOJI", DEFAULT_COG_EMOJI)
        filtered = await self.filter_commands(cog.get_commands(), sort=True)

        embed = discord.Embed(
            title=f"{emoji} {cog.qualified_name}",
            description=(cog.description or "").strip() or "No description provided.",
            color=EMBED_COLOR,
        )

        for command in filtered:
            embed.add_field(
                name=f"{prefix}{command.name} {command.signature}".strip(),
                value=command.help or "No description provided.",
                inline=False,
            )

        await self.get_destination().send(embed=embed)

    async def send_command_help(self, command):
        prefix = self.context.clean_prefix
        embed = discord.Embed(
            title=f"{prefix}{command.name} {command.signature}".strip(),
            description=command.help or "No description provided.",
            color=EMBED_COLOR,
        )
        if command.aliases:
            embed.add_field(name="Aliases", value=", ".join(command.aliases), inline=False)

        await self.get_destination().send(embed=embed)

    async def send_group_help(self, group):
        await self.send_command_help(group)

    async def send_error_message(self, error):
        embed = discord.Embed(description=error, color=discord.Color.red())
        await self.get_destination().send(embed=embed)
