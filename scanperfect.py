"""
ScanPerfect — Native Desktop App (PySide6)
Phase 6 of localization. Replaces browser-based HTML UI entirely.
Reads directly from SQLite + 5yr OHLCV pickle. No server process needed.
"""

import json
import math
import os
import pickle
import sys
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime

import numpy as np
import pandas as pd

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QScrollArea,
    QPlainTextEdit, QFrame, QCheckBox, QSizePolicy,
    QListWidget, QListWidgetItem, QAbstractItemView,
    QGridLayout, QLineEdit, QTextEdit, QSlider,
)
from PySide6.QtCore import Qt, QProcess, QTimer, Signal, QProcessEnvironment, QRectF, QPointF
from PySide6.QtGui import QFont, QFontDatabase, QColor, QPainter, QPen, QLinearGradient, QBrush, QPainterPath


# ============================================================
# PATHS
# ============================================================

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "data" / "scanperfect.db"
PIPELINE_FILE = REPO_ROOT / "data" / "pipeline_state.json"
PIPELINE_LOGS_FILE = REPO_ROOT / "data" / "pipeline_logs.json"
LOCAL_DIR = REPO_ROOT / "local_runner"


# ============================================================
# COLORS — matches existing UI design system
# ============================================================

C = {
    "bg":           "#000000",
    "surface":      "#0A0A0A",
    "surface2":     "#0A0A0A",
    "border":       "#1A1A1A",
    "border_bright":"#222222",
    "text":         "#E0E0E0",
    "text_dim":     "#888888",
    "text_muted":   "#555555",
    "green":        "#4ade80",
    "green_dark":   "#059669",
    "red":          "#f87171",
    "red_dark":     "#dc2626",
    "amber":        "#fbbf24",
    "blue":         "#888888",
    "purple":       "#a855f7",
    "white":        "#E0E0E0",
}


# ============================================================
# DATABASE
# ============================================================

