"""Platform detection and hardware abstraction layer."""

import platform
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PlatformInfo:
    """Detected platform information."""
    os_name: str = ""
    os_version: str = ""
    architecture: str = ""
    machine: str = ""
    python_version: str = ""
    is_raspberry_pi: bool = False
    is_headless: bool = False
    screen_width: int = 0
    screen_height: int = 0
    total_ram_mb: int = 0
    available_disk_gb: float = 0.0
    hostname: str = ""
    extras: dict = field(default_factory=dict)


def detect_platform() -> PlatformInfo:
    """Detect the current platform and its capabilities."""
    info = PlatformInfo(
        os_name=platform.system(),
        os_version=platform.version(),
        architecture=platform.architecture()[0],
        machine=platform.machine(),
        python_version=platform.python_version(),
        hostname=platform.node(),
    )

    info.is_raspberry_pi = _check_raspberry_pi()

    try:
        import psutil
        info.total_ram_mb = int(psutil.virtual_memory().total / (1024 * 1024))
        info.available_disk_gb = round(
            psutil.disk_usage(str(Path.home())).free / (1024 ** 3), 1
        )
    except ImportError:
        pass

    info.is_headless = _check_headless()

    if not info.is_headless:
        try:
            import tkinter as tk
            root = tk.Tk()
            info.screen_width = root.winfo_screenwidth()
            info.screen_height = root.winfo_screenheight()
            root.destroy()
        except Exception:
            pass

    return info


def _check_raspberry_pi() -> bool:
    """Check if running on a Raspberry Pi."""
    if platform.machine().startswith("arm") or platform.machine() == "aarch64":
        dt_path = Path("/sys/firmware/devicetree/base/model")
        if dt_path.exists():
            try:
                model = dt_path.read_text(errors="ignore").lower()
                return "raspberry pi" in model
            except OSError:
                pass
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            try:
                content = cpuinfo.read_text(errors="ignore").lower()
                return "raspberry pi" in content or "bcm2" in content
            except OSError:
                pass
    return False


def _check_headless() -> bool:
    """Check if running in a headless environment."""
    if platform.system() == "Linux":
        display = __import__("os").environ.get("DISPLAY", "")
        wayland = __import__("os").environ.get("WAYLAND_DISPLAY", "")
        if not display and not wayland:
            return True
    return False


def get_desktop_path() -> Path:
    """Get the user's desktop path with fallback to home directory."""
    desktop = Path.home() / "Desktop"
    if desktop.exists() and desktop.is_dir():
        return desktop
    if platform.system() == "Windows":
        try:
            result = subprocess.run(
                ["powershell", "-Command",
                 "[Environment]::GetFolderPath('Desktop')"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                p = Path(result.stdout.strip())
                if p.exists():
                    return p
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return Path.home()


def get_config_dir() -> Path:
    """Get the application configuration directory."""
    if platform.system() == "Windows":
        base = Path(__import__("os").environ.get(
            "APPDATA", str(Path.home() / "AppData" / "Roaming")
        ))
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(
            __import__("os").environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        )
    config_dir = base / "gemini_coder"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_log_dir() -> Path:
    """Get the application log directory."""
    log_dir = get_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir
