"""Main orchestrator for the voice assistant with Hybrid VAD Strategy.

Architecture:
- SoundDevice callbacks (non-blocking) for audio I/O
- Silero VAD (ONNX) for precise speech boundary detection
- Manual VAD signals to API (activity_start/activity_end)
- Supports Gemini and OpenAI realtime providers (selected via config)
- API VAD serves as failsafe with longer timeout
"""

import asyncio
import logging
import queue
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Deque, Optional

import numpy as np

from audio.input import AudioInput
from audio.output import AudioOutput
from audio.recorder import SessionRecorder, save_clip
from audio.sounds import SoundEvent, SoundPlayer
from audio.vad import HybridVAD, create_hybrid_vad
from audio.verifier import WakeWordVerifier
from audio.wake_word import WakeWordDetector
from config import (
    AppConfig,
    AudioConfig,
    LiveConfig,
    SoundConfig,
    VADConfig,
    WakeWordConfig,
)
from deferred_playback import DeferredPlayback
from functions.definitions import (
    close_radio_client,
    drop_prefetched,
    get_radio_client,
    inject_mpd_client,
)
import metrics
from mpd_client.client import MPDClientWrapper
from realtime import BaseRealtimeManager, create_realtime_manager
from session_keeper import SessionKeeper
from state import AudioFrame, SpeakerState, WakeWordResult
from wake_gate import WakeGate
from tools.xvf_client import ReSpeakerLeds, open_respeaker
from tools.respeaker_monitor import respeaker_monitor_task

logger = logging.getLogger(__name__)


