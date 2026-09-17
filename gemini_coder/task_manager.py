"""Task queue manager with timing, ordering, and state tracking."""

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .gemini_client import Conversation

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    OVERTIME = "overtime"
    CANCELLED = "cancelled"


@dataclass
class CodingTask:
    """A single coding task with time budget and state."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    description: str = ""
    time_budget_minutes: int = 30
    allow_overtime: bool = True
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    elapsed_seconds: float = 0.0
    iterations_completed: int = 0
    max_iterations: int = 50
    output_code: str = ""
    conversation: Conversation = field(default_factory=Conversation)
    error_message: str = ""
    tags: list[str] = field(default_factory=list)
    auto_improve: bool = False

    @property
    def time_budget_seconds(self) -> float:
        return self.time_budget_minutes * 60.0

    @property
    def time_remaining_seconds(self) -> float:
        return max(0, self.time_budget_seconds - self.elapsed_seconds)

    @property
    def is_overtime(self) -> bool:
        return self.elapsed_seconds > self.time_budget_seconds

    @property
    def progress_fraction(self) -> float:
        if self.time_budget_seconds <= 0:
            return 1.0
        return min(1.0, self.elapsed_seconds / self.time_budget_seconds)

    def to_dict(self) -> dict:
        """Serialize for saving (excludes conversation)."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "time_budget_minutes": self.time_budget_minutes,
            "allow_overtime": self.allow_overtime,
            "status": self.status.value,
            "priority": self.priority,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "iterations_completed": self.iterations_completed,
            "max_iterations": self.max_iterations,
            "output_code": self.output_code,
            "error_message": self.error_message,
            "tags": self.tags,
            "auto_improve": self.auto_improve,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CodingTask":
        """Deserialize from saved data."""
        task = cls()
        for key, value in data.items():
            if key == "status":
                task.status = TaskStatus(value)
            elif hasattr(task, key) and key != "conversation":
                setattr(task, key, value)
        return task


class TaskQueue:
    """Ordered queue of coding tasks with persistence."""

    def __init__(self, save_path: Optional[Path] = None) -> None:
        self._tasks: list[CodingTask] = []
        self._save_path = save_path
        self._lock = threading.Lock()
        self._on_change_callbacks: list[Callable] = []

        if save_path and save_path.exists():
            self._load()

    @property
    def tasks(self) -> list[CodingTask]:
        with self._lock:
            return list(self._tasks)

    @property
    def pending_tasks(self) -> list[CodingTask]:
        with self._lock:
            return [t for t in self._tasks if t.status == TaskStatus.PENDING]

    @property
    def current_task(self) -> Optional[CodingTask]:
        with self._lock:
            running = [t for t in self._tasks if t.status == TaskStatus.RUNNING]
            return running[0] if running else None

    def on_change(self, callback: Callable) -> None:
        """Register a callback for queue changes."""
        self._on_change_callbacks.append(callback)

    def _notify_change(self) -> None:
        for cb in self._on_change_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error("Change callback error: %s", e)

    def add_task(self, task: CodingTask) -> None:
        """Add a task to the queue."""
        with self._lock:
            self._tasks.append(task)
        self._save()
        self._notify_change()
        logger.info("Added task: %s (%d min)", task.title, task.time_budget_minutes)

    def remove_task(self, task_id: str) -> bool:
        """Remove a task by ID. Returns True if found and removed."""
        with self._lock:
            for i, task in enumerate(self._tasks):
                if task.id == task_id:
                    if task.status == TaskStatus.RUNNING:
                        return False
                    self._tasks.pop(i)
                    self._save()
                    self._notify_change()
                    return True
        return False

    def move_task(self, task_id: str, direction: int) -> bool:
        """Move a task up (-1) or down (+1) in the queue."""
        with self._lock:
            for i, task in enumerate(self._tasks):
                if task.id == task_id:
                    new_i = i + direction
                    if 0 <= new_i < len(self._tasks):
                        self._tasks[i], self._tasks[new_i] = (
                            self._tasks[new_i], self._tasks[i]
                        )
                        self._save()
                        self._notify_change()
                        return True
        return False

    def get_task(self, task_id: str) -> Optional[CodingTask]:
        with self._lock:
            for task in self._tasks:
                if task.id == task_id:
                    return task
        return None

    def get_next_pending(self) -> Optional[CodingTask]:
        """Get the next pending task in order."""
        with self._lock:
            for task in self._tasks:
                if task.status == TaskStatus.PENDING:
                    return task
        return None

    def clear_completed(self) -> int:
        """Remove all completed/failed/cancelled tasks."""
        with self._lock:
            before = len(self._tasks)
            self._tasks = [
                t for t in self._tasks
                if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED)
            ]
            removed = before - len(self._tasks)
        if removed:
            self._save()
            self._notify_change()
        return removed

    def total_time_remaining(self) -> float:
        """Total estimated time remaining in minutes."""
        with self._lock:
            return sum(
                t.time_remaining_seconds / 60.0
                for t in self._tasks
                if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
            )

    def _save(self) -> None:
        if not self._save_path:
            return
        try:
            data = [t.to_dict() for t in self._tasks]
            self._save_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.error("Failed to save task queue: %s", e)

    def _load(self) -> None:
        try:
            data = json.loads(self._save_path.read_text(encoding="utf-8"))
            self._tasks = [CodingTask.from_dict(d) for d in data]
            for task in self._tasks:
                if task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.PAUSED
            logger.info("Loaded %d tasks from disk", len(self._tasks))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load task queue: %s", e)
            self._tasks = []


