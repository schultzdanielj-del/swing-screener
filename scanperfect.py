"""
ScanPerfect — Native Desktop App (PySide6)
Phase 6 of localization. Replaces browser-based HTML UI entirely.
Reads directly from SQLite + 5yr OHLCV pickle. No server process needed.
"""

import json
import os
import sys
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QStackedWidget, QScrollArea,
    QPlainTextEdit, QFrame,
)
from PySide6.QtCore import Qt, QProcess, QTimer, Signal, QProcessEnvironment
from PySide6.QtGui import QFont, QFontDatabase, QColor, QPainter, QPen, QLinearGradient


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
        for st, name, desc, direction in [
            ("dtss", "DTSS", "Double Top Short Sell", "short"),
            ("3-4db", "3-4DB", "3-4 Day Bounce (Short)", "short"),
            ("htf", "HTF", "High Tight Flag (Long)", "long"),
        ]:
            db.execute(
                "INSERT OR IGNORE INTO setups (setup_type, name, description, direction) VALUES (?,?,?,?)",
                (st, name, desc, direction),
            )


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
    "causative":   ["signal_grind", "exit_grind", "refinement_grind"],
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
    """Check if causative grind is complete — state file OR output files exist."""
    # Check pipeline state
    subs = GRINDER_SUB_STEPS.get("causative", [])
    all_done = all(
        step_statuses.get(ss, {}).get("status") in ("done", "complete")
        for ss in subs
    )
    if all_done:
        return True
    # Fallback: check if refinement output exists in file_mirror or as local file
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT 1 FROM file_mirror WHERE path LIKE 'local_runner/cache/refinement_%' LIMIT 1"
            ).fetchone()
            if row:
                return True
    except Exception:
        pass
    # Also check local cache directory
    cache_dir = REPO_ROOT / "local_runner" / "cache"
    if cache_dir.exists():
        for f in cache_dir.iterdir():
            if f.name.startswith("refinement_") and f.suffix == ".json":
                return True
    return False


def _correlative_done(step_statuses):
    """Check if correlative grind is complete — state file OR output files exist."""
    subs = GRINDER_SUB_STEPS.get("correlative", [])
    all_done = all(
        step_statuses.get(ss, {}).get("status") in ("done", "complete")
        for ss in subs
    )
    if all_done:
        return True
    # Fallback: check if EV grinder output exists
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT 1 FROM file_mirror WHERE path LIKE 'local_runner/cache/ev_%' LIMIT 1"
            ).fetchone()
            if row:
                return True
    except Exception:
        pass
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

class TabButton(QPushButton):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._active = False
        self.setFixedHeight(48)
        self.setCursor(Qt.PointingHandCursor)
        self._restyle()

    def set_active(self, on):
        self._active = on
        self._restyle()

    def _restyle(self):
        if self._active:
            self.setStyleSheet(
                "border:none; padding:0 18px; border-bottom:2px solid %s;"
                "color:%s; font-size:12px; font-weight:600; letter-spacing:1px;"
                "background:transparent;" % (C["white"], C["white"])
            )
        else:
            self.setStyleSheet(
                "border:none; padding:0 18px; border-bottom:2px solid transparent;"
                "color:%s; font-size:12px; font-weight:600; letter-spacing:1px;"
                "background:transparent;" % C["text_muted"]
            )


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

        # Expanded card overlay — grows wider/taller, CENTERED on screen
        if self._expanded_id and self._expanded_id in self._rects:
            ex, ey, ew, _ = self._rects[self._expanded_id]
            new_w = ew + self._anim_expand_w
            new_h = nh + self._anim_expand_h
            # Center horizontally on the canvas
            center_x = w // 2 - new_w // 2
            # Lerp from original x to centered x based on animation progress
            target_w = getattr(self, '_anim_target_w', 0) or 1
            t = min(1.0, self._anim_expand_w / target_w) if target_w > 0 else 0
            new_x = int(ex + (center_x - ex) * t)
            self._rects[self._expanded_id] = (new_x, ey, new_w, new_h)

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
        """Expand/collapse a RUN node with animation — grows both directions."""
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
            # Target: much bigger — cover most of the flowchart
            nw = getattr(self, '_nw', 300)
            nh = getattr(self, '_nh', 100)
            self._anim_target_h = max(380, nh * 4)
            self._anim_target_w = max(200, int(nw * 0.7))
            if nid in self._detail_widgets:
                self._detail_widgets[nid].setVisible(True)
            self._anim_timer.start()

    def _anim_step(self):
        """Animate expand/collapse one frame — both dimensions."""
        speed_h = 35
        speed_w = 25

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
                nh = self._nh  # base card height (header area)
                # Place detail below the header area, within the expanded rect
                widget.setGeometry(int(x + 1), int(y + nh), int(w - 2), int(h - nh - 1))
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
            "examples":     {"bg": "#2a1215", "bg_r": "#3d1b20", "border": "#5c2d33"},
            "vetting":      {"bg": "#2a1f0d", "bg_r": "#3d2e15", "border": "#5c4422"},
            "scan_tuning":  {"bg": "#2a2610", "bg_r": "#3d3818", "border": "#5c5222"},
            "causative":    {"bg": "#0d1a2a", "bg_r": "#15263d", "border": "#223d5c"},
            "correlative":  {"bg": "#0d1a2a", "bg_r": "#15263d", "border": "#223d5c"},
            "profit_grind": {"bg": "#0d1a2a", "bg_r": "#15263d", "border": "#223d5c"},
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
        if self._hover:
            self.node_clicked.emit(self._hover)


