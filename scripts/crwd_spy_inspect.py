"""Inspect CRWD + SPY daily bars around the Feb/Mar correction and the early-April
turn. Read-only. Print close, MAs, 21EMA, and overnight gap%."""
import pickle
from pathlib import Path
import pandas as pd

CACHE = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\universe_ohlcv_daily.pkl")
with open(CACHE, "rb") as f:
    cache = pickle.load(f)


def show(ticker, start, end):
    if ticker not in cache:
        print(f"{ticker}: NOT IN CACHE")
        return
    df = cache[ticker].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"].astype(float)
    o = df["open"].astype(float)
    df["sma10"] = c.rolling(10).mean()
    df["sma20"] = c.rolling(20).mean()
    df["sma50"] = c.rolling(50).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()
    df["gap"] = (o / c.shift(1) - 1.0) * 100.0
    df["d1"] = (c / c.shift(1) - 1.0) * 100.0
    win = df[(df["date"] >= start) & (df["date"] <= end)]
    print(f"\n===== {ticker}  {start} -> {end} =====")
    print("date         close     d1%    gap%    sma10    sma20    sma50    ema21   10v20")
    prev_rel = None
    for _, r in win.iterrows():
        rel = "+" if r["sma10"] > r["sma20"] else "-"
        mark = ""
        if prev_rel is not None and prev_rel == "-" and rel == "+":
            mark = "  <== 10 crosses ABOVE 20"
        if prev_rel is not None and prev_rel == "+" and rel == "-":
            mark = "  <== 10 crosses below 20"
        prev_rel = rel
        print(f"{r['date'].date()}  {r['close']:7.2f}  {r['d1']:+5.1f}  {r['gap']:+5.1f}  "
              f"{r['sma10']:7.2f}  {r['sma20']:7.2f}  {r['sma50']:7.2f}  {r['ema21']:7.2f}    {rel}{mark}")


show("CRWD", "2026-02-25", "2026-05-15")
show("SPY", "2026-03-16", "2026-04-24")
