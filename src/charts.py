"""
mplfinance chart generation.
Produces clean D1 candlestick charts with MA overlays for visual screening.
"""
import os
from typing import Optional, Dict

import numpy as np
import pandas as pd
import mplfinance as mpf
import yaml


def load_config(config_path: str = "config.yaml") -> dict:
    """Load chart configuration from YAML."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def _compute_ma(series: pd.Series, period: int, ma_type: str = "sma") -> pd.Series:
    """Compute moving average — SMA or EMA."""
    if ma_type.lower() == "ema":
        return series.ewm(span=period, adjust=False).mean()
    else:
        return series.rolling(window=period).mean()


def _compute_highest_avwap(df: pd.DataFrame) -> pd.Series:
    """
    Find the anchor candle that produces the highest AVWAP at the current (last) bar.
    Returns the full AVWAP series from that anchor forward, with NaN before the anchor.
    """
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3.0
    volume = df['Volume'].values
    tp = typical_price.values
    n = len(df)

    best_anchor = 0
    best_final_avwap = -np.inf

    # Test each candle as a potential anchor
    for anchor in range(n):
        cum_tp_vol = 0.0
        cum_vol = 0.0
        for j in range(anchor, n):
            cum_tp_vol += tp[j] * volume[j]
            cum_vol += volume[j]
        if cum_vol > 0:
            final_avwap = cum_tp_vol / cum_vol
            if final_avwap > best_final_avwap:
                best_final_avwap = final_avwap
                best_anchor = anchor

    # Build the full AVWAP series from the best anchor
    avwap = np.full(n, np.nan)
    cum_tp_vol = 0.0
    cum_vol = 0.0
    for j in range(best_anchor, n):
        cum_tp_vol += tp[j] * volume[j]
        cum_vol += volume[j]
        if cum_vol > 0:
            avwap[j] = cum_tp_vol / cum_vol

    return pd.Series(avwap, index=df.index)


def _build_dark_style():
    """Custom dark style with standard green/red candles and volume."""
    return mpf.make_mpf_style(
        base_mpf_style='nightclouds',
        marketcolors=mpf.make_marketcolors(
            up='#26A69A',       # Green candle body
            down='#EF5350',     # Red candle body
            edge={'up': '#26A69A', 'down': '#EF5350'},
            wick={'up': '#26A69A', 'down': '#EF5350'},
            volume={'up': '#26A69A', 'down': '#EF5350'},
        ),
    )


def generate_chart(
    ticker: str,
    df: pd.DataFrame,
    output_dir: str = "output/charts",
    config: Optional[dict] = None
) -> Optional[str]:
    """
    Generate a single D1 candlestick chart with MA overlays and highest AVWAP.
    
    Args:
        ticker: Stock ticker symbol (used in title and filename)
        df: OHLCV DataFrame from yfinance
        output_dir: Directory to save chart images
        config: Chart configuration dict (loaded from config.yaml if None)
    
    Returns:
        Path to saved chart image, or None if generation fails
    """
    if config is None:
        config = load_config()
    
    chart_cfg = config.get('chart', {})
    os.makedirs(output_dir, exist_ok=True)
    
    # Build moving average plots
    ma_plots = []
    for ma in chart_cfg.get('moving_averages', []):
        period = ma['period']
        ma_type = ma.get('type', 'sma')
        if len(df) >= period:
            ma_series = _compute_ma(df['Close'], period, ma_type)
            ma_plots.append(
                mpf.make_addplot(
                    ma_series,
                    color=ma.get('color', '#888888'),
                    width=ma.get('width', 1.0)
                )
            )
    
    # Add highest AVWAP overlay
    if len(df) >= 2:
        avwap_series = _compute_highest_avwap(df)
        ma_plots.append(
            mpf.make_addplot(
                avwap_series,
                color='#FF69B4',    # Hot pink — stands out against MAs
                width=1.5
            )
        )
    
    # Use custom dark style with green/red candles
    style = _build_dark_style()
    
    # Output path
    filename = f"{ticker}.png"
    filepath = os.path.join(output_dir, filename)
    
    try:
        fig_kwargs = {
            'type': 'candle',
            'volume': chart_cfg.get('volume', True),
            'title': f"{ticker} — D1",
            'style': style,
            'figsize': (
                chart_cfg.get('width', 12),
                chart_cfg.get('height', 7)
            ),
            'savefig': {
                'fname': filepath,
                'dpi': chart_cfg.get('dpi', 150),
                'bbox_inches': 'tight'
            },
            'warn_too_much_data': 500
        }
        
        if ma_plots:
            fig_kwargs['addplot'] = ma_plots
        
        mpf.plot(df, **fig_kwargs)
        return filepath
        
    except Exception as e:
        print(f"  ✗ Chart generation failed for {ticker}: {e}")
        return None


def generate_batch(
    data: Dict[str, pd.DataFrame],
    output_dir: str = "output/charts",
    config: Optional[dict] = None
) -> Dict[str, str]:
    """
    Generate charts for multiple tickers.
    
    Args:
        data: Dict mapping ticker -> OHLCV DataFrame
        output_dir: Directory to save chart images
        config: Chart configuration dict
    
    Returns:
        Dict mapping ticker -> chart filepath (skips failures)
    """
    if config is None:
        config = load_config()
    
    results = {}
    total = len(data)
    
    for i, (ticker, df) in enumerate(data.items(), 1):
        print(f"  [{i}/{total}] Generating chart for {ticker}...", end=" ")
        path = generate_chart(ticker, df, output_dir, config)
        if path:
            results[ticker] = path
            print("✓")
        else:
            print("✗")
    
    print(f"\n  Generated {len(results)}/{total} charts")
    return results
