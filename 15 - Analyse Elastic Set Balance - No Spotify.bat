@echo off
setlocal
cd /d "%~dp0"
title Analyse Elastic Salsa Sets
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 salsa_spotify_manager.py set-health
) else (
  python salsa_spotify_manager.py set-health
)
echo.
pause
