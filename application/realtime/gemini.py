import asyncio
import inspect
import logging
import time
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
from websockets.exceptions import ConnectionClosed

import metrics
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

        # Output silence tracking: reset each turn via start_new_turn override below
        self._consecutive_silent_chunks: int = 0

        # Set when the server announces session termination (GoAway); lets the
        # receive task treat the subsequent connection close as a normal end of
        # session instead of an error (no error sound, no state reset).
        self._go_away_received: bool = False

        # generation_complete often comes many seconds before turn_complete
        # (measured 4-19 s on 01.11.2026): see _end_turn_after_generation.
        self._generation_end_task: Optional[asyncio.Task] = None
        self._late_turn_completes: int = 0
        # Per-turn timeline (seconds from the end of the user's input), logged
        # when turn_complete arrives: where a slow turn spends its time.
        self._timing: dict = {}
        self._late_timing_logged: int = 0  # late turn_completes whose timing went out already
        # live.trace_api_events: every message to and from the API, one line per turn
        self._trace: list[list] = []  # [kind, first, last (monotonic), count, seconds of audio]
        self._trace_ref: Optional[float] = None  # end of the user's input: time 0 in the trace
        # Silent chunks are held back until the model speaks again (then they
        # play: a pause) or generation_complete comes (then they were trailing
        # padding: up to 20 s of it on the speaker, 01.11.2026).
        self._held_silence: list[bytes] = []
        self._ended_early: bool = False  # this turn already ended before turn_complete

    def _silence_run_is_notable(self, run_length: int) -> bool:
        """Is this silent-chunk run long enough to warrant a warning?

        Warns once the run reaches output_silence_warn_after, then every
        output_silence_warn_every chunks for as long as the silence continues.
        """
        warn_after = self.live_cfg.output_silence_warn_after
        warn_every = self.live_cfg.output_silence_warn_every
        if run_length < warn_after:
            return False
        if run_length == warn_after:
            return True
        return warn_every > 0 and (run_length - warn_after) % warn_every == 0

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
        self._held_silence = []
        self._ended_early = False

    def _trace_event(self, kind: str, audio_seconds: float = 0.0) -> None:
        """Records an API event; consecutive events of one kind become one entry."""
        if not self.live_cfg.trace_api_events:
            return
        now = time.monotonic()
        if self._trace and self._trace[-1][0] == kind:
            last = self._trace[-1]
            last[2], last[3], last[4] = now, last[3] + 1, last[4] + audio_seconds
        else:
            self._trace.append([kind, now, now, 1, audio_seconds])

    def _trace_flush(self) -> None:
        if not self._trace:
            return
        ref = self._trace_ref or self._trace[0][1]
        parts = []
        for kind, a, b, n, sec in self._trace:
            a, b = round(a - ref, 2), round(b - ref, 2)
            span = f"{a}s" if n == 1 else f"{a}-{b}s"
            extra = f" x{n}" if n > 1 else ""
            extra += f" ({sec:.1f}s audio)" if sec else ""
            parts.append(f"{span} {kind}{extra}")
        self._trace = []
        logger.info("Turn trace: %s", " | ".join(parts))

    def _timing_start(self) -> None:
        if self._timing:
            # the previous turn ended early and its turn_complete has not come yet
            self._timing_log("next input before its turn_complete")
            if self._late_turn_completes:
                self._late_timing_logged += 1
        self._timing = {"t0": time.monotonic(), "audio": 0.0, "silent": 0.0}
        self._trace_ref = self._timing["t0"]

    def _timing_mark(self, key: str) -> None:
        if self._timing and key not in self._timing:
            self._timing[key] = round(time.monotonic() - self._timing["t0"], 1)

    def _timing_log(self, how: str) -> None:
        t = self._timing
        if not t:
            return
        self._trace_flush()
        self._timing = {}
        logger.info(
            "Turn timing (%s): first_audio=%ss first_voice=%ss generation_complete=%ss "
            "turn_complete=%.1fs audio=%.1fs of which silent=%.1fs, trailing silence not played=%.1fs",
            how, t.get("first_audio", "-"), t.get("first_voice", "-"),
            t.get("generation_complete", "-"), time.monotonic() - t["t0"], t["audio"], t["silent"],
            t.get("trimmed", 0.0),
        )

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
            system_instruction=self._instructions(),
            temperature=self.live_cfg.temperature,
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

        if not await self._wait_for_session(
            self.live_cfg.session_open_timeout, "Gemini"
        ):
            # Cancel the retry loop so it cannot open a session (or fire _on_error
            # for a second error sound) after the caller has given up on us.
            await self.close_session()

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
        self._cancel_generation_end()
        self._late_turn_completes = 0

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
            self._trace_event("→activity_start")
            self._activity_started = True
            logger.info("Sent activity_start to API")
        except ConnectionClosed as e:
            # The session can be closed between the checks above and the send
            # (e.g. initial-silence close racing local VAD) — normal shutdown,
            # not a failure worth an ERROR in the log.
            logger.debug("Session closed while sending activity_start: %s", e)
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
            self._timing_start()
            self._trace_event("→activity_end")
            logger.info("Sent activity_end to API")
        except ConnectionClosed as e:
            # The session can be closed between the checks above and the send
            # (e.g. initial-silence close racing local VAD) — normal shutdown,
            # not a failure worth an ERROR in the log.
            logger.debug("Session closed while sending activity_end: %s", e)
        except Exception as e:
            logger.error("Failed to send activity_end: %s", e)

    async def send_text(self, text: str) -> bool:
        if not self._session_active or self._session is None:
            return False
        self._begin_text_turn(text)
        self._timing_start()
        self._trace_event("→text")
        try:
            await self._session.send_realtime_input(text=text)
            logger.info("Sent text turn to API")
            return True
        except Exception as e:
            self._waiting_for_turn_complete = False
            logger.error("Failed to send text turn: %s", e)
            return False

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
                        self._late_turn_completes = 0
                        self._go_away_received = False
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
                self._trace_event("→audio", len(frame.data) / 2 / 16000)

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
                if self._go_away_received:
                    logger.warning(
                        "Session closed by server after GoAway — ending turn gracefully"
                    )
                    self._session_active = False
                    if self._on_turn_complete:
                        self._on_turn_complete()
                    break
                logger.error("Receive from API error: %s", e)
                traceback.print_exc()
                self._session_active = False
                if self._on_error:
                    self._on_error()
                elif self._on_turn_complete:
                    self._on_turn_complete()
                break

    def _trace_response(self, response: LiveServerMessage) -> None:
        if not self.live_cfg.trace_api_events:
            return
        if getattr(response, "setup_complete", None):
            self._trace_event("←setup_complete")
        calls = getattr(getattr(response, "tool_call", None), "function_calls", None)
        if calls:
            self._trace_event("←tool_call " + ",".join(str(getattr(c, "name", "?")) for c in calls))
        if getattr(response, "go_away", None):
            self._trace_event("←go_away")
        if getattr(response, "session_resumption_update", None):
            self._trace_event("←resumption_update")
        um = getattr(response, "usage_metadata", None)
        if um:
            self._trace_event(f"←usage(prompt={getattr(um, 'prompt_token_count', None)} "
                              f"out={getattr(um, 'response_token_count', None)} "
                              f"thoughts={getattr(um, 'thoughts_token_count', None)})")
        sc = getattr(response, "server_content", None)
        if not sc:
            return
        mt = getattr(sc, "model_turn", None)
        if mt and getattr(mt, "parts", None):
            for part in mt.parts:
                data = getattr(part, "inline_data", None)
                if data and isinstance(data.data, bytes) and data.data:
                    rms = self._pcm16_rms(data.data)
                    kind = "←silence" if rms < self.live_cfg.output_silence_rms_threshold else "←voice"
                    self._trace_event(kind, len(data.data) / 2 / self.config.audio.output_sample_rate)
                elif getattr(part, "text", None):
                    self._trace_event("←text")
        if getattr(getattr(sc, "input_transcription", None), "text", None):
            self._trace_event("←in_transcript")
        if getattr(getattr(sc, "output_transcription", None), "text", None):
            self._trace_event("←out_transcript")
        for flag in ("generation_complete", "turn_complete", "interrupted", "waiting_for_input"):
            if getattr(sc, flag, None):
                self._trace_event("←" + flag)
        if getattr(sc, "grounding_metadata", None):
            self._trace_event("←grounding")

    async def _process_api_response(self, response: LiveServerMessage) -> None:
        """Process a single response from the Gemini API.

        The watchdog is intentionally reset ONLY when audio chunks arrive, not on
        every API message.  This prevents the watchdog from being kept alive
        indefinitely by transcription updates or empty heartbeats when Gemini has
        already finished speaking but hasn't sent turn_complete yet.
        """
        self._trace_response(response)
        if getattr(response, "go_away", None):
            # Server announces imminent session termination (e.g. max session
            # duration). Mark it so the coming connection close is handled as a
            # graceful end of session, not an API error.
            self._go_away_received = True
            logger.warning(
                "API sent GoAway (time_left=%s) — session will close soon",
                response.go_away.time_left,
            )
            return

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
                                seconds = len(data.data) / 2 / self.config.audio.output_sample_rate
                                if self._timing:
                                    self._timing["audio"] += seconds
                                    self._timing_mark("first_audio")
                                    if is_silent:
                                        self._timing["silent"] += seconds
                                    else:
                                        self._timing_mark("first_voice")

                                if is_silent:
                                    self._consecutive_silent_chunks += 1
                                    if self._silence_run_is_notable(
                                        self._consecutive_silent_chunks
                                    ):
                                        logger.warning(
                                            "Receiving SILENT audio from Gemini (chunk #%d, rms=%.5f, consecutive_silent=%d) — watchdog NOT reset",
                                            self._chunks_received,
                                            rms,
                                            self._consecutive_silent_chunks,
                                        )
                                    else:
                                        logger.debug(
                                            "Silent chunk #%d (rms=%.5f, consecutive_silent=%d)",
                                            self._chunks_received,
                                            rms,
                                            self._consecutive_silent_chunks,
                                        )
                                else:
                                    if self._consecutive_silent_chunks > 0:
                                        log = (
                                            logger.info
                                            if self._consecutive_silent_chunks
                                            >= self.live_cfg.output_silence_warn_after
                                            else logger.debug
                                        )
                                        log(
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

                                if is_silent:
                                    self._held_silence.append(data.data)
                                    self._end_turn_on_long_silence()
                                    continue
                                self._cancel_generation_end()  # the model is still talking
                                # A pause in the speech plays, but at most max_played_pause:
                                # 6 s of silence came before a 0.1 s blip (01.11.2026).
                                budget = int(self.live_cfg.max_played_pause * self.config.audio.output_sample_rate * 2)
                                for held in self._held_silence:
                                    if budget <= 0:
                                        break
                                    await self._output_queue.put(held[:budget])
                                    budget -= len(held)
                                self._held_silence = []

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
                    # Discard transcription that arrives after a new turn started
                    # (Gemini sends async; may arrive after start_new_turn() cleared buffers)
                    if self._frames_sent >= self._min_frames_for_turn_complete or self._activity_started:
                        self._user_transcript += sc.input_transcription.text

            # Handle output transcription (AI's speech -> text)
            if hasattr(sc, "output_transcription") and sc.output_transcription:
                if (
                    hasattr(sc.output_transcription, "text")
                    and sc.output_transcription.text
                ):
                    self._ai_transcript += sc.output_transcription.text

            if getattr(sc, "generation_complete", False):
                self._timing_mark("generation_complete")
                if self._held_silence and self._timing:
                    self._timing["trimmed"] = sum(len(c) for c in self._held_silence) / 2 / self.config.audio.output_sample_rate
                self._held_silence = []  # trailing padding: not played
            if (
                getattr(sc, "generation_complete", False)
                and self._chunks_received > 0  # the model answered this turn
                and self.live_cfg.generation_complete_grace > 0
            ):
                self._schedule_turn_end()

            # Handle turn complete
            if hasattr(sc, "turn_complete") and sc.turn_complete:
                self._cancel_generation_end()
                if self._late_turn_completes:
                    # Belongs to a turn already ended after generation_complete; it
                    # must not end (or unblock) the turn that may be running now.
                    self._late_turn_completes -= 1
                    logger.info("Late turn_complete of a turn already ended — ignored")
                    if self._late_timing_logged:
                        self._late_timing_logged -= 1
                    else:
                        self._timing_log("late")
                    return
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
                self._log_transcripts()
                logger.info("API: turn_complete")
                self._timing_log("on time")
                if self._on_turn_complete:
                    self._on_turn_complete()

            # Handle interruption signal from API
            if hasattr(sc, "interrupted") and sc.interrupted:
                logger.info("API: interrupted signal")
                if self._on_interrupted:
                    self._on_interrupted()

        if hasattr(response, "usage_metadata") and response.usage_metadata:
            um = response.usage_metadata
            if um.prompt_token_count:
                metrics.TOKENS_INPUT.labels(provider='gemini').inc(um.prompt_token_count)
            if um.response_token_count:
                metrics.TOKENS_OUTPUT.labels(provider='gemini').inc(um.response_token_count)

        if hasattr(response, "setup_complete") and response.setup_complete:
            logger.debug("API: setup_complete")

        elif hasattr(response, "tool_call") and response.tool_call:
            self._cancel_generation_end()
            logger.info("API: tool_call received")
            if response.tool_call.function_calls:
                await self._handle_tool_calls(response.tool_call.function_calls)

    def _log_transcripts(self) -> None:
        if self._user_transcript.strip():
            logger.info("📝 USER: %s", self._user_transcript.strip())
        if self._ai_transcript.strip():
            logger.info("🤖 AI: %s", self._ai_transcript.strip())

    def _cancel_generation_end(self) -> None:
        if self._generation_end_task and not self._generation_end_task.done():
            self._generation_end_task.cancel()
        self._generation_end_task = None

    def _schedule_turn_end(self) -> None:
        if self._ended_early:
            return
        self._cancel_generation_end()
        self._generation_end_task = asyncio.create_task(
            self._end_turn_after_generation(), name="generation_end"
        )

    def _end_turn_on_long_silence(self) -> None:
        """Treat a long run of silence after speech like generation_complete:
        once Gemini generated 76 s of silence and sent generation_complete
        only after 12 s (01.11.2026)."""
        limit = self.live_cfg.trailing_silence_end
        if not limit or self._ended_early or self._generation_end_task:
            return
        if "first_voice" not in self._timing:
            return  # nothing said yet: leading silence
        held = sum(len(c) for c in self._held_silence) / 2 / self.config.audio.output_sample_rate
        if held >= limit:
            logger.info("%.1fs of silence after the answer — treating it as its end", held)
            self._schedule_turn_end()

    async def _end_turn_after_generation(self) -> None:
        """Ends the turn when turn_complete lags behind generation_complete.

        The model has finished speaking, but turn_complete often arrives 4-19 s
        later (01.11.2026). Meanwhile the speaker sat in silence and dropped
        the next activity_start. After generation_complete_grace with nothing
        new from the model, the turn ends here and the late turn_complete is
        ignored when it comes.
        """
        grace = self.live_cfg.generation_complete_grace
        await asyncio.sleep(grace)
        self._generation_end_task = None
        self._ended_early = True
        if self._held_silence and self._timing:
            self._timing["trimmed"] = sum(len(c) for c in self._held_silence) / 2 / self.config.audio.output_sample_rate
        self._held_silence = []  # trailing padding: not played
        self._late_turn_completes += 1
        self._waiting_for_turn_complete = False
        self.cancel_watchdog()
        self._log_transcripts()
        logger.info("API: generation_complete, no turn_complete within %.1fs — ending the turn", grace)
        if self._on_turn_complete:
            self._on_turn_complete()

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
                elif function_name in self.DEFERRED_FUNCTIONS:
                    func = get_function(function_name)
                    # Defer execution by getting metadata from the function call
                    payload = (
                        await func(**args)
                        if inspect.iscoroutinefunction(func)
                        else func(**args)
                    )
                    if isinstance(payload, dict) and payload.get("status") == "error":
                        # The lookup already failed: nothing to defer. Hand the model the
                        # real error, or it announces a station that never starts.
                        logger.warning(
                            f"'{function_name}' failed before deferral: {payload.get('details')}"
                        )
                        result = payload
                    else:
                        # Add to the queue for the orchestrator to handle later
                        self._defer(function_name, payload)
                        logger.info(
                            f"Deferring '{function_name}' for post-response execution. Payload: {payload}"
                        )
                        # Lie to the model, telling it the action was successful to not block conversation flow
                        # Facts the function already knows (e.g. the radio is already
                        # playing) go back with it, so the reply matches what will happen.
                        result = {"status": "success", **payload.get("for_model", {})}

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

        # Send all responses (real or fake) back to the API.
        #
        # NOTE: _handle_tool_calls is intentionally awaited inline in the
        # receive loop, even though a slow smart-home call (up to the HTTP
        # timeout) delays processing of subsequent API messages. Running it
        # as a background task would let turn_complete be processed BEFORE
        # pending_actions is populated — the orchestrator would then dispatch
        # deferred actions too early and e.g. the radio would not start until
        # the next wake word. Ordering is load-bearing here.
        logger.debug(f"Sending tool responses to API: {function_responses}")
        try:
            await self._session.send_tool_response(function_responses=function_responses)
            self._trace_event("→tool_response " + ",".join(r.name for r in function_responses))
        except Exception as e:
            logger.error("Failed to send tool responses: %s", e)
            return

        # Start watchdog in case API hangs after function call without sending turn_complete
        self.start_watchdog(timeout=self.live_cfg.turn_watchdog_timeout)
