"""Autocoder - Universal multi-session browser AI automation.

Manages up to 4 AI sessions simultaneously (Gemini, ChatGPT, Claude,
Ollama, OpenRouter, or any AI with a browser/window chat interface).
Each session gets a corner of the screen and its own task queue.

Broadcast Mode: Send one task to ALL sessions, loop improvements endlessly.
Prompt Architect integration: Tasks auto-engineered into production prompts.
"""

import customtkinter as ctk
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from gemini_coder import __version__ as base_version
from gemini_coder.config import ConfigManager
from gemini_coder.task_manager import TaskQueue, TaskExecutor, CodingTask, TaskStatus
from gemini_coder.expander import ExpansionEngine
from gemini_coder.history import HistoryManager
from gemini_coder.platform_utils import detect_platform, get_config_dir
from gemini_coder.ui.theme import get_colors, SPACING, ICONS
from gemini_coder.ui.app import GeminiCoderApp, StatusBar, ToastNotification

from .. import __version__, __app_name__
from ..ai_profiles import AIProfile, PRESET_PROFILES, get_profile_names, get_profile
from ..session_manager import SessionManager, Session, CORNERS
from ..auto_save import save_task_output
from ..broadcast import (
    BroadcastController, BroadcastConfig, engineer_prompt,
    PROMPT_ENGINE_AVAILABLE, IMPROVEMENT_FOCUSES, FOCUS_ORDER,
    FRONTIER_LENS_LABELS, frontier_lens_key_from_label, DEFAULT_FRONTIER_LENS,
    focuses_for_target, available_build_targets,
)
from ..window_manager import (
    list_candidate_windows, get_foreground_window,
    get_window_title, capture_foreground_and_position,
)
from ..cdp_client import (
    is_cdp_available, discover_cdp_targets,
    launch_chrome_with_cdp, launch_all_cdp_browsers, DEFAULT_CDP_PORT,
    CDP_CORNER_PORTS, get_cdp_port_for_corner,
    is_chrome_running, kill_chrome, prepare_for_cdp_launch,
)
from ..task_display_state import (
    set_running as _td_set_running,
    set_idle as _td_set_idle,
    set_iteration as _td_set_iteration,
    banner_lines,
    load_last_task_from_files,
)
from .. import coding_profiles as cprof

try:
    from mousetraffic.client import TrafficClient
    TRAFFIC_AVAILABLE = True
except ImportError:
    TRAFFIC_AVAILABLE = False

logger = logging.getLogger(__name__)

# Corner display labels
CORNER_LABELS = {
    "top-left": "Top-Left",
    "top-right": "Top-Right",
    "bottom-left": "Bot-Left",
    "bottom-right": "Bot-Right",
}


