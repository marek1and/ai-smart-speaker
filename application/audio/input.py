"""Audio input using SoundDevice (callback-based, non-blocking).

Captures at 16kHz (Gemini API input format).
Callbacks push mono int16 numpy arrays to a queue.Queue.
"""

from __future__ import annotations

import logging
import queue
import time
from typing import Optional

import numpy as np
import sounddevice as sd

from config import AudioConfig

logger = logging.getLogger(__name__)


def extract_first_channel(audio: np.ndarray, channels: int) -> np.ndarray:
    """Extract first channel from multi-channel audio.

    Args:
        audio: Audio array (can be 1D mono or 2D multi-channel)
        channels: Number of channels in input

    Returns:
        Mono audio array (int16)
    """
    if channels == 1:
        return audio.astype(np.int16)

    if audio.ndim == 1:
        # Interleaved format: reshape to (frames, channels)
        frames = audio.size // channels
        if frames == 0:
            return np.array([], dtype=np.int16)
        audio = audio[: frames * channels].reshape(frames, channels)

    # Extract first channel
    return audio[:, 0].astype(np.int16)


class AudioInput:
    """Non-blocking audio input using SoundDevice callbacks.

    Captures audio at 16kHz (Gemini API input format) and pushes
    mono int16 numpy arrays to the output queue.

    Usage:
        input_queue = queue.Queue()
        audio_in = AudioInput(config, input_queue)
        audio_in.start()

        while True:
            try:
                mono_audio = input_queue.get(timeout=0.1)
                # Process audio...
            except queue.Empty:
                continue

        audio_in.stop()
    """

    def __init__(
        self,
        config: AudioConfig,
        output_queue: queue.Queue,
    ) -> None:
        """Initialize audio input.

        Args:
            config: Audio configuration
            output_queue: Queue to receive mono int16 numpy arrays
        """
        self.config = config
        self.output_queue = output_queue

        self._stream: Optional[sd.InputStream] = None
        self._running = False

        # Telemetry for dropped frames (rate-limited logging)
        self._dropped_frames: int = 0
        self._last_drop_log_time: float = 0.0

        # Clipping detection (rate-limited logging)
        self._last_clip_log_time: float = 0.0
        self._clip_threshold: int = 32000  # Close to int16 max (32767)

        # Find device
        self._device = self._find_device(config.input_device, kind="input")
        if self._device is not None:
            info = sd.query_devices(self._device)
            logger.info("Audio input device: %s", info["name"])
        else:
            logger.info("Audio input: using system default")

    def _find_device(
        self, name: Optional[str], kind: str
    ) -> Optional[int]:
        """Find device by name.

        Args:
            name: Device name substring or None for default
            kind: "input" or "output"

        Returns:
            Device index or None for default
        """
        if name is None:
            return None

        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            channel_key = "max_input_channels" if kind == "input" else "max_output_channels"
            if name.lower() in dev["name"].lower() and dev[channel_key] > 0:
                return i

        logger.warning("Device '%s' not found, using default", name)
        return None

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: dict,
        status: sd.CallbackFlags,
    ) -> None:
        """SoundDevice input callback.

        Called from audio thread - must be fast, no blocking operations.
        """
        if status:
            logger.warning("Audio input status: %s", status)

        if not self._running:
            return

        # Extract mono channel (indata is (frames, channels) float32 or int16)
        # SoundDevice with dtype=np.int16 gives us int16 directly
        if indata.ndim == 2 and indata.shape[1] > 1:
            mono = indata[:, 0].copy()
        else:
            mono = indata.flatten().copy()

        # Clipping detection - check if audio is near int16 limit
        self._check_clipping(mono)

        # Push to queue (non-blocking, drop if full)
        try:
            self.output_queue.put_nowait(mono)
        except queue.Full:
            # Drop oldest frame and try again
            self._dropped_frames += 1
            self._log_dropped_frame()
            try:
                self.output_queue.get_nowait()
                self.output_queue.put_nowait(mono)
            except queue.Empty:
                pass

    def _log_dropped_frame(self) -> None:
        """Log dropped frames with rate limiting (max once per second)."""
        now = time.time()
        if now - self._last_drop_log_time >= 1.0:
            logger.warning(
                "Input queue full - dropped %d frame(s) in last second",
                self._dropped_frames,
            )
            self._dropped_frames = 0
            self._last_drop_log_time = now

    def _check_clipping(self, audio: np.ndarray) -> None:
        """Check for audio clipping and log warning (rate-limited).

        Args:
            audio: Mono int16 audio samples
        """
        if audio.size == 0:
            return

        max_val = int(np.max(np.abs(audio)))

        if max_val >= self._clip_threshold:
            now = time.time()
            # Rate limit: log at most once per second
            if now - self._last_clip_log_time >= 1.0:
                logger.warning(
                    "⚠️ CLIPPING DETECTED! Max amplitude: %d (limit: 32767)",
                    max_val,
                )
                self._last_clip_log_time = now

    def start(self) -> None:
        """Start audio capture."""
        if self._running:
            return

        self._running = True

        self._stream = sd.InputStream(
            samplerate=self.config.input_sample_rate,
            channels=self.config.input_channels,
            dtype=np.int16,
            blocksize=self.config.input_chunk,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()
        logger.info(
            "Audio input started (rate=%d, channels=%d, chunk=%d)",
            self.config.input_sample_rate,
            self.config.input_channels,
            self.config.input_chunk,
        )

    def stop(self) -> None:
        """Stop audio capture."""
        self._running = False

        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            logger.info("Audio input stopped")

    def is_active(self) -> bool:
        """Check if stream is active."""
        return self._stream is not None and self._stream.active
