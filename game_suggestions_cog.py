"""
game_suggestions_cog.py
-----------------------
Suggests games to play from a YAML list (games.yaml), grouped by genre, with
filter buttons (Mac / Remote Play / genre) and per-person "avoid" lists driven
by who is in your voice channel.

Filter rules
  - Within a group, selected buttons combine as OR (Mac + Remote Play = games
    that are Mac-compatible OR remote-play; RTS + Party = RTS OR party games).
  - Between groups they combine as AND (Mac + RTS = Mac-compatible RTS games).
    "Low effort" is its own group, so it narrows whatever else is selected.
  - Person buttons hide anything that person dislikes.

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
LOW_EFFORT_EMOJI = "🛋️"

# key -> (display label, emoji). Any other genre you write in the YAML still works;
# it just gets no emoji and is sorted after these.
GENRE_INFO = {
    "rts": ("RTS", "⚔️"),
    "party": ("Party", "🎉"),
    "shooter": ("Shooter", "🔫"),
    "sandbox": ("Sandbox", "🏗️"),
    "coop": ("Co-op", "🤝"),
}
# Alternate spellings that map onto the keys above (keys are lowercase, letters/digits only).
GENRE_ALIASES = {
    "partygame": "party",
    "partygames": "party",
    "cooperative": "coop",
    "realtimestrategy": "rts",
}

MAX_GENRE_BUTTONS = 10    # two rows
MAX_FIELD_CHARS = 1000    # embed field value cap is 1024
MAX_FIELDS = 24           # embed cap is 25; one is reserved for config warnings
EMBED_BUDGET = 5200       # embed total cap is 6000; leave headroom for title/footer/warnings
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
#   low_effort      - true/false (shows a 🛋️ tag): easy to pick up, fine to play tired or chatting
#   genre           - one genre or a list: genre: rts   or   genre: [shooter, coop]
#                     Built-ins: rts, party, shooter, sandbox, coop. Any other word also works.
#                     A game with several genres is listed under each one.
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
  #   low_effort: false
  #   genre: shooter
  # - title: "Stardew Valley"
  #   url: "https://store.steampowered.com/app/413150/"
  #   mac_compatible: true
  #   remote_play: true
  #   low_effort: true
  #   genre: [sandbox, coop]

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


def _genre_key(text: str) -> str:
    key = "".join(ch for ch in str(text).casefold() if ch.isalnum())
    return GENRE_ALIASES.get(key, key)


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
    genres: tuple = ()   # normalised genre keys
    low_effort: bool = False


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
    genre_labels: dict = field(default_factory=dict)  # key -> display label, only genres in use

    def ordered_genres(self) -> list:
        """Genre keys in use: built-ins in a fixed order, then any custom ones A-Z."""
        known = [k for k in GENRE_INFO if k in self.genre_labels]
        extra = sorted(
            (k for k in self.genre_labels if k not in GENRE_INFO),
            key=lambda k: self.genre_labels[k].casefold(),
        )
        return known + extra

    def label(self, key: str) -> str:
        return self.genre_labels.get(key, key)


def genre_emoji(key: str) -> Optional[str]:
    return GENRE_INFO[key][1] if key in GENRE_INFO else None


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

        raw_genre = entry.get("genre")
        if raw_genre is None:
            values = []
        elif isinstance(raw_genre, (list, tuple)):
            values = list(raw_genre)
        else:
            values = [raw_genre]
        genre_keys = []
        for value in values:
            text = str(value).strip()
            if not text:
                continue
            key = _genre_key(text)
            if not key:
                continue
            if key not in genre_keys:
                genre_keys.append(key)
            cfg.genre_labels.setdefault(key, GENRE_INFO[key][0] if key in GENRE_INFO else text)

        cfg.games.append(Game(
            title=title,
            url=url,
            mac_compatible=_as_bool(entry.get("mac_compatible", False)),
            remote_play=_as_bool(entry.get("remote_play", False)),
            genres=tuple(genre_keys),
            low_effort=_as_bool(entry.get("low_effort", False)),
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

def filter_games(games, players, *, mac_only: bool, remote_only: bool, genres: set,
                 avoid_ids: set, low_effort_only: bool = False):
    """Return (games to show, number hidden purely by dislikes).

    Platform buttons are OR'd together, genre buttons are OR'd together, and the
    groups (platform, genre, low effort) are AND'd. Dislikes are applied last.
    """
    platform_active = mac_only or remote_only

    def platform_ok(g: Game) -> bool:
        if not platform_active:
            return True
        return (mac_only and g.mac_compatible) or (remote_only and g.remote_play)

    def genre_ok(g: Game) -> bool:
        return not genres or bool(genres.intersection(g.genres))

    pool = [
        g for g in games
        if platform_ok(g) and genre_ok(g) and (not low_effort_only or g.low_effort)
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
        LOW_EFFORT_EMOJI if game.low_effort else "",
    ) if t)
    return f"• {name} {tags}".rstrip()[:MAX_FIELD_CHARS]


def _chunk_lines(lines: list) -> list:
    """Pack lines into chunks that each fit in one embed field."""
    chunks, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) + 1 > MAX_FIELD_CHARS:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)
    return chunks


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
    """
    Embed + toggle buttons.

    Button keys: 'mac', 'remote', 'low', 'reset', ('genre', key), or a player's Discord ID (int).
    Layout: row 0 = Mac / Remote Play / Low effort / Show everything, then genre rows, then person rows.
    """

    def __init__(self, config: Config, *, channel=None, present_ids: Optional[set] = None):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.config = config
        self.channel = channel
        self.message: Optional[discord.Message] = None

        self.mac_only = False
        self.remote_only = False
        self.low_effort_only = False
        self.genres: set = set()
        present_ids = present_ids or set()
        # Auto-avoid anyone configured who is in the voice channel right now.
        self.avoid_ids = {p.discord_id for p in config.players if p.discord_id in present_ids}

        self._toggles: dict = {}

        # Row 0: platform filters + reset
        self._add_button("mac", "Mac", MAC_EMOJI, row=0)
        self._add_button("remote", "Remote Play", REMOTE_EMOJI, row=0)
        self._add_button("low", "Low effort", LOW_EFFORT_EMOJI, row=0)
        self._add_button("reset", "Show everything", "🔄", row=0, toggle=False)

        # Genre rows (part of the "filters" bar, above the dislikes bar)
        genre_keys = config.ordered_genres()
        if len(genre_keys) > MAX_GENRE_BUTTONS:
            logger.warning("Only the first %d genres get buttons.", MAX_GENRE_BUTTONS)
        genre_keys = genre_keys[:MAX_GENRE_BUTTONS]
        for idx, key in enumerate(genre_keys):
            self._add_button(("genre", key), config.label(key)[:30], genre_emoji(key), row=1 + idx // 5)
        genre_rows = -(-len(genre_keys) // 5)  # ceil

        # Person rows (the "dislikes" bar). People currently in voice come first
        # so they're never cut by the button cap.
        first_person_row = 1 + genre_rows
        capacity = 5 * (5 - first_person_row)
        ordered = sorted(config.players, key=lambda p: p.discord_id not in present_ids)
        if len(ordered) > capacity:
            logger.warning("Only the first %d players get buttons.", capacity)
        for idx, player in enumerate(ordered[:capacity]):
            self._add_button(player.discord_id, player.name[:30], "🚫", row=first_person_row + idx // 5)

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
        if key == "low":
            return self.low_effort_only
        if isinstance(key, tuple):
            return key[1] in self.genres
        return key in self.avoid_ids

    def _refresh_styles(self):
        for key, button in self._toggles.items():
            if not self._is_active(key):
                button.style = discord.ButtonStyle.secondary
            elif isinstance(key, int):
                button.style = discord.ButtonStyle.danger
            elif isinstance(key, tuple):
                button.style = discord.ButtonStyle.primary
            else:
                button.style = discord.ButtonStyle.success

    async def handle(self, interaction: discord.Interaction, key):
        if key == "mac":
            self.mac_only = not self.mac_only
        elif key == "remote":
            self.remote_only = not self.remote_only
        elif key == "low":
            self.low_effort_only = not self.low_effort_only
        elif key == "reset":
            self.mac_only = self.remote_only = self.low_effort_only = False
            self.genres.clear()
            self.avoid_ids.clear()
        elif isinstance(key, tuple):
            self.genres.symmetric_difference_update({key[1]})
        else:
            self.avoid_ids.symmetric_difference_update({key})
        self._refresh_styles()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    def build_embed(self) -> discord.Embed:
        cfg = self.config
        shown, hidden = filter_games(
            cfg.games, cfg.players,
            mac_only=self.mac_only, remote_only=self.remote_only,
            genres=self.genres, avoid_ids=self.avoid_ids,
            low_effort_only=self.low_effort_only,
        )

        # --- header line describing what's active ---
        notes = []
        if self.channel is not None:
            notes.append(f"🔊 {self.channel.name}")
        platforms = []
        if self.mac_only:
            platforms.append(f"{MAC_EMOJI} Mac")
        if self.remote_only:
            platforms.append(f"{REMOTE_EMOJI} Remote Play")
        if platforms:
            notes.append(" or ".join(platforms))
        if self.genres:
            notes.append("Genre: " + " or ".join(cfg.label(k) for k in cfg.ordered_genres() if k in self.genres))
        if self.low_effort_only:
            notes.append(f"{LOW_EFFORT_EMOJI} Low effort")
        avoiding = [p.name for p in cfg.players if p.discord_id in self.avoid_ids]
        if avoiding:
            notes.append("🚫 Avoiding: " + ", ".join(avoiding))

        # --- sections: one per genre (a game appears under each of its genres) ---
        by_title = lambda g: g.title.casefold()
        sections = []  # (heading, [games])
        visible_keys = [k for k in cfg.ordered_genres() if not self.genres or k in self.genres]
        for key in visible_keys:
            games = sorted((g for g in shown if key in g.genres), key=by_title)
            if games:
                emoji = genre_emoji(key)
                heading = f"{emoji} {cfg.label(key)}" if emoji else cfg.label(key)
                sections.append((heading, games))
        if not self.genres:
            other = sorted((g for g in shown if not g.genres), key=by_title)
            if other:
                heading = "🎲 Other" if sections else "🎲 Games"
                sections.append((heading, other))

        # --- pack sections into embed fields within Discord's size limits ---
        embed = discord.Embed(title=f"🎲 Game suggestions ({len(shown)})", color=discord.Color.blurple())
        used, field_count, not_shown = 0, 0, 0
        for heading, games in sections:
            for i, chunk in enumerate(_chunk_lines([format_game(g) for g in games])):
                name = f"{heading} ({len(games)})" if i == 0 else f"{heading} (cont.)"
                value = "\n".join(chunk)
                if field_count >= MAX_FIELDS or used + len(name) + len(value) > EMBED_BUDGET:
                    not_shown += len(chunk)
                    continue
                embed.add_field(name=name, value=value, inline=False)
                used += len(name) + len(value)
                field_count += 1

        description_parts = []
        if notes:
            description_parts.append(" · ".join(notes))
        if not cfg.games:
            description_parts.append("*No games in `games.yaml` yet.*")
        elif not shown:
            description_parts.append("*No games match these filters.*")
        if not_shown:
            description_parts.append(f"*…and {not_shown} more not shown — use the filters to narrow it down.*")
        if description_parts:
            embed.description = "\n\n".join(description_parts)

        footer = f"{MAC_EMOJI} Mac · {REMOTE_EMOJI} Remote Play · {LOW_EFFORT_EMOJI} Low effort"
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
    """Suggest games to play, grouped by genre and filtered by platform and who's in voice."""
    COG_EMOJI = "🎲"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="suggest_games")
    @commands.guild_only()
    async def suggest_games(self, ctx: commands.Context):
        """Suggest games to play, grouped by genre, with filter buttons.

        Lists every game in games.yaml (🍎 = Mac compatible, 🎮 = Remote Play,
        🛋️ = low effort). Use the top buttons to filter: Mac and Remote Play
        combine as "either", as do genres; the groups (platform, genre,
        low effort) combine as "both".

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
