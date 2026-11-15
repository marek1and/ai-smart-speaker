"""Abstract base class for realtime streaming API managers."""

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from typing import Any, Callable, Optional

import metrics
from config import AppConfig
from functions.registry import get_function
from state import DeferredAction

logger = logging.getLogger(__name__)

# How often open_session() checks whether the conversation task has established
# the session. A fixed sleep races against slow networks; polling is reliable.
_SESSION_POLL_INTERVAL = 0.05
# How often the conversation task checks that its session is still wanted.
_SESSION_ALIVE_POLL_INTERVAL = 0.1
# How long a send task waits for a microphone frame before it looks whether
# the session is still wanted.
INPUT_POLL_INTERVAL = 0.5


class SessionRefused(Exception):
    """The API answered the connection with an error: retrying will not help."""


class BaseRealtimeManager(ABC):
    """
    Abstract base class for realtime API session managers.

    Provides a common interface for Gemini and OpenAI realtime APIs.
    Subclasses must implement provider-specific connection and communication.

    Common responsibilities:
    - Session lifecycle (open/close, connection retries)
    - Turn tracking (replies left over from a barged-in turn are filtered by
      the amount of microphone audio sent, see min_frames_before_reply)
    - Tool calls (immediate and deferred)
    - Manual VAD signals (activity_start/activity_end)
    - Transcription buffers
    - Callbacks for orchestrator events
    """

    # Tools executed by the orchestrator, the model gets "success" plus
    # for_model. Radio and volume run after the spoken reply; play_music starts
    # at once through on_deferred, ducked under the reply.
    DEFERRED_FUNCTIONS = frozenset({
        "play_internet_radio",
        "play_music",
        "set_playback_volume",
    })

    # Name used in the log.
    provider_name = "API"

    def __init__(
        self,
        config: AppConfig,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        self.config = config
        self.live_cfg = config.live
        self._input_queue = input_queue
        self._output_queue = output_queue

        # Callbacks for orchestrator
        self._on_turn_complete: Optional[Callable[[], None]] = None
        self._on_interrupted: Optional[Callable[[], None]] = None
        self._on_turn_timeout: Optional[Callable[[], None]] = None
        self._on_error: Optional[Callable[[], None]] = None
        self._on_deferred: Optional[Callable[[DeferredAction], None]] = None
        self._on_model_audio: Optional[Callable[[bytes], None]] = None
        # Appended to the system instruction when a session opens (current
        # device states); set by the orchestrator before open_session().
        self.session_context: str = ""

        # State tracking
        self._running: bool = False
        self._session_active: bool = False
        self._conversation_task: Optional[asyncio.Task] = None
        self._session_tasks: list[asyncio.Task] = []
        # Set by the orchestrator for the duration of a barge-in: audio that
        # still arrives for the interrupted reply is not queued.
        self.playback_interrupted: bool = False
        self._frames_sent: int = 0
        self._chunks_received: int = 0
        self._response_started: bool = False

        # Microphone frames that must be sent before audio or turn_complete is
        # accepted for a turn (filters what is left of a barged-in turn).
        self._min_frames_for_turn_complete: int = self.live_cfg.min_frames_before_reply

        # Manual VAD state
        self._activity_started: bool = False
        # True between activity_end and turn_complete — sending activity_start
        # during this window causes error 1007 "Request contains an invalid argument"
        self._waiting_for_turn_complete: bool = False

        # Transcription buffers (collected during turn, logged at turn_complete)
        self._user_transcript: str = ""
        self._ai_transcript: str = ""

        # Pending actions for deferred execution
        self.pending_actions: list[DeferredAction] = []

        # Watchdog for tool calls
        self._watchdog_task: Optional[asyncio.Task] = None
        self._watchdog_timeout: float = self.live_cfg.turn_watchdog_timeout
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
    def _connect(self) -> AbstractAsyncContextManager[None]:
        """Async context manager holding the provider connection: entered once
        the session is ready for audio. Raises SessionRefused when the API
        rejects the session."""
        ...

    @abstractmethod
    async def _send_to_api_task(self) -> None:
        """Forward microphone frames from the input queue to the API."""
        ...

    @abstractmethod
    async def _receive_from_api_task(self) -> None:
        """Process what the API sends until the session ends."""
        ...

    def _on_session_closed(self) -> None:
        """Hook for subclasses, called when the conversation task ends."""

    @abstractmethod
    async def send_activity_start(self) -> None:
        """Signal that user started speaking (manual VAD)."""
        ...

    @abstractmethod
    async def send_activity_end(self) -> None:
        """Signal that user stopped speaking (manual VAD)."""
        ...

    @abstractmethod
    async def send_text(self, text: str) -> bool:
        """Send a typed user turn; the reply comes back as audio like a spoken one."""
        ...

    async def open_session(self) -> None:
        """Open a session; session_active tells whether it came up."""
        if self._session_active:
            logger.warning("Session already active, skipping open")
            return

        self._conversation_task = asyncio.create_task(
            self._run_conversation(), name="conversation"
        )

        if not await self._wait_for_session(
            self.live_cfg.session_open_timeout, self.provider_name
        ):
            # Cancel the retry loop so it cannot open a session (or fire _on_error
            # for a second error sound) after the caller has given up on us.
            await self.close_session()

    async def close_session(self) -> None:
        """Close the active session by cancelling the conversation task."""
        if self._conversation_task and not self._conversation_task.done():
            self._conversation_task.cancel()
            try:
                await self._conversation_task
            except asyncio.CancelledError:
                pass
        self._conversation_task = None

    async def _run_conversation(self) -> None:
        """Run one session, from connecting until it is closed.

        Retries the connection up to max_reconnect_attempts times on network
        errors. A CancelledError (from close_session) stops retries immediately.
        """
        max_attempts = self.live_cfg.max_reconnect_attempts
        # Before the session first came up, open_session() is still waiting and
        # its caller reports the failure: _on_error then played a second error sound.
        opened = False

        def report_error() -> None:
            if opened and self._on_error:
                self._on_error()

        try:
            for attempt in range(1, max_attempts + 1):
                logger.info(
                    "Opening %s session (attempt %d/%d)...",
                    self.provider_name, attempt, max_attempts,
                )
                try:
                    async with self._connect():
                        opened = True
                        self._session_active = True
                        logger.info("Session opened successfully.")
                        await self._serve_session()
                    # Clean exit from the connection: don't retry.
                    break
                except asyncio.CancelledError:
                    raise
                except SessionRefused as e:
                    logger.error("%s refused the session: %s", self.provider_name, e)
                    break
                except (OSError, ConnectionError, TimeoutError) as e:
                    if attempt >= max_attempts or not self._running:
                        logger.error(
                            "%s session failed after %d attempt(s): %s",
                            self.provider_name, attempt, e,
                        )
                        report_error()
                        break
                    delay = min(
                        self.live_cfg.reconnect_backoff_base ** (attempt - 1),
                        self.live_cfg.reconnect_backoff_max,
                    )
                    logger.warning(
                        "Session connection error (attempt %d/%d), retrying in %.0fs: %s",
                        attempt, max_attempts, delay, e,
                    )
                    await asyncio.sleep(delay)
                except Exception:
                    logger.exception("Unexpected %s session error", self.provider_name)
                    report_error()
                    break
        finally:
            self._session_active = False
            self._activity_started = False
            self._on_session_closed()
            logger.info("Session closed.")

    async def _serve_session(self) -> None:
        """Run the send and receive tasks until the session ends."""
        self._session_tasks = [
            asyncio.create_task(self._send_to_api_task(), name="send_to_api"),
            asyncio.create_task(self._receive_from_api_task(), name="receive_from_api"),
        ]
        try:
            while self._running and self._session_active:
                await asyncio.sleep(_SESSION_ALIVE_POLL_INTERVAL)
        finally:
            self.cancel_watchdog()
            for task in self._session_tasks:
                task.cancel()
            await asyncio.gather(*self._session_tasks, return_exceptions=True)
            self._session_tasks.clear()

    def _begin_text_turn(self, text: str) -> None:
        """Bookkeeping shared by send_text implementations (after start_new_turn)."""
        # The early-audio/turn_complete guards wait for min_frames_before_reply
        # of mic audio to filter replies left over from a barged-in turn. A text turn starts
        # clean, and its reply can arrive before that much audio was sent.
        self._frames_sent = self._min_frames_for_turn_complete
        # Keeps local VAD from opening an audio turn on top of this one.
        self._waiting_for_turn_complete = True
        self._user_transcript = text

    async def _wait_for_session(self, timeout: float, provider: str) -> bool:
        """Wait for the conversation task to establish a session.

        Returns True once the session is up, False if the task died early (bad API
        key, no network) or the wait timed out. The caller MUST tear the task down
        on False: it retries connecting for much longer than this wait, so a session
        coming up afterwards would be invisible to the orchestrator — no recorder,
        no SESSIONS_OPENED, and the next wake word would silently reuse it.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while not self._session_active:
            if self._conversation_task and self._conversation_task.done():
                logger.warning(
                    "open_session: %s conversation task exited early", provider
                )
                return False
            if asyncio.get_running_loop().time() > deadline:
                logger.warning(
                    "open_session: timed out waiting for %s session (%.1fs), "
                    "abandoning the pending connection attempts",
                    provider,
                    timeout,
                )
                return False
            await asyncio.sleep(_SESSION_POLL_INTERVAL)
        return True

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
        """Start a new conversation turn: clears everything kept per turn."""
        self.cancel_watchdog()
        self._frames_sent = 0
        self._chunks_received = 0
        self._response_started = False
        self._activity_started = False
        self._waiting_for_turn_complete = False
        self._request_for_user_input = False
        self._user_transcript = ""
        self._ai_transcript = ""
        self.pending_actions.clear()
        logger.debug("Starting new turn")

    def start_watchdog(self, timeout: Optional[float] = None) -> None:
        """Start a watchdog timer for the turn to complete
        (live.turn_watchdog_timeout unless given)."""
        self.cancel_watchdog()
        if timeout is None:
            timeout = self.live_cfg.turn_watchdog_timeout
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
            self._on_watchdog_timeout()
            if self._on_turn_timeout:
                self._on_turn_timeout()
        except asyncio.CancelledError:
            logger.debug("Watchdog cancelled.")

    def _on_watchdog_timeout(self) -> None:
        """Hook for subclasses, called when the watchdog fires."""

    def cancel_watchdog(self) -> None:
        """Cancel the active watchdog timer."""
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    def set_callbacks(
        self,
        on_turn_complete: Optional[Callable[[], None]] = None,
        on_interrupted: Optional[Callable[[], None]] = None,
        on_turn_timeout: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[], None]] = None,
        on_deferred: Optional[Callable[[DeferredAction], None]] = None,
        on_model_audio: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        """Set callbacks for API events. on_deferred gets each deferred action
        as it is queued (play_music starts before the reply ends);
        on_model_audio gets every audio chunk the model sent, played or not."""
        self._on_turn_complete = on_turn_complete
        self._on_interrupted = on_interrupted
        self._on_turn_timeout = on_turn_timeout
        self._on_error = on_error
        self._on_deferred = on_deferred
        self._on_model_audio = on_model_audio

    @property
    def awaiting_reply(self) -> bool:
        """The user's part of the turn is over (activity_end sent, or a typed
        request) and the model has not finished answering yet."""
        return self._waiting_for_turn_complete

    def _instructions(self) -> str:
        """System instruction for a new session, with the session context."""
        base = self.live_cfg.system_instruction or ""
        return f"{base}\n\n{self.session_context}" if self.session_context else base

    def _defer(self, function_name: str, payload: dict) -> None:
        """Queues a deferred tool action for the orchestrator."""
        action = DeferredAction(function_name, payload)
        self.pending_actions.append(action)
        if self._on_deferred:
            self._on_deferred(action)

    def _accepts_reply(self) -> bool:
        """False while what arrives can only be left over from a barged-in
        turn: too little microphone audio was sent for this one to be answered."""
        return self._frames_sent >= self._min_frames_for_turn_complete

    def _log_transcripts(self) -> None:
        if self._user_transcript.strip():
            logger.info("📝 USER: %s", self._user_transcript.strip())
        if self._ai_transcript.strip():
            logger.info("🤖 AI: %s", self._ai_transcript.strip())

    async def _execute_tool(self, function_name: Optional[str], args: dict) -> Any:
        """Run one tool call and return what the model is told about it.

        Deferred tools (DEFERRED_FUNCTIONS) only look up what to do: the action
        is queued for the orchestrator and the model gets "success" with the
        facts the lookup already knows, so its reply matches what will happen.
        Other tools run now and their result goes back as it is. Never raises:
        a failure is reported to the model.
        """
        metrics.AI_TOOL_CALLS.labels(function=function_name or "unknown").inc()
        logger.info("Handling tool call: %s with args %s", function_name, args)
        try:
            if function_name == "request_for_user_input":
                self._request_for_user_input = True
                return {"status": "success"}

            func = get_function(function_name)
            result = func(**args)
            if inspect.isawaitable(result):
                result = await result

            if function_name in self.DEFERRED_FUNCTIONS:
                if isinstance(result, dict) and result.get("status") == "error":
                    # The lookup already failed: nothing to defer. Hand the model the
                    # real error, or it announces a station that never starts.
                    logger.warning(
                        "'%s' failed before deferral: %s", function_name, result.get("details")
                    )
                else:
                    self._defer(function_name, result)
                    logger.info("Deferred '%s'. Payload: %s", function_name, result)
                    result = {"status": "success", **result.get("for_model", {})}

            logger.info("Tool call '%s' handled. Result for API: %s", function_name, result)
            return result
        except Exception as e:
            logger.exception("Error handling tool call %s", function_name)
            return {"status": "error", "details": f"Error executing function: {e}"}
