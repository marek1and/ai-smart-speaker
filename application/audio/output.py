"""Audio output using SoundDevice (callback-based, non-blocking).

Plays at 24kHz (Gemini API output format).
PipeWire resamples to actual device rate (typically 48kHz).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

import numpy as np
import sounddevice as sd

from config import AudioConfig

logger = logging.getLogger(__name__)


class AudioOutput:
    """Non-blocking audio output using SoundDevice callbacks.

    Plays audio at 24kHz (Gemini API output format). PipeWire will
    resample to actual device rate (typically 48kHz).

    Usage:
        output_queue = queue.Queue()
        audio_out = AudioOutput(config, output_queue)
        audio_out.start()

        # Feed audio data
        output_queue.put(audio_bytes)

        audio_out.stop()
    """

    def __init__(
        self,
        config: AudioConfig,
        input_queue: queue.Queue,
    ) -> None:
        """Initialize audio output.

        Args:
            config: Audio configuration
            input_queue: Queue to receive audio bytes (int16 PCM)
        """
        self.config = config
        self.input_queue = input_queue

        self._stream: Optional[sd.OutputStream] = None
        self._running = False
        self._stop_event = threading.Event()

        # Buffer for partial frames
        self._buffer = np.array([], dtype=np.int16)
        self._buffer_lock = threading.Lock()

        # Find device
        self._device = self._find_device(config.output_device, kind="output")
        if self._device is not None:
            info = sd.query_devices(self._device)
            logger.info("Audio output device: %s", info["name"])
        else:
            logger.info("Audio output: using system default")

    def _find_device(
        self, name: Optional[str], kind: str
    ) -> Optional[int]:
        """Find device by name."""
        if name is None:
            return None

        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            channel_key = "max_output_channels" if kind == "output" else "max_input_channels"
            if name.lower() in dev["name"].lower() and dev[channel_key] > 0:
                return i

        logger.warning("Device '%s' not found, using default", name)
        return None

    def _callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: dict,
        status: sd.CallbackFlags,
    ) -> None:
        """SoundDevice output callback.

        Called from audio thread - must be fast, fill buffer completely.
        """
        if status:
            logger.warning("Audio output status: %s", status)

        if self._stop_event.is_set():
            outdata.fill(0)
            return

        needed = frames

        with self._buffer_lock:
            # Use buffered data first
            available = min(len(self._buffer), needed)
            if available > 0:
                outdata[:available, 0] = self._buffer[:available]
                self._buffer = self._buffer[available:]
                needed -= available

            # Get more data from queue
            while needed > 0:
                try:
                    data = self.input_queue.get_nowait()

                    # Convert bytes to numpy if needed
                    if isinstance(data, bytes):
                        audio = np.frombuffer(data, dtype=np.int16)
                    else:
                        audio = data

                    # Fill output buffer
                    take = min(len(audio), needed)
                    start = frames - needed
                    outdata[start : start + take, 0] = audio[:take]
                    needed -= take

                    # Buffer remaining
                    if take < len(audio):
                        self._buffer = np.concatenate([self._buffer, audio[take:]])

                except queue.Empty:
                    # No more data - fill with silence
                    start = frames - needed
                    outdata[start:, 0] = 0
                    break

    def start(self) -> None:
        """Start audio playback."""
        if self._running:
            return

        self._running = True
        self._stop_event.clear()

        self._stream = sd.OutputStream(
            samplerate=self.config.output_sample_rate,
            channels=self.config.output_channels,
            dtype=np.int16,
            blocksize=2048,  # ~85ms at 24kHz (larger buffer for smoother playback)
            device=self._device,
            callback=self._callback,
            latency="high",  # Request higher latency for more stable playback
        )
        self._stream.start()
        logger.info(
            "Audio output started (rate=%d)",
            self.config.output_sample_rate,
        )

    def stop(self) -> None:
        """Stop audio playback gracefully."""
        self._running = False

        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            logger.info("Audio output stopped")

    def stop_immediate(self) -> None:
        """Stop audio playback immediately (for barge-in).

        This signals the callback to output silence and then stops the stream.
        """
        self._stop_event.set()

        # Clear buffer
        with self._buffer_lock:
            self._buffer = np.array([], dtype=np.int16)

        # Clear queue
        while True:
            try:
                self.input_queue.get_nowait()
            except queue.Empty:
                break

        # Stop stream
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        self._running = False
        logger.info("Audio output stopped immediately (barge-in)")

    def clear_queue(self) -> None:
        """Clear the audio queue (for barge-in)."""
        with self._buffer_lock:
            self._buffer = np.array([], dtype=np.int16)

        while True:
            try:
                self.input_queue.get_nowait()
            except queue.Empty:
                break

    def is_active(self) -> bool:
        """Check if stream is active."""
        return self._stream is not None and self._stream.active

    def resume(self) -> None:
        """Resume playback after barge-in."""
        self._stop_event.clear()
        if not self._running:
            self.start()
