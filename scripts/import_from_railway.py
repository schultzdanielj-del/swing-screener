"""
Import from Railway — One-time migration to seed local SQLite DB.

Usage:
    python scripts/import_from_railway.py

Pulls from Railway API:
  - examples (all setups)
  - rejected_signals (all setups)
  - pending_examples (all setups)
  - earnings_dates

Idempotent — uses INSERT OR IGNORE, safe to run multiple times.
Does NOT import universe_ohlcv (11M rows) — that's handled separately via Phase 3.
"""

import os
import sys
import json
import sqlite3
import requests

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

RAILWAY_API = "https://web-production-e3025.up.railway.app"
DB_PATH = os.path.join(PROJECT_ROOT, "data", "scanperfect.db")
SETUPS = ["dtss", "3-4db", "htf"]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def railway_get(path):
    """GET from Railway API, return parsed JSON or None."""
    try:
        r = requests.get(f"{RAILWAY_API}{path}", timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ✗ GET {path} failed: {e}")
        return None


def railway_query(sql, limit=10000):
    """Run a SELECT query against Railway's bulk query endpoint."""
    try:
        r = requests.post(f"{RAILWAY_API}/api/query/bulk",
                          json={"sql": sql, "limit": limit}, timeout=60)
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception as e:
        print(f"  ✗ Query failed: {e}")
        return []


def import_examples(db):
    """Import examples from all setups."""
    print("\n── Examples ──")
    total = 0
    for setup in SETUPS:
        data = railway_get(f"/api/examples/{setup}")
        if not data:
            continue
        examples = data.get("examples", [])
        if not examples:
            print(f"  {setup}: 0 examples")
            continue
        inserted = 0
        for ex in examples:
            try:
                db.execute(
                    "INSERT OR IGNORE INTO examples (setup_type, ticker, chart_date, entry_date) VALUES (?,?,?,?)",
                    (setup, ex["ticker"], ex["chartDate"], ex["entryDate"]),
                )
                if db.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
            except Exception as e:
                print(f"  ✗ {setup} {ex.get('ticker','?')}: {e}")
        db.commit()
        print(f"  {setup}: {inserted} inserted ({len(examples)} on Railway)")
        total += inserted
    return total


def import_rejected(db):
    """Import rejected signals from all setups."""
    print("\n── Rejected Signals ──")
    total = 0
    for setup in SETUPS:
        data = railway_get(f"/api/vetting/{setup}/rejected")
        if not data:
            continue
        rejected = data.get("rejected", [])
        if not rejected:
            print(f"  {setup}: 0 rejected")
            continue
        inserted = 0
        for r in rejected:
            try:
                db.execute(
                    "INSERT OR IGNORE INTO rejected_signals (setup_type, ticker, signal_date) VALUES (?,?,?)",
                    (setup, r["ticker"], r["signal_date"]),
                )
                if db.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
            except Exception as e:
                print(f"  ✗ {setup} {r.get('ticker','?')}: {e}")
        db.commit()
        print(f"  {setup}: {inserted} inserted ({len(rejected)} on Railway)")
        total += inserted
    return total


def import_pending(db):
    """Import pending examples from all setups."""
    print("\n── Pending Examples ──")
    total = 0
    for setup in SETUPS:
        data = railway_get(f"/api/pending/{setup}")
        if not data:
            continue
        pending = data.get("pending", [])
        if not pending:
            print(f"  {setup}: 0 pending")
            continue
        inserted = 0
        for p in pending:
            try:
                db.execute(
                    "INSERT OR IGNORE INTO pending_examples "
                    "(setup_type, ticker, signal_date, entry_date, status, ai_verdict, ai_reasoning, review_notes) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (setup, p["ticker"], p["signal_date"], p["entry_date"],
                     p.get("status", "pending"), p.get("ai_verdict"),
                     p.get("ai_reasoning"), p.get("review_notes")),
                )
                if db.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
            except Exception as e:
                print(f"  ✗ {setup} {p.get('ticker','?')}: {e}")
        db.commit()
        print(f"  {setup}: {inserted} inserted ({len(pending)} on Railway)")
        total += inserted
    return total


def import_earnings(db):
    """Import earnings dates."""
    print("\n── Earnings Dates ──")
    rows = railway_query("SELECT ticker, earnings_date FROM earnings_dates ORDER BY ticker, earnings_date")
    if not rows:
        print("  0 rows")
        return 0
    inserted = 0
    batch = []
    for r in rows:
        batch.append((r["ticker"], r["earnings_date"]))
    db.executemany(
        "INSERT OR IGNORE INTO earnings_dates (ticker, earnings_date) VALUES (?,?)",
        batch,
    )
    inserted = db.execute("SELECT changes()").fetchone()[0]
    db.commit()
    print(f"  {inserted} inserted ({len(rows)} on Railway)")
    return inserted


def verify(db):
    """Print summary of local DB contents."""
    print("\n── Local DB Summary ──")
    for table in ["examples", "rejected_signals", "pending_examples", "earnings_dates", "setups"]:
        count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")
    # Examples per setup
    rows = db.execute("SELECT setup_type, COUNT(*) as n FROM examples GROUP BY setup_type ORDER BY setup_type").fetchall()
    for r in rows:
        print(f"    {r['setup_type']}: {r['n']} examples")


def main():
    print("=" * 60)
    print("  Import from Railway → Local SQLite")
    print(f"  DB: {DB_PATH}")
    print("=" * 60)

    if not os.path.exists(DB_PATH):
        print(f"\n  ✗ DB not found at {DB_PATH}")
        print("  Run 'python -c \"import server\"' first to create it.")
        sys.exit(1)

    db = get_db()

    n_ex = import_examples(db)
    n_rej = import_rejected(db)
    n_pend = import_pending(db)
    n_earn = import_earnings(db)

    verify(db)
    db.close()

    print(f"\n{'=' * 60}")
    print(f"  Done — {n_ex} examples, {n_rej} rejected, {n_pend} pending, {n_earn} earnings")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
