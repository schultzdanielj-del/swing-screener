"""Scan a wide set of features for surprisingly strong correlation with
SPY reversals / forward returns.

For each feature, on the last 5y of SPY, compute:
  - Reversal lift: at the top/bottom 5% tail of the feature, how many
    times more often is that bar a local peak / trough (vs base rate)?
  - Forward-return monotonicity: rank-correlation between decile of the
    feature and mean fwd-10d return across deciles.
  - Tail forward return: mean fwd-10d return for bottom 10% vs top 10%.

Sources: main repo daily pickle + market_ohlcv pickle (for VIX, sectors,
bonds, credit).

Output: ranked CSV + printed top-N for each scoring axis.
"""
from __future__ import annotations
import os, pickle, numpy as np, pandas as pd
from scipy.stats import spearmanr

ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
DAILY = os.path.join(ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl")
MARKET = os.path.join(ROOT, "local_runner", "cache", "market_ohlcv.pkl")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)

WIN = 10            # half-window for local extrema
FWD_H = 10          # forward horizon (trading days) for return tests
TAIL_PCT = 5        # tail % for "extreme" definition


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
print("Loading caches...")
with open(DAILY, "rb") as f:
    daily = pickle.load(f)
with open(MARKET, "rb") as f:
    market = pickle.load(f)


def get_close(ticker: str) -> pd.Series:
    """Return a date-indexed close series from whichever cache has the ticker."""
    src = daily if ticker in daily else market if ticker in market else None
    if src is None:
        raise KeyError(ticker)
    df = src[ticker].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["close"].sort_index()


def get_ohlc(ticker: str) -> pd.DataFrame:
    src = daily if ticker in daily else market if ticker in market else None
    if src is None:
        raise KeyError(ticker)
    df = src[ticker].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


