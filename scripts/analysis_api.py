"""Analysis API — wires profiling, discovery, outcome, and management optimizer
engines into the Railway FastAPI server as background tasks with status/results endpoints.

All computation runs in-process using direct DB access (no HTTP self-calls).
Long-running tasks use FastAPI BackgroundTasks with status polling.
Results stored in analysis_* tables for persistence.
"""

import json
import time
import traceback
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from scripts import local_db as db

# ============================================================
# DB TABLES
# ============================================================

ANALYSIS_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS analysis_status (
    id TEXT PRIMARY KEY,
    setup_type TEXT NOT NULL,
    engine TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'idle',
    progress_current INTEGER DEFAULT 0,
    progress_total INTEGER DEFAULT 0,
    progress_message TEXT DEFAULT '',
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    result_summary TEXT
);

CREATE TABLE IF NOT EXISTS analysis_profiling (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_type TEXT NOT NULL,
    ticker TEXT NOT NULL,
    entry_date TEXT,
    scan_date TEXT,
    is_example INTEGER DEFAULT 1,
    measurements_json TEXT NOT NULL,
    computed_at TEXT DEFAULT (datetime('now')),
    UNIQUE(setup_type, ticker, entry_date, is_example)
);

CREATE TABLE IF NOT EXISTS analysis_discovery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_type TEXT NOT NULL,
    run_id TEXT NOT NULL,
    feature_rank INTEGER NOT NULL,
    feature_name TEXT NOT NULL,
    concept_group TEXT,
    combined_score REAL,
    consistency REAL,
    selectivity REAL,
    direction TEXT,
    threshold REAL,
    example_min REAL,
    example_max REAL,
    universe_pct REAL,
    computed_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS analysis_discovery_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_type TEXT NOT NULL,
    run_id TEXT NOT NULL,
    n_examples INTEGER,
    n_universe INTEGER,
    n_features_scored INTEGER,
    n_features_significant INTEGER,
    top_concepts TEXT,
    summary_text TEXT,
    computed_at TEXT DEFAULT (datetime('now')),
    UNIQUE(setup_type, run_id)
);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    setup_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'examples',
    entry_price REAL,
    scan_bar_atr REAL,
    scan_bar_close REAL,
    bars_available INTEGER,
    bar_num INTEGER NOT NULL,
    bar_date TEXT,
    bar_open REAL,
    bar_high REAL,
    bar_low REAL,
    bar_close REAL,
    bar_volume INTEGER,
    open_vs_entry REAL,
    high_vs_entry REAL,
    low_vs_entry REAL,
    close_vs_entry REAL,
    mfe REAL,
    mae REAL,
    running_best REAL,
    running_worst REAL,
    UNIQUE(ticker, entry_date, setup_type, bar_num)
);

