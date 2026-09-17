"""AutocoderPi -- SSH-based coding agent for Raspberry Pi devices.

Distinct from the browser-based Autocoder used on PCs. AutocoderPi:
- Connects to Pi via SSH (paramiko)
- Detects what the Pi already has (autoworker, autocoder, ollama)
- Drives the Pi's existing autoworker/autocoder if present
- Falls back to an Ollama coding loop if no autoworker found
- Reports status back to the central AUI fleet view

Each Pi runs as an independent AutocoderPiClient. The FleetManager
coordinates broadcasting one goal to all connected Pis simultaneously.
"""

import json
import logging
import queue
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False
    logger.warning(
        "paramiko not installed -- Pi SSH connections unavailable. "
        "Run: uv pip install paramiko"
    )


# Paths AutocoderPi checks for an existing autoworker/autocoder on the Pi.
AUTOWORKER_CANDIDATES = [
    "~/autoworker.py",
    "~/autocoder/main.py",
    "~/autocoder.py",
    "~/autoworker/run.py",
    "~/autoworker/main.py",
    "~/ai/autoworker.py",
    "~/ai/autocoder.py",
]

# tmux session name used for all Pi jobs (makes kill / has-session unambiguous).
_TMUX_SESSION = "autocoderpi"

STATUS_OFFLINE   = "offline"
STATUS_ONLINE    = "online"
STATUS_DETECTING = "detecting"
STATUS_IDLE      = "idle"
STATUS_RUNNING   = "running"
STATUS_ERROR     = "error"

# Seconds after a job exits before re-firing an improvement pass.
FIRE_COOLDOWN = 45.0

# Seconds to wait after firing before verifying the process started.
_VERIFY_DELAY = 3.0

# Improvement focus phrases rotated across iterations (mirrors broadcast.py).
_IMPROVEMENT_FOCUSES = [
    "Review the code for edge cases and fix any you find.",
    "Add comprehensive error handling and input validation.",
    "Refactor for clarity: better variable names, smaller functions.",
    "Add docstrings and inline comments to every function.",
    "Optimize the most expensive operations.",
    "Write unit tests covering happy-path and error cases.",
    "Make the implementation more robust and production-ready.",
]


@dataclass
class PiCapabilities:
    """What tools AutocoderPi found on this Pi."""
    has_autoworker: bool = False
    autoworker_path: str = ""
    has_ollama: bool = False
    ollama_models: list = field(default_factory=list)
    has_tmux: bool = False
    python_cmd: str = "python3"
    pi_hostname: str = ""
    pi_os: str = ""


@dataclass
class PiStatus:
    """Live status of one Pi device."""
    name: str
    ip: str
    connection: str = STATUS_OFFLINE
    agent: str = STATUS_IDLE
    current_tool: str = ""
    current_model: str = ""
    current_task: str = ""
    last_output: str = ""
    iterations: int = 0
    capabilities: Optional[PiCapabilities] = None


def _ssh_run(
    ip: str,
    user: str,
    command: str,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    timeout: int = 15,
) -> tuple:
    """Run one SSH command. Returns (success: bool, output: str)."""
    if not PARAMIKO_AVAILABLE:
        return False, "[paramiko not installed]"
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        kw: dict = {"hostname": ip, "username": user, "timeout": timeout}
        if password:
            kw["password"] = password
        elif key_path:
            kw["key_filename"] = key_path
        else:
            kw["look_for_keys"] = True
        client.connect(**kw)
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        combined = (out + ("\n" + err if err else "")).strip()
        return True, combined
    except Exception as exc:
        return False, f"[SSH error] {exc}"
    finally:
        client.close()