# ============================================================
# PIPELINE TAB
# ============================================================

class PipelineTab(QWidget):
    navigate_to_tab = Signal(int)

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

        # Create detail widgets as children of the canvas
        for nd in FLOW_NODES:
            if nd["kind"] == "run":
                det = GrinderDetail(nd["id"])
                det.run_requested.connect(self._fwd_run)
                det.stop_requested.connect(self._fwd_stop)
                self._canvas.add_detail_widget(nd["id"], det)
                self._details[nd["id"]] = det

        scroll.setWidget(self._canvas)
        outer.addWidget(scroll, 1)

    def _on_node_click(self, nid):
        nd = next((n for n in FLOW_NODES if n["id"] == nid), None)
        if not nd:
            return
        if nd["kind"] == "do" and nd.get("tab") is not None:
            self.navigate_to_tab.emit(nd["tab"])
        elif nd["kind"] == "run":
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
                # Progress bar: examples / winner clusters
                # winner_signals is one entry per winning cluster (365 per refinement)
                n_winners = 0
                try:
                    with get_db() as db:
                        row = db.execute(
                            "SELECT data FROM file_mirror WHERE path LIKE ? ORDER BY created_at DESC LIMIT 1",
                            ("local_runner/cache/refinement_%s_%%" % setup,),
                        ).fetchone()
                        if row:
                            rdata = json.loads(row["data"])
                            n_winners = len(rdata.get("winner_signals", []))
                except Exception:
                    pass
                if n_winners == 0:
                    cache_dir = REPO_ROOT / "local_runner" / "cache"
                    if cache_dir.exists():
                        for fp in sorted(cache_dir.iterdir(), reverse=True):
                            if fp.name.startswith("refinement_%s_" % setup) and fp.suffix == ".json":
                                try:
                                    rdata = json.loads(fp.read_text())
                                    n_winners = len(rdata.get("winner_signals", []))
                                except Exception:
                                    pass
                                break

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


class PlaceholderTab(QWidget):
    def __init__(self, title, msg, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        for text, style in [
            ("◇", "font-size:48px; color:%s; border:none;" % C["text_muted"]),
            (title, "font-size:15px; font-weight:600; color:%s; border:none;" % C["text_dim"]),
            (msg, "font-size:13px; color:%s; border:none;" % C["text_muted"]),
        ]:
            lbl = QLabel(text)
            lbl.setStyleSheet(style)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setWordWrap(True)
            lbl.setMaximumWidth(400)
            lay.addWidget(lbl)


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

        # Top bar
        top = QFrame()
        top.setFixedHeight(48)
        top.setStyleSheet("QFrame { background:%s; border-bottom:1px solid %s; }" % (C["surface"], C["border"]))
        tl = QHBoxLayout(top)
        tl.setContentsMargins(20, 0, 20, 0)
        tl.setSpacing(0)

        logo = QLabel("SCANPERFECT")
        logo.setStyleSheet("font-size:12px; font-weight:700; letter-spacing:3px; color:%s; border:none; background:transparent;" % C["white"])
        tl.addWidget(logo)
        sep = QLabel("│")
        sep.setStyleSheet("color:%s; font-size:14px; margin:0 12px; border:none; background:transparent;" % C["border"])
        tl.addWidget(sep)

        self._tab_btns = []
        for i, name in enumerate(["PIPELINE", "EXAMPLES", "VETTING", "WATCHLIST"]):
            btn = TabButton(name)
            btn.clicked.connect(lambda _, idx=i: self._switch_tab(idx))
            tl.addWidget(btn)
            self._tab_btns.append(btn)

        tl.addStretch()

        self._setup_combo = QComboBox()
        self._setup_combo.currentIndexChanged.connect(self._on_setup_changed)
        tl.addWidget(self._setup_combo)

        ml.addWidget(top)

        # Stack
        self._stack = QStackedWidget()
        self._pipeline = PipelineTab()
        self._pipeline.navigate_to_tab.connect(self._switch_tab)
        self._stack.addWidget(self._pipeline)
        self._stack.addWidget(PlaceholderTab("Examples", "Coming in Increment 2 — example library with chart thumbnails."))
        self._stack.addWidget(PlaceholderTab("Vetting", "Coming in Increment 3 — full-screen chart vetting workflow."))
        self._stack.addWidget(PlaceholderTab("Nightly Watchlist", "Not yet built — will show ranked signals across all setups."))
        ml.addWidget(self._stack)

        # Init
        self._load_setups()
        self._switch_tab(0)
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

    def _switch_tab(self, idx):
        self._stack.setCurrentIndex(idx)
        for i, b in enumerate(self._tab_btns):
            b.set_active(i == idx)

    def _on_setup_changed(self):
        data = self._setup_combo.currentData()
        if data:
            self._setup_type = data
            self._pipeline.set_setup(data)
            self._pipeline.refresh()

    def _on_tick(self):
        if self._stack.currentIndex() == 0:
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


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    init_db()
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
