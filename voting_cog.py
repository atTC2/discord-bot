"""
voting_cog.py

A drop-in discord.py Cog that adds a nomination/voting feature set to an
existing bot, following the bot's cog conventions (see cog_guide.md):
name= on the Cog, docstrings for !help, COG_EMOJI, ctx.reply(...,
mention_author=False) for command responses, and cog_command_error.

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
    !end_vote     - once at least (n-1) of the n participants are ready
                    (voted or abstained), tallies points and announces a
                    winner. Errors if called too early. Ties automatically
                    trigger a tie-breaker round restricted to the tied
                    nominees.
    !cancel_vote  - closes an in-progress vote without printing results.

The live-updating message also carries three buttons doing the same things
as the three commands above, plus "Include Previous Runners-Up" (see below).

Voting (via DM to the bot):
    Reply with up to 3 lines, most preferred first, e.g.:
        1. Thing A
        2. Thing B
        3. Thing C
    Any leading number/letter followed by "." ")" "]" or "}" is stripped.
    Matching is case-insensitive. Replying with just "abstain" abstains.

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

!start_vote requires at least 3 people in the voice channel. With only 1 or
2, a vote doesn't add much over just talking it out.

    Clicking "Include Previous Runners-Up" on a live vote loads that file,
    posts its contents as a reply to the live-updating message, and, for
    this round only, adds each nominee's streak as bonus points on top of
    their normal score -- but only for nominees someone actually votes for
    this round. Nominees nobody votes for are left untouched either way.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import discord
from discord.ext import commands

# Matches a leading enumerator like "1.", "1)", "a.", "A)", "iii}" etc.
_ENUM_RE = re.compile(r"^\s*[0-9a-zA-Z]{1,3}\s*[.\)\]\}]\s*")

_WEIGHTS = [3, 2, 1]

_HISTORY_DIR = Path(__file__).resolve().parent / "data" / "vote_history"

SendFn = Callable[[str], Awaitable[None]]


def _strip_enumeration(line: str) -> str:
    return _ENUM_RE.sub("", line).strip()


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
    """Run ranked-choice votes among people in a voice channel. Needs 3+ people — smaller groups should just talk it out."""

    COG_EMOJI = "🗳️"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_votes: Dict[int, VoteSession] = {}
        self.history = HistoryStore(_HISTORY_DIR)

    # ---------- error handling ----------

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.reply(f"⚠️ Something went wrong: {error}", mention_author=False)

    # ---------- helpers ----------

    def _find_participant(self, user_id: int):
        for session in self.active_votes.values():
            if user_id in session.participants:
                return session, session.participants[user_id]
        return None, None

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
                icon = "➖" if p.status == "pending" else "✅"
                label = "no response" if p.status == "pending" else p.status
            else:
                icon = "✅" if p.status != "pending" else "❌"
                label = "not voted" if p.status == "pending" else p.status
            lines.append(f"{icon} {p.member.display_name} — {label}")
        embed.add_field(name="Status", value="\n".join(lines) or "No participants", inline=False)

        if not finished:
            ready = sum(1 for p in session.participants.values() if p.status != "pending")
            embed.set_footer(text=f"{ready}/{len(session.participants)} ready")

        return embed

    def _build_view(self, session: VoteSession) -> discord.ui.View:
        total = len(session.participants)
        ready = sum(1 for p in session.participants.values() if p.status != "pending")
        needed = max(total - 1, 1)
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
            disabled=ready < needed,
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
            options_list = "\n".join(f"- {opt}" for opt in session.runoff_options)
            return (
                f"🤝 Tie-breaker vote for **#{session.voice_channel.name}**!\n\n"
                f"Rank these in order of preference (top pick first):\n{options_list}\n\n"
                "One per line. Leading numbers/letters followed by a full stop or "
                "bracket (like \"1)\" or \"a.\") are ignored.\n\n"
                "Don't want to vote? Just reply with `abstain`."
            )
        return (
            f"🗳️ Vote for **#{session.voice_channel.name}**!\n\n"
            "Reply with up to 3 lines, your top pick first, e.g.:\n"
            "1. Thing A\n2. Thing B\n3. Thing C\n\n"
            "Leading numbers/letters followed by a full stop or bracket "
            "(like \"1)\" or \"a.\") are ignored, so plain lines work too.\n\n"
            "Don't want to vote this round? Just reply with `abstain`."
        )

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

    async def _start_runoff(self, session: VoteSession, tied_nominees: List[str], send_result: SendFn):
        await send_result(
            f"🤝 It's a tie between **{', '.join(tied_nominees)}**! Sending everyone a tie-breaker vote."
        )

        session.runoff_options = tied_nominees
        for p in session.participants.values():
            p.status = "pending"
            p.votes = []

        await self._update_tracking_message(session)

        failed_dms = []
        for p in session.participants.values():
            try:
                await p.member.send(self._format_prompt(session))
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

        total = len(session.participants)
        ready = sum(1 for p in session.participants.values() if p.status != "pending")
        needed = max(total - 1, 1)

        if ready < needed:
            return f"⚠️ Not enough people are ready yet ({ready}/{total} ready, need at least {needed})."

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

        lines = [f"**Winner: {winners[0]}** ({top_score} points)", "", "**Points:**"]
        for name, score in ranked:
            lines.append(f"- {name}: {score}")

        await send_result("\n".join(lines))
        await self._finish_session(guild, session)
        return None

    async def _perform_cancel_vote(self, guild: discord.Guild, send_notice: SendFn) -> Optional[str]:
        session = self.active_votes.get(guild.id)
        if session is None:
            return "There's no active vote in this server."

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

        async def send_result(text: str):
            await interaction.channel.send(text)

        error = await self._perform_end_vote(interaction.guild, send_result)
        if error:
            await interaction.followup.send(error, ephemeral=True)

    async def _on_cancel_vote_button(self, interaction: discord.Interaction, guild_id: int):
        await interaction.response.defer()

        async def send_notice(text: str):
            await interaction.channel.send(text)

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

        history = self.history.load(guild_id)
        session.include_history = True
        session.history_snapshot = history

        if history:
            ranked_history = sorted(history.items(), key=lambda kv: kv[1], reverse=True)
            lines = ["📜 **Runners-up history** (bonus only applies if voted for this round):"]
            lines.extend(f"- {name}: +{streak}" for name, streak in ranked_history)
            history_text = "\n".join(lines)
        else:
            history_text = "📜 No runners-up history yet for this server."

        try:
            await session.tracking_message.reply(history_text, mention_author=False)
        except discord.HTTPException:
            pass

        await self._update_tracking_message(session)
        await interaction.followup.send("Runners-up history included for this round.", ephemeral=True)

    # ---------- commands ----------

    @commands.command(name="start_vote")
    async def start_vote(self, ctx: commands.Context):
        """Start a new vote among everyone in your voice channel.

        Snapshots who's currently in the channel, then DMs each person asking
        for their ranked picks. Use !end_vote (or the button) once everyone's
        responded.

        Requires at least 3 people in the channel. With just 1 or 2 of you,
        skip the ceremony and talk it out directly.
        """
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

        if len(members) < 3:
            await ctx.reply(
                "Need at least 3 people in the voice channel to hold a vote — "
                "with only 1 or 2 of you, just talk it out instead!",
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

        tracking_message = await ctx.reply(
            embed=self._build_embed(session), view=self._build_view(session), mention_author=False
        )
        session.tracking_message = tracking_message

        failed_dms = []
        for participant in participants.values():
            try:
                await participant.member.send(self._format_prompt(session))
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

        Requires at least (participants - 1) people to have voted or
        abstained. Ties automatically trigger a tie-breaker round instead of
        ending the vote.
        """
        error = await self._perform_end_vote(ctx.guild, lambda text: ctx.reply(text, mention_author=False))
        if error:
            await ctx.reply(error, mention_author=False)

    @commands.command(name="cancel_vote")
    async def cancel_vote(self, ctx: commands.Context):
        """Cancel the current vote with no winner."""
        error = await self._perform_cancel_vote(ctx.guild, lambda text: ctx.reply(text, mention_author=False))
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

        if content.lower() == "abstain":
            participant.status = "abstained"
            participant.votes = []
            await message.channel.send("You've abstained from this vote.")
            await self._update_tracking_message(session)
            return

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
