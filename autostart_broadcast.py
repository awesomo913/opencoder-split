"""Headless Autocoder launcher — starts broadcast with prompt + files, no GUI clicking.

Usage: python autostart_broadcast.py
  - Connects to existing Chrome CDP on port 9222
  - Attaches all 26 reference files from the AutoEmerald coding package
  - Starts the endless improvement loop with the full prompt
  - Opens the GUI so you can watch progress
"""

import sys
import os
import time
import logging
import threading
from pathlib import Path

# Setup paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

# Force UTF-8 on stdout so log messages with Unicode arrows/chars
# don't choke on Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    import io as _io
    sys.stdout = _io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace",
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(
            Path.home() / ".autocoder" / "autocoder.log", encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("autostart")

# ── The prompt ──────────────────────────────────────────────────
# IMPORTANT: Gemini's web UI has NO file system access. All reference
# material is EMBEDDED directly in the prompt by the broadcast system
# (see broadcast._build_smart_file_context). The filenames below refer to
# sections you can find within the REFERENCE MATERIAL block of your prompt,
# NOT files on disk. Never attempt to 'open', 'read', or 'fetch' any file.
TASK_PROMPT = """You are building AutoEmerald — an autonomous Pokemon Emerald play/test framework that drives mGBA 0.10.5 via GDB stub + Win32 input. Your complete specification is embedded above this task in the REFERENCE MATERIAL section (13 design docs + 12 existing Python source files). Every filename referenced below appears as a section header in that block — search for them in the embedded content, do NOT try to open them as files.

PHASE 1 — Build every function marked [-] in the 02_REQUIRED_FUNCTIONS section. Start with the P0 critical items from 12_ROADMAP: A* pathfinding (08_NAVIGATION), type-effective move picker (07_BATTLE_SYSTEM + 09_type_chart.py), and post-battle dialog handlers (09_UI_FLOWS).

PHASE 2 — Build the P1 features: mart shopper, PC nurse heal walker, Gym 1 Roxanne walker, and story event handlers through Rustboro. Wire each into play_daily.py's main loop.

PHASE 3 — Build P2 data readers (bag contents, pokedex, full party stats, battle results) and P3 quality tools (GDB health monitor, regression harness, instrumentation, replay).

RULES:
- Work exclusively from the embedded REFERENCE MATERIAL above. If a referenced section was not included in this turn's embedding (check the 'files not included this turn' note), skip that requirement and note it as a TODO in your code rather than hallucinating.
- Follow the architecture from the embedded 01_ARCHITECTURE section.
- Extend the existing MGBAAuto / MGBAControl / ReflexRunner APIs from the embedded code/ sections — never replace them.
- New modules must match the patterns in 03_REFLEX_FRAMEWORK.
- GBA memory addresses come from 06_MEMORY_MAP.
- Output one complete consolidated Python file per iteration that imports and extends the existing codebase.
- Include acceptance criteria from 12_ROADMAP as inline test assertions."""

# ── Collect reference files ─────────────────────────────────────
REF_DIR = Path(r"C:\Users\computer\Desktop\stolenEauto\coding for autoemerald")
CODE_DIR = REF_DIR / "code"


def collect_files() -> list[str]:
    files = []
    # Doc files (numbered .txt in root)
    for f in sorted(REF_DIR.glob("*.txt")):
        files.append(str(f))
    # Code files
    for f in sorted(CODE_DIR.glob("*.txt")):
        files.append(str(f))
    return files


def main() -> None:
    logger.info("=== Autocoder Headless Launcher ===")

    # Collect files
    attached = collect_files()
    logger.info("Collected %d reference files", len(attached))

    # Import after path setup
    from gemini_coder_web.cdp_client import DEFAULT_CDP_PORT
    from gemini_coder_web.session_manager import SessionManager
    from gemini_coder_web.ai_profiles import get_profile
    from gemini_coder_web.broadcast import BroadcastController, BroadcastConfig
    from gemini_coder_web.model_config import load_config, reset_state
    from gemini_coder_web.provider_chain import (
        build_chain_from_names, build_default_chain,
    )

    # ── Reset + load user config ─────────────────────────────────
    cfg = load_config()
    if cfg.reset_on_launch:
        logger.info("Reset on launch: %s", reset_state())

    # ── Build provider chain in user's ranked order ─────────────
    # Default (today's empirical ranking):
    #   OpenRouter API → Gemini → ChatGPT → Ollama → Copilot → DeepSeek → Qwen
    # Edit ~/.autocoder/models.json "provider_chain.order" to customize.
    if cfg.provider_chain.enabled and cfg.provider_chain.order:
        chain = build_chain_from_names(cfg.provider_chain.order)
        logger.info(
            "Provider chain (rank 1 -> N): %s",
            " -> ".join(p.name for p in chain.providers),
        )
    else:
        chain = build_default_chain()
        logger.info("Using default provider chain")

    # Probe availability up front so the user sees what's ready
    ready_count = 0
    for p in chain.providers:
        try:
            c = p.factory()
            if c is not None:
                p.client = c
                ready_count += 1
                logger.info("  [ready]    %s", p.name)
            else:
                logger.info("  [skipped]  %s  (not configured)", p.name)
        except Exception as e:
            logger.info("  [error]    %s  %s", p.name, str(e)[:80])
    if ready_count == 0:
        logger.error(
            "No providers are ready. Check the chain output above and "
            "configure at least one. Exiting."
        )
        sys.exit(1)
    logger.info("%d of %d providers ready", ready_count, len(chain.providers))

    # ── Create session manager + minimal session wrapper ────────
    # BroadcastController reaches in via session.client.*, so we plug
    # the chain in as session.client. It already quacks like
    # UniversalBrowserClient (.generate / .new_conversation / .cancel).
    sm = SessionManager()
    profile = get_profile("Gemini")  # profile is only used for display labels
    session = sm.create_session(profile, "top-left")
    session.client = chain                   # Plug the chain in
    session.is_configured = True
    logger.info("Session configured with provider chain")

    # No single-provider navigation needed — the chain handles
    # per-provider conversation resets inside generate() as appropriate
    # (CDP adapters call new_conversation before each send; API clients
    # are stateless).

    # ── Build broadcast config ──────────────────────────────────
    # AutoEmerald is an "Automation / Bot" — that preset pre-selects
    # the right focuses automatically. No hand-picking needed.
    # Override BUILD_TARGET env var to pick a different preset without
    # editing this file (useful for the same script to drive different
    # project types).
    from gemini_coder_web.broadcast import focuses_for_target
    build_target = os.environ.get("BUILD_TARGET", "Automation / Bot")
    selected = focuses_for_target(build_target)
    logger.info(
        "Build target: %s -> %d focuses auto-selected",
        build_target, len(selected),
    )
    config = BroadcastConfig(
        task=TASK_PROMPT,
        build_target=build_target,
        endless=True,
        expand_on_stagnation=True,
        selected_focuses=selected,
        perfection_loop=False,
        attached_files=attached,
        session_ids=[session.session_id],
    )
    logger.info("Selected %d improvement focuses: %s",
                len(selected), ", ".join(selected))

    # ── Set up broadcast controller ─────────────────────────────
    bc = BroadcastController(sm)

    iteration_count = [0]

    def on_output(sid, kind, text):
        logger.info("[OUTPUT %s] %s: %d chars", sid[:8], kind, len(text) if text else 0)

    def on_status(msg):
        logger.info("[STATUS] %s", msg)

    def on_iteration(sid, iteration, focus, ai_name):
        iteration_count[0] = iteration
        logger.info("[ITERATION %d] %s — focus: %s", iteration, ai_name, focus)

    def on_complete(counts=None):
        logger.info("=== Broadcast complete after %d iterations ===", iteration_count[0])

    bc.set_callbacks(
        on_output=on_output,
        on_status=on_status,
        on_iteration=on_iteration,
        on_complete=on_complete,
    )

    # ── Start! ──────────────────────────────────────────────────
    logger.info("Starting broadcast with %d files, task: %s...", len(attached), TASK_PROMPT[:80])
    bc.start(config)
    logger.info("Broadcast thread launched — monitoring...")

    # Keep alive until broadcast finishes or Ctrl+C
    try:
        while bc.is_running:
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Ctrl+C — stopping broadcast")
        bc.stop()

    logger.info("Done. Total iterations: %d", iteration_count[0])


if __name__ == "__main__":
    main()
