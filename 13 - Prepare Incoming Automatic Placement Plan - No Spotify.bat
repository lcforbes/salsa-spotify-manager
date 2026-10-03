@echo off
setlocal
cd /d "%~dp0"
title Salsa Manager - Prepare Incoming Automatic Placement Plan
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 salsa_spotify_manager.py prepare-incoming
) else (
  python salsa_spotify_manager.py prepare-incoming
)
echo.
pause
