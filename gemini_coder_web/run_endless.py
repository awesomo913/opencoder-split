"""Endless Autocoder runner — headless, full options.

- Uses existing Chrome CDP on port 9222 with the Gemini tab already loaded.
- All 8 improvement focuses enabled.
- Perfection Loop ON (cycle through all focuses, repeat until no more gains).
- Expand on stagnation ON (auto-create new companion modules if code plateaus).
- Endless mode (no time limit, no max_iterations cap — stops only on Ctrl+K,
  5 consecutive errors, or manual kill).

Run: pythonw run_endless.py  (detached — logs to ~/.autocoder/endless.log)
     python  run_endless.py  (with console — stdout as well)

Session length defaults to **48 hours** then stops cleanly. Override with
``AUTOCODER_SESSION_HOURS`` (use ``0`` for unlimited). Optional env:
``AUTOCODER_APEX_FRONTIER=1`` enables Apex Frontier overdrive;
``AUTOCODER_FRONTIER_LENS`` may be a frontier lens key (e.g. ``plugins_extensions``).
Set ``OPENEDCLAW_ROOT`` to embed OpenedClaw HANDOFF/AGENTS excerpts.
"""
import os
import sys
import time
import logging
import signal
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "claude_interaction_tool"))

# Log file — separate from autocoder.log so it's easy to tail just this run
log_dir = Path.home() / ".autocoder"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "endless.log"
handlers = [logging.FileHandler(log_file, encoding="utf-8")]
if sys.stdout is not None and hasattr(sys.stdout, "write"):
    try:
        handlers.append(logging.StreamHandler(sys.stdout))
    except Exception:
        pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=handlers,
)
logger = logging.getLogger("run_endless")


def _env_truthy(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def session_limit_minutes() -> int:
    """AUTOCODER_SESSION_HOURS — default 48; 0 or negative means no time cap."""
    raw = (os.environ.get("AUTOCODER_SESSION_HOURS") or "48").strip()
    try:
        h = float(raw)
    except ValueError:
        return 48 * 60
    if h <= 0:
        return 0
    return max(1, int(round(h * 60)))


from Autocoder.ai_profiles import (
    GEMINI_PROFILE, OPENROUTER_PROFILE, CLAUDE_PROFILE, CHATGPT_PROFILE,
)
from Autocoder.session_manager import SessionManager
from Autocoder.broadcast import (
    BroadcastController,
    BroadcastConfig,
    FOCUS_ORDER,
    normalize_frontier_lens,
)
import threading
import ctypes
from ctypes import wintypes

# Fix #23: global F10 kill switch. Registers a Windows hotkey so pressing
# F10 anywhere shuts the runner down cleanly. Also watches a KILL file
# (~/.autocoder/KILL) as a filesystem fallback for any UI to trigger a
# kill without needing a keyboard event (used by the GUI kill button).
_kill_flag = threading.Event()


def _request_shutdown(reason: str = "") -> None:
    """Set shutdown flag once and log why."""
    if reason:
        logger.warning("Shutdown requested: %s", reason)
    _kill_flag.set()


def _signal_handler(signum, _frame):
    """Handle OS signals for graceful shutdown."""
    _request_shutdown(f"signal {signum}")


def _sleep_with_kill_check(seconds: int) -> bool:
    """Sleep up to N seconds, returning False if shutdown requested."""
    for _ in range(max(0, int(seconds))):
        if _kill_flag.is_set():
            return False
        time.sleep(1)
    return not _kill_flag.is_set()

def _f10_hotkey_thread():
    """Register F10 as a global Windows hotkey in a message-pump thread."""
    user32 = ctypes.windll.user32
    VK_F10 = 0x79
    MOD_NOREPEAT = 0x4000
    HOTKEY_ID = 0xBEEF
    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_NOREPEAT, VK_F10):
        logger.warning("Failed to register F10 hotkey (GetLastError=%d)",
                       ctypes.GetLastError())
        return
    logger.info("F10 kill-switch registered")
    msg = wintypes.MSG()
    while not _kill_flag.is_set():
        # GetMessageW blocks until any message arrives
        res = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if res in (0, -1):
            break
        if msg.message == 0x0312 and msg.wParam == HOTKEY_ID:  # WM_HOTKEY
            logger.warning("F10 pressed — initiating shutdown")
            _kill_flag.set()
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    try:
        user32.UnregisterHotKey(None, HOTKEY_ID)
    except Exception:
        pass

