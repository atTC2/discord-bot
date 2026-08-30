"""
Music Cog
---------
A loadable discord.py extension that streams audio from YouTube into a voice
channel. Load it from your main bot script with:

    await bot.load_extension("music_cog")

Commands:
    !play <youtube_url>   Joins your voice channel and plays/queues audio.
                           Accepts single video links (optionally with a
                           t=/start= timestamp) and playlist links.
    !skip                 Immediately skips to the next queued track.
    !queue                Posts a one-off snapshot of what's playing/queued.
    !stop                 Stops playback, clears the queue, and disconnects.

Playback status is shown in a single, live-updating "panel" message per
server (edited in place as tracks change) rather than a new message per
track, with a Skip button attached. See COG_GUIDE.md for how this cog's
docstrings/emoji feed into !help.

Requires FFmpeg on PATH. See README.md for setup instructions.
"""

import re
import asyncio
import logging
from collections import deque
from urllib.parse import urlparse, parse_qs

import discord
from discord.ext import commands
import yt_dlp

logger = logging.getLogger("music-bot")

# yt-dlp options for resolving a single video to a direct, streamable audio URL.
YTDLP_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}

# yt-dlp options for listing a playlist's contents WITHOUT resolving every
# video's stream URL up front (much faster for large playlists).
PLAYLIST_YTDLP_OPTIONS = {
    "extract_flat": "in_playlist",
    "quiet": True,
    "no_warnings": True,
}

FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn"

# How many upcoming tracks to list in the panel before summarizing the rest.
PANEL_QUEUE_PREVIEW_COUNT = 10
TITLE_DISPLAY_MAX_LENGTH = 70

ytdl = yt_dlp.YoutubeDL(YTDLP_OPTIONS)
playlist_ytdl = yt_dlp.YoutubeDL(PLAYLIST_YTDLP_OPTIONS)


def truncate(text: str, max_length: int = TITLE_DISPLAY_MAX_LENGTH) -> str:
    text = text or ""
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


# ---------------------------------------------------------------------------
# Per-server queue state
# ---------------------------------------------------------------------------

class GuildMusicState:
    """Holds the playback queue and a couple of flags for one Discord server."""
    def __init__(self):
        self.queue: deque[dict] = deque()
        self.current_track: dict | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.stopping: bool = False
        # The single live-updating "now playing / up next" message, and its
        # attached view, so we can edit it in place instead of posting new
        # messages every time the queue advances.
        self.panel_message: discord.Message | None = None
        self.panel_view: discord.ui.View | None = None


# ---------------------------------------------------------------------------
# URL / time parsing helpers
# ---------------------------------------------------------------------------

def parse_time_to_seconds(value: str) -> int:
    """
    Parses a YouTube-style timestamp into whole seconds. Supports:
        "90"        -> 90
        "43s"       -> 43
        "1m30s"     -> 90
        "1h2m3s"    -> 3723
        "1:30"      -> 90
        "01:02:03"  -> 3723
    Raises ValueError if the format isn't recognized.
    """
    value = value.strip()

    if re.fullmatch(r"\d+", value):
        return int(value)

    if ":" in value:
        parts = value.split(":")
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + int(part)
        return seconds

    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    if match and any(match.groups()):
        h, m, s = (int(g) if g else 0 for g in match.groups())
        return h * 3600 + m * 60 + s

    raise ValueError(f"Unrecognized time format: {value!r}")


