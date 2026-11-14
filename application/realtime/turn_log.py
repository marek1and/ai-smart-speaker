"""Per-turn timeline of a realtime API turn, written to the log."""

import logging
import time
from typing import Optional

from config import LiveConfig

logger = logging.getLogger(__name__)


class TurnLog:
    """Where a turn spends its time, in seconds from the end of the user's input.

    timing is one summary line per turn. With trace_events every message to
    and from the API is recorded too and logged with it as one line
    (live.trace_api_events).
    """

    def __init__(self, live_cfg: LiveConfig) -> None:
        self._live_cfg = live_cfg
        self.timing: dict = {}
        # [kind, first, last (monotonic), count, seconds of audio]
        self.events: list[list] = []
        self._ref: Optional[float] = None  # time 0 of the trace
        # Late turn_completes whose timing went out already.
        self._late_logged: int = 0

    def start(self, late_turn_complete_pending: bool) -> None:
        """The user's input ended: time 0 of a new turn."""
        if self.timing:
            # the previous turn ended early and its turn_complete has not come yet
            self.log("next input before its turn_complete")
            if late_turn_complete_pending:
                self._late_logged += 1
        self.timing = {"t0": time.monotonic(), "audio": 0.0, "silent": 0.0}
        self._ref = self.timing["t0"]

    def mark(self, key: str) -> None:
        """Records when something first happened in this turn."""
        if self.timing and key not in self.timing:
            self.timing[key] = round(time.monotonic() - self.timing["t0"], 1)

    def note(self, key: str, value: float) -> None:
        if self.timing:
            self.timing[key] = value

    def add_audio(self, seconds: float, silent: bool) -> None:
        if not self.timing:
            return
        self.timing["audio"] += seconds
        self.mark("first_audio")
        if silent:
            self.timing["silent"] += seconds
        else:
            self.mark("first_voice")

    def log(self, how: str) -> None:
        t = self.timing
        if not t:
            return
        self.flush_events()
        self.timing = {}
        logger.info(
            "Turn timing (%s): first_audio=%ss first_voice=%ss generation_complete=%ss "
            "turn_complete=%.1fs audio=%.1fs of which silent=%.1fs, trailing silence not played=%.1fs, "
            "voice over budget not played=%.1fs",
            how, t.get("first_audio", "-"), t.get("first_voice", "-"),
            t.get("generation_complete", "-"), time.monotonic() - t["t0"], t["audio"], t["silent"],
            t.get("trimmed", 0.0), t.get("dropped", 0.0),
        )

    def log_late_turn_complete(self) -> None:
        """The turn_complete of a turn that ended early has arrived."""
        if self._late_logged:
            self._late_logged -= 1
        else:
            self.log("late")

    def session_closed(self) -> None:
        # Otherwise it is logged hours later, with the next session's first turn.
        self.log("session closed before its turn_complete")
        self.events, self._late_logged = [], 0

    def event(self, kind: str, audio_seconds: float = 0.0) -> None:
        """Records an API event; consecutive events of one kind become one entry."""
        if not self._live_cfg.trace_api_events:
            return
        now = time.monotonic()
        if self.events and self.events[-1][0] == kind:
            last = self.events[-1]
            last[2], last[3], last[4] = now, last[3] + 1, last[4] + audio_seconds
        else:
            self.events.append([kind, now, now, 1, audio_seconds])

    def flush_events(self) -> None:
        if not self.events:
            return
        ref = self._ref or self.events[0][1]
        parts = []
        for kind, a, b, n, sec in self.events:
            a, b = round(a - ref, 2), round(b - ref, 2)
            span = f"{a}s" if n == 1 else f"{a}-{b}s"
            extra = f" x{n}" if n > 1 else ""
            extra += f" ({sec:.1f}s audio)" if sec else ""
            parts.append(f"{span} {kind}{extra}")
        self.events = []
        logger.info("Turn trace: %s", " | ".join(parts))
