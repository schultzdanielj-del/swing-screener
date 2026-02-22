"""
DTSS PCF Expression Profiler

Generates all PCF-translatable expressions defined in dtss_expression_config.json,
evaluates them across examples and universe, ranks by discrimination power.

Every expression is designed to be directly translatable to TC2000 PCF syntax.
No Python-only features.

Usage:
    python scripts/dtss_profiler.py --examples-only    # Profile examples only (fast)
    python scripts/dtss_profiler.py --full              # Full universe profiling (~5 min)
    python scripts/dtss_profiler.py --benchmark         # Time one ticker, report budget
"""

import os
import sys
import json
import time
import argparse
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from scripts.profiling_engine import (
    sma, ema, hma, fwma, atr, rsi, wrsi,
    di_plus, di_minus, adx, cci, stochastic_k,
    macd, bollinger_top, bollinger_bot,
    aroon_up, aroon_down, obv, bop, chaikin_money_flow,
    kaufman_efficiency, rolling_max, rolling_min,
    count_true, since_true, true_in_row
)

# ── Railway DB config ──────────────────────────────────────────
RAILWAY_BASE = 'https://web-production-e3025.up.railway.app'


class DTSSProfiler:
    """Generates and evaluates PCF expressions for DTSS setup profiling."""

    def __init__(self):
        self.project_root = os.path.dirname(os.path.dirname(__file__))
        self.config = self._load_config()
        self.lsp_data = self._load_lsp_data()
        self.ohlcv_cache = {}  # ticker_date -> DataFrame
        self.examples = []
        self.tradable_tickers = []

    def _load_config(self):
        path = os.path.join(self.project_root, 'data', 'dtss_expression_config.json')
        with open(path) as f:
            return json.load(f)

    def _load_lsp_data(self):
        path = os.path.join(self.project_root, 'data', 'dtss_lsp_data.json')
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            return {f"{item['ticker']}_{item['entry_date']}": item for item in data}
        return {}

    # ── Data loading ───────────────────────────────────────────

    def load_examples(self):
        """Load DTSS examples from Railway DB."""
        import requests
        r = requests.post(f'{RAILWAY_BASE}/api/query',
                          json={'sql': 'SELECT * FROM examples WHERE setup_type="dtss"'},
                          timeout=15)
        rows = r.json()['results']
        self.examples = []
        for row in rows:
            # scan_date = day before entry_date
            entry_dt = pd.Timestamp(row['entry_date'])
            # Go back 1 trading day (simple: subtract 1, skip weekends)
            scan_dt = entry_dt - pd.Timedelta(days=1)
            if scan_dt.weekday() == 6:  # Sunday
                scan_dt -= pd.Timedelta(days=2)
            elif scan_dt.weekday() == 5:  # Saturday
                scan_dt -= pd.Timedelta(days=1)
            self.examples.append({
                'ticker': row['ticker'],
                'entry_date': row['entry_date'],
                'scan_date': scan_dt.strftime('%Y-%m-%d'),
                'example_id': row['id'],
            })
        print(f"Loaded {len(self.examples)} DTSS examples")
        return self.examples

    def load_tradable_universe(self):
        """Load tradable universe tickers from Railway DB (paginated)."""
        import requests
        all_tickers = []
        offset = 0
        while True:
            r = requests.post(f'{RAILWAY_BASE}/api/query',
                              json={'sql': f'SELECT ticker FROM tradable_universe ORDER BY ticker LIMIT 100 OFFSET {offset}'},
                              timeout=15)
            batch = r.json().get('results', [])
            all_tickers.extend([row['ticker'] for row in batch])
            if len(batch) < 100:
                break
            offset += 100
        self.tradable_tickers = all_tickers
        print(f"Loaded {len(self.tradable_tickers)} tradable universe tickers")
        return self.tradable_tickers

    def fetch_ohlcv(self, ticker, target_date, lookback_days=300):
        """Fetch OHLCV for any ticker from universe_ohlcv table (with pagination)."""
        cache_key = f"{ticker}_{target_date}"
        if cache_key in self.ohlcv_cache:
            return self.ohlcv_cache[cache_key]

        import requests
        end_dt = pd.Timestamp(target_date)
        start_dt = end_dt - pd.Timedelta(days=lookback_days * 1.5)
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")

        try:
            all_rows = []
            offset = 0
            while True:
                r = requests.post(f'{RAILWAY_BASE}/api/query',
                                  json={'sql': f"""
                    SELECT date, open, high, low, close, volume
                    FROM universe_ohlcv
                    WHERE ticker='{ticker}'
                      AND date BETWEEN '{start_str}' AND '{end_str}'
                    ORDER BY date LIMIT 100 OFFSET {offset}
                """}, timeout=15)
                batch = r.json().get('results', [])
                all_rows.extend(batch)
                if len(batch) < 100:
                    break
                offset += 100

            if not all_rows:
                return None

            df = pd.DataFrame(all_rows)
            df['date'] = pd.to_datetime(df['date'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df = df.dropna().reset_index(drop=True)

            if len(df) < 50:
                return None

            self.ohlcv_cache[cache_key] = df
            return df
        except Exception:
            return None

    def fetch_bulk(self, ticker_date_pairs, max_workers=20):
        """Fetch OHLCV for multiple ticker/date pairs in parallel."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        to_fetch = [(t, d) for t, d in ticker_date_pairs
                    if f"{t}_{d}" not in self.ohlcv_cache]

        if not to_fetch:
            return

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.fetch_ohlcv, t, d): (t, d)
                for t, d in to_fetch
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass

    def fetch_examples_bulk(self, max_workers=20):
        """Fetch OHLCV for all examples in parallel."""
        import requests
        from concurrent.futures import ThreadPoolExecutor, as_completed

        to_fetch = [ex for ex in self.examples
                    if f"ex_{ex['example_id']}_{ex['scan_date']}" not in self.ohlcv_cache]

        if not to_fetch:
            return

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.fetch_ohlcv_example, ex['example_id'], ex['scan_date']): ex
                for ex in to_fetch
            }
            for future in as_completed(futures):
                ex = futures[future]
                try:
                    future.result()
                except Exception:
                    pass

    # ── Base indicator computation ────────────────────────────

    def compute_base_indicators(self, df):
        """Compute all base indicators defined in config. Returns dict of name -> Series."""
        c = df['close']
        h = df['high']
        l = df['low']
        o = df['open']
        v = df['volume']
        bi = self.config['base_indicators']
        bases = {}

        # SMAs
        for p in bi['sma_periods']:
            bases[f'avgc{p}'] = sma(c, p)

        # EMAs
        for p in bi['ema_periods']:
            bases[f'xavgc{p}'] = ema(c, p)

        # HMAs
        for p in bi.get('hma_periods', []):
            bases[f'havgc{p}'] = hma(c, p)

        # ATR
        for p in bi['atr_periods']:
            bases[f'atr{p}'] = atr(df, p)

        # ADR
        for p in bi['adr_periods']:
            bases[f'adr{p}'] = sma(h - l, p)

        # MAXH
        for p in bi['maxh_periods']:
            bases[f'maxh{p}'] = rolling_max(h, p)

        # MINL
        for p in bi['minl_periods']:
            bases[f'minl{p}'] = rolling_min(l, p)

        # MAXC / MINC
        for p in bi.get('maxc_periods', []):
            bases[f'maxc{p}'] = rolling_max(c, p)
        for p in bi.get('minc_periods', []):
            bases[f'minc{p}'] = rolling_min(c, p)

        # RSI
        for p in bi['rsi_periods']:
            bases[f'rsi{p}'] = rsi(c, p)

        # Wilder RSI
        for p in bi.get('wrsi_periods', []):
            bases[f'wrsi{p}'] = wrsi(c, p)

        # DI+, DI-, ADX
        for p in bi.get('di_periods', []):
            bases[f'diplus{p}'] = di_plus(df, p)
            bases[f'diminus{p}'] = di_minus(df, p)
        for p in bi.get('adx_periods', []):
            bases[f'adx{p}_{p}'] = adx(df, p, p)

        # CCI
        for p in bi.get('cci_periods', []):
            bases[f'cci{p}'] = cci(df, p)

        # Stochastic
        for p in bi.get('stochastic_periods', []):
            bases[f'stoc{p}'] = stochastic_k(df, p)

        # MACD
        for pair in bi.get('macd', []):
            bases[f'macd{pair[0]}_{pair[1]}'] = macd(c, pair[0], pair[1])

        # Bollinger
        for bb in bi.get('bollinger', []):
            p, w = bb['period'], bb['width']
            bases[f'bbtop{w}_{p}'] = bollinger_top(c, p, w)
            bases[f'bbbot{w}_{p}'] = bollinger_bot(c, p, w)

        # Stddev
        for p in bi.get('stddev_periods', []):
            bases[f'stddev{p}'] = c.rolling(p).std()

        # Aroon
        for p in bi.get('aroon_periods', []):
            bases[f'aroonup{p}'] = aroon_up(df, p)
            bases[f'aroondn{p}'] = aroon_down(df, p)

        # OBV
        if bi.get('obv'):
            bases['obv'] = obv(df)

        # BOP
        for p in bi.get('bop_periods', []):
            bases[f'bop{p}'] = bop(df, p)

        # CMF
        for p in bi.get('cmf_periods', []):
            bases[f'cmf{p}'] = chaikin_money_flow(df, p)

        # Kaufman Efficiency
        for p in bi.get('kaufman_eff_periods', []):
            bases[f'keff{p}'] = kaufman_efficiency(c, p)

        # Volume SMAs/EMAs
        for p in bi.get('vol_sma_periods', []):
            bases[f'avgv{p}'] = sma(v, p)
        for p in bi.get('vol_ema_periods', []):
            bases[f'xavgv{p}'] = ema(v, p)

        # Body / Wick averages
        for p in bi.get('body_avg_periods', []):
            bases[f'avg_body{p}'] = sma(abs(c - o), p)
        for p in bi.get('wick_avg_periods', []):
            max_co = pd.concat([c, o], axis=1).max(axis=1)
            min_co = pd.concat([c, o], axis=1).min(axis=1)
            bases[f'avg_uwk{p}'] = sma(h - max_co, p)
            bases[f'avg_lwk{p}'] = sma(min_co - l, p)

        return bases

    # ── Boolean condition computation ─────────────────────────

    def compute_boolean_features(self, df, bases, idx):
        """Compute CountTrue, SinceTrue, TrueInRow features. Returns dict of name -> value."""
        c = df['close']
        h = df['high']
        l = df['low']
        o = df['open']
        v = df['volume']
        out = {}

        rule6 = self.config['expression_rules']['rule_6_boolean_features']
        ct_periods = rule6['count_true_periods']
        st_periods = rule6['since_true_periods']
        tir_periods = rule6['true_in_row_periods']
        st_cond_names = set(rule6.get('since_true_conditions', []))
        tir_cond_names = set(rule6.get('true_in_row_conditions', []))

        # Build all boolean conditions
        conditions = {}
        all_ct_conds = rule6['count_true_conditions']
        for category in all_ct_conds.values():
            for item in category:
                name = item['name']
                cond = self._eval_boolean_condition(name, c, h, l, o, v, bases)
                if cond is not None:
                    conditions[name] = cond

        # CountTrue
        for name, cond in conditions.items():
            for p in ct_periods:
                ct = count_true(cond, p)
                if idx < len(ct) and pd.notna(ct.iloc[idx]):
                    out[f'ct_{name}_{p}'] = float(ct.iloc[idx])

        # SinceTrue (selective)
        for name in st_cond_names:
            cond = conditions.get(name)
            if cond is None:
                continue
            for p in st_periods:
                try:
                    st = since_true(cond, p)
                    if idx < len(st) and pd.notna(st.iloc[idx]):
                        out[f'st_{name}_{p}'] = float(st.iloc[idx])
                except Exception:
                    pass

        # TrueInRow (selective)
        for name in tir_cond_names:
            cond = conditions.get(name)
            if cond is None:
                continue
            for p in tir_periods:
                try:
                    tir = true_in_row(cond, p)
                    if idx < len(tir) and pd.notna(tir.iloc[idx]):
                        out[f'tir_{name}_{p}'] = float(tir.iloc[idx])
                except Exception:
                    pass

        return out

    def _eval_boolean_condition(self, name, c, h, l, o, v, bases):
        """Evaluate a named boolean condition. Returns boolean Series or None."""
        # Trend
        if name == 'c_gt_xavgc8': return c > bases.get('xavgc8', c)
        if name == 'c_gt_xavgc21': return c > bases.get('xavgc21', c)
        if name == 'c_gt_avgc50': return c > bases.get('avgc50', c)
        if name == 'c_gt_avgc200': return c > bases.get('avgc200', c)
        if name == 'c_gt_xavgc50': return c > bases.get('xavgc50', c)
        if name == 'c_gt_xavgc100': return c > bases.get('xavgc100', c)
        # MA stacking
        if name == 'xavgc8_gt_xavgc21': return bases.get('xavgc8', c) > bases.get('xavgc21', c)
        if name == 'xavgc21_gt_xavgc50': return bases.get('xavgc21', c) > bases.get('xavgc50', c)
        if name == 'xavgc50_gt_xavgc200': return bases.get('xavgc50', c) > bases.get('xavgc200', c)
        if name == 'avgc50_gt_avgc200': return bases.get('avgc50', c) > bases.get('avgc200', c)
        # Momentum
        if name == 'c_gt_c1': return c > c.shift(1)
        if name == 'c_lt_c1': return c < c.shift(1)
        if name == 'h_gt_h1': return h > h.shift(1)
        if name == 'l_lt_l1': return l < l.shift(1)
        if name == 'c_gt_o': return c > o
        if name == 'c_lt_o': return c < o
        # Volume
        if name == 'v_gt_avgv20':
            avgv20 = bases.get('avgv20')
            return v > avgv20 if avgv20 is not None else None
        if name == 'v_lt_avgv20':
            avgv20 = bases.get('avgv20')
            return v < avgv20 if avgv20 is not None else None
        if name == 'v_gt_2x_avgv20':
            avgv20 = bases.get('avgv20')
            return v > (2 * avgv20) if avgv20 is not None else None
        # MA rising
        if name == 'avgc50_gt_avgc50_1':
            s = bases.get('avgc50')
            return s > s.shift(1) if s is not None else None
        if name == 'avgc200_gt_avgc200_1':
            s = bases.get('avgc200')
            return s > s.shift(1) if s is not None else None
        if name == 'xavgc50_gt_xavgc50_1':
            s = bases.get('xavgc50')
            return s > s.shift(1) if s is not None else None
        # Structure
        if name == 'h_gt_maxh5_1':
            mh5 = bases.get('maxh5')
            return h > mh5.shift(1) if mh5 is not None else None
        if name == 'l_lt_minl5_1':
            ml5 = bases.get('minl5')
            return l < ml5.shift(1) if ml5 is not None else None
        if name == 'c_gt_maxc10_1':
            mc10 = bases.get('maxc10')
            return c > mc10.shift(1) if mc10 is not None else None
        if name == 'range_gt_atr':
            atr14 = bases.get('atr14')
            return (h - l) > atr14 if atr14 is not None else None
        if name == 'body_gt_half_range':
            return abs(c - o) > (h - l) / 2
        if name == 'c_near_h':
            return c > (h + l) / 2
        if name == 'diplus_gt_diminus':
            dp = bases.get('diplus14')
            dm = bases.get('diminus14')
            return dp > dm if dp is not None and dm is not None else None
        if name == 'rsi14_gt_50':
            r = bases.get('rsi14')
            return r > 50 if r is not None else None
        if name == 'rsi14_gt_70':
            r = bases.get('rsi14')
            return r > 70 if r is not None else None
        if name == 'adx14_gt_25':
            a = bases.get('adx14_14')
            return a > 25 if a is not None else None
        if name == 'c_gt_bbtop':
            bt = bases.get('bbtop2.0_20')
            return c > bt if bt is not None else None
        if name == 'c_lt_bbbot':
            bb = bases.get('bbbot2.0_20')
            return c < bb if bb is not None else None
        return None

    # ── Numeric expression generation ─────────────────────────

    def compute_numeric_expressions(self, df, bases, idx):
        """Generate all numeric PCF expressions from rules 1-5. Returns dict of name -> value."""
        c = df['close']
        h = df['high']
        l = df['low']
        o = df['open']
        v = df['volume']
        out = {}

        def _val(name, offset=0):
            """Get value from bases at idx-offset, or None."""
            s = bases.get(name)
            if s is None:
                return None
            i = idx - offset
            if 0 <= i < len(s) and pd.notna(s.iloc[i]):
                return float(s.iloc[i])
            return None

        def _priceval(series, offset=0):
            i = idx - offset
            if 0 <= i < len(series) and pd.notna(series.iloc[i]):
                return float(series.iloc[i])
            return None

        cv = _priceval(c)
        hv = _priceval(h)
        lv = _priceval(l)
        ov = _priceval(o)
        vv = _priceval(v)
        if cv is None:
            return out

        a14 = _val('atr14') or 0
        adr14 = _val('adr14') or 0

        def _safe_div(num, den):
            if den and den > 0:
                return num / den
            return None

        # ── Raw price offsets ──
        for off in [0, 1, 2, 3, 5, 10, 20]:
            pv = _priceval(c, off)
            if pv is not None: out[f'C.{off}'] = pv
            pv = _priceval(h, off)
            if pv is not None: out[f'H.{off}'] = pv
            pv = _priceval(l, off)
            if pv is not None: out[f'L.{off}'] = pv

        # ── Raw indicator values + offsets ──
        for name in bases:
            val = _val(name)
            if val is not None:
                out[name] = val
            for off in [1, 5, 10]:
                val = _val(name, off)
                if val is not None:
                    out[f'{name}.{off}'] = val

        # ── RULE 1: Near resistance ──
        rules = self.config['expression_rules']
        r1 = rules['rule_1_near_resistance']['expressions']

        # Proximity to MAXH
        for p in r1['proximity_to_maxh']['maxh_periods']:
            mh = _val(f'maxh{p}')
            if mh is None:
                continue
            for nn in r1['proximity_to_maxh']['normalizers']:
                nv = a14 if nn == 'atr14' else adr14
                val = _safe_div(mh - cv, nv)
                if val is not None:
                    out[f'prox_maxh{p}_{nn}'] = val

        # High proximity to MAXH
        for p in r1['high_proximity_to_maxh']['maxh_periods']:
            mh = _val(f'maxh{p}')
            if mh is None:
                continue
            for nn in r1['high_proximity_to_maxh']['normalizers']:
                nv = a14 if nn == 'atr14' else adr14
                val = _safe_div(mh - hv, nv)
                if val is not None:
                    out[f'hprox_maxh{p}_{nn}'] = val

        # Overshoot MAXH
        for p in r1['overshoot_maxh']['maxh_periods']:
            mh_prev = _val(f'maxh{p}', 1)
            if mh_prev is None:
                continue
            val = _safe_div(hv - mh_prev, a14)
            if val is not None:
                out[f'overshoot_maxh{p}_atr14'] = val

        # Close vs MAXH %
        for p in r1['close_vs_maxh_pct']['maxh_periods']:
            mh = _val(f'maxh{p}')
            if mh and mh > 0:
                out[f'c_vs_maxh{p}_pct'] = cv / mh

        # Range narrowing at top
        for cfg in [r1.get('range_narrowing_at_top', {})]:
            for mhp in cfg.get('maxh_periods', []):
                for mcp in cfg.get('maxc_periods', []):
                    mh = _val(f'maxh{mhp}')
                    mc = _val(f'maxc{mcp}')
                    if mh is not None and mc is not None:
                        val = _safe_div(mh - mc, a14)
                        if val is not None:
                            out[f'rng_narrow_maxh{mhp}_maxc{mcp}_atr14'] = val

        # ── RULE 2: Extension from MAs ──
        r2 = rules['rule_2_extension_from_mas']['expressions']

        # Extension ADR
        for ma in r2['extension_adr']['mas']:
            mv = _val(ma)
            if mv is not None:
                val = _safe_div(cv - mv, adr14)
                if val is not None:
                    out[f'ext_{ma}_adr14'] = val

        # Extension ATR
        for ma in r2['extension_atr']['mas']:
            mv = _val(ma)
            if mv is not None:
                val = _safe_div(cv - mv, a14)
                if val is not None:
                    out[f'ext_{ma}_atr14'] = val

        # Extension %
        for ma in r2['extension_pct']['mas']:
            mv = _val(ma)
            if mv and mv > 0:
                out[f'ext_{ma}_pct'] = (cv - mv) / mv * 100

        # Extension slope
        for ma in r2['extension_slope']['mas']:
            mv = _val(ma)
            if mv is None:
                continue
            for off in r2['extension_slope']['offsets']:
                mv_ago = _val(ma, off)
                cp = _priceval(c, off)
                adr_ago = _val('adr14', off)
                if mv_ago and cp and adr14 > 0 and adr_ago and adr_ago > 0:
                    ext_now = (cv - mv) / adr14
                    ext_ago = (cp - mv_ago) / adr_ago
                    out[f'extslope_{ma}_{off}b'] = ext_now - ext_ago

        # Extension peak
        for mhp in r2['extension_peak']['maxh_periods']:
            mh = _val(f'maxh{mhp}')
            if mh is None:
                continue
            for ma in r2['extension_peak']['mas']:
                mv = _val(ma)
                if mv is not None and adr14 > 0:
                    out[f'extpk_maxh{mhp}_{ma}_adr14'] = (mh - mv) / adr14

        # ── RULE 3: MA structure ──
        r3 = rules['rule_3_ma_structure']['expressions']

        # MA slopes ATR
        for ma in r3['ma_slopes_atr']['mas']:
            mv = _val(ma)
            if mv is None:
                continue
            for off in r3['ma_slopes_atr']['offsets']:
                mv_ago = _val(ma, off)
                if mv_ago is not None and a14 > 0:
                    out[f'slope_{ma}_{off}b_atr14'] = (mv - mv_ago) / a14

        # MA slopes ADR
        for ma in r3['ma_slopes_adr']['mas']:
            mv = _val(ma)
            if mv is None:
                continue
            for off in r3['ma_slopes_adr']['offsets']:
                mv_ago = _val(ma, off)
                if mv_ago is not None and adr14 > 0:
                    out[f'slope_{ma}_{off}b_adr14'] = (mv - mv_ago) / adr14

        # MA spreads ATR
        for ma1, ma2 in r3['ma_spreads_atr']['pairs']:
            v1 = _val(ma1)
            v2 = _val(ma2)
            if v1 is not None and v2 is not None and a14 > 0:
                out[f'spread_{ma1}_{ma2}_atr14'] = (v1 - v2) / a14

        # MA spreads ADR
        for ma1, ma2 in r3['ma_spreads_adr']['pairs']:
            v1 = _val(ma1)
            v2 = _val(ma2)
            if v1 is not None and v2 is not None and adr14 > 0:
                out[f'spread_{ma1}_{ma2}_adr14'] = (v1 - v2) / adr14

        # MA ratios
        for ma1, ma2 in r3['ma_ratios']['pairs']:
            v1 = _val(ma1)
            v2 = _val(ma2)
            if v1 is not None and v2 is not None and v2 > 0:
                out[f'ratio_{ma1}_{ma2}'] = v1 / v2

        # ── RULE 4: Momentum stalling ──
        r4 = rules['rule_4_momentum_stalling']['expressions']

        # ROC
        for off in r4['roc']['offsets']:
            cp = _priceval(c, off)
            if cp and cp > 0:
                out[f'roc_{off}'] = (cv - cp) / cp * 100

        # ROC deceleration
        roc5 = out.get('roc_5')
        roc10 = out.get('roc_10')
        if roc5 is not None and roc10 is not None:
            out['roc_decel_5_10'] = roc5 - roc10
        roc3 = out.get('roc_3')
        if roc3 is not None and roc10 is not None:
            out['roc_decel_3_10'] = roc3 - roc10

        # Volume ratios
        for p in r4['volume_ratios']['periods']:
            av = _val(f'avgv{p}')
            if av and av > 0 and vv:
                out[f'vratio_{p}'] = vv / av

        # Volume trend
        av10 = _val('avgv10')
        av50 = _val('avgv50')
        if av10 and av50 and av50 > 0:
            out['vol_trend_10_50'] = av10 / av50

        # Indicator offsets
        for ind in r4.get('indicator_offsets', {}).get('indicators', []):
            for off in r4['indicator_offsets']['offsets']:
                val = _val(ind, off)
                if val is not None:
                    out[f'{ind}.{off}'] = val

        # ── RULE 5: Range and channel ──
        r5 = rules['rule_5_range_and_channel']['expressions']

        # Range position
        for p in r5['range_position']['periods']:
            mh = _val(f'maxh{p}')
            ml = _val(f'minl{p}')
            if mh and ml and (mh - ml) > 0:
                out[f'pos_{p}'] = (cv - ml) / (mh - ml)

        # Channel slope
        for p in r5['channel_slope']['periods']:
            if idx >= p:
                w = c.iloc[idx - p + 1:idx + 1].values
                if len(w) == p:
                    x = np.arange(p, dtype=float)
                    xm = x.mean()
                    cm = w.mean()
                    denom = np.sum((x - xm) ** 2)
                    if denom > 0 and cm > 0:
                        sl = np.sum((x - xm) * (w - cm)) / denom
                        out[f'chslope_{p}'] = sl / cm * 100

        # Pullback from high
        for mhp in r5['pullback_from_high']['maxh_periods']:
            mh = _val(f'maxh{mhp}')
            if mh is None:
                continue
            for nn in r5['pullback_from_high']['normalizers']:
                if nn == 'atr14':
                    val = _safe_div(mh - cv, a14)
                elif nn == 'adr14':
                    val = _safe_div(mh - cv, adr14)
                elif nn == 'pct':
                    val = (mh - cv) / mh * 100 if mh > 0 else None
                else:
                    val = None
                if val is not None:
                    out[f'pb_maxh{mhp}_{nn}'] = val

        # Bollinger %b and bandwidth
        bt = _val('bbtop2.0_20')
        bb = _val('bbbot2.0_20')
        avgc20 = _val('avgc20')
        if bt and bb and (bt - bb) > 0:
            out['bb_pctb'] = (cv - bb) / (bt - bb)
            if avgc20 and avgc20 > 0:
                out['bb_bw'] = (bt - bb) / avgc20 * 100

        return out

    # ── LSP features (examples only) ──────────────────────────

    def compute_lsp_features(self, df, bases, idx, ticker, entry_date):
        """Compute LSP-specific features for examples. Returns dict."""
        out = {}
        lsp_key = f"{ticker}_{entry_date}"
        lsp_info = self.lsp_data.get(lsp_key)
        if not lsp_info:
            return out

        c = df['close']
        h = df['high']
        cv = float(c.iloc[idx])
        hv = float(h.iloc[idx])
        lsp_price = float(lsp_info['price'])
        lsp_date_str = lsp_info.get('date')

        a14 = bases.get('atr14')
        adr14_s = bases.get('adr14')
        a14_val = float(a14.iloc[idx]) if a14 is not None and pd.notna(a14.iloc[idx]) else 0
        adr14_val = float(adr14_s.iloc[idx]) if adr14_s is not None and pd.notna(adr14_s.iloc[idx]) else 0

        if a14_val <= 0:
            return out

        # Distance from close to LSP
        out['lsp_dist_close_atr'] = (lsp_price - cv) / a14_val
        if adr14_val > 0:
            out['lsp_dist_close_adr'] = (lsp_price - cv) / adr14_val
        out['lsp_dist_high_atr'] = (lsp_price - hv) / a14_val
        out['lsp_pct_of_close'] = (lsp_price / cv - 1.0) * 100

        # Valley depth and retracement
        if lsp_date_str:
            lsp_dt = pd.Timestamp(lsp_date_str)
            lsp_mask = df['date'] <= lsp_dt
            if lsp_mask.any():
                lsp_idx = df.loc[lsp_mask].index[-1]
                if lsp_idx < idx:
                    between = df.iloc[lsp_idx:idx + 1]
                    valley_low = float(between['low'].min())
                    out['lsp_valley_depth_atr'] = (lsp_price - valley_low) / a14_val
                    if lsp_price > 0:
                        out['lsp_valley_depth_pct'] = (lsp_price - valley_low) / lsp_price * 100
                    lsp_valley_range = lsp_price - valley_low
                    if lsp_valley_range > 0:
                        out['lsp_valley_retracement'] = (cv - valley_low) / lsp_valley_range
                    out['lsp_bars_back'] = float(idx - lsp_idx)
                    valley_idx = int(between['low'].idxmin())
                    out['lsp_bars_since_valley'] = float(idx - valley_idx)

        # Approach velocity
        for off in [3, 5]:
            if idx >= off:
                c_ago = float(c.iloc[idx - off])
                out[f'lsp_approach_velocity_{off}bar'] = (cv - c_ago) / a14_val

        # LSP vs MAXH (proxy validation)
        for p in [20, 50, 65, 120]:
            mh = bases.get(f'maxh{p}')
            if mh is not None and pd.notna(mh.iloc[idx]):
                mh_val = float(mh.iloc[idx])
                out[f'lsp_vs_maxh{p}_atr'] = (lsp_price - mh_val) / a14_val

        return out

    # ── Full profile for one ticker/date ──────────────────────

    def profile_ticker(self, df, target_date, ticker=None, entry_date=None, is_example=False):
        """Compute all expressions for one ticker at target_date. Returns dict of name -> value."""
        target_dt = pd.Timestamp(target_date)
        mask = df['date'] <= target_dt
        if not mask.any():
            return None
        idx = df.loc[mask].index[-1]

        # Need enough bars for longest indicator (200 SMA)
        if idx < 210:
            return None

        # Compute base indicators
        bases = self.compute_base_indicators(df)

        # Numeric expressions (rules 1-5)
        expressions = self.compute_numeric_expressions(df, bases, idx)

        # Boolean features (rule 6)
        bool_features = self.compute_boolean_features(df, bases, idx)
        expressions.update(bool_features)

        # LSP features (examples only)
        if is_example and ticker and entry_date:
            lsp_features = self.compute_lsp_features(df, bases, idx, ticker, entry_date)
            expressions.update(lsp_features)

        return expressions

    # ── Profiling runners ─────────────────────────────────────

    def profile_examples(self):
        """Profile all DTSS examples. Returns list of dicts."""
        if not self.examples:
            self.load_examples()

        # Fetch all OHLCV
        pairs = [(ex['ticker'], ex['scan_date']) for ex in self.examples]
        print(f"Fetching OHLCV for {len(pairs)} examples...")
        t0 = time.time()
        self.fetch_bulk(pairs)
        print(f"  Fetched in {time.time()-t0:.1f}s")

        results = []
        t0 = time.time()
        for ex in self.examples:
            cache_key = f"{ex['ticker']}_{ex['scan_date']}"
            df = self.ohlcv_cache.get(cache_key)
            if df is None:
                print(f"  SKIP {ex['ticker']} - no data")
                continue
            profile = self.profile_ticker(
                df, ex['scan_date'],
                ticker=ex['ticker'], entry_date=ex['entry_date'],
                is_example=True
            )
            if profile:
                profile['_ticker'] = ex['ticker']
                profile['_scan_date'] = ex['scan_date']
                profile['_entry_date'] = ex['entry_date']
                results.append(profile)
                print(f"  {ex['ticker']}: {len(profile)} expressions")
            else:
                print(f"  SKIP {ex['ticker']} - insufficient data")

        elapsed = time.time() - t0
        print(f"\nProfiled {len(results)} examples in {elapsed:.1f}s")
        if results:
            expr_counts = [len(r) - 3 for r in results]  # subtract _ticker, _scan_date, _entry_date
            print(f"Expressions per example: {min(expr_counts)}-{max(expr_counts)} (avg {sum(expr_counts)/len(expr_counts):.0f})")
        return results

    # ── Universe profiling ─────────────────────────────────────

    def fetch_ohlcv_bulk_api(self, ticker, target_date, lookback=300):
        """Fetch OHLCV via /api/ohlcv/bulk/{ticker} endpoint. Returns DataFrame or None."""
        cache_key = f"{ticker}_{target_date}"
        if cache_key in self.ohlcv_cache:
            return self.ohlcv_cache[cache_key]
        try:
            import requests
            r = requests.get(
                f'{RAILWAY_BASE}/api/ohlcv/bulk/{ticker}',
                params={'end_date': target_date, 'lookback': lookback},
                timeout=30,
            )
            if r.status_code != 200:
                return None
            rows = r.json().get('results', [])
            if not rows:
                return None
            df = pd.DataFrame(rows)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df = df.dropna().reset_index(drop=True)
            if len(df) < 50:
                return None
            self.ohlcv_cache[cache_key] = df
            return df
        except Exception:
            return None

    def profile_universe(self, target_date, max_workers=20, progress=True):
        """Profile all tradable universe tickers at target_date. Returns list of dicts.
        
        Streams data: fetch → profile → discard OHLCV to save memory.
        """
        if not self.tradable_tickers:
            self.load_tradable_universe()

        tickers = self.tradable_tickers
        total = len(tickers)
        if progress:
            print(f"\nProfiling {total} tradable universe tickers at {target_date}")
            print(f"{'='*60}")

        t0 = time.time()
        universe_results = []
        batch_size = max_workers
        failed = 0
        skipped = 0

        def fetch_and_profile(ticker):
            """Fetch OHLCV and profile in one step — no caching."""
            try:
                import requests as req
                r = req.get(
                    f'{RAILWAY_BASE}/api/ohlcv/bulk/{ticker}',
                    params={'end_date': target_date, 'lookback': 300},
                    timeout=30,
                )
                if r.status_code != 200:
                    return (ticker, None, 'fetch_error')
                rows = r.json().get('results', [])
                if not rows:
                    return (ticker, None, 'no_data')
                df = pd.DataFrame(rows)
                df['date'] = pd.to_datetime(df['date'])
                df = df.sort_values('date').reset_index(drop=True)
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                df = df.dropna().reset_index(drop=True)
                if len(df) < 220:
                    return (ticker, None, 'insufficient')
                
                profile = self.profile_ticker(df, target_date)
                if profile:
                    profile['_ticker'] = ticker
                    profile['_date'] = target_date
                    return (ticker, profile, 'ok')
                return (ticker, None, 'profile_failed')
            except Exception as e:
                return (ticker, None, f'error:{str(e)[:50]}')

        for i in range(0, total, batch_size):
            batch = tickers[i:i + batch_size]
            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                results = list(executor.map(fetch_and_profile, batch))
            for ticker, profile, status in results:
                if profile:
                    universe_results.append(profile)
                elif status == 'insufficient' or status == 'no_data':
                    skipped += 1
                else:
                    failed += 1

            if progress and (i + batch_size) % 200 == 0:
                elapsed = time.time() - t0
                done = min(i + batch_size, total)
                got = len(universe_results)
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                print(f"  {done}/{total} — {got} profiled, {skipped} skipped, {failed} failed ({elapsed:.0f}s, ETA {eta:.0f}s)")

        t_total = time.time() - t0
        if progress:
            print(f"  Complete: {len(universe_results)} profiled, {skipped} skipped, {failed} failed")
            print(f"  Total: {t_total:.0f}s ({t_total/60:.1f} min)")

        return universe_results

    # ── Discrimination ranking ────────────────────────────────

    def rank_expressions(self, example_profiles, universe_profiles, top_n=50):
        """Rank expressions by how well they discriminate examples from universe.

        For each expression, finds the threshold where examples cluster but universe doesn't.
        Returns sorted list of dicts with PCF expression, pass rates, direction, threshold.
        """
        # Get all expression names (exclude metadata keys)
        meta_keys = {'_ticker', '_scan_date', '_entry_date', '_date'}
        # Also exclude LSP features (only exist for examples, can't scan on them)
        lsp_keys = set()
        for p in example_profiles:
            for k in p:
                if k.startswith('lsp_'):
                    lsp_keys.add(k)

        # Collect all expression names present in examples
        expr_names = set()
        for p in example_profiles:
            for k in p:
                if k not in meta_keys and k not in lsp_keys:
                    expr_names.add(k)

        # Build value arrays
        n_ex = len(example_profiles)
        n_uni = len(universe_profiles)

        rankings = []

        for expr_name in sorted(expr_names):
            # Collect example values
            ex_vals = []
            for p in example_profiles:
                v = p.get(expr_name)
                if v is not None and np.isfinite(v):
                    ex_vals.append(v)

            # Need most examples to have this expression
            if len(ex_vals) < n_ex * 0.7:
                continue

            # Collect universe values
            uni_vals = []
            for p in universe_profiles:
                v = p.get(expr_name)
                if v is not None and np.isfinite(v):
                    uni_vals.append(v)

            if len(uni_vals) < n_uni * 0.3:
                continue

            ex_arr = np.array(ex_vals)
            uni_arr = np.array(uni_vals)

            # Try both directions: > threshold and < threshold
            best = None
            for direction in ['>', '<']:
                # Find threshold that captures most examples with least universe
                # Use example percentiles as candidate thresholds
                for pct in [5, 10, 15, 20, 25, 30, 40, 50]:
                    if direction == '>':
                        thresh = np.percentile(ex_arr, pct)
                        ex_pass = np.mean(ex_arr >= thresh)
                        uni_pass = np.mean(uni_arr >= thresh)
                    else:
                        thresh = np.percentile(ex_arr, 100 - pct)
                        ex_pass = np.mean(ex_arr <= thresh)
                        uni_pass = np.mean(uni_arr <= thresh)

                    # Skip if too many examples fail
                    if ex_pass < 0.75:
                        continue
                    # Skip if not selective enough
                    if uni_pass > 0.80:
                        continue

                    # Discrimination score: higher example pass + lower universe pass
                    # Use ratio as primary metric
                    if uni_pass > 0:
                        disc_ratio = ex_pass / uni_pass
                    else:
                        disc_ratio = ex_pass * 100  # Very selective

                    if best is None or disc_ratio > best['disc_ratio']:
                        best = {
                            'expression': expr_name,
                            'direction': direction,
                            'threshold': round(float(thresh), 4),
                            'ex_pass_rate': round(float(ex_pass), 3),
                            'uni_pass_rate': round(float(uni_pass), 3),
                            'disc_ratio': round(float(disc_ratio), 2),
                            'ex_count': len(ex_vals),
                            'uni_count': len(uni_vals),
                            'ex_median': round(float(np.median(ex_arr)), 4),
                            'uni_median': round(float(np.median(uni_arr)), 4),
                        }

            if best and best['disc_ratio'] > 1.5:
                rankings.append(best)

        # Sort by discrimination ratio (best first)
        rankings.sort(key=lambda x: x['disc_ratio'], reverse=True)
        return rankings[:top_n]

    def format_ranking_report(self, rankings, top_n=30):
        """Format rankings into a readable report."""
        lines = []
        lines.append(f"\n{'='*90}")
        lines.append(f"TOP {min(top_n, len(rankings))} DISCRIMINATING EXPRESSIONS")
        lines.append(f"{'='*90}")
        lines.append(f"{'#':>3} {'Expression':<40} {'Dir':>3} {'Threshold':>10} {'Ex%':>6} {'Uni%':>6} {'Ratio':>6}")
        lines.append(f"{'-'*90}")

        for i, r in enumerate(rankings[:top_n], 1):
            lines.append(
                f"{i:>3} {r['expression']:<40} {r['direction']:>3} {r['threshold']:>10.4f} "
                f"{r['ex_pass_rate']*100:>5.1f}% {r['uni_pass_rate']*100:>5.1f}% {r['disc_ratio']:>6.1f}x"
            )

        lines.append(f"{'='*90}")
        return '\n'.join(lines)

    # ── Benchmark ─────────────────────────────────────────────

    def benchmark_one_ticker(self):
        """Time profiling of one ticker to verify budget."""
        if not self.examples:
            self.load_examples()

        ex = self.examples[0]
        df = self.fetch_ohlcv(ex['ticker'], ex['scan_date'])
        if df is None:
            print(f"Could not fetch data for {ex['ticker']}")
            return

        # Warm up
        self.profile_ticker(df, ex['scan_date'], ex['ticker'], ex['entry_date'], is_example=True)

        # Time it
        iterations = 20
        t0 = time.time()
        for _ in range(iterations):
            profile = self.profile_ticker(df, ex['scan_date'], ex['ticker'], ex['entry_date'], is_example=True)
        elapsed = (time.time() - t0) / iterations * 1000

        expr_count = len(profile) - 3  # subtract metadata keys
        print(f"\n{'='*60}")
        print(f"BENCHMARK: {ex['ticker']} ({ex['scan_date']})")
        print(f"{'='*60}")
        print(f"Time per ticker:    {elapsed:.1f}ms")
        print(f"Expressions:        {expr_count}")
        print(f"4167 tickers:       {elapsed * 4167 / 1000:.0f}s ({elapsed * 4167 / 1000 / 60:.1f} min)")
        print(f"Budget (5 min):     300s")
        print(f"{'WITHIN' if elapsed * 4167 / 1000 <= 300 else 'OVER'} BUDGET")
        print(f"{'='*60}")

        return profile


def main():
    parser = argparse.ArgumentParser(description='DTSS PCF Expression Profiler')
    parser.add_argument('--examples-only', action='store_true', help='Profile examples only')
    parser.add_argument('--full', action='store_true', help='Full pipeline: examples + universe + ranking')
    parser.add_argument('--benchmark', action='store_true', help='Benchmark one ticker')
    parser.add_argument('--date', type=str, default='2026-02-19', help='Target date for universe profiling')
    parser.add_argument('--top', type=int, default=50, help='Number of top discriminators to show')
    parser.add_argument('--workers', type=int, default=20, help='Concurrent fetch workers')
    args = parser.parse_args()

    profiler = DTSSProfiler()

    if args.benchmark:
        profiler.benchmark_one_ticker()

    elif args.examples_only:
        results = profiler.profile_examples()
        out_path = os.path.join(profiler.project_root, 'data', 'dtss_example_profiles.json')
        with open(out_path, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\nSaved {len(results)} example profiles to {out_path}")

    elif args.full:
        # Step 1: Profile examples
        print("STEP 1: Profiling examples...")
        example_results = profiler.profile_examples()
        if not example_results:
            print("ERROR: No example profiles generated")
            sys.exit(1)

        # Step 2: Profile universe
        print(f"\nSTEP 2: Profiling tradable universe at {args.date}...")
        universe_results = profiler.profile_universe(args.date, max_workers=args.workers)
        if not universe_results:
            print("ERROR: No universe profiles generated")
            sys.exit(1)

        # Step 3: Rank expressions
        print(f"\nSTEP 3: Ranking expressions by discrimination power...")
        rankings = profiler.rank_expressions(example_results, universe_results, top_n=args.top)
        report = profiler.format_ranking_report(rankings, top_n=args.top)
        print(report)

        # Save all results
        out_dir = os.path.join(profiler.project_root, 'data')
        with open(os.path.join(out_dir, 'dtss_example_profiles.json'), 'w') as f:
            json.dump(example_results, f)
        with open(os.path.join(out_dir, 'dtss_rankings.json'), 'w') as f:
            json.dump(rankings, f, indent=2)
        # Universe profiles are large — save summary stats only
        uni_summary = {
            'date': args.date,
            'count': len(universe_results),
            'sample_ticker': universe_results[0]['_ticker'] if universe_results else None,
            'sample_expressions': len(universe_results[0]) - 2 if universe_results else 0,
        }
        with open(os.path.join(out_dir, 'dtss_universe_summary.json'), 'w') as f:
            json.dump(uni_summary, f, indent=2)

        print(f"\nSaved:")
        print(f"  Example profiles: data/dtss_example_profiles.json ({len(example_results)} examples)")
        print(f"  Rankings: data/dtss_rankings.json ({len(rankings)} discriminators)")
        print(f"  Universe summary: data/dtss_universe_summary.json ({len(universe_results)} tickers)")

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
