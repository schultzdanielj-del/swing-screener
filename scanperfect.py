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
from PySide6.QtGui import QFont, QFontDatabase, QColor, QPainter, QPen


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

FLOW_NODES = [
    {"id": "examples",     "name": "Examples",          "type": "nav",         "tab": 1,
     "desc": "Define setups · manage example libraries"},
    {"id": "causative",    "name": "Causative Grind",   "type": "grinder",     "tab": None,
     "desc": "Signal → Exit → Refinement",
     "sub_steps": ["signal_grind", "exit_grind", "refinement_grind"]},
    {"id": "vetting",      "name": "Vetting",           "type": "nav",         "tab": 2,
     "desc": "Review winners · bank new examples"},
    {"id": "correlative",  "name": "Correlative Grind", "type": "grinder",     "tab": None,
     "desc": "EV scoring — predicted WR, MFE, EV per signal",
     "sub_steps": ["ev_grind"]},
    {"id": "scan_tuning",  "name": "Scan Tuning",       "type": "placeholder", "tab": None,
     "desc": "Quality score + WR threshold sliders"},
    {"id": "profit_grind", "name": "Profit Grind",      "type": "placeholder", "tab": None,
     "desc": "Optimize exit strategy · maximize SQN"},
    {"id": "summary",      "name": "Summary",           "type": "summary",     "tab": None,
     "desc": "Setup readiness overview"},
]

GRINDER_SUB_STEPS = {
    "causative":   ["signal_grind", "exit_grind", "refinement_grind"],
    "correlative": ["ev_grind"],
}


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



class FlowCard(QFrame):
    """A single card in the pipeline flowchart."""
    clicked = Signal(str)

    def __init__(self, node_def, parent=None):
        super().__init__(parent)
        self.node_id = node_def["id"]
        self.node_type = node_def["type"]
        self._status = "idle"
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(200, 70)
        self._restyle()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._name_lbl = QLabel(node_def["name"])
        self._name_lbl.setStyleSheet(
            "font-size:12px; font-weight:700; color:%s; background:transparent; border:none;" % C["text"]
        )
        top.addWidget(self._name_lbl)
        top.addStretch()
        self._badge = StatusBadge("idle")
        top.addWidget(self._badge)
        lay.addLayout(top)

        self._desc_lbl = QLabel(node_def["desc"])
        self._desc_lbl.setStyleSheet(
            "font-size:10px; color:%s; background:transparent; border:none;" % C["text_muted"]
        )
        self._desc_lbl.setWordWrap(True)
        lay.addWidget(self._desc_lbl)

    def set_status(self, s):
        self._status = s
        self._badge.set_status(s)
        self._restyle()

    def set_info(self, text):
        self._desc_lbl.setText(text)

    def _restyle(self):
        cmap = {"done": C["green"], "complete": C["green"], "running": C["amber"],
                "queued": C["amber"], "error": C["red"]}
        bc = cmap.get(self._status, C["border_bright"])
        self.setStyleSheet(
            "FlowCard { background:%s; border:1px solid %s; border-left:3px solid %s; }"
            "FlowCard:hover { background:%s; border-color:%s; border-left:3px solid %s; }"
            % (C["surface"], C["border"], bc, C["surface2"], C["border_bright"], bc)
        )

    def mousePressEvent(self, ev):
        self.clicked.emit(self.node_id)
        super().mousePressEvent(ev)


