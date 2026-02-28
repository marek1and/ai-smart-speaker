"""OpenAI Realtime API session management via WebSocket.

Uses raw WebSocket connection for maximum control and compatibility.
Audio format: PCM16 at 24kHz, base64 encoded over WebSocket.
Input audio from mic (16kHz) is resampled to 24kHz before sending.
"""

import asyncio
import base64
import json
import logging
import os
from typing import Optional

import numpy as np
from scipy.signal import resample_poly

from config import AppConfig
from realtime.base import BaseRealtimeManager
from functions.registry import get_function
from functions.schemas import OPENAI_TOOLS as TOOLS

logger = logging.getLogger(__name__)

# OpenAI Realtime API WebSocket endpoint
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"


class OpenAIRealtimeManager(BaseRealtimeManager):
    """
    Manages OpenAI Realtime API session lifecycle via WebSocket.

    Handles:
    - WebSocket connection with JSON events
    - Audio streaming (PCM16 @ 24kHz, base64 encoded)
    - Manual VAD via input_audio_buffer.commit + response.create
    - Server VAD via turn_detection configuration
    - Barge-in via response.cancel
    - Input/output audio transcription
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

        # WebSocket connection
        self._ws = None
        self._conversation_task: Optional[asyncio.Task] = None
        self._session_tasks: list[asyncio.Task] = []

        # API key
        self._api_key: str = ""

        # Resampling: 16kHz -> 24kHz (OpenAI requires 24kHz for pcm16)
        self._need_resample = self.live_cfg.send_sample_rate != 24000

        # Tracking for conversation.item.truncate on barge-in
        self._current_response_item_id: Optional[str] = None
        self._audio_bytes_received: int = 0
        self._is_response_item_in_progress: bool = False

    def initialize(self) -> None:
        """Initialize the OpenAI client."""
        api_key = self.config.api_keys.openai_api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY is required for OpenAI provider, either in config.yml or as an environment variable"
            )
        self._api_key = api_key
        self._running = True
        logger.info("OpenAI Realtime API manager initialized")

    def shutdown(self) -> None:
        """Shutdown the manager."""
        self._running = False

    async def open_session(self) -> None:
        """Start a new conversation by spawning _run_conversation task."""
        if self._session_active:
            logger.warning("Session already active, skipping open")
            return

        self._conversation_task = asyncio.create_task(
            self._run_conversation(), name="openai_conversation"
        )

        # Wait a moment for session to establish
        await asyncio.sleep(0.3)

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
        """Signal that user started speaking (manual VAD).

        For OpenAI with manual turn detection, audio is streamed continuously
        via input_audio_buffer.append. We just mark activity as started.
        """
        if not self._session_active or self._ws is None:
            return

        if self._activity_started:
            logger.debug("Activity already started, skipping")
            return

        self._activity_started = True
        logger.info("Activity started (OpenAI: audio streaming)")

    async def send_activity_end(self) -> None:
        """Signal that user stopped speaking (manual VAD).

        Commits the audio buffer and triggers response generation.
        """
        if not self._session_active or self._ws is None:
            return

        if not self._activity_started:
            logger.debug("Activity not started, skipping activity_end")
            return

        if self._is_response_item_in_progress:
            logger.warning(
                "Response item is already in progress, skipping activity_end"
            )
            return

        try:
            # Give last audio frames a moment to be sent
            await asyncio.sleep(0.15)

            # Commit the audio buffer -> creates user message item
            await self._ws_send({"type": "input_audio_buffer.commit"})

            # Trigger response generation
            await self._ws_send({"type": "response.create"})

            self._activity_started = False
            logger.info("Sent activity_end to OpenAI (commit + response.create)")
        except Exception as e:
            logger.error("Failed to send activity_end: %s", e)

    def start_new_turn(self) -> None:
        """Start a new conversation turn.

        Overrides base to also cancel any in-progress response, truncate
        assistant audio, and clear the input audio buffer on the OpenAI side.
        """
        super().start_new_turn()

        # Cancel current response, truncate, and clear buffer (for barge-in)
        if self._ws and self._session_active:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._cancel_and_clear())
            except RuntimeError:
                pass

    async def _cancel_and_clear(self) -> None:
        """Cancel in-progress response, truncate assistant audio, and clear input buffer."""
        try:
            # Only cancel if a response item is active
            if self._is_response_item_in_progress:
                await self._ws_send({"type": "response.cancel"})

            # Truncate assistant audio to what was actually played.
            # This syncs the server's context with the client's playback position
            # so the model knows the user only heard part of the response.
            if self._current_response_item_id and self._audio_bytes_received > 0:
                # PCM16 @ 24kHz: 2 bytes/sample, 24000 samples/sec
                audio_end_ms = int(
                    self._audio_bytes_received / 2 / 24000 * 1000
                )
                await self._ws_send({
                    "type": "conversation.item.truncate",
                    "item_id": self._current_response_item_id,
                    "content_index": 0,
                    "audio_end_ms": audio_end_ms,
                })
                logger.info(
                    "Truncated assistant audio at %dms (item=%s)",
                    audio_end_ms,
                    self._current_response_item_id,
                )

            await self._ws_send({"type": "input_audio_buffer.clear"})
            
            log_msg = "Cancelled response and cleared audio buffer" if self._is_response_item_in_progress else "Cleared audio buffer"
            logger.info(log_msg)
        except Exception as e:
            logger.error("Failed to cancel/clear: %s", e)

    # -------------------------------------------------------------------------
    # WebSocket helpers
    # -------------------------------------------------------------------------

    async def _ws_send(self, event: dict) -> None:
        """Send a JSON event through the WebSocket."""
        if self._ws is not None:
            await self._ws.send(json.dumps(event))

    def _resample_to_24k(self, audio_bytes: bytes) -> bytes:
        """Resample 16kHz PCM16 audio to 24kHz PCM16."""
        if not self._need_resample:
            return audio_bytes

        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        if len(audio_int16) == 0:
            return audio_bytes

        # Resample: 16kHz -> 24kHz = ratio 3/2
        resampled = resample_poly(audio_int16.astype(np.float64), up=3, down=2)
        return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()

    # -------------------------------------------------------------------------
    # Session lifecycle
    # -------------------------------------------------------------------------

    async def _run_conversation(self) -> None:
        """Run a conversation session with the OpenAI Realtime API."""
        import websockets

        model = self.live_cfg.openai_model
        url = f"{OPENAI_REALTIME_URL}?model={model}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
        }

        logger.info("Opening OpenAI Realtime API session (model=%s)...", model)

        try:
            async with websockets.connect(
                url,
                additional_headers=headers,
                max_size=2**24,  # 16MB max message size for audio
            ) as ws:
                self._ws = ws
                self._session_active = True

                # Wait for session.created event
                initial_msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                initial_event = json.loads(initial_msg)

                if initial_event.get("type") == "session.created":
                    session_info = initial_event.get("session", {})
                    logger.info(
                        "Session created (id=%s)", session_info.get("id", "unknown")
                    )
                elif initial_event.get("type") == "error":
                    error = initial_event.get("error", {})
                    logger.error(
                        "Session creation error: %s - %s",
                        error.get("type", "unknown"),
                        error.get("message", ""),
                    )
                    return

                # Configure session
                await self._configure_session()
                logger.info("Session opened successfully.")

                # Run send and receive concurrently
                send_task = asyncio.create_task(
                    self._send_to_api_task(), name="openai_send"
                )
                receive_task = asyncio.create_task(
                    self._receive_from_api_task(), name="openai_receive"
                )
                self._session_tasks = [send_task, receive_task]

                # Keep session alive until cancelled
                try:
                    while self._running and self._session_active:
                        await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    logger.debug("Conversation task cancelled")
                    raise
                finally:
                    for task in self._session_tasks:
                        task.cancel()
                    await asyncio.gather(*self._session_tasks, return_exceptions=True)
                    self._session_tasks.clear()

        except (OSError, ConnectionError, TimeoutError) as e:
            logger.error("Session error: %s", e)
        except Exception as e:
            logger.error("Unexpected session error: %s", e)
            import traceback

            traceback.print_exc()
        finally:
            self._ws = None
            self._session_active = False
            self._activity_started = False
            logger.info("Session closed.")

    async def _configure_session(self) -> None:
        """Send session configuration event.

        Follows the current API structure from:
        https://platform.openai.com/docs/guides/realtime-conversations#handling-audio-with-websockets
        """
        # Audio input config
        audio_input: dict = {
            "format": {
                "type": "audio/pcm",
                "rate": 24000,
            },
            "transcription": {
                "model": "whisper-1",
            },
        }

        if self.live_cfg.enable_manual_vad:
            # Manual VAD - disable server-side turn detection
            audio_input["turn_detection"] = None
            logger.info(
                "Manual VAD enabled - server-side turn detection DISABLED"
            )
        else:
            # Server semantic VAD
            audio_input["turn_detection"] = {
                "type": "semantic_vad",
            }
            logger.info("Using server-side semantic VAD")

        # Audio output config
        audio_output: dict = {
            "format": {
                "type": "audio/pcm",
                "rate": 24000,
            },
            "voice": self.live_cfg.openai_voice,
        }

        session_config: dict = {
            "type": "realtime",
            "model": self.live_cfg.openai_model,
            "output_modalities": ["audio"],
            "instructions": self.live_cfg.system_instruction,
            "tools": TOOLS,
            "audio": {
                "input": audio_input,
                "output": audio_output,
            },
        }

        await self._ws_send({
            "type": "session.update",
            "session": session_config,
        })

    # -------------------------------------------------------------------------
    # Send / Receive tasks
    # -------------------------------------------------------------------------

    async def _send_to_api_task(self) -> None:
        """Send audio frames from input queue to OpenAI API."""
        while self._running and self._session_active:
            try:
                try:
                    frame = await asyncio.wait_for(
                        self._input_queue.get(), timeout=0.5
                    )
                except asyncio.TimeoutError:
                    continue

                # Resample 16kHz -> 24kHz
                audio_data = self._resample_to_24k(frame.data)

                # Base64 encode for WebSocket transport
                audio_b64 = base64.b64encode(audio_data).decode("ascii")

                # Send to API
                await self._ws_send({
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64,
                })

                self._frames_sent += 1

                if self._frames_sent == 1:
                    logger.info("Streaming audio to OpenAI API...")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Send to API error: %s", e)
                if not self._running:
                    break
                await asyncio.sleep(0.1)

    async def _receive_from_api_task(self) -> None:
        """Receive events from OpenAI API via WebSocket."""
        while self._running and self._session_active:
            try:
                msg = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
                event = json.loads(msg)
                await self._process_event(event)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Receive from API error: %s", e)
                if not self._running:
                    break
                await asyncio.sleep(0.5)

    # -------------------------------------------------------------------------
    # Event processing
    # -------------------------------------------------------------------------

    async def _process_event(self, event: dict) -> None:
        """Process a single event from the OpenAI Realtime API.

        Event names follow the current (non-beta) API:
        - response.output_audio.delta   (audio chunks)
        - response.output_audio_transcript.delta  (AI speech transcript)
        - response.output_text.delta     (text response)
        - conversation.item.input_audio_transcription.completed  (user transcript)
        - response.done                  (turn complete, status: completed|cancelled)
        """
        event_type = event.get("type", "")

        # --- Audio data ---
        if event_type == "response.output_audio.delta":
            audio_b64 = event.get("delta", "")
            if not audio_b64:
                return

            audio_data = base64.b64decode(audio_b64)
            if not audio_data:
                return

            # Skip if playback is interrupted (barge-in)
            if self._playback_interrupted:
                return

            # Ignore stale audio (same logic as Gemini manager)
            if (
                self._frames_sent < self._min_frames_for_turn_complete
                and self._chunks_received == 0
            ):
                logger.debug(
                    "Ignoring early audio (sent=%d < %d)",
                    self._frames_sent,
                    self._min_frames_for_turn_complete,
                )
                return

            self._chunks_received += 1
            self._audio_bytes_received += len(audio_data)
            self._response_started = True
            await self._output_queue.put(audio_data)

            if self._chunks_received == 1:
                logger.info("Receiving audio response...")
            if self._on_audio_received:
                self._on_audio_received()

        # --- AI transcript (model's speech -> text) ---
        elif event_type == "response.output_audio_transcript.delta":
            delta = event.get("delta", "")
            if delta:
                self._ai_transcript += delta

        # --- User transcript (input audio transcription) ---
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                self._user_transcript += transcript

        # --- Text response ---
        elif event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            if delta:
                logger.info("Text: %s", delta[:200])
                self._response_started = True
                if self._on_text_received:
                    self._on_text_received(delta)

        # --- Response complete (= turn complete) ---
        elif event_type == "response.done":
            self._is_response_item_in_progress = False
            response = event.get("response", {})
            status = response.get("status", "")

            # Handle tool calls if present in the output
            if "output" in response and response["output"]:
                tool_calls_handled = False
                for item in response["output"]:
                    if item.get("type") == "function_call":
                        await self._handle_tool_call(item)
                        tool_calls_handled = True
                
                if tool_calls_handled:
                    return # Stop further processing, as a new response will be created

            if status == "cancelled":
                logger.info("API: response cancelled (via response.done)")
                if self._on_interrupted:
                    self._on_interrupted()
                return

            # Ignore if we haven't sent enough audio (prevents stale signals)
            if (
                self._frames_sent < self._min_frames_for_turn_complete
                and not self._response_started
            ):
                logger.debug(
                    "Ignoring early response.done (sent=%d < %d, no response yet)",
                    self._frames_sent,
                    self._min_frames_for_turn_complete,
                )
                return

            # Log full transcripts at end of turn
            if self._user_transcript.strip():
                logger.info("\U0001f4dd USER: %s", self._user_transcript.strip())
            if self._ai_transcript.strip():
                logger.info("\U0001f916 AI: %s", self._ai_transcript.strip())

            logger.info("API: turn_complete (response.done, status=%s)", status)
            if self._on_turn_complete:
                self._on_turn_complete()

        # --- Server VAD events ---
        elif event_type == "input_audio_buffer.speech_started":
            logger.debug("Server VAD: speech started")

        elif event_type == "input_audio_buffer.speech_stopped":
            logger.debug("Server VAD: speech stopped")

        # --- Response created (reset audio tracking for truncation) ---
        elif event_type == "response.created":
            self._audio_bytes_received = 0
            self._current_response_item_id = None
            logger.debug("API: response.created")

        # --- Track assistant item ID (needed for conversation.item.truncate) ---
        # Docs lifecycle table uses .created, API reference uses .added; handle both
        elif event_type in (
            "response.output_item.added",
            "response.output_item.created",
        ):
            item = event.get("item", {})
            if item.get("role") == "assistant":
                self._current_response_item_id = item.get("id")
                self._is_response_item_in_progress = True
                logger.debug(
                    "Tracking assistant item: %s", self._current_response_item_id
                )

        # --- Session events ---
        elif event_type == "session.updated":
            logger.debug("Session configuration updated")

        # --- Error ---
        elif event_type == "error":
            error = event.get("error", {})
            logger.error(
                "API error: [%s] %s",
                error.get("type", "unknown"),
                error.get("message", ""),
            )

        # --- Informational events (debug level) ---
        elif event_type in (
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
            "response.output_audio.done",
            "response.output_audio_transcript.done",
            "response.output_text.done",
            "input_audio_buffer.committed",
            "input_audio_buffer.cleared",
            "input_audio_buffer.timeout_triggered",
            "input_audio_buffer.dtmf_event_received",
            "conversation.item.added",
            "conversation.item.done",
            "conversation.item.deleted",
            "conversation.item.truncated",
            "conversation.item.input_audio_transcription.delta",
            "conversation.item.input_audio_transcription.segment",
            "conversation.item.input_audio_transcription.failed",
            "output_audio_buffer.started",
            "output_audio_buffer.stopped",
            "output_audio_buffer.cleared",
            "rate_limits.updated",
        ):
            logger.debug("API: %s", event_type)

        else:
            logger.debug("Unhandled event: %s", event_type)

    async def _handle_tool_call(self, tool_call: dict):
        """Handle a tool call from the API based on the new documentation."""
        call_id = tool_call.get("call_id")
        function_name = tool_call.get("name")
        
        try:
            # Arguments are a JSON string
            args = json.loads(tool_call.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}

        logger.info(f"Tool call: {function_name} with args {args}")
        
        result_payload = {}
        try:
            func = get_function(function_name)
            result = func(**args)
            result_payload = {"result": result}
            logger.info(f"Tool call {function_name} successful, result: {result}")
        except Exception as e:
            logger.error(f"Error executing tool call {function_name}: {e}")
            result_payload = {"error": f"Error executing function: {e}"}

        try:
            # Send the result back to the API
            await self._ws_send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result_payload),
                    },
                }
            )
            # Ask the model to respond to the tool call result
            await self._ws_send({"type": "response.create"})
        except Exception as e:
            logger.error(f"Failed to send tool call result for {function_name}: {e}")
