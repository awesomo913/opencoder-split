"""Autocoder UI that talks only to the local OpenCode HTTP API (no CDP, Chrome, or fleet)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import customtkinter as ctk
from tkinter import filedialog

from gemini_coder.ui.theme import SPACING, ICONS

from ..opencode_bridge import (
    OpenCodeClient,
    OpenCodeHTTPError,
    ensure_opencode_serve,
    iter_sessions_from_sqlite,
)
from .app_web import AutocoderApp

logger = logging.getLogger(__name__)

_STATE_FILE = Path.home() / ".autocoder" / "opencode_autocoder_state.json"
_OC_CORNER = "bottom-right"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
    return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not write %s: %s", path, exc)


class OpenCodeAutocoderApp(AutocoderApp):
    """Same broadcast / task / profile tooling as Autocoder; transport is OpenCode HTTP only."""

    _enable_fleet = False

    def __init__(self) -> None:
        self._oc_id_from_display: dict[str, str] = {}
        self._oc_serve_proc: subprocess.Popen | None = None
        super().__init__()
        self.title(f"Autocoder (OpenCode) — local HTTP")

    def _oc_btn_kw(self, kind: str = "action", *, width: int | None = None) -> dict[str, Any]:
        """Solid, high-contrast buttons (visible without hover)."""
        c = self._colors
        out: dict[str, Any] = {
            "height": 36,
            "corner_radius": 6,
            "border_width": 1,
            "border_color": c["border"],
            "font": ("Segoe UI", 12, "bold"),
            "text_color": "#ffffff",
        }
        if kind == "action":
            out["fg_color"] = c["info"]
            out["hover_color"] = c["accent_hover"]
        elif kind == "launch":
            out["fg_color"] = c["warning"]
            out["hover_color"] = "#f5b041"
            out["text_color"] = "#1a1a2e"
        elif kind == "success":
            out["fg_color"] = c["success"]
            out["hover_color"] = "#58d68d"
        elif kind == "muted":
            out["fg_color"] = c["bg_input"]
            out["hover_color"] = c["bg_hover"]
            out["text_color"] = c["fg_primary"]
        else:
            out["fg_color"] = c["info"]
            out["hover_color"] = c["accent_hover"]
        if width is not None:
            out["width"] = width
        return out

    def _settings_banner(self, scroll: ctk.CTkScrollableFrame) -> None:
        c = self._colors
        ctk.CTkLabel(
            scroll,
            text=f"{ICONS['gear']} Autocoder for OpenCode",
            font=("Segoe UI", 20, "bold"),
            text_color=c["fg_heading"],
        ).pack(anchor="w", pady=(0, 4))

        ctk.CTkLabel(
            scroll,
            text=(
                "All prompts go through the OpenCode app’s HTTP server on this PC — "
                "no Chrome CDP, no fleet, no browser automation."
            ),
            font=("Segoe UI", 12),
            text_color=c["fg_secondary"],
            wraplength=720,
            justify="left",
        ).pack(anchor="w", pady=(0, SPACING["sm"]))

    def _build_session_slots(self, scroll: ctk.CTkScrollableFrame) -> None:
        c = self._colors
        card = ctk.CTkFrame(
            scroll, fg_color=c["bg_card"], corner_radius=8,
            border_width=2, border_color="#6c5ce7",
        )
        card.pack(fill="x", pady=SPACING["sm"])

        ctk.CTkLabel(
            card, text="OpenCode — connect before Start Autocoding",
            font=("Segoe UI", 16, "bold"),
            text_color=c["fg_heading"],
        ).pack(anchor="w", padx=SPACING["lg"], pady=(SPACING["md"], 4))

        howto = (
            "0. Launch Autocoder: open a terminal in this repo folder and run  python -m gemini_coder_web  "
            "(or your Autocoder.bat / pythonw launch shortcut). Logs: %USERPROFILE%\\.autocoder\\autocoder.log .\n"
            "1. Set API base URL (host + port). Use “Start OpenCode server” to run opencode-cli serve on that port "
            "if nothing is listening yet (needs OpenCode installed, or set OPENCODE_CLI).\n"
            "2. “Test server” = GET …/global/health. Or open  http://127.0.0.1:4096/global/health  in a browser (match your port).\n"
            "3. “Refresh sessions (HTTP)” loads chats from the server. If the server is down, “List from disk” reads OpenCode’s SQLite DB "
            "(optional folder filter below — use Browse… or leave blank for every session).\n"
            "4. Pick a session, Connect, then Start Autocoding. Prompts are sent as HTTP POST — no UI automation."
        )
        ctk.CTkLabel(
            card, text=howto,
            font=("Segoe UI", 11),
            text_color=c["fg_muted"],
            wraplength=720,
            justify="left",
        ).pack(anchor="w", padx=SPACING["lg"], pady=(0, SPACING["sm"]))

        st = _load_json(_STATE_FILE)
        default_base = (
            st.get("base_url")
            or os.environ.get("OPENCODE_API_BASE", "http://127.0.0.1:4096")
        ).rstrip("/")

        row1 = ctk.CTkFrame(card, fg_color="transparent")
        row1.pack(fill="x", padx=SPACING["lg"], pady=4)
        ctk.CTkLabel(row1, text="API base URL", width=120, anchor="w").pack(side="left")
        self._oc_base = ctk.CTkEntry(row1, width=360, placeholder_text="http://127.0.0.1:4096")
        self._oc_base.pack(side="left", padx=4)
        self._oc_base.insert(0, default_base)
        ctk.CTkButton(
            row1, text="Start OpenCode server",
            command=self._oc_start_server,
            **self._oc_btn_kw("launch", width=200),
        ).pack(side="left", padx=(8, 0))

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", padx=SPACING["lg"], pady=4)
        ctk.CTkLabel(row2, text="SQLite filter", width=120, anchor="w").pack(side="left")
        self._oc_path_filter = ctk.CTkEntry(
            row2, width=360,
            placeholder_text="Blank = all sessions. Or pick a project folder.",
        )
        self._oc_path_filter.pack(side="left", padx=4)
        path_default = str(st.get("path_filter", "")).strip()
        self._oc_path_filter.insert(0, path_default)
        ctk.CTkButton(
            row2, text="Browse…",
            command=self._oc_browse_sqlite_folder,
            **self._oc_btn_kw("action", width=100),
        ).pack(side="left", padx=(4, 0))

        ctk.CTkLabel(
            card,
            text=(
                "Only affects “List from disk”: limits rows to sessions whose project path contains this text. "
                "Clear the field entirely for no filter (all sessions)."
            ),
            font=("Segoe UI", 10),
            text_color=c["fg_muted"],
            wraplength=720,
            justify="left",
        ).pack(anchor="w", padx=SPACING["lg"], pady=(0, 2))

        row3 = ctk.CTkFrame(card, fg_color="transparent")
        row3.pack(fill="x", padx=SPACING["lg"], pady=4)
        ctk.CTkLabel(row3, text="Session", width=120, anchor="w").pack(side="left")
        self._oc_session_combo = ctk.CTkComboBox(
            row3, values=["(refresh to load)"], width=520,
            fg_color=c["bg_input"], text_color=c["fg_primary"],
            border_color=c["border"],
        )
        self._oc_session_combo.pack(side="left", padx=4)
        if st.get("last_session_id"):
            # Placeholder until loaded
            pass

        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=SPACING["lg"], pady=(8, 4))

        ctk.CTkButton(
            btn_row, text="Test server",
            command=self._oc_test_server,
            **self._oc_btn_kw("action", width=110),
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            btn_row, text="Refresh sessions (HTTP)",
            command=self._oc_refresh_http,
            **self._oc_btn_kw("action", width=185),
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            btn_row, text="List from disk (SQLite)",
            command=self._oc_refresh_sqlite,
            **self._oc_btn_kw("action", width=195),
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            btn_row, text="Connect",
            command=self._oc_connect,
            **self._oc_btn_kw("success", width=120),
        ).pack(side="left", padx=(12, 2))

        self._oc_status = ctk.CTkLabel(
            card, text="Not connected.",
            font=("Segoe UI", 11), text_color=c["fg_muted"], anchor="w",
        )
        self._oc_status.pack(fill="x", padx=SPACING["lg"], pady=(4, SPACING["md"]))

        self.after(300, self._oc_refresh_http)

    def _oc_save_state(self) -> None:
        choice = self._oc_session_combo.get()
        sid = self._oc_id_from_display.get(choice, "")
        _save_json(
            _STATE_FILE,
            {
                "base_url": self._oc_base.get().strip().rstrip("/"),
                "path_filter": self._oc_path_filter.get().strip(),
                "last_session_id": sid,
            },
        )

    def _oc_browse_sqlite_folder(self) -> None:
        cur = self._oc_path_filter.get().strip()
        initial = cur if cur and os.path.isdir(cur) else str(Path.cwd())
        picked = filedialog.askdirectory(
            initialdir=initial,
            title="Pick a project folder (session list will match paths containing it)",
        )
        if not picked:
            return
        self._oc_path_filter.delete(0, "end")
        self._oc_path_filter.insert(0, picked)
        self._oc_save_state()

    def _oc_start_server(self) -> None:
        base = self._oc_base.get().strip().rstrip("/")
        if not base:
            self._toast("Enter API base URL first", "warning")
            return

        def work() -> None:
            ok, msg, proc = ensure_opencode_serve(base)

            def apply() -> None:
                if ok:
                    if proc is not None:
                        self._oc_serve_proc = proc
                    self._oc_status.configure(
                        text=msg,
                        text_color=self._colors["success"],
                    )
                    self._toast(msg, "success")
                    self._oc_refresh_http()
                else:
                    self._oc_status.configure(
                        text=msg,
                        text_color=self._colors["error"],
                    )
                    self._toast(msg, "error")

            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _oc_test_server(self) -> None:
        base = self._oc_base.get().strip().rstrip("/")
        if not base:
            self._toast("Enter API base URL", "warning")
            return

        def work():
            try:
                c = OpenCodeClient(base)
                h = c.health()
                self.after(0, lambda: self._oc_status.configure(
                    text=f"Server OK: {h!r}",
                    text_color=self._colors["success"],
                ))
                self.after(0, lambda: self._toast("OpenCode server reachable", "success"))
            except OpenCodeHTTPError as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self._oc_status.configure(
                    text=m,
                    text_color=self._colors["error"],
                ))
                self.after(0, lambda m=msg: self._toast(m, "error"))

        threading.Thread(target=work, daemon=True).start()

    def _oc_refresh_http(self) -> None:
        base = self._oc_base.get().strip().rstrip("/")
        if not base:
            return

        def work():
            try:
                client = OpenCodeClient(base)
                client.health()
                sessions = client.session_list()
                displays: list[str] = []
                id_map: dict[str, str] = {}
                for s in sessions:
                    sid = s.get("id") or s.get("ID") or ""
                    title = (s.get("title") or s.get("Title") or "").strip()
                    label = f"{title} — {sid}" if title else str(sid)
                    displays.append(label)
                    id_map[label] = str(sid)
                if not displays:
                    displays = ["(no sessions — create one in OpenCode)"]
                st = _load_json(_STATE_FILE)
                preferred = st.get("last_session_id") or ""
                self.after(0, lambda: self._apply_session_choices(displays, id_map, preferred))
            except OpenCodeHTTPError as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self._oc_status.configure(
                    text=f"HTTP list failed: {m}",
                    text_color=self._colors["warning"],
                ))

        threading.Thread(target=work, daemon=True).start()

    def _apply_session_choices(
        self, displays: list[str], id_map: dict[str, str], preferred_id: str,
    ) -> None:
        self._oc_id_from_display = id_map
        self._oc_session_combo.configure(values=displays)
        pick = displays[0]
        if preferred_id:
            for label, sid in id_map.items():
                if sid == preferred_id:
                    pick = label
                    break
        self._oc_session_combo.set(pick)
        self._oc_status.configure(
            text=f"Loaded {len([d for d in displays if d not in ('(no sessions — create one in OpenCode)', '(refresh to load)')])} session(s) via HTTP.",
            text_color=self._colors["fg_secondary"],
        )

    def _oc_refresh_sqlite(self) -> None:
        filt = self._oc_path_filter.get().strip() or None

        def work():
            rows = list(iter_sessions_from_sqlite(directory_contains=filt))
            displays: list[str] = []
            id_map: dict[str, str] = {}
            for r in rows:
                short_dir = r.directory.replace("\n", " ")[:72]
                label = f"{r.title or '(no title)'} | {r.id} | {short_dir}"
                displays.append(label)
                id_map[label] = r.id
            if not displays:
                displays = ["(no rows — check OPENCODE_DB_PATH or filter)"]
            self.after(0, lambda: self._apply_session_choices(displays, id_map, ""))

        threading.Thread(target=work, daemon=True).start()

    def _oc_connect(self) -> None:
        base = self._oc_base.get().strip().rstrip("/")
        choice = self._oc_session_combo.get()
        sid = self._oc_id_from_display.get(choice, "").strip()
        if not sid and "—" in choice:
            sid = choice.split("—")[-1].strip()
        if not base or not sid or sid.startswith("("):
            self._toast("Pick a real session (refresh lists first)", "warning")
            return

        def work():
            try:
                sess = self.session_mgr.create_opencode_session(
                    _OC_CORNER, api_base=base, opencode_session_id=sid,
                )
                ok = self.session_mgr.configure_session(sess.session_id)
                if not ok:
                    self.after(0, lambda: self._toast("Connect failed — see log", "error"))
                    self.after(0, lambda: self._oc_status.configure(
                        text="Configure failed (health or session GET). Check server + session id.",
                        text_color=self._colors["error"],
                    ))
                    return
                self.after(0, lambda: self._activate_session(sess))
                self.after(0, self._update_assign_selector)
                self.after(0, self._oc_save_state)
                self.after(0, lambda: self._oc_status.configure(
                    text=f"Connected — {sid[:20]}… @ {base}",
                    text_color=self._colors["success"],
                ))
                self.after(0, lambda: self._toast("OpenCode session connected", "success"))
            except Exception as exc:
                logger.exception("OpenCode connect error")
                msg = str(exc)
                self.after(0, lambda m=msg: self._toast(m, "error"))
                self.after(0, lambda m=msg: self._oc_status.configure(
                    text=m,
                    text_color=self._colors["error"],
                ))

        threading.Thread(target=work, daemon=True).start()

    def _on_close(self) -> None:
        if self._oc_serve_proc is not None:
            try:
                if self._oc_serve_proc.poll() is None:
                    self._oc_serve_proc.terminate()
            except Exception:
                pass
            self._oc_serve_proc = None
        super()._on_close()

    def _build_auxiliary_settings_cards(self, scroll: ctk.CTkScrollableFrame) -> None:
        """Keyboard shortcuts + session log only (no CDP/traffic/fleet)."""
        c = self._colors

        kb_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        kb_card.pack(fill="x", pady=SPACING["sm"])
        ctk.CTkLabel(
            kb_card, text="\u2328 Shortcuts",
            font=("Segoe UI", 15, "bold"),
            text_color=c["fg_heading"],
        ).pack(padx=SPACING["lg"], pady=(SPACING["md"], 4), anchor="w")
        ctk.CTkLabel(
            kb_card, text=(
                "  Ctrl + 3    This settings view\n"
                "  Ctrl + K    Stop broadcast / sessions\n"
                "  F10         Drop KILL file + stop broadcast\n"
                "  Ctrl + Q    Quit"
            ),
            font=("Consolas", 12),
            text_color=c["fg_secondary"],
            justify="left", anchor="w",
        ).pack(padx=SPACING["lg"], pady=(0, SPACING["md"]), anchor="w")

        from pathlib import Path as _P_hist
        hist_card = ctk.CTkFrame(scroll, fg_color=c["bg_card"], corner_radius=8)
        hist_card.pack(fill="x", pady=SPACING["sm"])
        ctk.CTkLabel(
            hist_card, text="\U0001F4D2 Session history + endless.log",
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
            import subprocess
            from pathlib import Path

            buf: list[str] = []
            hist_file = _P_hist.home() / ".autocoder" / "sessions.jsonl"
            if hist_file.is_file():
                try:
                    lns = hist_file.read_text(encoding="utf-8").splitlines()
                    buf.append(f"=== SESSIONS ({len(lns)} events, last 15) ===")
                    for ln in lns[-15:]:
                        buf.append(ln[:180])
                except Exception as e:
                    buf.append(f"(history read failed: {e})")
            log_file = _P_hist.home() / ".autocoder" / "endless.log"
            if log_file.is_file():
                try:
                    lgl = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
                    buf.append("")
                    buf.append("=== LIVE LOG (last 20 lines) ===")
                    buf.extend(l[:180] for l in lgl)
                except Exception as e:
                    buf.append(f"(log read failed: {e})")
            try:
                self._hist_text.delete("1.0", "end")
                self._hist_text.insert(
                    "1.0",
                    "\n".join(buf) or "(no history yet)",
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
            hist_btn_row, text="Refresh",
            command=_refresh_hist,
            **self._oc_btn_kw("action", width=90),
        ).pack(side="left")
        ctk.CTkButton(
            hist_btn_row, text="Open sessions.jsonl",
            command=lambda: subprocess.Popen(
                ["explorer", str(Path.home() / ".autocoder" / "sessions.jsonl")],
            ),
            **self._oc_btn_kw("muted", width=150),
        ).pack(side="left", padx=6)
