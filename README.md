# Discord Music Bot

A simple, self-hosted Discord bot that streams audio from a YouTube link into a
voice channel.

**Commands**

| Command          | What it does                                                        |
|-------------------|----------------------------------------------------------------------|
| `!play <url>`     | Joins your current voice channel and plays/queues audio from the link. Accepts single videos (with optional start time) and playlists.  |
| `!skip`           | Immediately skips to the next track in the queue.                     |
| `!queue`          | Shows the voice channel, what's currently playing, and what's up next.|
| `!stop`           | Stops playback, clears the queue, and leaves the voice channel.       |
| `!help`           | Lists all commands, grouped by feature module.                        |

**Bot responses:** the bot replies directly to the message that triggered
it, rather than posting a separate message. When you queue a single video
behind others, the confirmation includes a **"Skip to this"** button — click
it to jump the queue straight to that track (dropping everything queued
ahead of it). This isn't offered for playlist entries, since a 25-track
playlist would need 25 buttons, which Discord doesn't support on one message.

**Supported link features:**
- Timestamps: `?t=90`, `?t=43s`, `?t=1h2m3s`, or `?t=1:30` will start playback at that point in the video.
- Playlists: a `?list=...` link queues every video in the playlist, posts the track list once, and plays through it automatically.
- Extra parameters (share-link `si=`, `index=`, `feature=`, etc.) are safely ignored.
- If you `!play` while something is already playing, the new link is added to the queue rather than interrupting.

This bot runs on your own PC — there is nothing to deploy to a server, and
**no port forwarding or router configuration is required** (see the note in
Step 5 for why).

---

## What you'll need

- A Windows PC that can stay on while you want the bot available
- About 15–20 minutes for one-time setup
- A free Discord account

---

## Step 1 — Create a Discord Bot Application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and log in.
2. Click **New Application**, give it a name (e.g. "My Music Bot"), and create it.
3. In the left sidebar, click **Bot**.
4. Click **Reset Token** (or **Add Bot** if this is the first time), then **copy the token**.
   - Treat this token like a password. Anyone with it can control your bot.
   - You'll paste this into the `.env` file in Step 4.
5. On the same **Bot** page, scroll to **Privileged Gateway Intents** and turn ON:
   - **Message Content Intent** (required — this lets the bot read your `!play` and `!stop` commands)

---

## Step 2 — Invite the Bot to Your Server

1. In the left sidebar, click **OAuth2** → **URL Generator**.
2. Under **Scopes**, check:
   - `bot`
3. Under **Bot Permissions**, check:
   - `Send Messages`
   - `Connect`
   - `Speak`
   - `Read Message History` (optional, but helpful)
4. Copy the generated URL at the bottom of the page, paste it into your browser,
   and select the server you want to add the bot to.

---

## Step 3 — Install Required Software on Windows

### Python

