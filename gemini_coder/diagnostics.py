"""Diagnostic report generation for Gemini Coder."""

import logging
import platform
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .platform_utils import PlatformInfo, get_config_dir, get_log_dir

logger = logging.getLogger(__name__)

REPORT_HEADER = """
================================================================================
                        GEMINI CODER - DIAGNOSTIC REPORT
================================================================================

Generated: {timestamp}

HOW TO SHARE THIS REPORT:
  If you need support, copy this file and share it. This report does NOT contain
  your API key or any passwords. It only contains system info and app state.

================================================================================
"""


def generate_diagnostic_report(
    platform_info: PlatformInfo,
    config,
    gemini_configured: bool,
    task_queue,
    uptime_seconds: float,
) -> str:
    """Generate a comprehensive diagnostic report."""
    sections = []

    sections.append(REPORT_HEADER.format(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ))

    sections.append(_section_system_info(platform_info))
    sections.append(_section_app_state(config, gemini_configured, uptime_seconds))
    sections.append(_section_dependencies())
    sections.append(_section_task_queue(task_queue))
    sections.append(_section_recent_errors())
    sections.append(_section_performance(uptime_seconds))
    sections.append(_section_connectivity())

    sections.append("\n" + "=" * 80 + "\nEND OF DIAGNOSTIC REPORT\n" + "=" * 80)

    return "\n".join(sections)


def _section_system_info(info: PlatformInfo) -> str:
    lines = [
        _section_header("SYSTEM INFORMATION"),
        f"  OS:              {info.os_name} {info.os_version}",
        f"  Architecture:    {info.architecture} ({info.machine})",
        f"  Python:          {info.python_version}",
        f"  Hostname:        {info.hostname}",
        f"  Raspberry Pi:    {'Yes' if info.is_raspberry_pi else 'No'}",
        f"  Headless:        {'Yes' if info.is_headless else 'No'}",
        f"  Screen:          {info.screen_width}x{info.screen_height}" if info.screen_width else "  Screen:          N/A",
        f"  RAM:             {info.total_ram_mb} MB" if info.total_ram_mb else "  RAM:             Unknown (install psutil)",
        f"  Free Disk:       {info.available_disk_gb} GB" if info.available_disk_gb else "  Free Disk:       Unknown",
    ]
    return "\n".join(lines)


def _section_app_state(config, gemini_configured: bool, uptime: float) -> str:
    uptime_str = str(timedelta(seconds=int(uptime)))
    lines = [
        _section_header("APPLICATION STATE"),
        f"  Uptime:          {uptime_str}",
        f"  API Connected:   {'Yes' if gemini_configured else 'No'}",
        f"  API Key Set:     {'Yes' if config.api_key else 'No'}",
        f"  Model:           {config.model_name}",
        f"  Temperature:     {config.temperature}",
        f"  Max Tokens:      {config.max_tokens}",
        f"  Theme:           {config.theme}",
        f"  Log Level:       {config.log_level}",
        f"  Config Dir:      {get_config_dir()}",
        f"  Log Dir:         {get_log_dir()}",
    ]
    return "\n".join(lines)


def _section_dependencies() -> str:
    lines = [_section_header("DEPENDENCY CHECK")]

    deps = [
        "customtkinter",
        "google.generativeai",
        "psutil",
        "PIL",
        "requests",
    ]

    for dep in deps:
        try:
            mod = __import__(dep.split(".")[0])
            version = getattr(mod, "__version__", getattr(mod, "VERSION", "installed"))
            lines.append(f"  {dep:30s} {ICONS['check']} {version}")
        except ImportError:
            lines.append(f"  {dep:30s} {ICONS['cross']} NOT INSTALLED")

    return "\n".join(lines)


def _section_task_queue(task_queue) -> str:
    lines = [_section_header("TASK QUEUE STATE")]
    tasks = task_queue.tasks
    lines.append(f"  Total tasks:     {len(tasks)}")
    lines.append(f"  Pending:         {len(task_queue.pending_tasks)}")
    lines.append(f"  Est. remaining:  {task_queue.total_time_remaining():.0f} min")

    if tasks:
        lines.append("")
        lines.append("  Recent tasks:")
        for task in tasks[-10:]:
            elapsed = timedelta(seconds=int(task.elapsed_seconds))
            lines.append(
                f"    [{task.status.value:10s}] {task.title[:50]:50s} "
                f"({elapsed} / {task.time_budget_minutes}min)"
            )

    return "\n".join(lines)


def _section_recent_errors() -> str:
    lines = [_section_header("RECENT ERRORS")]

    log_dir = get_log_dir()
    log_file = log_dir / "gemini_coder.log"

    if not log_file.exists():
        lines.append("  No log file found.")
        return "\n".join(lines)

    try:
        content = log_file.read_text(encoding="utf-8", errors="ignore")
        error_lines = [
            line for line in content.split("\n")
            if "[ERROR]" in line or "[WARNING]" in line
        ]
        recent = error_lines[-50:]
        if recent:
            lines.append(f"  Last {len(recent)} errors/warnings:")
            for line in recent:
                lines.append(f"    {line.strip()[:120]}")
        else:
            lines.append("  No errors or warnings found. Looking good!")
    except OSError as e:
        lines.append(f"  Could not read log file: {e}")

    return "\n".join(lines)


def _section_performance(uptime: float) -> str:
    lines = [_section_header("PERFORMANCE")]

    try:
        import psutil
        process = psutil.Process()
        mem = process.memory_info()
        lines.append(f"  Memory (RSS):    {mem.rss / (1024*1024):.1f} MB")
        lines.append(f"  Memory (VMS):    {mem.vms / (1024*1024):.1f} MB")
        lines.append(f"  CPU Percent:     {process.cpu_percent(interval=0.5):.1f}%")
        lines.append(f"  Threads:         {process.num_threads()}")
    except ImportError:
        lines.append("  Install psutil for performance metrics: pip install psutil")
    except Exception as e:
        lines.append(f"  Error collecting metrics: {e}")

    return "\n".join(lines)


def _section_connectivity() -> str:
    lines = [_section_header("CONNECTIVITY")]

    import urllib.request
    targets = [
        ("Google AI API", "https://generativelanguage.googleapis.com"),
        ("Google (general)", "https://www.google.com"),
    ]

    for name, url in targets:
        try:
            req = urllib.request.Request(url, method="HEAD")
            resp = urllib.request.urlopen(req, timeout=5)
            lines.append(f"  {name:30s} {ICONS['check']} OK ({resp.status})")
        except Exception as e:
            lines.append(f"  {name:30s} {ICONS['cross']} FAILED ({e})")

    return "\n".join(lines)


def _section_header(title: str) -> str:
    return f"\n{'─' * 80}\n  {title}\n{'─' * 80}"


ICONS = {"check": "[OK]", "cross": "[!!]"}
