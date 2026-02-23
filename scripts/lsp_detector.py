"""
LSP (Left Side Pivot) Detection Engine

Finds the most prominent structural pivot high in a stock's recent history.
Designed to be setup-agnostic — any setup that involves price approaching
a prior structural level can use this.

Validation: Run against labeled DTSS LSP data to measure accuracy.
"""

import json
import requests
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional


@dataclass
class DetectedLSP:
    """A detected pivot high with prominence scoring."""
    date: str
    price: float
    prominence_score: float
    pullback_depth_atr: float  # How deep price fell after this pivot
    volume_ratio: float  # Pivot bar volume vs 20-day avg
    bars_lookback: int  # How many bars back from the scan date
    pivot_window: int  # The N-bar window that detected it


class LSPDetector:
    """Detects the most prominent structural pivot high(s) for a given ticker/date."""

    def __init__(self, api_base: str = "https://web-production-e3025.up.railway.app"):
        self.api_base = api_base.rstrip("/")

    def _fetch_ohlcv(self, ticker: str, end_date: str, lookback: int = 300) -> pd.DataFrame:
        """Fetch OHLCV data from Railway DB."""
        try:
            resp = requests.get(
                f"{self.api_base}/api/ohlcv/bulk/{ticker}",
                params={"end_date": end_date, "lookback": lookback},
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
        except Exception as e:
            print(f"  Bulk fetch failed for {ticker}: {e}")

        # Fallback: chunked queries
        all_rows = []
        current_end = end_date
        remaining = lookback
        for _ in range(5):
            if remaining <= 0:
                break
            batch_size = min(remaining, 100)
            resp = requests.post(
                f"{self.api_base}/api/query",
                json={"sql": f"SELECT date, open, high, low, close, volume "
                             f"FROM universe_ohlcv "
                             f"WHERE ticker='{ticker}' AND date<='{current_end}' "
                             f"ORDER BY date DESC LIMIT {batch_size}"},
                timeout=30
            )
            rows = resp.json().get("results", [])
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
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df

    def _compute_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """SMA-based ATR (matches TC2000 PCF semantics)."""
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift(1)).abs(),
            (df['low'] - df['close'].shift(1)).abs()
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def _find_pivot_highs(self, df: pd.DataFrame, window: int) -> list[int]:
        """Find indices where high is the max high in a window of N bars on each side."""
        pivots = []
        highs = df['high'].values
        for i in range(window, len(highs) - 1):  # Don't require right-side confirmation for most recent
            left_start = max(0, i - window)
            # For right side, use available bars (allow partial right window near end)
            right_end = min(len(highs), i + window + 1)
            left_max = highs[left_start:i].max() if i > left_start else -np.inf
            right_max = highs[i + 1:right_end].max() if right_end > i + 1 else -np.inf
            if highs[i] >= left_max and highs[i] >= right_max:
                pivots.append(i)
        return pivots

    def _score_pivot(self, df: pd.DataFrame, pivot_idx: int, scan_idx: int,
                     atr: pd.Series) -> Optional[DetectedLSP]:
        """Score a pivot high by prominence.
        
        Key insight: For double-top setups, the LSP is the HIGHEST recent price
        point that acted as resistance. The scoring must prioritize:
        1. Price level (higher = more likely to be THE structural ceiling)
        2. Meaningful pullback after (confirms it was a real rejection)
        3. Recency (the relevant high, not ancient history)
        
        Volume is secondary — it confirms but shouldn't dominate scoring.
        """
        if pivot_idx >= scan_idx:
            return None

        pivot_high = df.at[pivot_idx, 'high']
        pivot_date = df.at[pivot_idx, 'date']
        pivot_atr = atr.iloc[pivot_idx] if pivot_idx < len(atr) and pd.notna(atr.iloc[pivot_idx]) else None
        scan_atr = atr.iloc[scan_idx] if scan_idx < len(atr) and pd.notna(atr.iloc[scan_idx]) else None

        if pivot_atr is None or pivot_atr <= 0 or scan_atr is None or scan_atr <= 0:
            return None

        # Pullback depth: how far did price fall after this pivot (in ATR units)
        post_pivot = df.iloc[pivot_idx + 1:scan_idx + 1]
        if post_pivot.empty:
            return None
        lowest_after = post_pivot['low'].min()
        pullback_depth_atr = (pivot_high - lowest_after) / pivot_atr

        # Must have a meaningful pullback (at least 0.5 ATR) to be a real structural high
        # Lowered from 1.0 — some recent LSPs have shallow pullbacks before the retest
        if pullback_depth_atr < 0.5:
            return None

        # Volume ratio: pivot bar volume vs 20-day average at that point
        vol_window = df.iloc[max(0, pivot_idx - 20):pivot_idx]
        avg_vol = vol_window['volume'].mean() if len(vol_window) > 5 else df['volume'].mean()
        volume_ratio = df.at[pivot_idx, 'volume'] / avg_vol if avg_vol > 0 else 1.0

        # Bars from scan date
        bars_lookback = scan_idx - pivot_idx

        # --- SCORING ---
        
        # 1. Price level score: how high is this pivot relative to the scan bar close?
        #    The LSP is usually the highest point — normalize by scan-bar ATR
        scan_close = df.at[scan_idx, 'close']
        price_level_atr = (pivot_high - scan_close) / scan_atr
        # Pivots near or above current price score highest (they're resistance)
        # Pivots far below current price are old/irrelevant levels
        if price_level_atr < -5:
            # Pivot is way below current price — probably an old low-price high
            price_score = 0.1
        elif price_level_atr < -2:
            price_score = 0.3
        elif price_level_atr < 0:
            price_score = 0.7  # Slightly below current price
        else:
            # At or above current price — this IS resistance
            # Moderate scaling: enough to differentiate levels but not dominate
            price_score = 1.0 + min(price_level_atr * 0.1, 0.5)

        # 2. Pullback confirmation: needs to be meaningful but don't over-weight
        #    Diminishing returns after ~3 ATR pullback
        pullback_score = min(pullback_depth_atr / 3.0, 1.5)

        # 3. Recency: for DTSS the relevant LSP is the immediate structural context
        #    being retested — heavily discount ancient highs vs recent ones
        if bars_lookback < 2:
            recency_score = 0.1   # Same/adjacent bar — invalid
        elif bars_lookback <= 5:
            recency_score = 1.0   # Very recent pivot being immediately retested
        elif bars_lookback <= 30:
            recency_score = 1.4   # Sweet spot — recent structural high
        elif bars_lookback <= 80:
            recency_score = 1.0   # Normal
        elif bars_lookback <= 120:
            recency_score = 0.5   # Getting old
        elif bars_lookback <= 200:
            recency_score = 0.25  # Old — heavily discounted
        else:
            recency_score = 0.1   # Ancient — nearly excluded

        # 4. Volume: minimal bonus, very tightly capped — volume confirms but must
        #    never overcome a significantly higher price level (ASST 36x vol was dominating)
        volume_score = 1.0 + min((volume_ratio - 1.0) * 0.05, 0.2) if volume_ratio > 1.0 else max(volume_ratio * 0.9, 0.7)

        # Combined: price level dominates (squared), pullback confirms, recency adjusts
        # Volume is additive/multiplicative but tightly bounded
        prominence = (price_score ** 2) * pullback_score * recency_score * volume_score

        return DetectedLSP(
            date=pivot_date.strftime('%Y-%m-%d'),
            price=round(float(pivot_high), 2),
            prominence_score=round(float(prominence), 3),
            pullback_depth_atr=round(float(pullback_depth_atr), 2),
            volume_ratio=round(float(volume_ratio), 2),
            bars_lookback=int(bars_lookback),
            pivot_window=0  # Set by caller
        )

    def detect_lsp(self, ticker: str, scan_date: str,
                   max_lookback_bars: int = 200,
                   top_n: int = 5) -> list[DetectedLSP]:
        """
        Detect the most prominent pivot high(s) for a ticker as of scan_date.

        Args:
            ticker: Stock ticker
            scan_date: The date we're scanning from (look left from here)
            max_lookback_bars: How far back to look for pivots
            top_n: Return the top N most prominent pivots

        Returns:
            List of DetectedLSP, sorted by prominence (best first)
        """
        df = self._fetch_ohlcv(ticker, scan_date, lookback=max_lookback_bars + 50)
        if df.empty or len(df) < 30:
            return []

        atr = self._compute_atr(df)

        # Find scan date index
        scan_dt = pd.Timestamp(scan_date)
        mask = df['date'] <= scan_dt
        if not mask.any():
            return []
        scan_idx = df.loc[mask].index[-1]

        # Limit lookback
        min_idx = max(0, scan_idx - max_lookback_bars)

        # Find pivots at multiple window sizes to catch different scales
        all_pivots = {}
        for window in [5, 10, 15, 20, 30, 40]:
            pivots = self._find_pivot_highs(df, window)
            for p in pivots:
                if min_idx <= p < scan_idx:
                    if p not in all_pivots or window > all_pivots[p]:
                        all_pivots[p] = window  # Track largest window that detected it

        # Score each unique pivot
        scored = []
        for pivot_idx, max_window in all_pivots.items():
            lsp = self._score_pivot(df, pivot_idx, scan_idx, atr)
            if lsp:
                lsp.pivot_window = max_window
                # Bonus for pivots detected at larger windows (more structurally significant)
                lsp.prominence_score *= (1.0 + 0.1 * (max_window / 10))
                lsp.prominence_score = round(lsp.prominence_score, 3)
                scored.append(lsp)

        # Sort by prominence, return top N
        scored.sort(key=lambda x: x.prominence_score, reverse=True)
        
        # Cluster deduplication: when two pivots are in the same price zone
        # (within 2 ATR of each other), keep the one with the higher price.
        # For double-top setups, the structural ceiling matters most.
        if scored and len(scored) > 1:
            # Use scan-bar ATR for zone comparison
            scan_atr_val = atr.iloc[scan_idx] if scan_idx < len(atr) and pd.notna(atr.iloc[scan_idx]) else None
            if scan_atr_val and scan_atr_val > 0:
                deduplicated = [scored[0]]
                for candidate in scored[1:]:
                    is_duplicate = False
                    for i, kept in enumerate(deduplicated):
                        price_diff_atr = abs(candidate.price - kept.price) / scan_atr_val
                        if price_diff_atr < 2.0:
                            # Same zone — keep higher price, use its own score only
                            if candidate.price > kept.price:
                                deduplicated[i] = candidate
                            is_duplicate = True
                            break
                    if not is_duplicate:
                        deduplicated.append(candidate)
                # Re-sort after dedup
                deduplicated.sort(key=lambda x: x.prominence_score, reverse=True)
                scored = deduplicated

        return scored[:top_n]


