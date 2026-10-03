AUTOMATIC TIMESTAMPED LOGGING
=============================

Every Salsa Spotify Manager run now writes a timestamped .txt log into:

    logs\

while continuing to display the same output live in the Command Prompt window.
Both normal output and Python error/traceback output are captured. Interactive
input prompts continue to work normally.

Example:
    sync_2026-08-30_01-42-18.txt

Each run creates a new file; existing logs are never overwritten.

The Generic Date-Order Clone side utility uses its own:

    Side Utility - Generic Date-Order Clone\logs\

Logging is implemented inside Python rather than with command-line redirection,
so prompts such as playlist URLs, SYNC confirmation and Spotify Client ID remain
visible and interactive.
