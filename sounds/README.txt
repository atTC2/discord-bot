Drop dramatic sound effects here for the voting cog to play.

Files are matched by name (any extension ffmpeg understands: mp3, wav,
ogg, m4a, flac...):

    voting_started.mp3    - played when !start_vote kicks off
    voting_finished.mp3   - played when a vote concludes with a result
    voting_cancelled.mp3  - played when !cancel_vote is used

If a file isn't present, that sting is just skipped silently - nothing
breaks. The bot connects to the voice channel being voted in, plays the
clip once, and disconnects again, but ONLY if it isn't already playing
something else in that server (e.g. music) - it will never interrupt or
talk over an existing stream.
