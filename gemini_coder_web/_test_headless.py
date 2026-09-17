"""Headless end-to-end test of patched Autocoder.

- Connects to existing Chrome CDP on port 9222 (already running with Gemini tab)
- Creates a Session bound to that tab
- Runs a full broadcast with the coding_essentials prompt + all 8 focuses
- Prints output + saves iterations via Autocoder's normal flow
- Exits after 2 iterations OR 25 minutes OR an unrecoverable error
"""
import sys
import time
import logging
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "claude_interaction_tool"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test_headless")

from Autocoder.ai_profiles import GEMINI_PROFILE
from Autocoder.session_manager import SessionManager
from Autocoder.broadcast import (
    BroadcastController, BroadcastConfig,
    IMPROVEMENT_FOCUSES, FOCUS_ORDER,
)

PROMPT = """Build a single-file Python program called coding_essentials.py — a lightweight toolkit a developer uses WHILE writing other apps. It must run standalone (python coding_essentials.py) and expose both a GUI and an importable API. Target: one file, under 1500 lines, stdlib + customtkinter only (no external deps beyond what a typical Python install has).

DELIVERABLE — five integrated subsystems in one file:

1. EFFICIENCY CORE: @timed decorator with ring buffer, @memoize with TTL + LRU, debounce/throttle, Batched() context manager, lazy_import helper, tiny Profiler panel.

2. GUI BACKEND KIT: EventBus (on/emit/off), Store (dataclass reactive state with shallow-diff subscribers), UndoStack with kb bindings, run_in_thread helper that marshals to mainloop, FormBuilder that turns dataclass into customtkinter form.

3. VISUAL POLISH: Theme Store (colors, spacing scale, 3 fonts, light+dark), styled wrappers (Button/Card/Toast/Badge/Divider/StatusDot) all theme-reactive with hover states, smooth .after()-based fade-in for toasts, empty-state component.

4. TUTORIAL MODULE: Walkthrough(steps) with dimmed overlay + cutout + tooltip bubble + Next/Skip/Back, persists completed tutorials to ~/.coding_essentials/state.json, first-run 4-step tour fires automatically.

5. COHESIVE DEMO APP: Main window with Playground, Profiler, Settings tabs. Playground exercises EVERY subsystem. Profiler shows ring buffer as sortable table with Clear + Export CSV. Settings has theme picker, reset tutorials, version info.

CROSS-CUTTING: docstrings with usage examples, type hints, single main() at bottom, no silent except (log to ~/.coding_essentials/app.log), graceful shutdown, --selftest flag exercises each subsystem headlessly and prints PASS/FAIL.

LOOP GUIDANCE: keep single-file, don't add a 6th subsystem, prefer leaner primitives over frameworks, verify demo exercises every subsystem each pass, CHANGELOG block at top.

OUTPUT: complete coding_essentials.py in a single code block, no prose."""


def main() -> int:
    logger.info("=== Headless Autocoder test ===")

    # 1) Create session + client via SessionManager (the canonical flow)
    sm = SessionManager()
    sess = sm.create_session(GEMINI_PROFILE, "top-left")
    sid = sess.session_id
    if not sm.configure_session(sid):
        logger.error("Could not configure session — Chrome may not have CDP on 9222, or Gemini tab isn't loaded")
        return 1
    client = sess.client
    logger.info("Session %s configured. is_configured=%s using_cdp=%s",
                sid, client.is_configured, client.using_cdp)

    # 3) Build broadcast with ALL 8 focuses
    config = BroadcastConfig(
        task=PROMPT,
        build_target="PC Desktop App",
        enhancements=list(FOCUS_ORDER),
        selected_focuses=list(FOCUS_ORDER),  # Needed for perfection_loop mode
        session_ids=[sid],
        perfection_loop=True,
    )

    bc = BroadcastController(sm)

    iteration_counter = {"n": 0}
    def on_output(sid, kind, text):
        if kind == "result":
            logger.info("[output] %d chars", len(text))
        else:
            logger.info("[%s] %s", kind, text[:120].replace("\n", " "))

    def on_iteration(*args, **kwargs):
        # Autocoder calls this with varying signatures: (sid, n, label, name)
        # for architect mode, (sid, n, code) for simple mode, etc. Accept any.
        n = args[1] if len(args) >= 2 else 0
        iteration_counter["n"] = n
        logger.info("=== ITERATION %d (args=%d) ===", n, len(args))

    bc.set_callbacks(
        on_output=on_output,
        on_status=lambda msg: logger.info("[status] %s", msg),
        on_iteration=on_iteration,
    )

    logger.info("Starting broadcast with %d focuses + Perfection Loop", len(FOCUS_ORDER))
    bc.start(config)

    # 4) Wait for 2 iterations OR 25 min OR death
    deadline = time.time() + 25 * 60
    while time.time() < deadline:
        time.sleep(20)
        logger.info("[watch] running=%s iterations=%d", bc.is_running, iteration_counter["n"])
        if iteration_counter["n"] >= 2:
            logger.info("=== TEST PASSED: 2 iterations completed ===")
            bc.stop()
            return 0
        if not bc.is_running:
            logger.error("=== TEST FAILED: broadcast stopped before 2 iterations ===")
            return 2

    logger.warning("=== TEST TIMED OUT after 25 min with %d iterations ===", iteration_counter["n"])
    bc.stop()
    return 3


if __name__ == "__main__":
    sys.exit(main())
