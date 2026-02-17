"""
Composite grid builder.
Stitches individual charts into grid images so you can paste
fewer images into Claude chat for batch analysis.

With 20 charts per grid at readable resolution, 200 tickers = 10 images
into a single Claude conversation.
"""
import os
import math
from typing import List, Dict, Optional, Tuple
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _get_font(size: int = 20):
    """Get a monospace font, falling back to default."""
    try:
        return ImageFont.truetype("cour.ttf", size)  # Windows Courier
    except OSError:
        pass
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)
    except OSError:
        pass
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf", size)
    except OSError:
        pass
    return ImageFont.load_default()


def build_composite(
    chart_paths: Dict[str, str],
    output_path: str,
    cols: int = 4,
    thumb_width: int = 480,
    thumb_height: int = 300,
    padding: int = 8,
    label_height: int = 28,
    bg_color: str = "#1a1a1a",
    label_color: str = "#00ff00",
) -> str:
    """
    Stitch multiple chart images into a single labeled grid.
    
    Args:
        chart_paths: Dict mapping ticker -> chart image filepath
        output_path: Where to save the composite image
        cols: Number of columns in the grid
        thumb_width: Width of each chart thumbnail
        thumb_height: Height of each chart thumbnail
        padding: Pixels between charts
        label_height: Height of ticker label bar above each chart
        bg_color: Background color
        label_color: Ticker label text color
    
    Returns:
        Path to the saved composite image
    """
    tickers = list(chart_paths.keys())
    n = len(tickers)
    rows = math.ceil(n / cols)

    cell_w = thumb_width + padding
    cell_h = thumb_height + label_height + padding

    canvas_w = cols * cell_w + padding
    canvas_h = rows * cell_h + padding

    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_color)
    draw = ImageDraw.Draw(canvas)
    font = _get_font(18)

    for idx, ticker in enumerate(tickers):
        row = idx // cols
        col = idx % cols

        x = padding + col * cell_w
        y = padding + row * cell_h

        # Draw ticker label
        draw.text((x + 4, y + 2), ticker, fill=label_color, font=font)

        # Paste chart thumbnail
        chart_path = chart_paths[ticker]
        try:
            img = Image.open(chart_path)
            img = img.resize((thumb_width, thumb_height), Image.LANCZOS)
            canvas.paste(img, (x, y + label_height))
        except Exception as e:
            # Draw error placeholder
            draw.rectangle(
                [x, y + label_height, x + thumb_width, y + label_height + thumb_height],
                fill="#333333",
                outline="#555555",
            )
            draw.text(
                (x + 10, y + label_height + thumb_height // 2),
                f"Error: {e}",
                fill="#ff4444",
                font=font,
            )

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    canvas.save(output_path, quality=95)
    print(f"  Saved composite: {output_path} ({n} charts, {rows}x{cols} grid)")
    return output_path


def build_composites(
    chart_paths: Dict[str, str],
    output_dir: str = "output/composites",
    charts_per_grid: int = 20,
    cols: int = 4,
    **kwargs,
) -> List[str]:
    """
    Split charts into groups and build composite grids.
    
    Args:
        chart_paths: Dict mapping ticker -> chart filepath
        output_dir: Directory for composite images
        charts_per_grid: Max charts per composite image
        cols: Columns per grid
        **kwargs: Extra args passed to build_composite()
    
    Returns:
        List of composite image paths
    """
    os.makedirs(output_dir, exist_ok=True)
    items = list(chart_paths.items())
    composites = []

    total_grids = math.ceil(len(items) / charts_per_grid)
    print(f"\nBuilding {total_grids} composite grid(s) from {len(items)} charts...")

    for i in range(0, len(items), charts_per_grid):
        batch = dict(items[i : i + charts_per_grid])
        grid_num = i // charts_per_grid + 1
        output_path = os.path.join(output_dir, f"grid_{grid_num:02d}.png")

        build_composite(batch, output_path, cols=cols, **kwargs)
        composites.append(output_path)

    print(f"  Done — {len(composites)} composite(s) ready to paste into Claude chat")
    return composites
