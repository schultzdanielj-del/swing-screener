"""
ScanPerfect Desktop — Native window launcher.

Usage:
    python scanperfect.py

Opens the ScanPerfect UI in a native desktop window.
The FastAPI server runs embedded — no separate process, no browser tab.
Close the window and everything shuts down cleanly.

Requirements:
    pip install pywebview

On Windows, pywebview uses Edge WebView2 (built into Windows 10/11).
"""

import sys
import os

# Ensure we're running from the project root
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Import the FastAPI app (this also triggers init_db + OHLCV cache load)
from server import app

import webview


def main():
    # Create native window pointing at the FastAPI ASGI app
    window = webview.create_window(
        title="ScanPerfect",
        url=app,
        width=1400,
        height=900,
        min_size=(1000, 600),
        background_color="#0a0e17",
        text_select=True,
    )

    # Start the GUI loop (blocks until window is closed)
    webview.start(debug="--debug" in sys.argv, private_mode=False)


if __name__ == "__main__":
    main()
