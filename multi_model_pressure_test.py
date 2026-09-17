"""Multi-model pressure test — run the same task through every free AI
one at a time, save each model's outputs with its name tagged, then
produce a side-by-side comparison report.

Purpose
-------
Stress-tests two things at once:
  1. THE MODELS — can each AI actually handle the full task + reference
     files, given the same inputs?
  2. THE SYSTEM — does the broadcast pipeline (CDP selectors, RAG
     retrieval, new_conversation, non-code detection, extraction) work
     across different AI web UIs, or does it silently degrade?

What it does
------------
For each AI profile below (skipping any whose tab isn't open / logged in):
  - Connect via CDP to its existing Chrome tab
  - Run N iterations (default 3) with pressure-relevant focuses:
      1. Initial Build       — can it do the task at all?
      2. Deep Code Dive      — can it clean up code?
      3. Pressure Test       — can it harden against failures?
  - For every iteration, record:
      - wall-clock time
      - response char count
      - extracted code char count (0 = non-code fallback)
      - any errors / stagnation markers
  - Save every output to Downloads tagged with the AI name

At the end writes:
  PRESSURE_TEST_REPORT_<timestamp>.md — side-by-side comparison table

Usage
-----
1. Open Chrome with:
       --remote-debugging-port=9222
2. In that Chrome, open + log into the AI sites you want to test
   (leave each in its own tab). Any tab you don't have open is skipped
   cleanly with a note in the report.
3. Run this script:
       python multi_model_pressure_test.py

Note: This is a read/write-to-your-browser automation. Don't use
accounts or tabs you aren't OK with being typed into.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Setup paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

LOG_PATH = Path.home() / ".autocoder" / "multi_model_pressure_test.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# Force UTF-8 on the stdout stream so Windows cp1252 doesn't choke on
# Unicode characters in log messages (═, ✓, →, etc.).
import io as _io
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # very old Python
    sys.stdout = _io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace",
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("multi_model")


# ── Configuration ───────────────────────────────────────────────────

# Same task as autostart_broadcast.py — keep in sync when that changes
TASK_PROMPT = """You are building AutoEmerald — an autonomous Pokemon Emerald play/test framework that drives mGBA 0.10.5 via GDB stub + Win32 input. Your complete specification is embedded above this task in the REFERENCE MATERIAL section. Every filename referenced below appears as a section header in that block — search for them in the embedded content, do NOT try to open them as files.

PHASE 1 — Build every function marked [-] in the 02_REQUIRED_FUNCTIONS section. Start with the P0 critical items from 12_ROADMAP: A* pathfinding (08_NAVIGATION), type-effective move picker (07_BATTLE_SYSTEM + 09_type_chart.py), and post-battle dialog handlers (09_UI_FLOWS).

PHASE 2 — Build P1 features: mart shopper, PC nurse heal walker, Gym 1 Roxanne walker, story event handlers through Rustboro. Wire each into play_daily.py's main loop.

PHASE 3 — Build P2 data readers (bag contents, pokedex, full party stats, battle results) and P3 quality tools (GDB health monitor, regression harness, instrumentation, replay).

