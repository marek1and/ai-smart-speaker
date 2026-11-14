"""Tells a dead or frozen stream from normal playback, from what MPD reports."""

import time
from typing import Any, Dict, Optional

from config import MPDConfig


class StreamHealth:
    """Follows a playback attempt from its start.

    MPD reports a dead stream in status.error, but Mopidy (mopidy-mpd) never
    sets that field, so two more signals are kept here: playback that stops on
    its own shortly after it was started, and a position that stays frozen
    while the server reports "play".
    """

    def __init__(self, config: MPDConfig) -> None:
        self._config = config
        self.started_at: Optional[float] = None
        # Station of the last start; a stream that dies is blamed on it, not on
        # whatever station became current meanwhile.
        self.started_key: Optional[str] = None
        self.expecting_stop = False
        # One failure per playback attempt: MPD often flips to "stop" seconds
        # before it fills in status.error, so both detectors can see the same
        # death and would otherwise report it twice.
        self.failure_reported = False
        # Mopidy reports "stop" for a moment after play while it buffers the
        # stream, so a stop seen before the first "play" is not a verdict yet.
        self.seen_play = False
        # Mid-stream stall. When a stream breaks after it started, Mopidy shuts
        # its GStreamer pipeline down but keeps reporting "play" with elapsed
        # frozen at 0 (core only mirrors PAUSED from the audio layer) —
        # 03-05.10.2026 the radio sat silent for 37 h that way.
        self.stalled_since: Optional[float] = None
        self.stall_restarts = 0
        self._last_elapsed: Optional[float] = None
        self._last_songid: Optional[str] = None

    def playback_started(self) -> None:
        """A start was issued: a new attempt, judged from now."""
        self.started_at = time.monotonic()
        self.expecting_stop = False
        self.failure_reported = False
        self.seen_play = False

    def mark_failure_reported(self) -> None:
        self.failure_reported = True
        self.started_at = None

    def _in_start_grace(self) -> bool:
        return (
            self.started_at is not None
            and time.monotonic() - self.started_at <= self._config.stream_start_grace
        )

    def still_starting(self) -> bool:
        """True while a start we issued has not reached "play" yet and the
        grace window is still open."""
        return self._in_start_grace() and not self.expecting_stop and not self.seen_play

    def silent_stop_age(self) -> Optional[float]:
        """Seconds since the start when a stop seen now means the stream died
        on its own, None when it is a legitimate stop.

        The discriminator is timing: a stream that fails does so within seconds
        of the start, while a manual stop comes after listening. Anything past
        the grace window is left alone.
        """
        if self.expecting_stop or self.started_at is None or self.failure_reported:
            return None
        played_for = time.monotonic() - self.started_at
        if played_for > self._config.stream_start_grace and self.seen_play:
            return None
        return played_for

    def track_position(self, status: Dict[str, Any]) -> None:
        """Follows status.elapsed across polls to spot a frozen stream.

        Not judged while a start we issued is still inside stream_start_grace:
        Mopidy reports "play" with elapsed 0 for as long as it buffers.
        """
        try:
            elapsed = float(status.get("elapsed"))
        except (TypeError, ValueError):
            elapsed = None
        songid = status.get("songid")
        if songid != self._last_songid:
            # Next track in a music queue: elapsed restarts from 0.
            self._last_songid = songid
            self.clear_stall()
        if status.get("state") != "play" or self._in_start_grace() or elapsed is None:
            self.clear_stall()
            return
        if self._last_elapsed is None or elapsed > self._last_elapsed:
            if self._last_elapsed is not None:
                self.stalled_since = None
                self.stall_restarts = 0
            self._last_elapsed = elapsed
            return
        if self.stalled_since is None:
            self.stalled_since = time.monotonic()

    def clear_stall(self) -> None:
        self._last_elapsed = None
        self.stalled_since = None

    @property
    def stalled(self) -> bool:
        """True when the last poll found the position frozen during "play"."""
        return self.stalled_since is not None

    def stall_duration(self) -> float:
        """Seconds the position has not moved while the server reports "play"."""
        if self.stalled_since is None:
            return 0.0
        return time.monotonic() - self.stalled_since