1. Download **Python 3.12** from [python.org/downloads](https://www.python.org/downloads/).
   - Avoid Python 3.13 for now — it removed a module some voice libraries still depend on.
2. Run the installer.
   - **Important:** check the box **"Add python.exe to PATH"** on the first screen before clicking Install.
3. Verify it worked by opening **Command Prompt** and running:
   ```
   python --version
   ```
   You should see something like `Python 3.12.x`.

### FFmpeg

FFmpeg is what actually decodes/encodes the audio stream. This is the step
people most often get stuck on, so follow it closely.

1. Go to [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) and download the
   **"release essentials"** build (a `.7z` or `.zip` file).
2. Extract it somewhere permanent, e.g. `C:\ffmpeg`.
3. Inside, you'll find a `bin` folder (e.g. `C:\ffmpeg\bin`) containing `ffmpeg.exe`.
4. Add that `bin` folder to your Windows PATH:
   - Press **Windows key**, search for "environment variables", open **"Edit the system environment variables"**.
   - Click **Environment Variables**.
   - Under **System variables**, select **Path**, click **Edit**, click **New**, and paste in the path to your `bin` folder (e.g. `C:\ffmpeg\bin`).
   - Click OK on all windows.
5. **Restart Command Prompt** (PATH changes don't apply to already-open windows) and verify:
   ```
   ffmpeg -version
   ```
   If you see version info, you're set.

---

## Step 4 — Set Up the Bot Project

1. Extract/place the bot files somewhere, e.g. `C:\discord-music-bot`.
   You should have `main.py`, `music_cog.py`, `requirements.txt`, and `.env.example`
   all in the same folder.
2. Open Command Prompt in that folder (`cd C:\discord-music-bot`).
3. Install the required Python packages:
   ```
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to a new file named `.env` in the same folder.
5. Open `.env` in a text editor and paste in the bot token you copied in Step 1:
   ```
   DISCORD_BOT_TOKEN=your_actual_token_here
   ```
6. Save the file.

---

## Step 5 — Run the Bot

In Command Prompt, from the project folder:

```
python main.py
```

If everything is set up correctly, you'll see a log line like:

```
Logged in as My Bot#1234 (id: ...)
Bot is ready. Waiting for commands...
```

Leave this window open — the bot is only online while this process is running.
Closing the window (or shutting down your PC) takes the bot offline.

### A note on port forwarding

You don't need to configure any port forwarding, router settings, or a static
IP for this bot. Discord bots work by making **outbound** connections from
your PC to Discord's servers (the same way a web browser connects to
websites) — nothing needs to accept incoming connections. Port forwarding is
only needed if you were hosting something that other people connect to
directly (like a Minecraft server), which isn't the case here.

---

## Step 6 — Use It

In any text channel the bot can see, while you're in a voice channel:

```
!play https://www.youtube.com/watch?v=dQw4w9WgXcQ
!stop
```

The bot will join your current voice channel on `!play` and leave on `!stop`.

---

## Keeping the Bot Running

By default, the bot only runs while the Command Prompt window is open and
your PC is on. A few options if you want it running more persistently:

- **Simplest:** just leave the Command Prompt window open and your PC awake
  while you want the bot available. This is fine for personal/casual use.
- **Slightly more robust:** create a `.bat` file with `python main.py` in it
  and pin it somewhere convenient, so restarting the bot after a reboot is
  a double-click.
- **More advanced (optional):** run it as a background task using Windows
  Task Scheduler, or use a process manager like [NSSM](https://nssm.cc/) to
  run it as a Windows service. This is not necessary to get started — only
  worth doing if you want the bot to auto-restart on PC reboot.

---

## Troubleshooting

- **"DISCORD_BOT_TOKEN is not set" error** — Make sure your `.env` file is in
  the same folder as `main.py` and the variable name is spelled exactly
  `DISCORD_BOT_TOKEN`.
- **Bot doesn't respond to commands** — Double check "Message Content Intent"
  is enabled in the Developer Portal (Step 1.5), and that the bot has
  permission to read/send messages in the channel you're using.
- **Bot joins but no audio plays / errors mentioning ffmpeg** — FFmpeg isn't
  on your PATH. Re-check Step 3, and make sure you opened a *new* Command
  Prompt window after editing PATH.
- **"Couldn't load that video" errors** — YouTube occasionally changes things
  that break stream extraction. Try updating yt-dlp:
  ```
  pip install -U yt-dlp
  ```
- **Bot can't join voice / errors about "opus" or "PyNaCl"** — Run
  `pip install -U PyNaCl` and make sure `discord.py[voice]` (not plain
  `discord.py`) is installed.

---

## Notes on Scope

- The bot maintains a simple per-server queue: `!play`ing a video while
  something's already playing adds it to the end of the queue, and playlists
  queue every video in order. `!stop` clears the whole queue.
- It streams directly (no files saved to disk).

---

## Project Structure & Adding More Features

The bot is split into a central entry point and feature "extensions" (cogs),
so you can add more capabilities without touching the music code:

- **`main.py`** — creates the bot, loads extensions, and starts it. This is
  the file you run (`python main.py`). It has no feature logic of its own.
- **`music_cog.py`** — all the music/YouTube/queue logic, as a loadable
  extension (`await bot.load_extension("music_cog")`).

To add another module — for example a `voting_cog.py` — just:

1. Drop `voting_cog.py` in the same folder as `main.py`.
2. Make sure it defines an `async def setup(bot):` function that calls
   `await bot.add_cog(YourCogClass(bot))`, same as `music_cog.py` does.
3. Add its name to the `EXTENSIONS` list near the top of `main.py`:
   ```python
   EXTENSIONS = [
       "music_cog",
       "voting_cog",
   ]
   ```

No changes to `music_cog.py` are needed to add unrelated features this way.

For the full convention on how a cog's docstrings feed into `!help` (and
what a minimal cog looks like), see **`COG_GUIDE.md`** in this folder — it's
written to be handed to whoever's writing a new cog, including your existing
`voting_cog.py`.
- Only people with access to your server and the bot's token can control it;
  keep your token private.