def parse_youtube_link(url: str) -> dict:
    """
    Pulls the pieces we care about (video id, playlist id, start time) out of
    a YouTube URL and ignores everything else -- share-link tracking params
    like si=, feature=, pp=, index=, etc. are simply never looked at.

    Returns a dict:
        {"kind": "video" | "playlist" | "unknown",
         "video_id": str | None,
         "list_id": str | None,
         "start_seconds": int | None}
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    video_id = None
    list_id = qs.get("list", [None])[0]

    if "youtu.be" in parsed.netloc:
        video_id = parsed.path.lstrip("/").split("/")[0] or None
    elif "/shorts/" in parsed.path:
        video_id = parsed.path.rstrip("/").split("/")[-1]
    else:
        video_id = qs.get("v", [None])[0]

    start_seconds = None
    t_value = qs.get("t", [None])[0] or qs.get("start", [None])[0]
    if t_value:
        try:
            start_seconds = parse_time_to_seconds(t_value)
        except ValueError:
            logger.warning(f"Ignoring unrecognized time value: {t_value!r}")
            start_seconds = None

    if list_id:
        kind = "playlist"
    elif video_id:
        kind = "video"
    else:
        kind = "unknown"

    return {
        "kind": kind,
        "video_id": video_id,
        "list_id": list_id,
        "start_seconds": start_seconds,
    }


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

async def extract_stream_url(url: str) -> tuple[str, str]:
    """Resolves a single video URL to a direct streamable audio URL + title."""
    loop = asyncio.get_running_loop()

    def _extract():
        info = ytdl.extract_info(url, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return info["url"], info.get("title", "Unknown title")

    return await loop.run_in_executor(None, _extract)


async def extract_playlist_entries(list_id: str) -> list[dict]:
    """Returns [{'video_id': ..., 'title': ...}, ...] for a playlist, without
    resolving each video's actual stream URL (fast)."""
    loop = asyncio.get_running_loop()
    playlist_url = f"https://www.youtube.com/playlist?list={list_id}"

    def _extract():
        info = playlist_ytdl.extract_info(playlist_url, download=False)
        entries = []
        for entry in info.get("entries", []) or []:
            if not entry:
                continue
            entries.append({
                "video_id": entry.get("id"),
                "title": entry.get("title", "Unknown title"),
            })
        return entries

    return await loop.run_in_executor(None, _extract)


# ---------------------------------------------------------------------------
# The live "Now Playing" panel
# ---------------------------------------------------------------------------