def _kill_file_watcher():
    """Watch ~/.autocoder/KILL for filesystem kill signal."""
    kill_path = Path.home() / ".autocoder" / "KILL"
    while not _kill_flag.is_set():
        if kill_path.exists():
            logger.warning("KILL file detected at %s — shutting down", kill_path)
            _kill_flag.set()
            try:
                kill_path.unlink()
            except Exception:
                pass
            break
        time.sleep(3)


# Fix #26: session history. Every run appends a JSON line so the user has
# a permanent record of what they asked Autocoder to build and what came
# out, across all sessions and restarts.
SESSIONS_LOG = Path.home() / ".autocoder" / "sessions.jsonl"

def _record_session_start(mode: str, task: str, session_id: str) -> dict:
    """Write a 'start' line to ~/.autocoder/sessions.jsonl and return the record."""
    import json as _json
    from datetime import datetime as _dt
    rec = {
        "event": "start",
        "ts": _dt.now().isoformat(),
        "mode": mode,
        "task_preview": task[:200],
        "session_id": session_id,
        "pid": os.getpid(),
    }
    try:
        SESSIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SESSIONS_LOG.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(rec) + "\n")
    except Exception as e:
        logger.debug("session-history start write failed: %s", e)
    return rec

def _record_session_event(session_id: str, **kwargs) -> None:
    """Append a session event (iteration, save, switch, end, etc.)."""
    import json as _json
    from datetime import datetime as _dt
    rec = {"ts": _dt.now().isoformat(), "session_id": session_id, **kwargs}
    try:
        with SESSIONS_LOG.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(rec) + "\n")
    except Exception:
        pass

# Fix #19: profile rotation order. Tried in sequence when current profile
# is rate-limited or unresponsive. Each profile holds a 30-min cooldown
# after triggering the circuit breaker.
PROFILE_ROTATION = [GEMINI_PROFILE, OPENROUTER_PROFILE, CLAUDE_PROFILE, CHATGPT_PROFILE]
COOLDOWN_SECONDS = 30 * 60

# Fix #27: build-mode prompts. Run with --mode=backend|gui|full (default full)
# or set AUTOCODER_MODE env var. The GUI's new Backend/GUI/Full buttons just
# relaunch this script with the matching mode.
MODE = os.environ.get("AUTOCODER_MODE", "full").lower().strip()
MODEL_ROTATION_ENABLED = False
MODEL_ROTATION_INTERVAL_MINUTES = 12
MODEL_ROTATION_RANDOM = True
for _arg in sys.argv[1:]:
    if _arg.startswith("--mode="):
        MODE = _arg.split("=", 1)[1].lower().strip()
    elif _arg.startswith("--model-rotation="):
        MODEL_ROTATION_ENABLED = _arg.split("=", 1)[1].strip() in ("1", "true", "yes", "on")
    elif _arg.startswith("--model-rotation-min="):
        try:
            MODEL_ROTATION_INTERVAL_MINUTES = max(1, int(_arg.split("=", 1)[1].strip()))
        except Exception:
            pass
    elif _arg.startswith("--model-rotation-random="):
        MODEL_ROTATION_RANDOM = _arg.split("=", 1)[1].strip() in ("1", "true", "yes", "on")

