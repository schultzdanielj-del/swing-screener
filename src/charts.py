"""
mplfinance chart generation.
Produces clean D1 candlestick charts with MA overlays for visual screening.
"""
import os
from typing import Optional, Dict

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


def generate_chart(
    ticker: str,
    df: pd.DataFrame,
    output_dir: str = "output/charts",
    config: Optional[dict] = None
) -> Optional[str]:
    """
    Generate a single D1 candlestick chart with MA overlays.
    
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
    
    # Chart style
    style = chart_cfg.get('style', 'nightclouds')
    
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
