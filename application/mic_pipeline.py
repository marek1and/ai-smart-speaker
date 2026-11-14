"""Microphone side of the speaker: frames, the audio kept from before the wake
word, wake word clips and the AEC health metric."""

import asyncio
import logging
import queue
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Deque, Optional

import numpy as np

from audio.recorder import save_clip
from config import AppConfig
import metrics
from state import WakeWordResult

logger = logging.getLogger(__name__)


class MicPipeline:
    """Takes frames off the capture queue and keeps what later steps need:
    the preroll sent to the API once a session opens, and the detector input
    saved as a clip when the wake word fires or nearly fires."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.audio_cfg = config.audio
        self.wake_cfg = config.wake_word
        # Set in AudioOrchestrator.start(), with the rest of the components.
        self.executor: Optional[ThreadPoolExecutor] = None

        # Filled by the SoundDevice callback thread.
        self.queue: queue.Queue[np.ndarray] = queue.Queue(
            maxsize=self.audio_cfg.input_queue_maxsize
        )

        # Preroll buffer for capturing audio before wake word
        preroll_frames = int(
            config.live.preroll_seconds
            * self.audio_cfg.input_sample_rate
            / self.audio_cfg.input_chunk
        )
        self._preroll: Deque[bytes] = deque(maxlen=max(preroll_frames, 5))

        # Detector input ring buffer, dumped to a WAV at trigger time. The
        # preroll buffer can't serve this purpose: it is flushed at session
        # open (~0.7s after the trigger), by which point it has rolled past
        # most of the triggering audio.
        self._clip_buffer: Optional[Deque[bytes]] = None
        self._clip_channels: int = 1
        self._last_candidate_time: float = 0.0
        if self.wake_cfg.save_trigger_clips or self.wake_cfg.save_candidate_clips:
            clip_frames = int(
                self.wake_cfg.trigger_clip_seconds
                * self.audio_cfg.input_sample_rate
                / self.audio_cfg.input_chunk
            )
            self._clip_buffer = deque(maxlen=max(clip_frames, 5))

        # AEC health metric: mic/reference ratios from playback frames
        # (see _update_aec_metric).
        self._aec_ratio_window: Deque[float] = deque(
            maxlen=max(config.metrics.aec_window_frames, 1)
        )
        self._aec_frame_counter: int = 0

    async def next_frame(self) -> Optional[tuple[np.ndarray, bytes]]:
        """The next captured frame as (mono, mono bytes); None when nothing
        arrived in time, so the caller can check whether to keep running."""
        try:
            raw_audio = await asyncio.wait_for(
                asyncio.to_thread(self.queue.get, timeout=0.5),
                timeout=1.0,
            )
        except (asyncio.TimeoutError, queue.Empty):
            return None

        # Extract mono channel (mic might be stereo)
        stereo = raw_audio.ndim == 2 and raw_audio.shape[1] > 1
        if stereo:
            mono = raw_audio[:, 0]
        else:
            mono = raw_audio.flatten()

        if mono.size == 0:
            return None

        mono_bytes = mono.tobytes()
        self._preroll.append(mono_bytes)
        if self._clip_buffer is not None:
            # Full frame, not just mono: with the XVF3800 right channel
            # muxed to the far-end reference (5 0), a stereo clip
            # self-labels whether our own audio was playing and what it
            # was. Downstream prepare_recordings.py scores channel 0.
            self._clip_channels = raw_audio.shape[1] if stereo else 1
            self._clip_buffer.append(
                raw_audio.tobytes() if stereo else mono_bytes
            )
        if stereo:
            self._update_aec_metric(raw_audio)
        return mono, mono_bytes

    def preroll(self) -> list[bytes]:
        """The frames from before now, oldest first (the wake word is in them)."""
        return list(self._preroll)

    def take_preroll(self) -> list[bytes]:
        """The preroll, handed over once: the next turn starts with a fresh one."""
        frames = list(self._preroll)
        self._preroll.clear()
        return frames

    def save_trigger_clip(self, result: WakeWordResult) -> None:
        if self._clip_buffer is not None:
            self._save_clip("trigger", result)

    def maybe_save_candidate_clip(self, result: WakeWordResult, last_trigger_time: float) -> None:
        """Dump a near-miss (hot score, no trigger) for offline review.

        Captures the band invisible to trigger clips: voices the model
        undershoots (household members) and borderline noises. Rate-limited
        by cooldown_seconds against both hot streaks of the same event and
        the hot tail that follows a real trigger.
        """
        if (
            not self.wake_cfg.save_candidate_clips
            or self._clip_buffer is None
            or result.score < self.wake_cfg.candidate_threshold
        ):
            return
        now = time.monotonic()
        if (
            now - self._last_candidate_time < self.wake_cfg.cooldown_seconds
            or now - last_trigger_time < self.wake_cfg.cooldown_seconds
        ):
            return
        self._last_candidate_time = now
        logger.info(
            "Near-miss candidate (score=%.3f, verifier=%s, rms=%.0f) — saving clip",
            result.score,
            f"{result.verifier_score:.3f}" if result.verifier_score is not None else "n/a",
            result.window_rms,
        )
        self._save_clip("candidate", result)

    def _save_clip(self, kind: str, result: WakeWordResult) -> None:
        # Snapshot before more frames roll the deque; write off-loop.
        clip_frames = list(self._clip_buffer)
        asyncio.get_running_loop().run_in_executor(
            self.executor,
            save_clip,
            clip_frames,
            self.audio_cfg.input_sample_rate,
            kind,
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
