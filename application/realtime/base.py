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

        # Transcription buffers (collected during turn, logged at turn_complete)
        self._user_transcript: str = ""
        self._ai_transcript: str = ""

        # Radio info
        self.radio_info: list = []

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

    def start_new_turn(self) -> None:
        """
        Start a new conversation turn.

        Increments the turn generation to ignore any stale data from previous turns.
        """
        self._turn_generation += 1
        self._active_turn_generation = self._turn_generation
        self._frames_sent = 0
        self._chunks_received = 0
        self._response_started = False
        self._activity_started = False
        self._user_transcript = ""
        self._ai_transcript = ""
        self.radio_info.clear()
        logger.debug("Starting new turn (generation=%d)", self._turn_generation)

    def set_callbacks(
        self,
        on_audio_received: Optional[Callable[[], None]] = None,
        on_text_received: Optional[Callable[[str], None]] = None,
        on_turn_complete: Optional[Callable[[], None]] = None,
        on_interrupted: Optional[Callable[[], None]] = None,
    ) -> None:
        """Set callbacks for API events."""
        self._on_audio_received = on_audio_received
        self._on_text_received = on_text_received
        self._on_turn_complete = on_turn_complete
        self._on_interrupted = on_interrupted
