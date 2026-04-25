"""MOC (Market-On-Close) level detector — D1 horizontal S/R levels from
high-RVOL candle H/L prints.

Mirrors lsp_detector_v2 / algo_line_detector interface:
  compute_all_moc_series(df) -> dict[col_name -> ndarray(n_bars)]
  get_moc_expression_names() -> list of 61 column names

Spec: EXPRESSION_ENGINE_V2.md §6 MOC.

State per level (mutated chronologically across bars):
  price, birth_bar, stack_weight, stack_count, max_contributor_rvol,
  cross_count, last_contribution_bar, last_close_side,
  max_abs_beyond_atr, contact_count

Per-bar emission: top 3 above + top 3 below current close by stack_weight,
9 features per slot + 7 composites = 61 total.

Historical immutability: snapshot at bar N uses only state from bars 0..N.
"""
import numpy as np
import pandas as pd

# Spec constants
TOP_N_PER_SIDE = 3
N_SLOTS = 2 * TOP_N_PER_SIDE  # 6
PER_LEVEL_FEATURES = [
    "distance",
    "stack_weight",
    "stack_count",
    "max_contributor_rvol",
    "cross_count",
    "bars_since_birth",
    "bars_since_last_contribution",
    "max_abs_beyond_atr",
    "contact_count",
]
N_PER_LEVEL = len(PER_LEVEL_FEATURES)  # 9
COMPOSITE_FEATURES = [
    "total_weight_above",
    "total_weight_below",
    "n_levels_above",
    "n_levels_below",
    "n_levels_within_2atr",
    "top1_spread_atr",
    "weight_asymmetry",
]
N_COMPOSITE = len(COMPOSITE_FEATURES)  # 7
N_TOTAL = N_SLOTS * N_PER_LEVEL + N_COMPOSITE  # 54 + 7 = 61

# Spec: birth gate, tolerance, contact-resolution window
RVOL_BIRTH_MIN = 1.0
TOLERANCE_FRAC = float(np.sqrt(1.0 / 78.0))  # ≈ 0.1132
CONTACT_FWD_BARS = 5
RVOL_PERIOD = 50
ATR_PERIOD = 14


def get_moc_expression_names():
    """Return the 61 ordered column names. Stable across runs."""
    names = []
    for side in ("above", "below"):
        for rank in (1, 2, 3):
            for feat in PER_LEVEL_FEATURES:
                names.append(f"moc_{side}_{rank}_{feat}")
    for c in COMPOSITE_FEATURES:
        names.append(f"moc_composite_{c}")
    assert len(names) == N_TOTAL
    return names


def _rolling_mean_partial(arr, period):
    """Rolling mean with min_periods=1, computed via pandas to bit-match
    research/moc_usefulness_test.py (which uses pd.Series.rolling().mean()).

    A cumsum-based replacement drifts at the last float bit and flips boundary
    cases in `n_levels_within_2atr`. Pandas internals are the validation truth.
    """
    return pd.Series(arr).rolling(period, min_periods=1).mean().values


def _compute_atr14(high, low, close):
    """SMA-14 of true range with min_periods=1 partial-window warmup.
    Matches research/moc_usefulness_test.py compute_atr14."""
    n = len(close)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    return _rolling_mean_partial(tr, ATR_PERIOD)


def _compute_rvol(volume):
    """volume / SMA(volume, 50) with min_periods=1 partial-window warmup.
    Matches research/moc_usefulness_test.py compute_rvol. Non-finite results → 0."""
    sma = _rolling_mean_partial(volume, RVOL_PERIOD)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(sma > 0, volume / sma, 0.0)
    out[~np.isfinite(out)] = 0.0
    return out