@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS setups (
                setup_type TEXT PRIMARY KEY, name TEXT NOT NULL,
                description TEXT, direction TEXT NOT NULL DEFAULT 'short',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT, setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL, chart_date TEXT NOT NULL, entry_date TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')), UNIQUE(setup_type, ticker, entry_date)
            );
            CREATE TABLE IF NOT EXISTS pending_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT, setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL, signal_date TEXT NOT NULL, entry_date TEXT NOT NULL,
                status TEXT DEFAULT 'pending', ai_verdict TEXT, ai_reasoning TEXT,
                review_notes TEXT, created_at TEXT DEFAULT (datetime('now')), reviewed_at TEXT,
                UNIQUE(setup_type, ticker, entry_date)
            );
            CREATE TABLE IF NOT EXISTS rejected_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL, signal_date TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')), UNIQUE(setup_type, ticker, signal_date)
            );
            CREATE TABLE IF NOT EXISTS earnings_dates (
                ticker TEXT NOT NULL, earnings_date TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now')), UNIQUE(ticker, earnings_date)
            );
            CREATE TABLE IF NOT EXISTS file_mirror (
                path TEXT PRIMARY KEY, data TEXT NOT NULL,
                size_bytes INTEGER, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nightly_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT NOT NULL,
                setup_type TEXT NOT NULL, ticker TEXT NOT NULL, signal_date TEXT NOT NULL,
                cycle_id TEXT NOT NULL, regime_score REAL, expected_win_rate REAL,
                rank INTEGER, expected_move_adr REAL, ai_vet_status TEXT,
                ai_vet_reason TEXT, created_at TEXT NOT NULL
            );
        """)
        _DTSS_DESC = (
            "Short setup targeting failed double tops. Look for: clear prior high/resistance "
            "(left side pivot), second rally into the same zone (can be slightly above or below), "
            "rejection candle or reversal pattern at the double top level, volume often spikes on "
            "the failed attempt then dries up. MAs may be flattening or rolling over. The stock "
            "should be FAILING at or near the double top, not still rallying. After the double top, "
            "price breaks down through the LSP AVWAP and continues lower.\n\n"
            "Reject if: no clear double top visible, stock still in uptrend with no reversal, "
            "the \"double top\" is just consolidation in a trend, move after entry is tiny or "
            "bounces back, entry is too late (already crashed), or entry is too early (top not confirmed)."
        )
        for st, name, desc, direction in [
            ("dtss", "DTSS", _DTSS_DESC, "short"),
            ("3-4db", "3-4DB", "3-4 Day Bounce (Short)", "short"),
            ("htf", "HTF", "High Tight Flag (Long)", "long"),
        ]:
            db.execute(
                "INSERT OR IGNORE INTO setups (setup_type, name, description, direction) VALUES (?,?,?,?)",
                (st, name, desc, direction),
            )
        # Update existing rows that only have the short name
        db.execute("UPDATE setups SET description=? WHERE setup_type='dtss' AND length(description) < 100",
                   (_DTSS_DESC,))


# ============================================================
# OHLCV CACHE — 5yr pickle loaded into memory at startup
# ============================================================

_ohlcv_cache = {}  # {ticker: DataFrame}


def _load_ohlcv_cache():
    """Load the 5yr OHLCV pickle into memory."""
    global _ohlcv_cache
    cache_path = REPO_ROOT / "local_runner" / "cache" / "universe_ohlcv_5yr.pkl"
    if not cache_path.exists():
        print("WARNING: 5yr OHLCV cache not found at %s" % cache_path)
        print("  Charts will not work. Run: python local_runner/cache_builder.py --5yr --force")
        return
    t0 = time.time()
    with open(cache_path, "rb") as f:
        _ohlcv_cache = pickle.load(f)
    print("OHLCV cache loaded: %d tickers in %.1fs" % (len(_ohlcv_cache), time.time() - t0))


def _get_ohlcv(ticker):
    """Get OHLCV DataFrame for a ticker from in-memory cache. Returns None if not found."""
    df = _ohlcv_cache.get(ticker.upper())
    if df is None:
        return None
    return df.copy()


# ============================================================
# JSON + HELPERS
# ============================================================

def load_json(path, default=None):
    if default is None:
        default = {}
    try:
        p = Path(path)
        if p.exists():
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return default


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2, default=str)


def fmt_dur(s):
    if not s or s < 0:
        return "—"
    s = float(s)
    if s < 60:
        return f"{s:.0f}s"
    m = int(s // 60)
    sec = int(s % 60)
    if m < 60:
        return f"{m}m {sec}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m}m"


# ============================================================
# STEP COMMANDS — same as server.py
# ============================================================

STEP_COMMANDS = {
    "signal_grind": [
        sys.executable, str(LOCAL_DIR / "pyramid_grinder.py"),
        "--setup", "{setup}", "--peak-target", "3", "--beam", "10000", "--depth", "100",
    ],
    "exit_grind": [
        sys.executable, str(REPO_ROOT / "scripts" / "exit_grinder.py"),
        "--setup", "{setup}", "--max-forward", "120",
    ],
    "refinement_grind": [
        sys.executable, str(LOCAL_DIR / "pyramid_grinder.py"),
        "--setup", "{setup}", "--blackout",
    ],
    "signal_filter": [
        sys.executable, str(REPO_ROOT / "scripts" / "signal_filter.py"),
        "--setup", "{setup}",
    ],
    "entry_score": [
        sys.executable, str(REPO_ROOT / "scripts" / "entry_candle_scorer.py"),
        "--setup", "{setup}",
    ],
    "ev_grind": [
        sys.executable, str(REPO_ROOT / "scripts" / "ev_grinder.py"),
        "--setup", "{setup}",
    ],
}


# ============================================================
# PIPELINE NODE DEFINITIONS
# ============================================================

# 7 nodes, two loops:
#   Loop 1 (top row):  Examples → Causative Grind → Vetting  (back-arrow to Examples)
#   Then down:         Vetting → Correlative Grind
#   Loop 2 (bot row):  Scan Tuning ↔ Profit Grind
#   Summary at bottom


# ============================================================
# NODE DEFINITIONS — 7 nodes, two feedback loops
# ============================================================

FLOW_NODES = [
    # DO nodes — interactive, user does the work
    {"id": "examples",     "name": "Examples",          "kind": "do",  "tab": 1,
     "desc": "Define setups \u00b7 manage example library"},
    {"id": "vetting",      "name": "Vetting",           "kind": "do",  "tab": 2,
     "desc": "Review winners \u00b7 bank new examples"},
    {"id": "scan_tuning",  "name": "Scan Tuning",       "kind": "do",  "tab": None,
     "desc": "Quality + WR threshold sliders"},
    # RUN nodes — automated, machine does the work
    {"id": "causative",    "name": "Causative Processing",   "kind": "run", "tab": None,
     "desc": "Signal \u2192 Exit \u2192 Refinement"},
    {"id": "correlative",  "name": "Correlative Targeting", "kind": "run", "tab": None,
     "desc": "EV scoring \u2014 predicted WR, MFE, EV"},
    {"id": "profit_grind", "name": "Optimal Management",      "kind": "run", "tab": None,
     "desc": "Optimize exit strategy \u00b7 maximize SQN"},
    # Summary — read-only output
    {"id": "summary",      "name": "Summary",           "kind": "summary", "tab": None,
     "desc": "Setup readiness overview"},
]

GRINDER_SUB_STEPS = {
    "causative":   ["signal_grind", "exit_grind", "refinement_grind", "signal_filter", "entry_score"],
    "correlative": ["ev_grind"],
    "profit_grind": [],  # future
}

# Unlock requirements: (node_id) -> callable(n_examples, step_statuses) -> bool
def _is_unlocked(nid, n_examples, step_statuses):
    """Check if a pipeline node is unlocked based on current progress.
    
    Uses both pipeline_state.json AND existence of output files,
    since grinds may have been run before the state file existed.
    """
    if nid == "examples":
        return True
    if nid == "causative":
        return n_examples >= 20
    if nid == "vetting":
        # Unlocked when causative is done — check state OR refinement output exists
        if _causative_done(step_statuses):
            return True
        return False
    if nid == "correlative":
        # Need 60+ examples AND causative done
        if n_examples < 60:
            return False
        return _causative_done(step_statuses)
    if nid == "scan_tuning":
        return _correlative_done(step_statuses)
    if nid == "profit_grind":
        return _correlative_done(step_statuses)
    if nid == "summary":
        return False  # future
    return False


def _causative_done(step_statuses):
    """Check if causative grind is complete — pipeline state OR local output files."""
    subs = GRINDER_SUB_STEPS.get("causative", [])
    if all(step_statuses.get(ss, {}).get("status") in ("done", "complete") for ss in subs):
        return True
    # Check if cluster-aware refinement output exists locally
    cache_dir = REPO_ROOT / "local_runner" / "cache"
    if cache_dir.exists():
        for f in cache_dir.iterdir():
            if f.name.startswith("refinement_") and "_cl" in f.name and f.suffix == ".json":
                return True
    return False


def _correlative_done(step_statuses):
    """Check if correlative grind is complete — pipeline state OR local output files."""
    subs = GRINDER_SUB_STEPS.get("correlative", [])
    if all(step_statuses.get(ss, {}).get("status") in ("done", "complete") for ss in subs):
        return True
    # Check if EV grinder output exists locally
    cache_dir = REPO_ROOT / "local_runner" / "cache"
    if cache_dir.exists():
        for f in cache_dir.iterdir():
            if f.name.startswith("ev_") and f.suffix == ".json":
                return True
    return False


# ============================================================
# STYLESHEET
# ============================================================

STYLESHEET = """
QMainWindow, QWidget {
    background-color: %(bg)s;
    color: %(text)s;
    font-family: "DM Sans", "Segoe UI", sans-serif;
}
QLabel { color: %(text)s; background: transparent; }
QPushButton {
    background: transparent; border: 1px solid %(border_bright)s;
    color: %(text_dim)s; padding: 6px 16px; font-size: 11px;
    font-weight: 600; letter-spacing: 0.5px;
}
QPushButton:hover {
    background: %(surface2)s; color: %(text)s; border-color: %(text_muted)s;
}
QPushButton:disabled { color: %(text_muted)s; border-color: %(border)s; }
QPushButton[objectName="runBtn"] {
    background: %(green_dark)s; border-color: %(green)s; color: #fff;
}
QPushButton[objectName="runBtn"]:hover { background: %(green)s; color: #000; }
QPushButton[objectName="runBtn"]:disabled {
    background: %(border)s; border-color: %(border_bright)s; color: %(text_muted)s;
}
QPushButton[objectName="stopBtn"] {
    background: %(red_dark)s; border-color: %(red)s; color: #fff;
}
QPushButton[objectName="stopBtn"]:hover { background: %(red)s; }
QPushButton[objectName="stopBtn"]:disabled {
    background: %(border)s; border-color: %(border_bright)s; color: %(text_muted)s;
}
QComboBox {
    background: %(surface)s; border: 1px solid %(border)s;
    color: %(text)s; padding: 4px 10px; font-size: 12px; min-width: 100px;
}
QComboBox:hover { border-color: %(text_muted)s; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: %(surface)s; color: %(text)s; border: 1px solid %(border)s;
    selection-background-color: %(surface2)s;
}
QPlainTextEdit {
    background: %(bg)s; color: %(text_dim)s; border: 1px solid %(border)s;
    font-family: "JetBrains Mono", "Consolas", monospace; font-size: 11px;
    selection-background-color: %(surface2)s;
}
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 6px; }
QScrollBar::handle:vertical {
    background: %(border_bright)s; border-radius: 3px; min-height: 20px;
}
QScrollBar::handle:vertical:hover { background: %(text_muted)s; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 6px; }
QScrollBar::handle:horizontal { background: %(border_bright)s; border-radius: 3px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
""" % C


# ============================================================
# WIDGETS
# ============================================================

class StatusBadge(QLabel):
    _MAP = {
        "idle":    (C["text_muted"], C["surface2"]),
        "pending": (C["text_muted"], C["surface2"]),
        "running": (C["amber"],      "rgba(251,191,36,0.12)"),
        "queued":  (C["amber"],      "rgba(251,191,36,0.12)"),
        "done":    (C["green"],      "rgba(74,222,128,0.12)"),
        "complete":(C["green"],      "rgba(74,222,128,0.12)"),
        "error":   (C["red"],        "rgba(248,113,113,0.12)"),
        "stopped": (C["text_muted"], C["surface2"]),
    }

    def __init__(self, status="idle", parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(20)
        self.setMinimumWidth(60)
        self.set_status(status)

    def set_status(self, s):
        s = (s or "idle").lower()
        fg, bg = self._MAP.get(s, self._MAP["idle"])
        self.setText(s.upper())
        self.setStyleSheet(
            "background:%s; color:%s; border:none; font-size:9px;"
            "font-weight:700; letter-spacing:0.6px; padding:2px 8px;" % (bg, fg)
        )


# ============================================================
# GRINDER DETAIL PANEL
# ============================================================

class GrinderDetail(QFrame):
    """Expandable detail: sub-step badges, metrics, Run/Stop, log."""
    run_requested = Signal(str)
    stop_requested = Signal(str)

    def __init__(self, node_id, parent=None):
        super().__init__(parent)
        self.node_id = node_id
        self.setStyleSheet(
            "GrinderDetail { background:%s; border:1px solid %s; }" % (C["surface"], C["border"])
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(10)

        # Metrics
        mrow = QHBoxLayout()
        mrow.setSpacing(24)
        self._m = {}
        for key, label in [("status","STATUS"), ("lastrun","LAST RUN"),
                           ("duration","DURATION"), ("setup","SETUP")]:
            col = QVBoxLayout()
            col.setSpacing(2)
            lbl = QLabel(label)
            lbl.setStyleSheet(
                "font-size:9px; font-weight:600; letter-spacing:1.2px; color:%s;"
                "background:transparent; border:none;" % C["text_muted"]
            )
            val = QLabel("\u2014")
            val.setStyleSheet(
                "font-size:13px; font-weight:500; color:%s;"
                "background:transparent; border:none;" % C["text"]
            )
            col.addWidget(lbl)
            col.addWidget(val)
            mrow.addLayout(col)
            self._m[key] = val
        mrow.addStretch()
        lay.addLayout(mrow)

        # Sub-step badges
        subs = GRINDER_SUB_STEPS.get(node_id, [])
        self._sub_badges = {}
        if len(subs) > 1:
            sr = QHBoxLayout()
            sr.setSpacing(12)
            for sid in subs:
                sl = QLabel(sid.replace("_", " ").title())
                sl.setStyleSheet(
                    "font-size:10px; color:%s; background:transparent; border:none;" % C["text_muted"]
                )
                sb = StatusBadge("idle")
                sr.addWidget(sl)
                sr.addWidget(sb)
                self._sub_badges[sid] = sb
            sr.addStretch()
            lay.addLayout(sr)

        # Buttons
        brow = QHBoxLayout()
        brow.setSpacing(8)
        self._run_btn = QPushButton("\u25b6  RUN")
        self._run_btn.setObjectName("runBtn")
        self._run_btn.clicked.connect(self._on_run)
        brow.addWidget(self._run_btn)
        self._stop_btn = QPushButton("\u25a0  STOP")
        self._stop_btn.setObjectName("stopBtn")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        brow.addWidget(self._stop_btn)
        clr = QPushButton("CLEAR LOG")
        clr.clicked.connect(lambda: self._log.clear())
        brow.addWidget(clr)
        brow.addStretch()
        lay.addLayout(brow)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(180)
        self._log.setMaximumHeight(350)
        self._log.setPlaceholderText("No logs yet")
        lay.addWidget(self._log)
        self._load_logs()

    def _on_run(self):
        subs = GRINDER_SUB_STEPS.get(self.node_id, [])
        self.run_requested.emit(subs[0] if subs else self.node_id)

    def _on_stop(self):
        subs = GRINDER_SUB_STEPS.get(self.node_id, [])
        self.stop_requested.emit(subs[0] if subs else self.node_id)

    def _load_logs(self):
        logs = load_json(PIPELINE_LOGS_FILE, {})
        subs = GRINDER_SUB_STEPS.get(self.node_id, [self.node_id])
        all_lines = []
        for sid in subs:
            all_lines.extend(logs.get(sid, []))
        if all_lines:
            self._log.setPlainText("\n".join(all_lines[-500:]))
            sb = self._log.verticalScrollBar()
            sb.setValue(sb.maximum())

    def set_setup(self, setup):
        self._m["setup"].setText(setup.upper())

    def update_from_state(self, pipeline_steps):
        subs = GRINDER_SUB_STEPS.get(self.node_id, [self.node_id])
        statuses = [pipeline_steps.get(s, {}).get("status", "idle") for s in subs]
        if "running" in statuses or "queued" in statuses:
            overall = "running"
        elif "error" in statuses:
            overall = "error"
        elif all(s in ("done", "complete") for s in statuses):
            overall = "done"
        else:
            overall = "idle"

        self._m["status"].setText(overall.upper())
        sc = {"done": C["green"], "complete": C["green"],
              "running": C["amber"], "error": C["red"]}.get(overall, C["text"])
        self._m["status"].setStyleSheet(
            "font-size:13px; font-weight:500; color:%s; background:transparent; border:none;" % sc
        )
        for sid in reversed(subs):
            ss = pipeline_steps.get(sid, {})
            fin = ss.get("finished_at")
            if fin:
                try:
                    self._m["lastrun"].setText(datetime.fromisoformat(fin).strftime("%Y-%m-%d %H:%M"))
                except Exception:
                    self._m["lastrun"].setText(str(fin)[:16])
                dur = ss.get("duration_s")
                if dur:
                    self._m["duration"].setText(fmt_dur(dur))
                break
        for sid, badge in self._sub_badges.items():
            badge.set_status(pipeline_steps.get(sid, {}).get("status", "idle"))
        is_run = overall in ("running", "queued")
        self._run_btn.setEnabled(not is_run)
        self._stop_btn.setEnabled(is_run)

    def append_log(self, text):
        self._log.appendPlainText(text)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear_log(self):
        self._log.clear()


# ============================================================
# FLOWCHART CANVAS — QPainter 2D diagram
# ============================================================

class FlowchartCanvas(QWidget):
    """Draws pipeline as a spatial flowchart.

    DO nodes (left column): rounded corners, softer border
    RUN nodes (right column): sharp corners, heavier border
    Locked nodes: fully dimmed
    Two feedback loops drawn with dashed arcs
    """
    node_clicked = Signal(str)

    NW, NH = 210, 72  # fallback minimums
    COL_GAP = 120
    ROW_GAP = 56
    LOOP_M = 32

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(560)
        self._rects = {}
        self._statuses = {}
        self._infos = {}
        self._locked = {}
        self._hover = None
        self._n_examples = 0
        self._expanded_id = None  # which RUN node is expanded
        self._detail_widgets = {}  # nid -> GrinderDetail widget (children of canvas)
        self._anim_expand_h = 0.0
        self._anim_expand_w = 0.0
        self._anim_target_h = 0.0
        self._anim_target_w = 0.0
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)  # ~60fps
        self._anim_timer.timeout.connect(self._anim_step)
        self._example_progress = 0.0  # 0.0 to 1.0
        self._example_n = 0
        self._example_total = 0
        self._scan_tuning_tab = "entry"  # "entry" or "exit"
        self.setMouseTracking(True)

    def set_status(self, nid, s):
        self._statuses[nid] = s
        self.update()

    def set_info(self, nid, t):
        self._infos[nid] = t
        self.update()

    def set_locked(self, nid, locked):
        self._locked[nid] = locked
        self.update()

    def set_n_examples(self, n):
        self._n_examples = n
        self.update()

    def set_example_progress(self, ratio, n_examples=0, n_total=0):
        """Set examples progress bar data."""
        self._example_progress = ratio
        self._example_n = n_examples
        self._example_total = n_total
        self.update()

    def _calc_base_layout(self):
        """Calculate base card sizes from window dimensions. Called on resize only."""
        w = self.width()
        h = self.height()

        loop_margin = 50
        side_pad = 60
        usable_w = w - side_pad * 2 - loop_margin
        col_gap = max(80, usable_w * 0.08)
        nw = max(240, min(420, (usable_w - col_gap) / 2))

        top_pad = 50
        bot_pad = 50
        loop_v = 50
        usable_h = h - top_pad - bot_pad - loop_v
        row_gap = max(40, usable_h * 0.06)
        nh = max(80, min(130, (usable_h - row_gap * 5) / 5))

        self._nw = nw
        self._nh = nh
        self._col_gap = col_gap
        self._row_gap = row_gap
        self._loop_margin = loop_margin
        self._side_pad = side_pad
        self._top_pad = top_pad
        self._loop_v = loop_v
        self._loop_m = 36

    def _layout(self):
        """Position all cards. Expanded card overlaps on top."""
        nw = getattr(self, '_nw', 300)
        nh = getattr(self, '_nh', 100)
        col_gap = getattr(self, '_col_gap', 100)
        row_gap = getattr(self, '_row_gap', 50)
        loop_margin = getattr(self, '_loop_margin', 50)
        side_pad = getattr(self, '_side_pad', 60)
        top_pad = getattr(self, '_top_pad', 50)
        loop_v = getattr(self, '_loop_v', 50)
        w = self.width()

        lx = side_pad + loop_margin
        rx = lx + nw + col_gap

        y = top_pad

        row_nodes = [
            [("examples", lx), ("causative", rx)],
            [("vetting", lx)],
            [("correlative", rx)],
            [("scan_tuning", lx), ("profit_grind", rx)],
        ]

        for row in row_nodes:
            for nid, nx in row:
                self._rects[nid] = (nx, y, nw, nh)
            y += nh + row_gap

        # Summary: to the RIGHT of the flowchart, vertically centered
        summary_w = min(nw, 280)
        summary_h = nh * 2 + row_gap  # taller card
        summary_x = rx + nw + max(30, col_gap // 2)
        # If it would go off-screen, place it below profit_grind row instead
        if summary_x + summary_w > w - 20:
            summary_x = rx
            summary_y = y + loop_v
        else:
            # Vertically center against rows 2-3 (correlative / scan_tuning area)
            row2_top = self._rects["correlative"][1] if "correlative" in self._rects else top_pad + 2*(nh+row_gap)
            summary_y = row2_top
        self._rects["summary"] = (summary_x, summary_y, summary_w, summary_h)

        # Expanded card overlay
        if self._expanded_id and self._expanded_id in self._rects:
            nd = next((n for n in FLOW_NODES if n["id"] == self._expanded_id), None)
            is_do = nd and nd["kind"] == "do"

            if is_do:
                # DO nodes: fill viewport with 20px padding
                viewport = self.parent()
                scroll_y = 0
                if viewport and hasattr(viewport, 'parent'):
                    scroll_area = viewport.parent()
                    if scroll_area and hasattr(scroll_area, 'verticalScrollBar'):
                        scroll_y = scroll_area.verticalScrollBar().value()
                new_w = self._anim_expand_w
                new_h = self._anim_expand_h
                self._rects[self._expanded_id] = (20, scroll_y + 20, new_w, new_h)
            else:
                # RUN nodes: grow from original position
                ex, ey, ew, _ = self._rects[self._expanded_id]
                new_w = ew + self._anim_expand_w
                new_h = nh + self._anim_expand_h
                self._rects[self._expanded_id] = (ex, ey, new_w, new_h)

        self._expand_h = self._anim_expand_h
        self.setMinimumHeight(int(y + loop_v + nh + 40))

    def resizeEvent(self, ev):
        self._calc_base_layout()
        self._layout()
        self._position_detail()
        super().resizeEvent(ev)

    def showEvent(self, ev):
        self._calc_base_layout()
        self._layout()
        self._position_detail()
        super().showEvent(ev)

    def add_detail_widget(self, nid, widget):
        """Register a GrinderDetail as a child widget of the canvas."""
        widget.setParent(self)
        widget.setVisible(False)
        self._detail_widgets[nid] = widget

    def expand_node(self, nid):
        """Expand/collapse a node with animation."""
        nd = next((n for n in FLOW_NODES if n["id"] == nid), None)
        if not nd:
            return

        if self._expanded_id == nid:
            # Collapse
            self._anim_target_h = 0
            self._anim_target_w = 0
            self._anim_timer.start()
        else:
            if self._expanded_id and self._expanded_id in self._detail_widgets:
                self._detail_widgets[self._expanded_id].setVisible(False)
            self._expanded_id = nid
            self._anim_expand_h = 0
            self._anim_expand_w = 0

            viewport = self.parent()
            vis_w = viewport.width() if viewport and hasattr(viewport, 'width') else self.width()
            vis_h = viewport.height() if viewport and hasattr(viewport, 'height') else 600

            if nd["kind"] == "do":
                self._anim_target_w = vis_w - 40
                self._anim_target_h = vis_h - 40
            else:
                nh = getattr(self, '_nh', 100)
                nw = getattr(self, '_nw', 300)
                self._anim_target_h = max(380, nh * 4)
                self._anim_target_w = max(200, int(nw * 0.7))

            if nid in self._detail_widgets:
                self._detail_widgets[nid].setVisible(True)
            self._anim_timer.start()

    def _anim_step(self):
        """Animate expand/collapse one frame — both dimensions."""
        speed_h = 80
        speed_w = 160

        if self._anim_target_h > self._anim_expand_h:
            self._anim_expand_h = min(self._anim_expand_h + speed_h, self._anim_target_h)
        elif self._anim_target_h < self._anim_expand_h:
            self._anim_expand_h = max(self._anim_expand_h - speed_h, self._anim_target_h)

        if self._anim_target_w > self._anim_expand_w:
            self._anim_expand_w = min(self._anim_expand_w + speed_w, self._anim_target_w)
        elif self._anim_target_w < self._anim_expand_w:
            self._anim_expand_w = max(self._anim_expand_w - speed_w, self._anim_target_w)

        self._layout()
        self._position_detail()
        self.update()

        done_h = abs(self._anim_expand_h - self._anim_target_h) < 1
        done_w = abs(self._anim_expand_w - self._anim_target_w) < 1
        if done_h and done_w:
            self._anim_expand_h = self._anim_target_h
            self._anim_expand_w = self._anim_target_w
            self._anim_timer.stop()
            if self._anim_target_h == 0:
                if self._expanded_id in self._detail_widgets:
                    self._detail_widgets[self._expanded_id].setVisible(False)
                self._expanded_id = None
            self._layout()
            self._position_detail()
            self.update()

    def _position_detail(self):
        """Position the active detail widget inside the expanded card rect."""
        for nid, widget in self._detail_widgets.items():
            if nid == self._expanded_id and nid in self._rects:
                x, y, w, h = self._rects[nid]
                # Vetting gets a thin header (just enough to click-collapse)
                # Other nodes use the full base card height as header
                header_h = 32 if nid in ("vetting", "examples") else (56 if nid == "scan_tuning" else self._nh)
                widget.setGeometry(int(x + 1), int(y + header_h), int(w - 2), int(h - header_h - 1))
                widget.setVisible(True)
                widget.raise_()
            else:
                widget.setVisible(False)

    def _edge(self, nid, side):
        x, y, w, h = self._rects[nid]
        if side == "r":  return x + w, y + h // 2
        if side == "l":  return x, y + h // 2
        if side == "b":  return x + w // 2, y + h
        if side == "t":  return x + w // 2, y

    def paintEvent(self, ev):
        self._layout()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._draw_connections(p)
        # Draw non-expanded nodes first, expanded node last (on top)
        for nd in FLOW_NODES:
            if nd["id"] != self._expanded_id:
                self._draw_node(p, nd)
        if self._expanded_id:
            nd = next((n for n in FLOW_NODES if n["id"] == self._expanded_id), None)
            if nd:
                self._draw_node(p, nd)
        p.end()

    def _draw_connections(self, p):
        from PySide6.QtCore import QRectF
        solid = QPen(QColor(C["border_bright"]), 1.5)
        solid_locked = QPen(QColor("#151515"), 1)
        dash = QPen(QColor(C["text_muted"]), 1, Qt.DashLine)
        dash_locked = QPen(QColor("#1A1A1A"), 1, Qt.DashLine)

        def is_locked(nid):
            return self._locked.get(nid, False)

        def conn_pen(nid_from, nid_to):
            return solid_locked if (is_locked(nid_from) or is_locked(nid_to)) else solid

        def arrow_tip(p, x, y, direction, locked=False):
            c = QColor("#1A1A1A" if locked else C["text_muted"])
            p.setPen(QPen(c, 1.5))
            s = 5
            if direction == "r":
                p.drawLine(x-s, y-s, x, y); p.drawLine(x-s, y+s, x, y)
            elif direction == "d":
                p.drawLine(x-s, y-s, x, y); p.drawLine(x+s, y-s, x, y)
            elif direction == "l":
                p.drawLine(x+s, y-s, x, y); p.drawLine(x+s, y+s, x, y)
            elif direction == "u":
                p.drawLine(x-s, y+s, x, y); p.drawLine(x+s, y+s, x, y)

        # Examples → Causative (horizontal right)
        p.setPen(conn_pen("examples", "causative"))
        x1, y1 = self._edge("examples", "r"); x2, y2 = self._edge("causative", "l")
        p.drawLine(x1, y1, x2, y2)
        arrow_tip(p, x2, y2, "r", is_locked("causative"))

        # Causative → Vetting (down-left L-shape)
        p.setPen(conn_pen("causative", "vetting"))
        x1, y1 = self._edge("causative", "b"); x2, y2 = self._edge("vetting", "r")
        p.drawLine(x1, y1, x1, y2); p.drawLine(x1, y2, x2, y2)
        arrow_tip(p, x2, y2, "l", is_locked("vetting"))

        # Loop 1: Vetting → Examples (dashed, left side)
        lk1 = is_locked("vetting")
        p.setPen(dash_locked if lk1 else dash)
        ex = self._rects["examples"]; vt = self._rects["vetting"]
        vl_x, vl_y = vt[0], vt[1] + vt[3] // 2
        el_x, el_y = ex[0], ex[1] + ex[3] // 2
        loop_x = min(vl_x, el_x) - self._loop_m
        p.drawLine(vl_x, vl_y, loop_x, vl_y)
        p.drawLine(loop_x, vl_y, loop_x, el_y)
        p.drawLine(loop_x, el_y, el_x, el_y)
        arrow_tip(p, el_x, el_y, "r", lk1)
        if not lk1:
            p.setPen(QPen(QColor(C["text_muted"])))
            f = QFont("DM Sans", 1); f.setPixelSize(9); p.setFont(f)
            p.save()
            p.translate(loop_x - 8, (vl_y + el_y) // 2 + 30)
            p.rotate(-90)
            p.drawText(0, 0, "add examples")
            p.restore()

        # Vetting → Correlative (down from vetting, across to correlative)
        p.setPen(conn_pen("vetting", "correlative"))
        x1, y1 = self._edge("vetting", "b"); x2, y2 = self._edge("correlative", "t")
        my = y1 + (y2 - y1) // 2
        p.drawLine(x1, y1, x1, my); p.drawLine(x1, my, x2, my); p.drawLine(x2, my, x2, y2)
        arrow_tip(p, x2, y2, "d", is_locked("correlative"))

        # Gate label: 60 examples
        if is_locked("correlative"):
            p.setPen(QPen(QColor(C["text_muted"])))
            f = QFont("DM Sans", 1); f.setPixelSize(9); p.setFont(f)
            gate_x = (x1 + x2) // 2
            p.drawText(QRectF(gate_x - 40, my - 14, 80, 14), Qt.AlignCenter,
                       "%d/60 examples" % self._n_examples)

        # Correlative → Scan Tuning (down-left L-shape)
        p.setPen(conn_pen("correlative", "scan_tuning"))
        x1, y1 = self._edge("correlative", "b"); x2, y2 = self._edge("scan_tuning", "r")
        p.drawLine(x1, y1, x1, y2); p.drawLine(x1, y2, x2, y2)
        arrow_tip(p, x2, y2, "l", is_locked("scan_tuning"))

        # Scan Tuning → Profit Grind (horizontal right)
        p.setPen(conn_pen("scan_tuning", "profit_grind"))
        x1, y1 = self._edge("scan_tuning", "r"); x2, y2 = self._edge("profit_grind", "l")
        p.drawLine(x1, y1, x2, y2)
        arrow_tip(p, x2, y2, "r", is_locked("profit_grind"))

        # Loop 2: Profit Grind → Scan Tuning (dashed, below)
        lk2 = is_locked("profit_grind")
        p.setPen(dash_locked if lk2 else dash)
        st = self._rects["scan_tuning"]; pg = self._rects["profit_grind"]
        pb_x, pb_y = pg[0] + pg[2] // 2, pg[1] + pg[3]
        sb_x, sb_y = st[0] + st[2] // 2, st[1] + st[3]
        ly = max(pb_y, sb_y) + self._loop_m // 2 + 4
        p.drawLine(pb_x, pb_y, pb_x, ly)
        p.drawLine(pb_x, ly, sb_x, ly)
        p.drawLine(sb_x, ly, sb_x, sb_y)
        arrow_tip(p, sb_x, sb_y, "u", lk2)
        if not lk2:
            p.setPen(QPen(QColor(C["text_muted"])))
            f = QFont("DM Sans", 1); f.setPixelSize(9); p.setFont(f)
            p.drawText(QRectF((pb_x + sb_x)//2 - 40, ly + 2, 80, 14), Qt.AlignCenter, "tweak \u00b7 re-run")

        # Profit Grind → Summary (horizontal right, or L-shape)
        p.setPen(conn_pen("profit_grind", "summary"))
        x1, y1 = self._edge("profit_grind", "r"); x2, y2 = self._edge("summary", "l")
        if abs(y1 - y2) < 10:
            p.drawLine(x1, y1, x2, y2)
            arrow_tip(p, x2, y2, "r", is_locked("summary"))
        else:
            mx = (x1 + x2) // 2
            p.drawLine(x1, y1, mx, y1); p.drawLine(mx, y1, mx, y2); p.drawLine(mx, y2, x2, y2)
            arrow_tip(p, x2, y2, "r", is_locked("summary"))

    def _draw_node(self, p, nd):
        from PySide6.QtCore import QRectF, QPointF
        nid = nd["id"]
        if nid not in self._rects:
            return
        x, y, w, h = self._rects[nid]
        is_expanded = (nid == self._expanded_id)
        header_h = self._nh if is_expanded else h
        status = self._statuses.get(nid, "idle")
        locked = self._locked.get(nid, False)
        hover = self._hover == nid and not locked
        kind = nd["kind"]

        # Title font scales up when expanded
        if is_expanded and self._anim_expand_h > 10:
            expand_ratio = min(1.0, self._anim_expand_h / (getattr(self, '_anim_target_h', 380) or 380))
            name_px = max(13, int(header_h * 0.17 + 10 * expand_ratio))
        else:
            name_px = max(13, int(header_h * 0.17))
        desc_px = max(10, int(header_h * 0.12))
        badge_px = max(9, int(header_h * 0.1))
        pad = max(14, int(w * 0.04))
        radius = max(6, int(header_h * 0.08))

        NODE_COLORS = {
            "examples":     {"bg": "#F08080", "bg_r": "#d9736f", "border": "#F08080", "dark_text": True},
            "vetting":      {"bg": "#FFD166", "bg_r": "#e6bc5c", "border": "#FFD166", "dark_text": True},
            "scan_tuning":  {"bg": "#FFD166", "bg_r": "#e6bc5c", "border": "#FFD166", "dark_text": True},
            "causative":    {"bg": "#7DC4FF", "bg_r": "#6ab0e6", "border": "#7DC4FF", "dark_text": True},
            "correlative":  {"bg": "#7DC4FF", "bg_r": "#6ab0e6", "border": "#7DC4FF", "dark_text": True},
            "profit_grind": {"bg": "#7DC4FF", "bg_r": "#6ab0e6", "border": "#7DC4FF", "dark_text": True},
            "summary":      {"bg": "#0d2a1a", "bg_r": "#153d26", "border": "#225c3d"},
        }

        if locked:
            grad = QLinearGradient(QPointF(x, y), QPointF(x + w, y))
            grad.setColorAt(0, QColor("#050505"))
            grad.setColorAt(1, QColor("#080808"))
            p.setBrush(grad)
            border_col = QColor("#111111")
            text_col = QColor("#333333")
            sub_col = QColor("#222222")
        else:
            nc = NODE_COLORS.get(nid, {"bg": "#0A0A0A", "bg_r": "#141414", "border": "#222222"})
            left_c = nc["bg"]
            right_c = nc["bg_r"]
            if hover:
                left_c = QColor(left_c).lighter(120).name()
                right_c = QColor(right_c).lighter(120).name()
            grad = QLinearGradient(QPointF(x, y), QPointF(x + w, y))
            grad.setColorAt(0, QColor(left_c))
            grad.setColorAt(1, QColor(right_c))
            p.setBrush(grad)
            border_col = QColor(nc["border"])
            if nc.get("dark_text"):
                text_col = QColor("#1a1a1a")
                sub_col = QColor("#333333")
            else:
                text_col = QColor(C["text"])
                sub_col = QColor(C["text_dim"])
            sc_map = {"running": C["amber"], "queued": C["amber"], "error": C["red"]}
            if status in sc_map:
                border_col = QColor(sc_map[status])

        # Thicker borders
        p.setPen(QPen(border_col, 2.5))

        if kind == "do":
            p.drawRoundedRect(x, y, w, h, radius, radius)
        elif kind == "summary":
            p.drawRoundedRect(x, y, w, h, radius, radius)
        else:
            p.drawRect(x, y, w, h)

        # Name + inline example count for Examples card
        p.setPen(QPen(text_col))
        f = QFont("DM Sans", 1)
        f.setPixelSize(name_px); f.setWeight(QFont.DemiBold); p.setFont(f)
        name_y = y + pad
        name_h = int(header_h * 0.4)
        title_text = nd["name"]
        if nid == "examples" and not locked:
            n_ex = getattr(self, "_example_n", 0)
            n_tot = getattr(self, "_example_total", 0)
            if n_tot > 0:
                title_text = "%s  %d / %d" % (nd["name"], n_ex, n_tot)
            else:
                title_text = "%s  %d" % (nd["name"], n_ex)
        p.drawText(QRectF(x + pad, name_y, w - pad*2, name_h),
                   Qt.AlignLeft | Qt.AlignVCenter, title_text)

        # ENTRY / EXIT Chrome-style tabs for Scan Tuning (in header, below title)
        if nid == "scan_tuning" and is_expanded and not locked:
            tab_y = y + 30
            tab_h = 26
            tab_bottom = tab_y + tab_h
            half_w = w // 2
            entry_x = x
            exit_x = x + half_w
            active = self._scan_tuning_tab
            curve = 8  # shoulder curve radius

            # Draw tab bar background (the "shelf" behind inactive tabs)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#1A1A1A"))
            p.drawRect(int(x), int(tab_y), int(w), tab_h)

            # Active tab: raised shape with curved top corners, no bottom border
            # Inactive tab: flat, sits on the shelf
            for tab_key, tx, tw, bright_col, dim_col in [
                ("entry", entry_x, half_w, "#4ade80", "#2a5e3f"),
                ("exit", exit_x, w - half_w, "#f87171", "#8a4444"),
            ]:
                is_active = active == tab_key
                if is_active:
                    # Chrome-style raised tab: curved top shoulders, flat bottom
                    path = QPainterPath()
                    path.moveTo(tx, tab_bottom)
                    path.lineTo(tx, tab_y + curve)
                    path.quadTo(tx, tab_y, tx + curve, tab_y)
                    path.lineTo(tx + tw - curve, tab_y)
                    path.quadTo(tx + tw, tab_y, tx + tw, tab_y + curve)
                    path.lineTo(tx + tw, tab_bottom)
                    path.closeSubpath()
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(bright_col))
                    p.drawPath(path)
                    # Text
                    p.setPen(QColor("#000000"))
                else:
                    # Inactive: subtle fill, sits behind
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(dim_col))
                    p.drawRect(int(tx + 2), int(tab_y + 6), int(tw - 4), tab_h - 6)
                    # Text
                    p.setPen(QColor("#999999"))

                f.setPixelSize(12); f.setWeight(QFont.Bold); p.setFont(f)
                text_y = tab_y + (0 if is_active else 4)
                text_h = tab_h - (0 if is_active else 4)
                p.drawText(QRectF(tx, text_y, tw, text_h),
                           Qt.AlignCenter, tab_key)

            # Store tab rects for click detection
            self._scan_tab_rects = {
                "entry": (int(entry_x), int(tab_y), int(half_w), tab_h),
                "exit": (int(exit_x), int(tab_y), int(w - half_w), tab_h),
            }

        # Progress bar for Examples (below title, no desc text)
        if nid == "examples" and not locked:
            progress = getattr(self, "_example_progress", 0.0)
            bar_y = name_y + name_h + 2
            bar_h = max(18, int(header_h * 0.18))
            bar_w = w - pad * 2
            bar_r = bar_h // 2

            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#1a0a0c"))
            p.drawRoundedRect(int(x + pad), int(bar_y), int(bar_w), bar_h, bar_r, bar_r)

            if progress > 0:
                fill_w = max(bar_h, int(bar_w * min(progress, 1.0)))
                fg = QLinearGradient(QPointF(x + pad, bar_y), QPointF(x + pad + fill_w, bar_y))
                fg.setColorAt(0, QColor("#5c2d33"))
                fg.setColorAt(1, QColor("#8c4450"))
                p.setBrush(fg)
                p.drawRoundedRect(int(x + pad), int(bar_y), fill_w, bar_h, bar_r, bar_r)

        # Lock icon
        elif locked:
            p.setPen(QPen(QColor("#333333")))
            f.setPixelSize(max(16, int(header_h * 0.18))); p.setFont(f)
            p.drawText(QRectF(x + w - pad - 20, y + header_h//2 - 12, 24, 24),
                       Qt.AlignCenter, "\U0001f512")

        # Status badge (running/error only)
        elif status in ("running", "queued", "error"):
            sc_map = {"running": C["amber"], "queued": C["amber"], "error": C["red"]}
            p.setPen(QPen(QColor(sc_map.get(status, C["text_muted"]))))
            f.setPixelSize(badge_px); f.setWeight(QFont.Bold); p.setFont(f)
            p.drawText(QRectF(x + w - 70, y + pad - 2, 56, 16),
                       Qt.AlignRight | Qt.AlignVCenter, status)

        # Nav arrow for DO nodes
        if kind == "do" and nd.get("tab") is not None and not locked:
            p.setPen(QPen(QColor(C["text_muted"])))
            f.setPixelSize(max(16, int(header_h * 0.18))); p.setFont(f)
            p.drawText(QRectF(x + w - pad - 16, y + header_h//2 - 10, 20, 20),
                       Qt.AlignCenter, "\u2192")

        # Dot connectors
        if not locked:
            p.setBrush(QColor(C["border_bright"]))
            p.setPen(Qt.NoPen)
            for side in ("l", "r", "t", "b"):
                dx, dy = self._edge(nid, side)
                p.drawEllipse(dx - 3, dy - 3, 6, 6)

    def mouseMoveEvent(self, ev):
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        mx, my = int(pos.x()), int(pos.y())
        old = self._hover
        self._hover = None
        for nid, (x, y, w, h) in self._rects.items():
            if x <= mx <= x + w and y <= my <= y + h:
                locked = self._locked.get(nid, False)
                if not locked:
                    self._hover = nid
                break
        if old != self._hover:
            self.setCursor(Qt.PointingHandCursor if self._hover else Qt.ArrowCursor)
            self.update()

    def mousePressEvent(self, ev):
        # Check scan tuning tab clicks first (when expanded)
        if self._expanded_id == "scan_tuning" and hasattr(self, "_scan_tab_rects"):
            pos = ev.position() if hasattr(ev, "position") else ev.pos()
            mx, my = int(pos.x()), int(pos.y())
            for tab_key, (tx, ty, tw, th) in self._scan_tab_rects.items():
                if tx <= mx <= tx + tw and ty <= my <= ty + th:
                    if tab_key != self._scan_tuning_tab:
                        self._scan_tuning_tab = tab_key
                        # Tell the workspace to switch tabs
                        ws = self._detail_widgets.get("scan_tuning")
                        if ws and hasattr(ws, "_set_tab"):
                            ws._set_tab(tab_key)
                        self.update()
                    return  # consumed — don't collapse

        if self._hover:
            self.node_clicked.emit(self._hover)


class WorkspaceDetail(QFrame):
    """Expandable workspace for DO nodes. Generic placeholder."""

    def __init__(self, node_id, parent=None):
        super().__init__(parent)
        self.node_id = node_id
        self.setStyleSheet(
            "WorkspaceDetail { background:%s; border:1px solid %s; }" % (C["surface"], C["border"])
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.addStretch()

    def set_setup(self, setup):
        pass


class ScanTuningWorkspace(QFrame):
    """Scan Tuning workspace with two tabs:
      ENTRY — setup/market aggressiveness, refinement depth, WR floor
      EXIT  — profit grinder exit expression selection, trim, management objective

    Both tabs share the SPY bubble chart on the right.

    Data sources:
      - ev_{setup}_*.json  — signals with scores + killed_at_depth
      - refinement_{setup}_*.json — depth_progression
      - profit_{setup}.json — profit grinder results (exit tab)
    """

    def __init__(self, node_id="scan_tuning", parent=None):
        super().__init__(parent)
        self.node_id = node_id
        self._setup = "dtss"
        self._tab = "entry"         # "entry" or "exit"
        self._ev_data = None
        self._signals = []
        self._depth_progression = []
        self._profit_data = None    # profit grinder output
        self._loaded_setup = None

        self.setStyleSheet(
            "ScanTuningWorkspace { background:%s; border:1px solid %s; }" % (C["surface"], C["border"])
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Top bar: stats ──
        top_bar = QFrame()
        top_bar.setFixedHeight(32)
        top_bar.setStyleSheet(
            "QFrame { background:#0A0A0A; border-bottom:1px solid %s; }" % C["border"]
        )
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(12, 0, 12, 0)
        tb_lay.setSpacing(8)

        self._stats_label = QLabel("No data loaded")
        self._stats_label.setStyleSheet(
            "font-family:'JetBrains Mono','Consolas',monospace; font-size:11px;"
            "color:%s; background:transparent; border:none;" % C["text_dim"]
        )
        tb_lay.addWidget(self._stats_label)
        tb_lay.addStretch()

        lay.addWidget(top_bar)

        # ── Main body: left panel (swappable) + right chart (shared) ──
        body = QWidget()
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        # Left panel container — holds both entry and exit panels, only one visible
        self._left_container = QWidget()
        self._left_container.setFixedWidth(280)
        left_stack = QVBoxLayout(self._left_container)
        left_stack.setContentsMargins(0, 0, 0, 0)
        left_stack.setSpacing(0)

        # ── ENTRY panel ──
        self._entry_panel = QFrame()
        self._entry_panel.setStyleSheet(
            "QFrame { background:#050505; border-right:1px solid %s; }" % C["border"]
        )
        entry_lay = QVBoxLayout(self._entry_panel)
        entry_lay.setContentsMargins(12, 12, 12, 12)
        entry_lay.setSpacing(16)

        entry_lay.addWidget(self._make_section_label("SETUP FEATURES"))
        self._setup_slider = self._make_slider()
        entry_lay.addWidget(self._setup_slider)
        self._setup_val_label = self._make_value_label("Off")
        entry_lay.addWidget(self._setup_val_label)

        entry_lay.addWidget(self._make_section_label("MARKET FEATURES"))
        self._market_slider = self._make_slider()
        entry_lay.addWidget(self._market_slider)
        self._market_val_label = self._make_value_label("Off")
        entry_lay.addWidget(self._market_val_label)

        entry_lay.addWidget(self._make_section_label("REFINEMENT DEPTH"))
        self._depth_slider = self._make_slider()
        entry_lay.addWidget(self._depth_slider)
        self._depth_val_label = self._make_value_label("max")
        entry_lay.addWidget(self._depth_val_label)

        entry_lay.addWidget(self._make_section_label("WIN RATE FLOOR"))
        self._wr_slider = self._make_slider()
        entry_lay.addWidget(self._wr_slider)
        self._wr_val_label = self._make_value_label("Off")
        entry_lay.addWidget(self._wr_val_label)

        # Surviving signal count (entry tab)
        self._surviving_label = QLabel("—")
        self._surviving_label.setStyleSheet(
            "font-family:'JetBrains Mono','Consolas',monospace; font-size:14px;"
            "font-weight:700; color:%s; background:transparent; border:none;"
            "padding-top:12px;" % C["amber"]
        )
        self._surviving_label.setAlignment(Qt.AlignCenter)
        entry_lay.addWidget(self._surviving_label)

        entry_lay.addStretch()
        left_stack.addWidget(self._entry_panel)

        # ── EXIT panel ──
        self._exit_panel = QFrame()
        self._exit_panel.setStyleSheet(
            "QFrame { background:#050505; border-right:1px solid %s; }" % C["border"]
        )
        exit_lay = QVBoxLayout(self._exit_panel)
        exit_lay.setContentsMargins(12, 12, 12, 12)
        exit_lay.setSpacing(16)

        exit_lay.addWidget(self._make_section_label("MANAGEMENT OBJECTIVE"))
        # SQN vs Max Profit toggle
        obj_row = QHBoxLayout()
        obj_row.setSpacing(4)
        self._obj_btns = {}
        for obj_key, obj_label in [("sqn", "SQN"), ("max_profit", "MAX PROFIT")]:
            btn = QPushButton(obj_label)
            btn.setFixedHeight(22)
            btn.setCheckable(True)
            btn.setChecked(obj_key == "sqn")
            btn.clicked.connect(lambda checked, o=obj_key: self._set_objective(o))
            obj_row.addWidget(btn)
            self._obj_btns[obj_key] = btn
        exit_lay.addLayout(obj_row)
        self._update_obj_btn_styles()

        exit_lay.addWidget(self._make_section_label("EXIT EXPRESSION"))
        self._exit_expr_label = QLabel("No profit grinder data")
        self._exit_expr_label.setStyleSheet(
            "font-family:'JetBrains Mono','Consolas',monospace; font-size:10px;"
            "color:%s; background:transparent; border:none; padding:4px 0;" % C["text_dim"]
        )
        self._exit_expr_label.setWordWrap(True)
        exit_lay.addWidget(self._exit_expr_label)

        exit_lay.addWidget(self._make_section_label("TRIM"))
        self._trim_slider = self._make_slider()
        self._trim_slider.setRange(0, 100)
        self._trim_slider.setValue(0)
        exit_lay.addWidget(self._trim_slider)
        self._trim_val_label = self._make_value_label("Off")
        exit_lay.addWidget(self._trim_val_label)

        # Exit stats
        self._exit_stats_label = QLabel("—")
        self._exit_stats_label.setStyleSheet(
            "font-family:'JetBrains Mono','Consolas',monospace; font-size:14px;"
            "font-weight:700; color:%s; background:transparent; border:none;"
            "padding-top:12px;" % C["amber"]
        )
        self._exit_stats_label.setAlignment(Qt.AlignCenter)
        exit_lay.addWidget(self._exit_stats_label)

        exit_lay.addStretch()
        left_stack.addWidget(self._exit_panel)
        self._exit_panel.setVisible(False)

        body_lay.addWidget(self._left_container)

        # Right panel — SPY bubble chart (shared between tabs)
        right_panel = QFrame()
        right_panel.setStyleSheet(
            "QFrame { background:#000; }"
        )
        right_lay = QVBoxLayout(right_panel)
        right_lay.setContentsMargins(0, 0, 0, 0)

        self._spy_chart = SpyBubbleChart()
        right_lay.addWidget(self._spy_chart)

        body_lay.addWidget(right_panel, 1)

        lay.addWidget(body, 1)

        # Connect entry sliders
        self._setup_slider.valueChanged.connect(self._on_slider_changed)
        self._market_slider.valueChanged.connect(self._on_slider_changed)
        self._depth_slider.valueChanged.connect(self._on_slider_changed)
        self._wr_slider.valueChanged.connect(self._on_slider_changed)
        self._trim_slider.valueChanged.connect(self._on_slider_changed)

    # ── Tab switching (called by FlowchartCanvas tab click) ──
    def _set_tab(self, tab):
        if tab == self._tab:
            return
        self._tab = tab
        self._entry_panel.setVisible(tab == "entry")
        self._exit_panel.setVisible(tab == "exit")
        self._apply_filters()

    # ── Objective toggle ──
    def _set_objective(self, obj):
        for key, btn in self._obj_btns.items():
            btn.setChecked(key == obj)
        self._update_obj_btn_styles()

    def _update_obj_btn_styles(self):
        for key, btn in self._obj_btns.items():
            active = btn.isChecked()
            if active:
                btn.setStyleSheet(
                    "QPushButton { background:#222; color:#E0E0E0; border:none;"
                    "font-family:'JetBrains Mono','Consolas',monospace; font-size:10px;"
                    "font-weight:700; padding:2px 8px; }")
            else:
                btn.setStyleSheet(
                    "QPushButton { background:transparent; color:#555; border:none;"
                    "font-family:'JetBrains Mono','Consolas',monospace; font-size:10px;"
                    "font-weight:500; padding:2px 8px; }"
                    "QPushButton:hover { color:#888; }")

    # ── Event handling ──
    def mousePressEvent(self, ev):
        ev.accept()  # consume — prevent canvas from collapsing workspace

    # ── Helper: create styled section labels ──
    def _make_section_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-family:'JetBrains Mono','Consolas',monospace; font-size:9px;"
            "font-weight:700; color:%s; background:transparent; border:none;"
            "letter-spacing:1px;" % C["text_muted"]
        )
        return lbl

    def _make_slider(self):
        s = QSlider(Qt.Horizontal)
        s.setRange(0, 100)
        s.setValue(0)
        s.setFixedHeight(20)
        s.setStyleSheet(
            "QSlider::groove:horizontal { background:#1A1A1A; height:4px; border-radius:2px; }"
            "QSlider::handle:horizontal { background:%s; width:12px; margin:-4px 0; border-radius:6px; }"
            "QSlider::sub-page:horizontal { background:%s; border-radius:2px; }" % (
                C["amber"], "#3d3818")
        )
        return s

    def _make_value_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-family:'JetBrains Mono','Consolas',monospace; font-size:11px;"
            "color:%s; background:transparent; border:none;" % C["text"]
        )
        lbl.setAlignment(Qt.AlignCenter)
        return lbl

    # ── Data loading ──
    def set_setup(self, setup):
        self._setup = setup

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._signals or self._loaded_setup != self._setup:
            self._load_data()

    def _load_data(self):
        """Load EV output + refinement depth_progression + profit grinder output."""
        self._loaded_setup = self._setup
        setup = self._setup
        cache_dir = REPO_ROOT / "local_runner" / "cache"

        # Load EV data
        self._ev_data = None
        self._signals = []
        if cache_dir.exists():
            ev_files = sorted(
                [f for f in cache_dir.iterdir()
                 if f.name.startswith("ev_%s_" % setup) and f.suffix == ".json"],
                key=lambda f: f.stat().st_mtime, reverse=True
            )
            if ev_files:
                try:
                    self._ev_data = json.loads(ev_files[0].read_text())
                    self._signals = self._ev_data.get("signals", [])
                except Exception as e:
                    print(f"  ScanTuning: EV load error: {e}")

        # Load depth progression from refinement output
        self._depth_progression = []
        if cache_dir.exists():
            ref_files = sorted(
                [f for f in cache_dir.iterdir()
                 if f.name.startswith("refinement_%s_" % setup) and f.suffix == ".json"],
                key=lambda f: f.stat().st_mtime, reverse=True
            )
            if ref_files:
                try:
                    ref_data = json.loads(ref_files[0].read_text())
                    self._depth_progression = ref_data.get("depth_progression", [])
                except Exception as e:
                    print(f"  ScanTuning: refinement load error: {e}")

        # Load profit grinder output
        self._profit_data = None
        if cache_dir.exists():
            profit_path = cache_dir / ("profit_%s.json" % setup)
            if profit_path.exists():
                try:
                    self._profit_data = json.loads(profit_path.read_text())
                except Exception as e:
                    print(f"  ScanTuning: profit load error: {e}")

        # Configure depth slider range
        max_depth = len(self._depth_progression)
        if max_depth > 0:
            self._depth_slider.setRange(0, max_depth)
            self._depth_slider.setValue(max_depth)
        else:
            self._depth_slider.setRange(0, 100)
            self._depth_slider.setValue(100)

        self._update_stats()
        self._update_exit_panel()
        self._spy_chart.load_spy()
        self._apply_filters()

    # ── Filtering ──
    def _on_slider_changed(self, _value=None):
        self._apply_filters()

    def _apply_filters(self):
        """Recompute surviving signals based on current slider positions."""
        if not self._signals:
            self._surviving_label.setText("No signals")
            self._update_slider_labels()
            return

        setup_floor = self._setup_slider.value()     # 0-100: percentile floor
        market_floor = self._market_slider.value()    # 0-100: percentile floor
        depth_val = self._depth_slider.value()        # 0-max_depth
        wr_floor = self._wr_slider.value() / 100.0    # 0-1.0

        surviving = []
        for s in self._signals:
            # Depth filter: killed_at_depth = depth level where this cluster was
            # fully eliminated. If slider depth >= kad, cluster is dead — skip signal.
            # kad=None means winner or never-killed loser — always survives depth filter.
            kad = s.get("killed_at_depth")
            if kad is not None and depth_val >= kad:
                continue  # cluster eliminated at this depth

            # Setup score floor
            ss = s.get("setup_score", 50.0)
            if ss < setup_floor:
                continue

            # Market score floor
            ms = s.get("market_score", 50.0)
            if ms < market_floor:
                continue

            # WR floor
            pwr = s.get("predicted_wr", 0.5)
            if pwr < wr_floor:
                continue

            surviving.append(s)

        n_win = sum(1 for s in surviving if "WIN" in s.get("classification", ""))
        n_lose = sum(1 for s in surviving if "LOSE" in s.get("classification", "").upper()
                     or "LOSS" in s.get("classification", "").upper())
        n_total = len(surviving)
        wr = n_win / n_total if n_total > 0 else 0

        self._surviving_label.setText(
            "%d signals\n%d W / %d L\n%.0f%% WR" % (n_total, n_win, n_lose, wr * 100)
        )

        self._update_slider_labels()
        self._spy_chart.set_bubbles(surviving)

    def _update_slider_labels(self):
        """Update the value labels under each slider."""
        self._setup_val_label.setText(
            "Floor: %d%%" % self._setup_slider.value() if self._setup_slider.value() > 0
            else "Off"
        )
        self._market_val_label.setText(
            "Floor: %d%%" % self._market_slider.value() if self._market_slider.value() > 0
            else "Off"
        )
        dp = self._depth_slider
        if dp.value() == dp.maximum():
            self._depth_val_label.setText("Max depth (%d)" % dp.maximum())
        else:
            self._depth_val_label.setText("Depth: %d / %d" % (dp.value(), dp.maximum()))
        self._wr_val_label.setText(
            "Floor: %d%%" % self._wr_slider.value() if self._wr_slider.value() > 0
            else "Off"
        )

    def _update_stats(self):
        """Update the top stats bar with data summary."""
        if not self._signals:
            self._stats_label.setText("No EV data — run Correlative Targeting first")
            return
        n = len(self._signals)
        n_w = sum(1 for s in self._signals if "WIN" in s.get("classification", ""))
        n_l = n - n_w

        # Check for component scores
        has_setup = any(s.get("setup_score") is not None for s in self._signals[:5])
        has_market = any(s.get("market_score") is not None for s in self._signals[:5])
        has_depth = len(self._depth_progression) > 0

        parts = ["%d signals (%d W / %d L)" % (n, n_w, n_l)]
        if has_setup:
            ss_vals = [s.get("setup_score", 50) for s in self._signals]
            parts.append("Setup: %.0f–%.0f" % (min(ss_vals), max(ss_vals)))
        if has_market:
            ms_vals = [s.get("market_score", 50) for s in self._signals]
            parts.append("Market: %.0f–%.0f" % (min(ms_vals), max(ms_vals)))
        if has_depth:
            parts.append("Depth: %d levels" % len(self._depth_progression))
        if not has_setup and not has_market:
            parts.append("⚠ Re-run EV grinder for setup/market scores")

        self._stats_label.setText("  ·  ".join(parts))

    def _update_exit_panel(self):
        """Update exit tab with profit grinder data."""
        if not self._profit_data:
            self._exit_expr_label.setText("No profit grinder data — run Optimal Management first")
            self._exit_stats_label.setText("—")
            return

        # Show top exit expression from stage 1
        s1 = self._profit_data.get("stage_1", {})
        top = s1.get("top_100", [])
        if top:
            best = top[0]
            expr_name = best.get("expr_name", best.get("expression", "?"))
            direction = best.get("direction", "?")
            threshold = best.get("threshold", 0)
            expectancy = best.get("expectancy", 0)
            self._exit_expr_label.setText(
                "%s %s %.4f\nExpectancy: %.3f ADR" % (expr_name, direction, threshold, expectancy)
            )
        else:
            self._exit_expr_label.setText("No exit candidates found")

        # Show stage 2 summary if available
        s2 = self._profit_data.get("stage_2", {})
        n_s2 = s2.get("n_candidates", 0)
        n_beating = s2.get("n_beating_1stage", 0)
        pop = self._profit_data.get("population", {})
        n_total = pop.get("total", 0)

        parts = ["%d signals" % n_total]
        if n_s2 > 0:
            parts.append("%d 2-stage combos" % n_s2)
            if n_beating > 0:
                parts.append("%d beat 1-stage" % n_beating)
        self._exit_stats_label.setText("\n".join(parts))


# ============================================================
# MINI CHART THUMBNAIL — simplified candlestick for grid cards
# ============================================================

class MiniChartWidget(QWidget):
    """Candlestick thumbnail for example grid cards — matches old web UI SVG thumbs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._candles = None
        self._entry_date = None
        self._exit_date = None
        self._profit_exit_date = None
        self.setMinimumSize(80, 50)

    def set_data(self, candles, entry_date, exit_date=None, profit_exit_date=None):
        self._candles = candles
        self._entry_date = entry_date
        self._exit_date = exit_date
        self._profit_exit_date = profit_exit_date
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#000000"))

        if not self._candles or len(self._candles) == 0:
            p.end()
            return

        candles = self._candles
        n = len(candles)
        PAD = 4
        cw = (w - PAD * 2) / n
        bw = max(1, cw * 0.6)

        prices = []
        for c in candles:
            prices.extend([c["high"], c["low"]])
        p_min = min(prices) * 0.998
        p_max = max(prices) * 1.002
        p_range = p_max - p_min
        if p_range < 0.001:
            p_range = 1.0

        def py(price):
            return PAD + (h - PAD * 2) * (1 - (price - p_min) / p_range)

        def cx(i):
            return PAD + i * cw + cw / 2

        # Find entry index
        entry_idx = -1
        for i, c in enumerate(candles):
            if c["date"] == self._entry_date:
                entry_idx = i
                break

        # Entry marker — white vertical line + highlight
        if entry_idx >= 0:
            ex = cx(entry_idx)
            p.fillRect(QRectF(ex - cw, PAD, cw * 2, h - PAD * 2), QColor(255, 255, 255, 15))
            p.setPen(QPen(QColor(224, 224, 224, 128), 1))
            p.drawLine(int(ex), PAD, int(ex), h - PAD)

        # EMA 8 + EMA 21 lines
        closes = [c["close"] for c in candles]
        for period, alpha in [(8, 77), (21, 50)]:
            ema_vals = _compute_ema(closes, period)
            path = QPainterPath()
            started = False
            for i, v in enumerate(ema_vals):
                if v is None:
                    continue
                x = cx(i)
                y = py(v)
                if not started:
                    path.moveTo(x, y)
                    started = True
                else:
                    path.lineTo(x, y)
            if started:
                p.setPen(QPen(QColor(170, 170, 170, alpha), 0.7))
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)

        # Candles
        for i, c in enumerate(candles):
            x = cx(i)
            is_up = c["close"] >= c["open"]
            color = QColor("#00e87b" if is_up else "#ff3b3b")
            # Wick
            p.setPen(QPen(color, 0.5))
            p.drawLine(int(x), int(py(c["high"])), int(x), int(py(c["low"])))
            # Body
            bt = py(max(c["open"], c["close"]))
            bb = py(min(c["open"], c["close"]))
            bh = max(0.5, bb - bt)
            col70 = QColor(color)
            col70.setAlpha(180)
            p.setPen(Qt.NoPen)
            p.setBrush(col70)
            p.drawRect(QRectF(x - bw / 2, bt, bw, bh))

        # Exit signal marker — amber line
        if self._exit_date:
            for i, c in enumerate(candles):
                if c["date"] == self._exit_date:
                    ex = cx(i)
                    p.setPen(QPen(QColor(232, 167, 53, 180), 1))
                    p.drawLine(int(ex), PAD, int(ex), h - PAD)
                    break

        # White dot on entry candle close
        if entry_idx >= 0:
            ec = candles[entry_idx]
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(224, 224, 224, 200))
            p.drawEllipse(QPointF(cx(entry_idx), py(ec["close"])), 2.5, 2.5)

        p.end()