PROMPT_BACKEND = """Build a single-file Python module called coding_essentials_backend.py — a stdlib-only library a developer imports WHILE writing other apps. NO GUI code, NO tkinter. Target: one file, under 1000 lines, pure stdlib only.

DELIVERABLE — five integrated backend subsystems:

1. EFFICIENCY: @timed (ring-buffer timing), @memoize (TTL + LRU), debounce/throttle decorators, Batched() context manager, lazy_import helper, a Profiler class with get_stats()/export_csv()/reset() methods.

2. EVENT SYSTEM: EventBus with on/emit/off; a reactive Store (dataclass-backed state + subscribe() with shallow-diff so callbacks only fire on changed slices); UndoStack with push/undo/redo.

3. CONCURRENCY: run_in_thread(fn, on_done, on_error) with a callback-marshaling layer; a tiny ThreadSafeDict; a SimpleTaskQueue for background work.

4. DATA UTILITIES: JSON/CSV readers that auto-recover from partial writes; a TinyKVStore backed by pathlib; simple schema validation decorator; safe_call wrapper.

5. RELIABILITY: circuit-breaker decorator; exponential-backoff retry; structured-logging setup; graceful-shutdown context manager; self-test harness.

CROSS-CUTTING: every public symbol has type hints + docstring with a usage example. A single if __name__ == '__main__': at the bottom runs --selftest and prints PASS/FAIL per module.

Output: the complete coding_essentials_backend.py in a single fenced code block. No prose."""

PROMPT_GUI = """Build a single-file Python program called coding_essentials_gui.py — a VISUAL-ONLY toolkit. Assumes a backend library (imported as `be`) exists with EventBus, Store, UndoStack, run_in_thread, @timed, @memoize, Profiler classes/functions. This file contains ONLY the UI layer.

DELIVERABLE — five integrated UI subsystems in one file:

1. THEME: Theme dataclass (colors dict, spacing scale xs/sm/md/lg, fonts ui/mono/heading); light + dark presets; Theme is a Store so toggling re-themes live.

2. STYLED WIDGETS: Button, Card, Toast, Badge, Divider, StatusDot — all subclass customtkinter, all theme-reactive, all have hover/pressed states, smooth .after()-based fade-in for Toast.

3. FORMS: FormBuilder that turns a dataclass into a CTk form (label + input per field, two-way bound to a backend Store), with validation hooks.

4. TUTORIAL: Walkthrough(steps) renders dimmed overlay + cutout around a target widget + tooltip bubble + Next/Skip/Back; persists completed tutorials to ~/.coding_essentials/state.json so each only fires once.

5. DEMO APP: 3-tab window (Playground, Profiler, Settings). Playground exercises every widget. Profiler shows the backend's ring-buffer timing as a sortable table. Settings has theme picker + reset-tutorials + version.

CROSS-CUTTING: every public class has docstring + usage example. Graceful shutdown on window close. --selftest builds the app headlessly and prints PASS/FAIL per subsystem without displaying any window.

Output: the complete coding_essentials_gui.py in a single fenced code block. No prose."""

