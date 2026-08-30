# Writing Cogs for This Bot

This bot is built as a small central script (`main.py`) plus a set of
loadable extensions ("cogs") — `music_cog.py` is one, and your `voting_cog.py`
is another. This doc covers two things any cog author needs to know:

1. How to structure a cog so `main.py` can load it.
2. How to make it show up nicely in `!help` — for free, with no extra code.

---

## 1. The Loading Contract

Every cog module needs exactly one thing: an `async def setup(bot):` function
that registers the cog.

```python
from discord.ext import commands

class MyCog(commands.Cog, name="My Feature"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context):
        await ctx.reply("pong", mention_author=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(MyCog(bot))
```

Then in `main.py`, add the module's filename (without `.py`) to `EXTENSIONS`:

```python
EXTENSIONS = [
    "music_cog",
    "voting_cog",
]
```

`main.py` calls `await bot.load_extension(name)` for each entry at startup,
which imports the module and calls its `setup(bot)`. That's the entire
integration point — cogs don't need to touch `main.py` beyond this one line,
and `main.py` doesn't need to know anything about what's inside a cog.

---

## 2. Getting a Good `!help` Entry, For Free

This bot replaces discord.py's default `!help` with a custom one
(`help_command.py`, `PrettyHelpCommand`) that builds its embed purely from
things you're probably writing anyway: docstrings.

You don't need to register anything or write any help text in a special
format. Three optional pieces of information, all standard discord.py:

### a) A docstring on your Cog class

This becomes the category description shown at the top of your cog's section.

```python
class VotingCog(commands.Cog, name="Voting"):
    """Run ranked-choice votes among people in a voice channel."""
```

If you omit it, the section just won't have a description line — everything
still works.

### b) A docstring on each `@commands.command()`

The **first line** is shown next to the command name in the master `!help`
list. The **whole docstring** is shown when someone runs `!help <command>`.

```python
@commands.command(name="start_vote")
async def start_vote(self, ctx: commands.Context):
    """Start a new vote among everyone in your voice channel.

    Snapshots who's currently in the channel, then DMs each person asking
    for their ranked picks. Use !end_vote once everyone's responded.
    """
```

Keep the first line short (a few words to one sentence) since it's shown
inline in the command list. Put any extra detail on the following lines —
it'll only show up in the focused `!help <command>` view.

If a command has no docstring, it'll show up as "No description provided." —
harmless, but worth avoiding.

### c) An optional `COG_EMOJI` class attribute

Purely cosmetic — a leading emoji on your cog's section header.

```python
class VotingCog(commands.Cog, name="Voting"):
    """Run ranked-choice votes among people in a voice channel."""
    COG_EMOJI = "🗳️"
```

If you skip this, your section just gets a generic 🔧 instead.

---

## That's the Whole Convention

To recap, a fully "help-friendly" cog looks like:

```python
from discord.ext import commands

class VotingCog(commands.Cog, name="Voting"):
    """Run ranked-choice votes among people in a voice channel."""
    COG_EMOJI = "🗳️"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="start_vote")
    async def start_vote(self, ctx: commands.Context):
        """Start a new vote among everyone in your voice channel."""
        ...

    @commands.command(name="end_vote")
    async def end_vote(self, ctx: commands.Context):
        """Tally votes and announce the winner."""
        ...

    @commands.command(name="cancel_vote")
    async def cancel_vote(self, ctx: commands.Context):
        """Cancel the current vote with no winner."""
        ...

async def setup(bot: commands.Bot):
    await bot.add_cog(VotingCog(bot))
```

No changes to `main.py` or `help_command.py` are ever needed to support a new
cog — just write normal docstrings, optionally add `COG_EMOJI`, and add the
module name to `EXTENSIONS`.

---

## A Couple of Other Conventions Worth Following (Not Required, But Consistent)

These aren't enforced by anything — `music_cog.py` just follows them, and
matching them keeps the bot's behavior consistent across features:

- **Reply, don't send.** Use `await ctx.reply(content, mention_author=False)`
  instead of `ctx.send(...)` for command responses, so replies visibly thread
  back to the command that triggered them. `mention_author=False` keeps it
  from pinging people unnecessarily.
- **Cog-level error handling.** Define `async def cog_command_error(self, ctx, error):`
  on your cog to catch and report errors from any command in it, rather than
  attaching a separate `@command.error` handler to every command.
