import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

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
from realtime.base import INPUT_POLL_INTERVAL, BaseRealtimeManager
from realtime.reply_audio import ReplyAudio
from realtime.turn_log import TurnLog
from functions.schemas import GEMINI_TOOLS as TOOLS

logger = logging.getLogger(__name__)

_PCM16_BYTES_PER_SAMPLE = 2


class GeminiRealtimeManager(BaseRealtimeManager):
    """
    Manages Gemini Live API session lifecycle with Hybrid VAD support.

    Handles:
    - Session connection/disconnection
    - Send/receive tasks
    - Response processing
    - Manual VAD signals (activity_start/activity_end)
    """

    provider_name = "Gemini"

    def __init__(
        self,
        config: "AppConfig",
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        super().__init__(config, input_queue, output_queue)

        # Client and session
        self._client: Optional[genai.Client] = None
        self._session: Optional[AsyncSession] = None

        # Set when the server announces session termination (GoAway); lets the
        # receive task treat the subsequent connection close as a normal end of
        # session instead of an error (no error sound, no state reset).
        self._go_away_received: bool = False

        # generation_complete often comes many seconds before turn_complete
        # (measured 4-19 s on 01.11.2026): see _end_turn_after_generation.
        self._generation_end_task: Optional[asyncio.Task] = None
        self._late_turn_completes: int = 0
        self._reply = self._new_reply()
        self._turn_log = TurnLog(self.live_cfg)

    def _new_reply(self) -> ReplyAudio:
        return ReplyAudio(self.live_cfg, self.config.audio.output_sample_rate)

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
        # A grace timer of the previous turn must not end this one (barge-in).
        # Its generation_complete came, so its turn_complete is still to come.
        if self._generation_end_task and not self._generation_end_task.done():
            self._late_turn_completes += 1
        self._cancel_generation_end()
        self._reply = self._new_reply()

    def _on_watchdog_timeout(self) -> None:
        # The turn ended without turn_complete: a late one expected for an
        # earlier turn did not come either, so stop waiting for it.
        if self._late_turn_completes:
            logger.info("Dropping %d expected late turn_complete(s) after the watchdog", self._late_turn_completes)
            self._late_turn_completes = 0

    def _drop_held_silence(self) -> None:
        """Trailing silence: not played."""
        trimmed = self._reply.drop_held()
        if trimmed:
            self._turn_log.note("trimmed", trimmed)

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
            # Without the language the transcript guessed it ("cocina" for
            # "Krystyna, zgaś telewizję", 02.11.2026).
            input_audio_transcription=AudioTranscriptionConfig(language_codes=[self.live_cfg.lang]),
            output_audio_transcription=AudioTranscriptionConfig(),
            # Enable Google Search tool
            tools=TOOLS + [Tool(google_search=GoogleSearch())],
        )

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[None]:
        async with self._client.aio.live.connect(
            model=self.live_cfg.model,
            config=self._get_session_config(),
        ) as session:
            self._session = session
            self._late_turn_completes = 0
            self._go_away_received = False
            try:
                yield
            finally:
                self._session = None

    def _on_session_closed(self) -> None:
        self._turn_log.session_closed()

    async def close_session(self) -> None:
        """Close the active session by cancelling the conversation task.

        The genai client is kept alive across sessions so its connection pool
        can be reused — calling aio.aclose() would permanently close httpx and
        break every subsequent open_session() call.
        """
        await super().close_session()
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
            self._turn_log.event("→activity_start")
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
            self._turn_log.start(bool(self._late_turn_completes))
            self._turn_log.event("→activity_end")
            logger.info("Sent activity_end to API")
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
        if not self._session_active or self._session is None:
            return False
        self._begin_text_turn(text)
        self._turn_log.start(bool(self._late_turn_completes))
        self._turn_log.event("→text")
        try:
            await self._session.send_realtime_input(text=text)
            logger.info("Sent text turn to API")
            return True
        except Exception as e:
            self._waiting_for_turn_complete = False
            logger.error("Failed to send text turn: %s", e)
            return False

    async def _send_to_api_task(self) -> None:
        """Send audio frames from input queue to Gemini API."""
        while self._running and self._session_active:
            try:
                try:
                    frame = await asyncio.wait_for(
                        self._input_queue.get(), timeout=INPUT_POLL_INTERVAL
                    )
                except asyncio.TimeoutError:
                    continue

                # Send to API
                await self._session.send_realtime_input(
                    audio={"data": frame.data, "mime_type": frame.mime_type}
                )
                self._frames_sent += 1
                self._turn_log.event(
                    "→audio",
                    len(frame.data) / _PCM16_BYTES_PER_SAMPLE / self.config.audio.input_sample_rate,
                )

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
                logger.exception("Receive from API error: %s", e)
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
            self._turn_log.event("←setup_complete")
        calls = getattr(getattr(response, "tool_call", None), "function_calls", None)
        if calls:
            self._turn_log.event("←tool_call " + ",".join(str(getattr(c, "name", "?")) for c in calls))
        if getattr(response, "go_away", None):
            self._turn_log.event("←go_away")
        if getattr(response, "session_resumption_update", None):
            self._turn_log.event("←resumption_update")
        um = getattr(response, "usage_metadata", None)
        if um:
            self._turn_log.event(f"←usage(prompt={getattr(um, 'prompt_token_count', None)} "
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
                    kind = "←silence" if self._reply.is_silent(data.data) else "←voice"
                    self._turn_log.event(kind, self._reply.seconds(len(data.data)))
                elif getattr(part, "text", None):
                    self._turn_log.event("←text")
        if getattr(getattr(sc, "input_transcription", None), "text", None):
            self._turn_log.event("←in_transcript")
        if getattr(getattr(sc, "output_transcription", None), "text", None):
            self._turn_log.event("←out_transcript")
        for flag in ("generation_complete", "turn_complete", "interrupted", "waiting_for_input"):
            if getattr(sc, flag, None):
                self._turn_log.event("←" + flag)
        if getattr(sc, "grounding_metadata", None):
            self._turn_log.event("←grounding")

    async def _process_api_response(self, response: LiveServerMessage) -> None:
        """Process a single response from the Gemini API.

        The watchdog is intentionally reset ONLY when audio chunks arrive, not on
        every API message.  This prevents the watchdog from being kept alive
        indefinitely by transcription updates or empty heartbeats when Gemini has
        already finished speaking but hasn't sent turn_complete yet.
        """
        self._trace_response(response)
        go_away = getattr(response, "go_away", None)
        if go_away:
            # Server announces imminent session termination (e.g. max session
            # duration). Mark it so the coming connection close is handled as a
            # graceful end of session, not an API error.
            self._go_away_received = True
            logger.warning(
                "API sent GoAway (time_left=%s) — session will close soon", go_away.time_left
            )
            return

        sc = getattr(response, "server_content", None)
        if sc and not await self._handle_server_content(sc):
            return

        um = getattr(response, "usage_metadata", None)
        if um:
            if um.prompt_token_count:
                metrics.TOKENS_INPUT.labels(provider='gemini').inc(um.prompt_token_count)
            if um.response_token_count:
                metrics.TOKENS_OUTPUT.labels(provider='gemini').inc(um.response_token_count)

        if getattr(response, "setup_complete", None):
            logger.debug("API: setup_complete")
            return
        tool_call = getattr(response, "tool_call", None)
        if tool_call:
            self._cancel_generation_end()
            logger.info("API: tool_call received")
            if tool_call.function_calls:
                await self._handle_tool_calls(tool_call.function_calls)

    async def _handle_server_content(self, sc) -> bool:
        """False when the message carried a turn_complete that was ignored:
        nothing else in it is processed."""
        model_turn = getattr(sc, "model_turn", None)
        for part in (model_turn.parts or []) if model_turn else []:
            await self._handle_part(part)

        # Transcription arrives asynchronously, possibly after start_new_turn()
        # cleared the buffers: what belongs to the previous turn is dropped.
        user_text = getattr(getattr(sc, "input_transcription", None), "text", None)
        if user_text and (self._accepts_reply() or self._activity_started):
            self._user_transcript += user_text
        ai_text = getattr(getattr(sc, "output_transcription", None), "text", None)
        if ai_text:
            self._ai_transcript += ai_text

        if getattr(sc, "generation_complete", False):
            self._turn_log.mark("generation_complete")
            self._drop_held_silence()
            # only when the model answered this turn
            if self._chunks_received > 0 and self.live_cfg.generation_complete_grace > 0:
                self._schedule_turn_end()

        if getattr(sc, "turn_complete", False) and not self._handle_turn_complete():
            return False

        if getattr(sc, "interrupted", False):
            logger.info("API: interrupted signal")
            if self._on_interrupted:
                self._on_interrupted()
        return True

    async def _handle_part(self, part) -> None:
        data = getattr(getattr(part, "inline_data", None), "data", None)
        if isinstance(data, bytes) and data:
            await self._handle_audio(data)

        text = getattr(part, "text", None)
        if text:
            logger.info("Text: %s", text[:200])
            self.response_started = True

        function_call = getattr(part, "function_call", None)
        if function_call:
            await self._handle_tool_calls([function_call])

    async def _handle_audio(self, data: bytes) -> None:
        """One chunk of the model's audio: decides whether it is played."""
        if self._on_model_audio:
            self._on_model_audio(data)
        if self.playback_interrupted:
            return  # barge-in
        if not self._accepts_reply() and self._chunks_received == 0:
            logger.debug(
                "Ignoring early audio (sent=%d < %d)",
                self._frames_sent, self._min_frames_for_turn_complete,
            )
            return

        self._chunks_received += 1
        chunk = self._reply.take(data, len(self._ai_transcript.strip()))
        self._turn_log.add_audio(chunk.seconds, chunk.silent)

        # The first chunk starts the watchdog (response_started setter), later
        # voice extends it. Silent chunks do not: Gemini streaming silence
        # indefinitely must eventually time out.
        if not self._response_started:
            self.response_started = True
        elif not chunk.silent:
            self.reset_watchdog_if_running()

        if chunk.silent:
            self._end_turn_on_long_silence()
            return
        if chunk.over_budget:
            self._turn_log.note("dropped", self._reply.voice_dropped)
            if not self._generation_end_task:
                self._schedule_turn_end()
            return
        self._cancel_generation_end()  # the model is still talking
        for audio in chunk.play:
            await self._output_queue.put(audio)
        if self._chunks_received == 1:
            logger.info(
                "Receiving audio response (chunk_bytes=%d, rms=%.5f)...", len(data), chunk.rms
            )

    def _handle_turn_complete(self) -> bool:
        """False when the turn_complete was ignored (late or stale)."""
        self._cancel_generation_end()
        if self._late_turn_completes:
            # Belongs to a turn already ended early (Gemini answers in order).
            # It must not end (or unblock) the turn that may be running now.
            # If it never comes, the watchdog ends this turn and drops the count.
            self._late_turn_completes -= 1
            logger.info("Late turn_complete of a turn already ended — ignored")
            self._turn_log.log_late_turn_complete()
            return False
        self._waiting_for_turn_complete = False

        # Ignore turn_complete if we haven't sent enough audio yet — likely a
        # stale signal from the previous turn after barge-in. Do NOT cancel the
        # watchdog here: it must keep running so the session doesn't hang.
        if not self._accepts_reply() and not self._response_started:
            logger.debug(
                "Ignoring early turn_complete (sent=%d < %d, no response yet)",
                self._frames_sent, self._min_frames_for_turn_complete,
            )
            return False

        # Cancel watchdog only for a real (non-stale) turn_complete
        self.cancel_watchdog()
        self._log_transcripts()
        self._check_voice_matches_transcript()
        logger.info("API: turn_complete")
        self._turn_log.log("on time")
        if self._on_turn_complete:
            self._on_turn_complete()
        return True

    def _check_voice_matches_transcript(self) -> None:
        """Counts answers whose audio is too short for their transcript."""
        chars = len(self._ai_transcript.strip())
        if self.playback_interrupted or not self._reply.is_incomplete(chars):
            return
        metrics.TRUNCATED_ANSWERS.inc()
        logger.warning(
            "Answer audio incomplete: %.1fs of voice for a %d-character transcript",
            self._reply.voice_played, chars,
        )

    def _cancel_generation_end(self) -> None:
        if self._generation_end_task and not self._generation_end_task.done():
            self._generation_end_task.cancel()
        self._generation_end_task = None

    def _schedule_turn_end(self) -> None:
        if self._reply.ended_early:
            return
        self._cancel_generation_end()
        self._generation_end_task = asyncio.create_task(
            self._end_turn_after_generation(), name="generation_end"
        )

    def _end_turn_on_long_silence(self) -> None:
        """Treat a long run of silence after speech like generation_complete."""
        if self._reply.ended_early or self._generation_end_task:
            return  # done, or already ending
        if self._reply.trailing_silence_is_long():
            logger.info(
                "%.1fs of silence after the answer — treating it as its end",
                self._reply.seconds(self._reply.held_bytes),
            )
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
        self._reply.ended_early = True
        self._drop_held_silence()
        self._late_turn_completes += 1
        self._waiting_for_turn_complete = False
        self.cancel_watchdog()
        self._log_transcripts()
        self._check_voice_matches_transcript()
        logger.info("API: generation_complete, no turn_complete within %.1fs — ending the turn", grace)
        if self._on_turn_complete:
            self._on_turn_complete()

    async def _handle_tool_calls(self, function_calls: list[FunctionCall]) -> None:
        """Run the tool calls (see _execute_tool) and send their results back."""
        function_responses = []
        for function_call in function_calls:
            result = await self._execute_tool(function_call.name, function_call.args or {})
            function_responses.append(
                FunctionResponse(
                    id=function_call.id,
                    name=function_call.name,
                    response=result if isinstance(result, dict) else {"result": str(result)},
                )
            )

        # NOTE: _handle_tool_calls is intentionally awaited inline in the
        # receive loop, even though a slow smart-home call (up to the HTTP
        # timeout) delays processing of subsequent API messages. Running it
        # as a background task would let turn_complete be processed BEFORE
        # pending_actions is populated — the orchestrator would then dispatch
        # deferred actions too early and e.g. the radio would not start until
        # the next wake word. Ordering is load-bearing here.
        try:
            await self._session.send_tool_response(function_responses=function_responses)
            self._turn_log.event("→tool_response " + ",".join(r.name for r in function_responses))
        except Exception as e:
            logger.error("Failed to send tool responses: %s", e)
            return

        # Start watchdog in case API hangs after function call without sending turn_complete
        self.start_watchdog()
