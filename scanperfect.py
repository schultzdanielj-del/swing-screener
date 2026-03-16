"""
ScanPerfect Desktop — Auto-launch server + browser.

Usage:
    python scanperfect.py

Starts the local FastAPI server and opens the UI in your default browser.
Close the terminal window (or Ctrl+C) to stop the server.
"""

import os
import sys
import time
import webbrowser
import threading

# Ensure we're running from the project root
os.chdir(os.path.dirname(os.path.abspath(__file__)))

PORT = 8000
URL = f"http://localhost:{PORT}"


def open_browser():
    """Wait for server to be ready, then open browser."""
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{URL}/api/setups", timeout=2)
            webbrowser.open(URL)
            return
        except Exception:
            time.sleep(0.5)
    # Fallback: open anyway
    webbrowser.open(URL)


def main():
    # Open browser in background thread once server is ready
    threading.Thread(target=open_browser, daemon=True).start()

    # Start uvicorn (blocks until Ctrl+C or window close)
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