class AutocoderPiClient:
    """Controls one Raspberry Pi as a coding agent (AutocoderPi).

    Tool priority: autoworker > ollama > none.
    Ollama uses tmux when available for reliable PTY-backed execution.
    After each job exits the loop re-fires with an improvement prompt.
    """

    POLL_INTERVAL = 10.0

    def __init__(
        self,
        name: str,
        ip: str,
        user: str = "pi",
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        on_status_change: Optional[Callable] = None,
    ) -> None:
        self._name = name
        self._ip = ip
        self._user = user
        self._password = password
        self._key_path = key_path
        self._on_status_change = on_status_change

        self._status = PiStatus(name=name, ip=ip)
        self._status_lock = threading.Lock()
        self._task_queue: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

    # ── Properties ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def ip(self) -> str:
        return self._ip

    # ── Status ────────────────────────────────────────────────────

    def get_status(self) -> PiStatus:
        with self._status_lock:
            return self._status

    def _update_status(self, **kwargs) -> None:
        with self._status_lock:
            for k, v in kwargs.items():
                setattr(self._status, k, v)
            snap = self._status
        if self._on_status_change:
            try:
                self._on_status_change(snap)
            except Exception:
                pass

    # ── SSH helpers ───────────────────────────────────────────────

    def _ssh(self, command: str, timeout: int = 15) -> tuple:
        return _ssh_run(
            self._ip, self._user, command,
            password=self._password, key_path=self._key_path,
            timeout=timeout,
        )

    # ── Connection / capability detection ─────────────────────────

    def connect(self) -> bool:
        """Test SSH connection and detect capabilities."""
        self._update_status(connection=STATUS_DETECTING, agent=STATUS_IDLE)
        ok, out = self._ssh("echo PING && hostname", timeout=8)
        if not ok or "PING" not in out:
            self._update_status(connection=STATUS_OFFLINE)
            logger.warning("AutocoderPi: cannot reach %s (%s)", self._name, self._ip)
            return False
        self._update_status(connection=STATUS_ONLINE)
        logger.info("AutocoderPi: connected to %s (%s)", self._name, self._ip)
        caps = self._detect_capabilities()
        self._update_status(capabilities=caps)
        return True

    def _detect_capabilities(self) -> PiCapabilities:
        """Single SSH round-trip gathers all capability info."""
        caps = PiCapabilities()

        # One compound command — sentinels delimit each section.
        probe = (
            # hostname + OS
            "HN=$(hostname); "
            "OS=$(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | head -1 | "
            "sed 's/PRETTY_NAME=//' | tr -d '\"'); "
            # python
            "PY=$(python3 --version 2>&1 | grep -q Python && echo python3 || echo python); "
            # autoworker — check candidate paths
            "AW=''; "
            "for f in " + " ".join(AUTOWORKER_CANDIDATES) + "; do "
            "exp=$(eval echo \"$f\"); [ -f \"$exp\" ] && AW=$exp && break; done; "
            # ollama models (path suppressed)
            "OL=''; command -v ollama > /dev/null 2>&1 && "
            "OL=$(ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | grep -v '^/'); "
            # tmux
            "TM=$(command -v tmux > /dev/null 2>&1 && echo 1 || echo 0); "
            # print sentinels
            "printf 'HN:%s\\nOS:%s\\nPY:%s\\nAW:%s\\nTM:%s\\nOL_START\\n%s\\n' "
            "\"$HN\" \"$OS\" \"$PY\" \"$AW\" \"$TM\" \"$OL\""
        )

        ok, out = self._ssh(probe, timeout=15)
        if not ok:
            logger.warning("AutocoderPi %s: capability probe failed: %s", self._name, out)
            return caps

        # Parse sentinels.
        for line in out.splitlines():
            if line.startswith("HN:"):
                caps.pi_hostname = line[3:].strip()
            elif line.startswith("OS:"):
                caps.pi_os = line[3:].strip()
            elif line.startswith("PY:"):
                caps.python_cmd = line[3:].strip() or "python3"
            elif line.startswith("AW:"):
                path = line[3:].strip()
                if path:
                    caps.has_autoworker = True
                    caps.autoworker_path = path
                    logger.info("AutocoderPi %s: autoworker at %s", self._name, path)
            elif line.startswith("TM:"):
                caps.has_tmux = line[3:].strip() == "1"

        if "OL_START" in out:
            ol_section = out.split("OL_START", 1)[1].strip()
            models = [
                m.strip() for m in ol_section.splitlines()
                if m.strip() and not m.startswith("/")
            ]
            if models:
                caps.has_ollama = True
                caps.ollama_models = models
                logger.info("AutocoderPi %s: ollama models: %s", self._name, models[:3])

        if caps.has_tmux:
            logger.info("AutocoderPi %s: tmux available — will use for Ollama jobs", self._name)

        return caps

    # ── Task dispatch ─────────────────────────────────────────────

    def send_task(self, goal: str) -> bool:
        """Queue a coding goal. Returns True (always queued)."""
        if self.get_status().connection not in (STATUS_ONLINE, STATUS_DETECTING):
            logger.warning("AutocoderPi %s: offline, queuing task anyway", self._name)
        self._task_queue.put(goal)
        return True

    def start(self) -> None:
        """Start the background polling/execution thread."""
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._cancel.clear()
        self._poll_thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"autocoderpi-{self._name}",
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self._cancel.set()
        self._ssh(
            f"tmux kill-session -t {_TMUX_SESSION} 2>/dev/null; "
            "pkill -f autoworker 2>/dev/null",
            timeout=5,
        )
        self._update_status(agent=STATUS_IDLE, current_task="")

    # ── Tool selection ────────────────────────────────────────────

    def _choose_tool(self) -> tuple:
        """Return (tool_name, model_name). Priority: autoworker > ollama > none."""
        caps = self.get_status().capabilities or PiCapabilities()
        if caps.has_autoworker:
            return "autoworker", ""
        if caps.has_ollama and caps.ollama_models:
            # Prefer a coding-focused model if one exists.
            for model in caps.ollama_models:
                if any(k in model.lower() for k in ["coder", "code", "qwen", "deepseek", "codellama"]):
                    return "ollama", model
            return "ollama", caps.ollama_models[0]
        return "none", ""

    # ── Execution ─────────────────────────────────────────────────

    def _build_iteration_prompt(self, goal: str, prev_output: str, iteration: int) -> str:
        """Improvement prompt for iteration N >= 1 (escalating focus)."""
        focus = _IMPROVEMENT_FOCUSES[(iteration - 1) % len(_IMPROVEMENT_FOCUSES)]
        snippet = prev_output[-600:].strip() if prev_output else "(no prior output)"
        return (
            f"You are a coding expert in an iterative improvement loop (pass {iteration}).\n"
            f"Original goal: {goal[:200]}\n\n"
            f"Most recent output:\n{snippet}\n\n"
            f"Improvement focus: {focus}\n"
            f"Apply the improvement and output the updated, complete implementation."
        )

    def _execute_goal(self, goal: str, iteration: int = 0) -> bool:
        """Fire a coding job on the Pi. Returns True if launch command succeeded.

        iteration=0  → first run, raw goal.
        iteration>=1 → improvement pass with prev-output context.
        Uses tmux for Ollama (needs PTY); nohup for autoworker (plain script).
        """
        tool, model = self._choose_tool()
        if tool == "none":
            self._update_status(
                agent=STATUS_ERROR,
                last_output="No coding tool found on Pi (no autoworker or ollama).",
            )
            return False

        status = self.get_status()
        prev_output = status.last_output if iteration > 0 else ""
        prompt = (
            self._build_iteration_prompt(goal, prev_output, iteration)
            if iteration > 0
            else f"You are a coding expert. Implement completely: {goal[:300]}"
        )

        self._update_status(
            agent=STATUS_RUNNING,
            current_tool=tool,
            current_model=model,
            current_task=goal[:120],
        )
        logger.info("AutocoderPi %s: %s iteration=%d", self._name, tool, iteration)

        caps = self.get_status().capabilities or PiCapabilities()

        if tool == "autoworker":
            # Plain Python script — no TTY needed; nohup is fine.
            safe_goal = shlex.quote(goal[:300])
            cmd = (
                f"nohup {caps.python_cmd} {caps.autoworker_path} {safe_goal} "
                f">> ~/autocoderpi_task.log 2>&1 &"
            )

        else:  # ollama
            safe_model = shlex.quote(model)
            safe_prompt = shlex.quote(prompt)
            inner = (
                f"printf %s {safe_prompt} | ollama run {safe_model} "
                f">> ~/autocoderpi_task.log 2>&1"
            )
            if caps.has_tmux:
                # Kill any stale session first, then start fresh in a detached PTY.
                cmd = (
                    f"tmux kill-session -t {_TMUX_SESSION} 2>/dev/null; "
                    f"tmux new-session -d -s {_TMUX_SESSION} {shlex.quote(inner)}"
                )
            else:
                cmd = f"nohup bash -c {shlex.quote(inner)} &"

        ok, out = self._ssh(cmd, timeout=12)
        if not ok:
            self._update_status(agent=STATUS_ERROR, last_output=out)
            return False

        # Verify the process actually started after a short delay.
        time.sleep(_VERIFY_DELAY)
        started = self._verify_started(tool)
        if not started:
            msg = f"Process did not start (tool={tool}). Launch output: {out[:200]}"
            logger.warning("AutocoderPi %s: %s", self._name, msg)
            self._update_status(agent=STATUS_ERROR, last_output=msg)
            return False

        return True

    def _verify_started(self, tool: str) -> bool:
        """Return True if the expected process is running on the Pi."""
        caps = self.get_status().capabilities or PiCapabilities()

        if tool == "autoworker":
            pattern = "autoworker"
        else:
            pattern = "ollama"

        if tool == "ollama" and caps.has_tmux:
            # Check tmux session exists OR ollama process is running.
            check = (
                f"tmux has-session -t {_TMUX_SESSION} 2>/dev/null && echo 1 || "
                f"pgrep -c -f {shlex.quote(pattern)} 2>/dev/null || echo 0"
            )
        else:
            check = f"pgrep -c -f {shlex.quote(pattern)} 2>/dev/null || echo 0"

        ok, out = self._ssh(check, timeout=8)
        return ok and out.strip() not in ("", "0")

    # ── Polling ───────────────────────────────────────────────────

    def _poll_status(self) -> None:
        """Single SSH round-trip: running state + log tail + iteration count."""
        caps = self.get_status().capabilities or PiCapabilities()

        # Running check: tmux session OR pgrep for autoworker/ollama.
        if caps.has_tmux:
            running_check = (
                f"RUNNING=$(tmux has-session -t {_TMUX_SESSION} 2>/dev/null && echo 1 || "
                f"pgrep -c -f 'autoworker|ollama' 2>/dev/null || echo 0); "
            )
        else:
            running_check = (
                "RUNNING=$(pgrep -c -f 'autoworker|ollama' 2>/dev/null || echo 0); "
            )

        cmd = (
            running_check
            + "ITERS=$(grep -c -i 'iteration\\|loop\\|cycle\\|attempt' "
            "~/autocoderpi_task.log 2>/dev/null || echo 0); "
            "LOG=$(tail -20 ~/autocoderpi_task.log 2>/dev/null || echo ''); "
            "printf 'RUNNING:%s\\nITERS:%s\\nLOG_START\\n%s' \"$RUNNING\" \"$ITERS\" \"$LOG\""
        )

        ok, out = self._ssh(cmd, timeout=12)
        if not ok:
            self._update_status(connection=STATUS_OFFLINE)
            return

        running_str = "0"
        iters_str = "0"
        log_tail = ""

        for line in out.splitlines():
            if line.startswith("RUNNING:"):
                running_str = line.split(":", 1)[1].strip()
            elif line.startswith("ITERS:"):
                iters_str = line.split(":", 1)[1].strip()

        if "LOG_START" in out:
            log_tail = out.split("LOG_START", 1)[1].strip()

        is_running = running_str not in ("", "0")
        try:
            iters = int(iters_str)
        except ValueError:
            iters = 0

        self._update_status(
            connection=STATUS_ONLINE,
            agent=STATUS_RUNNING if is_running else STATUS_IDLE,
            last_output=log_tail[-500:] if log_tail else "",
            iterations=iters,
        )

    # ── Main loop ─────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Background thread: connect → receive goal → fire → poll → improve → repeat.

        - Drains task queue each cycle (latest goal wins, resets iteration counter).
        - Re-fires with improvement prompt after FIRE_COOLDOWN seconds of idle.
        - Post-fire verification: if process didn't start, sets ERROR and retries next cycle.
        """
        # Initial connect with exponential back-off retry.
        backoff = 15
        while not self._cancel.is_set() and not self.connect():
            self._cancel.wait(timeout=backoff)
            backoff = min(backoff * 2, 120)

        if self._cancel.is_set():
            return

        current_goal: str = ""
        pi_iteration: int = 0
        last_fired_at: float = 0.0

        while not self._cancel.is_set():
            # Drain queued goals — last one wins.
            new_goal: Optional[str] = None
            while True:
                try:
                    new_goal = self._task_queue.get_nowait()
                except queue.Empty:
                    break
            if new_goal:
                current_goal = new_goal
                pi_iteration = 0
                last_fired_at = 0.0  # fire immediately on first poll

            if current_goal and not self._cancel.is_set():
                now = time.time()
                cooldown_ok = (now - last_fired_at) >= FIRE_COOLDOWN
                status = self.get_status()
                is_idle = status.agent in (STATUS_IDLE, STATUS_ERROR)

                if cooldown_ok and is_idle:
                    has_output = bool(status.last_output.strip())
                    if pi_iteration == 0 or has_output:
                        fired = self._execute_goal(current_goal, iteration=pi_iteration)
                        if fired:
                            pi_iteration += 1
                            last_fired_at = time.time()
                        else:
                            # Launch failed — back off before retry.
                            last_fired_at = time.time()

            self._poll_status()
            self._cancel.wait(timeout=self.POLL_INTERVAL)


class AutocoderPiPool:
    """Manages a set of AutocoderPiClient instances."""

    def __init__(self) -> None:
        self._clients: dict = {}  # ip -> AutocoderPiClient
        self._lock = threading.Lock()

    def load_from_config(
        self,
        config_path: str,
        on_status_change: Optional[Callable] = None,
    ) -> list:
        """Load Pis from pi_config.json. Returns list of names added."""
        added = []
        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            for entry in data.get("pis", []):
                ip = entry.get("ip", "")
                if not ip or ip in self._clients:
                    continue
                client = AutocoderPiClient(
                    name=entry.get("name", ip),
                    ip=ip,
                    user=entry.get("user", "pi"),
                    password=entry.get("password"),
                    key_path=entry.get("key_path"),
                    on_status_change=on_status_change,
                )
                with self._lock:
                    self._clients[ip] = client
                added.append(client.name)
        except Exception as exc:
            logger.error("AutocoderPiPool.load_from_config: %s", exc)
        return added

    def start_all(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
        for c in clients:
            c.start()

    def stop_all(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
        for c in clients:
            c.stop()

    def broadcast(self, goal: str) -> int:
        """Send goal to all Pis. Returns count dispatched."""
        with self._lock:
            clients = list(self._clients.values())
        return sum(1 for c in clients if c.send_task(goal))

    def all_statuses(self) -> list:
        with self._lock:
            clients = list(self._clients.values())
        return [c.get_status() for c in clients]

    def get_client(self, ip: str) -> Optional[AutocoderPiClient]:
        with self._lock:
            return self._clients.get(ip)

    def add_pi(
        self,
        name: str,
        ip: str,
        user: str = "pi",
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        on_status_change: Optional[Callable] = None,
    ) -> "AutocoderPiClient":
        client = AutocoderPiClient(
            name=name, ip=ip, user=user,
            password=password, key_path=key_path,
            on_status_change=on_status_change,
        )
        with self._lock:
            self._clients[ip] = client
        client.start()
        return client
