"""Defensive execution for subprocesses and callable tasks.

Python translation of the Claude Code Rust kill-switch pattern.
Three modes:
  1. Background (fire-and-forget) - spawn, return PID, don't block
  2. Timeout with kill - poll loop, force-kill on timeout, capture partial output
  3. Normal blocking - run and return

Use these to wrap any subprocess or long-running callable so a hang
never freezes your main loop.

Usage:
    from gemini_coder.safe_exec import safe_shell, safe_call, TaskResult

    # Run a shell command with 30s timeout
    result = safe_shell("python heavy_script.py", timeout_ms=30000)
    if result.interrupted:
        print(f"Killed after timeout: {result.stderr}")

    # Run a Python callable with timeout
    result = safe_call(my_function, args=(x, y), timeout_ms=60000)
    if result.interrupted:
        print("Function timed out, partial result:", result.stdout)

    # Fire and forget
    result = safe_shell("python server.py", background=True)
    print(f"Running as PID {result.background_pid}")
"""

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Poll interval for timeout loop (matches Rust: 10ms)
_POLL_INTERVAL_MS = 10


@dataclass
class TaskResult:
    """Result of a defensive execution - mirrors the Rust BashCommandOutput."""
    stdout: str = ""
    stderr: str = ""
    interrupted: bool = False
    return_code: Optional[int] = None
    background_pid: Optional[int] = None
    is_background: bool = False
    timed_out: bool = False
    elapsed_ms: float = 0.0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True if the command finished successfully (code 0, no timeout)."""
        return (
            not self.interrupted
            and not self.timed_out
            and self.error is None
            and (self.return_code is None or self.return_code == 0)
        )


def safe_shell(
    command: str,
    timeout_ms: Optional[int] = None,
    background: bool = False,
    shell_exe: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> TaskResult:
    """Execute a shell command with defensive timeout and kill logic.

    Directly translates the Rust execute_shell_command pattern:
    - background=True: spawn and return PID immediately
    - timeout_ms set: poll loop at 10ms, kill on timeout, capture partial output
    - neither: normal blocking execution

    Args:
        command: The shell command string to execute
        timeout_ms: Max time in milliseconds before force-killing (None = no limit)
        background: If True, spawn and return immediately with PID
        shell_exe: Shell to use (default: system shell)
        cwd: Working directory
        env: Environment variables (merged with current env)
    """
    if shell_exe is None:
        if sys.platform == "win32":
            shell_exe = "powershell.exe"
            shell_args = [shell_exe, "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            shell_exe = "/bin/bash"
            shell_args = [shell_exe, "-c", command]
    else:
        shell_args = [shell_exe, "-c", command]

    merged_env = None
    if env:
        import os
        merged_env = {**os.environ, **env}

    # ── Mode 1: Background (fire-and-forget) ─────────────────────
    if background:
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                )
            child = subprocess.Popen(
                shell_args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
                env=merged_env,
                **kwargs,
            )
            return TaskResult(
                background_pid=child.pid,
                is_background=True,
            )
        except Exception as e:
            return TaskResult(error=str(e), stderr=str(e))

    # ── Mode 2: Timeout with kill ────────────────────────────────
    if timeout_ms is not None:
        try:
            child = subprocess.Popen(
                shell_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=merged_env,
            )
            started = time.monotonic()
            deadline_s = timeout_ms / 1000.0

            while True:
                retcode = child.poll()
                if retcode is not None:
                    # Process finished naturally
                    stdout_bytes = child.stdout.read() if child.stdout else b""
                    stderr_bytes = child.stderr.read() if child.stderr else b""
                    elapsed = (time.monotonic() - started) * 1000
                    return TaskResult(
                        stdout=stdout_bytes.decode("utf-8", errors="replace"),
                        stderr=stderr_bytes.decode("utf-8", errors="replace"),
                        return_code=retcode,
                        elapsed_ms=elapsed,
                    )

                elapsed_s = time.monotonic() - started
                if elapsed_s >= deadline_s:
                    # TIMEOUT: kill the process
                    logger.warning(
                        "Command exceeded timeout of %dms, killing PID %d",
                        timeout_ms, child.pid
                    )
                    child.kill()
                    try:
                        stdout_bytes, stderr_bytes = child.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        stdout_bytes, stderr_bytes = b"", b""

                    stderr_text = stderr_bytes.decode("utf-8", errors="replace").rstrip()
                    timeout_msg = f"Command exceeded timeout of {timeout_ms}ms"
                    if stderr_text:
                        stderr_text = f"{stderr_text}\n{timeout_msg}"
                    else:
                        stderr_text = timeout_msg

                    return TaskResult(
                        stdout=stdout_bytes.decode("utf-8", errors="replace"),
                        stderr=stderr_text,
                        interrupted=True,
                        timed_out=True,
                        return_code=-1,
                        elapsed_ms=timeout_ms,
                    )

                # Poll interval: 10ms (matches Rust)
                time.sleep(_POLL_INTERVAL_MS / 1000.0)

        except Exception as e:
            return TaskResult(error=str(e), stderr=str(e))

    # ── Mode 3: Normal blocking execution ────────────────────────
    try:
        started = time.monotonic()
        result = subprocess.run(
            shell_args,
            capture_output=True,
            cwd=cwd,
            env=merged_env,
        )
        elapsed = (time.monotonic() - started) * 1000
        return TaskResult(
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
            return_code=result.returncode,
            elapsed_ms=elapsed,
        )
    except Exception as e:
        return TaskResult(error=str(e), stderr=str(e))


def safe_call(
    fn: Callable,
    args: tuple = (),
    kwargs: Optional[dict] = None,
    timeout_ms: Optional[int] = None,
    background: bool = False,
) -> TaskResult:
    """Execute a Python callable with defensive timeout and kill logic.

    Same pattern as safe_shell but for Python functions.
    Uses a daemon thread that can be abandoned on timeout.

    Args:
        fn: The function to call
        args: Positional arguments
        kwargs: Keyword arguments
        timeout_ms: Max time in ms before abandoning (None = no limit)
        background: If True, start in daemon thread and return immediately
    """
    if kwargs is None:
        kwargs = {}

    # ── Background mode ──────────────────────────────────────────
    if background:
        thread = threading.Thread(
            target=fn, args=args, kwargs=kwargs, daemon=True
        )
        thread.start()
        return TaskResult(
            is_background=True,
            background_pid=thread.ident,
        )

    # ── With or without timeout ──────────────────────────────────
    result_holder: dict = {"value": None, "error": None}

    def worker():
        try:
            result_holder["value"] = fn(*args, **kwargs)
        except Exception as e:
            result_holder["error"] = e

    thread = threading.Thread(target=worker, daemon=True)
    started = time.monotonic()
    thread.start()

    if timeout_ms is not None:
        thread.join(timeout=timeout_ms / 1000.0)
    else:
        thread.join()

    elapsed = (time.monotonic() - started) * 1000

    if thread.is_alive():
        # Thread is still running = timeout
        logger.warning(
            "Callable %s exceeded timeout of %dms",
            getattr(fn, "__name__", str(fn)), timeout_ms
        )
        return TaskResult(
            stderr=f"Callable exceeded timeout of {timeout_ms}ms",
            interrupted=True,
            timed_out=True,
            elapsed_ms=timeout_ms or elapsed,
        )

    if result_holder["error"]:
        return TaskResult(
            stderr=str(result_holder["error"]),
            error=str(result_holder["error"]),
            elapsed_ms=elapsed,
        )

    value = result_holder["value"]
    return TaskResult(
        stdout=str(value) if value is not None else "",
        elapsed_ms=elapsed,
    )


class GuardedExecutor:
    """Wraps a callable with automatic timeout, retry, and status reporting.

    Use this to guard your generate() calls so a hung Gemini session
    doesn't freeze the task executor.

    Usage:
        guard = GuardedExecutor(
            timeout_ms=120000,      # 2 minutes per call
            max_retries=3,
            on_status=my_callback,  # (status, detail) -> None
        )

        result = guard.run(client.generate, prompt="hello")
        if result.ok:
            print(result.stdout)
        elif result.timed_out:
            print("Gemini hung, was killed after 2 min")
    """

    def __init__(
        self,
        timeout_ms: int = 120_000,
        max_retries: int = 2,
        retry_delay_ms: int = 2000,
        on_status: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._timeout_ms = timeout_ms
        self._max_retries = max_retries
        self._retry_delay_ms = retry_delay_ms
        self._on_status = on_status
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Signal cancellation."""
        self._cancel.set()

    def reset(self) -> None:
        """Reset cancellation flag for reuse."""
        self._cancel.clear()

    def _emit(self, status: str, detail: str = "") -> None:
        if self._on_status:
            try:
                self._on_status(status, detail)
            except Exception:
                pass

    def run(self, fn: Callable, *args, **kwargs) -> TaskResult:
        """Run fn with timeout and retry protection."""
        self._cancel.clear()
        last_result = None

        for attempt in range(self._max_retries + 1):
            if self._cancel.is_set():
                return TaskResult(
                    stderr="Cancelled",
                    interrupted=True,
                )

            attempt_label = f"(attempt {attempt + 1}/{self._max_retries + 1})"

            self._emit("working", f"Running {attempt_label}")

            result = safe_call(
                fn, args=args, kwargs=kwargs,
                timeout_ms=self._timeout_ms,
            )
            last_result = result

            if result.ok:
                return result

            if result.timed_out:
                self._emit("error", f"Timed out {attempt_label}")
                logger.warning("Guarded call timed out %s", attempt_label)
            elif result.error:
                self._emit("error", f"Error: {result.error} {attempt_label}")
                logger.warning("Guarded call error %s: %s", attempt_label, result.error)

            if attempt < self._max_retries:
                self._emit("thinking", f"Retrying in {self._retry_delay_ms}ms...")
                # Interruptible sleep
                if self._cancel.wait(self._retry_delay_ms / 1000.0):
                    return TaskResult(stderr="Cancelled", interrupted=True)

        return last_result or TaskResult(stderr="All retries exhausted", error="max_retries")
