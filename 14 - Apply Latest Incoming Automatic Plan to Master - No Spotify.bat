@echo off
setlocal
cd /d "%~dp0"
title Salsa Manager - Apply Incoming Automatic Plan
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 salsa_spotify_manager.py apply-incoming
) else (
  python salsa_spotify_manager.py apply-incoming
)
echo.
pause