def _snapshot(levels, close_t, atr_t, bar_idx):
    """Compute the 61-feature snapshot at bar `bar_idx`.

    Returns float64 array length N_TOTAL.
    """
    vec = np.full(N_TOTAL, np.nan, dtype=np.float64)
    atr_safe = atr_t if (atr_t is not None and np.isfinite(atr_t) and atr_t > 0) else np.nan

    # Partition by side (level price strictly above / below current close)
    above = [l for l in levels if l["price"] > close_t]
    below = [l for l in levels if l["price"] < close_t]
    # Sort by stack_weight descending; Python sort is stable so ties break by birth order
    above.sort(key=lambda l: -l["stack_weight"])
    below.sort(key=lambda l: -l["stack_weight"])

    # Pad to TOP_N_PER_SIDE so missing slots emit NaN
    top = (above[:TOP_N_PER_SIDE] + [None] * TOP_N_PER_SIDE)[:TOP_N_PER_SIDE] + \
          (below[:TOP_N_PER_SIDE] + [None] * TOP_N_PER_SIDE)[:TOP_N_PER_SIDE]

    for slot_i, lvl in enumerate(top):
        if lvl is None:
            continue
        base = slot_i * N_PER_LEVEL
        vec[base + 0] = (lvl["price"] - close_t) / atr_safe
        vec[base + 1] = lvl["stack_weight"]
        vec[base + 2] = lvl["stack_count"]
        vec[base + 3] = lvl["max_contributor_rvol"]
        vec[base + 4] = lvl["cross_count"]
        vec[base + 5] = bar_idx - lvl["birth_bar"]
        vec[base + 6] = bar_idx - lvl["last_contribution_bar"]
        vec[base + 7] = lvl["max_abs_beyond_atr"]
        vec[base + 8] = lvl["contact_count"]

    # Composites computed from full active-levels list
    composite_base = N_SLOTS * N_PER_LEVEL
    total_w_above = sum(l["stack_weight"] for l in above)
    total_w_below = sum(l["stack_weight"] for l in below)
    n_above = len(above)
    n_below = len(below)
    if np.isfinite(atr_safe):
        thresh = 2.0 * atr_safe
        n_within_2atr = sum(1 for l in levels if abs(l["price"] - close_t) < thresh)
    else:
        n_within_2atr = np.nan
    if above and below and np.isfinite(atr_safe):
        top_above_price = max(above, key=lambda l: l["stack_weight"])["price"]
        top_below_price = max(below, key=lambda l: l["stack_weight"])["price"]
        top1_spread = (top_above_price - top_below_price) / atr_safe
    else:
        top1_spread = np.nan
    total_weight = total_w_above + total_w_below
    weight_asym = ((total_w_above - total_w_below) / total_weight) if total_weight > 0 else np.nan

    vec[composite_base + 0] = total_w_above
    vec[composite_base + 1] = total_w_below
    vec[composite_base + 2] = n_above
    vec[composite_base + 3] = n_below
    vec[composite_base + 4] = n_within_2atr
    vec[composite_base + 5] = top1_spread
    vec[composite_base + 6] = weight_asym

    return vec


def _step_one_bar(levels, high, low, close, volume, t, atr_t, rvol_t):
    """Apply one bar of MOC mechanics at index t. Mutates `levels` in place.

    Returns the 61-feature snapshot vec at bar t.
    """
    ct = close[t]
    ht = high[t]
    lt = low[t]

    if not np.isfinite(atr_t) or atr_t <= 0:
        tol_t = np.nan
    else:
        tol_t = atr_t * TOLERANCE_FRAC

    # Birth / stack (only when RVOL gate passes and tolerance is defined)
    if np.isfinite(rvol_t) and rvol_t > RVOL_BIRTH_MIN and np.isfinite(tol_t):
        for price in (ht, lt):
            matched = None
            for lvl in levels:
                if abs(lvl["price"] - price) <= tol_t:
                    matched = lvl
                    break
            if matched is None:
                levels.append({
                    "price": float(price),
                    "birth_bar": t,
                    "stack_weight": float(rvol_t),
                    "stack_count": 1,
                    "max_contributor_rvol": float(rvol_t),
                    "cross_count": 0,
                    "last_contribution_bar": t,
                    "last_close_side": None,
                    "max_abs_beyond_atr": 0.0,
                    "contact_count": 0,
                })
            else:
                matched["stack_weight"] += float(rvol_t)
                matched["stack_count"] += 1
                if rvol_t > matched["max_contributor_rvol"]:
                    matched["max_contributor_rvol"] = float(rvol_t)
                matched["last_contribution_bar"] = t

    # Cross tracking + max-beyond update (for every active level)
    for lvl in levels:
        if t <= lvl["birth_bar"]:
            continue
        dist = ct - lvl["price"]
        if np.isfinite(atr_t) and atr_t > 0:
            beyond_atr = abs(dist) / atr_t
            if beyond_atr > lvl["max_abs_beyond_atr"]:
                lvl["max_abs_beyond_atr"] = float(beyond_atr)
        if np.isfinite(tol_t):
            if dist > tol_t:
                curr_side = 1
            elif dist < -tol_t:
                curr_side = -1
            else:
                curr_side = 0
            if lvl["last_close_side"] is not None and curr_side != 0:
                if curr_side != lvl["last_close_side"] and lvl["last_close_side"] != 0:
                    lvl["cross_count"] += 1
            if curr_side != 0:
                lvl["last_close_side"] = curr_side

    # Contact resolution: events at bar t-CONTACT_FWD_BARS resolved at bar t.
    check_bar = t - CONTACT_FWD_BARS
    if check_bar >= 1:
        hc = high[check_bar]
        lc = low[check_bar]
        cc_prev = close[check_bar - 1]
        for lvl in levels:
            if check_bar <= lvl["birth_bar"]:
                continue
            p = lvl["price"]
            if not (lc <= p <= hc):
                continue
            prev_dist = cc_prev - p
            if abs(prev_dist) <= 1e-9:
                continue
            lvl["contact_count"] += 1

    return _snapshot(levels, ct, atr_t, t)


