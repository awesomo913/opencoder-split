"""Re-launch the Autocoder Chrome on port 9222 with all the AI tabs.

The CDP broadcast needs Chrome running with --remote-debugging-port=9222
AND a dedicated user-data-dir (Chrome refuses to run two instances on
the same profile, so we use ~/.autocoder/chrome_profile/).

This script does exactly that: launches Chrome against that profile,
opens tabs for every AI site the provider chain might need, and
leaves them there logged in (persistent cookies) for future runs.

Run:
    python start_chrome.py

Or from another script:
    from start_chrome import start
    start()
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Make sure the package is importable no matter where we run from
sys.path.insert(0, str(Path(__file__).resolve().parent))


# Every tab we want open for the broadcast to have maximum provider coverage.
# If any fail to load (site down, account logged out), the chain still works —
# it auto-skips rungs that can't connect.
AI_TABS: list[str] = [
    "https://gemini.google.com/app",               # Gemini (default)
    "https://gemini.google.com/app?model=2.5-pro", # Gemini 2.5 Pro mode
    "https://gemini.google.com/app/deepresearch",  # Deep Research mode
    "https://chatgpt.com/",                        # ChatGPT
    "https://copilot.microsoft.com/",              # Microsoft Copilot
    "https://openrouter.ai/chat",                  # OpenRouter chat UI
    "http://localhost:3000/",                      # Ollama Web UI (if running)
    "http://127.0.0.1:1234/",                      # LM Studio local server UI
]


def start() -> bool:
    """Launch Chrome with CDP + all AI tabs. Returns True on success."""
    from gemini_coder_web.cdp_client import (
        launch_chrome_with_cdp,
        discover_cdp_targets,
        DEFAULT_CDP_PORT,
    )

    # If Chrome is already up with CDP, reuse it — just add missing tabs
    existing = discover_cdp_targets(DEFAULT_CDP_PORT)
    if existing:
        print(f"[start_chrome] Chrome already running on :{DEFAULT_CDP_PORT} "
              f"with {len(existing)} tabs — reusing.")
    else:
        # First tab is the one Chrome launches with; rest get opened via /json/new
        print("[start_chrome] launching Chrome with CDP…")
        ok = launch_chrome_with_cdp(url=AI_TABS[0], port=DEFAULT_CDP_PORT)
        if not ok:
            print("[start_chrome] launch failed — Chrome may not be installed "
                  "at the expected path. See cdp_client.launch_chrome_with_cdp "
                  "for details.")
            return False
        # Wait for Chrome to open
        for _ in range(30):
            time.sleep(0.5)
            if discover_cdp_targets(DEFAULT_CDP_PORT):
                break

    # Open the remaining tabs via CDP /json/new
    import urllib.request, urllib.parse
    existing_urls = {t.url for t in discover_cdp_targets(DEFAULT_CDP_PORT)}
    opened = 0
    for url in AI_TABS:
        # Rough match — don't reopen if any existing tab shares the same domain
        if any(url.split("?")[0].rstrip("/") in e for e in existing_urls):
            continue
        endpoint = (f"http://127.0.0.1:{DEFAULT_CDP_PORT}/json/new"
                    f"?{urllib.parse.quote(url, safe=':/?=&')}")
        try:
            req = urllib.request.Request(endpoint, method="PUT")
            with urllib.request.urlopen(req, timeout=5) as _:
                opened += 1
                print(f"[start_chrome]   opened {url}")
        except Exception as e:
            print(f"[start_chrome]   failed {url}: {e}")

    print(f"[start_chrome] Done. {opened} new tabs opened. "
          f"Log into any that show a login page — cookies persist for "
          f"future runs.")
    return True


if __name__ == "__main__":
    start()
