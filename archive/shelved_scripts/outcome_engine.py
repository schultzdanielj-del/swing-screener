"""
Outcome Engine — Build TODO #3 from ANALYSIS_SYSTEM.md

For every signal (validated examples first, then historical backtest signals),
computes forward outcome data from entry:

  - MFE (Max Favorable Excursion) in ATR units at each bar
  - MAE (Max Adverse Excursion) in ATR units at each bar
  - Close-to-close P&L in ATR units at each bar
  - Bar-by-bar H/L/C relative to entry price (in ATR units)
  - Running max high / running min low (for trailing stop sim)

All values normalized by ATR at entry (ATR14 on scan bar = day before entry).

The Management Optimizer (#4) will sweep every stop/target/trail/time
combination as simple array math against this precomputed matrix.

Usage:
  from scripts.outcome_engine import OutcomeEngine

  engine = OutcomeEngine()

  # Compute outcomes for all examples of a setup type
  results = engine.compute_for_examples("3-4db")

  # Compute outcomes for all backtest signals
  results = engine.compute_for_backtest_signals("3-4db")

  # Compute for a single ticker/date
  result = engine.compute_single("AAPL", "2024-05-22", direction="short")

  # Store results to DB
  engine.store_outcomes(results)
"""

import requests
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional


DEFAULT_API = "https://web-production-e3025.up.railway.app"
DEFAULT_FORWARD_BARS = 60  # Configurable per setup if needed


@dataclass
class OutcomeRow:
    """Outcome data for a single signal at a single forward bar."""
    bar: int              # Forward bar number (1 = entry day, 2 = day after, etc.)
    date: str             # Calendar date of this bar
    open: float           # Raw open price
    high: float           # Raw high price
    low: float            # Raw low price
    close: float          # Raw close price
    volume: int           # Raw volume
    # Relative to entry (in ATR units, sign-adjusted for direction)
    open_vs_entry: float  # (open - entry) / atr, flipped for shorts
    high_vs_entry: float  # (high - entry) / atr
    low_vs_entry: float   # (low - entry) / atr
    close_vs_entry: float # (close - entry) / atr = P&L at this bar
    # Running extremes (in ATR units, sign-adjusted)
    mfe: float            # Max Favorable Excursion from entry through this bar
    mae: float            # Max Adverse Excursion from entry through this bar
    # Running price extremes (raw)
    running_best: float   # Best price seen so far (lowest low for shorts, highest high for longs)
    running_worst: float  # Worst price seen so far (highest high for shorts, lowest low for longs)