class NowPlayingView(discord.ui.View):
    """A single Skip button attached to the live now-playing/up-next panel."""

    def __init__(self, cog: "MusicCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_skip_button(interaction, self.guild_id)


# ---------------------------------------------------------------------------
# The Cog
# ---------------------------------------------------------------------------

class MusicCog(commands.Cog, name="Music"):
    """Play music streamed from YouTube in a voice channel."""

    # Used by the base bot's !help embed to prefix this cog's section.
    # This is the only thing a cog needs beyond docstrings to show up nicely
    # in !help -- see COG_GUIDE.md.
    COG_EMOJI = "🎵"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildMusicState] = {}

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = GuildMusicState()
        return self.guild_states[guild_id]

    def build_panel_embed(self, guild: discord.Guild, state: GuildMusicState) -> discord.Embed:
        """Builds the "Now Playing / Up Next" embed reflecting current state."""
        voice_client = guild.voice_client
        channel_name = voice_client.channel.name if voice_client and voice_client.channel else "Not connected"

        embed = discord.Embed(title="🎵 Music Player", color=discord.Color.blurple())
        embed.add_field(name="Voice Channel", value=channel_name, inline=False)

        if state.current_track:
            title = truncate(state.current_track.get("title") or state.current_track.get("video_id"))
            start_seconds = state.current_track.get("start_seconds")
            suffix = f" (started at {start_seconds}s)" if start_seconds else ""
            embed.add_field(name="▶️ Now Playing", value=f"{title}{suffix}", inline=False)
        else:
            embed.add_field(name="▶️ Now Playing", value="Nothing is currently playing.", inline=False)

        if state.queue:
            upcoming = list(state.queue)
            listing = "\n".join(
                f"{i + 1}. {truncate(t.get('title') or t.get('video_id'))}"
                for i, t in enumerate(upcoming[:PANEL_QUEUE_PREVIEW_COUNT])
            )
            remainder = len(upcoming) - PANEL_QUEUE_PREVIEW_COUNT
            if remainder > 0:
                listing += f"\n...and {remainder} more (see `!queue`)"
            embed.add_field(name=f"📃 Up Next ({len(upcoming)})", value=listing, inline=False)
        else:
            embed.add_field(name="📃 Up Next", value="Nothing queued.", inline=False)

        return embed

    async def refresh_panel(self, guild: discord.Guild):
        """Edits the guild's live panel message in place, creating one if it
        doesn't exist yet (or if the old one was deleted)."""
        state = self.get_state(guild.id)
        if state.text_channel is None:
            return

        embed = self.build_panel_embed(guild, state)

        if state.panel_message is not None:
            try:
                await state.panel_message.edit(embed=embed, view=state.panel_view)
                return
            except discord.HTTPException:
                state.panel_message = None  # deleted or inaccessible -- fall through to resend

        view = NowPlayingView(self, guild.id)
        state.panel_view = view
        state.panel_message = await state.text_channel.send(embed=embed, view=view)

    async def handle_skip_button(self, interaction: discord.Interaction, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        voice_client = guild.voice_client if guild else None

        if voice_client is None or not voice_client.is_connected():
            await interaction.response.send_message("I'm not connected to a voice channel anymore.", ephemeral=True)
            return

        if not voice_client.is_playing() and not voice_client.is_paused():
            await interaction.response.send_message("Nothing is currently playing to skip.", ephemeral=True)
            return

        # Triggers the after_playback callback, which advances the queue and
        # refreshes the panel automatically.
        voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

    async def play_next(self, guild: discord.Guild, voice_client: discord.VoiceClient):
        """Pops the next track off this guild's queue and plays it. Chains
        itself via the `after=` callback so the queue plays through automatically."""
        state = self.get_state(guild.id)

        if state.stopping:
            return

        if not state.queue:
            state.current_track = None
            await self.refresh_panel(guild)
            return

        track = state.queue.popleft()
        video_id = track["video_id"]
        start_seconds = track.get("start_seconds")
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        state.current_track = track

        try:
            stream_url, resolved_title = await extract_stream_url(video_url)
        except Exception as e:
            logger.exception(f"Failed to resolve stream for {video_url}")
            if state.text_channel:
                label = track.get("title") or video_id
                await state.text_channel.send(f"⚠️ Skipping a track I couldn't load ({label}): {e}")
            state.current_track = None
            await self.play_next(guild, voice_client)
            return

        track["title"] = resolved_title  # fill in the title now that we know it

        before_options = FFMPEG_BEFORE_OPTIONS
        if start_seconds:
            before_options = f"-ss {start_seconds} {before_options}"

        source = discord.FFmpegPCMAudio(stream_url, before_options=before_options, options=FFMPEG_OPTIONS)

        def after_playback(error):
            if error:
                logger.error(f"Playback error: {error}")
            # This callback runs outside the event loop's thread, so hop back into it.
            fut = asyncio.run_coroutine_threadsafe(self.play_next(guild, voice_client), self.bot.loop)
            try:
                fut.result()
            except Exception:
                logger.exception("Error advancing queue")

        voice_client.play(source, after=after_playback)
        await self.refresh_panel(guild)

    @commands.command(name="play")
    async def play(self, ctx: commands.Context, url: str = None):
        """Play or queue a YouTube video or playlist.

        Accepts single video links (with an optional t=/start= timestamp) and
        playlist links. If something is already playing, this adds to the queue.
        """

        if url is None:
            await ctx.reply("Usage: `!play <youtube_url>`", mention_author=False)
            return

        if "http://" not in url and "https://" not in url:
            await ctx.reply("That doesn't look like a valid URL. Please provide a full YouTube link.", mention_author=False)
            return

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.reply("You need to be in a voice channel first.", mention_author=False)
            return

        voice_channel = ctx.author.voice.channel

        if ctx.voice_client is None:
            voice_client = await voice_channel.connect()
        elif ctx.voice_client.channel != voice_channel:
            await ctx.voice_client.move_to(voice_channel)
            voice_client = ctx.voice_client
        else:
            voice_client = ctx.voice_client

        state = self.get_state(ctx.guild.id)
        state.text_channel = ctx.channel
        state.stopping = False

        parsed = parse_youtube_link(url)

        if parsed["kind"] == "unknown":
            await ctx.reply("❌ I couldn't find a video or playlist in that link.", mention_author=False)
            return

        if parsed["kind"] == "playlist":
            await ctx.reply("🔎 Loading playlist...", mention_author=False)
            try:
                entries = await extract_playlist_entries(parsed["list_id"])
            except Exception as e:
                logger.exception("Failed to load playlist")
                await ctx.reply(f"❌ Couldn't load that playlist: {e}", mention_author=False)
                return

            entries = [e for e in entries if e.get("video_id")]
            if not entries:
                await ctx.reply("That playlist appears to be empty or private.", mention_author=False)
                return

            for entry in entries:
                state.queue.append({
                    "video_id": entry["video_id"],
                    "title": entry["title"],
                    "start_seconds": None,
                })

            await ctx.reply(f"📃 Queued **{len(entries)}** tracks from the playlist.", mention_author=False)

        else:  # single video
            state.queue.append({
                "video_id": parsed["video_id"],
                "title": None,
                "start_seconds": parsed["start_seconds"],
            })
            start_note = f" (starting at {parsed['start_seconds']}s)" if parsed["start_seconds"] else ""
            await ctx.reply(f"➕ Added to queue{start_note}.", mention_author=False)

        await self.refresh_panel(ctx.guild)

        if not voice_client.is_playing() and not voice_client.is_paused():
            await self.play_next(ctx.guild, voice_client)

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context):
        """Stop playback, clear the queue, and disconnect from the voice channel."""

        voice_client = ctx.voice_client

        if voice_client is None or not voice_client.is_connected():
            await ctx.reply("I'm not currently in a voice channel.", mention_author=False)
            return

        state = self.get_state(ctx.guild.id)
        remaining = len(state.queue)
        state.queue.clear()
        state.current_track = None
        state.stopping = True

        voice_client.stop()
        await voice_client.disconnect()

        state.stopping = False

        if state.panel_message is not None:
            try:
                stopped_embed = discord.Embed(title="🎵 Music Player", description="⏹️ Stopped.", color=discord.Color.red())
                await state.panel_message.edit(embed=stopped_embed, view=None)
            except discord.HTTPException:
                pass
            state.panel_message = None
            state.panel_view = None

        if remaining > 0:
            track_word = "track" if remaining == 1 else "tracks"
            await ctx.reply(f"⏹️ Stopped, cleared {remaining} queued {track_word}, and disconnected.", mention_author=False)
        else:
            await ctx.reply("⏹️ Stopped and disconnected.", mention_author=False)

    @commands.command(name="skip")
    async def skip(self, ctx: commands.Context):
        """Skip the current track and move to the next queued one."""

        voice_client = ctx.voice_client

        if voice_client is None or not voice_client.is_connected():
            await ctx.reply("I'm not currently connected to a voice channel.", mention_author=False)
            return

        if not voice_client.is_playing() and not voice_client.is_paused():
            await ctx.reply("Nothing is currently playing to skip.", mention_author=False)
            return

        # Stopping the current source triggers the after_playback callback,
        # which automatically advances to the next queued track (if any)
        # and refreshes the panel.
        voice_client.stop()
        await ctx.reply("⏭️ Skipped.", mention_author=False)

    @commands.command(name="queue")
    async def queue_cmd(self, ctx: commands.Context):
        """Post a one-off snapshot of what's playing and what's queued next."""

        voice_client = ctx.voice_client
        state = self.get_state(ctx.guild.id)

        if voice_client is None or not voice_client.is_connected():
            await ctx.reply("I'm not connected to a voice channel right now.", mention_author=False)
            return

        embed = self.build_panel_embed(ctx.guild, state)
        await ctx.reply(embed=embed, mention_author=False)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        logger.exception(f"Error in music command: {error}")
        await ctx.reply(f"⚠️ Something went wrong: {error}", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