class Tooltip:
    """Hover-activated tooltip for any tkinter/CTk widget.

    Usage:  Tooltip(my_widget, "Text to show on hover")

    Shows a small borderless popup near the cursor after a short delay.
    Wraps text at a reasonable width. Self-destructs on leave.
    """

    _OPEN_DELAY_MS = 400     # How long to hover before showing
    _WRAP_PIXELS = 360       # Wrap long descriptions

    def __init__(self, widget, text: str, *,
                 bg: str = "#1f2937", fg: str = "#f9fafb") -> None:
        self._widget = widget
        self._text = text
        self._bg = bg
        self._fg = fg
        self._tip: ctk.CTkToplevel | None = None
        self._after_id: str | None = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _event=None) -> None:
        self._cancel_pending()
        self._after_id = self._widget.after(self._OPEN_DELAY_MS, self._show)

    def _on_leave(self, _event=None) -> None:
        self._cancel_pending()
        self._hide()

    def _cancel_pending(self) -> None:
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        if self._tip is not None or not self._text:
            return
        try:
            x = self._widget.winfo_rootx() + 20
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
            self._tip = ctk.CTkToplevel(self._widget)
            self._tip.overrideredirect(True)
            self._tip.attributes("-topmost", True)
            self._tip.configure(fg_color=self._bg)
            label = ctk.CTkLabel(
                self._tip, text=self._text,
                font=("Segoe UI", 10),
                text_color=self._fg,
                fg_color=self._bg,
                wraplength=self._WRAP_PIXELS,
                justify="left",
                padx=8, pady=6,
            )
            label.pack()
            self._tip.update_idletasks()
            # Clamp to screen so it doesn't fall off the right edge
            sw = self._tip.winfo_screenwidth()
            tw = self._tip.winfo_reqwidth()
            if x + tw > sw - 8:
                x = max(8, sw - tw - 8)
            self._tip.geometry(f"+{x}+{y}")
        except Exception:
            # Tooltip is a nice-to-have — never break the app
            self._tip = None

    def _hide(self) -> None:
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class SessionCard(ctk.CTkFrame):
    """UI card for one AI session slot (one corner of the screen)."""

    def __init__(self, parent, corner: str, colors: dict, app, **kwargs):
        super().__init__(parent, fg_color=colors["bg_card"], corner_radius=8,
                         border_width=1, border_color=colors["border"], **kwargs)
        self._corner = corner
        self._colors = colors
        self._app = app
        self._session: Session = None

        c = colors
        label = CORNER_LABELS.get(corner, corner)

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(8, 4))

        self._dot = ctk.CTkLabel(header, text="\u25CF", font=("Segoe UI", 14),
                                  text_color=c["fg_muted"])
        self._dot.pack(side="left", padx=(0, 4))

        ctk.CTkLabel(header, text=label, font=("Segoe UI", 12, "bold"),
                      text_color=c["fg_heading"]).pack(side="left")

        self._status_label = ctk.CTkLabel(header, text="Empty",
                                           font=("Segoe UI", 10),
                                           text_color=c["fg_muted"])
        self._status_label.pack(side="right")

        # AI selector
        ai_row = ctk.CTkFrame(self, fg_color="transparent")
        ai_row.pack(fill="x", padx=8, pady=2)

        ctk.CTkLabel(ai_row, text="AI:", font=("Segoe UI", 11),
                      text_color=c["fg_secondary"]).pack(side="left")

        self._ai_selector = ctk.CTkComboBox(
            ai_row, values=get_profile_names(),
            font=("Segoe UI", 11), width=140,
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._ai_selector.pack(side="left", padx=4)
        self._ai_selector.set("Gemini")

        # Window picker - list available windows
        pick_row = ctk.CTkFrame(self, fg_color="transparent")
        pick_row.pack(fill="x", padx=8, pady=2)

        self._window_picker = ctk.CTkComboBox(
            pick_row, values=["(click Refresh)"],
            font=("Segoe UI", 10), width=180, height=24,
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._window_picker.pack(side="left", padx=(0, 2))

        ctk.CTkButton(
            pick_row, text="\u21BB", font=("Segoe UI", 11),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], height=24, width=28,
            command=self._refresh_window_list,
        ).pack(side="left")

        # Buttons
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=8, pady=(2, 8))

        self._capture_btn = ctk.CTkButton(
            btn_row, text="Grab", font=("Segoe UI", 11, "bold"),
            fg_color="#8e44ad", hover_color="#9b59b6",
            height=28, width=55,
            command=self._on_capture,
        )
        self._capture_btn.pack(side="left", padx=1)

        self._launch_btn = ctk.CTkButton(
            btn_row, text="Launch", font=("Segoe UI", 11, "bold"),
            fg_color=c["accent"], hover_color=c["accent_hover"],
            height=28, width=60,
            command=self._on_launch,
        )
        self._launch_btn.pack(side="left", padx=1)

        self._start_btn = ctk.CTkButton(
            btn_row, text=f"{ICONS['play']}", font=("Segoe UI", 11, "bold"),
            fg_color=c["success"], hover_color="#27ae60",
            height=28, width=35, state="disabled",
            command=self._on_start,
        )
        self._start_btn.pack(side="left", padx=1)

        self._stop_btn = ctk.CTkButton(
            btn_row, text=f"{ICONS['stop']}", font=("Segoe UI", 11, "bold"),
            fg_color=c["error"],
            height=28, width=35, state="disabled",
            command=self._on_stop,
        )
        self._stop_btn.pack(side="left", padx=1)

        self._remove_btn = ctk.CTkButton(
            btn_row, text="X", font=("Segoe UI", 10),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["error"], height=28, width=28,
            command=self._on_remove,
        )
        self._remove_btn.pack(side="right")

        # CDP mode indicator
        self._mode_label = ctk.CTkLabel(self, text="", font=("Segoe UI", 9),
                                          text_color=c["fg_muted"])
        self._mode_label.pack(fill="x", padx=8, pady=(0, 4))

        # Window handle tracking
        self._window_handles: dict[str, int] = {}  # display_name -> hwnd
        self._task_text: str = ""  # Current task status text

    def _refresh_window_list(self) -> None:
        """Populate the window picker with available windows.

        Shows CDP tabs first (preferred), then pyautogui windows as fallback.
        """
        from ..universal_client import UniversalBrowserClient

        self._window_handles.clear()
        self._cdp_targets: dict[str, object] = {}  # display -> CDPTarget
        values = []

        # ── CDP targets first (preferred) — scan all ports ────
        seen_target_ids = set()
        try:
            for port in sorted(set(CDP_CORNER_PORTS.values())):
                cdp_targets = discover_cdp_targets(port)
                for target in cdp_targets:
                    if target.target_id in seen_target_ids:
                        continue
                    seen_target_ids.add(target.target_id)
                    if not target.title or len(target.title) < 3:
                        continue
                    if target.url.startswith("chrome://") or target.url == "about:blank":
                        continue
                    short = target.title[:45]
                    display = f"\U0001F310 {short}"
                    self._cdp_targets[display] = target
                    values.append(display)
        except Exception:
            pass

        # ── Pyautogui windows (fallback) ───────────────────────
        candidates = list_candidate_windows()
        with UniversalBrowserClient._claimed_lock:
            claimed = set(UniversalBrowserClient._claimed_hwnds)

        try:
            app_hwnd = self._app.winfo_id()
        except Exception:
            app_hwnd = None

        for hwnd, cls, title in candidates:
            if hwnd in claimed:
                continue
            if app_hwnd and hwnd == app_hwnd:
                continue
            if "autocoder" in title.lower():
                continue
            short = title[:45]
            display = f"[{hwnd}] {short}"
            self._window_handles[display] = hwnd
            values.append(display)

        if values:
            self._window_picker.configure(values=values)
            self._window_picker.set(values[0])
        else:
            self._window_picker.configure(values=["(no windows found)"])
            self._window_picker.set("(no windows found)")

    def _ensure_session(self) -> bool:
        """Create session for this corner if it doesn't exist yet. Returns True on success."""
        if self._session:
            return True
        ai_name = self._ai_selector.get()
        profile = get_profile(ai_name)
        try:
            self._session = self._app.session_mgr.create_session(profile, self._corner)
            self._wire_callbacks()
            self._app._activate_session(self._session)
            return True
        except RuntimeError as e:
            self._app._toast(str(e), "error")
            return False

    def _on_capture(self) -> None:
        """Capture the selected window from the picker dropdown."""
        selection = self._window_picker.get()

        # Check if it's a CDP target (starts with globe emoji)
        cdp_target = getattr(self, '_cdp_targets', {}).get(selection)
        hwnd = self._window_handles.get(selection)

        if not cdp_target and not hwnd:
            # Auto-refresh if dropdown is empty/stale
            self._refresh_window_list()
            selection = self._window_picker.get()
            cdp_target = getattr(self, '_cdp_targets', {}).get(selection)
            hwnd = self._window_handles.get(selection)

        if not cdp_target and not hwnd:
            self._app._toast("No windows found. Open a browser/terminal first.", "warning")
            return

        ai_name = self._ai_selector.get()
        profile = get_profile(ai_name)

        if not self._ensure_session():
            return

        self._session.ai_profile = profile
        if self._session.client:
            self._session.client.profile = profile

        if cdp_target:
            # CDP target — connect directly via CDP
            self._capture_btn.configure(state="disabled", text="...")
            self._status_label.configure(text="Connecting CDP...", text_color=self._colors["info"])

            def cdp_worker():
                from ..cdp_client import (
                    CDPConnection, CDPChatAutomation, get_selectors_for_profile,
                    claim_ws_url, release_ws_url, _find_unclaimed_target,
                    discover_cdp_targets, CDP_CORNER_PORTS,
                )
                try:
                    # ── Find an UNCLAIMED tab matching this AI site ──
                    # Don't reuse cdp_target directly — another session may
                    # have already claimed that exact WebSocket URL.
                    url_hint = cdp_target.url.split("/")[2] if "/" in cdp_target.url else ""
                    actual_target = None

                    # Try all ports to find an unclaimed tab for this site
                    for port in sorted(set(CDP_CORNER_PORTS.values())):
                        actual_target = _find_unclaimed_target(url_hint, "", port)
                        if actual_target:
                            break

                    if not actual_target:
                        # Fallback: try the original target if nothing unclaimed
                        actual_target = cdp_target

                    ws_url = actual_target.ws_url

                    # Claim the WebSocket URL to prevent other sessions using it
                    if not claim_ws_url(ws_url):
                        self.after(0, lambda: self._set_state("error"))
                        self.after(0, lambda: self._app._toast(
                            f"Tab already claimed by another session", "warning"))
                        return

                    conn = CDPConnection(ws_url)
                    if conn.connect():
                        selectors = get_selectors_for_profile(ai_name)
                        automation = CDPChatAutomation(conn, selectors, ai_name)
                        # Attach to the client
                        client = self._session.client
                        client._cdp = automation
                        client._cdp_available = True
                        client._configured = True
                        self._session.is_configured = True

                        self.after(0, lambda: self._set_state("ready"))
                        self.after(0, lambda: self._mode_label.configure(
                            text="Mode: CDP (reliable)", text_color=self._colors["success"]))
                        self.after(0, lambda: self._app._toast(
                            f"CDP connected: {actual_target.title[:35]} -> {CORNER_LABELS[self._corner]}", "success"))
                        self.after(0, self._app._update_assign_selector)
                        self.after(0, self._app._refresh_all_window_lists)
                    else:
                        release_ws_url(ws_url)
                        self.after(0, lambda: self._set_state("error"))
                        self.after(0, lambda: self._app._toast("CDP connection failed", "error"))
                except Exception as e:
                    self.after(0, lambda: self._set_state("error"))
                    self.after(0, lambda: self._app._toast(f"CDP error: {e}", "error"))

            threading.Thread(target=cdp_worker, daemon=True).start()
        else:
            # Pyautogui fallback — window handle
            ok = self._app.session_mgr.capture_window_for_session(
                self._session.session_id, hwnd
            )
            if ok:
                title = get_window_title(hwnd)
                self._set_state("ready")
                if self._session.client and self._session.client.using_cdp:
                    self._mode_label.configure(text="Mode: CDP (reliable)", text_color=self._colors["success"])
                    self._app._toast(f"Grabbed via CDP: {title[:35]} -> {CORNER_LABELS[self._corner]}", "success")
                else:
                    self._mode_label.configure(text="Mode: pyautogui (fallback)", text_color=self._colors["warning"])
                    self._app._toast(f"Grabbed: {title[:35]} -> {CORNER_LABELS[self._corner]}", "success")
                self._app._update_assign_selector()
                self._app._refresh_all_window_lists()
            else:
                self._app._toast("Failed to capture window", "error")

    def _on_launch(self) -> None:
        ai_name = self._ai_selector.get()
        profile = get_profile(ai_name)
        port = CDP_CORNER_PORTS.get(self._corner, DEFAULT_CDP_PORT)

        if not self._ensure_session():
            return

        self._session.ai_profile = profile
        if self._session.client:
            self._session.client.profile = profile
            self._session.client._cdp_port = port

        self._launch_btn.configure(state="disabled", text="...")
        self._status_label.configure(text="Launching...", text_color=self._colors["info"])

        def worker():
            # If no Chrome is listening on this corner's port, launch one now
            if not is_cdp_available(port):
                url = profile.url or "https://gemini.google.com/app"
                self.after(0, lambda: self._status_label.configure(
                    text=f"Starting Chrome on port {port}…",
                    text_color=self._colors["info"],
                ))
                prepare_for_cdp_launch(ports=[port])
                launch_chrome_with_cdp(url=url, port=port, corner=self._corner)

            ok = self._app.session_mgr.configure_session(self._session.session_id)
            if ok:
                self.after(0, lambda: self._set_state("ready"))
                cdp = self._session.client and self._session.client.using_cdp
                mode = "CDP" if cdp else "pyautogui"
                self.after(0, lambda: self._mode_label.configure(
                    text=f"Mode: {mode}" + (" (reliable)" if cdp else " (fallback)"),
                    text_color=self._colors["success"] if cdp else self._colors["warning"],
                ))
                self.after(0, lambda: self._app._toast(
                    f"{profile.name} ready [{mode}] at {CORNER_LABELS[self._corner]}", "success"))
                self.after(0, self._app._update_assign_selector)
            else:
                self.after(0, lambda: self._set_state("error"))
                self.after(0, lambda: self._app._toast(
                    f"Failed to find {profile.name} window", "error"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_start(self) -> None:
        if self._session:
            ok = self._app.session_mgr.start_session(self._session.session_id)
            if ok:
                self._set_state("running")
            else:
                self._app._toast("No tasks in queue or not configured", "warning")

    def _on_stop(self) -> None:
        if self._session:
            self._app.session_mgr.stop_session(self._session.session_id)
            self._set_state("ready")

    def _on_remove(self) -> None:
        if self._session:
            self._app.session_mgr.remove_session(self._session.session_id)
            self._session = None
        self._set_state("empty")

    def _wire_callbacks(self) -> None:
        if not self._session:
            return
        sid = self._session.session_id

        def on_output(kind, text):
            self._app._on_session_output(sid, kind, text)

        def on_task_start(task):
            self.after(0, lambda: self._set_state("running"))
            self._app._on_session_task_start(sid, task)

        def on_task_complete(task):
            self._app._on_session_task_complete(sid, task)
            # Check if more tasks pending
            if self._session and self._session.task_queue:
                if self._session.task_queue.pending_tasks:
                    self.after(0, lambda: self._set_state("running"))
                else:
                    self.after(0, lambda: self._set_state("ready"))

        def on_tick(task):
            self._app._on_session_tick(sid, task)

        def on_status(status, detail):
            self._app._on_session_status(sid, status, detail)

        self._app.session_mgr.set_session_callbacks(
            sid,
            on_output=on_output,
            on_task_start=on_task_start,
            on_task_complete=on_task_complete,
            on_tick=on_tick,
            on_status=on_status,
        )

    def _set_state(self, state: str) -> None:
        c = self._colors
        if state == "empty":
            self._dot.configure(text_color=c["fg_muted"])
            self._status_label.configure(text="Empty", text_color=c["fg_muted"])
            self._capture_btn.configure(state="normal", text="Grab")
            self._launch_btn.configure(state="normal", text="Launch")
            self._start_btn.configure(state="disabled")
            self._stop_btn.configure(state="disabled")
            self._ai_selector.configure(state="normal")
        elif state == "ready":
            self._dot.configure(text_color=c["success"])
            ai = self._session.ai_profile.name if self._session else "?"
            self._status_label.configure(text=f"{ai} Ready", text_color=c["success"])
            self._capture_btn.configure(state="normal", text="Grab")
            self._launch_btn.configure(state="normal", text="Relaunch")
            self._start_btn.configure(state="normal")
            self._stop_btn.configure(state="disabled")
        elif state == "running":
            self._dot.configure(text_color=c["warning"])
            self._status_label.configure(text=self._task_text or "Running...", text_color=c["warning"])
            self._capture_btn.configure(state="disabled")
            self._launch_btn.configure(state="disabled")
            self._start_btn.configure(state="disabled")
            self._stop_btn.configure(state="normal")
        elif state == "error":
            self._dot.configure(text_color=c["error"])
            self._status_label.configure(text="Error", text_color=c["error"])
            self._capture_btn.configure(state="normal", text="Grab")
            self._launch_btn.configure(state="normal", text="Retry")
            self._start_btn.configure(state="disabled")
            self._stop_btn.configure(state="disabled")

    @property
    def session(self) -> Session:
        return self._session


class FleetDeviceMiniCard(ctk.CTkFrame):
    """Compact status tile for one device in the Fleet Dashboard."""

    _STATUS_COLORS = {
        "running":   "#27ae60",
        "idle":      "#7f8c8d",
        "offline":   "#555555",
        "error":     "#e74c3c",
        "detecting": "#f39c12",
    }

    def __init__(self, parent, colors: dict, **kwargs) -> None:
        super().__init__(
            parent, fg_color=colors["bg_card"], corner_radius=6,
            border_width=1, border_color=colors["border"], **kwargs,
        )
        c = colors
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=6, pady=(6, 1))

        self._dot = ctk.CTkLabel(hdr, text="●", font=("Segoe UI", 12),
                                  text_color=c["fg_muted"])
        self._dot.pack(side="left", padx=(0, 3))

        self._name_lbl = ctk.CTkLabel(hdr, text="Device",
                                       font=("Segoe UI", 10, "bold"),
                                       text_color=c["fg_heading"])
        self._name_lbl.pack(side="left")

        self._ip_lbl = ctk.CTkLabel(self, text="",
                                     font=("Segoe UI", 9), text_color=c["fg_muted"])
        self._ip_lbl.pack(anchor="w", padx=6)

        self._tool_lbl = ctk.CTkLabel(self, text="—",
                                       font=("Segoe UI", 9), text_color=c["fg_secondary"])
        self._tool_lbl.pack(anchor="w", padx=6)

        self._status_lbl = ctk.CTkLabel(self, text="offline",
                                         font=("Segoe UI", 9), text_color=c["fg_muted"])
        self._status_lbl.pack(anchor="w", padx=6)

        self._task_lbl = ctk.CTkLabel(self, text="", font=("Segoe UI", 8),
                                       text_color=c["fg_muted"], wraplength=155, justify="left")
        self._task_lbl.pack(anchor="w", padx=6, pady=(0, 4))

    def update_from_device(self, dev, colors: dict) -> None:  # dev: DeviceInfo
        dot_color = self._STATUS_COLORS.get(dev.status, colors["fg_muted"])
        self._dot.configure(text_color=dot_color)
        self._name_lbl.configure(text=dev.name)
        ip_text = dev.ip if dev.ip not in ("local", "") else ""
        self._ip_lbl.configure(text=ip_text)
        self._tool_lbl.configure(text=dev.tool or "—")
        self._status_lbl.configure(text=dev.status, text_color=dot_color)
        task = dev.current_task
        if len(task) > 55:
            task = task[:52] + "…"
        self._task_lbl.configure(text=task)


class AutocoderApp(GeminiCoderApp):
    """AI Browser Coder - multi-session universal AI automation."""

    _enable_fleet: bool = True

    def __init__(self) -> None:
        # super().__init__(). The base GeminiCoderApp.__init__ constructs a
        # real GeminiClient + TaskExecutor + ExpansionEngine and then calls
        # self._build_ui() which builds expand/tasks/history/diagnostics
        # views we don't want in browser mode. We override _build_ui below
        # to build a minimal container set (status bar + content + settings
        # view) and call it explicitly after our own state is set up.
        ctk.CTk.__init__(self)

        self._start_time = time.time()
        self._config_manager = ConfigManager()
        self._cfg = self._config_manager.config
        self._platform = detect_platform()

        ctk.set_appearance_mode(self._cfg.theme)
        ctk.set_default_color_theme("blue")
        self._colors = get_colors(self._cfg.theme)

        self.title(f"{__app_name__} v{__version__}")
        self.geometry(f"{self._cfg.window_width}x{self._cfg.window_height}")
        self.minsize(1000, 650)

        # ── Session manager (replaces single client) ─────────────
        # Clear any stale claimed hwnds from a previous run
        from ..universal_client import UniversalBrowserClient
        UniversalBrowserClient._claimed_hwnds.clear()

        self.session_mgr = SessionManager()

        # No default session — session cards handle creation.
        # Base class references self._gemini, so use a stub with is_configured=False.
        class _Stub:
            is_configured = False
            def cancel(self): pass
            def update_settings(self, **kw): pass
        self._gemini = _Stub()
        self._task_queue = TaskQueue()  # Empty queue until a session is selected
        self._task_executor = None
        self._expander = None  # Created when first session connects
        self._history = HistoryManager()

        # Track active session for output display
        self._active_session_id = None
        self._session_outputs: dict[str, str] = {}  # session_id -> latest output

        self._current_view = "expand"
        self._last_completed_task = None
        self._session_cards: dict[str, SessionCard] = {}

        if self._enable_fleet:
            from ..fleet_manager import FleetManager
            self._fleet_mgr = FleetManager(self.session_mgr)
            self._fleet_device_cards: dict[str, FleetDeviceMiniCard] = {}
            self._fleet_poll_id: str | None = None
        else:
            self._fleet_mgr = None
            self._fleet_device_cards = {}
            self._fleet_poll_id = None

        self._broadcast = BroadcastController(self.session_mgr)
        self._broadcast.set_callbacks(
            on_output=lambda sid, kind, text: self._on_session_output(sid, kind, text),
            on_status=lambda msg: self.after(0, lambda: self._bc_status.configure(text=msg)),
            on_iteration=self._on_broadcast_iteration,
            on_complete=self._on_broadcast_complete,
        )
        self._auto_resume_attempts = 0
        self._openclaw_ref_bootstrap_done = False

        self._setup_logging()
        self._build_ui()
        self._bind_shortcuts()
        self._setup_callbacks()
        self._start_clock()

        self.after(500, lambda: self._show_view("settings"))
        self.after(200, self._refresh_task_panel)
        self.after(400, self._restore_prompt_if_placeholder)
        self.after(1200, self._maybe_startup_summary)
        self.after(600, self._bootstrap_openclaw_reference_pack)
        self.after(3500, self._maybe_auto_resume_broadcast)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if self._enable_fleet:
            def _start_fleet():
                try:
                    self._fleet_mgr.load_pis()
                    self._fleet_mgr.start()
                except Exception as exc:
                    logger.warning("FleetManager Pi load failed: %s", exc)
            threading.Thread(target=_start_fleet, daemon=True).start()
            self.after(2500, self._refresh_fleet_view)

    # ── UI construction ───────────────────────────────────────────

    def _build_ui(self) -> None:
        """Minimal UI: status bar (bottom) + scrollable content (fill).

        Overrides the base class's full UI (top bar + sidebar + 5 views) since
        browser mode only uses the settings-style session management view.
        Pack ORDER matters: status bar MUST be packed at side="bottom" BEFORE
        the content frame is packed with expand=True. If content is packed
        first, it claims all available space and pushes the status bar off
        the bottom edge on maximized windows.
        """
        c = self._colors
        self.configure(fg_color=c["bg_primary"])

        # Status bar FIRST at bottom so it reserves its slot.
        # Note: StatusBar.__init__ already sets fg_color internally from
        # `colors["bg_secondary"]` then forwards **kwargs — passing
        # fg_color here raises "multiple values for keyword argument".
        self._status_bar = StatusBar(self, colors=c)
        self._status_bar.pack(fill="x", side="bottom")
        self._status_bar.set_status("Ready", "info")

        # Main content container AFTER so it fills the remaining space.
        self._content = ctk.CTkFrame(
            self, fg_color=c["bg_primary"], corner_radius=0
        )
        self._content.pack(fill="both", expand=True)

        # Base class _show_view iterates these — must exist even if empty.
        self._frames: dict[str, ctk.CTkFrame] = {}
        self._nav_buttons: dict[str, ctk.CTkButton] = {}

        # Build the one view this app uses and pack it immediately.
        self._build_settings_view()
        if "settings" in self._frames:
            self._frames["settings"].pack(fill="both", expand=True)

    def _settings_banner(self, scroll: ctk.CTkScrollableFrame) -> None:
        """Title + subtitle above session slots (override in OpenCode-only app)."""
        c = self._colors
        ctk.CTkLabel(
            scroll, text=f"{ICONS['gear']} AI Browser Coder - Sessions",
            font=("Segoe UI", 20, "bold"),
            text_color=c["fg_heading"],
        ).pack(anchor="w", pady=(0, 4))

        ctk.CTkLabel(
            scroll,
            text="Connect ONE AI window. Autocode builds and endlessly improves code.",
            font=("Segoe UI", 12),
            text_color=c["fg_secondary"],
        ).pack(anchor="w", pady=(0, SPACING["md"]))

    def _build_session_slots(self, scroll: ctk.CTkScrollableFrame) -> None:
        """CDP/browser session cards (four corners; primary slot visible)."""
        c = self._colors
        session_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        session_frame.pack(fill="x", pady=SPACING["sm"])

        top_row = ctk.CTkFrame(session_frame, fg_color="transparent")
        top_row.pack(fill="x", pady=2)

        card_tl = SessionCard(top_row, "top-left", c, self)
        card_tl.pack(fill="x", expand=True)
        self._session_cards["top-left"] = card_tl

        card_tr = SessionCard(top_row, "top-right", c, self)
        self._session_cards["top-right"] = card_tr

        card_bl = SessionCard(top_row, "bottom-left", c, self)
        self._session_cards["bottom-left"] = card_bl

        card_br = SessionCard(top_row, "bottom-right", c, self)
        self._session_cards["bottom-right"] = card_br

    # ── Settings view with session management ─────────────────────

    def _build_settings_view(self) -> None:
        c = self._colors
        frame = ctk.CTkFrame(self._content, fg_color=c["bg_primary"], corner_radius=0)
        self._frames["settings"] = frame

        scroll = ctk.CTkScrollableFrame(frame, fg_color=c["bg_primary"])
        scroll.pack(fill="both", expand=True, padx=SPACING["lg"], pady=SPACING["md"])

        self._settings_banner(scroll)

        self._build_session_slots(scroll)

        # ── TASK ASSIGNMENT ──────────────────────────────────────
        assign_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        assign_card.pack(fill="x", pady=SPACING["sm"])

        ctk.CTkLabel(
            assign_card, text=f"{ICONS['tasks']} Assign Tasks to Sessions",
            font=("Segoe UI", 15, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")

        ctk.CTkLabel(
            assign_card, text=(
                "When you add a task in the Task Queue view, it goes to the "
                "selected session below."
            ),
            font=("Segoe UI", 11),
            text_color=c["fg_muted"],
            wraplength=650, justify="left",
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        assign_row = ctk.CTkFrame(assign_card, fg_color="transparent")
        assign_row.pack(fill="x", padx=SPACING["lg"], pady=(0, SPACING["md"]))

        ctk.CTkLabel(assign_row, text="Send tasks to:",
                      font=("Segoe UI", 12, "bold"),
                      text_color=c["fg_primary"]).pack(side="left")

        self._assign_selector = ctk.CTkComboBox(
            assign_row, values=["(no sessions yet)"],
            font=("Segoe UI", 11), width=250,
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
            command=self._on_assign_change,
        )
        self._assign_selector.pack(side="left", padx=SPACING["sm"])

        # ── BROADCAST MODE (Endless Loop) ────────────────────────
        bc_card = ctk.CTkFrame(scroll, fg_color=c["bg_tertiary"], corner_radius=8,
                                border_color="#8e44ad", border_width=2)
        bc_card.pack(fill="x", pady=SPACING["sm"])

        ctk.CTkLabel(
            bc_card, text="\u26A1 Autocode — Endless Improvement Loop",
            font=("Segoe UI", 15, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")

        engine_tag = " (Prompt Architect active)" if PROMPT_ENGINE_AVAILABLE else " (raw prompts)"
        ctk.CTkLabel(
            bc_card, text=(
                "Type ONE task. It gets engineered into a production prompt" + engine_tag + ".\n"
                "The AI builds the code, then endlessly improves it until you click Stop.\n"
                "Each iteration saves a .py file to Downloads."
            ),
            font=("Segoe UI", 11),
            text_color=c["fg_muted"],
            wraplength=650, justify="left",
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        # ── Current / last task (persisted; survives restart) ─────
        self._ps = cprof.load_profile_settings()
        self._task_panel = ctk.CTkFrame(bc_card, fg_color=c["bg_input"],
                                        corner_radius=8, border_width=1, border_color=c["border"])
        if self._ps.get("show_task_panel", True):
            self._task_panel.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))
        self._task_title_lbl = ctk.CTkLabel(
            self._task_panel, text="", font=("Segoe UI", 12, "bold"), text_color="#8e44ad", anchor="w",
        )
        self._task_title_lbl.pack(fill="x", padx=8, pady=(6, 0))
        self._task_sub_lbl = ctk.CTkLabel(
            self._task_panel, text="", font=("Segoe UI", 10), text_color=c["fg_secondary"], anchor="w",
        )
        self._task_sub_lbl.pack(fill="x", padx=8, pady=(0, 2))
        self._task_body_lbl = ctk.CTkLabel(
            self._task_panel, text="",
            font=("Segoe UI", 11), text_color=c["fg_primary"],
            wraplength=620, justify="left", anchor="w",
        )
        self._task_body_lbl.pack(fill="x", padx=8, pady=(0, 6))

        # ── Coding profile (presets) + display opt-out ─────────────
        prof_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        prof_row.pack(fill="x", padx=SPACING["lg"], pady=(2, 2))
        ctk.CTkLabel(
            prof_row, text="Profile:",
            font=("Segoe UI", 11, "bold"), text_color=c["fg_primary"],
        ).pack(side="left")
        _pid = self._ps.get("active_preset", "balanced")
        if _pid not in cprof.PRESETS:
            _pid = "balanced"
        self._profile_combo = ctk.CTkComboBox(
            prof_row, values=cprof.preset_choices(), width=220,
            font=("Segoe UI", 11),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
            command=self._on_coding_profile_changed,
        )
        self._profile_combo.pack(side="left", padx=6)
        self._profile_combo.set(cprof.PRESETS[_pid]["label"])
        ctk.CTkButton(
            prof_row, text="Apply profile", width=100, height=28,
            font=("Segoe UI", 11), fg_color=c["bg_tertiary"],
            text_color=c["fg_secondary"],
            command=self._apply_active_profile_to_ui,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            prof_row, text="Edit plan…", width=100, height=28,
            font=("Segoe UI", 11), fg_color=c["bg_tertiary"],
            text_color=c["fg_secondary"],
            command=self._open_profile_editor,
        ).pack(side="left", padx=4)
        Tooltip(
            self._profile_combo,
            "Pick a preset for your project type, then Apply profile to set "
            "build target and improvement checkboxes. Edit plan overrides "
            "the preset you last edited.",
        )

        self._var_show_task_panel = ctk.BooleanVar(
            value=self._ps.get("show_task_panel", True))
        self._var_show_startup = ctk.BooleanVar(
            value=self._ps.get("show_startup_summary", True))
        self._var_autostart_windows = ctk.BooleanVar(
            value=bool(self._ps.get("autostart_windows_login", False)))
        self._var_autoresume_endless = ctk.BooleanVar(
            value=bool(self._ps.get("autoresume_endless_on_login", False)))
        disp_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        disp_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))
        ctk.CTkSwitch(
            disp_row, text="Show task / status box",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
            variable=self._var_show_task_panel,
            command=self._on_toggle_task_panel,
        ).pack(side="left", padx=(0, 12))
        ctk.CTkSwitch(
            disp_row, text="Startup tip (last task)",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
            variable=self._var_show_startup,
            command=self._on_toggle_startup_tip,
        ).pack(side="left")
        startup_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        startup_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))
        ctk.CTkSwitch(
            startup_row, text="Start Autocoder when Windows signs in",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
            variable=self._var_autostart_windows,
            command=self._on_toggle_windows_startup,
        ).pack(side="left")
        ctk.CTkSwitch(
            startup_row, text="Auto-resume endless worker at startup",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
            variable=self._var_autoresume_endless,
            command=self._on_toggle_windows_startup,
        ).pack(side="left", padx=(12, 0))

        # Task input
        self._bc_task_input = ctk.CTkTextbox(
            bc_card, height=60, font=("Segoe UI", 12),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"], border_width=1, corner_radius=8,
        )
        self._bc_task_input.pack(fill="x", padx=SPACING["lg"], pady=4)
        # [ported fix #24] Restore last-typed prompt if present
        from pathlib import Path as _P_lp
        _LAST_PROMPT_FILE = _P_lp.home() / ".autocoder" / "last_prompt.txt"
        _placeholder = "e.g., Build a calculator with history and themes"
        try:
            if _LAST_PROMPT_FILE.exists():
                saved = _LAST_PROMPT_FILE.read_text(encoding="utf-8")
                self._bc_task_input.insert("1.0", saved.strip() or _placeholder)
            else:
                self._bc_task_input.insert("1.0", _placeholder)
        except Exception:
            self._bc_task_input.insert("1.0", _placeholder)

        # Debounced save on every keystroke (400ms) so a crash/reload
        # doesn't lose what the user typed.
        self._last_prompt_save_id = None
        def _save_prompt(_e=None):
            if self._last_prompt_save_id:
                try: self.after_cancel(self._last_prompt_save_id)
                except Exception: pass
            def _do_save():
                try:
                    text = self._bc_task_input.get("1.0", "end").rstrip("\n")
                    _LAST_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
                    _LAST_PROMPT_FILE.write_text(text, encoding="utf-8")
                except Exception:
                    pass
            self._last_prompt_save_id = self.after(400, _do_save)
        self._bc_task_input.bind("<KeyRelease>", _save_prompt)
        self._bc_task_input.bind("<FocusOut>", _save_prompt)

        # Prompt helper row for non-coders: one click picks sensible defaults
        prompt_helper_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        prompt_helper_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))
        ctk.CTkButton(
            prompt_helper_row,
            text="Auto Select Setup",
            font=("Segoe UI", 11, "bold"),
            width=150,
            height=28,
            fg_color=c["bg_tertiary"],
            hover_color=c["bg_hover"],
            text_color=c["fg_secondary"],
            command=self._on_auto_select_from_prompt,
        ).pack(side="left")
        ctk.CTkLabel(
            prompt_helper_row,
            text="Reads your prompt and sets target/scope/templates/focuses for you.",
            font=("Segoe UI", 10),
            text_color=c["fg_muted"],
        ).pack(side="left", padx=8)

        # [ported fix #23] F10 → stop broadcast + drop KILL file so the
        # headless endless runner also shuts down.
        def _on_f10(event=None):
            try:
                (_P_lp.home() / ".autocoder").mkdir(parents=True, exist_ok=True)
                (_P_lp.home() / ".autocoder" / "KILL").write_text("1", encoding="utf-8")
                if hasattr(self, "_broadcast") and self._broadcast:
                    try: self._broadcast.stop()
                    except Exception: pass
                try: self._toast("F10: Kill signal sent", "warning")
                except Exception: pass
            except Exception as e:
                logger.warning("F10 kill handler failed: %s", e)
        self.bind_all("<F10>", _on_f10)

        # Attach files section
        file_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        file_row.pack(fill="x", padx=SPACING["lg"], pady=4)

        ctk.CTkButton(
            file_row, text="\U0001F4CE Attach Files",
            font=("Segoe UI", 11, "bold"), width=120, height=30,
            fg_color=c["bg_tertiary"], hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], corner_radius=6,
            command=self._on_attach_files,
        ).pack(side="left")

        ctk.CTkButton(
            file_row, text="Clear",
            font=("Segoe UI", 10), width=50, height=26,
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_muted"],
            command=self._on_clear_files,
        ).pack(side="left", padx=4)

        self._bc_file_label = ctk.CTkLabel(
            file_row, text="No files attached",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
        )
        self._bc_file_label.pack(side="left", padx=8)

        # Attached file list (hidden until files added)
        self._bc_file_list_frame = ctk.CTkFrame(bc_card, fg_color=c["bg_input"],
                                                  corner_radius=6, height=0)
        self._bc_attached_files: list[str] = []
        self._bc_file_labels: list[ctk.CTkLabel] = []

        # Build target selector
        bt_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        bt_row.pack(fill="x", padx=SPACING["lg"], pady=4)

        ctk.CTkLabel(bt_row, text="Build target:",
                      font=("Segoe UI", 11, "bold"),
                      text_color=c["fg_primary"]).pack(side="left")

        # Now sourced from broadcast.BUILD_TARGET_PRESETS so picking a
        # target auto-selects the right focuses. Keeps the UI in sync
        # with whatever presets are defined in one place.
        bt_values = available_build_targets()
        self._bc_build_target = ctk.CTkComboBox(
            bt_row, values=bt_values,
            font=("Segoe UI", 11), width=220,
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
            command=self._on_build_target_changed,  # auto-apply preset
        )
        self._bc_build_target.pack(side="left", padx=SPACING["sm"])
        # Default to Beginner-Friendly so new coders don't get overwhelmed
        self._bc_build_target.set("Beginner-Friendly")

        # Tiny hint label: how many focuses this target will pre-select
        self._bc_preset_hint = ctk.CTkLabel(
            bt_row, text="",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
        )
        self._bc_preset_hint.pack(side="left", padx=4)

        # Button to re-apply the preset if the user's modifications
        # got out of hand and they want a clean slate.
        ctk.CTkButton(
            bt_row, text="\u21BB Reapply preset",
            font=("Segoe UI", 10), width=120, height=26,
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_muted"],
            command=lambda: self._apply_build_target_preset(
                self._bc_build_target.get(), toast=True,
            ),
        ).pack(side="left", padx=4)

        # Strategy controls: scoped runs, template usage, reference-adjacent mode
        strategy_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        strategy_row.pack(fill="x", padx=SPACING["lg"], pady=4)
        ctk.CTkLabel(strategy_row, text="Run scope:",
                     font=("Segoe UI", 11, "bold"),
                     text_color=c["fg_primary"]).pack(side="left")
        self._bc_run_scope = ctk.CTkComboBox(
            strategy_row,
            values=["Full system", "Single component / feature"],
            width=210,
            font=("Segoe UI", 11),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._bc_run_scope.pack(side="left", padx=6)
        self._bc_run_scope.set("Single component / feature")

        ctk.CTkLabel(strategy_row, text="Templates:",
                     font=("Segoe UI", 11, "bold"),
                     text_color=c["fg_primary"]).pack(side="left", padx=(12, 4))
        self._bc_scaffold_mode = ctk.CTkComboBox(
            strategy_row,
            values=["None", "Selective", "Template-heavy"],
            width=130,
            font=("Segoe UI", 11),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._bc_scaffold_mode.pack(side="left", padx=4)
        self._bc_scaffold_mode.set("Selective")

        self._bc_reference_adjacent_mode = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            strategy_row,
            text="Build adjacent to attached reference code",
            variable=self._bc_reference_adjacent_mode,
            font=("Segoe UI", 10),
            text_color=c["fg_secondary"],
        ).pack(side="left", padx=(12, 0))

        ideation_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        ideation_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))
        self._bc_outside_box_mode = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            ideation_row,
            text="Outside-the-box mode (hunt non-obvious functions/features)",
            variable=self._bc_outside_box_mode,
            font=("Segoe UI", 10),
            text_color=c["fg_secondary"],
        ).pack(side="left")
        ctk.CTkLabel(
            ideation_row,
            text="Frequency (min):",
            font=("Segoe UI", 10),
            text_color=c["fg_muted"],
        ).pack(side="left", padx=(12, 4))
        self._bc_outside_box_freq = ctk.StringVar(value="10")
        ctk.CTkEntry(
            ideation_row, textvariable=self._bc_outside_box_freq, width=48
        ).pack(side="left")

        frontier_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        frontier_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))
        self._bc_frontier_overdrive = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            frontier_row,
            text="Apex Frontier overdrive (reference-first R&D — overrides timid defaults)",
            variable=self._bc_frontier_overdrive,
            font=("Segoe UI", 10, "bold"),
            text_color=c["fg_primary"],
        ).pack(side="left")
        ctk.CTkLabel(
            frontier_row,
            text="Frontier lens:",
            font=("Segoe UI", 10),
            text_color=c["fg_muted"],
        ).pack(side="left", padx=(14, 4))
        _fl_values = list(FRONTIER_LENS_LABELS.values())
        self._bc_frontier_lens = ctk.CTkComboBox(
            frontier_row,
            values=_fl_values,
            width=340,
            font=("Segoe UI", 11),
            fg_color=c["bg_input"],
            text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._bc_frontier_lens.pack(side="left", padx=2)
        self._bc_frontier_lens.set(
            FRONTIER_LENS_LABELS.get(DEFAULT_FRONTIER_LENS, _fl_values[0])
        )

        model_cycle_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        model_cycle_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))
        self._bc_model_rotation = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            model_cycle_row,
            text="Rotate models/providers in Chrome",
            variable=self._bc_model_rotation,
            font=("Segoe UI", 10),
            text_color=c["fg_secondary"],
        ).pack(side="left")
        ctk.CTkLabel(
            model_cycle_row, text="Interval (min):",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
        ).pack(side="left", padx=(12, 4))
        self._bc_model_rotation_minutes = ctk.StringVar(value="12")
        ctk.CTkEntry(model_cycle_row, textvariable=self._bc_model_rotation_minutes, width=48).pack(side="left")
        self._bc_model_rotation_random = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            model_cycle_row,
            text="Random order",
            variable=self._bc_model_rotation_random,
            font=("Segoe UI", 10),
            text_color=c["fg_secondary"],
        ).pack(side="left", padx=(12, 0))

        gate_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        gate_row.pack(fill="x", padx=SPACING["lg"], pady=4)
        self._bc_strict_acceptance = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            gate_row,
            text="Strict acceptance gate per iteration (auto-reject failures)",
            variable=self._bc_strict_acceptance,
            font=("Segoe UI", 10),
            text_color=c["fg_secondary"],
        ).pack(side="left")
        self._bc_promote_passing_only = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            gate_row,
            text="Promote only passing outputs (candidate_final/scratch)",
            variable=self._bc_promote_passing_only,
            font=("Segoe UI", 10),
            text_color=c["fg_secondary"],
        ).pack(side="left", padx=(12, 0))

        gate_cmd_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        gate_cmd_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))
        ctk.CTkLabel(gate_cmd_row, text="Acceptance commands (; separated):",
                     font=("Segoe UI", 10), text_color=c["fg_muted"]).pack(side="left")
        self._bc_acceptance_cmds = ctk.StringVar(
            value="npm test; npm run build; npm run lint; npm run smoke"
        )
        ctk.CTkEntry(
            gate_cmd_row, textvariable=self._bc_acceptance_cmds, width=520
        ).pack(side="left", padx=6)
        ctk.CTkLabel(gate_cmd_row, text="Golden branch:",
                     font=("Segoe UI", 10), text_color=c["fg_muted"]).pack(side="left", padx=(12, 4))
        self._bc_golden_branch = ctk.StringVar(value="ai/golden")
        ctk.CTkEntry(gate_cmd_row, textvariable=self._bc_golden_branch, width=140).pack(side="left")

        # Expand on stagnation toggle
        expand_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        expand_row.pack(fill="x", padx=SPACING["lg"], pady=4)

        self._bc_expand_on_stagnation = ctk.BooleanVar(value=False)
        self._bc_expand_switch = ctk.CTkSwitch(
            expand_row,
            text="Expand on stagnation — create new functions when code stops evolving",
            font=("Segoe UI", 11),
            text_color=c["fg_secondary"],
            variable=self._bc_expand_on_stagnation,
            onvalue=True, offvalue=False,
        )
        self._bc_expand_switch.pack(side="left")

        # ── Improvement Focus Options (checkboxes) ──────────────
        focus_label = ctk.CTkLabel(
            bc_card,
            text="Improvement Focuses:",
            font=("Segoe UI", 12, "bold"),
            text_color=c["fg_primary"],
        )
        focus_label.pack(padx=SPACING["lg"], pady=(8, 2), anchor="w")

        ctk.CTkLabel(
            bc_card,
            text="Select which improvement passes to run each cycle. Unchecked = use defaults.",
            font=("Segoe UI", 10),
            text_color=c["fg_muted"],
        ).pack(padx=SPACING["lg"], pady=(0, 4), anchor="w")

        # Two-column grid for focus checkboxes
        focus_grid = ctk.CTkFrame(bc_card, fg_color="transparent")
        focus_grid.pack(fill="x", padx=SPACING["lg"], pady=2)
        focus_grid.columnconfigure(0, weight=1)
        focus_grid.columnconfigure(1, weight=1)

        self._bc_focus_vars: dict[str, ctk.BooleanVar] = {}
        for i, key in enumerate(FOCUS_ORDER):
            info = IMPROVEMENT_FOCUSES[key]
            var = ctk.BooleanVar(value=False)
            self._bc_focus_vars[key] = var
            row_idx = i // 2
            col_idx = i % 2
            cb = ctk.CTkCheckBox(
                focus_grid,
                text=info["label"],
                font=("Segoe UI", 11),
                text_color=c["fg_secondary"],
                variable=var,
                onvalue=True, offvalue=False,
                checkbox_width=18, checkbox_height=18,
                corner_radius=4,
            )
            cb.grid(row=row_idx, column=col_idx, sticky="w", padx=(0, 12), pady=2)
            # Hover tooltip shows the full description so a coder knows
            # exactly what this focus will do before checking the box.
            Tooltip(cb, info["description"])

        # Apply the default target's preset so checkboxes match the
        # dropdown on first render (instead of everything being unchecked).
        self._apply_build_target_preset(
            self._bc_build_target.get(), toast=False,
        )

        # Perfection Loop toggle
        perf_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        perf_row.pack(fill="x", padx=SPACING["lg"], pady=(6, 2))

        self._bc_perfection_loop = ctk.BooleanVar(value=False)
        perf_switch = ctk.CTkSwitch(
            perf_row,
            text="Perfection Loop — cycle all selected focuses, repeat until fully optimized",
            font=("Segoe UI", 11, "bold"),
            text_color="#8e44ad",
            variable=self._bc_perfection_loop,
            onvalue=True, offvalue=False,
        )
        perf_switch.pack(side="left")

        # Session schedule: total cap, pre-review build window, review window, MASTER log
        sched_frame = ctk.CTkFrame(bc_card, fg_color="transparent")
        sched_frame.pack(fill="x", padx=SPACING["lg"], pady=(8, 2))
        ctk.CTkLabel(
            sched_frame,
            text="Schedule: total session min (0=∞) — default 2880 (48h)",
            font=("Segoe UI", 10),
            text_color=c["fg_muted"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self._bc_time_limit = ctk.StringVar(value="2880")
        ctk.CTkEntry(sched_frame, textvariable=self._bc_time_limit, width=48).grid(
            row=0, column=1, padx=4
        )
        ctk.CTkLabel(
            sched_frame, text="Build before review",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
        ).grid(row=0, column=2, sticky="w", padx=(12, 4))
        self._bc_build_before_review = ctk.StringVar(value="30")
        ctk.CTkEntry(sched_frame, textvariable=self._bc_build_before_review, width=40).grid(
            row=0, column=3, padx=2
        )
        ctk.CTkLabel(
            sched_frame, text="Review phase (min, 0=off)",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
        ).grid(row=0, column=4, sticky="w", padx=(12, 4))
        self._bc_review_phase = ctk.StringVar(value="0")
        ctk.CTkEntry(sched_frame, textvariable=self._bc_review_phase, width=40).grid(
            row=0, column=5, padx=2
        )

        sched2 = ctk.CTkFrame(bc_card, fg_color="transparent")
        sched2.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))
        ctk.CTkLabel(
            sched2, text="Project slug (MASTER log)", font=("Segoe UI", 10), text_color=c["fg_muted"],
        ).pack(side="left", padx=(0, 6))
        self._bc_project_slug = ctk.StringVar(value="")
        ctk.CTkEntry(sched2, textvariable=self._bc_project_slug, width=200).pack(side="left", padx=4)
        self._bc_master_log = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            sched2, text="MASTER log (~/.autocoder/projects/.../MASTER.md)",
            variable=self._bc_master_log, font=("Segoe UI", 10), text_color=c["fg_secondary"],
        ).pack(side="left", padx=(16, 4))
        self._bc_auto_advance = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            sched2, text="Auto-advance goal queue on session end",
            variable=self._bc_auto_advance, font=("Segoe UI", 10), text_color=c["fg_secondary"],
        ).pack(side="left", padx=8)

        # Select All / Clear All buttons
        focus_btn_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        focus_btn_row.pack(fill="x", padx=SPACING["lg"], pady=(2, 4))

        ctk.CTkButton(
            focus_btn_row, text="Select All",
            font=("Segoe UI", 10), width=80, height=26,
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"],
            command=lambda: [v.set(True) for v in self._bc_focus_vars.values()],
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            focus_btn_row, text="Clear All",
            font=("Segoe UI", 10), width=80, height=26,
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"],
            command=lambda: [v.set(False) for v in self._bc_focus_vars.values()],
        ).pack(side="left")

        # [ported fix #27] Build-mode buttons: Backend / GUI / Full (both).
        # Each launches run_endless.py as a detached subprocess with
        # --mode=... so backend and GUI develop separately but with the
        # same prompt-persistence + session-history backend.
        mode_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        mode_row.pack(fill="x", padx=SPACING["lg"], pady=(4, 2))
        ctk.CTkLabel(mode_row, text="Build mode:",
                      font=("Segoe UI", 11, "bold"),
                      text_color=c["fg_primary"]).pack(side="left")
        for label, mode_key in (("Backend only", "backend"),
                                 ("Frontend only", "gui"),
                                 ("Full (both)", "full")):
            ctk.CTkButton(
                mode_row, text=label,
                font=("Segoe UI", 11), width=110, height=28,
                fg_color=c["bg_tertiary"], hover_color=c["bg_hover"],
                text_color=c["fg_secondary"],
                command=lambda m=mode_key: self._on_mode_launch(m),
            ).pack(side="left", padx=4)

        # Buttons
        bc_btn_row = ctk.CTkFrame(bc_card, fg_color="transparent")
        bc_btn_row.pack(fill="x", padx=SPACING["lg"], pady=(4, SPACING["md"]))

        self._bc_start_btn = ctk.CTkButton(
            bc_btn_row, text="\u26A1 Start Autocoding",
            font=("Segoe UI", 14, "bold"),
            fg_color="#8e44ad", hover_color="#9b59b6",
            height=40,
            command=self._on_broadcast_start,
        )
        self._bc_start_btn.pack(side="left", padx=(0, 4))

        self._bc_stop_btn = ctk.CTkButton(
            bc_btn_row, text=f"{ICONS['stop']} Stop All (Ctrl+K)",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["error"],
            height=40, state="disabled",
            command=self._on_broadcast_stop,
        )
        self._bc_stop_btn.pack(side="left", padx=4)

        # Graceful-end button: finish current iteration, then produce one
        # last output — a FINAL polished version if the project is >1/3
        # done, or a HANDOFF document if it's still closer to the start.
        # Unlike Stop, this doesn't kill work in progress; it wraps up.
        self._bc_graceful_btn = ctk.CTkButton(
            bc_btn_row, text="\U0001F3C1 End Gracefully",
            font=("Segoe UI", 13, "bold"),
            fg_color="#d35400", hover_color="#e67e22",
            height=40, state="disabled",
            command=self._on_broadcast_graceful_end,
        )
        self._bc_graceful_btn.pack(side="left", padx=4)
        Tooltip(
            self._bc_graceful_btn,
            "Wrap up at the best stopping point: finish the current "
            "iteration, then produce one final output.\n\n"
            "• If the project is more than 1/3 done: a FINAL polished, "
            "consolidated version of the whole idea.\n\n"
            "• If it's closer to the start: a HANDOFF document "
            "listing what's done, what's remaining, and exactly what "
            "to do next — plus the current work completed so nothing "
            "is left mid-flight.\n\n"
            "Use this instead of Stop when you want a clean conclusion."
        )

        self._bc_save_btn = ctk.CTkButton(
            bc_btn_row, text="\U0001F4BE Save All",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["success"], hover_color="#27ae60",
            height=40,
            command=self._on_broadcast_save,
        )
        self._bc_save_btn.pack(side="left", padx=4)

        ctk.CTkButton(
            bc_btn_row, text="\U0001F4C2 Open Downloads",
            font=("Segoe UI", 11),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], height=36,
            command=lambda: subprocess.Popen(
                ["explorer", str(Path.home() / "Downloads")]),
        ).pack(side="left", padx=4)

        self._bc_status = ctk.CTkLabel(
            bc_btn_row, text="",
            font=("Segoe UI", 11, "bold"),
            text_color="#8e44ad",
        )
        self._bc_status.pack(side="left", padx=SPACING["sm"])

        self._build_auxiliary_settings_cards(scroll)

    def _build_auxiliary_settings_cards(self, scroll: ctk.CTkScrollableFrame) -> None:
        """CDP browser tools, traffic controller, shortcuts, session log, fleet."""
        c = self._colors

        # ── CDP BROWSER AUTOMATION ────────────────────────────────
        cdp_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8,
                                 border_color=c["success"], border_width=2)
        cdp_card.pack(fill="x", pady=SPACING["sm"])

        ctk.CTkLabel(
            cdp_card, text="\U0001F310 CDP Browser Automation (Recommended)",
            font=("Segoe UI", 15, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")

        ctk.CTkLabel(
            cdp_card, text=(
                "CDP connects directly to Chrome's DOM — no blind clicking!\n"
                "It finds elements by CSS selector, types text directly, and reads\n"
                "responses from specific HTML elements. Much more reliable than pyautogui.\n\n"
                "Click 'Launch CDP Browser' to open a dedicated Autocoder Chrome.\n"
                "If needed, Autocoder will end Chrome/Edge and free ports 9222–9225 first so CDP can start.\n"
                "Log in to your AI sites ONCE — logins are remembered.\n"
                "Then Grab each AI tab in the session cards above."
            ),
            font=("Segoe UI", 11),
            text_color=c["fg_muted"],
            wraplength=650, justify="left",
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        cdp_btn_row = ctk.CTkFrame(cdp_card, fg_color="transparent")
        cdp_btn_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))

        ctk.CTkButton(
            cdp_btn_row, text="\U0001F310 Launch CDP Browser",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["success"], hover_color="#27ae60", height=38,
            command=self._on_launch_cdp_browser,
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            cdp_btn_row, text="Check CDP",
            font=("Segoe UI", 11),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], height=30,
            command=self._on_check_cdp,
        ).pack(side="left", padx=SPACING["sm"])

        self._cdp_status = ctk.CTkLabel(
            cdp_card, text="",
            font=("Segoe UI", 11), text_color=c["fg_muted"],
        )
        self._cdp_status.pack(padx=SPACING["lg"], pady=(0, SPACING["md"]), anchor="w")

        # ── TRAFFIC CONTROLLER ───────────────────────────────────
        traffic_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        traffic_card.pack(fill="x", pady=SPACING["sm"])

        ctk.CTkLabel(
            traffic_card, text="\U0001F6A6 Mouse Traffic Controller",
            font=("Segoe UI", 15, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")

        ctk.CTkLabel(
            traffic_card, text=(
                "The Traffic Controller makes AI sessions take turns with the mouse.\n"
                "Start it before running multiple sessions. It also detects when YOU\n"
                "use the mouse and pauses bots until you're done (10s idle or top-right corner)."
            ),
            font=("Segoe UI", 11),
            text_color=c["fg_muted"],
            wraplength=650, justify="left",
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        traffic_btn_row = ctk.CTkFrame(traffic_card, fg_color="transparent")
        traffic_btn_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))

        ctk.CTkButton(
            traffic_btn_row, text="\U0001F6A6 Start Traffic Controller",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["success"], height=38,
            command=self._on_start_traffic,
        ).pack(side="left")

        ctk.CTkButton(
            traffic_btn_row, text="Check Status",
            font=("Segoe UI", 11),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], height=30,
            command=self._on_check_traffic,
        ).pack(side="left", padx=SPACING["sm"])

        self._traffic_status = ctk.CTkLabel(
            traffic_card, text="",
            font=("Segoe UI", 11), text_color=c["fg_muted"],
        )
        self._traffic_status.pack(padx=SPACING["lg"], pady=(0, SPACING["md"]), anchor="w")

        # ── KEYBOARD SHORTCUTS ───────────────────────────────────
        kb_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        kb_card.pack(fill="x", pady=SPACING["sm"])

        ctk.CTkLabel(
            kb_card, text="\u2328 Keyboard Shortcuts",
            font=("Segoe UI", 15, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")

        ctk.CTkLabel(
            kb_card, text=(
                "  Ctrl + 1    Explore Mode\n"
                "  Ctrl + 2    Task Queue\n"
                "  Ctrl + 3    Settings / Sessions\n"
                "  Ctrl + K    KILL all sessions\n"
                "  Ctrl + R    Resume after kill\n"
                "  Esc         Stop current session\n"
                "  F10         Kill headless endless runner (global)\n"
                "  Ctrl + Q    Quit"
            ),
            font=("Consolas", 12),
            text_color=c["fg_secondary"],
            justify="left", anchor="w",
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["md"]), anchor="w")

        # [ported fix #26] Session History + Activity Log card — live tail
        from pathlib import Path as _P_hist
        hist_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        hist_card.pack(fill="x", pady=SPACING["sm"])
        ctk.CTkLabel(
            hist_card, text="\U0001F4D2 Endless Session History + Live Log",
            font=("Segoe UI", 15, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")
        self._hist_text = ctk.CTkTextbox(
            hist_card, height=180, font=("Consolas", 10),
            fg_color=c["bg_input"], text_color=c["fg_secondary"],
            border_color=c["border"], border_width=1, corner_radius=6,
        )
        self._hist_text.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))

        def _refresh_hist():
            buf = []
            hist_file = _P_hist.home() / ".autocoder" / "sessions.jsonl"
            if hist_file.exists():
                try:
                    lns = hist_file.read_text(encoding="utf-8").splitlines()
                    buf.append(f"=== SESSIONS ({len(lns)} events, last 15) ===")
                    for ln in lns[-15:]:
                        buf.append(ln[:180])
                except Exception as e:
                    buf.append(f"(history read failed: {e})")
            log_file = _P_hist.home() / ".autocoder" / "endless.log"
            if log_file.exists():
                try:
                    lgl = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
                    buf.append("")
                    buf.append("=== LIVE LOG (last 20 lines of endless.log) ===")
                    buf.extend(l[:180] for l in lgl)
                except Exception as e:
                    buf.append(f"(log read failed: {e})")
            try:
                self._hist_text.delete("1.0", "end")
                self._hist_text.insert(
                    "1.0",
                    "\n".join(buf) or "(no history yet — click a Build-mode button above)",
                )
            except Exception:
                pass
            try:
                self.after(5000, _refresh_hist)
            except Exception:
                pass
        self.after(500, _refresh_hist)

        hist_btn_row = ctk.CTkFrame(hist_card, fg_color="transparent")
        hist_btn_row.pack(fill="x", padx=SPACING["lg"], pady=(0, SPACING["md"]))
        ctk.CTkButton(
            hist_btn_row, text="Refresh", width=80, height=26,
            font=("Segoe UI", 10),
            fg_color=c["bg_tertiary"], hover_color=c["bg_hover"],
            text_color=c["fg_secondary"],
            command=_refresh_hist,
        ).pack(side="left")
        ctk.CTkButton(
            hist_btn_row, text="Open sessions.jsonl", width=160, height=26,
            font=("Segoe UI", 10),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"],
            command=lambda: subprocess.Popen(
                ["explorer", str(Path.home() / ".autocoder" / "sessions.jsonl")]),
        ).pack(side="left", padx=6)

        if self._enable_fleet:
            self._build_fleet_card(scroll)

    # ── Override base-class setup that assumes a single executor ────

    def _setup_callbacks(self) -> None:
        """No-op: session cards wire their own callbacks via _wire_callbacks()."""
        self._task_queue.on_change(lambda: self.after(0, self._refresh_task_list))

    # ── Activate a session (set it as current for base-class refs) ──

    def _activate_session(self, session: Session) -> None:
        """Make a session the 'active' one for task queue, output, etc."""
        self._active_session_id = session.session_id
        self._task_queue = session.task_queue
        self._task_executor = session.executor
        if session.client:
            self._gemini = session.client
            if not self._expander:
                self._expander = ExpansionEngine(
                    self._gemini, depth_limit=self._cfg.expand_depth_limit
                )

    # ── Session callbacks ─────────────────────────────────────────

    def _on_session_output(self, session_id: str, kind: str, text: str) -> None:
        # Always store the latest text for display; "result" type is the actual AI output
        self._session_outputs[session_id] = text
        if session_id == self._active_session_id:
            def update():
                self._task_output.delete("1.0", "end")
                self._task_output.insert("1.0", text)
                if self._cfg.auto_scroll:
                    self._task_output.see("end")
            self.after(0, update)

    def _on_session_task_start(self, session_id: str, task: CodingTask) -> None:
        session = self.session_mgr.get_session(session_id)
        name = session.ai_profile.name if session else "?"
        self.after(0, lambda: self._status_bar.set_status(
            f"{name}: Working on {task.title}", "info"))

    def _on_session_task_complete(self, session_id: str, task: CodingTask) -> None:
        session = self.session_mgr.get_session(session_id)
        name = session.ai_profile.name if session else "?"
        corner = session.corner if session else ""

        level = "success" if task.status == TaskStatus.COMPLETED else "error"
        msg = f"{name}: {'Completed' if task.status == TaskStatus.COMPLETED else 'Failed'}: {task.title}"
        self.after(0, lambda: self._toast(msg, level))
        self.after(0, lambda: self._status_bar.set_status(msg, level))
        self.after(0, self._refresh_task_list)

        if task.status == TaskStatus.COMPLETED and task.output_code:
            self._last_completed_task = task

        # Auto-save to Downloads
        if task.status == TaskStatus.COMPLETED and task.output_code:
            try:
                path = save_task_output(
                    title=task.title,
                    output=task.output_code,
                    ai_name=name,
                    corner=corner,
                    elapsed_seconds=task.elapsed_seconds,
                    iterations=task.iterations_completed,
                )
                if path:
                    self.after(0, lambda p=path: self._toast(
                        f"Saved to {p.name}", "info"))
            except Exception as e:
                logger.error("Auto-save failed: %s", e)

        # Save to history
        from gemini_coder.history import HistoryEntry
        self._history.add(HistoryEntry(
            entry_type="task",
            title=f"[{name}] {task.title}",
            prompt=task.description,
            response=task.output_code,
            model=name,
            elapsed_seconds=task.elapsed_seconds,
            status=task.status.value,
        ))

    def _on_session_tick(self, session_id: str, task: CodingTask) -> None:
        if session_id == self._active_session_id:
            from datetime import timedelta
            def update():
                elapsed = timedelta(seconds=int(task.elapsed_seconds))
                budget = timedelta(seconds=int(task.time_budget_seconds))
                remaining = max(0, task.time_budget_seconds - task.elapsed_seconds)
                self._task_progress_bar.set(task.progress_fraction)
                self._task_progress_label.configure(
                    text=f"{elapsed} / {budget} ({int(remaining)}s left)")
            self.after(0, update)

    def _on_session_status(self, session_id: str, status: str, detail: str) -> None:
        session = self.session_mgr.get_session(session_id)
        name = session.ai_profile.name if session else "?"
        corner = session.corner if session else ""
        display = f"{name}: {detail}" if detail else f"{name}: {status}"

        # Update the session card's task text
        if corner and corner in self._session_cards:
            card = self._session_cards[corner]
            short = detail[:25] if detail else status
            card._task_text = f"{short}"
            self.after(0, lambda c=card, s=short: c._status_label.configure(text=s))

        c = self._colors
        color_map = {
            "working": c["success"],
            "thinking": c["warning"],
            "improving": "#8e44ad",
            "idle": c["fg_muted"],
            "error": c["error"],
        }
        color = color_map.get(status, c["fg_muted"])

        def update():
            if hasattr(self, "_ai_status_dot"):
                self._ai_status_dot.configure(text_color=color)
            if hasattr(self, "_ai_status_label"):
                self._ai_status_label.configure(text=display, text_color=color)
        self.after(0, update)

    # ── Task assignment ───────────────────────────────────────────

    def _on_assign_change(self, choice: str) -> None:
        """Change which session receives new tasks."""
        for session in self.session_mgr.sessions:
            label = f"{CORNER_LABELS[session.corner]} ({session.ai_profile.name})"
            if label == choice:
                self._active_session_id = session.session_id
                self._task_queue = session.task_queue
                self._task_executor = session.executor
                if session.client:
                    self._gemini = session.client
                self._refresh_task_list()
                self._toast(f"Tasks now go to {label}", "info")
                return

    def _refresh_all_window_lists(self) -> None:
        """Refresh window picker dropdowns on all session cards."""
        for card in self._session_cards.values():
            card._refresh_window_list()

    def _update_assign_selector(self) -> None:
        """Refresh the session assignment dropdown."""
        values = []
        for session in self.session_mgr.sessions:
            label = f"{CORNER_LABELS[session.corner]} ({session.ai_profile.name})"
            values.append(label)
        if values:
            self._assign_selector.configure(values=values)

    # ── Broadcast mode ─────────────────────────────────────────────

    def _on_attach_files(self) -> None:
        """Open file dialog to attach reference files."""
        from tkinter import filedialog
        filetypes = [
            ("Code files", "*.py *.js *.ts *.c *.h *.cpp *.hpp *.rs *.go *.java"),
            ("Config files", "*.json *.yaml *.yml *.toml *.ini *.cfg *.env"),
            ("Text/Docs", "*.txt *.md *.rst *.csv"),
            ("Web files", "*.html *.css *.jsx *.tsx *.vue *.svelte"),
            ("All files", "*.*"),
        ]
        paths = filedialog.askopenfilenames(
            title="Attach reference files",
            filetypes=filetypes,
        )
        if not paths:
            return

        for p in paths:
            if p not in self._bc_attached_files:
                self._bc_attached_files.append(p)

        self._refresh_file_list()

    def _on_clear_files(self) -> None:
        """Clear all attached files."""
        self._bc_attached_files.clear()
        self._refresh_file_list()

    def _refresh_file_list(self) -> None:
        """Update the attached file display."""
        # Clear old labels
        for lbl in self._bc_file_labels:
            lbl.destroy()
        self._bc_file_labels.clear()

        count = len(self._bc_attached_files)
        if count == 0:
            self._bc_file_label.configure(text="No files attached")
            self._bc_file_list_frame.pack_forget()
            return

        self._bc_file_label.configure(text=f"{count} file{'s' if count != 1 else ''} attached")
        self._bc_file_list_frame.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))

        c = self._colors
        for fp in self._bc_attached_files:
            name = Path(fp).name
            size_kb = Path(fp).stat().st_size / 1024 if Path(fp).exists() else 0
            lbl = ctk.CTkLabel(
                self._bc_file_list_frame,
                text=f"  📄 {name}  ({size_kb:.0f} KB)",
                font=("Consolas", 10),
                text_color=c["fg_secondary"],
                anchor="w",
            )
            lbl.pack(fill="x", padx=8, pady=1, anchor="w")
            self._bc_file_labels.append(lbl)

    def _current_coding_profile_id(self) -> str:
        try:
            return cprof.id_from_label(self._profile_combo.get().strip())
        except Exception:
            return "balanced"

    def _refresh_task_panel(self) -> None:
        """Show last / current task from task_display.json + file fallbacks."""
        if not hasattr(self, "_task_title_lbl"):
            return
        t1, t2, body = banner_lines()
        if not (body and body.strip()) or body == "(no task text saved yet)":
            body = load_last_task_from_files() or body
        self._task_title_lbl.configure(text=t1)
        self._task_sub_lbl.configure(text=t2)
        self._task_body_lbl.configure(text=body)
        if self._broadcast.is_running and hasattr(self, "_bc_status"):
            st = self._bc_status.cget("text")
            if st:
                self._task_sub_lbl.configure(text=f"{t2}  |  {st}")

    def _restore_prompt_if_placeholder(self) -> None:
        """If the task box is still empty or the default placeholder, restore last task."""
        _PH = "e.g., Build a calculator with history and themes"
        try:
            t = self._bc_task_input.get("1.0", "end").strip()
            if t and t != _PH:
                self._refresh_task_panel()
                return
            last = load_last_task_from_files()
            if last and not last.strip().lower().startswith("e.g.,"):
                self._bc_task_input.delete("1.0", "end")
                self._bc_task_input.insert("1.0", last.strip())
        except Exception:
            pass
        self._refresh_task_panel()

    def _maybe_startup_summary(self) -> None:
        ps = cprof.load_profile_settings()
        if not ps.get("show_startup_summary", True):
            return
        t = (load_last_task_from_files() or "").strip()
        if len(t) < 8:
            return
        sample = t[:120] + ("…" if len(t) > 120 else "")
        self._toast(f"Last task (also in Task panel; editor restored if it was empty):\n{sample}", "info")

    def _on_coding_profile_changed(self, choice: str) -> None:
        pid = cprof.id_from_label(choice)
        s = cprof.load_profile_settings()
        s["active_preset"] = pid
        cprof.save_profile_settings(s)
        # Choosing from list alone does not overwrite checkboxes — user hits Apply
        if pid != "custom":
            meta = cprof.PRESETS.get(pid, {})
            self._status_bar.set_status(
                f"Profile “{meta.get('label', pid)}” — click Apply profile to set options.",
                "info",
            )

    def _apply_active_profile_to_ui(self) -> None:
        pid = self._current_coding_profile_id()
        b = cprof.effective_bundle(pid)
        if b.get("build_target"):
            self._bc_build_target.set(b["build_target"])
        ex = b.get("expand_on_stagnation")
        if ex is not None:
            self._bc_expand_on_stagnation.set(bool(ex))
        pl = b.get("perfection_loop")
        if pl is not None:
            self._bc_perfection_loop.set(bool(pl))
        foc = b.get("focuses")
        if foc is not None:
            fset = set(foc)
            for k, var in self._bc_focus_vars.items():
                var.set(k in fset)
        if "acceptance_commands" in b:
            ac = b["acceptance_commands"]
            if isinstance(ac, list):
                self._bc_acceptance_cmds.set("; ".join(str(x).strip() for x in ac if str(x).strip()))
            else:
                self._bc_acceptance_cmds.set(str(ac or ""))
        if "strict_acceptance" in b and b["strict_acceptance"] is not None:
            self._bc_strict_acceptance.set(bool(b["strict_acceptance"]))
        if "promote_only_passing" in b and b["promote_only_passing"] is not None:
            self._bc_promote_passing_only.set(bool(b["promote_only_passing"]))
        s = cprof.load_profile_settings()
        s["active_preset"] = pid
        cprof.save_profile_settings(s)
        self._status_bar.set_status(f"Applied profile: {cprof.PRESETS[pid]['label']}", "success")
        self._refresh_task_panel()

    def _on_auto_select_from_prompt(self) -> None:
        """Auto-pick a friendly run setup based on prompt keywords."""
        text = (self._bc_task_input.get("1.0", "end") or "").strip()
        if not text or text.lower().startswith("e.g.,"):
            self._toast("Add your task prompt first, then click Auto Select Setup.", "warning")
            return

        t = text.lower()
        words = set(t.replace("/", " ").replace("-", " ").split())

        def has_any(*keys: str) -> bool:
            return any(k in t for k in keys)

        # Build target
        if has_any("android", "mobile app", "apk", "play store"):
            self._bc_build_target.set("Android App")
        elif has_any("raspberry pi", "raspi", "iot", "gpio"):
            self._bc_build_target.set("Raspberry Pi App")
        elif has_any("website", "web app", "landing page", "next.js", "react", "html", "css", "frontend"):
            self._bc_build_target.set("Website / Web App")
        elif has_any("game", "pygame", "unity", "godot"):
            self._bc_build_target.set("Game")
        elif has_any("cross-platform", "cross platform", "desktop + pi", "pc + pi"):
            self._bc_build_target.set("Cross-Platform (PC + Pi)")
        else:
            self._bc_build_target.set("PC Desktop App")

        # Scope mode
        component_words = (
            "add", "feature", "integrate", "plugin", "extend", "adjacent",
            "incremental", "fix", "bug", "patch", "refactor", "improve",
        )
        is_component = any(w in words for w in component_words) or has_any(
            "add ", "build on top", "based on reference", "from reference"
        )
        self._bc_run_scope.set("Single component / feature" if is_component else "Full system")

        # Template mode
        if has_any("boilerplate", "scaffold", "starter", "mvp", "prototype", "quick"):
            self._bc_scaffold_mode.set("Template-heavy")
        elif has_any("production", "harden", "stability", "existing codebase", "strict"):
            self._bc_scaffold_mode.set("Selective")
        else:
            self._bc_scaffold_mode.set("Selective")

        # Reference-adjacent mode defaults on when files attached or prompt asks for it
        ref_adjacent = bool(self._bc_attached_files) or has_any(
            "reference", "based on", "adjacent", "existing code", "this file", "this repo"
        )
        self._bc_reference_adjacent_mode.set(ref_adjacent)

        outside_box = has_any(
            "outside the box", "outside-the-box", "never considered", "surprise me",
            "wild ideas", "unconventional", "hidden capability", "non-obvious",
            "innovative", "creative"
        )
        self._bc_outside_box_mode.set(outside_box)
        if outside_box:
            self._bc_outside_box_freq.set("10")

        # Auto focuses by domain
        for var in self._bc_focus_vars.values():
            var.set(False)

        focus_candidates: list[str]
        if has_any("api", "backend", "server", "database", "auth", "worker", "queue"):
            focus_candidates = [
                "solid_functional", "error_recovery", "api_design",
                "database_persistence", "network_resilience",
                "configuration", "logging_observability", "review_grade",
            ]
        elif has_any("ui", "ux", "frontend", "screen", "layout", "design", "component"):
            focus_candidates = [
                "solid_functional", "beautiful_gui", "accessibility",
                "configuration", "review_grade",
            ]
        else:
            focus_candidates = [
                "solid_functional", "error_recovery",
                "configuration", "review_grade",
            ]

        selected_count = 0
        for key in focus_candidates:
            var = self._bc_focus_vars.get(key)
            if var is not None:
                var.set(True)
                selected_count += 1

        # Acceptance gate commands by likely stack
        if has_any("react", "next.js", "node", "typescript", "javascript", "npm", "pnpm", "vite"):
            self._bc_acceptance_cmds.set("npm test; npm run build; npm run lint; npm run smoke")
            self._bc_strict_acceptance.set(True)
        elif has_any("python", "flask", "fastapi", "django", "pytest"):
            self._bc_acceptance_cmds.set("python -m pytest")
            self._bc_strict_acceptance.set(True)
        else:
            # Unknown stack: don't hard-block non-coders with wrong commands
            self._bc_acceptance_cmds.set("")
            self._bc_strict_acceptance.set(False)

        self._status_bar.set_status(
            f"Auto-selected setup from prompt ({selected_count} focuses enabled).", "success"
        )
        self._toast("Auto setup applied. Review options and click Start.", "success")

    def _on_toggle_task_panel(self) -> None:
        show = self._var_show_task_panel.get()
        s = cprof.load_profile_settings()
        s["show_task_panel"] = show
        cprof.save_profile_settings(s)
        if show:
            self._task_panel.pack(
                fill="x", padx=SPACING["lg"], pady=(0, 4), before=self._bc_task_input
            )
        else:
            self._task_panel.pack_forget()

    def _on_toggle_startup_tip(self) -> None:
        s = cprof.load_profile_settings()
        s["show_startup_summary"] = self._var_show_startup.get()
        cprof.save_profile_settings(s)

    def _on_toggle_windows_startup(self) -> None:
        s = cprof.load_profile_settings()
        s["autostart_windows_login"] = bool(self._var_autostart_windows.get())
        s["autoresume_endless_on_login"] = bool(self._var_autoresume_endless.get())
        cprof.save_profile_settings(s)
        self._apply_windows_startup()

    def _apply_windows_startup(self) -> None:
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            self._toast("Could not resolve APPDATA for startup setup", "error")
            return
        startup_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        startup_dir.mkdir(parents=True, exist_ok=True)
        startup_cmd = startup_dir / "AutocoderStartup.cmd"
        runner = Path(__file__).resolve().parent.parent / "run_endless.py"
        if self._var_autostart_windows.get():
            lines = ["@echo off", "start \"\" pythonw -m Autocoder"]
            if self._var_autoresume_endless.get():
                endless_cmd = f"start \"\" pythonw \"{runner}\" --mode=full"
                if hasattr(self, "_bc_model_rotation") and bool(self._bc_model_rotation.get()):
                    rot_min = max(1, self._bc_parse_nonneg_int(self._bc_model_rotation_minutes.get(), 12))
                    rot_rand = 1 if bool(self._bc_model_rotation_random.get()) else 0
                    endless_cmd += (
                        f" --model-rotation=1"
                        f" --model-rotation-min={rot_min}"
                        f" --model-rotation-random={rot_rand}"
                    )
                lines.append(endless_cmd)
            startup_cmd.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._toast("Windows startup enabled for Autocoder", "success")
        else:
            if startup_cmd.exists():
                try:
                    startup_cmd.unlink()
                except Exception:
                    pass
            self._toast("Windows startup disabled", "info")

    def _open_profile_editor(self) -> None:
        """Toplevel: edit focuses + build target for the active profile preset."""
        pid = self._current_coding_profile_id()
        c = self._colors
        win = ctk.CTkToplevel(self)
        win.title(f"Edit plan: {cprof.PRESETS[pid]['label']}")
        win.geometry("520x480")
        win.transient(self)
        win.lift()

        ctk.CTkLabel(
            win, text=f"Override “{cprof.PRESETS[pid]['label']}”\n"
            "Checked items are used when you click Apply profile.",
            font=("Segoe UI", 12), text_color=c["fg_heading"],
        ).pack(anchor="w", padx=12, pady=8)

        bundle = cprof.effective_bundle(pid)
        bt_row = ctk.CTkFrame(win, fg_color="transparent")
        bt_row.pack(fill="x", padx=8, pady=4)
        ctk.CTkLabel(bt_row, text="Build target:").pack(side="left")
        _btv = self._bc_build_target.cget("values")
        bt = ctk.CTkComboBox(
            bt_row, values=_btv, width=200,
        )
        bt.set(bundle.get("build_target") or "PC Desktop App")
        bt.pack(side="left", padx=8)

        exp_var = ctk.BooleanVar(value=bool(bundle.get("expand_on_stagnation", False)))
        ctk.CTkSwitch(
            win, text="Expand on stagnation", variable=exp_var,
        ).pack(anchor="w", padx=12, pady=2)
        pl_var = ctk.BooleanVar(value=bool(bundle.get("perfection_loop", False)))
        ctk.CTkSwitch(
            win, text="Perfection loop", variable=pl_var,
        ).pack(anchor="w", padx=12, pady=2)

        scroll = ctk.CTkScrollableFrame(win, height=220)
        scroll.pack(fill="both", expand=True, padx=8, pady=8)
        edit_vars: dict[str, ctk.BooleanVar] = {}
        fsel = set(bundle.get("focuses") or [])
        for key in FOCUS_ORDER:
            info = IMPROVEMENT_FOCUSES.get(key) or {"label": key}
            v = ctk.BooleanVar(value=key in fsel)
            edit_vars[key] = v
            ctk.CTkCheckBox(
                scroll, text=info.get("label", key), variable=v,
            ).pack(anchor="w", pady=1)

        def _save() -> None:
            focuses = [k for k, vv in edit_vars.items() if vv.get()]
            s = cprof.load_profile_settings()
            if "overrides" not in s or not isinstance(s["overrides"], dict):
                s["overrides"] = {}
            s["overrides"][pid] = {
                "build_target": bt.get(),
                "focuses": focuses,
                "expand_on_stagnation": exp_var.get(),
                "perfection_loop": pl_var.get(),
            }
            cprof.save_profile_settings(s)
            try:
                self._toast(f"Saved overrides for {cprof.PRESETS[pid]['label']}", "success")
            except Exception:
                pass
            win.destroy()
            self._apply_active_profile_to_ui()

        ctk.CTkButton(
            win, text="Save & apply", command=_save,
            fg_color="#8e44ad", hover_color="#9b59b6",
        ).pack(pady=8)
        ctk.CTkButton(
            win, text="Cancel", command=win.destroy, fg_color="gray",
        ).pack()

    def _on_broadcast_complete(self, counts: dict) -> None:
        it = 0
        for v in counts.values():
            try:
                it = max(it, int(v))
            except Exception:
                pass
        _td_set_idle(keep_task=True, last_iteration=it)
        self.after(0, self._refresh_task_panel)

        cfg = getattr(self._broadcast, "_config", None)
        if not cfg or not getattr(cfg, "auto_advance_goal_queue", False):
            return
        try:
            from ..project_maintainer import queue_advance
        except ImportError:  # pragma: no cover
            try:
                from project_maintainer import queue_advance
            except ImportError:
                return
        nxt = queue_advance()
        if isinstance(nxt, dict) and (nxt.get("task") or "").strip():
            self.after(150, lambda g=dict(nxt): self._apply_next_queued_goal(g))

    def _apply_next_queued_goal(self, goal: dict) -> None:
        """Start the next goal from the queue (task + optional schedule fields)."""
        task = (goal.get("task") or "").strip()
        if not task:
            return
        self._bc_task_input.delete("1.0", "end")
        self._bc_task_input.insert("1.0", task)
        if "time_limit_minutes" in goal:
            self._bc_time_limit.set(str(int(goal.get("time_limit_minutes", 0) or 0)))
        if "build_before_review_minutes" in goal:
            self._bc_build_before_review.set(
                str(int(goal.get("build_before_review_minutes", 0) or 0))
            )
        if "review_phase_minutes" in goal:
            self._bc_review_phase.set(str(int(goal.get("review_phase_minutes", 0) or 0)))
        if goal.get("project_slug") is not None:
            self._bc_project_slug.set(str(goal.get("project_slug", "") or ""))
        try:
            self._toast("Starting next queued goal…", "info")
        except Exception:
            pass
        self._on_broadcast_start()

    def _on_mode_launch(self, mode: str) -> None:
        """[ported fix #27] Launch run_endless.py --mode=X detached so the
        GUI stays responsive and the worker survives window close."""
        import subprocess
        from pathlib import Path as _P
        here = _P(__file__).resolve().parent.parent  # Autocoder/
        runner = here / "run_endless.py"
        if not runner.exists():
            try: self._toast(f"run_endless.py not found at {runner}", "error")
            except Exception: pass
            return
        # Flush the current typed prompt NOW so the worker sees it
        try:
            text = self._bc_task_input.get("1.0", "end").rstrip("\n")
            (_P.home() / ".autocoder").mkdir(parents=True, exist_ok=True)
            (_P.home() / ".autocoder" / "last_prompt.txt").write_text(text, encoding="utf-8")
        except Exception:
            pass
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            args = ["pythonw", str(runner), f"--mode={mode}"]
            if bool(self._bc_model_rotation.get()):
                args.extend([
                    "--model-rotation=1",
                    f"--model-rotation-min={max(1, self._bc_parse_nonneg_int(self._bc_model_rotation_minutes.get(), 12))}",
                    f"--model-rotation-random={1 if bool(self._bc_model_rotation_random.get()) else 0}",
                ])
            p = subprocess.Popen(
                args,
                creationflags=flags, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(here),
            )
            try: self._toast(f"Launched {mode} worker (PID {p.pid})", "success")
            except Exception: pass
        except Exception as e:
            try: self._toast(f"Launch failed: {e}", "error")
            except Exception: pass

    @staticmethod
    def _bc_parse_nonneg_int(raw: str, default: int = 0) -> int:
        try:
            v = int(str(raw).strip())
            return max(0, v)
        except Exception:
            return default

    def _openclaw_reference_pack_dir(self) -> Path:
        """Bundled OpenClaw + machine playbooks (markdown) shipped with Autocoder."""
        return Path(__file__).resolve().parent.parent / "docs" / "openclaw_reference_pack"

    def _bootstrap_openclaw_reference_pack(self) -> None:
        """Attach the reference pack, apply sovereign preset, enable perfection/expand/oob.

        Controlled by ``openclaw_reference_bootstrap`` in ``~/.autocoder/coding_profile_settings.json``.
        Runs at most once per process (``_openclaw_ref_bootstrap_done``).
        """
        if self._openclaw_ref_bootstrap_done:
            return
        try:
            ps = cprof.load_profile_settings()
            if not ps.get("openclaw_reference_bootstrap", True):
                return
            d = self._openclaw_reference_pack_dir()
            if not d.is_dir():
                return
            md_files = sorted(d.glob("*.md"))
            if not md_files:
                return
            master = d / "MASTER_TASK_ONE_SHOT.md"
            for p in md_files:
                if p.name.upper() == "MASTER_TASK_ONE_SHOT.MD":
                    continue
                sp = str(p.resolve())
                if sp not in self._bc_attached_files:
                    self._bc_attached_files.append(sp)
            self._refresh_file_list()

            if "openclaw_sovereign" in cprof.PRESETS:
                self._profile_combo.set(cprof.PRESETS["openclaw_sovereign"]["label"])
                s = cprof.load_profile_settings()
                s["active_preset"] = "openclaw_sovereign"
                cprof.save_profile_settings(s)
                self._apply_active_profile_to_ui()

            self._bc_perfection_loop.set(True)
            self._bc_expand_on_stagnation.set(True)
            self._bc_outside_box_mode.set(True)
            self._bc_reference_adjacent_mode.set(True)
            ex = self._bc_focus_vars.get("explore_expand")
            if ex is not None:
                ex.set(True)
            if ps.get("openclaw_frontier_on_bootstrap", True):
                self._bc_frontier_overdrive.set(True)
                self._bc_frontier_lens.set(
                    FRONTIER_LENS_LABELS.get(
                        DEFAULT_FRONTIER_LENS,
                        next(iter(FRONTIER_LENS_LABELS.values())),
                    )
                )

            ph = "e.g., Build a calculator with history and themes"
            try:
                cur = (self._bc_task_input.get("1.0", "end") or "").strip()
                if master.is_file():
                    body = master.read_text(encoding="utf-8").strip()
                    if body and (not cur or cur == ph or cur.lower().startswith("e.g.,")):
                        self._bc_task_input.delete("1.0", "end")
                        self._bc_task_input.insert("1.0", body)
            except Exception:
                pass
            self._refresh_task_panel()
            try:
                self._toast(
                    "OpenClaw reference pack attached; profile “OpenClaw / sovereign stack” applied. "
                    "Perfection Loop, Expand on stagnation, Outside-the-box, Explore & Expand, "
                    "and Apex Frontier overdrive (if enabled in settings) are on.",
                    "info",
                )
            except Exception:
                pass
        finally:
            self._openclaw_ref_bootstrap_done = True

    def _maybe_auto_resume_broadcast(self) -> None:
        """After crash/reboot: resume from ~/.autocoder/broadcast_state.json when a session is ready."""
        state_path = Path.home() / ".autocoder" / "broadcast_state.json"
        try:
            if not state_path.exists() or state_path.stat().st_size < 20:
                return
        except OSError:
            return
        if self._broadcast.is_running:
            return

        configured = [s for s in self.session_mgr.sessions if s and s.is_configured]
        if not configured:
            self._auto_resume_attempts += 1
            if self._auto_resume_attempts < 45:
                self.after(3000, self._maybe_auto_resume_broadcast)
            elif self._auto_resume_attempts == 45:
                try:
                    self._toast(
                        "Saved broadcast state found — connect a session (Launch CDP / Grab) "
                        "to auto-resume, or click Start Autocoding.",
                        "warning",
                    )
                except Exception:
                    pass
            return

        self._auto_resume_attempts = 0
        try:
            ok = self._broadcast.resume()
        except Exception as exc:
            logger.warning("auto-resume broadcast failed: %s", exc)
            return
        if not ok:
            logger.info("auto-resume: resume() returned False")
            return

        session = configured[0]
        cfg = getattr(self._broadcast, "_config", None)
        task = ""
        try:
            if cfg and getattr(cfg, "task", None):
                task = str(cfg.task)
                self._bc_task_input.delete("1.0", "end")
                self._bc_task_input.insert("1.0", task)
        except Exception:
            pass

        _td_set_running(
            task.strip() if task else "(resumed)",
            build_target=(cfg.build_target if cfg else "PC Desktop App") or "PC Desktop App",
            ai_name=session.ai_profile.name,
            iteration=0,
            focus_label="resumed",
        )
        self._refresh_task_panel()
        self._bc_start_btn.configure(state="disabled")
        self._bc_stop_btn.configure(state="normal")
        self._bc_graceful_btn.configure(
            state="normal", text="\U0001F3C1 End Gracefully",
        )
        try:
            self._toast(
                f"Resumed broadcast where it left off ({session.ai_profile.name}, {session.corner}).",
                "success",
            )
        except Exception:
            pass
        logger.info("Auto-resumed broadcast from %s", state_path)

    def _on_broadcast_start(self) -> None:
        task = self._bc_task_input.get("1.0", "end").strip()
        logger.info("Autocode start clicked. Task: %r, running=%s",
                     task[:60] if task else "(empty)", self._broadcast.is_running)

        if not task or task.startswith("e.g.,"):
            self._toast("Enter a task description", "warning")
            return

        # If broadcast thinks it's still running from a previous attempt, reset it
        if self._broadcast.is_running:
            logger.warning("Autocode was stuck in running state — resetting")
            self._broadcast._running = False
            self._broadcast._stop_event.set()

        active = [s for s in self.session_mgr.sessions if s.is_configured]
        logger.info("Active configured sessions: %d (total sessions: %d)",
                     len(active), len(self.session_mgr.sessions))
        if not active:
            self._toast("No configured session. Launch or capture an AI window first.", "error")
            return

        # Use first configured session only
        session = active[0]

        # Collect selected improvement focuses
        selected_focuses = [
            key for key, var in self._bc_focus_vars.items() if var.get()
        ]

        tlim = self._bc_parse_nonneg_int(self._bc_time_limit.get(), 0)
        rphase = self._bc_parse_nonneg_int(self._bc_review_phase.get(), 0)
        bfr = self._bc_parse_nonneg_int(self._bc_build_before_review.get(), 0)
        run_scope_mode = (
            "component"
            if self._bc_run_scope.get() == "Single component / feature"
            else "full_system"
        )
        scaffold_map = {
            "None": "none",
            "Selective": "selective",
            "Template-heavy": "always",
        }
        scaffold_mode = scaffold_map.get(self._bc_scaffold_mode.get(), "none")
        acceptance_commands = [
            cmd.strip()
            for cmd in (self._bc_acceptance_cmds.get() or "").split(";")
            if cmd.strip()
        ]
        frontier_lens_key = frontier_lens_key_from_label(self._bc_frontier_lens.get())

        config = BroadcastConfig(
            task=task,
            build_target=self._bc_build_target.get(),
            endless=(tlim == 0),
            time_limit_minutes=tlim,
            expand_on_stagnation=self._bc_expand_on_stagnation.get(),
            selected_focuses=selected_focuses,
            perfection_loop=self._bc_perfection_loop.get(),
            attached_files=list(self._bc_attached_files),
            session_ids=[session.session_id],
            build_before_review_minutes=bfr,
            review_phase_minutes=rphase,
            project_slug=(self._bc_project_slug.get() or "").strip(),
            master_log_enabled=bool(self._bc_master_log.get()),
            auto_advance_goal_queue=bool(self._bc_auto_advance.get()),
            run_scope_mode=run_scope_mode,
            scaffold_mode=scaffold_mode,
            reference_adjacent_mode=bool(self._bc_reference_adjacent_mode.get()),
            outside_box_mode=bool(self._bc_outside_box_mode.get()),
            outside_box_frequency_minutes=max(1, self._bc_parse_nonneg_int(self._bc_outside_box_freq.get(), 10)),
            model_rotation_enabled=bool(self._bc_model_rotation.get()),
            model_rotation_interval_minutes=max(1, self._bc_parse_nonneg_int(self._bc_model_rotation_minutes.get(), 12)),
            model_rotation_random=bool(self._bc_model_rotation_random.get()),
            strict_acceptance=bool(self._bc_strict_acceptance.get()),
            acceptance_commands=acceptance_commands,
            promote_only_passing=bool(self._bc_promote_passing_only.get()),
            golden_branch=(self._bc_golden_branch.get() or "ai/golden").strip(),
            frontier_overdrive=bool(self._bc_frontier_overdrive.get()),
            frontier_lens=frontier_lens_key,
        )

        self._broadcast.start(config)
        _td_set_running(
            task,
            build_target=config.build_target,
            ai_name=session.ai_profile.name,
            iteration=0,
            focus_label="starting",
        )
        self._refresh_task_panel()
        self._bc_start_btn.configure(state="disabled")
        self._bc_stop_btn.configure(state="normal")
        # Reset graceful-end button to its default state for the new run
        self._bc_graceful_btn.configure(
            state="normal", text="\U0001F3C1 End Gracefully",
        )
        self._toast(
            f"Autocoding on {session.ai_profile.name} ({session.corner})", "success"
        )
        self._status_bar.set_status(f"Autocoding: {task[:40]}...", "info")

    def _on_broadcast_stop(self) -> None:
        it = 0
        for _sid, n in self._broadcast._iteration_counts.items():
            try:
                it = max(it, int(n))
            except Exception:
                pass
        if self._broadcast.is_running:
            self._broadcast.stop()
        self.session_mgr.stop_all()
        _td_set_idle(keep_task=True, last_iteration=it)
        self._refresh_task_panel()
        self._bc_start_btn.configure(state="normal")
        self._bc_stop_btn.configure(state="disabled")
        self._bc_graceful_btn.configure(state="disabled")
        self._bc_status.configure(text="Stopped")
        self._toast("Broadcast stopped", "info")

    # ── Build-target preset handlers ───────────────────────────────
    def _on_build_target_changed(self, choice: str) -> None:
        """Called when the user picks a new build target from the combo.
        Applies that target's focus preset so the checkboxes reflect
        what's most useful for that kind of project.
        """
        self._apply_build_target_preset(choice, toast=True)

    def _apply_build_target_preset(self, target: str, *, toast: bool = False) -> None:
        """Check the boxes for every focus in the preset, uncheck others.

        This is the 'smart default' moment: the user went from choosing
        among 46 focuses manually to getting a curated list for their
        specific kind of project. They can still tweak — presets are a
        starting point, not a lock.

        Hint label also updates to show the count so the user sees
        that SOMETHING happened when they changed the dropdown (silent
        checkbox churn on the right side would be easy to miss).
        """
        preset = set(focuses_for_target(target))
        changed = 0
        for key, var in self._bc_focus_vars.items():
            desired = key in preset
            if var.get() != desired:
                var.set(desired)
                changed += 1

        # Update the hint next to the dropdown so the user sees what happened
        self._bc_preset_hint.configure(
            text=f"→ {len(preset)} focuses auto-selected"
        )
        if toast:
            self._toast(
                f"Applied '{target}' preset — {len(preset)} focuses "
                f"selected ({changed} changed). Edit any as needed.",
                "info",
            )

    def _on_broadcast_graceful_end(self) -> None:
        """Request a clean wrap-up: finish current iteration, produce FINAL
        or HANDOFF output depending on how far along the project is, then
        stop. Unlike Stop, this doesn't cut work mid-flight."""
        if not self._broadcast.is_running:
            self._toast("No active broadcast to end", "info")
            return
        self._broadcast.request_graceful_end()
        # Lock the button so it can't be spammed; the broadcast takes
        # care of itself from here. Stop stays available as the escape hatch.
        self._bc_graceful_btn.configure(
            state="disabled",
            text="\u23F3 Wrapping up…",
        )
        self._bc_status.configure(text="Wrapping up — finishing last iteration")
        self._toast(
            "Ending gracefully — will produce a final or handoff output "
            "after current iteration.",
            "info",
        )

    def _on_broadcast_save(self) -> None:
        """Save all current broadcast outputs to Downloads."""
        saved = 0
        for session in self.session_mgr.sessions:
            sid = session.session_id
            # Use actual AI result, not progress text
            output = self._broadcast._results.get(sid, "")
            if not output or not output.strip():
                output = self._session_outputs.get(sid, "")
            if output and output.strip():
                try:
                    task_text = self._bc_task_input.get("1.0", "end").strip()
                    title = task_text[:60] if task_text and not task_text.startswith("e.g.,") else "broadcast"
                    path = save_task_output(
                        title=title,
                        output=output,
                        ai_name=session.ai_profile.name,
                        corner=session.corner,
                        elapsed_seconds=0,
                        iterations=self._broadcast._iteration_counts.get(sid, 0),
                    )
                    if path:
                        saved += 1
                except Exception as e:
                    logger.error("Save failed for %s: %s", session.display_name, e)
        if saved:
            dl = Path.home() / "Downloads"
            self._toast(f"Saved {saved} files to {dl}", "success")
        else:
            self._toast("No broadcast output to save yet", "warning")

    def _on_broadcast_iteration(self, session_id: str, iteration: int,
                                 focus: str, ai_name: str) -> None:
        # Update the session card with iteration info
        session = self.session_mgr.get_session(session_id)
        if session and session.corner in self._session_cards:
            card = self._session_cards[session.corner]
            card._task_text = f"#{iteration} {focus[:15]}"
            self.after(0, lambda c=card, t=f"#{iteration} {focus[:15]}":
                       c._status_label.configure(text=t))

        def update():
            self._bc_status.configure(
                text=f"{ai_name}: #{iteration} ({focus})"
            )
            self._status_bar.set_status(
                f"Broadcast: {ai_name} iteration #{iteration} - {focus}", "info"
            )
            try:
                task_text = self._bc_task_input.get("1.0", "end").strip()
                _td_set_iteration(
                    iteration, focus, task=task_text,
                )
            except Exception:
                _td_set_iteration(iteration, focus)
            self._refresh_task_panel()
        self.after(0, update)

        # Save to history so broadcast iterations are tracked
        try:
            from gemini_coder.history import HistoryEntry
            task_text = self._bc_task_input.get("1.0", "end").strip()[:80]
            output = self._broadcast._results.get(session_id, "")
            self._history.add(HistoryEntry(
                entry_type="broadcast",
                title=f"[{ai_name}] #{iteration} {focus} - {task_text}",
                prompt=task_text,
                response=output[:2000] if output else "",
                model=ai_name,
                elapsed_seconds=0,
                status="completed",
            ))
        except Exception as e:
            logger.warning("Failed to save broadcast history: %s", e)

    # ── Override start/stop to work with active session ───────────

    def _on_start_queue(self) -> None:
        if not self._active_session_id:
            self._toast("No session created yet. Set up a session in Settings first.", "error")
            self._show_view("settings")
            return
        session = self.session_mgr.get_session(self._active_session_id)
        if not session:
            self._toast("No active session selected", "error")
            return

        if not session.is_configured:
            self._toast("Launch the AI browser window first in Settings", "error")
            self._show_view("settings")
            return

        if not session.task_queue.pending_tasks:
            self._toast("No pending tasks in queue", "warning")
            return

        self.session_mgr.start_session(self._active_session_id)
        self._start_queue_btn.configure(state="disabled")
        self._stop_queue_btn.configure(state="normal")
        self._kill_resume_btn.configure(state="normal")

    def _on_stop_queue(self) -> None:
        self.session_mgr.stop_session(self._active_session_id)
        self._start_queue_btn.configure(state="normal")
        self._stop_queue_btn.configure(state="disabled")

    def _on_kill_all(self) -> None:
        # Stop broadcast if running
        if self._broadcast.is_running:
            self._broadcast.stop()
            self._bc_start_btn.configure(state="normal")
            self._bc_stop_btn.configure(state="disabled")
            self._bc_graceful_btn.configure(state="disabled")
            self._bc_status.configure(text="KILLED")

        self.session_mgr.stop_all()
        self._start_queue_btn.configure(state="disabled")
        self._stop_queue_btn.configure(state="disabled")
        self._kill_resume_btn.configure(state="disabled")
        self._resume_btn.configure(state="normal")
        self._status_bar.set_status("KILLED - All sessions stopped", "error")
        self._toast("All sessions killed!", "error")

    def _on_resume_all(self) -> None:
        self._start_queue_btn.configure(state="normal")
        self._stop_queue_btn.configure(state="disabled")
        self._kill_resume_btn.configure(state="disabled")
        self._resume_btn.configure(state="disabled")
        self._status_bar.set_status("Ready - select a session and start", "info")
        self._toast("Resumed", "success")

    # ── Fleet dashboard ────────────────────────────────────────────

    def _build_fleet_card(self, scroll: ctk.CTkScrollableFrame) -> None:
        """Build the Fleet Dashboard card at the bottom of the settings scroll."""
        c = self._colors
        fleet_card = ctk.CTkFrame(
            scroll, fg_color=c["bg_card"], corner_radius=8,
            border_width=2, border_color="#2563eb",
        )
        fleet_card.pack(fill="x", pady=SPACING["sm"])

        ctk.CTkLabel(
            fleet_card, text="\U0001F310 Fleet Dashboard — All Devices",
            font=("Segoe UI", 15, "bold"), text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 2), anchor="w")

        ctk.CTkLabel(
            fleet_card,
            text="Send one goal to all PCs + Pis simultaneously. Each device codes independently.",
            font=("Segoe UI", 11), text_color=c["fg_muted"],
            wraplength=650, justify="left",
        ).pack(padx=SPACING["lg"], pady=(0, 4), anchor="w")

        # Broadcast input row
        bc_row = ctk.CTkFrame(fleet_card, fg_color="transparent")
        bc_row.pack(fill="x", padx=SPACING["lg"], pady=4)

        self._fleet_goal_input = ctk.CTkEntry(
            bc_row, placeholder_text="Goal to broadcast to ALL devices…",
            font=("Segoe UI", 11), height=32,
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._fleet_goal_input.pack(side="left", fill="x", expand=True, padx=(0, 4))

        ctk.CTkButton(
            bc_row, text="⚡ Broadcast to ALL",
            font=("Segoe UI", 11, "bold"),
            fg_color="#8e44ad", hover_color="#9b59b6",
            height=32, width=155,
            command=self._on_fleet_broadcast,
        ).pack(side="left")

        self._fleet_bc_status = ctk.CTkLabel(
            fleet_card, text="",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
        )
        self._fleet_bc_status.pack(padx=SPACING["lg"], pady=(0, 2), anchor="w")

        # PC sessions row
        ctk.CTkLabel(
            fleet_card, text="PC Sessions",
            font=("Segoe UI", 12, "bold"), text_color=c["fg_secondary"],
        ).pack(padx=SPACING["lg"], pady=(8, 2), anchor="w")

        pc_row = ctk.CTkFrame(fleet_card, fg_color="transparent")
        pc_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))

        for corner in ("top-left", "top-right", "bottom-left", "bottom-right"):
            device_id = f"pc:{corner}"
            col = ctk.CTkFrame(pc_row, fg_color="transparent")
            col.pack(side="left", padx=3, anchor="n")

            mini = FleetDeviceMiniCard(col, c, width=170)
            mini.pack()
            self._fleet_device_cards[device_id] = mini

            ctk.CTkButton(
                col, text="OpenCode",
                font=("Segoe UI", 9), height=22, width=170,
                fg_color=c["bg_tertiary"], hover_color=c["bg_hover"],
                text_color=c["fg_secondary"],
                command=lambda cn=corner: self._on_fleet_opencode(cn),
            ).pack(pady=(2, 0))

        # Pi devices row
        ctk.CTkLabel(
            fleet_card, text="Pi Devices",
            font=("Segoe UI", 12, "bold"), text_color=c["fg_secondary"],
        ).pack(padx=SPACING["lg"], pady=(8, 2), anchor="w")

        self._fleet_pi_row = ctk.CTkFrame(fleet_card, fg_color="transparent")
        self._fleet_pi_row.pack(fill="x", padx=SPACING["lg"], pady=(0, SPACING["md"]))

        self._fleet_pi_empty_lbl = ctk.CTkLabel(
            self._fleet_pi_row,
            text="No Pi devices connected yet. Add Pis to picontrol/pi_config.json.",
            font=("Segoe UI", 10), text_color=c["fg_muted"],
        )
        self._fleet_pi_empty_lbl.pack(anchor="w")

    def _refresh_fleet_view(self) -> None:
        """Poll FleetManager and update all device mini-cards (every 5 s)."""
        if self._fleet_mgr is None:
            return
        try:
            devices = self._fleet_mgr.all_devices()
            has_pi = False

            for dev in devices:
                if dev.kind == "pc":
                    card = self._fleet_device_cards.get(dev.device_id)
                    if card:
                        card.update_from_device(dev, self._colors)

                elif dev.kind == "pi":
                    has_pi = True
                    if dev.device_id not in self._fleet_device_cards:
                        # Hide the empty-state label on first Pi
                        try:
                            self._fleet_pi_empty_lbl.pack_forget()
                        except Exception:
                            pass
                        pi_card = FleetDeviceMiniCard(self._fleet_pi_row, self._colors, width=170)
                        pi_card.pack(side="left", padx=3, anchor="n")
                        self._fleet_device_cards[dev.device_id] = pi_card

                    card = self._fleet_device_cards.get(dev.device_id)
                    if card:
                        card.update_from_device(dev, self._colors)

            if not has_pi:
                try:
                    self._fleet_pi_empty_lbl.pack(anchor="w")
                except Exception:
                    pass

        except Exception as exc:
            logger.debug("Fleet refresh error: %s", exc)

        # Run OpenCode watchdog on a worker thread so SSH/subprocess checks don't block UI.
        try:
            import threading as _threading
            _threading.Thread(
                target=self._fleet_mgr.watchdog_opencode,
                daemon=True,
                name="fleet-oc-watchdog",
            ).start()
        except Exception:
            pass

        try:
            self._fleet_poll_id = self.after(5000, self._refresh_fleet_view)
        except Exception:
            pass

    def _on_fleet_broadcast(self) -> None:
        """Broadcast the fleet goal to all PCs + Pis simultaneously."""
        goal = self._fleet_goal_input.get().strip()
        if not goal:
            self._fleet_bc_status.configure(
                text="Enter a goal first.", text_color=self._colors["warning"])
            return

        self._fleet_bc_status.configure(
            text="Broadcasting to all devices…", text_color=self._colors["warning"])

        def worker() -> None:
            results = self._fleet_mgr.broadcast(goal)
            ok_count = sum(1 for v in results.values() if v)
            total = len(results)
            color = self._colors["success"] if ok_count else self._colors["error"]
            msg = (f"✓ Broadcast sent to {ok_count}/{total} device(s)."
                   if total else "No active devices — connect sessions or Pi devices first.")
            self.after(0, lambda: self._fleet_bc_status.configure(text=msg, text_color=color))
            if ok_count:
                self.after(0, lambda: self._toast(
                    f"Broadcast to {ok_count} device(s)!", "success"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_fleet_opencode(self, corner: str) -> None:
        """Launch OpenCode in a new terminal for the given PC corner."""
        goal = self._fleet_goal_input.get().strip()
        ok = self._fleet_mgr.launch_opencode(corner, goal=goal)
        if ok:
            self._toast(f"OpenCode launched for {corner}", "success")
        else:
            self._toast("OpenCode not found on PATH — install opencode first", "warning")

    # ── CDP browser automation ────────────────────────────────────

    def _on_launch_cdp_browser(self) -> None:
        """Launch one Autocoder Chrome per session card, each on its own CDP port."""
        from ..cdp_client import (
            CDPConnection, CDPChatAutomation, get_selectors_for_profile,
            claim_ws_url, release_ws_url, _find_unclaimed_target,
        )

        # Build the list of corners to launch — all 4 cards regardless of state
        corners_to_launch: list[tuple[str, str, str]] = []  # (corner, ai_name, url)
        for corner, card in self._session_cards.items():
            ai_name = card._ai_selector.get()
            profile = get_profile(ai_name)
            url = profile.url or "https://gemini.google.com/app"
            corners_to_launch.append((corner, ai_name, url))

        self._cdp_status.configure(
            text=f"Launching {len(corners_to_launch)} Chrome instance(s)…",
            text_color=self._colors["warning"],
        )

        def worker():
            successes: list[str] = []

            for i, (corner, ai_name, url) in enumerate(corners_to_launch):
                port = CDP_CORNER_PORTS.get(corner, DEFAULT_CDP_PORT)

                # Skip if already running on this port
                if is_cdp_available(port):
                    logger.info("CDP already active on port %d (%s), skipping launch", port, corner)
                else:
                    status_msg = f"Starting {ai_name} on port {port} ({corner})…"
                    self.after(0, lambda m=status_msg: self._cdp_status.configure(
                        text=m, text_color=self._colors["info"],
                    ))
                    prepare_for_cdp_launch(ports=[port])
                    ok = launch_chrome_with_cdp(url=url, port=port, corner=corner)
                    if not ok:
                        logger.warning("Chrome launch failed for %s on port %d", corner, port)
                        continue
                    # Stagger between launches so the OS doesn't choke
                    if i < len(corners_to_launch) - 1:
                        time.sleep(2.0)

                # Auto-grab this tab into its session card
                profile = get_profile(ai_name)
                url_pattern = profile.url_pattern or ""
                target = _find_unclaimed_target(url_pattern, "", port)
                if not target:
                    logger.warning("No matching tab found on port %d for %s", port, ai_name)
                    continue

                if not claim_ws_url(target.ws_url):
                    logger.warning("Tab on port %d already claimed", port)
                    continue

                conn = CDPConnection(target.ws_url)
                if not conn.connect():
                    release_ws_url(target.ws_url)
                    logger.warning("CDP connection failed on port %d", port)
                    continue

                selectors = get_selectors_for_profile(ai_name)
                automation = CDPChatAutomation(conn, selectors, ai_name)

                card = self._session_cards.get(corner)
                if not card:
                    continue

                if not card._session:
                    try:
                        session = self.session_mgr.create_session(profile, corner)
                        card._session = session
                        self.after(0, card._wire_callbacks)
                    except RuntimeError as e:
                        logger.warning("Could not create session for %s: %s", corner, e)
                        release_ws_url(target.ws_url)
                        conn.disconnect()
                        continue

                if card._session and card._session.client:
                    client = card._session.client
                    client._cdp = automation
                    client._cdp_available = True
                    client._configured = True
                    client._cdp_port = port
                    card._session.is_configured = True
                    card._session.ai_profile = profile

                    def _update(c=card, n=ai_name):
                        c._set_state("ready")
                        c._mode_label.configure(
                            text="Mode: CDP (reliable)",
                            text_color=c._colors["success"],
                        )
                        c._ai_selector.set(n)
                    self.after(0, _update)
                    successes.append(f"{ai_name}@{corner}")
                    logger.info("Auto-grabbed %s CDP tab for %s (port %d)", ai_name, corner, port)

            if successes:
                msg = f"{ICONS['check']} {len(successes)} session(s) ready: {', '.join(successes)}"
                self.after(0, lambda: self._cdp_status.configure(
                    text=msg, text_color=self._colors["success"],
                ))
                self.after(0, lambda: self._toast(
                    f"{len(successes)} AI session(s) connected!", "success"
                ))
            else:
                self.after(0, lambda: self._cdp_status.configure(
                    text="Launch failed — check Chrome is installed and try again.",
                    text_color=self._colors["error"],
                ))
            self.after(500, self._refresh_all_window_lists)

        threading.Thread(target=worker, daemon=True).start()

    def _on_check_cdp(self) -> None:
        """Check CDP connectivity on all ports."""
        all_targets = []
        active_ports = []
        for port in sorted(set(CDP_CORNER_PORTS.values())):
            targets = discover_cdp_targets(port)
            if targets:
                active_ports.append(port)
                all_targets.extend(targets)

        if all_targets:
            names = [t.title[:25] for t in all_targets[:6]]
            self._cdp_status.configure(
                text=f"{ICONS['check']} CDP on port(s) {active_ports}: {len(all_targets)} tab(s) — {', '.join(names)}",
                text_color=self._colors["success"],
            )
        else:
            self._cdp_status.configure(
                text="CDP not available. Click 'Launch CDP Browser' first.",
                text_color=self._colors["warning"],
            )

    # ── Traffic controller (same as before) ───────────────────────

    def _on_start_traffic(self) -> None:
        try:
            subprocess.Popen(
                [sys.executable, "-m", "mousetraffic"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._traffic_status.configure(
                text=f"{ICONS['check']} Traffic Controller starting...",
                text_color=self._colors["success"],
            )
            self._toast("Traffic Controller started!", "success")
        except Exception as e:
            self._traffic_status.configure(
                text=f"Failed: {e}", text_color=self._colors["error"],
            )

    def _on_check_traffic(self) -> None:
        if TRAFFIC_AVAILABLE:
            status = TrafficClient.get_status()
            if "error" not in status:
                holder = status.get("current_holder_name", "Nobody")
                queue = status.get("queue_depth", 0)
                human = status.get("human_override", False)
                human_txt = " | Human Override!" if human else ""
                self._traffic_status.configure(
                    text=f"{ICONS['check']} Running | Mouse: {holder or 'Free'} | Queue: {queue}{human_txt}",
                    text_color=self._colors["success"],
                )
            else:
                self._traffic_status.configure(
                    text="Not running. Click 'Start Traffic Controller' first.",
                    text_color=self._colors["warning"],
                )
        else:
            self._traffic_status.configure(
                text="mousetraffic package not found",
                text_color=self._colors["error"],
            )

    # ── Overrides for base class ──────────────────────────────────

    def _on_save_api_key(self) -> None:
        pass  # No API keys in browser mode

    def _show_api_key_prompt(self) -> None:
        self._show_view("settings")

    def _on_save_task_settings(self) -> None:
        try:
            default_min = int(self._default_time_input.get().strip())
            if default_min < 1:
                raise ValueError
        except ValueError:
            self._toast("Default minutes must be a positive number", "warning")
            return
        try:
            depth = int(self._depth_input.get().strip())
            if depth < 1:
                raise ValueError
        except ValueError:
            self._toast("Depth limit must be a positive number", "warning")
            return
        self._config_manager.update(
            task_default_minutes=default_min, expand_depth_limit=depth,
        )
        if self._expander:
            self._expander._depth_limit = depth
        self._toast("Settings saved!", "success")

    def _on_close(self) -> None:
        # Stop fleet poll timer
        if self._fleet_poll_id is not None:
            try:
                self.after_cancel(self._fleet_poll_id)
            except Exception:
                pass
        if self._fleet_mgr is not None:
            try:
                self._fleet_mgr.stop()
            except Exception:
                pass

        # Stop all broadcast and session threads first
        if self._broadcast.is_running:
            self._broadcast.stop()
        self.session_mgr.stop_all()

        # Don't call super()._on_close() — it expects self._task_executor
        # which is None in the web app (we use SessionManager instead).
        # Replicate the safe parts: save config and destroy.
        try:
            self._config_manager.update(
                auto_scroll=self._cfg.auto_scroll,
            )
        except Exception:
            pass
        self.destroy()


# Backward-compatibility alias
GeminiCoderWebApp = AutocoderApp
