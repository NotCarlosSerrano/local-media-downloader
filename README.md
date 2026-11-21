# local-media-downloader

Self-hosted web app to download video and audio from YouTube links. Runs entirely on your machine — no ads, no pop-ups, no third-party converter sites.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [ffmpeg](https://ffmpeg.org/) (bundled via `imageio-ffmpeg`, so there is nothing else to install).

## Features

- **Audio**: MP3, M4A or Opus, 128–320 kbps
- **Video**: MP4 from 360p up to 1080p / best available
- **Playlists**: download a whole playlist as a ZIP (no item limit)
- **Queue**: paste several links or playlists, tick the videos you want and set a start/end for each; they download one after another, straight into your Downloads folder, even if you close the tab (pending items survive a server restart)
- **Trim**: download only a section (e.g. `1:20` → `3:45`)
- **Preview**: title, channel, duration and thumbnail before downloading
- **Live progress**: percentage, speed and time remaining for both download and conversion; cancel anytime
- **Watchdog**: a stalled conversion is stopped with a clear error instead of hanging
- **Local only**: the server listens on `127.0.0.1`; nothing is exposed to your network

## Requirements

- Python 3.10+
- Windows, macOS or Linux

## Quick start

**Windows** — double-click `run.bat`. It installs the dependencies the first time and opens the app in your browser.

**Any OS:**

```bash
pip install -r requirements.txt
python server.py
```

Then open <http://localhost:8000>. Use `--port 9000` (or `PORT=9000`) to change the port, `--out <folder>` (or `OUT_DIR`) to change where the queue saves files, and `--no-browser` to skip opening the browser.

### GitHub Codespaces (when you are away from your computer)

On github.com open the repo → **Code** → **Codespaces** → **Create codespace**. The app installs and starts by itself; open the **Ports** tab and click the link for port 8000 (the port is private: only your GitHub account can open it). Use the normal download button so the file goes to your device — queue downloads are saved inside the codespace in `descargas/`. Stop the codespace when you are done to save free hours.

**"Sign in to confirm you're not a bot"**: YouTube blocks most cloud IPs, so the app needs YouTube cookies. Two options:

- **Log in inside the codespace (works from a phone, including iPhone):** open the **Ports** tab, open port **6080** and click *Connect* (password: `vscode`). In a new VS Code terminal run `DISPLAY=:1 firefox-esr https://www.youtube.com &`, log in to YouTube in that Firefox (ideally with a secondary Google account), then **close Firefox** so the cookies are saved to disk. The app reads them automatically (`COOKIES_FROM_BROWSER=firefox`).
- **Upload a `cookies.txt`** (Netscape format, e.g. exported with the *Get cookies.txt LOCALLY* extension) to the codespace's root folder, next to `server.py`. It takes priority over the browser cookies and `.gitignore` keeps it out of git. Another path can be set with the `COOKIES` environment variable.

Delete the codespace when you no longer need it: it holds your logged-in session.

## How it works

```
browser ──/info───▶ server.py   preview (no download)
        ──/start──▶ server.py ──▶ yt-dlp downloads, ffmpeg converts (background thread)
        ◀─/status─  polled every 500 ms for progress
        ──/cancel─▶ stops the job and deletes temp files
        ◀─/file───  finished file, then temp files are deleted
```

- `server.py` — local web server and job queue
- `downloader.py` — download engine (yt-dlp + ffmpeg)
- `index.html` — the web interface
- `run.bat` — Windows launcher

Unclaimed jobs and their temp files expire after 15 minutes.

## Troubleshooting

YouTube changes often. If downloads stop working, update yt-dlp first:

```bash
pip install -U yt-dlp
```

## Legal

This tool is for downloading content you have the right to download — your own uploads, Creative Commons or public-domain material, or anything the rights holder allows. Downloading copyrighted content may violate YouTube's Terms of Service and the law in your country. You are responsible for how you use it.

## License

[MIT](LICENSE)