PROMPT_FULL = """Build a single-file Python program called coding_essentials.py — a lightweight toolkit a developer uses WHILE writing other apps. It must run standalone (python coding_essentials.py) and expose both a GUI and an importable API. Target: one file, under 1500 lines, stdlib + customtkinter only (no external deps beyond what a typical Python install has).

DELIVERABLE — five integrated subsystems in one file:

1. EFFICIENCY CORE
   - @timed decorator that logs call duration to a ring buffer
   - @memoize with TTL + max size (LRU eviction)
   - Debounce / throttle decorators for UI callbacks
   - Batched() context manager that coalesces repeated operations
   - lazy_import(name) helper so heavy modules load on first use
   - tiny Profiler that shows per-function call count + total ms in a panel

2. GUI BACKEND KIT
   - EventBus with .on(event, fn) / .emit(event, *a) / .off(handle)
   - Store class: dataclass-backed reactive state, subscribe(fn) fires on change, with shallow diff so listeners only run when their slice actually changes
   - UndoStack with push/undo/redo + keyboard bindings
   - run_in_thread(fn, on_done=..., on_error=...) that marshals results back to the tk mainloop safely
   - FormBuilder that turns a dataclass into a customtkinter form (label + input per field, two-way bound to a Store)

3. VISUAL POLISH
   - Theme: dict of colors (bg, fg, accent, muted, success, warn, error) + spacing scale (xs/sm/md/lg) + 3 fonts (ui / mono / heading). Light + dark presets. Theme is a Store so toggling re-themes live.
   - Styled wrappers: Button, Card, Toast, Badge, Divider, StatusDot — all use the theme Store, all have hover/pressed states
   - Smooth fade-in for toasts using .after() frames (no external animation libs)
   - Empty-state component for "no data yet" panels (icon + message + primary action)

4. TUTORIAL MODULE
   - Walkthrough(steps) where each step is {target_widget, title, body, placement}
   - Renders a dimmed overlay with a cutout around the target widget, a tooltip bubble with title/body, and Next / Skip / Back buttons
   - Persists "completed tutorials" to ~/.coding_essentials/state.json so a tutorial only fires once unless reset
   - First-run flow: on first launch, a 4-step tour of the main window fires automatically

5. COHESIVE DEMO APP
   - Main window with three tabs: Playground, Profiler, Settings
   - Playground: a live demo that uses EVERY other subsystem — a small form (FormBuilder), a task list (Store + UndoStack), a "run slow function" button (timed + run_in_thread + toast on completion), theme toggle
   - Profiler: shows the ring buffer from @timed as a sortable table with a "Clear" and "Export CSV" button
   - Settings: theme picker, "Reset tutorials" button, version info

CROSS-CUTTING REQUIREMENTS
   - Every subsystem has a short docstring with a usage example
   - Every public function has type hints
   - A single main() at the bottom: builds the demo app, wires first-run tutorial, runs mainloop
   - No silent except: blocks — log exceptions to ~/.coding_essentials/app.log
   - Graceful shutdown: flush profiler ring buffer + state.json on close
   - Self-test: if run with --selftest, exercise each subsystem headlessly and print PASS/FAIL per module, exit 0 on all pass

IMPROVEMENT LOOP GUIDANCE
   - Each iteration: KEEP the single-file layout. Do not split into packages.
   - Add features only if they strengthen an EXISTING subsystem or make subsystems work together better. Do not invent a 6th subsystem.
   - If a feature feels heavy, prefer a leaner primitive over a framework.
   - On every pass, verify the demo app still exercises every subsystem — if a subsystem has no demo, that's the next thing to fix.
   - Record your own changes in a CHANGELOG comment block at the top of the file (one line per iteration: date + what changed + why).

OUTPUT
   - Return the complete coding_essentials.py file in a single code block. No prose around it."""


