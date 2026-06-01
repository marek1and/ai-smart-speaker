"""Abstract base class for realtime streaming API managers."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class BaseRealtimeManager(ABC):
    """
    Abstract base class for realtime API session managers.

    Provides a common interface for Gemini and OpenAI realtime APIs.
    Subclasses must implement provider-specific connection and communication.

    Common responsibilities:
    - Session lifecycle (open/close)
    - Turn tracking with generation counters (prevents stale data after barge-in)
    - Manual VAD signals (activity_start/activity_end)
    - Transcription buffers
    - Callbacks for orchestrator events
    """

    def __init__(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        self._input_queue = input_queue
        self._output_queue = output_queue

        # Callbacks for orchestrator
        self._on_audio_received: Optional[Callable[[], None]] = None
        self._on_text_received: Optional[Callable[[str], None]] = None
        self._on_turn_complete: Optional[Callable[[], None]] = None
        self._on_interrupted: Optional[Callable[[], None]] = None
        self._on_turn_timeout: Optional[Callable[[], None]] = None
        self._on_error: Optional[Callable[[], None]] = None

        # State tracking
        self._running: bool = False
        self._session_active: bool = False
        self._playback_interrupted: bool = False
        self._frames_sent: int = 0
        self._chunks_received: int = 0
        self._response_started: bool = False

        # Turn generation counter - incremented on each new turn to ignore stale data
        self._turn_generation: int = 0
        self._active_turn_generation: int = 0

        # Minimum frames required before accepting turn_complete (prevents stale signals)
        self._min_frames_for_turn_complete: int = 20  # ~1.6 seconds of audio

        # Manual VAD state
        self._activity_started: bool = False
        # True between activity_end and turn_complete — sending activity_start
        # during this window causes error 1007 "Request contains an invalid argument"
        self._waiting_for_turn_complete: bool = False

        # Transcription buffers (collected during turn, logged at turn_complete)
        self._user_transcript: str = ""
        self._ai_transcript: str = ""

        # Pending actions for deferred execution
        self.pending_actions: list = []

        # Watchdog for tool calls
        self._watchdog_task: Optional[asyncio.Task] = None
        self._watchdog_timeout: float = 5.0
        self._request_for_user_input: bool = False

    # -------------------------------------------------------------------------
    # Abstract methods (provider-specific)
    # -------------------------------------------------------------------------

    @abstractmethod
    def initialize(self) -> None:
        """Initialize the API client."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown the manager."""
        ...

    @abstractmethod
    async def open_session(self) -> None:
        """Open a new API session."""
        ...

    @abstractmethod
    async def close_session(self) -> None:
        """Close the active session."""
        ...

    @abstractmethod
    async def send_activity_start(self) -> None:
        """Signal that user started speaking (manual VAD)."""
        ...

    @abstractmethod
    async def send_activity_end(self) -> None:
        """Signal that user stopped speaking (manual VAD)."""
        ...

    # -------------------------------------------------------------------------
    # Common properties
    # -------------------------------------------------------------------------

    @property
    def session_active(self) -> bool:
        return self._session_active

    @property
    def frames_sent(self) -> int:
        return self._frames_sent

    @frames_sent.setter
    def frames_sent(self, value: int) -> None:
        self._frames_sent = value

    @property
    def chunks_received(self) -> int:
        return self._chunks_received

    @chunks_received.setter
    def chunks_received(self, value: int) -> None:
        self._chunks_received = value

    @property
    def response_started(self) -> bool:
        return self._response_started

    @response_started.setter
    def response_started(self, value: bool) -> None:
        if value and not self._response_started:
            logger.debug("API response started, starting global watchdog (%.1fs)", self._watchdog_timeout)
            self.start_watchdog(self._watchdog_timeout)
        self._response_started = value

    @property
    def playback_interrupted(self) -> bool:
        return self._playback_interrupted

    @playback_interrupted.setter
    def playback_interrupted(self, value: bool) -> None:
        self._playback_interrupted = value

    @property
    def activity_started(self) -> bool:
        """Whether we've sent activity_start to API."""
        return self._activity_started

    @property
    def ai_transcript(self) -> str:
        """Get the accumulated AI transcript for the current turn."""
        return self._ai_transcript

    @property
    def user_transcript(self) -> str:
        """Get the accumulated user transcript for the current turn."""
        return self._user_transcript

    # -------------------------------------------------------------------------
    # Common methods
    # -------------------------------------------------------------------------

    @property
    def request_for_user_input(self) -> bool:
        """Returns True if the API requested user input via function call."""
        return self._request_for_user_input

    def start_new_turn(self) -> None:
        """
        Start a new conversation turn.

        Increments the turn generation to ignore any stale data from previous turns.
        """
        self.cancel_watchdog()
        self._turn_generation += 1
        self._active_turn_generation = self._turn_generation
        self._frames_sent = 0
        self._chunks_received = 0
        self._response_started = False
        self._activity_started = False
        self._waiting_for_turn_complete = False
        self._request_for_user_input = False
        self._user_transcript = ""
        self._ai_transcript = ""
        self.pending_actions.clear()
        logger.debug("Starting new turn (generation=%d)", self._turn_generation)

    def start_watchdog(self, timeout: float = 5.0) -> None:
        """Start a watchdog timer for the turn to complete."""
        self.cancel_watchdog()
        self._watchdog_timeout = timeout
        self._watchdog_task = asyncio.create_task(self._watchdog_coro(timeout))

    def reset_watchdog_if_running(self) -> None:
        """Reset the watchdog timer if it is currently active."""
        if self._watchdog_task and not self._watchdog_task.done():
            logger.debug("Watchdog reset (chunk #%d)", self._chunks_received)
            self.start_watchdog(self._watchdog_timeout)

    async def _watchdog_coro(self, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
            logger.warning(
                "Watchdog timeout (%.1fs)! API did not send turn_complete.", timeout
            )
            if self._on_turn_timeout:
                self._on_turn_timeout()
        except asyncio.CancelledError:
            logger.debug("Watchdog cancelled.")

    def cancel_watchdog(self) -> None:
        """Cancel the active watchdog timer."""
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    def set_callbacks(
        self,
        on_audio_received: Optional[Callable[[], None]] = None,
        on_text_received: Optional[Callable[[str], None]] = None,
        on_turn_complete: Optional[Callable[[], None]] = None,
        on_interrupted: Optional[Callable[[], None]] = None,
        on_turn_timeout: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[], None]] = None,
    ) -> None:
        """Set callbacks for API events."""
        self._on_audio_received = on_audio_received
        self._on_text_received = on_text_received
        self._on_turn_complete = on_turn_complete
        self._on_interrupted = on_interrupted
        self._on_turn_timeout = on_turn_timeout
        self._on_error = on_error
