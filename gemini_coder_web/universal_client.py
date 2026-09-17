"""Universal browser-based AI client.

Same generate() interface as GeminiClient — TaskExecutor and ExpansionEngine
work unchanged. Drives ANY AI web chat via an AIProfile.

Chrome must be available on the DevTools debug port (CDP only). There is no
pyautogui / non-CDP fallback — launch Chrome with --remote-debugging-port (e.g.
via Autocoder’s CDP launch) and open the AI site before configuring a session.
It finds the right tab by URL pattern, then interacts via DOM selectors.
"""

import logging
import os
import threading
import time
from typing import Callable, Optional

from gemini_coder.gemini_client import Conversation

from .ai_profiles import AIProfile, GEMINI_PROFILE
from .window_manager import position_existing_window

from .cdp_client import (
    CDPChatAutomation,
    connect_to_ai_site,
    reclaim_cdp_session_slots,
    DEFAULT_CDP_PORT,
)

logger = logging.getLogger(__name__)

# URL patterns for matching AI sites to CDP tabs
AI_URL_PATTERNS = {
    "Gemini": "gemini.google.com",
    "ChatGPT": "chatgpt.com",
    "Claude": "claude.ai",
    "OpenRouter": "openrouter.ai",
    "Copilot": "copilot.microsoft.com",
}


