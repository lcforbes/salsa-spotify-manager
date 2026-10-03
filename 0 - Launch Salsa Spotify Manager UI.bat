@echo off
setlocal
cd /d "%~dp0"

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 salsa_spotify_gui.py
    exit /b 0
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw salsa_spotify_gui.py
    exit /b 0
)

rem Fallback: use console Python if pythonw/pyw is unavailable.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 salsa_spotify_gui.py
) else (
    python salsa_spotify_gui.py
)
