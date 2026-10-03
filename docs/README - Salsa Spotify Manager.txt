SALSA SOCIAL – STRUCTURED: ORGANIC-GROWTH WINDOWS SPOTIFY MANAGER
=================================================================

CURRENT MODEL
-------------
• Permanent destination: the already-shared public Spotify playlist.
• Canonical source: salsa_social_master.csv.
• V2.0 baseline: 492 tracks.
• Track count is now VARIABLE; organic growth is allowed.
• 25-track social sets are ELASTIC programming blocks, not rigid containers.
• Curator decides Promote / Reserve / Reject. The code never promotes automatically.
• Preserve Sync remains the normal Spotify update method.
• Cache/checkpoint/quota-unlock protections remain in place.

RECOMMENDED INCOMING CYCLE
--------------------------
Spotify Incoming
  -> export Incoming CSV/Chosic
  -> 13 Prepare Incoming Review (no Spotify API)
  -> curator marks Promote / Reserve / Reject
  -> 14 Apply Approved Incoming Decisions (local master only)
  -> 1 Validate Master CSV
  -> 3 Preview from Cache
  -> 4 Preserve Sync In Place

PROMOTION DOES NOT REQUIRE REMOVAL
----------------------------------
If 10 strong tracks are promoted and nothing is removed, a 492-track master becomes
502 tracks. That is valid. Remove/rest existing songs only for a curatorial reason.

ELASTIC SETS
------------
Target: 25 tracks per internal social set.
Elastic working range: 23–27.
Outside that range: Review.

The manager reports set health and natural rebaseline points. It does not automatically
shuffle the whole playlist to force exact 25s. Example: 500 active tracks is a natural
opportunity to deliberately rebuild to 20 × 25.

LAUNCHERS
---------
1  Validate Master CSV - Variable Size & Elastic Sets
2  Adopt Existing Shared Playlist as Master
3  Preview from Cache - Zero API Calls
4  Apply Preserve Sync In Place - Quota Efficient
5  Emergency Fast Rebuild In Place - Resets Added At
6  Refresh and Verify Live Spotify State
7  Backup Current Master
8  Create New Public Master - Fallback Only
9  Chronology Build - Historical Dates Only - Quota Heavy
10 Show Saved Master Config
11 Show Saved Quota Unlock Status
12 Self Test
13 Prepare Incoming Review CSV - No Spotify
14 Apply Approved Incoming Decisions to Master CSV - No Spotify
15 Analyse Elastic Set Balance - No Spotify

SOURCE-OF-TRUTH RULE
--------------------
Do not make casual structural edits directly in Spotify. Update salsa_social_master.csv
(or use the Incoming review workflow), validate it, preview, then sync Spotify.

ADDED AT
--------
Historical pre-rebuild dates are unavailable and are not used for programming. Live
Spotify Added At remains useful going forward. Preserve Sync retains existing playlist
memberships wherever possible; genuinely new tracks receive their real new Added At.

QUOTA EFFICIENCY
----------------
• Cached playlist state + Spotify snapshot_id.
• Zero-call offline preview.
• Batched additions/removals up to 100.
• Contiguous-block reordering.
• Resume checkpoints after successful writes.
• Automatic QUOTA_EXCEEDED Retry-After detection and local safe-retry lock.
• Timestamped logs for every manager run.
REMOVE / REST EXISTING TRACKS
-----------------------------
Use:
  16 - Remove or Rest Tracks from Master - No Spotify.bat

This is the preferred way to curate tracks OUT of the canonical master without
manually editing salsa_social_master.csv. Search by current position, title, artist,
Spotify URI or ISRC, then choose:
• Remove — delete the occurrence completely from the canonical master.
• Rest — move it from Main Rotation to Reserve / Specialist at the end.

You can queue several actions, review the complete proposal, then type APPLY.
The manager automatically backs up the canonical CSV, renumbers New Position, updates
set slots, reports elastic-set health, and makes ZERO Spotify API calls. Afterwards run
Validate → Preview from Cache → Preserve Sync to publish the changes.

AUTOMATIC INCOMING PROGRAMMING
------------------------------
Incoming now has a simple meaning:

  If a genuinely new track is present in the Incoming CSV, it is to be PROMOTED.

There is no Reject category and no per-track curator decision after export.

Run:
  13 - Prepare Incoming Automatic Placement Plan - No Spotify.bat

The manager automatically chooses for every new track:
• Style
• Floor Role
• elastic Set
• exact insertion point in the master

How it decides:
• Style first uses the artist's established style in the current master when available;
  otherwise it uses Chosic genre metadata and recording-era fallbacks.
• Floor Role compares the track's measured audio features with the empirical V2 role
  profiles. New additions use expandable roles (Warm-up, Build, Lift, Peak, Recovery,
  Rebuild); structural Settle/Release/Closer anchors remain stable until a deliberate
  full rebaseline.
• Set selection favours elastic balance, missing role/style coverage, and artist spacing.
• Insertion minimises local pulse-BPM/intensity disruption while respecting the chosen
  Floor Role and avoiding nearby repetition of the same primary artist.

The generated incoming_auto_plan_*.csv is a REPORT/PLAN, not a worksheet requiring edits.

Then run:
  14 - Apply Latest Incoming Automatic Plan to Master - No Spotify.bat

It verifies the master has not changed since planning, replays the automatic placements,
backs up salsa_social_master.csv, and updates the canonical master locally. A single
APPLY confirmation protects against accidental execution; it is not a curation decision.

Already-present tracks and duplicate occurrences in the Incoming export are skipped.
A row with no usable Spotify URI/Track ID stops planning so it cannot be silently lost.

Neither command contacts Spotify. After Apply use:
  Validate -> Preview from Cache -> Preserve Sync