def _walk_levels(high, low, close, volume):
    """Walk D1 chronologically maintaining `levels` state, snapshotting every bar.

    Returns (matrix, final_levels):
      matrix: ndarray shape (n_bars, N_TOTAL) float64
      final_levels: list of level dicts at end of walk
    """
    n = len(close)
    rvol = _compute_rvol(volume)
    atr = _compute_atr14(high, low, close)

    levels = []
    out = np.full((n, N_TOTAL), np.nan, dtype=np.float64)

    for t in range(n):
        out[t] = _step_one_bar(levels, high, low, close, volume, t, atr[t], rvol[t])

    return out, levels


def compute_all_moc_series(df):
    """Compute all 61 MOC expression series for a ticker.

    Args:
        df: DataFrame with columns date, open, high, low, close, volume

    Returns:
        dict[col_name -> ndarray(n_bars,) float64]
    """
    high = np.asarray(df["high"].values, dtype=np.float64)
    low = np.asarray(df["low"].values, dtype=np.float64)
    close = np.asarray(df["close"].values, dtype=np.float64)
    volume = np.asarray(df["volume"].values, dtype=np.float64)
    matrix, _ = _walk_levels(high, low, close, volume)
    names = get_moc_expression_names()
    return {names[j]: matrix[:, j] for j in range(N_TOTAL)}


# ─── Forward-prop API ─────────────────────────────────────────────

def bootstrap_moc_state(df):
    """One-time setup: walk full history, return state at the cache end-bar.

    Returns:
        dict with key 'levels' (list of level dicts), JSON-serializable.
    """
    high = np.asarray(df["high"].values, dtype=np.float64)
    low = np.asarray(df["low"].values, dtype=np.float64)
    close = np.asarray(df["close"].values, dtype=np.float64)
    volume = np.asarray(df["volume"].values, dtype=np.float64)
    _, levels = _walk_levels(high, low, close, volume)
    return {"levels": levels}


def step_moc_one_bar(prior_state, df, t=None):
    """Forward-prop one bar of MOC mechanics.

    Args:
        prior_state: dict with key 'levels' (state at end of bar t-1).
                     Pass `{'levels': []}` if no prior state.
        df: DataFrame containing OHLCV up to and including bar t.
        t: bar index to compute (default: len(df) - 1).

    Returns:
        (vec, new_state) where vec is the 61-feature snapshot at bar t and
        new_state is the updated state to persist for the next call.

    To match the full-rebuild path exactly, this re-derives ATR14 and RVOL
    over the full df via pandas rolling (the same path the full rebuild uses).
    """
    if t is None:
        t = len(df) - 1
    high = np.asarray(df["high"].values, dtype=np.float64)
    low = np.asarray(df["low"].values, dtype=np.float64)
    close = np.asarray(df["close"].values, dtype=np.float64)
    volume = np.asarray(df["volume"].values, dtype=np.float64)
    atr = _compute_atr14(high, low, close)
    rvol = _compute_rvol(volume)
    # Deep-copy the levels list so callers can keep prior_state intact
    levels = [dict(lvl) for lvl in prior_state.get("levels", [])]
    vec = _step_one_bar(levels, high, low, close, volume, t, atr[t], rvol[t])
    return vec, {"levels": levels}
