"""
BidFTA personal monitor (CDP) — reads a logged-in browser tab; does not place bids.

Why this is not an "eBay sniper"
--------------------------------
BidFTA (and many liquidation auction sites) add time when bids arrive near the
scheduled end, so the closing stretch can last a long time. "Win in the last
2–3 minutes" is a mindset from fixed-end auctions. Here, persistence, max
bid, and whether you are willing to ride extensions matter more. Read the
official FAQ: https://www.bidfta.com/faq

Compliance
----------
You are responsible for BidFTA's Terms of Service. This script only polls the
open tab's text and prints alerts. Do not use it to automate bidding or
circumvent site rules. Prefer the site Watch List, mobile notifications, and
Max Bid for serious participation.

Run
---
1) Start Chrome with remote debugging, e.g.:
     chrome.exe --remote-debugging-port=9222
2) Log into bidfta.com and open a lot or search page in a tab.
3) From the Autocoder repo root:
     python examples/bidfta_watch/watch_bidfta.py --config examples/bidfta_watch/config.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Repo root: .../Autocoder
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cdp_client import (  # noqa: E402
    CDPConnection,
    DEFAULT_CDP_PORT,
    find_target_by_url,
)

log = logging.getLogger("bidfta_watch")


@dataclass
class WatchConfig:
    cdp_port: int
    url_substring: str
    poll_seconds: float
    keywords: list[str]
    max_current_bid_usd: float
    min_title_chars: int

    @classmethod
    def from_path(cls, path: Path) -> "WatchConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            cdp_port=int(data.get("cdp_port", DEFAULT_CDP_PORT)),
            url_substring=str(data.get("url_substring", "bidfta")).lower(),
            poll_seconds=float(data.get("poll_seconds", 20)),
            keywords=[str(x).lower() for x in data.get("keywords", [])],
            max_current_bid_usd=float(data.get("max_current_bid_usd", 10_000)),
            min_title_chars=int(data.get("min_title_chars", 1)),
        )


def _load_body_text(conn: CDPConnection) -> str:
    try:
        return str(
            conn.evaluate_js(
                "document.body ? (document.body.innerText || '') : ''"
            )
            or ""
        )
    except Exception:
        return ""


def _parse_money_values(text: str) -> list[float]:
    # Capture $12, $12.50, 12.50, USD 20 — best-effort; pages vary.
    found: list[float] = []
    for m in re.finditer(
        r"(?:USD|\$)\s*([0-9]+(?:[.,][0-9]{1,2})?)|\b([0-9]+(?:[.,][0-9]{1,2})?)\s*USD",
        text,
        re.I,
    ):
        g = m.group(1) or m.group(2) or ""
        g = g.replace(",", "")
        try:
            found.append(float(g))
        except ValueError:
            continue
    if not found:
        for m in re.finditer(r"\$([0-9]+(?:[.,][0-9]{1,2})?)", text):
            g = m.group(1).replace(",", "")
            try:
                found.append(float(g))
            except ValueError:
                continue
    return found


def _min_relevant_bid(numbers: list[float], cap: float) -> float | None:
    """Prefer the smallest plausible 'current' number under the configured cap."""
    under = [n for n in numbers if n <= cap and n > 0]
    if not under:
        return None
    return min(under)


def _keyword_hits(haystack: str, keywords: list[str]) -> list[str]:
    h = haystack.lower()
    return [k for k in keywords if k and k in h]


def run_loop(cfg: WatchConfig) -> None:
    while True:
        target = find_target_by_url(cfg.url_substring, port=cfg.cdp_port)
        if not target:
            log.warning(
                "No tab URL contains %r on port %s — open bidfta.com in Chrome "
                "with --remote-debugging-port=%s",
                cfg.url_substring,
                cfg.cdp_port,
                cfg.cdp_port,
            )
            time.sleep(cfg.poll_seconds)
            continue

        conn = CDPConnection(target.ws_url)
        if not conn.connect():
            time.sleep(3)
            continue
        try:
            title = conn.get_page_title()
            body = _load_body_text(conn)
            if len(title) < cfg.min_title_chars and len(body) < 20:
                log.info("Page still loading or empty; retrying…")
            else:
                numbers = _parse_money_values(body)
                low = _min_relevant_bid(numbers, cfg.max_current_bid_usd)
                hits = _keyword_hits(title + " " + body[:8000], cfg.keywords)
                if low is not None and (not cfg.keywords or hits):
                    log.info(
                        "WATCH: title=%r | under_cap=%.2f (min seen=%.2f) | "
                        "kw=%s | url=%s",
                        title[:120],
                        cfg.max_current_bid_usd,
                        low,
                        hits or "(no keyword filter hit)",
                        conn.get_page_url()[:200],
                    )
                else:
                    log.debug("tick: no alert | title=%r", title[:80])
        finally:
            conn.disconnect()
        time.sleep(cfg.poll_seconds)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.json"),
        help="JSON config (copy from config.example.json)",
    )
    args = ap.parse_args()
    if not args.config.is_file():
        log.error("Missing %s — copy config.example.json and edit.", args.config)
        return 1
    cfg = WatchConfig.from_path(args.config)
    log.info(
        "BidFTA CDP watch — port=%s pattern=%r — Ctrl+C to stop. "
        "This does not bid; it only logs when numbers look 'cheap' under your cap.",
        cfg.cdp_port,
        cfg.url_substring,
    )
    try:
        run_loop(cfg)
    except KeyboardInterrupt:
        log.info("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
