"""State management for voice assistant."""

import enum
import time
from dataclasses import dataclass, field
from typing import Optional


class SpeakerState(enum.Enum):
    """State machine states for the voice assistant."""
    IDLE = "idle"              # Waiting for wake word, NOT sending audio
    LISTENING = "listening"    # Sending audio to API, waiting for response
    RESPONDING = "responding"  # AI is responding, NOT sending audio (barge-in via wake word)
    FOLLOW_UP = "follow_up"    # Waiting for user to speak (local VAD), then -> LISTENING


@dataclass
class AudioFrame:
    """Wrapper for audio data with metadata."""
    data: bytes
    timestamp: float = field(default_factory=time.time)
    mime_type: str = "audio/pcm;rate=16000"


@dataclass
class WakeWordResult:
    """Result from wake word detection."""
    triggered: bool
    score: float
    max_score: float
    vad_score: float = 0.0
    verifier_score: Optional[float] = None
    # Peak frame RMS over the detector score window — calibration/diagnostic
    # data logged with every detection.
    window_rms: float = 0.0
    timestamp: float = field(default_factory=time.time)
