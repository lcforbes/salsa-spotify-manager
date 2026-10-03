# Salsa Spotify Manager

A Windows desktop application for curating a structured salsa playlist and syncing its canonical CSV order to a permanent Spotify playlist. The Tkinter UI wraps the existing command-line manager.

## Features

- Overview of the local master, elastic sets, cached sync and quota status.
- Incoming workflow with **Open Chosic Playlist Analyzer**, automatic placement planning and local application of the plan.
- Search and filter master tracks; remove tracks or rest them to Reserve.
- Guided Validate → Preview → Preserve Sync → Verify Live workflow.
- Maintenance tools, Spotify Client ID setup and activity output.
- Command-line recovery launchers, caching, checkpoints and quota retry protection.

## Setup

1. Install Python 3.10 or newer with Tkinter and the Windows Python launcher, or make `python` available on PATH. No third-party Python packages are required.
2. Clone or download this repository into a writable folder.
3. Copy your own canonical `salsa_social_master.csv` into that folder beside the Python files. Real playlist data is deliberately absent from this repository. Existing users should retain their private local CSV and configuration when updating the application.
4. Double-click **Run Salsa Manager.bat** to open the UI. **Run Salsa Manager - Command Line.bat** opens the console menu.
5. For Spotify operations, configure your own Spotify Developer application with redirect URI `http://127.0.0.1:8888/callback`, then enter its Client ID under **Maintenance**. Browser authorization is requested when a live operation needs it.
6. Validate your CSV, adopt the existing playlist if appropriate, preview the changes, and use Preserve Sync to apply them. Review the preview before changing Spotify.

The Incoming page opens Chosic in your browser. Export your Incoming playlist there, select the export in the UI, prepare its automatic placement plan, and apply the plan locally before validating and syncing. Inclusion of a new track in the Incoming export means promotion; the current workflow does not require per-track Promote/Reserve/Reject decisions.

## Repository layout

| Path | Purpose |
| --- | --- |
| `salsa_spotify_gui.py` | Desktop UI, including the Chosic button |
| `salsa_spotify_manager.py` | Command-line engine and Spotify operations |
| `*.bat` | GUI, console and numbered specialist launchers |
| `docs/` | Original application documentation and changelog |
| `UI - README.txt` | UI usage guide |

Launchers remain beside the Python files because they resolve the application from their own directory. Some original documentation describes earlier Incoming review workflows; the automatic workflow above and the current UI take precedence.

## Private local files

The repository excludes Client ID, master configuration, Spotify state/cache/quota files, logs, backups, master history, incoming reviews, CSV exports and live playlist data. Generated data stays local through `.gitignore`; do not force-add it. OAuth credentials and machine-specific shortcuts are also excluded. Empty runtime folders are created by the application when needed.

This source snapshot was copied from the installed UI-enabled generated package, including its Chosic update. It excludes the archive, side utility and all operational data; the installed application was left unchanged.

## Local verification

After adding your own canonical CSV, run **12 - Self Test.bat** or `python salsa_spotify_manager.py self-test`. This checks local algorithms and the CSV without Spotify authorization. Live sync requires your own Spotify account and configuration.
