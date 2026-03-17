"""
ScanPerfect — Native Desktop UI (PySide6)
Phase 6 of localization. Replaces browser-based HTML UI entirely.
Reads directly from SQLite + OHLCV pickle. No server process needed.
"""

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    Qt, QProcess, QProcessEnvironment, QTimer, Signal, QSize,
)
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QPainter, QPen,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QStackedWidget, QScrollArea,
    QTextEdit, QSizePolicy,
)


# ============================================================
# PATHS
# ============================================================

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "scanperfect.db"
PIPELINE_FILE = DATA_DIR / "pipeline_state.json"
PIPELINE_LOGS_FILE = DATA_DIR / "pipeline_logs.json"
LOCAL_DIR = REPO_ROOT / "local_runner"

DATA_DIR.mkdir(exist_ok=True)


# ============================================================
# COLORS
# ============================================================

class C:
    """Color constants — Gordon Murray stripped style."""
    BG        = "#0a0e17"
    SURFACE   = "#111827"
    SURFACE2  = "#1a2035"
    BORDER    = "#2a3550"
    BORDER_DIM = "#1e2940"
    TEXT      = "#e2e8f0"
    TEXT_DIM  = "#94a3b8"
    TEXT_MUTED = "#64748b"
    GREEN     = "#4ade80"
    GREEN_DK  = "#059669"
    RED       = "#f87171"
    RED_DK    = "#dc2626"
    AMBER     = "#fbbf24"
    BLUE      = "#60a5fa"
    WHITE     = "#ffffff"
    NODE_BG   = "#0f1520"
    NODE_HOVER = "#151d2e"
    ARROW     = "#2a3550"


# ============================================================
# JSON HELPERS
# ============================================================

def _load_json(path, default=None):
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


def _save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ============================================================
# DATABASE HELPERS
# ============================================================

@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def db_exists():
    return DB_PATH.exists()


def get_setups():
    if not db_exists():
        return {"dtss": {"name": "DTSS", "desc": "Double Top Short Sell",
                         "direction": "short", "examples": 0}}
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT setup_type, name, description, direction FROM setups ORDER BY created_at"
            ).fetchall()
            result = {}
            for r in rows:
                n = db.execute(
                    "SELECT COUNT(*) FROM examples WHERE setup_type=?", (r["setup_type"],)
                ).fetchone()[0]
                result[r["setup_type"]] = {
                    "name": r["name"], "desc": r["description"],
                    "direction": r["direction"], "examples": n,
                }
            return result if result else {
                "dtss": {"name": "DTSS", "desc": "", "direction": "short", "examples": 0}
            }
    except Exception:
        return {"dtss": {"name": "DTSS", "desc": "", "direction": "short", "examples": 0}}


def get_vetting_counts(setup_type):
    try:
        vetting_path = DATA_DIR / "vetting" / f"vetting_{setup_type}.json"
        decisions = _load_json(vetting_path, {})
        n_vetted = len(decisions)
        if db_exists():
            with get_db() as db:
                row = db.execute(
                    "SELECT data FROM file_mirror WHERE path LIKE ? ORDER BY created_at DESC LIMIT 1",
                    (f"local_runner/cache/refinement_{setup_type}_%",),
                ).fetchone()
                if row:
                    data = json.loads(row["data"])
                    return n_vetted, len(data.get("winner_signals", []))
        return n_vetted, 0
    except Exception:
        return 0, 0


# ============================================================
# PIPELINE STATE
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


def load_pipeline_state():
    return _load_json(PIPELINE_FILE, {"steps": {}, "jobs": []})

def save_pipeline_state(state):
    _save_json(PIPELINE_FILE, state)

def load_pipeline_logs():
    return _load_json(PIPELINE_LOGS_FILE, {})

def save_pipeline_logs(logs):
    _save_json(PIPELINE_LOGS_FILE, logs)

def get_step_status(step_id):
    state = load_pipeline_state()
    return state.get("steps", {}).get(step_id, {}).get("status", "idle")

