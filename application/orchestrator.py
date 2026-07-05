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
from audio.recorder import SessionRecorder
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
from functions.definitions import close_radio_client, get_radio_client, inject_mpd_client
import metrics
from mpd_client.client import MPDClientWrapper
from realtime import BaseRealtimeManager, create_realtime_manager
from state import AudioFrame, SpeakerState, WakeWordResult
from tools.xvf_client import ReSpeakerLeds, open_respeaker
from tools.respeaker_monitor import respeaker_monitor_task

logger = logging.getLogger(__name__)

BARGE_IN_COOLDOWN: float = 0.5


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
        self._last_barge_in_time: float = 0.0

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

        # Events for coordination
        self._wake_event = asyncio.Event()
        self._turn_complete_event = asyncio.Event()
        self._stop_playback_event = asyncio.Event()
        self._last_wake_result: Optional[WakeWordResult] = None

        # Components (initialized in start())
        self._audio_input: Optional[AudioInput] = None
        self._audio_output: Optional[AudioOutput] = None
        self._wake_detector: Optional[WakeWordDetector] = None
        self._hybrid_vad: Optional[HybridVAD] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._sound_player: Optional[SoundPlayer] = None

        # Live API manager (Gemini or OpenAI, selected via config)
        self._api_manager: Optional[BaseRealtimeManager] = None

        # Session recorder — manages WAV file lifecycle per API session
        self._recorder = SessionRecorder(self.audio_cfg.input_sample_rate)

        # Warmup control (prevent false wake word triggers after state transitions)
        self._frames_since_reset: int = 0
        self._warmup_frames_needed: int = 0

        # Initial silence timeout: tracks when we entered LISTENING from IDLE.
        # None means the check is disabled (barge-in or follow-up path).
        self._listening_entry_time: Optional[float] = None

        # Counts completed turns in the current session. Reset when a new session
        # is opened; when it reaches session_max_turns the session is closed at
        # the next natural IDLE boundary.
        self._session_turn_count: int = 0


        # Current audio frame for status display
        self._current_audio_frame: Optional[np.ndarray] = None

        # STT wake word verifier (optional, loaded at startup if enabled)
        self._verifier: Optional[WakeWordVerifier] = None
        self._pending_verification: Optional[asyncio.Future] = None

        # ReSpeaker LEDs
        self._respeaker = None
        self._leds = None

        # Strong references to fire-and-forget tasks (MQTT bridge, barge-in,
        # MPD watcher). The event loop keeps only weak refs — an unreferenced
        # task can be garbage-collected mid-flight and silently disappear.
        self._bg_tasks: set[asyncio.Task] = set()

        self._is_tty = sys.stdout.isatty()

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

    def _set_state(self, new_state: SpeakerState) -> None:
        """Transition to a new state with logging."""
        old_state = self._state
        self._state = new_state
        logger.info("STATE: %s -> %s", old_state.value, new_state.value)
        self._last_activity_time = time.time()

        metrics.STATE_TRANSITIONS.labels(from_state=old_state.value, to_state=new_state.value).inc()
        metrics.STATE_ACTIVE.labels(state=old_state.value).set(0)
        metrics.STATE_ACTIVE.labels(state=new_state.value).set(1)

        if self._leds:
            if new_state == SpeakerState.IDLE:
                self._leds.set_idle()
            elif new_state in (SpeakerState.LISTENING, SpeakerState.FOLLOW_UP):
                self._leds.set_listening()
            elif new_state == SpeakerState.RESPONDING:
                self._leds.set_responding()

    def _reset_wake_detector(
        self, *, short_warmup: bool = False, cooldown: bool = False
    ) -> None:
        """Reset the wake word detector and recalculate the warmup budget.

        Args:
            short_warmup: Use 0.5 s warmup (barge-in or re-wake while LISTENING).
                          Default (False) uses 1.5 s warmup after a full turn ends.
            cooldown:     Additionally arm the detector cooldown to prevent
                          immediate re-triggering on the same audio event.
        """
        if self._wake_detector:
            self._wake_detector.reset()
            if cooldown:
                self._wake_detector.set_cooldown()
        self._frames_since_reset = 0
        self._warmup_frames_needed = int(
            (0.5 if short_warmup else 1.5)
            * self.audio_cfg.input_sample_rate
            / self.audio_cfg.input_chunk
        )

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
            from mqtt.bridge import MQTTBridge
            self._mqtt_bridge = MQTTBridge(self.config.mqtt, self.mpd_client, get_radio_client(), self._sound_player)
            self.mpd_client.add_state_listener(self._mqtt_bridge.on_state_change)
            self._spawn(self._mqtt_bridge.run(), name="mqtt_bridge")
            logger.info("MQTT bridge started (broker=%s:%d)", self.config.mqtt.broker, self.config.mqtt.port)

        # Watch for MPD state changes made outside this app (stream died, mpc,
        # another client) so MQTT/HA stay in sync. Also acts as the keepalive.
        self._spawn(self.mpd_client.watch_external_changes(), name="mpd_watcher")

        # Initialize audio I/O (SoundDevice)
        self._audio_input = AudioInput(self.audio_cfg, self._mic_queue)
        self._audio_output = AudioOutput(self.audio_cfg, self._speaker_queue)

        # Initialize wake word detector
        self._wake_detector = WakeWordDetector(self.wake_cfg, self.audio_cfg)

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
        self._api_manager.set_callbacks(
            on_turn_complete=self._on_turn_complete,
            on_interrupted=self._on_interrupted,
            on_turn_timeout=self._on_turn_timeout,
            on_error=self._on_api_error,
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

        # Long initial warmup to suppress false triggers on startup noise
        self._reset_wake_detector()

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
            tasks.append(asyncio.create_task(
                respeaker_monitor_task(self._respeaker), name="respeaker_monitor"
            ))

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
        loop = asyncio.get_running_loop()

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
                if raw_audio.ndim == 2 and raw_audio.shape[1] > 1:
                    mono = raw_audio[:, 0]
                else:
                    mono = raw_audio.flatten()

                if mono.size == 0:
                    continue

                self._current_audio_frame = mono
                mono_bytes = mono.tobytes()
                self._preroll_buffer.append(mono_bytes)
                self._frames_since_reset += 1

                wake_result = await self._detect_wake_word_async(loop, mono)
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
                self._listening_entry_time = None  # user spoke — silence timeout no longer applies
                await self._api_manager.send_activity_start()

            if speech_ended:
                logger.info("VAD: Speech ended - sending activity_end")
                await self._api_manager.send_activity_end()

        await self._forward_audio_to_api(mono_bytes)

    async def _process_followup_audio(
        self, mono: np.ndarray, mono_bytes: bytes
    ) -> None:
        """Process audio in FOLLOW_UP state - detect speech to continue conversation."""
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
        if self._hybrid_vad:
            self._hybrid_vad.reset()

        self._api_manager.start_new_turn()
        self._turn_complete_event.clear()

        await self._send_preroll()
        # Speech was already detected before entering LISTENING — no silence timeout.
        self._listening_entry_time = None
        self._set_state(SpeakerState.LISTENING)

    async def _detect_wake_word_async(
        self,
        loop: asyncio.AbstractEventLoop,
        mono: np.ndarray,
    ) -> WakeWordResult:
        """Run wake word inference in executor."""

        def _inference() -> tuple[float, float, float, bool]:
            return self._wake_detector.process(mono)

        if self._frames_since_reset <= self._warmup_frames_needed:
            await loop.run_in_executor(self._executor, _inference)
            return WakeWordResult(triggered=False, score=0.0, max_score=0.0)

        score, max_score, vad_score, triggered = await loop.run_in_executor(
            self._executor, _inference
        )
        assert self._wake_detector is not None
        return WakeWordResult(
            triggered=triggered,
            score=score,
            max_score=max_score,
            vad_score=vad_score,
            verifier_score=self._wake_detector.last_verifier_score,
        )

    async def _handle_wake_word_result(self, result: WakeWordResult) -> None:
        """Handle wake word detection result based on current state."""
        if not result.triggered:
            self._print_status(result)
            return

        now = time.time()
        if now - self._last_barge_in_time < BARGE_IN_COOLDOWN:
            return

        if self._is_tty:
            print()  # New line after status
        if result.verifier_score is not None:
            logger.info("Wake word detected! (score=%.3f, verifier=%.3f)", result.score, result.verifier_score)
        else:
            logger.info("Wake word detected! (score=%.3f)", result.score)
        self._last_barge_in_time = now

        _wake_context = (
            'barge_in' if self._state == SpeakerState.RESPONDING
            else 're_listen' if self._state == SpeakerState.LISTENING
            else 'idle'
        )
        metrics.WAKE_DETECTIONS.labels(context=_wake_context).inc()

        await self._duck_radio_if_playing()

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

        elif self._state == SpeakerState.RESPONDING:
            logger.info("BARGE-IN: Interrupting AI playback!")
            await self._execute_barge_in()

        elif self._state == SpeakerState.LISTENING:
            # Re-wake while already listening — quick reset, no cooldown needed.
            self._reset_wake_detector(short_warmup=True)

    def _print_status(self, result: WakeWordResult) -> None:
        """Print current status line based on state."""
        if not self._is_tty:
            return
        if self._state == SpeakerState.IDLE:
            if self._frames_since_reset <= self._warmup_frames_needed:
                print(
                    f"\r[WARMUP] {self._frames_since_reset}/{self._warmup_frames_needed}",
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
        self._reset_wake_detector(short_warmup=True)
        if self._hybrid_vad:
            self._hybrid_vad.reset()

        self._api_manager.start_new_turn()
        self._turn_complete_event.clear()

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
                await asyncio.sleep(1.0)

    async def _handle_idle_state(self) -> None:
        """Handle IDLE state: wait for wake word."""
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            return

        self._wake_event.clear()

        if not self._api_manager.session_active:
            await self._api_manager.open_session()
            if not self._api_manager.session_active:
                logger.error("Failed to open API session — playing error sound and returning to IDLE")
                metrics.API_ERRORS.inc()
                if self._sound_player:
                    self._sound_player.play(SoundEvent.ERROR)
                await self.mpd_client.unduck()
                self._set_state(SpeakerState.IDLE)
                return
            metrics.SESSIONS_OPENED.inc()
            self._recorder.start(
                model_score=self._last_wake_result.score if self._last_wake_result else None,
                verifier_score=self._last_wake_result.verifier_score if self._last_wake_result else None,
            )
            self._session_turn_count = 0
        else:
            logger.info("Reusing existing session (recording continues)")

        if self._hybrid_vad:
            self._hybrid_vad.reset()

        # Execute any deferred actions that arrived while in IDLE (Gemini can send
        # tool_calls after the watchdog fired and state already transitioned here).
        # Must run before start_new_turn() clears pending_actions.
        await self._dispatch_deferred_actions()

        self._api_manager.start_new_turn()
        self._turn_complete_event.clear()
        self._last_activity_time = time.time()

        await self._send_preroll()
        # Arm initial-silence check — no speech detected yet after wake word.
        self._listening_entry_time = time.time()
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
                metrics.FALSE_TRIGGERS.labels(reason='stt_rejection').inc()
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
            await self._execute_post_response_actions()
            return

        # Initial silence timeout: no speech detected since wake word —
        # likely a false positive. Close session silently.
        if self._listening_entry_time is not None and not (
            self._api_manager and self._api_manager.activity_started
        ):
            elapsed = time.time() - self._listening_entry_time
            if elapsed > self.live_cfg.initial_silence_timeout:
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
                logger.warning("Speaker queue did not drain within %.1fs — clearing buffers", self.live_cfg.speaker_drain_timeout)
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

    async def _dispatch_deferred_actions(self) -> bool:
        """Execute and clear all pending deferred tool actions.

        Returns True if a radio play/stop action was executed (signals turn should end
        without the end-conversation sound).
        """
        if not self._api_manager or not self._api_manager.pending_actions:
            return False

        actions = list(self._api_manager.pending_actions)
        self._api_manager.pending_actions.clear()

        ran_radio_action = False
        for action in actions:
            action_type = action.get("type")
            payload = action.get("payload", {})

            if action_type == "play_internet_radio":
                if payload.get("status") == "error":
                    logger.warning("play_internet_radio deferred error: %s", payload.get("details"))
                elif payload.get("url"):
                    logger.info("Executing deferred action: play_internet_radio (url)")
                    station = payload.get("name") or ""
                    await self.mpd_client.play_station(payload["url"], station, key=payload.get("key"))
                    metrics.RADIO_PLAYS.labels(source='ai_new', station=station).inc()
                    ran_radio_action = True
                elif payload.get("action") == "play":
                    logger.info("Executing deferred action: play_internet_radio (resume)")
                    await self.mpd_client.play()
                    station = self.mpd_client.get_current_station_name() or 'unknown'
                    metrics.RADIO_PLAYS.labels(source='ai_resume', station=station).inc()
                    ran_radio_action = True

            elif action_type == "set_playback_volume":
                logger.info("Executing deferred action: set_playback_volume")
                if "volume_percentage" in payload:
                    await self.mpd_client.set_volume(payload["volume_percentage"])

        return ran_radio_action

    async def _execute_post_response_actions(self) -> None:
        """Execute deferred actions and decide the next state after AI response."""
        turn_should_end = await self._dispatch_deferred_actions()

        if turn_should_end:
            logger.info("Radio action performed, ending turn.")
            await self._finish_turn(play_end_sound=False)
            return

        radio_playing = await self.mpd_client.is_playing()

        if not self._playback_interrupted:
            requested = self._api_manager is not None and self._api_manager.request_for_user_input
            if requested or self._ai_ended_with_question():
                if not requested:
                    logger.info("Follow-up triggered by question mark in AI transcript.")
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

        if radio_playing:
            logger.info("Radio is playing, ending turn.")
            await self._finish_turn(play_end_sound=False)
        else:
            logger.info("Radio not playing, ending turn.")
            await self._finish_turn()

    async def _finish_turn(self, play_end_sound: bool = True) -> None:
        """Common cleanup after a conversation turn is finished."""
        if self._pending_verification is not None:
            self._pending_verification.cancel()
            self._pending_verification = None

        await self.mpd_client.unduck()

        # Guard: a barge-in may have already transitioned state to LISTENING while
        # we were awaiting unduck() above.  In that case the new turn is already
        # in progress — do not stomp it by setting IDLE here.
        # NOTE: FOLLOW_UP is also a valid caller (timeout path) and must proceed to IDLE.
        if self._state == SpeakerState.LISTENING:
            logger.debug("_finish_turn: barge-in already in progress (LISTENING), skipping IDLE transition")
            return

        self._drain_queue(self._api_input_queue)
        self._reset_wake_detector(cooldown=True)

        if self._hybrid_vad:
            self._hybrid_vad.reset()

        if play_end_sound and self._sound_player:
            self._sound_player.play(SoundEvent.END_CONVERSATION)

        self._set_state(SpeakerState.IDLE)
        self._last_activity_time = time.time()

        self._session_turn_count += 1
        metrics.SESSION_TURNS.inc()
        if (
            self._api_manager is not None
            and self._api_manager.session_active
            and self._session_turn_count >= self.live_cfg.session_max_turns
        ):
            logger.info(
                "Session turn limit reached (%d), closing session.",
                self._session_turn_count,
            )
            metrics.SESSIONS_CLOSED.labels(reason='max_turns').inc()
            await self._api_manager.close_session()
            self._recorder.close()

        if self._is_tty:
            print()
        if self._api_manager is not None and self._api_manager.session_active:
            logger.info(
                "Listening for wake word... (session kept alive for %.0fs)",
                self.live_cfg.session_inactivity_timeout,
            )
        else:
            logger.info("Listening for wake word...")

    async def _close_on_initial_silence(self) -> None:
        """Close session silently after a false-positive wake word.

        Restores radio volume, closes the API session to save quota, and returns
        to IDLE without playing the end-conversation sound.
        """
        self._listening_entry_time = None
        if self._pending_verification is not None:
            self._pending_verification.cancel()
            self._pending_verification = None

        await self.mpd_client.unduck()
        self._drain_queue(self._api_input_queue)

        if self._api_manager:
            await self._api_manager.close_session()
        self._recorder.close_as_false_trigger()
        metrics.FALSE_TRIGGERS.labels(reason='initial_silence').inc()
        metrics.SESSIONS_CLOSED.labels(reason='false_trigger').inc()

        self._reset_wake_detector(cooldown=True)
        if self._hybrid_vad:
            self._hybrid_vad.reset()

        self._set_state(SpeakerState.IDLE)
        logger.info("Listening for wake word... (session closed after false trigger)")

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

    async def _inactivity_monitor_task(self) -> None:
        """Close the API session after a period of IDLE inactivity.

        (MPD keepalive lives in mpd_client.watch_external_changes now.)
        """
        while self._running:
            await asyncio.sleep(5.0)

            metrics.update_system_metrics()

            if not self._api_manager.session_active:
                continue
            if self._state != SpeakerState.IDLE:
                continue

            elapsed = time.time() - self._last_activity_time
            if elapsed > self.live_cfg.session_inactivity_timeout:
                logger.info("Session inactive for %.0fs, closing...", elapsed)
                metrics.SESSIONS_CLOSED.labels(reason='inactivity').inc()
                await self._api_manager.close_session()
                self._recorder.close()

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
