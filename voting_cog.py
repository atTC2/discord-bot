"""
voting_cog.py

A drop-in discord.py Cog that adds a nomination/voting feature set to an
existing bot, following the bot's cog conventions (see cog_guide.md):
name= on the Cog, docstrings for !help, COG_EMOJI, ctx.reply(...,
mention_author=False) for command responses, and cog_command_error.

Dependency: PyYAML (`pip install pyyaml`) for the config file. Everything
else here only needs discord.py and the standard library.

Integration:
    # main.py
    EXTENSIONS = [
        "music_cog",
        "voting_cog",
    ]

Commands:
    !start_vote   - snapshots everyone in your current voice channel, posts a
                    live-updating status message (with buttons), and DMs each
                    participant asking them to vote.
    !end_vote     - once at least features.min_votes_to_end people have
                    actually voted (abstentions don't count toward this,
                    though they still show up as a response), tallies
                    points and announces a winner. Errors if called too
                    early. Ties automatically trigger a tie-breaker round
                    restricted to the tied nominees.
    !cancel_vote  - closes an in-progress vote without printing results.

The live-updating message also carries three buttons doing the same things
as the three commands above, plus "Include Previous Runners-Up" (see below).

Voting (via DM to the bot):
    Reply with up to 3 lines, most preferred first, e.g.:
        1. Option A
        2. Option B
        3. Option C
    Any leading number/letter followed by "." ")" "]" or "}" is stripped.
    Matching is case-insensitive. Each vote DM also carries an Abstain
    button for anyone who doesn't want to vote that round.

Scoring: 1st line = 3 points, 2nd line = 2 points, 3rd line = 1 point.

Runners-up history:
    Whenever a round ends with a single winner, every other nominee that
    actually received votes that round has its "runner-up streak" bumped by
    1 in a per-guild JSON file on disk (data/vote_history/<guild_id>.json).
    The winner's streak (if any) is cleared.

    If a round ends in a tie instead, anyone who got votes but wasn't part
    of the tie has already lost outright, so their streak is bumped right
    then -- otherwise those first-round votes would be lost once the
    tie-break narrows the field down to just the tied nominees. The tied
    nominees themselves stay untouched until the tie-break resolves (bumped
    if they lose it, cleared if they win it, or bumped again if it ties yet
    again).

    Clicking "Include Previous Runners-Up" on a live vote loads that file,
    posts its contents as a reply to the live-updating message, and, for
    this round only, adds each nominee's streak as bonus points on top of
    their normal score -- but only for nominees someone actually votes for
    this round. Nominees nobody votes for are left untouched either way.

!start_vote requires at least features.min_participants_to_start people in
the voice channel (default 3 -- below that, a vote doesn't add much over
just talking it out). Set it to 0 to remove the check entirely.

Config file (voting_config.yaml, written next to this file on first run,
re-read at the start of every command so edits need no restart):

    features:
      sound_effects_enabled -- off by default. See "Sound effects" below.
      min_participants_to_start -- default 3. !start_vote refuses to start
        with fewer people than this in the voice channel. 0 removes the
        check (a vote can start with any number of people, even 1).
      min_votes_to_end -- default 3. !end_vote refuses to tally results
        until at least this many actual votes have been cast (abstentions
        don't count toward it). 0 removes the check (!end_vote works even
        with zero votes cast -- handled as "no winner this round").

    lookups:
    Maps a short vote key (what people actually type/vote, e.g. "oam") to
    a full display title and an optional url. Wherever a nominee is shown
    in results, the tie announcement, or the runner-up history list, a
    configured key is swapped for its title; keys with no entry just show
    as typed. When url is set, that title becomes a clickable link (e.g.
    "Age of Mythology" linking to its Steam store page) -- those messages
    are sent as embeds specifically so the link renders, since Discord
    only supports markdown links `[text](url)` inside embeds/interaction
    responses, never in plain message content. Use an ordinary http(s)
    page (a store page, a Spotify track, a YouTube video...) rather than
    an app-specific protocol like steam:// -- Discord no longer renders
    custom protocols as clickable links at all, but a plain web link works
    everywhere, and most such pages show their own "Play"/"Launch" button
    once you're signed in anyway.

Sound effects:
    Off by default (features.sound_effects_enabled: false in
    voting_config.yaml) until you've actually got clips to play. Once
    enabled: drop your own audio clips into a "sounds" folder next to this
    file (voting_started / voting_finished / voting_cancelled /
    voting_tiebreak / voting_toofew, any ffmpeg-readable format) and the
    bot will connect to the voice channel being voted in, play the clip
    once, and disconnect. voting_tiebreak plays whenever a round ties and
    a tie-breaker round starts (in addition to the usual tie announcement
    message). voting_toofew plays whenever !start_vote is rejected for
    having fewer than 3 people in the voice channel. A README.txt is
    written into that folder automatically the first time this cog runs.
    If a clip is missing, that sting is silently skipped. If the bot is
    already playing something in that guild (e.g. music), the sting is
    skipped entirely rather than interrupting it -- discord.py only
    supports one audio stream per voice connection, so a voice line and
    music can't play at once on the same connection.

    Playback volume is features.sound_effects_volume in voting_config.yaml
    -- 1.0 is the clip's original volume, 0.5 is half, 0.0 is silent;
    values above 1.0 amplify further but can distort, so it's clamped to
    2.0 max. Defaults to 0.5.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Union

import discord
import yaml
from discord.ext import commands

# Matches a leading enumerator like "1.", "1)", "a.", "A)", "iii}" etc.
_ENUM_RE = re.compile(r"^\s*[0-9a-zA-Z]{1,3}\s*[.\)\]\}]\s*")

_WEIGHTS = [3, 2, 1]

# Defaults for the two config-driven minimums below (see VotingConfig and
# voting_config.yaml) -- only used the first time the config file is
# created, or if a value is missing/invalid in it.
_DEFAULT_MIN_PARTICIPANTS_TO_START = 3
_DEFAULT_MIN_VOTES_TO_END = 3

_HISTORY_DIR = Path(__file__).resolve().parent / "data" / "vote_history"

_CONFIG_PATH = Path(__file__).resolve().parent / "voting_config.yaml"
_CONFIG_TEMPLATE = """\
# Voting cog configuration.
# Edit this file directly and it takes effect on the next !start_vote or
# !end_vote - no bot restart needed.

