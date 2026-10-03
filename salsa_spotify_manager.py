#!/usr/bin/env python3
"""Salsa Social – Structured: Windows Spotify playlist manager.

Current operating model
-----------------------
* Canonical source: salsa_social_master.csv. V2.0 starts at 492 tracks, but the
  master is allowed to grow organically; there is no permanent track-count ceiling.
* Published destination: one permanent PUBLIC Spotify playlist.
* RECOMMENDED initial build: adopt an EXISTING shared/public Spotify playlist,
  keep its playlist ID/share URL, back it up, then convert it IN PLACE to the
  exact canonical Custom order.
* Fallback initial build: create a brand-new PUBLIC master only when there is
  no existing shared playlist worth preserving.
* Historical Added At chronology is NOT available in the current salsa exports.
  A separate chronology build is supported only if a genuinely varied historical
  export is supplied later.
* Maintenance: sync the SAME playlist ID in place to preserve its public share
  link. Existing tracks are reordered rather than removed/re-added wherever
  possible, preserving their existing Spotify Added At history as far as the
  API permits. New tracks naturally receive a new Added At timestamp.
* Quota-efficient state caching: one live snapshot check can reuse a cached
  full-playlist state instead of repaging the playlist. Successful mutations save a
  checkpoint so a quota-interrupted sync can resume later.
* New additions are batched (up to 100 per request) by default. Reordering moves
  the largest matching contiguous blocks it can find instead of one track per call.
* Incoming is now curator-light: inclusion in the Incoming CSV means PROMOTE.
  The manager automatically chooses Style, Floor Role, elastic Set and insertion
  position from the existing V2 programming model. A two-stage local Plan -> Apply
  workflow remains for safety, but no per-track promotion decisions are required.
* Local curation still includes a dedicated Remove / Rest command for taking tracks
  back out of the active flow when desired.

No third-party Python packages are required.
"""
from __future__ import annotations

import argparse
import atexit
import base64
import bisect
import collections
import csv
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

PLAYLIST_NAME = "Salsa Social – Structured"
CSV_FILENAME = "salsa_social_master.csv"
V2_BASELINE_TRACKS = 492
ELASTIC_SET_TARGET = 25
ELASTIC_SET_MIN = 23
ELASTIC_SET_MAX = 27
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-read-private playlist-modify-public playlist-modify-private"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
DEFAULT_DELAY = 1.05
APP_DIR = Path(__file__).resolve().parent
CLIENT_ID_FILE = APP_DIR / "spotify_client_id.txt"
CONFIG_FILE = APP_DIR / "salsa_master_config.json"
BACKUP_DIR = APP_DIR / "backups"
STATE_CACHE_FILE = APP_DIR / "salsa_master_state_cache.json"
INCOMING_DIR = APP_DIR / "incoming_reviews"
MASTER_HISTORY_DIR = APP_DIR / "master_history"
QUOTA_STATUS_FILE = APP_DIR / "spotify_quota_status.json"
API_CALLS = collections.Counter()
LOG_DIR = APP_DIR / "logs"
_LOG_FILE = None
_LOG_STARTED = False
_ORIG_STDOUT = None
_ORIG_STDERR = None

class _TeeStream:
    def __init__(self, console, logfile):
        self.console=console; self.logfile=logfile
    def write(self,data):
        self.console.write(data); self.logfile.write(data); return len(data)
    def flush(self):
        self.console.flush(); self.logfile.flush()
    def isatty(self):
        try: return self.console.isatty()
        except Exception: return False
    @property
    def encoding(self):
        return getattr(self.console,"encoding","utf-8")

def start_session_log():
    global _LOG_FILE,_LOG_STARTED,_ORIG_STDOUT,_ORIG_STDERR
    if _LOG_STARTED: return None
    _LOG_STARTED=True
    LOG_DIR.mkdir(parents=True,exist_ok=True)
    label=sys.argv[1] if len(sys.argv)>1 and not sys.argv[1].startswith("-") else "interactive-manager"
    label=re.sub(r"[^A-Za-z0-9._-]+","_",label).strip("_") or "interactive-manager"
    stamp=datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path=LOG_DIR/f"{label}_{stamp}.txt"
    _LOG_FILE=log_path.open("w",encoding="utf-8",buffering=1)
    _ORIG_STDOUT=sys.stdout; _ORIG_STDERR=sys.stderr
    sys.stdout=_TeeStream(_ORIG_STDOUT,_LOG_FILE)
    sys.stderr=_TeeStream(_ORIG_STDERR,_LOG_FILE)
    def _finish():
        global _LOG_FILE
        try:
            sys.stdout=_ORIG_STDOUT; sys.stderr=_ORIG_STDERR
            if _LOG_FILE and not _LOG_FILE.closed:
                _LOG_FILE.flush(); _LOG_FILE.close()
        except Exception:
            pass
    atexit.register(_finish)
    print(f"Session log: {log_path}")
    return log_path



def banner():
    print("=" * 78)
    print(" SALSA SOCIAL – STRUCTURED | Spotify Playlist Manager")
    print("=" * 78)
    print("Permanent public master • in-place sync • optional chronology recovery")
    print()


def parse_datetime(value: str):
    if not value:
        return (0, datetime.min.replace(tzinfo=timezone.utc))
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (1, dt.astimezone(timezone.utc))
    except ValueError:
        return (1, datetime.max.replace(tzinfo=timezone.utc))


def _is_main_rotation(row):
    section=(row.get("Section") or "").strip().lower()
    return section in ("main rotation","main","active","active rotation")


def _set_number(row):
    raw=(row.get("Set") or "").strip()
    try:
        return int(float(raw))
    except Exception:
        return None


