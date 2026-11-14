"""OpenAI Realtime API session management via WebSocket.

Uses raw WebSocket connection for maximum control and compatibility.
Audio format: PCM16 at 24kHz, base64 encoded over WebSocket.
Input audio from mic (16kHz) is resampled to 24kHz before sending.
"""

import asyncio
import base64
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import numpy as np
import websockets
from scipy.signal import resample_poly
from websockets.exceptions import ConnectionClosed

import metrics
from config import AppConfig
from realtime.base import INPUT_POLL_INTERVAL, BaseRealtimeManager, SessionRefused
from functions.schemas import OPENAI_TOOLS as TOOLS

logger = logging.getLogger(__name__)

# OpenAI Realtime API WebSocket endpoint
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
# The only rate the API takes and returns for audio/pcm.
OPENAI_SAMPLE_RATE = 24000
_PCM16_BYTES_PER_SAMPLE = 2
_WS_MAX_MESSAGE_BYTES = 2**24
# How often the receive task looks whether the session is still wanted.
_RECV_POLL_INTERVAL = 1.0


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

    provider_name = "OpenAI"

    def __init__(
        self,
        config: "AppConfig",
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        super().__init__(config, input_queue, output_queue)

        # WebSocket connection
        self._ws = None

        # API key
        self._api_key: str = ""

        # The microphone rate is resampled to the API's.
        self._need_resample = self.live_cfg.send_sample_rate != OPENAI_SAMPLE_RATE

        # Tracking for conversation.item.truncate on barge-in
        self._current_response_item_id: Optional[str] = None
        self._audio_bytes_received: int = 0
        self._is_response_item_in_progress: bool = False

        # Pending cancel/clear to execute before sending the first frame of a new turn.
        # None = no pending operation; True/False = value to pass as cancel_response.
        self._pending_cancel: Optional[bool] = None

    def initialize(self) -> None:
        """Initialize the OpenAI client."""
        api_key = self.config.api_keys.openai_api_key or os.environ.get(
            "OPENAI_API_KEY"
        )
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

        if self._waiting_for_turn_complete:
            logger.debug("Turn in progress (waiting for turn_complete), dropping activity_start")
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
            await asyncio.sleep(self.live_cfg.openai_commit_delay)

            # Commit the audio buffer -> creates user message item
            await self._ws_send({"type": "input_audio_buffer.commit"})

            # Trigger response generation
            await self._ws_send({"type": "response.create"})

            self._activity_started = False
            self._waiting_for_turn_complete = True
            logger.info("Sent activity_end to OpenAI (commit + response.create)")
            # Ends a turn the API never answers; audio and tool calls restart it.
            self.start_watchdog()
        except ConnectionClosed as e:
            # The session can be closed between the checks above and the send
            # (e.g. initial-silence close racing local VAD) — normal shutdown,
            # not a failure worth an ERROR in the log.
            logger.debug("Session closed while sending activity_end: %s", e)
        except Exception as e:
            logger.error("Failed to send activity_end: %s", e)

    async def send_text(self, text: str) -> bool:
        if not self._session_active or self._ws is None:
            return False
        self._begin_text_turn(text)
        try:
            # Run the cancel/clear start_new_turn queued now: left to the send
            # task, it could cancel this turn's response. It also drops the mic
            # audio appended so far, so that doesn't join the turn.
            cancel = bool(self._pending_cancel)
            self._pending_cancel = None  # before awaiting, or the send task runs it too
            await self._cancel_and_clear(cancel_response=cancel)
            await self._ws_send({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            })
            await self._ws_send({"type": "response.create"})
            logger.info("Sent text turn to OpenAI")
            return True
        except Exception as e:
            self._waiting_for_turn_complete = False
            logger.error("Failed to send text turn: %s", e)
            return False

    def start_new_turn(self) -> None:
        """Start a new conversation turn.

        Overrides base to also cancel any in-progress response, truncate
        assistant audio, and clear the input audio buffer on the OpenAI side.

        The cancel/clear is NOT fired as a background task here — that caused a
        race where new audio frames arrived at OpenAI before the buffer.clear
        message, so those frames were silently discarded (losing initial user
        speech after barge-in).  Instead we set a flag; _send_to_api_task checks
        it and awaits _cancel_and_clear() before forwarding the first new frame.
        """
        was_response_in_progress = self._is_response_item_in_progress
        super().start_new_turn()
        self._is_response_item_in_progress = False

        if self._ws and self._session_active:
            self._pending_cancel = was_response_in_progress

    async def _cancel_and_clear(self, cancel_response: bool = True) -> None:
        """Cancel in-progress response, truncate assistant audio, and clear input buffer."""
        try:
            if cancel_response:
                await self._ws_send({"type": "response.cancel"})

            # Truncate assistant audio to what was actually played.
            # This syncs the server's context with the client's playback position
            # so the model knows the user only heard part of the response.
            if self._current_response_item_id and self._audio_bytes_received > 0:
                audio_end_ms = int(
                    self._audio_bytes_received / _PCM16_BYTES_PER_SAMPLE / OPENAI_SAMPLE_RATE * 1000
                )
                await self._ws_send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": self._current_response_item_id,
                        "content_index": 0,
                        "audio_end_ms": audio_end_ms,
                    }
                )
                logger.info(
                    "Truncated assistant audio at %dms (item=%s)",
                    audio_end_ms,
                    self._current_response_item_id,
                )

            await self._ws_send({"type": "input_audio_buffer.clear"})

            log_msg = (
                "Cancelled response and cleared audio buffer"
                if cancel_response
                else "Cleared audio buffer"
            )
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
        """Resample PCM16 audio from the microphone rate to the API's."""
        if not self._need_resample:
            return audio_bytes

        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        if len(audio_int16) == 0:
            return audio_bytes

        divisor = math.gcd(OPENAI_SAMPLE_RATE, self.live_cfg.send_sample_rate)
        resampled = resample_poly(
            audio_int16.astype(np.float64),
            up=OPENAI_SAMPLE_RATE // divisor,
            down=self.live_cfg.send_sample_rate // divisor,
        )
        return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()

    # -------------------------------------------------------------------------
    # Session lifecycle
    # -------------------------------------------------------------------------

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[None]:
        model = self.live_cfg.openai_model
        async with websockets.connect(
            f"{OPENAI_REALTIME_URL}?model={model}",
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            max_size=_WS_MAX_MESSAGE_BYTES,
        ) as ws:
            self._ws = ws
            try:
                initial_event = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=self.live_cfg.session_open_timeout)
                )
                if initial_event.get("type") == "session.created":
                    logger.info(
                        "Session created (id=%s, model=%s)",
                        initial_event.get("session", {}).get("id", "unknown"), model,
                    )
                elif initial_event.get("type") == "error":
                    error = initial_event.get("error", {})
                    raise SessionRefused(
                        f"{error.get('type', 'unknown')} - {error.get('message', '')}"
                    )
                await self._configure_session()
                yield
            finally:
                self._ws = None

    async def _configure_session(self) -> None:
        """Send session configuration event.

        Follows the current API structure from:
        https://platform.openai.com/docs/guides/realtime-conversations#handling-audio-with-websockets
        """
        # Audio input config
        audio_input: dict = {
            "format": {
                "type": "audio/pcm",
                "rate": OPENAI_SAMPLE_RATE,
            },
            "transcription": {
                "model": "whisper-1",
            },
        }

        if self.live_cfg.enable_manual_vad:
            # Manual VAD - disable server-side turn detection
            audio_input["turn_detection"] = None
            logger.info("Manual VAD enabled - server-side turn detection DISABLED")
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
                "rate": OPENAI_SAMPLE_RATE,
            },
            "voice": self.live_cfg.openai_voice,
        }

        session_config: dict = {
            "type": "realtime",
            "model": self.live_cfg.openai_model,
            "output_modalities": ["audio"],
            "instructions": self._instructions(),
            "tools": TOOLS,
            "audio": {
                "input": audio_input,
                "output": audio_output,
            },
        }

        await self._ws_send(
            {
                "type": "session.update",
                "session": session_config,
            }
        )

    # -------------------------------------------------------------------------
    # Send / Receive tasks
    # -------------------------------------------------------------------------

    async def _send_to_api_task(self) -> None:
        """Send audio frames from input queue to OpenAI API."""
        while self._running and self._session_active:
            # Execute pending cancel/clear before forwarding any new frames.
            # This guarantees buffer.clear arrives at OpenAI before new audio,
            # preventing the barge-in race where initial user speech was dropped.
            if self._pending_cancel is not None:
                await self._cancel_and_clear(cancel_response=self._pending_cancel)
                self._pending_cancel = None

            try:
                try:
                    frame = await asyncio.wait_for(
                        self._input_queue.get(), timeout=INPUT_POLL_INTERVAL
                    )
                except asyncio.TimeoutError:
                    continue

                # Resample 16kHz -> 24kHz
                audio_data = self._resample_to_24k(frame.data)

                # Base64 encode for WebSocket transport
                audio_b64 = base64.b64encode(audio_data).decode("ascii")

                # Send to API
                await self._ws_send(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": audio_b64,
                    }
                )

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
                msg = await asyncio.wait_for(self._ws.recv(), timeout=_RECV_POLL_INTERVAL)
                event = json.loads(msg)
                await self._process_event(event)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Receive from API error: %s", e)
                self._session_active = False
                if self._on_error:
                    self._on_error()
                elif self._on_turn_complete:
                    self._on_turn_complete()
                break

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

        The watchdog is reset ONLY on audio chunks, not on every event.
        Transcription deltas, bookkeeping events and other informational messages
        must not keep the watchdog alive — otherwise a stuck turn can hang
        indefinitely while non-audio events keep arriving.
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
            if self.playback_interrupted:
                return

            # Ignore stale audio (same logic as Gemini manager)
            if not self._accepts_reply() and self._chunks_received == 0:
                logger.debug(
                    "Ignoring early audio (sent=%d < %d)",
                    self._frames_sent,
                    self._min_frames_for_turn_complete,
                )
                return

            self._chunks_received += 1
            self._audio_bytes_received += len(audio_data)
            # First chunk: response_started setter auto-starts the watchdog.
            # Subsequent chunks: extend the running watchdog.
            if not self._response_started:
                self.response_started = True  # starts watchdog via setter
            else:
                self.reset_watchdog_if_running()  # extends watchdog
            await self._output_queue.put(audio_data)

            if self._chunks_received == 1:
                logger.info("Receiving audio response...")

        # --- AI transcript (model's speech -> text) ---
        elif event_type == "response.output_audio_transcript.delta":
            delta = event.get("delta", "")
            if delta:
                self._ai_transcript += delta

        # --- User transcript (input audio transcription) ---
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                # Discard if a new turn already started (late delivery after barge-in)
                if self._accepts_reply() or self._activity_started:
                    self._user_transcript += transcript

        # --- Text response ---
        elif event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            if delta:
                logger.info("Text: %s", delta[:200])
                self.response_started = True

        # --- Response complete (= turn complete) ---
        elif event_type == "response.done":
            self._is_response_item_in_progress = False
            self._waiting_for_turn_complete = False
            response = event.get("response", {})
            status = response.get("status", "")
            usage = response.get("usage", {})
            if usage:
                if usage.get("input_tokens"):
                    metrics.TOKENS_INPUT.labels(provider='openai').inc(usage["input_tokens"])
                if usage.get("output_tokens"):
                    metrics.TOKENS_OUTPUT.labels(provider='openai').inc(usage["output_tokens"])

            # Handle tool calls if present in the output.
            # Intentionally awaited inline (not spawned as a task): a background
            # task would let the next response.done be processed before
            # pending_actions is populated, so the orchestrator could dispatch
            # deferred actions too early. Ordering is load-bearing here.
            if "output" in response and response["output"]:
                tool_calls_handled = False
                for item in response["output"]:
                    if item.get("type") == "function_call":
                        await self._handle_tool_call(item)
                        tool_calls_handled = True

                if tool_calls_handled:
                    return  # Stop further processing, as a new response will be created

            if status == "cancelled":
                logger.info("API: response cancelled (via response.done)")
                if self._on_interrupted:
                    self._on_interrupted()
                return

            # Ignore if we haven't sent enough audio — likely a stale signal after
            # barge-in. Do NOT cancel the watchdog: it must keep running so the
            # session doesn't hang.
            if not self._accepts_reply() and not self._response_started:
                logger.debug(
                    "Ignoring early response.done (sent=%d < %d, no response yet)",
                    self._frames_sent,
                    self._min_frames_for_turn_complete,
                )
                return

            # Cancel watchdog only for a real (non-stale) response.done
            self.cancel_watchdog()

            self._log_transcripts()

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

        # --- Audio output stream complete ---
        # OpenAI explicitly signals that all audio chunks have been sent.
        # At this point we know the model finished speaking; response.done should
        # follow shortly. Switch to a tighter watchdog so a stuck turn is caught
        # quickly without waiting for the full turn_watchdog_timeout.
        elif event_type == "response.output_audio.done":
            logger.debug("API: response.output_audio.done — audio stream complete")
            if self._response_started and self._watchdog_task and not self._watchdog_task.done():
                logger.debug(
                    "Tightening watchdog to %.1fs after audio stream ended",
                    self.live_cfg.audio_done_watchdog_timeout,
                )
                self.start_watchdog(timeout=self.live_cfg.audio_done_watchdog_timeout)

        # --- Informational events (debug level) ---
        elif event_type in (
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
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

    async def _handle_tool_call(self, tool_call: dict) -> None:
        """Run the tool call (see _execute_tool) and send its result back."""
        function_name = tool_call.get("name")
        try:
            # Arguments are a JSON string
            args = json.loads(tool_call.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}

        result = await self._execute_tool(function_name, args)
        # The API expects the output as a string.
        output = json.dumps(result) if isinstance(result, (dict, list)) else str(result)

        try:
            await self._ws_send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": tool_call.get("call_id"),
                        "output": output,
                    },
                }
            )
            # Ask the model to respond to the tool call result
            await self._ws_send({"type": "response.create"})
        except Exception as e:
            logger.error("Failed to send tool call result for %s: %s", function_name, e)
            return

        # Start watchdog in case API hangs after function call without sending turn_complete
        self.start_watchdog()
