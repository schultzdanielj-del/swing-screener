"""ScanPerfect V2 — FastAPI backend. Deploy: 2026-03-06."""

import os
import json as _json
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

import io
import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="ScanPerfect V2")

DB_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "scanperfect.db"

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


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
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL,
                chart_date TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(setup_type, ticker, entry_date)
            );
            CREATE TABLE IF NOT EXISTS ohlcv (
                example_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                FOREIGN KEY (example_id) REFERENCES examples(id) ON DELETE CASCADE,
                UNIQUE(example_id, date)
            );
            CREATE INDEX IF NOT EXISTS idx_ohlcv_example ON ohlcv(example_id);
            CREATE INDEX IF NOT EXISTS idx_examples_setup ON examples(setup_type);
            CREATE TABLE IF NOT EXISTS rejected_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(setup_type, ticker, signal_date)
            );
            CREATE TABLE IF NOT EXISTS pending_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                ai_verdict TEXT,
                ai_reasoning TEXT,
                review_notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT,
                UNIQUE(setup_type, ticker, entry_date)
            );
            CREATE TABLE IF NOT EXISTS earnings_dates (
                ticker TEXT NOT NULL,
                earnings_date TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(ticker, earnings_date)
            );
            CREATE INDEX IF NOT EXISTS idx_earnings_ticker ON earnings_dates(ticker);
            CREATE TABLE IF NOT EXISTS universe_ohlcv (
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                UNIQUE(ticker, date)
            );
            CREATE INDEX IF NOT EXISTS idx_universe_ohlcv_ticker ON universe_ohlcv(ticker, date);
            CREATE TABLE IF NOT EXISTS tradable_universe (ticker TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS grind_cycles (
                cycle_id TEXT PRIMARY KEY,
                setup_type TEXT NOT NULL,
                status TEXT NOT NULL,
                error_msg TEXT,
                is_current INTEGER NOT NULL DEFAULT 0,
                n_examples_at_grind INTEGER,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                reverted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS cycle_conditions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL,
                tier TEXT NOT NULL,
                expression_name TEXT NOT NULL,
                low REAL, high REAL, filter_power REAL, sort_order INTEGER,
                FOREIGN KEY (cycle_id) REFERENCES grind_cycles(cycle_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cycle_conditions_cycle ON cycle_conditions(cycle_id);
            CREATE TABLE IF NOT EXISTS cycle_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL,
                setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                bar_idx INTEGER,
                close REAL, adr REAL,
                is_example INTEGER NOT NULL DEFAULT 0,
                classification TEXT,
                classification_source TEXT,
                exit_triggered INTEGER,
                exit_date TEXT,
                move_adr REAL, mfe_adr REAL, capture_eff REAL,
                regime_score REAL, vetted_at TEXT,
                FOREIGN KEY (cycle_id) REFERENCES grind_cycles(cycle_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cycle_signals_cycle ON cycle_signals(cycle_id);
            CREATE INDEX IF NOT EXISTS idx_cycle_signals_ticker_date ON cycle_signals(ticker, signal_date);
            CREATE TABLE IF NOT EXISTS cycle_sacrificial_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL,
                setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                bar_idx INTEGER,
                close REAL,
                FOREIGN KEY (cycle_id) REFERENCES grind_cycles(cycle_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cycle_sacrificial_cycle ON cycle_sacrificial_signals(cycle_id);
            CREATE TABLE IF NOT EXISTS exit_conditions (
                setup_type TEXT PRIMARY KEY,
                expression_name TEXT NOT NULL,
                direction TEXT NOT NULL,
                threshold REAL NOT NULL,
                max_forward_bars INTEGER NOT NULL,
                adr_threshold_multiplier REAL NOT NULL DEFAULT 1.0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cycle_health (
                cycle_id TEXT PRIMARY KEY,
                setup_type TEXT NOT NULL,
                n_signals INTEGER, peak_per_day REAL, avg_per_day REAL,
                signal_stability_pct REAL, examples_passing INTEGER,
                examples_added_this_cycle INTEGER, examples_since_last_grind INTEGER,
                win_rate_auto REAL, win_rate_vetted REAL, pct_manually_vetted REAL,
                median_winner_adr REAL, median_loser_adr REAL, ev_estimate REAL,
                prev_cycle_id TEXT, signal_count_delta INTEGER,
                condition_count_delta INTEGER, win_rate_delta REAL,
                promote_recommendation TEXT, flag_reason TEXT,
                live_ready INTEGER, live_ready_blockers TEXT, computed_at TEXT NOT NULL,
                FOREIGN KEY (cycle_id) REFERENCES grind_cycles(cycle_id)
            );
            CREATE TABLE IF NOT EXISTS nightly_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                setup_type TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                regime_score REAL, expected_win_rate REAL,
                rank INTEGER, expected_move_adr REAL,
                ai_vet_status TEXT, ai_vet_reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_watchlist_run_date ON nightly_watchlist(run_date);
            CREATE TABLE IF NOT EXISTS regime_model (
                setup_type TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL,
                n_signals_used INTEGER,
                n_features_tested INTEGER,
                feature_weights TEXT,
                top_features TEXT,
                win_rate_by_decile TEXT,
                baseline_win_rate REAL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (cycle_id) REFERENCES grind_cycles(cycle_id)
            );
            CREATE TABLE IF NOT EXISTS signal_regime_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_signal_id INTEGER NOT NULL,
                cycle_id TEXT NOT NULL,
                regime_score REAL,
                expected_win_rate REAL,
                FOREIGN KEY (cycle_id) REFERENCES grind_cycles(cycle_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_srs_signal ON signal_regime_scores(cycle_signal_id);
            CREATE INDEX IF NOT EXISTS idx_srs_cycle ON signal_regime_scores(cycle_id);
            CREATE TABLE IF NOT EXISTS file_mirror (
                path TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                size_bytes INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,
                args TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                claimed_at TEXT,
                completed_at TEXT,
                exit_code INTEGER,
                error TEXT,
                log_tail TEXT,
                result_path TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_queue(status);
            CREATE TABLE IF NOT EXISTS research_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                branch TEXT,
                max_time_s INTEGER DEFAULT 14400,
                max_phases INTEGER DEFAULT 8,
                created_at TEXT DEFAULT (datetime('now')),
                claimed_at TEXT,
                completed_at TEXT,
                summary TEXT,
                diff TEXT,
                signal_before INTEGER,
                signal_after INTEGER,
                examples_benched TEXT,
                error TEXT,
                log TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_research_jobs_status ON research_jobs(status);
            CREATE TABLE IF NOT EXISTS setups (
                setup_type TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                direction TEXT NOT NULL DEFAULT 'short',
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        # ── Add new grind_cycles columns (safe — no-op if already exist) ──
        for col, coltype in [
            ("step_type", "TEXT"),
            ("grind_params", "TEXT"),
            ("source_hash", "TEXT"),
        ]:
            try:
                db.execute(f"ALTER TABLE grind_cycles ADD COLUMN {col} {coltype}")
            except Exception:
                pass  # Column already exists
        # ── Add research_jobs config columns ──
        for col, coltype in [
            ("max_time_s", "INTEGER DEFAULT 14400"),
            ("max_phases", "INTEGER DEFAULT 8"),
        ]:
            try:
                db.execute(f"ALTER TABLE research_jobs ADD COLUMN {col} {coltype}")
            except Exception:
                pass
        # ── Seed default setups ──
        for st, name, desc, direction in [
            ("dtss", "DTSS", "Double Top Short Sell", "short"),
            ("3-4db", "3-4DB", "3-4 Day Bounce (Short)", "short"),
            ("htf", "HTF", "High Tight Flag (Long)", "long"),
        ]:
            db.execute(
                "INSERT OR IGNORE INTO setups (setup_type, name, description, direction) VALUES (?,?,?,?)",
                (st, name, desc, direction),
            )


init_db()


# ============================================================
# HELPERS
# ============================================================

def clean_val(v):
    if v is None: return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)): return None
    if hasattr(v, 'item'): v = v.item()
    return v


def normalize_ticker(ticker):
    SHARE_CLASS = {
        "BRKA":"BRK-A","BRKB":"BRK-B","BRK.A":"BRK-A","BRK.B":"BRK-B",
        "BF.A":"BF-A","BF.B":"BF-B","BFA":"BF-A","BFB":"BF-B",
        "MOG.A":"MOG-A","MOG.B":"MOG-B","MOGA":"MOG-A","MOGB":"MOG-B",
        "GEF.B":"GEF-B","GEFB":"GEF-B","LGF.A":"LGF-A","LGF.B":"LGF-B",
        "LGFA":"LGF-A","LGFB":"LGF-B",
    }
    up = ticker.upper().strip()
    if up in SHARE_CLASS: return SHARE_CLASS[up]
    if "." in up: return up.replace(".", "-")
    return up


def fetch_ohlcv_yf(ticker, chart_date_str):
    ticker = normalize_ticker(ticker)
    chart_dt = datetime.strptime(chart_date_str, "%Y-%m-%d")
    start = chart_dt - timedelta(days=250)
    end   = chart_dt + timedelta(days=60)
    raw = yf.download(ticker, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"), progress=False)
    if raw.empty: return None
    if isinstance(raw.columns, pd.MultiIndex): raw.columns = raw.columns.get_level_values(0)
    return raw.reset_index().sort_values("Date").reset_index(drop=True)


def add_indicators(df):
    df = df.copy()
    df["EMA8"] = df["Close"].ewm(span=8, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()
    df["SMA200"] = df["Close"].rolling(200).mean()
    tr = pd.concat([df["High"]-df["Low"], (df["High"]-df["Close"].shift(1)).abs(), (df["Low"]-df["Close"].shift(1)).abs()], axis=1).max(axis=1)
    df["ATR14"] = tr.rolling(14).mean()
    df["VolAvg20"] = df["Volume"].rolling(20).mean()
    return df


def generate_chart_png(df, ticker, entry_date, at_entry=False, setup_type=None):
    """Generate a chart PNG for AI review. Returns bytes or None."""
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    df = add_indicators(df)
    cfg = {"dtss": {"at_entry_before": 100, "default_before": 100, "default_after": 30, "min_total": 130}}.get(setup_type, {})
    at_entry_before = cfg.get("at_entry_before", 50)
    default_before = cfg.get("default_before", 30)
    default_after = cfg.get("default_after", 30)
    min_total = cfg.get("min_total", 60)
    entry_dt = pd.Timestamp(entry_date)
    entry_rows = df[df["Date"] == entry_dt]
    if entry_rows.empty:
        before = df[df["Date"] <= entry_dt]
        if before.empty: return None
        entry_idx = before.index[-1]
    else:
        entry_idx = entry_rows.index[0]
    if at_entry:
        want_before = min(at_entry_before, entry_idx)
        start_idx = entry_idx - want_before
        chart_df = df.iloc[start_idx:entry_idx + 1].copy().reset_index(drop=True)
        entry_pos = want_before
        n = len(chart_df)
        total_width = n + max(int(n * 0.18), 5)
    else:
        avail_after = len(df) - entry_idx - 1
        avail_before = entry_idx
        want_after = min(default_after, avail_after)
        want_before2 = min(default_before, avail_before)
        total = want_before2 + 1 + want_after
        if total < min_total:
            extra = min_total - total
            if want_before2 < default_before: want_after = min(want_after + extra, avail_after)
            else: want_before2 = min(want_before2 + extra, avail_before)
        chart_df = df.iloc[entry_idx - want_before2:entry_idx + want_after + 1].copy().reset_index(drop=True)
        entry_pos = want_before2
        total_width = len(chart_df)
    if chart_df.empty: return None
    fig, (ax, ax_vol) = plt.subplots(2, 1, figsize=(8, 4), dpi=120, gridspec_kw={"height_ratios": [3, 1]}, facecolor="#0a0e17")
    ax.set_facecolor("#0a0e17"); ax_vol.set_facecolor("#0a0e17")
    n = len(chart_df); w = 0.6
    for i, row in chart_df.iterrows():
        o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
        color = "#26A69A" if c >= o else "#EF5350"
        ax.plot([i, i], [l, h], color=color, linewidth=0.8)
        ax.add_patch(Rectangle((i - w/2, min(o, c)), w, max(abs(c - o), 0.001), facecolor=color, edgecolor=color, linewidth=0.5))
        ax_vol.bar(i, row["Volume"], width=w, color=color, alpha=0.7)
    for period, ma_type, color, lw in [(8, "ema", "#ADD8E6", 1.0), (21, "ema", "#D2B48C", 1.0), (50, "sma", "#FFD700", 1.2), (200, "sma", "#FF0000", 1.5)]:
        if n >= period:
            s = chart_df["Close"].ewm(span=period, adjust=False).mean() if ma_type == "ema" else chart_df["Close"].rolling(window=period).mean()
            ax.plot(range(n), s.values, color=color, linewidth=lw, alpha=0.8)
    entry_open = float(chart_df.iloc[entry_pos]["Open"])
    ax.axvline(x=entry_pos, color="#3b82f6", linewidth=1, alpha=0.6, linestyle="--")
    ax.axhline(y=entry_open, color="#3b82f6", linewidth=1, alpha=0.6, linestyle="--")
    ax_vol.axvline(x=entry_pos, color="#3b82f6", linewidth=1, alpha=0.6, linestyle="--")
    ax.set_title(f"{ticker}  •  {entry_date}", color="#e2e8f0", fontsize=11, fontweight="bold", pad=8)
    ax.tick_params(colors="#64748b", labelsize=8); ax_vol.tick_params(colors="#64748b", labelsize=7)
    for spine in ax.spines.values(): spine.set_color("#2a3550")
    for spine in ax_vol.spines.values(): spine.set_color("#2a3550")
    ax.set_xlim(-1, total_width); ax_vol.set_xlim(-1, total_width)
    ax.set_xticks([]); ax_vol.set_xticks([]); ax_vol.yaxis.set_visible(False)
    ax.grid(True, alpha=0.1, color="#64748b")
    fig.tight_layout(pad=0.5)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="#0a0e17", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def store_ohlcv(db, example_id, df):
    rows = [(example_id, r["Date"].strftime("%Y-%m-%d"), clean_val(r["Open"]), clean_val(r["High"]),
             clean_val(r["Low"]), clean_val(r["Close"]), clean_val(r["Volume"])) for _, r in df.iterrows()]
    db.executemany("INSERT OR REPLACE INTO ohlcv (example_id, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)", rows)


def _load_json(path, default=None):
    if default is None: default = {}
    try:
        p = Path(path)
        if p.exists():
            with open(p) as f: return _json.load(f)
    except: pass
    return default


def _save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f: _json.dump(data, f, indent=2, default=str)


# ============================================================
# PIPELINE / AGENT
# ============================================================

PIPELINE_FILE      = DATA_DIR / "pipeline_state.json"
PIPELINE_LOGS_FILE = DATA_DIR / "pipeline_logs.json"
GRINDER_AGENT_FILE = DATA_DIR / "grinder_agent.json"
VETTING_DATA_DIR   = DATA_DIR

PIPELINE_STEPS = [
    {"id":"signal_grind",     "name":"1. Signal Grind",      "category":"pipeline", "prerequisites":[], "description":"Pyramid grinder — examples vs universe → candidate conditions."},
    {"id":"exit_grind",       "name":"2. Exit Grind",        "category":"pipeline", "prerequisites":[], "description":"Brute-force optimal exit condition from example entry bar highs."},
    {"id":"refinement_grind", "name":"3. Refinement Grind",  "category":"pipeline", "prerequisites":[], "description":"Scan universe, classify winners/losers via ceiling+exit race, beam-search to eliminate losers."},
    {"id":"ev_grind",         "name":"4. EV Grinder",        "category":"pipeline", "prerequisites":[], "description":"Score every signal with predicted WR, MFE, EV. Unified correlative scoring across all features."},
]


def _load_pipeline_state():
    return _load_json(PIPELINE_FILE, {"steps":{}, "jobs":[]})

def _save_pipeline_state(s): _save_json(PIPELINE_FILE, s)
def _load_pipeline_logs():   return _load_json(PIPELINE_LOGS_FILE, {})
def _save_pipeline_logs(l):  _save_json(PIPELINE_LOGS_FILE, l)


@app.get("/api/pipeline/steps")
async def get_pipeline_steps():
    state = _load_pipeline_state()
    valid_ids = {s["id"] for s in PIPELINE_STEPS}
    if state.get("jobs"):
        state["jobs"] = [j for j in state["jobs"] if j.get("step_id") in valid_ids]
        _save_pipeline_state(state)
    agent = _load_json(GRINDER_AGENT_FILE, {})
    agent_status = "unknown"
    last_hb = agent.get("last_heartbeat","")
    if last_hb:
        try:
            hb_time = datetime.fromisoformat(last_hb.replace('+00:00','').replace('Z',''))
            agent_status = "online" if (datetime.utcnow()-hb_time).total_seconds()<20 else "offline"
        except: agent_status = "unknown"
    steps_out = []
    for step_def in PIPELINE_STEPS:
        step_state = state.get("steps",{}).get(step_def["id"], {
            "status":"pending","started_at":None,"finished_at":None,
            "duration_s":None,"exit_code":None,"error":None,"result_summary":None,
        })
        can_run = not any(j.get("status") in ("queued","running","claimed") for j in state.get("jobs",[]))
        if can_run:
            for prereq in step_def["prerequisites"]:
                if state.get("steps",{}).get(prereq,{}).get("status") != "done":
                    can_run = False; break
        if step_def["id"] in ("optimal_samples","sample_expansion"):
            try:
                decisions = _load_json(VETTING_DATA_DIR/"vetting"/"vetting_dtss.json", {})
                n_total = 0
                fp = VETTING_DATA_DIR/"signal_filter"/"filtered_dtss.json"
                if fp.exists(): n_total = len(_load_json(fp,{}).get("signals",[]))
                counts = {"yes":0,"maybe":0,"no":0}
                for v in decisions.values():
                    vd = v.get("verdict","")
                    if vd in counts: counts[vd]+=1
                n_vetted = sum(counts.values())
                with get_db() as db:
                    n_examples = db.execute("SELECT COUNT(*) FROM examples WHERE setup_type='dtss'").fetchone()[0]
                    n_rejected = db.execute("SELECT COUNT(*) FROM rejected_signals WHERE setup_type='dtss'").fetchone()[0]
                    n_pending  = db.execute("SELECT COUNT(*) FROM pending_examples WHERE setup_type='dtss'").fetchone()[0]
                step_state["vetting_stats"] = {"n_total":n_total,"n_vetted":n_vetted,
                    "n_yes":counts["yes"],"n_maybe":counts["maybe"],"n_no":counts["no"],
                    "n_examples":n_examples,"n_rejected":n_rejected,"n_pending":n_pending}
                if step_def["id"]=="sample_expansion" and n_vetted>0:
                    if n_vetted>=n_total: step_state["status"]="done"
                    step_state["result_summary"]=f"{n_vetted}/{n_total} vetted · {counts['yes']} yes · {counts['no']} no · {n_examples} total optimal samples"
            except: pass
        steps_out.append({**step_def,"state":step_state,"can_run":can_run})
    running = next((j.get("step_id") for j in state.get("jobs",[]) if j.get("status") in ("queued","running","claimed")),None)
    return {"steps":steps_out,"running":running,"agent_status":agent_status,"agent_last_heartbeat":last_hb}


@app.post("/api/pipeline/run/{step_id}")
async def pipeline_run_step(step_id: str, request: Request = None):
    step_params = {}
    if request:
        try:
            body = await request.json()
            if isinstance(body, dict): step_params = body.get("params",{})
        except: pass
    step_def = next((s for s in PIPELINE_STEPS if s["id"]==step_id), None)
    if not step_def: return {"error":f"Unknown step: {step_id}"}
    state = _load_pipeline_state()
    agent = _load_json(GRINDER_AGENT_FILE,{})
    last_hb = agent.get("last_heartbeat","")
    agent_alive = False
    if last_hb:
        try:
            hb_time = datetime.fromisoformat(last_hb.replace('+00:00','').replace('Z',''))
            agent_alive = (datetime.utcnow()-hb_time).total_seconds()<30
        except: pass
    if not agent_alive:
        state["jobs"] = []
    else:
        active_other = [j for j in state.get("jobs",[]) if j.get("status") in ("queued","running","claimed") and j.get("step_id")!=step_id]
        if active_other: return {"error":f"Already running: {active_other[0].get('step_id')}"}
        state["jobs"] = [j for j in state.get("jobs",[]) if j.get("step_id")!=step_id]
    for prereq in step_def["prerequisites"]:
        if state.get("steps",{}).get(prereq,{}).get("status")!="done":
            return {"error":f"Prerequisite not met: {prereq}"}
    job = {"job_id":f"pipe_{step_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
           "step_id":step_id,"status":"queued","params":step_params,"created_at":datetime.now().isoformat()}
    state.setdefault("jobs",[]).append(job)
    state.setdefault("steps",{})[step_id] = {"status":"queued","started_at":None,"finished_at":None,
        "duration_s":None,"exit_code":None,"error":None,"result_summary":None}
    _save_pipeline_state(state)
    logs = _load_pipeline_logs(); logs[step_id]=[]; _save_pipeline_logs(logs)
    return {"status":"queued","job_id":job["job_id"],"step_id":step_id}


@app.get("/api/pipeline/jobs/pending")
async def pipeline_pending_jobs():
    state = _load_pipeline_state(); pending = []
    for j in state.get("jobs",[]):
        if j.get("status")=="queued": j["status"]="claimed"; pending.append(j)
    if pending: _save_pipeline_state(state)
    return {"jobs":pending}


@app.post("/api/pipeline/status")
async def pipeline_update_status(request: Request):
    body = await request.json(); step_id=body.get("step_id"); status=body.get("status")
    state = _load_pipeline_state()
    ss = state.setdefault("steps",{}).setdefault(step_id,{})
    ss["status"]=status
    if status=="running": ss["started_at"]=body.get("timestamp",datetime.now().isoformat())
    elif status in ("done","error","stopped"):
        ss["finished_at"]=body.get("timestamp",datetime.now().isoformat())
        ss["duration_s"]=body.get("duration_s"); ss["exit_code"]=body.get("exit_code")
        ss["error"]=body.get("error"); ss["result_summary"]=body.get("result_summary")
        for j in state.get("jobs",[]):
            if j.get("step_id")==step_id and j.get("status") in ("claimed","running"): j["status"]=status
    _save_pipeline_state(state); return {"ok":True}


@app.post("/api/pipeline/logs")
async def pipeline_append_logs(request: Request):
    body=await request.json(); step_id=body.get("step_id"); lines=body.get("lines",[])
    logs=_load_pipeline_logs(); existing=logs.get(step_id,[]); existing.extend(lines)
    if len(existing)>5000: existing=existing[-4000:]
    logs[step_id]=existing; _save_pipeline_logs(logs)
    return {"ok":True,"total_lines":len(existing)}


@app.get("/api/pipeline/logs/{step_id}")
async def pipeline_get_logs(step_id: str, after: int=0):
    logs=_load_pipeline_logs(); all_lines=logs.get(step_id,[])
    return {"step_id":step_id,"lines":all_lines[after:],"total":len(all_lines),"after":after}


@app.post("/api/pipeline/reset/{step_id}")
async def pipeline_reset_step(step_id: str):
    state=_load_pipeline_state()
    state.setdefault("steps",{})[step_id]={"status":"pending","started_at":None,"finished_at":None,
        "duration_s":None,"exit_code":None,"error":None,"result_summary":None}
    state["jobs"]=[j for j in state.get("jobs",[]) if j.get("step_id")!=step_id]
    _save_pipeline_state(state); return {"ok":True,"step_id":step_id}


@app.post("/api/pipeline/stop")
async def pipeline_stop():
    state=_load_pipeline_state()
    for j in state.get("jobs",[]):
        if j.get("status") in ("queued","claimed","running"): j["status"]="stop_requested"
    _save_pipeline_state(state); return {"ok":True}


@app.get("/api/pipeline/stop-check/{step_id}")
async def pipeline_stop_check(step_id: str):
    state=_load_pipeline_state()
    for j in state.get("jobs",[]):
        if j.get("step_id")==step_id and j.get("status")=="stop_requested": return {"stop":True}
    return {"stop":False}


@app.post("/api/grinder/agent/register")
async def register_agent(request: Request):
    _save_json(GRINDER_AGENT_FILE, await request.json()); return {"ok":True}


@app.post("/api/grinder/agent/heartbeat")
async def agent_heartbeat(request: Request):
    body=await request.json(); agent=_load_json(GRINDER_AGENT_FILE,{})
    agent["last_heartbeat"]=body.get("timestamp",datetime.now().isoformat()); agent["status"]="online"
    _save_json(GRINDER_AGENT_FILE,agent); return {"ok":True}


@app.get("/api/grinder/agent/status")
async def get_agent_status():
    agent=_load_json(GRINDER_AGENT_FILE,{})
    if not agent: return {"status":"unknown","agent":None}
    last_hb=agent.get("last_heartbeat","")
    if last_hb:
        try:
            hb=datetime.fromisoformat(last_hb.replace('+00:00','').replace('Z',''))
            if (datetime.utcnow()-hb).total_seconds()>20: agent["status"]="offline"
        except: pass
    return {"status":agent.get("status","unknown"),"agent":agent}


# ============================================================
# EXAMPLES
# ============================================================

@app.get("/api/setups")
async def get_setups():
    with get_db() as db:
        rows = db.execute("SELECT setup_type, name, description, direction FROM setups ORDER BY created_at").fetchall()
        result = {}
        for r in rows:
            n = db.execute("SELECT COUNT(*) FROM examples WHERE setup_type=?", (r["setup_type"],)).fetchone()[0]
            result[r["setup_type"]] = {
                "name": r["name"], "desc": r["description"],
                "direction": r["direction"], "examples": n,
            }
    return result


class CreateSetupRequest(BaseModel):
    name: str
    description: str = ""
    direction: str = "short"


@app.post("/api/setups")
async def create_setup(req: CreateSetupRequest):
    import re
    setup_type = re.sub(r"[^a-z0-9]+", "-", req.name.lower()).strip("-")
    if not setup_type:
        raise HTTPException(400, "Invalid setup name")
    if req.direction not in ("long", "short"):
        raise HTTPException(400, "direction must be 'long' or 'short'")
    with get_db() as db:
        if db.execute("SELECT setup_type FROM setups WHERE setup_type=?", (setup_type,)).fetchone():
            raise HTTPException(409, f"Setup '{setup_type}' already exists")
        db.execute(
            "INSERT INTO setups (setup_type, name, description, direction) VALUES (?,?,?,?)",
            (setup_type, req.name.strip(), req.description.strip(), req.direction),
        )
    return {"setup_type": setup_type, "name": req.name.strip(), "direction": req.direction}


@app.patch("/api/setups/{setup_type}")
async def patch_setup(setup_type: str, request: Request):
    body = await request.json()
    ALLOWED = {"name", "description", "direction"}
    updates = {k: v for k, v in body.items() if k in ALLOWED}
    if not updates:
        raise HTTPException(400, "No valid fields to update")
    if "direction" in updates and updates["direction"] not in ("long", "short"):
        raise HTTPException(400, "direction must be 'long' or 'short'")
    with get_db() as db:
        if not db.execute("SELECT setup_type FROM setups WHERE setup_type=?", (setup_type,)).fetchone():
            raise HTTPException(404, f"Setup '{setup_type}' not found")
        set_clause = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE setups SET {set_clause} WHERE setup_type=?", list(updates.values()) + [setup_type])
    return {"setup_type": setup_type, "updated": list(updates.keys())}


@app.get("/api/examples/{setup_type}")
async def get_examples(setup_type: str):
    with get_db() as db:
        rows=db.execute("SELECT id,ticker,chart_date,entry_date FROM examples WHERE setup_type=? ORDER BY ticker",(setup_type,)).fetchall()
        examples=[]
        for r in rows:
            ex={"id":r["id"],"ticker":r["ticker"],"chartDate":r["chart_date"],"entryDate":r["entry_date"]}
            try:
                pre=db.execute("SELECT high,low FROM universe_ohlcv WHERE ticker=? AND date<? ORDER BY date DESC LIMIT 14",(r["ticker"],r["entry_date"])).fetchall()
                if pre and len(pre)>=5:
                    adr=sum(abs(p["high"]-p["low"]) for p in pre)/len(pre)
                    if adr>0:
                        fwd=db.execute("SELECT high,close FROM universe_ohlcv WHERE ticker=? AND date>=? ORDER BY date LIMIT 120",(r["ticker"],r["entry_date"])).fetchall()
                        if fwd and len(fwd)>=2:
                            entry_high=fwd[0]["high"]
                            best_close=min(b["close"] for b in fwd[1:])
                            ex["adrMove"]=round((entry_high-best_close)/adr,1)
            except: pass
            examples.append(ex)
    return {"setupType":setup_type,"examples":examples}


class SaveExampleRequest(BaseModel):
    ticker: str; chart_date: str; entry_date: str


@app.post("/api/examples/{setup_type}")
async def save_example(setup_type: str, req: SaveExampleRequest):
    ticker=req.ticker.upper().strip()
    with get_db() as db:
        if db.execute("SELECT id FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?",(setup_type,ticker,req.entry_date)).fetchone():
            raise HTTPException(409,f"{ticker} {req.entry_date} already exists")
        eid=db.execute("INSERT INTO examples (setup_type,ticker,chart_date,entry_date) VALUES (?,?,?,?)",(setup_type,ticker,req.chart_date,req.entry_date)).lastrowid
    return {"id":eid,"ticker":ticker,"entry_date":req.entry_date}


@app.delete("/api/examples/{setup_type}/{example_id}")
async def delete_example(setup_type: str, example_id: int):
    with get_db() as db:
        db.execute("DELETE FROM examples WHERE id=? AND setup_type=?",(example_id,setup_type))
    return {"deleted":example_id}


@app.post("/api/examples/{setup_type}/bulk")
async def bulk_add_examples(setup_type: str, request: Request):
    """Parse a text blob of 'TICKER MM/DD/YYYY' lines and add them as examples."""
    body = await request.json()
    raw = body.get("text", "")
    if not raw.strip():
        raise HTTPException(400, "No text provided")

    from dateutil import parser as dateparser
    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
    added = []
    failed = []

    with get_db() as db:
        # Build set of valid tickers and trading dates for fast lookup
        valid_tickers = set()
        rows = db.execute("SELECT DISTINCT ticker FROM universe_ohlcv").fetchall()
        for r in rows:
            valid_tickers.add(r[0].upper())

        for line in lines:
            parts = line.split()
            if len(parts) < 2:
                failed.append({"line": line, "reason": "Could not parse — need TICKER DATE"})
                continue

            ticker = normalize_ticker(parts[0])
            date_str = " ".join(parts[1:])

            # Parse flexible date formats
            try:
                dt = dateparser.parse(date_str)
                entry_date = dt.strftime("%Y-%m-%d")
            except Exception:
                failed.append({"line": line, "reason": f"Could not parse date: {date_str}"})
                continue

            # Validate ticker exists in universe
            if ticker not in valid_tickers:
                failed.append({"line": line, "reason": f"Ticker {ticker} not in universe"})
                continue

            # Validate date is a trading day (exists in OHLCV)
            has_bar = db.execute(
                "SELECT 1 FROM universe_ohlcv WHERE ticker=? AND date=?", (ticker, entry_date)
            ).fetchone()
            if not has_bar:
                failed.append({"line": line, "reason": f"No trading data for {ticker} on {entry_date}"})
                continue

            # Check duplicate
            if db.execute(
                "SELECT id FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?",
                (setup_type, ticker, entry_date),
            ).fetchone():
                failed.append({"line": line, "reason": f"Duplicate: {ticker} {entry_date}"})
                continue

            # Insert
            db.execute(
                "INSERT INTO examples (setup_type, ticker, chart_date, entry_date) VALUES (?,?,?,?)",
                (setup_type, ticker, entry_date, entry_date),
            )
            added.append({"ticker": ticker, "entry_date": entry_date})

    return {"added": len(added), "failed": len(failed), "details_added": added, "details_failed": failed}
async def get_chart_by_ticker(setup_type: str, ticker: str, entry_date: str):
    """Generate chart PNG for any ticker+date (used by AI review)."""
    # Try universe_ohlcv first, fall back to yfinance
    with get_db() as db:
        rows = db.execute("SELECT date as Date, open as Open, high as High, low as Low, close as Close, volume as Volume FROM universe_ohlcv WHERE ticker=? ORDER BY date", (ticker.upper(),)).fetchall()
    if rows:
        df = pd.DataFrame([dict(r) for r in rows])
        df["Date"] = pd.to_datetime(df["Date"])
    else:
        df = fetch_ohlcv_yf(ticker, entry_date)
    if df is None or df.empty:
        raise HTTPException(404, f"No OHLCV data for {ticker}")
    png = generate_chart_png(df, ticker, entry_date, at_entry=False, setup_type=setup_type)
    if png is None:
        raise HTTPException(500, "Chart generation failed")
    return Response(content=png, media_type="image/png")


# ============================================================
# VETTING
# ============================================================

@app.post("/api/vetting/{setup_type}/upload-signals")
async def upload_vetting_signals(setup_type: str, request: Request):
    body=await request.json()
    out=VETTING_DATA_DIR/"signal_filter"; out.mkdir(parents=True,exist_ok=True)
    path=out/f"filtered_{setup_type}.json"
    with open(path,"w") as f: _json.dump(body,f,indent=2,default=str)
    return {"status":"ok","path":str(path),"n_signals":len(body.get("signals",[]))}


@app.post("/api/setup-grinder/{setup_type}/upload-signals")
async def upload_setup_grinder_signals(setup_type: str, request: Request):
    body=await request.json()
    out=VETTING_DATA_DIR/"setup_refiner"; out.mkdir(parents=True,exist_ok=True)
    path=out/f"refined_{setup_type}.json"
    with open(path,"w") as f: _json.dump(body,f,indent=2,default=str)
    return {"status":"ok","path":str(path),"n_signals":len(body.get("signals",[]))}


def _get_example_dates(setup_type):
    with get_db() as db:
        rows=db.execute("SELECT ticker,entry_date FROM examples WHERE setup_type=?",(setup_type,)).fetchall()
    ed={}
    for r in rows:
        ed.setdefault(r["ticker"],[])
        try: ed[r["ticker"]].append(datetime.strptime(r["entry_date"],"%Y-%m-%d"))
        except: pass
    return ed

def _is_dup(sig, example_dates):
    t=sig.get("ticker","")
    if t not in example_dates: return False
    try: sig_dt=datetime.strptime(sig["date"],"%Y-%m-%d")
    except: return False
    return any(abs((sig_dt-ex_dt).days)<=5 for ex_dt in example_dates[t])


@app.get("/api/vetting/{setup_type}/signals")
async def get_vetting_signals(setup_type: str):
    path=VETTING_DATA_DIR/"signal_filter"/f"filtered_{setup_type}.json"
    if not path.exists(): raise HTTPException(404,f"No filtered signals for {setup_type}. Run signal_filter.py first.")
    data=_load_json(path,{})
    decisions=_load_json(VETTING_DATA_DIR/"vetting"/f"vetting_{setup_type}.json",{})
    signals=[s for s in data.get("signals",[]) if not _is_dup(s,_get_example_dates(setup_type))]
    for sig in signals:
        key=f"{sig['ticker']}_{sig['date']}"
        sig["verdict"]=decisions.get(key,{}).get("verdict")
        sig["entry_date"]=decisions.get(key,{}).get("entry_date")
    return {"setup_type":setup_type,"n_signals":len(signals),
            "exit_condition":data.get("exit_condition",""),
            "min_adr_threshold":data.get("min_adr_threshold",0),"signals":signals}


@app.get("/api/setup-grinder/{setup_type}/signals")
async def get_setup_grinder_signals(setup_type: str):
    path=VETTING_DATA_DIR/"setup_refiner"/f"refined_{setup_type}.json"
    if not path.exists(): raise HTTPException(404,f"No refined signals for {setup_type}. Run setup_refiner.py first.")
    data=_load_json(path,{})
    decisions=_load_json(VETTING_DATA_DIR/"vetting"/f"vetting_{setup_type}.json",{})
    signals=[s for s in data.get("signals",[]) if not _is_dup(s,_get_example_dates(setup_type))]
    for sig in signals:
        key=f"{sig['ticker']}_{sig['date']}"
        sig["verdict"]=decisions.get(key,{}).get("verdict")
        sig["entry_date"]=decisions.get(key,{}).get("entry_date")
    return {"setup_type":setup_type,"n_signals":len(signals),"signals":signals}


@app.get("/api/vetting/{setup_type}/ohlcv/{ticker}")
async def get_vetting_ohlcv(setup_type: str, ticker: str,
                             signal_date: str=Query(...), lookback: int=Query(120), forward: int=Query(80)):
    with get_db() as db:
        rows=db.execute("SELECT date,open,high,low,close,volume FROM universe_ohlcv WHERE ticker=? ORDER BY date",(ticker,)).fetchall()
    if not rows: raise HTTPException(404,f"No OHLCV for {ticker}")
    all_data=[dict(r) for r in rows]; dates=[r["date"] for r in all_data]
    try: sig_idx=dates.index(signal_date)
    except ValueError:
        sig_idx=min(range(len(dates)),key=lambda i:abs((datetime.strptime(dates[i],"%Y-%m-%d")-datetime.strptime(signal_date,"%Y-%m-%d")).days))
    return {"ticker":ticker,"signal_date":signal_date,"data":all_data[max(0,sig_idx-lookback):min(len(all_data),sig_idx+forward)]}


@app.get("/api/vetting/{setup_type}/refinement-signals")
async def get_refinement_signals(setup_type: str, pile: str = Query("post")):
    """Serve signals from the latest refinement JSON.
    pile=pre: all signals (winners + losers + eliminated)
    pile=post: winners + surviving losers only (eliminated removed)
    """
    # Find latest refinement file
    with get_db() as db:
        rows = db.execute(
            "SELECT path, data FROM file_mirror WHERE path LIKE ? ORDER BY created_at DESC LIMIT 1",
            (f"local_runner/cache/refinement_{setup_type}_%",),
        ).fetchall()
    if not rows:
        raise HTTPException(404, f"No refinement data for {setup_type}. Run the refinement grind first.")
    data = _json.loads(rows[0]["data"])
    winners = data.get("winner_signals", [])
    losers = data.get("loser_signals", [])
    eliminated = data.get("eliminated_signals", [])
    # Tag each signal with its pile
    for s in winners:
        s["pile"] = "winner"
    for s in losers:
        s["pile"] = "loser"
    for s in eliminated:
        s["pile"] = "eliminated"
    if pile == "pre":
        signals = winners + losers + eliminated
    else:
        signals = winners + losers
    # Normalize field names for the vetting UI
    vetting_decisions = _load_json(VETTING_DATA_DIR / "vetting" / f"vetting_{setup_type}.json", {})
    example_dates = _get_example_dates(setup_type)
    # Also load rejected signals from DB
    with get_db() as db:
        rejected_rows = db.execute(
            "SELECT ticker, signal_date FROM rejected_signals WHERE setup_type=?", (setup_type,)
        ).fetchall()
    rejected_set = set(f"{r['ticker']}_{r['signal_date']}" for r in rejected_rows)
    out = []
    for s in signals:
        sig_date = s.get("signal_date", s.get("date", ""))
        tk = s.get("ticker", "")
        # Skip if it's already an example
        if _is_dup({"ticker": tk, "date": sig_date}, example_dates):
            continue
        key = f"{tk}_{sig_date}"
        verdict_data = vetting_decisions.get(key, {})
        verdict = verdict_data.get("verdict")
        if not verdict and key in rejected_set:
            verdict = "no"
        out.append({
            "ticker": tk,
            "signal_date": sig_date,
            "date": sig_date,
            "move_adr": clean_val(s.get("move_adr")),
            "adr_at_signal": clean_val(s.get("adr_at_signal")),
            "classification": s.get("classification"),
            "pile": s.get("pile"),
            "is_example": s.get("is_example", 0),
            "verdict": verdict,
            "entry_date": verdict_data.get("entry_date"),
        })
    n_winners = sum(1 for s in out if s["pile"] == "winner")
    n_losers = sum(1 for s in out if s["pile"] == "loser")
    n_eliminated = sum(1 for s in out if s["pile"] == "eliminated")
    return {
        "setup_type": setup_type,
        "pile": pile,
        "n_signals": len(out),
        "n_winners": n_winners,
        "n_losers": n_losers,
        "n_eliminated": n_eliminated,
        "signals": out,
    }


class VettingDecision(BaseModel):
    ticker: str; signal_date: str; verdict: str; entry_date: str = None


@app.post("/api/vetting/{setup_type}/decide")
async def save_vetting_decision(setup_type: str, req: VettingDecision):
    if req.verdict not in ("yes","maybe","no"): raise HTTPException(400,"verdict must be yes/maybe/no")
    if req.verdict=="yes" and not req.entry_date: raise HTTPException(400,"entry_date required for yes verdict")
    vetting_dir=VETTING_DATA_DIR/"vetting"; vetting_dir.mkdir(exist_ok=True)
    vetting_path=vetting_dir/f"vetting_{setup_type}.json"
    decisions=_load_json(vetting_path,{})
    key=f"{req.ticker}_{req.signal_date}"
    decisions[key]={"ticker":req.ticker,"signal_date":req.signal_date,"verdict":req.verdict,
                    "entry_date":req.entry_date,"timestamp":datetime.now().isoformat()}
    _save_json(vetting_path,decisions)
    result={"status":"saved","verdict":req.verdict}
    if req.verdict=="yes":
        try:
            with get_db() as db:
                if not db.execute("SELECT id FROM pending_examples WHERE setup_type=? AND ticker=? AND entry_date=?",(setup_type,req.ticker,req.entry_date)).fetchone():
                    db.execute("INSERT INTO pending_examples (setup_type,ticker,signal_date,entry_date) VALUES (?,?,?,?)",(setup_type,req.ticker,req.signal_date,req.entry_date))
                    result["message"]=f"Added to pending review: {req.ticker} {req.entry_date}"
                else: result["message"]=f"Already pending: {req.ticker} {req.entry_date}"
        except Exception as e: result["example_error"]=str(e)
        # Auto-queue AI review if agent idle
        try:
            state=_load_pipeline_state(); agent=_load_json(GRINDER_AGENT_FILE,{})
            last_hb=agent.get("last_heartbeat",""); agent_alive=False
            if last_hb:
                try:
                    hb=datetime.fromisoformat(last_hb.replace('+00:00','').replace('Z',''))
                    agent_alive=(datetime.utcnow()-hb).total_seconds()<30
                except: pass
            active_other=[j for j in state.get("jobs",[]) if j.get("status") in ("queued","running","claimed") and j.get("step_id")!="sample_review"]
            already_queued=any(j.get("step_id")=="sample_review" and j.get("status") in ("queued","running","claimed") for j in state.get("jobs",[]))
            if agent_alive and not active_other and not already_queued:
                state["jobs"]=[j for j in state.get("jobs",[]) if j.get("step_id")!="sample_review"]
                job={"job_id":f"pipe_sample_review_{datetime.now().strftime('%Y%m%d_%H%M%S')}","step_id":"sample_review",
                     "status":"queued","params":{"setup":setup_type},"created_at":datetime.now().isoformat()}
                state.setdefault("jobs",[]).append(job)
                state.setdefault("steps",{})["sample_review"]={"status":"queued","started_at":None,"finished_at":None,"duration_s":None,"exit_code":None,"error":None,"result_summary":None}
                _save_pipeline_state(state); logs=_load_pipeline_logs(); logs["sample_review"]=[]; _save_pipeline_logs(logs)
                result["review_queued"]=True
        except Exception as e: result["review_queue_error"]=str(e)
    elif req.verdict=="no":
        try:
            with get_db() as db:
                db.execute("INSERT OR IGNORE INTO rejected_signals (setup_type,ticker,signal_date) VALUES (?,?,?)",(setup_type,req.ticker,req.signal_date))
            result["message"]=f"Rejected: {req.ticker} {req.signal_date}"
        except Exception as e: result["reject_error"]=str(e)
    return result


@app.get("/api/vetting/earnings/{ticker}")
async def get_earnings_dates(ticker: str):
    ticker=ticker.upper()
    try:
        with get_db() as db:
            rows=db.execute("SELECT earnings_date FROM earnings_dates WHERE ticker=? ORDER BY earnings_date",[ticker]).fetchall()
            dates=[r[0] for r in rows]
        if dates: return {"ticker":ticker,"earnings_dates":dates}
        t=yf.Ticker(ticker); cal=t.get_earnings_dates(limit=20)
        if cal is not None and not cal.empty:
            dates=[d.strftime("%Y-%m-%d") for d in cal.index]
            with get_db() as db:
                for d in dates:
                    db.execute("INSERT OR REPLACE INTO earnings_dates (ticker,earnings_date,updated_at) VALUES (?,?,datetime('now'))",[ticker,d])
            return {"ticker":ticker,"earnings_dates":sorted(dates)}
        return {"ticker":ticker,"earnings_dates":[]}
    except Exception as e: return {"ticker":ticker,"earnings_dates":[],"error":str(e)}


@app.get("/api/vetting/{setup_type}/rejected")
async def get_rejected_signals(setup_type: str):
    with get_db() as db:
        rows=db.execute("SELECT ticker,signal_date,created_at FROM rejected_signals WHERE setup_type=? ORDER BY created_at DESC",(setup_type,)).fetchall()
    return {"setup_type":setup_type,"count":len(rows),"rejected":[dict(r) for r in rows]}


# ============================================================
# PENDING / AI REVIEW
# ============================================================

@app.get("/api/pending/{setup_type}")
async def get_pending_examples(setup_type: str):
    try:
        with get_db() as db:
            rows=db.execute("SELECT id,ticker,signal_date,entry_date,status,ai_verdict,ai_reasoning,review_notes,created_at FROM pending_examples WHERE setup_type=? ORDER BY created_at DESC",[setup_type]).fetchall()
        return {"pending":[dict(r) for r in rows]}
    except Exception as e: return {"pending":[],"error":str(e)}


@app.post("/api/pending/{setup_type}/backfill")
async def backfill_pending(setup_type: str):
    vp=VETTING_DATA_DIR/"vetting"/f"vetting_{setup_type}.json"
    if not vp.exists(): return {"status":"no vetting file","added":0}
    decisions=_load_json(vp,{}); added=0
    with get_db() as db:
        for key,v in decisions.items():
            if v.get("verdict")!="yes": continue
            ticker=v["ticker"]; entry_date=v.get("entry_date"); signal_date=v.get("signal_date","")
            if not entry_date: continue
            if db.execute("SELECT id FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?",(setup_type,ticker,entry_date)).fetchone(): continue
            if db.execute("SELECT id FROM pending_examples WHERE setup_type=? AND ticker=? AND entry_date=?",(setup_type,ticker,entry_date)).fetchone(): continue
            db.execute("INSERT OR IGNORE INTO pending_examples (setup_type,ticker,signal_date,entry_date) VALUES (?,?,?,?)",(setup_type,ticker,signal_date,entry_date)); added+=1
    return {"status":"ok","added":added}


@app.post("/api/pending/{setup_type}/{pending_id}/approve")
async def approve_pending(setup_type: str, pending_id: int):
    with get_db() as db:
        row=db.execute("SELECT * FROM pending_examples WHERE id=? AND setup_type=?",(pending_id,setup_type)).fetchone()
        if not row: raise HTTPException(404,"Not found")
        ticker,entry_date=row["ticker"],row["entry_date"]
        if not db.execute("SELECT id FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?",(setup_type,ticker,entry_date)).fetchone():
            ohlcv_df=fetch_ohlcv_yf(ticker,entry_date)
            if ohlcv_df is not None:
                eid=db.execute("INSERT INTO examples (setup_type,ticker,chart_date,entry_date) VALUES (?,?,?,?)",(setup_type,ticker,entry_date,entry_date)).lastrowid
                store_ohlcv(db,eid,ohlcv_df)
        db.execute("DELETE FROM pending_examples WHERE id=?",(pending_id,))
    return {"status":"approved","ticker":ticker,"entry_date":entry_date}


@app.post("/api/pending/{setup_type}/{pending_id}/reject")
async def reject_pending(setup_type: str, pending_id: int):
    with get_db() as db:
        row=db.execute("SELECT * FROM pending_examples WHERE id=? AND setup_type=?",(pending_id,setup_type)).fetchone()
        if not row: raise HTTPException(404,"Not found")
        db.execute("INSERT OR IGNORE INTO rejected_signals (setup_type,ticker,signal_date) VALUES (?,?,?)",(setup_type,row["ticker"],row["signal_date"]))
        db.execute("DELETE FROM pending_examples WHERE id=?",(pending_id,))
    return {"status":"rejected","ticker":row["ticker"]}


@app.post("/api/pending/{setup_type}/{pending_id}/review")
async def store_ai_review(setup_type: str, pending_id: int, request: Request):
    body=await request.json()
    with get_db() as db:
        if not db.execute("SELECT id FROM pending_examples WHERE id=? AND setup_type=?",(pending_id,setup_type)).fetchone():
            raise HTTPException(404,"Not found")
        db.execute("UPDATE pending_examples SET ai_verdict=?,ai_reasoning=?,status='reviewed',reviewed_at=datetime('now') WHERE id=?",
                   (body.get("ai_verdict",""),body.get("ai_reasoning",""),pending_id))
    return {"status":"ok","ai_verdict":body.get("ai_verdict","")}


@app.post("/api/pending/{setup_type}/reset-reviews")
async def reset_pending_reviews(setup_type: str):
    with get_db() as db:
        db.execute("UPDATE pending_examples SET ai_verdict=NULL,ai_reasoning=NULL,status='pending',reviewed_at=NULL WHERE setup_type=?",[setup_type])
        n=db.execute("SELECT changes()").fetchone()[0]
    return {"status":"ok","reset":n}


@app.post("/api/pending/{setup_type}/approve-all")
async def approve_all_pending(setup_type: str):
    with get_db() as db:
        rows=db.execute("SELECT * FROM pending_examples WHERE setup_type=? AND ai_verdict='APPROVE'",[setup_type]).fetchall()
        approved=0
        for row in rows:
            if not db.execute("SELECT id FROM examples WHERE setup_type=? AND ticker=? AND entry_date=?",(setup_type,row["ticker"],row["entry_date"])).fetchone():
                ohlcv_df=fetch_ohlcv_yf(row["ticker"],row["entry_date"])
                if ohlcv_df is not None:
                    eid=db.execute("INSERT INTO examples (setup_type,ticker,chart_date,entry_date) VALUES (?,?,?,?)",(setup_type,row["ticker"],row["entry_date"],row["entry_date"])).lastrowid
                    store_ohlcv(db,eid,ohlcv_df); approved+=1
        db.execute("DELETE FROM pending_examples WHERE setup_type=? AND ai_verdict='APPROVE'",[setup_type])
    return {"status":"ok","approved":approved}


# ============================================================
# UNIVERSE OHLCV
# ============================================================

@app.post("/api/query/bulk")
async def query_bulk(request: Request):
    body=await request.json(); sql=body.get("sql",""); limit=body.get("limit",1000)
    if not sql: raise HTTPException(400,"sql required")
    sql_upper=sql.strip().upper()
    if not sql_upper.startswith("SELECT"): raise HTTPException(400,"Only SELECT queries allowed")
    for forbidden in ["DROP","DELETE","UPDATE","INSERT","ALTER","CREATE"]:
        if forbidden in sql_upper: raise HTTPException(400,f"{forbidden} not allowed")
    try:
        with get_db() as db:
            rows=db.execute(sql).fetchall()
            results=[dict(r) for r in rows[:limit]]
        return {"results":results,"count":len(results)}
    except Exception as e: raise HTTPException(500,str(e))


@app.post("/api/universe/insert-ohlcv")
async def insert_ohlcv(request: Request):
    body=await request.json(); ticker=body.get("ticker","").strip().upper(); rows=body.get("rows",[])
    if not ticker or not rows: return {"error":"Need ticker and rows"}
    try:
        with get_db() as db:
            db.execute("INSERT OR IGNORE INTO tradable_universe (ticker) VALUES (?)",(ticker,))
            db.executemany("INSERT OR REPLACE INTO universe_ohlcv (ticker,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                           [(ticker,r["date"],r["open"],r["high"],r["low"],r["close"],r["volume"]) for r in rows])
        return {"ok":True,"ticker":ticker,"rows_inserted":len(rows)}
    except Exception as e: return {"error":str(e)}


@app.post("/api/universe/append-daily")
async def append_daily_data():
    try:
        from scripts.fetch_universe import append_daily
        result = append_daily()
        return result
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ============================================================
# V2 — Cycle management
# ============================================================

@app.post("/api/v2/cycles")
async def v2_create_cycle(request: Request):
    body=await request.json(); cycle_id=body.get("cycle_id"); setup_type=body.get("setup_type")
    if not cycle_id or not setup_type: raise HTTPException(400,"cycle_id and setup_type required")
    with get_db() as db:
        if db.execute("SELECT cycle_id FROM grind_cycles WHERE cycle_id=?",(cycle_id,)).fetchone():
            return {"cycle_id":cycle_id,"already_exists":True}
        db.execute("""INSERT INTO grind_cycles (cycle_id,setup_type,status,error_msg,is_current,n_examples_at_grind,created_at,completed_at,step_type,grind_params,source_hash)
                      VALUES (?,?,?,?,0,?,?,?,?,?,?)""",
                   (cycle_id,setup_type,body.get("status","complete"),body.get("error_msg"),body.get("n_examples_at_grind"),body.get("created_at"),body.get("completed_at"),
                    body.get("step_type"),body.get("grind_params"),body.get("source_hash")))
    return {"cycle_id":cycle_id,"created":True}


@app.patch("/api/v2/cycles/{cycle_id}")
async def v2_patch_cycle(cycle_id: str, request: Request):
    body=await request.json()
    ALLOWED={"status","error_msg","step_type","grind_params","source_hash","completed_at","reverted_at"}
    updates={k:v for k,v in body.items() if k in ALLOWED}
    if not updates: raise HTTPException(400,"No valid fields to update")
    with get_db() as db:
        if not db.execute("SELECT cycle_id FROM grind_cycles WHERE cycle_id=?",(cycle_id,)).fetchone():
            raise HTTPException(404,f"cycle_id {cycle_id!r} not found")
        set_clause=", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE grind_cycles SET {set_clause} WHERE cycle_id=?", list(updates.values())+[cycle_id])
    return {"cycle_id":cycle_id,"updated":list(updates.keys())}


@app.post("/api/v2/cycles/{cycle_id}/conditions")
async def v2_upload_conditions(cycle_id: str, request: Request):
    body=await request.json(); conditions=body.get("conditions",[])
    if not conditions: raise HTTPException(400,"conditions list is empty")
    with get_db() as db:
        if not db.execute("SELECT cycle_id FROM grind_cycles WHERE cycle_id=?",(cycle_id,)).fetchone():
            raise HTTPException(404,f"cycle_id {cycle_id!r} not found")
        db.execute("DELETE FROM cycle_conditions WHERE cycle_id=?",(cycle_id,))
        db.executemany("INSERT INTO cycle_conditions (cycle_id,tier,expression_name,low,high,filter_power,sort_order) VALUES (?,?,?,?,?,?,?)",
                       [(cycle_id,c.get("tier","D1"),c.get("expression_name",""),c.get("low"),c.get("high"),c.get("filter_power"),c.get("sort_order",i)) for i,c in enumerate(conditions)])
    return {"cycle_id":cycle_id,"inserted":len(conditions)}


@app.post("/api/v2/cycles/{cycle_id}/activate")
async def v2_activate_cycle(cycle_id: str):
    with get_db() as db:
        row=db.execute("SELECT setup_type FROM grind_cycles WHERE cycle_id=?",(cycle_id,)).fetchone()
        if not row: raise HTTPException(404,f"cycle_id {cycle_id!r} not found")
        setup_type=row["setup_type"]
        db.execute("UPDATE grind_cycles SET is_current=0 WHERE setup_type=?",(setup_type,))
        db.execute("UPDATE grind_cycles SET is_current=1 WHERE cycle_id=?",(cycle_id,))
    return {"cycle_id":cycle_id,"setup_type":setup_type,"message":f"Cycle {cycle_id} is now current for {setup_type}"}


@app.get("/api/v2/cycles/{setup_type}")
async def v2_list_cycles(setup_type: str, step_type: str = None):
    with get_db() as db:
        sql="""SELECT gc.cycle_id,gc.status,gc.is_current,gc.n_examples_at_grind,gc.created_at,gc.completed_at,gc.reverted_at,gc.step_type,gc.grind_params,gc.source_hash,COUNT(cc.id) AS n_conditions
               FROM grind_cycles gc LEFT JOIN cycle_conditions cc ON cc.cycle_id=gc.cycle_id
               WHERE gc.setup_type=?"""
        params=[setup_type]
        if step_type:
            sql+=" AND gc.step_type=?"
            params.append(step_type)
        sql+=" GROUP BY gc.cycle_id ORDER BY gc.created_at DESC"
        rows=db.execute(sql,params).fetchall()
    return {"setup_type":setup_type,"cycles":[dict(r) for r in rows]}


@app.get("/api/v2/cycles/{cycle_id}/conditions")
async def v2_get_conditions(cycle_id: str):
    with get_db() as db:
        rows=db.execute("SELECT tier,expression_name,low,high,filter_power,sort_order FROM cycle_conditions WHERE cycle_id=? ORDER BY sort_order",(cycle_id,)).fetchall()
    return {"cycle_id":cycle_id,"conditions":[dict(r) for r in rows]}


@app.post("/api/v2/cycles/{cycle_id}/signals")
async def v2_upload_signals(cycle_id: str, request: Request):
    body=await request.json(); signals=body.get("signals",[]); replace=body.get("replace",False)
    if not signals: raise HTTPException(400,"signals list is empty")
    with get_db() as db:
        if not db.execute("SELECT cycle_id FROM grind_cycles WHERE cycle_id=?",(cycle_id,)).fetchone():
            raise HTTPException(404,f"cycle_id {cycle_id!r} not found")
        if replace: db.execute("DELETE FROM cycle_signals WHERE cycle_id=?",(cycle_id,))
        db.executemany("INSERT INTO cycle_signals (cycle_id,setup_type,ticker,signal_date,bar_idx,close,adr,is_example,classification,classification_source,exit_triggered,exit_date,move_adr,mfe_adr,capture_eff) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       [(cycle_id,s.get("setup_type",""),s.get("ticker",""),s.get("signal_date",""),s.get("bar_idx"),s.get("close"),s.get("adr"),s.get("is_example",0),s.get("classification"),s.get("classification_source"),s.get("exit_triggered",0),s.get("exit_date"),s.get("move_adr"),s.get("mfe_adr"),s.get("capture_eff")) for s in signals])
    return {"cycle_id":cycle_id,"inserted":len(signals)}


@app.get("/api/v2/cycles/{cycle_id}/signals")
async def v2_get_signals(cycle_id: str):
    with get_db() as db:
        rows=db.execute("SELECT * FROM cycle_signals WHERE cycle_id=? ORDER BY signal_date,ticker",(cycle_id,)).fetchall()
    return {"cycle_id":cycle_id,"signals":[dict(r) for r in rows]}


@app.post("/api/v2/cycles/{cycle_id}/sacrificial_signals")
async def v2_upload_sacrificial(cycle_id: str, request: Request):
    body=await request.json(); signals=body.get("signals",[]); replace=body.get("replace",False)
    if not signals: raise HTTPException(400,"signals list is empty")
    with get_db() as db:
        if not db.execute("SELECT cycle_id FROM grind_cycles WHERE cycle_id=?",(cycle_id,)).fetchone():
            raise HTTPException(404,f"cycle_id {cycle_id!r} not found")
        if replace: db.execute("DELETE FROM cycle_sacrificial_signals WHERE cycle_id=?",(cycle_id,))
        db.executemany("INSERT INTO cycle_sacrificial_signals (cycle_id,setup_type,ticker,signal_date,bar_idx,close) VALUES (?,?,?,?,?,?)",
                       [(cycle_id,s.get("setup_type",""),s.get("ticker",""),s.get("signal_date",""),s.get("bar_idx"),s.get("close")) for s in signals])
    return {"cycle_id":cycle_id,"inserted":len(signals)}


@app.get("/api/v2/cycles/{cycle_id}/sacrificial_signals")
async def v2_get_sacrificial(cycle_id: str):
    with get_db() as db:
        rows=db.execute("SELECT * FROM cycle_sacrificial_signals WHERE cycle_id=? ORDER BY signal_date,ticker",(cycle_id,)).fetchall()
    return {"cycle_id":cycle_id,"signals":[dict(r) for r in rows]}


@app.get("/api/v2/exit_conditions/{setup_type}")
async def v2_get_exit_condition(setup_type: str):
    with get_db() as db:
        row=db.execute("SELECT * FROM exit_conditions WHERE setup_type=?",(setup_type,)).fetchone()
    return {"setup_type":setup_type,"exit_condition":dict(row) if row else None}


@app.post("/api/v2/exit_conditions")
async def v2_upsert_exit_condition(request: Request):
    body=await request.json()
    for k in ["setup_type","expression_name","direction","threshold","max_forward_bars"]:
        if k not in body: raise HTTPException(400,f"Missing field: {k}")
    now=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as db:
        db.execute("""INSERT INTO exit_conditions (setup_type,expression_name,direction,threshold,max_forward_bars,adr_threshold_multiplier,updated_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(setup_type) DO UPDATE SET expression_name=excluded.expression_name,direction=excluded.direction,threshold=excluded.threshold,max_forward_bars=excluded.max_forward_bars,adr_threshold_multiplier=excluded.adr_threshold_multiplier,updated_at=excluded.updated_at""",
                   (body["setup_type"],body["expression_name"],body["direction"],float(body["threshold"]),int(body["max_forward_bars"]),float(body.get("adr_threshold_multiplier",1.0)),now))
    return {"setup_type":body["setup_type"],"upserted":True}


@app.post("/api/v2/health")
async def v2_upsert_health(request: Request):
    body=await request.json(); cycle_id=body.get("cycle_id")
    if not cycle_id: raise HTTPException(400,"cycle_id required")
    with get_db() as db:
        db.execute("""INSERT INTO cycle_health (cycle_id,setup_type,n_signals,peak_per_day,avg_per_day,signal_stability_pct,examples_passing,examples_added_this_cycle,examples_since_last_grind,win_rate_auto,win_rate_vetted,pct_manually_vetted,median_winner_adr,median_loser_adr,ev_estimate,prev_cycle_id,signal_count_delta,condition_count_delta,win_rate_delta,promote_recommendation,flag_reason,live_ready,live_ready_blockers,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cycle_id) DO UPDATE SET n_signals=excluded.n_signals,peak_per_day=excluded.peak_per_day,avg_per_day=excluded.avg_per_day,signal_stability_pct=excluded.signal_stability_pct,examples_passing=excluded.examples_passing,examples_added_this_cycle=excluded.examples_added_this_cycle,examples_since_last_grind=excluded.examples_since_last_grind,win_rate_auto=excluded.win_rate_auto,win_rate_vetted=excluded.win_rate_vetted,pct_manually_vetted=excluded.pct_manually_vetted,median_winner_adr=excluded.median_winner_adr,median_loser_adr=excluded.median_loser_adr,ev_estimate=excluded.ev_estimate,prev_cycle_id=excluded.prev_cycle_id,signal_count_delta=excluded.signal_count_delta,condition_count_delta=excluded.condition_count_delta,win_rate_delta=excluded.win_rate_delta,promote_recommendation=excluded.promote_recommendation,flag_reason=excluded.flag_reason,live_ready=excluded.live_ready,live_ready_blockers=excluded.live_ready_blockers,computed_at=excluded.computed_at""",
                   (cycle_id,body.get("setup_type"),body.get("n_signals"),body.get("peak_per_day"),body.get("avg_per_day"),body.get("signal_stability_pct"),body.get("examples_passing"),body.get("examples_added_this_cycle"),body.get("examples_since_last_grind"),body.get("win_rate_auto"),body.get("win_rate_vetted"),body.get("pct_manually_vetted"),body.get("median_winner_adr"),body.get("median_loser_adr"),body.get("ev_estimate"),body.get("prev_cycle_id"),body.get("signal_count_delta"),body.get("condition_count_delta"),body.get("win_rate_delta"),body.get("promote_recommendation"),body.get("flag_reason"),body.get("live_ready"),body.get("live_ready_blockers"),body.get("computed_at")))
    return {"cycle_id":cycle_id,"message":f"Health metrics saved for {cycle_id}"}


@app.get("/api/v2/health/{cycle_id}")
async def v2_get_health(cycle_id: str):
    with get_db() as db:
        row=db.execute("SELECT * FROM cycle_health WHERE cycle_id=?",(cycle_id,)).fetchone()
    return {"cycle_id":cycle_id,"health":dict(row) if row else None}


@app.get("/api/v2/health/{setup_type}/latest")
async def v2_get_latest_health(setup_type: str):
    with get_db() as db:
        gc=db.execute("SELECT cycle_id FROM grind_cycles WHERE setup_type=? AND is_current=1",(setup_type,)).fetchone()
        if not gc: return {"setup_type":setup_type,"cycle_id":None,"health":None}
        cycle_id=gc["cycle_id"]
        row=db.execute("SELECT * FROM cycle_health WHERE cycle_id=?",(cycle_id,)).fetchone()
    return {"setup_type":setup_type,"cycle_id":cycle_id,"health":dict(row) if row else None}


# ── Regime Model ──────────────────────────────────────────────────────────────

@app.post("/api/v2/regime/model")
async def v2_upsert_regime_model(request: Request):
    body = await request.json()
    for k in ["setup_type","cycle_id","baseline_win_rate","updated_at"]:
        if k not in body: raise HTTPException(400, f"Missing field: {k}")
    with get_db() as db:
        db.execute("""INSERT INTO regime_model
            (setup_type,cycle_id,n_signals_used,n_features_tested,feature_weights,
             top_features,win_rate_by_decile,baseline_win_rate,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(setup_type) DO UPDATE SET
              cycle_id=excluded.cycle_id,
              n_signals_used=excluded.n_signals_used,
              n_features_tested=excluded.n_features_tested,
              feature_weights=excluded.feature_weights,
              top_features=excluded.top_features,
              win_rate_by_decile=excluded.win_rate_by_decile,
              baseline_win_rate=excluded.baseline_win_rate,
              updated_at=excluded.updated_at""",
            (body["setup_type"], body["cycle_id"],
             body.get("n_signals_used"), body.get("n_features_tested"),
             body.get("feature_weights"), body.get("top_features"),
             body.get("win_rate_by_decile"), float(body["baseline_win_rate"]),
             body["updated_at"]))
    return {"setup_type": body["setup_type"], "upserted": True}


@app.get("/api/v2/regime/model/{setup_type}")
async def v2_get_regime_model(setup_type: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM regime_model WHERE setup_type=?", (setup_type,)).fetchone()
    if not row:
        return {"setup_type": setup_type, "model": None}
    m = dict(row)
    # Parse JSON blobs for convenience
    for field in ["feature_weights", "top_features", "win_rate_by_decile"]:
        if m.get(field):
            try: m[field] = _json.loads(m[field])
            except: pass
    return {"setup_type": setup_type, "model": m}


@app.post("/api/v2/regime/scores")
async def v2_upsert_signal_scores(request: Request):
    body = await request.json()
    cycle_id = body.get("cycle_id")
    scores   = body.get("scores", [])
    if not cycle_id: raise HTTPException(400, "cycle_id required")
    if not scores:   raise HTTPException(400, "scores array required")
    with get_db() as db:
        # Clear existing scores for this cycle first
        db.execute("DELETE FROM signal_regime_scores WHERE cycle_id=?", (cycle_id,))
        # Insert all scores
        db.executemany("""INSERT INTO signal_regime_scores
            (cycle_signal_id, cycle_id, regime_score, expected_win_rate)
            VALUES (?,?,?,?)""",
            [(r["cycle_signal_id"], cycle_id,
              r.get("regime_score"), r.get("expected_win_rate"))
             for r in scores])
        # Denormalize regime_score back to cycle_signals
        db.executemany("""UPDATE cycle_signals SET regime_score=?
            WHERE id=? AND cycle_id=?""",
            [(r.get("regime_score"), r["cycle_signal_id"], cycle_id)
             for r in scores])
    return {"cycle_id": cycle_id, "n_scores": len(scores), "upserted": True}


@app.get("/api/v2/regime/scores/{cycle_id}")
async def v2_get_signal_scores(cycle_id: str):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM signal_regime_scores WHERE cycle_id=? ORDER BY regime_score DESC NULLS LAST",
            (cycle_id,)
        ).fetchall()
    return {"cycle_id": cycle_id, "scores": [dict(r) for r in rows]}


@app.get("/api/v2/watchlist/latest")
async def v2_get_latest_watchlist():
    with get_db() as db:
        # Get most recent run_date
        row = db.execute(
            "SELECT run_date FROM nightly_watchlist ORDER BY run_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"entries": [], "run_date": None}
        run_date = row["run_date"]
        rows = db.execute(
            "SELECT * FROM nightly_watchlist WHERE run_date=? ORDER BY rank ASC",
            (run_date,)
        ).fetchall()
    return {"run_date": run_date, "entries": [dict(r) for r in rows]}


# ════════════════════════════════════════════════════════════════
# FILE MIRROR — exact copies of local grinder JSON files
# ════════════════════════════════════════════════════════════════

# ============================================================
# TASK QUEUE — Remote command execution via agent
# ============================================================

ALLOWED_TASK_COMMANDS = {
    "signal_grind": "python local_runner/pyramid_grinder.py --setup {setup} --beam {beam} --depth {depth} --peak-target {peak_target}",
    "signal_grind_dartboard": "python local_runner/dartboard_grinder.py --setup {setup} --top-n {top_n}",
    "signal_grind_blackout": "python local_runner/pyramid_grinder.py --setup {setup} --blackout --beam {beam} --depth {depth} --peak-target {peak_target}",
    "exit_grind": "python scripts/exit_grinder.py --setup {setup}",
    "scan": "python scripts/signal_filter.py --setup {setup}",
    "refinement_grind": "python scripts/setup_refiner.py --setup {setup}",
    "proximity_grind": "python scripts/proximity_grinder.py --setup {setup}",
    "profit_grind": "python scripts/profit_grinder.py --setup {setup}",
    "regime_model": "python scripts/market_grinder.py --setup {setup}",
    "health_check": "python scripts/cycle_health.py --setup {setup}",
    "outlier_analysis": "python scripts/example_outlier_analysis.py --setup {setup}",
    "nightly": "python local_runner/nightly.py",
}

@app.post("/api/v2/tasks")
async def create_task(request: Request):
    body = await request.json()
    command = body.get("command")
    args = body.get("args", {})
    if command not in ALLOWED_TASK_COMMANDS:
        raise HTTPException(400, f"Unknown command: {command}. Allowed: {list(ALLOWED_TASK_COMMANDS.keys())}")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO task_queue (command, args, status) VALUES (?,?,?)",
            (command, _json.dumps(args), "pending")
        )
        task_id = cur.lastrowid
    return {"id": task_id, "command": command, "args": args, "status": "pending"}


@app.get("/api/v2/tasks/pending")
async def get_pending_tasks():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM task_queue WHERE status='pending' ORDER BY created_at ASC"
        ).fetchall()
        tasks = []
        for r in rows:
            db.execute("UPDATE task_queue SET status='claimed', claimed_at=datetime('now') WHERE id=?", (r["id"],))
            task = dict(r)
            task["status"] = "claimed"
            # Resolve the command template
            args = _json.loads(task["args"]) if task["args"] else {}
            template = ALLOWED_TASK_COMMANDS[task["command"]]
            # Fill defaults
            args.setdefault("setup", "dtss")
            args.setdefault("beam", "10000")
            args.setdefault("depth", "100")
            args.setdefault("peak_target", "3")
            args.setdefault("top_n", "500")
            task["resolved_command"] = template.format(**{k: str(v) for k, v in args.items()})
            tasks.append(task)
    return {"tasks": tasks}


@app.patch("/api/v2/tasks/{task_id}")
async def update_task(task_id: int, request: Request):
    body = await request.json()
    with get_db() as db:
        row = db.execute("SELECT id FROM task_queue WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Task not found: {task_id}")
        updates = []
        params = []
        for field in ("status", "exit_code", "error", "log_tail", "result_path"):
            if field in body:
                updates.append(f"{field}=?")
                params.append(body[field])
        if "status" in body and body["status"] in ("completed", "failed"):
            updates.append("completed_at=datetime('now')")
        if updates:
            params.append(task_id)
            db.execute(f"UPDATE task_queue SET {','.join(updates)} WHERE id=?", params)
    return {"id": task_id, "updated": True}


@app.get("/api/v2/tasks/{task_id}")
async def get_task(task_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM task_queue WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"Task not found: {task_id}")
    return dict(row)


@app.get("/api/v2/tasks")
async def list_tasks(status: str = None, limit: int = 20):
    with get_db() as db:
        if status:
            rows = db.execute(
                "SELECT * FROM task_queue WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM task_queue ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return {"tasks": [dict(r) for r in rows]}


# ============================================================
# RESEARCH JOBS — Open-ended Claude Code research sessions
# ============================================================

@app.post("/api/v2/research")
async def create_research_job(request: Request):
    body = await request.json()
    prompt = body.get("prompt")
    if not prompt:
        raise HTTPException(400, "prompt is required")
    max_time = body.get("max_time_s", 14400)
    max_phases = body.get("max_phases", 8)
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO research_jobs (prompt, status, max_time_s, max_phases) VALUES (?,?,?,?)",
            (prompt, "pending", max_time, max_phases)
        )
        job_id = cur.lastrowid
    return {"id": job_id, "prompt": prompt, "status": "pending", "max_time_s": max_time, "max_phases": max_phases}


@app.get("/api/v2/research/pending")
async def get_pending_research():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM research_jobs WHERE status='pending' ORDER BY created_at ASC LIMIT 1"
        ).fetchall()
        tasks = []
        for r in rows:
            db.execute("UPDATE research_jobs SET status='claimed', claimed_at=datetime('now') WHERE id=?", (r["id"],))
            tasks.append(dict(r))
    return {"jobs": tasks}


@app.patch("/api/v2/research/{job_id}")
async def update_research_job(job_id: int, request: Request):
    body = await request.json()
    with get_db() as db:
        row = db.execute("SELECT id FROM research_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Research job not found: {job_id}")
        updates = []
        params = []
        for field in ("status", "branch", "summary", "diff", "signal_before", "signal_after",
                       "examples_benched", "error", "log"):
            if field in body:
                updates.append(f"{field}=?")
                params.append(body[field])
        if "status" in body and body["status"] in ("completed", "failed"):
            updates.append("completed_at=datetime('now')")
        if updates:
            params.append(job_id)
            db.execute(f"UPDATE research_jobs SET {','.join(updates)} WHERE id=?", params)
    return {"id": job_id, "updated": True}


@app.get("/api/v2/research/{job_id}")
async def get_research_job(job_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"Research job not found: {job_id}")
    return dict(row)


@app.get("/api/v2/research")
async def list_research_jobs(status: str = None, limit: int = 20):
    with get_db() as db:
        if status:
            rows = db.execute(
                "SELECT id, prompt, status, branch, created_at, completed_at, summary, signal_before, signal_after FROM research_jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, prompt, status, branch, created_at, completed_at, summary, signal_before, signal_after FROM research_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return {"jobs": [dict(r) for r in rows]}


@app.post("/api/v2/research/{job_id}/reset")
async def reset_research_job(job_id: int):
    with get_db() as db:
        row = db.execute("SELECT id FROM research_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Research job not found: {job_id}")
        db.execute(
            "UPDATE research_jobs SET status='pending', claimed_at=NULL, completed_at=NULL, "
            "summary=NULL, diff=NULL, error=NULL, log=NULL, branch=NULL WHERE id=?",
            (job_id,)
        )
    return {"id": job_id, "status": "pending", "reset": True}


@app.post("/api/v2/files")
async def v2_upload_file(request: Request):
    body = await request.json()
    path = body.get("path")
    data = body.get("data")
    if not path or data is None:
        raise HTTPException(400, "path and data are required")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO file_mirror (path, data, size_bytes, created_at) VALUES (?,?,?,?)",
            (path, data, len(data), now)
        )
    return {"path": path, "size_bytes": len(data), "created": True}


@app.get("/api/v2/files")
async def v2_list_files(prefix: str = None):
    with get_db() as db:
        if prefix:
            rows = db.execute(
                "SELECT path, size_bytes, created_at FROM file_mirror WHERE path LIKE ? ORDER BY created_at DESC",
                (prefix + "%",)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT path, size_bytes, created_at FROM file_mirror ORDER BY created_at DESC"
            ).fetchall()
    return {"files": [dict(r) for r in rows]}


@app.get("/api/v2/files/{path:path}")
async def v2_get_file(path: str):
    with get_db() as db:
        row = db.execute("SELECT data, created_at FROM file_mirror WHERE path=?", (path,)).fetchone()
    if not row:
        raise HTTPException(404, f"File not found: {path}")
    return Response(content=row["data"], media_type="application/json",
                    headers={"X-Created-At": row["created_at"]})


@app.delete("/api/v2/files/{path:path}")
async def v2_delete_file(path: str):
    with get_db() as db:
        row = db.execute("SELECT path FROM file_mirror WHERE path=?", (path,)).fetchone()
        if not row:
            raise HTTPException(404, f"File not found: {path}")
        db.execute("DELETE FROM file_mirror WHERE path=?", (path,))
    return {"path": path, "deleted": True}


# Serve frontend — MUST be last
app.mount("/", StaticFiles(directory="app", html=True), name="frontend")
