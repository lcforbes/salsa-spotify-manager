@echo off
setlocal
cd /d "%~dp0"
title Salsa Spotify Manager - Remove or Rest Tracks
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 salsa_spotify_manager.py remove-rest
) else (
  python salsa_spotify_manager.py remove-rest
)
echo.
pause
