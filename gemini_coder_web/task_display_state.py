"""Persist what Autocoder is / was working on for the UI banner and restarts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


def _autocoder_dir() -> Path:
    d = Path.home() / ".autocoder"
    d.mkdir(parents=True, exist_ok=True)
    return d


def task_display_path() -> Path:
    return _autocoder_dir() / "task_display.json"


def load_task_display() -> dict[str, Any]:
    p = task_display_path()
    if not p.exists():
        return _default()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default()
        return {**_default(), **data}
    except Exception:
        return _default()


def _default() -> dict[str, Any]:
    return {
        "status": "idle",  # idle | running
        "task_text": "",
        "iteration": 0,
        "focus_label": "",
        "ai_name": "",
        "build_target": "",
        "updated_at": 0.0,
        "started_at": 0.0,
    }


def save_task_display(**kwargs: Any) -> None:
    cur = load_task_display()
    cur.update(kwargs)
    cur["updated_at"] = time.time()
    try:
        task_display_path().write_text(
            json.dumps(cur, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def set_running(
    task: str,
    *,
    build_target: str = "",
    ai_name: str = "",
    iteration: int = 0,
    focus_label: str = "",
) -> None:
    t = time.time()
    d = load_task_display()
    started = d.get("started_at", t) if d.get("status") == "running" else t
    save_task_display(
        status="running",
        task_text=(task or "").strip(),
        build_target=build_target,
        ai_name=ai_name,
        iteration=iteration,
        focus_label=focus_label,
        started_at=started,
    )


def set_idle(
    *,
    keep_task: bool = True,
    last_iteration: int = 0,
) -> None:
    d = load_task_display()
    task = d.get("task_text", "") if keep_task else ""
    save_task_display(
        status="idle",
        task_text=task,
        iteration=last_iteration or d.get("iteration", 0),
        focus_label="",
    )


def set_iteration(
    iteration: int,
    focus_label: str,
    *,
    task: str | None = None,
) -> None:
    d = load_task_display()
    upd = {
        "status": "running",
        "iteration": iteration,
        "focus_label": focus_label,
    }
    if task is not None:
        upd["task_text"] = task.strip()
    save_task_display(**upd)


def banner_lines() -> tuple[str, str, str]:
    """Return (title, subtitle, detail) for the task panel."""
    d = load_task_display()
    st = d.get("status", "idle")
    task = (d.get("task_text") or "").strip()
    if len(task) > 220:
        short = task[:217].rstrip() + "…"
    else:
        short = task or "(no task text saved yet)"
    if st == "running":
        title = "● Autocoding in progress"
        sub = f"Iteration {d.get('iteration', 0)}"
        if d.get("focus_label"):
            sub += f" · {d['focus_label']}"
        if d.get("ai_name"):
            sub += f" · {d['ai_name']}"
    else:
        title = "○ Last / current task (idle)"
        sub = f"Last recorded iteration: {d.get('iteration', 0)}"
    detail = short
    return title, sub, detail


def load_last_task_from_files() -> str:
    """Best-effort: last_prompt.txt, then broadcast_state.json."""
    p = _autocoder_dir() / "last_prompt.txt"
    if p.exists():
        try:
            t = p.read_text(encoding="utf-8").strip()
            if t and not t.startswith("e.g.,"):
                return t
        except Exception:
            pass
    b = _autocoder_dir() / "broadcast_state.json"
    if b.exists():
        try:
            data = json.loads(b.read_text(encoding="utf-8"))
            cfg = data.get("config") or {}
            t = (cfg.get("task") or "").strip()
            if t:
                return t
        except Exception:
            pass
    return load_task_display().get("task_text") or ""
