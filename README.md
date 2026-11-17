# Deep Cuts

On-demand narrative podcast episodes, written by Claude and narrated with Gemini TTS.

- Feed: https://casacowanpodbot.github.io/deep-cuts/feed.xml
- Add an episode: commit `episodes/YYYY-MM-DD-slug.txt` (see `scripts/build.py` for the format). The GitHub Action renders new audio, saves it to `audio/`, and republishes the feed.
- Settings (voice, style, models): `podcast.json`. Requires the `GEMINI_API_KEY` repository secret and Pages source set to "GitHub Actions".
- To re-render an episode, delete its file in `audio/` and push.