# Auto-improvement prompts - cycled through when auto_improve is enabled on a task
IMPROVEMENT_PROMPTS = [
    {
        "name": "Add Features",
        "prompt": (
            "Expand on what you built. Add 2-3 useful features a real user would want:\n"
            "- Config options, CLI arguments, or settings\n"
            "- Logging, progress indicators, status output\n"
            "- Better input validation and error messages\n"
            "- Any missing functionality that makes this more complete\n\n"
            "Provide the COMPLETE updated code with improvements."
        ),
    },
    {
        "name": "Professional Polish",
        "prompt": (
            "Make this code look and feel professional:\n"
            "- Module/function docstrings, type hints\n"
            "- Consistent naming, organized imports\n"
            "- Proper __main__ block, clean structure\n"
            "- More polished output/UI\n\n"
            "Provide the COMPLETE updated code."
        ),
    },
    {
        "name": "Robustness",
        "prompt": (
            "Harden this code for real-world use:\n"
            "- Handle edge cases (empty input, missing files, network errors)\n"
            "- Add retry logic, graceful shutdown, resource cleanup\n"
            "- Validate all inputs at boundaries\n\n"
            "Provide the COMPLETE updated code."
        ),
    },
    {
        "name": "Performance",
        "prompt": (
            "Optimize for better performance:\n"
            "- Fix bottlenecks, use efficient data structures\n"
            "- Add caching where sensible\n"
            "- Minimize unnecessary I/O\n\n"
            "Provide the COMPLETE updated code."
        ),
    },
    {
        "name": "Final Review",
        "prompt": (
            "Senior developer final review:\n"
            "- Ensure all features work together\n"
            "- Check for remaining TODOs or incomplete sections\n"
            "- Add usage examples in comments\n"
            "- Final polish touches\n\n"
            "Provide the FINAL, COMPLETE code."
        ),
    },
]


