"""
game_suggestions_cog.py
-----------------------
Suggests games to play from a YAML list (games.yaml), with filter buttons
(Mac / Remote Play) and per-person "avoid" lists driven by who is in your
voice channel.

Config is auto-created next to this file on first run and re-read on every
command, so edits to games.yaml need no bot restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import discord
import yaml
from discord.ext import commands

logger = logging.getLogger("bot.game_suggestions")

CONFIG_PATH = Path(__file__).parent / "games.yaml"

MAC_EMOJI = "🍎"
REMOTE_EMOJI = "🎮"

MAX_PLAYER_BUTTONS = 20   # rows 1-4 of a Discord view (row 0 holds the filters)
DESC_LIMIT = 3800         # embed description cap is 4096; leave headroom
VIEW_TIMEOUT = 600        # seconds before the buttons go inactive

TEMPLATE = '''\
# Config for the !suggest_games command.
# This file is re-read every time the command runs - no bot restart needed.
#
# games:
#   title           - shown in the list (also what `dislikes` below refers to)
#   url             - optional; http(s) links become clickable
#   mac_compatible  - true/false (shows a 🍎 tag)
#   remote_play     - true/false (shows a 🎮 tag)
#
# players (optional, for the "avoid games" feature):
#   name        - label used on the filter buttons
#   discord_id  - run !game_ids in Discord to get this
#   dislikes    - list of game titles to hide when this person is in your voice channel
#                 (must match a title above; case-insensitive)

games:
  # - title: "Counter-Strike 2"
  #   url: "https://store.steampowered.com/app/730/"
  #   mac_compatible: false
  #   remote_play: false
  # - title: "Stardew Valley"
  #   url: "https://store.steampowered.com/app/413150/"
  #   mac_compatible: true
  #   remote_play: true

players:
  # - name: "Alex"
  #   discord_id: 123456789012345678
  #   dislikes:
  #     - "Counter-Strike 2"
'''


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #

class ConfigError(Exception):
    """games.yaml is unreadable or structurally invalid."""


def _norm(text: str) -> str:
    return " ".join(str(text).casefold().split())


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1"}
    return bool(value)


@dataclass(frozen=True)
class Game:
    title: str
    url: Optional[str]
    mac_compatible: bool
    remote_play: bool


@dataclass(frozen=True)
class Player:
    name: str
    discord_id: int
    dislikes: frozenset  # of normalised titles


@dataclass
class Config:
    games: list = field(default_factory=list)
    players: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        path.write_text(TEMPLATE, encoding="utf-8")
        logger.info("Created template %s", path.name)

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"`{path.name}` has a YAML syntax error: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"`{path.name}` should contain top-level `games:` and `players:` keys.")

    cfg = Config()

    for i, entry in enumerate(raw.get("games") or [], start=1):
        title = str(entry.get("title", "")).strip() if isinstance(entry, dict) else ""
        if not title:
            cfg.warnings.append(f"games entry #{i} skipped (needs a `title`).")
            continue
        url = str(entry.get("url") or "").strip() or None
        cfg.games.append(Game(
            title=title,
            url=url,
            mac_compatible=_as_bool(entry.get("mac_compatible", False)),
            remote_play=_as_bool(entry.get("remote_play", False)),
        ))

    known_titles = {_norm(g.title) for g in cfg.games}

    for i, entry in enumerate(raw.get("players") or [], start=1):
        if not isinstance(entry, dict):
            cfg.warnings.append(f"players entry #{i} skipped (not a mapping).")
            continue
        raw_id = entry.get("discord_id")
        try:
            if isinstance(raw_id, bool):
                raise ValueError
            discord_id = int(str(raw_id).strip())
        except (TypeError, ValueError):
            cfg.warnings.append(f"players entry #{i} skipped (`discord_id` must be a number).")
            continue

        name = str(entry.get("name") or f"User {discord_id}").strip()
        dislikes = entry.get("dislikes") or []
        if isinstance(dislikes, str):
            dislikes = [dislikes]

        normed = set()
        for title in dislikes:
            key = _norm(title)
            normed.add(key)
            if key not in known_titles:
                cfg.warnings.append(f"{name} dislikes \"{title}\", which isn't in the games list.")
        cfg.players.append(Player(name=name, discord_id=discord_id, dislikes=frozenset(normed)))

    for w in cfg.warnings:
        logger.warning("%s: %s", path.name, w)
    return cfg


# --------------------------------------------------------------------------- #
# Filtering / formatting
# --------------------------------------------------------------------------- #

def filter_games(games, players, *, mac_only: bool, remote_only: bool, avoid_ids: set):
    """Return (games to show, number hidden purely by dislikes)."""
    pool = [
        g for g in games
        if (not mac_only or g.mac_compatible) and (not remote_only or g.remote_play)
    ]
    avoided = set()
    for p in players:
        if p.discord_id in avoid_ids:
            avoided |= p.dislikes
    shown = [g for g in pool if _norm(g.title) not in avoided]
    return shown, len(pool) - len(shown)


def format_game(game: Game) -> str:
    safe = discord.utils.escape_markdown(game.title).replace("[", "\\[").replace("]", "\\]")
    if game.url and game.url.lower().startswith(("http://", "https://")):
        url = game.url.replace(" ", "%20").replace(")", "%29")
        name = f"[{safe}]({url})"
    elif game.url:
        # Custom schemes (steam://...) aren't clickable in Discord; show copyable text.
        name = f"{safe} — `{game.url.replace('`', '')}`"
    else:
        name = safe

    tags = " ".join(t for t in (
        MAC_EMOJI if game.mac_compatible else "",
        REMOTE_EMOJI if game.remote_play else "",
    ) if t)
    return f"• {name} {tags}".rstrip()


# --------------------------------------------------------------------------- #
# Interactive filter view
# --------------------------------------------------------------------------- #

class _FilterButton(discord.ui.Button):
    def __init__(self, key, label: str, emoji: Optional[str] = None, row: int = 0):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle(interaction, self.key)


class GameFilterView(discord.ui.View):
    """Embed + toggle buttons. Keys: 'mac', 'remote', 'reset', or a player's Discord ID."""

    def __init__(self, config: Config, *, channel=None, present_ids: Optional[set] = None):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.config = config
        self.channel = channel
        self.message: Optional[discord.Message] = None

        self.mac_only = False
        self.remote_only = False
        present_ids = present_ids or set()
        # Auto-avoid anyone configured who is in the voice channel right now.
        self.avoid_ids = {p.discord_id for p in config.players if p.discord_id in present_ids}

        self._toggles: dict = {}
        self._add_button("mac", "Mac only", MAC_EMOJI, row=0)
        self._add_button("remote", "Remote Play only", REMOTE_EMOJI, row=0)
        self._add_button("reset", "Show everything", "🔄", row=0, toggle=False)

        # People currently in voice come first so they're never cut by the button cap.
        ordered = sorted(config.players, key=lambda p: p.discord_id not in present_ids)
        if len(ordered) > MAX_PLAYER_BUTTONS:
            logger.warning("Only the first %d players get buttons.", MAX_PLAYER_BUTTONS)
        for idx, player in enumerate(ordered[:MAX_PLAYER_BUTTONS]):
            self._add_button(player.discord_id, player.name[:30], "🚫", row=1 + idx // 5)

        self._refresh_styles()

    def _add_button(self, key, label, emoji, row, toggle: bool = True):
        button = _FilterButton(key, label, emoji, row)
        self.add_item(button)
        if toggle:
            self._toggles[key] = button

    def _is_active(self, key) -> bool:
        if key == "mac":
            return self.mac_only
        if key == "remote":
            return self.remote_only
        return key in self.avoid_ids

    def _refresh_styles(self):
        for key, button in self._toggles.items():
            if not self._is_active(key):
                button.style = discord.ButtonStyle.secondary
            elif isinstance(key, int):
                button.style = discord.ButtonStyle.danger
            else:
                button.style = discord.ButtonStyle.success

    async def handle(self, interaction: discord.Interaction, key):
        if key == "mac":
            self.mac_only = not self.mac_only
        elif key == "remote":
            self.remote_only = not self.remote_only
        elif key == "reset":
            self.mac_only = self.remote_only = False
            self.avoid_ids.clear()
        else:
            self.avoid_ids.symmetric_difference_update({key})
        self._refresh_styles()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def build_embed(self) -> discord.Embed:
        cfg = self.config
        shown, hidden = filter_games(
            cfg.games, cfg.players,
            mac_only=self.mac_only, remote_only=self.remote_only, avoid_ids=self.avoid_ids,
        )

        notes = []
        if self.channel is not None:
            notes.append(f"🔊 {self.channel.name}")
        if self.mac_only:
            notes.append(f"{MAC_EMOJI} Mac only")
        if self.remote_only:
            notes.append(f"{REMOTE_EMOJI} Remote Play only")
        avoiding = [p.name for p in cfg.players if p.discord_id in self.avoid_ids]
        if avoiding:
            notes.append("🚫 Avoiding: " + ", ".join(avoiding))

        lines = [format_game(g) for g in sorted(shown, key=lambda g: g.title.casefold())]
        body, used = [], 0
        for i, line in enumerate(lines):
            if used + len(line) + 1 > DESC_LIMIT:
                body.append(f"*…and {len(lines) - i} more — use the filters to narrow it down.*")
                break
            body.append(line)
            used += len(line) + 1

        if not cfg.games:
            body = ["*No games in `games.yaml` yet.*"]
        elif not body:
            body = ["*No games match these filters.*"]

        description = "\n".join(body)
        if notes:
            description = " · ".join(notes) + "\n\n" + description

        embed = discord.Embed(
            title=f"🎲 Game suggestions ({len(shown)})",
            description=description,
            color=discord.Color.blurple(),
        )
        footer = f"{MAC_EMOJI} Mac compatible · {REMOTE_EMOJI} Remote Play"
        if hidden:
            footer += f" · {hidden} hidden by dislikes"
        embed.set_footer(text=footer)

        if cfg.warnings:
            shown_warnings = [f"• {w[:180]}" for w in cfg.warnings[:5]]
            if len(cfg.warnings) > 5:
                shown_warnings.append(f"…and {len(cfg.warnings) - 5} more (see bot log)")
            embed.add_field(name="⚠️ games.yaml issues", value="\n".join(shown_warnings), inline=False)
        return embed

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #

class GameSuggestionsCog(commands.Cog, name="Game Suggestions"):
    """Suggest games to play, filtered by platform and by who's in voice."""
    COG_EMOJI = "🎲"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="suggest_games")
    @commands.guild_only()
    async def suggest_games(self, ctx: commands.Context):
        """Suggest games to play, with filter buttons.

        Lists every game in games.yaml (🍎 = Mac compatible, 🎮 = Remote Play).
        Use the buttons to show only Mac and/or Remote Play games.

        If you're in a voice channel, games disliked by anyone in it are
        hidden automatically. A button per person lets you toggle that, and
        "Show everything" clears all filters.
        """
        config = load_config()
        voice = getattr(ctx.author, "voice", None)
        channel = voice.channel if voice and voice.channel else None
        # Use voice_states (user IDs straight from Discord's voice data) rather than
        # channel.members: without the privileged "members" intent, anyone who was
        # already in voice when the bot started isn't in the member cache and gets dropped.
        present_ids = set(channel.voice_states) if channel else set()
        logger.info(
            "!suggest_games: channel=%s present_ids=%s configured_players=%s",
            channel.name if channel else None,
            sorted(present_ids),
            [p.discord_id for p in config.players],
        )

        view = GameFilterView(config, channel=channel, present_ids=present_ids)
        view.message = await ctx.reply(embed=view.build_embed(), view=view, mention_author=False)

    @commands.command(name="game_ids")
    @commands.guild_only()
    async def game_ids(self, ctx: commands.Context, *members: discord.Member):
        """Show Discord IDs, ready to paste into games.yaml.

        Lists everyone in your voice channel, or the people you @mention.
        Example: !game_ids @Alex @Sam
        """
        if members:
            targets = list(members)
        else:
            voice = getattr(ctx.author, "voice", None)
            if not voice or not voice.channel:
                await ctx.reply(
                    "Join a voice channel or @mention the people you want IDs for.",
                    mention_author=False,
                )
                return
            targets = []
            for user_id in voice.channel.voice_states:
                member = ctx.guild.get_member(user_id)
                if member is None:  # not cached (no members intent) - ask Discord directly
                    try:
                        member = await ctx.guild.fetch_member(user_id)
                    except discord.HTTPException:
                        continue
                if not member.bot:
                    targets.append(member)

        targets = targets[:20]
        snippet = "\n".join(
            f'  - name: "{m.display_name.replace(chr(34), chr(39))}"\n'
            f"    discord_id: {m.id}\n"
            f"    dislikes: []"
            for m in targets
        )
        await ctx.reply(
            f"Paste these under `players:` in `games.yaml`:\n```yaml\n{snippet}\n```",
            mention_author=False,
        )

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        error = getattr(error, "original", error)
        if isinstance(error, ConfigError):
            await ctx.reply(f"⚠️ {error}", mention_author=False)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.reply("This command only works in a server.", mention_author=False)
        elif isinstance(error, commands.BadArgument):
            await ctx.reply(f"Couldn't understand that: {error}", mention_author=False)
        else:
            logger.error("Unhandled error in %s", ctx.command, exc_info=error)
            await ctx.reply("Something went wrong — check the bot log.", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(GameSuggestionsCog(bot))
