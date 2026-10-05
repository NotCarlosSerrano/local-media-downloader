# local-media-downloader

Self-hosted web app to download video and audio from YouTube links. Runs entirely on your machine — no ads, no pop-ups, no third-party converter sites.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [ffmpeg](https://ffmpeg.org/) (bundled via `imageio-ffmpeg`, so there is nothing else to install).

## Features

- **Audio**: MP3, M4A or Opus, 128–320 kbps
- **Video**: MP4 from 360p up to 1080p / best available
- **Playlists**: download a whole playlist as a ZIP
- **Trim**: download only a section (e.g. `1:20` → `3:45`)
- **Preview**: title, channel, duration and thumbnail before downloading
- **Live progress**: percentage, speed, time remaining and current stage; cancel anytime
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

Then open <http://localhost:8000>.

## Other ways to use it

```bash
python downloader.py                       # small desktop window (tkinter)
python downloader.py URL [URL ...]         # command line, MP3 to ~/Music/YouTube MP3
python downloader.py URL -o out -q 320     # custom folder and bitrate
```

## How it works

```
browser ──/start──▶ server.py ──▶ yt-dlp + ffmpeg (background thread)
        ◀─/status─ (polled every 500 ms for progress)
        ◀─/file─── finished file, then temp files are deleted
```

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