features:
  # Dramatic start/finish/cancel/tie-break voice lines (see
  # sounds/README.txt). Leave this off until you've actually dropped clips
  # in there - flipping it on with no clips present just means nothing
  # plays, but there's no point turning it on early.
  sound_effects_enabled: false

  # Playback volume for those clips: 1.0 is the clip's original volume,
  # 0.5 is half as loud, 0.0 is silent. Values above 1.0 amplify further
  # but can distort - kept clamped to 2.0 max. Defaults to 0.5 so a clip
  # recorded at full volume doesn't blast anyone.
  sound_effects_volume: 0.5

  # Fewer than this many people in the voice channel and !start_vote
  # refuses to start ("just talk it out instead"). 0 removes the check
  # entirely - a vote can start with any number of people, even 1.
  min_participants_to_start: 3

  # !end_vote needs at least this many actual votes cast before it'll
  # tally and announce a winner - abstentions don't count toward this,
  # though they still show up as a response. 0 removes the check entirely
  # - !end_vote works even with zero votes cast (handled as "no winner").
  # Note: if min_participants_to_start is 3 (or more) and this is left at
  # its own default of 3, a vote can deadlock if anyone abstains, since
  # that leaves only 2 people who can still vote - !cancel_vote is the way
  # out of that one. Lowering this value avoids the deadlock.
  min_votes_to_end: 3

# Lookup table: short vote keys -> full title (+ optional web link).
# People still vote using the short key (e.g. "oam") - results, the tie
# announcement, and the runner-up history list all show the full title
# instead, as a clickable link whenever a url is given.
#
# Use an ordinary http(s) page here (a store page, a Spotify track, a
# YouTube video...) rather than an app-specific protocol like steam:// -
# Discord no longer renders custom protocols as clickable links at all,
# but a plain web link works everywhere, and most of these pages show
# their own "Play"/"Launch" button once you're signed in anyway.
#
# url is optional; omit it for a plain title with no link.
lookups:
  oam:
    title: "Age of Mythology"
    url: "https://store.steampowered.com/app/266840/Age_of_Mythology_Extended_Edition/"
"""

# Drop your own audio files here, named voting_started/voting_finished/
# voting_cancelled/voting_tiebreak/voting_toofew with any ffmpeg-readable
# extension (mp3, wav, ogg, m4a, flac...). If a file's missing, that sting
# is just silently skipped.
_SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"
_SOUND_BASENAMES = {
    "start": "voting_started",
    "end": "voting_finished",
    "cancel": "voting_cancelled",
    "tie": "voting_tiebreak",
    "too_few": "voting_toofew",
}
_SOUND_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a", ".flac")
_MAX_SOUND_VOLUME = 2.0
_DEFAULT_SOUND_VOLUME = 0.5
_SOUNDS_README = """\
Drop dramatic sound effects here for the voting cog to play.