CREATE TABLE IF NOT EXISTS analysis_optimization (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_type TEXT NOT NULL,
    run_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    ev REAL,
    win_rate REAL,
    avg_winner REAL,
    avg_loser REAL,
    profit_factor REAL,
    n_trades INTEGER,
    stop_atr REAL,
    target_atr REAL,
    time_stop INTEGER,
    trail_type TEXT,
    trail_value REAL,
    partial_type TEXT,
    partial_value REAL,
    computed_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS analysis_plateaus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_type TEXT NOT NULL,
    run_id TEXT NOT NULL,
    plateau_rank INTEGER NOT NULL,
    n_strategies INTEGER,
    mean_ev REAL,
    min_ev REAL,
    max_ev REAL,
    mean_win_rate REAL,
    robustness REAL,
    stop_range TEXT,
    target_range TEXT,
    time_range TEXT,
    trail_types TEXT,
    recommendation TEXT,
    computed_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_profiling_setup ON analysis_profiling(setup_type);
CREATE INDEX IF NOT EXISTS idx_discovery_setup ON analysis_discovery(setup_type, run_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_setup ON signal_outcomes(setup_type, source);
CREATE INDEX IF NOT EXISTS idx_outcomes_ticker ON signal_outcomes(ticker, entry_date);
CREATE INDEX IF NOT EXISTS idx_optimization_setup ON analysis_optimization(setup_type, run_id);
"""


def init_analysis_tables():
    """Create analysis tables if they don't exist."""
    db.executescript(ANALYSIS_TABLES_SQL)


# ============================================================
# STATUS TRACKING
# ============================================================

def _status_key(engine: str, setup_type: str) -> str:
    return f"{engine}_{setup_type}"


def _update_status(key: str, setup_type: str, engine: str, **kwargs):
    """Update or insert analysis status."""
    with db.get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM analysis_status WHERE id=?", (key,)
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            vals = list(kwargs.values()) + [key]
            conn.execute(f"UPDATE analysis_status SET {sets} WHERE id=?", vals)
        else:
            kwargs.update({"id": key, "setup_type": setup_type, "engine": engine})
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join(["?"] * len(kwargs))
            conn.execute(
                f"INSERT INTO analysis_status ({cols}) VALUES ({placeholders})",
                list(kwargs.values())
            )
        conn.commit()


def _get_status(engine: str, setup_type: str) -> dict:
    """Get analysis status."""
    key = _status_key(engine, setup_type)
    rows = db.query(f"SELECT * FROM analysis_status WHERE id='{key}'")
    if rows:
        return rows[0]
    return {"id": key, "state": "idle", "engine": engine, "setup_type": setup_type}


# ============================================================
# PROFILING RUNNER
# ============================================================

def run_profiling(setup_type: str, universe_n: int = 500):
    """Run profiling engine for a setup type. Designed to run as background task."""
    key = _status_key("profiling", setup_type)
    _update_status(key, setup_type, "profiling",
                   state="running", started_at=datetime.now().isoformat(),
                   progress_current=0, progress_total=0,
                   progress_message="Initializing profiling engine...",
                   error="", completed_at="")

    try:
        from scripts.profiling_engine import ProfilingEngine

        # Use the Railway API URL (self-call) — the engines are designed for this
        # and it keeps the code paths identical to standalone usage
        api_base = "https://web-production-e3025.up.railway.app"
        engine = ProfilingEngine(api_base=api_base)

        def progress(current, total, msg):
            _update_status(key, setup_type, "profiling",
                           progress_current=current, progress_total=total,
                           progress_message=msg)

        # Profile examples
        progress(0, 0, "Profiling examples...")
        examples_df = engine.profile_examples(
            setup_type, include_market=True, progress_callback=progress
        )

        if examples_df.empty:
            raise ValueError(f"No examples profiled for '{setup_type}'")

        n_examples = len(examples_df)

        # Store example profiles
        progress(0, 0, f"Storing {n_examples} example profiles...")
        with db.get_db() as conn:
            # Clear old profiles for this setup
            conn.execute(
                "DELETE FROM analysis_profiling WHERE setup_type=? AND is_example=1",
                (setup_type,)
            )
            for _, row in examples_df.iterrows():
                measurements = {k: (float(v) if pd.notna(v) and np.isfinite(v) else None)
                                for k, v in row.items()
                                if k not in ('ticker', 'entry_date', 'scan_date', 'is_example')}
                conn.execute(
                    "INSERT INTO analysis_profiling "
                    "(setup_type, ticker, entry_date, scan_date, is_example, measurements_json) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (setup_type, row.get('ticker', ''), row.get('entry_date', ''),
                     row.get('scan_date', ''), json.dumps(measurements))
                )
            conn.commit()

        # Profile universe sample
        progress(0, 0, f"Profiling {universe_n} universe tickers...")

        # Get latest date
        latest_rows = db.query("SELECT MAX(date) as d FROM universe_ohlcv")
        latest_date = latest_rows[0]['d'] if latest_rows else None

        if latest_date:
            universe_df = engine.profile_universe_sample(
                date=latest_date, n=universe_n,
                include_market=True, progress_callback=progress
            )

            if not universe_df.empty:
                n_universe = len(universe_df)
                progress(0, 0, f"Storing {n_universe} universe profiles...")
                with db.get_db() as conn:
                    conn.execute(
                        "DELETE FROM analysis_profiling WHERE setup_type=? AND is_example=0",
                        (setup_type,)
                    )
                    for _, row in universe_df.iterrows():
                        measurements = {k: (float(v) if pd.notna(v) and np.isfinite(v) else None)
                                        for k, v in row.items()
                                        if k not in ('ticker', 'entry_date', 'scan_date', 'is_example')}
                        conn.execute(
                            "INSERT INTO analysis_profiling "
                            "(setup_type, ticker, entry_date, scan_date, is_example, measurements_json) "
                            "VALUES (?, ?, ?, ?, 0, ?)",
                            (setup_type, row.get('ticker', ''), '',
                             row.get('scan_date', latest_date), json.dumps(measurements))
                        )
                    conn.commit()
            else:
                n_universe = 0
        else:
            n_universe = 0

        summary = json.dumps({
            "examples_profiled": n_examples,
            "universe_profiled": n_universe,
            "features_computed": len([c for c in examples_df.columns
                                      if c not in ('ticker', 'entry_date', 'scan_date', 'is_example')])
        })
        _update_status(key, setup_type, "profiling",
                       state="complete", completed_at=datetime.now().isoformat(),
                       progress_message="Done", result_summary=summary)

    except Exception as e:
        _update_status(key, setup_type, "profiling",
                       state="error", error=f"{e}\n{traceback.format_exc()}")


# ============================================================
# DISCOVERY RUNNER
# ============================================================

def run_discovery(setup_type: str):
    """Run discovery engine using stored profiling data. Background task."""
    key = _status_key("discovery", setup_type)
    _update_status(key, setup_type, "discovery",
                   state="running", started_at=datetime.now().isoformat(),
                   progress_current=0, progress_total=0,
                   progress_message="Loading profiling data...",
                   error="", completed_at="")

    try:
        from scripts.discovery_engine import DiscoveryEngine

        # Load stored profiling data
        example_rows = db.query(
            f"SELECT ticker, entry_date, scan_date, measurements_json "
            f"FROM analysis_profiling WHERE setup_type='{setup_type}' AND is_example=1"
        )
        universe_rows = db.query(
            f"SELECT ticker, scan_date, measurements_json "
            f"FROM analysis_profiling WHERE setup_type='{setup_type}' AND is_example=0"
        )

        if not example_rows:
            raise ValueError(f"No profiling data for '{setup_type}'. Run profiling first.")
        if not universe_rows:
            raise ValueError(f"No universe profiling data. Run profiling first.")

        # Reconstruct DataFrames
        def rows_to_df(rows):
            records = []
            for r in rows:
                m = json.loads(r['measurements_json'])
                m['ticker'] = r['ticker']
                m['scan_date'] = r.get('scan_date', '')
                if 'entry_date' in r:
                    m['entry_date'] = r['entry_date']
                records.append(m)
            return pd.DataFrame(records)

        examples_df = rows_to_df(example_rows)
        universe_df = rows_to_df(universe_rows)

        _update_status(key, setup_type, "discovery",
                       progress_message=f"Running discovery on {len(examples_df)} examples vs {len(universe_df)} universe...")

        # Run discovery
        disc = DiscoveryEngine()
        report = disc.discover(examples_df, universe_df, setup_type=setup_type)

        # Store results
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        _update_status(key, setup_type, "discovery",
                       progress_message="Storing discovery results...")

        with db.get_db() as conn:
            # Clear old discovery results for this setup
            conn.execute("DELETE FROM analysis_discovery WHERE setup_type=?", (setup_type,))
            conn.execute("DELETE FROM analysis_discovery_meta WHERE setup_type=?", (setup_type,))

            # Store ranked features
            for rank, feat in enumerate(report.top_features(200), 1):
                conn.execute(
                    "INSERT INTO analysis_discovery "
                    "(setup_type, run_id, feature_rank, feature_name, concept_group, "
                    "combined_score, consistency, selectivity, direction, threshold, "
                    "example_min, example_max, universe_pct) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (setup_type, run_id, rank, feat.name, feat.concept,
                     feat.combined_score, feat.consistency_score, feat.selectivity_score,
                     feat.threshold_direction, feat.threshold_value,
                     feat.example_min, feat.example_max, feat.selectivity_score)
                )

            # Store meta
            grouped = report.grouped_by_concept(100)
            top_concepts = list(grouped.keys())[:10]
            summary_text = report.summary(30)

            conn.execute(
                "INSERT INTO analysis_discovery_meta "
                "(setup_type, run_id, n_examples, n_universe, n_features_scored, "
                "n_features_significant, top_concepts, summary_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (setup_type, run_id, len(examples_df), len(universe_df),
                 report.total_features, len(report.features),
                 json.dumps(top_concepts), summary_text)
            )
            conn.commit()

        summary = json.dumps({
            "run_id": run_id,
            "features_scored": report.total_features,
            "features_significant": len(report.features),
            "top_concepts": top_concepts
        })
        _update_status(key, setup_type, "discovery",
                       state="complete", completed_at=datetime.now().isoformat(),
                       progress_message="Done", result_summary=summary)

    except Exception as e:
        _update_status(key, setup_type, "discovery",
                       state="error", error=f"{e}\n{traceback.format_exc()}")


