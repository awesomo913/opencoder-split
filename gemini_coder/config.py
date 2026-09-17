"""Configuration management with auto-save and validation."""

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from .platform_utils import get_config_dir

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
DEFAULT_MODEL = "gemini-2.0-flash"


@dataclass
class AppConfig:
    """Application configuration with defaults."""
    api_key: str = ""
    model_name: str = DEFAULT_MODEL
    theme: str = "dark"
    font_size: int = 13
    max_tokens: int = 8192
    temperature: float = 0.7
    auto_save_interval_seconds: int = 300
    max_concurrent_requests: int = 1
    task_default_minutes: int = 30
    expand_depth_limit: int = 10
    log_level: str = "INFO"
    window_width: int = 1400
    window_height: int = 900
    show_timestamps: bool = True
    auto_scroll: bool = True
    sound_notifications: bool = False
    recent_tasks: list = field(default_factory=list)
    recent_expansions: list = field(default_factory=list)

    def validate(self) -> list[str]:
        """Validate configuration, return list of issues."""
        issues = []
        if self.font_size < 8 or self.font_size > 32:
            issues.append(f"Font size {self.font_size} out of range [8, 32]")
            self.font_size = max(8, min(32, self.font_size))
        if self.temperature < 0.0 or self.temperature > 2.0:
            issues.append(f"Temperature {self.temperature} out of range [0.0, 2.0]")
            self.temperature = max(0.0, min(2.0, self.temperature))
        if self.max_tokens < 256 or self.max_tokens > 65536:
            issues.append(f"Max tokens {self.max_tokens} out of range [256, 65536]")
            self.max_tokens = max(256, min(65536, self.max_tokens))
        if self.task_default_minutes < 1 or self.task_default_minutes > 480:
            issues.append(f"Task default minutes {self.task_default_minutes} out of range")
            self.task_default_minutes = max(1, min(480, self.task_default_minutes))
        if self.theme not in ("dark", "light"):
            issues.append(f"Unknown theme '{self.theme}', defaulting to dark")
            self.theme = "dark"
        return issues


class ConfigManager:
    """Manages loading, saving, and updating configuration."""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        self._config_dir = config_dir or get_config_dir()
        self._config_path = self._config_dir / CONFIG_FILE
        self._config = AppConfig()
        self._load()

    @property
    def config(self) -> AppConfig:
        return self._config

    def _load(self) -> None:
        """Load config from disk, creating defaults if missing."""
        if self._config_path.exists():
            try:
                data = json.loads(self._config_path.read_text(encoding="utf-8"))
                for key, value in data.items():
                    if hasattr(self._config, key):
                        setattr(self._config, key, value)
                issues = self._config.validate()
                for issue in issues:
                    logger.warning("Config validation: %s", issue)
            except (json.JSONDecodeError, OSError) as e:
                logger.error("Failed to load config: %s", e)
                self._config = AppConfig()
        self.save()

    def save(self) -> None:
        """Save current config to disk."""
        try:
            data = asdict(self._config)
            data.pop("api_key", None)
            key = self._config.api_key
            data["api_key"] = key
            self._config_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        except OSError as e:
            logger.error("Failed to save config: %s", e)

    def update(self, **kwargs: Any) -> list[str]:
        """Update config values and save."""
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)
        issues = self._config.validate()
        self.save()
        return issues

    def reset_to_defaults(self) -> None:
        """Reset all settings to defaults, preserving API key."""
        api_key = self._config.api_key
        self._config = AppConfig(api_key=api_key)
        self.save()
