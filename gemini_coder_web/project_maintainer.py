"""Per-project MASTER log and goal queue for long Autocoder sessions."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _autocoder_root() -> Path:
    p = Path.home() / ".autocoder"
    p.mkdir(parents=True, exist_ok=True)
    return p


def slugify(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[-\s]+", "_", s).strip("_")
    return (s[:max_len] or "project").rstrip("_")


def master_path(project_slug: str) -> Path:
    p = _autocoder_root() / "projects" / project_slug / "MASTER.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def ensure_master_header(project_slug: str, task_summary: str) -> None:
    path = master_path(project_slug)
    if path.exists():
        return
    path.write_text(
        f"# MASTER — {project_slug}\n\n"
        f"**Original task (summary):** {task_summary[:500]}\n\n"
        f"## Timeline\n\n"
        f"(Autocoder appends dated entries below. Code exports still go to Downloads.)\n",
        encoding="utf-8",
    )


def append_master(
    project_slug: str,
    *,
    phase: str,
    iteration: int,
    focus: str,
    did: str,
    next_steps: str,
    task: str = "",
) -> None:
    """Append one structured block. Keeps a single running doc per app."""
    if not project_slug.strip():
        return
    ensure_master_header(project_slug, task or "(see session)")
    path = master_path(project_slug)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = (
        f"\n### {ts} · iter {iteration} · **{phase}** · {focus}\n"
        f"- **Did / has now:** {did}\n"
        f"- **Next / will need:** {next_steps}\n"
    )
    try:
        cur = path.read_text(encoding="utf-8")
        path.write_text(cur + block, encoding="utf-8")
    except Exception:
        pass


GOAL_QUEUE_FILE = "goal_queue.json"


def goal_queue_path() -> Path:
    return _autocoder_root() / GOAL_QUEUE_FILE


def load_goal_queue() -> dict[str, Any]:
    p = goal_queue_path()
    if not p.exists():
        return {"goals": [], "current_index": 0}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"goals": [], "current_index": 0}
        data.setdefault("goals", [])
        data.setdefault("current_index", 0)
        return data
    except Exception:
        return {"goals": [], "current_index": 0}


def save_goal_queue(data: dict[str, Any]) -> None:
    try:
        goal_queue_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def queue_append(goal: dict[str, Any]) -> None:
    q = load_goal_queue()
    q["goals"].append(goal)
    save_goal_queue(q)


def queue_set(list_goals: list[dict[str, Any]], start_index: int = 0) -> None:
    save_goal_queue({"goals": list_goals, "current_index": start_index})


def queue_current() -> Optional[dict[str, Any]]:
    q = load_goal_queue()
    goals = q.get("goals") or []
    i = int(q.get("current_index", 0))
    if i < len(goals):
        return goals[i]
    return None


def queue_advance() -> Optional[dict[str, Any]]:
    q = load_goal_queue()
    goals = q.get("goals") or []
    i = int(q.get("current_index", 0)) + 1
    q["current_index"] = i
    save_goal_queue(q)
    if i < len(goals):
        return goals[i]
    return None