# Anchor calendar = SPY trading days
spy = get_ohlc("SPY")
cutoff = spy.index.max() - pd.Timedelta(days=365 * 5)
spy5 = spy[spy.index >= cutoff].copy()
print(f"SPY last 5y: {spy5.index.min().date()} -> {spy5.index.max().date()}  n={len(spy5)}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()

def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def pct_from_high(close: pd.Series, n: int) -> pd.Series:
    return close / close.rolling(n).max() - 1.0

def pct_from_low(close: pd.Series, n: int) -> pd.Series:
    return close / close.rolling(n).min() - 1.0

def mark_extrema(close: np.ndarray, win: int) -> tuple[np.ndarray, np.ndarray]:
    n = len(close)
    is_peak = np.zeros(n, dtype=bool)
    is_trough = np.zeros(n, dtype=bool)
    for i in range(win, n - win):
        w = close[i - win:i + win + 1]
        c = close[i]
        if c == w.max() and (w > c).sum() == 0 and (w < c).sum() >= win:
            is_peak[i] = True
        if c == w.min() and (w < c).sum() == 0 and (w > c).sum() >= win:
            is_trough[i] = True
    return is_peak, is_trough


# ---------------------------------------------------------------------------
# Build features (last-5y window, indexed on SPY trading days)
# ---------------------------------------------------------------------------
close = spy["close"]
high = spy["high"]
low = spy["low"]
vol = spy["volume"]

features: dict[str, pd.Series] = {}

# --- price-derived on SPY ---
features["ema8_minus_ema21_$"] = ema(close, 8) - ema(close, 21)
features["ema5_minus_ema21_$"] = ema(close, 5) - ema(close, 21)
features["ema8_minus_ema34_$"] = ema(close, 8) - ema(close, 34)
features["ema13_minus_ema34_$"] = ema(close, 13) - ema(close, 34)
features["ema21_minus_ema50_$"] = ema(close, 21) - ema(close, 50)
features["close_vs_sma20"] = close / sma(close, 20) - 1
features["close_vs_sma50"] = close / sma(close, 50) - 1
features["close_vs_sma200"] = close / sma(close, 200) - 1
features["close_vs_ema21"] = close / ema(close, 21) - 1
features["sma20_vs_sma50"] = sma(close, 20) / sma(close, 50) - 1
features["sma50_vs_sma200"] = sma(close, 50) / sma(close, 200) - 1
features["pct_from_20d_high"] = pct_from_high(close, 20)
features["pct_from_60d_high"] = pct_from_high(close, 60)
features["pct_from_252d_high"] = pct_from_high(close, 252)
features["pct_from_20d_low"] = pct_from_low(close, 20)
features["pct_from_60d_low"] = pct_from_low(close, 60)
features["roc_5d"] = close.pct_change(5)
features["roc_10d"] = close.pct_change(10)
features["roc_20d"] = close.pct_change(20)
features["roc_60d"] = close.pct_change(60)
features["rsi_2"] = rsi(close, 2)
features["rsi_14"] = rsi(close, 14)
features["rsi_5"] = rsi(close, 5)
# Bollinger %B (20, 2)
ma20 = sma(close, 20); sd20 = close.rolling(20).std()
features["bbands_pctB"] = (close - (ma20 - 2 * sd20)) / (4 * sd20)
# ATR/close
features["atr14_pct"] = atr(spy, 14) / close * 100

# --- volume ---
features["vol_vs_sma20"] = vol / vol.rolling(20).mean()
features["dvol20_vs_dvol100"] = (close * vol).rolling(20).mean() / (close * vol).rolling(100).mean()
# OBV slope (rolling 20d change in cumulative OBV / close)
obv = (np.sign(close.diff()) * vol).fillna(0).cumsum()
features["obv_roc20"] = obv.diff(20) / close / 1e6  # normalize roughly

# --- VIX family ---
try:
    vix = get_close("VIX.INDX").reindex(spy.index, method="ffill")
    features["vix"] = vix
    features["vix_vs_sma20"] = vix / sma(vix, 20) - 1
    features["vix_roc5"] = vix.pct_change(5)
    features["vix_roc20"] = vix.pct_change(20)
    features["vix_zscore_60d"] = (vix - sma(vix, 60)) / vix.rolling(60).std()
except KeyError:
    pass

try:
    vix3m = get_close("VIX3M.INDX").reindex(spy.index, method="ffill")
    features["vix_term_vix_vix3m"] = features["vix"] / vix3m  # >1 = backwardation
except KeyError:
    pass

try:
    vvix = get_close("VVIX.INDX").reindex(spy.index, method="ffill")
    features["vvix"] = vvix
    features["vvix_vs_sma20"] = vvix / sma(vvix, 20) - 1
except KeyError:
    pass

try:
    skew = get_close("SKEW.INDX").reindex(spy.index, method="ffill")
    features["skew"] = skew
    features["skew_zscore_60d"] = (skew - sma(skew, 60)) / skew.rolling(60).std()
except KeyError:
    pass

# --- ratios: cross-asset relative ---
def rel(num: str, den: str, name: str):
    try:
        n = get_close(num).reindex(spy.index, method="ffill")
        d = get_close(den).reindex(spy.index, method="ffill")
        r = n / d
        features[f"{name}"] = r
        features[f"{name}_roc20"] = r.pct_change(20)
        features[f"{name}_vs_sma50"] = r / sma(r, 50) - 1
    except KeyError:
        pass

rel("QQQ", "SPY", "ratio_QQQ_SPY")     # tech leadership
rel("IWM", "SPY", "ratio_IWM_SPY")     # small caps
rel("XLK", "SPY", "ratio_XLK_SPY")
rel("XLF", "SPY", "ratio_XLF_SPY")
rel("XLY", "SPY", "ratio_XLY_SPY")     # consumer disc
rel("XLP", "SPY", "ratio_XLP_SPY")     # consumer staples (defensive)
rel("XLU", "SPY", "ratio_XLU_SPY")     # utilities (defensive)
rel("HYG", "IEF", "ratio_HYG_IEF")     # credit risk
rel("HYG", "TLT", "ratio_HYG_TLT")
rel("TLT", "SPY", "ratio_TLT_SPY")
rel("GLD", "SPY", "ratio_GLD_SPY")
rel("UUP", "SPY", "ratio_UUP_SPY")     # dollar vs stocks
rel("XLP", "XLY", "ratio_XLP_XLY")     # defensive vs offensive
rel("XLU", "XLK", "ratio_XLU_XLK")     # safety bid vs tech bid


# ---------------------------------------------------------------------------
# Restrict everything to last-5y window and compute scores
# ---------------------------------------------------------------------------
spy_close_5y = close[close.index >= cutoff]
is_peak, is_trough = mark_extrema(spy_close_5y.values, WIN)
n_valid = len(spy_close_5y) - 2 * WIN
base_peak = is_peak.sum() / n_valid
base_trough = is_trough.sum() / n_valid

print(f"Base rates last 5y: peak={base_peak*100:.2f}%  trough={base_trough*100:.2f}%")
print()

fwd = spy_close_5y.shift(-FWD_H) / spy_close_5y - 1.0

results = []
for name, raw in features.items():
    s = raw.reindex(spy_close_5y.index)
    if s.notna().sum() < 250:
        continue
    valid = s.dropna()
    # Align to extremum-eligible bars
    eligible = pd.Series(False, index=spy_close_5y.index)
    eligible.iloc[WIN:-WIN] = True
    s_e = valid.copy()
    common = s_e.index.intersection(spy_close_5y.index[eligible.values])
    if len(common) < 100:
        continue
    s_e = s_e.loc[common]

    # Top/bottom tail thresholds.
    hi = np.percentile(s_e.values, 100 - TAIL_PCT)
    lo = np.percentile(s_e.values, TAIL_PCT)

    idx_int = spy_close_5y.index.get_indexer(common)
    peak_arr = is_peak[idx_int]
    trough_arr = is_trough[idx_int]

    top_mask = s_e.values >= hi
    bot_mask = s_e.values <= lo

    n_top = int(top_mask.sum()); n_bot = int(bot_mask.sum())
    if n_top < 20 or n_bot < 20:
        continue

    # Peak lift = (peak rate in top tail) / base. Same for bottom -> trough.
    peak_in_top = int(peak_arr[top_mask].sum())
    trough_in_top = int(trough_arr[top_mask].sum())
    peak_in_bot = int(peak_arr[bot_mask].sum())
    trough_in_bot = int(trough_arr[bot_mask].sum())

    peak_lift_top = (peak_in_top / n_top) / base_peak
    trough_lift_top = (trough_in_top / n_top) / base_trough
    peak_lift_bot = (peak_in_bot / n_bot) / base_peak
    trough_lift_bot = (trough_in_bot / n_bot) / base_trough

    # Forward-return decile pattern (Spearman of decile-rank vs decile-mean fwd).
    panel = pd.DataFrame({"feat": s.values, "fwd": fwd.values}, index=s.index).dropna()
    if len(panel) < 200:
        continue
    bins = pd.qcut(panel["feat"], q=10, labels=False, duplicates="drop")
    decile_mean = panel.groupby(bins)["fwd"].mean()
    if len(decile_mean) < 8:
        continue
    rho, _ = spearmanr(decile_mean.index.values, decile_mean.values)

    # Tail fwd return (decile 1 vs decile 10).
    d1 = panel.iloc[:0]
    bin_min = decile_mean.idxmin()
    bin_max = decile_mean.idxmax()
    fwd_dec1 = panel[bins == decile_mean.index.min()]["fwd"].mean() * 100
    fwd_dec10 = panel[bins == decile_mean.index.max()]["fwd"].mean() * 100

    results.append({
        "feature": name,
        "n": int(s_e.notna().sum()),
        "peak_lift_top": peak_lift_top,        # extreme high -> peak
        "trough_lift_bot": trough_lift_bot,    # extreme low -> trough
        "peak_lift_bot": peak_lift_bot,        # extreme low -> peak (inverse signal)
        "trough_lift_top": trough_lift_top,    # extreme high -> trough (inverse signal)
        "decile_rho_fwd10": rho,
        "fwd10_dec_low%": fwd_dec1 * 100 / 100,
        "fwd10_dec_high%": fwd_dec10 * 100 / 100,
        "fwd10_spread%": fwd_dec10 - fwd_dec1,
        "max_reversal_lift": max(peak_lift_top, trough_lift_bot, peak_lift_bot, trough_lift_top),
    })

res = pd.DataFrame(results)
csv_out = os.path.join(OUT_DIR, "spy_feature_scan.csv")
res.to_csv(csv_out, index=False, float_format="%.3f")
print(f"Saved {len(res)} feature scores -> {csv_out}")
print()

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)
pd.set_option("display.float_format", lambda v: f"{v:+.2f}")

