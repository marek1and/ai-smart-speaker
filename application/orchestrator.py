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
import os
import queue
import time
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Deque, Optional

import numpy as np

from audio.input import AudioInput
from audio.output import AudioOutput
from audio.sounds import SoundEvent, SoundPlayer
from audio.vad import HybridVAD, create_hybrid_vad
from audio.wake_word import WakeWordDetector
from config import (
    AppConfig,
    AudioConfig,
    LiveConfig,
    SoundConfig,
    VADConfig,
    WakeWordConfig,
)
from realtime import BaseRealtimeManager, create_realtime_manager
from state import AudioFrame, SpeakerState, WakeWordResult

logger = logging.getLogger(__name__)

# Constants
RECORDINGS_DIR = "recordings"
BARGE_IN_COOLDOWN: float = 0.5


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
        """
        Initializes the AudioOrchestrator with the application configuration.
        """
        self.config = config
        self.live_cfg = config.live
        self.audio_cfg = config.audio
        self.wake_cfg = config.wake_word
        self.vad_cfg = config.vad
        self.sound_cfg = config.sound

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

        # Components (initialized in start())
        self._audio_input: Optional[AudioInput] = None
        self._audio_output: Optional[AudioOutput] = None
        self._wake_detector: Optional[WakeWordDetector] = None
        self._hybrid_vad: Optional[HybridVAD] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._sound_player: Optional[SoundPlayer] = None

        # Live API manager (Gemini or OpenAI, selected via config)
        self._api_manager: Optional[BaseRealtimeManager] = None

        # Recording
        self._wav_file: Optional[wave.Wave_write] = None
        self._wav_path: Optional[str] = None

        # Warmup control (prevent false wake word triggers after state transitions)
        self._frames_since_reset: int = 0
        self._warmup_frames_needed: int = 0

        # Current audio frame for status display
        self._current_audio_frame: Optional[np.ndarray] = None

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

    async def start(self) -> None:
        """Initialize components and start the main loop."""
        logger.info("Starting Voice Assistant...")

        # Initialize audio I/O (SoundDevice)
        self._audio_input = AudioInput(self.audio_cfg, self._mic_queue)
        self._audio_output = AudioOutput(self.audio_cfg, self._speaker_queue)

        # Initialize wake word detector
        self._wake_detector = WakeWordDetector(self.wake_cfg, self.audio_cfg)

        # Initialize sound player
        self._sound_player = SoundPlayer(self.sound_cfg)

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
        )

        # Thread pool for CPU-bound tasks (wake word, VAD)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="audio")

        # Initial warmup
        self._warmup_frames_needed = int(
            1.5 * self.audio_cfg.input_sample_rate / self.audio_cfg.input_chunk
        )
        self._frames_since_reset = 0

        self._running = True
        self._last_activity_time = time.time()

        # Start audio streams
        self._audio_input.start()
        self._audio_output.start()

        logger.info("Barge-in: %s", "enabled" if self.live_cfg.barge_in else "disabled")
        logger.info("Manual VAD: %s", "enabled" if self.live_cfg.enable_manual_vad else "disabled")
        
        # Play startup sound
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

    async def _cleanup(self) -> None:
        """Clean up all resources."""
        self._running = False

        # Close any active conversation
        if self._api_manager:
            self._api_manager.shutdown()
            await self._api_manager.close_session()

        # Close session recording
        self._close_session_recording()

        # Stop audio streams
        if self._audio_output:
            self._audio_output.stop()

        if self._audio_input:
            self._audio_input.stop()

        # Close VAD
        if self._hybrid_vad:
            self._hybrid_vad.close()

        if self._executor:
            self._executor.shutdown(wait=False)

        logger.info("Voice Assistant stopped.")

    async def _main_loop(self) -> None:
        """Main event loop orchestrating all async tasks."""
        tasks = [
            asyncio.create_task(self._audio_capture_task(), name="audio_capture"),
            asyncio.create_task(self._audio_playback_bridge_task(), name="playback_bridge"),
            asyncio.create_task(self._state_machine_task(), name="state_machine"),
            asyncio.create_task(self._inactivity_monitor_task(), name="inactivity_monitor"),
        ]

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
        loop = asyncio.get_event_loop()

        while self._running:
            try:
                # Get audio from mic queue (blocking with timeout)
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

                # Wake word detection in executor
                wake_result = await self._detect_wake_word_async(loop, mono)
                await self._handle_wake_word_result(wake_result)

                # Process audio based on state
                if self._state == SpeakerState.LISTENING:
                    await self._process_listening_audio(mono, mono_bytes)

                elif self._state == SpeakerState.FOLLOW_UP:
                    await self._process_followup_audio(mono, mono_bytes)

            except OSError as e:
                logger.error("Audio capture error: %s", e)
                await asyncio.sleep(0.1)

    async def _process_listening_audio(
        self, mono: np.ndarray, mono_bytes: bytes
    ) -> None:
        """Process audio in LISTENING state with Hybrid VAD."""
        if not self._api_manager.session_active:
            return

        # Process through Hybrid VAD
        if self._hybrid_vad and self.live_cfg.enable_manual_vad:
            speech_started, speech_ended = self._hybrid_vad.process_frame(mono)

            # Send activity signals to API
            if speech_started:
                logger.info("VAD: Speech started - sending activity_start")
                await self._api_manager.send_activity_start()

            if speech_ended:
                logger.info("VAD: Speech ended - sending activity_end")
                await self._api_manager.send_activity_end()

        # Forward audio to API
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
        # Reset VAD state for new turn
        if self._hybrid_vad:
            self._hybrid_vad.reset()

        # Start new turn
        self._api_manager.start_new_turn()
        self._turn_complete_event.clear()

        # Recording continues within the same session (no new file)
        await self._send_preroll()
        self._set_state(SpeakerState.LISTENING)

    async def _detect_wake_word_async(
        self,
        loop: asyncio.AbstractEventLoop,
        mono: np.ndarray,
    ) -> WakeWordResult:
        """Run wake word inference in executor."""

        def _inference() -> tuple[float, float, bool]:
            return self._wake_detector.process(mono)

        if self._frames_since_reset <= self._warmup_frames_needed:
            await loop.run_in_executor(self._executor, _inference)
            return WakeWordResult(triggered=False, score=0.0, max_score=0.0)

        score, max_score, triggered = await loop.run_in_executor(
            self._executor, _inference
        )

        return WakeWordResult(triggered=triggered, score=score, max_score=max_score)

    async def _handle_wake_word_result(self, result: WakeWordResult) -> None:
        """Handle wake word detection result based on current state."""
        if not result.triggered:
            self._print_status(result)
            return

        # Cooldown check
        now = time.time()
        if now - self._last_barge_in_time < BARGE_IN_COOLDOWN:
            return

        print()  # New line after status
        logger.info("Wake word detected! (score=%.3f)", result.score)
        self._last_barge_in_time = now

        # Play wake word sound
        if self._sound_player:
            self._sound_player.play(SoundEvent.WAKE_WORD)

        if self._state == SpeakerState.IDLE:
            self._wake_event.set()

        elif self._state == SpeakerState.RESPONDING:
            logger.info("BARGE-IN: Interrupting AI playback!")
            await self._execute_barge_in()

        elif self._state == SpeakerState.LISTENING:
            self._wake_detector.reset()
            self._frames_since_reset = 0
            self._warmup_frames_needed = int(
                0.5 * self.audio_cfg.input_sample_rate / self.audio_cfg.input_chunk
            )

    def _print_status(self, result: WakeWordResult) -> None:
        """Print current status based on state."""
        if self._state == SpeakerState.IDLE:
            if self._frames_since_reset <= self._warmup_frames_needed:
                print(
                    f"\r[WARMUP] {self._frames_since_reset}/{self._warmup_frames_needed}",
                    end="",
                    flush=True,
                )
            else:
                print(
                    f"\r[IDLE] score={result.score:.3f} max={result.max_score:.3f}",
                    end="",
                    flush=True,
                )
        elif self._state == SpeakerState.LISTENING:
            vad_prob = 0.0
            if self._hybrid_vad:
                vad_prob = self._hybrid_vad.last_probability
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
            vad_prob = 0.0
            if self._hybrid_vad:
                vad_prob = self._hybrid_vad.last_probability
            print(
                f"\r[FOLLOW_UP] vad={vad_prob:.2f}",
                end="",
                flush=True,
            )

    async def _execute_barge_in(self) -> None:
        """Execute hard barge-in with instant audio cutoff."""
        self._stop_playback_event.set()
        self._playback_interrupted = True
        self._api_manager.playback_interrupted = True

        # INSTANT FLUSH: Stop audio output immediately
        if self._audio_output:
            self._audio_output.stop_immediate()

        # Clear API queues
        while not self._api_output_queue.empty():
            try:
                self._api_output_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        while not self._api_input_queue.empty():
            try:
                self._api_input_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Reset wake word and VAD state
        self._wake_detector.reset()
        if self._hybrid_vad:
            self._hybrid_vad.reset()

        self._frames_since_reset = 0
        self._warmup_frames_needed = int(
            0.5 * self.audio_cfg.input_sample_rate / self.audio_cfg.input_chunk
        )

        # Start new turn - ignores stale data from previous turn
        self._api_manager.start_new_turn()
        self._turn_complete_event.clear()

        # Send activity_start to signal to API that user is interrupting
        if self.live_cfg.enable_manual_vad:
            await self._api_manager.send_activity_start()

        self._set_state(SpeakerState.LISTENING)

        # Resume audio output for next response
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

        if self._wav_file:
            self._wav_file.writeframes(audio_bytes)

    # -------------------------------------------------------------------------
    # Audio Playback Bridge (API queue -> Speaker queue)
    # -------------------------------------------------------------------------

    async def _audio_playback_bridge_task(self) -> None:
        """Bridge audio from API output queue to speaker queue.

        Processes ALL available chunks at once to prevent buffer underruns.
        """
        while self._running:
            try:
                # Wait for at least one chunk
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

                # Transfer all chunks to speaker queue
                for chunk in chunks:
                    if self._stop_playback_event.is_set():
                        break
                    try:
                        self._speaker_queue.put_nowait(chunk)
                    except queue.Full:
                        # Drop oldest and try again
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
                        follow_up_start = None
                        await self._finish_turn()
                    else:
                        await asyncio.sleep(0.05)

            except (OSError, ConnectionError) as e:
                logger.error("State machine error: %s", e)
                import traceback

                traceback.print_exc()
                await asyncio.sleep(1.0)

    async def _handle_idle_state(self) -> None:
        """Handle IDLE state: wait for wake word."""
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            return

        self._wake_event.clear()

        # Open session if not already active
        if not self._api_manager.session_active:
            await self._api_manager.open_session()
            # Start recording for this new session
            self._start_session_recording()
        else:
            logger.info("Reusing existing session (recording continues)")

        # Reset VAD for new turn
        if self._hybrid_vad:
            self._hybrid_vad.reset()

        # Start new turn - this resets counters and ignores stale data
        self._api_manager.start_new_turn()
        self._turn_complete_event.clear()
        self._last_activity_time = time.time()

        await self._send_preroll()
        self._set_state(SpeakerState.LISTENING)

    async def _handle_listening_state(self) -> None:
        """Handle LISTENING state: wait for first audio response."""
        if self._api_manager.chunks_received > 0:
            logger.info("First audio received, transitioning to RESPONDING")
            self._set_state(SpeakerState.RESPONDING)
            return

        if self._turn_complete_event.is_set():
            self._turn_complete_event.clear()
            logger.info("Turn complete without audio response")
            await self._finish_turn()
            return

        await asyncio.sleep(0.05)

    async def _handle_responding_state(self) -> None:
        """Handle RESPONDING state: wait for turn_complete + playback."""
        if not self._turn_complete_event.is_set():
            await asyncio.sleep(0.05)
            return

        self._turn_complete_event.clear()

        # Wait for speaker queue to drain
        while not self._speaker_queue.empty() and self._running:
            if self._playback_interrupted:
                break
            await asyncio.sleep(0.1)

        if self._state != SpeakerState.RESPONDING:
            return

        await asyncio.sleep(0.3)

        if not self._playback_interrupted:
            logger.info(
                "Waiting for follow-up (%.1fs, Silero VAD)...",
                self.live_cfg.followup_timeout,
            )
            # Play follow-up sound
            if self._sound_player:
                self._sound_player.play(SoundEvent.FOLLOW_UP)
            # Reset VAD for follow-up detection
            if self._hybrid_vad:
                self._hybrid_vad.reset()
            self._set_state(SpeakerState.FOLLOW_UP)
        else:
            await self._finish_turn()

    async def _finish_turn(self) -> None:
        """Common cleanup after conversation turn is finished.
        
        Note: Recording continues within session - WAV file is NOT closed here.
        """
        while not self._api_input_queue.empty():
            try:
                self._api_input_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._wake_detector.reset()
        self._wake_detector.set_cooldown()
        self._frames_since_reset = 0
        self._warmup_frames_needed = int(
            1.5 * self.audio_cfg.input_sample_rate / self.audio_cfg.input_chunk
        )

        # Reset VAD state
        if self._hybrid_vad:
            self._hybrid_vad.reset()

        # Play end conversation sound
        if self._sound_player:
            self._sound_player.play(SoundEvent.END_CONVERSATION)

        self._set_state(SpeakerState.IDLE)
        self._last_activity_time = time.time()

        print()
        if self._api_manager.session_active:
            logger.info(
                "Listening for wake word... (session kept alive for %.0fs)",
                self.live_cfg.session_inactivity_timeout,
            )
        else:
            logger.info("Listening for wake word...")

    async def _inactivity_monitor_task(self) -> None:
        """Monitor for session inactivity and close session after timeout."""
        while self._running:
            await asyncio.sleep(5.0)

            if not self._api_manager.session_active:
                continue

            if self._state != SpeakerState.IDLE:
                continue

            elapsed = time.time() - self._last_activity_time
            if elapsed > self.live_cfg.session_inactivity_timeout:
                logger.info("Session inactive for %.0fs, closing...", elapsed)
                await self._api_manager.close_session()
                # Close recording when session ends
                self._close_session_recording()

    # -------------------------------------------------------------------------
    # Recording (session-scoped)
    # -------------------------------------------------------------------------

    async def _send_preroll(self) -> None:
        """Send preroll frames to capture audio before wake word."""
        for frame_data in self._preroll_buffer:
            frame = AudioFrame(data=frame_data, mime_type=self.live_cfg.input_mime_type)
            try:
                self._api_input_queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

            if self._wav_file:
                self._wav_file.writeframes(frame_data)

        self._preroll_buffer.clear()

    def _start_session_recording(self) -> None:
        """Start recording for the entire API session."""
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._wav_path = os.path.join(RECORDINGS_DIR, f"session_{timestamp}.wav")
        self._wav_file = wave.open(self._wav_path, "wb")
        self._wav_file.setnchannels(1)
        self._wav_file.setsampwidth(2)
        self._wav_file.setframerate(self.audio_cfg.input_sample_rate)
        logger.info("Started session recording: %s", self._wav_path)

    def _close_session_recording(self) -> None:
        """Close the session recording WAV file."""
        if self._wav_file:
            self._wav_file.close()
            logger.info("Closed session recording: %s", self._wav_path)
            self._wav_file = None
            self._wav_path = None
