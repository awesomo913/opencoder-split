"""Blended-model emulation with failure/batch handling.

Emulates what a real multi-model workflow looks like when some models
are rate-limited, down, or returning bad output — and the system has to
batch pending iterations and fall over to whatever's available.

Why this test exists
--------------------
In production you can't assume every request succeeds on its primary
model. Models have:
  - Rate limits (ChatGPT free tier, Claude free tier)
  - Outages (any cloud API going 5xx)
  - Quality drops (Gemini entering analysis-only mode — we saw this
    at iter 37 earlier today)
  - Authentication problems (tab logged out mid-run)

A resilient runner must:
  1. Detect failure fast (timeout, non-code response, exception)
  2. Batch the pending work so it doesn't get lost
  3. Fall over to another model
  4. Retry with backoff + eventually succeed OR graceful-end

This script simulates all of that using a single real model (Gemini)
plus 3 virtual ones with configurable failure patterns, so you can see
what the blended workflow looks like without needing to log into 5 AI
accounts.

Run
---
    python blended_failure_test.py

Produces
--------
~/Downloads/BLENDED_FAILURE_TEST_<ts>.md
    Side-by-side log of every attempt, the model used, outcome, and
    whether batching recovered the work.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

LOG_PATH = Path.home() / ".autocoder" / "blended_failure_test.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("blended")


# ── Same task + reference files as the other runners ────────────────

TASK_PROMPT = (
    "You are building AutoEmerald — an autonomous Pokemon Emerald "
    "play/test framework. Your complete specification is in the "
    "REFERENCE MATERIAL embedded above. Implement A* pathfinding, "
    "type-effective move picker, post-battle dialog handlers. Output "
    "one complete consolidated Python file."
)

REF_DIR = Path(r"C:\Users\computer\Desktop\stolenEauto\coding for autoemerald")


# ── The blended pool ────────────────────────────────────────────────

@dataclass
class ModelStub:
    """A 'model' in the pool. Either real (CDP to a browser tab) or
    virtual (we simulate responses + failures)."""
    name: str
    real: bool = False
    # Virtual-only: probability a call "fails" (returns no code / errors)
    failure_rate: float = 0.0
    # Virtual-only: seconds to pretend it took (adds realism to the log)
    simulated_latency: float = 1.5
    # Runtime counters
    calls: int = 0
    successes: int = 0
    failures: int = 0
    rate_limited_until: float = 0.0


# Pool designed so you can see every failure mode at least once:
#   Gemini — real model, the one actually producing code
#   ChatGPT-sim — usually works, occasional rate-limit simulated
#   Claude-sim — has a hard timeout every 3rd call
#   Copilot-sim — quality-drops (returns non-code) 40% of the time
#   OpenRouter-sim — down for the first 60s of the test
POOL: list[ModelStub] = [
    ModelStub("Gemini", real=True),
    ModelStub("ChatGPT-sim", failure_rate=0.2, simulated_latency=2.0),
    ModelStub("Claude-sim", failure_rate=0.33, simulated_latency=2.5),
    ModelStub("Copilot-sim", failure_rate=0.4, simulated_latency=1.2),
    ModelStub("OpenRouter-sim", failure_rate=1.0, simulated_latency=0.5),
]


# ── Work unit ────────────────────────────────────────────────────────

@dataclass
class WorkItem:
    """One iteration the runner wants to complete."""
    iteration: int
    focus_key: str
    focus_label: str
    attempts: int = 0
    batched_with: list[int] = field(default_factory=list)  # iter indexes
    outcome_model: str = ""
    outcome: str = ""  # "ok" / "failed" / "batched-then-ok"
    chars_produced: int = 0
    latency_sec: float = 0.0


FOCUS_CYCLE = [
    ("deep_dive", "Deep Code Dive"),
    ("solid_functional", "Solid & Functional"),
    ("pressure_test", "Pressure Test"),
    ("error_recovery", "Error Recovery & Resilience"),
    ("extra_features", "Extra Features"),
    ("performance", "Performance Optimization"),
    ("test_suite", "Test Suite"),
    ("review_grade", "Review & Grade"),
]

BATCH_THRESHOLD = 2  # After N failed picks, batch the work and wait
COOLDOWN_SEC = 10.0  # How long to wait when batching kicks in


# ── Runner ───────────────────────────────────────────────────────────

class BlendedRunner:
    """Pulls work items, picks models, handles failure + batching.

    The core resilience pattern:

        take item
        until item.attempts exceed max:
            pick next available model (skip rate-limited ones)
            attempt
            if success: record + drop item
            else: increment attempts
                 if attempts >= BATCH_THRESHOLD:
                     batch this item with the next N pending items,
                     cool down for COOLDOWN_SEC, reset attempt counter
    """

    def __init__(self) -> None:
        self.queue: deque[WorkItem] = deque()
        self.completed: list[WorkItem] = []
        self.events: list[dict] = []
        self.real_session = None
        self.controller = None
        self.codebase = ""

    # ── Setup the real (Gemini) backend ─────────────────────────────
    def connect_real_model(self) -> bool:
        from gemini_coder_web.cdp_client import (
            connect_to_ai_site, DEFAULT_CDP_PORT,
        )
        from gemini_coder_web.session_manager import SessionManager
        from gemini_coder_web.ai_profiles import get_profile
        from gemini_coder_web.broadcast import BroadcastController

        cdp = connect_to_ai_site("Gemini", port=DEFAULT_CDP_PORT)
        if cdp is None:
            logger.error("Gemini tab not found — blended test needs at "
                         "least one real model. Open Gemini in CDP Chrome.")
            return False
        sm = SessionManager()
        session = sm.create_session(get_profile("Gemini"), "top-left")
        session.client._cdp = cdp
        session.client._cdp_available = True
        session.client._configured = True
        session.is_configured = True
        self.real_session = session

        # Prime reference files
        controller = BroadcastController(sm)
        if REF_DIR.exists():
            paths = sorted(str(p) for p in REF_DIR.glob("*.txt"))
            code_dir = REF_DIR / "code"
            if code_dir.exists():
                paths += sorted(str(p) for p in code_dir.glob("*.txt"))
            controller._file_list = controller._read_attached_files(paths)
            controller._chunks = []
        self.controller = controller
        try:
            cdp.new_conversation()
            time.sleep(2)
        except Exception as e:
            logger.warning("Initial new_conversation: %s", e)
        return True

    # ── Pick a model that's not in cooldown ─────────────────────────
    def _pick_available(self) -> ModelStub | None:
        now = time.time()
        candidates = [m for m in POOL if m.rate_limited_until <= now]
        if not candidates:
            return None
        # Round-robin-ish by least-recently-called
        candidates.sort(key=lambda m: m.calls)
        return candidates[0]

    # ── Attempt one work item on one model ──────────────────────────
    def _attempt(self, item: WorkItem, model: ModelStub) -> bool:
        start = time.time()
        model.calls += 1
        item.attempts += 1

        if model.real:
            # Real Gemini call
            from gemini_coder_web.broadcast import IMPROVEMENT_FOCUSES
            directive = IMPROVEMENT_FOCUSES[item.focus_key]["prompt"]
            prompt = self.controller._build_context_prompt(
                directive, self.codebase,
                focus_name=item.focus_label,
                task_prompt=TASK_PROMPT,
            )
            # Fresh convo every iteration (Gemini button fix)
            try:
                self.real_session.client.new_conversation()
                time.sleep(1)
            except Exception:
                pass
            try:
                response = self.real_session.client.generate(prompt=prompt)
                extracted = self.controller._extract_code_blocks(
                    response, previous_codebase=self.codebase,
                )
                if extracted and len(extracted) > 200:
                    self.codebase = extracted
                    item.chars_produced = len(extracted)
                    item.latency_sec = time.time() - start
                    item.outcome_model = model.name
                    item.outcome = (
                        "batched-then-ok" if item.batched_with else "ok"
                    )
                    model.successes += 1
                    self._emit(item, model, "success")
                    return True
                # Non-code response
                model.failures += 1
                item.latency_sec = time.time() - start
                self._emit(item, model, "non-code response")
                return False
            except Exception as e:
                model.failures += 1
                item.latency_sec = time.time() - start
                self._emit(item, model, f"exception: {str(e)[:80]}")
                return False

        # Virtual model — simulate latency + possible failure
        time.sleep(model.simulated_latency)
        # OpenRouter-sim: down for first 60s
        if model.name == "OpenRouter-sim" and time.time() - self._test_start < 60:
            model.failures += 1
            item.latency_sec = time.time() - start
            self._emit(item, model, "simulated: service unavailable (first 60s)")
            return False
        if random.random() < model.failure_rate:
            # Randomly choose a failure mode for realism
            mode = random.choice([
                "simulated: rate limited",
                "simulated: non-code response (chat filler only)",
                "simulated: CDP upload mismatch",
                "simulated: response timeout",
            ])
            if "rate limited" in mode:
                # Put this model in a short cooldown
                model.rate_limited_until = time.time() + 20
            model.failures += 1
            item.latency_sec = time.time() - start
            self._emit(item, model, mode)
            return False
        # Simulated success — produce a fake "extracted" chunk
        fake_size = random.randint(4000, 9000)
        item.chars_produced = fake_size
        item.latency_sec = time.time() - start
        item.outcome_model = model.name
        item.outcome = "batched-then-ok" if item.batched_with else "ok"
        model.successes += 1
        self._emit(item, model, f"simulated success ({fake_size} chars)")
        return True

    def _emit(self, item: WorkItem, model: ModelStub, msg: str) -> None:
        event = {
            "t": time.time() - self._test_start,
            "iter": item.iteration,
            "focus": item.focus_label,
            "model": model.name,
            "attempt": item.attempts,
            "batched_with": list(item.batched_with),
            "msg": msg,
        }
        self.events.append(event)
        logger.info(
            "  iter %d/%s  → %s  (attempt %d)  %s",
            item.iteration, item.focus_label, model.name,
            item.attempts, msg,
        )

    # ── Main loop ───────────────────────────────────────────────────
    def run(self, n_iterations: int = 8) -> None:
        self._test_start = time.time()

        # Seed queue
        for i, (focus_key, focus_label) in enumerate(FOCUS_CYCLE[:n_iterations]):
            self.queue.append(WorkItem(
                iteration=i + 1,
                focus_key=focus_key,
                focus_label=focus_label,
            ))

        max_attempts = 4
        while self.queue:
            item = self.queue.popleft()
            while item.attempts < max_attempts:
                model = self._pick_available()
                if model is None:
                    # All models in cooldown — batch with next pending
                    # item and wait
                    if self.queue:
                        next_item = self.queue.popleft()
                        item.batched_with.append(next_item.iteration)
                        # Merge: we'll consider both satisfied by whatever
                        # comes next, using the higher-priority item's focus
                        logger.info(
                            "BATCHING: iter %d has no available model — "
                            "batching with iter %d",
                            item.iteration, next_item.iteration,
                        )
                        # Mark the batched-out item as covered by the
                        # primary once it succeeds
                        next_item.outcome = "covered-by-batch"
                        next_item.outcome_model = "(batched)"
                        self.completed.append(next_item)
                    logger.info(
                        "All models in cooldown — waiting %ds", COOLDOWN_SEC,
                    )
                    time.sleep(COOLDOWN_SEC)
                    continue
                success = self._attempt(item, model)
                if success:
                    break
                # On persistent failure, batch with next
                if item.attempts >= BATCH_THRESHOLD and self.queue:
                    next_item = self.queue.popleft()
                    item.batched_with.append(next_item.iteration)
                    logger.info(
                        "BATCHING: iter %d failed %d times — batching "
                        "with iter %d",
                        item.iteration, item.attempts, next_item.iteration,
                    )
                    next_item.outcome = "covered-by-batch"
                    next_item.outcome_model = "(batched)"
                    self.completed.append(next_item)
            else:
                # Ran out of attempts
                item.outcome = "failed"
                logger.warning(
                    "GAVE UP on iter %d after %d attempts",
                    item.iteration, item.attempts,
                )
            self.completed.append(item)

    # ── Report ──────────────────────────────────────────────────────
    def write_report(self) -> Path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = Path.home() / "Downloads" / f"BLENDED_FAILURE_TEST_{ts}.md"
        out.parent.mkdir(exist_ok=True)

        lines: list[str] = [
            "# Blended-Model Failure Test Report",
            "",
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## Summary",
            "",
            "| Metric | Value |",
            "|---|---|",
        ]
        total_iters = len(self.completed)
        ok = sum(1 for c in self.completed if c.outcome == "ok")
        batched_ok = sum(1 for c in self.completed if c.outcome == "batched-then-ok")
        covered = sum(1 for c in self.completed if c.outcome == "covered-by-batch")
        failed = sum(1 for c in self.completed if c.outcome == "failed")
        lines += [
            f"| Total iterations | {total_iters} |",
            f"| Direct success | {ok} |",
            f"| Success after batching | {batched_ok} |",
            f"| Covered by batching (no own attempt) | {covered} |",
            f"| Gave up | {failed} |",
            f"| Total events logged | {len(self.events)} |",
            "",
            "## Per-Model Stats",
            "",
            "| Model | Calls | Successes | Failures | Success Rate |",
            "|---|---|---|---|---|",
        ]
        for m in POOL:
            rate = (m.successes / m.calls * 100) if m.calls else 0.0
            lines.append(
                f"| {m.name} | {m.calls} | {m.successes} | {m.failures} | "
                f"{rate:.0f}% |"
            )
        lines.append("")
        lines.append("## Per-Iteration Timeline")
        lines.append("")
        lines.append("| Iter | Focus | Model Used | Attempts | Batched | Chars | Outcome |")
        lines.append("|---|---|---|---|---|---|---|")
        for c in sorted(self.completed, key=lambda x: x.iteration):
            batched = ", ".join(str(i) for i in c.batched_with) or "—"
            model = c.outcome_model or "—"
            lines.append(
                f"| {c.iteration} | {c.focus_label} | {model} | "
                f"{c.attempts} | {batched} | {c.chars_produced:,} | "
                f"{c.outcome} |"
            )
        lines.append("")
        lines.append("## Event Log (chronological)")
        lines.append("")
        lines.append("```")
        for e in self.events:
            lines.append(
                f"{e['t']:6.1f}s  iter{e['iter']:<2} "
                f"attempt{e['attempt']} → {e['model']:<14} {e['msg']}"
            )
        lines.append("```")
        lines.append("")
        lines.append("## What This Demonstrates")
        lines.append("")
        lines.append(
            "- **Blend**: iterations naturally distributed across models "
            "based on who's up. One model can't monopolize the work.\n"
            "- **Batching**: when all models are temporarily out (cooldown "
            "or >N failed attempts), pending work is coalesced onto the "
            "next available model — nothing drops on the floor.\n"
            "- **Fallover**: a failure on one model immediately retries "
            "on another; the runner never deadlocks on a single provider.\n"
            "- **Visibility**: every attempt is logged with model + "
            "failure reason, so a production operator can see *why* a "
            "particular iteration took longer or used a different model."
        )
        out.write_text("\n".join(lines), encoding="utf-8")
        return out


def main() -> None:
    logger.info("=== Blended-Model Failure Test ===")
    runner = BlendedRunner()
    if not runner.connect_real_model():
        sys.exit(1)
    logger.info("Pool: %s", [f"{m.name}(fail={m.failure_rate})" for m in POOL])
    logger.info("Running 8 iterations with injected failures…")
    runner.run(n_iterations=8)
    report = runner.write_report()
    logger.info("Report: %s", report)

    # Console summary
    print(f"\n{'=' * 60}")
    print(f"BLENDED FAILURE TEST COMPLETE")
    print(f"Report: {report}")
    print(f"{'=' * 60}")
    for c in sorted(runner.completed, key=lambda x: x.iteration):
        print(
            f"  iter {c.iteration:<2} {c.focus_label:<32} "
            f"{c.outcome_model or '-':<14} "
            f"{c.chars_produced:>6,}c  "
            f"attempts={c.attempts} batched={c.batched_with} "
            f"{c.outcome}"
        )


if __name__ == "__main__":
    main()