print("=" * 100)
print("TOP 12 features by max reversal lift in either tail")
print("=" * 100)
top_rev = res.sort_values("max_reversal_lift", ascending=False).head(12)
print(top_rev[["feature","n","peak_lift_top","trough_lift_bot","peak_lift_bot","trough_lift_top"]].to_string(index=False))

print()
print("=" * 100)
print("TOP 12 features by |Spearman decile vs fwd10d mean| (monotonic forward-return signal)")
print("=" * 100)
res["abs_rho"] = res["decile_rho_fwd10"].abs()
top_mono = res.sort_values("abs_rho", ascending=False).head(12)
print(top_mono[["feature","decile_rho_fwd10","fwd10_dec_low%","fwd10_dec_high%","fwd10_spread%"]].to_string(index=False))

print()
print("=" * 100)
print("TOP 12 features by tail forward-return spread (best - worst decile, fwd 10d)")
print("=" * 100)
top_tail = res.sort_values("fwd10_spread%", ascending=False).head(12)
print(top_tail[["feature","decile_rho_fwd10","fwd10_dec_low%","fwd10_dec_high%","fwd10_spread%"]].to_string(index=False))

print()
print("=" * 100)
print("EMA8-EMA21 baseline for comparison")
print("=" * 100)
base = res[res["feature"]=="ema8_minus_ema21_$"]
print(base.to_string(index=False))