def analyze_elastic_sets(rows, verbose=True):
    """Assess internal social-set health without changing any data.

    Exact 25-track sets are the design target, not a hard constraint. 23–27 is
    treated as an acceptable elastic range during organic-growth maintenance.
    """
    main=[r for r in rows if _is_main_rotation(r)]
    reserve=[r for r in rows if not _is_main_rotation(r)]
    by_set=collections.OrderedDict()
    unassigned=[]
    for r in main:
        n=_set_number(r)
        if n is None:
            unassigned.append(r)
        else:
            by_set.setdefault(n,[]).append(r)

    details=[]
    for n,items in by_set.items():
        c=len(items)
        if c==ELASTIC_SET_TARGET:
            status="Balanced (25)"
        elif ELASTIC_SET_MIN <= c <= ELASTIC_SET_MAX:
            status="Elastic OK"
        else:
            status="Review"
        details.append({"set":n,"count":c,"status":status})

    active=len(main)
    exact_sets=(active // ELASTIC_SET_TARGET) if active % ELASTIC_SET_TARGET==0 else None
    next_multiple=((active + ELASTIC_SET_TARGET - 1)//ELASTIC_SET_TARGET)*ELASTIC_SET_TARGET if active else ELASTIC_SET_TARGET
    to_next=next_multiple-active
    current_set_count=len(by_set)
    review_sets=[d for d in details if d["status"]=="Review"]

    if verbose:
        print("ELASTIC SET HEALTH")
        print("------------------")
        print(f"Main Rotation tracks:      {active}")
        print(f"Reserve / other tracks:    {len(reserve)}")
        print(f"Programmed main sets:      {current_set_count}")
        print(f"Elastic range per set:     {ELASTIC_SET_MIN}–{ELASTIC_SET_MAX} (target {ELASTIC_SET_TARGET})")
        if details:
            print("\nSet sizes:")
            for d in details:
                print(f"  Set {d['set']:>2}: {d['count']:>2} tracks  — {d['status']}")
        if unassigned:
            print(f"\nWARNING: {len(unassigned)} active track(s) have no numeric Set assignment.")
        if exact_sets is not None and exact_sets>0:
            if exact_sets != current_set_count or review_sets:
                print(f"\nREBASELINE OPPORTUNITY: {active} active tracks = {exact_sets} × 25 exactly.")
                print("A deliberate musical rebalance can restore exact 25-track sets.")
            else:
                print(f"\nExact architecture currently possible: {exact_sets} × 25 active tracks.")
        elif active:
            print(f"\nNext clean 25-track rebaseline point: {next_multiple} active tracks")
            print(f"Net active additions needed to reach it: {to_next}")
        if review_sets:
            print("\nSet(s) outside the elastic 23–27 range should be reviewed, but are not automatically changed.")
        print("\nRule: preserve musical flow continuously; restore exact 25-track symmetry periodically.")

    return {
        "active":active,"reserve":len(reserve),"set_count":current_set_count,
        "details":details,"unassigned":len(unassigned),"review_sets":review_sets,
        "exact_sets":exact_sets,"next_multiple":next_multiple,"to_next":to_next,
    }


def load_target_csv(path: Path | None = None):
    path = path or (APP_DIR / CSV_FILENAME)
    if not path.exists():
        # One-time convenience for older extracted packages.
        legacy=APP_DIR / "salsa_social_v2_full_492.csv"
        if path == APP_DIR / CSV_FILENAME and legacy.exists():
            raise SystemExit(
                f"Canonical master not found: {path.name}. This revised package includes it by default. "
                f"If you migrated files manually, copy/rename {legacy.name} to {CSV_FILENAME}."
            )
        raise SystemExit(f"Canonical CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("Canonical CSV is empty.")
    required = {"New Position", "Song", "Artist", "Spotify URI"}
    missing = required.difference(rows[0].keys())
    if missing:
        raise SystemExit("CSV is missing required columns: " + ", ".join(sorted(missing)))
    positions=[]
    for i,r in enumerate(rows, start=1):
        try:
            pos=int((r.get("New Position") or "").strip())
        except Exception:
            raise SystemExit(f"Invalid New Position on CSV data row {i}.")
        positions.append(pos)
        uri=(r.get("Spotify URI") or "").strip()
        if not uri.startswith("spotify:track:"):
            raise SystemExit(f"Invalid Spotify URI at target position {pos}: {r.get('Song')} — {r.get('Artist')}")
        r["_pos"] = pos
        r["_uri"] = uri
    expected=list(range(1,len(rows)+1))
    if positions != expected:
        raise SystemExit(f"New Position must be consecutive 1–{len(rows)} in exact file order.")
    return rows


def validate_target(rows):
    counts = collections.Counter(r["_uri"] for r in rows)
    dup_occ = sum(c-1 for c in counts.values() if c > 1)
    growth=len(rows)-V2_BASELINE_TRACKS
    print("Canonical master CSV validation passed.")
    print(f"Current target tracks:      {len(rows)}")
    print(f"V2.0 baseline:              {V2_BASELINE_TRACKS}")
    print(f"Net growth vs V2.0:         {growth:+d}")
    print(f"Repeated URI occurrences:   {dup_occ}")
    print(f"First Custom position:      {rows[0]['Song']} — {rows[0]['Artist']}")
    print(f"Last Custom position:       {rows[-1]['Song']} — {rows[-1]['Artist']}")
    print("Historical Added At order:  unavailable in the original salsa source exports")
    print("Master-size policy:         organic growth allowed; no fixed ceiling")
    print()
    analyze_elastic_sets(rows,verbose=True)
    return True


def get_client_id():
    if CLIENT_ID_FILE.exists():
        cid=CLIENT_ID_FILE.read_text(encoding="utf-8").strip()
        if cid:
            return cid
    print("First-run Spotify setup")
    print("Paste the Client ID from your Spotify Developer Dashboard app.")
    print(f"Configure this Redirect URI exactly: {REDIRECT_URI}")
    cid=input("Spotify Client ID: ").strip()
    if not cid:
        raise SystemExit("No Client ID supplied.")
    CLIENT_ID_FILE.write_text(cid+"\n", encoding="utf-8")
    print(f"Saved Client ID to {CLIENT_ID_FILE.name} (not a secret).")
    return cid


def b64url(data: bytes):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class Callback(http.server.BaseHTTPRequestHandler):
    result=None
    def do_GET(self):
        parsed=urllib.parse.urlparse(self.path)
        qs=urllib.parse.parse_qs(parsed.query)
        type(self).result={
            "code": qs.get("code", [None])[0],
            "state": qs.get("state", [None])[0],
            "error": qs.get("error", [None])[0],
        }
        body=("<html><body style='font-family:Segoe UI,Arial;padding:30px'>"
              "<h2>Spotify authorization received.</h2>"
              "<p>You can close this tab and return to the Salsa manager.</p></body></html>").encode("utf-8")
        self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args):
        pass


def authorize(client_id: str):
    verifier=b64url(secrets.token_bytes(64))
    challenge=b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state=secrets.token_urlsafe(24)
    params={
        "client_id":client_id,"response_type":"code","redirect_uri":REDIRECT_URI,
        "scope":SCOPES,"state":state,"code_challenge_method":"S256","code_challenge":challenge,
    }
    url=AUTH_URL+"?"+urllib.parse.urlencode(params)
    try:
        server=socketserver.TCPServer(("127.0.0.1",8888), Callback)
    except OSError as e:
        raise SystemExit(f"Cannot listen on 127.0.0.1:8888. Close anything using that port.\n{e}")
    print("Opening Spotify authorization in your browser...")
    print("If necessary, open this URL manually:\n"+url+"\n")
    webbrowser.open(url)
    with server:
        server.handle_request()
    result=Callback.result or {}
    if result.get("error"):
        raise SystemExit("Spotify authorization failed: "+result["error"])
    if result.get("state") != state:
        raise SystemExit("OAuth state mismatch. Aborting.")
    code=result.get("code")
    if not code:
        raise SystemExit("No authorization code received.")
    body=urllib.parse.urlencode({
        "client_id":client_id,"grant_type":"authorization_code","code":code,
        "redirect_uri":REDIRECT_URI,"code_verifier":verifier,
    }).encode("utf-8")
    req=urllib.request.Request(TOKEN_URL,data=body,method="POST",
        headers={"Content-Type":"application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req,timeout=30) as resp:
            data=json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Token exchange failed ({e.code}): {e.read().decode('utf-8','replace')}")
    token=data.get("access_token")
    if not token:
        raise SystemExit("Spotify returned no access token.")
    return token



def _last_sunday(year: int, month: int) -> datetime:
    if month == 12:
        first_next = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        first_next = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    d = first_next - timedelta(days=1)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d


def _uk_offset_for_utc(dt_utc: datetime) -> timedelta:
    """Return UK offset without requiring Windows tzdata/zoneinfo."""
    dt_utc = dt_utc.astimezone(timezone.utc)
    march = _last_sunday(dt_utc.year, 3).replace(hour=1)
    october = _last_sunday(dt_utc.year, 10).replace(hour=1)
    return timedelta(hours=1) if march <= dt_utc < october else timedelta(0)


def _format_uk(dt_utc: datetime) -> str:
    dt_utc = dt_utc.astimezone(timezone.utc)
    offset = _uk_offset_for_utc(dt_utc)
    label = "BST" if offset == timedelta(hours=1) else "GMT"
    local = dt_utc.astimezone(timezone(offset))
    return local.strftime("%A %d %B %Y at %H:%M:%S") + f" {label}"


def _parse_spotify_date(headers) -> datetime:
    raw = headers.get("Date") if headers else None
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _quota_unlock_details(headers):
    retry_raw = headers.get("Retry-After") if headers else None
    server_time = _parse_spotify_date(headers)
    if retry_raw is None:
        return None

    # Spotify normally supplies delta-seconds. Also accept an HTTP date defensively.
    try:
        seconds = max(float(retry_raw), 0.0)
        unlock = server_time + timedelta(seconds=seconds)
    except (TypeError, ValueError):
        try:
            unlock = parsedate_to_datetime(retry_raw)
            if unlock.tzinfo is None:
                unlock = unlock.replace(tzinfo=timezone.utc)
            unlock = unlock.astimezone(timezone.utc)
            seconds = max((unlock - server_time).total_seconds(), 0.0)
        except Exception:
            return None

    safe = unlock + timedelta(seconds=5)
    return {
        "retry_after_raw": str(retry_raw),
        "retry_after_seconds": seconds,
        "spotify_server_utc": server_time.isoformat(),
        "unlock_utc": unlock.isoformat(),
        "safe_retry_utc": safe.isoformat(),
        "spotify_server_uk": _format_uk(server_time),
        "unlock_uk": _format_uk(unlock),
        "safe_retry_uk": _format_uk(safe),
    }


def _save_quota_status(details, reason="QUOTA_EXCEEDED"):
    if not details:
        return
    payload = {
        "reason": reason,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    QUOTA_STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _clear_quota_status():
    try:
        if QUOTA_STATUS_FILE.exists():
            QUOTA_STATUS_FILE.unlink()
    except OSError:
        pass


def check_saved_quota_lock():
    """Stop locally before OAuth/API work while a known quota lock is still active."""
    if not QUOTA_STATUS_FILE.exists():
        return
    try:
        data = json.loads(QUOTA_STATUS_FILE.read_text(encoding="utf-8"))
        raw = data.get("safe_retry_utc")
        if not raw:
            return
        safe = datetime.fromisoformat(raw)
        if safe.tzinfo is None:
            safe = safe.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now < safe:
            remaining = int((safe - now).total_seconds() + 0.999)
            raise SystemExit(
                "Spotify Development Mode quota is still locked according to the last "
                "QUOTA_EXCEEDED response.\n"
                f"Safe retry time: {data.get('safe_retry_uk') or _format_uk(safe)}\n"
                f"About {remaining} seconds remain.\n\n"
                "No Spotify Web API request was made by this run. "
                f"Saved status: {QUOTA_STATUS_FILE.name}"
            )
    except SystemExit:
        raise
    except Exception:
        # A stale/corrupt helper file must never make the manager unusable.
        return

def spotify_request(token, method, path_or_url, body=None, max_retries=8):
    url=path_or_url if path_or_url.startswith("http") else API_BASE+path_or_url
    data=None
    headers={"Authorization":f"Bearer {token}","Accept":"application/json"}
    if body is not None:
        data=json.dumps(body).encode("utf-8"); headers["Content-Type"]="application/json"
    for attempt in range(max_retries+1):
        API_CALLS["total"] += 1
        API_CALLS[method.upper()] += 1
        req=urllib.request.Request(url,data=data,method=method,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=45) as resp:
                raw=resp.read()
                _clear_quota_status()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as e:
            raw=e.read().decode("utf-8","replace")
            if e.code==429:
                try:
                    parsed=json.loads(raw)
                except Exception:
                    parsed={}
                reason=parsed.get("reason") or (parsed.get("error") or {}).get("reason")
                if reason=="QUOTA_EXCEEDED":
                    details=_quota_unlock_details(e.headers)
                    if details:
                        _save_quota_status(details, reason)
                        raise SystemExit(
                            "Spotify reports QUOTA_EXCEEDED.\n"
                            f"Retry-After: {details['retry_after_raw']} seconds\n"
                            f"Spotify server time: {details['spotify_server_uk']}\n"
                            f"Calculated unlock:   {details['unlock_uk']}\n"
                            f"SAFE retry time:     {details['safe_retry_uk']}\n"
                            f"Web API calls made by this command before the stop: {API_CALLS['total']}\n\n"
                            f"The reset has been saved to {QUOTA_STATUS_FILE.name}. Until that safe time, "
                            "later manager runs will stop locally before making another Web API request.\n"
                            "If this happened during Sync, the manager checkpoint remains available for resume."
                        )
                    raise SystemExit(
                        "Spotify reports QUOTA_EXCEEDED, but this response did not include a usable Retry-After value.\n"
                        f"Web API calls made by this command before the stop: {API_CALLS['total']}\n"
                        "No exact unlock time can be calculated from this response."
                    )
                try:
                    wait=max(float(e.headers.get("Retry-After")),1.0)
                except Exception:
                    wait=min(2**attempt,60)
                if attempt>=max_retries:
                    raise SystemExit("Spotify rate limit persisted after retries.")
                print(f"Spotify rate limit: waiting {wait:g}s...")
                time.sleep(wait); continue
            if e.code==401:
                raise SystemExit("Spotify rejected/expired the access token. Re-run the command.")
            if e.code==403:
                raise SystemExit("Spotify returned 403 Forbidden. Ensure you own/collaborate on the playlist and granted the required scopes.\n"+raw)
            raise SystemExit(f"Spotify API error {e.code} on {method} {url}: {raw}")
        except urllib.error.URLError as e:
            if attempt>=max_retries:
                raise SystemExit(f"Network error: {e}")
            wait=min(2**attempt,30); print(f"Network error; retrying in {wait}s..."); time.sleep(wait)
    raise SystemExit("Spotify request failed.")

def playlist_id(value: str):
    value=value.strip()
    for pat in [r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", r"spotify:playlist:([A-Za-z0-9]+)"]:
        m=re.search(pat,value)
        if m: return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]+",value): return value
    raise SystemExit("Could not recognise that Spotify playlist URL / URI / ID.")


def get_playlist(token,pid):
    return spotify_request(token,"GET",f"/playlists/{pid}")


def get_items(token,pid):
    rows=[]; offset=0; limit=50
    while True:
        page=spotify_request(token,"GET",f"/playlists/{pid}/items?limit={limit}&offset={offset}")
        items=page.get("items",[])
        if not items: break
        for entry in items:
            item=entry.get("item") or entry.get("track") or {}
            artists=item.get("artists") or []
            rows.append({
                "position":len(rows)+1,
                "added_at":entry.get("added_at") or "",
                "uri":item.get("uri") or "",
                "name":item.get("name") or "",
                "artist":", ".join(a.get("name","") for a in artists if a.get("name")),
                "album":(item.get("album") or {}).get("name") or "",
                "is_local":bool(entry.get("is_local")),
            })
        offset += len(items)
        total=page.get("total")
        if total is not None and offset>=int(total): break
        if not page.get("next") and len(items)<limit: break
    return rows


def save_config(meta):
    data={
        "playlist_id":meta.get("id"),
        "playlist_name":meta.get("name"),
        "playlist_url":(meta.get("external_urls") or {}).get("spotify",""),
        "public":meta.get("public"),
        "snapshot_id":meta.get("snapshot_id") or "",
        "saved_at":datetime.now(timezone.utc).isoformat(),
    }
    CONFIG_FILE.write_text(json.dumps(data,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    return data

def load_config():
    if not CONFIG_FILE.exists():
        raise SystemExit(
            f"No {CONFIG_FILE.name} found. Run 'Adopt Existing Shared Master' (recommended) or 'Create New Master' first."
        )
    try:
        data=json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"Could not read {CONFIG_FILE.name}: {e}")
    if not data.get("playlist_id"):
        raise SystemExit(f"{CONFIG_FILE.name} contains no playlist_id.")
    return data




def target_signature(target):
    payload="\n".join(r["_uri"] for r in target).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def save_state_cache(pid, snapshot_id, items, target_sig=""):
    data={
        "playlist_id":pid,
        "snapshot_id":snapshot_id or "",
        "target_signature":target_sig or "",
        "saved_at":datetime.now(timezone.utc).isoformat(),
        "items":items,
    }
    tmp=STATE_CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
    tmp.replace(STATE_CACHE_FILE)
    return data


def load_state_cache(pid=None):
    if not STATE_CACHE_FILE.exists(): return None
    try:
        data=json.loads(STATE_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if pid and data.get("playlist_id") != pid: return None
    if not isinstance(data.get("items"),list): return None
    return data


def update_config_snapshot(snapshot_id):
    if not CONFIG_FILE.exists(): return
    try:
        data=json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    data["snapshot_id"]=snapshot_id or data.get("snapshot_id","")
    data["saved_at"]=datetime.now(timezone.utc).isoformat()
    CONFIG_FILE.write_text(json.dumps(data,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")


def get_current_state(token,pid,allow_cache=True):
    """Return (metadata, items, source_label).

    If a saved cache exists, only one GET /playlists/{id} call is needed to
    compare Spotify's snapshot_id. If unchanged, the full-playlist paged read is
    skipped entirely. If the snapshot differs, the live items are refreshed.
    """
    cache=load_state_cache(pid) if allow_cache else None
    meta=get_playlist(token,pid)
    snap=meta.get("snapshot_id") or ""
    if cache and snap and cache.get("snapshot_id") == snap:
        return meta,cache["items"],"verified cache (1 Spotify call)"
    items=get_items(token,pid)
    save_state_cache(pid,snap,items)
    return meta,items,"live refresh"


def offline_cached_state(pid):
    cache=load_state_cache(pid)
    if not cache:
        raise SystemExit("No saved playlist state cache exists yet. Adopt/refresh the master first.")
    return cache["items"]


def api_stats():
    if not API_CALLS["total"]: return
    print(f"\nWeb API calls this command: {API_CALLS['total']} "
          f"(GET {API_CALLS['GET']}, POST {API_CALLS['POST']}, "
          f"PUT {API_CALLS['PUT']}, DELETE {API_CALLS['DELETE']})")
def adopt_existing_master(token, rows, value=None):
    """Adopt an existing shared/public playlist without redundant rereads."""
    value = value or input("Existing SHARED playlist URL / URI / ID: ").strip()
    pid = playlist_id(value)

    if CONFIG_FILE.exists():
        try: old=json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception: old={}
        old_id=old.get("playlist_id")
        if old_id and old_id != pid:
            print(f"WARNING: {CONFIG_FILE.name} currently points to a different playlist:")
            print(f"  {old.get('playlist_url') or old_id}")
            if input("Type ADOPT to replace the saved master reference: ").strip() != "ADOPT":
                raise SystemExit("Cancelled. Existing master config was left unchanged.")

    # One metadata call + the unavoidable paged item read. The same data is then
    # reused for config, backup and preview; no second full-playlist read.
    meta,current,source=get_current_state(token,pid,allow_cache=False)
    data=save_config(meta)

    print("\nEXISTING PLAYLIST ADOPTED AS PERMANENT MASTER")
    print("---------------------------------------------")
    print(f"Name:             {data.get('playlist_name')}")
    print(f"Playlist ID:      {data.get('playlist_id')}")
    print(f"Public share URL: {data.get('playlist_url')}")
    print(f"Current items:    {len(current)}")
    print(f"Public:           {data.get('public')}")
    print(f"State source:     {source}")
    if data.get("public") is not True:
        print("\nWARNING: This playlist is not currently public.")

    print("\nCreating pre-conversion backup from the SAME fetched data (0 extra API calls)...")
    backup_master(token,pid,meta=meta,items=current)

    print("\nPREVIEW OF INITIAL CANONICAL IN-PLACE CONVERSION")
    print("-----------------------------------------")
    plan=calculate_plan(current,rows)
    print_plan(plan,current,rows)
    save_state_cache(pid,meta.get("snapshot_id") or "",current,target_signature(rows))

    print("\nNo playlist contents have been changed.")
    print("The next Sync will normally need only ONE snapshot-check call before modifications.")
    return data

def set_master(token, value=None):
    value=value or input("Existing Salsa master playlist URL / URI / ID: ").strip()
    pid=playlist_id(value)
    meta=get_playlist(token,pid)
    data=save_config(meta)
    print("Master playlist recorded.")
    print(f"Name:   {data.get('playlist_name')}")
    print(f"Public: {data.get('public')}")
    print(f"URL:    {data.get('playlist_url')}")
    if data.get("public") is not True:
        print("NOTE: Our agreed sharing strategy assumes an ordinary PUBLIC master playlist.")
    return data


def backup_master(token,pid,meta=None,items=None):
    """Write a CSV backup. Reuses supplied/cached state whenever possible."""
    if meta is None or items is None:
        meta,items,source=get_current_state(token,pid,allow_cache=True)
        print(f"Backup state source: {source}")
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    path=BACKUP_DIR/f"salsa_master_backup_{stamp}.csv"
    fields=["Position","Added At","Spotify URI","Song","Artist","Album","Is Local","Playlist ID","Playlist Name"]
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for idx,r in enumerate(items,1):
            w.writerow({
                "Position":idx,"Added At":r.get("added_at","") or "","Spotify URI":r.get("uri","") or "",
                "Song":r.get("name","") or "","Artist":r.get("artist","") or "","Album":r.get("album","") or "",
                "Is Local":"Yes" if r.get("is_local") else "No","Playlist ID":pid,
                "Playlist Name":meta.get("name") or "",
            })
    print(f"Backup saved: {path}")
    return path

def create_public_master(token):
    return spotify_request(token,"POST","/me/playlists",{
        "name":PLAYLIST_NAME,"public":True,"collaborative":False,
        "description":"Salsa Social – Structured: organically maintained social-dance master. Permanent public playlist; maintained in place.",
    })


def create_new_master(token,rows):
    if CONFIG_FILE.exists():
        existing=json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        print(f"WARNING: {CONFIG_FILE.name} already points to: {existing.get('playlist_url') or existing.get('playlist_id')}")
        if input("Create ANOTHER new master anyway? Type CREATE to continue: ").strip()!="CREATE":
            raise SystemExit("Cancelled. Existing master config was left unchanged.")
    print(f"FALLBACK: creating NEW PUBLIC playlist: {PLAYLIST_NAME}")
    print("This build preserves the exact canonical Custom order. Historical pre-rebuild Added At chronology is unavailable.")
    if input("Type BUILD to continue: ").strip()!="BUILD": raise SystemExit("Cancelled.")
    meta=create_public_master(token); pid=meta.get("id")
    if not pid: raise SystemExit("Spotify did not return the new playlist ID.")
    uris=[r["_uri"] for r in rows]; snapshot=meta.get("snapshot_id") or ""
    cached=[]
    for start in range(0,len(uris),100):
        batch=uris[start:start+100]
        resp=spotify_request(token,"POST",f"/playlists/{pid}/items",{"uris":batch})
        snapshot=resp.get("snapshot_id") or snapshot
        for r in rows[start:start+len(batch)]:
            cached.append({"position":len(cached)+1,"added_at":"","uri":r["_uri"],"name":r["Song"],"artist":r["Artist"],"album":r.get("Album","") or "","is_local":False})
        save_state_cache(pid,snapshot,cached,target_signature(rows))
        print(f"Imported {min(start+len(batch),len(uris))}/{len(uris)} tracks...")
    meta["snapshot_id"]=snapshot; data=save_config(meta)
    print("\nNEW-MASTER BUILD COMPLETE")
    print(f"Permanent playlist ID: {pid}")
    print(f"Public share URL:      {data.get('playlist_url')}")
    print("Custom order = canonical master sequence.")
    return data

def load_chronology_export(path: Path, target_rows):
    if not path.exists(): raise SystemExit(f"Chronology export not found: {path}")
    with path.open("r",encoding="utf-8-sig",newline="") as f:
        source=list(csv.DictReader(f))
    if not source: raise SystemExit("Chronology export is empty.")
    fields=set(source[0].keys())
    uri_field="Spotify URI" if "Spotify URI" in fields else "Track URI" if "Track URI" in fields else None
    id_field="Spotify Track Id" if "Spotify Track Id" in fields else None
    date_field="Original Added At" if "Original Added At" in fields else "Added At" if "Added At" in fields else None
    if not (uri_field or id_field) or not date_field:
        raise SystemExit("Chronology CSV needs Spotify URI/Track URI/Spotify Track Id plus Added At/Original Added At.")
    mapping={}
    for r in source:
        if uri_field:
            u=(r.get(uri_field) or "").strip()
        else:
            tid=(r.get(id_field) or "").strip()
            u=("spotify:track:"+tid) if tid else ""
        d=(r.get(date_field) or "").strip()
        if u and d and u not in mapping: mapping[u]=d
    missing=[r for r in target_rows if r["_uri"] not in mapping]
    if missing:
        raise SystemExit(f"Chronology export is missing {len(missing)} of {len(target_rows)} target Spotify URIs.")
    dates=[mapping[r["_uri"]] for r in target_rows]
    distinct=len(set(dates))
    if distinct < 2:
        raise SystemExit(
            "Chronology build refused: every target track has the same Added At value. "
            "That does not contain an original relative chronology to preserve."
        )
    print(f"Historical chronology source accepted: {distinct} distinct Added At values across {len(target_rows)} target tracks.")
    return mapping


def chronology_build(token,rows,chronology_path,delay):
    mapping=load_chronology_export(chronology_path,rows)
    if CONFIG_FILE.exists():
        existing=json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        print(f"WARNING: {CONFIG_FILE.name} already points to: {existing.get('playlist_url') or existing.get('playlist_id')}")
        ans=input("Create ANOTHER new master anyway? Type CREATE to continue: ").strip()
        if ans!="CREATE": raise SystemExit("Cancelled.")
    print(f"Creating chronology-aware PUBLIC playlist: {PLAYLIST_NAME}")
    print(f"Deliberate timestamp spacing: {delay:.2f}s")
    print("Spotify will still assign NEW dates; only relative historical ordering is reconstructed.")
    ans=input("Type BUILD to continue: ").strip()
    if ans!="BUILD": raise SystemExit("Cancelled.")
    meta=create_public_master(token); pid=meta.get("id")
    if not pid: raise SystemExit("Spotify did not return the new playlist ID.")
    chronological=sorted(rows,key=lambda r:(parse_datetime(mapping[r["_uri"]]),r["_pos"]))
    placed=[]
    for i,r in enumerate(chronological,1):
        insert_at=bisect.bisect_left(placed,r["_pos"])
        spotify_request(token,"POST",f"/playlists/{pid}/items",{"uris":[r["_uri"]],"position":insert_at})
        bisect.insort(placed,r["_pos"])
        print(f"[{i:>3}/{len(rows)}] chronology -> Custom #{insert_at+1:<3} {r['Song']} — {r['Artist']}")
        if i<len(rows) and delay>0: time.sleep(delay)
    meta=get_playlist(token,pid); data=save_config(meta)
    print("\nCHRONOLOGY-AWARE BUILD COMPLETE")
    print(f"Public share URL: {data.get('playlist_url')}")
    return data


def _project_after_add_remove(current,plan):
    remove_all={u for u,n in plan["removals"].items() if n>0}
    projected=[r.get("uri","") for r in current if r.get("uri","") not in remove_all]
    projected.extend(r["_uri"] for r in sorted(plan["add_rows"],key=lambda r:r["_pos"]))
    return projected


def _best_block(cur,tgt,i):
    want=tgt[i]; best_j=None; best_len=0
    for j in range(i+1,len(cur)):
        if cur[j] != want: continue
        length=0; max_len=min(len(cur)-j,len(tgt)-i)
        while length<max_len and cur[j+length] == tgt[i+length]:
            length += 1
        if length > best_len:
            best_j,best_len=j,length
    if best_j is None:
        raise ValueError(want)
    return best_j,max(1,best_len)


def simulate_block_reorder(cur,tgt):
    cur=list(cur); moves=[]; i=0
    if collections.Counter(cur) != collections.Counter(tgt):
        return None,[]
    while i < len(tgt):
        if cur[i] == tgt[i]:
            i += 1; continue
        j,length=_best_block(cur,tgt,i)
        block=cur[j:j+length]
        del cur[j:j+length]
        cur[i:i]=block
        moves.append((j,i,length))
        i += length
    return len(moves),moves


def calculate_plan(current,target):
    cur_uris=[r.get("uri","") for r in current]
    tgt_uris=[r["_uri"] for r in target]
    cc=collections.Counter(cur_uris); tc=collections.Counter(tgt_uris)
    removals={u:cc[u]-tc[u] for u in cc if cc[u]>tc[u]}
    additions={u:tc[u]-cc[u] for u in tc if tc[u]>cc[u]}
    unsafe_partial=[u for u,n in removals.items() if tc[u]>0]
    seen=collections.Counter(); add_rows=[]
    for r in target:
        seen[r["_uri"]]+=1
        if seen[r["_uri"]] > cc[r["_uri"]]: add_rows.append(r)
    same_sequence=cur_uris==tgt_uris
    projected=_project_after_add_remove(current,{"removals":removals,"add_rows":add_rows})
    block_count,_=simulate_block_reorder(projected,tgt_uris) if not unsafe_partial else (None,[])
    removal_calls=(len([u for u,n in removals.items() if n>0])+99)//100
    add_calls=(len(add_rows)+99)//100
    preserve_calls=None if block_count is None else removal_calls+add_calls+block_count
    fast_calls=1+max(0,(len(tgt_uris)-100+99)//100)
    return {
        "current_count":len(cur_uris),"target_count":len(tgt_uris),
        "removals":removals,"additions":additions,"add_rows":add_rows,
        "unsafe_partial_duplicate_removals":unsafe_partial,"same_sequence":same_sequence,
        "estimated_block_reorders":block_count,"estimated_preserve_mutations":preserve_calls,
        "estimated_fast_mutations":fast_calls,
    }


def print_plan(plan,current,target):
    print("SYNC PREVIEW")
    print("------------")
    print(f"Spotify current items:     {plan['current_count']}")
    print(f"CSV target items:          {plan['target_count']}")
    print(f"URI occurrences to add:    {sum(plan['additions'].values())}")
    print(f"URI occurrences to remove: {sum(plan['removals'].values())}")
    if plan["same_sequence"]:
        print("Custom order already matches target exactly.")
    else:
        print("Custom order differs.")
    if plan["estimated_block_reorders"] is not None:
        print(f"Estimated contiguous-block reorder calls: {plan['estimated_block_reorders']}")
        print(f"Estimated PRESERVE-mode mutation calls:   {plan['estimated_preserve_mutations']}")
        print(f"FAST rebuild mutation calls (current target): {plan['estimated_fast_mutations']}")
    print("\nPRESERVE mode keeps existing playlist memberships/Added At where possible.")
    print("FAST mode keeps the same playlist ID/share link and exact Custom order, but wholesale-replaces contents; treat existing Added At history as lost.")
    if plan["unsafe_partial_duplicate_removals"]:
        print("\nAUTOMATIC PRESERVE SYNC BLOCKED: target reduces but does not remove all occurrences of:")
        for u in plan["unsafe_partial_duplicate_removals"]: print("  "+u)
    if plan["add_rows"]:
        print("\nNew occurrences (first 20):")
        for r in plan["add_rows"][:20]: print(f"  + target #{r['_pos']}: {r['Song']} — {r['Artist']}")
        if len(plan["add_rows"])>20: print(f"  ... and {len(plan['add_rows'])-20} more")
    if plan["removals"]:
        print("\nRemoved URI occurrence counts:")
        for u,n in list(plan["removals"].items())[:20]: print(f"  - {n} x {u}")

def _placeholder_from_target(r):
    return {"position":0,"added_at":"","uri":r["_uri"],"name":r["Song"],"artist":r["Artist"],"album":r.get("Album","") or "","is_local":False}


def remove_obsolete(token,pid,plan,current_items,snapshot,target_sig):
    uris=[u for u,n in plan["removals"].items() if n>0]
    if not uris: return current_items,snapshot
    if plan["unsafe_partial_duplicate_removals"]:
        raise SystemExit("Unsafe partial duplicate removal detected; automatic preserve sync stopped.")
    remove_set=set(uris)
    for start in range(0,len(uris),100):
        batch=uris[start:start+100]
        body={"items":[{"uri":u} for u in batch]}
        if snapshot: body["snapshot_id"]=snapshot
        resp=spotify_request(token,"DELETE",f"/playlists/{pid}/items",body)
        snapshot=resp.get("snapshot_id") or snapshot
        batch_set=set(batch)
        current_items=[r for r in current_items if r.get("uri","") not in batch_set]
        save_state_cache(pid,snapshot,current_items,target_sig)
        print(f"Removed obsolete URI batch {start//100+1} ({len(batch)} URI(s)).")
    return current_items,snapshot


def add_new_occurrences(token,pid,rows,current_items,snapshot,target_sig,preserve_new_chronology=False,delay=1.05):
    if not rows: return current_items,snapshot
    ordered=sorted(rows,key=lambda r:r["_pos"])
    if preserve_new_chronology:
        def new_date(r): return r.get("Incoming Added At","") or r.get("Original Added At","") or ""
        if any(new_date(r).strip() for r in rows):
            ordered=sorted(rows,key=lambda r:(parse_datetime(new_date(r)),r["_pos"]))
        chunks=[[r] for r in ordered]
        print("Adding new tracks one-by-one to preserve their relative Incoming chronology (quota-heavier mode).")
    else:
        chunks=[ordered[i:i+100] for i in range(0,len(ordered),100)]
        print("Adding new tracks in batches of up to 100 (quota-efficient default).")
    done=0
    for chunk in chunks:
        resp=spotify_request(token,"POST",f"/playlists/{pid}/items",{"uris":[r["_uri"] for r in chunk]})
        snapshot=resp.get("snapshot_id") or snapshot
        current_items.extend(_placeholder_from_target(r) for r in chunk)
        done += len(chunk)
        save_state_cache(pid,snapshot,current_items,target_sig)
        print(f"Added new: {done}/{len(ordered)}")
        if preserve_new_chronology and done<len(ordered) and delay>0: time.sleep(delay)
    return current_items,snapshot


def reorder_to_target(token,pid,target,current_items,snapshot,target_sig):
    cur=[r.get("uri","") for r in current_items]; tgt=[r["_uri"] for r in target]
    if collections.Counter(cur)!=collections.Counter(tgt):
        raise SystemExit("After add/remove, Spotify and target do not contain the same URI multiset. Stopping before reorder.")
    moves=0; i=0
    while i<len(tgt):
        if cur[i]==tgt[i]:
            i+=1; continue
        try:
            j,length=_best_block(cur,tgt,i)
        except ValueError:
            raise SystemExit(f"Could not find target URI during reorder: {tgt[i]}")
        body={"range_start":j,"insert_before":i,"range_length":length}
        if snapshot: body["snapshot_id"]=snapshot
        resp=spotify_request(token,"PUT",f"/playlists/{pid}/items",body)
        snapshot=resp.get("snapshot_id") or snapshot
        block_uris=cur[j:j+length]; del cur[j:j+length]; cur[i:i]=block_uris
        block_items=current_items[j:j+length]; del current_items[j:j+length]; current_items[i:i]=block_items
        moves+=1; i+=length
        save_state_cache(pid,snapshot,current_items,target_sig)
        if moves%10==0 or length>1:
            print(f"Reorder calls: {moves} (last move: {length} contiguous item(s))")
    print(f"Contiguous-block reorder operations: {moves}")
    if cur!=tgt: raise SystemExit("Internal reorder verification failed.")
    return current_items,snapshot,moves


def fast_rebuild_in_place(token,pid,target,current_items,snapshot,target_sig):
    """Quota-saver: replace playlist contents in batches of up to 100 tracks.

    Keeps playlist ID/share link and exact Custom order, but does not attempt to
    preserve old playlist-item Added At history.
    """
    print("\nFAST REBUILD MODE")
    print("This wholesale-replaces playlist contents. The public playlist ID/link stays the same,")
    print("but existing Added At history should be treated as lost/reset.")
    if input("Type RESET-DATES to continue: ").strip() != "RESET-DATES":
        raise SystemExit("Cancelled.")
    first=target[:100]
    resp=spotify_request(token,"PUT",f"/playlists/{pid}/items",{"uris":[r["_uri"] for r in first]})
    snapshot=resp.get("snapshot_id") or snapshot
    current_items=[_placeholder_from_target(r) for r in first]
    save_state_cache(pid,snapshot,current_items,target_sig)
    done=len(first)
    for start in range(100,len(target),100):
        chunk=target[start:start+100]
        resp=spotify_request(token,"POST",f"/playlists/{pid}/items",{"uris":[r["_uri"] for r in chunk]})
        snapshot=resp.get("snapshot_id") or snapshot
        current_items.extend(_placeholder_from_target(r) for r in chunk)
        done += len(chunk)
        save_state_cache(pid,snapshot,current_items,target_sig)
        print(f"Fast rebuild: {done}/{len(target)}")
    update_config_snapshot(snapshot)
    return current_items,snapshot

def preview(token,rows,offline=False):
    cfg=load_config(); pid=cfg["playlist_id"]
    if offline:
        current=offline_cached_state(pid)
        print("OFFLINE PREVIEW: 0 Spotify Web API calls. Cache may be stale if the playlist was edited elsewhere.\n")
    else:
        meta,current,source=get_current_state(token,pid,allow_cache=True)
        print(f"State source: {source}\n")
    plan=calculate_plan(current,rows); print_plan(plan,current,rows)
    return plan

def sync(token,rows,delay,preserve_new_chronology=False,mode="preserve"):
    cfg=load_config(); pid=cfg["playlist_id"]; sig=target_signature(rows)
    meta,current,source=get_current_state(token,pid,allow_cache=True)
    snapshot=meta.get("snapshot_id") or cfg.get("snapshot_id") or ""
    print(f"State source: {source}")
    plan=calculate_plan(current,rows); print_plan(plan,current,rows)
    if plan["same_sequence"] and not plan["additions"] and not plan["removals"]:
        print("Nothing to do."); return
    if mode=="preserve" and plan["unsafe_partial_duplicate_removals"]:
        raise SystemExit("No changes made because duplicate-occurrence removal is ambiguous in preserve mode.")

    if mode=="fast":
        print("\nStrategy: FAST in-place rebuild. Same playlist ID/share link; minimal API calls; Added At history sacrificed.")
    else:
        print("\nStrategy: PRESERVE existing playlist memberships where possible, batch genuine additions, and move contiguous blocks.")
        print("A checkpoint is saved after every successful mutation, so an interrupted run can resume later.")
    if input("Type SYNC to apply these changes: ").strip()!="SYNC":
        raise SystemExit("Cancelled. No Spotify changes made.")

    # Reuse already-fetched/cached state; no duplicate Spotify read for the backup.
    backup_master(token,pid,meta=meta,items=current)

    if mode=="fast":
        current,snapshot=fast_rebuild_in_place(token,pid,rows,current,snapshot,sig)
    else:
        current,snapshot=remove_obsolete(token,pid,plan,current,snapshot,sig)
        current,snapshot=add_new_occurrences(
            token,pid,plan["add_rows"],current,snapshot,sig,
            preserve_new_chronology=preserve_new_chronology,delay=delay
        )
        current,snapshot,moves=reorder_to_target(token,pid,rows,current,snapshot,sig)
        update_config_snapshot(snapshot)

    tgt=[r["_uri"] for r in rows]
    if [r.get("uri","") for r in current] != tgt:
        raise SystemExit("LOCAL FINAL VERIFICATION FAILED. Backup is available in the backups folder.")
    save_state_cache(pid,snapshot,current,sig)
    print("\nSYNC COMPLETE")
    print(f"Permanent playlist ID: {pid}")
    print(f"Public share URL:      {cfg.get('playlist_url') or ''}")
    print("Custom order:          canonical master target verified against the accepted API operations.")
    print("No expensive full-playlist final reread was performed; use 'Refresh / Verify Live State' when quota permits if you want a separate live audit.")

def refresh_state(token,rows):
    cfg=load_config(); pid=cfg["playlist_id"]
    meta,items,source=get_current_state(token,pid,allow_cache=False)
    save_state_cache(pid,meta.get("snapshot_id") or "",items,target_signature(rows))
    print(f"Live state refreshed: {len(items)} item(s).")
    print(f"Snapshot ID: {meta.get('snapshot_id') or ''}")
    plan=calculate_plan(items,rows); print_plan(plan,items,rows)


def _spotify_uri_from_any(row):
    for field in ("Spotify URI","Track URI","URI"):
        u=(row.get(field) or "").strip()
        if u.startswith("spotify:track:"):
            return u
    for field in ("Spotify Track Id","Track ID","Spotify Track ID"):
        tid=(row.get(field) or "").strip()
        if tid:
            return "spotify:track:"+tid
    return ""



AUTO_EXPANDABLE_ROLES = ("Warm-up","Build","Lift","Peak","Recovery","Rebuild")
AUTO_ROLE_TARGETS = {
    "Warm-up":2, "Settle":1, "Build":4, "Lift":5, "Peak":6,
    "Recovery":3, "Rebuild":2, "Release":1, "Closer":1,
}
AUTO_ROLE_SLOTS = {
    "Warm-up":[1,2],
    "Build":[4,5,12,16],
    "Lift":[6,7,13,17,21],
    "Peak":[8,9,14,18,22,23],
    "Recovery":[10,15,19],
    "Rebuild":[11,20],
}
AUTO_FEATURE_WEIGHTS = {
    "Pulse BPM":1.8,
    "Energy":2.2,
    "Danceability":1.1,
    "Valence":0.45,
    "Loudness dB":0.75,
    "Acousticness":0.35,
    "Instrumentalness":0.25,
    "Liveness":0.25,
    "Popularity":0.20,
}


def _incoming_value(r,*names):
    for n in names:
        v=(r.get(n) or "").strip()
        if v:
            return v
    return ""


def _floatish(value):
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _normalise_artist(value):
    text=(value or "").strip().lower()
    text=re.sub(r"\s+"," ",text)
    return text


def _primary_artist(value):
    # Chosic normally uses one display field. For balancing, use the first listed
    # artist when obvious separators are present.
    text=(value or "").strip()
    for sep in (" feat. "," ft. "," featuring ",";"):
        if sep in text.lower():
            # preserve the original slice boundary approximately
            low=text.lower(); idx=low.find(sep)
            return _normalise_artist(text[:idx])
    return _normalise_artist(text.split(",")[0] if "," in text else text)


def _candidate_feature_map(row):
    bpm=_floatish(_incoming_value(row,"BPM","Chosic BPM"))
    pulse=(bpm/2.0 if bpm is not None and bpm>=135 else bpm)
    return {
        "Pulse BPM":pulse,
        "Energy":_floatish(_incoming_value(row,"Energy")),
        "Danceability":_floatish(_incoming_value(row,"Danceability","Dance")),
        "Valence":_floatish(_incoming_value(row,"Valence")),
        "Loudness dB":_floatish(_incoming_value(row,"Loudness dB","Loud (Db)","Loudness")),
        "Acousticness":_floatish(_incoming_value(row,"Acousticness","Acoustic")),
        "Instrumentalness":_floatish(_incoming_value(row,"Instrumentalness","Instrumental")),
        "Liveness":_floatish(_incoming_value(row,"Liveness","Live")),
        "Popularity":_floatish(_incoming_value(row,"Popularity")),
    }


def _row_feature_map(row):
    return {k:_floatish(row.get(k)) for k in AUTO_FEATURE_WEIGHTS}


def _feature_scales(master_rows):
    active=[r for r in master_rows if _is_main_rotation(r)]
    scales={}
    for field in AUTO_FEATURE_WEIGHTS:
        vals=[_floatish(r.get(field)) for r in active]
        vals=[v for v in vals if v is not None]
        if len(vals)<2:
            scales[field]=1.0
            continue
        mean=sum(vals)/len(vals)
        var=sum((v-mean)**2 for v in vals)/max(len(vals)-1,1)
        scales[field]=max(var**0.5,1.0)
    return scales


def _feature_distance(a, b, scales):
    total=0.0; used=0.0
    for field,weight in AUTO_FEATURE_WEIGHTS.items():
        av=a.get(field); bv=b.get(field)
        if av is None or bv is None:
            continue
        scale=scales.get(field,1.0) or 1.0
        total += weight * ((av-bv)/scale)**2
        used += weight
    if used <= 0:
        return None
    return total/used


def _auto_style(incoming, master_rows):
    artist=_normalise_artist(_incoming_value(incoming,"Artist","Artists"))
    if artist:
        matches=[r["Style"] for r in master_rows
                 if _is_main_rotation(r)
                 and _normalise_artist(r.get("Artist"))==artist
                 and (r.get("Style") or "").strip()]
        if matches:
            mode=collections.Counter(matches).most_common(1)[0]
            confidence="High" if mode[1] >= max(2,len(matches)*0.6) else "Medium"
            return mode[0], f"artist history in master ({mode[1]}/{len(matches)})", confidence

    genres=_incoming_value(incoming,"Genres","Parent Genres").lower()
    title=_incoming_value(incoming,"Song","Track","Name").lower()
    blob=genres+" "+title

    rules=[
        (("salsa romantica","salsa romántica","romantica salsa","romántica salsa"),"Salsa romántica","High"),
        (("salsa dura","salsa brava","guaguanco salsa","guaguancó salsa"),"Salsa dura / Brava","High"),
        (("timba","cubaton","cuban salsa"),"Cuban / Timba","High"),
        (("mambo","latin jazz","afro-cuban jazz","big band"),"Mambo / Big-band","High"),
        (("son cubano","son montuno","guajira","traditional cuban"),"Son / Traditional","Medium"),
        (("afrobeat","highlife","world"),"Afro-salsa / World","Medium"),
        (("salsa urbana","salsaton","salsa fusion","latin pop"),"Modern / Crossover","Medium"),
    ]
    for keys,style,conf in rules:
        if any(k in blob for k in keys):
            # salsa + son often behaves more like classic salsa than pure traditional son.
            if style=="Son / Traditional" and "salsa" in genres:
                return "Classic / Sonero","genre rules: salsa + son/cuban metadata","Medium"
            return style,"genre metadata rule",conf

    # Album era provides a modest fallback distinction for otherwise generic salsa.
    year=None
    date=_incoming_value(incoming,"Album Date")
    m=re.match(r"(\d{4})",date)
    if m:
        try: year=int(m.group(1))
        except Exception: year=None
    if "salsa" in genres and year and year <= 1985:
        return "Classic / Sonero","generic salsa + early recording era","Medium"
    if "salsa" in genres:
        return "General salsa","generic salsa metadata","Medium"
    return "General salsa","fallback: no decisive style metadata","Low"


def _auto_role(incoming, master_rows):
    candidate=_candidate_feature_map(incoming)
    active=[r for r in master_rows if _is_main_rotation(r) and (r.get("Floor Role") in AUTO_EXPANDABLE_ROLES)]
    scales=_feature_scales(active)

    # Compare with the empirical centroid of each expandable V2 Floor Role.
    role_scores=[]
    for role in AUTO_EXPANDABLE_ROLES:
        role_rows=[r for r in active if r.get("Floor Role")==role]
        centroid={}
        for field in AUTO_FEATURE_WEIGHTS:
            vals=[_floatish(r.get(field)) for r in role_rows]
            vals=[v for v in vals if v is not None]
            centroid[field]=(sum(vals)/len(vals) if vals else None)
        dist=_feature_distance(candidate,centroid,scales)
        if dist is not None:
            role_scores.append((dist,role))
    if not role_scores:
        return "Build","insufficient audio features; conservative fallback","Low"
    role_scores.sort()
    best_dist,best_role=role_scores[0]
    if len(role_scores)>1:
        gap=role_scores[1][0]-best_dist
        confidence="High" if gap>=0.45 else "Medium" if gap>=0.15 else "Low"
    else:
        confidence="Medium"
    return best_role,f"closest V2 audio-feature role profile (distance {best_dist:.2f})",confidence


def _knn_estimate(incoming, master_rows, target_field, k=9):
    candidate=_candidate_feature_map(incoming)
    active=[r for r in master_rows if _is_main_rotation(r)]
    scales=_feature_scales(active)
    scored=[]
    for r in active:
        target=_floatish(r.get(target_field))
        if target is None:
            continue
        dist=_feature_distance(candidate,_row_feature_map(r),scales)
        if dist is not None:
            scored.append((dist,target))
    if not scored:
        return None
    scored.sort(key=lambda x:x[0])
    nearest=scored[:k]
    weighted=0.0; weight_sum=0.0
    for dist,val in nearest:
        w=1.0/max(dist,0.05)
        weighted += w*val; weight_sum += w
    return weighted/weight_sum if weight_sum else None


def _style_global_share(master_rows, style):
    active=[r for r in master_rows if _is_main_rotation(r)]
    if not active:
        return 0.0
    return sum(1 for r in active if r.get("Style")==style)/len(active)


def _choose_auto_set(master_rows, role, style, artist):
    active=[r for r in master_rows if _is_main_rotation(r)]
    set_numbers=sorted({sn for r in active if (sn:=_set_number(r)) is not None})
    if not set_numbers:
        return 1,"no existing sets; created Set 1"

    global_share=_style_global_share(master_rows,style)
    artist_key=_primary_artist(artist)
    scored=[]
    for sn in set_numbers:
        group=[r for r in active if _set_number(r)==sn]
        size=len(group)
        role_count=sum(1 for r in group if r.get("Floor Role")==role)
        style_count=sum(1 for r in group if r.get("Style")==style)
        artist_count=sum(1 for r in group if _primary_artist(r.get("Artist"))==artist_key and artist_key)

        # Elasticity dominates so additions spread rather than causing one set to bloat.
        projected=size+1
        size_score=((projected-ELASTIC_SET_TARGET)**2)*5.0
        if projected>ELASTIC_SET_MAX:
            size_score += 80.0 + (projected-ELASTIC_SET_MAX)*25.0

        expected_role=AUTO_ROLE_TARGETS.get(role,1)
        role_excess=(role_count+1)-expected_role
        role_score=max(role_excess,0)*4.0 - max(-role_excess,0)*0.6

        expected_style=max(global_share*projected,0.5)
        style_excess=(style_count+1)-expected_style
        style_score=max(style_excess,0)*1.5

        artist_score=artist_count*35.0

        total=size_score+role_score+style_score+artist_score
        scored.append((total,sn,size,role_count,style_count,artist_count))

    scored.sort(key=lambda x:(x[0],x[1]))
    _,sn,size,rc,sc,ac=scored[0]
    reason=(f"best elastic balance: Set {sn} currently {size} tracks; "
            f"{role}={rc}; {style}={sc}; same primary artist={ac}")
    return sn,reason


def _transition_cost(left, candidate_features, candidate_intensity, right, scales):
    def row_cost(row):
        if row is None:
            return 0.0
        rf=_row_feature_map(row)
        cost=0.0
        # Tempo and intensity dominate local sequencing.
        p=candidate_features.get("Pulse BPM"); rp=rf.get("Pulse BPM")
        if p is not None and rp is not None:
            cost += abs(p-rp)/max(scales.get("Pulse BPM",8.0),1.0)*2.2
        ri=_floatish(row.get("Intensity V2"))
        if candidate_intensity is not None and ri is not None:
            cost += abs(candidate_intensity-ri)/10.0*1.5
        return cost
    return row_cost(left)+row_cost(right)


def _choose_auto_insertion(master_rows, set_no, incoming, role, style, intensity_est):
    indices=[i for i,r in enumerate(master_rows) if _is_main_rotation(r) and _set_number(r)==set_no]
    if not indices:
        # insert before first reserve item
        reserve_idx=next((i for i,r in enumerate(master_rows) if not _is_main_rotation(r)),len(master_rows))
        return reserve_idx, "empty/new set insertion"

    start=indices[0]; end=indices[-1]+1
    group=master_rows[start:end]
    n=len(group)
    cand_features=_candidate_feature_map(incoming)
    scales=_feature_scales(master_rows)
    artist_key=_primary_artist(_incoming_value(incoming,"Artist","Artists"))
    target_slots=AUTO_ROLE_SLOTS.get(role,[13])

    candidates=[]
    for local_idx in range(n+1):
        # Map the elastic set position back to a nominal 25-slot V2 coordinate.
        nominal=1.0 + (local_idx/max(n,1))*24.0
        role_penalty=min(abs(nominal-s) for s in target_slots)*1.25

        left=group[local_idx-1] if local_idx>0 else None
        right=group[local_idx] if local_idx<n else None
        transition=_transition_cost(left,cand_features,intensity_est,right,scales)

        artist_penalty=0.0
        for d in range(1,5):
            for j in (local_idx-d, local_idx+d-1):
                if 0<=j<n and artist_key and _primary_artist(group[j].get("Artist"))==artist_key:
                    artist_penalty += (5-d)*8.0

        style_penalty=0.0
        if left is not None and left.get("Style")==style: style_penalty+=0.7
        if right is not None and right.get("Style")==style: style_penalty+=0.7

        score=role_penalty+transition+artist_penalty+style_penalty
        candidates.append((score,local_idx,nominal,transition,artist_penalty))

    candidates.sort(key=lambda x:(x[0],x[1]))
    score,local_idx,nominal,transition,artist_penalty=candidates[0]
    global_insert_index=start+local_idx
    reason=(f"best local flow in Set {set_no}: nominal V2 slot {nominal:.1f}; "
            f"transition score {transition:.2f}; artist-spacing penalty {artist_penalty:.1f}")
    return global_insert_index,reason


def _new_master_row_from_auto(source, master_fields, style, role, set_no,
                              intensity_est=None, social_fit_est=None, note=""):
    row={k:"" for k in master_fields}
    bpm=_floatish(_incoming_value(source,"BPM","Chosic BPM"))
    pulse=(bpm/2.0 if bpm is not None and bpm>=135 else bpm) if bpm is not None else None
    uri=_spotify_uri_from_any(source)
    track_id=uri.split(":")[-1] if uri.startswith("spotify:track:") else _incoming_value(source,"Spotify Track Id","Spotify Track ID")
    mapping={
        "Section":"Main Rotation",
        "Set":str(set_no),
        "Set Name":f"Social Set {set_no:02d}",
        "Floor Role":role,
        "Song":_incoming_value(source,"Song","Track","Name"),
        "Artist":_incoming_value(source,"Artist","Artists"),
        "Style":style,
        "Chosic BPM":("" if bpm is None else str(round(bpm,3))),
        "Pulse BPM":("" if pulse is None else str(round(pulse,3))),
        "Energy":_incoming_value(source,"Energy"),
        "Danceability":_incoming_value(source,"Danceability","Dance"),
        "Valence":_incoming_value(source,"Valence"),
        "Intensity V2":("" if intensity_est is None else f"{intensity_est:.2f}"),
        "Social Fit V2":("" if social_fit_est is None else f"{social_fit_est:.2f}"),
        "Camelot":_incoming_value(source,"Camelot"),
        "Key":_incoming_value(source,"Key"),
        "Loudness dB":_incoming_value(source,"Loudness dB","Loud (Db)","Loudness"),
        "Acousticness":_incoming_value(source,"Acousticness","Acoustic"),
        "Instrumentalness":_incoming_value(source,"Instrumentalness","Instrumental"),
        "Liveness":_incoming_value(source,"Liveness","Live"),
        "Popularity":_incoming_value(source,"Popularity"),
        "Duration":_incoming_value(source,"Duration"),
        "Genres":_incoming_value(source,"Genres"),
        "Album":_incoming_value(source,"Album"),
        "Album Date":_incoming_value(source,"Album Date"),
        "Spotify Track Id":track_id,
        "Spotify URI":uri,
        "ISRC":_incoming_value(source,"ISRC"),
        "Original Chosic Position":_incoming_value(source," #","Original Chosic Position"),
        "V2 Notes":note or "Incoming auto-promotion.",
    }
    row.update({k:v for k,v in mapping.items() if k in row})
    return row


def _renumber_master(rows):
    slot_counts=collections.Counter()
    for pos,r in enumerate(rows,1):
        r["New Position"]=str(pos)
        if _is_main_rotation(r):
            sn=_set_number(r)
            if sn is not None:
                slot_counts[sn]+=1
                if "Slot" in r:
                    r["Slot"]=str(slot_counts[sn])
                if "Set Name" in r:
                    r["Set Name"]=f"Social Set {sn:02d}"
        elif "Slot" in r and not (r.get("Slot") or "").strip():
            r["Slot"]=""
    return rows


def prepare_incoming_auto_plan(master_rows, incoming_path: Path):
    """Automatically programme every genuinely new Incoming track.

    Presence in Incoming means PROMOTE. There is no Reject category and no
    per-track curator decision. Already-present tracks and duplicate Incoming
    occurrences are skipped. Missing Spotify identifiers abort the plan because
    such a track cannot be promoted safely.
    """
    if not incoming_path.exists():
        raise SystemExit(f"Incoming CSV not found: {incoming_path}")
    with incoming_path.open("r",encoding="utf-8-sig",newline="") as f:
        incoming=list(csv.DictReader(f))
    if not incoming:
        raise SystemExit("Incoming CSV is empty.")

    baseline_sig=target_signature(master_rows)
    master_uris={r["_uri"] for r in master_rows}
    seen=set()
    invalid=[]
    statuses=[]
    for idx,r in enumerate(incoming,2):
        uri=_spotify_uri_from_any(r)
        if not uri:
            invalid.append(idx)
            statuses.append((r,uri,"Missing Spotify URI/ID"))
        elif uri in master_uris:
            statuses.append((r,uri,"Already in master"))
        elif uri in seen:
            statuses.append((r,uri,"Duplicate within Incoming export"))
        else:
            seen.add(uri)
            statuses.append((r,uri,"Auto Promote"))

    if invalid:
        raise SystemExit(
            "Incoming contains row(s) with no usable Spotify URI/Track ID: "
            + ", ".join(map(str,invalid))
            + ". Fix/re-export the Incoming CSV; no plan was created."
        )

    work_rows=[dict(r) for r in master_rows]
    planned=[]
    sequence=0

    for source,uri,status in statuses:
        if status!="Auto Promote":
            planned.append({
                "_source":source, "Plan Sequence":"", "Action":"Skip",
                "Master Status":status, "Spotify URI":uri,
                "Auto Set":"", "Auto Floor Role":"", "Auto Style":"",
                "Auto Insert After Position":"", "Planned Final Position":"",
                "Auto Intensity V2":"", "Auto Social Fit V2":"",
                "Style Basis":"", "Role Basis":"", "Set Basis":"",
                "Placement Basis":"", "Confidence":"", "Master Baseline Signature":baseline_sig,
            })
            continue

        sequence+=1
        style,style_basis,style_conf=_auto_style(source,work_rows)
        role,role_basis,role_conf=_auto_role(source,work_rows)
        set_no,set_basis=_choose_auto_set(
            work_rows,role,style,_incoming_value(source,"Artist","Artists")
        )
        intensity_est=_knn_estimate(source,work_rows,"Intensity V2")
        social_fit_est=_knn_estimate(source,work_rows,"Social Fit V2")
        insert_index,placement_basis=_choose_auto_insertion(
            work_rows,set_no,source,role,style,intensity_est
        )

        # "Insert After" is the number of rows already before the insertion point
        # in the evolving simulation. Replaying plan rows in sequence reproduces it.
        anchor=insert_index
        conf_rank={"High":3,"Medium":2,"Low":1}
        conf="High" if min(conf_rank.get(style_conf,1),conf_rank.get(role_conf,1))>=3 else \
             "Medium" if min(conf_rank.get(style_conf,1),conf_rank.get(role_conf,1))>=2 else "Low"

        note=(f"Incoming auto-promotion | Style: {style_basis} | Role: {role_basis} | "
              f"Set: {set_basis} | Placement: {placement_basis}")
        with (APP_DIR/CSV_FILENAME).open("r",encoding="utf-8-sig",newline="") as f:
            master_fields=csv.DictReader(f).fieldnames or []
        newrow=_new_master_row_from_auto(
            source,master_fields,style,role,set_no,intensity_est,social_fit_est,note
        )
        work_rows.insert(insert_index,newrow)
        _renumber_master(work_rows)

        planned.append({
            "_source":source, "Plan Sequence":str(sequence), "Action":"Promote",
            "Master Status":"Auto Promote", "Spotify URI":uri,
            "Auto Set":str(set_no), "Auto Floor Role":role, "Auto Style":style,
            "Auto Insert After Position":str(anchor), "Planned Final Position":"",
            "Auto Intensity V2":("" if intensity_est is None else f"{intensity_est:.2f}"),
            "Auto Social Fit V2":("" if social_fit_est is None else f"{social_fit_est:.2f}"),
            "Style Basis":style_basis, "Role Basis":role_basis, "Set Basis":set_basis,
            "Placement Basis":placement_basis, "Confidence":conf,
            "Master Baseline Signature":baseline_sig,
        })

    # Final positions after all incoming insertions have been simulated.
    final_pos={r.get("Spotify URI") or r.get("_uri"):i for i,r in enumerate(work_rows,1)}
    for p in planned:
        if p["Action"]=="Promote":
            p["Planned Final Position"]=str(final_pos.get(p["Spotify URI"],""))

    INCOMING_DIR.mkdir(exist_ok=True)
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    out=INCOMING_DIR/f"incoming_auto_plan_{stamp}.csv"
    plan_fields=[
        "Plan Sequence","Action","Master Status","Spotify URI",
        "Auto Set","Auto Floor Role","Auto Style","Auto Insert After Position",
        "Planned Final Position","Auto Intensity V2","Auto Social Fit V2",
        "Confidence","Style Basis","Role Basis","Set Basis",
        "Placement Basis","Master Baseline Signature"
    ]
    source_fields=list(incoming[0].keys())
    source_fields=[f for f in source_fields if f not in plan_fields and f!="Spotify URI"]
    fields=plan_fields+source_fields
    with out.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for p in planned:
            row={k:"" for k in fields}
            for k in plan_fields:
                row[k]=p.get(k,"")
            for k,v in p["_source"].items():
                if k in row and k not in plan_fields:
                    row[k]=v
            w.writerow(row)

    promoted=sum(1 for p in planned if p["Action"]=="Promote")
    existing=sum(1 for p in planned if p["Master Status"]=="Already in master")
    dupes=sum(1 for p in planned if p["Master Status"]=="Duplicate within Incoming export")
    print("INCOMING AUTOMATIC PLACEMENT PLAN CREATED")
    print("-----------------------------------------")
    print(f"Incoming rows:             {len(incoming)}")
    print(f"Automatically promoted:    {promoted}")
    print(f"Already in master:         {existing}")
    print(f"Duplicate Incoming rows:   {dupes}")
    print(f"Current master size:       {len(master_rows)}")
    print(f"Planned master size:       {len(work_rows)}")
    print(f"Plan file:                 {out}")
    print("\nNo Promote/Reserve/Reject choices are required.")
    print("Style, Floor Role, Set and insertion point have all been chosen automatically.")
    print("Spotify has NOT been contacted and the canonical master has NOT yet changed.")
    print("\nPlanned promotions:")
    for p in planned:
        if p["Action"]=="Promote":
            s=p["_source"]
            print(f"  #{p['Plan Sequence']}: {_incoming_value(s,'Song','Track','Name')} — "
                  f"{_incoming_value(s,'Artist','Artists')} | Set {p['Auto Set']} | "
                  f"{p['Auto Floor Role']} | {p['Auto Style']} | final ~{p['Planned Final Position']}")
    print()
    analyze_elastic_sets(work_rows,verbose=True)
    return out


def _latest_incoming_auto_plan():
    INCOMING_DIR.mkdir(exist_ok=True)
    plans=sorted(INCOMING_DIR.glob("incoming_auto_plan_*.csv"),key=lambda p:p.stat().st_mtime,reverse=True)
    return plans[0] if plans else None


def apply_incoming_auto_plan(master_rows, plan_path: Path | None = None):
    """Apply the automatically generated Incoming plan to the local canonical CSV.

    No per-track curator decisions are read. All Action=Promote rows are applied
    exactly in Plan Sequence order; Skip rows remain untouched.
    """
    plan_path=plan_path or _latest_incoming_auto_plan()
    if plan_path is None:
        raise SystemExit("No incoming_auto_plan_*.csv found. Run Prepare Incoming first.")
    if not plan_path.exists():
        raise SystemExit(f"Incoming plan CSV not found: {plan_path}")

    with plan_path.open("r",encoding="utf-8-sig",newline="") as f:
        plan=list(csv.DictReader(f))
    if not plan:
        raise SystemExit("Incoming automatic plan is empty.")

    expected_sig=next((r.get("Master Baseline Signature","").strip() for r in plan
                       if r.get("Master Baseline Signature","").strip()),"")
    current_sig=target_signature(master_rows)
    if expected_sig and current_sig != expected_sig:
        raise SystemExit(
            "The canonical master has changed since this Incoming plan was prepared.\n"
            "For safety, this plan will NOT be applied. Run Prepare Incoming again."
        )

    promotions=[r for r in plan if (r.get("Action") or "").strip().lower()=="promote"]
    promotions.sort(key=lambda r:int(r.get("Plan Sequence") or "0"))
    if not promotions:
        print("The latest Incoming plan contains no new tracks to promote.")
        return master_rows

    master_path=APP_DIR/CSV_FILENAME
    with master_path.open("r",encoding="utf-8-sig",newline="") as f:
        reader=csv.DictReader(f); master_fields=reader.fieldnames or []

    current_uris={r["_uri"] for r in master_rows}
    rows=[dict(r) for r in master_rows]

    # Simulate the full apply first, before touching the canonical file.
    for idx,p in enumerate(promotions,1):
        uri=_spotify_uri_from_any(p)
        if not uri:
            uri=(p.get("Spotify URI") or "").strip()
        if not uri:
            raise SystemExit(f"Plan sequence {idx}: missing Spotify URI.")
        if uri in current_uris:
            raise SystemExit(
                f"Plan sequence {idx}: {uri} is already in the current master. "
                "Prepare a fresh Incoming plan."
            )
        try:
            anchor=int((p.get("Auto Insert After Position") or "").strip())
            set_no=int(float((p.get("Auto Set") or "").strip()))
        except Exception:
            raise SystemExit(f"Plan sequence {idx}: invalid automatic placement metadata.")
        if not (0 <= anchor <= len(rows)):
            raise SystemExit(f"Plan sequence {idx}: insertion anchor {anchor} is outside the current simulated master.")
        style=(p.get("Auto Style") or "").strip() or "General salsa"
        role=(p.get("Auto Floor Role") or "").strip() or "Build"
        intensity_est=_floatish(p.get("Auto Intensity V2"))
        social_fit_est=_floatish(p.get("Auto Social Fit V2"))
        note=(f"Incoming auto-promotion | {p.get('Style Basis','')} | {p.get('Role Basis','')} | "
              f"{p.get('Set Basis','')} | {p.get('Placement Basis','')}")
        newrow=_new_master_row_from_auto(
            p,master_fields,style,role,set_no,intensity_est,social_fit_est,note
        )
        rows.insert(anchor,newrow)
        current_uris.add(uri)
        _renumber_master(rows)

    # Verify planned final positions where available.
    pos_by_uri={(r.get("Spotify URI") or r.get("_uri")):i for i,r in enumerate(rows,1)}
    mismatches=[]
    for p in promotions:
        expected=(p.get("Planned Final Position") or "").strip()
        uri=(p.get("Spotify URI") or "").strip()
        if expected and uri:
            actual=str(pos_by_uri.get(uri,""))
            if actual != expected:
                mismatches.append((uri,expected,actual))
    if mismatches:
        raise SystemExit(
            "Automatic plan replay did not reproduce its planned final positions. "
            "Canonical master was NOT changed. Prepare the Incoming plan again."
        )

    print("READY TO APPLY AUTOMATIC INCOMING PLAN")
    print("--------------------------------------")
    print(f"Plan:                  {plan_path}")
    print(f"Tracks to promote:     {len(promotions)}")
    print(f"Master size:           {len(master_rows)} -> {len(rows)}")
    answer=input("Type APPLY to update the LOCAL canonical master (Spotify will not be contacted): ").strip()
    if answer!="APPLY":
        raise SystemExit("Cancelled. Canonical master was not changed.")

    MASTER_HISTORY_DIR.mkdir(exist_ok=True)
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    backup=MASTER_HISTORY_DIR/f"salsa_social_master_before_incoming_auto_{stamp}.csv"
    shutil.copy2(master_path,backup)

    with master_path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=master_fields,extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

    print("\nAUTOMATIC INCOMING PLAN APPLIED")
    print("-------------------------------")
    print(f"Automatically promoted:    {len(promotions)}")
    print(f"New canonical size:        {len(rows)}")
    print(f"Previous master backup:    {backup}")
    print(f"Updated canonical file:    {master_path}")
    print("\nSpotify has NOT been changed.")
    print("Next: Validate Master CSV -> Preview from Cache -> Preserve Sync.")
    refreshed=load_target_csv(master_path)
    print()
    analyze_elastic_sets(refreshed,verbose=True)
    return refreshed


# Backward-compatible command aliases from the previous package.
def prepare_incoming_review(master_rows, incoming_path: Path):
    return prepare_incoming_auto_plan(master_rows,incoming_path)


def apply_incoming_review(master_rows, review_path: Path):
    return apply_incoming_auto_plan(master_rows,review_path)




def remove_or_rest_tracks(master_rows):
    """Interactively remove tracks or move them to Reserve in the local master.

    This is a curator-controlled, zero-Spotify-call operation. Searches can use
    the current master position, title, artist, or Spotify URI. Multiple changes
    may be queued, reviewed, and applied together. The canonical CSV is backed
    up before any write.
    """
    master_path=APP_DIR/CSV_FILENAME
    with master_path.open("r",encoding="utf-8-sig",newline="") as f:
        reader=csv.DictReader(f)
        master_fields=reader.fieldnames or []
    if not master_fields:
        raise SystemExit("Canonical master has no header row.")

    # Work from the current canonical positions. Queue by original position so
    # duplicates/alternate occurrences remain independently addressable.
    by_pos={int(r["_pos"]):r for r in master_rows}
    queued={}  # original position -> "remove" | "rest"

    def label(r):
        section=(r.get("Section") or "").strip() or "(no section)"
        set_value=(r.get("Set") or "").strip()
        extra=f" | {section}" + (f" | Set {set_value}" if set_value else "")
        return f"#{int(r['_pos']):>4}  {r.get('Song','')} — {r.get('Artist','')}{extra}"

    def find_matches(raw):
        q=raw.strip()
        if not q:
            return []
        # A bare integer or #integer is interpreted as an exact current position.
        m=re.fullmatch(r"#?\s*(\d+)",q)
        if m:
            pos=int(m.group(1))
            return [by_pos[pos]] if pos in by_pos else []
        ql=q.casefold()
        out=[]
        for r in master_rows:
            hay=" | ".join([
                str(r.get("Song") or ""), str(r.get("Artist") or ""),
                str(r.get("Spotify URI") or ""), str(r.get("ISRC") or ""),
            ]).casefold()
            if ql in hay:
                out.append(r)
        return out

    print("REMOVE / REST TRACKS FROM LOCAL MASTER")
    print("---------------------------------------")
    print("Search by current position, title, artist, Spotify URI or ISRC.")
    print("For each selected occurrence choose:")
    print("  R = Remove completely from the master")
    print("  S = Rest / move to Reserve / Specialist")
    print("  C = Cancel selection")
    print("Type DONE when you have finished queuing changes, or QUIT to exit unchanged.\n")

    while True:
        raw=input("Search (or DONE / QUIT): ").strip()
        if raw.casefold() in ("quit","q","exit"):
            print("No changes applied.")
            return master_rows
        if raw.casefold() in ("done","d"):
            break

        matches=find_matches(raw)
        if not matches:
            print("No matching track found.\n")
            continue
        if len(matches)>30:
            print(f"{len(matches)} matches found. Showing the first 30; narrow your search if needed.")
            matches=matches[:30]
        for i,r in enumerate(matches,1):
            pending=queued.get(int(r["_pos"]))
            suffix=f"  [QUEUED: {pending.upper()}]" if pending else ""
            print(f"  {i:>2}. {label(r)}{suffix}")

        if len(matches)==1:
            chosen=matches[0]
        else:
            sel=input("Select match number (or press Enter to cancel this search): ").strip()
            if not sel:
                print()
                continue
            try:
                n=int(sel)
            except ValueError:
                print("Invalid selection.\n")
                continue
            if not (1 <= n <= len(matches)):
                print("Selection out of range.\n")
                continue
            chosen=matches[n-1]

        pos=int(chosen["_pos"])
        print("Selected:",label(chosen))
        current_action=queued.get(pos)
        if current_action:
            print(f"Currently queued as: {current_action.upper()}")
        action=input("Action [R]emove / re[S]t to Reserve / [C]ancel / [U]nqueue: ").strip().casefold()
        if action in ("r","remove"):
            queued[pos]="remove"
            print("Queued for REMOVE.\n")
        elif action in ("s","rest","reserve"):
            if not _is_main_rotation(chosen):
                print("This track is already outside the Main Rotation; no Rest action is needed.\n")
            else:
                queued[pos]="rest"
                print("Queued to REST / MOVE TO RESERVE.\n")
        elif action in ("u","unqueue"):
            queued.pop(pos,None)
            print("Removed from queue.\n")
        else:
            print("Selection cancelled.\n")

    if not queued:
        print("No Remove/Rest actions were queued. Canonical master was not changed.")
        return master_rows

    print("\nPROPOSED LOCAL MASTER CHANGES")
    print("-----------------------------")
    remove_positions=sorted(p for p,a in queued.items() if a=="remove")
    rest_positions=sorted(p for p,a in queued.items() if a=="rest")
    if remove_positions:
        print("REMOVE completely:")
        for p in remove_positions:
            print("  -",label(by_pos[p]))
    if rest_positions:
        print("REST to Reserve / Specialist:")
        for p in rest_positions:
            print("  -",label(by_pos[p]))
    print(f"\nCurrent master size: {len(master_rows)}")
    print(f"After changes:       {len(master_rows)-len(remove_positions)}")
    print("Rested tracks remain in the master, at the end of the Reserve section.")
    print("Spotify will NOT be contacted by this command.")

    confirm=input("\nType APPLY to update salsa_social_master.csv: ").strip()
    if confirm != "APPLY":
        print("Cancelled. Canonical master was not changed.")
        return master_rows

    MASTER_HISTORY_DIR.mkdir(exist_ok=True)
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    backup=MASTER_HISTORY_DIR/f"salsa_social_master_before_remove_rest_{stamp}.csv"
    shutil.copy2(master_path,backup)

    # Preserve source order for untouched tracks. Rested occurrences are removed
    # from their active positions and appended after all remaining rows.
    remaining=[]
    rested=[]
    today=datetime.now().strftime("%Y-%m-%d")
    for r0 in master_rows:
        p=int(r0["_pos"])
        action=queued.get(p)
        if action=="remove":
            continue
        r=dict(r0)
        if action=="rest":
            r["Section"]="Reserve / Specialist"
            if "Set" in r: r["Set"]="Reserve"
            if "Set Name" in r: r["Set Name"]="Reserve / Specialist"
            if "Slot" in r: r["Slot"]=""
            if "Floor Role" in r: r["Floor Role"]="Reserve"
            if "V2 Notes" in r:
                prior=(r.get("V2 Notes") or "").strip()
                note=f"Rested from Main Rotation by curator on {today}."
                r["V2 Notes"]=(prior+" " + note).strip()
            rested.append(r)
        else:
            remaining.append(r)

    rows=remaining+rested
    _renumber_master(rows)
    with master_path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=master_fields,extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

    print("\nLOCAL MASTER UPDATED")
    print("--------------------")
    print(f"Removed completely:        {len(remove_positions)}")
    print(f"Rested to Reserve:         {len(rest_positions)}")
    print(f"New canonical size:        {len(rows)}")
    print(f"Previous master backup:    {backup}")
    print(f"Updated canonical file:    {master_path}")
    print("\nSpotify has NOT been changed.")
    print("Next: Validate Master CSV → Preview from Cache → Preserve Sync.")

    refreshed=load_target_csv(master_path)
    print()
    analyze_elastic_sets(refreshed,verbose=True)
    return refreshed


def show_config():
    if not CONFIG_FILE.exists():
        print("No master config saved yet."); return
    print(CONFIG_FILE.read_text(encoding="utf-8"))


def show_quota_status():
    if not QUOTA_STATUS_FILE.exists():
        print("No saved Spotify quota lock is currently recorded.")
        return
    try:
        data=json.loads(QUOTA_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Could not read {QUOTA_STATUS_FILE.name}: {e}")
        return
    print("Saved Spotify quota status")
    print("---------------------------")
    print(f"Reason:              {data.get('reason','')}")
    print(f"Retry-After:         {data.get('retry_after_raw','')}")
    print(f"Spotify server time: {data.get('spotify_server_uk','')}")
    print(f"Calculated unlock:   {data.get('unlock_uk','')}")
    print(f"Safe retry time:     {data.get('safe_retry_uk','')}")
    raw=data.get('safe_retry_utc')
    if raw:
        try:
            safe=datetime.fromisoformat(raw)
            if safe.tzinfo is None: safe=safe.replace(tzinfo=timezone.utc)
            remaining=max(int((safe-datetime.now(timezone.utc)).total_seconds()+0.999),0)
            if remaining:
                print(f"Remaining locally:   about {remaining} seconds")
            else:
                print("Remaining locally:   safe retry time has passed")
        except Exception:
            pass


def self_test():
    # Initial chronology-aware build invariant.
    sample=[
        {"_pos":1,"Original Added At":"2022-05-01T00:00:00Z","_uri":"A"},
        {"_pos":2,"Original Added At":"2020-05-01T00:00:00Z","_uri":"B"},
        {"_pos":3,"Original Added At":"2023-05-01T00:00:00Z","_uri":"C"},
        {"_pos":4,"Original Added At":"2021-05-01T00:00:00Z","_uri":"D"},
    ]
    chrono=sorted(sample,key=lambda r:(parse_datetime(r["Original Added At"]),r["_pos"]))
    placed=[]; final=[]; adds=[]
    for r in chrono:
        idx=bisect.bisect_left(placed,r["_pos"]); final.insert(idx,r["_uri"]); bisect.insort(placed,r["_pos"]); adds.append(r["_uri"])
    assert adds==["B","D","A","C"] and final==["A","B","C","D"]
    # Reorder invariant incl duplicate URI.
    cur=["X","B","A","X","C"]; tgt=["A","X","B","C","X"]
    for i,w in enumerate(tgt):
        if cur[i]==w: continue
        j=cur.index(w,i+1); item=cur.pop(j); cur.insert(i,item)
    assert cur==tgt
    # Contiguous-block reorder estimator should use fewer calls when runs exist.
    cur2=["D","E","A","B","C","F"]; tgt2=["A","B","C","D","E","F"]
    count,moves=simulate_block_reorder(cur2,tgt2); assert count==1 and moves[0][2]==3
    # Exact example observed from Spotify on 29 Aug 2026.
    class Headers(dict):
        pass
    q=_quota_unlock_details(Headers({"Retry-After":"57398", "Date":"Sat, 29 Aug 2026 00:23:21 GMT"}))
    assert q and q["unlock_uk"] == "Saturday 29 August 2026 at 17:19:59 BST"
    assert q["safe_retry_uk"] == "Saturday 29 August 2026 at 17:20:04 BST"
    rows=load_target_csv(); assert len(rows)>=1; assert rows[-1]["_pos"]==len(rows)
    print("SELF-TEST PASSED")
    print("• chronology-aware insertion algorithm")
    print("• duplicate-tolerant reorder simulation")
    print("• contiguous-block reorder optimisation")
    print(f"• flexible canonical master CSV ({len(rows)} tracks; V2.0 baseline {V2_BASELINE_TRACKS})")
    print("• QUOTA_EXCEEDED Retry-After / UK unlock calculation")
    print("• automatic Incoming placement helpers available (Style / Floor Role / Set / insertion)")
    health=analyze_elastic_sets(rows,verbose=False); assert health["active"]>=1


def menu():
    print("Choose an action:")
    print("  1. Validate canonical master CSV — variable size / elastic sets")
    print("  2. ADOPT EXISTING SHARED PLAYLIST as permanent master  [RECOMMENDED]")
    print("  3. Preview from saved cache — ZERO Web API calls")
    print("  4. Apply PRESERVE sync in place — quota-efficient / resumable")
    print("  5. Apply FAST rebuild in place — resets live Added At history")
    print("  6. Refresh / verify live Spotify state — full live read")
    print("  7. Backup current Spotify master")
    print("  8. Create a BRAND-NEW public master  [FALLBACK ONLY]")
    print("  9. Chronology build — historical dates only (quota-heavy)")
    print(" 10. Show saved master config")
    print(" 11. Show saved quota unlock status — ZERO API calls")
    print(" 12. Self-test (no Spotify changes)")
    print(" 13. Prepare Incoming AUTO placement plan — inclusion means Promote; ZERO Spotify calls")
    print(" 14. Apply latest Incoming AUTO plan to local master — ZERO Spotify calls")
    print(" 15. Analyse elastic 25-track set balance — ZERO Spotify calls")
    print(" 16. Remove / Rest Tracks from Master — deliberate removal/rest only; ZERO Spotify calls")
    choice=input("Selection: ").strip()
    return {"1":"validate","2":"adopt-master","3":"preview-offline","4":"sync-preserve",
            "5":"sync-fast","6":"refresh-state","7":"backup","8":"create-new-master",
            "9":"chronology-build","10":"show-config","11":"show-quota","12":"self-test",
            "13":"prepare-incoming","14":"apply-incoming","15":"set-health",
            "16":"remove-rest"}.get(choice)


def build_parser():
    p=argparse.ArgumentParser(description="Quota-efficient manager for the organically growing Salsa Social – Structured Spotify master.")
    sub=p.add_subparsers(dest="cmd")
    sub.add_parser("validate")
    am=sub.add_parser("adopt-master"); am.add_argument("playlist",nargs="?")
    pv=sub.add_parser("preview"); pv.add_argument("--offline",action="store_true")
    sy=sub.add_parser("sync")
    sy.add_argument("--mode",choices=["preserve","fast"],default="preserve")
    sy.add_argument("--delay",type=float,default=DEFAULT_DELAY)
    sy.add_argument("--preserve-new-chronology",action="store_true")
    sub.add_parser("refresh-state")
    sub.add_parser("backup")
    sub.add_parser("create-new-master")
    cb=sub.add_parser("chronology-build"); cb.add_argument("chronology_csv",nargs="?"); cb.add_argument("--delay",type=float,default=DEFAULT_DELAY)
    sub.add_parser("show-config")
    sub.add_parser("show-quota")
    sub.add_parser("self-test")
    pi=sub.add_parser("prepare-incoming"); pi.add_argument("incoming_csv",nargs="?")
    ai=sub.add_parser("apply-incoming"); ai.add_argument("plan_csv",nargs="?")
    sub.add_parser("set-health")
    sub.add_parser("remove-rest")
    sub.add_parser("initial-build",help=argparse.SUPPRESS)
    sm=sub.add_parser("set-master",help=argparse.SUPPRESS); sm.add_argument("playlist",nargs="?")
    return p


def main():
    os.chdir(APP_DIR); start_session_log(); banner(); p=build_parser(); args=p.parse_args(); cmd=args.cmd or menu()
    if not cmd: raise SystemExit("Unknown selection.")
    if cmd=="self-test": self_test(); return
    rows=load_target_csv()
    if cmd=="validate": validate_target(rows); return
    if cmd=="show-config": show_config(); return
    if cmd=="show-quota": show_quota_status(); return
    if cmd=="set-health": analyze_elastic_sets(rows,verbose=True); return
    if cmd=="remove-rest": remove_or_rest_tracks(rows); return
    if cmd=="prepare-incoming":
        raw=getattr(args,"incoming_csv",None) or input("Path to Chosic/CSV export of Incoming playlist: ").strip().strip('"')
        prepare_incoming_auto_plan(rows,Path(raw).expanduser().resolve()); return
    if cmd=="apply-incoming":
        raw=getattr(args,"plan_csv",None)
        plan_path=Path(raw).expanduser().resolve() if raw else None
        apply_incoming_auto_plan(rows,plan_path); return
    if cmd=="preview-offline" or (cmd=="preview" and getattr(args,"offline",False)):
        preview(None,rows,offline=True); return

    adopt_value=None
    if cmd=="adopt-master":
        validate_target(rows)
        adopt_value=getattr(args,"playlist",None) or input("Existing SHARED playlist URL / URI / ID: ").strip()
        if not adopt_value: raise SystemExit("No playlist was supplied.")

    check_saved_quota_lock()
    token=authorize(get_client_id())
    if cmd=="adopt-master": adopt_existing_master(token,rows,adopt_value)
    elif cmd in ("create-new-master","initial-build"): validate_target(rows); create_new_master(token,rows)
    elif cmd=="chronology-build":
        raw=getattr(args,"chronology_csv",None) or input("Path to historical Added At CSV: ").strip().strip('"')
        print("WARNING: chronology build deliberately uses one addition per track and is quota-heavy.")
        chronology_build(token,rows,Path(raw).expanduser().resolve(),max(0.0,getattr(args,"delay",DEFAULT_DELAY)))
    elif cmd=="set-master": set_master(token,getattr(args,"playlist",None))
    elif cmd=="preview": preview(token,rows,offline=getattr(args,"offline",False))
    elif cmd=="sync": sync(token,rows,max(0.0,getattr(args,"delay",DEFAULT_DELAY)),
                           preserve_new_chronology=getattr(args,"preserve_new_chronology",False),
                           mode=getattr(args,"mode","preserve"))
    elif cmd=="sync-preserve": sync(token,rows,DEFAULT_DELAY,False,"preserve")
    elif cmd=="sync-fast": sync(token,rows,DEFAULT_DELAY,False,"fast")
    elif cmd=="refresh-state": refresh_state(token,rows)
    elif cmd=="backup":
        cfg=load_config(); meta,items,source=get_current_state(token,cfg["playlist_id"],allow_cache=True)
        print(f"State source: {source}"); backup_master(token,cfg["playlist_id"],meta=meta,items=items)
    api_stats()



if __name__=="__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
