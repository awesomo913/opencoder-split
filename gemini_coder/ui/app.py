"""Main application window for Gemini Coder."""

import customtkinter as ctk
import json
import logging
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

from .. import __version__, __app_name__
from ..config import ConfigManager
from ..gemini_client import GeminiClient, Conversation
from ..history import HistoryManager, HistoryEntry
from ..task_manager import CodingTask, TaskQueue, TaskExecutor, TaskStatus
from ..expander import ExpansionEngine, ExpansionNode, ExpansionOption
from ..platform_utils import detect_platform, get_config_dir, get_desktop_path
from .theme import get_colors, FONTS, SPACING, ICONS

logger = logging.getLogger(__name__)


class StatusBar(ctk.CTkFrame):
    """Bottom status bar with contextual information."""

    def __init__(self, parent: ctk.CTkFrame, colors: dict, **kwargs) -> None:
        super().__init__(parent, height=30, corner_radius=0, **kwargs)
        self._colors = colors
        self.pack_propagate(False)

        self._status_label = ctk.CTkLabel(
            self, text="Ready", anchor="w",
            font=("Segoe UI", 11),
            text_color=colors["fg_secondary"],
        )
        self._status_label.pack(side="left", padx=SPACING["md"])

        self._time_label = ctk.CTkLabel(
            self, text="", anchor="e",
            font=("Segoe UI", 11),
            text_color=colors["fg_muted"],
        )
        self._time_label.pack(side="right", padx=SPACING["md"])

        self._model_label = ctk.CTkLabel(
            self, text="", anchor="e",
            font=("Segoe UI", 11),
            text_color=colors["fg_muted"],
        )
        self._model_label.pack(side="right", padx=SPACING["md"])

    def set_status(self, text: str, level: str = "info") -> None:
        color_map = {
            "info": self._colors["fg_secondary"],
            "success": self._colors["success"],
            "warning": self._colors["warning"],
            "error": self._colors["error"],
        }
        self._status_label.configure(
            text=text,
            text_color=color_map.get(level, self._colors["fg_secondary"])
        )

    def set_model(self, model: str) -> None:
        self._model_label.configure(text=f"Model: {model}")

    def set_time(self, text: str) -> None:
        self._time_label.configure(text=text)


class ToastNotification(ctk.CTkFrame):
    """Non-blocking toast notification."""

    def __init__(self, parent, message: str, level: str = "info",
                 duration_ms: int = 3000, colors: dict = None) -> None:
        color_map = {
            "info": colors["info"] if colors else "#3498db",
            "success": colors["success"] if colors else "#2ecc71",
            "warning": colors["warning"] if colors else "#f39c12",
            "error": colors["error"] if colors else "#e74c3c",
        }
        bg = color_map.get(level, color_map["info"])
        super().__init__(parent, fg_color=bg, corner_radius=8)

        ctk.CTkLabel(
            self, text=message,
            font=("Segoe UI", 12, "bold"),
            text_color="#ffffff",
        ).pack(padx=SPACING["lg"], pady=SPACING["sm"])

        self.place(relx=0.5, rely=0.02, anchor="n")
        self.after(duration_ms, self.destroy)