def main() -> int:
    logger.info("=" * 60)
    logger.info("ENDLESS AUTOCODER RUNNER starting")
    logger.info("=" * 60)

    # Fix #27: pick prompt based on MODE (backend|gui|full)
    PROMPTS = {"backend": PROMPT_BACKEND, "gui": PROMPT_GUI, "full": PROMPT_FULL}
    PROMPT = PROMPTS.get(MODE, PROMPT_FULL)
    logger.info("MODE=%s (prompt=%d chars)", MODE, len(PROMPT))

    # Fix #24b: if the user typed a prompt in the Autocoder GUI, it lives at
    # ~/.autocoder/last_prompt.txt. When set, it overrides the baked-in
    # mode prompt so the CLI and GUI stay in sync.
    last_prompt_file = Path.home() / ".autocoder" / "last_prompt.txt"
    if last_prompt_file.exists():
        try:
            saved = last_prompt_file.read_text(encoding="utf-8").strip()
            if len(saved) > 80 and not saved.startswith("e.g., "):
                PROMPT = saved
                logger.info("Loaded user prompt from last_prompt.txt (%d chars)", len(PROMPT))
        except Exception as e:
            logger.debug("last_prompt.txt read failed: %s", e)

    # 1) Session via the canonical SessionManager flow
    sm = SessionManager()
    sess = sm.create_session(GEMINI_PROFILE, "top-left")
    sid = sess.session_id
    if not sm.configure_session(sid):
        logger.error("Could not configure session — is Chrome running with port 9222 and Gemini loaded?")
        return 1
    client = sess.client
    logger.info("Session %s configured. using_cdp=%s", sid, client.using_cdp)

    # Fix #26: record session start in ~/.autocoder/sessions.jsonl
    _record_session_start(MODE, PROMPT, sid)

    _tlim = session_limit_minutes()
    logger.info(
        "Session time limit: %s",
        f"{_tlim} min (~{_tlim / 60:.1f} h)" if _tlim else "none (AUTOCODER_SESSION_HOURS=0)",
    )

    # 2) Full-options config
    config = BroadcastConfig(
        task=PROMPT,
        build_target="PC Desktop App",
        enhancements=list(FOCUS_ORDER),              # All 8 focuses
        selected_focuses=list(FOCUS_ORDER),          # All 8 for Perfection Loop
        session_ids=[sid],
        endless=True,                                 # Never stop from time/iter caps
        max_iterations=999,                           # Safety ceiling
        time_limit_minutes=_tlim,                     # Default 48h; 0 = unlimited
        perfection_loop=True,                         # Cycle through all focuses
        expand_on_stagnation=True,                    # Expand on plateau
        frontier_overdrive=_env_truthy("AUTOCODER_APEX_FRONTIER"),
        frontier_lens=normalize_frontier_lens(os.environ.get("AUTOCODER_FRONTIER_LENS", "")),
        model_rotation_enabled=MODEL_ROTATION_ENABLED,
        model_rotation_interval_minutes=MODEL_ROTATION_INTERVAL_MINUTES,
        model_rotation_random=MODEL_ROTATION_RANDOM,
    )

    bc = BroadcastController(sm)
    for _sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is not None:
            try:
                signal.signal(_sig, _signal_handler)
            except Exception:
                pass
    # Start kill listeners early so shutdown works even during startup/resume.
    threading.Thread(target=_f10_hotkey_thread, daemon=True).start()
    threading.Thread(target=_kill_file_watcher, daemon=True).start()

    # Restart "where it left off": load the last GOOD codebase from
    # ~/Downloads. The biggest plain-text iteration file with Python keywords
    # wins. We hand it to BroadcastController.resume() via a synthesized
    # state file so the loop continues improving instead of starting fresh.
    import json
    dl = Path.home() / "Downloads"
    # Fix #25c: skip RAW_ companions AND transcript-leak files (saved
    # earlier before Fix #25 was in place)
    TRANSCRIPT_MARKERS_SEED = ("Ran a command", "Ran 2 commands", "Read a file",
                                 "Edited the ", "Ran the following commands")
    raw_candidates = sorted(
        [p for p in dl.glob("Gemini_top-left_singlefile_python_program_called_codingessentialsp_*.txt")
         if not p.name.startswith("RAW_")],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    candidates = []
    for p in raw_candidates:
        try:
            sample = p.read_text(encoding="utf-8", errors="ignore")[:5000]
            if any(m in sample for m in TRANSCRIPT_MARKERS_SEED):
                logger.info("Resume seed: skipping transcript-leak file %s", p.name)
                continue
            candidates.append(p)
        except Exception:
            continue
    seed_codebase = ""
    if candidates:
        best = candidates[0]
        text = best.read_text(encoding="utf-8", errors="ignore")
        # Strip the leading metadata header (lines that start with "# ...")
        lines = text.splitlines()
        i = 0
        while i < len(lines) and lines[i].startswith("# "):
            i += 1
        # Skip an optional separator line
        if i < len(lines) and lines[i].strip() == "# ---":
            i += 1
        seed_codebase = "\n".join(lines[i:]).strip()
        if len(seed_codebase) >= 1500 and any(t in seed_codebase for t in ("def ", "class ", "import ")):
            logger.info("Resume seed: %s (%d chars)", best.name, len(seed_codebase))
            # Write into broadcast_state.json so .resume() picks it up
            state_path = Path.home() / ".autocoder" / "broadcast_state.json"
            state_data = {
                "task": PROMPT,
                "config": {
                    "task": PROMPT,
                    "build_target": "PC Desktop App",
                    "enhancements": list(FOCUS_ORDER),
                    "selected_focuses": list(FOCUS_ORDER),
                    "session_ids": [sid],
                    "endless": True,
                    "max_iterations": 999,
                    "time_limit_minutes": _tlim,
                    "perfection_loop": True,
                    "expand_on_stagnation": True,
                    "attached_files": [],
                    "context": "",
                    "mode": "uniform",
                    "frontier_overdrive": _env_truthy("AUTOCODER_APEX_FRONTIER"),
                    "frontier_lens": normalize_frontier_lens(
                        os.environ.get("AUTOCODER_FRONTIER_LENS", "")
                    ),
                    "outside_box_mode": _env_truthy("AUTOCODER_APEX_FRONTIER"),
                    "reference_adjacent_mode": True,
                },
                "sessions": {
                    "top-left": {
                        "iteration": 1,
                        "codebase": seed_codebase,
                    }
                },
            }
            state_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
            logger.info("Wrote resume state to %s", state_path)
        else:
            logger.warning("Best seed file failed validation, starting fresh")
            seed_codebase = ""
    else:
        logger.warning("No prior iteration files found in Downloads, starting fresh")

    iteration_counter = {"n": 0}
    placeholder_streak = {"n": 0}  # Fix #18: track placeholder replies
    error_streak = {"n": 0}        # Fix #21: track CDP/transport errors
    # Fix #19: per-profile cooldown timestamps. profile_name -> epoch when
    # cooldown ends. A profile is "available" if its name isn't in here, or
    # the cooldown timestamp is in the past.
    cooldown_until: dict = {}

    def on_iteration(*args, **kwargs):
        n = args[1] if len(args) >= 2 else 0
        iteration_counter["n"] = n
        label = args[2] if len(args) >= 3 else ""
        _record_session_event(sid, event="iteration", iteration=n, label=str(label))
        logger.info("=== ITERATION %d (%s) ===", n, label)

    def _next_available_profile(current_name):
        """Pick the next profile that's not on cooldown and has a CDP tab."""
        from Autocoder.cdp_client import discover_cdp_targets
        try:
            targets = discover_cdp_targets(9222)
        except Exception:
            targets = []
        now = time.time()
        # Build rotation starting AFTER current profile to avoid retrying it
        try:
            cur_idx = next(i for i, p in enumerate(PROFILE_ROTATION) if p.name == current_name)
        except StopIteration:
            cur_idx = -1
        ordered = PROFILE_ROTATION[cur_idx + 1:] + PROFILE_ROTATION[:cur_idx + 1]
        for p in ordered:
            if cooldown_until.get(p.name, 0) > now:
                continue
            if p.url_pattern and not any(p.url_pattern in t.url for t in targets):
                continue  # no live tab for this profile
            return p
        return None

    def on_output(sid, kind, text):
        if kind == "result":
            n = len(text)
            logger.info("[output] %d chars", n)
            # Fix #19: circuit breaker with profile rotation. After 5
            # placeholder replies from the current profile, mark it on
            # 30-min cooldown and switch to the next available profile
            # that has a live CDP tab. If none, sleep until something frees.
            if n < 200:
                placeholder_streak["n"] += 1
                if placeholder_streak["n"] >= 5:
                    sess = sm.get_session(sid)
                    if not sess or not sess.client:
                        return
                    cur_name = sess.client._profile.name
                    cooldown_until[cur_name] = time.time() + COOLDOWN_SECONDS
                    logger.warning(
                        "[circuit-breaker] %d placeholder replies from %s — "
                        "30-min cooldown engaged",
                        placeholder_streak["n"], cur_name,
                    )
                    placeholder_streak["n"] = 0
                    next_p = _next_available_profile(cur_name)
                    if next_p is None:
                        logger.warning(
                            "[circuit-breaker] no other profile available — "
                            "stopping broadcast, sleeping until next cron check"
                        )
                        try:
                            bc.stop()
                        except Exception:
                            pass
                        return
                    logger.info("[circuit-breaker] switching profile %s -> %s",
                                cur_name, next_p.name)
                    try:
                        bc.stop()
                    except Exception:
                        pass
                    time.sleep(2)
                    if sess.client.switch_profile(next_p):
                        sess.ai_profile = next_p
                        sess.is_configured = True
                        try:
                            bc.resume()
                            logger.info("[circuit-breaker] resumed on %s", next_p.name)
                        except Exception as e:
                            logger.error("[circuit-breaker] resume failed: %s", e)
                    else:
                        logger.error("[circuit-breaker] switch_profile to %s failed", next_p.name)
            else:
                placeholder_streak["n"] = 0
        elif kind == "error":
            logger.warning("[%s] %s", kind, text.strip()[:200])
            # Fix #21: also rotate on CDP error streaks (separate from
            # placeholder-reply streaks). 3 consecutive transport errors
            # likely means the tab is gone or unresponsive.
            error_streak["n"] += 1
            if error_streak["n"] >= 3:
                sess = sm.get_session(sid)
                if not sess or not sess.client:
                    return
                cur_name = sess.client._profile.name
                cooldown_until[cur_name] = time.time() + COOLDOWN_SECONDS
                logger.warning(
                    "[circuit-breaker:errors] %d errors from %s — "
                    "30-min cooldown engaged",
                    error_streak["n"], cur_name,
                )
                error_streak["n"] = 0
                next_p = _next_available_profile(cur_name)
                if next_p is None:
                    logger.warning(
                        "[circuit-breaker:errors] no other profile available — "
                        "stopping broadcast"
                    )
                    try:
                        bc.stop()
                    except Exception:
                        pass
                    return
                logger.info("[circuit-breaker:errors] switching profile %s -> %s",
                            cur_name, next_p.name)
                try:
                    bc.stop()
                except Exception:
                    pass
                time.sleep(2)
                if sess.client.switch_profile(next_p):
                    sess.ai_profile = next_p
                    sess.is_configured = True
                    try:
                        bc.resume()
                        logger.info("[circuit-breaker:errors] resumed on %s", next_p.name)
                    except Exception as e:
                        logger.error("[circuit-breaker:errors] resume failed: %s", e)
                else:
                    logger.error("[circuit-breaker:errors] switch_profile to %s failed", next_p.name)

    bc.set_callbacks(
        on_output=on_output,
        on_status=lambda msg: logger.info("[status] %s", msg),
        on_iteration=on_iteration,
    )

    if seed_codebase:
        logger.info("Resuming broadcast from %d-char codebase + %d focuses + perfection_loop",
                    len(seed_codebase), len(FOCUS_ORDER))
        if not bc.resume():
            logger.warning("Resume returned False — falling back to fresh start")
            bc.start(config)
    else:
        logger.info("Starting broadcast: %d focuses + perfection_loop + expand_on_stagnation",
                    len(FOCUS_ORDER))
        bc.start(config)

    # 3) Watcher — just keep the process alive, log heartbeat every 60s
    # Fix #21: outer keep-alive loop. If the broadcast stops (e.g. 5
    # consecutive errors), wait a few minutes for any cooldown to clear,
    # then auto-resume so the runner doesn't go silent until the next cron
    # check intervenes.
    heartbeat_interval = 60
    last_iter = 0
    start_ts = time.time()
    auto_restart_attempts = 0
    try:
        while not _kill_flag.is_set():
            # Inner loop: heartbeat while broadcast runs
            while bc.is_running and not _kill_flag.is_set():
                if not _sleep_with_kill_check(heartbeat_interval):
                    break
                cur_iter = iteration_counter["n"]
                minutes = (time.time() - start_ts) / 60
                if cur_iter != last_iter:
                    logger.info("[heartbeat] running  iter=%d  elapsed=%.1fm", cur_iter, minutes)
                    last_iter = cur_iter
                else:
                    logger.info("[heartbeat] running  iter=%d (no change)  elapsed=%.1fm", cur_iter, minutes)

            if _kill_flag.is_set():
                logger.warning("Kill flag set — stopping broadcast and exiting")
                return 0

            # Broadcast stopped — auto-revive
            auto_restart_attempts += 1
            logger.warning(
                "Broadcast stopped after %d iterations in %.1fm — "
                "auto-restart attempt #%d in 90s",
                iteration_counter["n"], (time.time() - start_ts) / 60,
                auto_restart_attempts,
            )
            if auto_restart_attempts > 20:
                logger.error("Too many auto-restart attempts (%d) — giving up",
                             auto_restart_attempts)
                return 1
            if not _sleep_with_kill_check(90):
                logger.warning("Shutdown requested during restart delay")
                return 0
            try:
                if not bc.resume():
                    logger.warning("Resume returned False — falling back to start()")
                    bc.start(config)
                logger.info("Broadcast auto-restarted")
            except Exception as e:
                logger.error("Auto-restart failed: %s", e)
                if not _sleep_with_kill_check(120):
                    logger.warning("Shutdown requested after auto-restart failure")
                    return 0
    except KeyboardInterrupt:
        _request_shutdown("KeyboardInterrupt")
        return 0
    finally:
        try:
            if bc.is_running:
                bc.stop()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