class UniversalBrowserClient:
    """Controls ANY AI web chat through browser automation.

    Drop-in replacement for GeminiClient. TaskExecutor calls generate()
    and gets back a response string — doesn't care which AI produced it.

    CDP-only: configure fails if the debug port has no matching AI tab.
    """

    MAX_RETRIES = 3
    BASE_DELAY = 2.0

    def __init__(
        self,
        ai_profile: AIProfile = GEMINI_PROFILE,
        corner: str = "bottom-right",
        use_traffic_control: bool = True,
        traffic_timeout: float = 600.0,
        cdp_port: int = DEFAULT_CDP_PORT,
    ) -> None:
        self._profile = ai_profile
        self._corner = corner
        # Pyautogui + traffic control removed; CDP does not use the traffic lock.
        self._use_traffic = False
        self._traffic_timeout = traffic_timeout
        self._cdp_port = cdp_port
        self._hwnd: Optional[int] = None
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._configured = False

        # CDP automation (preferred)
        self._cdp: Optional[CDPChatAutomation] = None
        self._cdp_available = False

    @property
    def profile(self) -> AIProfile:
        return self._profile

    @profile.setter
    def profile(self, value: AIProfile) -> None:
        self._profile = value
        self._configured = False
        self._hwnd = None
        # Disconnect CDP and release tab claim when profile changes
        if self._cdp and self._cdp.connection:
            from .cdp_client import release_ws_url
            release_ws_url(self._cdp.connection._ws_url)
            self._cdp.connection.disconnect()
        self._cdp = None
        self._cdp_available = False

    @property
    def corner(self) -> str:
        return self._corner

    @property
    def is_configured(self) -> bool:
        return bool(self._configured and self.using_cdp)

    @property
    def hwnd(self) -> Optional[int]:
        return self._hwnd

    @property
    def using_cdp(self) -> bool:
        """True when connected via Chrome DevTools Protocol."""
        return self._cdp_available and self._cdp is not None

    # Track which hwnds are already claimed by other sessions
    _claimed_hwnds: set = set()
    _claimed_lock = threading.Lock()

    @classmethod
    def _claim_hwnd(cls, hwnd: int) -> bool:
        """Thread-safe hwnd claiming."""
        with cls._claimed_lock:
            if hwnd in cls._claimed_hwnds:
                return False
            cls._claimed_hwnds.add(hwnd)
            return True

    @classmethod
    def _release_hwnd(cls, hwnd: int) -> None:
        """Thread-safe hwnd release."""
        with cls._claimed_lock:
            cls._claimed_hwnds.discard(hwnd)

    def configure(self, api_key: str = "") -> bool:
        """Connect via CDP only. api_key param ignored (compat).

        Requires Chrome (or compatible browser) with remote debugging and a tab
        matching this profile. Use Autocoder’s CDP browser launch if nothing is listening.
        """
        if self._try_configure_cdp():
            logger.info("Configured %s via CDP (reliable DOM mode)", self._profile.name)
            self._configured = True
            return True

        if os.environ.get("AUTOCODER_SKIP_CDP_RECLAIM", "").strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            logger.info(
                "CDP unavailable for %s — reclaiming debug ports / terminating stray CDP sessions…",
                self._profile.name,
            )
            reclaim_cdp_session_slots()

        if self._try_configure_cdp():
            logger.info("Configured %s via CDP after reclaim", self._profile.name)
            self._configured = True
            return True

        logger.error(
            "CDP not available for %s — start Chrome with --remote-debugging-port and open the AI site, "
            "or use Launch CDP Browser in Autocoder. Non-CDP mode is disabled.",
            self._profile.name,
        )
        return False

    def _try_configure_cdp(self) -> bool:
        """Try to connect to the AI site via CDP."""
        url_pattern = self._profile.url_pattern or AI_URL_PATTERNS.get(self._profile.name, "")
        title_pattern = self._profile.title_pattern

        if not url_pattern and not title_pattern:
            return False

        cdp = connect_to_ai_site(
            profile_name=self._profile.name,
            url_pattern=url_pattern,
            title_pattern=title_pattern,
            port=self._cdp_port,
        )

        if cdp and cdp.is_connected:
            self._cdp = cdp
            self._cdp_available = True
            logger.info("CDP connected to %s: %s", self._profile.name, cdp.connection.get_page_url())
            return True

        return False

    def configure_with_hwnd(self, hwnd: int) -> bool:
        """Directly assign a window handle (from capture mode).

        Also tries to find a matching CDP target for this window.
        """
        import ctypes
        if not ctypes.windll.user32.IsWindow(hwnd):
            return False

        # Reject if another session already claimed this hwnd
        with self._claimed_lock:
            if hwnd in self._claimed_hwnds and hwnd != self._hwnd:
                logger.warning("hwnd=%d already claimed by another session", hwnd)
                return False

        # Release old hwnd if any
        if self._hwnd:
            self._release_hwnd(self._hwnd)

        self._hwnd = hwnd
        with self._claimed_lock:
            self._claimed_hwnds.add(hwnd)
        position_existing_window(hwnd, self._corner)

        # Require CDP — capturing a window without a debuggable tab is unsupported
        self._try_configure_cdp()
        if not self._cdp_available or not self._cdp:
            self._release_hwnd(hwnd)
            self._hwnd = None
            logger.error(
                "Captured hwnd=%d but CDP did not attach — use Chrome with remote debugging "
                "and the AI tab open; capture again after Launch CDP Browser.",
                hwnd,
            )
            return False

        self._configured = True
        logger.info("Captured window hwnd=%d for %s at %s (CDP)",
                     hwnd, self._profile.name, self._corner)
        return True

    def switch_profile(self, profile: AIProfile) -> bool:
        """Switch this client to another AI profile and reconnect."""
        if not profile:
            return False
        self.profile = profile
        return self.configure()

    def release_hwnd(self) -> None:
        """Release the claimed hwnd and CDP tab when session is removed."""
        if self._hwnd:
            self._release_hwnd(self._hwnd)
            self._hwnd = None
        if self._cdp:
            # Release the claimed WebSocket URL so other sessions can use this tab
            from .cdp_client import release_ws_url
            ws_url = self._cdp.connection._ws_url
            self._cdp.connection.disconnect()
            release_ws_url(ws_url)
            self._cdp = None
            self._cdp_available = False
        self._configured = False

    def update_settings(
        self,
        model_name: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> None:
        """Ignored — browser AI uses whatever model is loaded in the UI."""
        pass

    def cancel(self) -> None:
        self._cancel_event.set()

    def new_conversation(self) -> bool:
        """Start a fresh AI conversation (navigate to clean chat URL).

        Used when the conversation is stale/full and the AI stops
        producing new responses. Only works with CDP.
        Returns True if successful.
        """
        if self._cdp and self._cdp.is_connected:
            return self._cdp.new_conversation()
        logger.warning("new_conversation() requires CDP — not available for %s", self._profile.name)
        return False

    def generate(
        self,
        prompt: str,
        conversation: Optional[Conversation] = None,
        system_instruction: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Generate a response via CDP DOM automation."""
        self._cancel_event.clear()

        if not self.is_configured:
            raise RuntimeError(
                f"{self._profile.name} not configured. "
                "Click 'Launch' in the session panel."
            )

        with self._lock:
            if self._cdp_available and self._cdp and self._cdp.is_connected:
                return self._generate_cdp(prompt, conversation, on_progress)

            if self._try_configure_cdp():
                return self._generate_cdp(prompt, conversation, on_progress)

            raise RuntimeError(
                f"{self._profile.name}: CDP not connected — cannot generate without DevTools. "
                "Restart Chrome with remote debugging and reconnect the session."
            )

    def _generate_cdp(
        self,
        prompt: str,
        conversation: Optional[Conversation],
        on_progress: Optional[Callable[[str], None]],
    ) -> str:
        """Generate using CDP — direct DOM manipulation. No mouse needed."""
        for attempt in range(self.MAX_RETRIES):
            if self._cancel_event.is_set():
                raise InterruptedError("Generation cancelled")

            try:
                if on_progress:
                    on_progress(f"{self._profile.name}: Sending via CDP...")

                result = self._cdp.send_and_read(
                    prompt=prompt,
                    timeout=300,
                    on_progress=on_progress,
                    cancel_event=self._cancel_event,
                )

                if not result or len(result.strip()) < 5:
                    raise RuntimeError("Empty response from CDP")

                if conversation:
                    conversation.add_user_message(prompt)
                    conversation.add_model_message(result)

                logger.info("CDP response from %s: %d chars", self._profile.name, len(result))
                return result

            except InterruptedError:
                raise
            except Exception as e:
                logger.warning(
                    "%s CDP generation failed (attempt %d/%d): %s",
                    self._profile.name, attempt + 1, self.MAX_RETRIES, e
                )
                if attempt < self.MAX_RETRIES - 1:
                    # Try reconnecting CDP — must release claimed URL first
                    old_ws_url = self._cdp.connection._ws_url
                    self._cdp.connection.disconnect()
                    try:
                        from .cdp_client import release_ws_url
                    except Exception:
                        from gemini_coder_web.cdp_client import release_ws_url
                    release_ws_url(old_ws_url)
                    time.sleep(self.BASE_DELAY * (2 ** attempt))
                    if not self._try_configure_cdp():
                        raise RuntimeError(
                            f"{self._profile.name}: CDP reconnect failed — non-CDP fallback is disabled."
                        )
                else:
                    raise RuntimeError(
                        f"{self._profile.name} CDP failed after {self.MAX_RETRIES} attempts: {e}"
                    ) from e

        return ""

    def test_connection(self) -> tuple[bool, str]:
        """Test that CDP is connected to the AI tab."""
        if self._cdp_available and self._cdp and self._cdp.is_connected:
            try:
                title = self._cdp.connection.get_page_title()
                return True, f"{self._profile.name} ready via CDP ({title[:30]})"
            except Exception as e:
                return False, f"{self._profile.name} CDP error: {e}"

        if not self.configure():
            return False, (
                f"{self._profile.name}: CDP unavailable — launch Chrome with remote debugging "
                "and open this AI site, then use Launch CDP Browser or Grab again."
            )

        if self._cdp_available and self._cdp and self._cdp.is_connected:
            try:
                title = self._cdp.connection.get_page_title()
                return True, f"{self._profile.name} ready via CDP ({title[:30]})"
            except Exception as e:
                return False, str(e)

        return False, f"{self._profile.name}: CDP not connected"

    def list_available_models(self) -> list[str]:
        return [f"{self._profile.name} (uses whatever model is loaded in browser)"]


# Backward compatibility alias
class BrowserGeminiClient(UniversalBrowserClient):
    """Legacy alias — creates a UniversalBrowserClient with Gemini preset."""
    def __init__(self, **kwargs):
        profile = kwargs.pop("ai_profile", GEMINI_PROFILE)
        super().__init__(ai_profile=profile, **kwargs)