class RightClickMenu:
    """Right-click context menu for any text widget with copy/paste/select all."""

    @staticmethod
    def bind(widget, app_ref=None) -> None:
        """Bind right-click menu to a text widget (CTkTextbox or CTkEntry)."""
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Cut          Ctrl+X",
                         command=lambda: RightClickMenu._cut(widget))
        menu.add_command(label="Copy         Ctrl+C",
                         command=lambda: RightClickMenu._copy(widget))
        menu.add_command(label="Paste        Ctrl+V",
                         command=lambda: RightClickMenu._paste(widget))
        menu.add_separator()
        menu.add_command(label="Select All   Ctrl+A",
                         command=lambda: RightClickMenu._select_all(widget))
        if app_ref:
            menu.add_separator()
            menu.add_command(
                label="Copy All to Clipboard",
                command=lambda: RightClickMenu._copy_all(widget, app_ref),
            )

        def show_menu(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", show_menu)

    @staticmethod
    def _cut(widget) -> None:
        try:
            widget.event_generate("<<Cut>>")
        except Exception:
            pass

    @staticmethod
    def _copy(widget) -> None:
        try:
            widget.event_generate("<<Copy>>")
        except Exception:
            pass

    @staticmethod
    def _paste(widget) -> None:
        try:
            widget.event_generate("<<Paste>>")
        except Exception:
            pass

    @staticmethod
    def _select_all(widget) -> None:
        try:
            if isinstance(widget, ctk.CTkTextbox):
                widget.tag_add("sel", "1.0", "end")
            elif isinstance(widget, ctk.CTkEntry):
                widget.select_range(0, "end")
        except Exception:
            pass

    @staticmethod
    def _copy_all(widget, app_ref) -> None:
        try:
            if isinstance(widget, ctk.CTkTextbox):
                text = widget.get("1.0", "end").strip()
            else:
                text = widget.get().strip()
            if text and app_ref:
                app_ref._copy_to_clipboard(text)
        except Exception:
            pass


class GeminiCoderApp(ctk.CTk):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()

        self._start_time = time.time()
        self._config_manager = ConfigManager()
        self._cfg = self._config_manager.config
        self._platform = detect_platform()

        ctk.set_appearance_mode(self._cfg.theme)
        ctk.set_default_color_theme("blue")
        self._colors = get_colors(self._cfg.theme)

        self.title(f"{__app_name__} v{__version__}")
        self.geometry(f"{self._cfg.window_width}x{self._cfg.window_height}")
        self.minsize(900, 600)

        self._gemini = GeminiClient(
            api_key=self._cfg.api_key,
            model_name=self._cfg.model_name,
            max_tokens=self._cfg.max_tokens,
            temperature=self._cfg.temperature,
        )

        queue_path = get_config_dir() / "task_queue.json"
        self._task_queue = TaskQueue(save_path=queue_path)
        self._task_executor = TaskExecutor(self._gemini, self._task_queue)
        self._expander = ExpansionEngine(
            self._gemini, depth_limit=self._cfg.expand_depth_limit
        )
        self._history = HistoryManager()

        self._current_view = "expand"
        self._last_completed_task: Optional[CodingTask] = None
        self._setup_logging()
        self._build_ui()
        self._bind_shortcuts()
        self._setup_callbacks()
        self._start_clock()

        if not self._cfg.api_key:
            self.after(500, self._show_api_key_prompt)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_logging(self) -> None:
        from ..platform_utils import get_log_dir
        from logging.handlers import RotatingFileHandler

        log_dir = get_log_dir()
        handler = RotatingFileHandler(
            log_dir / "gemini_coder.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        root_logger.setLevel(getattr(logging, self._cfg.log_level, logging.INFO))

    def _build_ui(self) -> None:
        """Build the complete UI layout."""
        c = self._colors

        self.configure(fg_color=c["bg_primary"])

        self._top_bar = ctk.CTkFrame(
            self, height=50, corner_radius=0, fg_color=c["bg_secondary"]
        )
        self._top_bar.pack(fill="x", side="top")
        self._top_bar.pack_propagate(False)

        title_label = ctk.CTkLabel(
            self._top_bar,
            text=f"{ICONS['rocket']} {__app_name__}",
            font=("Segoe UI", 18, "bold"),
            text_color=c["accent"],
        )
        title_label.pack(side="left", padx=SPACING["lg"])

        self._connection_indicator = ctk.CTkLabel(
            self._top_bar, text="\u25CF Disconnected",
            font=("Segoe UI", 11),
            text_color=c["error"],
        )
        self._connection_indicator.pack(side="right", padx=SPACING["lg"])

        theme_btn = ctk.CTkButton(
            self._top_bar, text="\u263E", width=36, height=36,
            command=self._toggle_theme,
            fg_color="transparent",
            hover_color=c["bg_hover"],
            font=("Segoe UI", 16),
        )
        theme_btn.pack(side="right", padx=4)

        self._status_bar = StatusBar(
            self, colors=c, fg_color=c["bg_secondary"]
        )
        self._status_bar.pack(fill="x", side="bottom")
        self._status_bar.set_model(self._cfg.model_name)

        main_container = ctk.CTkFrame(self, fg_color=c["bg_primary"], corner_radius=0)
        main_container.pack(fill="both", expand=True)

        self._sidebar = ctk.CTkFrame(
            main_container, width=200, corner_radius=0,
            fg_color=c["bg_secondary"],
        )
        self._sidebar.pack(fill="y", side="left")
        self._sidebar.pack_propagate(False)

        self._content = ctk.CTkFrame(
            main_container, fg_color=c["bg_primary"], corner_radius=0
        )
        self._content.pack(fill="both", expand=True, side="left")

        self._build_sidebar()

        self._frames: dict[str, ctk.CTkFrame] = {}
        self._build_expand_view()
        self._build_task_view()
        self._build_history_view()
        self._build_settings_view()
        self._build_diagnostics_view()

        self._show_view("expand")

    def _build_sidebar(self) -> None:
        c = self._colors
        pad = SPACING["sm"]

        ctk.CTkLabel(
            self._sidebar, text="Navigation",
            font=("Segoe UI", 11, "bold"),
            text_color=c["fg_muted"],
        ).pack(padx=pad, pady=(SPACING["lg"], pad), anchor="w")

        nav_items = [
            ("expand", f"{ICONS['expand']} Expand Mode", "Explore ideas infinitely"),
            ("tasks", f"{ICONS['tasks']} Task Queue", "Timed coding tasks"),
            ("history", f"{ICONS['folder']} History", "Past sessions & results"),
            ("settings", f"{ICONS['gear']} Settings", "API key & preferences"),
            ("diagnostics", f"{ICONS['diagnostics']} Diagnostics", "System report"),
        ]

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        for key, label, tooltip in nav_items:
            btn = ctk.CTkButton(
                self._sidebar, text=label, anchor="w",
                font=("Segoe UI", 13),
                fg_color="transparent",
                hover_color=c["bg_hover"],
                text_color=c["fg_primary"],
                height=40,
                command=lambda k=key: self._show_view(k),
            )
            btn.pack(fill="x", padx=pad, pady=2)
            self._nav_buttons[key] = btn

        ctk.CTkFrame(
            self._sidebar, height=1, fg_color=c["border"]
        ).pack(fill="x", padx=pad, pady=SPACING["md"])

        self._queue_summary = ctk.CTkLabel(
            self._sidebar, text="Queue: 0 tasks",
            font=("Segoe UI", 11),
            text_color=c["fg_muted"],
        )
        self._queue_summary.pack(padx=pad, anchor="w")

        self._time_remaining = ctk.CTkLabel(
            self._sidebar, text="Est: 0 min",
            font=("Segoe UI", 11),
            text_color=c["fg_muted"],
        )
        self._time_remaining.pack(padx=pad, anchor="w")

    def _show_view(self, view_name: str) -> None:
        """Switch the main content view."""
        c = self._colors
        for key, btn in self._nav_buttons.items():
            if key == view_name:
                btn.configure(fg_color=c["bg_tertiary"], text_color=c["fg_heading"])
            else:
                btn.configure(fg_color="transparent", text_color=c["fg_primary"])

        for name, frame in self._frames.items():
            if name == view_name:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

        self._current_view = view_name
        if view_name == "history" and hasattr(self, "_history_list_frame"):
            self._refresh_history()

    # ── EXPAND MODE ──────────────────────────────────────────────

    def _build_expand_view(self) -> None:
        c = self._colors
        frame = ctk.CTkFrame(self._content, fg_color=c["bg_primary"], corner_radius=0)
        self._frames["expand"] = frame

        top = ctk.CTkFrame(frame, fg_color=c["bg_secondary"], corner_radius=8)
        top.pack(fill="x", padx=SPACING["lg"], pady=SPACING["md"])

        ctk.CTkLabel(
            top, text=f"{ICONS['expand']} What do you want to build?",
            font=("Segoe UI", 16, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")

        ctk.CTkLabel(
            top,
            text="Describe your idea and I'll break it down into options, explained like you've never coded before.",
            font=("Segoe UI", 12),
            text_color=c["fg_secondary"],
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        input_row = ctk.CTkFrame(top, fg_color="transparent")
        input_row.pack(fill="x", padx=SPACING["lg"], pady=SPACING["md"])

        self._expand_input = ctk.CTkTextbox(
            input_row, height=80, font=("Segoe UI", 13),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"], border_width=1,
            corner_radius=8,
        )
        self._expand_input.pack(fill="x", side="left", expand=True, padx=(0, SPACING["sm"]))
        RightClickMenu.bind(self._expand_input, self)

        btn_col = ctk.CTkFrame(input_row, fg_color="transparent")
        btn_col.pack(side="right")

        self._expand_btn = ctk.CTkButton(
            btn_col, text=f"{ICONS['rocket']} Explore",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["accent"], hover_color=c["accent_hover"],
            width=120, height=40,
            command=self._on_expand_start,
        )
        self._expand_btn.pack(pady=(0, 4))

        self._expand_gen_btn = ctk.CTkButton(
            btn_col, text=f"{ICONS['code']} Generate Code",
            font=("Segoe UI", 12),
            fg_color=c["accent_secondary"],
            width=120, height=32,
            command=self._on_generate_code,
            state="disabled",
        )
        self._expand_gen_btn.pack()

        self._breadcrumb_frame = ctk.CTkFrame(frame, fg_color="transparent", height=30)
        self._breadcrumb_frame.pack(fill="x", padx=SPACING["lg"])

        self._expand_scroll = ctk.CTkScrollableFrame(
            frame, fg_color=c["bg_primary"], corner_radius=0,
        )
        self._expand_scroll.pack(fill="both", expand=True, padx=SPACING["lg"], pady=SPACING["sm"])

        self._expand_content = self._expand_scroll

    def _on_expand_start(self) -> None:
        text = self._expand_input.get("1.0", "end").strip()
        if not text:
            self._toast("Please enter an idea to explore", "warning")
            return
        if not self._gemini.is_configured:
            self._toast("Set your API key first in Settings", "error")
            self._show_view("settings")
            return

        self._expand_btn.configure(state="disabled", text="Thinking...")
        self._status_bar.set_status("Expanding idea...", "info")
        self._clear_expand_content()

        def worker():
            try:
                node = self._expander.start_new(text)
                self.after(0, lambda: self._render_expansion(node))
            except Exception as e:
                self.after(0, lambda: self._toast(f"Error: {e}", "error"))
            finally:
                self.after(0, lambda: self._expand_btn.configure(
                    state="normal", text=f"{ICONS['rocket']} Explore"
                ))
                self.after(0, lambda: self._status_bar.set_status("Ready"))

        threading.Thread(target=worker, daemon=True).start()

    def _render_expansion(self, node: ExpansionNode) -> None:
        if node.depth == 0 and node.full_response:
            self._history.add(HistoryEntry(
                entry_type="expansion",
                title=node.prompt[:80],
                prompt=node.prompt,
                response=node.response_summary,
                model=self._cfg.model_name,
                status="completed",
            ))
        self._clear_expand_content()
        c = self._colors

        self._update_breadcrumbs()

        if node.response_summary:
            summary_card = ctk.CTkFrame(
                self._expand_content, fg_color=c["bg_card"], corner_radius=8
            )
            summary_card.pack(fill="x", pady=SPACING["sm"])

            ctk.CTkLabel(
                summary_card, text="Summary",
                font=("Segoe UI", 14, "bold"),
                text_color=c["fg_heading"],
            ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")

            ctk.CTkLabel(
                summary_card, text=node.response_summary,
                font=("Segoe UI", 12),
                text_color=c["fg_secondary"],
                wraplength=700, justify="left",
            ).pack(padx=SPACING["lg"], pady=(0, SPACING["md"]), anchor="w")

        if node.options:
            ctk.CTkLabel(
                self._expand_content,
                text=f"Choose an option to explore deeper (Depth {node.depth + 1}/{self._cfg.expand_depth_limit}):",
                font=("Segoe UI", 13, "bold"),
                text_color=c["fg_heading"],
            ).pack(padx=4, pady=SPACING["sm"], anchor="w")

            for option in node.options:
                self._render_option_card(option)

        self._expand_gen_btn.configure(state="normal")

    def _render_option_card(self, option: ExpansionOption) -> None:
        c = self._colors

        card = ctk.CTkFrame(
            self._expand_content, fg_color=c["bg_card"], corner_radius=8
        )
        card.pack(fill="x", pady=4)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=SPACING["lg"], pady=(SPACING["md"], 4))

        ctk.CTkLabel(
            header, text=option.title,
            font=("Segoe UI", 14, "bold"),
            text_color=c["fg_heading"],
        ).pack(side="left")

        diff_color = c.get(f"tag_{option.difficulty}", c["fg_muted"])
        ctk.CTkLabel(
            header, text=option.difficulty.capitalize(),
            font=("Segoe UI", 10, "bold"),
            text_color=diff_color,
        ).pack(side="right", padx=4)

        if option.estimated_time:
            ctk.CTkLabel(
                header, text=f"{ICONS['clock']} {option.estimated_time}",
                font=("Segoe UI", 10),
                text_color=c["fg_muted"],
            ).pack(side="right", padx=SPACING["sm"])

        ctk.CTkLabel(
            card, text=option.description[:300],
            font=("Segoe UI", 12),
            text_color=c["fg_secondary"],
            wraplength=650, justify="left",
        ).pack(padx=SPACING["lg"], pady=4, anchor="w")

        if option.code_preview:
            code_frame = ctk.CTkFrame(card, fg_color=c["code_bg"], corner_radius=4)
            code_frame.pack(fill="x", padx=SPACING["lg"], pady=4)
            ctk.CTkLabel(
                code_frame,
                text=option.code_preview[:500],
                font=("Consolas", 11),
                text_color=c["code_fg"],
                justify="left", anchor="w",
            ).pack(padx=SPACING["sm"], pady=SPACING["sm"], anchor="w")

        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=SPACING["lg"], pady=(4, SPACING["md"]))

        ctk.CTkButton(
            btn_row,
            text=f"{ICONS['forward']} Explore This",
            font=("Segoe UI", 12, "bold"),
            fg_color=c["accent"],
            hover_color=c["accent_hover"],
            height=32,
            command=lambda oid=option.id: self._on_expand_option(oid),
        ).pack(side="left")

        ctk.CTkButton(
            btn_row,
            text=f"{ICONS['copy']} Copy Preview",
            font=("Segoe UI", 11),
            fg_color="transparent",
            hover_color=c["bg_hover"],
            text_color=c["fg_secondary"],
            height=28,
            command=lambda: self._copy_to_clipboard(option.code_preview),
        ).pack(side="left", padx=SPACING["sm"])

    def _on_expand_option(self, option_id: str) -> None:
        if not self._expander.can_go_deeper:
            self._toast("Depth limit reached! Generate code from current selections.", "warning")
            return

        self._expand_btn.configure(state="disabled", text="Exploring...")
        self._status_bar.set_status("Exploring option...", "info")

        def worker():
            try:
                node = self._expander.expand_option(option_id)
                if node:
                    self.after(0, lambda: self._render_expansion(node))
                else:
                    self.after(0, lambda: self._toast("Could not expand option", "warning"))
            except Exception as e:
                self.after(0, lambda: self._toast(f"Error: {e}", "error"))
            finally:
                self.after(0, lambda: self._expand_btn.configure(
                    state="normal", text=f"{ICONS['rocket']} Explore"
                ))
                self.after(0, lambda: self._status_bar.set_status("Ready"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_generate_code(self) -> None:
        if not self._gemini.is_configured:
            self._toast("Set your API key first", "error")
            return

        self._expand_gen_btn.configure(state="disabled", text="Generating...")
        self._status_bar.set_status("Generating full code...", "info")

        def worker():
            try:
                code = self._expander.generate_code_for_current()
                self.after(0, lambda: self._show_code_result(code))
            except Exception as e:
                self.after(0, lambda: self._toast(f"Error: {e}", "error"))
            finally:
                self.after(0, lambda: self._expand_gen_btn.configure(
                    state="normal", text=f"{ICONS['code']} Generate Code"
                ))
                self.after(0, lambda: self._status_bar.set_status("Ready"))

        threading.Thread(target=worker, daemon=True).start()

    def _show_code_result(self, code: str) -> None:
        crumbs = self._expander.get_breadcrumbs()
        title = crumbs[-1][1] if crumbs else "Generated Code"
        prompt_path = " > ".join(c[1] for c in crumbs)
        self._history.add(HistoryEntry(
            entry_type="code_generation",
            title=title,
            prompt=prompt_path,
            response=code,
            model=self._cfg.model_name,
            status="completed",
        ))

        c = self._colors
        win = ctk.CTkToplevel(self)
        win.title("Generated Code")
        win.geometry("900x700")
        win.configure(fg_color=c["bg_primary"])

        toolbar = ctk.CTkFrame(win, fg_color=c["bg_secondary"], height=40)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)

        ctk.CTkButton(
            toolbar, text=f"{ICONS['copy']} Copy All",
            font=("Segoe UI", 12),
            fg_color=c["accent"],
            command=lambda: self._copy_to_clipboard(code),
        ).pack(side="left", padx=SPACING["sm"], pady=4)

        ctk.CTkButton(
            toolbar, text=f"{ICONS['save']} Save to File",
            font=("Segoe UI", 12),
            fg_color=c["accent_secondary"],
            command=lambda: self._save_code_to_file(code),
        ).pack(side="left", padx=SPACING["sm"], pady=4)

        text_widget = ctk.CTkTextbox(
            win, font=("Consolas", 12),
            fg_color=c["code_bg"], text_color=c["code_fg"],
            wrap="none",
        )
        text_widget.pack(fill="both", expand=True, padx=SPACING["sm"], pady=SPACING["sm"])
        text_widget.insert("1.0", code)
        RightClickMenu.bind(text_widget, self)

    def _update_breadcrumbs(self) -> None:
        for w in self._breadcrumb_frame.winfo_children():
            w.destroy()

        c = self._colors
        crumbs = self._expander.get_breadcrumbs()

        if len(crumbs) > 1:
            ctk.CTkButton(
                self._breadcrumb_frame,
                text=f"{ICONS['back']} Back",
                font=("Segoe UI", 11),
                fg_color="transparent",
                hover_color=c["bg_hover"],
                text_color=c["accent"],
                height=24, width=60,
                command=self._on_expand_back,
            ).pack(side="left", padx=2)

        for i, (nid, title) in enumerate(crumbs):
            if i > 0:
                ctk.CTkLabel(
                    self._breadcrumb_frame, text=" > ",
                    font=("Segoe UI", 11),
                    text_color=c["fg_muted"],
                ).pack(side="left")

            is_current = (i == len(crumbs) - 1)
            btn = ctk.CTkButton(
                self._breadcrumb_frame,
                text=title,
                font=("Segoe UI", 11, "bold" if is_current else "normal"),
                fg_color="transparent",
                hover_color=c["bg_hover"],
                text_color=c["accent"] if is_current else c["fg_muted"],
                height=24,
                command=lambda nid=nid: self._on_breadcrumb_click(nid),
            )
            btn.pack(side="left", padx=2)

    def _on_expand_back(self) -> None:
        node = self._expander.go_back()
        if node:
            self._render_expansion(node)

    def _on_breadcrumb_click(self, node_id: str) -> None:
        node = self._expander.go_to_node(node_id)
        if node:
            self._render_expansion(node)

    def _clear_expand_content(self) -> None:
        for w in self._expand_content.winfo_children():
            w.destroy()

    # ── TASK QUEUE MODE ──────────────────────────────────────────

    def _build_task_view(self) -> None:
        c = self._colors
        frame = ctk.CTkFrame(self._content, fg_color=c["bg_primary"], corner_radius=0)
        self._frames["tasks"] = frame

        top = ctk.CTkFrame(frame, fg_color=c["bg_secondary"], corner_radius=8)
        top.pack(fill="x", padx=SPACING["lg"], pady=SPACING["md"])

        ctk.CTkLabel(
            top, text=f"{ICONS['tasks']} Task Queue - Automated Coding Pipeline",
            font=("Segoe UI", 16, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")

        ctk.CTkLabel(
            top,
            text="Add tasks in order. Set time budgets. Gemini will code each one, moving to the next when time is up.",
            font=("Segoe UI", 12),
            text_color=c["fg_secondary"],
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        add_frame = ctk.CTkFrame(top, fg_color="transparent")
        add_frame.pack(fill="x", padx=SPACING["lg"], pady=SPACING["md"])

        left_inputs = ctk.CTkFrame(add_frame, fg_color="transparent")
        left_inputs.pack(fill="x", side="left", expand=True)

        row1 = ctk.CTkFrame(left_inputs, fg_color="transparent")
        row1.pack(fill="x", pady=2)

        ctk.CTkLabel(row1, text="Title:", font=("Segoe UI", 12),
                      text_color=c["fg_secondary"]).pack(side="left")
        self._task_title_input = ctk.CTkEntry(
            row1, font=("Segoe UI", 13),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
            placeholder_text="e.g., Build a REST API for user management",
        )
        self._task_title_input.pack(fill="x", side="left", expand=True, padx=SPACING["sm"])
        RightClickMenu.bind(self._task_title_input, self)

        ctk.CTkLabel(row1, text="Minutes:", font=("Segoe UI", 12),
                      text_color=c["fg_secondary"]).pack(side="left")
        self._task_time_input = ctk.CTkEntry(
            row1, width=60, font=("Segoe UI", 13),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._task_time_input.pack(side="left", padx=SPACING["sm"])
        self._task_time_input.insert(0, str(self._cfg.task_default_minutes))

        self._task_desc_input = ctk.CTkTextbox(
            left_inputs, height=60, font=("Segoe UI", 12),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"], border_width=1, corner_radius=8,
        )
        self._task_desc_input.pack(fill="x", pady=4)
        RightClickMenu.bind(self._task_desc_input, self)

        btn_col = ctk.CTkFrame(add_frame, fg_color="transparent", width=160)
        btn_col.pack(side="right", padx=(SPACING["sm"], 0))
        btn_col.pack_propagate(False)

        ctk.CTkButton(
            btn_col, text=f"{ICONS['add']} Add Task",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["accent"], hover_color=c["accent_hover"],
            height=36,
            command=self._on_add_task,
        ).pack(fill="x", pady=2)

        self._task_allow_overtime = ctk.CTkCheckBox(
            btn_col, text="Allow overtime",
            font=("Segoe UI", 11),
            text_color=c["fg_secondary"],
        )
        self._task_allow_overtime.pack(pady=2, anchor="w")
        self._task_allow_overtime.select()

        self._task_auto_improve = ctk.CTkCheckBox(
            btn_col, text="\u2728 Auto-Improve",
            font=("Segoe UI", 11),
            text_color="#8e44ad",
        )
        self._task_auto_improve.pack(pady=2, anchor="w")

        # ── Control bar with hotkey labels ───────────────────────────
        control_bar = ctk.CTkFrame(frame, fg_color=c["bg_secondary"], corner_radius=8)
        control_bar.pack(fill="x", padx=SPACING["lg"], pady=(0, SPACING["sm"]))

        self._start_queue_btn = ctk.CTkButton(
            control_bar, text=f"{ICONS['play']} Start Queue",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["success"], hover_color="#27ae60",
            height=36,
            command=self._on_start_queue,
        )
        self._start_queue_btn.pack(side="left", padx=SPACING["sm"], pady=SPACING["sm"])

        self._stop_queue_btn = ctk.CTkButton(
            control_bar, text=f"{ICONS['stop']} Stop (Esc)",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["error"],
            height=36, state="disabled",
            command=self._on_stop_queue,
        )
        self._stop_queue_btn.pack(side="left", padx=4, pady=SPACING["sm"])

        self._kill_resume_btn = ctk.CTkButton(
            control_bar, text="\u26A0 Kill (Ctrl+K)",
            font=("Segoe UI", 12, "bold"),
            fg_color="#c0392b", hover_color="#e74c3c",
            height=36, state="disabled",
            command=self._on_kill_all,
        )
        self._kill_resume_btn.pack(side="left", padx=4, pady=SPACING["sm"])

        self._resume_btn = ctk.CTkButton(
            control_bar, text="\u25B6 Resume (Ctrl+R)",
            font=("Segoe UI", 12, "bold"),
            fg_color="#2980b9", hover_color="#3498db",
            height=36, state="disabled",
            command=self._on_resume_all,
        )
        self._resume_btn.pack(side="left", padx=4, pady=SPACING["sm"])

        # ── AI Status indicator ──────────────────────────────────────
        self._ai_status_frame = ctk.CTkFrame(control_bar, fg_color="transparent")
        self._ai_status_frame.pack(side="left", padx=SPACING["sm"], pady=SPACING["sm"])

        self._ai_status_dot = ctk.CTkLabel(
            self._ai_status_frame, text="\u25CF",
            font=("Segoe UI", 16),
            text_color=c["fg_muted"],
        )
        self._ai_status_dot.pack(side="left", padx=(0, 4))

        self._ai_status_label = ctk.CTkLabel(
            self._ai_status_frame, text="Idle",
            font=("Segoe UI", 11, "bold"),
            text_color=c["fg_muted"],
        )
        self._ai_status_label.pack(side="left")

        ctk.CTkButton(
            control_bar, text="Clear Completed",
            font=("Segoe UI", 11),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], height=30,
            command=self._on_clear_completed,
        ).pack(side="right", padx=SPACING["sm"], pady=SPACING["sm"])

        ctk.CTkButton(
            control_bar, text=f"{ICONS['folder']} Load Tasks",
            font=("Segoe UI", 11),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], height=30,
            command=self._on_load_task_list,
        ).pack(side="right", padx=2, pady=SPACING["sm"])

        ctk.CTkButton(
            control_bar, text=f"{ICONS['save']} Save Tasks",
            font=("Segoe UI", 11),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], height=30,
            command=self._on_save_task_list,
        ).pack(side="right", padx=2, pady=SPACING["sm"])

        self._task_progress_bar = ctk.CTkProgressBar(
            control_bar, width=200, height=12,
            progress_color=c["accent"],
        )
        self._task_progress_bar.pack(side="right", padx=SPACING["sm"], pady=SPACING["sm"])
        self._task_progress_bar.set(0)

        self._task_progress_label = ctk.CTkLabel(
            control_bar, text="",
            font=("Segoe UI", 11),
            text_color=c["fg_muted"],
        )
        self._task_progress_label.pack(side="right", padx=4, pady=SPACING["sm"])

        paned = ctk.CTkFrame(frame, fg_color="transparent")
        paned.pack(fill="both", expand=True, padx=SPACING["lg"], pady=(0, SPACING["sm"]))

        left_panel = ctk.CTkFrame(paned, fg_color=c["bg_secondary"], corner_radius=8)
        left_panel.pack(fill="both", expand=True, side="left", padx=(0, SPACING["sm"]))

        ctk.CTkLabel(
            left_panel, text="Task Queue",
            font=("Segoe UI", 13, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["md"], pady=SPACING["sm"], anchor="w")

        self._task_list_frame = ctk.CTkScrollableFrame(
            left_panel, fg_color="transparent",
        )
        self._task_list_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        right_panel = ctk.CTkFrame(paned, fg_color=c["bg_secondary"], corner_radius=8, width=450)
        right_panel.pack(fill="both", expand=True, side="right")
        right_panel.pack_propagate(False)

        output_header = ctk.CTkFrame(right_panel, fg_color="transparent")
        output_header.pack(fill="x", padx=SPACING["md"], pady=SPACING["sm"])

        ctk.CTkLabel(
            output_header, text=f"{ICONS['code']} Output",
            font=("Segoe UI", 13, "bold"),
            text_color=c["fg_heading"],
        ).pack(side="left")

        ctk.CTkButton(
            output_header, text=f"{ICONS['copy']} Copy",
            font=("Segoe UI", 10), height=24, width=70,
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], border_color=c["border"], border_width=1,
            command=lambda: self._copy_to_clipboard(
                self._task_output.get("1.0", "end").strip()
            ),
        ).pack(side="right", padx=2)

        ctk.CTkButton(
            output_header, text=f"{ICONS['save']} Save",
            font=("Segoe UI", 10), height=24, width=70,
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], border_color=c["border"], border_width=1,
            command=lambda: self._save_code_to_file(
                self._task_output.get("1.0", "end").strip()
            ),
        ).pack(side="right", padx=2)

        self._task_output = ctk.CTkTextbox(
            right_panel, font=("Consolas", 11),
            fg_color=c["code_bg"], text_color=c["code_fg"],
            wrap="word",
        )
        self._task_output.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        RightClickMenu.bind(self._task_output, self)

    def _on_add_task(self) -> None:
        title = self._task_title_input.get().strip()
        if not title:
            self._toast("Enter a task title", "warning")
            return

        try:
            minutes = int(self._task_time_input.get().strip())
            if minutes < 1:
                raise ValueError
        except ValueError:
            self._toast("Minutes must be a positive number", "warning")
            return

        desc = self._task_desc_input.get("1.0", "end").strip()
        allow_ot = bool(self._task_allow_overtime.get())
        auto_improve = bool(self._task_auto_improve.get())

        task = CodingTask(
            title=title,
            description=desc or title,
            time_budget_minutes=minutes,
            allow_overtime=allow_ot,
            auto_improve=auto_improve,
        )
        self._task_queue.add_task(task)

        self._task_title_input.delete(0, "end")
        self._task_desc_input.delete("1.0", "end")
        self._refresh_task_list()
        self._toast(f"Added: {title} ({minutes}min)", "success")

    def _refresh_task_list(self) -> None:
        for w in self._task_list_frame.winfo_children():
            w.destroy()

        c = self._colors
        tasks = self._task_queue.tasks

        if not tasks:
            ctk.CTkLabel(
                self._task_list_frame,
                text="No tasks yet. Add one above!",
                font=("Segoe UI", 12),
                text_color=c["fg_muted"],
            ).pack(pady=SPACING["xl"])
            return

        for i, task in enumerate(tasks):
            self._render_task_card(task, i)

        pending = len(self._task_queue.pending_tasks)
        total_time = self._task_queue.total_time_remaining()
        self._queue_summary.configure(text=f"Queue: {len(tasks)} tasks ({pending} pending)")
        self._time_remaining.configure(text=f"Est: {total_time:.0f} min remaining")

    def _render_task_card(self, task: CodingTask, index: int) -> None:
        c = self._colors

        status_colors = {
            TaskStatus.PENDING: c["fg_muted"],
            TaskStatus.RUNNING: c["info"],
            TaskStatus.PAUSED: c["warning"],
            TaskStatus.COMPLETED: c["success"],
            TaskStatus.FAILED: c["error"],
            TaskStatus.OVERTIME: c["warning"],
            TaskStatus.CANCELLED: c["fg_muted"],
        }

        border_color = status_colors.get(task.status, c["border"])
        card = ctk.CTkFrame(
            self._task_list_frame, fg_color=c["bg_card"], corner_radius=6,
            border_color=border_color, border_width=2 if task.status == TaskStatus.RUNNING else 1,
        )
        card.pack(fill="x", pady=2)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=SPACING["sm"], pady=(SPACING["sm"], 2))

        ctk.CTkLabel(
            header, text=f"{index + 1}. {task.title}",
            font=("Segoe UI", 12, "bold"),
            text_color=c["fg_heading"],
        ).pack(side="left")

        status_text = task.status.value.upper()
        ctk.CTkLabel(
            header, text=status_text,
            font=("Segoe UI", 10, "bold"),
            text_color=status_colors.get(task.status, c["fg_muted"]),
        ).pack(side="right")

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.pack(fill="x", padx=SPACING["sm"], pady=(0, 2))

        elapsed = timedelta(seconds=int(task.elapsed_seconds))
        budget = timedelta(seconds=int(task.time_budget_seconds))
        improve_tag = "  \u2728 AI" if task.auto_improve else ""
        ctk.CTkLabel(
            info,
            text=f"{ICONS['clock']} {elapsed} / {budget}  |  Itr: {task.iterations_completed}{improve_tag}",
            font=("Segoe UI", 10),
            text_color=c["fg_muted"],
        ).pack(side="left")

        if task.status == TaskStatus.RUNNING:
            prog = ctk.CTkProgressBar(info, width=100, height=8, progress_color=c["accent"])
            prog.pack(side="right", padx=SPACING["sm"])
            prog.set(task.progress_fraction)

        if task.status == TaskStatus.PENDING:
            btn_frame = ctk.CTkFrame(card, fg_color="transparent")
            btn_frame.pack(fill="x", padx=SPACING["sm"], pady=(0, SPACING["sm"]))

            ctk.CTkButton(
                btn_frame, text=ICONS["up"], width=28, height=24,
                fg_color="transparent", hover_color=c["bg_hover"],
                command=lambda tid=task.id: self._move_task(tid, -1),
            ).pack(side="left", padx=1)

            ctk.CTkButton(
                btn_frame, text=ICONS["down"], width=28, height=24,
                fg_color="transparent", hover_color=c["bg_hover"],
                command=lambda tid=task.id: self._move_task(tid, 1),
            ).pack(side="left", padx=1)

            ctk.CTkButton(
                btn_frame, text=f"{ICONS['remove']} Remove", width=80, height=24,
                fg_color="transparent", hover_color=c["bg_hover"],
                text_color=c["error"],
                font=("Segoe UI", 10),
                command=lambda tid=task.id: self._remove_task(tid),
            ).pack(side="right", padx=1)

    def _move_task(self, task_id: str, direction: int) -> None:
        self._task_queue.move_task(task_id, direction)
        self._refresh_task_list()

    def _remove_task(self, task_id: str) -> None:
        self._task_queue.remove_task(task_id)
        self._refresh_task_list()

    def _on_start_queue(self) -> None:
        if not self._gemini.is_configured:
            self._toast("Set your API key first in Settings", "error")
            self._show_view("settings")
            return
        if not self._task_queue.pending_tasks:
            self._toast("No pending tasks in queue", "warning")
            return

        self._start_queue_btn.configure(state="disabled")
        self._stop_queue_btn.configure(state="normal")
        self._kill_resume_btn.configure(state="normal")
        self._resume_btn.configure(state="disabled")
        self._status_bar.set_status("Running task queue...", "info")
        self._task_executor.start()

    def _on_stop_queue(self) -> None:
        self._task_executor.stop()
        self._start_queue_btn.configure(state="normal")
        self._stop_queue_btn.configure(state="disabled")
        self._kill_resume_btn.configure(state="disabled")
        self._resume_btn.configure(state="disabled")
        self._status_bar.set_status("Queue stopped", "warning")

    def _on_kill_all(self) -> None:
        """Emergency kill - stops everything immediately."""
        if self._task_executor.is_running:
            self._task_executor.stop()
        self._gemini.cancel()

        self._start_queue_btn.configure(state="disabled")
        self._stop_queue_btn.configure(state="disabled")
        self._kill_resume_btn.configure(state="disabled")
        self._resume_btn.configure(state="normal")
        self._on_ai_status("error", "KILLED")
        self._status_bar.set_status("KILLED - All operations stopped", "error")
        self._toast("All operations killed!", "error")

    def _on_resume_all(self) -> None:
        """Resume after a kill - re-enables controls."""
        self._start_queue_btn.configure(state="normal")
        self._stop_queue_btn.configure(state="disabled")
        self._kill_resume_btn.configure(state="disabled")
        self._resume_btn.configure(state="disabled")
        self._on_ai_status("idle")
        self._status_bar.set_status("Ready - click Start Queue to go", "info")
        self._toast("Resumed - ready to go", "success")

    def _on_clear_completed(self) -> None:
        removed = self._task_queue.clear_completed()
        self._refresh_task_list()
        self._toast(f"Cleared {removed} completed tasks", "info")

    # ── SETTINGS VIEW ────────────────────────────────────────────

    def _open_url(self, url: str) -> None:
        """Open a URL in the user's default web browser."""
        try:
            webbrowser.open(url)
        except Exception as e:
            logger.error("Failed to open URL: %s", e)
            self._toast(f"Could not open browser: {e}", "error")

    def _make_link_button(self, parent, text: str, url: str) -> ctk.CTkButton:
        """Create a clickable link-styled button that opens a URL."""
        c = self._colors
        btn = ctk.CTkButton(
            parent, text=text,
            font=("Segoe UI", 12, "underline"),
            fg_color="transparent",
            hover_color=c["bg_hover"],
            text_color=c["info"],
            anchor="w", height=24,
            cursor="hand2",
            command=lambda: self._open_url(url),
        )
        return btn

    def _make_help_text(self, parent, text: str) -> ctk.CTkLabel:
        """Create a styled help/explanation label."""
        c = self._colors
        lbl = ctk.CTkLabel(
            parent, text=text,
            font=("Segoe UI", 11),
            text_color=c["fg_muted"],
            wraplength=650, justify="left", anchor="w",
        )
        return lbl

    def _make_section_header(self, parent, icon: str, title: str) -> ctk.CTkLabel:
        """Create a styled section header inside a card."""
        c = self._colors
        return ctk.CTkLabel(
            parent, text=f"{icon} {title}",
            font=("Segoe UI", 15, "bold"),
            text_color=c["fg_heading"],
        )

    def _build_settings_view(self) -> None:
        c = self._colors
        frame = ctk.CTkFrame(self._content, fg_color=c["bg_primary"], corner_radius=0)
        self._frames["settings"] = frame

        scroll = ctk.CTkScrollableFrame(frame, fg_color=c["bg_primary"])
        scroll.pack(fill="both", expand=True, padx=SPACING["lg"], pady=SPACING["md"])

        ctk.CTkLabel(
            scroll, text=f"{ICONS['gear']} Settings",
            font=("Segoe UI", 20, "bold"),
            text_color=c["fg_heading"],
        ).pack(anchor="w", pady=(0, 4))

        ctk.CTkLabel(
            scroll,
            text="Everything you need to get started. Each setting is explained below.",
            font=("Segoe UI", 12),
            text_color=c["fg_secondary"],
        ).pack(anchor="w", pady=(0, SPACING["md"]))

        # ── SETUP WIZARD CARD ────────────────────────────────────
        setup_card = ctk.CTkFrame(scroll, fg_color=c["bg_tertiary"], corner_radius=8,
                                   border_color=c["accent"], border_width=2)
        setup_card.pack(fill="x", pady=SPACING["sm"])

        self._make_section_header(setup_card, ICONS["rocket"], "Quick Setup Guide").pack(
            padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w"
        )

        steps_text = (
            "Follow these 3 steps and you'll be coding with AI in under 2 minutes:\n\n"
            "Step 1:  Click the link below to open Google AI Studio (it's free!)\n"
            "Step 2:  Sign in with your Google account, then click \"Create API Key\"\n"
            "Step 3:  Copy the key, paste it in the box below, and click \"Save & Test\""
        )
        ctk.CTkLabel(
            setup_card, text=steps_text,
            font=("Segoe UI", 12),
            text_color=c["fg_primary"],
            wraplength=650, justify="left", anchor="w",
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        link_row = ctk.CTkFrame(setup_card, fg_color="transparent")
        link_row.pack(fill="x", padx=SPACING["lg"], pady=(0, SPACING["md"]))

        api_url = "https://aistudio.google.com/apikey"
        self._make_link_button(
            link_row,
            text=f"{ICONS['key']} Open Google AI Studio  \u2192  aistudio.google.com/apikey",
            url=api_url,
        ).pack(side="left")

        ctk.CTkButton(
            link_row, text="Copy Link",
            font=("Segoe UI", 11),
            fg_color="transparent",
            hover_color=c["bg_hover"],
            text_color=c["fg_secondary"],
            height=24, width=80,
            command=lambda: self._copy_to_clipboard(api_url),
        ).pack(side="left", padx=SPACING["sm"])

        # ── API KEY CARD ─────────────────────────────────────────
        api_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        api_card.pack(fill="x", pady=SPACING["sm"])

        self._make_section_header(api_card, ICONS["key"], "API Key").pack(
            padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w"
        )

        self._make_help_text(api_card, (
            "What is an API key?  Think of it like a password that lets this app "
            "talk to Google's Gemini AI. It's a long string of letters and numbers "
            "that looks like \"AIzaSyD...\" -- Google gives you one for free.\n\n"
            "Is it safe?  Your key is saved only on YOUR computer, in a config file. "
            "It is never sent anywhere except to Google's own servers to use Gemini."
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        key_row = ctk.CTkFrame(api_card, fg_color="transparent")
        key_row.pack(fill="x", padx=SPACING["lg"], pady=(0, 4))

        self._api_key_input = ctk.CTkEntry(
            key_row, font=("Segoe UI", 13),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"], show="*",
            placeholder_text="Paste your API key here (starts with AIza...)",
        )
        self._api_key_input.pack(fill="x", side="left", expand=True, padx=(0, SPACING["sm"]))
        if self._cfg.api_key:
            self._api_key_input.insert(0, self._cfg.api_key)

        self._show_key_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            key_row, text="Show",
            variable=self._show_key_var,
            command=self._toggle_key_visibility,
            font=("Segoe UI", 11),
            text_color=c["fg_secondary"],
        ).pack(side="left", padx=4)

        btn_row_api = ctk.CTkFrame(api_card, fg_color="transparent")
        btn_row_api.pack(fill="x", padx=SPACING["lg"], pady=(4, 4))

        ctk.CTkButton(
            btn_row_api, text=f"{ICONS['check']} Save & Test Connection",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["accent"],
            hover_color=c["accent_hover"],
            height=38,
            command=self._on_save_api_key,
        ).pack(side="left")

        self._make_link_button(
            btn_row_api,
            text="Don't have a key? Get one free here",
            url="https://aistudio.google.com/apikey",
        ).pack(side="left", padx=SPACING["md"])

        self._api_status_label = ctk.CTkLabel(
            api_card, text="",
            font=("Segoe UI", 11),
            text_color=c["fg_muted"],
        )
        self._api_status_label.pack(padx=SPACING["lg"], pady=(0, SPACING["md"]), anchor="w")

        # ── MODEL SETTINGS CARD ──────────────────────────────────
        model_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        model_card.pack(fill="x", pady=SPACING["sm"])

        self._make_section_header(model_card, ICONS["code"], "AI Model").pack(
            padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w"
        )

        self._make_help_text(model_card, (
            "What is a model?  A \"model\" is which version of Google's AI brain you're using. "
            "It's like choosing between a quick calculator and a supercomputer -- "
            "faster models give shorter answers quicker, bigger models write better code but take longer."
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        # Model selector
        model_row = ctk.CTkFrame(model_card, fg_color="transparent")
        model_row.pack(fill="x", padx=SPACING["lg"], pady=SPACING["sm"])

        ctk.CTkLabel(model_row, text="Choose Model:", font=("Segoe UI", 12, "bold"),
                      text_color=c["fg_primary"]).pack(side="left")
        self._model_select = ctk.CTkComboBox(
            model_row, values=[
                "gemini-2.0-flash",
                "gemini-2.0-flash-lite",
                "gemini-2.5-flash-preview-05-20",
                "gemini-2.5-pro-preview-05-06",
            ],
            font=("Segoe UI", 12),
            fg_color=c["bg_input"],
            border_color=c["border"],
            width=280,
        )
        self._model_select.pack(side="left", padx=SPACING["sm"])
        self._model_select.set(self._cfg.model_name)

        model_help_frame = ctk.CTkFrame(model_card, fg_color=c["bg_primary"], corner_radius=6)
        model_help_frame.pack(fill="x", padx=SPACING["lg"], pady=SPACING["sm"])

        model_descriptions = (
            "  gemini-2.0-flash          -- Fast & free. Great for most tasks. START HERE.\n"
            "  gemini-2.0-flash-lite     -- Even faster, less detailed. Good for simple questions.\n"
            "  gemini-2.5-flash-preview  -- Newer brain, better at complex code. May be slower.\n"
            "  gemini-2.5-pro-preview    -- Most powerful. Best code quality but slowest & may cost $."
        )
        ctk.CTkLabel(
            model_help_frame, text=model_descriptions,
            font=("Consolas", 11),
            text_color=c["fg_secondary"],
            justify="left", anchor="w",
        ).pack(padx=SPACING["sm"], pady=SPACING["sm"], anchor="w")

        self._make_help_text(model_card, (
            "Recommendation: Start with \"gemini-2.0-flash\". "
            "It's fast, free, and handles most coding tasks well. "
            "Only switch to a bigger model if the code quality isn't good enough."
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        # Temperature
        temp_card_inner = ctk.CTkFrame(model_card, fg_color="transparent")
        temp_card_inner.pack(fill="x", padx=SPACING["lg"], pady=SPACING["sm"])

        ctk.CTkLabel(temp_card_inner, text="Creativity Level (Temperature):",
                      font=("Segoe UI", 12, "bold"),
                      text_color=c["fg_primary"]).pack(anchor="w")

        self._make_help_text(temp_card_inner, (
            "This controls how \"creative\" vs. \"predictable\" the AI is. "
            "Think of it like a dial between a careful accountant (low = 0.0) "
            "and a wild artist (high = 2.0).\n\n"
            "  0.0 - 0.3  =  Very precise, same answer every time. Best for math & bug fixes.\n"
            "  0.4 - 0.8  =  Balanced. Good for most coding. (0.7 is the sweet spot)\n"
            "  0.9 - 2.0  =  Very creative, surprising answers. Fun for brainstorming, risky for code."
        )).pack(anchor="w", pady=(2, SPACING["sm"]))

        temp_row = ctk.CTkFrame(temp_card_inner, fg_color="transparent")
        temp_row.pack(fill="x")

        ctk.CTkLabel(temp_row, text="Precise", font=("Segoe UI", 10),
                      text_color=c["fg_muted"]).pack(side="left")
        self._temp_slider = ctk.CTkSlider(
            temp_row, from_=0.0, to=2.0,
            number_of_steps=20,
        )
        self._temp_slider.pack(side="left", padx=SPACING["sm"], expand=True, fill="x")
        self._temp_slider.set(self._cfg.temperature)
        self._temp_label = ctk.CTkLabel(
            temp_row, text=f"{self._cfg.temperature:.1f}",
            font=("Segoe UI", 14, "bold"), text_color=c["accent"],
            width=40,
        )
        self._temp_label.pack(side="left", padx=4)
        ctk.CTkLabel(temp_row, text="Creative", font=("Segoe UI", 10),
                      text_color=c["fg_muted"]).pack(side="left")
        self._temp_slider.configure(command=lambda v: self._temp_label.configure(text=f"{v:.1f}"))

        # Max Tokens
        tokens_frame = ctk.CTkFrame(model_card, fg_color="transparent")
        tokens_frame.pack(fill="x", padx=SPACING["lg"], pady=SPACING["sm"])

        ctk.CTkLabel(tokens_frame, text="Maximum Response Length (Tokens):",
                      font=("Segoe UI", 12, "bold"),
                      text_color=c["fg_primary"]).pack(anchor="w")

        self._make_help_text(tokens_frame, (
            "What are tokens?  The AI reads and writes in chunks called \"tokens\". "
            "One token is roughly 3/4 of a word. So 8192 tokens is about 6000 words "
            "-- enough for a full program file.\n\n"
            "  1024   =  Short answers only (a few paragraphs)\n"
            "  8192   =  Good for most code files (DEFAULT - recommended)\n"
            "  32768  =  Very long responses (whole applications)\n"
            "  65536  =  Maximum length (use only when you need huge files)"
        )).pack(anchor="w", pady=(2, SPACING["sm"]))

        tokens_row = ctk.CTkFrame(tokens_frame, fg_color="transparent")
        tokens_row.pack(fill="x")

        self._tokens_input = ctk.CTkEntry(
            tokens_row, width=100, font=("Segoe UI", 13),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._tokens_input.pack(side="left")
        self._tokens_input.insert(0, str(self._cfg.max_tokens))

        ctk.CTkLabel(tokens_row, text="tokens",
                      font=("Segoe UI", 11), text_color=c["fg_muted"]).pack(side="left", padx=4)

        for preset_name, preset_val in [("Short (1K)", 1024), ("Normal (8K)", 8192),
                                         ("Long (32K)", 32768), ("Max (64K)", 65536)]:
            ctk.CTkButton(
                tokens_row, text=preset_name,
                font=("Segoe UI", 10), height=24, width=80,
                fg_color="transparent", hover_color=c["bg_hover"],
                text_color=c["fg_secondary"], border_color=c["border"], border_width=1,
                command=lambda v=preset_val: (
                    self._tokens_input.delete(0, "end"),
                    self._tokens_input.insert(0, str(v)),
                ),
            ).pack(side="left", padx=2)

        # Save button for model settings
        ctk.CTkButton(
            model_card, text=f"{ICONS['save']} Save Model Settings",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["accent_secondary"],
            height=38,
            command=self._on_save_model_settings,
        ).pack(padx=SPACING["lg"], pady=SPACING["md"], anchor="w")

        # ── TASK DEFAULTS CARD ───────────────────────────────────
        task_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        task_card.pack(fill="x", pady=SPACING["sm"])

        self._make_section_header(task_card, ICONS["tasks"], "Task Queue Defaults").pack(
            padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w"
        )

        self._make_help_text(task_card, (
            "These settings control the Task Queue mode -- where you give the AI "
            "a list of coding jobs and it works through them one by one, like a to-do list.\n\n"
            "What is a time budget?  Each task gets a timer. When the timer runs out, "
            "the AI finishes what it's doing and moves to the next task. This keeps it "
            "from spending all day on one thing."
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        task_row1 = ctk.CTkFrame(task_card, fg_color="transparent")
        task_row1.pack(fill="x", padx=SPACING["lg"], pady=SPACING["sm"])

        ctk.CTkLabel(task_row1, text="Default time per task:",
                      font=("Segoe UI", 12, "bold"),
                      text_color=c["fg_primary"]).pack(side="left")

        self._default_time_input = ctk.CTkEntry(
            task_row1, width=60, font=("Segoe UI", 13),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._default_time_input.pack(side="left", padx=SPACING["sm"])
        self._default_time_input.insert(0, str(self._cfg.task_default_minutes))

        ctk.CTkLabel(task_row1, text="minutes",
                      font=("Segoe UI", 11), text_color=c["fg_muted"]).pack(side="left")

        self._make_help_text(task_card, (
            "  5 min   =  Quick fixes, small functions\n"
            " 30 min   =  A full feature or module (DEFAULT)\n"
            " 60 min   =  A complex system with multiple parts\n"
            "You can always change the time for individual tasks when you add them."
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        # Expand depth
        depth_row = ctk.CTkFrame(task_card, fg_color="transparent")
        depth_row.pack(fill="x", padx=SPACING["lg"], pady=SPACING["sm"])

        ctk.CTkLabel(depth_row, text="Explore Mode depth limit:",
                      font=("Segoe UI", 12, "bold"),
                      text_color=c["fg_primary"]).pack(side="left")

        self._depth_input = ctk.CTkEntry(
            depth_row, width=60, font=("Segoe UI", 13),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._depth_input.pack(side="left", padx=SPACING["sm"])
        self._depth_input.insert(0, str(self._cfg.expand_depth_limit))

        ctk.CTkLabel(depth_row, text="levels deep",
                      font=("Segoe UI", 11), text_color=c["fg_muted"]).pack(side="left")

        self._make_help_text(task_card, (
            "In Explore Mode, each time you click an option it goes one level deeper. "
            "This limits how many times you can drill down. "
            "10 is plenty for most ideas. Set it higher if you want to explore further."
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        ctk.CTkButton(
            task_card, text=f"{ICONS['save']} Save Task Settings",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["accent_secondary"],
            height=38,
            command=self._on_save_task_settings,
        ).pack(padx=SPACING["lg"], pady=SPACING["md"], anchor="w")

        # ── APPEARANCE CARD ──────────────────────────────────────
        appearance_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        appearance_card.pack(fill="x", pady=SPACING["sm"])

        self._make_section_header(appearance_card, "\U0001F3A8", "Appearance").pack(
            padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w"
        )

        self._make_help_text(appearance_card, (
            "Customize how the app looks. Dark mode is easier on your eyes "
            "during late-night coding sessions. Light mode is better in bright rooms."
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        theme_row = ctk.CTkFrame(appearance_card, fg_color="transparent")
        theme_row.pack(fill="x", padx=SPACING["lg"], pady=SPACING["sm"])

        ctk.CTkLabel(theme_row, text="Theme:", font=("Segoe UI", 12, "bold"),
                      text_color=c["fg_primary"]).pack(side="left")

        self._theme_select = ctk.CTkSegmentedButton(
            theme_row, values=["Dark", "Light"],
            font=("Segoe UI", 12),
            command=self._on_theme_select,
        )
        self._theme_select.pack(side="left", padx=SPACING["sm"])
        self._theme_select.set("Dark" if self._cfg.theme == "dark" else "Light")

        auto_scroll_row = ctk.CTkFrame(appearance_card, fg_color="transparent")
        auto_scroll_row.pack(fill="x", padx=SPACING["lg"], pady=SPACING["sm"])

        self._auto_scroll_var = ctk.BooleanVar(value=self._cfg.auto_scroll)
        ctk.CTkCheckBox(
            auto_scroll_row, text="Auto-scroll output to bottom",
            variable=self._auto_scroll_var,
            font=("Segoe UI", 12),
            text_color=c["fg_primary"],
            command=lambda: self._config_manager.update(auto_scroll=self._auto_scroll_var.get()),
        ).pack(side="left")

        self._make_help_text(appearance_card, (
            "When checked, the code output window automatically scrolls down "
            "as new text appears -- so you always see the latest line being written."
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        self._timestamps_var = ctk.BooleanVar(value=self._cfg.show_timestamps)
        ctk.CTkCheckBox(
            appearance_card, text="Show timestamps in output",
            variable=self._timestamps_var,
            font=("Segoe UI", 12),
            text_color=c["fg_primary"],
            command=lambda: self._config_manager.update(show_timestamps=self._timestamps_var.get()),
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["md"]), anchor="w")

        # ── HELP & LINKS CARD ────────────────────────────────────
        help_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        help_card.pack(fill="x", pady=SPACING["sm"])

        self._make_section_header(help_card, ICONS["info"], "Helpful Links").pack(
            padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w"
        )

        self._make_help_text(help_card, (
            "These links open in your web browser. Bookmark them if you find them useful!"
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        links = [
            (f"{ICONS['key']} Get your free Gemini API key",
             "https://aistudio.google.com/apikey"),
            (f"{ICONS['code']} Gemini API documentation (for nerds)",
             "https://ai.google.dev/gemini-api/docs"),
            (f"{ICONS['star']} See available Gemini models & pricing",
             "https://ai.google.dev/gemini-api/docs/models"),
            (f"{ICONS['info']} What is an API? (beginner guide)",
             "https://en.wikipedia.org/wiki/API"),
        ]

        for link_text, link_url in links:
            self._make_link_button(help_card, link_text, link_url).pack(
                padx=SPACING["lg"], pady=1, anchor="w"
            )

        ctk.CTkFrame(help_card, height=SPACING["md"], fg_color="transparent").pack()

        # ── KEYBOARD SHORTCUTS CARD ──────────────────────────────
        kb_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        kb_card.pack(fill="x", pady=SPACING["sm"])

        self._make_section_header(kb_card, "\u2328", "Keyboard Shortcuts").pack(
            padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w"
        )

        self._make_help_text(kb_card, (
            "You can use these key combinations instead of clicking buttons. "
            "Hold the Ctrl key and press the other key at the same time."
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        shortcuts_text = (
            "  Ctrl + 1    Switch to Explore Mode\n"
            "  Ctrl + 2    Switch to Task Queue\n"
            "  Ctrl + 3    Switch to Settings\n"
            "  Ctrl + 4    Switch to Diagnostics\n"
            "  Ctrl + S    Save current settings\n"
            "  Ctrl + Q    Quit the application"
        )
        ctk.CTkLabel(
            kb_card, text=shortcuts_text,
            font=("Consolas", 12),
            text_color=c["fg_secondary"],
            justify="left", anchor="w",
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["md"]), anchor="w")

        # ── DANGER ZONE CARD ─────────────────────────────────────
        danger_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8,
                                    border_color=c["error"], border_width=1)
        danger_card.pack(fill="x", pady=SPACING["sm"])

        self._make_section_header(danger_card, ICONS["warning"], "Reset").pack(
            padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w"
        )

        self._make_help_text(danger_card, (
            "This resets ALL settings back to their original defaults. "
            "Your API key will be kept, but everything else goes back to how it was "
            "when you first opened the app. You usually don't need this."
        )).pack(padx=SPACING["lg"], pady=(0, SPACING["sm"]), anchor="w")

        ctk.CTkButton(
            danger_card, text="Reset All Settings to Defaults",
            font=("Segoe UI", 12),
            fg_color="transparent",
            hover_color=c["bg_hover"],
            text_color=c["error"],
            border_color=c["error"], border_width=1,
            height=32,
            command=self._on_reset_settings,
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["md"]), anchor="w")

    def _on_theme_select(self, choice: str) -> None:
        """Handle theme segmented button change."""
        new_theme = choice.lower()
        self._config_manager.update(theme=new_theme)
        ctk.set_appearance_mode(new_theme)
        self._colors = get_colors(new_theme)
        self._toast(f"Theme changed to {choice}", "info")

    def _on_save_task_settings(self) -> None:
        """Save task queue and explore settings."""
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
            task_default_minutes=default_min,
            expand_depth_limit=depth,
        )
        self._expander._depth_limit = depth
        self._toast("Task settings saved!", "success")

    def _on_reset_settings(self) -> None:
        """Reset all settings to defaults after confirmation."""
        if messagebox.askyesno(
            "Reset Settings",
            "Reset all settings to defaults?\n\nYour API key will be kept."
        ):
            self._config_manager.reset_to_defaults()
            self._toast("Settings reset to defaults. Restart for full effect.", "info")

    def _toggle_key_visibility(self) -> None:
        self._api_key_input.configure(show="" if self._show_key_var.get() else "*")

    def _on_save_api_key(self) -> None:
        key = self._api_key_input.get().strip()
        if not key:
            self._toast("Enter an API key", "warning")
            return

        self._api_status_label.configure(text="Testing connection...", text_color=self._colors["info"])
        self.update_idletasks()

        def worker():
            self._gemini.configure(key)
            ok, msg = self._gemini.test_connection()
            if ok:
                self._config_manager.update(api_key=key)
                self.after(0, lambda: self._api_status_label.configure(
                    text=f"{ICONS['check']} {msg}", text_color=self._colors["success"]
                ))
                self.after(0, lambda: self._connection_indicator.configure(
                    text=f"\u25CF Connected", text_color=self._colors["success"]
                ))
                self.after(0, lambda: self._toast("API key saved and verified!", "success"))
            else:
                self.after(0, lambda: self._api_status_label.configure(
                    text=f"{ICONS['cross']} {msg}", text_color=self._colors["error"]
                ))

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_model_settings(self) -> None:
        model = self._model_select.get()
        temp = self._temp_slider.get()
        try:
            tokens = int(self._tokens_input.get().strip())
        except ValueError:
            self._toast("Max tokens must be a number", "warning")
            return

        self._config_manager.update(
            model_name=model, temperature=temp, max_tokens=tokens
        )
        self._gemini.update_settings(
            model_name=model, temperature=temp, max_tokens=tokens
        )
        self._status_bar.set_model(model)
        self._toast("Model settings saved", "success")

    def _show_api_key_prompt(self) -> None:
        self._show_view("settings")
        self._toast("Welcome! Follow the Quick Setup Guide to get started.", "info")

    # ── DIAGNOSTICS VIEW ─────────────────────────────────────────

    # ── HISTORY VIEW ──────────────────────────────────────────────

    def _build_history_view(self) -> None:
        c = self._colors
        frame = ctk.CTkFrame(self._content, fg_color=c["bg_primary"], corner_radius=0)
        self._frames["history"] = frame

        top = ctk.CTkFrame(frame, fg_color=c["bg_secondary"], corner_radius=8)
        top.pack(fill="x", padx=SPACING["lg"], pady=SPACING["md"])

        ctk.CTkLabel(
            top, text=f"{ICONS['folder']} History - Past Sessions & Results",
            font=("Segoe UI", 16, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")

        toolbar = ctk.CTkFrame(top, fg_color="transparent")
        toolbar.pack(fill="x", padx=SPACING["lg"], pady=(0, SPACING["md"]))

        self._history_search = ctk.CTkEntry(
            toolbar, font=("Segoe UI", 12),
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
            placeholder_text="Search history...",
            width=300,
        )
        self._history_search.pack(side="left")
        self._history_search.bind("<Return>", lambda e: self._refresh_history())
        RightClickMenu.bind(self._history_search, self)

        ctk.CTkButton(
            toolbar, text=f"{ICONS['diagnostics']} Search",
            font=("Segoe UI", 12),
            fg_color=c["accent"], hover_color=c["accent_hover"],
            height=32,
            command=self._refresh_history,
        ).pack(side="left", padx=SPACING["sm"])

        self._history_filter = ctk.CTkSegmentedButton(
            toolbar, values=["All", "Tasks", "Expansions", "Code"],
            font=("Segoe UI", 11),
            command=lambda v: self._refresh_history(),
        )
        self._history_filter.pack(side="left", padx=SPACING["sm"])
        self._history_filter.set("All")

        ctk.CTkButton(
            toolbar, text="Clear All History",
            font=("Segoe UI", 10),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["error"], height=28,
            command=self._on_clear_history,
        ).pack(side="right")

        ctk.CTkButton(
            toolbar, text=f"{ICONS['refresh']} Refresh",
            font=("Segoe UI", 11),
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], height=28,
            command=self._refresh_history,
        ).pack(side="right", padx=4)

        paned = ctk.CTkFrame(frame, fg_color="transparent")
        paned.pack(fill="both", expand=True, padx=SPACING["lg"], pady=(0, SPACING["sm"]))

        left = ctk.CTkScrollableFrame(paned, fg_color=c["bg_secondary"], corner_radius=8)
        left.pack(fill="both", expand=True, side="left", padx=(0, SPACING["sm"]))
        self._history_list_frame = left

        right = ctk.CTkFrame(paned, fg_color=c["bg_secondary"], corner_radius=8, width=500)
        right.pack(fill="both", expand=True, side="right")
        right.pack_propagate(False)

        detail_header = ctk.CTkFrame(right, fg_color="transparent")
        detail_header.pack(fill="x", padx=SPACING["md"], pady=SPACING["sm"])

        self._history_detail_title = ctk.CTkLabel(
            detail_header, text="Select an item from the list",
            font=("Segoe UI", 13, "bold"),
            text_color=c["fg_heading"],
        )
        self._history_detail_title.pack(side="left")

        ctk.CTkButton(
            detail_header, text=f"{ICONS['copy']} Copy",
            font=("Segoe UI", 10), height=24, width=70,
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], border_color=c["border"], border_width=1,
            command=lambda: self._copy_to_clipboard(
                self._history_detail.get("1.0", "end").strip()
            ),
        ).pack(side="right", padx=2)

        ctk.CTkButton(
            detail_header, text=f"{ICONS['save']} Save",
            font=("Segoe UI", 10), height=24, width=70,
            fg_color="transparent", hover_color=c["bg_hover"],
            text_color=c["fg_secondary"], border_color=c["border"], border_width=1,
            command=lambda: self._save_code_to_file(
                self._history_detail.get("1.0", "end").strip()
            ),
        ).pack(side="right", padx=2)

        self._history_detail = ctk.CTkTextbox(
            right, font=("Consolas", 11),
            fg_color=c["code_bg"], text_color=c["code_fg"],
            wrap="word",
        )
        self._history_detail.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        RightClickMenu.bind(self._history_detail, self)

    def _refresh_history(self) -> None:
        for w in self._history_list_frame.winfo_children():
            w.destroy()

        c = self._colors
        query = self._history_search.get().strip()
        filter_type = self._history_filter.get()

        type_map = {
            "All": None,
            "Tasks": "task",
            "Expansions": "expansion",
            "Code": "code_generation",
        }
        entry_type = type_map.get(filter_type)

        if query:
            entries = self._history.search(query)
        elif entry_type:
            entries = self._history.get_by_type(entry_type)
        else:
            entries = self._history.entries

        if not entries:
            ctk.CTkLabel(
                self._history_list_frame,
                text="No history yet. Use Explore Mode or Task Queue to generate code!",
                font=("Segoe UI", 12),
                text_color=c["fg_muted"],
                wraplength=350,
            ).pack(pady=SPACING["xl"])
            return

        for entry in entries[:100]:
            self._render_history_card(entry)

    def _render_history_card(self, entry: HistoryEntry) -> None:
        c = self._colors
        type_icons = {
            "task": ICONS["tasks"],
            "expansion": ICONS["expand"],
            "code_generation": ICONS["code"],
        }
        type_colors = {
            "task": c["info"],
            "expansion": c["accent_secondary"],
            "code_generation": c["success"],
        }

        card = ctk.CTkFrame(
            self._history_list_frame, fg_color=c["bg_card"], corner_radius=6,
            cursor="hand2",
        )
        card.pack(fill="x", pady=2)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=SPACING["sm"], pady=(SPACING["sm"], 2))

        icon = type_icons.get(entry.entry_type, ICONS["code"])
        ctk.CTkLabel(
            header, text=f"{icon} {entry.title[:50]}",
            font=("Segoe UI", 12, "bold"),
            text_color=c["fg_heading"],
        ).pack(side="left")

        ctk.CTkLabel(
            header, text=entry.time_str,
            font=("Segoe UI", 10),
            text_color=c["fg_muted"],
        ).pack(side="right")

        type_label = entry.entry_type.replace("_", " ").title()
        tcolor = type_colors.get(entry.entry_type, c["fg_muted"])
        ctk.CTkLabel(
            card, text=f"{type_label} | {entry.status}",
            font=("Segoe UI", 10),
            text_color=tcolor,
        ).pack(padx=SPACING["sm"], anchor="w")

        if entry.short_preview:
            ctk.CTkLabel(
                card, text=entry.short_preview,
                font=("Segoe UI", 10),
                text_color=c["fg_muted"],
                wraplength=350, justify="left", anchor="w",
            ).pack(padx=SPACING["sm"], pady=(0, SPACING["sm"]), anchor="w")

        for widget in [card, header]:
            widget.bind("<Button-1>", lambda e, eid=entry.id: self._show_history_detail(eid))

    def _show_history_detail(self, entry_id: str) -> None:
        entry = self._history.get(entry_id)
        if not entry:
            return

        self._history_detail_title.configure(text=entry.title)
        self._history_detail.delete("1.0", "end")

        content = f"Date: {entry.time_str}\n"
        content += f"Type: {entry.entry_type}\n"
        content += f"Status: {entry.status}\n"
        if entry.elapsed_seconds:
            content += f"Duration: {timedelta(seconds=int(entry.elapsed_seconds))}\n"
        content += f"\n{'='*60}\nPROMPT:\n{'='*60}\n{entry.prompt}\n"
        content += f"\n{'='*60}\nRESPONSE:\n{'='*60}\n{entry.response}\n"

        self._history_detail.insert("1.0", content)

    def _on_clear_history(self) -> None:
        if messagebox.askyesno("Clear History", "Delete all history entries? This cannot be undone."):
            count = self._history.clear_all()
            self._refresh_history()
            self._toast(f"Cleared {count} history entries", "info")

    # ── TASK SAVE/LOAD ───────────────────────────────────────────

    def _on_save_task_list(self) -> None:
        tasks = self._task_queue.tasks
        if not tasks:
            self._toast("No tasks to save", "warning")
            return
        filepath = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Save Task List",
        )
        if filepath:
            try:
                data = [t.to_dict() for t in tasks]
                Path(filepath).write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                self._toast(f"Saved {len(tasks)} tasks to {Path(filepath).name}", "success")
            except OSError as e:
                self._toast(f"Failed to save: {e}", "error")

    def _on_load_task_list(self) -> None:
        filepath = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Load Task List",
        )
        if filepath:
            try:
                data = json.loads(Path(filepath).read_text(encoding="utf-8"))
                loaded = 0
                for item in data:
                    task = CodingTask.from_dict(item)
                    task.status = TaskStatus.PENDING
                    task.elapsed_seconds = 0
                    task.started_at = None
                    task.completed_at = None
                    self._task_queue.add_task(task)
                    loaded += 1
                self._refresh_task_list()
                self._toast(f"Loaded {loaded} tasks from {Path(filepath).name}", "success")
            except (json.JSONDecodeError, OSError) as e:
                self._toast(f"Failed to load: {e}", "error")

    # ── DIAGNOSTICS VIEW ─────────────────────────────────────────

    def _build_diagnostics_view(self) -> None:
        c = self._colors
        frame = ctk.CTkFrame(self._content, fg_color=c["bg_primary"], corner_radius=0)
        self._frames["diagnostics"] = frame

        ctk.CTkLabel(
            frame, text=f"{ICONS['diagnostics']} System Diagnostics",
            font=("Segoe UI", 18, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=SPACING["md"], anchor="w")

        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=SPACING["lg"])

        ctk.CTkButton(
            btn_row, text=f"{ICONS['save']} Generate Report to Desktop",
            font=("Segoe UI", 13, "bold"),
            fg_color=c["accent"],
            hover_color=c["accent_hover"],
            height=40,
            command=self._on_generate_diagnostic,
        ).pack(side="left", padx=(0, SPACING["sm"]))

        ctk.CTkButton(
            btn_row, text=f"{ICONS['copy']} Copy to Clipboard",
            font=("Segoe UI", 12),
            fg_color=c["accent_secondary"],
            height=36,
            command=self._on_copy_diagnostic,
        ).pack(side="left")

        self._diag_output = ctk.CTkTextbox(
            frame, font=("Consolas", 11),
            fg_color=c["code_bg"], text_color=c["code_fg"],
            wrap="word",
        )
        self._diag_output.pack(
            fill="both", expand=True,
            padx=SPACING["lg"], pady=SPACING["md"],
        )
        RightClickMenu.bind(self._diag_output, self)

    def _generate_diagnostic_text(self) -> str:
        from ..diagnostics import generate_diagnostic_report
        return generate_diagnostic_report(
            platform_info=self._platform,
            config=self._cfg,
            gemini_configured=self._gemini.is_configured,
            task_queue=self._task_queue,
            uptime_seconds=time.time() - self._start_time,
        )

    def _on_generate_diagnostic(self) -> None:
        report = self._generate_diagnostic_text()
        self._diag_output.delete("1.0", "end")
        self._diag_output.insert("1.0", report)

        desktop = get_desktop_path()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        filename = f"gemini_coder_diagnostic_{timestamp}.txt"
        filepath = desktop / filename

        try:
            filepath.write_text(report, encoding="utf-8")
            self._toast(f"Report saved: {filename}", "success")
            self._status_bar.set_status(f"Diagnostic saved to {filepath}", "success")
        except OSError as e:
            self._toast(f"Failed to save: {e}", "error")

    def _on_copy_diagnostic(self) -> None:
        report = self._generate_diagnostic_text()
        self._diag_output.delete("1.0", "end")
        self._diag_output.insert("1.0", report)
        self._copy_to_clipboard(report)

    # ── COMMON UTILITIES ─────────────────────────────────────────

    def _setup_callbacks(self) -> None:
        self._task_executor.set_callbacks(
            on_output=self._on_task_output,
            on_task_start=self._on_task_started,
            on_task_complete=self._on_task_completed,
            on_tick=self._on_task_tick,
            on_status=self._on_ai_status,
        )
        self._task_queue.on_change(lambda: self.after(0, self._refresh_task_list))

    def _on_task_output(self, kind: str, text: str) -> None:
        def update():
            self._task_output.delete("1.0", "end")
            self._task_output.insert("1.0", text)
            if self._cfg.auto_scroll:
                self._task_output.see("end")
        self.after(0, update)

    def _on_task_started(self, task: CodingTask) -> None:
        self.after(0, lambda: self._status_bar.set_status(
            f"Working on: {task.title}", "info"
        ))
        self.after(0, self._refresh_task_list)

    def _on_task_completed(self, task: CodingTask) -> None:
        level = "success" if task.status == TaskStatus.COMPLETED else "error"
        msg = f"Completed: {task.title}" if task.status == TaskStatus.COMPLETED else f"Failed: {task.title}"
        self.after(0, lambda: self._toast(msg, level))
        self.after(0, lambda: self._status_bar.set_status(msg, level))
        self.after(0, self._refresh_task_list)

        if task.status == TaskStatus.COMPLETED and task.output_code:
            self._last_completed_task = task

        self._history.add(HistoryEntry(
            entry_type="task",
            title=task.title,
            prompt=task.description,
            response=task.output_code,
            model=self._cfg.model_name,
            elapsed_seconds=task.elapsed_seconds,
            status=task.status.value,
        ))

        if not self._task_executor.is_running:
            self.after(0, lambda: self._start_queue_btn.configure(state="normal"))
            self.after(0, lambda: self._stop_queue_btn.configure(state="disabled"))
            self.after(0, lambda: self._kill_resume_btn.configure(state="disabled"))
            self.after(0, lambda: self._resume_btn.configure(state="disabled"))

    def _on_task_tick(self, task: CodingTask) -> None:
        def update():
            elapsed = timedelta(seconds=int(task.elapsed_seconds))
            budget = timedelta(seconds=int(task.time_budget_seconds))
            remaining = max(0, task.time_budget_seconds - task.elapsed_seconds)
            self._task_progress_bar.set(task.progress_fraction)
            self._task_progress_label.configure(
                text=f"{elapsed} / {budget} ({int(remaining)}s left)"
            )
        self.after(0, update)

    # ── AI Status indicator ────────────────────────────────────

    def _on_ai_status(self, status: str, detail: str = "") -> None:
        """Update the AI status dot and label in the control bar."""
        from .theme import get_colors
        c = self._colors

        status_styles = {
            "working":   ("\u25CF", c["success"],  "Working"),
            "thinking":  ("\u25CF", c["warning"],  "Thinking"),
            "improving": ("\u2728", "#8e44ad",     "Improving"),
            "idle":      ("\u25CF", c["fg_muted"], "Idle"),
            "error":     ("\u25CF", c["error"],    "Error"),
        }

        dot, color, label = status_styles.get(status, status_styles["idle"])
        display = f"{label}: {detail}" if detail else label

        def update():
            self._ai_status_dot.configure(text=dot, text_color=color)
            self._ai_status_label.configure(text=display, text_color=color)
        self.after(0, update)

    # ── Save output ──────────────────────────────────────────

    def _on_save_output(self) -> None:
        """Save the current task output to a file."""
        code = self._task_output.get("1.0", "end").strip()
        if not code:
            self._toast("No output to save", "warning")
            return
        self._save_code_to_file(code)

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-Key-1>", lambda e: self._show_view("expand"))
        self.bind("<Control-Key-2>", lambda e: self._show_view("tasks"))
        self.bind("<Control-Key-3>", lambda e: self._show_view("history"))
        self.bind("<Control-Key-4>", lambda e: self._show_view("settings"))
        self.bind("<Control-Key-5>", lambda e: self._show_view("diagnostics"))
        self.bind("<Control-q>", lambda e: self._on_close())
        self.bind("<Control-s>", lambda e: self._config_manager.save())
        self.bind("<Control-k>", lambda e: self._on_kill_all())     # Kill everything
        self.bind("<Control-r>", lambda e: self._on_resume_all())   # Resume after kill
        self.bind("<Escape>", lambda e: self._on_stop_queue() if self._task_executor.is_running else None)

    def _start_clock(self) -> None:
        def update():
            uptime = timedelta(seconds=int(time.time() - self._start_time))
            self._status_bar.set_time(f"Uptime: {uptime}")
            self.after(1000, update)
        update()

    def _toggle_theme(self) -> None:
        new_theme = "light" if self._cfg.theme == "dark" else "dark"
        self._config_manager.update(theme=new_theme)
        ctk.set_appearance_mode(new_theme)
        self._colors = get_colors(new_theme)
        self._toast(f"Theme: {new_theme}", "info")
        if hasattr(self, "_theme_select"):
            self._theme_select.set("Dark" if new_theme == "dark" else "Light")

    def _copy_to_clipboard(self, text: str) -> None:
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._toast("Copied to clipboard!", "success")

    def _save_code_to_file(self, code: str) -> None:
        filepath = filedialog.asksaveasfilename(
            defaultextension=".py",
            filetypes=[
                ("Python", "*.py"), ("JavaScript", "*.js"),
                ("TypeScript", "*.ts"), ("All files", "*.*"),
            ],
        )
        if filepath:
            Path(filepath).write_text(code, encoding="utf-8")
            self._toast(f"Saved to {Path(filepath).name}", "success")

    def _toast(self, message: str, level: str = "info") -> None:
        ToastNotification(self, message, level, colors=self._colors)
        logger.log(
            getattr(logging, level.upper(), logging.INFO),
            "Toast: %s", message,
        )

    def _on_close(self) -> None:
        if self._task_executor.is_running:
            if not messagebox.askyesno(
                "Quit", "Tasks are still running. Quit anyway?"
            ):
                return
            self._task_executor.stop()

        try:
            self._config_manager.update(
                window_width=self.winfo_width(),
                window_height=self.winfo_height(),
            )
        except Exception:
            pass

        self.destroy()
