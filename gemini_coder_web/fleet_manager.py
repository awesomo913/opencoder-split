"""Fleet manager -- unified view of all coding devices (PCs + Pis).

Aggregates:
- PC sessions (top-left / top-right / bottom-left / bottom-right)
  running Autocoder (browser CDP) + optionally OpenCode (terminal)
- Pi devices loaded from picontrol/pi_config.json
  running AutocoderPi (SSH-driven)

The broadcast() method sends one goal to every online device simultaneously.
"""

import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Path to picontrol's pi config (relative to this file's package root)
_PI_CONFIG_DEFAULT = str(
    Path(__file__).resolve().parent.parent / "picontrol" / "pi_config.json"
)


@dataclass
class DeviceInfo:
    """Unified device descriptor used by the fleet view UI."""
    device_id: str          # unique key
    kind: str               # "pc" or "pi"
    name: str               # display name
    ip: str                 # IP / "local"
    tool: str               # "Autocoder" / "AutocoderPi" / "OpenCode"
    model: str              # AI model or browser profile
    status: str             # offline / idle / running / error
    current_task: str       # truncated task description
    last_output: str        # recent log lines
    iterations: int         # improvement cycle count


class FleetManager:
    """Central coordinator for all coding devices.

    Usage:
        fm = FleetManager(session_manager)
        fm.load_pis()          # from picontrol/pi_config.json
        fm.start()             # begin Pi polling threads
        fm.broadcast(goal)     # send to all PCs + Pis
        devices = fm.all_devices()  # for the fleet view
    """

    def __init__(self, session_manager=None) -> None:
        self._session_mgr = session_manager
        self._pi_pool = None
        self._status_cb: Optional[Callable] = None
        self._lock = threading.Lock()
        self._opencode_procs: dict = {}  # corner -> subprocess.Popen
        self._broadcast_goal: str = ""   # last goal sent via broadcast()

    # ── Pi management ──────────────────────────────────────────────

    def load_pis(
        self,
        config_path: str = _PI_CONFIG_DEFAULT,
        on_status_change: Optional[Callable] = None,
    ) -> list:
        """Load Pi devices from config. Returns list of names loaded."""
        from .pi_client import AutocoderPiPool
        if self._pi_pool is None:
            self._pi_pool = AutocoderPiPool()
        self._status_cb = on_status_change
        names = self._pi_pool.load_from_config(config_path, on_status_change)
        logger.info("FleetManager: loaded %d Pi(s): %s", len(names), names)
        return names

    def start(self) -> None:
        """Start all Pi background polling threads."""
        if self._pi_pool:
            self._pi_pool.start_all()

    def stop(self) -> None:
        """Stop all Pi threads and launched OpenCode processes."""
        if self._pi_pool:
            self._pi_pool.stop_all()
        with self._lock:
            for proc in self._opencode_procs.values():
                try:
                    proc.terminate()
                except Exception:
                    pass
            self._opencode_procs.clear()

    # ── OpenCode on PCs ────────────────────────────────────────────

    def launch_opencode(self, corner: str, goal: str = "") -> bool:
        """Launch OpenCode in a new terminal window for a PC session corner.

        OpenCode is a terminal-based AI coding agent. Each PC corner gets
        its own OpenCode window working on the same broadcast goal.
        """
        opencode_exe = self._find_opencode()
        if not opencode_exe:
            logger.warning("FleetManager: opencode not found on PATH")
            return False

        with self._lock:
            old = self._opencode_procs.get(corner)
            if old and old.poll() is None:
                logger.info("FleetManager: opencode already running for %s", corner)
                return True

        cmd = [opencode_exe, "run", goal[:400]] if goal else [opencode_exe, "run"]

        try:
            # Launch in a new console window so it's visible
            proc = subprocess.Popen(
                cmd,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                env={**os.environ, "OPENCODE_SESSION": f"autocoder-{corner}"},
            )
            with self._lock:
                self._opencode_procs[corner] = proc
            logger.info("FleetManager: launched OpenCode for %s (pid=%d)", corner, proc.pid)
            return True
        except Exception as exc:
            logger.error("FleetManager: failed to launch opencode for %s: %s", corner, exc)
            return False

    def stop_opencode(self, corner: str) -> None:
        with self._lock:
            proc = self._opencode_procs.pop(corner, None)
        if proc and proc.poll() is None:
            proc.terminate()

    def watchdog_opencode(self) -> None:
        """Restart any OpenCode process that has exited while a broadcast goal is active.

        Call this periodically (e.g. from the fleet-view refresh timer).
        Only acts when a broadcast goal is set and opencode is available.
        """
        goal = self._broadcast_goal
        if not goal or not self.opencode_available():
            return

        with self._lock:
            corners = list(self._opencode_procs.keys())

        for corner in corners:
            with self._lock:
                proc = self._opencode_procs.get(corner)
            if proc is not None and proc.poll() is not None:
                # Process exited — restart it on the same corner with the same goal.
                logger.info(
                    "FleetManager: watchdog restarting OpenCode for corner=%s", corner
                )
                self.launch_opencode(corner, goal=goal)

    @staticmethod
    def _find_opencode() -> Optional[str]:
        """Find the opencode executable on PATH."""
        import shutil
        for name in ("opencode", "opencode.exe", "opencode.cmd"):
            exe = shutil.which(name)
            if exe:
                return exe
        return None

    @staticmethod
    def opencode_available() -> bool:
        return FleetManager._find_opencode() is not None

    # ── Broadcast ──────────────────────────────────────────────────

    def broadcast(self, goal: str, include_pis: bool = True, include_pcs: bool = True) -> dict:
        """Send goal to every online device simultaneously.

        Returns {device_id: bool} indicating dispatch success.
        """
        self._broadcast_goal = goal
        results = {}

        # PCs: queue goal into each active session + optionally launch OpenCode
        if include_pcs and self._session_mgr:
            for session in self._session_mgr.active_sessions:
                device_id = f"pc:{session.corner}"
                try:
                    if session.task_queue:
                        from gemini_coder.task_manager import CodingTask
                        task = CodingTask(description=goal)
                        session.task_queue.add_task(task)
                    results[device_id] = True
                except Exception as exc:
                    logger.error("FleetManager: PC broadcast to %s failed: %s", session.corner, exc)
                    results[device_id] = False

                # Also launch / refresh OpenCode for this corner
                if self.opencode_available():
                    self.launch_opencode(session.corner, goal=goal)

        # Pis: send via SSH
        if include_pis and self._pi_pool:
            for status in self._pi_pool.all_statuses():
                device_id = f"pi:{status.ip}"
                client = self._pi_pool.get_client(status.ip)
                if client:
                    ok = client.send_task(goal)
                    results[device_id] = ok

        logger.info("FleetManager: broadcast dispatched to %d device(s)", len(results))
        return results

    # ── Status aggregation ─────────────────────────────────────────

    def all_devices(self) -> list:
        """Return unified DeviceInfo list for all PCs + Pis."""
        devices = []

        # PC sessions
        if self._session_mgr:
            for session in self._session_mgr.sessions:
                client = session.client
                using_cdp = client and client.using_cdp if hasattr(client, "using_cdp") else False

                opencode_running = False
                with self._lock:
                    proc = self._opencode_procs.get(session.corner)
                    opencode_running = proc is not None and proc.poll() is None

                status = "running" if session.is_running else ("idle" if session.is_configured else "offline")
                tool_parts = []
                if session.is_configured:
                    tool_parts.append("Autocoder")
                if opencode_running:
                    tool_parts.append("OpenCode")
                tool = " + ".join(tool_parts) if tool_parts else "—"

                devices.append(DeviceInfo(
                    device_id=f"pc:{session.corner}",
                    kind="pc",
                    name=f"PC {session.corner}",
                    ip="local",
                    tool=tool,
                    model=session.ai_profile.name if session.ai_profile else "?",
                    status=status,
                    current_task=(
                        session.task_queue.current_task.description[:80]
                        if session.task_queue and session.task_queue.current_task
                        else ""
                    ),
                    last_output="",
                    iterations=0,
                ))

        # Pi devices
        if self._pi_pool:
            for pi_status in self._pi_pool.all_statuses():
                caps = pi_status.capabilities
                pi_tool = "autoworker" if (caps and caps.has_autoworker) else "ollama"
                tool = f"AutocoderPi ({pi_tool})"
                model = pi_status.current_model or (
                    (caps.ollama_models[0] if caps and caps.ollama_models else "")
                )

                devices.append(DeviceInfo(
                    device_id=f"pi:{pi_status.ip}",
                    kind="pi",
                    name=pi_status.name,
                    ip=pi_status.ip,
                    tool=tool,
                    model=model,
                    status=pi_status.agent if pi_status.connection == "online" else pi_status.connection,
                    current_task=pi_status.current_task,
                    last_output=pi_status.last_output,
                    iterations=pi_status.iterations,
                ))

        return devices

    def pi_pool(self):
        return self._pi_pool
