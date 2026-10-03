#!/usr/bin/env python3
"""Desktop UI for the Salsa Social – Structured Spotify Playlist Manager.

This UI deliberately keeps the existing salsa_spotify_manager.py as the engine.
Spotify-changing operations are still executed by that tested command-line manager,
while local curation and dashboard state are presented natively in Tkinter.

No third-party Python packages are required.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_DIR = Path(__file__).resolve().parent
MANAGER = APP_DIR / "salsa_spotify_manager.py"
MASTER_CSV = APP_DIR / "salsa_social_master.csv"
CONFIG_FILE = APP_DIR / "salsa_master_config.json"
CACHE_FILE = APP_DIR / "salsa_master_state_cache.json"
QUOTA_FILE = APP_DIR / "spotify_quota_status.json"
CLIENT_ID_FILE = APP_DIR / "spotify_client_id.txt"
INCOMING_DIR = APP_DIR / "incoming_reviews"
MASTER_HISTORY_DIR = APP_DIR / "master_history"
BACKUP_DIR = APP_DIR / "backups"
LOG_DIR = APP_DIR / "logs"
CHOSIC_ANALYZER_URL = "https://www.chosic.com/spotify-playlist-analyzer/"

V2_BASELINE = 492
ELASTIC_TARGET = 25
ELASTIC_MIN = 23
ELASTIC_MAX = 27

# Palette: intentionally calm and readable, with Spotify green reserved for live actions.
C = {
    "bg": "#F4F6F8",
    "surface": "#FFFFFF",
    "surface2": "#F8FAFB",
    "border": "#DDE3E8",
    "text": "#17212B",
    "muted": "#65727E",
    "sidebar": "#17212B",
    "sidebar2": "#22303C",
    "sidebar_text": "#EAF0F4",
    "green": "#1DB954",
    "green_dark": "#168B40",
    "blue": "#276EF1",
    "amber": "#B26A00",
    "amber_bg": "#FFF4DD",
    "red": "#B42318",
    "red_bg": "#FDECEC",
    "good_bg": "#EAF8EF",
    "blue_bg": "#EDF4FF",
    "queue": "#FFF7E8",
}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def fmt_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%d %b %Y, %H:%M")
    except Exception:
        return str(value)


def is_main(row: dict) -> bool:
    return (row.get("Section") or "").strip().lower() in (
        "main rotation", "main", "active", "active rotation"
    )


def set_number(row: dict):
    try:
        return int(float((row.get("Set") or "").strip()))
    except Exception:
        return None


def load_master_raw() -> tuple[list[str], list[dict]]:
    if not MASTER_CSV.exists():
        return [], []
    with MASTER_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def target_signature(rows: list[dict]) -> str:
    payload = "\n".join((r.get("Spotify URI") or "").strip() for r in rows).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def master_stats(rows: list[dict]) -> dict:
    main = [r for r in rows if is_main(r)]
    reserve = [r for r in rows if not is_main(r)]
    by_set: OrderedDict[int, list] = OrderedDict()
    unassigned = 0
    for r in main:
        sn = set_number(r)
        if sn is None:
            unassigned += 1
        else:
            by_set.setdefault(sn, []).append(r)
    details = []
    for sn, group in by_set.items():
        n = len(group)
        status = "Balanced" if n == 25 else "Elastic OK" if ELASTIC_MIN <= n <= ELASTIC_MAX else "Review"
        details.append((sn, n, status))
    review = [d for d in details if d[2] == "Review"]
    active = len(main)
    next_multiple = ((active + 24) // 25) * 25 if active else 25
    return {
        "total": len(rows),
        "main": active,
        "reserve": len(reserve),
        "sets": len(by_set),
        "details": details,
        "review": review,
        "unassigned": unassigned,
        "next_multiple": next_multiple,
        "to_next": next_multiple - active,
        "growth": len(rows) - V2_BASELINE,
    }


def latest_file(folder: Path, pattern: str) -> Path | None:
    try:
        files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None
    except Exception:
        return None


def latest_plan_summary() -> dict:
    plan = latest_file(INCOMING_DIR, "incoming_auto_plan_*.csv")
    if not plan:
        return {"path": None, "promote": 0, "skip": 0, "rows": []}
    try:
        with plan.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        promote = sum(1 for r in rows if (r.get("Action") or "").strip().lower() == "promote")
        skip = len(rows) - promote
        baseline = next((r.get("Master Baseline Signature", "") for r in rows if r.get("Master Baseline Signature")), "")
        return {"path": plan, "promote": promote, "skip": skip, "rows": rows, "baseline": baseline}
    except Exception:
        return {"path": plan, "promote": 0, "skip": 0, "rows": [], "baseline": ""}


def cache_sync_status(rows: list[dict], config: dict | None, cache: dict | None) -> tuple[str, str, str]:
    if not config or not config.get("playlist_id"):
        return "Master not adopted", "Adopt the existing public Spotify playlist first.", "warn"
    if not cache or cache.get("playlist_id") != config.get("playlist_id"):
        return "No usable cache", "Refresh live Spotify state or re-adopt the master.", "warn"
    tgt = [(r.get("Spotify URI") or "").strip() for r in rows]
    cur = [(r.get("uri") or "").strip() for r in cache.get("items", [])]
    if cur == tgt:
        return "Matches cached Spotify state", f"{len(cur)} tracks in the same Custom order.", "good"
    add = sum((Counter(tgt) - Counter(cur)).values())
    remove = sum((Counter(cur) - Counter(tgt)).values())
    if add or remove:
        return "Local changes pending", f"Cached comparison: +{add} / −{remove}; order may also differ.", "info"
    return "Reorder pending", "Track membership matches, but the Custom order differs.", "info"


def open_path(path: Path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        messagebox.showerror("Could not open", f"Could not open:\n{path}\n\n{e}")


class Card(tk.Frame):
    def __init__(self, parent, title: str, **kwargs):
        super().__init__(parent, bg=C["surface"], highlightthickness=1, highlightbackground=C["border"], **kwargs)
        self.columnconfigure(0, weight=1)
        tk.Label(self, text=title, bg=C["surface"], fg=C["muted"], font=("Segoe UI", 9, "bold"), anchor="w").grid(
            row=0, column=0, sticky="ew", padx=16, pady=(14, 4)
        )
        self.value = tk.Label(self, text="—", bg=C["surface"], fg=C["text"], font=("Segoe UI Semibold", 16), anchor="w")
        self.value.grid(row=1, column=0, sticky="ew", padx=16)
        self.detail = tk.Label(self, text="", bg=C["surface"], fg=C["muted"], font=("Segoe UI", 9), anchor="w", justify="left", wraplength=320)
        self.detail.grid(row=2, column=0, sticky="ew", padx=16, pady=(4, 14))

    def set(self, value: str, detail: str = "", tone: str = "normal"):
        colors = {"good": C["green_dark"], "warn": C["amber"], "bad": C["red"], "info": C["blue"], "normal": C["text"]}
        self.value.configure(text=value, fg=colors.get(tone, C["text"]))
        self.detail.configure(text=detail)


class SalsaManagerUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Salsa Social – Structured | Spotify Manager")
        self.geometry("1240x820")
        self.minsize(1040, 680)
        self.configure(bg=C["bg"])
        self.option_add("*tearOff", False)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Keep the user-facing folders available even on a fresh extraction.
        for folder in (INCOMING_DIR, MASTER_HISTORY_DIR, BACKUP_DIR, LOG_DIR):
            folder.mkdir(parents=True, exist_ok=True)

        self.process = None
        self.process_thread = None
        self.output_queue: queue.Queue = queue.Queue()
        self.busy = False
        self.pending_refresh = False
        self.queued_track_actions: dict[int, dict] = {}
        self.incoming_file = tk.StringVar()
        self.master_search = tk.StringVar()
        self.master_filter = tk.StringVar(value="All")
        self.status_text = tk.StringVar(value="Ready")
        self.nav_buttons = {}
        self.pages = {}
        self.current_page = None

        self._setup_style()
        self._build_shell()
        self._build_pages()
        self.show_page("overview")
        self.after(100, self._poll_output)
        self.after(250, self.refresh_all)

    # ---------- styling / shell ----------
    def _setup_style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("Treeview", rowheight=28, font=("Segoe UI", 9), background=C["surface"], fieldbackground=C["surface"], foreground=C["text"], borderwidth=0)
        s.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), background="#EEF2F5", foreground=C["text"], relief="flat")
        s.map("Treeview", background=[("selected", "#DCE8FF")], foreground=[("selected", C["text"])])
        s.configure("TCombobox", padding=5)

    def _build_shell(self):
        self.sidebar = tk.Frame(self, bg=C["sidebar"], width=224)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=C["sidebar"])
        brand.pack(fill="x", padx=18, pady=(20, 22))
        tk.Label(brand, text="SALSA", bg=C["sidebar"], fg="#F2C14E", font=("Segoe UI Black", 17)).pack(anchor="w")
        tk.Label(brand, text="Spotify Manager", bg=C["sidebar"], fg=C["sidebar_text"], font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(brand, text="Structured master playlist", bg=C["sidebar"], fg="#9FB0BC", font=("Segoe UI", 8)).pack(anchor="w", pady=(2, 0))

        nav = [
            ("overview", "⌂  Overview"),
            ("incoming", "＋  Incoming"),
            ("tracks", "≡  Master Tracks"),
            ("sync", "↻  Spotify Sync"),
            ("maintenance", "⚙  Maintenance"),
            ("activity", "▤  Activity"),
        ]
        for key, label in nav:
            b = tk.Button(self.sidebar, text=label, command=lambda k=key: self.show_page(k), anchor="w", relief="flat", bd=0,
                          bg=C["sidebar"], fg=C["sidebar_text"], activebackground=C["sidebar2"], activeforeground="white",
                          font=("Segoe UI", 10), padx=18, pady=10, cursor="hand2")
            b.pack(fill="x", padx=8, pady=2)
            self.nav_buttons[key] = b

        tk.Frame(self.sidebar, bg="#2B3945", height=1).pack(fill="x", padx=16, pady=(18, 12))
        self.sidebar_master = tk.Label(self.sidebar, text="Master: checking…", bg=C["sidebar"], fg="#A9BBC6", font=("Segoe UI", 8), justify="left", wraplength=188)
        self.sidebar_master.pack(fill="x", padx=18, anchor="w")
        self.sidebar_count = tk.Label(self.sidebar, text="", bg=C["sidebar"], fg="#A9BBC6", font=("Segoe UI", 8), justify="left")
        self.sidebar_count.pack(fill="x", padx=18, pady=(5, 0), anchor="w")

        self.main = tk.Frame(self, bg=C["bg"])
        self.main.pack(side="left", fill="both", expand=True)

        self.page_host = tk.Frame(self.main, bg=C["bg"])
        self.page_host.pack(fill="both", expand=True)

        status = tk.Frame(self.main, bg="#E9EDF0", height=28)
        status.pack(fill="x", side="bottom")
        status.pack_propagate(False)
        self.status_dot = tk.Label(status, text="●", bg="#E9EDF0", fg=C["green_dark"], font=("Segoe UI", 8))
        self.status_dot.pack(side="left", padx=(12, 6))
        tk.Label(status, textvariable=self.status_text, bg="#E9EDF0", fg=C["muted"], font=("Segoe UI", 8)).pack(side="left")
        tk.Button(status, text="Activity", command=lambda: self.show_page("activity"), bg="#E9EDF0", fg=C["blue"], relief="flat", bd=0, font=("Segoe UI", 8), cursor="hand2").pack(side="right", padx=10)

    def _page_frame(self, key, title, subtitle):
        f = tk.Frame(self.page_host, bg=C["bg"])
        self.pages[key] = f
        head = tk.Frame(f, bg=C["bg"])
        head.pack(fill="x", padx=28, pady=(24, 14))
        tk.Label(head, text=title, bg=C["bg"], fg=C["text"], font=("Segoe UI Semibold", 22)).pack(anchor="w")
        tk.Label(head, text=subtitle, bg=C["bg"], fg=C["muted"], font=("Segoe UI", 10), wraplength=900, justify="left").pack(anchor="w", pady=(3, 0))
        return f

    def _build_pages(self):
        self._build_overview()
        self._build_incoming()
        self._build_tracks()
        self._build_sync()
        self._build_maintenance()
        self._build_activity()

    def show_page(self, key):
        if self.current_page:
            self.pages[self.current_page].pack_forget()
            self.nav_buttons[self.current_page].configure(bg=C["sidebar"])
        self.current_page = key
        self.pages[key].pack(fill="both", expand=True)
        self.nav_buttons[key].configure(bg=C["sidebar2"])
        if key == "tracks":
            self.refresh_track_table()
        elif key == "incoming":
            self.refresh_incoming_panel()
        elif key == "overview":
            self.refresh_overview()
        elif key == "sync":
            self.refresh_sync_panel()
        elif key == "maintenance":
            self.refresh_maintenance()

    # ---------- common UI helpers ----------
    def section(self, parent, title, subtitle=None):
        box = tk.Frame(parent, bg=C["surface"], highlightbackground=C["border"], highlightthickness=1)
        head = tk.Frame(box, bg=C["surface"])
        head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(head, text=title, bg=C["surface"], fg=C["text"], font=("Segoe UI", 11, "bold")).pack(anchor="w")
        if subtitle:
            tk.Label(head, text=subtitle, bg=C["surface"], fg=C["muted"], font=("Segoe UI", 9), wraplength=900, justify="left").pack(anchor="w", pady=(2, 0))
        return box

    def action_button(self, parent, text, command, kind="secondary", width=None):
        if kind == "primary":
            bg, fg, active = C["blue"], "white", "#1F5DCB"
        elif kind == "spotify":
            bg, fg, active = C["green"], "#07180D", "#18A64B"
        elif kind == "danger":
            bg, fg, active = C["red"], "white", "#8F1B13"
        elif kind == "warning":
            bg, fg, active = "#F2B84B", C["text"], "#D99D2D"
        else:
            bg, fg, active = "#EEF2F5", C["text"], "#E0E6EA"
        return tk.Button(parent, text=text, command=command, relief="flat", bd=0, bg=bg, fg=fg,
                         activebackground=active, activeforeground=fg, font=("Segoe UI", 9, "bold"),
                         padx=13, pady=8, cursor="hand2", width=width)

    # ---------- Overview ----------
    def _build_overview(self):
        f = self._page_frame("overview", "Overview", "At-a-glance state of the canonical master, the adopted Spotify playlist, cache, quota and recommended next action.")
        body = tk.Frame(f, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        for c in range(2): body.columnconfigure(c, weight=1, uniform="cards")

        self.card_master = Card(body, "SPOTIFY MASTER")
        self.card_master.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=(0, 12))
        self.card_local = Card(body, "CANONICAL MASTER")
        self.card_local.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=(0, 12))
        self.card_sync = Card(body, "SYNC STATUS")
        self.card_sync.grid(row=1, column=0, sticky="nsew", padx=(0, 7), pady=(0, 12))
        self.card_quota = Card(body, "SPOTIFY QUOTA")
        self.card_quota.grid(row=1, column=1, sticky="nsew", padx=(7, 0), pady=(0, 12))

        workflow = self.section(body, "Recommended next step")
        workflow.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(2, 12))
        wfinner = tk.Frame(workflow, bg=C["surface"])
        wfinner.pack(fill="x", padx=16, pady=(0, 15))
        self.next_title = tk.Label(wfinner, text="Checking…", bg=C["surface"], fg=C["blue"], font=("Segoe UI", 13, "bold"), anchor="w")
        self.next_title.pack(fill="x")
        self.next_detail = tk.Label(wfinner, text="", bg=C["surface"], fg=C["muted"], font=("Segoe UI", 9), anchor="w", justify="left", wraplength=850)
        self.next_detail.pack(fill="x", pady=(3, 9))
        self.next_button = self.action_button(wfinner, "Open", lambda: None, "primary")
        self.next_button.pack(anchor="w")

        quick = tk.Frame(body, bg=C["bg"])
        quick.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.action_button(quick, "Refresh dashboard", self.refresh_all).pack(side="left", padx=(0, 8))
        self.action_button(quick, "Open master on Spotify", self.open_spotify_master).pack(side="left", padx=(0, 8))
        self.action_button(quick, "Open application folder", lambda: open_path(APP_DIR)).pack(side="left", padx=(0, 8))
        self.action_button(quick, "Open master CSV", lambda: open_path(MASTER_CSV)).pack(side="left")

    def refresh_overview(self):
        fields, rows = load_master_raw()
        stats = master_stats(rows)
        cfg = read_json(CONFIG_FILE, {}) or {}
        cache = read_json(CACHE_FILE, {}) or {}
        quota = read_json(QUOTA_FILE, {}) or {}

        name = cfg.get("playlist_name") or "Not adopted"
        public = cfg.get("public")
        detail = cfg.get("playlist_url") or "Use Maintenance → Adopt Existing Master."
        if public is False:
            detail = "Private playlist • " + detail
        self.card_master.set(name, detail, "good" if cfg.get("playlist_id") else "warn")

        self.card_local.set(f"{stats['total']} tracks", f"{stats['main']} Main Rotation • {stats['reserve']} Reserve • {stats['sets']} sets • growth {stats['growth']:+d}", "good" if not stats['review'] else "warn")

        st, sd, tone = cache_sync_status(rows, cfg, cache)
        self.card_sync.set(st, sd, tone)

        if quota and quota.get("safe_retry_utc"):
            try:
                safe = datetime.fromisoformat(quota["safe_retry_utc"])
                if safe.tzinfo is None: safe = safe.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < safe:
                    self.card_quota.set("Temporarily locked", quota.get("safe_retry_uk") or fmt_time(quota.get("safe_retry_utc")), "warn")
                else:
                    self.card_quota.set("Retry window passed", "The saved lock has expired; a successful Spotify call will clear it.", "good")
            except Exception:
                self.card_quota.set("Saved quota status", quota.get("safe_retry_uk") or "See Maintenance", "warn")
        else:
            self.card_quota.set("No saved lock", "No active QUOTA_EXCEEDED status is recorded.", "good")

        self.sidebar_master.configure(text=f"Master: {name}")
        self.sidebar_count.configure(text=f"Canonical: {stats['total']} tracks")

        # Dynamic next action.
        if not cfg.get("playlist_id"):
            title = "Adopt your existing shared Spotify playlist"
            detail = "The UI needs a saved permanent master playlist ID before live sync operations can run."
            cmd = lambda: self.show_page("maintenance")
            label = "Go to Maintenance"
        elif not CACHE_FILE.exists():
            title = "Refresh live Spotify state"
            detail = "There is no state cache yet. A live refresh establishes the current Spotify baseline."
            cmd = self.refresh_live
            label = "Refresh & Verify"
        else:
            state, _, _ = cache_sync_status(rows, cfg, cache)
            if state == "Matches cached Spotify state":
                plan = latest_plan_summary()
                title = "Master is aligned with the cached Spotify state"
                detail = "Add new discoveries through Incoming, or remove/rest tracks from Master Tracks."
                cmd = lambda: self.show_page("incoming")
                label = "Open Incoming"
            else:
                title = "Local master has changes waiting for Spotify"
                detail = "Preview the difference from cache first, then apply Preserve Sync when the preview looks correct."
                cmd = lambda: self.show_page("sync")
                label = "Go to Spotify Sync"
        self.next_title.configure(text=title)
        self.next_detail.configure(text=detail)
        self.next_button.configure(text=label, command=cmd)

    # ---------- Incoming ----------
    def _build_incoming(self):
        f = self._page_frame("incoming", "Incoming", "Export your ‘Salsa - Incoming’ playlist through Chosic. Every genuinely new track in that file is promoted automatically; the manager chooses Style, Floor Role, elastic Set and insertion position.")
        body = tk.Frame(f, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(0, 24))

        select = self.section(body, "1  Select Chosic Incoming CSV", "Already-present tracks and duplicate Incoming rows are skipped automatically. No Reject category is used.")
        select.pack(fill="x", pady=(0, 12))

        chosic = tk.Frame(select, bg=C["surface2"], highlightbackground=C["border"], highlightthickness=1)
        chosic.pack(fill="x", padx=16, pady=(0, 10))
        tk.Label(chosic, text="Need a fresh Incoming export? Open Chosic, analyse ‘Salsa - Incoming’, then export the results to CSV.",
                 bg=C["surface2"], fg=C["muted"], font=("Segoe UI", 9), anchor="w").pack(side="left", fill="x", expand=True, padx=10, pady=9)
        self.action_button(chosic, "Open Chosic Playlist Analyzer ↗", self.open_chosic_analyzer).pack(side="right", padx=8, pady=6)

        row = tk.Frame(select, bg=C["surface"])
        row.pack(fill="x", padx=16, pady=(0, 15))
        entry = tk.Entry(row, textvariable=self.incoming_file, font=("Segoe UI", 9), relief="solid", bd=1)
        entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.action_button(row, "Browse…", self.browse_incoming).pack(side="left", padx=(8, 0))
        self.action_button(row, "Prepare automatic plan", self.prepare_incoming, "primary").pack(side="left", padx=(8, 0))

        planbox = self.section(body, "2  Automatic placement plan", "The plan is a report, not a worksheet you need to curate.")
        planbox.pack(fill="both", expand=True, pady=(0, 12))
        top = tk.Frame(planbox, bg=C["surface"])
        top.pack(fill="x", padx=16, pady=(0, 8))
        self.plan_status = tk.Label(top, text="No plan yet", bg=C["surface"], fg=C["muted"], font=("Segoe UI", 10, "bold"), anchor="w")
        self.plan_status.pack(side="left", fill="x", expand=True)
        self.action_button(top, "Open plan CSV", self.open_latest_plan).pack(side="right")

        cols = ("track", "artist", "set", "role", "style", "position")
        self.plan_tree = ttk.Treeview(planbox, columns=cols, show="headings", height=8)
        heads = [("track", "Track", 210), ("artist", "Artist", 170), ("set", "Set", 55), ("role", "Floor Role", 90), ("style", "Style", 145), ("position", "Final #", 65)]
        for key, text, w in heads:
            self.plan_tree.heading(key, text=text)
            self.plan_tree.column(key, width=w, minwidth=45, anchor="w" if key not in ("set", "position") else "center")
        self.plan_tree.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        actions = tk.Frame(planbox, bg=C["surface"])
        actions.pack(fill="x", padx=16, pady=(0, 15))
        self.apply_plan_btn = self.action_button(actions, "Apply latest plan to local master", self.apply_incoming_plan, "primary")
        self.apply_plan_btn.pack(side="left")
        tk.Label(actions, text="Zero Spotify API calls", bg=C["surface"], fg=C["muted"], font=("Segoe UI", 8)).pack(side="left", padx=10)
        self.action_button(actions, "Continue to Spotify Sync →", lambda: self.show_page("sync")).pack(side="right")

    def open_chosic_analyzer(self):
        try:
            opened = webbrowser.open(CHOSIC_ANALYZER_URL)
            if opened is False:
                raise RuntimeError("The default browser did not accept the request.")
        except Exception as exc:
            messagebox.showerror(
                "Could not open Chosic",
                f"The Chosic Spotify Playlist Analyzer could not be opened automatically.\n\n{CHOSIC_ANALYZER_URL}\n\n{exc}",
            )

    def browse_incoming(self):
        path = filedialog.askopenfilename(title="Select Chosic export of Salsa - Incoming", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.incoming_file.set(path)

    def prepare_incoming(self):
        raw = self.incoming_file.get().strip()
        if not raw:
            self.browse_incoming()
            raw = self.incoming_file.get().strip()
        if not raw:
            return
        if not Path(raw).exists():
            messagebox.showerror("File not found", "The selected Incoming CSV does not exist.")
            return
        self.run_manager(["prepare-incoming", raw], "Preparing automatic Incoming placement plan", on_done=self.refresh_incoming_panel)

    def apply_incoming_plan(self):
        plan = latest_plan_summary()
        if not plan.get("path"):
            messagebox.showinfo("No plan", "Prepare an Incoming automatic placement plan first.")
            return
        if plan.get("promote", 0) == 0:
            messagebox.showinfo("Nothing to promote", "The latest plan contains no genuinely new tracks to promote.")
            return
        msg = (f"Apply the latest automatic plan to salsa_social_master.csv?\n\n"
               f"Tracks to promote: {plan['promote']}\n"
               f"Skipped: {plan['skip']}\n\n"
               "A backup of the current canonical master will be created. Spotify will not be contacted.")
        if not messagebox.askyesno("Apply Incoming plan", msg, icon="question"):
            return
        self.run_manager(["apply-incoming", str(plan["path"])], "Applying Incoming plan to local master", stdin_lines=["APPLY"], on_done=self.refresh_all)

    def refresh_incoming_panel(self):
        if not hasattr(self, "plan_tree"):
            return
        plan = latest_plan_summary()
        self.plan_tree.delete(*self.plan_tree.get_children())
        p = plan.get("path")
        if not p:
            self.plan_status.configure(text="No automatic plan has been created yet.", fg=C["muted"])
            return
        _, master_rows = load_master_raw()
        current_sig = target_signature(master_rows) if master_rows else ""
        baseline = plan.get("baseline") or ""
        if baseline and baseline != current_sig:
            state = "Master changed since this plan was prepared"
            color = C["amber"]
        else:
            state = "Ready to apply"
            color = C["green_dark"]
        stamp = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %Y, %H:%M")
        self.plan_status.configure(text=f"{p.name}  •  {plan['promote']} promote  •  {plan['skip']} skip  •  {state}  •  {stamp}", fg=color)
        for r in plan.get("rows", []):
            if (r.get("Action") or "").lower() != "promote":
                continue
            song = r.get("Song") or r.get("Track") or r.get("Name") or ""
            artist = r.get("Artist") or r.get("Artists") or ""
            self.plan_tree.insert("", "end", values=(song, artist, r.get("Auto Set", ""), r.get("Auto Floor Role", ""), r.get("Auto Style", ""), r.get("Planned Final Position", "")))

    def open_latest_plan(self):
        p = latest_plan_summary().get("path")
        if p:
            open_path(p)
        else:
            messagebox.showinfo("No plan", "No Incoming automatic plan exists yet.")

    # ---------- Master Tracks ----------
    def _build_tracks(self):
        f = self._page_frame("tracks", "Master Tracks", "Search the canonical master and queue tracks for removal or rest to Reserve. Changes are local first; Spotify is updated later through Preserve Sync.")
        body = tk.Frame(f, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(0, 24))

        bar = tk.Frame(body, bg=C["bg"])
        bar.pack(fill="x", pady=(0, 10))
        search = tk.Entry(bar, textvariable=self.master_search, font=("Segoe UI", 10), relief="solid", bd=1)
        search.pack(side="left", fill="x", expand=True, ipady=7)
        search.bind("<KeyRelease>", lambda e: self.refresh_track_table())
        combo = ttk.Combobox(bar, textvariable=self.master_filter, values=["All", "Main Rotation", "Reserve / Specialist"], state="readonly", width=20)
        combo.pack(side="left", padx=(8, 0))
        combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_track_table())
        self.action_button(bar, "Open master CSV", lambda: open_path(MASTER_CSV)).pack(side="left", padx=(8, 0))

        tablebox = tk.Frame(body, bg=C["surface"], highlightbackground=C["border"], highlightthickness=1)
        tablebox.pack(fill="both", expand=True)
        cols = ("pos", "song", "artist", "section", "set", "role", "style", "queued")
        self.track_tree = ttk.Treeview(tablebox, columns=cols, show="headings", selectmode="extended")
        specs = [
            ("pos", "#", 55, "center"), ("song", "Song", 220, "w"), ("artist", "Artist", 170, "w"),
            ("section", "Section", 120, "w"), ("set", "Set", 55, "center"), ("role", "Floor Role", 90, "w"),
            ("style", "Style", 135, "w"), ("queued", "Queued", 75, "center")
        ]
        for key, title, width, anchor in specs:
            self.track_tree.heading(key, text=title)
            self.track_tree.column(key, width=width, minwidth=45, anchor=anchor)
        scroll = ttk.Scrollbar(tablebox, orient="vertical", command=self.track_tree.yview)
        self.track_tree.configure(yscrollcommand=scroll.set)
        self.track_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.track_tree.tag_configure("remove", background=C["red_bg"])
        self.track_tree.tag_configure("rest", background=C["queue"])

        actions = tk.Frame(body, bg=C["bg"])
        actions.pack(fill="x", pady=(10, 0))
        self.action_button(actions, "Queue Remove", lambda: self.queue_selected_tracks("remove"), "danger").pack(side="left", padx=(0, 8))
        self.action_button(actions, "Queue Rest to Reserve", lambda: self.queue_selected_tracks("rest"), "warning").pack(side="left", padx=(0, 8))
        self.action_button(actions, "Unqueue selected", self.unqueue_selected_tracks).pack(side="left", padx=(0, 8))
        self.action_button(actions, "Clear queue", self.clear_track_queue).pack(side="left")
        self.apply_queue_btn = self.action_button(actions, "Apply queued changes", self.apply_track_queue, "primary")
        self.apply_queue_btn.pack(side="right")
        self.queue_label = tk.Label(body, text="No queued changes", bg=C["bg"], fg=C["muted"], font=("Segoe UI", 9))
        self.queue_label.pack(fill="x", pady=(7, 0), anchor="w")

    def refresh_track_table(self):
        if not hasattr(self, "track_tree"):
            return
        _, rows = load_master_raw()
        q = self.master_search.get().strip().casefold()
        filt = self.master_filter.get()
        self.track_tree.delete(*self.track_tree.get_children())
        for idx, r in enumerate(rows, 1):
            section = (r.get("Section") or "").strip()
            if filt != "All":
                if filt == "Main Rotation" and not is_main(r):
                    continue
                if filt == "Reserve / Specialist" and is_main(r):
                    continue
            hay = " | ".join([r.get("Song", ""), r.get("Artist", ""), r.get("Spotify URI", ""), r.get("ISRC", ""), str(idx)]).casefold()
            if q and q not in hay:
                continue
            action = self.queued_track_actions.get(idx, {}).get("action", "")
            tag = action if action in ("remove", "rest") else ""
            self.track_tree.insert("", "end", iid=str(idx), values=(
                r.get("New Position") or idx, r.get("Song", ""), r.get("Artist", ""), section,
                r.get("Set", ""), r.get("Floor Role", ""), r.get("Style", ""), action.upper()
            ), tags=(tag,) if tag else ())
        self.update_queue_label()

    def queue_selected_tracks(self, action: str):
        selections = self.track_tree.selection()
        if not selections:
            messagebox.showinfo("Select tracks", "Select one or more tracks first.")
            return
        _, rows = load_master_raw()
        for iid in selections:
            pos = int(iid)
            if not (1 <= pos <= len(rows)):
                continue
            r = rows[pos - 1]
            if action == "rest" and not is_main(r):
                continue
            self.queued_track_actions[pos] = {
                "action": action,
                "uri": (r.get("Spotify URI") or "").strip(),
                "song": r.get("Song", ""),
                "artist": r.get("Artist", ""),
            }
        self.refresh_track_table()

    def unqueue_selected_tracks(self):
        for iid in self.track_tree.selection():
            self.queued_track_actions.pop(int(iid), None)
        self.refresh_track_table()

    def clear_track_queue(self):
        self.queued_track_actions.clear()
        self.refresh_track_table()

    def update_queue_label(self):
        remove = sum(1 for v in self.queued_track_actions.values() if v["action"] == "remove")
        rest = sum(1 for v in self.queued_track_actions.values() if v["action"] == "rest")
        if not (remove or rest):
            text = "No queued changes"
        else:
            text = f"Queued: {remove} remove • {rest} rest to Reserve • Spotify will not be contacted until you later run Preserve Sync."
        self.queue_label.configure(text=text)

    def apply_track_queue(self):
        if not self.queued_track_actions:
            messagebox.showinfo("Nothing queued", "Queue at least one Remove or Rest action first.")
            return
        fields, rows = load_master_raw()
        if not fields or not rows:
            messagebox.showerror("Master unavailable", "Could not read salsa_social_master.csv.")
            return
        # Ensure positions still refer to the same occurrences that were queued.
        stale = []
        for pos, info in self.queued_track_actions.items():
            if pos > len(rows) or (rows[pos - 1].get("Spotify URI") or "").strip() != info["uri"]:
                stale.append(pos)
        if stale:
            self.queued_track_actions.clear()
            self.refresh_track_table()
            messagebox.showwarning("Master changed", "The canonical master changed after tracks were queued. The queue was cleared for safety; please select the tracks again.")
            return

        removes = [(p, v) for p, v in sorted(self.queued_track_actions.items()) if v["action"] == "remove"]
        rests = [(p, v) for p, v in sorted(self.queued_track_actions.items()) if v["action"] == "rest"]
        preview = []
        if removes:
            preview.append("REMOVE COMPLETELY:\n" + "\n".join(f"  #{p}  {v['song']} — {v['artist']}" for p, v in removes[:12]))
            if len(removes) > 12: preview.append(f"  …and {len(removes)-12} more")
        if rests:
            preview.append("REST TO RESERVE:\n" + "\n".join(f"  #{p}  {v['song']} — {v['artist']}" for p, v in rests[:12]))
            if len(rests) > 12: preview.append(f"  …and {len(rests)-12} more")
        preview.append(f"\nMaster size: {len(rows)} → {len(rows)-len(removes)}\n\nA timestamped backup will be created. Spotify will NOT be contacted.")
        if not messagebox.askyesno("Apply local master changes", "\n\n".join(preview), icon="warning"):
            return

        MASTER_HISTORY_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = MASTER_HISTORY_DIR / f"salsa_social_master_before_remove_rest_{stamp}.csv"
        shutil.copy2(MASTER_CSV, backup)

        remaining, rested = [], []
        today = datetime.now().strftime("%Y-%m-%d")
        for pos, r0 in enumerate(rows, 1):
            action = self.queued_track_actions.get(pos, {}).get("action")
            if action == "remove":
                continue
            r = dict(r0)
            if action == "rest":
                r["Section"] = "Reserve / Specialist"
                if "Set" in r: r["Set"] = "Reserve"
                if "Set Name" in r: r["Set Name"] = "Reserve / Specialist"
                if "Slot" in r: r["Slot"] = ""
                if "Floor Role" in r: r["Floor Role"] = "Reserve"
                if "V2 Notes" in r:
                    prior = (r.get("V2 Notes") or "").strip()
                    note = f"Rested from Main Rotation by curator on {today}."
                    r["V2 Notes"] = (prior + " " + note).strip()
                rested.append(r)
            else:
                remaining.append(r)
        new_rows = remaining + rested
        slots = Counter()
        for pos, r in enumerate(new_rows, 1):
            r["New Position"] = str(pos)
            if is_main(r):
                sn = set_number(r)
                if sn is not None:
                    slots[sn] += 1
                    if "Slot" in r: r["Slot"] = str(slots[sn])
                    if "Set Name" in r: r["Set Name"] = f"Social Set {sn:02d}"
            elif "Slot" in r:
                r["Slot"] = ""

        tmp = MASTER_CSV.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader(); w.writerows(new_rows)
        tmp.replace(MASTER_CSV)

        # GUI operation log mirrors the CLI's auditability.
        LOG_DIR.mkdir(exist_ok=True)
        log = LOG_DIR / f"gui_remove-rest_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"
        log.write_text(
            "GUI REMOVE / REST TRACKS FROM LOCAL MASTER\n"
            f"Removed completely: {len(removes)}\nRested to Reserve: {len(rests)}\n"
            f"Master size: {len(rows)} -> {len(new_rows)}\nBackup: {backup}\nUpdated: {MASTER_CSV}\n"
            "Spotify contacted: No\n",
            encoding="utf-8"
        )
        self.append_activity(f"\nLOCAL MASTER UPDATED\nRemoved: {len(removes)} | Rested: {len(rests)} | New size: {len(new_rows)}\nBackup: {backup}\nNext: Validate → Preview → Preserve Sync\n")
        self.queued_track_actions.clear()
        self.refresh_all()
        messagebox.showinfo("Local master updated", f"Removed: {len(removes)}\nRested to Reserve: {len(rests)}\nNew master size: {len(new_rows)}\n\nSpotify has not been changed. Next: Validate → Preview → Preserve Sync.")

    # ---------- Spotify Sync ----------
    def _build_sync(self):
        f = self._page_frame("sync", "Spotify Sync", "Safely reconcile the canonical CSV with the permanent Spotify master. The recommended path is Validate → Preview from Cache → Preserve Sync → optional Live Verify.")
        body = tk.Frame(f, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(0, 24))

        self.sync_banner = tk.Frame(body, bg=C["blue_bg"], highlightbackground="#C9DCF9", highlightthickness=1)
        self.sync_banner.pack(fill="x", pady=(0, 12))
        self.sync_banner_title = tk.Label(self.sync_banner, text="Checking sync state…", bg=C["blue_bg"], fg=C["blue"], font=("Segoe UI", 12, "bold"), anchor="w")
        self.sync_banner_title.pack(fill="x", padx=16, pady=(12, 2))
        self.sync_banner_detail = tk.Label(self.sync_banner, text="", bg=C["blue_bg"], fg=C["muted"], font=("Segoe UI", 9), anchor="w", justify="left")
        self.sync_banner_detail.pack(fill="x", padx=16, pady=(0, 12))

        steps = self.section(body, "Recommended workflow")
        steps.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(steps, bg=C["surface"])
        inner.pack(fill="x", padx=16, pady=(0, 15))
        items = [
            ("1", "Validate local master", "Checks positions, Spotify URIs and elastic-set health. Zero API calls.", self.validate_master, "secondary"),
            ("2", "Preview from cache", "Shows additions, removals and reorder work. Zero API calls.", self.preview_cache, "primary"),
            ("3", "Apply Preserve Sync", "Updates the actual Spotify master while preserving existing memberships/Added At where possible.", self.sync_preserve, "spotify"),
            ("4", "Refresh & Verify Live", "Optional live audit after sync. Reads Spotify and refreshes the cache.", self.refresh_live, "secondary"),
        ]
        for i, (num, title, desc, cmd, kind) in enumerate(items):
            r = tk.Frame(inner, bg=C["surface"])
            r.pack(fill="x", pady=4)
            tk.Label(r, text=num, bg="#E8EDF1", fg=C["text"], font=("Segoe UI", 9, "bold"), width=3, pady=5).pack(side="left")
            text = tk.Frame(r, bg=C["surface"])
            text.pack(side="left", fill="x", expand=True, padx=10)
            tk.Label(text, text=title, bg=C["surface"], fg=C["text"], font=("Segoe UI", 10, "bold")).pack(anchor="w")
            tk.Label(text, text=desc, bg=C["surface"], fg=C["muted"], font=("Segoe UI", 8), wraplength=650, justify="left").pack(anchor="w")
            self.action_button(r, title if i != 2 else "SYNC LIVE", cmd, kind).pack(side="right")

        advanced = self.section(body, "Advanced / recovery actions", "These are not part of the normal maintenance path.")
        advanced.pack(fill="x")
        ai = tk.Frame(advanced, bg=C["surface"])
        ai.pack(fill="x", padx=16, pady=(0, 15))
        self.action_button(ai, "Backup current Spotify master", self.backup_live).pack(side="left", padx=(0, 8))
        self.action_button(ai, "Emergency Fast Rebuild", self.fast_rebuild, "danger").pack(side="left", padx=(0, 8))
        self.action_button(ai, "Open backups", lambda: open_path(BACKUP_DIR)).pack(side="left")

    def refresh_sync_panel(self):
        if not hasattr(self, "sync_banner_title"):
            return
        _, rows = load_master_raw()
        cfg = read_json(CONFIG_FILE, {}) or {}
        cache = read_json(CACHE_FILE, {}) or {}
        state, detail, tone = cache_sync_status(rows, cfg, cache)
        colors = {"good": C["green_dark"], "warn": C["amber"], "info": C["blue"], "bad": C["red"]}
        self.sync_banner_title.configure(text=state, fg=colors.get(tone, C["blue"]))
        self.sync_banner_detail.configure(text=detail)

    def validate_master(self):
        self.run_manager(["validate"], "Validating canonical master", on_done=self.refresh_all)

    def preview_cache(self):
        self.run_manager(["preview", "--offline"], "Previewing from cache (zero Spotify calls)")

    def sync_preserve(self):
        if not self.ensure_spotify_ready():
            return
        _, rows = load_master_raw()
        cfg = read_json(CONFIG_FILE, {}) or {}
        cache = read_json(CACHE_FILE, {}) or {}
        state, detail, _ = cache_sync_status(rows, cfg, cache)
        msg = ("Apply the canonical master to the LIVE Spotify playlist using Preserve mode?\n\n"
               f"Current cached status: {state}\n{detail}\n\n"
               "The manager creates a Spotify backup before mutations and checkpoints each successful change. The public playlist ID/link stays the same.")
        if not messagebox.askyesno("Apply Preserve Sync", msg, icon="warning"):
            return
        self.run_manager(["sync", "--mode", "preserve"], "Applying Preserve Sync to Spotify", stdin_lines=["SYNC"], spotify=True, on_done=self.refresh_all)

    def refresh_live(self):
        if not self.ensure_spotify_ready():
            return
        if not messagebox.askyesno("Refresh live Spotify state", "Read the complete live master playlist from Spotify and compare it with the canonical CSV?\n\nThis uses Spotify Web API quota but makes no playlist changes."):
            return
        self.run_manager(["refresh-state"], "Refreshing and verifying live Spotify state", spotify=True, on_done=self.refresh_all)

    def backup_live(self):
        if not self.ensure_spotify_ready():
            return
        self.run_manager(["backup"], "Backing up current Spotify master", spotify=True, on_done=self.refresh_all)

    def fast_rebuild(self):
        if not self.ensure_spotify_ready():
            return
        phrase = simpledialog.askstring("Emergency Fast Rebuild", "This keeps the same playlist ID/link but wholesale-replaces contents and resets live Added At history.\n\nType RESET-DATES to continue:", parent=self)
        if phrase != "RESET-DATES":
            return
        if not messagebox.askyesno("Final confirmation", "Proceed with Emergency Fast Rebuild of the LIVE Spotify master?", icon="warning"):
            return
        self.run_manager(["sync", "--mode", "fast"], "Emergency Fast Rebuild", stdin_lines=["SYNC", "RESET-DATES"], spotify=True, on_done=self.refresh_all)

    # ---------- Maintenance ----------
    def _build_maintenance(self):
        f = self._page_frame("maintenance", "Maintenance", "Master identity, Spotify setup, diagnostics, backups and advanced one-off operations.")
        canvas = tk.Canvas(f, bg=C["bg"], highlightthickness=0)
        scroll = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=C["bg"])
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(28, 0), pady=(0, 24))
        scroll.pack(side="right", fill="y", padx=(0, 8), pady=(0, 24))
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        master = self.section(inner, "Permanent Spotify master", "The saved playlist ID is the destination used by live sync commands.")
        master.pack(fill="x", pady=(0, 12))
        mi = tk.Frame(master, bg=C["surface"])
        mi.pack(fill="x", padx=16, pady=(0, 15))
        self.maint_master = tk.Label(mi, text="Checking…", bg=C["surface"], fg=C["text"], font=("Segoe UI", 10), justify="left", anchor="w")
        self.maint_master.pack(fill="x", pady=(0, 8))
        self.action_button(mi, "Adopt existing shared playlist", self.adopt_master, "primary").pack(side="left", padx=(0, 8))
        self.action_button(mi, "Open on Spotify", self.open_spotify_master).pack(side="left", padx=(0, 8))
        self.action_button(mi, "Show saved config", lambda: self.run_manager(["show-config"], "Showing saved master config")).pack(side="left")

        setup = self.section(inner, "Spotify Developer setup", "The Client ID is not a secret. OAuth opens Spotify in your browser when a live command needs authorization.")
        setup.pack(fill="x", pady=(0, 12))
        si = tk.Frame(setup, bg=C["surface"])
        si.pack(fill="x", padx=16, pady=(0, 15))
        self.client_status = tk.Label(si, text="", bg=C["surface"], fg=C["muted"], font=("Segoe UI", 9))
        self.client_status.pack(side="left")
        self.action_button(si, "Set / change Client ID", self.set_client_id).pack(side="right")

        diagnostics = self.section(inner, "Local diagnostics", "Safe checks that do not modify Spotify.")
        diagnostics.pack(fill="x", pady=(0, 12))
        di = tk.Frame(diagnostics, bg=C["surface"])
        di.pack(fill="x", padx=16, pady=(0, 15))
        self.action_button(di, "Self Test", lambda: self.run_manager(["self-test"], "Running self-test")).pack(side="left", padx=(0, 8))
        self.action_button(di, "Elastic Set Analysis", lambda: self.run_manager(["set-health"], "Analysing elastic set balance")).pack(side="left", padx=(0, 8))
        self.action_button(di, "Quota Status", lambda: self.run_manager(["show-quota"], "Showing quota status")).pack(side="left", padx=(0, 8))
        self.action_button(di, "Open logs", lambda: open_path(LOG_DIR)).pack(side="left")

        files = self.section(inner, "Files & backups")
        files.pack(fill="x", pady=(0, 12))
        fi = tk.Frame(files, bg=C["surface"])
        fi.pack(fill="x", padx=16, pady=(0, 15))
        for text, path in [("Master CSV", MASTER_CSV), ("Master history", MASTER_HISTORY_DIR), ("Spotify backups", BACKUP_DIR), ("Incoming plans", INCOMING_DIR), ("Application folder", APP_DIR)]:
            self.action_button(fi, text, lambda p=path: open_path(p)).pack(side="left", padx=(0, 7))

        danger = self.section(inner, "One-off / specialist operations", "Normally leave these alone once the permanent master is established.")
        danger.pack(fill="x", pady=(0, 12))
        dai = tk.Frame(danger, bg=C["surface"])
        dai.pack(fill="x", padx=16, pady=(0, 15))
        self.action_button(dai, "Create brand-new public master", self.create_new_master, "danger").pack(side="left", padx=(0, 8))
        self.action_button(dai, "Chronology Build", self.chronology_build, "warning").pack(side="left")

    def refresh_maintenance(self):
        if not hasattr(self, "maint_master"):
            return
        cfg = read_json(CONFIG_FILE, {}) or {}
        if cfg.get("playlist_id"):
            self.maint_master.configure(text=f"{cfg.get('playlist_name') or '(unnamed)'}\n{cfg.get('playlist_url') or ''}\nPlaylist ID: {cfg.get('playlist_id')}\nSaved: {fmt_time(cfg.get('saved_at'))}")
        else:
            self.maint_master.configure(text="No permanent Spotify master has been adopted yet.")
        cid = CLIENT_ID_FILE.read_text(encoding="utf-8").strip() if CLIENT_ID_FILE.exists() else ""
        self.client_status.configure(text="Client ID saved" if cid else "Client ID not yet saved", fg=C["green_dark"] if cid else C["amber"])

    def ensure_spotify_ready(self) -> bool:
        cfg = read_json(CONFIG_FILE, {}) or {}
        if not cfg.get("playlist_id"):
            messagebox.showwarning("No Spotify master", "Adopt your existing shared Spotify master first in Maintenance.")
            self.show_page("maintenance")
            return False
        if not CLIENT_ID_FILE.exists() or not CLIENT_ID_FILE.read_text(encoding="utf-8").strip():
            return self.set_client_id()
        return True

    def set_client_id(self):
        current = CLIENT_ID_FILE.read_text(encoding="utf-8").strip() if CLIENT_ID_FILE.exists() else ""
        cid = simpledialog.askstring("Spotify Client ID", "Paste the Client ID from your Spotify Developer Dashboard app.\n\nRedirect URI must be:\nhttp://127.0.0.1:8888/callback", initialvalue=current, parent=self)
        if cid is None:
            return False
        cid = cid.strip()
        if not cid:
            messagebox.showerror("Client ID required", "The Client ID cannot be blank.")
            return False
        CLIENT_ID_FILE.write_text(cid + "\n", encoding="utf-8")
        self.refresh_maintenance()
        return True

    def adopt_master(self):
        if not CLIENT_ID_FILE.exists() or not CLIENT_ID_FILE.read_text(encoding="utf-8").strip():
            if not self.set_client_id():
                return
        value = simpledialog.askstring("Adopt existing Spotify master", "Paste the ordinary Spotify playlist URL, Spotify URI, or playlist ID to adopt as the permanent master.", parent=self)
        if not value or not value.strip():
            return
        existing = read_json(CONFIG_FILE, {}) or {}
        if existing.get("playlist_id"):
            msg = f"Current saved master:\n{existing.get('playlist_name') or ''}\n{existing.get('playlist_url') or existing.get('playlist_id')}\n\nAdopt the supplied playlist instead if it is different?"
            if not messagebox.askyesno("Replace master reference if needed", msg):
                return
        # ADOPT is consumed only when the CLI sees a different existing playlist.
        self.run_manager(["adopt-master", value.strip()], "Adopting existing shared Spotify master", stdin_lines=["ADOPT"], spotify=True, on_done=self.refresh_all)

    def create_new_master(self):
        if not CLIENT_ID_FILE.exists() or not CLIENT_ID_FILE.read_text(encoding="utf-8").strip():
            if not self.set_client_id(): return
        phrase = simpledialog.askstring("Create brand-new master", "This is a FALLBACK operation and creates a new Spotify playlist ID/share link.\n\nType CREATE to continue:", parent=self)
        if phrase != "CREATE":
            return
        lines = ["CREATE", "BUILD"] if CONFIG_FILE.exists() else ["BUILD"]
        self.run_manager(["create-new-master"], "Creating brand-new public Spotify master", stdin_lines=lines, spotify=True, on_done=self.refresh_all)

    def chronology_build(self):
        if not CLIENT_ID_FILE.exists() or not CLIENT_ID_FILE.read_text(encoding="utf-8").strip():
            if not self.set_client_id(): return
        path = filedialog.askopenfilename(title="Select genuine historical Added At CSV", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        phrase = simpledialog.askstring("Chronology Build", "This is quota-heavy and only useful with genuinely varied historical Added At dates.\n\nType BUILD to continue:", parent=self)
        if phrase != "BUILD":
            return
        lines = ["CREATE", "BUILD"] if CONFIG_FILE.exists() else ["BUILD"]
        self.run_manager(["chronology-build", path], "Building chronology-aware new Spotify master", stdin_lines=lines, spotify=True, on_done=self.refresh_all)

    def open_spotify_master(self):
        cfg = read_json(CONFIG_FILE, {}) or {}
        url = cfg.get("playlist_url")
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("No master saved", "No Spotify master URL is currently saved.")

    # ---------- Activity ----------
    def _build_activity(self):
        f = self._page_frame("activity", "Activity", "Live command output and diagnostics. Every CLI-engine run also writes its own timestamped text log in the logs folder.")
        body = tk.Frame(f, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        toolbar = tk.Frame(body, bg=C["bg"])
        toolbar.pack(fill="x", pady=(0, 8))
        self.activity_state = tk.Label(toolbar, text="Ready", bg=C["bg"], fg=C["muted"], font=("Segoe UI", 9, "bold"))
        self.activity_state.pack(side="left")
        self.action_button(toolbar, "Clear", self.clear_activity).pack(side="right", padx=(8, 0))
        self.action_button(toolbar, "Open logs folder", lambda: open_path(LOG_DIR)).pack(side="right")
        box = tk.Frame(body, bg="#0F1720", highlightthickness=1, highlightbackground="#273442")
        box.pack(fill="both", expand=True)
        self.activity = tk.Text(box, bg="#0F1720", fg="#D8E4EC", insertbackground="white", font=("Cascadia Mono", 9), relief="flat", wrap="word", padx=12, pady=12)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.activity.yview)
        self.activity.configure(yscrollcommand=scroll.set)
        self.activity.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.activity.insert("end", "Salsa Spotify Manager UI ready.\n")
        self.activity.configure(state="disabled")

    def append_activity(self, text: str):
        if not hasattr(self, "activity"):
            return
        self.activity.configure(state="normal")
        self.activity.insert("end", text)
        self.activity.see("end")
        self.activity.configure(state="disabled")

    def clear_activity(self):
        self.activity.configure(state="normal")
        self.activity.delete("1.0", "end")
        self.activity.configure(state="disabled")

    # ---------- command runner ----------
    def run_manager(self, args: list[str], description: str, stdin_lines: list[str] | None = None, spotify=False, on_done=None):
        if self.busy:
            messagebox.showwarning("Command already running", "Finish the current command before starting another.")
            self.show_page("activity")
            return
        if not MANAGER.exists():
            messagebox.showerror("Manager missing", f"Could not find {MANAGER.name} in the application folder.")
            return
        self.busy = True
        self.pending_refresh = bool(on_done)
        self._after_command = on_done
        self.status_text.set(description)
        self.status_dot.configure(fg=C["amber"])
        if hasattr(self, "activity_state"):
            self.activity_state.configure(text=description, fg=C["amber"])
        self.append_activity(f"\n{'='*72}\n{description}\n{'='*72}\n")
        self.show_page("activity")

        def worker():
            cmd = [sys.executable, str(MANAGER), *args]
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            flags = 0
            if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
                flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            try:
                p = subprocess.Popen(
                    cmd, cwd=str(APP_DIR), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, env=env, creationflags=flags
                )
                self.process = p
                if stdin_lines and p.stdin:
                    p.stdin.write("\n".join(stdin_lines) + "\n")
                    p.stdin.flush()
                assert p.stdout is not None
                while True:
                    chunk = p.stdout.readline()
                    if chunk:
                        self.output_queue.put(("text", chunk))
                    elif p.poll() is not None:
                        break
                rc = p.wait()
                self.output_queue.put(("done", rc, description))
            except Exception as e:
                self.output_queue.put(("error", str(e), description))
            finally:
                self.process = None
        self.process_thread = threading.Thread(target=worker, daemon=True)
        self.process_thread.start()

    def _poll_output(self):
        try:
            while True:
                item = self.output_queue.get_nowait()
                kind = item[0]
                if kind == "text":
                    self.append_activity(item[1])
                elif kind == "done":
                    rc, desc = item[1], item[2]
                    self.busy = False
                    if rc == 0:
                        self.status_text.set("Ready")
                        self.status_dot.configure(fg=C["green_dark"])
                        if hasattr(self, "activity_state"): self.activity_state.configure(text=f"Completed: {desc}", fg=C["green_dark"])
                    else:
                        self.status_text.set(f"Command ended with status {rc}")
                        self.status_dot.configure(fg=C["red"])
                        if hasattr(self, "activity_state"): self.activity_state.configure(text=f"Stopped: {desc} (status {rc})", fg=C["red"])
                    cb = getattr(self, "_after_command", None)
                    self._after_command = None
                    if cb:
                        try: cb()
                        except Exception: pass
                    self.refresh_all()
                elif kind == "error":
                    self.busy = False
                    self.status_text.set("Command failed")
                    self.status_dot.configure(fg=C["red"])
                    self.append_activity(f"\nUI runner error: {item[1]}\n")
                    messagebox.showerror("Command failed", item[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_output)

    # ---------- global refresh / close ----------
    def refresh_all(self):
        try:
            self.refresh_overview()
            self.refresh_incoming_panel()
            self.refresh_track_table()
            self.refresh_sync_panel()
            self.refresh_maintenance()
        except Exception as e:
            self.append_activity(f"\nDashboard refresh warning: {e}\n")

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno("Command still running", "A manager command is still running. Close the UI anyway?\n\nThe subprocess may also be terminated."):
                return
            try:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
            except Exception:
                pass
        self.destroy()


def main():
    if not MANAGER.exists():
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("Salsa Manager", f"{MANAGER.name} must be in the same folder as this UI.")
        root.destroy(); return 2
    app = SalsaManagerUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
