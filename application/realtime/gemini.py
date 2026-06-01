import asyncio
import inspect
import logging
import traceback
from typing import Optional

import numpy as np
from google import genai
from google.genai.live import AsyncSession
from google.genai.types import (
    ActivityEnd,
    ActivityStart,
    AudioTranscriptionConfig,
    AutomaticActivityDetection,
    EndSensitivity,
    GenerationConfig,
    GoogleSearch,
    LiveConnectConfig,
    PrebuiltVoiceConfig,
    RealtimeInputConfig,
    SpeechConfig,
    StartSensitivity,
    Tool,
    VoiceConfig,
    FunctionCall,
    FunctionResponse,
    LiveServerMessage,
)

from config import AppConfig
from realtime.base import BaseRealtimeManager
from functions.registry import get_function
from functions.schemas import GEMINI_TOOLS as TOOLS

logger = logging.getLogger(__name__)


class GeminiRealtimeManager(BaseRealtimeManager):
    """
    Manages Gemini Live API session lifecycle with Hybrid VAD support.

    Handles:
    - Session connection/disconnection
    - Send/receive tasks
    - Response processing
    - Manual VAD signals (activity_start/activity_end)
    """

    def __init__(
        self,
        config: "AppConfig",
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        super().__init__(input_queue, output_queue)
        self.config = config
        self.live_cfg = config.live

        # Client and session
        self._client: Optional[genai.Client] = None
        self._session: Optional[AsyncSession] = None
        self._session_tasks: list[asyncio.Task] = []
        self._conversation_task: Optional[asyncio.Task] = None

        # Output silence tracking: reset each turn via start_new_turn override below
        self._consecutive_silent_chunks: int = 0

    @staticmethod
    def _pcm16_rms(data: bytes) -> float:
        """Return normalised RMS [0.0, 1.0] for a 16-bit little-endian PCM buffer."""
        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(np.square(arr)))) / 32768.0

    def initialize(self) -> None:
        """Initialize the Gemini client."""
        api_key = self.config.api_keys.google_api_key
        if not api_key:
            raise ValueError("Google API key is not configured in config.yml.")

        self._client = genai.Client(api_key=api_key)
        self._running = True

    def shutdown(self) -> None:
        """Shutdown the manager."""
        self._running = False

    def start_new_turn(self) -> None:
        super().start_new_turn()
        self._consecutive_silent_chunks = 0

    def _get_session_config(self) -> LiveConnectConfig:
        """Build the LiveConnectConfig for API sessions.

        When manual VAD is enabled, server-side VAD must be DISABLED to allow
        explicit ActivityStart/ActivityEnd signals from our local Silero VAD.
        """
        if self.live_cfg.enable_manual_vad:
            logger.info(
                "Manual VAD enabled - server-side VAD DISABLED (using local Silero VAD)"
            )
        else:
            logger.info("Using server-side automatic VAD")

        return LiveConnectConfig(
            response_modalities=list(self.live_cfg.response_modalities),
            system_instruction=self.live_cfg.system_instruction,
            generation_config=GenerationConfig(
                temperature=self.live_cfg.temperature,
            ),
            realtime_input_config=RealtimeInputConfig(
                automatic_activity_detection=AutomaticActivityDetection(
                    # When manual VAD is enabled, we MUST disable server-side VAD.
                    # Sensitivity fields must be absent when disabled=True (API error 1007).
                    disabled=self.live_cfg.enable_manual_vad,
                    **(
                        {}
                        if self.live_cfg.enable_manual_vad
                        else dict(
                            start_of_speech_sensitivity=StartSensitivity.START_SENSITIVITY_HIGH,
                            end_of_speech_sensitivity=EndSensitivity.END_SENSITIVITY_HIGH,
                            prefix_padding_ms=100,
                            silence_duration_ms=1000,
                        )
                    ),
                )
            ),
            speech_config=SpeechConfig(
                voice_config=VoiceConfig(
                    prebuilt_voice_config=PrebuiltVoiceConfig(
                        voice_name=self.live_cfg.voice_name,
                    )
                ),
                language_code=self.live_cfg.lang,
            ),
            # Enable audio transcription for logging
            input_audio_transcription=AudioTranscriptionConfig(),
            output_audio_transcription=AudioTranscriptionConfig(),
            # Enable Google Search tool
            tools=TOOLS + [Tool(google_search=GoogleSearch())],
        )

    async def open_session(self) -> None:
        """Start a new conversation by spawning _run_conversation task."""
        if self._session_active:
            logger.warning("Session already active, skipping open")
            return

        self._conversation_task = asyncio.create_task(
            self._run_conversation(), name="conversation"
        )

        # Poll until the session is established (or give up after 10 s).
        # A fixed sleep races against slow networks; polling is reliable.
        deadline = asyncio.get_running_loop().time() + 10.0
        while not self._session_active:
            if asyncio.get_running_loop().time() > deadline:
                logger.warning("open_session: timed out waiting for Gemini session")
                break
            await asyncio.sleep(0.05)

    async def close_session(self) -> None:
        """Close the active session by cancelling the conversation task.

        The genai client is kept alive across sessions so its connection pool
        can be reused — calling aio.aclose() would permanently close httpx and
        break every subsequent open_session() call.
        """
        if self._conversation_task and not self._conversation_task.done():
            self._conversation_task.cancel()
            try:
                await self._conversation_task
            except asyncio.CancelledError:
                pass
        self._conversation_task = None

    async def send_activity_start(self) -> None:
        """Send activity_start signal to API (manual VAD: speech started).

        This tells the API that the user has started speaking, based on local VAD.
        """
        if not self._session_active or self._session is None:
            return

        if self._activity_started:
            logger.debug("Activity already started, skipping")
            return

        if self._waiting_for_turn_complete:
            logger.debug("Turn in progress (waiting for turn_complete), dropping activity_start")
            return

        try:
            await self._session.send_realtime_input(activity_start=ActivityStart())
            self._activity_started = True
            logger.info("Sent activity_start to API")
        except Exception as e:
            logger.error("Failed to send activity_start: %s", e)

    async def send_activity_end(self) -> None:
        """Send activity_end signal to API (manual VAD: speech ended).

        This tells the API that the user has stopped speaking, based on local VAD.
        The API will then process the accumulated audio and generate a response.
        """
        if not self._session_active or self._session is None:
            return

        if not self._activity_started:
            logger.debug("Activity not started, skipping activity_end")
            return

        try:
            await self._session.send_realtime_input(activity_end=ActivityEnd())
            self._activity_started = False
            self._waiting_for_turn_complete = True
            logger.info("Sent activity_end to API")
        except Exception as e:
            logger.error("Failed to send activity_end: %s", e)

    async def _run_conversation(self) -> None:
        """
        Run a conversation session with the Gemini API.

        Retries the connection up to max_reconnect_attempts times on network
        errors.  A CancelledError (from close_session) stops retries immediately.
        """
        config = self._get_session_config()
        max_attempts = self.live_cfg.max_reconnect_attempts

        try:
            for attempt in range(1, max_attempts + 1):
                logger.info("Opening Gemini Live API session (attempt %d/%d)...", attempt, max_attempts)
                try:
                    async with self._client.aio.live.connect(
                        model=self.live_cfg.model,
                        config=config,
                    ) as session:
                        self._session = session
                        self._session_active = True
                        logger.info("Session opened successfully.")

                        send_task = asyncio.create_task(
                            self._send_to_api_task(), name="send_to_api"
                        )
                        receive_task = asyncio.create_task(
                            self._receive_from_api_task(), name="receive_from_api"
                        )
                        self._session_tasks = [send_task, receive_task]

                        try:
                            while self._running and self._session_active:
                                await asyncio.sleep(0.1)
                        except asyncio.CancelledError:
                            logger.debug("Conversation task cancelled")
                            raise
                        finally:
                            self.cancel_watchdog()
                            for task in self._session_tasks:
                                task.cancel()
                            await asyncio.gather(*self._session_tasks, return_exceptions=True)
                            self._session_tasks.clear()

                    # Clean exit from async-with — don't retry.
                    break

                except asyncio.CancelledError:
                    raise
                except (OSError, ConnectionError, TimeoutError) as e:
                    if attempt >= max_attempts or not self._running:
                        logger.error("Gemini session failed after %d attempt(s): %s", attempt, e)
                        if self._on_error:
                            self._on_error()
                        break
                    delay = min(2.0 ** (attempt - 1), 30.0)
                    logger.warning(
                        "Session connection error (attempt %d/%d), retrying in %.0fs: %s",
                        attempt, max_attempts, delay, e,
                    )
                    await asyncio.sleep(delay)
                except Exception as e:
                    logger.error("Unexpected Gemini session error: %s", e)
                    traceback.print_exc()
                    if self._on_error:
                        self._on_error()
                    break
        finally:
            self._session = None
            self._session_active = False
            self._activity_started = False
            logger.info("Session closed.")

    async def _send_to_api_task(self) -> None:
        """Send audio frames from input queue to Gemini API."""
        while self._running and self._session_active:
            try:
                try:
                    frame = await asyncio.wait_for(self._input_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                # Send to API
                await self._session.send_realtime_input(
                    audio={"data": frame.data, "mime_type": frame.mime_type}
                )
                self._frames_sent += 1

                if self._frames_sent == 1:
                    logger.info("Streaming audio to API...")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Send to API error: %s", e)
                if not self._running:
                    break
                await asyncio.sleep(0.1)

    async def _receive_from_api_task(self) -> None:
        """Receive responses from Gemini API and queue for playback."""
        while self._running and self._session_active:
            try:
                turn = self._session.receive()
                async for response in turn:
                    if not self._running:
                        break

                    await self._process_api_response(response)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Receive from API error: %s", e)
                traceback.print_exc()
                self._session_active = False
                if self._on_error:
                    self._on_error()
                elif self._on_turn_complete:
                    self._on_turn_complete()
                break

    async def _process_api_response(self, response: LiveServerMessage) -> None:
        """Process a single response from the Gemini API.

        The watchdog is intentionally reset ONLY when audio chunks arrive, not on
        every API message.  This prevents the watchdog from being kept alive
        indefinitely by transcription updates or empty heartbeats when Gemini has
        already finished speaking but hasn't sent turn_complete yet.
        """
        if hasattr(response, "server_content") and response.server_content:
            sc = response.server_content

            # Handle model turn (audio data)
            if hasattr(sc, "model_turn") and sc.model_turn:
                mt = sc.model_turn

                if mt.parts:
                    for part in mt.parts:
                        if hasattr(part, "inline_data") and part.inline_data:
                            data = part.inline_data
                            if isinstance(data.data, bytes) and len(data.data) > 0:
                                # Skip if playback is interrupted (barge-in)
                                if self._playback_interrupted:
                                    continue

                                # Ignore stale audio if we haven't sent enough frames yet
                                # This prevents old audio from previous turn being counted after barge-in
                                if (
                                    self._frames_sent
                                    < self._min_frames_for_turn_complete
                                    and self._chunks_received == 0
                                ):
                                    logger.debug(
                                        "Ignoring early audio (sent=%d < %d)",
                                        self._frames_sent,
                                        self._min_frames_for_turn_complete,
                                    )
                                    continue

                                self._chunks_received += 1

                                rms = self._pcm16_rms(data.data)
                                is_silent = rms < self.live_cfg.output_silence_rms_threshold

                                if is_silent:
                                    self._consecutive_silent_chunks += 1
                                    if self._consecutive_silent_chunks in (1, 5, 20, 50) or self._consecutive_silent_chunks % 100 == 0:
                                        logger.warning(
                                            "Receiving SILENT audio from Gemini (chunk #%d, rms=%.5f, consecutive_silent=%d) — watchdog NOT reset",
                                            self._chunks_received,
                                            rms,
                                            self._consecutive_silent_chunks,
                                        )
                                else:
                                    if self._consecutive_silent_chunks > 0:
                                        logger.info(
                                            "Audio resumed after %d silent chunks (rms=%.5f)",
                                            self._consecutive_silent_chunks,
                                            rms,
                                        )
                                    self._consecutive_silent_chunks = 0

                                # Setting response_started=True on the first chunk
                                # auto-starts the watchdog via the property setter.
                                # On subsequent NON-SILENT chunks, reset_watchdog_if_running()
                                # extends it. Silent chunks do NOT reset the watchdog so that
                                # Gemini streaming silence indefinitely will eventually time out.
                                if not self._response_started:
                                    self.response_started = True  # starts watchdog
                                elif not is_silent:
                                    self.reset_watchdog_if_running()  # extends watchdog

                                await self._output_queue.put(data.data)
                                if self._chunks_received == 1:
                                    logger.info("Receiving audio response (chunk_bytes=%d, rms=%.5f)...", len(data.data), rms)
                                if self._on_audio_received:
                                    self._on_audio_received()

                        if hasattr(part, "text") and part.text:
                            logger.info("Text: %s", part.text[:200])
                            self.response_started = True
                            if self._on_text_received:
                                self._on_text_received(part.text)

                        # Handle single function call
                        if hasattr(part, "function_call") and part.function_call:
                            await self._handle_tool_calls([part.function_call])

            # Handle input transcription (user's speech -> text)
            if hasattr(sc, "input_transcription") and sc.input_transcription:
                if (
                    hasattr(sc.input_transcription, "text")
                    and sc.input_transcription.text
                ):
                    self._user_transcript += sc.input_transcription.text

            # Handle output transcription (AI's speech -> text)
            if hasattr(sc, "output_transcription") and sc.output_transcription:
                if (
                    hasattr(sc.output_transcription, "text")
                    and sc.output_transcription.text
                ):
                    self._ai_transcript += sc.output_transcription.text

            # Handle turn complete
            if hasattr(sc, "turn_complete") and sc.turn_complete:
                self._waiting_for_turn_complete = False

                # Ignore turn_complete if we haven't sent enough audio yet — likely a
                # stale signal from the previous turn after barge-in. Do NOT cancel the
                # watchdog here: it must keep running so the session doesn't hang.
                if (
                    self._frames_sent < self._min_frames_for_turn_complete
                    and not self._response_started
                ):
                    logger.debug(
                        "Ignoring early turn_complete (sent=%d < %d, no response yet)",
                        self._frames_sent,
                        self._min_frames_for_turn_complete,
                    )
                    return

                # Cancel watchdog only for a real (non-stale) turn_complete
                self.cancel_watchdog()

                # Log full transcripts at end of turn
                if self._user_transcript.strip():
                    logger.info("📝 USER: %s", self._user_transcript.strip())
                if self._ai_transcript.strip():
                    logger.info("🤖 AI: %s", self._ai_transcript.strip())

                logger.info("API: turn_complete")
                if self._on_turn_complete:
                    self._on_turn_complete()

            # Handle interruption signal from API
            if hasattr(sc, "interrupted") and sc.interrupted:
                logger.info("API: interrupted signal")
                if self._on_interrupted:
                    self._on_interrupted()

        elif hasattr(response, "setup_complete") and response.setup_complete:
            logger.debug("API: setup_complete")

        elif hasattr(response, "tool_call") and response.tool_call:
            logger.info("API: tool_call received")
            if response.tool_call.function_calls:
                await self._handle_tool_calls(response.tool_call.function_calls)

    async def _handle_tool_calls(self, function_calls: list[FunctionCall]):
        """
        Handle a list of function calls from the API using an optimistic execution strategy.

        - **Immediate Functions** (`get_radio_status`, etc.): Executed instantly,
          and their real results are sent back to the API.
        - **Deferred Functions** (`play...`, `stop...`, `set_volume...`):
          The action is added to a queue (`self.pending_actions`) for the Orchestrator
          to execute AFTER the AI finishes speaking. A fake "success" message
          is sent to the API immediately to prevent deadlocks and allow the AI
          to generate its verbal response.
        """
        function_responses = []
        DEFERRED_FUNCTIONS = {
            "play_internet_radio",
            "set_playback_volume",
        }

        for function_call in function_calls:
            function_name = function_call.name
            args = function_call.args
            logger.info(f"Handling tool call: {function_name} with args {args}")

            try:
                # 1. Immediate override for Gemini state machine bug
                if function_name == "request_for_user_input":
                    logger.info("Received 'request_for_user_input' tool call. Setting flag for follow-up.")
                    self._request_for_user_input = True
                    result = {"status": "success"}  # watchdog started by post-loop call below

                # 2. Deferred execution handling
                elif function_name in DEFERRED_FUNCTIONS:
                    func = get_function(function_name)
                    # Defer execution by getting metadata from the function call
                    payload = (
                        await func(**args)
                        if inspect.iscoroutinefunction(func)
                        else func(**args)
                    )
                    # Add to the queue for the orchestrator to handle later
                    self.pending_actions.append({"type": function_name, "payload": payload})
                    logger.info(
                        f"Deferring '{function_name}' for post-response execution. Payload: {payload}"
                    )
                    # Lie to the model, telling it the action was successful to not block conversation flow
                    result = {"status": "success"}

                # 3. Standard execution for immediate functions
                else:
                    func = get_function(function_name)
                    logger.info(f"Executing '{function_name}' immediately.")
                    result = (
                        await func(**args)
                        if inspect.iscoroutinefunction(func)
                        else func(**args)
                    )

                logger.info(
                    f"Tool call '{function_name}' handled. Result for API: {result}"
                )

                # Format the response dictionary for the Gemini API.
                response_dict: dict
                if isinstance(result, dict):
                    response_dict = result
                else:
                    response_dict = {"result": str(result)}

                function_responses.append(
                    FunctionResponse(
                        id=function_call.id,
                        name=function_name,
                        response=response_dict,
                    )
                )

            except Exception as e:
                logger.error(f"Error handling tool call {function_name}: {e}")
                function_responses.append(
                    FunctionResponse(
                        id=function_call.id,
                        name=function_name,
                        response={"error": f"Error executing function: {e}"},
                    )
                )

        # Send all responses (real or fake) back to the API
        logger.debug(f"Sending tool responses to API: {function_responses}")
        await self._session.send_tool_response(function_responses=function_responses)
        
        # Start watchdog in case API hangs after function call without sending turn_complete
        self.start_watchdog(timeout=self.live_cfg.turn_watchdog_timeout)
