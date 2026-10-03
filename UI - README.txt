SALSA SPOTIFY MANAGER — DESKTOP UI
==================================

NORMAL START
------------
Double-click:

    Run Salsa Manager.bat

or:

    0 - Launch Salsa Spotify Manager UI.bat

The UI uses only Python's built-in Tkinter library. No extra Python packages are
required. The existing salsa_spotify_manager.py remains the playlist-management
engine underneath the UI.

The original numbered BAT commands remain available for troubleshooting and for
users who prefer the command line. The old interactive launcher is retained as:

    Run Salsa Manager - Command Line.bat


UI AREAS
--------
OVERVIEW
  Shows the adopted Spotify master, canonical track counts, cached sync state,
  quota status, and a recommended next action.

INCOMING
  • Use "Open Chosic Playlist Analyzer" to launch Chosic in your default browser.
  1. Analyze/export the "Salsa - Incoming" playlist from Chosic as CSV.
  2. Select that Chosic CSV export in the UI.
  3. Prepare the automatic placement plan.
  4. Review the generated assignments if desired.
  5. Apply the plan to salsa_social_master.csv.

  Inclusion in Incoming means Promote. The code automatically chooses Style,
  Floor Role, elastic Set, insertion position, Intensity V2 and Social Fit V2.

MASTER TRACKS
  Search/filter the canonical master and multi-select tracks.
  Queue either:
    • Remove completely
    • Rest to Reserve / Specialist

  Apply Queued Changes creates a timestamped master-history backup, updates the
  canonical CSV, renumbers positions and slots, and makes ZERO Spotify API calls.

SPOTIFY SYNC
  Presents the normal update path in the correct order:
    1. Validate local master
    2. Preview from cache
    3. Apply Preserve Sync
    4. Optional Refresh & Verify Live

  Spotify-changing actions use clear confirmation dialogs. The same quota-safe,
  checkpointed manager engine is used underneath.

MAINTENANCE
  Adopt/check the permanent master, set the Spotify Client ID, run Self Test and
  Elastic Set Analysis, inspect quota status, open backups/logs, and access the
  specialist Create New Master / Chronology Build operations.

ACTIVITY
  Shows live command output inside the application. CLI-engine commands continue
  to write their normal timestamped TXT logs to the logs folder.


SPOTIFY AUTHORIZATION
---------------------
Live Spotify commands open the Spotify authorization page in your normal browser.
After authorization, return to the UI. The browser callback remains:

    http://127.0.0.1:8888/callback

The Client ID can be entered or changed from Maintenance. It is not a secret.


SOURCE OF TRUTH
---------------
salsa_social_master.csv remains the canonical playlist source of truth.

The UI does not replace this design principle. Local Incoming, Remove and Rest
changes modify the canonical master first. The permanent Spotify playlist changes
only when you explicitly run Apply Preserve Sync.
