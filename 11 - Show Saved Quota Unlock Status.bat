@echo off
setlocal
cd /d "%~dp0"
title Spotify Quota Unlock Status
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 salsa_spotify_manager.py show-quota
) else (
  python salsa_spotify_manager.py show-quota
)
echo.
pause
