"""Lifetime of the realtime API session and of its recording."""

import logging
from typing import Optional

from audio.recorder import SessionRecorder
from config import LiveConfig
from functions.definitions import prompt_state_context
import metrics
from realtime import BaseRealtimeManager
from state import WakeWordResult

logger = logging.getLogger(__name__)


class SessionKeeper:
    """Opens the API session or reuses a live one, counts its turns and is the
    one place that closes it together with its recording."""

    def __init__(self, live_cfg: LiveConfig, recorder: SessionRecorder) -> None:
        self.live_cfg = live_cfg
        self.recorder = recorder
        # Set in AudioOrchestrator.start(), with the rest of the components.
        self.api: Optional[BaseRealtimeManager] = None
        # Completed turns in the current session; at session_max_turns it is
        # closed at the next turn end.
        self.turn_count: int = 0

    @property
    def active(self) -> bool:
        return self.api is not None and self.api.session_active

    async def open(self, wake_result: Optional[WakeWordResult]) -> bool:
        """Open a session unless one is live. False when the API refused."""
        if self.active:
            logger.info("Reusing existing session (recording continues)")
            return True
        self.api.session_context = await prompt_state_context()
        await self.api.open_session()
        if not self.api.session_active:
            logger.error(
                "Failed to open API session — playing error sound and returning to IDLE"
            )
            metrics.API_ERRORS.inc()
            return False
        metrics.SESSIONS_OPENED.inc()
        self.recorder.start(
            model_score=wake_result.score if wake_result else None,
            verifier_score=wake_result.verifier_score if wake_result else None,
        )
        self.turn_count = 0
        return True

    async def turn_finished(self) -> None:
        self.turn_count += 1
        metrics.SESSION_TURNS.inc()
        if self.active and self.turn_count >= self.live_cfg.session_max_turns:
            logger.info("Session turn limit reached (%d), closing session.", self.turn_count)
            await self.close("max_turns")

    async def close_if_inactive(self, idle_seconds: float) -> None:
        if self.active and idle_seconds > self.live_cfg.session_inactivity_timeout:
            logger.info("Session inactive for %.0fs, closing...", idle_seconds)
            await self.close("inactivity")

    async def close(self, reason: str) -> None:
        """reason labels the metric; "false_trigger" also files the recording as one."""
        metrics.SESSIONS_CLOSED.labels(reason=reason).inc()
        if self.api is not None:
            await self.api.close_session()
        if reason == "false_trigger":
            self.recorder.close_as_false_trigger()
        else:
            self.recorder.close()
