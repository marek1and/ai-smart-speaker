import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class StationEntry:
    name: str
    url: str
    resolved_at: float = 0.0
    play_count: int = 0


class RadioStateManager:
    def __init__(self, state_file: str) -> None:
        self._file = Path(state_file)
        self._current_key: Optional[str] = None
        self._stations: dict[str, StationEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text())
            self._current_key = data.get("current_station_key")
            for key, entry in data.get("stations", {}).items():
                self._stations[key] = StationEntry(**entry)
            logger.info(
                "Restored radio state: current=%s stations=%d",
                self._current_key,
                len(self._stations),
            )
        except Exception as e:
            logger.warning("Could not load radio state: %s", e)

    def _save(self) -> None:
        try:
            data = {
                "current_station_key": self._current_key,
                "stations": {k: asdict(v) for k, v in self._stations.items()},
            }
            self._file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("Could not save radio state: %s", e)

    def get_current_name(self) -> Optional[str]:
        if self._current_key:
            entry = self._stations.get(self._current_key)
            if entry:
                return entry.name
        return None

    def get_current_key(self) -> Optional[str]:
        return self._current_key

    def get_cached_url(self, key: str) -> Optional[str]:
        entry = self._stations.get(key)
        return entry.url if entry and entry.url else None

    def get_name_by_url(self, url: str) -> Optional[str]:
        for entry in self._stations.values():
            if entry.url == url:
                return entry.name
        return None

    def get_key_by_url(self, url: str) -> Optional[str]:
        for key, entry in self._stations.items():
            if entry.url == url:
                return key
        return None

    def get_entry(self, key: str) -> Optional[StationEntry]:
        return self._stations.get(key)

    def update_station(self, key: str, url: str, name: str) -> None:
        """Cache resolved URL. Does NOT change current_station_key."""
        entry = self._stations.get(key)
        if entry:
            entry.url = url
            entry.name = name
            entry.resolved_at = time.time()
        else:
            self._stations[key] = StationEntry(name=name, url=url, resolved_at=time.time())
        self._save()

    def set_current(self, key: str) -> None:
        self._current_key = key
        self._save()

    def record_play(self, key: str) -> None:
        entry = self._stations.get(key)
        if entry:
            entry.play_count += 1
            self._save()

    def set_current_and_play(self, key: str) -> None:
        """Set current station and increment play count in a single save."""
        self._current_key = key
        entry = self._stations.get(key)
        if entry:
            entry.play_count += 1
        self._save()

    def get_play_counts(self) -> dict[str, int]:
        return {k: v.play_count for k, v in self._stations.items()}