RULES: Work only from the embedded REFERENCE MATERIAL. Extend existing MGBAAuto/MGBAControl/ReflexRunner APIs — never replace them. Output one complete consolidated Python file that imports and extends the existing codebase."""


# Reference files directory — must match autostart_broadcast.py
REF_DIR = Path(r"C:\Users\computer\Desktop\stolenEauto\coding for autoemerald")
CODE_DIR = REF_DIR / "code"


# Models to test. Expanded roster 2026-04-18.
#
# Included:
#   - All four Gemini modes (2.5 Pro, 2.5 Flash, Canvas, Deep Research)
#     user wants every mode tested head-to-head
#   - ChatGPT + Copilot (browser)
#   - OpenRouter API (rotates 14 free models internally)
#   - Groq + Cerebras (fast free Llama 3.3 70B)
#   - OpenCode (meta-router: DeepSeek/Anthropic/Groq via its creds)
#   - Ollama API (local, qwen2.5-coder or deepseek-coder-v2)
#
# Skipped:
#   - Gemini (generic) — superseded by the 4 mode variants
#   - DeepSeek direct — routed through OpenCode now
#   - Claude — per user request
MODELS_TO_TEST: list[str] = [
    "Gemini 2.5 Pro",           # browser, best Gemini quality
    "Gemini 2.5 Flash",         # browser, fast free Gemini
    "Gemini Canvas",            # browser, interactive edit mode
    "Gemini Deep Research",     # browser, multi-step reports
    "ChatGPT",                  # browser, cloud
    "Copilot",                  # browser, cloud
    "OpenRouter API",           # direct HTTP, rotates free models
    "Groq",                     # direct HTTP, fast free Llama
    "Cerebras",                 # direct HTTP, fastest free Llama
    "OpenCode",                 # local HTTP server, meta-router
    "Ollama API",               # local HTTP, qwen/deepseek-coder
    "Qwen Local",               # local LM Studio + Qwen3.5-35B
]


# Focuses to run per model. We pick a broad cross-section that probes
# different capabilities of each AI.
PRESSURE_FOCUSES: list[tuple[str, str]] = [
    ("initial_build", "Initial Build"),     # Can it do the task at all?
    ("pressure_test", "Pressure Test"),     # Can it harden edges?
    ("deep_dive", "Deep Code Dive"),        # Can it refactor/simplify?
    ("review_grade", "Review & Grade"),     # Can it self-critique?
]


# Wall-clock cap per model so a single hung model doesn't block the run.
PER_MODEL_TIMEOUT_SEC = 600  # 10 minutes


# ── Data ────────────────────────────────────────────────────────────

@dataclass
class IterationResult:
    """Outcome of a single iteration against one model."""
    focus: str
    elapsed_sec: float
    response_chars: int
    extracted_chars: int
    saved_path: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.extracted_chars > 100

    @property
    def is_code(self) -> bool:
        return self.extracted_chars > 0


@dataclass
class ModelResult:
    """Aggregate outcome for one model across all its iterations."""
    model: str
    tab_found: bool = False
    connect_ok: bool = False
    iterations: list[IterationResult] = field(default_factory=list)
    fatal_error: str = ""
    total_sec: float = 0.0

    @property
    def summary(self) -> dict:
        return {
            "model": self.model,
            "tab_found": self.tab_found,
            "connect_ok": self.connect_ok,
            "iterations_run": len(self.iterations),
            "iterations_ok": sum(1 for i in self.iterations if i.ok),
            "non_code_iterations": sum(
                1 for i in self.iterations if not i.is_code
            ),
            "total_extracted_chars": sum(
                i.extracted_chars for i in self.iterations
            ),
            "avg_sec_per_iter": round(
                (self.total_sec / len(self.iterations))
                if self.iterations else 0.0, 1,
            ),
            "total_sec": round(self.total_sec, 1),
            "fatal_error": self.fatal_error,
        }


# ── Helpers ─────────────────────────────────────────────────────────

def collect_files() -> list[str]:
    """Same logic as autostart_broadcast.py to keep inputs identical."""
    files: list[str] = []
    if REF_DIR.exists():
        files.extend(str(f) for f in sorted(REF_DIR.glob("*.txt")))
        if CODE_DIR.exists():
            files.extend(str(f) for f in sorted(CODE_DIR.glob("*.txt")))
    if not files:
        logger.warning("No reference files found under %s", REF_DIR)
    return files


# URLs for auto-opening tabs when a model isn't already open in Chrome.
# The CDP HTTP endpoint /json/new?url=... creates a new tab in the same
# browser instance, so we can spin up all 6 AIs automatically.
MODEL_URLS: dict[str, str] = {
    "Gemini":        "https://gemini.google.com/app",
    "ChatGPT":       "https://chatgpt.com/",
    "Claude":        "https://claude.ai/chats",
    "Copilot":       "https://copilot.microsoft.com/",
    "OpenRouter":    "https://openrouter.ai/chat",
    "Ollama Web UI": "http://localhost:3000/",
}


def open_tab_for_model(name: str, wait_sec: float = 8.0) -> bool:
    """Open a Chrome tab for this AI via CDP's /json/new endpoint.

    Returns True if the tab opened (it may still require login — we can't
    detect that from here; the pressure test will fail fast with an
    input-not-found error if login is needed).
    """
    url = MODEL_URLS.get(name)
    if not url:
        logger.warning("[%s] No URL configured — can't auto-open", name)
        return False

    import urllib.parse
    import urllib.request
    from gemini_coder_web.cdp_client import DEFAULT_CDP_PORT

    endpoint = (
        f"http://127.0.0.1:{DEFAULT_CDP_PORT}/json/new"
        f"?{urllib.parse.quote(url, safe=':/?=&')}"
    )
    try:
        req = urllib.request.Request(endpoint, method="PUT")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 400:
                logger.warning("[%s] /json/new returned %d", name, resp.status)
                return False
    except Exception as e:
        logger.warning("[%s] failed to open tab (%s): %s", name, url, e)
        return False

    logger.info("[%s] opened tab %s — waiting %.0fs for page to settle",
                name, url, wait_sec)
    time.sleep(wait_sec)
    return True


def connect_to_model(name: str, try_open: bool = True):
    """Return a client for the model, or None if unreachable.

    Delegates to the ProviderChain factory for this name — that way the
    pressure test always uses the same code path the production chain
    uses. New providers wired into provider_chain.build_chain_from_names
    are automatically testable here with zero changes to this function.
    """
    from gemini_coder_web.provider_chain import build_chain_from_names

    # build_chain_from_names looks up the factory by name and returns
    # a ProviderChain with one entry; we call its factory directly.
    chain = build_chain_from_names([name])
    if not chain.providers:
        logger.warning("[%s] no factory registered — check provider_chain.py", name)
        return None
    entry = chain.providers[0]
    try:
        client = entry.factory()
    except Exception as e:
        logger.warning("[%s] factory raised: %s", name, e)
        return None
    if client is None:
        # Factory explicitly returned None — the provider isn't available
        # (no key, no tab, no local server). Log but don't treat as error.
        logger.info("[%s] factory returned None (not configured / unreachable)",
                    name)
    return client


def run_one_iteration(
    session,
    controller,
    focus_key: str,
    focus_label: str,
    codebase: str,
    iteration: int,
    task: str,
) -> IterationResult:
    """Run a single iteration and return its result record."""
    ai_name = session.ai_profile.name
    start = time.time()

    # Build the prompt — initial iteration uses the full RAG retrieval;
    # later iterations use directive + codebase + manifest.
    if focus_key == "initial_build":
        # For initial, use the same retrieval-augmented prompt the real
        # broadcast uses on iteration 0.
        embedded = controller._build_retrieval_context(
            budget=26_000,
            query=task,
            focus_name="deep_dive",
            task_prompt=task,
        )
        prompt = f"{embedded}\n\n═══ YOUR TASK ═══\n{task}" if embedded else task
    else:
        # Pull the directive from IMPROVEMENT_FOCUSES
        from gemini_coder_web.broadcast import IMPROVEMENT_FOCUSES
        directive = IMPROVEMENT_FOCUSES[focus_key]["prompt"]
        prompt = controller._build_context_prompt(
            directive=directive,
            codebase=codebase,
            focus_name=focus_label,
            task_prompt=task,
        )

    # Cap prompt — most AI web UIs hard-limit ~32K chars of input
    if len(prompt) > 30_000:
        logger.info("[%s] Trimming prompt %d → 30000 chars", ai_name, len(prompt))
        prompt = prompt[:30_000]

    # Fresh conversation so each iteration has a clean slate (required for
    # Gemini, safe on the others; stateless on API clients)
    is_api_client = ai_name in (
        "Ollama Web UI", "OpenRouter",
        "Ollama API", "OpenRouter API",
        "Groq", "Cerebras", "OpenCode", "Qwen Local",
        "DeepSeek API", "Qwen API",
    )
    try:
        session.client.new_conversation()
        if not is_api_client:
            time.sleep(1)
    except Exception as e:
        logger.warning("[%s] new_conversation failed: %s", ai_name, e)

    try:
        response = session.client.generate(prompt=prompt)
    except Exception as e:
        return IterationResult(
            focus=focus_label,
            elapsed_sec=time.time() - start,
            response_chars=0,
            extracted_chars=0,
            error=str(e)[:300],
        )

    elapsed = time.time() - start
    extracted = controller._extract_code_blocks(
        response, previous_codebase=codebase,
    )

    # Save tagged with model name
    try:
        from gemini_coder_web.auto_save import save_task_output
        title = (
            f"PRESSURE_TEST_{ai_name.replace(' ', '_')}"
            f"_{focus_label.replace(' ', '_')}_v{iteration}"
        )
        path = save_task_output(
            title=title,
            output=extracted or response,
            ai_name=ai_name,
            corner=session.corner,
            elapsed_seconds=elapsed,
            iterations=iteration,
        )
        saved = str(path) if path else ""
    except Exception as e:
        logger.warning("[%s] save failed: %s", ai_name, e)
        saved = ""

    return IterationResult(
        focus=focus_label,
        elapsed_sec=elapsed,
        response_chars=len(response or ""),
        extracted_chars=len(extracted or ""),
        saved_path=saved,
    )


def run_one_model(model_name: str, attached_files: list[str]) -> ModelResult:
    """Run the full pressure test against a single model. Never raises.

    Handles two backends:
    - CDP browser tabs (Gemini, ChatGPT, Claude, Copilot, etc.)
    - Direct HTTP clients (Ollama API, OpenRouter API) — no browser
    """
    from gemini_coder_web.session_manager import SessionManager
    from gemini_coder_web.ai_profiles import get_profile, AIProfile
    from gemini_coder_web.broadcast import BroadcastController

    result = ModelResult(model=model_name)
    overall_start = time.time()

    logger.info("=" * 60)
    logger.info("PRESSURE TESTING: %s", model_name)
    logger.info("=" * 60)

    client = connect_to_model(model_name)
    if client is None:
        hints = {
            "Ollama API": "Make sure `ollama serve` is running with "
                          "at least one model pulled.",
            "OpenRouter API": "Set OPENROUTER_API_KEY env var or write "
                              "~/.autocoder/openrouter.key.",
            "Groq": "Set GROQ_API_KEY or write ~/.autocoder/groq.key. "
                    "Free keys at https://console.groq.com/keys.",
            "Cerebras": "Set CEREBRAS_API_KEY or write "
                        "~/.autocoder/cerebras.key. Free keys at "
                        "https://cloud.cerebras.ai/.",
            "OpenCode": "Install OpenCode from opencode.ai and ensure "
                        "opencode-cli.exe is at "
                        r"C:\Users\computer\AppData\Local\OpenCode\.",
            "Qwen API": "Set DASHSCOPE_API_KEY or write "
                        "~/.autocoder/qwen.key. Key at "
                        "https://dashscope.aliyun.com/.",
            "Qwen Local": "Install LM Studio + download Qwen3.5-35B-A3B-GGUF. "
                          "lms.exe must be at ~/.lmstudio/bin/. Check: "
                          "`lms ls` shows your models.",
        }
        hint = hints.get(
            model_name,
            f"No Chrome tab found for {model_name}. Open + log into it "
            f"in the CDP-enabled browser, then re-run.",
        )
        result.fatal_error = hint
        logger.warning("[%s] %s", model_name, result.fatal_error)
        return result

    result.tab_found = True

    # connect_to_model now returns fully-constructed adapters for EVERY
    # provider (CDP-wrapped or HTTP), so the session setup is uniform —
    # just plug the client in. No CDP-specific attribute juggling.
    sm = SessionManager()
    is_api_client = model_name in (
        "Ollama API", "OpenRouter API",
        "Groq", "Cerebras", "OpenCode", "Qwen Local",
        "DeepSeek API", "Qwen API",
    )
    # Pick a display profile. Gemini mode variants aren't in the profile
    # registry; fall back to the plain Gemini profile for labeling.
    profile = get_profile(model_name) or get_profile("Gemini") \
        or get_profile("Ollama Web UI")
    session = sm.create_session(profile, "top-left")
    session.client = client                # Every adapter quacks alike now
    session.is_configured = True
    result.connect_ok = True

    # Prime the controller with reference files (shared chunking cache)
    controller = BroadcastController(sm)
    controller._file_list = controller._read_attached_files(attached_files)
    controller._file_context = controller._format_file_context(controller._file_list)
    controller._chunks = []  # Rebuild on first retrieval call
    logger.info("[%s] Loaded %d reference files", model_name, len(controller._file_list))

    # Fresh page — browser clients only (API clients are stateless)
    if not is_api_client:
        try:
            client.new_conversation()
            time.sleep(2)
        except Exception as e:
            logger.warning("[%s] initial new_conversation: %s", model_name, e)

    codebase = ""
    for i, (focus_key, focus_label) in enumerate(PRESSURE_FOCUSES):
        # Respect per-model timeout
        if time.time() - overall_start > PER_MODEL_TIMEOUT_SEC:
            logger.warning("[%s] hit %ds timeout at iter %d",
                           model_name, PER_MODEL_TIMEOUT_SEC, i + 1)
            break

        logger.info("[%s] Iteration %d: %s", model_name, i + 1, focus_label)
        iter_result = run_one_iteration(
            session=session, controller=controller,
            focus_key=focus_key, focus_label=focus_label,
            codebase=codebase, iteration=i + 1, task=TASK_PROMPT,
        )
        result.iterations.append(iter_result)

        if iter_result.extracted_chars > 100:
            codebase = iter_result.saved_path and _read_text(iter_result.saved_path) or codebase
            # Use extracted content if readable, otherwise keep what we had

        logger.info(
            "[%s]   → %s: %ds, %d chars response, %d chars extracted%s",
            model_name, focus_label,
            int(iter_result.elapsed_sec),
            iter_result.response_chars,
            iter_result.extracted_chars,
            f" ERROR: {iter_result.error}" if iter_result.error else "",
        )

    # Cleanup
    try:
        session.client.cancel()
        sm.close_session(session.session_id)
    except Exception:
        pass

    result.total_sec = time.time() - overall_start
    return result


def _read_text(path: str) -> str:
    """Best-effort read, empty string on failure."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