class GrinderDetail(QFrame):
    """Expandable detail panel with sub-step progress, Run/Stop, and log."""
    run_requested = Signal(str)
    stop_requested = Signal(str)

    def __init__(self, node_id, sub_steps, parent=None):
        super().__init__(parent)
        self.node_id = node_id
        self._sub_steps = sub_steps
        self.setStyleSheet(
            "GrinderDetail { background:%s; border:1px solid %s; }" % (C["surface"], C["border"])
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)

        # Sub-step indicators (only if multiple sub-steps)
        self._sub_labels = {}
        if len(sub_steps) > 1:
            sub_row = QHBoxLayout()
            sub_row.setSpacing(6)
            for ss in sub_steps:
                nice = ss.replace("_", " ").title()
                lbl = QLabel(nice)
                lbl.setStyleSheet(
                    "font-size:10px; font-weight:600; color:%s; background:%s;"
                    "padding:3px 8px; border:none;" % (C["text_muted"], C["surface2"])
                )
                lbl.setAlignment(Qt.AlignCenter)
                sub_row.addWidget(lbl)
                self._sub_labels[ss] = lbl
            sub_row.addStretch()
            lay.addLayout(sub_row)

        # Metrics + buttons row
        mrow = QHBoxLayout()
        mrow.setSpacing(20)
        self._m = {}
        for key, label in [("status", "STATUS"), ("lastrun", "LAST RUN"),
                           ("duration", "DURATION"), ("setup", "SETUP")]:
            col = QVBoxLayout()
            col.setSpacing(1)
            k = QLabel(label)
            k.setStyleSheet(
                "font-size:9px; font-weight:700; letter-spacing:1px; color:%s;"
                "background:transparent; border:none;" % C["text_muted"]
            )
            col.addWidget(k)
            v = QLabel("\u2014")
            v.setStyleSheet(
                "font-size:13px; font-weight:500; color:%s; background:transparent; border:none;"
                "font-family:\'JetBrains Mono\',\'Consolas\',monospace;" % C["text"]
            )
            col.addWidget(v)
            self._m[key] = v
            mrow.addLayout(col)
        mrow.addStretch()

        brow = QHBoxLayout()
        brow.setSpacing(8)
        self._run_btn = QPushButton("\u25b6  RUN")
        self._run_btn.setObjectName("runBtn")
        self._run_btn.clicked.connect(lambda: self.run_requested.emit(self.node_id))
        brow.addWidget(self._run_btn)
        self._stop_btn = QPushButton("\u25a0  STOP")
        self._stop_btn.setObjectName("stopBtn")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(lambda: self.stop_requested.emit(self.node_id))
        brow.addWidget(self._stop_btn)
        clr = QPushButton("CLEAR LOG")
        clr.clicked.connect(lambda: self._log.clear())
        brow.addWidget(clr)
        brow.addStretch()
        mrow.addLayout(brow)
        lay.addLayout(mrow)

        # Log
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(180)
        self._log.setMaximumHeight(350)
        self._log.setPlaceholderText("No logs yet")
        lay.addWidget(self._log)

        # Load existing logs
        logs = load_json(PIPELINE_LOGS_FILE, {})
        combined = []
        for ss in sub_steps:
            combined.extend(logs.get(ss, []))
        if combined:
            self._log.setPlainText("\n".join(combined[-500:]))
            sb = self._log.verticalScrollBar()
            sb.setValue(sb.maximum())

    def set_setup(self, setup):
        self._m["setup"].setText(setup.upper())

    def update_from_state(self, pipeline_steps):
        overall = "idle"
        last_finished = None
        total_dur = 0
        for ss in self._sub_steps:
            st = pipeline_steps.get(ss, {})
            s = st.get("status", "idle")
            if s in ("running", "queued"):
                overall = "running"
            elif s in ("done", "complete") and overall not in ("running", "error"):
                overall = "done"
            elif s == "error":
                overall = "error"
            if st.get("duration_s"):
                total_dur += st["duration_s"]
            fin = st.get("finished_at")
            if fin:
                last_finished = fin
            if ss in self._sub_labels:
                scmap = {"done": C["green"], "complete": C["green"],
                         "running": C["amber"], "error": C["red"]}
                sc = scmap.get(s, C["text_muted"])
                bgc = {"done": "rgba(74,222,128,0.12)", "complete": "rgba(74,222,128,0.12)",
                        "running": "rgba(251,191,36,0.12)", "error": "rgba(248,113,113,0.12)"
                       }.get(s, C["surface2"])
                self._sub_labels[ss].setStyleSheet(
                    "font-size:10px; font-weight:600; color:%s; background:%s;"
                    "padding:3px 8px; border:none;" % (sc, bgc)
                )

        self._m["status"].setText(overall.upper())
        sc = {"done": C["green"], "running": C["amber"], "error": C["red"]}.get(overall, C["text"])
        self._m["status"].setStyleSheet(
            "font-size:13px; font-weight:500; color:%s; background:transparent; border:none;"
            "font-family:\'JetBrains Mono\',\'Consolas\',monospace;" % sc
        )
        if last_finished:
            try:
                self._m["lastrun"].setText(datetime.fromisoformat(last_finished).strftime("%Y-%m-%d %H:%M"))
            except Exception:
                self._m["lastrun"].setText(str(last_finished)[:16])
        if total_dur > 0:
            self._m["duration"].setText(fmt_dur(total_dur))
        is_run = overall in ("running", "queued")
        self._run_btn.setEnabled(not is_run)
        self._stop_btn.setEnabled(is_run)
        return overall

    def append_log(self, text):
        self._log.appendPlainText(text)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear_log(self):
        self._log.clear()


