"""OpenCode local HTTP client with the same surface as UniversalBrowserClient for broadcast/tasks."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Optional

from gemini_coder.gemini_client import Conversation

from .ai_profiles import AIProfile, OPENCODE_PROFILE
from .opencode_bridge import OpenCodeClient, OpenCodeHTTPError

logger = logging.getLogger(__name__)


def _extract_assistant_text(data: Any) -> str:
    """Best-effort parse of POST /session/:id/message JSON into plain text."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data).strip()

    for key in ("content", "text", "response", "body", "message", "output", "reply"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    parts = data.get("parts")
    if isinstance(parts, list):
        chunks: list[str] = []
        for p in parts:
            if isinstance(p, dict):
                if p.get("type") == "text" and isinstance(p.get("text"), str):
                    chunks.append(p["text"])
                elif isinstance(p.get("content"), str):
                    chunks.append(p["content"])
            elif isinstance(p, str):
                chunks.append(p)
        if chunks:
            return "\n".join(chunks).strip()

    msgs = data.get("messages")
    if isinstance(msgs, list) and msgs:
        last = msgs[-1]
        if isinstance(last, dict):
            return _extract_assistant_text(last)

    nested = data.get("data")
    if isinstance(nested, (dict, list)):
        inner = _extract_assistant_text(nested)
        if inner:
            return inner

    # Fallback: compact JSON for debugging (truncated)
    try:
        blob = json.dumps(data, ensure_ascii=False)[:12000]
    except (TypeError, ValueError):
        blob = repr(data)[:12000]
    logger.warning("OpenCode message response shape unrecognized; using raw excerpt")
    return blob.strip()


class OpenCodeBrowserClient:
    """Drives OpenCode via its local OpenAPI server — no CDP or browser automation."""

    MAX_RETRIES = 3
    BASE_DELAY = 2.0

    def __init__(
        self,
        *,
        base_url: str,
        session_id: str,
        corner: str = "bottom-right",
        ai_profile: Optional[AIProfile] = None,
        timeout_s: float = 600.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session_id = session_id.strip()
        self._corner = corner
        self._profile = ai_profile or OPENCODE_PROFILE
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._configured = False
        self._http = OpenCodeClient(self._base_url, timeout_s=timeout_s)

    @property
    def profile(self) -> AIProfile:
        return self._profile

    @profile.setter
    def profile(self, value: AIProfile) -> None:
        self._profile = value

    @property
    def corner(self) -> str:
        return self._corner

    @property
    def using_cdp(self) -> bool:
        return False

    @property
    def using_opencode(self) -> bool:
        return True

    @property
    def is_configured(self) -> bool:
        return bool(self._configured and self._session_id)

    @property
    def hwnd(self) -> Optional[int]:
        return None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def opencode_session_id(self) -> str:
        return self._session_id

    def set_target(self, *, base_url: Optional[str] = None, session_id: Optional[str] = None) -> None:
        if base_url is not None:
            self._base_url = base_url.rstrip("/")
            self._http = OpenCodeClient(self._base_url, timeout_s=self._http.timeout_s)
        if session_id is not None:
            self._session_id = session_id.strip()
        self._configured = False

    def configure(self, api_key: str = "") -> bool:
        del api_key  # compat with UniversalBrowserClient
        try:
            self._http.health()
            if self._session_id:
                self._http.session_get(self._session_id)
            self._configured = True
            logger.info("OpenCode configured: %s session=%s", self._base_url, self._session_id[:16])
            return True
        except OpenCodeHTTPError as exc:
            logger.error("OpenCode configure failed: %s", exc)
            self._configured = False
            return False

    def configure_with_hwnd(self, hwnd: int) -> bool:
        del hwnd
        return False

    def switch_profile(self, profile: AIProfile) -> bool:
        """Model rotation in broadcast uses this for CDP profiles — not applicable to OpenCode."""
        del profile
        return False

    def release_hwnd(self) -> None:
        self._configured = False

    def cancel(self) -> None:
        self._cancel_event.set()

    def update_settings(
        self,
        model_name: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> None:
        del model_name, max_tokens, temperature

    def new_conversation(self) -> bool:
        """OpenCode keeps chat history server-side; fork/create a session in the app if you need a clean thread."""
        logger.debug("OpenCode new_conversation(): no-op (same session)")
        return True

    def generate(
        self,
        prompt: str,
        conversation: Optional[Conversation] = None,
        system_instruction: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> str:
        if not self.is_configured:
            raise RuntimeError(
                "OpenCode is not connected. Set API base URL, pick a session, click Connect, "
                "and ensure the OpenCode desktop app is running with its HTTP server enabled."
            )
        self._cancel_event.clear()
        text_in = prompt
        if system_instruction and system_instruction.strip():
            text_in = f"{system_instruction.strip()}\n\n---\n\n{prompt}"

        with self._lock:
            last_err: Optional[Exception] = None
            for attempt in range(self.MAX_RETRIES):
                if self._cancel_event.is_set():
                    raise InterruptedError("Generation cancelled")
                try:
                    if on_progress:
                        on_progress(f"OpenCode: sending (attempt {attempt + 1}/{self.MAX_RETRIES})…")
                    raw = self._http.send_message(self._session_id, text_in)
                    out = _extract_assistant_text(raw)
                    if not out or len(out.strip()) < 2:
                        raise RuntimeError(f"Empty or unusable OpenCode response ({type(raw).__name__})")
                    if conversation:
                        conversation.add_user_message(prompt)
                        conversation.add_model_message(out)
                    logger.info("OpenCode response: %d chars", len(out))
                    return out
                except InterruptedError:
                    raise
                except Exception as exc:
                    last_err = exc
                    logger.warning(
                        "OpenCode generate failed (%d/%d): %s",
                        attempt + 1, self.MAX_RETRIES, exc,
                    )
                    if attempt < self.MAX_RETRIES - 1:
                        time.sleep(self.BASE_DELAY * (2**attempt))
            raise RuntimeError(
                f"OpenCode failed after {self.MAX_RETRIES} attempts: {last_err}"
            ) from last_err

    def test_connection(self) -> tuple[bool, str]:
        try:
            h = self._http.health()
            extra = f" health={h!r}" if h else ""
            if self._session_id:
                self._http.session_get(self._session_id)
                return True, f"OpenCode OK{extra} session={self._session_id[:12]}…"
            return True, f"OpenCode server OK{extra} (no session selected)"
        except OpenCodeHTTPError as exc:
            return False, str(exc)
