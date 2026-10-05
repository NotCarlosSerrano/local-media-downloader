@echo off
cd /d "%~dp0"
py -c "import yt_dlp, imageio_ffmpeg" 2>nul || py -m pip install -r requirements.txt
py server.py