# ============================================================
# OUTCOME RUNNER
# ============================================================

def run_outcomes(setup_type: str, source: str = "examples", limit: Optional[int] = None):
    """Compute forward outcomes for examples or backtest signals. Background task."""
    key = _status_key("outcomes", setup_type)
    _update_status(key, setup_type, "outcomes",
                   state="running", started_at=datetime.now().isoformat(),
                   progress_current=0, progress_total=0,
                   progress_message=f"Computing {source} outcomes...",
                   error="", completed_at="")

    try:
        from scripts.outcome_engine import OutcomeEngine, SETUP_CONFIGS, DEFAULT_FORWARD_BARS

        api_base = "https://web-production-e3025.up.railway.app"
        engine = OutcomeEngine(api_base=api_base)

        if source == "examples":
            outcomes = engine.compute_for_examples(setup_type)
        elif source == "backtest":
            outcomes = engine.compute_for_backtest_signals(
                setup_type, limit=limit
            )
        else:
            raise ValueError(f"Invalid source: {source}")

        if not outcomes:
            _update_status(key, setup_type, "outcomes",
                           state="complete", completed_at=datetime.now().isoformat(),
                           progress_message="No outcomes computed",
                           result_summary=json.dumps({"stored": 0}))
            return

        # Store outcomes directly to DB
        _update_status(key, setup_type, "outcomes",
                       progress_message=f"Storing {len(outcomes)} signal outcomes...")

        stored = 0
        with db.get_db() as conn:
            # Ensure table exists
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    entry_date TEXT NOT NULL,
                    setup_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'examples',
                    entry_price REAL,
                    scan_bar_atr REAL,
                    scan_bar_close REAL,
                    bars_available INTEGER,
                    bar_num INTEGER NOT NULL,
                    bar_date TEXT,
                    bar_open REAL,
                    bar_high REAL,
                    bar_low REAL,
                    bar_close REAL,
                    bar_volume INTEGER,
                    open_vs_entry REAL,
                    high_vs_entry REAL,
                    low_vs_entry REAL,
                    close_vs_entry REAL,
                    mfe REAL,
                    mae REAL,
                    running_best REAL,
                    running_worst REAL,
                    UNIQUE(ticker, entry_date, setup_type, bar_num)
                );
            """)

            for outcome in outcomes:
                # Delete existing
                conn.execute(
                    "DELETE FROM signal_outcomes WHERE ticker=? AND entry_date=? AND setup_type=?",
                    (outcome.ticker, outcome.entry_date, outcome.setup_type)
                )
                # Insert rows
                for row in outcome.rows:
                    conn.execute(
                        "INSERT INTO signal_outcomes "
                        "(ticker, entry_date, setup_type, direction, source, "
                        "entry_price, scan_bar_atr, scan_bar_close, bars_available, "
                        "bar_num, bar_date, bar_open, bar_high, bar_low, bar_close, bar_volume, "
                        "open_vs_entry, high_vs_entry, low_vs_entry, close_vs_entry, "
                        "mfe, mae, running_best, running_worst) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (outcome.ticker, outcome.entry_date, outcome.setup_type,
                         outcome.direction, source,
                         outcome.entry_price, outcome.scan_bar_atr, outcome.scan_bar_close,
                         outcome.bars_available,
                         row.bar, row.date, row.open, row.high, row.low, row.close, row.volume,
                         row.open_vs_entry, row.high_vs_entry, row.low_vs_entry, row.close_vs_entry,
                         row.mfe, row.mae, row.running_best, row.running_worst)
                    )
                    stored += 1
            conn.commit()

        summary = json.dumps({
            "signals_computed": len(outcomes),
            "rows_stored": stored,
            "source": source
        })
        _update_status(key, setup_type, "outcomes",
                       state="complete", completed_at=datetime.now().isoformat(),
                       progress_message="Done", result_summary=summary)

    except Exception as e:
        _update_status(key, setup_type, "outcomes",
                       state="error", error=f"{e}\n{traceback.format_exc()}")


# ============================================================
# OPTIMIZATION RUNNER
# ============================================================

def run_optimization(setup_type: str, mode: str = "quick", source: str = "examples"):
    """Run management optimizer against stored outcomes. Background task."""
    key = _status_key("optimization", setup_type)
    _update_status(key, setup_type, "optimization",
                   state="running", started_at=datetime.now().isoformat(),
                   progress_current=0, progress_total=0,
                   progress_message="Loading outcome data...",
                   error="", completed_at="")

    try:
        from scripts.outcome_engine import OutcomeEngine, SignalOutcome, OutcomeRow
        from scripts.management_optimizer import ManagementOptimizer

        # Load outcomes from DB
        outcome_rows = db.query(
            f"SELECT * FROM signal_outcomes "
            f"WHERE setup_type='{setup_type}' AND source='{source}' "
            f"ORDER BY ticker, entry_date, bar_num"
        )

        if not outcome_rows:
            raise ValueError(f"No outcomes for '{setup_type}' source='{source}'. Run outcomes first.")

        # Reconstruct SignalOutcome objects
        signal_map = {}
        for r in outcome_rows:
            key_sig = f"{r['ticker']}_{r['entry_date']}"
            if key_sig not in signal_map:
                signal_map[key_sig] = SignalOutcome(
                    ticker=r['ticker'],
                    entry_date=r['entry_date'],
                    setup_type=r['setup_type'],
                    direction=r['direction'],
                    entry_price=r['entry_price'],
                    scan_bar_atr=r['scan_bar_atr'],
                    scan_bar_close=r['scan_bar_close'],
                    bars_available=r['bars_available'],
                    rows=[]
                )
            signal_map[key_sig].rows.append(OutcomeRow(
                bar=r['bar_num'],
                date=r['bar_date'],
                open=r['bar_open'],
                high=r['bar_high'],
                low=r['bar_low'],
                close=r['bar_close'],
                volume=r['bar_volume'] or 0,
                open_vs_entry=r['open_vs_entry'],
                high_vs_entry=r['high_vs_entry'],
                low_vs_entry=r['low_vs_entry'],
                close_vs_entry=r['close_vs_entry'],
                mfe=r['mfe'],
                mae=r['mae'],
                running_best=r['running_best'],
                running_worst=r['running_worst']
            ))

        outcomes = list(signal_map.values())
        n_signals = len(outcomes)

        _update_status(_status_key("optimization", setup_type), setup_type, "optimization",
                       progress_message=f"Converting {n_signals} signals to matrix...")

        # Convert to matrix
        matrix = OutcomeEngine.outcomes_to_matrix(outcomes)

        # Get direction
        direction = outcomes[0].direction if outcomes else "short"

        _update_status(_status_key("optimization", setup_type), setup_type, "optimization",
                       progress_message=f"Running {mode} optimization on {n_signals} signals...")

        # Run optimizer
        optimizer = ManagementOptimizer()

        if mode == "quick":
            results = optimizer.optimize_quick(matrix, direction=direction)
        else:
            results = optimizer.optimize(matrix, direction=direction)

        # Find plateaus
        _update_status(_status_key("optimization", setup_type), setup_type, "optimization",
                       progress_message="Finding robust plateaus...")

        plateaus = optimizer.find_plateaus(results)

        # Store results
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        _update_status(_status_key("optimization", setup_type), setup_type, "optimization",
                       progress_message="Storing optimization results...")

        with db.get_db() as conn:
            conn.execute("DELETE FROM analysis_optimization WHERE setup_type=?", (setup_type,))
            conn.execute("DELETE FROM analysis_plateaus WHERE setup_type=?", (setup_type,))

            # Store top 100 strategies
            sorted_results = sorted(results, key=lambda r: r.ev, reverse=True)[:100]
            for rank, r in enumerate(sorted_results, 1):
                conn.execute(
                    "INSERT INTO analysis_optimization "
                    "(setup_type, run_id, rank, ev, win_rate, avg_winner, avg_loser, "
                    "profit_factor, n_trades, stop_atr, target_atr, time_stop, "
                    "trail_type, trail_value, partial_type, partial_value) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (setup_type, run_id, rank,
                     float(r.ev), float(r.win_rate),
                     float(r.avg_winner), float(r.avg_loser),
                     float(r.profit_factor), int(r.n_trades),
                     float(r.stop), float(r.target), int(r.time_stop),
                     r.trail_type, float(r.trail_value) if r.trail_value else None,
                     r.partial_type, float(r.partial_value) if r.partial_value else None)
                )

            # Store plateaus
            for rank, p in enumerate(plateaus, 1):
                conn.execute(
                    "INSERT INTO analysis_plateaus "
                    "(setup_type, run_id, plateau_rank, n_strategies, "
                    "mean_ev, min_ev, max_ev, mean_win_rate, robustness, "
                    "stop_range, target_range, time_range, trail_types, recommendation) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (setup_type, run_id, rank,
                     p.n_strategies, p.mean_ev, p.min_ev, p.max_ev,
                     p.mean_win_rate, p.robustness,
                     json.dumps(p.stop_range) if hasattr(p, 'stop_range') else "[]",
                     json.dumps(p.target_range) if hasattr(p, 'target_range') else "[]",
                     json.dumps(p.time_range) if hasattr(p, 'time_range') else "[]",
                     json.dumps(p.trail_types) if hasattr(p, 'trail_types') else "[]",
                     getattr(p, 'recommendation', ''))
                )
            conn.commit()

        summary = json.dumps({
            "run_id": run_id,
            "mode": mode,
            "n_signals": n_signals,
            "n_strategies_tested": len(results),
            "best_ev": float(sorted_results[0].ev) if sorted_results else 0,
            "best_win_rate": float(sorted_results[0].win_rate) if sorted_results else 0,
            "n_plateaus": len(plateaus),
            "best_plateau_ev": float(plateaus[0].mean_ev) if plateaus else 0
        })
        _update_status(_status_key("optimization", setup_type), setup_type, "optimization",
                       state="complete", completed_at=datetime.now().isoformat(),
                       progress_message="Done", result_summary=summary)

    except Exception as e:
        _update_status(_status_key("optimization", setup_type), setup_type, "optimization",
                       state="error", error=f"{e}\n{traceback.format_exc()}")


# ============================================================
# FULL PIPELINE RUNNER
# ============================================================

def run_full_pipeline(setup_type: str, universe_n: int = 500):
    """Run all 4 engines in sequence. Background task."""
    key = _status_key("pipeline", setup_type)
    _update_status(key, setup_type, "pipeline",
                   state="running", started_at=datetime.now().isoformat(),
                   progress_message="Starting full analysis pipeline...",
                   error="", completed_at="")
    try:
        # 1. Profile
        _update_status(key, setup_type, "pipeline",
                       progress_message="Step 1/4: Profiling...")
        run_profiling(setup_type, universe_n=universe_n)
        prof_status = _get_status("profiling", setup_type)
        if prof_status.get("state") == "error":
            raise ValueError(f"Profiling failed: {prof_status.get('error', 'unknown')}")

        # 2. Discover
        _update_status(key, setup_type, "pipeline",
                       progress_message="Step 2/4: Discovery...")
        run_discovery(setup_type)
        disc_status = _get_status("discovery", setup_type)
        if disc_status.get("state") == "error":
            raise ValueError(f"Discovery failed: {disc_status.get('error', 'unknown')}")

        # 3. Outcomes
        _update_status(key, setup_type, "pipeline",
                       progress_message="Step 3/4: Computing outcomes...")
        run_outcomes(setup_type, source="examples")

        # 4. Optimize
        _update_status(key, setup_type, "pipeline",
                       progress_message="Step 4/4: Optimizing management...")
        run_optimization(setup_type, mode="quick")

        _update_status(key, setup_type, "pipeline",
                       state="complete", completed_at=datetime.now().isoformat(),
                       progress_message="Full pipeline complete",
                       result_summary=json.dumps({"steps_completed": 4}))

    except Exception as e:
        _update_status(key, setup_type, "pipeline",
                       state="error", error=f"{e}\n{traceback.format_exc()}")