# ============================================================
# EXAMPLES WORKSPACE — example library + pending review
# ============================================================

class ExamplesWorkspace(QFrame):
    """Examples workspace — matches the old localized web UI exactly."""

    def __init__(self, node_id="examples", parent=None):
        super().__init__(parent)
        self.node_id = node_id
        self._setup = "dtss"
        self._sort = "ticker"
        self._examples = []
        self._pending = []
        self.setStyleSheet("ExamplesWorkspace { background:#000; border:1px solid %s; }" % C["border"])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Scrollable body ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setStyleSheet("background:#000;")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(20, 12, 20, 20)
        bl.setSpacing(16)

        # Count + sub label row
        count_row = QHBoxLayout()
        self._sub_label = QLabel("DTSS")
        self._sub_label.setStyleSheet("font-size:12px; color:#888; background:transparent; border:none;")
        count_row.addWidget(self._sub_label)
        count_row.addStretch()
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("font-size:13px; color:#888; background:transparent; border:none;")
        count_row.addWidget(self._count_label)
        bl.addLayout(count_row)

        # ── Top row: Add Examples (left) + Setup Description (right) ──
        top_row = QHBoxLayout()
        top_row.setSpacing(16)

        # Left: Add Examples collapsible
        add_frame = QFrame()
        add_frame.setStyleSheet("QFrame { border:1px solid #1A1A1A; background:#000; }")
        af_lay = QVBoxLayout(add_frame)
        af_lay.setContentsMargins(0, 0, 0, 0)
        af_lay.setSpacing(0)

        self._add_hdr = QPushButton("ADD EXAMPLES")
        self._add_hdr.setStyleSheet(
            "QPushButton { background:#0A0A0A; color:#888; border:none; text-align:left;"
            "font-size:11px; font-weight:500; letter-spacing:1px; padding:10px 15px; }"
            "QPushButton:hover { background:#111; }")
        self._add_hdr.clicked.connect(self._toggle_add)
        af_lay.addWidget(self._add_hdr)

        self._add_body = QWidget()
        self._add_body.setVisible(False)
        ab = QVBoxLayout(self._add_body)
        ab.setContentsMargins(15, 10, 15, 15)
        ab.setSpacing(8)

        # Single add
        sr = QHBoxLayout()
        sr.setSpacing(8)
        self._inp_tk = QLineEdit()
        self._inp_tk.setPlaceholderText("TICKER")
        self._inp_tk.setFixedWidth(90)
        self._inp_tk.setStyleSheet(
            "QLineEdit { background:#000; border:1px solid #2A2A2A; color:#E0E0E0;"
            "font-size:13px; padding:6px 8px; }")
        sr.addWidget(self._inp_tk)
        self._inp_dt = QLineEdit()
        self._inp_dt.setPlaceholderText("MM/DD/YYYY")
        self._inp_dt.setFixedWidth(120)
        self._inp_dt.setStyleSheet(
            "QLineEdit { background:#000; border:1px solid #2A2A2A; color:#E0E0E0;"
            "font-size:13px; padding:6px 8px; }")
        sr.addWidget(self._inp_dt)
        add_btn = QPushButton("ADD")
        add_btn.setStyleSheet(
            "QPushButton { background:#E0E0E0; color:#000; border:none;"
            "font-size:12px; font-weight:700; padding:7px 16px; }"
            "QPushButton:hover { background:#fff; }")
        add_btn.clicked.connect(self._add_single)
        sr.addWidget(add_btn)
        sr.addStretch()
        ab.addLayout(sr)

        # Bulk paste
        self._bulk = QTextEdit()
        self._bulk.setPlaceholderText("Paste list \u2014 one per line: TICKER MM/DD/YYYY")
        self._bulk.setFixedHeight(80)
        self._bulk.setStyleSheet(
            "QTextEdit { background:#000; border:1px solid #2A2A2A; color:#888;"
            "font-size:12px; padding:6px; }")
        ab.addWidget(self._bulk)

        imp_row = QHBoxLayout()
        imp_btn = QPushButton("IMPORT ALL")
        imp_btn.setStyleSheet(
            "QPushButton { background:transparent; color:#E0E0E0; border:1px solid #2A2A2A;"
            "font-size:11px; font-weight:600; padding:5px 12px; }"
            "QPushButton:hover { background:#111; }")
        imp_btn.clicked.connect(self._bulk_add)
        imp_row.addWidget(imp_btn)
        self._add_msg = QLabel("")
        self._add_msg.setStyleSheet("font-size:12px; color:#888; background:transparent; border:none;")
        imp_row.addWidget(self._add_msg)
        imp_row.addStretch()
        ab.addLayout(imp_row)

        # Pending review area (inside add section)
        self._pend_area = QWidget()
        self._pend_area.setVisible(False)
        self._pend_lay = QVBoxLayout(self._pend_area)
        self._pend_lay.setContentsMargins(0, 12, 0, 0)
        self._pend_lay.setSpacing(8)
        ab.addWidget(self._pend_area)

        af_lay.addWidget(self._add_body)
        top_row.addWidget(add_frame, 1)

        # Right: Setup Description
        desc_frame = QFrame()
        desc_frame.setStyleSheet("QFrame { border:none; background:#000; }")
        df_lay = QVBoxLayout(desc_frame)
        df_lay.setContentsMargins(0, 0, 0, 0)
        df_lay.setSpacing(4)
        dh = QHBoxLayout()
        dl = QLabel("SETUP DESCRIPTION")
        dl.setStyleSheet("font-size:10px; font-weight:500; letter-spacing:1px; color:#555;"
                         "background:transparent; border:none;")
        dh.addWidget(dl)
        dh.addStretch()
        save_btn = QPushButton("SAVE")
        save_btn.setStyleSheet(
            "QPushButton { background:transparent; color:#888; border:1px solid #2A2A2A;"
            "font-size:10px; font-weight:600; padding:2px 8px; }"
            "QPushButton:hover { background:#111; }")
        save_btn.clicked.connect(self._save_desc)
        dh.addWidget(save_btn)
        df_lay.addLayout(dh)
        self._desc_edit = QTextEdit()
        self._desc_edit.setStyleSheet(
            "QTextEdit { background:#000; border:1px solid #1A1A1A; color:#E0E0E0;"
            "font-size:13px; padding:8px; }")
        self._desc_edit.setMinimumHeight(120)
        df_lay.addWidget(self._desc_edit)
        top_row.addWidget(desc_frame, 1)
        bl.addLayout(top_row)

        # ── Legend + Sort bar ──
        ls_bar = QHBoxLayout()
        ls_bar.setContentsMargins(0, 4, 0, 4)
        for label, color in [("Entry", "#E0E0E0"), ("Exit Signal", "#E8A735"), ("Profit Exit", "#A855F7")]:
            dot = QLabel("\u25a0")
            dot.setStyleSheet("font-size:10px; color:%s; background:transparent; border:none;" % color)
            ls_bar.addWidget(dot)
            ll = QLabel(label)
            ll.setStyleSheet("font-size:11px; color:#888; background:transparent; border:none;")
            ls_bar.addWidget(ll)
            ls_bar.addSpacing(12)
        ls_bar.addStretch()
        self._sort_btns = {}
        for key, label in [("adr", "ADR MOVE \u2193"), ("ticker", "TICKER"), ("date", "DATE")]:
            btn = QPushButton(label)
            btn.setFixedHeight(20)
            btn.setCheckable(True)
            btn.setChecked(key == self._sort)
            btn.clicked.connect(lambda checked, k=key: self._set_sort(k))
            ls_bar.addWidget(btn)
            self._sort_btns[key] = btn
        self._style_sort()
        bl.addLayout(ls_bar)

        # ── Chart grid ──
        self._grid_w = QWidget()
        self._grid_lay = QGridLayout(self._grid_w)
        self._grid_lay.setSpacing(12)
        self._grid_lay.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self._grid_w)

        bl.addStretch()
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)

    def set_setup(self, setup):
        self._setup = setup

    def showEvent(self, ev):
        super().showEvent(ev)
        self._load_data()

    def _style_sort(self):
        for k, b in self._sort_btns.items():
            active = k == self._sort
            b.setChecked(active)
            if active:
                b.setStyleSheet(
                    "QPushButton { background:#222; color:#E0E0E0; border:none;"
                    "font-size:11px; font-weight:700; padding:2px 8px; }")
            else:
                b.setStyleSheet(
                    "QPushButton { background:transparent; color:#555; border:none;"
                    "font-size:11px; font-weight:500; padding:2px 8px; }"
                    "QPushButton:hover { color:#888; }")

    def _set_sort(self, k):
        self._sort = k
        self._style_sort()
        self._rebuild_grid()

    def _toggle_add(self):
        vis = not self._add_body.isVisible()
        self._add_body.setVisible(vis)
        self._update_add_hdr()

    def _update_add_hdr(self):
        arrow = "\u25be" if self._add_body.isVisible() else "\u25b8"
        pend_txt = " (%d PENDING REVIEW)" % len(self._pending) if self._pending else ""
        self._add_hdr.setText("%s  ADD EXAMPLES%s" % (arrow, pend_txt))

    def _load_data(self):
        setup = self._setup
        self._sub_label.setText(setup.upper())
        self._examples = []
        self._pending = []
        try:
            with get_db() as db:
                self._examples = [dict(r) for r in db.execute(
                    "SELECT id, ticker, entry_date, chart_date FROM examples WHERE setup_type=?",
                    (setup,)).fetchall()]
        except Exception:
            pass
        try:
            with get_db() as db:
                self._pending = [dict(r) for r in db.execute(
                    "SELECT id, ticker, signal_date, entry_date, status "
                    "FROM pending_examples WHERE setup_type=? ORDER BY created_at DESC",
                    (setup,)).fetchall()]
        except Exception:
            pass

        # Enrich pending with ADR + entry candle %
        entry_scores = {}
        es_path = REPO_ROOT / "local_runner" / "cache" / ("entry_scores_%s.json" % setup)
        if es_path.exists():
            try:
                es_data = json.loads(es_path.read_text())
                for sc in es_data.get("scored_signals", []):
                    entry_scores["%s_%s" % (sc.get("ticker", ""), sc.get("signal_date", ""))] = sc
            except Exception:
                pass
        for p in self._pending:
            # ADR from OHLCV pickle
            df = _get_ohlcv(p["ticker"])
            if df is not None and not df.empty:
                try:
                    ranges = (df["high"] - df["low"]).rolling(14).mean()
                    if hasattr(df["date"].iloc[0], "strftime"):
                        dates = [d.strftime("%Y-%m-%d") for d in df["date"]]
                    else:
                        dates = [str(d) for d in df["date"]]
                    idx = None
                    for i, d in enumerate(dates):
                        if d == p["entry_date"]:
                            idx = i
                            break
                    if idx is not None and idx < len(ranges):
                        p["adr"] = round(float(ranges.iloc[idx]), 2) if pd.notna(ranges.iloc[idx]) else None
                except Exception:
                    pass
            # Entry candle % from scores file
            sc = entry_scores.get("%s_%s" % (p["ticker"], p.get("signal_date", "")))
            if sc:
                p["entry_candle_pct"] = sc.get("entry_candle_pct")

        # Load setup description
        try:
            with get_db() as db:
                row = db.execute("SELECT description FROM setups WHERE setup_type=?", (setup,)).fetchone()
            if row:
                self._desc_edit.setPlainText(row["description"] or "")
        except Exception:
            pass

        # Load profit exit lookup for chart markers
        self._profit_exit_lookup = {}
        # Profit exit dates
        profit_path = REPO_ROOT / "data" / "profit_grind" / ("profit_%s.json" % setup)
        if profit_path.exists():
            try:
                profit_data = json.loads(profit_path.read_text())
                for pk, pv in profit_data.get("exit_dates", {}).items():
                    # Key format: TICKER|entry_date
                    parts = pk.split("|", 1)
                    if len(parts) == 2:
                        self._profit_exit_lookup["%s_%s" % (parts[0], parts[1])] = pv
            except Exception:
                pass

        ne = len(self._examples)
        np_ = len(self._pending)
        parts = ["%d examples" % ne]
        if np_:
            parts.append("%d pending" % np_)
        if ne < 20:
            parts.append("need %d more" % (20 - ne))
        self._count_label.setText("  \u00b7  ".join(parts))

        if np_ > 0 and not self._add_body.isVisible():
            self._add_body.setVisible(True)
        self._update_add_hdr()
        self._rebuild_pending()
        self._rebuild_grid()

    def _rebuild_pending(self):
        while self._pend_lay.count():
            item = self._pend_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not self._pending:
            self._pend_area.setVisible(False)
            return
        self._pend_area.setVisible(True)

        lbl = QLabel("PENDING FINAL REVIEW")
        lbl.setStyleSheet("font-size:10px; font-weight:500; letter-spacing:1px; color:#555;"
                          "background:transparent; border:none;")
        self._pend_lay.addWidget(lbl)

        pg = QWidget()
        pg_lay = QGridLayout(pg)
        pg_lay.setSpacing(12)
        pg_lay.setContentsMargins(0, 4, 0, 0)
        cols = 4
        for i, p in enumerate(self._pending):
            card = self._make_card(p, "pending")
            pg_lay.addWidget(card, i // cols, i % cols)
        self._pend_lay.addWidget(pg)

    def _rebuild_grid(self):
        while self._grid_lay.count():
            item = self._grid_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        exs = list(self._examples)
        if self._sort == "date":
            exs.sort(key=lambda x: x.get("entry_date", ""), reverse=True)
        else:
            exs.sort(key=lambda x: x.get("ticker", ""))

        cols = 4
        if not exs:
            lbl = QLabel("No examples yet. Open Add Examples above.")
            lbl.setStyleSheet("font-size:12px; color:#555; padding:40px; background:transparent; border:none;")
            lbl.setAlignment(Qt.AlignCenter)
            self._grid_lay.addWidget(lbl, 0, 0, 1, cols)
            return
        for i, ex in enumerate(exs):
            card = self._make_card(ex, "banked")
            self._grid_lay.addWidget(card, i // cols, i % cols)

        # Defer chart loading — process in batches via QTimer so UI appears instantly
        QTimer.singleShot(50, self._load_deferred_charts)

    def _load_deferred_charts(self):
        """Load candle data in batches of 4 so the UI stays responsive."""
        charts = self._find_deferred_charts(self._grid_w)
        charts += self._find_deferred_charts(self._pend_area)
        self._deferred_queue = charts
        self._load_next_batch()

    def _load_next_batch(self):
        """Load up to 4 charts, then schedule next batch."""
        for _ in range(4):
            if not self._deferred_queue:
                return
            chart = self._deferred_queue.pop(0)
            tk = getattr(chart, "_deferred_ticker", "")
            ed = getattr(chart, "_deferred_entry", "")
            if not tk:
                continue
            candles = _prepare_candles(tk, ed, lookback=80, forward=40)
            if candles:
                exit_dt = _compute_exit_date(candles, ed)
                profit_dt = getattr(self, "_profit_exit_lookup", {}).get("%s_%s" % (tk, ed))
                chart.set_data(candles, ed, exit_date=exit_dt, profit_exit_date=profit_dt)
            chart._deferred_ticker = ""
        if self._deferred_queue:
            QTimer.singleShot(16, self._load_next_batch)

    def _find_deferred_charts(self, parent):
        """Find all MiniChartWidgets with deferred data under a parent."""
        charts = []
        if parent is None:
            return charts
        for child in parent.findChildren(MiniChartWidget):
            if getattr(child, "_deferred_ticker", ""):
                charts.append(child)
        return charts

    def _make_card(self, data, section):
        """Chart card: tall chart filling card, label row below. Matches old web UI."""
        card = QFrame()
        card.setStyleSheet("QFrame { background:#0A0A0A; border:1px solid #2A2A2A; }")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        ticker = data.get("ticker", "")
        entry_date = data.get("entry_date", "")

        # Chart — defer loading for speed (loaded via _load_visible_charts after grid built)
        chart = MiniChartWidget()
        chart.setMinimumHeight(180)
        chart._deferred_ticker = ticker
        chart._deferred_entry = entry_date
        chart._deferred_signal = data.get("signal_date", "")
        cl.addWidget(chart, 1)

        # Label row below chart
        lbl_row = QFrame()
        lbl_row.setFixedHeight(24)
        lbl_row.setStyleSheet("QFrame { background:#000; border-top:1px solid #1A1A1A; }")
        lr = QHBoxLayout(lbl_row)
        lr.setContentsMargins(8, 0, 8, 0)
        lr.setSpacing(6)
        mono = "font-family:'JetBrains Mono','Consolas',monospace;"

        if section == "pending":
            # ADR label
            adr = data.get("adr")
            if adr:
                adr_lbl = QLabel("%.1f%%" % adr)
                adr_lbl.setStyleSheet(
                    "%s font-size:9px; font-weight:600; color:#fbbf24; padding:1px 5px;"
                    "background:rgba(251,191,36,0.08); border:1px solid rgba(251,191,36,0.2);" % mono)
                lr.addWidget(adr_lbl)
            # Entry candle % label
            ecp = data.get("entry_candle_pct")
            if ecp is not None:
                ecp_lbl = QLabel("%.0f%% match" % (ecp * 100))
                ecp_lbl.setStyleSheet(
                    "%s font-size:9px; font-weight:600; color:#60a5fa; padding:1px 5px;"
                    "background:rgba(96,165,250,0.08); border:1px solid rgba(96,165,250,0.2);" % mono)
                lr.addWidget(ecp_lbl)

        tk_lbl = QLabel(ticker)
        tk_lbl.setStyleSheet("%s font-size:13px; font-weight:500; color:#fff;"
                             "background:transparent; border:none;" % mono)
        lr.addWidget(tk_lbl)
        dt_lbl = QLabel(entry_date)
        dt_lbl.setStyleSheet("%s font-size:11px; color:#B0B0B0;"
                             "background:transparent; border:none;" % mono)
        lr.addWidget(dt_lbl)
        lr.addStretch()

        if section == "pending":
            appr = QPushButton("Approve")
            appr.setFixedHeight(18)
            appr.setStyleSheet(
                "QPushButton { background:transparent; color:#4ade80; border:1px solid rgba(0,200,100,0.3);"
                "%s font-size:10px; font-weight:600; padding:0px 6px; }"
                "QPushButton:hover { background:rgba(0,200,100,0.15); }" % mono)
            appr.clicked.connect(lambda c, d=data: self._approve_one(d))
            lr.addWidget(appr)
            rej = QPushButton("Reject")
            rej.setFixedHeight(18)
            rej.setStyleSheet(
                "QPushButton { background:transparent; color:#f87171; border:1px solid rgba(255,80,80,0.3);"
                "%s font-size:10px; font-weight:600; padding:0px 6px; }"
                "QPushButton:hover { background:rgba(255,80,80,0.15); }" % mono)
            rej.clicked.connect(lambda c, d=data: self._reject_one(d))
            lr.addWidget(rej)
        elif section == "banked":
            del_btn = QPushButton("\u00d7")
            del_btn.setFixedSize(18, 18)
            del_btn.setStyleSheet(
                "QPushButton { background:transparent; color:#555; border:none;"
                "font-size:14px; font-weight:700; }"
                "QPushButton:hover { color:#f87171; }")
            del_btn.clicked.connect(lambda c, d=data: self._delete_example(d))
            lr.addWidget(del_btn)

        cl.addWidget(lbl_row)
        return card

    # ── Add examples ──

    def _add_single(self):
        tk = self._inp_tk.text().strip().upper()
        dt = self._inp_dt.text().strip()
        if not tk or not dt:
            self._add_msg.setText("Enter both ticker and date")
            self._add_msg.setStyleSheet("font-size:12px; color:#f87171; background:transparent; border:none;")
            return
        if "/" in dt:
            try:
                parts = dt.split("/")
                dt = "%s-%s-%s" % (parts[2], parts[0].zfill(2), parts[1].zfill(2))
            except Exception:
                pass
        try:
            with get_db() as db:
                if db.execute("SELECT id FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?",
                              (self._setup, tk, dt)).fetchone():
                    self._add_msg.setText("Already exists"); return
                db.execute("INSERT INTO examples (setup_type, ticker, chart_date, entry_date) VALUES (?,?,?,?)",
                           (self._setup, tk, dt, dt))
            self._inp_tk.clear(); self._inp_dt.clear()
            self._add_msg.setText("Added %s" % tk)
            self._add_msg.setStyleSheet("font-size:12px; color:#4ade80; background:transparent; border:none;")
            self._load_data()
        except Exception as e:
            self._add_msg.setText("Error: %s" % e)
            self._add_msg.setStyleSheet("font-size:12px; color:#f87171; background:transparent; border:none;")

    def _bulk_add(self):
        raw = self._bulk.toPlainText().strip()
        if not raw:
            return
        added = failed = 0
        for line in raw.splitlines():
            parts = line.strip().split()
            if len(parts) < 2:
                failed += 1; continue
            tk, dt = parts[0].upper(), parts[1]
            if "/" in dt:
                try:
                    p = dt.split("/"); dt = "%s-%s-%s" % (p[2], p[0].zfill(2), p[1].zfill(2))
                except Exception:
                    pass
            try:
                with get_db() as db:
                    if not db.execute("SELECT id FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?",
                                      (self._setup, tk, dt)).fetchone():
                        db.execute("INSERT INTO examples (setup_type, ticker, chart_date, entry_date) VALUES (?,?,?,?)",
                                   (self._setup, tk, dt, dt)); added += 1
                    else:
                        failed += 1
            except Exception:
                failed += 1
        self._add_msg.setText("%d added%s" % (added, ", %d failed" % failed if failed else ""))
        self._add_msg.setStyleSheet("font-size:12px; color:%s; background:transparent; border:none;" % (
            "#4ade80" if not failed else "#f87171"))
        if added:
            self._bulk.clear(); self._load_data()

    def _save_desc(self):
        try:
            with get_db() as db:
                db.execute("UPDATE setups SET description=? WHERE setup_type=?",
                           (self._desc_edit.toPlainText(), self._setup))
        except Exception:
            pass

    # ── Actions ──

    def _approve_one(self, p):
        try:
            with get_db() as db:
                tk, ed = p["ticker"], p["entry_date"]
                if not db.execute("SELECT id FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?",
                                  (self._setup, tk, ed)).fetchone():
                    db.execute("INSERT INTO examples (setup_type, ticker, chart_date, entry_date) VALUES (?,?,?,?)",
                               (self._setup, tk, ed, ed))
                db.execute("DELETE FROM pending_examples WHERE id=?", (p["id"],))
        except Exception as e:
            print("Approve error: %s" % e)
        self._load_data()

    def _reject_one(self, p):
        try:
            with get_db() as db:
                db.execute("INSERT OR IGNORE INTO rejected_signals (setup_type, ticker, signal_date) VALUES (?,?,?)",
                           (self._setup, p["ticker"], p.get("signal_date", p.get("entry_date", ""))))
                db.execute("DELETE FROM pending_examples WHERE id=?", (p["id"],))
        except Exception as e:
            print("Reject error: %s" % e)
        self._load_data()

    def _delete_example(self, ex):
        try:
            with get_db() as db:
                db.execute("DELETE FROM examples WHERE id=?", (ex["id"],))
        except Exception as e:
            print("Delete error: %s" % e)
        self._load_data()


# ============================================================
# MA HELPERS — EMA / SMA computation (matches web UI)
# ============================================================

def _compute_ema(data, period):
    """Compute EMA. data is a list of floats (or None). Returns list of same length."""
    k = 2.0 / (period + 1)
    result = [None] * len(data)
    prev = None
    for i, v in enumerate(data):
        if v is None:
            continue
        if prev is None:
            prev = v
            result[i] = prev
        else:
            prev = v * k + prev * (1 - k)
            result[i] = prev
    return result


def _compute_sma(data, period):
    """Compute SMA. data is a list of floats (or None). Returns list of same length."""
    result = [None] * len(data)
    for i in range(period - 1, len(data)):
        vals = [data[j] for j in range(i - period + 1, i + 1) if data[j] is not None]
        if len(vals) == period:
            result[i] = sum(vals) / period
    return result


def _compute_exit_date(candles, entry_date):
    """Find exit date by applying the DTSS exit condition to OHLCV data.
    Exit condition: slope_xavgc21_off7_adr14 <= -1.128826
    i.e. (EMA21[i] - EMA21[i-7]) / ADR14[i] <= -1.128826
    Scans forward from entry bar. Returns date string or None.
    """
    closes = [c["close"] for c in candles]
    ranges = [c["high"] - c["low"] for c in candles]
    ema21 = _compute_ema(closes, 21)
    adr14 = _compute_sma(ranges, 14)

    # Find entry bar index
    entry_idx = None
    for i, c in enumerate(candles):
        if c["date"] == entry_date:
            entry_idx = i
            break
    if entry_idx is None:
        return None

    # Scan forward from entry
    for i in range(entry_idx + 1, len(candles)):
        if i < 7:
            continue
        e21_now = ema21[i]
        e21_ago = ema21[i - 7]
        adr = adr14[i]
        if e21_now is None or e21_ago is None or adr is None or adr < 0.001:
            continue
        slope = (e21_now - e21_ago) / adr
        if slope <= -1.128826:
            return candles[i]["date"]
    return None



def _prepare_candles(ticker, signal_date, lookback=250, forward=80):
    """Load OHLCV from cache, slice around signal, compute MAs. Returns list of dicts."""
    df = _get_ohlcv(ticker)
    if df is None or df.empty:
        return None
    # Ensure date column is string for comparison
    if hasattr(df["date"].iloc[0], "strftime"):
        dates = [d.strftime("%Y-%m-%d") for d in df["date"]]
    else:
        dates = [str(d) for d in df["date"]]
    # Find signal index
    try:
        sig_idx = dates.index(signal_date)
    except ValueError:
        # Find closest
        try:
            target = datetime.strptime(signal_date, "%Y-%m-%d")
            sig_idx = min(range(len(dates)),
                         key=lambda i: abs((datetime.strptime(dates[i], "%Y-%m-%d") - target).days))
        except Exception:
            return None
    start = max(0, sig_idx - lookback)
    end = min(len(df), sig_idx + forward)
    sliced = df.iloc[start:end]
    # Build candle dicts
    candles = []
    for _, r in sliced.iterrows():
        d = r["date"]
        if hasattr(d, "strftime"):
            d = d.strftime("%Y-%m-%d")
        else:
            d = str(d)
        candles.append({
            "date": d,
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["volume"]),
        })
    # Compute MAs
    closes = [c["close"] for c in candles]
    vols = [c["volume"] for c in candles]
    ema8 = _compute_ema(closes, 8)
    ema21 = _compute_ema(closes, 21)
    sma50 = _compute_sma(closes, 50)
    sma200 = _compute_sma(closes, 200)
    vol_avg = _compute_sma(vols, 20)
    for i, c in enumerate(candles):
        c["ema8"] = ema8[i]
        c["ema21"] = ema21[i]
        c["sma50"] = sma50[i]
        c["sma200"] = sma200[i]
        c["vol_avg20"] = vol_avg[i]
    return candles


# ============================================================
# SPY BUBBLE CHART — candlestick chart with signal overlay bubbles
# ============================================================

class SpyBubbleChart(QWidget):
    """SPY candlestick chart with colored bubbles for each surviving signal.
    Green = winner (sized by move_adr), Red = loser (fixed small size).
    Used by ScanTuningWorkspace as the visual feedback chart."""

    UP_COLOR = "#4ade80"
    DOWN_COLOR = "#f87171"
    BG_COLOR = "#000000"
    GRID_COLOR = "#1A1A1A"
    EMA21_COLOR = "#d4a853"
    SMA50_COLOR = "#f5c542"
    MARGIN_TOP = 24
    MARGIN_RIGHT = 56
    VOL_H = 40
    BUBBLE_MIN = 3
    BUBBLE_MAX = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(200)

        self._candles = []
        self._date_index = {}     # date_str -> candle index
        self._bubbles = []        # list of {"date", "is_winner", "move_adr", "ticker"}
        self._hover_idx = None
        self._scroll_offset = 0
        self._visible_count = 500
        self._max_move_adr = 1.0  # for bubble scaling
        self._drag_start_x = None
        self._drag_start_offset = None
        self._dragging = False

    def load_spy(self):
        """Load SPY candles from OHLCV cache."""
        df = _get_ohlcv("SPY")
        if df is None or df.empty:
            self._candles = []
            self._date_index = {}
            self.update()
            return
        # Convert full history to candle dicts
        candles = []
        for _, r in df.iterrows():
            d = r["date"]
            if hasattr(d, "strftime"):
                d = d.strftime("%Y-%m-%d")
            else:
                d = str(d)
            candles.append({
                "date": d,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            })
        # Compute MAs
        closes = [c["close"] for c in candles]
        ema21 = _compute_ema(closes, 21)
        sma50 = _compute_sma(closes, 50)
        for i, c in enumerate(candles):
            c["ema21"] = ema21[i]
            c["sma50"] = sma50[i]
        self._candles = candles
        self._date_index = {c["date"]: i for i, c in enumerate(candles)}
        # Default: show last 500 bars
        self._scroll_offset = max(0, len(candles) - self._visible_count)
        self.update()

    def set_bubbles(self, signals):
        """Update bubble overlay from filtered signal list.
        Each signal dict needs: date, classification, move_adr (optional), ticker."""
        self._bubbles = []
        max_adr = 1.0
        for s in signals:
            is_win = "WIN" in s.get("classification", "")
            madr = s.get("move_adr") or 0
            if is_win and madr > max_adr:
                max_adr = madr
            self._bubbles.append({
                "date": s.get("date", ""),
                "is_winner": is_win,
                "move_adr": madr,
                "ticker": s.get("ticker", ""),
            })
        self._max_move_adr = max(max_adr, 1.0)
        self.update()

    def _visible_slice(self):
        n = len(self._candles)
        max_off = max(0, n - self._visible_count)
        off = min(max(0, self._scroll_offset), max_off)
        return self._candles[off:off + self._visible_count], off

    def _chart_geometry(self):
        w = self.width()
        h = self.height()
        chart_h = h - self.MARGIN_TOP - self.VOL_H
        chart_w = w - self.MARGIN_RIGHT
        return w, h, chart_w, chart_h

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(self.BG_COLOR))

        if not self._candles:
            p.setPen(QColor(C["text_muted"]))
            f = QFont("DM Sans", 12)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, "Loading SPY data...")
            p.end()
            return

        visible, offset = self._visible_slice()
        if not visible:
            p.end()
            return

        w, h, chart_w, chart_h = self._chart_geometry()
        n_vis = len(visible)
        candle_w = chart_w / n_vis if n_vis else 1
        body_w = max(1, candle_w * 0.55)

        # Price range
        prices = []
        for c in visible:
            prices.extend([c["high"], c["low"]])
        p_min = min(prices) * 0.998
        p_max = max(prices) * 1.002
        p_range = max(p_max - p_min, 0.01)

        def price_y(price):
            return self.MARGIN_TOP + chart_h * (1 - (price - p_min) / p_range)

        def candle_x(i):
            return i * candle_w + candle_w / 2

        # Grid lines + price labels
        f = QFont("JetBrains Mono", 1)
        f.setPixelSize(9)
        p.setFont(f)
        for i in range(6):
            y = self.MARGIN_TOP + (chart_h / 5) * i
            p.setPen(QPen(QColor(self.GRID_COLOR), 0.5))
            p.drawLine(0, int(y), chart_w, int(y))
            price = p_max - (p_range / 5) * i
            p.setPen(QColor(C["text_muted"]))
            p.drawText(QRectF(chart_w + 2, y - 6, self.MARGIN_RIGHT - 4, 12),
                       Qt.AlignLeft | Qt.AlignVCenter, "%.0f" % price)

        # SPY watermark
        p.setPen(QColor(255, 255, 255, 10))
        f2 = QFont("DM Sans", 1)
        f2.setPixelSize(40)
        f2.setWeight(QFont.Bold)
        p.setFont(f2)
        p.drawText(QRectF(chart_w - 120, self.MARGIN_TOP, 110, 48),
                   Qt.AlignRight | Qt.AlignTop, "SPY")

        # MAs
        def draw_ma(key, color):
            p.setPen(QPen(QColor(color), 1.0))
            path = QPainterPath()
            started = False
            for i, c in enumerate(visible):
                v = c.get(key)
                if v is None:
                    continue
                x = candle_x(i)
                y = price_y(v)
                if not started:
                    path.moveTo(x, y)
                    started = True
                else:
                    path.lineTo(x, y)
            if started:
                p.drawPath(path)

        draw_ma("ema21", self.EMA21_COLOR)
        draw_ma("sma50", self.SMA50_COLOR)

        # Candles
        for i, c in enumerate(visible):
            x = candle_x(i)
            is_up = c["close"] >= c["open"]
            col = QColor(self.UP_COLOR if is_up else self.DOWN_COLOR)
            col.setAlpha(140)
            # Wick
            p.setPen(QPen(col, 0.5))
            p.drawLine(int(x), int(price_y(c["high"])), int(x), int(price_y(c["low"])))
            # Body
            bt = price_y(max(c["open"], c["close"]))
            bb = price_y(min(c["open"], c["close"]))
            bh = max(1, bb - bt)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRect(QRectF(x - body_w / 2, bt, body_w, bh))

        # Date labels
        p.setPen(QColor(C["text_muted"]))
        f.setPixelSize(9)
        f.setWeight(QFont.Normal)
        p.setFont(f)
        step = max(1, n_vis // 10)
        for i, c in enumerate(visible):
            if i % step == 0:
                parts = c["date"].split("-")
                if len(parts) >= 3:
                    p.drawText(QRectF(candle_x(i) - 20, h - 12, 40, 12),
                               Qt.AlignCenter, "%s/%s" % (parts[1], parts[2]))

        # ── SIGNAL BUBBLES ──
        vis_start_date = visible[0]["date"]
        vis_end_date = visible[-1]["date"]
        for b in self._bubbles:
            bd = b["date"]
            if bd < vis_start_date or bd > vis_end_date:
                continue
            ci = self._date_index.get(bd)
            if ci is None:
                continue
            local_i = ci - offset
            if local_i < 0 or local_i >= n_vis:
                continue
            # Position: x from candle index, y at SPY close price
            x = candle_x(local_i)
            spy_close = visible[local_i]["close"]
            y = price_y(spy_close)

            if b["is_winner"]:
                # Green bubble, sqrt-scaled by move_adr
                adr = max(b["move_adr"], 0.5)
                t = min(adr / self._max_move_adr, 1.0)
                t = t ** 0.5  # sqrt scaling — spreads small/mid, compresses big
                radius = self.BUBBLE_MIN + t * (self.BUBBLE_MAX - self.BUBBLE_MIN)
                col = QColor(74, 222, 128, 60)
                p.setPen(QPen(QColor(74, 222, 128, 90), 0.5))
            else:
                # Red bubble, fixed small size
                radius = self.BUBBLE_MIN
                col = QColor(248, 113, 113, 50)
                p.setPen(QPen(QColor(248, 113, 113, 80), 0.5))

            p.setBrush(col)
            p.drawEllipse(QPointF(x, y), radius, radius)

        # Hover tooltip
        if self._hover_idx is not None and 0 <= self._hover_idx < n_vis:
            hc = visible[self._hover_idx]
            hx = candle_x(self._hover_idx)
            p.setPen(QPen(QColor(255, 255, 255, 30), 0.5, Qt.DashLine))
            p.drawLine(int(hx), self.MARGIN_TOP, int(hx), self.MARGIN_TOP + chart_h)
            # Check for bubbles on this date
            hd = hc["date"]
            hits = [b for b in self._bubbles if b["date"] == hd]
            p.setPen(QColor(C["text"]))
            f.setPixelSize(10)
            p.setFont(f)
            if hits:
                labels = []
                for hit in hits[:5]:
                    wl = "W" if hit["is_winner"] else "L"
                    madr = hit["move_adr"]
                    labels.append("%s %s %.1f" % (hit["ticker"], wl, madr))
                txt = "%s  SPY:%.2f  %s" % (hd, hc["close"], " | ".join(labels))
            else:
                txt = "%s  SPY:%.2f" % (hd, hc["close"])
            p.drawText(QRectF(8, 2, w - 16, 16), Qt.AlignLeft | Qt.AlignVCenter, txt)

        # Bubble count legend
        n_green = sum(1 for b in self._bubbles if b["is_winner"])
        n_red = sum(1 for b in self._bubbles if not b["is_winner"])
        p.setPen(QColor(C["text_dim"]))
        f.setPixelSize(10)
        p.setFont(f)
        p.drawText(QRectF(8, h - self.VOL_H - 16, 200, 14),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   "%d signals (%d W / %d R)" % (len(self._bubbles), n_green, n_red))

        p.end()

    def mousePressEvent(self, ev):
        ev.accept()  # consume — prevent canvas from collapsing the workspace
        if not self._candles:
            return
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        self._drag_start_x = pos.x()
        self._drag_start_offset = self._scroll_offset
        self._dragging = False

    def mouseReleaseEvent(self, ev):
        ev.accept()
        self._drag_start_x = None
        self._drag_start_offset = None
        self._dragging = False

    def mouseMoveEvent(self, ev):
        ev.accept()
        if not self._candles:
            return
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        mx = pos.x()

        # Drag scrolling
        if self._drag_start_x is not None:
            dx = mx - self._drag_start_x
            if abs(dx) > 4:
                self._dragging = True
            if self._dragging:
                visible, offset = self._visible_slice()
                n_vis = len(visible) if visible else 1
                w, h, chart_w, chart_h = self._chart_geometry()
                candle_w = chart_w / n_vis if n_vis else 1
                bar_shift = int(-dx / candle_w) if candle_w > 0 else 0
                new_off = self._drag_start_offset + bar_shift
                max_off = max(0, len(self._candles) - self._visible_count)
                self._scroll_offset = max(0, min(new_off, max_off))
                self.update()
                return

        # Hover (when not dragging)
        visible, offset = self._visible_slice()
        if not visible:
            return
        w, h, chart_w, chart_h = self._chart_geometry()
        candle_w = chart_w / len(visible) if visible else 1
        idx = int(mx / candle_w) if candle_w > 0 else None
        if idx is not None and 0 <= idx < len(visible):
            if self._hover_idx != idx:
                self._hover_idx = idx
                self.update()
        elif self._hover_idx is not None:
            self._hover_idx = None
            self.update()

    def leaveEvent(self, ev):
        if self._hover_idx is not None:
            self._hover_idx = None
            self.update()
        self._drag_start_x = None
        self._dragging = False

    def wheelEvent(self, ev):
        ev.accept()  # consume — prevent scroll area from scrolling
        if not self._candles:
            return
        delta = ev.angleDelta().y()
        old_count = self._visible_count
        if delta > 0:
            self._visible_count = max(60, self._visible_count - 40)
        else:
            self._visible_count = min(len(self._candles), self._visible_count + 40)
        # Keep view roughly centered
        if old_count != self._visible_count:
            visible, offset = self._visible_slice()
            center = offset + old_count // 2
            self._scroll_offset = max(0, center - self._visible_count // 2)
        self.update()

    def keyPressEvent(self, ev):
        """Arrow keys to scroll left/right."""
        if ev.key() == Qt.Key_Left:
            self._scroll_offset = max(0, self._scroll_offset - max(1, self._visible_count // 10))
            self.update()
        elif ev.key() == Qt.Key_Right:
            max_off = max(0, len(self._candles) - self._visible_count)
            self._scroll_offset = min(max_off, self._scroll_offset + max(1, self._visible_count // 10))
            self.update()


# ============================================================
# CANDLESTICK CHART WIDGET (QPainter)
# ============================================================

class CandlestickChart(QWidget):
    """QPainter candlestick chart with MAs, signal/exit/entry/earnings markers."""
    candle_clicked = Signal(str)  # emits date string

    UP_COLOR = "#4ade80"
    DOWN_COLOR = "#f87171"
    BG_COLOR = "#000000"
    GRID_COLOR = "#1A1A1A"
    EMA8_COLOR = "#5dade2"
    EMA21_COLOR = "#d4a853"
    SMA50_COLOR = "#f5c542"
    SMA200_COLOR = "#e74c3c"
    SIGNAL_COLOR = "#FFFFFF"
    ENTRY_COLOR = "#4ade80"
    EXIT_COLOR = "#E8A735"
    EARNINGS_COLOR = "#EF4444"

    MARGIN_TOP = 32
    MARGIN_RIGHT = 64
    VOL_H = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(200)

        self._candles = []       # list of dicts with date/OHLCV/MA
        self._ticker = ""
        self._signal_date = None
        self._entry_date = None
        self._exit_date = None
        self._profit_exit_date = None
        self._earnings_dates = set()
        self._hover_idx = None
        self._scroll_offset = 0
        self._visible_count = 200  # how many bars to show (zoom)

        # Floating "Yes ✓" button
        self._yes_btn = QPushButton("Yes ✓", self)
        self._yes_btn.setVisible(False)
        self._yes_btn.setStyleSheet(
            "QPushButton { background:#059669; color:#fff; border:1px solid #4ade80;"
            "font-size:11px; font-weight:700; padding:4px 12px; }"
            "QPushButton:hover { background:#4ade80; color:#000; }"
        )
        self._yes_btn.clicked.connect(lambda: self.candle_clicked.emit("__yes__"))

    def set_data(self, candles, ticker, signal_date, exit_date=None, profit_exit_date=None, earnings_dates=None):
        self._candles = candles or []
        self._ticker = ticker
        self._signal_date = signal_date
        self._exit_date = exit_date
        self._profit_exit_date = profit_exit_date
        self._earnings_dates = set(earnings_dates or [])
        self._entry_date = None
        self._hover_idx = None
        self._yes_btn.setVisible(False)
        # Auto-scroll to center on signal
        self._visible_count = min(max(len(self._candles), 100), 330)
        sig_idx = self._find_idx(signal_date)
        if sig_idx is not None:
            self._scroll_offset = max(0, sig_idx - self._visible_count // 2)
        else:
            self._scroll_offset = max(0, len(self._candles) - self._visible_count)
        self.update()

    def set_entry_date(self, d):
        self._entry_date = d
        self.update()

    def _find_idx(self, date_str):
        if not date_str:
            return None
        for i, c in enumerate(self._candles):
            if c["date"] == date_str:
                return i
        return None

    def _visible_slice(self):
        n = len(self._candles)
        max_off = max(0, n - self._visible_count)
        off = min(max(0, self._scroll_offset), max_off)
        return self._candles[off:off + self._visible_count], off

    def _chart_geometry(self):
        w = self.width()
        h = self.height()
        chart_h = h - self.MARGIN_TOP - self.VOL_H
        chart_w = w - self.MARGIN_RIGHT
        return w, h, chart_w, chart_h

    def paintEvent(self, ev):
        if not self._candles:
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(self.BG_COLOR))
            p.setPen(QColor(C["text_muted"]))
            f = QFont("DM Sans", 12)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, "Select a signal to view chart")
            p.end()
            return

        visible, offset = self._visible_slice()
        if not visible:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, chart_w, chart_h = self._chart_geometry()
        candle_w = chart_w / len(visible) if visible else 1
        body_w = max(1, candle_w * 0.6)

        # Background
        p.fillRect(self.rect(), QColor(self.BG_COLOR))

        # Price range
        prices = []
        for c in visible:
            prices.extend([c["high"], c["low"]])
        if not prices:
            p.end()
            return
        p_min = min(prices) * 0.995
        p_max = max(prices) * 1.005
        p_range = p_max - p_min
        if p_range < 0.001:
            p_range = 1.0

        def price_y(price):
            return self.MARGIN_TOP + chart_h * (1 - (price - p_min) / p_range)

        def candle_x(i):
            return i * candle_w + candle_w / 2

        # Grid lines + price labels
        p.setPen(QPen(QColor(self.GRID_COLOR), 0.5))
        f = QFont("JetBrains Mono", 1)
        f.setPixelSize(10)
        p.setFont(f)
        for i in range(7):
            y = self.MARGIN_TOP + (chart_h / 6) * i
            p.setPen(QPen(QColor(self.GRID_COLOR), 0.5))
            p.drawLine(0, int(y), chart_w, int(y))
            price = p_max - (p_range / 6) * i
            p.setPen(QColor(C["text_muted"]))
            p.drawText(QRectF(chart_w + 4, y - 6, self.MARGIN_RIGHT - 6, 12),
                       Qt.AlignLeft | Qt.AlignVCenter, "%.2f" % price)

        # Ticker watermark
        p.setPen(QColor(255, 255, 255, 12))
        f2 = QFont("DM Sans", 1)
        f2.setPixelSize(52)
        f2.setWeight(QFont.Bold)
        p.setFont(f2)
        p.drawText(QRectF(chart_w - 200, self.MARGIN_TOP, 188, 60),
                   Qt.AlignRight | Qt.AlignTop, self._ticker)

        # MAs
        def draw_ma(key, color):
            p.setPen(QPen(QColor(color), 1.2))
            path = QPainterPath()
            started = False
            for i, c in enumerate(visible):
                v = c.get(key)
                if v is None:
                    continue
                x = candle_x(i)
                y = price_y(v)
                if not started:
                    path.moveTo(x, y)
                    started = True
                else:
                    path.lineTo(x, y)
            if started:
                p.drawPath(path)

        draw_ma("ema8", self.EMA8_COLOR)
        draw_ma("ema21", self.EMA21_COLOR)
        draw_ma("sma50", self.SMA50_COLOR)
        draw_ma("sma200", self.SMA200_COLOR)

        # Earnings markers (behind candles)
        if self._earnings_dates:
            for i, c in enumerate(visible):
                prev_date = visible[i - 1]["date"] if i > 0 else (
                    self._candles[offset - 1]["date"] if offset > 0 else None)
                is_report = c["date"] in self._earnings_dates
                is_reaction = prev_date and prev_date in self._earnings_dates
                if is_report or is_reaction:
                    x = candle_x(i)
                    alpha = 30 if is_reaction else 15
                    p.fillRect(QRectF(x - candle_w / 2, self.MARGIN_TOP, candle_w, chart_h),
                               QColor(239, 68, 68, alpha))
                    p.setPen(QColor(self.EARNINGS_COLOR))
                    f.setPixelSize(9)
                    f.setWeight(QFont.Bold)
                    p.setFont(f)
                    p.drawText(QRectF(x - 6, self.MARGIN_TOP, 12, 12), Qt.AlignCenter, "E")

        # Signal / Entry / Exit / Profit Exit markers
        has_profit_exit = self._profit_exit_date is not None
        for i, c in enumerate(visible):
            x = candle_x(i)
            is_sig = c["date"] == self._signal_date
            is_entry = c["date"] == self._entry_date
            is_exit = c["date"] == self._exit_date
            is_profit = c["date"] == self._profit_exit_date

            if is_sig:
                p.fillRect(QRectF(x - candle_w / 2, self.MARGIN_TOP, candle_w, chart_h),
                           QColor(255, 255, 255, 15))
                pen = QPen(QColor(self.SIGNAL_COLOR), 1, Qt.DashLine)
                p.setPen(pen)
                p.drawLine(int(x), self.MARGIN_TOP, int(x), self.MARGIN_TOP + chart_h)

            if is_entry and not is_sig:
                p.fillRect(QRectF(x - candle_w / 2, self.MARGIN_TOP, candle_w, chart_h),
                           QColor(74, 222, 128, 20))
                pen = QPen(QColor(self.ENTRY_COLOR), 1, Qt.DashLine)
                p.setPen(pen)
                p.drawLine(int(x), self.MARGIN_TOP, int(x), self.MARGIN_TOP + chart_h)

            if is_exit:
                # Reduced opacity when profit exit also exists
                exit_col = QColor(self.EXIT_COLOR)
                if has_profit_exit:
                    exit_col.setAlpha(80)
                pen = QPen(exit_col, 1, Qt.DashLine)
                p.setPen(pen)
                p.drawLine(int(x), self.MARGIN_TOP, int(x), self.MARGIN_TOP + chart_h)

            if is_profit:
                pen = QPen(QColor("#A855F7"), 1, Qt.DashLine)
                p.setPen(pen)
                p.drawLine(int(x), self.MARGIN_TOP, int(x), self.MARGIN_TOP + chart_h)

        # Candles
        for i, c in enumerate(visible):
            x = candle_x(i)
            is_up = c["close"] >= c["open"]
            color = QColor(self.UP_COLOR if is_up else self.DOWN_COLOR)

            # Wick
            p.setPen(QPen(color, 1))
            p.drawLine(int(x), int(price_y(c["high"])), int(x), int(price_y(c["low"])))

            # Body
            b_top = price_y(max(c["open"], c["close"]))
            b_bot = price_y(min(c["open"], c["close"]))
            b_h = max(1, b_bot - b_top)
            if is_up:
                p.setPen(QPen(color, 1))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(x - body_w / 2, b_top, body_w, b_h))
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(color)
                p.drawRect(QRectF(x - body_w / 2, b_top, body_w, b_h))

        # Labels for signal/entry/exit
        f.setPixelSize(9)
        f.setWeight(QFont.Bold)
        p.setFont(f)
        for i, c in enumerate(visible):
            x = candle_x(i)
            if c["date"] == self._signal_date:
                lbl = "ENTRY" if c["date"] == self._entry_date else "SIG"
                col = self.ENTRY_COLOR if c["date"] == self._entry_date else self.SIGNAL_COLOR
                p.setPen(QColor(col))
                p.drawText(QRectF(x - 18, self.MARGIN_TOP + chart_h - 14, 36, 12),
                           Qt.AlignCenter, lbl)
            elif c["date"] == self._entry_date:
                p.setPen(QColor(self.ENTRY_COLOR))
                p.setBrush(QColor(self.ENTRY_COLOR))
                p.drawEllipse(QPointF(x, price_y(c["low"]) + 10), 3, 3)
                p.drawText(QRectF(x - 18, price_y(c["low"]) + 16, 36, 12),
                           Qt.AlignCenter, "ENTRY")
            if c["date"] == self._exit_date:
                exit_lbl_col = QColor(self.EXIT_COLOR)
                if has_profit_exit:
                    exit_lbl_col.setAlpha(80)
                p.setPen(exit_lbl_col)
                p.drawText(QRectF(x - 12, self.MARGIN_TOP + 2, 24, 12),
                           Qt.AlignCenter, "EXIT")
            if c["date"] == self._profit_exit_date:
                p.setPen(QColor("#A855F7"))
                p.drawText(QRectF(x - 18, self.MARGIN_TOP + 2, 36, 12),
                           Qt.AlignCenter, "PROFIT")

        # Volume bars
        vol_top = h - self.VOL_H
        p.setPen(QPen(QColor(self.GRID_COLOR), 0.5))
        p.drawLine(0, vol_top, chart_w, vol_top)
        vol_max = max((c["volume"] for c in visible), default=1) or 1
        for i, c in enumerate(visible):
            x = candle_x(i)
            is_up = c["close"] >= c["open"]
            bar_h = (c["volume"] / vol_max) * (self.VOL_H - 8)
            alpha = 90
            col = QColor(self.UP_COLOR) if is_up else QColor(self.DOWN_COLOR)
            col.setAlpha(alpha)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRect(QRectF(x - body_w / 2, h - bar_h, body_w, bar_h))
        # Vol average line
        pen = QPen(QColor(245, 158, 11, 128), 1)
        p.setPen(pen)
        vol_path = QPainterPath()
        started = False
        for i, c in enumerate(visible):
            va = c.get("vol_avg20")
            if va is None:
                continue
            x = candle_x(i)
            y = h - (va / vol_max) * (self.VOL_H - 8)
            if not started:
                vol_path.moveTo(x, y)
                started = True
            else:
                vol_path.lineTo(x, y)
        if started:
            p.drawPath(vol_path)

        # Date labels on volume
        p.setPen(QColor(C["text_muted"]))
        f.setPixelSize(9)
        f.setWeight(QFont.Normal)
        p.setFont(f)
        step = max(1, len(visible) // 8)
        for i, c in enumerate(visible):
            if i % step == 0:
                parts = c["date"].split("-")
                if len(parts) >= 3:
                    p.drawText(QRectF(candle_x(i) - 18, h - 12, 36, 12),
                               Qt.AlignCenter, "%s/%s" % (parts[1], parts[2]))

        # Hover crosshair + OHLCV readout
        if self._hover_idx is not None and 0 <= self._hover_idx < len(visible):
            hc = visible[self._hover_idx]
            hx = candle_x(self._hover_idx)
            p.setPen(QPen(QColor(255, 255, 255, 40), 0.5, Qt.DashLine))
            p.drawLine(int(hx), self.MARGIN_TOP, int(hx), self.MARGIN_TOP + chart_h)
            is_up = hc["close"] >= hc["open"]
            col = QColor(self.UP_COLOR if is_up else self.DOWN_COLOR)
            p.setPen(col)
            f.setPixelSize(11)
            p.setFont(f)
            txt = "%s  O:%.2f H:%.2f L:%.2f C:%.2f  V:%.1fM" % (
                hc["date"], hc["open"], hc["high"], hc["low"], hc["close"],
                hc["volume"] / 1e6)
            p.drawText(QRectF(8, 2, w - 16, 16), Qt.AlignLeft | Qt.AlignVCenter, txt)

        # MA legend at top
        legend_y = self.MARGIN_TOP + chart_h + 2
        legend_x = 8
        f.setPixelSize(9)
        f.setWeight(QFont.Normal)
        p.setFont(f)
        for label, color in [("EMA 8", self.EMA8_COLOR), ("EMA 21", self.EMA21_COLOR),
                             ("SMA 50", self.SMA50_COLOR), ("SMA 200", self.SMA200_COLOR)]:
            p.setPen(QPen(QColor(color), 2))
            p.drawLine(int(legend_x), int(vol_top + 8), int(legend_x + 12), int(vol_top + 8))
            p.setPen(QColor(color))
            p.drawText(QRectF(legend_x + 14, vol_top + 2, 50, 12),
                       Qt.AlignLeft | Qt.AlignVCenter, label)
            legend_x += 68

        p.end()

    def mouseMoveEvent(self, ev):
        if not self._candles:
            return
        visible, offset = self._visible_slice()
        if not visible:
            return
        w, h, chart_w, chart_h = self._chart_geometry()
        candle_w = chart_w / len(visible) if visible else 1
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        mx = pos.x()
        idx = int(mx / candle_w) if candle_w > 0 else None
        if idx is not None and 0 <= idx < len(visible):
            if self._hover_idx != idx:
                self._hover_idx = idx
                self.update()
        elif self._hover_idx is not None:
            self._hover_idx = None
            self.update()

    def leaveEvent(self, ev):
        if self._hover_idx is not None:
            self._hover_idx = None
            self.update()

    def mousePressEvent(self, ev):
        if not self._candles:
            return
        visible, offset = self._visible_slice()
        if not visible:
            return
        w, h, chart_w, chart_h = self._chart_geometry()
        candle_w = chart_w / len(visible) if visible else 1
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        mx, my = pos.x(), pos.y()
        idx = int(mx / candle_w) if candle_w > 0 else None
        if idx is not None and 0 <= idx < len(visible):
            clicked_date = visible[idx]["date"]
            self._entry_date = clicked_date
            # Position floating Yes button near click
            btn_x = min(int(mx + 8), self.width() - 70)
            btn_y = max(int(my - 30), 4)
            self._yes_btn.move(btn_x, btn_y)
            self._yes_btn.setVisible(True)
            self.candle_clicked.emit(clicked_date)
            self.update()

    def wheelEvent(self, ev):
        ev.accept()  # consume event so QScrollArea never sees it
        if not self._candles:
            return
        delta = ev.angleDelta().y()
        if delta > 0:
            self._visible_count = max(40, self._visible_count - 20)
        else:
            self._visible_count = min(len(self._candles), self._visible_count + 20)
        # Re-center on signal
        sig_idx = self._find_idx(self._signal_date)
        if sig_idx is not None:
            self._scroll_offset = max(0, sig_idx - self._visible_count // 2)
        self.update()



# ============================================================
# VETTING WORKSPACE — full chart vetting workflow
# ============================================================

class VettingWorkspace(QFrame):
    """Full vetting workspace: signal list, candlestick chart, verdict buttons."""

    def __init__(self, node_id="vetting", parent=None):
        super().__init__(parent)
        self.node_id = node_id
        self._setup = "dtss"
        self._mode = "causative"  # "causative" or "correlative"
        self._vet_sort = "combined"  # "adr", "entry", "combined"
        self._signals = []       # list of signal dicts
        self._filtered = []      # filtered view
        self._current_idx = 0
        self._candle_cache = {}  # "ticker_date" -> candle list
        self._has_entry_scores = False
        self.setStyleSheet(
            "VettingWorkspace { background:%s; border:1px solid %s; }" % (C["surface"], C["border"])
        )
        self.setFocusPolicy(Qt.StrongFocus)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Top bar: mode toggle + stats + hints ──
        top_bar = QFrame()
        top_bar.setFixedHeight(32)
        top_bar.setStyleSheet(
            "QFrame { background:#0A0A0A; border-bottom:1px solid %s; }" % C["border"]
        )
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(12, 0, 12, 0)
        tb_lay.setSpacing(8)

        # Mode toggle buttons
        self._mode_btns = {}
        for mode_key, mode_label in [("causative", "CAUSATIVE"), ("correlative", "CORRELATIVE")]:
            btn = QPushButton(mode_label)
            btn.setFixedHeight(22)
            btn.setCheckable(True)
            btn.setChecked(mode_key == "causative")
            btn.clicked.connect(lambda checked, m=mode_key: self._set_mode(m))
            tb_lay.addWidget(btn)
            self._mode_btns[mode_key] = btn
        self._update_mode_btn_styles()

        tb_lay.addSpacing(8)
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet(
            "font-family:'JetBrains Mono','Consolas',monospace; font-size:11px;"
            "color:%s; background:transparent; border:none;" % C["text_dim"]
        )
        tb_lay.addWidget(self._stats_label)
        tb_lay.addStretch()

        # Entry indicator
        self._entry_label = QLabel("")
        self._entry_label.setStyleSheet(
            "font-family:'JetBrains Mono','Consolas',monospace; font-size:11px;"
            "color:%s; background:transparent; border:none; font-weight:700;" % C["green"]
        )
        tb_lay.addWidget(self._entry_label)

        # Verdict buttons
        for key, label, fg, bg_hover in [
            ("yes", "YES (1)", C["green"], "#059669"),
            ("no", "NO (2)", C["red"], "#dc2626"),
            ("skip", "SKIP (3)", C["text_dim"], C["surface2"]),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(22)
            btn.setFixedWidth(70)
            btn.setStyleSheet(
                "QPushButton { color:%s; border:1px solid %s; background:transparent;"
                "font-family:'JetBrains Mono','Consolas',monospace; font-size:10px;"
                "font-weight:700; padding:2px 6px; }"
                "QPushButton:hover { background:%s; color:#000; }" % (fg, fg, bg_hover)
            )
            btn.clicked.connect(lambda checked, k=key: self._do_verdict(k))
            tb_lay.addWidget(btn)

        lay.addWidget(top_bar)

        # ── Body: sidebar + chart + bottom bar ──
        body = QWidget()
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        # LEFT: filter bar + signal list
        left = QFrame()
        left.setFixedWidth(260)
        left.setStyleSheet("QFrame { background:#050505; border-right:1px solid %s; }" % C["border"])
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)

        # Filter checkboxes
        fbar = QFrame()
        fbar.setFixedHeight(30)
        fbar.setStyleSheet("QFrame { background:#0A0A0A; border-bottom:1px solid %s; }" % C["border"])
        fb_lay = QHBoxLayout(fbar)
        fb_lay.setContentsMargins(8, 0, 8, 0)
        fb_lay.setSpacing(6)
        self._filter_checks = {}
        for key, label, color in [
            ("yes", "V", C["green"]),
            ("unvetted", "U", C["text_dim"]),
            ("no", "N", C["red"]),
        ]:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.setStyleSheet(
                "QCheckBox { color:%s; font-size:10px; font-weight:700;"
                "font-family:'JetBrains Mono','Consolas',monospace;"
                "background:transparent; border:none; spacing:3px; }"
                "QCheckBox::indicator { width:12px; height:12px; }" % color
            )
            cb.stateChanged.connect(self._on_filter_changed)
            fb_lay.addWidget(cb)
            self._filter_checks[key] = cb
        fb_lay.addStretch()
        # Sort buttons
        self._vet_sort_btns = {}
        for sort_key, sort_label in [("adr", "ADR"), ("entry", "ENTRY"), ("combined", "COMBINED")]:
            btn = QPushButton(sort_label)
            btn.setFixedHeight(18)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, k=sort_key: self._set_vet_sort(k))
            fb_lay.addWidget(btn)
            self._vet_sort_btns[sort_key] = btn
        self._style_vet_sort_btns()
        left_lay.addWidget(fbar)

        # Signal list
        self._sig_list = QListWidget()
        self._sig_list.setStyleSheet(
            "QListWidget { background:#050505; border:none; outline:none; }"
            "QListWidget::item { padding:6px 10px; border-bottom:1px solid #111; }"
            "QListWidget::item:selected { background:rgba(74,222,128,0.1); }"
            "QListWidget::item:hover { background:rgba(255,255,255,0.03); }"
        )
        self._sig_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._sig_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._sig_list.currentRowChanged.connect(self._on_signal_selected)
        left_lay.addWidget(self._sig_list, 1)
        body_lay.addWidget(left)

        # CENTER: chart
        center = QWidget()
        center_lay = QVBoxLayout(center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(0)

        self._chart = CandlestickChart()
        self._chart.candle_clicked.connect(self._on_candle_click)
        center_lay.addWidget(self._chart, 1)

        # Metadata label overlaid at bottom of chart area
        self._meta_label = QLabel("")
        self._meta_label.setStyleSheet(
            "font-family:'JetBrains Mono','Consolas',monospace; font-size:11px;"
            "color:%s; background:transparent; border:none;" % C["text_dim"]
        )
        center_lay.addWidget(self._meta_label)

        body_lay.addWidget(center, 1)
        lay.addWidget(body, 1)

    def set_setup(self, setup):
        self._setup = setup

    def _set_mode(self, mode):
        """Switch between causative and correlative vetting modes."""
        if mode == self._mode:
            return
        self._mode = mode
        self._current_idx = 0
        self._candle_cache = {}
        self._update_mode_btn_styles()
        self._load_signals()
        self.setFocus()

    def _update_mode_btn_styles(self):
        for key, btn in self._mode_btns.items():
            active = key == self._mode
            btn.setChecked(active)
            if active:
                btn.setStyleSheet(
                    "QPushButton { background:%s; color:#000; border:1px solid %s;"
                    "font-family:'JetBrains Mono','Consolas',monospace; font-size:10px;"
                    "font-weight:700; padding:2px 10px; }" % (C["green"], C["green"])
                )
            else:
                btn.setStyleSheet(
                    "QPushButton { background:transparent; color:%s; border:1px solid %s;"
                    "font-family:'JetBrains Mono','Consolas',monospace; font-size:10px;"
                    "font-weight:600; padding:2px 10px; }"
                    "QPushButton:hover { background:%s; color:%s; }" % (
                        C["text_muted"], C["border"], C["surface2"], C["text"])
                )

    def _set_vet_sort(self, sort_key):
        self._vet_sort = sort_key
        self._style_vet_sort_btns()
        self._sort_and_refresh()
        self.setFocus()

    def _style_vet_sort_btns(self):
        for key, btn in self._vet_sort_btns.items():
            active = key == self._vet_sort
            btn.setChecked(active)
            if active:
                btn.setStyleSheet(
                    "QPushButton { background:#222; color:#E0E0E0; border:none;"
                    "font-family:'JetBrains Mono','Consolas',monospace; font-size:10px;"
                    "font-weight:700; padding:2px 8px; }")
            else:
                btn.setStyleSheet(
                    "QPushButton { background:transparent; color:#555; border:none;"
                    "font-family:'JetBrains Mono','Consolas',monospace; font-size:10px;"
                    "font-weight:500; padding:2px 8px; }"
                    "QPushButton:hover { color:#888; }")

    def _sort_and_refresh(self):
        """Re-sort signals based on current sort key and rebuild list."""
        if self._vet_sort == "adr":
            self._signals.sort(key=lambda x: x.get("move_adr") or 0, reverse=True)
        elif self._vet_sort == "entry":
            self._signals.sort(key=lambda x: x.get("entry_candle_pct") or 0, reverse=True)
        elif self._vet_sort == "combined":
            self._signals.sort(key=lambda x: x.get("combined_score") or 0, reverse=True)
        self._current_idx = 0
        self._apply_filter()
        self._update_stats()

    def showEvent(self, ev):
        super().showEvent(ev)
        # Only reload if we haven't loaded yet or setup changed
        if not self._signals or getattr(self, "_loaded_setup", None) != self._setup:
            self._load_signals()

    def _load_signals(self):
        """Load signals based on current mode."""
        self._loaded_setup = self._setup
        if self._mode == "correlative":
            self._load_correlative_signals()
        else:
            self._load_causative_signals()

    def _load_vetting_decisions(self, setup):
        """Load vetting decisions, rejected signals, and example set. Shared by both modes."""
        decisions = load_json(REPO_ROOT / "data" / "vetting" / ("vetting_%s.json" % setup), {})
        rejected_set = set()
        try:
            with get_db() as db:
                rows = db.execute(
                    "SELECT ticker, signal_date FROM rejected_signals WHERE setup_type=?",
                    (setup,)
                ).fetchall()
            rejected_set = set("%s_%s" % (r["ticker"], r["signal_date"]) for r in rows)
        except Exception:
            pass
        example_set = set()
        try:
            with get_db() as db:
                rows = db.execute(
                    "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
                ).fetchall()
            example_set = set("%s_%s" % (r["ticker"], r["entry_date"]) for r in rows)
        except Exception:
            pass
        return decisions, rejected_set, example_set

    def _apply_verdict(self, sig, decisions, rejected_set):
        """Apply verdict from decisions/rejected onto a signal dict."""
        key = "%s_%s" % (sig["ticker"], sig["signal_date"])
        vd = decisions.get(key, {})
        verdict = vd.get("verdict")
        if not verdict and key in rejected_set:
            verdict = "no"
        sig["verdict"] = verdict
        sig["entry_date"] = vd.get("entry_date")

    def _make_signal_dict(self, ticker, signal_date, **extra):
        """Create a signal dict with all standard fields."""
        sig = {
            "ticker": ticker, "signal_date": signal_date,
            "move_adr": None, "adr_at_signal": None, "classification": "",
            "verdict": None, "entry_date": None,
            "exit_date": None, "exit_bar": None, "profit_exit_date": None,
            "mfe_adr": None, "capture_eff": None, "move_pct": None,
            "combined_score": None, "entry_candle_score": None,
            "entry_candle_date": None, "entry_candle_pct": None, "move_adr_pct": None,
            "quality_score": None, "predicted_wr": None, "predicted_mfe": None, "ev": None,
        }
        sig.update(extra)
        return sig

    def _load_causative_signals(self):
        """Load signals from filtered file — same source the old web UI used.
        filtered_{setup}.json has exit_date, exit_bar, move_adr, capture_eff on every signal.
        """
        setup = self._setup
        decisions, rejected_set, example_set = self._load_vetting_decisions(setup)
        signals = []

        # Primary source: filtered_{setup}.json (produced by signal_filter.py)
        filtered_path = REPO_ROOT / "data" / "signal_filter" / ("filtered_%s.json" % setup)
        if not filtered_path.exists():
            self._signals = []
            self._apply_filter()
            self._update_stats()
            return
        try:
            filt_data = json.loads(filtered_path.read_text())
        except Exception:
            self._signals = []
            self._apply_filter()
            self._update_stats()
            return

        for s in filt_data.get("signals", []):
            tk = s.get("ticker", "")
            sd = s.get("date", "")  # filtered file uses "date", not "signal_date"
            if any("%s_%s" % (tk, sd) == ex for ex in example_set):
                continue
            sig = self._make_signal_dict(tk, sd,
                move_adr=s.get("move_adr"), adr_at_signal=s.get("adr_at_signal"),
                exit_date=s.get("exit_date"), exit_bar=s.get("exit_bar"),
                mfe_adr=s.get("mfe_adr"), capture_eff=s.get("capture_eff"),
                move_pct=s.get("move_pct"))
            self._apply_verdict(sig, decisions, rejected_set)
            signals.append(sig)

        if not signals:
            self._signals = []
            self._apply_filter()
            self._update_stats()
            return

        # Join profit grind exit dates if available
        profit_path = REPO_ROOT / "data" / "profit_grind" / ("profit_%s.json" % setup)
        if profit_path.exists():
            try:
                profit_data = json.loads(profit_path.read_text())
                profit_exits = profit_data.get("exit_dates", {})
                for sig in signals:
                    pk1 = "%s|%s" % (sig["ticker"], sig["signal_date"])
                    pk2 = "%s|%s" % (sig["ticker"], sig.get("entry_date", ""))
                    ped = profit_exits.get(pk1) or profit_exits.get(pk2)
                    if ped:
                        sig["profit_exit_date"] = ped
            except Exception:
                pass

        # Join entry candle scores if available
        entry_scores_path = REPO_ROOT / "local_runner" / "cache" / ("entry_scores_%s.json" % setup)
        has_entry_scores = False
        if entry_scores_path.exists():
            try:
                es_data = json.loads(entry_scores_path.read_text())
                score_lookup = {
                    "%s_%s" % (sc.get("ticker", ""), sc.get("signal_date", "")): sc
                    for sc in es_data.get("scored_signals", [])
                }
                for sig in signals:
                    sc = score_lookup.get("%s_%s" % (sig["ticker"], sig["signal_date"]))
                    if sc:
                        sig["combined_score"] = sc.get("combined_score")
                        sig["entry_candle_score"] = sc.get("entry_candle_score")
                        sig["entry_candle_date"] = sc.get("entry_candle_date")
                        sig["entry_candle_pct"] = sc.get("entry_candle_pct")
                        sig["move_adr_pct"] = sc.get("move_adr_pct")
                        has_entry_scores = True
            except Exception:
                pass

        self._has_entry_scores = has_entry_scores
        self._signals = signals
        self._style_vet_sort_btns()
        self._sort_and_refresh()

    def _load_correlative_signals(self):
        """Load EV-scored signals from latest ev_{setup}_*.json."""
        setup = self._setup
        cache_dir = REPO_ROOT / "local_runner" / "cache"
        ev_files = []
        if cache_dir.exists():
            ev_files = sorted(
                [f for f in cache_dir.iterdir()
                 if f.name.startswith("ev_%s_" % setup) and f.suffix == ".json"],
                key=lambda f: f.stat().st_mtime, reverse=True
            )
        if not ev_files:
            self._signals = []
            self._apply_filter()
            self._update_stats()
            return
        try:
            ev_data = json.loads(ev_files[0].read_text())
        except Exception:
            self._signals = []
            self._apply_filter()
            self._update_stats()
            return

        # Use signals_post (post-refinement) if available, else signals
        raw_signals = ev_data.get("signals_post", ev_data.get("signals", []))
        decisions, rejected_set, example_set = self._load_vetting_decisions(setup)

        signals = []
        for s in raw_signals:
            tk = s.get("ticker", "")
            sd = s.get("date", s.get("signal_date", ""))
            if any("%s_%s" % (tk, sd) == ex for ex in example_set):
                continue
            if s.get("is_example"):
                continue
            sig = self._make_signal_dict(tk, sd,
                move_adr=s.get("move_adr"), adr_at_signal=s.get("adr_at_signal"),
                classification=s.get("classification", ""),
                quality_score=s.get("quality_score"),
                predicted_wr=s.get("predicted_wr"),
                predicted_mfe=s.get("predicted_mfe"),
                ev=s.get("ev"))
            self._apply_verdict(sig, decisions, rejected_set)
            signals.append(sig)

        # Join entry candle scores if available (same as causative)
        entry_scores_path = REPO_ROOT / "local_runner" / "cache" / ("entry_scores_%s.json" % setup)
        has_entry_scores = False
        if entry_scores_path.exists():
            try:
                es_data = json.loads(entry_scores_path.read_text())
                score_lookup = {
                    "%s_%s" % (sc.get("ticker", ""), sc.get("signal_date", "")): sc
                    for sc in es_data.get("scored_signals", [])
                }
                for sig in signals:
                    sc = score_lookup.get("%s_%s" % (sig["ticker"], sig["signal_date"]))
                    if sc:
                        sig["combined_score"] = sc.get("combined_score")
                        sig["entry_candle_score"] = sc.get("entry_candle_score")
                        sig["entry_candle_date"] = sc.get("entry_candle_date")
                        sig["entry_candle_pct"] = sc.get("entry_candle_pct")
                        sig["move_adr_pct"] = sc.get("move_adr_pct")
                        has_entry_scores = True
            except Exception:
                pass

        # Sort: same as causative — combined_score if available, else move_adr
        self._has_entry_scores = has_entry_scores
        self._signals = signals
        self._style_vet_sort_btns()
        self._sort_and_refresh()

    def _apply_filter(self):
        """Filter signals based on checkbox state."""
        show_yes = self._filter_checks["yes"].isChecked()
        show_unvetted = self._filter_checks["unvetted"].isChecked()
        show_no = self._filter_checks["no"].isChecked()

        filtered = []
        for s in self._signals:
            v = s.get("verdict")
            if v == "yes" and show_yes:
                filtered.append(s)
            elif v == "no" and show_no:
                filtered.append(s)
            elif v is None and show_unvetted:
                filtered.append(s)
        self._filtered = filtered

        # Rebuild list widget — plain text items for speed
        self._sig_list.blockSignals(True)
        self._sig_list.clear()
        for sig in filtered:
            text = self._format_signal_text(sig)
            item = QListWidgetItem(text)
            item.setFont(QFont("JetBrains Mono", 9))
            # Color based on verdict
            v = sig.get("verdict")
            if v == "yes":
                item.setForeground(QColor(C["green"]))
            elif v == "no":
                item.setForeground(QColor(C["red"]))
            else:
                item.setForeground(QColor(C["text"]))
            self._sig_list.addItem(item)
        self._sig_list.blockSignals(False)

        # Preserve or reset selection
        if self._filtered:
            idx = min(self._current_idx, len(self._filtered) - 1)
            self._current_idx = idx
            self._sig_list.setCurrentRow(idx)
        self._update_stats()

    def _format_signal_text(self, sig):
        """Format signal as a compact text line for the list."""
        tk = sig["ticker"]
        adr = sig.get("move_adr") or 0
        parts = ["%-6s" % tk, "+%.1f" % adr]

        if self._mode == "correlative" and sig.get("ev") is not None:
            parts.append("EV%.1f" % sig["ev"])
            wr = sig.get("predicted_wr")
            if wr is not None:
                parts.append("%.0f%%" % (wr * 100))
        else:
            cs = sig.get("combined_score")
            if cs is not None:
                parts.append("%.0f%%" % (cs * 100))

        eb = sig.get("exit_bar")
        if eb:
            parts.append("%dd" % eb)

        v = sig.get("verdict")
        if v:
            parts.append(v.upper())

        return "  ".join(parts)

    def _update_stats(self):
        n_yes = sum(1 for s in self._signals if s.get("verdict") == "yes")
        n_no = sum(1 for s in self._signals if s.get("verdict") == "no")
        n_unvetted = sum(1 for s in self._signals if s.get("verdict") is None)
        total = len(self._signals)

        # Update filter checkbox labels
        self._filter_checks["yes"].setText("V %d" % n_yes)
        self._filter_checks["unvetted"].setText("U %d" % n_unvetted)
        self._filter_checks["no"].setText("N %d" % n_no)

        sort_names = {"adr": "by ADR", "entry": "by entry candle", "combined": "by combined"}
        sort_mode = sort_names.get(getattr(self, "_vet_sort", "combined"), "by combined")
        self._stats_label.setText(
            "%d signals  ·  %d yes  ·  %d no  ·  %d unvetted  ·  sorted %s" % (
                total, n_yes, n_no, n_unvetted, sort_mode)
        )

    def _on_filter_changed(self):
        self._apply_filter()

    def _on_signal_selected(self, row):
        if row < 0 or row >= len(self._filtered):
            return
        try:
            self._current_idx = row
            sig = self._filtered[row]
            self._load_chart(sig)
            self._update_meta(sig)
        except Exception:
            pass

    def _load_chart(self, sig):
        """Load candles for signal (from cache or compute)."""
        key = "%s_%s" % (sig["ticker"], sig["signal_date"])
        candles = self._candle_cache.get(key)
        if not candles:
            candles = _prepare_candles(sig["ticker"], sig["signal_date"])
            if candles:
                self._candle_cache[key] = candles
        # Load earnings dates
        earnings = []
        try:
            with get_db() as db:
                rows = db.execute(
                    "SELECT earnings_date FROM earnings_dates WHERE ticker=? ORDER BY earnings_date",
                    (sig["ticker"],)
                ).fetchall()
            earnings = [r[0] for r in rows]
        except Exception:
            pass
        self._chart.set_data(candles, sig["ticker"], sig["signal_date"],
                             exit_date=sig.get("exit_date"),
                             profit_exit_date=sig.get("profit_exit_date"),
                             earnings_dates=earnings)
        # Restore entry if previously set
        if sig.get("entry_date"):
            self._chart.set_entry_date(sig["entry_date"])
            self._entry_label.setText("Entry: %s" % sig["entry_date"])
        else:
            self._entry_label.setText("")

    def _update_meta(self, sig):
        parts = ["Ticker: %s" % sig["ticker"], "Signal: %s" % sig["signal_date"]]
        if self._mode == "correlative":
            if sig.get("ev") is not None:
                parts.append("EV: %.2f" % sig["ev"])
            if sig.get("predicted_wr") is not None:
                parts.append("WR: %.0f%%" % (sig["predicted_wr"] * 100))
            if sig.get("predicted_mfe") is not None:
                parts.append("MFE: %.1f ADR" % sig["predicted_mfe"])
            if sig.get("quality_score") is not None:
                parts.append("QS: %.0f" % sig["quality_score"])
            if sig.get("move_adr"):
                parts.append("Move: +%.1f ADR" % sig["move_adr"])
        else:
            if sig.get("move_adr"):
                parts.append("Move: +%.1f ADR" % sig["move_adr"])
            if sig.get("mfe_adr"):
                parts.append("MFE: %.1f ADR" % sig["mfe_adr"])
            if sig.get("capture_eff") is not None:
                parts.append("Eff: %.0f%%" % (sig["capture_eff"] * 100))
            if sig.get("exit_date"):
                exit_info = "Exit: %s" % sig["exit_date"]
                if sig.get("exit_bar"):
                    exit_info += " (%dd)" % sig["exit_bar"]
                parts.append(exit_info)
            if sig.get("adr_at_signal"):
                parts.append("ADR: %.2f" % sig["adr_at_signal"])
            cs = sig.get("combined_score")
            if cs is not None:
                parts.append("Score: %.0f%%" % (cs * 100))
                ecd = sig.get("entry_candle_date")
                if ecd:
                    parts.append("Best entry: %s" % ecd)
        parts.append("Entry = click chart")
        self._meta_label.setText("  ·  ".join(parts))

    def _on_candle_click(self, date_str):
        if date_str == "__yes__":
            self._do_verdict("yes")
            return
        # Set entry date
        if self._filtered and 0 <= self._current_idx < len(self._filtered):
            self._filtered[self._current_idx]["entry_date"] = date_str
            self._entry_label.setText("Entry: %s" % date_str)

    def _do_verdict(self, verdict):
        if not self._filtered or self._current_idx >= len(self._filtered):
            return
        try:
            self._do_verdict_inner(verdict)
        except Exception:
            pass

    def _do_verdict_inner(self, verdict):
        sig = self._filtered[self._current_idx]

        if verdict == "skip":
            self._advance_to_next()
            self.setFocus()
            return

        if verdict == "yes":
            entry = sig.get("entry_date") or self._chart._entry_date
            if not entry:
                return  # need entry date
            sig["entry_date"] = entry
        sig["verdict"] = verdict

        # Save to vetting JSON
        vetting_dir = REPO_ROOT / "data" / "vetting"
        vetting_dir.mkdir(parents=True, exist_ok=True)
        vetting_path = vetting_dir / ("vetting_%s.json" % self._setup)
        decisions = load_json(vetting_path, {})
        key = "%s_%s" % (sig["ticker"], sig["signal_date"])
        decisions[key] = {
            "ticker": sig["ticker"],
            "signal_date": sig["signal_date"],
            "verdict": verdict,
            "entry_date": sig.get("entry_date"),
            "timestamp": datetime.now().isoformat(),
        }
        save_json(vetting_path, decisions)

        # Save to DB
        if verdict == "yes":
            try:
                with get_db() as db:
                    if not db.execute(
                        "SELECT id FROM pending_examples WHERE setup_type=? AND ticker=? AND entry_date=?",
                        (self._setup, sig["ticker"], sig["entry_date"])
                    ).fetchone():
                        db.execute(
                            "INSERT INTO pending_examples (setup_type, ticker, signal_date, entry_date) "
                            "VALUES (?,?,?,?)",
                            (self._setup, sig["ticker"], sig["signal_date"], sig["entry_date"])
                        )
            except Exception:
                pass
        elif verdict == "no":
            try:
                with get_db() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO rejected_signals (setup_type, ticker, signal_date) "
                        "VALUES (?,?,?)",
                        (self._setup, sig["ticker"], sig["signal_date"])
                    )
            except Exception:
                pass

        # Update master signals list too
        for s in self._signals:
            if s["ticker"] == sig["ticker"] and s["signal_date"] == sig["signal_date"]:
                s["verdict"] = verdict
                s["entry_date"] = sig.get("entry_date")
                break

        self._chart._yes_btn.setVisible(False)
        self._update_stats()
        self._apply_filter()
        self._advance_to_next()
        self.setFocus()  # re-grab focus so arrow keys work

    def _advance_to_next(self):
        """Advance to next unvetted signal, or just next signal."""
        # Find next unvetted in filtered list
        for i in range(self._current_idx + 1, len(self._filtered)):
            if self._filtered[i].get("verdict") is None:
                self._current_idx = i
                self._sig_list.setCurrentRow(i)
                return
        # Otherwise just go to next
        if self._current_idx < len(self._filtered) - 1:
            self._current_idx += 1
            self._sig_list.setCurrentRow(self._current_idx)

    def keyPressEvent(self, ev):
        try:
            key = ev.key()
            if key == Qt.Key_1:
                self._do_verdict("yes")
            elif key == Qt.Key_2:
                self._do_verdict("no")
            elif key == Qt.Key_3:
                self._do_verdict("skip")
            elif key == Qt.Key_Up:
                if self._current_idx > 0:
                    self._current_idx -= 1
                    self._sig_list.setCurrentRow(self._current_idx)
            elif key == Qt.Key_Down:
                if self._current_idx < len(self._filtered) - 1:
                    self._current_idx += 1
                    self._sig_list.setCurrentRow(self._current_idx)
            else:
                super().keyPressEvent(ev)
        except Exception:
            pass


# ============================================================
# PIPELINE TAB
# ============================================================

class PipelineTab(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup = "dtss"
        self._details = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._canvas = FlowchartCanvas()
        self._canvas.node_clicked.connect(self._on_node_click)

        # Create detail widgets for ALL expandable nodes
        for nd in FLOW_NODES:
            if nd["kind"] == "run":
                det = GrinderDetail(nd["id"])
                det.run_requested.connect(self._fwd_run)
                det.stop_requested.connect(self._fwd_stop)
                self._canvas.add_detail_widget(nd["id"], det)
                self._details[nd["id"]] = det
            elif nd["kind"] == "do":
                if nd["id"] == "vetting":
                    det = VettingWorkspace(nd["id"])
                elif nd["id"] == "examples":
                    det = ExamplesWorkspace(nd["id"])
                elif nd["id"] == "scan_tuning":
                    det = ScanTuningWorkspace(nd["id"])
                else:
                    det = WorkspaceDetail(nd["id"])
                self._canvas.add_detail_widget(nd["id"], det)
                self._details[nd["id"]] = det

        scroll.setWidget(self._canvas)
        outer.addWidget(scroll, 1)

    def _on_node_click(self, nid):
        nd = next((n for n in FLOW_NODES if n["id"] == nid), None)
        if not nd:
            return
        if nd["kind"] in ("do", "run"):
            self._canvas.expand_node(nid)

    def _fwd_run(self, step_id):
        win = self.window()
        if hasattr(win, "run_pipeline_step"):
            win.run_pipeline_step(step_id)

    def _fwd_stop(self, step_id):
        win = self.window()
        if hasattr(win, "stop_pipeline_step"):
            win.stop_pipeline_step(step_id)

    def get_node(self, step_id):
        for gid, subs in GRINDER_SUB_STEPS.items():
            if step_id in subs:
                return self._details.get(gid)
        return self._details.get(step_id)

    def set_setup(self, setup):
        self._setup = setup
        for d in self._details.values():
            if hasattr(d, 'set_setup'):
                d.set_setup(setup)

    def refresh(self):
        state = load_json(PIPELINE_FILE, {"steps": {}})
        steps = state.get("steps", {})

        # Get example count for unlock gates
        n_examples = 0
        try:
            win = self.window()
            setup = win._setup_type if hasattr(win, "_setup_type") else "dtss"
            with get_db() as db:
                n_examples = db.execute(
                    "SELECT COUNT(*) FROM examples WHERE setup_type=?", (setup,)
                ).fetchone()[0]
        except Exception:
            pass

        self._canvas.set_n_examples(n_examples)

        for nd in FLOW_NODES:
            nid = nd["id"]
            locked = not _is_unlocked(nid, n_examples, steps)
            self._canvas.set_locked(nid, locked)

            if nd["kind"] == "run":
                subs = GRINDER_SUB_STEPS.get(nid, [nid])
                statuses = [steps.get(s, {}).get("status", "idle") for s in subs]
                if "running" in statuses or "queued" in statuses:
                    overall = "running"
                elif "error" in statuses:
                    overall = "error"
                elif all(s in ("done", "complete") for s in statuses) and len(subs) > 0:
                    overall = "done"
                else:
                    overall = "idle"
                self._canvas.set_status(nid, overall)
                if nid in self._details:
                    self._details[nid].update_from_state(steps)

            elif nid == "examples":
                # Progress: examples / winner clusters from refinement_dtss_cl*.json
                n_winners = 0
                cache_dir = REPO_ROOT / "local_runner" / "cache"
                if cache_dir.exists():
                    ref_files = sorted(
                        [f for f in cache_dir.iterdir()
                         if f.name.startswith("refinement_%s_cl" % setup) and f.suffix == ".json"],
                        key=lambda f: f.stat().st_mtime,
                        reverse=True
                    )
                    if ref_files:
                        try:
                            rdata = json.loads(ref_files[0].read_text())
                            n_winners = len(rdata.get("winner_signals", []))
                        except Exception:
                            pass

                if n_winners > 0:
                    progress = min(1.0, n_examples / n_winners)
                    self._canvas.set_example_progress(progress, n_examples, n_winners)
                else:
                    self._canvas.set_example_progress(0.0, n_examples, 0)
                self._canvas.set_info(nid, "")
                self._canvas.set_status(nid, "idle")

            elif nid == "vetting":
                try:
                    setup = self._setup
                    vp = REPO_ROOT / "data" / "vetting" / ("vetting_%s.json" % setup)
                    dec = load_json(vp, {})
                    ny = sum(1 for v in dec.values() if v.get("verdict") == "yes")
                    self._canvas.set_info(nid, "%d yes / %d vetted" % (ny, len(dec)))
                    self._canvas.set_status(nid, "done" if ny > 0 else "idle")
                except Exception:
                    pass

            elif nid == "summary":
                self._canvas.set_status(nid, "idle")
                self._canvas.set_info(nid, "setup readiness overview")

            else:
                self._canvas.set_status(nid, "idle")
                if locked:
                    self._canvas.set_info(nid, nd["desc"])
                else:
                    self._canvas.set_info(nid, "not yet built")


# ============================================================
# MAIN WINDOW
# ============================================================

class ScanPerfectWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ScanPerfect")
        self.setMinimumSize(1000, 700)
        self.resize(1400, 900)
        self._setup_type = "dtss"
        self._process = None
        self._process_step = None
        self._process_lines = []
        self._process_start = None

        central = QWidget()
        self.setCentralWidget(central)
        ml = QVBoxLayout(central)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)

        # Top bar — logo + setup selector only (no tabs)
        top = QFrame()
        top.setFixedHeight(48)
        top.setStyleSheet("QFrame { background:%s; border-bottom:1px solid %s; }" % (C["surface"], C["border"]))
        tl = QHBoxLayout(top)
        tl.setContentsMargins(20, 0, 20, 0)
        tl.setSpacing(0)

        logo = QLabel("SCANPERFECT")
        logo.setStyleSheet("font-size:12px; font-weight:700; letter-spacing:3px; color:%s; border:none; background:transparent;" % C["white"])
        tl.addWidget(logo)

        tl.addStretch()

        self._setup_combo = QComboBox()
        self._setup_combo.currentIndexChanged.connect(self._on_setup_changed)
        tl.addWidget(self._setup_combo)

        ml.addWidget(top)

        # Pipeline flowchart is the entire UI
        self._pipeline = PipelineTab()
        ml.addWidget(self._pipeline)

        # Init
        self._load_setups()
        self._pipeline.set_setup(self._setup_type)
        self._pipeline.refresh()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(5000)

    def _load_setups(self):
        self._setup_combo.clear()
        try:
            with get_db() as db:
                rows = db.execute("SELECT setup_type, name FROM setups ORDER BY created_at").fetchall()
            for r in rows:
                self._setup_combo.addItem("%s (%s)" % (r["name"], r["setup_type"]), r["setup_type"])
        except Exception:
            self._setup_combo.addItem("DTSS (dtss)", "dtss")

    def _on_setup_changed(self):
        data = self._setup_combo.currentData()
        if data:
            self._setup_type = data
            self._pipeline.set_setup(data)
            self._pipeline.refresh()

    def _on_tick(self):
        self._pipeline.refresh()

    # ── Subprocess management (centralized) ──

    def run_pipeline_step(self, step_id):
        if self._process is not None:
            return
        cmd_tpl = STEP_COMMANDS.get(step_id)
        if not cmd_tpl:
            return
        cmd = [a.replace("{setup}", self._setup_type) for a in cmd_tpl]

        self._process_step = step_id
        self._process_lines = []
        self._process_start = time.time()

        # Update state
        state = load_json(PIPELINE_FILE, {"steps": {}})
        state.setdefault("steps", {})[step_id] = {
            "status": "running", "started_at": datetime.now().isoformat(),
            "finished_at": None, "duration_s": None, "exit_code": None,
            "error": None, "result_summary": None,
        }
        save_json(PIPELINE_FILE, state)

        # Clear logs
        logs = load_json(PIPELINE_LOGS_FILE, {})
        logs[step_id] = []
        save_json(PIPELINE_LOGS_FILE, logs)

        det = self._pipeline.get_node(step_id)
        if det:
            det.clear_log()
            det.append_log("Starting %s for %s..." % (step_id, self._setup_type.upper()))
            det.append_log("Command: %s\n" % " ".join(cmd))

        self._pipeline.refresh()

        self._process = QProcess(self)
        self._process.setWorkingDirectory(str(REPO_ROOT))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self._process.setProcessEnvironment(env)
        self._process.setProcessChannelMode(QProcess.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._on_stdout)
        self._process.finished.connect(self._on_finished)
        self._process.start(cmd[0], cmd[1:])

    def stop_pipeline_step(self, step_id):
        if self._process and self._process.state() != QProcess.NotRunning:
            self._process.terminate()
            QTimer.singleShot(3000, self._force_kill)

    def _force_kill(self):
        if self._process and self._process.state() != QProcess.NotRunning:
            self._process.kill()

    def _on_stdout(self):
        if not self._process:
            return
        raw = self._process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        det = self._pipeline.get_node(self._process_step)
        for line in raw.splitlines():
            self._process_lines.append(line)
            if det:
                det.append_log(line)
        if len(self._process_lines) % 20 == 0:
            logs = load_json(PIPELINE_LOGS_FILE, {})
            logs[self._process_step] = self._process_lines[-4000:]
            save_json(PIPELINE_LOGS_FILE, logs)

    def _on_finished(self, exit_code, _):
        sid = self._process_step
        dur = round(time.time() - self._process_start, 1) if self._process_start else 0

        # Save logs
        logs = load_json(PIPELINE_LOGS_FILE, {})
        logs[sid] = self._process_lines[-4000:]
        save_json(PIPELINE_LOGS_FILE, logs)

        status = "done" if exit_code == 0 else "error"
        state = load_json(PIPELINE_FILE, {"steps": {}})
        prev = state.get("steps", {}).get(sid, {})
        state.setdefault("steps", {})[sid] = {
            "status": status, "started_at": prev.get("started_at"),
            "finished_at": datetime.now().isoformat(), "duration_s": dur,
            "exit_code": exit_code,
            "error": "\n".join(self._process_lines[-20:]) if exit_code != 0 else None,
            "result_summary": None,
        }
        save_json(PIPELINE_FILE, state)

        det = self._pipeline.get_node(sid)
        if det:
            if exit_code == 0:
                det.append_log("\n✓ Complete (%s)" % fmt_dur(dur))
            else:
                det.append_log("\n✗ Error (exit code %d, %s)" % (exit_code, fmt_dur(dur)))

        self._process = None
        self._process_step = None
        self._process_lines = []
        self._process_start = None
        self._pipeline.refresh()

        # Auto-chain: if this step succeeded and is part of a sub-step sequence,
        # automatically start the next sub-step
        if exit_code == 0:
            for gid, subs in GRINDER_SUB_STEPS.items():
                if sid in subs:
                    idx = subs.index(sid)
                    if idx + 1 < len(subs):
                        next_step = subs[idx + 1]
                        if det:
                            det.append_log("\n→ Auto-starting %s..." % next_step)
                        QTimer.singleShot(500, lambda s=next_step: self.run_pipeline_step(s))
                    break


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    init_db()
    _load_ohlcv_cache()
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)

    # Try loading DM Sans if installed
    for fp in ["C:/Windows/Fonts/DMSans-Regular.ttf", "C:/Windows/Fonts/DMSans-Bold.ttf"]:
        if os.path.exists(fp):
            QFontDatabase.addApplicationFont(fp)

    app.setFont(QFont("DM Sans", 10))

    win = ScanPerfectWindow()
    win.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
