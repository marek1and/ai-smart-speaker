"""Sound effects player for voice assistant events.

Plays notification sounds when specific events occur (wake word, state changes, etc.).
Uses sounddevice for non-blocking playback.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

if TYPE_CHECKING:
    from config import SoundConfig

logger = logging.getLogger(__name__)

# Default sounds directory
SOUNDS_DIR = Path(__file__).parent.parent / "sounds"


class SoundEvent(Enum):
    """Events that trigger sound playback."""

    STARTUP = "startup"
    WAKE_WORD = "wake_word"
    FOLLOW_UP = "follow_up"
    END_CONVERSATION = "end_conversation"
    ERROR = "error"
    CONFIRM_ACTION = "confirm_action"


# Mapping from events to sound files
DEFAULT_SOUND_MAP: dict[SoundEvent, str] = {
    SoundEvent.STARTUP: "startup.wav",
    SoundEvent.WAKE_WORD: "wake_up.wav",
    SoundEvent.FOLLOW_UP: "follow_up.wav",
    SoundEvent.END_CONVERSATION: "end_conversation.wav",
    SoundEvent.ERROR: "error.wav",
    SoundEvent.CONFIRM_ACTION: "confirm_action.wav",
}


class SoundPlayer:
    """Non-blocking sound effects player.
    
    Loads sounds on init and plays them asynchronously when events occur.
    Thread-safe - can be called from any thread.
    
    Usage:
        player = SoundPlayer()
        player.play(SoundEvent.WAKE_WORD)
    """
    
    def __init__(
        self,
        config: Optional["SoundConfig"] = None,
        sound_map: Optional[dict[SoundEvent, str]] = None,
    ) -> None:
        """Initialize sound player.
        
        Args:
            config: Sound player configuration
            sound_map: Custom mapping of events to sound files
        """
        # Import here to avoid circular imports
        from config import SoundConfig as SoundConfigClass
        
        self.config = config or SoundConfigClass()
        self.sound_map = sound_map or DEFAULT_SOUND_MAP
        
        # Determine sounds directory
        self._sounds_dir = self.config.sounds_dir or SOUNDS_DIR
        
        # Cache for loaded sounds: {event: (audio_data, sample_rate)}
        self._sounds: dict[SoundEvent, tuple[np.ndarray, int]] = {}
        
        # Lock for thread-safe playback
        self._lock = threading.Lock()
        
        # Currently playing stream (for potential cancellation)
        self._current_stream: Optional[sd.OutputStream] = None

        # Monotonic timestamp when the most recent effect finishes playing.
        # Consumers (VAD gating) compare against it to ignore our own sound
        # residue picked up by the microphone.
        self._busy_until: float = 0.0
        
        # Find output device
        self._device = self._find_device(self.config.output_device)
        
        # Preload all sounds
        self._preload_sounds()
    
    def _find_device(self, name: Optional[str]) -> Optional[int]:
        """Find output device by name."""
        if name is None:
            return None
        
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            if name.lower() in dev["name"].lower() and dev["max_output_channels"] > 0:
                return i
        
        logger.warning("Sound output device '%s' not found, using default", name)
        return None
    
    def _preload_sounds(self) -> None:
        """Preload all sound files into memory."""
        for event, filename in self.sound_map.items():
            filepath = self._sounds_dir / filename
            
            if not filepath.exists():
                logger.warning("Sound file not found: %s", filepath)
                continue
            
            try:
                audio, sample_rate = sf.read(filepath, dtype="float32")
                
                # Convert to mono if stereo
                if audio.ndim == 2:
                    audio = np.mean(audio, axis=1)
                
                # Apply volume
                audio = audio * self.config.volume
                
                self._sounds[event] = (audio, sample_rate)
                logger.debug("Loaded sound: %s (%s)", event.value, filename)
                
            except Exception as e:
                logger.error("Failed to load sound %s: %s", filename, e)
        
        logger.info(
            "Sound player initialized: %d/%d sounds loaded (enabled=%s, volume=%.1f)",
            len(self._sounds),
            len(self.sound_map),
            self.config.enabled,
            self.config.volume,
        )
    
    def play(self, event: SoundEvent, block: bool = False) -> None:
        """Play sound for an event.
        
        Args:
            event: The event that triggered the sound
            block: If True, wait for sound to finish playing
        """
        if not self.config.enabled:
            logger.debug("Sound player disabled, skipping: %s", event.value)
            return
        
        if event not in self._sounds:
            logger.warning("No sound loaded for event: %s", event.value)
            return
        
        audio, sample_rate = self._sounds[event]

        logger.info("🔊 Playing sound: %s (sr=%d, len=%.2fs)",
                    event.value, sample_rate, len(audio) / sample_rate)
        self._busy_until = time.monotonic() + len(audio) / sample_rate
        
        if block:
            self._play_blocking(audio, sample_rate)
        else:
            # Play in background thread
            thread = threading.Thread(
                target=self._play_blocking,
                args=(audio, sample_rate),
                daemon=True,
            )
            thread.start()
    
    def _play_blocking(self, audio: np.ndarray, sample_rate: int) -> None:
        """Play audio synchronously.
        
        Args:
            audio: Audio data (float32)
            sample_rate: Sample rate in Hz
        """
        with self._lock:
            try:
                sd.play(audio, sample_rate, device=self._device)
                sd.wait()
                logger.debug("Sound playback completed")
            except Exception as e:
                logger.error("Error playing sound: %s", e)
    
    @property
    def busy_until(self) -> float:
        """Monotonic time when the last started effect finishes playing."""
        return self._busy_until

    def stop(self) -> None:
        """Stop any currently playing sound."""
        with self._lock:
            try:
                sd.stop()
            except Exception as e:
                logger.error("Error stopping sound: %s", e)
    
    def set_volume(self, volume: float) -> None:
        """Set volume for future playback (reloads all sounds).
        
        Args:
            volume: Volume level (0.0 - 1.0)
        """
        from config import SoundConfig
        self.config = SoundConfig(
            enabled=self.config.enabled,
            volume=max(0.0, min(1.0, volume)),
            sounds_dir=self.config.sounds_dir,
            output_device=self.config.output_device,
        )
        # Reload sounds with new volume
        self._preload_sounds()
    
    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable sound playback.
        
        Args:
            enabled: Whether sounds should play
        """
        self.config.enabled = enabled
        logger.info("Sound player %s", "enabled" if enabled else "disabled")