Files are matched by name (any extension ffmpeg understands: mp3, wav,
ogg, m4a, flac...):

    voting_started.mp3    - played when !start_vote kicks off
    voting_finished.mp3   - played when a vote concludes with a result
    voting_cancelled.mp3  - played when !cancel_vote is used
    voting_tiebreak.mp3   - played when a round ties and a tie-break starts
    voting_toofew.mp3     - played when !start_vote is rejected for having
                             fewer than 3 people in the voice channel

If a file isn't present, that sting is just skipped silently - nothing
breaks. The bot connects to the voice channel being voted in, plays the
clip once, and disconnects again, but ONLY if it isn't already playing
something else in that server (e.g. music) - it will never interrupt or
talk over an existing stream.

Playback volume is controlled by features.sound_effects_volume in
voting_config.yaml (0.0-2.0, default 0.5 - half volume so nobody gets
blasted), not by editing these files.
"""

SendFn = Callable[[Union[str, discord.Embed]], Awaitable[None]]


def _strip_enumeration(line: str) -> str:
    return _ENUM_RE.sub("", line).strip()


@dataclass
class LookupEntry:
    title: str
    url: Optional[str] = None


class VotingConfig:
    """Loads voting_config.yaml: the sound-effects feature toggle, the
    participant/vote minimums, and the key -> title/link lookup table.
    Cheap to re-read, so callers reload it at the start of each command
    rather than caching forever - edits take effect on the next vote with
    no bot restart."""

    def __init__(self, path: Path):
        self.path = path
        if not self.path.exists():
            self.path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
        self.sound_effects_enabled: bool = False
        self.sound_effects_volume: float = _DEFAULT_SOUND_VOLUME
        self.min_participants_to_start: int = _DEFAULT_MIN_PARTICIPANTS_TO_START
        self.min_votes_to_end: int = _DEFAULT_MIN_VOTES_TO_END
        self.lookups: Dict[str, LookupEntry] = {}
        self.reload()

    def reload(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            data = {}

        features = data.get("features") or {}
        self.sound_effects_enabled = bool(features.get("sound_effects_enabled", False))

        try:
            volume = float(features.get("sound_effects_volume", _DEFAULT_SOUND_VOLUME))
        except (TypeError, ValueError):
            volume = _DEFAULT_SOUND_VOLUME
        self.sound_effects_volume = max(0.0, min(volume, _MAX_SOUND_VOLUME))

        try:
            min_participants = int(features.get("min_participants_to_start", _DEFAULT_MIN_PARTICIPANTS_TO_START))
        except (TypeError, ValueError):
            min_participants = _DEFAULT_MIN_PARTICIPANTS_TO_START
        self.min_participants_to_start = max(0, min_participants)

        try:
            min_votes = int(features.get("min_votes_to_end", _DEFAULT_MIN_VOTES_TO_END))
        except (TypeError, ValueError):
            min_votes = _DEFAULT_MIN_VOTES_TO_END
        self.min_votes_to_end = max(0, min_votes)

        lookups: Dict[str, LookupEntry] = {}
        for key, value in (data.get("lookups") or {}).items():
            if not isinstance(value, dict) or "title" not in value:
                continue
            lookups[str(key).lower()] = LookupEntry(
                title=str(value["title"]),
                url=value.get("url"),
            )
        self.lookups = lookups

    def lookup(self, key: str) -> Optional[LookupEntry]:
        return self.lookups.get(key.lower())


class SoundEffects:
    """Finds and plays short "sting" audio clips (vote started/finished/
    cancelled/tie-break) at a configurable volume in a guild's voice
    channel, connecting and disconnecting just for the clip. Never
    interrupts audio the bot is already playing (e.g. music) - if the
    guild's voice client is already playing something, the sting is
    skipped entirely."""

    def __init__(self, bot: commands.Bot, directory: Path):
        self.bot = bot
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        readme = directory / "README.txt"
        if not any(directory.iterdir()):
            readme.write_text(_SOUNDS_README, encoding="utf-8")

    def _find(self, key: str) -> Optional[Path]:
        base = _SOUND_BASENAMES.get(key)
        if base is None:
            return None
        for ext in _SOUND_EXTENSIONS:
            candidate = self.directory / f"{base}{ext}"
            if candidate.exists():
                return candidate
        return None

    def fire(
        self,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel,
        key: str,
        volume: float = _DEFAULT_SOUND_VOLUME,
    ) -> None:
        """Schedules the sting in the background; never awaited/blocking."""
        path = self._find(key)
        if path is None:
            return
        self.bot.loop.create_task(self._play(guild, voice_channel, path, volume))

    async def _play(
        self, guild: discord.Guild, voice_channel: discord.VoiceChannel, path: Path, volume: float
    ) -> None:
        vc = guild.voice_client

        if vc is not None and vc.is_playing():
            return  # already playing something (e.g. music) - never step on it

        connected_here = False
        try:
            if vc is None:
                vc = await voice_channel.connect()
                connected_here = True
            elif vc.channel.id != voice_channel.id:
                await vc.move_to(voice_channel)

            finished = asyncio.Event()

            def _after(_error):
                self.bot.loop.call_soon_threadsafe(finished.set)

            clamped_volume = max(0.0, min(volume, _MAX_SOUND_VOLUME))
            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(str(path)), volume=clamped_volume)
            vc.play(source, after=_after)
            await asyncio.wait_for(finished.wait(), timeout=30)
        except (discord.ClientException, discord.DiscordException, asyncio.TimeoutError, OSError):
            pass
        finally:
            if connected_here and vc is not None:
                try:
                    await vc.disconnect()
                except discord.ClientException:
                    pass


class HistoryStore:
    """Tiny per-guild JSON store of {nominee: consecutive_runner_up_rounds}."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, guild_id: int) -> Path:
        return self.directory / f"{guild_id}.json"

    def load(self, guild_id: int) -> Dict[str, int]:
        path = self._path(guild_id)
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, guild_id: int, data: Dict[str, int]) -> None:
        with self._path(guild_id).open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)


