"""Google Gemini API client with retry logic and conversation management."""

import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    genai = None
    types = None


@dataclass
class Message:
    """A single message in a conversation."""
    role: str  # "user" or "model"
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class Conversation:
    """A conversation thread with Gemini.

    Supports automatic compaction: when the conversation grows past the
    configured token threshold, older messages are summarized and replaced
    with a compact continuation message (using Anthropic's exact prompt
    format from the Claude Code Rust engine).
    """
    title: str = "Untitled"
    messages: list[Message] = field(default_factory=list)
    system_instruction: str = ""
    auto_compact: bool = True
    _compact_config: Optional[object] = field(default=None, repr=False)

    def to_contents(self) -> list[types.Content]:
        """Convert to Gemini API content format."""
        if not types:
            return []
        contents = []
        for msg in self.messages:
            contents.append(types.Content(
                role=msg.role,
                parts=[types.Part.from_text(text=msg.content)],
            ))
        return contents

    def to_message_dicts(self) -> list[dict]:
        """Convert to simple dict format for compaction."""
        return [{"role": m.role, "content": m.content} for m in self.messages]

    def add_user_message(self, content: str) -> None:
        self.messages.append(Message(role="user", content=content))
        self._maybe_compact()

    def add_model_message(self, content: str) -> None:
        self.messages.append(Message(role="model", content=content))
        self._maybe_compact()

    def clear(self) -> None:
        self.messages.clear()

    def _maybe_compact(self) -> None:
        """Auto-compact if enabled and thresholds exceeded."""
        if not self.auto_compact:
            return
        try:
            from .compaction import should_compact, compact_session, CompactionConfig
            config = self._compact_config or CompactionConfig()
            dicts = self.to_message_dicts()
            if should_compact(dicts, config):
                result = compact_session(dicts, config)
                # Rebuild messages from compacted dicts
                self.messages = []
                for d in result.compacted_session:
                    role = d.get("role", "user")
                    # System messages become user messages for Gemini API compat
                    if role == "system":
                        role = "user"
                    self.messages.append(Message(role=role, content=d["content"]))
                logger.info(
                    "Conversation auto-compacted: removed %d messages",
                    result.removed_message_count,
                )
        except Exception as e:
            logger.debug("Auto-compact skipped: %s", e)


class GeminiClient:
    """Thread-safe Gemini API client with retry and streaming."""

    MAX_RETRIES = 5
    BASE_DELAY = 1.0

    def __init__(
        self,
        api_key: str = "",
        model_name: str = "gemini-2.0-flash",
        max_tokens: int = 8192,
        temperature: float = 0.7,
    ) -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._client = None
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._configured = False

        if api_key:
            self.configure(api_key)

    @property
    def is_configured(self) -> bool:
        return self._configured and GEMINI_AVAILABLE

    def configure(self, api_key: str) -> bool:
        """Configure the client with an API key."""
        if not GEMINI_AVAILABLE:
            logger.error("google-genai package not installed")
            return False
        try:
            self._client = genai.Client(api_key=api_key)
            self._api_key = api_key
            self._configured = True
            logger.info("Gemini client configured with model %s", self._model_name)
            return True
        except Exception as e:
            logger.error("Failed to configure Gemini: %s", e)
            self._configured = False
            return False

    def update_settings(
        self,
        model_name: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> None:
        """Update model settings."""
        if model_name and model_name != self._model_name:
            self._model_name = model_name
        if max_tokens is not None:
            self._max_tokens = max_tokens
        if temperature is not None:
            self._temperature = temperature

    def cancel(self) -> None:
        """Signal cancellation of the current generation."""
        self._cancel_event.set()

    def generate(
        self,
        prompt: str,
        conversation: Optional[Conversation] = None,
        system_instruction: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Generate a response with retry logic. Returns full text."""
        self._cancel_event.clear()

        if not self.is_configured:
            raise RuntimeError("Gemini client not configured. Set your API key first.")

        with self._lock:
            return self._generate_with_retry(
                prompt, conversation, system_instruction, on_progress
            )

    def _generate_with_retry(
        self,
        prompt: str,
        conversation: Optional[Conversation],
        system_instruction: str,
        on_progress: Optional[Callable[[str], None]],
    ) -> str:
        """Internal generate with exponential backoff retry."""
        last_error = None

        for attempt in range(self.MAX_RETRIES):
            if self._cancel_event.is_set():
                raise InterruptedError("Generation cancelled by user")

            try:
                config = types.GenerateContentConfig(
                    max_output_tokens=self._max_tokens,
                    temperature=self._temperature,
                )
                if system_instruction:
                    config.system_instruction = system_instruction

                contents = []
                if conversation and conversation.messages:
                    contents = conversation.to_contents()
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=prompt)],
                ))

                full_text = ""
                for chunk in self._client.models.generate_content_stream(
                    model=self._model_name,
                    contents=contents,
                    config=config,
                ):
                    if self._cancel_event.is_set():
                        raise InterruptedError("Generation cancelled by user")
                    if chunk.text:
                        full_text += chunk.text
                        if on_progress:
                            on_progress(full_text)

                if conversation:
                    conversation.add_user_message(prompt)
                    conversation.add_model_message(full_text)

                return full_text

            except InterruptedError:
                raise
            except Exception as e:
                last_error = e
                error_str = str(e)

                if "429" in error_str or "quota" in error_str.lower() or "resource_exhausted" in error_str.lower():
                    delay = self.BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "Rate limited (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, self.MAX_RETRIES, delay, e
                    )
                    time.sleep(delay)
                    continue
                elif "api key" in error_str.lower() or "unauthorized" in error_str.lower() or "invalid" in error_str.lower():
                    raise RuntimeError(
                        f"API key error: {e}. Check your Gemini API key."
                    ) from e
                else:
                    delay = self.BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "Transient error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, self.MAX_RETRIES, delay, e
                    )
                    time.sleep(delay)
                    continue

        raise RuntimeError(
            f"Failed after {self.MAX_RETRIES} attempts. Last error: {last_error}"
        )

    def test_connection(self) -> tuple[bool, str]:
        """Test the API connection with a minimal request."""
        if not GEMINI_AVAILABLE:
            return False, "google-genai package not installed. Run: pip install google-genai"
        if not self._api_key:
            return False, "No API key configured"
        try:
            self.configure(self._api_key)
            response = self._client.models.generate_content(
                model=self._model_name,
                contents="Say 'OK' and nothing else.",
            )
            if response.text:
                return True, f"Connected to {self._model_name}"
            return False, "Empty response from API"
        except Exception as e:
            return False, f"Connection failed: {e}"

    def list_available_models(self) -> list[str]:
        """List available Gemini models."""
        if not self.is_configured:
            return []
        try:
            models = self._client.models.list()
            return [
                m.name.replace("models/", "")
                for m in models
                if hasattr(m, "name")
            ]
        except Exception as e:
            logger.error("Failed to list models: %s", e)
            return []
