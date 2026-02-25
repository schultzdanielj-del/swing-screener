"""
Test: Does the AVWAP anchored to the LSP bar get broken near the entry date
for all DTSS examples?

Hypothesis: All valid DTSS entries involve price breaking below the LSP AVWAP.
If true → mechanical entry trigger from daily data, no manual tagging needed.
"""

import json
import requests
import pandas as pd
import numpy as np

API = "https://web-production-e3025.up.railway.app"


def fetch_ohlcv(ticker, end_date, lookback=500):
    """Fetch OHLCV from Railway bulk endpoint."""
    r = requests.get(f"{API}/api/ohlcv/bulk/{ticker}", 
                     params={"end_date": end_date, "lookback": lookback})
    if r.status_code != 200:
        return None
    data = r.json()
    if "error" in data or not data.get("results"):
        return None
    
    df = pd.DataFrame(data["results"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df = df.sort_values("date").reset_index(drop=True)
    return df


def compute_avwap(df, anchor_idx):
    """
    Compute AVWAP from anchor_idx forward.
    AVWAP = cumsum(typical_price * volume) / cumsum(volume)
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tpv = typical * df["volume"]
    
    avwap = pd.Series(np.nan, index=df.index)
    cum_tpv = tpv.iloc[anchor_idx:].cumsum()
    cum_vol = df["volume"].iloc[anchor_idx:].cumsum()
    avwap.iloc[anchor_idx:] = cum_tpv / cum_vol
    return avwap


def compute_atr(df, period=14):
    """ATR14."""
    tr = pd.concat([
        df["high"] - df["low"],
        abs(df["high"] - df["close"].shift(1)),
        abs(df["low"] - df["close"].shift(1))
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def analyze_example(ticker, entry_date, lsp_date, lsp_price):
    """Analyze one example."""
    entry_dt = pd.Timestamp(entry_date)
    lsp_dt = pd.Timestamp(lsp_date)
    
    # Fetch enough data: end well after entry for forward path
    end = (entry_dt + pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    df = fetch_ohlcv(ticker, end, lookback=500)
    if df is None or len(df) == 0:
        return {"ticker": ticker, "entry_date": entry_date, "error": "no data"}
    
    # Find LSP bar
    lsp_mask = df["date"] == lsp_dt
    if not lsp_mask.any():
        diffs = abs(df["date"] - lsp_dt)
        nearest_idx = diffs.idxmin()
        if diffs.iloc[nearest_idx].days > 5:
            return {"ticker": ticker, "entry_date": entry_date, "error": f"LSP date {lsp_date} not in data"}
        anchor_idx = nearest_idx
    else:
        anchor_idx = df.index[lsp_mask][0]
    
    # Find entry bar
    entry_mask = df["date"] == entry_dt
    if not entry_mask.any():
        diffs = abs(df["date"] - entry_dt)
        nearest_idx = diffs.idxmin()
        if diffs.iloc[nearest_idx].days > 5:
            return {"ticker": ticker, "entry_date": entry_date, "error": f"entry date not in data"}
        entry_idx = nearest_idx
    else:
        entry_idx = df.index[entry_mask][0]
    
    # Compute AVWAP from LSP bar
    avwap = compute_avwap(df, anchor_idx)
    atr = compute_atr(df)
    
    avwap_at_entry = avwap.iloc[entry_idx]
    atr_at_scan = atr.iloc[entry_idx - 1] if entry_idx > 0 else np.nan
    
    result = {
        "ticker": ticker,
        "entry_date": entry_date,
        "lsp_date": lsp_date,
        "lsp_price": lsp_price,
        "lsp_bar_high": float(df.iloc[anchor_idx]["high"]),
        "avwap_at_entry": round(float(avwap_at_entry), 2) if not np.isnan(avwap_at_entry) else None,
        "entry_open": round(float(df.iloc[entry_idx]["open"]), 2),
        "entry_high": round(float(df.iloc[entry_idx]["high"]), 2),
        "entry_low": round(float(df.iloc[entry_idx]["low"]), 2),
        "entry_close": round(float(df.iloc[entry_idx]["close"]), 2),
        "atr": round(float(atr_at_scan), 2) if not np.isnan(atr_at_scan) else None,
    }
    
    if avwap_at_entry and not np.isnan(avwap_at_entry):
        # Key tests
        result["entry_close_below_avwap"] = bool(df.iloc[entry_idx]["close"] < avwap_at_entry)
        result["entry_low_below_avwap"] = bool(df.iloc[entry_idx]["low"] < avwap_at_entry)
        result["close_vs_avwap"] = round(float(df.iloc[entry_idx]["close"] - avwap_at_entry), 2)
        
        # Distance: how far is AVWAP below the LSP? (as % of LSP price)
        result["lsp_to_avwap_pct"] = round((lsp_price - avwap_at_entry) / lsp_price * 100, 2)
        
        # Distance in ATR
        if atr_at_scan and not np.isnan(atr_at_scan) and atr_at_scan > 0:
            result["lsp_to_avwap_atr"] = round((lsp_price - avwap_at_entry) / atr_at_scan, 2)
        
        # Bars between LSP and entry
        result["bars_lsp_to_entry"] = int(entry_idx - anchor_idx)
        
        # Find first bar after LSP where close < AVWAP
        first_break_idx = None
        for i in range(anchor_idx + 1, len(df)):
            if not np.isnan(avwap.iloc[i]) and df.iloc[i]["close"] < avwap.iloc[i]:
                first_break_idx = i
                break
        
        if first_break_idx is not None:
            result["first_break_date"] = df.iloc[first_break_idx]["date"].strftime("%Y-%m-%d")
            result["first_break_bars_vs_entry"] = int(first_break_idx - entry_idx)
        else:
            result["first_break_date"] = None
            result["first_break_bars_vs_entry"] = None
        
        # Did price exceed LSP between LSP date and entry? (double top confirmation)
        between = df.iloc[anchor_idx:entry_idx + 1]
        result["max_high_between"] = round(float(between["high"].max()), 2)
        result["exceeded_lsp"] = bool(between["high"].max() > lsp_price)
        
        # Scan bar (day before entry) — was close still above AVWAP?
        scan_idx = entry_idx - 1
        if scan_idx >= anchor_idx:
            result["scan_close_vs_avwap"] = round(float(df.iloc[scan_idx]["close"] - avwap.iloc[scan_idx]), 2)
            result["scan_close_above_avwap"] = bool(df.iloc[scan_idx]["close"] > avwap.iloc[scan_idx])
    
    return result


def main():
    # Load LSP data
    with open("/home/claude/swing-screener/data/dtss_lsp_data.json") as f:
        lsp_data = json.load(f)
    
    # Load examples from DB
    r = requests.get(f"{API}/api/examples/dtss")
    db_examples = {(x["ticker"], x["entryDate"]) for x in r.json()["examples"]}
    
    print(f"LSP data entries: {len(lsp_data)}")
    print(f"DB examples: {len(db_examples)}")
    
    results = []
    for ex in lsp_data:
        ticker = ex["ticker"]
        entry_date = ex["entry_date"]
        lsp_date = ex["date"]
        lsp_price = ex["price"]
        
        in_db = (ticker, entry_date) in db_examples
        tag = "" if in_db else "  [not in DB]"
        print(f"  {ticker:<8} entry={entry_date} LSP={lsp_date} @ ${lsp_price}{tag}...", end=" ")
        
        result = analyze_example(ticker, entry_date, lsp_date, lsp_price)
        result["in_db"] = in_db
        results.append(result)
        
        if "error" in result:
            print(f"ERROR: {result['error']}")
        else:
            cb = "✓" if result.get("entry_close_below_avwap") else "✗"
            print(f"close<AVWAP: {cb}  AVWAP=${result['avwap_at_entry']}  close=${result['entry_close']}")
    
    # ========== SUMMARY ==========
    valid = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]
    
    print(f"\n{'='*100}")
    print(f"AVWAP ENTRY HYPOTHESIS TEST — {len(valid)} valid / {len(errors)} errors")
    print(f"{'='*100}\n")
    
    # Main table
    print(f"{'Ticker':<8} {'Entry':<12} {'LSP$':>8} {'AVWAP$':>8} {'LSP→AV%':>8} {'LSP→AV ATR':>10} "
          f"{'Close$':>8} {'Cl<AV':>6} {'1stBreak':>10} {'vEntry':>7} {'Bars':>5}")
    print("-"*100)
    
    close_below = 0
    low_below = 0
    break_before = 0
    break_on = 0
    break_after = 0
    
    for r in valid:
        cb = "✓" if r.get("entry_close_below_avwap") else "✗"
        fb = r.get("first_break_date", "?")
        fv = r.get("first_break_bars_vs_entry", "?")
        la = r.get("lsp_to_avwap_atr", "?")
        lp = r.get("lsp_to_avwap_pct", "?")
        bars = r.get("bars_lsp_to_entry", "?")
        
        if r.get("entry_close_below_avwap"):
            close_below += 1
        if r.get("entry_low_below_avwap"):
            low_below += 1
        if isinstance(fv, (int, float)):
            if fv < 0: break_before += 1
            elif fv == 0: break_on += 1
            else: break_after += 1
        
        print(f"{r['ticker']:<8} {r['entry_date']:<12} {r['lsp_price']:>8.2f} "
              f"{r.get('avwap_at_entry','?'):>8} {lp:>7}% {la:>10} "
              f"{r['entry_close']:>8.2f} {cb:>6} {str(fb):>10} {str(fv):>7} {str(bars):>5}")
    
    print(f"\n{'='*60}")
    n = len(valid)
    if n > 0:
        print(f"Total valid: {n}")
        print(f"Entry CLOSE below LSP AVWAP: {close_below}/{n} ({close_below/n*100:.0f}%)")
        print(f"Entry LOW below LSP AVWAP:   {low_below}/{n} ({low_below/n*100:.0f}%)")
        print(f"\nFirst AVWAP break timing (close < AVWAP):")
        print(f"  Before entry day: {break_before}")
        print(f"  On entry day:     {break_on}")
        print(f"  After entry day:  {break_after}")
    
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for r in errors:
            print(f"  {r['ticker']} {r['entry_date']}: {r['error']}")
    
    # Save
    out_path = "/home/claude/swing-screener/data/avwap_test_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results → {out_path}")


if __name__ == "__main__":
    main()
