"""Wake word detection around the model: which gates apply, warmup, cooldown."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import numpy as np

from audio.wake_word import WakeWordDetector
from config import AudioConfig, WakeWordConfig
from mpd_client.client import MPDClientWrapper
from state import WakeWordResult

# Two triggers closer than this are one wake word.
TRIGGER_COOLDOWN: float = 0.5
# "Radio playing" is held this long after the last True, see WakeGate.detect.
RADIO_PLAYING_HOLD: float = 5.0


class WakeGate:
    """Runs the wake word detector on each microphone frame.

    Decides between the strict and the relaxed gate set, swallows the frames
    of the warmup after a reset and the repeated triggers of one wake word.
    """

    def __init__(
        self,
        wake_cfg: WakeWordConfig,
        audio_cfg: AudioConfig,
        mpd_client: MPDClientWrapper,
        responding: Callable[[], bool],
    ) -> None:
        self.wake_cfg = wake_cfg
        self.audio_cfg = audio_cfg
        self.mpd_client = mpd_client
        self._responding = responding
        # Set in AudioOrchestrator.start(), with the rest of the components.
        self.detector: Optional[WakeWordDetector] = None
        self.executor: Optional[ThreadPoolExecutor] = None

        # Warmup control (prevent false wake word triggers after state transitions)
        self.frames_since_reset: int = 0
        self.warmup_frames_needed: int = 0
        self._radio_playing_until: float = 0.0
        self.last_trigger_time: float = 0.0

    @property
    def warming_up(self) -> bool:
        return self.frames_since_reset <= self.warmup_frames_needed

    def reset(self, *, short_warmup: bool = False, cooldown: bool = False) -> None:
        """Reset the wake word detector and recalculate the warmup budget.

        Args:
            short_warmup: Use 0.5 s warmup (barge-in or re-wake while LISTENING).
                          Default (False) uses 1.5 s warmup after a full turn ends.
            cooldown:     Additionally arm the detector cooldown to prevent
                          immediate re-triggering on the same audio event.
        """
        if self.detector:
            self.detector.reset()
            if cooldown:
                self.detector.set_cooldown()
        self.frames_since_reset = 0
        self.warmup_frames_needed = int(
            (0.5 if short_warmup else 1.5)
            * self.audio_cfg.input_sample_rate
            / self.audio_cfg.input_chunk
        )

    def accept_trigger(self) -> bool:
        """False for a trigger within TRIGGER_COOLDOWN of the last accepted one."""
        now = time.time()
        if now - self.last_trigger_time < TRIGGER_COOLDOWN:
            return False
        self.last_trigger_time = now
        return True

    async def detect(self, mono: np.ndarray) -> WakeWordResult:
        """Run wake word inference on one frame, in the executor."""
        self.frames_since_reset += 1

        # Relax gating whenever THIS speaker produces output — radio playing OR
        # our own TTS during RESPONDING (barge-in). Voice spoken over that output
        # scores lower on every gate (post-AEC residue + postfilter artefacts),
        # so the detector adapts per config (relaxed verifier threshold,
        # relaxed VAD gate). Without covering RESPONDING, barge-in fails: the
        # strict IDLE verifier rejects wake word spoken over the residue of
        # speaker own voice. Only the cached play state is read, which the MPD
        # watcher refreshes every state_poll_interval: asking MPD here made every
        # frame wait for the connect timeout while Mopidy did not answer.
        relaxation_configured = (
            self.wake_cfg.relaxed_verifier_threshold is not None
            or self.wake_cfg.relaxed_vad_threshold is not None
            or self.wake_cfg.relaxed_min_activation_frames is not None
        )
        # Debounce: the play state flips to False transiently even while
        # MPD plays steadily (stale/corrupted status reads in the client cache; MPD
        # itself is rock-stable). Those bad reads
        # flipped the wake thresholds to strict IDLE mid-radio, dropping ~half of valid
        # calls. Hold "radio playing" for 5s after the last True so gaps are bridged.
        # A frozen stream is silent, so it must not relax the gates either.
        playing = (
            self.mpd_client.is_playing_cached()
            and not self.mpd_client.is_stream_stalled()
        )
        if playing:
            self._radio_playing_until = time.monotonic() + RADIO_PLAYING_HOLD
        relaxed_mode = relaxation_configured and (
            self._responding()
            or playing
            or time.monotonic() < self._radio_playing_until
        )

        def _inference() -> tuple[float, float, float, bool]:
            return self.detector.process(mono, relaxed_mode=relaxed_mode)

        loop = asyncio.get_running_loop()
        if self.warming_up:
            await loop.run_in_executor(self.executor, _inference)
            return WakeWordResult(triggered=False, score=0.0, max_score=0.0)

        score, max_score, vad_score, triggered = await loop.run_in_executor(
            self.executor, _inference
        )
        return WakeWordResult(
            triggered=triggered,
            score=score,
            max_score=max_score,
            vad_score=vad_score,
            verifier_score=self.detector.last_verifier_score,
            window_rms=self.detector.last_window_rms,
            relaxed_mode=relaxed_mode,
        )