class TaskExecutor:
    """Executes tasks from the queue using the Gemini client."""

    SYSTEM_PROMPT = (
        "You are an expert coding assistant. You write clean, well-documented, "
        "production-ready code. When given a coding task:\n"
        "1. Think through the approach step by step\n"
        "2. Write the complete implementation\n"
        "3. Include error handling and edge cases\n"
        "4. Add brief inline comments for non-obvious logic\n"
        "5. If the task is large, break it into logical sections\n\n"
        "Always provide COMPLETE, RUNNABLE code. Never use placeholders like "
        "'// TODO' or '...' unless explicitly told the task is partial."
    )

    def __init__(self, gemini_client, task_queue: TaskQueue) -> None:
        self._client = gemini_client
        self._queue = task_queue
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._timer_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._on_output: Optional[Callable[[str, str], None]] = None
        self._on_task_start: Optional[Callable[[CodingTask], None]] = None
        self._on_task_complete: Optional[Callable[[CodingTask], None]] = None
        self._on_tick: Optional[Callable[[CodingTask], None]] = None
        self._on_status: Optional[Callable[[str, str], None]] = None

    @property
    def is_running(self) -> bool:
        return self._running

    def set_callbacks(
        self,
        on_output: Optional[Callable[[str, str], None]] = None,
        on_task_start: Optional[Callable[[CodingTask], None]] = None,
        on_task_complete: Optional[Callable[[CodingTask], None]] = None,
        on_tick: Optional[Callable[[CodingTask], None]] = None,
        on_status: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """Set event callbacks."""
        self._on_output = on_output
        self._on_task_start = on_task_start
        self._on_task_complete = on_task_complete
        self._on_tick = on_tick
        self._on_status = on_status

    def start(self) -> None:
        """Start executing the task queue."""
        if self._running:
            return
        self._stop_event.clear()
        self._pause_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Task executor started")

    def stop(self) -> None:
        """Stop executing after current task finishes."""
        self._stop_event.set()
        self._client.cancel()
        self._running = False
        logger.info("Task executor stopping")

    def pause(self) -> None:
        """Pause execution between tasks."""
        self._pause_event.set()

    def resume(self) -> None:
        """Resume paused execution."""
        self._pause_event.clear()

    def _run_loop(self) -> None:
        """Main execution loop - processes tasks in queue order."""
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                time.sleep(0.5)
                continue

            task = self._queue.get_next_pending()
            if task is None:
                self._running = False
                if self._on_output:
                    self._on_output("system", "All tasks completed!")
                break

            self._execute_task(task)

        self._running = False

    # Status constants for UI status indicator
    STATUS_WORKING = "working"
    STATUS_THINKING = "thinking"
    STATUS_IMPROVING = "improving"
    STATUS_IDLE = "idle"
    STATUS_ERROR = "error"

    def _emit_status(self, status: str, detail: str = "") -> None:
        """Emit a status change for the UI status bar."""
        if self._on_status:
            try:
                self._on_status(status, detail)
            except Exception:
                pass

    def _execute_task(self, task: CodingTask) -> None:
        """Execute a single task with time tracking.

        If task.auto_improve is True, after the initial build succeeds
        it keeps improving the code in cycles until time runs out.
        """
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        self._queue._notify_change()

        if self._on_task_start:
            self._on_task_start(task)

        timer_stop = threading.Event()
        timer = threading.Thread(
            target=self._timer_loop, args=(task, timer_stop), daemon=True
        )
        timer.start()

        try:
            # Phase 1: Initial build
            self._emit_status(self.STATUS_WORKING, f"Building: {task.title}")
            prompt = self._build_task_prompt(task)
            result = self._client.generate(
                prompt=prompt,
                conversation=task.conversation,
                system_instruction=self.SYSTEM_PROMPT,
                on_progress=lambda text: (
                    self._on_output("code", text) if self._on_output else None
                ),
            )
            task.output_code = result
            task.iterations_completed += 1

            # Phase 2: Refinement / Auto-improvement loop
            if task.auto_improve:
                improve_round = 0
                improve_prompts = IMPROVEMENT_PROMPTS
                while not self._stop_event.is_set() and not task.is_overtime:
                    # Cycle through improvement types
                    idx = improve_round % len(improve_prompts)
                    round_info = improve_prompts[idx]
                    round_name = round_info["name"]
                    improve_round += 1

                    self._emit_status(
                        self.STATUS_IMPROVING,
                        f"Round {improve_round}: {round_name}"
                    )
                    if self._on_output:
                        self._on_output(
                            "system",
                            f"\n{'='*50}\n"
                            f"Auto-Improve Round {improve_round}: {round_name}\n"
                            f"{'='*50}\n"
                        )

                    refinement = round_info["prompt"]
                    try:
                        result = self._client.generate(
                            prompt=refinement,
                            conversation=task.conversation,
                            system_instruction=self.SYSTEM_PROMPT,
                            on_progress=lambda text: (
                                self._on_output("code", text)
                                if self._on_output else None
                            ),
                        )
                        task.output_code = result
                        task.iterations_completed += 1
                    except Exception as e:
                        self._emit_status(self.STATUS_ERROR, str(e))
                        logger.warning("Improvement round failed: %s", e)
                        # Don't crash - keep trying next round
                        continue

                    if task.is_overtime and not task.allow_overtime:
                        break
                    if task.is_overtime:
                        break
            else:
                # Standard refinement loop (no auto-improve)
                while not self._stop_event.is_set() and not task.is_overtime:
                    self._emit_status(self.STATUS_THINKING, "Reviewing code...")
                    refinement = self._build_refinement_prompt(task)
                    result = self._client.generate(
                        prompt=refinement,
                        conversation=task.conversation,
                        system_instruction=self.SYSTEM_PROMPT,
                        on_progress=lambda text: (
                            self._on_output("code", text)
                            if self._on_output else None
                        ),
                    )
                    task.output_code = result
                    task.iterations_completed += 1

                    if task.is_overtime and not task.allow_overtime:
                        break
                    if task.is_overtime:
                        break

            task.status = TaskStatus.COMPLETED

        except InterruptedError:
            task.status = TaskStatus.CANCELLED
            task.error_message = "Cancelled by user"
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            self._emit_status(self.STATUS_ERROR, str(e))
            logger.error("Task '%s' failed: %s", task.title, e)
        finally:
            task.completed_at = time.time()
            timer_stop.set()
            timer.join(timeout=2)
            self._queue._save()
            self._queue._notify_change()
            self._emit_status(self.STATUS_IDLE)

            if self._on_task_complete:
                self._on_task_complete(task)

    def _timer_loop(self, task: CodingTask, stop_event: threading.Event) -> None:
        """Update elapsed time every second."""
        while not stop_event.is_set():
            if task.started_at:
                task.elapsed_seconds = time.time() - task.started_at
            if self._on_tick:
                try:
                    self._on_tick(task)
                except Exception:
                    pass
            stop_event.wait(1.0)

    def _build_task_prompt(self, task: CodingTask) -> str:
        """Build the initial prompt for a task."""
        prompt = f"# Coding Task: {task.title}\n\n"
        prompt += f"{task.description}\n\n"
        prompt += (
            "Please implement this completely. Provide the full, runnable code "
            "with all necessary imports, error handling, and documentation."
        )
        if task.tags:
            prompt += f"\n\nTechnologies/tags: {', '.join(task.tags)}"
        return prompt

    def _build_refinement_prompt(self, task: CodingTask) -> str:
        """Build a refinement prompt to improve the code."""
        return (
            "Review the code you just wrote and improve it:\n"
            "1. Are there any bugs or edge cases you missed?\n"
            "2. Can the error handling be more robust?\n"
            "3. Is the code well-structured and readable?\n"
            "4. Are there any performance improvements?\n\n"
            "Provide the complete improved version of the code."
        )


class ContinuousImprover:
    """Runs continuous improvement loops on completed code.

    After a task produces working code, this takes over and iteratively:
    1. Adds useful features (logging, config, CLI args, etc.)
    2. Makes it look more professional (formatting, docstrings, structure)
    3. Improves accuracy and robustness (validation, error handling, tests)

    Each iteration focuses on one improvement area so Gemini gives
    focused, high-quality changes rather than trying to do everything at once.
    """

    IMPROVEMENT_ROUNDS = [
        {
            "name": "Add Useful Features",
            "prompt": (
                "You previously wrote this code. Now ADD useful features that a "
                "production user would expect. Consider:\n"
                "- Command-line arguments or configuration options\n"
                "- Logging with proper log levels\n"
                "- Progress indicators or status output\n"
                "- Input validation and helpful error messages\n"
                "- Any missing functionality that would make this more complete\n\n"
                "Add 2-3 meaningful features. Provide the COMPLETE updated code."
            ),
        },
        {
            "name": "Professional Polish",
            "prompt": (
                "Make this code look and feel more professional:\n"
                "- Clean up formatting and structure\n"
                "- Add module docstring and function docstrings\n"
                "- Use consistent naming conventions\n"
                "- Add type hints where missing\n"
                "- Organize imports properly\n"
                "- Add a proper __main__ block if appropriate\n"
                "- Make the output/UI more polished\n\n"
                "Provide the COMPLETE updated code."
            ),
        },
        {
            "name": "Robustness & Edge Cases",
            "prompt": (
                "Harden this code for real-world use:\n"
                "- Handle edge cases (empty input, network errors, missing files, etc.)\n"
                "- Add retry logic where appropriate\n"
                "- Ensure resources are properly cleaned up (files closed, connections released)\n"
                "- Add graceful shutdown/cancellation support\n"
                "- Validate all inputs at boundaries\n"
                "- Add any missing error handling\n\n"
                "Provide the COMPLETE updated code."
            ),
        },
        {
            "name": "Performance & Optimization",
            "prompt": (
                "Optimize this code for better performance:\n"
                "- Identify and fix any performance bottlenecks\n"
                "- Use efficient data structures\n"
                "- Add caching where it makes sense\n"
                "- Minimize unnecessary I/O or computation\n"
                "- Consider memory usage for large inputs\n\n"
                "Provide the COMPLETE updated code."
            ),
        },
        {
            "name": "Final Review & Integration",
            "prompt": (
                "Do a final review of this code as a senior developer would:\n"
                "- Ensure all features work together correctly\n"
                "- Check for any remaining TODOs or incomplete sections\n"
                "- Verify the code is self-contained and runnable\n"
                "- Add a brief usage example in the docstring or comments\n"
                "- Make any final polish touches\n\n"
                "Provide the FINAL, COMPLETE, production-ready code."
            ),
        },
    ]

    SYSTEM_PROMPT = (
        "You are a senior software engineer doing iterative code improvement. "
        "You receive code that already works and your job is to make it BETTER. "
        "Each round focuses on one area of improvement. "
        "ALWAYS return the COMPLETE updated code - never partial snippets. "
        "Keep all existing functionality while adding improvements."
    )

    def __init__(self, gemini_client) -> None:
        self._client = gemini_client
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._on_output: Optional[Callable[[str, str], None]] = None
        self._on_round_start: Optional[Callable[[int, str], None]] = None
        self._on_round_complete: Optional[Callable[[int, str], None]] = None
        self._on_complete: Optional[Callable[[str], None]] = None
        self._current_round = 0
        self._total_rounds = len(self.IMPROVEMENT_ROUNDS)
        self._current_code = ""
        self._conversation: Optional[Conversation] = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def current_round(self) -> int:
        return self._current_round

    @property
    def total_rounds(self) -> int:
        return self._total_rounds

    @property
    def current_round_name(self) -> str:
        if 0 <= self._current_round < len(self.IMPROVEMENT_ROUNDS):
            return self.IMPROVEMENT_ROUNDS[self._current_round]["name"]
        return ""

    def set_callbacks(
        self,
        on_output: Optional[Callable[[str, str], None]] = None,
        on_round_start: Optional[Callable[[int, str], None]] = None,
        on_round_complete: Optional[Callable[[int, str], None]] = None,
        on_complete: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_output = on_output
        self._on_round_start = on_round_start
        self._on_round_complete = on_round_complete
        self._on_complete = on_complete

    def start(self, code: str, task_title: str = "") -> None:
        """Start continuous improvement on the given code."""
        if self._running:
            return
        self._current_code = code
        self._stop_event.clear()
        self._running = True
        self._current_round = 0
        self._conversation = Conversation()
        self._thread = threading.Thread(
            target=self._improvement_loop,
            args=(task_title,),
            daemon=True,
        )
        self._thread.start()
        logger.info("Continuous improvement started for: %s", task_title)

    def stop(self) -> None:
        """Stop improvement after current round finishes."""
        self._stop_event.set()
        self._client.cancel()
        self._running = False
        logger.info("Continuous improvement stopping")

    def _improvement_loop(self, task_title: str) -> None:
        """Run through all improvement rounds."""
        try:
            for i, round_info in enumerate(self.IMPROVEMENT_ROUNDS):
                if self._stop_event.is_set():
                    break

                self._current_round = i
                round_name = round_info["name"]
                round_prompt = round_info["prompt"]

                if self._on_round_start:
                    self._on_round_start(i, round_name)

                if self._on_output:
                    self._on_output(
                        "system",
                        f"\n{'='*60}\n"
                        f"Improvement Round {i+1}/{self._total_rounds}: {round_name}\n"
                        f"{'='*60}\n"
                    )

                # Build prompt with current code context
                full_prompt = (
                    f"Here is the current code for '{task_title}':\n\n"
                    f"```\n{self._current_code}\n```\n\n"
                    f"{round_prompt}"
                )

                try:
                    result = self._client.generate(
                        prompt=full_prompt,
                        conversation=self._conversation,
                        system_instruction=self.SYSTEM_PROMPT,
                        on_progress=lambda text: (
                            self._on_output("code", text)
                            if self._on_output else None
                        ),
                    )

                    # Update the code for the next round
                    self._current_code = result

                    if self._on_round_complete:
                        self._on_round_complete(i, round_name)

                    if self._on_output:
                        self._on_output(
                            "system",
                            f"Round {i+1} ({round_name}) complete.\n"
                        )

                except InterruptedError:
                    logger.info("Improvement cancelled during round %d", i + 1)
                    break
                except Exception as e:
                    logger.error("Improvement round %d failed: %s", i + 1, e)
                    if self._on_output:
                        self._on_output("error", f"Round {i+1} failed: {e}\n")
                    # Continue to next round despite failure
                    continue

        finally:
            self._running = False
            if self._on_complete:
                self._on_complete(self._current_code)
            logger.info("Continuous improvement finished")
