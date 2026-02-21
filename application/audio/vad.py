"""Voice Activity Detection (VAD) implementations.

Uses openwakeword's built-in Silero VAD (ONNX-based).
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np

from config import VADConfig

logger = logging.getLogger(__name__)


@dataclass
class VADState:
    """Tracks VAD state for hybrid detection."""

    is_speaking: bool = False
    speech_frames: int = 0
    silence_frames: int = 0
    last_speech_time: float = 0.0

    def reset(self) -> None:
        """Reset state for new turn."""
        self.is_speaking = False
        self.speech_frames = 0
        self.silence_frames = 0


class BaseVAD(ABC):
    """Abstract base class for VAD implementations."""

    @abstractmethod
    def is_speech(self, audio: np.ndarray) -> bool:
        """Check if audio frame contains speech.

        Args:
            audio: Mono int16 audio samples at 16kHz

        Returns:
            True if speech detected
        """
        raise NotImplementedError

    def get_speech_probability(self, audio: np.ndarray) -> float:
        """Get speech probability (0.0 - 1.0).

        Default implementation returns 1.0 for speech, 0.0 for silence.
        """
        return 1.0 if self.is_speech(audio) else 0.0

    def reset(self) -> None:
        """Reset VAD internal state (if any)."""
        pass

    def close(self) -> None:
        """Release resources."""
        pass


class SileroVAD(BaseVAD):
    """Silero VAD using openwakeword's built-in implementation.

    This uses openwakeword.vad.VAD which is ONNX-based and works on Python 3.12+.
    """

    def __init__(self, threshold: float = 0.5, frame_size: int = 512) -> None:
        """Initialize Silero VAD.

        Args:
            threshold: Speech probability threshold (0.0 - 1.0)
            frame_size: Audio frame size in samples (512 recommended for 16kHz)
        """
        self.threshold = threshold
        self.frame_size = frame_size
        self._last_probability: float = 0.0

        try:
            from openwakeword.vad import VAD

            self._vad = VAD()
            self._vad.reset_states()
            logger.info(
                "Silero VAD initialized via openwakeword (threshold=%.2f, frame_size=%d)",
                threshold,
                frame_size,
            )
        except ImportError as e:
            raise RuntimeError(
                "openwakeword is required for Silero VAD. "
                "Install with: pip install openwakeword --no-deps"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Failed to initialize Silero VAD: {e}") from e

    def is_speech(self, audio: np.ndarray) -> bool:
        """Check if audio contains speech."""
        prob = self.get_speech_probability(audio)
        return prob >= self.threshold

    def get_speech_probability(self, audio: np.ndarray) -> float:
        """Get speech probability from Silero model."""
        if self._vad is None:
            return 0.0

        if audio.size < self.frame_size:
            return self._last_probability

        # Ensure audio is properly sized for Silero
        usable_size = audio.size - (audio.size % self.frame_size)
        if usable_size == 0:
            return self._last_probability

        usable = audio[:usable_size]

        try:
            # openwakeword's VAD.predict() returns speech probability
            probability = float(self._vad.predict(usable, frame_size=self.frame_size))
            self._last_probability = probability
            return probability
        except Exception as e:
            logger.warning("Silero VAD error: %s", e)
            return self._last_probability

    def reset(self) -> None:
        """Reset Silero internal state."""
        if self._vad is not None:
            self._vad.reset_states()
        self._last_probability = 0.0

    def close(self) -> None:
        """Release resources."""
        self._vad = None


class RMSVAD(BaseVAD):
    """Simple RMS energy-based VAD (fallback)."""

    def __init__(self, threshold: float = 1500.0) -> None:
        """Initialize RMS VAD.

        Args:
            threshold: RMS energy threshold for speech detection
        """
        self.threshold = threshold
        logger.info("RMS VAD initialized (threshold=%.1f)", threshold)

    def is_speech(self, audio: np.ndarray) -> bool:
        """Check if audio RMS exceeds threshold."""
        if audio.size == 0:
            return False

        rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float32)))))
        return rms >= self.threshold

    def get_speech_probability(self, audio: np.ndarray) -> float:
        """Estimate speech probability from RMS (normalized)."""
        if audio.size == 0:
            return 0.0

        rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float32)))))
        # Normalize: 0 at threshold/2, 1 at threshold*2
        normalized = (rms - self.threshold * 0.5) / (self.threshold * 1.5)
        return max(0.0, min(1.0, normalized))


class HybridVAD:
    """Hybrid VAD with state tracking for speech start/end detection.

    Combines VAD predictions with temporal smoothing to determine:
    - Speech start: N consecutive speech frames
    - Speech end: M consecutive silence frames
    """

    def __init__(self, vad: BaseVAD, config: VADConfig) -> None:
        """Initialize Hybrid VAD.

        Args:
            vad: Underlying VAD implementation
            config: VAD configuration
        """
        self.vad = vad
        self.config = config
        self.state = VADState()
        self._last_probability: float = 0.0

    def process_frame(self, audio: np.ndarray) -> tuple[bool, bool]:
        """Process audio frame and detect speech start/end.

        Args:
            audio: Mono int16 audio samples at 16kHz

        Returns:
            Tuple of (speech_started, speech_ended)
            - speech_started: True if speech just started (transition from silence to speech)
            - speech_ended: True if speech just ended (transition from speech to silence)
        """
        is_speech = self.vad.is_speech(audio)
        self._last_probability = self.vad.get_speech_probability(audio)

        speech_started = False
        speech_ended = False

        if is_speech:
            self.state.speech_frames += 1
            self.state.silence_frames = 0
            self.state.last_speech_time = time.time()

            # Detect speech start
            if (
                not self.state.is_speaking
                and self.state.speech_frames >= self.config.speech_frames_required
            ):
                self.state.is_speaking = True
                speech_started = True
                logger.debug("VAD: Speech started (frames=%d)", self.state.speech_frames)
        else:
            self.state.silence_frames += 1

            # Only count silence if we were speaking
            if self.state.is_speaking:
                # Detect speech end
                if self.state.silence_frames >= self.config.silence_frames_required:
                    self.state.is_speaking = False
                    speech_ended = True
                    self.state.speech_frames = 0
                    logger.debug(
                        "VAD: Speech ended (silence_frames=%d)", self.state.silence_frames
                    )
            else:
                # Not speaking, reset speech frame count
                self.state.speech_frames = 0

        return speech_started, speech_ended

    @property
    def is_speaking(self) -> bool:
        """Whether user is currently speaking."""
        return self.state.is_speaking

    @property
    def last_probability(self) -> float:
        """Last speech probability."""
        return self._last_probability

    def reset(self) -> None:
        """Reset state for new conversation turn."""
        self.state.reset()
        self.vad.reset()

    def close(self) -> None:
        """Release resources."""
        self.vad.close()


def create_vad(config: VADConfig) -> BaseVAD:
    """Create VAD instance based on configuration.

    Args:
        config: VAD configuration

    Returns:
        VAD instance
    """
    mode = config.mode.lower()

    if mode == "silero":
        try:
            return SileroVAD(
                threshold=config.silero_threshold,
                frame_size=config.silero_frame_size,
            )
        except Exception as e:
            logger.warning("Silero VAD unavailable (%s), falling back to RMS VAD", e)
            return RMSVAD(threshold=config.rms_threshold)

    return RMSVAD(threshold=config.rms_threshold)


def create_hybrid_vad(config: VADConfig) -> HybridVAD:
    """Create Hybrid VAD with state tracking.

    Args:
        config: VAD configuration

    Returns:
        HybridVAD instance
    """
    vad = create_vad(config)
    return HybridVAD(vad, config)