def _mode_label(relaxed: bool) -> str:
    """Metric label for the wake-word gate set that produced a detection."""
    return "relaxed" if relaxed else "strict"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class AudioOrchestrator:
    """
    Main orchestrator for the voice assistant with Hybrid VAD Strategy.

    Manages:
    - State machine transitions
    - Audio input/output pipelines (SoundDevice, non-blocking)
    - Wake word detection (in executor)
    - Silero VAD for speech boundary detection
    - Realtime API session lifecycle (Gemini / OpenAI)
    - Hard barge-in logic (instant audio cutoff)
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.live_cfg = config.live
        self.audio_cfg = config.audio
        self.wake_cfg = config.wake_word
        self.vad_cfg = config.vad
        self.sound_cfg = config.sound
        self.mpd_cfg = config.mpd
        self.mpd_client = MPDClientWrapper(config.mpd)

        # State
        self._state = SpeakerState.IDLE
        self._running = False
        self._last_activity_time: float = 0.0
        # True from the moment a wake word fires until the state leaves IDLE.
        # The session is opened during that window while the state is still
        # IDLE, so the inactivity monitor must not treat it as an idle session.
        self._wake_in_progress = False

        # Numbers the turns. Whoever ends a turn remembers its number: a
        # barge-in or a follow-up answer during an await starts the next one.
        self._turn_id = 0

        # Flags
        self._playback_interrupted = False

        # Thread-safe queues for audio I/O
        self._mic_queue: queue.Queue[np.ndarray] = queue.Queue(
            maxsize=self.audio_cfg.input_queue_maxsize
        )
        self._speaker_queue: queue.Queue[bytes] = queue.Queue(
            maxsize=self.audio_cfg.output_queue_maxsize
        )

        # Async queues for API communication
        self._api_input_queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=50)
        self._api_output_queue: asyncio.Queue[bytes] = asyncio.Queue()

        # Preroll buffer for capturing audio before wake word
        preroll_frames = int(
            self.live_cfg.preroll_seconds
            * self.audio_cfg.input_sample_rate
            / self.audio_cfg.input_chunk
        )
        self._preroll_buffer: Deque[bytes] = deque(maxlen=max(preroll_frames, 5))

        # Detector input ring buffer, dumped to a WAV at trigger time. The
        # preroll buffer can't serve this purpose: it is flushed at session
        # open (~0.7s after the trigger), by which point it has rolled past
        # most of the triggering audio.
        self._trigger_clip_buffer: Optional[Deque[bytes]] = None
        self._clip_channels: int = 1
        self._last_candidate_time: float = 0.0
        if self.wake_cfg.save_trigger_clips or self.wake_cfg.save_candidate_clips:
            clip_frames = int(
                self.wake_cfg.trigger_clip_seconds
                * self.audio_cfg.input_sample_rate
                / self.audio_cfg.input_chunk
            )
            self._trigger_clip_buffer = deque(maxlen=max(clip_frames, 5))

        # AEC health metric: mic/reference ratios from playback frames
        # (see _update_aec_metric).
        self._aec_ratio_window: Deque[float] = deque(
            maxlen=max(config.metrics.aec_window_frames, 1)
        )
        self._aec_frame_counter: int = 0

        # Events for coordination
        self._wake_event = asyncio.Event()
        # Wakes the IDLE loop for a wake word or a typed request; what to do is
        # decided from _wake_event and _text_requests, this is only the nudge.
        self._idle_signal = asyncio.Event()
        # Typed requests (MQTT); one at a time, taken by the state machine.
        self._text_requests: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self._text_turn: Optional[str] = None
        self._mqtt_bridge = None
        self._turn_complete_event = asyncio.Event()
        self._stop_playback_event = asyncio.Event()
        self._last_wake_result: Optional[WakeWordResult] = None

        # Components (initialized in start())
        self._audio_input: Optional[AudioInput] = None
        self._audio_output: Optional[AudioOutput] = None
        self._hybrid_vad: Optional[HybridVAD] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._sound_player: Optional[SoundPlayer] = None

        # Live API manager (Gemini or OpenAI, selected via config)
        self._api_manager: Optional[BaseRealtimeManager] = None

        # Session recorder — manages WAV file lifecycle per API session
        self._recorder = SessionRecorder(
            self.audio_cfg.input_sample_rate, self.audio_cfg.output_sample_rate
        )
        self._session = SessionKeeper(self.live_cfg, self._recorder)

        # Initial silence timeout: tracks when we entered LISTENING from IDLE.
        # None means the check is disabled (barge-in or follow-up path).
        self._listening_entry_time: Optional[float] = None
        # The deadline was re-armed after a reply without audio: when it
        # passes, the turn ends normally instead of as a false trigger.
        self._reply_was_empty: bool = False

        # Current audio frame for status display
        self._current_audio_frame: Optional[np.ndarray] = None

        # STT wake word verifier (optional, loaded at startup if enabled)
        self._verifier: Optional[WakeWordVerifier] = None
        self._pending_verification: Optional[asyncio.Future] = None

        # ReSpeaker LEDs
        self._respeaker = None
        self._leds = None

        # Duck started by the wake word, see _duck_radio_in_background.
        self._duck_task: Optional[asyncio.Task] = None

        # Strong references to fire-and-forget tasks (MQTT bridge, barge-in,
        # MPD watcher). The event loop keeps only weak refs — an unreferenced
        # task can be garbage-collected mid-flight and silently disappear.
        self._bg_tasks: set[asyncio.Task] = set()
        self._wake_gate = WakeGate(
            self.wake_cfg,
            self.audio_cfg,
            self.mpd_client,
            responding=lambda: self._state == SpeakerState.RESPONDING,
        )
        self._deferred_playback = DeferredPlayback(
            config,
            self.mpd_client,
            spawn=self._spawn,
            # While the AI is still speaking the music starts at the duck level.
            in_turn=lambda: self._state
            in (SpeakerState.LISTENING, SpeakerState.RESPONDING),
            play_error_sound=self._play_error_sound,
        )

        self._is_tty = sys.stdout.isatty()

    def _play_error_sound(self) -> None:
        if self._sound_player:
            self._sound_player.play(SoundEvent.ERROR)

    def _spawn(self, coro, name: str) -> asyncio.Task:
        """create_task with a strong reference held for the task's lifetime."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # -------------------------------------------------------------------------
    # State and wake-detector helpers
    # -------------------------------------------------------------------------

    @property
    def state(self) -> SpeakerState:
        """Current state of the assistant."""
        return self._state

    def _wake_mode_label(self) -> str:
        """Gate set of the wake word that opened the current session.

        Falls back to 'strict' when no detection is on record (session opened by
        something other than a wake word), which matches the IDLE defaults.
        """
        return _mode_label(
            self._last_wake_result.relaxed_mode if self._last_wake_result else False
        )

    def _set_state(self, new_state: SpeakerState) -> None:
        """Transition to a new state with logging."""
        old_state = self._state
        self._state = new_state
        logger.info("STATE: %s -> %s", old_state.value, new_state.value)
        self._last_activity_time = time.time()

        metrics.STATE_TRANSITIONS.labels(
            from_state=old_state.value, to_state=new_state.value
        ).inc()
        metrics.STATE_ACTIVE.labels(state=old_state.value).set(0)
        metrics.STATE_ACTIVE.labels(state=new_state.value).set(1)

        if self._leds:
            if new_state == SpeakerState.IDLE:
                self._leds.set_idle()
            elif new_state in (SpeakerState.LISTENING, SpeakerState.FOLLOW_UP):
                self._leds.set_listening()
            elif new_state == SpeakerState.RESPONDING:
                self._leds.set_responding()

    def _begin_turn(self, text: Optional[str] = None) -> None:
        """Start a turn: spoken, or answering a typed request when text is given."""
        self._turn_id += 1
        self._api_manager.start_new_turn()
        self._turn_complete_event.clear()
        self._text_turn = text

    @staticmethod
    def _drain_queue(q: asyncio.Queue) -> None:
        """Discard all currently-queued items without blocking."""
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize components and start the main loop."""
        logger.info("Starting Voice Assistant...")

        # Initialize MPD state now that event loop is running
        await self.mpd_client.initialize_state()

        # Share this MPD client with all tool functions so they operate on the same
        # connection and see the same duck/unduck state as the orchestrator.
        inject_mpd_client(self.mpd_client)

        # Initialize sound player (before MQTT bridge so confirm sounds work)
        self._sound_player = SoundPlayer(self.sound_cfg)

        if self.config.mqtt.enabled:
            from mqtt_bridge.bridge import MQTTBridge

            self._mqtt_bridge = MQTTBridge(
                self.config.mqtt,
                self.mpd_client,
                get_radio_client(),
                self._sound_player,
            )
            self.mpd_client.add_state_listener(self._mqtt_bridge.on_state_change)
            self._mqtt_bridge.set_text_handler(self.submit_text)
            self._spawn(self._mqtt_bridge.run(), name="mqtt_bridge")
            logger.info(
                "MQTT bridge started (broker=%s:%d)",
                self.config.mqtt.broker,
                self.config.mqtt.port,
            )

        # Watch for MPD state changes made outside this app (stream died, mpc,
        # another client) so MQTT/HA stay in sync, and restart streams that froze
        # mid-play. Also acts as the keepalive.
        self._spawn(
            self.mpd_client.watch_external_changes(get_radio_client().search_station),
            name="mpd_watcher",
        )

        # Initialize audio I/O (SoundDevice)
        self._audio_input = AudioInput(self.audio_cfg, self._mic_queue)
        self._audio_output = AudioOutput(self.audio_cfg, self._speaker_queue)

        # Initialize wake word detector
        self._wake_gate.detector = WakeWordDetector(self.wake_cfg, self.audio_cfg)

        # Initialize STT verifier (loads faster-whisper model — takes a few seconds)
        if self.wake_cfg.verify_with_stt:
            self._verifier = WakeWordVerifier(self.wake_cfg)

        # Initialize Hybrid VAD (Silero)
        if self.live_cfg.enable_manual_vad:
            self._hybrid_vad = create_hybrid_vad(self.vad_cfg)
            logger.info(
                "Hybrid VAD enabled (Silero threshold=%.2f)",
                self.vad_cfg.silero_threshold,
            )

        # Initialize Live API manager (provider selected via live_cfg.provider)
        self._api_manager = create_realtime_manager(
            self.config,
            self._api_input_queue,
            self._api_output_queue,
        )
        self._api_manager.initialize()
        self._session.api = self._api_manager
        self._api_manager.set_callbacks(
            on_turn_complete=self._on_turn_complete,
            on_interrupted=self._on_interrupted,
            on_turn_timeout=self._on_turn_timeout,
            on_error=self._on_api_error,
            on_deferred=self._deferred_playback.on_action,
            on_model_audio=self._recorder.write_model,
        )

        # Initialize ReSpeaker LEDs
        try:
            self._respeaker = open_respeaker()
            self._leds = ReSpeakerLeds(self._respeaker)
            self._leds.set_idle()
            logger.info("ReSpeaker LEDs initialized.")
        except Exception as e:
            logger.warning("ReSpeaker LEDs not available: %s", e)

        # Thread pool for CPU-bound tasks (wake word, VAD)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="audio")
        self._wake_gate.executor = self._executor

        # Long initial warmup to suppress false triggers on startup noise
        self._wake_gate.reset()

        self._running = True
        self._last_activity_time = time.time()

        # Start audio streams
        self._audio_input.start()
        self._audio_output.start()

        logger.info("Barge-in: %s", "enabled" if self.live_cfg.barge_in else "disabled")
        logger.info(
            "Manual VAD: %s",
            "enabled" if self.live_cfg.enable_manual_vad else "disabled",
        )

        if self._sound_player:
            self._sound_player.play(SoundEvent.STARTUP)

        logger.info("Listening for wake word...")

        try:
            await self._main_loop()
        finally:
            await self._cleanup()

    def _on_turn_complete(self) -> None:
        """Callback when API signals turn complete."""
        self._turn_complete_event.set()

    def _on_interrupted(self) -> None:
        """Callback when API signals interruption."""
        if self._state == SpeakerState.RESPONDING:
            logger.info("API signaled interruption, executing barge-in")
            self._spawn(self._execute_barge_in(), name="barge_in")

    def _on_turn_timeout(self) -> None:
        """Callback when API hangs (watchdog timeout)."""
        logger.warning("API turn timeout detected, treating as turn_complete")
        self._turn_complete_event.set()

    def _on_api_error(self) -> None:
        """Callback when API session fails unexpectedly."""
        logger.error("API session error — playing error sound and resetting state")
        metrics.API_ERRORS.inc()
        if self._sound_player:
            self._sound_player.play(SoundEvent.ERROR)
        self._turn_complete_event.set()

    async def _cleanup(self) -> None:
        """Clean up all resources."""
        self._running = False

        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)

        if self._api_manager:
            self._api_manager.shutdown()
            await self._api_manager.close_session()

        await self.mpd_client.disconnect()
        await close_radio_client()
        self._recorder.close()

        if self._audio_output:
            self._audio_output.stop()
        if self._audio_input:
            self._audio_input.stop()

        if self._leds:
            self._leds.set_idle()
        if self._respeaker:
            self._respeaker.close()

        if self._hybrid_vad:
            self._hybrid_vad.close()
        if self._executor:
            self._executor.shutdown(wait=False)

        logger.info("Voice Assistant stopped.")

    async def _main_loop(self) -> None:
        """Main event loop orchestrating all async tasks."""
        tasks = [
            asyncio.create_task(self._audio_capture_task(), name="audio_capture"),
            asyncio.create_task(
                self._audio_playback_bridge_task(), name="playback_bridge"
            ),
            asyncio.create_task(self._state_machine_task(), name="state_machine"),
            asyncio.create_task(
                self._inactivity_monitor_task(), name="inactivity_monitor"
            ),
        ]
        if self._respeaker:
            tasks.append(
                asyncio.create_task(
                    respeaker_monitor_task(self._respeaker), name="respeaker_monitor"
                )
            )

        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc:
                    logger.error("Task %s failed: %s", task.get_name(), exc)
                    raise exc
        finally:
            self._running = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -------------------------------------------------------------------------
    # Audio Capture with Hybrid VAD
    # -------------------------------------------------------------------------

    async def _audio_capture_task(self) -> None:
        """Continuous audio capture from microphone with VAD processing."""
        while self._running:
            try:
                try:
                    raw_audio = await asyncio.wait_for(
                        asyncio.to_thread(self._mic_queue.get, timeout=0.5),
                        timeout=1.0,
                    )
                except (asyncio.TimeoutError, queue.Empty):
                    continue

                # Extract mono channel (mic might be stereo)
                stereo = raw_audio.ndim == 2 and raw_audio.shape[1] > 1
                if stereo:
                    mono = raw_audio[:, 0]
                else:
                    mono = raw_audio.flatten()

                if mono.size == 0:
                    continue

                self._current_audio_frame = mono
                mono_bytes = mono.tobytes()
                self._preroll_buffer.append(mono_bytes)
                if self._trigger_clip_buffer is not None:
                    # Full frame, not just mono: with the XVF3800 right channel
                    # muxed to the far-end reference (5 0), a stereo clip
                    # self-labels whether our own audio was playing and what it
                    # was. Downstream prepare_recordings.py scores channel 0.
                    self._clip_channels = raw_audio.shape[1] if stereo else 1
                    self._trigger_clip_buffer.append(
                        raw_audio.tobytes() if stereo else mono_bytes
                    )
                if stereo:
                    self._update_aec_metric(raw_audio)

                wake_result = await self._wake_gate.detect(mono)
                await self._handle_wake_word_result(wake_result)

                if self._state == SpeakerState.LISTENING:
                    await self._process_listening_audio(mono, mono_bytes)
                elif self._state == SpeakerState.FOLLOW_UP:
                    await self._process_followup_audio(mono, mono_bytes)

            except asyncio.CancelledError:
                raise
            except Exception:
                # Broad by design: this task must survive any handler bug —
                # letting an exception escape kills the whole application
                # mid-conversation (FIRST_EXCEPTION in _main_loop).
                logger.exception("Audio capture error")
                await asyncio.sleep(0.1)

    def _maybe_save_candidate_clip(self, result: WakeWordResult) -> None:
        """Dump a near-miss (hot score, no trigger) for offline review.

        Captures the band invisible to trigger clips: voices the model
        undershoots (household members) and borderline noises. Rate-limited
        by cooldown_seconds against both hot streaks of the same event and
        the hot tail that follows a real trigger.
        """
        if (
            not self.wake_cfg.save_candidate_clips
            or self._trigger_clip_buffer is None
            or result.score < self.wake_cfg.candidate_threshold
        ):
            return
        now = time.time()
        if (
            now - self._last_candidate_time < self.wake_cfg.cooldown_seconds
            or now - self._wake_gate.last_trigger_time < self.wake_cfg.cooldown_seconds
        ):
            return
        self._last_candidate_time = now
        clip_frames = list(self._trigger_clip_buffer)
        logger.info(
            "Near-miss candidate (score=%.3f, verifier=%s, rms=%.0f) — saving clip",
            result.score,
            f"{result.verifier_score:.3f}" if result.verifier_score is not None else "n/a",
            result.window_rms,
        )
        asyncio.get_running_loop().run_in_executor(
            self._executor,
            save_clip,
            clip_frames,
            self.audio_cfg.input_sample_rate,
            "candidate",
            result.score,
            result.verifier_score,
            self._clip_channels,
        )

    def _update_aec_metric(self, raw_audio: np.ndarray) -> None:
        """Track echo-cancellation health from the far-end reference channel.

        Right input channel carries the XVF3800 far-end reference with system
        delay (mux 5 0), time-aligned with the mic channel. On frames where
        the reference has signal (our audio is playing), the mic channel of a
        healthy AEC holds only residue — the median mic/ref ratio over a
        rolling window is exported in dB as speaker_aec_residual_db. Median
        (not mean) so double-talk frames don't dominate the trend.
        """
        ref = raw_audio[:, 1].astype(np.float32)
        ref_rms = float(np.sqrt(np.mean(ref**2)))
        if ref_rms < self.config.metrics.aec_ref_min_rms:
            return
        mic = raw_audio[:, 0].astype(np.float32)
        mic_rms = float(np.sqrt(np.mean(mic**2)))
        self._aec_ratio_window.append(mic_rms / ref_rms)
        self._aec_frame_counter += 1
        # Refresh the gauge ~1×/s (31 frames × 32 ms), not every frame.
        if self._aec_frame_counter % 31 == 0:
            median_ratio = float(np.median(self._aec_ratio_window))
            metrics.AEC_RESIDUAL_DB.set(20.0 * np.log10(max(median_ratio, 1e-6)))

    async def _process_listening_audio(
        self, mono: np.ndarray, mono_bytes: bytes
    ) -> None:
        """Process audio in LISTENING state with Hybrid VAD."""
        if not self._api_manager.session_active:
            return

        if self._hybrid_vad and self.live_cfg.enable_manual_vad:
            speech_started, speech_ended = self._hybrid_vad.process_frame(mono)

            if speech_started:
                logger.info("VAD: Speech started - sending activity_start")
                self._listening_entry_time = (
                    None  # user spoke — silence timeout no longer applies
                )
                await self._api_manager.send_activity_start()

            if speech_ended:
                logger.info("VAD: Speech ended - sending activity_end")
                await self._api_manager.send_activity_end()

        if self._api_manager.awaiting_reply:
            # The utterance is complete. Room audio sent now (radio, TV, our own
            # music) made Gemini produce up to ~20 s of silence before it ended
            # its reply: 5 of 32 probe turns with music, 0 of 30 with no audio
            # or digital silence (01.11.2026). It is still recorded: shows
            # whether the user was cut off mid-sentence.
            self._recorder.write(mono_bytes)
            return
        await self._forward_audio_to_api(mono_bytes)

    async def _process_followup_audio(
        self, mono: np.ndarray, mono_bytes: bytes
    ) -> None:
        """Process audio in FOLLOW_UP state - detect speech to continue conversation."""
        # Skip frames while our own sound effect plays (+ tail): the follow-up
        # beep residue reads as speech on the ASR path and instantly flipped
        # this state into LISTENING — which has no initial-silence timeout on
        # the follow-up path, so a false entry hangs the session. Skipping
        # (not feeding) the VAD delays a genuine fast answer by only the
        # guard window; the speech edge fires right after it.
        if self._sound_player and time.monotonic() < (
            self._sound_player.busy_until + self.vad_cfg.sound_guard_tail_seconds
        ):
            return
        if not self._hybrid_vad:
            # Fallback to RMS-based detection
            rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float32)))))
            if rms > self.vad_cfg.rms_threshold:
                logger.info("Speech detected (RMS), starting follow-up conversation")
                await self._start_followup_conversation()
            return

        speech_started, _ = self._hybrid_vad.process_frame(mono)
        if speech_started:
            logger.info("Speech detected (Silero VAD), starting follow-up conversation")
            await self._start_followup_conversation()

    async def _start_followup_conversation(self) -> None:
        """Start a new conversation turn from follow-up state."""
        metrics.FOLLOWUPS_STARTED.inc()
        self._drop_queued_text()
        if self._hybrid_vad:
            self._hybrid_vad.reset()

        self._begin_turn()

        await self._send_preroll()
        # Speech was already detected before entering LISTENING — no silence timeout.
        self._listening_entry_time = None
        self._set_state(SpeakerState.LISTENING)

    async def _handle_wake_word_result(self, result: WakeWordResult) -> None:
        """Handle wake word detection result based on current state."""
        if not result.triggered:
            self._maybe_save_candidate_clip(result)
            self._print_status(result)
            return

        if not self._wake_gate.accept_trigger():
            return

        if self._is_tty:
            print()  # New line after status
        if result.verifier_score is not None:
            logger.info(
                "Wake word detected! (score=%.3f, verifier=%.3f, rms=%.0f)",
                result.score,
                result.verifier_score,
                result.window_rms,
            )
        else:
            logger.info(
                "Wake word detected! (score=%.3f, rms=%.0f)",
                result.score,
                result.window_rms,
            )

        if self._trigger_clip_buffer is not None:
            # Snapshot before more frames roll the deque; write off-loop.
            clip_frames = list(self._trigger_clip_buffer)
            asyncio.get_running_loop().run_in_executor(
                self._executor,
                save_clip,
                clip_frames,
                self.audio_cfg.input_sample_rate,
                "trigger",
                result.score,
                result.verifier_score,
                self._clip_channels,
            )

        _wake_context = (
            "barge_in"
            if self._state == SpeakerState.RESPONDING
            else "re_listen"
            if self._state == SpeakerState.LISTENING
            else "idle"
        )
        metrics.WAKE_DETECTIONS.labels(
            context=_wake_context, mode=_mode_label(result.relaxed_mode)
        ).inc()

        self._duck_radio_in_background()

        if self._state == SpeakerState.IDLE and self._verifier is not None:
            # Fire verification in background — don't block wake sound or session open.
            # Result is checked each iteration of _handle_listening_state.
            self._pending_verification = asyncio.get_running_loop().run_in_executor(
                self._executor,
                self._verifier.verify,
                list(self._preroll_buffer),
            )

        if self._sound_player:
            self._sound_player.play(SoundEvent.WAKE_WORD)

        if self._state == SpeakerState.IDLE:
            self._last_wake_result = result
            self._wake_event.set()
            self._idle_signal.set()

        elif self._state == SpeakerState.RESPONDING:
            logger.info("BARGE-IN: Interrupting AI playback!")
            await self._execute_barge_in()

        elif self._state == SpeakerState.LISTENING:
            # Re-wake while already listening — quick reset, no cooldown needed.
            self._wake_gate.reset(short_warmup=True)

    def _print_status(self, result: WakeWordResult) -> None:
        """Print current status line based on state."""
        if not self._is_tty:
            return
        if self._state == SpeakerState.IDLE:
            if self._wake_gate.warming_up:
                print(
                    f"\r[WARMUP] {self._wake_gate.frames_since_reset}/{self._wake_gate.warmup_frames_needed}",
                    end="",
                    flush=True,
                )
            else:
                print(
                    f"\r[IDLE] score={result.score:.3f} max={result.max_score:.3f} vad={result.vad_score:.2f}",
                    end="",
                    flush=True,
                )
        elif self._state == SpeakerState.LISTENING:
            vad_prob = self._hybrid_vad.last_probability if self._hybrid_vad else 0.0
            print(
                f"\r[LISTENING] sent={self._api_manager.frames_sent} vad={vad_prob:.2f}",
                end="",
                flush=True,
            )
        elif self._state == SpeakerState.RESPONDING:
            print(
                f"\r[RESPONDING] recv={self._api_manager.chunks_received} wake={result.score:.3f}",
                end="",
                flush=True,
            )
        elif self._state == SpeakerState.FOLLOW_UP:
            vad_prob = self._hybrid_vad.last_probability if self._hybrid_vad else 0.0
            print(f"\r[FOLLOW_UP] vad={vad_prob:.2f}", end="", flush=True)

    async def _execute_barge_in(self) -> None:
        """Execute hard barge-in with instant audio cutoff."""
        if self._state != SpeakerState.RESPONDING:
            # In FOLLOW_UP the user is already talking, so there's nothing to interrupt.
            # In other states a duplicate callback from _on_interrupted may arrive
            # after _handle_wake_word_result already handled it — both cases: no-op.
            return
        metrics.BARGE_INS.inc()
        self._stop_playback_event.set()
        self._playback_interrupted = True
        self._api_manager.playback_interrupted = True

        # Instant flush: stop audio output immediately
        if self._audio_output:
            self._audio_output.stop_immediate()

        # Drop all buffered API audio and pending mic frames
        self._drain_queue(self._api_output_queue)
        self._drain_queue(self._api_input_queue)

        # Short warmup — user is actively speaking, re-arm quickly
        self._wake_gate.reset(short_warmup=True)
        if self._hybrid_vad:
            self._hybrid_vad.reset()

        self._begin_turn()

        if self.live_cfg.enable_manual_vad:
            await self._api_manager.send_activity_start()

        # User is already speaking — silence timeout does not apply.
        self._listening_entry_time = None
        self._set_state(SpeakerState.LISTENING)

        await asyncio.sleep(0.1)
        if self._audio_output:
            self._audio_output.resume()

        self._stop_playback_event.clear()
        self._playback_interrupted = False
        self._api_manager.playback_interrupted = False

    async def _forward_audio_to_api(self, audio_bytes: bytes) -> None:
        """Forward audio frame to the API send queue."""
        frame = AudioFrame(data=audio_bytes, mime_type=self.live_cfg.input_mime_type)
        try:
            self._api_input_queue.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                self._api_input_queue.get_nowait()
                self._api_input_queue.put_nowait(frame)
            except asyncio.QueueEmpty:
                pass

        self._recorder.write(audio_bytes)

    # -------------------------------------------------------------------------
    # Audio Playback Bridge (API queue -> Speaker queue)
    # -------------------------------------------------------------------------

    async def _audio_playback_bridge_task(self) -> None:
        """Bridge audio from API output queue to speaker queue.

        Processes ALL available chunks at once to prevent buffer underruns.
        """
        while self._running:
            try:
                try:
                    audio_data = await asyncio.wait_for(
                        self._api_output_queue.get(), timeout=0.1
                    )
                except asyncio.TimeoutError:
                    continue

                if self._stop_playback_event.is_set():
                    continue

                # Collect ALL available chunks (batch processing)
                chunks = [audio_data]
                while not self._api_output_queue.empty():
                    try:
                        chunk = self._api_output_queue.get_nowait()
                        chunks.append(chunk)
                    except asyncio.QueueEmpty:
                        break

                for chunk in chunks:
                    if self._stop_playback_event.is_set():
                        break
                    try:
                        self._speaker_queue.put_nowait(chunk)
                    except queue.Full:
                        try:
                            self._speaker_queue.get_nowait()
                            self._speaker_queue.put_nowait(chunk)
                        except queue.Empty:
                            pass

            except Exception as e:
                logger.error("Playback bridge error: %s", e)
                await asyncio.sleep(0.1)

    # -------------------------------------------------------------------------
    # State Machine
    # -------------------------------------------------------------------------

    async def _state_machine_task(self) -> None:
        """Main state machine controller."""
        follow_up_start: Optional[float] = None

        while self._running:
            try:
                if self._state == SpeakerState.IDLE:
                    await self._handle_idle_state()
                    follow_up_start = None

                elif self._state == SpeakerState.LISTENING:
                    await self._handle_listening_state()
                    follow_up_start = None

                elif self._state == SpeakerState.RESPONDING:
                    await self._handle_responding_state()
                    follow_up_start = None

                elif self._state == SpeakerState.FOLLOW_UP:
                    if not self._text_requests.empty():
                        follow_up_start = None
                        await self._open_session_and_listen(self._text_requests.get_nowait())
                        continue
                    if follow_up_start is None:
                        follow_up_start = time.time()

                    elapsed = time.time() - follow_up_start
                    if elapsed >= self.live_cfg.followup_timeout:
                        logger.info(
                            "Follow-up timeout (%.1fs), ending conversation", elapsed
                        )
                        metrics.FOLLOWUP_TIMEOUTS.inc()
                        follow_up_start = None
                        await self._finish_turn()
                    else:
                        await asyncio.sleep(0.05)

            except asyncio.CancelledError:
                raise
            except Exception:
                # Broad by design — see _audio_capture_task.
                logger.exception("State machine error")
                follow_up_start = None
                await self._recover_from_state_error()
                await asyncio.sleep(1.0)

    async def _recover_from_state_error(self) -> None:
        """Return to IDLE after a handler blew up mid-turn.

        Logging the exception and looping is not enough: the state is unchanged,
        so RESPONDING keeps waiting for a turn_complete that already fired and
        the speaker goes deaf until the API drops the session on its own. That
        happened on every radio command under Mopidy (04.09.2026: crash 20:36:43,
        stuck until Gemini closed the socket at 20:39:13). The error sound tells
        the user the command failed instead of leaving them with silence.
        """
        if self._state == SpeakerState.IDLE:
            return
        try:
            if self._sound_player:
                self._sound_player.play(SoundEvent.ERROR)
            await self._finish_turn(play_end_sound=False)
        except Exception:
            # Recovery itself must never take the state machine down.
            logger.exception("State recovery failed — forcing IDLE")
            self._set_state(SpeakerState.IDLE)

    async def _handle_idle_state(self) -> None:
        """Handle IDLE state: wait for wake word or a typed request."""
        if not self._text_requests.empty():
            text = self._text_requests.get_nowait()
            await self._duck_radio_if_playing()
            self._wake_in_progress = True
            try:
                await self._open_session_and_listen(text)
            finally:
                self._wake_in_progress = False
            return
        if not self._wake_event.is_set():
            try:
                await asyncio.wait_for(self._idle_signal.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                return
            self._idle_signal.clear()
            if not self._wake_event.is_set():
                return  # a typed request: taken on the next pass

        self._wake_event.clear()
        self._wake_in_progress = True
        try:
            await self._open_session_and_listen()
        finally:
            self._wake_in_progress = False

    def submit_text(self, text: str) -> bool:
        """Queues a typed request (called by the MQTT bridge).

        Refused while a turn is in progress or another request waits: the reply
        would talk over the current one.
        """
        if self._state not in (SpeakerState.IDLE, SpeakerState.FOLLOW_UP):
            return False
        try:
            self._text_requests.put_nowait(text)
        except asyncio.QueueFull:
            return False
        self._idle_signal.set()
        return True

    def _drop_queued_text(self) -> None:
        """A voice turn started first: answer a waiting typed request with busy
        instead of firing it later, out of context."""
        while not self._text_requests.empty():
            text = self._text_requests.get_nowait()
            logger.info("Voice turn started first — dropping text request: %s", text)
            self._publish_turn("text", text, "", error="busy")

    async def _open_session_and_listen(self, text: Optional[str] = None) -> None:
        """Open (or reuse) the API session and start a turn: listening after a
        wake word, or answering a typed request when text is given."""
        if text is None:
            self._drop_queued_text()
        if not await self._session.open(self._last_wake_result):
            self._play_error_sound()
            await self._unduck_radio()
            self._set_state(SpeakerState.IDLE)
            if text is not None:
                self._publish_turn("text", text, "", error="session_failed")
            return

        if self._hybrid_vad:
            self._hybrid_vad.reset()

        # Execute any deferred actions that arrived while in IDLE (Gemini can send
        # tool_calls after the watchdog fired and state already transitioned here).
        # Must run before start_new_turn() clears pending_actions.
        await self._dispatch_deferred_actions()

        self._begin_turn(text)
        self._last_activity_time = time.time()

        if text is not None:
            logger.info("Text request: %s", text)
            # No initial-silence check: nobody is expected to speak.
            self._listening_entry_time = None
            self._set_state(SpeakerState.LISTENING)
            if await self._api_manager.send_text(text):
                # Nobody speaks after a typed request, so nothing else ends a
                # turn the API never answers (stalled socket).
                self._api_manager.start_watchdog(self.live_cfg.turn_watchdog_timeout)
            else:
                self._publish_turn("text", text, "", error="send_failed")
                self._text_turn = None
                await self._finish_turn(play_end_sound=False)
            return

        await self._send_preroll()
        # Arm initial-silence check — no speech detected yet after wake word.
        self._listening_entry_time = time.time()
        self._reply_was_empty = False
        self._set_state(SpeakerState.LISTENING)

    async def _handle_listening_state(self) -> None:
        """Handle LISTENING state: wait for first audio response from API."""
        if self._pending_verification is not None and self._pending_verification.done():
            try:
                verified = self._pending_verification.result()
            except Exception:
                verified = True  # don't reject on verifier error
            self._pending_verification = None
            activity_started = self._api_manager and self._api_manager.activity_started
            if not verified and not activity_started:
                logger.info("STT verification failed — false trigger, aborting session")
                metrics.FALSE_TRIGGERS.labels(
                    reason="stt_rejection", mode=self._wake_mode_label()
                ).inc()
                await self._close_on_initial_silence()
                return

        if self._api_manager.chunks_received > 0:
            logger.info("First audio received, transitioning to RESPONDING")
            self._listening_entry_time = None
            self._set_state(SpeakerState.RESPONDING)
            return

        if self._turn_complete_event.is_set():
            self._turn_complete_event.clear()
            logger.info("Turn complete without audio response")
            self._listening_entry_time = None
            # A voice turn keeps waiting for late audio (05.09.2026) only while
            # the API ended it and the session lives. After an API error or the
            # watchdog (still awaiting_reply) nothing would leave LISTENING:
            # deaf until restart on 07.11.2026.
            await_late_audio = (
                self._text_turn is None
                and self._api_manager.session_active
                and not self._api_manager.awaiting_reply
            )
            await self._execute_post_response_actions(await_late_audio)
            if await_late_audio and self._state == SpeakerState.LISTENING:
                # Late audio gets as long as speech after the wake word; with
                # no deadline the microphone streamed to the API until
                # somebody spoke or the server closed the session.
                self._listening_entry_time = time.time()
                self._reply_was_empty = True
            return

        # Initial silence timeout: no speech detected since wake word —
        # likely a false positive. Close session silently.
        if self._listening_entry_time is not None and not (
            self._api_manager and self._api_manager.activity_started
        ):
            elapsed = time.time() - self._listening_entry_time
            if elapsed > self.live_cfg.initial_silence_timeout:
                if self._reply_was_empty:
                    await self._end_turn_after_empty_reply(elapsed)
                    return
                logger.info(
                    "Initial silence timeout (%.1fs) — no speech after wake word, likely false trigger.",
                    elapsed,
                )
                await self._close_on_initial_silence()
                return

        await asyncio.sleep(0.05)

    async def _handle_responding_state(self) -> None:
        """Handle RESPONDING state: wait for turn_complete then drain playback."""
        if not self._turn_complete_event.is_set():
            await asyncio.sleep(0.05)
            return

        self._turn_complete_event.clear()

        # Wait for speaker queue to drain (bounded — guards against AudioOutput hang)
        drain_deadline = time.time() + self.live_cfg.speaker_drain_timeout
        while not self._speaker_queue.empty() and self._running:
            if self._playback_interrupted:
                break
            if time.time() > drain_deadline:
                logger.warning(
                    "Speaker queue did not drain within %.1fs — clearing buffers",
                    self.live_cfg.speaker_drain_timeout,
                )
                break
            await asyncio.sleep(0.1)

        # Clear both queues so any remaining AI audio doesn't bleed into the END
        # sound or the next turn. This matters when the drain loop hit the timeout
        # (lots of buffered audio) or when the bridge task was still refilling
        # _speaker_queue from _api_output_queue as we drained.
        self._drain_queue(self._api_output_queue)
        while not self._speaker_queue.empty():
            try:
                self._speaker_queue.get_nowait()
            except queue.Empty:
                break

        if self._state != SpeakerState.RESPONDING:
            return

        await asyncio.sleep(0.3)

        # Re-check after sleep: barge-in may have transitioned state to LISTENING.
        if self._state != SpeakerState.RESPONDING:
            return

        await self._execute_post_response_actions()

    async def _dispatch_deferred_actions(self) -> tuple[bool, Optional[str]]:
        """Run and clear the pending deferred tool actions, see DeferredPlayback.dispatch."""
        if not self._api_manager or not self._api_manager.pending_actions:
            return False, None
        actions = list(self._api_manager.pending_actions)
        self._api_manager.pending_actions.clear()
        return await self._deferred_playback.dispatch(actions)

    async def _execute_post_response_actions(self, await_late_audio: bool = False) -> None:
        """Execute deferred actions and decide the next state after AI response.

        With await_late_audio the turn is not ended: the radio is released and
        the state stays LISTENING, unless the AI asked for a follow-up.
        """
        turn_id = self._turn_id
        # Read the turn first: a barge-in during the deferred actions starts a
        # new turn and clears the transcripts. Publish after them, with their error.
        turn = None
        if self._api_manager is not None:
            turn = (
                "text" if self._text_turn is not None else "voice",
                self._text_turn or self._api_manager.user_transcript.strip(),
                self._api_manager.ai_transcript.strip(),
            )
        self._text_turn = None
        turn_should_end, error = await self._dispatch_deferred_actions()
        if turn is not None:
            self._publish_turn(*turn, error)

        if turn_should_end:
            logger.info("Radio action performed, ending turn.")
            await self._finish_turn(False, turn_id, await_late_audio)
            return

        radio_playing = await self.mpd_client.is_playing()
        if turn_id != self._turn_id:
            return

        if not self._playback_interrupted:
            requested = (
                self._api_manager is not None
                and self._api_manager.request_for_user_input
            )
            if requested or self._ai_ended_with_question():
                if not requested:
                    logger.info(
                        "Follow-up triggered by question mark in AI transcript."
                    )
                logger.info(
                    "Waiting for follow-up (%.1fs, Silero VAD)...",
                    self.live_cfg.followup_timeout,
                )
                if self._sound_player:
                    self._sound_player.play(SoundEvent.FOLLOW_UP)
                if self._hybrid_vad:
                    self._hybrid_vad.reset()
                self._set_state(SpeakerState.FOLLOW_UP)
                return

        logger.info("Radio %s, ending turn.", "is playing" if radio_playing else "not playing")
        await self._finish_turn(not radio_playing, turn_id, await_late_audio)

    def _superseded(self, turn_id: int) -> bool:
        if turn_id == self._turn_id:
            return False
        logger.debug("Turn %d superseded by turn %d, leaving the state alone", turn_id, self._turn_id)
        return True

    async def _finish_turn(
        self,
        play_end_sound: bool = True,
        turn_id: Optional[int] = None,
        keep_listening: bool = False,
    ) -> None:
        """Common cleanup after a conversation turn is finished.

        turn_id is the turn being finished, the current one unless the caller
        awaited something since it decided to end it. A barge-in or a follow-up
        answer can start the next turn during any await: the finished turn then
        leaves the radio volume and the state to the new one. keep_listening
        only releases the radio and stays in the current state.
        """
        if turn_id is None:
            turn_id = self._turn_id
        if self._superseded(turn_id):
            return

        if self._pending_verification is not None:
            self._pending_verification.cancel()
            self._pending_verification = None

        await self._unduck_radio()
        if self._superseded(turn_id):
            return
        await drop_prefetched()
        if self._superseded(turn_id) or keep_listening:
            return

        self._drain_queue(self._api_input_queue)
        self._wake_gate.reset(cooldown=True)

        if self._hybrid_vad:
            self._hybrid_vad.reset()

        if play_end_sound and self._sound_player:
            self._sound_player.play(SoundEvent.END_CONVERSATION)

        self._set_state(SpeakerState.IDLE)
        self._last_activity_time = time.time()

        await self._session.turn_finished()

        if self._is_tty:
            print()
        if self._session.active:
            logger.info(
                "Listening for wake word... (session kept alive for %.0fs)",
                self.live_cfg.session_inactivity_timeout,
            )
        else:
            logger.info("Listening for wake word...")

    async def _end_turn_after_empty_reply(self, waited: float) -> None:
        """The user spoke and the API answered with nothing: a finished turn,
        not a false wake word, so the session and its recording stay."""
        logger.info("No late audio %.1fs after an empty reply, ending turn.", waited)
        self._listening_entry_time = None
        await self._finish_turn(play_end_sound=not await self.mpd_client.is_playing())

    async def _close_on_initial_silence(self) -> None:
        """Close session silently after a false-positive wake word.

        Restores radio volume, closes the API session to save quota, and returns
        to IDLE without playing the end-conversation sound.
        """
        self._listening_entry_time = None
        if self._pending_verification is not None:
            self._pending_verification.cancel()
            self._pending_verification = None

        await self._unduck_radio()
        self._drain_queue(self._api_input_queue)

        await self._session.close("false_trigger")
        metrics.FALSE_TRIGGERS.labels(
            reason="initial_silence", mode=self._wake_mode_label()
        ).inc()

        self._wake_gate.reset(cooldown=True)
        if self._hybrid_vad:
            self._hybrid_vad.reset()

        self._set_state(SpeakerState.IDLE)
        logger.info("Listening for wake word... (session closed after false trigger)")

    def _publish_turn(
        self, source: str, request: str, reply: str, error: Optional[str] = None
    ) -> None:
        """Sends the finished turn to MQTT so it can be read away from the speaker."""
        if self._mqtt_bridge is None:
            return
        event = {"source": source, "request": request, "reply": reply}
        if error:
            event["error"] = error
        self._mqtt_bridge.publish_turn(event)

    def _ai_ended_with_question(self) -> bool:
        """Fallback follow-up trigger: AI transcript ends with '?'.

        Catches cases where the model asked a question but forgot to call
        request_for_user_input. Checks the final character only — a '?' inside
        a sentence (e.g., a parenthetical) must not trigger a follow-up.
        """
        if self._api_manager is None:
            return False
        transcript = self._api_manager.ai_transcript.strip()
        return bool(transcript) and transcript.endswith("?")

    async def _duck_radio_if_playing(self) -> None:
        """Duck radio volume if playback is active."""
        if await self.mpd_client.is_playing():
            await self.mpd_client.duck()

    def _duck_radio_in_background(self) -> None:
        """Duck from the microphone loop, which must not wait for MPD."""

        async def duck() -> None:
            try:
                await self._duck_radio_if_playing()
            except Exception:
                logger.exception("Radio duck failed")

        if self._duck_task is None or self._duck_task.done():
            self._duck_task = self._spawn(duck(), name="radio_duck")

    async def _unduck_radio(self) -> None:
        """Unduck, after a duck still in flight: one landing later would stay."""
        if self._duck_task is not None and not self._duck_task.done():
            await asyncio.wait({self._duck_task})
        await self.mpd_client.unduck()

    async def _inactivity_monitor_task(self) -> None:
        """Close the API session after a period of IDLE inactivity.

        (MPD keepalive lives in mpd_client.watch_external_changes now.)
        """
        while self._running:
            await asyncio.sleep(5.0)

            metrics.update_system_metrics()

            if self._state != SpeakerState.IDLE:
                continue
            # A wake word is mid-flight: the session it just opened is still
            # paired with an IDLE state, and _last_activity_time still measures
            # the gap before the wake word. Closing here kills the session the
            # user is about to talk into (seen 2026-09-10: "inactive for 58732s"
            # one second after "Session opened successfully").
            if self._wake_in_progress:
                continue

            await self._session.close_if_inactive(time.time() - self._last_activity_time)

    # -------------------------------------------------------------------------
    # Preroll
    # -------------------------------------------------------------------------

    async def _send_preroll(self) -> None:
        """Flush the preroll buffer into the API input queue and recorder."""
        for frame_data in self._preroll_buffer:
            frame = AudioFrame(data=frame_data, mime_type=self.live_cfg.input_mime_type)
            try:
                self._api_input_queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass
            self._recorder.write(frame_data)

        self._preroll_buffer.clear()