class FlowchartCanvas(QWidget):
    """QPainter canvas that draws connecting lines and loop-back arrows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(900, 520)
        self._positions = {}

    def paintEvent(self, _):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pos = self._positions
        if not pos:
            p.end()
            return

        pen_main = QPen(QColor(C["border_bright"]), 1.5)
        pen_loop = QPen(QColor(C["text_muted"]), 1, Qt.DashLine)
        pen_text = QPen(QColor(C["text_muted"]))

        def rect_center(nid):
            r = pos.get(nid)
            return (r[0] + r[2]//2, r[1] + r[3]//2) if r else None

        def rect_edge(nid, side):
            r = pos.get(nid)
            if not r:
                return None
            x, y, w, h = r
            if side == "top":    return (x + w//2, y)
            if side == "bottom": return (x + w//2, y + h)
            if side == "left":   return (x, y + h//2)
            if side == "right":  return (x + w, y + h//2)

        def arrow_head(x2, y2, x1, y1, size=6):
            angle = math.atan2(y2 - y1, x2 - x1)
            p.drawLine(int(x2), int(y2),
                       int(x2 - size * math.cos(angle - 0.4)),
                       int(y2 - size * math.sin(angle - 0.4)))
            p.drawLine(int(x2), int(y2),
                       int(x2 - size * math.cos(angle + 0.4)),
                       int(y2 - size * math.sin(angle + 0.4)))

        def draw_line(x1, y1, x2, y2, head=True):
            p.drawLine(int(x1), int(y1), int(x2), int(y2))
            if head:
                arrow_head(x2, y2, x1, y1)

        def dot(x, y):
            p.setBrush(QColor(C["border_bright"]))
            p.drawEllipse(int(x) - 3, int(y) - 3, 6, 6)
            p.setBrush(Qt.NoBrush)

        # ═══ LOOP 1: Examples → Causative → Vetting ═══
        p.setPen(pen_main)

        a = rect_edge("examples", "right")
        b = rect_edge("causative", "left")
        if a and b:
            dot(a[0], a[1])
            draw_line(a[0], a[1], b[0], b[1])

        a = rect_edge("causative", "right")
        b = rect_edge("vetting", "left")
        if a and b:
            dot(a[0], a[1])
            draw_line(a[0], a[1], b[0], b[1])

        # Loop-back arc: Vetting → Examples (above row 1)
        vt = rect_edge("vetting", "top")
        et = rect_edge("examples", "top")
        if vt and et:
            p.setPen(pen_loop)
            arc_y = et[1] - 30
            dot(vt[0], vt[1])
            p.drawLine(int(vt[0]), int(vt[1]), int(vt[0]), int(arc_y))
            p.drawLine(int(vt[0]), int(arc_y), int(et[0]), int(arc_y))
            p.setPen(pen_loop)
            draw_line(et[0], arc_y, et[0], et[1])
            p.setPen(pen_text)
            p.setFont(QFont("DM Sans", 8))
            mid_x = (vt[0] + et[0]) // 2
            p.drawText(int(mid_x) - 50, int(arc_y) - 6, "add examples \u00b7 regrind")
            p.setPen(pen_main)

        # ═══ Vetting → Correlative (downward) ═══
        a = rect_edge("vetting", "bottom")
        b = rect_edge("correlative", "top")
        if a and b:
            dot(a[0], a[1])
            # Route: down from vetting, then left/right to correlative top
            mid_y = (a[1] + b[1]) // 2
            p.drawLine(int(a[0]), int(a[1]), int(a[0]), int(mid_y))
            p.drawLine(int(a[0]), int(mid_y), int(b[0]), int(mid_y))
            draw_line(b[0], mid_y, b[0], b[1])

        # ═══ Correlative → Scan Tuning (downward) ═══
        a = rect_edge("correlative", "bottom")
        b = rect_edge("scan_tuning", "top")
        if a and b:
            dot(a[0], a[1])
            mid_y = (a[1] + b[1]) // 2
            p.drawLine(int(a[0]), int(a[1]), int(a[0]), int(mid_y))
            p.drawLine(int(a[0]), int(mid_y), int(b[0]), int(mid_y))
            draw_line(b[0], mid_y, b[0], b[1])

        # ═══ LOOP 2: Scan Tuning → Profit Grind ═══
        a = rect_edge("scan_tuning", "right")
        b = rect_edge("profit_grind", "left")
        if a and b:
            dot(a[0], a[1])
            draw_line(a[0], a[1], b[0], b[1])

        # Loop-back: Profit Grind → Scan Tuning (below row 3)
        pb = rect_edge("profit_grind", "bottom")
        sb = rect_edge("scan_tuning", "bottom")
        if pb and sb:
            p.setPen(pen_loop)
            arc_y = max(pb[1], sb[1]) + 30
            dot(pb[0], pb[1])
            p.drawLine(int(pb[0]), int(pb[1]), int(pb[0]), int(arc_y))
            p.drawLine(int(pb[0]), int(arc_y), int(sb[0]), int(arc_y))
            p.setPen(pen_loop)
            draw_line(sb[0], arc_y, sb[0], sb[1])
            p.setPen(pen_text)
            p.setFont(QFont("DM Sans", 8))
            mid_x = (pb[0] + sb[0]) // 2
            p.drawText(int(mid_x) - 30, int(arc_y) + 14, "tweak \u00b7 re-run")
            p.setPen(pen_main)

        # ═══ Profit Grind → Summary ═══
        a = rect_edge("profit_grind", "bottom")
        b = rect_edge("summary", "top")
        if a and b:
            dot(a[0], a[1])
            mid_y = (a[1] + 40 + b[1]) // 2  # offset to avoid loop arc
            p.drawLine(int(a[0]), int(a[1] + 40), int(a[0]), int(mid_y))
            p.drawLine(int(a[0]), int(mid_y), int(b[0]), int(mid_y))
            draw_line(b[0], mid_y, b[0], b[1])

        p.end()


# ============================================================
# PIPELINE TAB
# ============================================================

class PipelineTab(QWidget):
    navigate_to_tab = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Flowchart scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._canvas = FlowchartCanvas()

        self._cards = {}
        self._details = {}
        self._expanded = None

        for nd in PIPELINE_NODES:
            card = FlowCard(nd, self._canvas)
            card.clicked.connect(self._on_click)
            self._cards[nd["id"]] = card

            if nd["type"] == "grinder":
                sub = GRINDER_SUB_STEPS.get(nd["id"], [])
                det = GrinderDetail(nd["id"], sub)
                det.setVisible(False)
                det.run_requested.connect(self._fwd_run)
                det.stop_requested.connect(self._fwd_stop)
                self._details[nd["id"]] = det

        scroll.setWidget(self._canvas)
        outer.addWidget(scroll, 1)

        # Detail area below flowchart
        self._detail_area = QWidget()
        det_lay = QVBoxLayout(self._detail_area)
        det_lay.setContentsMargins(40, 0, 40, 12)
        for det in self._details.values():
            det_lay.addWidget(det)
        self._detail_area.setVisible(False)
        outer.addWidget(self._detail_area)

        QTimer.singleShot(50, self._layout_cards)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._layout_cards()

    def _layout_cards(self):
        cw, ch = 200, 70
        canvas_w = max(self._canvas.width(), 900)
        gap_h, gap_v = 50, 70

        # Row 1: Examples — Causative — Vetting
        row1_w = cw * 3 + gap_h * 2
        row1_x = (canvas_w - row1_w) // 2
        row1_y = 55

        pos = {}
        pos["examples"]  = (row1_x, row1_y, cw, ch)
        pos["causative"] = (row1_x + cw + gap_h, row1_y, cw, ch)
        pos["vetting"]   = (row1_x + 2*(cw + gap_h), row1_y, cw, ch)

        # Row 2: Correlative (centered under row 1)
        row2_y = row1_y + ch + gap_v
        row1_center = row1_x + row1_w // 2
        pos["correlative"] = (row1_center - cw//2, row2_y, cw, ch)

        # Row 3: Scan Tuning — Profit Grind
        row3_y = row2_y + ch + gap_v
        row3_w = cw * 2 + gap_h
        row3_x = row1_center - row3_w // 2
        pos["scan_tuning"]  = (row3_x, row3_y, cw, ch)
        pos["profit_grind"] = (row3_x + cw + gap_h, row3_y, cw, ch)

        # Row 4: Summary
        row4_y = row3_y + ch + gap_v + 30
        pos["summary"] = (row1_center - cw//2, row4_y, cw, ch)

        for nid, (x, y, w, h) in pos.items():
            card = self._cards.get(nid)
            if card:
                card.setGeometry(int(x), int(y), int(w), int(h))
                card.show()

        self._canvas._positions = pos
        self._canvas.setMinimumHeight(int(row4_y + ch + 40))
        self._canvas.update()

    def _on_click(self, nid):
        nd = next((n for n in PIPELINE_NODES if n["id"] == nid), None)
        if not nd:
            return
        if nd["type"] == "nav":
            self.navigate_to_tab.emit(nd["tab"])
        elif nd["type"] == "grinder":
            if self._expanded == nid:
                self._details[nid].setVisible(False)
                self._detail_area.setVisible(False)
                self._expanded = None
            else:
                for k, d in self._details.items():
                    d.setVisible(k == nid)
                self._detail_area.setVisible(True)
                self._expanded = nid

    def _fwd_run(self, node_id):
        win = self.window()
        if hasattr(win, "run_pipeline_step"):
            sub = GRINDER_SUB_STEPS.get(node_id, [node_id])
            win.run_pipeline_step(sub[0])

    def _fwd_stop(self, step_id):
        win = self.window()
        if hasattr(win, "stop_pipeline_step"):
            win.stop_pipeline_step(step_id)

    def get_node(self, nid):
        for gid, subs in GRINDER_SUB_STEPS.items():
            if nid in subs:
                return self._details.get(gid)
        return self._details.get(nid)

    def set_setup(self, setup):
        for d in self._details.values():
            d.set_setup(setup)

    def refresh(self):
        state = load_json(PIPELINE_FILE, {"steps": {}})
        steps = state.get("steps", {})
        for nd in PIPELINE_NODES:
            nid = nd["id"]
            card = self._cards.get(nid)
            if not card:
                continue
            if nd["type"] == "grinder":
                sub = GRINDER_SUB_STEPS.get(nid, [])
                agg = "idle"
                for ss in sub:
                    s = steps.get(ss, {}).get("status", "idle")
                    if s in ("running", "queued"):
                        agg = "running"
                    elif s in ("done", "complete") and agg not in ("running", "error"):
                        agg = "done"
                    elif s == "error":
                        agg = "error"
                card.set_status(agg)
                if nid in self._details:
                    self._details[nid].update_from_state(steps)
            elif nid == "examples":
                try:
                    win = self.window()
                    setup = win._setup_type if hasattr(win, "_setup_type") else "dtss"
                    with get_db() as db:
                        n = db.execute("SELECT COUNT(*) FROM examples WHERE setup_type=?", (setup,)).fetchone()[0]
                    card.set_info(f"{n} examples")
                    card.set_status("done" if n > 0 else "idle")
                except Exception:
                    pass
            elif nid == "vetting":
                try:
                    win = self.window()
                    setup = win._setup_type if hasattr(win, "_setup_type") else "dtss"
                    vp = REPO_ROOT / "data" / "vetting" / f"vetting_{setup}.json"
                    dec = load_json(vp, {})
                    ny = sum(1 for v in dec.values() if v.get("verdict") == "yes")
                    card.set_info(f"{ny} yes / {len(dec)} vetted")
                    card.set_status("done" if ny > 0 else "idle")
                except Exception:
                    pass
            elif nid == "summary":
                card.set_status("idle")
                card.set_info("Setup readiness overview")
            else:
                card.set_status("idle")
                card.set_info("not yet built")


# ============================================================
# PLACEHOLDER TAB
# ============================================================

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
