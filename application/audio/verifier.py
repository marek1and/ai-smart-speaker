"""Wake word STT verifier using faster-whisper (tiny model).

Runs synchronously in a thread executor — do NOT call from the event loop directly.
Accepts raw PCM audio (int16, 16kHz) and returns True if any configured keyword
is found in the transcript.
"""

import logging

import numpy as np

from config import WakeWordConfig

logger = logging.getLogger(__name__)


class WakeWordVerifier:
    """Post-trigger STT verification to suppress false wake word activations."""

    def __init__(self, cfg: WakeWordConfig) -> None:
        from faster_whisper import WhisperModel

        self._keywords = cfg.stt_keywords
        logger.info("Loading STT verifier model '%s' (int8, cpu)...", cfg.stt_model)
        self._model = WhisperModel(cfg.stt_model, device="cpu", compute_type="int8")
        logger.info("STT verifier ready. Keywords: %s", self._keywords)

    def verify(self, audio_chunks: list[bytes]) -> bool:
        """Verify that a wake keyword is present in the given audio.

        Args:
            audio_chunks: List of raw PCM bytes (int16, 16kHz mono).

        Returns:
            True if any keyword is found in the transcript.
        """
        if not audio_chunks:
            return False

        raw = b"".join(audio_chunks)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

        segments, _ = self._model.transcribe(
            audio,
            language="en",
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            no_speech_threshold=0.4,
            vad_filter=True,
        )
        transcript = " ".join(s.text for s in segments).lower().strip()
        matched = any(kw in transcript for kw in self._keywords)
        logger.info(
            "STT verify: %r → %s", transcript, "OK" if matched else "REJECTED"
        )
        return matched
