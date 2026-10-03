@echo off
setlocal
cd /d "%~dp0"
title Validate Salsa Master CSV
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 salsa_spotify_manager.py validate
) else (
  python salsa_spotify_manager.py validate
)
echo.
pause
