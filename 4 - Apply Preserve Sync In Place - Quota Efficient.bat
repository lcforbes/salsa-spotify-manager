@echo off
setlocal
cd /d "%~dp0"
title Salsa Social - Structured Manager
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 salsa_spotify_manager.py sync --mode preserve
) else (
  python salsa_spotify_manager.py sync --mode preserve
)
echo.
pause
