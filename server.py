"""ScanPerfect V2 — FastAPI backend. Deploy: 2026-03-06."""

import os
import json as _json
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

import numpy as np
import pandas as pd
import yfinance as yf

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
        """)


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
    {"id":"nightly",     "name":"Nightly Refresh",       "category":"data",     "description":"Append new OHLCV bars, rebuild caches and expression matrix."},
    {"id":"grind",       "name":"Layer 1: Grind",        "category":"pipeline", "description":"Pyramid grinder — examples vs universe → candidate conditions. Uploads cycle to Railway."},
    {"id":"scan",        "name":"Layer 2: Scan",         "category":"pipeline", "description":"Apply conditions to full 5yr history → deduped raw signal set."},
    {"id":"exit_filter", "name":"Layer 3: Exit Filter",  "category":"pipeline", "description":"Apply exit condition → identify which signals moved. Measures ADR, MFE, capture efficiency."},
    {"id":"exit_grind",  "name":"Exit Grinder",          "category":"pipeline", "description":"Find optimal exit condition from example set. Re-run when example library grows materially."},
    {"id":"classify",    "name":"Layer 4: Classify",     "category":"pipeline", "description":"Label every signal winner or loser (AUTO WIN / AUTO LOSS / MANUAL)."},
    {"id":"vet",         "name":"Layer 5: Vet",          "category":"pipeline", "description":"Human review → AI queue → final approval → example library.", "is_manual":True},
    {"id":"regime",      "name":"Layer 6: Regime",       "category":"pipeline", "description":"Correlate signal classifications vs market conditions → regime score model."},
    {"id":"health",      "name":"Layer 7: Health Check", "category":"pipeline", "description":"Measure cycle quality vs previous cycle. Drives promote / revert / live-ready."},
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
    types={"dtss":{"name":"DTSS","desc":"Double Top Short Sell"},
            "3-4db":{"name":"3-4DB","desc":"3-4 Day Bounce (Short)"},
            "htf":{"name":"HTF","desc":"High Tight Flag (Long)"}}
    with get_db() as db:
        for st in types:
            types[st]["examples"]=db.execute("SELECT COUNT(*) FROM examples WHERE setup_type=?",(st,)).fetchone()[0]
    return types


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
    return {"status":"queued"}


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
        db.execute("INSERT INTO grind_cycles (cycle_id,setup_type,status,error_msg,is_current,n_examples_at_grind,created_at,completed_at) VALUES (?,?,?,?,0,?,?,?)",
                   (cycle_id,setup_type,body.get("status","complete"),body.get("error_msg"),body.get("n_examples_at_grind"),body.get("created_at"),body.get("completed_at")))
    return {"cycle_id":cycle_id,"created":True}


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
async def v2_list_cycles(setup_type: str):
    with get_db() as db:
        rows=db.execute("""SELECT gc.cycle_id,gc.status,gc.is_current,gc.n_examples_at_grind,gc.created_at,gc.completed_at,gc.reverted_at,COUNT(cc.id) AS n_conditions
               FROM grind_cycles gc LEFT JOIN cycle_conditions cc ON cc.cycle_id=gc.cycle_id
               WHERE gc.setup_type=? GROUP BY gc.cycle_id ORDER BY gc.created_at DESC""",(setup_type,)).fetchall()
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


# Serve frontend — MUST be last
app.mount("/", StaticFiles(directory="app", html=True), name="frontend")
