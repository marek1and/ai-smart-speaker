"""Gemini Live API session management with Manual VAD support."""

import asyncio
import logging
from typing import Optional

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
)

from config import AppConfig
from realtime.base import BaseRealtimeManager

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
                    # When manual VAD is enabled, we MUST disable server-side VAD
                    # to avoid error 1007: "Explicit activity control is not supported
                    # when automatic activity detection is enabled"
                    disabled=self.live_cfg.enable_manual_vad,
                    # These are used only when disabled=False (server-side VAD)
                    start_of_speech_sensitivity=StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=EndSensitivity.END_SENSITIVITY_HIGH,
                    prefix_padding_ms=100,
                    silence_duration_ms=1000,
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
            tools=[Tool(google_search=GoogleSearch())],
        )

    async def open_session(self) -> None:
        """Start a new conversation by spawning _run_conversation task."""
        if self._session_active:
            logger.warning("Session already active, skipping open")
            return

        # Spawn conversation as a task
        self._conversation_task = asyncio.create_task(
            self._run_conversation(), name="conversation"
        )

        # Wait a moment for session to establish
        await asyncio.sleep(0.1)

    async def close_session(self) -> None:
        """Close the active session by cancelling the conversation task."""
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
            logger.info("Sent activity_end to API")
        except Exception as e:
            logger.error("Failed to send activity_end: %s", e)

    async def _run_conversation(self) -> None:
        """
        Run a conversation session with the Gemini API.

        Keeps the session open until explicitly cancelled.
        """
        config = self._get_session_config()

        logger.info("Opening Gemini Live API session...")

        try:
            async with self._client.aio.live.connect(
                model=self.live_cfg.model,
                config=config,
            ) as session:
                self._session = session
                self._session_active = True
                logger.info("Session opened successfully.")

                # Run send and receive concurrently
                send_task = asyncio.create_task(
                    self._send_to_api_task(), name="send_to_api"
                )
                receive_task = asyncio.create_task(
                    self._receive_from_api_task(), name="receive_from_api"
                )
                self._session_tasks = [send_task, receive_task]

                # Keep session alive until cancelled by state machine
                try:
                    while self._running and self._session_active:
                        await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    logger.debug("Conversation task cancelled")
                    raise
                finally:
                    # Cancel session tasks
                    for task in self._session_tasks:
                        task.cancel()
                    await asyncio.gather(*self._session_tasks, return_exceptions=True)
                    self._session_tasks.clear()

        except (OSError, ConnectionError, TimeoutError) as e:
            logger.error("Session error: %s", e)
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
                    frame = await asyncio.wait_for(
                        self._input_queue.get(), timeout=0.5
                    )
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
                import traceback

                traceback.print_exc()
                if not self._running:
                    break
                await asyncio.sleep(0.5)

    async def _process_api_response(self, response: object) -> None:
        """Process a single response from the Gemini API."""
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
                                    self._frames_sent < self._min_frames_for_turn_complete
                                    and self._chunks_received == 0
                                ):
                                    logger.debug(
                                        "Ignoring early audio (sent=%d < %d)",
                                        self._frames_sent,
                                        self._min_frames_for_turn_complete,
                                    )
                                    continue

                                self._chunks_received += 1
                                self._response_started = True
                                await self._output_queue.put(data.data)
                                if self._chunks_received == 1:
                                    logger.info("Receiving audio response...")
                                if self._on_audio_received:
                                    self._on_audio_received()

                        if hasattr(part, "text") and part.text:
                            logger.info("Text: %s", part.text[:200])
                            self._response_started = True
                            if self._on_text_received:
                                self._on_text_received(part.text)

            # Handle input transcription (user's speech -> text)
            if hasattr(sc, "input_transcription") and sc.input_transcription:
                if hasattr(sc.input_transcription, "text") and sc.input_transcription.text:
                    self._user_transcript += sc.input_transcription.text

            # Handle output transcription (AI's speech -> text)
            if hasattr(sc, "output_transcription") and sc.output_transcription:
                if hasattr(sc.output_transcription, "text") and sc.output_transcription.text:
                    self._ai_transcript += sc.output_transcription.text

            # Handle turn complete
            if hasattr(sc, "turn_complete") and sc.turn_complete:
                # Ignore turn_complete if we haven't sent enough audio yet
                # This prevents stale turn_complete from previous turns after barge-in
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