def get_step_state(step_id):
    state = load_pipeline_state()
    return state.get("steps", {}).get(step_id, {
        "status": "idle", "started_at": None, "finished_at": None,
        "duration_s": None, "exit_code": None, "error": None, "result_summary": None,
    })


# ============================================================
# STYLESHEET
# ============================================================

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {C.BG};
    color: {C.TEXT};
    font-family: "DM Sans", "Segoe UI", sans-serif;
    font-size: 13px;
}}
QLabel {{
    color: {C.TEXT};
    background: transparent;
}}
QPushButton {{
    background: transparent;
    color: {C.TEXT_DIM};
    border: 1px solid {C.BORDER};
    padding: 6px 16px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}
QPushButton:hover {{
    border-color: {C.TEXT_MUTED};
    color: {C.TEXT};
}}
QPushButton:disabled {{
    color: {C.BORDER};
    border-color: {C.BORDER_DIM};
}}
QComboBox {{
    background: {C.SURFACE};
    color: {C.TEXT};
    border: 1px solid {C.BORDER};
    padding: 4px 10px;
    font-size: 12px;
    min-width: 120px;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {C.SURFACE};
    color: {C.TEXT};
    border: 1px solid {C.BORDER};
    selection-background-color: {C.SURFACE2};
}}
QTextEdit {{
    background: {C.BG};
    color: {C.TEXT_DIM};
    border: 1px solid {C.BORDER_DIM};
    font-family: "JetBrains Mono", "Consolas", "Courier New", monospace;
    font-size: 11px;
    padding: 8px;
    selection-background-color: {C.SURFACE2};
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{
    background: transparent; width: 6px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {C.BORDER}; border-radius: 3px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {C.TEXT_MUTED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ height: 0; }}
"""


# ============================================================
# STATUS BADGE
# ============================================================

class StatusBadge(QLabel):
    COLORS = {
        "idle":      (C.TEXT_MUTED, C.SURFACE2),
        "pending":   (C.TEXT_MUTED, C.SURFACE2),
        "running":   (C.AMBER, "rgba(251,191,36,0.15)"),
        "queued":    (C.AMBER, "rgba(251,191,36,0.15)"),
        "claimed":   (C.AMBER, "rgba(251,191,36,0.15)"),
        "done":      (C.GREEN, "rgba(74,222,128,0.1)"),
        "complete":  (C.GREEN, "rgba(74,222,128,0.1)"),
        "error":     (C.RED,   "rgba(248,113,113,0.1)"),
        "stopped":   (C.TEXT_MUTED, C.SURFACE2),
        "not_built": (C.TEXT_MUTED, C.SURFACE2),
    }

    def __init__(self, status="idle", parent=None):
        super().__init__(parent)
        self.set_status(status)

    def set_status(self, status):
        fg, bg = self.COLORS.get(status, (C.TEXT_MUTED, C.SURFACE2))
        self.setText(status.upper().replace("_", " "))
        self.setStyleSheet(
            f"QLabel {{ color: {fg}; background: {bg}; font-size: 9px; font-weight: 700;"
            f" letter-spacing: 0.6px; padding: 2px 8px; border: none; }}"
        )


# ============================================================
# PIPELINE NODE
# ============================================================

class PipelineNode(QWidget):
    clicked = Signal(str)

    def __init__(self, node_id, number, name, description, node_type="grinder", parent=None):
        super().__init__(parent)
        self.node_id = node_id
        self.node_type = node_type  # grinder | navigate | placeholder
        self._expanded = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(64)

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(0)

        # ── Header bar ──
        self._header = QWidget()
        self._set_header_bg(False)
        hdr = QHBoxLayout(self._header)
        hdr.setContentsMargins(16, 12, 16, 12)
        hdr.setSpacing(12)

        # Number
        self._num = QLabel(str(number))
        self._num.setFixedSize(28, 28)
        self._num.setAlignment(Qt.AlignCenter)
        self._style_num("idle")
        hdr.addWidget(self._num)

        # Name + desc
        txt = QVBoxLayout()
        txt.setSpacing(2)
        nm = QLabel(name)
        nm.setStyleSheet(f"QLabel {{ color: {C.TEXT}; font-size: 13px; font-weight: 600; border: none; }}")
        txt.addWidget(nm)
        ds = QLabel(description)
        ds.setStyleSheet(f"QLabel {{ color: {C.TEXT_MUTED}; font-size: 11px; border: none; }}")
        txt.addWidget(ds)
        hdr.addLayout(txt, 1)

        # Info label
        self._info = QLabel("")
        self._info.setStyleSheet(
            f"QLabel {{ color: {C.TEXT_DIM}; font-size: 11px; border: none;"
            f" font-family: 'JetBrains Mono', 'Consolas', monospace; }}"
        )
        hdr.addWidget(self._info)

        # Badge
        self._badge = StatusBadge("idle")
        hdr.addWidget(self._badge)

        # Chevron / arrow
        if node_type == "grinder":
            self._chev = QLabel("▸")
            self._chev.setStyleSheet(f"QLabel {{ color: {C.TEXT_MUTED}; font-size: 14px; border: none; }}")
            hdr.addWidget(self._chev)
        elif node_type == "navigate":
            arrow = QLabel("→")
            arrow.setStyleSheet(f"QLabel {{ color: {C.BLUE}; font-size: 14px; border: none; }}")
            hdr.addWidget(arrow)

        self._main_layout.addWidget(self._header)

        # ── Detail panel (grinder nodes only) ──
        self._detail = None
        self._log_view = None
        self._metrics = {}
        self._result_lbl = None
        self._run_btn = None
        self._stop_btn = None
        if node_type == "grinder":
            self._build_detail()

    def _set_header_bg(self, hover):
        bg = C.NODE_HOVER if hover else C.NODE_BG
        brd = C.BORDER if hover else C.BORDER_DIM
        self._header.setStyleSheet(f"QWidget {{ background: {bg}; border: 1px solid {brd}; }}")

    def _style_num(self, status):
        if status in ("done", "complete"):
            s = f"color:{C.GREEN}; border:1px solid {C.GREEN_DK}; background:rgba(74,222,128,0.08);"
        elif status in ("running", "queued", "claimed"):
            s = f"color:{C.AMBER}; border:1px solid rgba(251,191,36,0.3); background:rgba(251,191,36,0.08);"
        elif status == "error":
            s = f"color:{C.RED}; border:1px solid {C.RED_DK}; background:rgba(248,113,113,0.08);"
        else:
            s = f"color:{C.TEXT_MUTED}; border:1px solid {C.BORDER}; background:transparent;"
        self._num.setStyleSheet(f"QLabel {{ {s} font-size:12px; font-weight:700; }}")

    def _build_detail(self):
        self._detail = QWidget()
        self._detail.setVisible(False)
        self._detail.setStyleSheet(
            f"QWidget {{ background: {C.BG}; border: 1px solid {C.BORDER_DIM}; border-top: none; }}"
        )
        dl = QVBoxLayout(self._detail)
        dl.setContentsMargins(16, 12, 16, 12)
        dl.setSpacing(10)

        # Metrics + buttons row
        row = QHBoxLayout()
        row.setSpacing(24)
        for key, label in [("status", "STATUS"), ("last_run", "LAST RUN"),
                           ("duration", "DURATION"), ("setup", "SETUP")]:
            box = QVBoxLayout()
            box.setSpacing(2)
            lbl = QLabel(label)
            lbl.setStyleSheet(
                f"QLabel {{ color:{C.TEXT_MUTED}; font-size:9px; font-weight:700;"
                f" letter-spacing:1.2px; border:none; }}"
            )
            box.addWidget(lbl)
            val = QLabel("—")
            val.setStyleSheet(
                f"QLabel {{ color:{C.TEXT_DIM}; font-size:13px; font-weight:500;"
                f" font-family:'JetBrains Mono','Consolas',monospace; border:none; }}"
            )
            self._metrics[key] = val
            box.addWidget(val)
            row.addLayout(box)
        row.addStretch()

        self._run_btn = QPushButton("▶  RUN")
        self._run_btn.setCursor(Qt.PointingHandCursor)
        self._run_btn.setFixedWidth(100)
        self._run_btn.setStyleSheet(
            f"QPushButton {{ color:{C.GREEN}; border:1px solid {C.GREEN_DK}; padding:6px 16px;"
            f" font-size:11px; font-weight:600; }}"
            f" QPushButton:hover {{ background:{C.GREEN_DK}; color:{C.WHITE}; }}"
            f" QPushButton:disabled {{ color:{C.BORDER}; border-color:{C.BORDER_DIM}; }}"
        )
        self._run_btn.clicked.connect(self._on_run)
        row.addWidget(self._run_btn)

        self._stop_btn = QPushButton("■  STOP")
        self._stop_btn.setCursor(Qt.PointingHandCursor)
        self._stop_btn.setFixedWidth(100)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            f"QPushButton {{ color:{C.RED}; border:1px solid {C.RED_DK}; padding:6px 16px;"
            f" font-size:11px; font-weight:600; }}"
            f" QPushButton:hover {{ background:{C.RED_DK}; color:{C.WHITE}; }}"
            f" QPushButton:disabled {{ color:{C.BORDER}; border-color:{C.BORDER_DIM}; }}"
        )
        self._stop_btn.clicked.connect(self._on_stop)
        row.addWidget(self._stop_btn)

        dl.addLayout(row)

        # Log header
        lh = QHBoxLayout()
        ll = QLabel("LOG")
        ll.setStyleSheet(
            f"QLabel {{ color:{C.TEXT_MUTED}; font-size:9px; font-weight:700;"
            f" letter-spacing:1.2px; border:none; }}"
        )
        lh.addWidget(ll)
        lh.addStretch()
        clr = QPushButton("CLEAR")
        clr.setCursor(Qt.PointingHandCursor)
        clr.setStyleSheet(
            f"QPushButton {{ font-size:9px; padding:2px 8px; color:{C.TEXT_MUTED};"
            f" border:1px solid {C.BORDER_DIM}; }}"
            f" QPushButton:hover {{ color:{C.TEXT_DIM}; border-color:{C.BORDER}; }}"
        )
        clr.clicked.connect(self._clear_log)
        lh.addWidget(clr)
        dl.addLayout(lh)

        # Log text
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(200)
        self._log_view.setMaximumHeight(400)
        self._log_view.setPlaceholderText("No logs yet")
        dl.addWidget(self._log_view)

        # Result summary
        self._result_lbl = QLabel("")
        self._result_lbl.setWordWrap(True)
        self._result_lbl.setStyleSheet(
            f"QLabel {{ color:{C.GREEN}; font-size:11px;"
            f" font-family:'JetBrains Mono','Consolas',monospace;"
            f" background:rgba(74,222,128,0.05); padding:8px;"
            f" border:1px solid rgba(74,222,128,0.12); }}"
        )
        self._result_lbl.setVisible(False)
        dl.addWidget(self._result_lbl)

        self._main_layout.addWidget(self._detail)

    # ── Public API ──

    def set_status(self, status):
        self._badge.set_status(status)
        self._style_num(status)

    def set_info(self, text):
        self._info.setText(text)

    def toggle_expand(self):
        if self.node_type != "grinder" or not self._detail:
            return
        self._expanded = not self._expanded
        self._detail.setVisible(self._expanded)
        if hasattr(self, '_chev'):
            self._chev.setText("▾" if self._expanded else "▸")
        if self._expanded:
            self._load_existing_logs()

    def collapse(self):
        if self._expanded:
            self._expanded = False
            if self._detail:
                self._detail.setVisible(False)
            if hasattr(self, '_chev'):
                self._chev.setText("▸")

    def update_metrics(self, setup_type):
        if not self._detail:
            return
        st = get_step_state(self.node_id)
        status = st.get("status", "idle")

        # Status metric color
        color = C.GREEN if status in ("done", "complete") else (
            C.AMBER if status == "running" else C.TEXT_DIM
        )
        self._metrics["status"].setText(status.upper())
        self._metrics["status"].setStyleSheet(
            f"QLabel {{ color:{color}; font-size:13px; font-weight:500;"
            f" font-family:'JetBrains Mono','Consolas',monospace; border:none; }}"
        )

        # Last run
        fin = st.get("finished_at")
        if fin:
            try:
                dt = datetime.fromisoformat(fin)
                self._metrics["last_run"].setText(dt.strftime("%Y-%m-%d %H:%M"))
            except Exception:
                self._metrics["last_run"].setText("—")
        else:
            self._metrics["last_run"].setText("—")

        # Duration
        dur = st.get("duration_s")
        if dur:
            if dur < 60:
                self._metrics["duration"].setText(f"{int(dur)}s")
            elif dur < 3600:
                self._metrics["duration"].setText(f"{int(dur//60)}m {int(dur%60)}s")
            else:
                self._metrics["duration"].setText(f"{int(dur//3600)}h {int((dur%3600)//60)}m")
        else:
            self._metrics["duration"].setText("—")

        self._metrics["setup"].setText(setup_type.upper())

        # Result
        summary = st.get("result_summary")
        if summary:
            self._result_lbl.setText(summary)
            self._result_lbl.setVisible(True)
        else:
            self._result_lbl.setVisible(False)

        # Buttons
        is_running = status in ("running", "queued", "claimed")
        self._run_btn.setEnabled(not is_running)
        self._stop_btn.setEnabled(is_running)

    def append_log(self, text):
        if self._log_view:
            self._log_view.append(text)
            sb = self._log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ── Private ──

    def _load_existing_logs(self):
        if not self._log_view:
            return
        logs = load_pipeline_logs()
        lines = logs.get(self.node_id, [])
        if lines:
            self._log_view.setPlainText("\n".join(lines[-2000:]))
            sb = self._log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _clear_log(self):
        if self._log_view:
            self._log_view.clear()
        logs = load_pipeline_logs()
        logs[self.node_id] = []
        save_pipeline_logs(logs)

    def _on_run(self):
        win = self.window()
        if hasattr(win, 'run_pipeline_step'):
            win.run_pipeline_step(self.node_id)

    def _on_stop(self):
        win = self.window()
        if hasattr(win, 'stop_pipeline_step'):
            win.stop_pipeline_step(self.node_id)

    # ── Hover ──

    def enterEvent(self, event):
        self._set_header_bg(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_header_bg(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.node_id)
        super().mousePressEvent(event)


# ============================================================
# ARROW CONNECTOR
# ============================================================

class ArrowConnector(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(24)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(C.ARROW))
        pen.setWidth(1)
        p.setPen(pen)
        cx = self.width() // 2
        p.drawLine(cx, 0, cx, self.height() - 6)
        p.drawLine(cx - 4, self.height() - 10, cx, self.height() - 4)
        p.drawLine(cx + 4, self.height() - 10, cx, self.height() - 4)
        p.end()


# ============================================================
# PIPELINE TAB
# ============================================================

class PipelineTab(QWidget):
    navigate_to_tab = Signal(int)

    NODE_DEFS = [
        ("examples",          1, "Examples",           "Define setups and manage example libraries",              "navigate"),
        ("signal_grind",      2, "Signal Grind",       "Examples vs universe → candidate conditions",             "grinder"),
        ("exit_grind",        3, "Exit Grind",         "Brute-force optimal exit condition",                      "grinder"),
        ("refinement_grind",  4, "Refinement Grind",   "Classify winners/losers, eliminate losing clusters",      "grinder"),
        ("vetting",           5, "Vetting",            "Review winner signals, bank new examples",                "navigate"),
        ("ev_grind",          6, "EV Grinder",         "Score signals with predicted WR, MFE, EV",               "grinder"),
        ("scan_tuning",       7, "Scan Tuning",        "Quality score + WR threshold sliders",                   "placeholder"),
        ("profit_grind",      8, "Profit Grind",       "Optimize exit strategy for max account growth (SQN)",    "placeholder"),
        ("watchlist",         9, "Live Watchlist",     "Nightly ranked signal list across all setup types",       "navigate"),
    ]

    TAB_MAP = {"examples": 1, "vetting": 2, "watchlist": 3}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_type = "dtss"
        self._nodes = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        flow = QVBoxLayout(container)
        flow.setContentsMargins(80, 24, 80, 24)
        flow.setSpacing(0)
        flow.setAlignment(Qt.AlignTop)

        for i, (nid, num, name, desc, ntype) in enumerate(self.NODE_DEFS):
            node = PipelineNode(nid, num, name, desc, ntype)
            node.clicked.connect(self._on_node_clicked)
            self._nodes[nid] = node
            flow.addWidget(node)
            if i < len(self.NODE_DEFS) - 1:
                flow.addWidget(ArrowConnector(), alignment=Qt.AlignHCenter)

        flow.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll)

    def _on_node_clicked(self, node_id):
        node = self._nodes.get(node_id)
        if not node:
            return
        if node.node_type == "navigate":
            tab_idx = self.TAB_MAP.get(node_id)
            if tab_idx is not None:
                self.navigate_to_tab.emit(tab_idx)
        elif node.node_type == "grinder":
            for nid, n in self._nodes.items():
                if nid != node_id and n.node_type == "grinder":
                    n.collapse()
            node.toggle_expand()
            node.update_metrics(self._setup_type)

    def set_setup(self, setup_type):
        self._setup_type = setup_type
        self.refresh()

    def refresh(self):
        for nid in ("signal_grind", "exit_grind", "refinement_grind", "ev_grind"):
            node = self._nodes[nid]
            node.set_status(get_step_status(nid))
            if node._expanded:
                node.update_metrics(self._setup_type)

        setups = get_setups()
        info = setups.get(self._setup_type, {})
        n_ex = info.get("examples", 0)
        self._nodes["examples"].set_info(f"{n_ex} examples")
        self._nodes["examples"].set_status("done" if n_ex > 0 else "idle")

        nv, nw = get_vetting_counts(self._setup_type)
        if nw > 0:
            self._nodes["vetting"].set_info(f"{nv}/{nw} vetted")
            self._nodes["vetting"].set_status("done" if nv > 0 else "idle")
        else:
            self._nodes["vetting"].set_info("")
            self._nodes["vetting"].set_status("idle")

        for nid in ("scan_tuning", "profit_grind", "watchlist"):
            self._nodes[nid].set_status("not_built")
            self._nodes[nid].set_info("not yet built")

    def get_node(self, node_id):
        return self._nodes.get(node_id)


# ============================================================
# TOP BAR
# ============================================================

class TopBar(QWidget):
    tab_clicked = Signal(int)
    setup_changed = Signal(str)
    TAB_NAMES = ["Pipeline", "Examples", "Vetting", "Watchlist"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setups = {}
        self.setFixedHeight(52)
        self.setStyleSheet(f"background: {C.SURFACE}; border-bottom: 1px solid {C.BORDER};")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(0)

        logo = QLabel("SCANPERFECT")
        logo.setStyleSheet(
            f"font-size:13px; font-weight:700; letter-spacing:2px;"
            f" color:{C.TEXT}; padding-right:15px; border:none;"
        )
        lay.addWidget(logo)

        sep = QLabel("│")
        sep.setStyleSheet(f"color:{C.BORDER}; font-size:14px; padding:0 8px; border:none;")
        lay.addWidget(sep)

        self._tabs = []
        for i, name in enumerate(self.TAB_NAMES):
            btn = QPushButton(name.upper())
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(self._tab_css(i == 0))
            btn.clicked.connect(lambda _, idx=i: self._on_tab(idx))
            self._tabs.append(btn)
            lay.addWidget(btn)

        lay.addStretch()

        self.setup_combo = QComboBox()
        self.setup_combo.currentIndexChanged.connect(self._on_setup_idx)
        lay.addWidget(self.setup_combo)

    def _tab_css(self, active):
        if active:
            return (
                f"QPushButton {{ background:transparent; border:none;"
                f" border-bottom:2px solid {C.TEXT}; color:{C.TEXT};"
                f" font-size:11px; font-weight:600; letter-spacing:1px;"
                f" padding:0 18px; min-height:50px; }}"
            )
        return (
            f"QPushButton {{ background:transparent; border:none;"
            f" border-bottom:2px solid transparent; color:{C.TEXT_MUTED};"
            f" font-size:11px; font-weight:600; letter-spacing:1px;"
            f" padding:0 18px; min-height:50px; }}"
            f" QPushButton:hover {{ color:{C.TEXT_DIM}; }}"
        )

    def _on_tab(self, idx):
        for i, btn in enumerate(self._tabs):
            btn.setStyleSheet(self._tab_css(i == idx))
        self.tab_clicked.emit(idx)

    def set_active_tab(self, idx):
        self._on_tab(idx)

    def _on_setup_idx(self, idx):
        keys = list(self._setups.keys())
        if 0 <= idx < len(keys):
            self.setup_changed.emit(keys[idx])

    def populate_setups(self, setups_dict):
        self._setups = setups_dict
        self.setup_combo.blockSignals(True)
        self.setup_combo.clear()
        for st, info in setups_dict.items():
            self.setup_combo.addItem(f"{info['name']} ({info['examples']})")
        self.setup_combo.blockSignals(False)


# ============================================================
# PLACEHOLDER TAB
# ============================================================

class PlaceholderTab(QWidget):
    def __init__(self, title, subtitle, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        icon = QLabel("◈")
        icon.setStyleSheet(f"font-size:45px; color:{C.BORDER}; border:none;")
        icon.setAlignment(Qt.AlignCenter)
        lay.addWidget(icon)
        t = QLabel(title)
        t.setStyleSheet(f"font-size:14px; font-weight:600; color:{C.TEXT_MUTED}; border:none;")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        s = QLabel(subtitle)
        s.setStyleSheet(f"font-size:13px; color:{C.BORDER}; border:none;")
        s.setAlignment(Qt.AlignCenter)
        lay.addWidget(s)


# ============================================================
# MAIN WINDOW
# ============================================================

class ScanPerfectWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ScanPerfect")
        self.setMinimumSize(1200, 700)
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

        self._top = TopBar()
        self._top.tab_clicked.connect(self._switch_tab)
        self._top.setup_changed.connect(self._on_setup)
        ml.addWidget(self._top)

        self._stack = QStackedWidget()
        ml.addWidget(self._stack)

        self._pipeline_tab = PipelineTab()
        self._pipeline_tab.navigate_to_tab.connect(self._switch_tab_from_pipeline)
        self._stack.addWidget(self._pipeline_tab)

        self._stack.addWidget(PlaceholderTab("Examples", "Coming in Increment 2"))
        self._stack.addWidget(PlaceholderTab("Vetting", "Coming in Increment 3"))
        self._stack.addWidget(PlaceholderTab("Nightly Watchlist", "Not yet built"))

        self._load_setups()
        self._pipeline_tab.set_setup(self._setup_type)
        self._pipeline_tab.refresh()

        self._timer = QTimer()
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(5000)

    def _load_setups(self):
        setups = get_setups()
        self._top.populate_setups(setups)
        if setups:
            self._setup_type = list(setups.keys())[0]

    def _switch_tab(self, idx):
        self._stack.setCurrentIndex(idx)

    def _switch_tab_from_pipeline(self, idx):
        self._top.set_active_tab(idx)
        self._stack.setCurrentIndex(idx)

    def _on_setup(self, setup_type):
        self._setup_type = setup_type
        self._pipeline_tab.set_setup(setup_type)

    def _on_tick(self):
        if self._stack.currentIndex() == 0:
            self._pipeline_tab.refresh()

    # ── Subprocess management ──

    def run_pipeline_step(self, step_id):
        if self._process is not None:
            return
        cmd_tpl = STEP_COMMANDS.get(step_id)
        if not cmd_tpl:
            return

        cmd = [arg.replace("{setup}", self._setup_type) for arg in cmd_tpl]
        self._process_step = step_id
        self._process_lines = []
        self._process_start = datetime.now()

        # Update state file
        state = load_pipeline_state()
        state.setdefault("steps", {})[step_id] = {
            "status": "running",
            "started_at": datetime.now().isoformat(),
            "finished_at": None, "duration_s": None,
            "exit_code": None, "error": None, "result_summary": None,
        }
        save_pipeline_state(state)

        # Clear logs
        logs = load_pipeline_logs()
        logs[step_id] = []
        save_pipeline_logs(logs)

        # Update UI
        node = self._pipeline_tab.get_node(step_id)
        if node:
            node.set_status("running")
            node.update_metrics(self._setup_type)
            if node._log_view:
                node._log_view.clear()

        # Launch QProcess
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

    def _on_stdout(self):
        if not self._process:
            return
        raw = self._process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        for line in raw.split("\n"):
            s = line.rstrip()
            if s:
                self._process_lines.append(s)
                node = self._pipeline_tab.get_node(self._process_step)
                if node:
                    node.append_log(s)
        if len(self._process_lines) % 20 == 0:
            logs = load_pipeline_logs()
            logs[self._process_step] = self._process_lines[-4000:]
            save_pipeline_logs(logs)

    def _on_finished(self, exit_code, _exit_status):
        sid = self._process_step
        dur = (datetime.now() - self._process_start).total_seconds()

        logs = load_pipeline_logs()
        logs[sid] = self._process_lines[-4000:]
        save_pipeline_logs(logs)

        summary = self._extract_summary(self._process_lines)
        status = "done" if exit_code == 0 else "error"

        state = load_pipeline_state()
        prev = state.get("steps", {}).get(sid, {})
        state.setdefault("steps", {})[sid] = {
            "status": status,
            "started_at": prev.get("started_at"),
            "finished_at": datetime.now().isoformat(),
            "duration_s": round(dur, 1),
            "exit_code": exit_code,
            "error": "\n".join(self._process_lines[-20:]) if exit_code != 0 else None,
            "result_summary": summary,
        }
        save_pipeline_state(state)

        node = self._pipeline_tab.get_node(sid)
        if node:
            node.set_status(status)
            node.update_metrics(self._setup_type)
            if exit_code == 0:
                node.append_log(f"\n✓ Complete ({round(dur, 1)}s)")
            else:
                node.append_log(f"\n✗ Error (exit code {exit_code})")

        self._process = None
        self._process_step = None
        self._process_lines = []
        self._pipeline_tab.refresh()

    @staticmethod
    def _extract_summary(lines):
        out = []
        for line in lines[-80:]:
            lo = line.lower().strip()
            if any(kw in lo for kw in [
                "winner", "best:", "result:", "final", "complete",
                "signals", "peak", "conditions", "floor=", "median=",
                "all passes complete", "total signals", "✓",
            ]):
                out.append(line.strip())
        return "\n".join(out[-10:]) if out else None


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    app = QApplication(sys.argv)

    # Try loading DM Sans from a fonts/ directory if present
    fonts_dir = REPO_ROOT / "fonts"
    if fonts_dir.is_dir():
        for f in fonts_dir.iterdir():
            if f.suffix.lower() in (".ttf", ".otf"):
                QFontDatabase.addApplicationFont(str(f))

    app.setFont(QFont("DM Sans", 10))
    app.setStyleSheet(STYLESHEET)

    win = ScanPerfectWindow()
    win.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