# ── Report generation ───────────────────────────────────────────────

def _grade_from_summary(s: dict) -> str:
    """A rough letter grade for quick eyeballing."""
    if s["fatal_error"]:
        return "F (not run)"
    if not s["iterations_run"]:
        return "F (0 iters)"
    ok = s["iterations_ok"]
    total = s["iterations_run"]
    if ok == total and s["total_extracted_chars"] > 10_000:
        return "A"
    if ok >= total - 1 and s["total_extracted_chars"] > 5_000:
        return "B"
    if ok >= 1:
        return "C"
    return "D"


def write_report(results: list[ModelResult]) -> Path:
    """Side-by-side markdown comparison of every model."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path.home() / "Downloads"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"PRESSURE_TEST_REPORT_{ts}.md"

    lines: list[str] = [
        f"# Multi-Model Pressure Test Report",
        f"",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"## Summary",
        f"",
        f"| Model | Grade | Iters OK | Non-Code | Total Chars | Avg Sec/Iter | Fatal |",
        f"|---|---|---|---|---|---|---|",
    ]
    for r in results:
        s = r.summary
        lines.append(
            f"| {s['model']} | {_grade_from_summary(s)} | "
            f"{s['iterations_ok']}/{s['iterations_run']} | "
            f"{s['non_code_iterations']} | "
            f"{s['total_extracted_chars']:,} | "
            f"{s['avg_sec_per_iter']}s | "
            f"{s['fatal_error'][:50] if s['fatal_error'] else '—'} |"
        )
    lines.append("")

    # Per-model detail
    lines.append("## Per-Model Detail")
    lines.append("")
    for r in results:
        lines.append(f"### {r.model}")
        lines.append("")
        if r.fatal_error:
            lines.append(f"> **Fatal:** {r.fatal_error}")
            lines.append("")
            continue
        lines.append(
            f"Tab found: `{r.tab_found}` · "
            f"Connect OK: `{r.connect_ok}` · "
            f"Total time: `{r.total_sec:.1f}s`"
        )
        lines.append("")
        lines.append("| # | Focus | Time | Response chars | Extracted | OK | Error |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, it in enumerate(r.iterations, 1):
            lines.append(
                f"| {i} | {it.focus} | {it.elapsed_sec:.0f}s | "
                f"{it.response_chars:,} | {it.extracted_chars:,} | "
                f"{'✓' if it.ok else '✗'} | "
                f"{(it.error[:60] + '…') if it.error else '—'} |"
            )
        lines.append("")

    # Raw JSON dump for machine consumption
    lines.append("## Raw JSON")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(
        [r.summary for r in results], indent=2, ensure_ascii=False,
    ))
    lines.append("```")

    out.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Report saved to %s", out)
    return out


# ── Entry point ─────────────────────────────────────────────────────

def main() -> None:
    logger.info("=== Multi-Model Pressure Test ===")

    # Load user config and honor skip list + reset flag
    try:
        from gemini_coder_web.model_config import load_config, reset_state
        cfg = load_config()
        if cfg.reset_on_launch:
            logger.info("Reset on launch: %s", reset_state())
        skip = set(cfg.pressure_test.skip_models or [])
    except Exception as e:
        logger.warning("Config load failed (%s), using defaults", e)
        skip = set()

    effective_models = [m for m in MODELS_TO_TEST if m not in skip]
    logger.info("Models to test (after skip filter): %s", effective_models)

    attached = collect_files()
    logger.info("Collected %d reference files", len(attached))

    results: list[ModelResult] = []
    for model_name in effective_models:
        try:
            r = run_one_model(model_name, attached)
        except Exception as e:
            logger.exception("[%s] unhandled exception", model_name)
            r = ModelResult(model=model_name, fatal_error=f"unhandled: {e}")
        results.append(r)

    # Write report
    report_path = write_report(results)
    print(f"\n{'═' * 60}")
    print(f"Report: {report_path}")
    print(f"{'═' * 60}\n")

    # Brief console summary
    for r in results:
        s = r.summary
        grade = _grade_from_summary(s)
        print(
            f"  {s['model']:<12} {grade:<10} "
            f"{s['iterations_ok']}/{s['iterations_run']} ok  "
            f"{s['total_extracted_chars']:>7,} chars  "
            f"{s['total_sec']:>6.1f}s  "
            f"{s['fatal_error'][:50]}"
        )


if __name__ == "__main__":
    main()