@dataclass
class SignalOutcome:
    """Complete outcome data for one signal."""
    ticker: str
    entry_date: str
    setup_type: str
    direction: str        # "long" or "short"
    entry_price: float    # Open of entry bar
    scan_bar_atr: float   # ATR14 on scan bar (day before entry)
    scan_bar_close: float # Close on scan bar
    bars_available: int   # How many forward bars of data exist
    rows: list[OutcomeRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization / DB storage."""
        return {
            "ticker": self.ticker,
            "entry_date": self.entry_date,
            "setup_type": self.setup_type,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "scan_bar_atr": self.scan_bar_atr,
            "scan_bar_close": self.scan_bar_close,
            "bars_available": self.bars_available,
            "rows": [
                {
                    "bar": r.bar, "date": r.date,
                    "open": r.open, "high": r.high, "low": r.low,
                    "close": r.close, "volume": r.volume,
                    "open_vs_entry": round(r.open_vs_entry, 4),
                    "high_vs_entry": round(r.high_vs_entry, 4),
                    "low_vs_entry": round(r.low_vs_entry, 4),
                    "close_vs_entry": round(r.close_vs_entry, 4),
                    "mfe": round(r.mfe, 4),
                    "mae": round(r.mae, 4),
                    "running_best": round(r.running_best, 4),
                    "running_worst": round(r.running_worst, 4),
                }
                for r in self.rows
            ]
        }

    def summary(self) -> dict:
        """Quick summary stats for this signal."""
        if not self.rows:
            return {"ticker": self.ticker, "entry_date": self.entry_date, "bars": 0}
        final = self.rows[-1]
        peak_mfe = max(r.mfe for r in self.rows)
        peak_mae = min(r.mae for r in self.rows)  # Most negative = worst
        return {
            "ticker": self.ticker,
            "entry_date": self.entry_date,
            "direction": self.direction,
            "entry_price": round(self.entry_price, 2),
            "atr": round(self.scan_bar_atr, 2),
            "bars": self.bars_available,
            "peak_mfe_atr": round(peak_mfe, 2),
            "peak_mae_atr": round(peak_mae, 2),
            "final_pl_atr": round(final.close_vs_entry, 2),
        }


# ==========================================================
# Setup type configuration
# ==========================================================

SETUP_CONFIGS = {
    "3-4db": {
        "direction": "short",
        "forward_bars": 60,
    },
    "dtss": {
        "direction": "short",
        "forward_bars": 60,
    },
    "htf": {
        "direction": "long",
        "forward_bars": 60,
    },
}


class OutcomeEngine:
    """Computes forward outcome matrices for trading signals."""

    def __init__(self, api_base: str = DEFAULT_API):
        self.api_base = api_base.rstrip("/")

    # ----------------------------------------------------------
    # Data fetching (same pattern as ProfilingEngine)
    # ----------------------------------------------------------

    def _query(self, sql: str) -> list[dict]:
        """Run read-only SQL against the Railway DB."""
        resp = requests.post(
            f"{self.api_base}/api/query",
            json={"sql": sql}, timeout=30
        )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"DB query error: {data['error']}")
        return data.get("results", [])

    def _fetch_ohlcv(self, ticker: str, start_date: str,
                     forward_bars: int) -> pd.DataFrame:
        """Fetch OHLCV data starting from start_date going forward.
        Uses bulk endpoint with enough lookback to cover forward window."""
        try:
            # Fetch extra to ensure we get enough forward data
            # We get data ending far in the future (or latest available)
            resp = requests.get(
                f"{self.api_base}/api/ohlcv/bulk/{ticker}",
                params={"lookback": 1500},
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                rows = data.get("results", [])
                if rows:
                    df = pd.DataFrame(rows)
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.sort_values('date').reset_index(drop=True)
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                    return df
        except Exception:
            pass

        # Fallback: chunked query approach
        all_rows = []
        current_end = "2030-01-01"  # Far future to get all data
        remaining = 1500
        for _ in range(15):
            if remaining <= 0:
                break
            batch_size = min(remaining, 100)
            rows = self._query(
                f"SELECT date, open, high, low, close, volume "
                f"FROM universe_ohlcv "
                f"WHERE ticker='{ticker}' AND date<='{current_end}' "
                f"ORDER BY date DESC LIMIT {batch_size}"
            )
            if not rows:
                break
            all_rows.extend(rows)
            remaining -= len(rows)
            if len(rows) < batch_size:
                break
            from datetime import datetime, timedelta
            dt = datetime.strptime(rows[-1]['date'], '%Y-%m-%d') - timedelta(days=1)
            current_end = dt.strftime('%Y-%m-%d')

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        df = df.drop_duplicates(subset=['date'])
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df

    # ----------------------------------------------------------
    # ATR computation (SMA-based, matches TC2000 PCF semantics)
    # ----------------------------------------------------------

    @staticmethod
    def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """ATR = SMA of True Range (TC2000 style, not Wilder's)."""
        high = df['high']
        low = df['low']
        prev_close = df['close'].shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    # ----------------------------------------------------------
    # Core computation
    # ----------------------------------------------------------

    def compute_single(self, ticker: str, entry_date: str,
                       direction: str = "short",
                       forward_bars: int = DEFAULT_FORWARD_BARS) -> Optional[SignalOutcome]:
        """Compute outcome data for a single signal.

        Args:
            ticker: Stock ticker
            entry_date: The date the trade is entered (at the open)
            direction: "long" or "short"
            forward_bars: How many bars forward to compute

        Returns:
            SignalOutcome or None if data unavailable
        """
        # Fetch full OHLCV history for the ticker
        df = self._fetch_ohlcv(ticker, entry_date, forward_bars)
        if df.empty:
            print(f"  WARNING: No OHLCV data for {ticker}")
            return None

        entry_dt = pd.Timestamp(entry_date)

        # Find entry bar index
        entry_idx = df.index[df['date'] == entry_dt]
        if len(entry_idx) == 0:
            # entry_date might be a weekend/holiday — find next trading day
            mask = df['date'] >= entry_dt
            if mask.sum() == 0:
                print(f"  WARNING: No data on or after {entry_date} for {ticker}")
                return None
            entry_idx = df.index[mask][0]
        else:
            entry_idx = entry_idx[0]

        # Scan bar = trading day before entry
        if entry_idx == 0:
            print(f"  WARNING: No data before entry for {ticker} on {entry_date}")
            return None
        scan_idx = entry_idx - 1

        # Entry price = open of entry bar
        entry_price = df.loc[entry_idx, 'open']
        scan_bar_close = df.loc[scan_idx, 'close']

        # ATR14 on scan bar
        atr_series = self._compute_atr(df, 14)
        scan_bar_atr = atr_series.iloc[scan_idx]
        if pd.isna(scan_bar_atr) or scan_bar_atr <= 0:
            # Fallback: use ATR from closest available bar
            valid_atr = atr_series.dropna()
            if len(valid_atr) == 0 or valid_atr.iloc[-1] <= 0:
                print(f"  WARNING: No valid ATR for {ticker} on {entry_date}")
                return None
            scan_bar_atr = valid_atr.loc[:scan_idx].iloc[-1]

        # Forward slice: entry bar through forward_bars
        forward_df = df.iloc[entry_idx:entry_idx + forward_bars].copy()
        bars_available = len(forward_df)

        if bars_available == 0:
            print(f"  WARNING: No forward data for {ticker} from {entry_date}")
            return None

        # Direction multiplier: +1 for longs (profit = price goes up),
        # -1 for shorts (profit = price goes down)
        mult = 1.0 if direction == "long" else -1.0

        # Build outcome rows
        rows = []
        running_mfe = 0.0
        running_mae = 0.0
        # For longs: best = highest high, worst = lowest low
        # For shorts: best = lowest low, worst = highest high
        if direction == "long":
            running_best_price = forward_df.iloc[0]['low']   # Will track highest high
            running_worst_price = forward_df.iloc[0]['high']  # Will track lowest low
        else:
            running_best_price = forward_df.iloc[0]['high']  # Will track lowest low
            running_worst_price = forward_df.iloc[0]['low']   # Will track highest high

        for i, (idx, bar) in enumerate(forward_df.iterrows()):
            bar_num = i + 1  # 1-indexed

            # Raw relative values in ATR units, sign-adjusted
            o_vs = mult * (bar['open'] - entry_price) / scan_bar_atr
            h_vs = mult * (bar['high'] - entry_price) / scan_bar_atr
            l_vs = mult * (bar['low'] - entry_price) / scan_bar_atr
            c_vs = mult * (bar['close'] - entry_price) / scan_bar_atr

            # For favorable excursion:
            #   Long: best intrabar = highest point = h_vs
            #   Short: best intrabar = lowest point = l_vs (which after mult flip is h_vs equivalent)
            if direction == "long":
                bar_best = h_vs   # Highest point is most favorable for longs
                bar_worst = l_vs  # Lowest point is most adverse for longs
                running_best_price = max(running_best_price, bar['high'])
                running_worst_price = min(running_worst_price, bar['low'])
            else:
                bar_best = -1 * (bar['low'] - entry_price) / scan_bar_atr  # Lowest price = best for shorts
                bar_worst = -1 * (bar['high'] - entry_price) / scan_bar_atr  # Highest price = worst for shorts
                # Wait — let me be more careful. For shorts:
                # Favorable = price drops below entry. MFE = max drop.
                # (entry_price - low) / atr = favorable distance
                bar_best = (entry_price - bar['low']) / scan_bar_atr
                bar_worst = -1 * (bar['high'] - entry_price) / scan_bar_atr  # Negative = adverse
                running_best_price = min(running_best_price, bar['low'])
                running_worst_price = max(running_worst_price, bar['high'])

            running_mfe = max(running_mfe, bar_best)
            running_mae = min(running_mae, bar_worst)

            rows.append(OutcomeRow(
                bar=bar_num,
                date=bar['date'].strftime('%Y-%m-%d'),
                open=round(bar['open'], 4),
                high=round(bar['high'], 4),
                low=round(bar['low'], 4),
                close=round(bar['close'], 4),
                volume=int(bar['volume']),
                open_vs_entry=round(o_vs, 4),
                high_vs_entry=round(h_vs, 4),
                low_vs_entry=round(l_vs, 4),
                close_vs_entry=round(c_vs, 4),
                mfe=round(running_mfe, 4),
                mae=round(running_mae, 4),
                running_best=round(running_best_price, 4),
                running_worst=round(running_worst_price, 4),
            ))

        return SignalOutcome(
            ticker=ticker,
            entry_date=entry_date,
            setup_type="",  # Set by caller
            direction=direction,
            entry_price=entry_price,
            scan_bar_atr=scan_bar_atr,
            scan_bar_close=scan_bar_close,
            bars_available=bars_available,
            rows=rows,
        )

    # ----------------------------------------------------------
    # Batch computation for examples
    # ----------------------------------------------------------

    def compute_for_examples(self, setup_type: str,
                             forward_bars: Optional[int] = None) -> list[SignalOutcome]:
        """Compute outcomes for all validated examples of a setup type."""
        config = SETUP_CONFIGS.get(setup_type, {})
        direction = config.get("direction", "short")
        if forward_bars is None:
            forward_bars = config.get("forward_bars", DEFAULT_FORWARD_BARS)

        # Get examples from DB
        examples = self._query(
            f"SELECT ticker, entry_date FROM examples "
            f"WHERE setup_type='{setup_type}' ORDER BY ticker"
        )

        if not examples:
            print(f"No examples found for setup type '{setup_type}'")
            return []

        print(f"Computing outcomes for {len(examples)} {setup_type} examples "
              f"({direction}, {forward_bars} bars forward)...")

        results = []
        for i, ex in enumerate(examples):
            ticker = ex['ticker']
            entry_date = ex['entry_date']
            print(f"  [{i+1}/{len(examples)}] {ticker} @ {entry_date}...", end=" ")

            outcome = self.compute_single(ticker, entry_date, direction, forward_bars)
            if outcome:
                outcome.setup_type = setup_type
                results.append(outcome)
                print(f"OK — {outcome.bars_available} bars, "
                      f"MFE={max(r.mfe for r in outcome.rows):.1f}R, "
                      f"MAE={min(r.mae for r in outcome.rows):.1f}R")
            else:
                print("SKIPPED")

        print(f"\nDone: {len(results)}/{len(examples)} computed successfully")
        return results

    # ----------------------------------------------------------
    # Batch computation for backtest signals
    # ----------------------------------------------------------

    def compute_for_backtest_signals(self, setup_type: str,
                                     table: str = "scan_backtest_clean",
                                     forward_bars: Optional[int] = None,
                                     limit: Optional[int] = None) -> list[SignalOutcome]:
        """Compute outcomes for historical backtest signals.

        Args:
            setup_type: Setup type for config lookup
            table: Which backtest table to read from
            forward_bars: Override forward bar count
            limit: Max signals to process (None = all)
        """
        config = SETUP_CONFIGS.get(setup_type, {})
        direction = config.get("direction", "short")
        if forward_bars is None:
            forward_bars = config.get("forward_bars", DEFAULT_FORWARD_BARS)

        limit_clause = f"LIMIT {limit}" if limit else ""
        signals = self._query(
            f"SELECT ticker, date FROM {table} "
            f"ORDER BY date, ticker {limit_clause}"
        )

        if not signals:
            print(f"No backtest signals found in {table}")
            return []

        print(f"Computing outcomes for {len(signals)} backtest signals "
              f"({direction}, {forward_bars} bars forward)...")

        results = []
        errors = 0
        for i, sig in enumerate(signals):
            ticker = sig['ticker']
            # Backtest table stores scan date; entry = next trading day
            scan_date = sig['date']

            # Find entry date (next trading day after scan date)
            entry_rows = self._query(
                f"SELECT date FROM universe_ohlcv "
                f"WHERE ticker='{ticker}' AND date>'{scan_date}' "
                f"ORDER BY date ASC LIMIT 1"
            )
            if not entry_rows:
                errors += 1
                continue
            entry_date = entry_rows[0]['date']

            if (i + 1) % 50 == 0 or i == 0:
                print(f"  [{i+1}/{len(signals)}] {ticker} @ {entry_date}...")

            outcome = self.compute_single(ticker, entry_date, direction, forward_bars)
            if outcome:
                outcome.setup_type = setup_type
                results.append(outcome)
            else:
                errors += 1

        print(f"\nDone: {len(results)}/{len(signals)} computed, {errors} errors")
        return results

    # ----------------------------------------------------------
    # DB storage
    # ----------------------------------------------------------

    def store_outcomes(self, outcomes: list[SignalOutcome],
                       source: str = "examples") -> dict:
        """Store computed outcomes to Railway DB via API.

        Creates/updates the signal_outcomes table.
        Returns summary stats.
        """
        if not outcomes:
            return {"stored": 0}

        # Build SQL for table creation and inserts
        # We'll use the /api/query endpoint for DDL and the bulk approach for data
        create_sql = """
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            setup_type TEXT NOT NULL,
            direction TEXT NOT NULL,
            source TEXT NOT NULL,
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
        )
        """

        # Create table
        try:
            self._query(create_sql)
        except Exception as e:
            print(f"Table creation note: {e}")

        stored = 0
        for outcome in outcomes:
            # Delete existing rows for this signal (upsert)
            try:
                self._query(
                    f"DELETE FROM signal_outcomes "
                    f"WHERE ticker='{outcome.ticker}' "
                    f"AND entry_date='{outcome.entry_date}' "
                    f"AND setup_type='{outcome.setup_type}'"
                )
            except Exception:
                pass

            # Insert rows in batches
            for row in outcome.rows:
                try:
                    self._query(
                        f"INSERT INTO signal_outcomes "
                        f"(ticker, entry_date, setup_type, direction, source, "
                        f"entry_price, scan_bar_atr, scan_bar_close, bars_available, "
                        f"bar_num, bar_date, bar_open, bar_high, bar_low, bar_close, bar_volume, "
                        f"open_vs_entry, high_vs_entry, low_vs_entry, close_vs_entry, "
                        f"mfe, mae, running_best, running_worst) "
                        f"VALUES ('{outcome.ticker}', '{outcome.entry_date}', "
                        f"'{outcome.setup_type}', '{outcome.direction}', '{source}', "
                        f"{outcome.entry_price}, {outcome.scan_bar_atr}, {outcome.scan_bar_close}, "
                        f"{outcome.bars_available}, "
                        f"{row.bar}, '{row.date}', {row.open}, {row.high}, {row.low}, "
                        f"{row.close}, {row.volume}, "
                        f"{row.open_vs_entry}, {row.high_vs_entry}, {row.low_vs_entry}, "
                        f"{row.close_vs_entry}, {row.mfe}, {row.mae}, "
                        f"{row.running_best}, {row.running_worst})"
                    )
                    stored += 1
                except Exception as e:
                    print(f"  Insert error for {outcome.ticker} bar {row.bar}: {e}")

        print(f"Stored {stored} outcome rows for {len(outcomes)} signals")
        return {"stored": stored, "signals": len(outcomes)}

    # ----------------------------------------------------------
    # Retrieval helpers
    # ----------------------------------------------------------

    def get_outcomes(self, setup_type: str,
                     source: Optional[str] = None) -> list[dict]:
        """Retrieve stored outcomes from DB."""
        where = f"WHERE setup_type='{setup_type}'"
        if source:
            where += f" AND source='{source}'"
        rows = self._query(
            f"SELECT * FROM signal_outcomes {where} "
            f"ORDER BY ticker, entry_date, bar_num"
        )
        return rows

    def get_outcome_summary(self, setup_type: str,
                            source: Optional[str] = None) -> list[dict]:
        """Get per-signal summary stats from stored outcomes."""
        where = f"WHERE setup_type='{setup_type}'"
        if source:
            where += f" AND source='{source}'"
        rows = self._query(
            f"SELECT ticker, entry_date, direction, entry_price, scan_bar_atr, "
            f"bars_available, MAX(mfe) as peak_mfe, MIN(mae) as peak_mae, "
            f"(SELECT close_vs_entry FROM signal_outcomes s2 "
            f" WHERE s2.ticker=s1.ticker AND s2.entry_date=s1.entry_date "
            f" AND s2.setup_type=s1.setup_type AND s2.bar_num=s1.bars_available) as final_pl "
            f"FROM signal_outcomes s1 {where} "
            f"GROUP BY ticker, entry_date, setup_type "
            f"ORDER BY ticker, entry_date"
        )
        return rows

    # ----------------------------------------------------------
    # Quick analysis helpers
    # ----------------------------------------------------------

    @staticmethod
    def outcomes_to_matrix(outcomes: list[SignalOutcome]) -> dict:
        """Convert list of outcomes to numpy arrays for fast management optimization.

        Returns dict with arrays indexed [signal_index, bar_index]:
          - mfe: shape (n_signals, max_bars)
          - mae: shape (n_signals, max_bars)
          - close_pl: shape (n_signals, max_bars)
          - high_vs_entry: shape (n_signals, max_bars)
          - low_vs_entry: shape (n_signals, max_bars)
          - labels: list of (ticker, entry_date) tuples
        """
        if not outcomes:
            return {}

        max_bars = max(o.bars_available for o in outcomes)
        n = len(outcomes)

        mfe = np.full((n, max_bars), np.nan)
        mae = np.full((n, max_bars), np.nan)
        close_pl = np.full((n, max_bars), np.nan)
        high_vs = np.full((n, max_bars), np.nan)
        low_vs = np.full((n, max_bars), np.nan)
        labels = []

        for i, outcome in enumerate(outcomes):
            labels.append((outcome.ticker, outcome.entry_date))
            for row in outcome.rows:
                j = row.bar - 1  # 0-indexed
                if j < max_bars:
                    mfe[i, j] = row.mfe
                    mae[i, j] = row.mae
                    close_pl[i, j] = row.close_vs_entry
                    high_vs[i, j] = row.high_vs_entry
                    low_vs[i, j] = row.low_vs_entry

        return {
            "mfe": mfe,
            "mae": mae,
            "close_pl": close_pl,
            "high_vs_entry": high_vs,
            "low_vs_entry": low_vs,
            "labels": labels,
            "n_signals": n,
            "max_bars": max_bars,
        }

    @staticmethod
    def print_summary(outcomes: list[SignalOutcome]):
        """Print a formatted summary table of outcomes."""
        if not outcomes:
            print("No outcomes to display")
            return

        print(f"\n{'Ticker':<8} {'Entry':<12} {'Dir':<6} {'Entry$':<9} {'ATR':<7} "
              f"{'Bars':<6} {'MFE(R)':<8} {'MAE(R)':<8} {'Final(R)':<9}")
        print("-" * 85)

        mfes = []
        maes = []
        finals = []

        for o in outcomes:
            s = o.summary()
            peak_mfe = s['peak_mfe_atr']
            peak_mae = s['peak_mae_atr']
            final_pl = s['final_pl_atr']
            mfes.append(peak_mfe)
            maes.append(peak_mae)
            finals.append(final_pl)

            print(f"{s['ticker']:<8} {s['entry_date']:<12} {s['direction']:<6} "
                  f"${s['entry_price']:<8.2f} {s['atr']:<6.2f} "
                  f"{s['bars']:<6} {peak_mfe:<8.2f} {peak_mae:<8.2f} {final_pl:<9.2f}")

        print("-" * 85)
        print(f"{'AVG':<8} {'':12} {'':6} {'':9} {'':7} "
              f"{'':6} {np.mean(mfes):<8.2f} {np.mean(maes):<8.2f} {np.mean(finals):<9.2f}")
        print(f"{'MEDIAN':<8} {'':12} {'':6} {'':9} {'':7} "
              f"{'':6} {np.median(mfes):<8.2f} {np.median(maes):<8.2f} {np.median(finals):<9.2f}")


# ==========================================================
# CLI usage
# ==========================================================

if __name__ == "__main__":
    import sys

    setup = sys.argv[1] if len(sys.argv) > 1 else "3-4db"
    mode = sys.argv[2] if len(sys.argv) > 2 else "examples"

    engine = OutcomeEngine()

    if mode == "examples":
        results = engine.compute_for_examples(setup)
    elif mode == "backtest":
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
        results = engine.compute_for_backtest_signals(setup, limit=limit)
    elif mode == "single":
        # outcome_engine.py TICKER ENTRY_DATE DIRECTION
        ticker = sys.argv[2]
        entry_date = sys.argv[3]
        direction = sys.argv[4] if len(sys.argv) > 4 else "short"
        result = engine.compute_single(ticker, entry_date, direction)
        if result:
            result.setup_type = setup
            results = [result]
        else:
            results = []
    else:
        print(f"Unknown mode: {mode}")
        sys.exit(1)

    if results:
        OutcomeEngine.print_summary(results)

        if "--store" in sys.argv:
            engine.store_outcomes(results, source=mode)