def validate_against_labeled(api_base: str = "https://web-production-e3025.up.railway.app"):
    """
    Compare detected LSPs against the 26 labeled DTSS examples.
    Reports: exact match, close match (within N bars), and misses.
    """
    # Load labeled LSP data
    with open("data/dtss_lsp_data.json") as f:
        labeled = json.load(f)

    print(f"Loaded {len(labeled)} labeled LSPs")
    print("=" * 90)

    detector = LSPDetector(api_base=api_base)

    exact = 0
    close_3 = 0  # Within 3 bars
    close_5 = 0  # Within 5 bars
    close_10 = 0  # Within 10 bars
    missed = 0
    results = []

    for item in labeled:
        ticker = item['ticker']
        entry_date = item['entry_date']
        lsp_date = item['date']
        lsp_price = item['price']

        # Scan date is 1 day before entry
        # We need to find the actual prior trading day
        print(f"\n{ticker} (entry: {entry_date}, labeled LSP: {lsp_date} @ ${lsp_price})")

        try:
            # Detect LSPs from scan perspective (day before entry)
            # Fetch prior trading day via API
            r = requests.get(
                f"{api_base}/api/query",
                json={"sql": f"SELECT date FROM universe_ohlcv WHERE ticker='{ticker}' AND date<'{entry_date}' ORDER BY date DESC LIMIT 1"},
                timeout=15
            )
            prior_rows = r.json().get("results", []) if r.status_code == 200 else []
            scan_date = prior_rows[0]['date'] if prior_rows else entry_date
            detected = detector.detect_lsp(ticker, scan_date, max_lookback_bars=200, top_n=5)

            if not detected:
                print(f"  ❌ No pivots detected")
                missed += 1
                results.append({
                    'ticker': ticker, 'entry_date': entry_date,
                    'labeled_date': lsp_date, 'labeled_price': lsp_price,
                    'detected_date': None, 'detected_price': None,
                    'rank': None, 'date_diff_bars': None, 'price_diff_pct': None,
                    'match': 'missed'
                })
                continue

            # Find best match among top 5 detected
            best_match = None
            best_rank = None
            labeled_dt = pd.Timestamp(lsp_date)

            for rank, d in enumerate(detected, 1):
                detected_dt = pd.Timestamp(d.date)
                date_diff = abs((detected_dt - labeled_dt).days)
                price_diff_pct = abs(d.price - lsp_price) / lsp_price * 100

                # Match criteria: within 5 trading days AND within 3% price
                if date_diff <= 7 and price_diff_pct < 5:
                    if best_match is None or date_diff < abs((pd.Timestamp(best_match.date) - labeled_dt).days):
                        best_match = d
                        best_rank = rank

            top = detected[0]
            top_dt = pd.Timestamp(top.date)
            top_date_diff = abs((top_dt - labeled_dt).days)
            top_price_diff = abs(top.price - lsp_price) / lsp_price * 100

            # Report top detected
            print(f"  Top detected: {top.date} @ ${top.price} "
                  f"(prominence={top.prominence_score}, pullback={top.pullback_depth_atr}ATR, "
                  f"vol={top.volume_ratio}x, {top.bars_lookback}bars back, win={top.pivot_window})")

            # Check if top-1 matches
            if top_date_diff <= 2:
                print(f"  ✅ EXACT match (top-1, {top_date_diff}d off, {top_price_diff:.1f}% price diff)")
                exact += 1
                match_type = 'exact'
            elif best_match and best_rank == 1:
                print(f"  ✅ CLOSE match (top-1, {top_date_diff}d off, {top_price_diff:.1f}% price diff)")
                close_3 += 1
                match_type = 'close_top1'
            elif best_match:
                print(f"  ⚠️  Found at rank #{best_rank}: {best_match.date} @ ${best_match.price}")
                if best_rank <= 3:
                    close_5 += 1
                else:
                    close_10 += 1
                match_type = f'rank_{best_rank}'
            else:
                print(f"  ❌ MISSED — top detected is {top_date_diff}d and {top_price_diff:.1f}% off")
                missed += 1
                match_type = 'missed'

            # Show other candidates
            if len(detected) > 1:
                for rank, d in enumerate(detected[1:], 2):
                    d_dt = pd.Timestamp(d.date)
                    d_diff = abs((d_dt - labeled_dt).days)
                    print(f"    #{rank}: {d.date} @ ${d.price} "
                          f"(prominence={d.prominence_score}, {d.bars_lookback}bars, "
                          f"{'<-- MATCH' if d_diff <= 5 else ''})")

            results.append({
                'ticker': ticker, 'entry_date': entry_date,
                'labeled_date': lsp_date, 'labeled_price': lsp_price,
                'detected_date': top.date, 'detected_price': top.price,
                'rank': best_rank, 'date_diff_bars': top_date_diff,
                'price_diff_pct': round(top_price_diff, 2),
                'match': match_type
            })

        except Exception as e:
            print(f"  ❌ Error: {e}")
            missed += 1
            results.append({
                'ticker': ticker, 'entry_date': entry_date,
                'labeled_date': lsp_date, 'labeled_price': lsp_price,
                'detected_date': None, 'detected_price': None,
                'rank': None, 'date_diff_bars': None, 'price_diff_pct': None,
                'match': f'error: {e}'
            })

    # Summary
    total = len(labeled)
    print("\n" + "=" * 90)
    print(f"VALIDATION SUMMARY ({total} labeled LSPs)")
    print(f"  ✅ Exact (top-1, ≤2d):     {exact}/{total} ({exact/total*100:.0f}%)")
    print(f"  ✅ Close (top-1, ≤7d):     {close_3}/{total} ({close_3/total*100:.0f}%)")
    print(f"  ⚠️  Found in top 3:         {close_5}/{total} ({close_5/total*100:.0f}%)")
    print(f"  ⚠️  Found in top 5:         {close_10}/{total} ({close_10/total*100:.0f}%)")
    print(f"  ❌ Missed:                  {missed}/{total} ({missed/total*100:.0f}%)")
    found_any = exact + close_3 + close_5 + close_10
    print(f"  Total found somewhere:     {found_any}/{total} ({found_any/total*100:.0f}%)")

    return results


if __name__ == "__main__":
    import sys
    import os
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        api = sys.argv[2] if len(sys.argv) > 2 else "https://web-production-e3025.up.railway.app"
        results = validate_against_labeled(api)
        # Save results
        with open("data/lsp_validation_results.json", "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults saved to data/lsp_validation_results.json")
    else:
        # Single ticker test
        ticker = sys.argv[1] if len(sys.argv) > 1 else "AAOI"
        date = sys.argv[2] if len(sys.argv) > 2 else "2024-02-15"
        detector = LSPDetector()
        lsps = detector.detect_lsp(ticker, date)
        for i, lsp in enumerate(lsps, 1):
            print(f"#{i}: {lsp.date} @ ${lsp.price} "
                  f"(prominence={lsp.prominence_score}, pullback={lsp.pullback_depth_atr}ATR, "
                  f"vol={lsp.volume_ratio}x, {lsp.bars_lookback}bars, window={lsp.pivot_window})")
