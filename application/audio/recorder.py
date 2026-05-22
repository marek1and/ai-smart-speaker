import logging
import os
import wave
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_RECORDINGS_DIR = "recordings"


class SessionRecorder:
    """Manages WAV recording scoped to a single API session.

    Owns the WAV file lifecycle independently of the orchestrator.
    All public methods are safe to call when no recording is active (no-ops).

    Usage::

        recorder = SessionRecorder(sample_rate=16000)
        recorder.start()
        recorder.write(pcm_bytes)   # called for every audio frame
        recorder.close()
    """

    def __init__(self, sample_rate: int) -> None:
        self._sample_rate = sample_rate
        self._wav_file: Optional[wave.Wave_write] = None
        self._wav_path: Optional[str] = None

    @property
    def is_active(self) -> bool:
        """True while a WAV file is open."""
        return self._wav_file is not None

    def start(
        self,
        model_score: Optional[float] = None,
        verifier_score: Optional[float] = None,
    ) -> None:
        """Open a new timestamped WAV file and begin recording."""
        os.makedirs(_RECORDINGS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        score_tag = ""
        if model_score is not None:
            score_tag += f"_s{model_score:.3f}"
        if verifier_score is not None:
            score_tag += f"_v{verifier_score:.3f}"
        self._wav_path = os.path.join(_RECORDINGS_DIR, f"session_{timestamp}{score_tag}.wav")
        self._wav_file = wave.open(self._wav_path, "wb")
        self._wav_file.setnchannels(1)
        self._wav_file.setsampwidth(2)
        self._wav_file.setframerate(self._sample_rate)
        logger.info("Started session recording: %s", self._wav_path)

    def write(self, data: bytes) -> None:
        """Write raw PCM16 bytes to the current recording. No-op if inactive."""
        if self._wav_file:
            self._wav_file.writeframes(data)

    def close(self) -> None:
        """Close and flush the current recording. No-op if inactive."""
        if self._wav_file:
            self._wav_file.close()
            logger.info("Closed session recording: %s", self._wav_path)
            self._wav_file = None
            self._wav_path = None

    def close_as_false_trigger(self) -> None:
        """Close and rename the recording to mark it as a false wake word trigger.

        Renames session_TIMESTAMP.wav -> false_wake_TIMESTAMP.wav so these files
        can be collected separately for wake word model fine-tuning.
        No-op if inactive.
        """
        if not self._wav_file:
            return
        self._wav_file.close()
        self._wav_file = None
        if self._wav_path and os.path.exists(self._wav_path):
            basename = os.path.basename(self._wav_path)
            new_basename = basename.replace("session_", "false_wake_", 1)
            new_path = os.path.join(_RECORDINGS_DIR, new_basename)
            os.rename(self._wav_path, new_path)
            logger.info("False trigger recording saved: %s", new_path)
        self._wav_path = None