@dataclass
class Participant:
    member: discord.Member
    status: str = "pending"          # "pending" | "voted" | "abstained"
    votes: List[str] = field(default_factory=list)


@dataclass
class VoteSession:
    guild_id: int
    voice_channel: discord.VoiceChannel
    text_channel: discord.TextChannel
    participants: Dict[int, Participant]
    tracking_message: Optional[discord.Message] = None
    # Set during a tie-breaker round to the list of tied nominees; votes are
    # then restricted to these options only.
    runoff_options: Optional[List[str]] = None
    # Whether "Include Previous Runners-Up" has been used this round.
    include_history: bool = False
    history_snapshot: Dict[str, int] = field(default_factory=dict)


class VotingCog(commands.Cog, name="Voting"):
    """Run ranked-choice votes among people in a voice channel. Needs a minimum headcount to start (configurable, default 3) — smaller groups should just talk it out."""

    COG_EMOJI = "🗳️"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_votes: Dict[int, VoteSession] = {}
        self.history = HistoryStore(_HISTORY_DIR)
        self.sounds = SoundEffects(bot, _SOUNDS_DIR)
        self.config = VotingConfig(_CONFIG_PATH)

    # ---------- error handling ----------

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.reply(f"⚠️ Something went wrong: {error}", mention_author=False)

    # ---------- helpers ----------

    def _display_nominee(self, key: str, as_markdown_link: bool = False) -> str:
        """Turns a raw vote key into its configured display form. Falls
        back to the raw key when there's no lookup entry for it. Masked
        markdown links only render in embeds/interaction responses, never
        in plain message content, so as_markdown_link should only be True
        when the caller is about to put this into an Embed."""
        entry = self.config.lookup(key)
        if entry is None:
            return key
        if as_markdown_link and entry.url:
            return f"[{entry.title}]({entry.url})"
        return entry.title

    async def _reply_payload(self, ctx: commands.Context, payload: Union[str, discord.Embed]) -> None:
        if isinstance(payload, discord.Embed):
            await ctx.reply(embed=payload, mention_author=False)
        else:
            await ctx.reply(payload, mention_author=False)

    async def _channel_payload(self, channel: discord.abc.Messageable, payload: Union[str, discord.Embed]) -> None:
        if isinstance(payload, discord.Embed):
            await channel.send(embed=payload)
        else:
            await channel.send(payload)

    def _find_participant(self, user_id: int):
        for session in self.active_votes.values():
            if user_id in session.participants:
                return session, session.participants[user_id]
        return None, None

    def _end_vote_status(self, session: VoteSession):
        """Returns (votes_cast, responded, total, can_end). votes_cast only
        counts actual votes -- abstentions don't count toward the threshold
        even though they do count as a response."""
        total = len(session.participants)
        votes_cast = sum(1 for p in session.participants.values() if p.status == "voted")
        responded = sum(1 for p in session.participants.values() if p.status != "pending")
        can_end = votes_cast >= self.config.min_votes_to_end
        return votes_cast, responded, total, can_end

    def _build_embed(self, session: VoteSession, finished: bool = False, cancelled: bool = False) -> discord.Embed:
        if cancelled:
            return discord.Embed(
                title="🗳️ Voting cancelled",
                description=f"Tracking voice channel: **{session.voice_channel.name}**",
                color=discord.Color.red(),
            )

        if finished:
            title = "🗳️ Voting finished"
            color = discord.Color.dark_grey()
        elif session.runoff_options:
            title = "🗳️ Tie-breaker vote in progress"
            color = discord.Color.blurple()
        else:
            title = "🗳️ Vote in progress"
            color = discord.Color.blurple()

        description = f"Tracking voice channel: **{session.voice_channel.name}**"
        if not finished:
            if session.runoff_options:
                description += "\nTie-breaker between: " + ", ".join(session.runoff_options)
            if session.include_history:
                description += "\n📜 Runners-up bonus included this round."

        embed = discord.Embed(title=title, description=description, color=color)

        lines = []
        for p in session.participants.values():
            if finished:
                icon, label = {
                    "pending": ("➖", "no response"),
                    "voted": ("✅", "voted"),
                    "abstained": ("🙅", "abstained"),
                }[p.status]
            else:
                icon, label = {
                    "pending": ("❌", "not voted"),
                    "voted": ("✅", "voted"),
                    "abstained": ("🙅", "abstained"),
                }[p.status]
            lines.append(f"{icon} {p.member.display_name} — {label}")
        embed.add_field(name="Status", value="\n".join(lines) or "No participants", inline=False)

        if not finished:
            votes_cast, responded, total, _ = self._end_vote_status(session)
            if self.config.min_votes_to_end > 0:
                votes_part = f"{votes_cast}/{self.config.min_votes_to_end} votes to end vote"
            else:
                votes_part = "no votes required to end vote"
            embed.set_footer(text=f"{votes_part} • {responded}/{total} responded")

        return embed

    def _build_view(self, session: VoteSession) -> discord.ui.View:
        _, _, _, can_end = self._end_vote_status(session)
        guild_id = session.guild_id

        view = discord.ui.View(timeout=None)

        include_button = discord.ui.Button(
            label="Include Previous Runners-Up",
            style=discord.ButtonStyle.blurple,
            disabled=session.include_history or session.runoff_options is not None,
        )

        async def include_callback(interaction: discord.Interaction):
            await self._on_include_history_button(interaction, guild_id)

        include_button.callback = include_callback
        view.add_item(include_button)

        end_button = discord.ui.Button(
            label="End Vote",
            style=discord.ButtonStyle.green,
            disabled=not can_end,
        )

        async def end_callback(interaction: discord.Interaction):
            await self._on_end_vote_button(interaction, guild_id)

        end_button.callback = end_callback
        view.add_item(end_button)

        cancel_button = discord.ui.Button(label="Cancel Vote", style=discord.ButtonStyle.red)

        async def cancel_callback(interaction: discord.Interaction):
            await self._on_cancel_vote_button(interaction, guild_id)

        cancel_button.callback = cancel_callback
        view.add_item(cancel_button)

        return view

    async def _update_tracking_message(self, session: VoteSession, finished: bool = False, cancelled: bool = False):
        if session.tracking_message is None:
            return
        embed = self._build_embed(session, finished=finished, cancelled=cancelled)
        view = None if (finished or cancelled) else self._build_view(session)
        try:
            await session.tracking_message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass

    def _format_prompt(self, session: VoteSession) -> str:
        if session.runoff_options:
            options_list = "\n".join(
                f"- {opt}" + (f" — {title}" if (title := self._display_nominee(opt)) != opt else "")
                for opt in session.runoff_options
            )
            return (
                f"🤝 Tie-breaker vote for **#{session.voice_channel.name}**!\n\n"
                f"Rank these in order of preference (top pick first), using the key on the left:\n{options_list}\n\n"
                "One per line. Leading numbers/letters followed by a full stop or "
                "bracket (like \"1)\" or \"a.\") are ignored.\n\n"
                "Don't want to vote? Tap the Abstain button below."
            )
        return (
            f"🗳️ Vote for **#{session.voice_channel.name}**!\n\n"
            "Reply with up to 3 lines, your top pick first, e.g.:\n"
            "1. Option A\n2. Option B\n3. Option C\n\n"
            "Leading numbers/letters followed by a full stop or bracket "
            "(like \"1)\" or \"a.\") are ignored, so plain lines work too.\n\n"
            "Don't want to vote this round? Tap the Abstain button below."
        )

    def _build_abstain_view(self) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        button = discord.ui.Button(label="Abstain", style=discord.ButtonStyle.red)

        async def callback(interaction: discord.Interaction):
            await self._on_abstain_button(interaction)

        button.callback = callback
        view.add_item(button)
        return view

    def _tally(self, session: VoteSession) -> Dict[str, int]:
        points: Dict[str, int] = {}
        for p in session.participants.values():
            if p.status != "voted":
                continue
            for i, nominee in enumerate(p.votes[:3]):
                points[nominee] = points.get(nominee, 0) + _WEIGHTS[i]

        if session.include_history and session.history_snapshot:
            for nominee in points:
                bonus = session.history_snapshot.get(nominee, 0)
                if bonus:
                    points[nominee] += bonus

        return points

    def _update_history(self, guild_id: int, points: Dict[str, int], winner: str) -> None:
        """Bump runner-up streaks for everyone who got votes but didn't win.
        Nominees not present in `points` (nobody voted for them this round)
        are left untouched. The winner's streak, if any, is cleared."""
        history = self.history.load(guild_id)
        for nominee in points:
            if nominee == winner:
                history.pop(nominee, None)
            else:
                history[nominee] = history.get(nominee, 0) + 1
        self.history.save(guild_id, history)

    def _bump_non_tied(self, guild_id: int, points: Dict[str, int], tied: List[str]) -> None:
        """Called the moment a tie is detected. Everyone who got votes this
        round but isn't part of the tie has already lost outright, so their
        streak bumps by 1 now -- otherwise that round's votes would be lost
        once the tie-break narrows things down to just the tied nominees.
        The tied nominees themselves are left alone; they're still live and
        get resolved (bumped or cleared) once the tie-break concludes."""
        still_live = set(tied)
        history = self.history.load(guild_id)
        changed = False
        for nominee in points:
            if nominee in still_live:
                continue
            history[nominee] = history.get(nominee, 0) + 1
            changed = True
        if changed:
            self.history.save(guild_id, history)

    async def _finish_session(self, guild: discord.Guild, session: VoteSession, cancelled: bool = False):
        await self._update_tracking_message(session, finished=True, cancelled=cancelled)
        self.active_votes.pop(guild.id, None)
        if self.config.sound_effects_enabled:
            self.sounds.fire(
                guild, session.voice_channel, "cancel" if cancelled else "end", volume=self.config.sound_effects_volume
            )

    async def _start_runoff(self, session: VoteSession, tied_nominees: List[str], send_result: SendFn):
        display_names = ", ".join(self._display_nominee(n, as_markdown_link=True) for n in tied_nominees)
        embed = discord.Embed(
            description=f"🤝 It's a tie between **{display_names}**! Sending everyone a tie-breaker vote.",
            color=discord.Color.orange(),
        )
        await send_result(embed)

        if self.config.sound_effects_enabled:
            self.sounds.fire(
                session.voice_channel.guild, session.voice_channel, "tie", volume=self.config.sound_effects_volume
            )

        session.runoff_options = tied_nominees
        for p in session.participants.values():
            p.status = "pending"
            p.votes = []

        await self._update_tracking_message(session)

        failed_dms = []
        for p in session.participants.values():
            try:
                await p.member.send(self._format_prompt(session), view=self._build_abstain_view())
            except discord.Forbidden:
                failed_dms.append(p.member.display_name)

        if failed_dms:
            await send_result(f"⚠️ Couldn't DM for the tie-breaker: {', '.join(failed_dms)}.")

    # ---------- shared core logic (used by both commands and buttons) ----------

    async def _perform_end_vote(self, guild: discord.Guild, send_result: SendFn) -> Optional[str]:
        """Returns an error string if the vote can't be ended yet, else None
        (in which case the result/tie notice has already been sent)."""
        session = self.active_votes.get(guild.id)
        if session is None:
            return "There's no active vote in this server."

        self.config.reload()

        votes_cast, responded, total, can_end = self._end_vote_status(session)

        if not can_end:
            return (
                f"⚠️ Not enough votes yet ({votes_cast}/{self.config.min_votes_to_end} votes cast — "
                f"abstentions don't count toward this; {responded}/{total} have responded)."
            )

        points = self._tally(session)

        if not points:
            await send_result("No votes were cast — no winner this round.")
            await self._finish_session(guild, session)
            return None

        ranked = sorted(points.items(), key=lambda kv: kv[1], reverse=True)
        top_score = ranked[0][1]
        winners = [name for name, score in ranked if score == top_score]

        if len(winners) > 1:
            self._bump_non_tied(guild.id, points, winners)
            await self._start_runoff(session, winners, send_result)
            return None

        self._update_history(guild.id, points, winners[0])

        winner_display = self._display_nominee(winners[0], as_markdown_link=True)
        points_text = "\n".join(
            f"{self._display_nominee(name, as_markdown_link=True)} — {score}" for name, score in ranked
        )
        embed = discord.Embed(
            title="🏆 Vote results",
            description=f"**Winner: {winner_display}** ({top_score} points)",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Points", value=points_text, inline=False)

        await send_result(embed)
        await self._finish_session(guild, session)
        return None

    async def _perform_cancel_vote(self, guild: discord.Guild, send_notice: SendFn) -> Optional[str]:
        session = self.active_votes.get(guild.id)
        if session is None:
            return "There's no active vote in this server."

        self.config.reload()

        await send_notice(f"🛑 Voting for **{session.voice_channel.name}** has been cancelled.")

        for p in session.participants.values():
            try:
                await p.member.send(f"🛑 The vote for **{session.voice_channel.name}** has been cancelled.")
            except discord.Forbidden:
                pass

        await self._finish_session(guild, session, cancelled=True)
        return None

    # ---------- button callbacks ----------

    async def _on_end_vote_button(self, interaction: discord.Interaction, guild_id: int):
        await interaction.response.defer()

        async def send_result(payload: Union[str, discord.Embed]):
            await self._channel_payload(interaction.channel, payload)

        error = await self._perform_end_vote(interaction.guild, send_result)
        if error:
            await interaction.followup.send(error, ephemeral=True)

    async def _on_cancel_vote_button(self, interaction: discord.Interaction, guild_id: int):
        await interaction.response.defer()

        async def send_notice(payload: Union[str, discord.Embed]):
            await self._channel_payload(interaction.channel, payload)

        error = await self._perform_cancel_vote(interaction.guild, send_notice)
        if error:
            await interaction.followup.send(error, ephemeral=True)

    async def _on_include_history_button(self, interaction: discord.Interaction, guild_id: int):
        await interaction.response.defer(ephemeral=True)

        session = self.active_votes.get(guild_id)
        if session is None:
            await interaction.followup.send("This vote has already ended.", ephemeral=True)
            return
        if session.include_history:
            await interaction.followup.send("Runners-up history is already included this round.", ephemeral=True)
            return

        self.config.reload()
        history = self.history.load(guild_id)
        session.include_history = True
        session.history_snapshot = history

        if history:
            ranked_history = sorted(history.items(), key=lambda kv: kv[1], reverse=True)
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, (name, streak) in enumerate(ranked_history):
                prefix = medals[i] if i < len(medals) else "•"
                lines.append(f"{prefix} {self._display_nominee(name, as_markdown_link=True)} — +{streak}")
            embed = discord.Embed(
                title="📜 Runners-up history",
                description="Bonus only applies if voted for this round.",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Streaks", value="\n".join(lines), inline=False)
        else:
            embed = discord.Embed(
                title="📜 Runners-up history",
                description="No runners-up history yet for this server.",
                color=discord.Color.blurple(),
            )

        try:
            await session.tracking_message.reply(embed=embed, mention_author=False)
        except discord.HTTPException:
            try:
                await session.text_channel.send(embed=embed)
            except discord.HTTPException:
                pass

        await self._update_tracking_message(session)
        await interaction.followup.send("Runners-up history included for this round.", ephemeral=True)

    async def _on_abstain_button(self, interaction: discord.Interaction):
        session, participant = self._find_participant(interaction.user.id)
        if session is None:
            await interaction.response.send_message("This vote has already ended.", ephemeral=True)
            return
        if participant.status != "pending":
            await interaction.response.send_message(
                "You've already submitted your vote for this round.", ephemeral=True
            )
            return

        participant.status = "abstained"
        participant.votes = []
        await interaction.response.send_message("You've abstained from this vote.", ephemeral=True)
        await self._update_tracking_message(session)

    # ---------- commands ----------

    @commands.command(name="start_vote")
    async def start_vote(self, ctx: commands.Context):
        """Start a new vote among everyone in your voice channel.

        Snapshots who's currently in the channel, then DMs each person asking
        for their ranked picks. Use !end_vote (or the button) once everyone's
        responded.

        Requires at least features.min_participants_to_start people in the
        channel (default 3; configurable in voting_config.yaml, down to 0
        to remove the check). Below that, skip the ceremony and talk it
        out directly.
        """
        self.config.reload()

        if ctx.guild.id in self.active_votes:
            await ctx.reply(
                "A vote is already in progress in this server. Use `!end_vote` or `!cancel_vote` first.",
                mention_author=False,
            )
            return

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.reply("You need to be in a voice channel to start a vote.", mention_author=False)
            return

        voice_channel = ctx.author.voice.channel
        members = [m for m in voice_channel.members if not m.bot]

        if len(members) < self.config.min_participants_to_start:
            if self.config.sound_effects_enabled:
                self.sounds.fire(ctx.guild, voice_channel, "too_few", volume=self.config.sound_effects_volume)
            await ctx.reply(
                f"Need at least {self.config.min_participants_to_start} people in the voice channel to hold a "
                "vote — with fewer than that, just talk it out instead!",
                mention_author=False,
            )
            return

        participants = {m.id: Participant(member=m) for m in members}
        session = VoteSession(
            guild_id=ctx.guild.id,
            voice_channel=voice_channel,
            text_channel=ctx.channel,
            participants=participants,
        )
        self.active_votes[ctx.guild.id] = session
        if self.config.sound_effects_enabled:
            self.sounds.fire(ctx.guild, voice_channel, "start", volume=self.config.sound_effects_volume)

        tracking_message = await ctx.reply(
            embed=self._build_embed(session), view=self._build_view(session), mention_author=False
        )
        session.tracking_message = tracking_message

        failed_dms = []
        for participant in participants.values():
            try:
                await participant.member.send(self._format_prompt(session), view=self._build_abstain_view())
            except discord.Forbidden:
                failed_dms.append(participant.member.display_name)

        if failed_dms:
            await ctx.reply(
                f"⚠️ Couldn't DM: {', '.join(failed_dms)}. "
                "They'll need to allow DMs from server members to vote.",
                mention_author=False,
            )

    @commands.command(name="end_vote")
    async def end_vote(self, ctx: commands.Context):
        """Tally votes and announce the winner.

        Requires at least features.min_votes_to_end actual votes to have
        been cast (default 3; configurable in voting_config.yaml, down to
        0 to remove the check) — abstentions don't count toward this.
        Ties automatically trigger a tie-breaker round instead of ending
        the vote.
        """
        error = await self._perform_end_vote(ctx.guild, lambda payload: self._reply_payload(ctx, payload))
        if error:
            await ctx.reply(error, mention_author=False)

    @commands.command(name="cancel_vote")
    async def cancel_vote(self, ctx: commands.Context):
        """Cancel the current vote with no winner."""
        error = await self._perform_cancel_vote(ctx.guild, lambda payload: self._reply_payload(ctx, payload))
        if error:
            await ctx.reply(error, mention_author=False)

    # ---------- DM handling ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return

        session, participant = self._find_participant(message.author.id)
        if session is None:
            return  # not part of any active vote; ignore

        if participant.status != "pending":
            await message.channel.send("You've already submitted your vote for this round.")
            return

        content = message.content.strip()

        raw_lines = [line.strip() for line in content.splitlines() if line.strip()]
        parsed = [_strip_enumeration(line).lower() for line in raw_lines[:3]]
        parsed = [p for p in parsed if p]

        if session.runoff_options:
            valid = set(session.runoff_options)
            filtered = []
            for item in parsed:
                if item in valid and item not in filtered:
                    filtered.append(item)
            parsed = filtered
            if not parsed:
                await message.channel.send(
                    "Please pick from the tied options: " + ", ".join(session.runoff_options)
                )
                return
        elif not parsed:
            await message.channel.send(
                "I couldn't read any picks from that. " + self._format_prompt(session)
            )
            return

        participant.votes = parsed
        participant.status = "voted"
        await message.channel.send(f"Vote recorded: {', '.join(parsed)}. Thanks!")
        await self._update_tracking_message(session)


async def setup(bot: commands.Bot):
    await bot.add_cog(VotingCog(bot))
