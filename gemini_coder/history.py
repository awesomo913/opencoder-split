"""Session history manager - persists all generated code, expansions, and tasks."""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .platform_utils import get_config_dir

logger = logging.getLogger(__name__)


@dataclass
class HistoryEntry:
    """A single history record."""
    id: str = ""
    timestamp: float = field(default_factory=time.time)
    entry_type: str = ""  # "task", "expansion", "code_generation"
    title: str = ""
    prompt: str = ""
    response: str = ""
    model: str = ""
    elapsed_seconds: float = 0.0
    status: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def time_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M")

    @property
    def date_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d")

    @property
    def short_preview(self) -> str:
        text = self.response or self.prompt
        if len(text) > 150:
            return text[:150] + "..."
        return text

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        entry = cls()
        for key, value in data.items():
            if hasattr(entry, key):
                setattr(entry, key, value)
        return entry


class HistoryManager:
    """Manages persistent history of all sessions."""

    MAX_ENTRIES = 500
    HISTORY_FILE = "history.json"

    def __init__(self, history_dir: Optional[Path] = None) -> None:
        self._dir = history_dir or get_config_dir()
        self._path = self._dir / self.HISTORY_FILE
        self._entries: list[HistoryEntry] = []
        self._load()

    @property
    def entries(self) -> list[HistoryEntry]:
        return list(reversed(self._entries))

    @property
    def count(self) -> int:
        return len(self._entries)

    def add(self, entry: HistoryEntry) -> None:
        """Add an entry to history and save."""
        if not entry.id:
            import uuid
            entry.id = uuid.uuid4().hex[:10]
        self._entries.append(entry)
        if len(self._entries) > self.MAX_ENTRIES:
            self._entries = self._entries[-self.MAX_ENTRIES:]
        self._save()
        logger.info("History: added %s '%s'", entry.entry_type, entry.title[:50])

    def get(self, entry_id: str) -> Optional[HistoryEntry]:
        for entry in self._entries:
            if entry.id == entry_id:
                return entry
        return None

    def delete(self, entry_id: str) -> bool:
        for i, entry in enumerate(self._entries):
            if entry.id == entry_id:
                self._entries.pop(i)
                self._save()
                return True
        return False

    def clear_all(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        self._save()
        return count

    def search(self, query: str) -> list[HistoryEntry]:
        query_lower = query.lower()
        return [
            e for e in reversed(self._entries)
            if query_lower in e.title.lower()
            or query_lower in e.prompt.lower()
            or query_lower in e.response.lower()
        ]

    def get_by_type(self, entry_type: str) -> list[HistoryEntry]:
        return [
            e for e in reversed(self._entries)
            if e.entry_type == entry_type
        ]

    def get_by_date(self, date_str: str) -> list[HistoryEntry]:
        return [
            e for e in reversed(self._entries)
            if e.date_str == date_str
        ]

    def get_unique_dates(self) -> list[str]:
        dates = []
        seen = set()
        for entry in reversed(self._entries):
            if entry.date_str not in seen:
                seen.add(entry.date_str)
                dates.append(entry.date_str)
        return dates

    def export_entry(self, entry_id: str, filepath: Path) -> bool:
        entry = self.get(entry_id)
        if not entry:
            return False
        try:
            content = f"# {entry.title}\n"
            content += f"# Date: {entry.time_str}\n"
            content += f"# Type: {entry.entry_type}\n\n"
            if entry.prompt:
                content += f"## Prompt\n{entry.prompt}\n\n"
            if entry.response:
                content += f"## Response\n{entry.response}\n"
            filepath.write_text(content, encoding="utf-8")
            return True
        except OSError as e:
            logger.error("Failed to export: %s", e)
            return False

    def _save(self) -> None:
        try:
            data = [e.to_dict() for e in self._entries]
            self._path.write_text(
                json.dumps(data, indent=1, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error("Failed to save history: %s", e)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._entries = [HistoryEntry.from_dict(d) for d in data]
            logger.info("Loaded %d history entries", len(self._entries))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load history: %s", e)
            self._entries = []
